"""End-to-end smoke test with a fake NapCat WebSocket and fake DeepSeek API.

This does not touch a real QQ account. It verifies:
- NoneBot connects to a forward OneBot WebSocket.
- First @ activates a group and produces a reply.
- Name mention and same-user follow-up work.
- Group commands enforce permissions and toggle state.
- Private chat / close / open works.
- The bot actually calls the OpenAI-compatible chat endpoint.

Usage:
    .\\.venv\\Scripts\\python.exe scripts/e2e_smoke.py
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import websockets

ROOT = Path(__file__).resolve().parents[1]
BOT_QQ = 10000
GROUP_ID = 30001
USER_A = 20001
USER_B = 20002
USER_OWNER = 99999
WS_PORT = int(os.getenv("E2E_WS_PORT", "3001"))
API_PORT = int(os.getenv("E2E_API_PORT", "18080"))
HTTP_PORT = int(os.getenv("E2E_HTTP_PORT", "18081"))
PNG_BYTES = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


class State:
    def __init__(self) -> None:
        self.connected = asyncio.Event()
        self.connection: Any = None
        self.sent: list[dict[str, Any]] = []
        self.actions: list[dict[str, Any]] = []
        self.llm_requests: list[dict[str, Any]] = []
        self.bot_output: list[str] = []
        self._next_message_id = 9000

    def next_id(self) -> int:
        self._next_message_id += 1
        return self._next_message_id


STATE = State()


def message_text(message: Any) -> str:
    if isinstance(message, str):
        return message
    if isinstance(message, list):
        parts: list[str] = []
        for segment in message:
            if not isinstance(segment, dict):
                continue
            if segment.get("type") == "text":
                parts.append(str(segment.get("data", {}).get("text", "")))
            elif segment.get("type") == "at":
                parts.append(f"@{segment.get('data', {}).get('qq', '')}")
            elif segment.get("type") == "image":
                parts.append("[图片]")
        return "".join(parts)
    return str(message)


class FakeDeepSeek(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        if self.path.split("?", 1)[0] != "/image.png":
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", "image/png")
        self.send_header("Content-Length", str(len(PNG_BYTES)))
        self.end_headers()
        self.wfile.write(PNG_BYTES)

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length)
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            payload = {}
        STATE.llm_requests.append(payload)

        if payload.get("response_format"):
            prompt_text = "\n".join(
                str(message.get("content", ""))
                for message in payload.get("messages", [])
                if isinstance(message, dict)
            )
            if "自主决策器" in prompt_text:
                if "E2E拒绝" in prompt_text:
                    content = json.dumps(
                        {"allow": False, "reason": "这条我不想干"},
                        ensure_ascii=False,
                    )
                else:
                    content = json.dumps(
                        {"allow": True, "reason": "行"},
                        ensure_ascii=False,
                    )
            elif "nickname" in prompt_text and "signature" in prompt_text:
                content = json.dumps(
                    {
                        "nickname": "E2E大肥鱼",
                        "signature": "测试签名",
                        "bio": "这是一条用于端到端测试的主页简介，内容正常。",
                        "tags": ["测试", "摸鱼"],
                        "mood": "在线",
                    },
                    ensure_ascii=False,
                )
            else:
                content = "[]"
        elif payload.get("max_tokens") == 240:
            content = "之前的对话摘要。"
        else:
            last_user = ""
            for message in reversed(payload.get("messages", [])):
                if message.get("role") == "user" and isinstance(message.get("content"), str):
                    last_user = message["content"]
                    break
            if "表情分条" in last_user:
                content = "第一句。\n第二句。[[sticker:1]]"
            elif "分条测试" in last_user:
                content = "第一句。\n第二句。"
            elif "表情测试" in last_user:
                content = "行，给你看。[[sticker:1]]"
            else:
                content = "测试回复"

        if payload.get("stream"):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            for chunk in (content[:2], content[2:]):
                data = {
                    "id": "mock-chat",
                    "model": payload.get("model", "mock"),
                    "choices": [{"index": 0, "delta": {"content": chunk}}],
                }
                self.wfile.write(f"data: {json.dumps(data, ensure_ascii=False)}\n\n".encode())
                self.wfile.flush()
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
            return

        response = {
            "id": "mock-chat",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": payload.get("model", "mock"),
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": content},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        }
        body = json.dumps(response, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        return


def event_base(**extra: Any) -> dict[str, Any]:
    event = {
        "time": int(time.time()),
        "self_id": BOT_QQ,
        "post_type": "message",
        "font": 0,
        "sender": {
            "user_id": USER_A,
            "nickname": "小明",
            "card": "",
            "role": "member",
        },
        "anonymous": None,
        "to_me": False,
    }
    event.update(extra)
    return event


def group_event(
    message_id: int,
    user_id: int,
    text: str,
    *,
    at: bool = False,
    image_url: str | None = None,
    image_sub_type: int | None = None,
    role: str = "member",
) -> dict[str, Any]:
    segments: list[dict[str, Any]] = []
    if at:
        segments.append({"type": "at", "data": {"qq": str(BOT_QQ)}})
    if text:
        segments.append({"type": "text", "data": {"text": text}})
    if image_url:
        image_data: dict[str, Any] = {"url": image_url, "file": "image.png"}
        if image_sub_type is not None:
            image_data["sub_type"] = image_sub_type
        segments.append({"type": "image", "data": image_data})
    sender = {
        "user_id": user_id,
        "nickname": "小明" if user_id == USER_A else "小红",
        "card": "",
        "role": role,
    }
    raw = (f"[CQ:at,qq={BOT_QQ}] " if at else "") + text
    return event_base(
        sub_type="normal",
        user_id=user_id,
        message_type="group",
        message_id=message_id,
        message=segments,
        original_message=segments,
        raw_message=raw,
        sender=sender,
        group_id=GROUP_ID,
        to_me=at,
    )


def private_event(message_id: int, user_id: int, text: str) -> dict[str, Any]:
    segments = [{"type": "text", "data": {"text": text}}] if text else []
    return event_base(
        sub_type="friend",
        user_id=user_id,
        message_type="private",
        message_id=message_id,
        message=segments,
        original_message=segments,
        raw_message=text,
        sender={
            "user_id": user_id,
            "nickname": "小明",
            "card": "",
            "role": "member",
        },
    )


async def fake_napcat(ws: Any) -> None:
    STATE.connection = ws
    STATE.connected.set()
    await ws.send(
        json.dumps(
            {
                "post_type": "meta_event",
                "meta_event_type": "lifecycle",
                "sub_type": "connect",
                "time": int(time.time()),
                "self_id": BOT_QQ,
                "interval": 5000,
            }
        )
    )
    try:
        async for raw in ws:
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError:
                continue
            action = payload.get("action")
            params = payload.get("params") or {}
            if not action:
                continue
            STATE.actions.append({"action": action, "params": params})

            if action in {"send_msg", "send_group_msg", "send_private_msg"}:
                STATE.sent.append(
                    {
                        "action": action,
                        "group_id": params.get("group_id"),
                        "user_id": params.get("user_id"),
                        "text": message_text(params.get("message")),
                    }
                )
                data: dict[str, Any] = {"message_id": STATE.next_id()}
            elif action == "get_supported_actions":
                data = [
                    "get_login_info",
                    "get_version_info",
                    "get_group_list",
                    "get_group_info",
                    "get_group_member_list",
                    "get_group_member_info",
                    "get_group_msg_history",
                    "get_recent_contact",
                    "get_status",
                    "set_group_kick",
                ]
            elif action == "get_login_info":
                data = {"user_id": BOT_QQ, "nickname": "E2E Bot"}
            elif action == "get_version_info":
                data = {"app_name": "NapCat", "app_version": "test", "protocol_version": "v11"}
            elif action == "get_group_member_info":
                user_id = int(params.get("user_id") or 0)
                data = {
                    "user_id": user_id,
                    "role": "owner" if user_id == BOT_QQ else "member",
                    "nickname": "E2E Bot" if user_id == BOT_QQ else "群友",
                }
            elif action == "send_qzone_msg":
                data = {"tid": "e2e-qzone-1"}
            elif action in {
                "set_group_ban",
                "set_group_kick",
                "delete_msg",
                "_send_group_notice",
                "set_group_card",
                "set_group_whole_ban",
                "set_group_admin",
                "set_group_name",
                "set_qq_profile",
                "delete_qzone_msg",
                "set_qq_avatar",
                "set_self_longnick",
                "set_online_status",
                "send_like",
                "send_poke",
                "set_group_portrait",
                "set_group_special_title",
                "send_group_sign",
                "set_essence_msg",
                "del_essence_msg",
            }:
                data = {}
            elif action in {"get_group_list", "get_friend_list"}:
                data = []
            elif action == "get_status":
                data = {"online": True, "good": True}
            else:
                data = {}

            await ws.send(
                json.dumps(
                    {
                        "status": "ok",
                        "retcode": 0,
                        "data": data,
                        "echo": payload.get("echo"),
                    }
                )
            )
    except websockets.ConnectionClosed:
        pass
    finally:
        STATE.connection = None


async def wait_for_send(predicate: Any, *, timeout_seconds: float = 10.0, start: int = 0) -> tuple[dict[str, Any], int]:
    deadline = time.monotonic() + timeout_seconds
    index = start
    while time.monotonic() < deadline:
        while index < len(STATE.sent):
            item = STATE.sent[index]
            index += 1
            if predicate(item):
                return item, index
        await asyncio.sleep(0.05)
    raise TimeoutError(f"Timed out waiting for outgoing message. sent={STATE.sent!r}")


async def send_event(connection: Any, event: dict[str, Any]) -> None:
    await connection.send(json.dumps(event, ensure_ascii=False))


async def main() -> None:
    http_server = ThreadingHTTPServer(("127.0.0.1", API_PORT), FakeDeepSeek)
    http_server.daemon_threads = True
    http_thread = threading.Thread(target=http_server.serve_forever, daemon=True)
    http_thread.start()

    ws_server = await websockets.serve(fake_napcat, "127.0.0.1", WS_PORT)
    data_dir = Path(tempfile.mkdtemp(prefix="qqbot-e2e-"))

    env = os.environ.copy()
    env.update(
        {
            "PYTHONPATH": str(ROOT),
            "PYTHONIOENCODING": "utf-8",
            "PYTHONUTF8": "1",
            "DRIVER": "~fastapi+~websockets",
            "HOST": "127.0.0.1",
            "PORT": str(HTTP_PORT),
            "COMMAND_START": '["/"]',
            "SUPERUSERS": '["99999"]',
            "NICKNAME": '["大肥鱼"]',
            "ADMIN_TOKEN": "e2e-admin-token",
            "LLM_PROVIDER": "deepseek",
            "LLM_API_KEY": "test-key",
            "LLM_BASE_URL": f"http://127.0.0.1:{API_PORT}",
            "LLM_MODEL": "mock-model",
            "THINKING": "false",
            "ONEBOT_WS_URLS": f'["ws://127.0.0.1:{WS_PORT}"]',
            "ONEBOT_ACCESS_TOKEN": "",
            "DATA_DIR": str(data_dir),
            "BRAIN_DIR": str(data_dir / "brain"),
            "MEMORY_ENABLED": "false",
            "IMAGE_ALLOW_PRIVATE_HOSTS": "true",
            "STICKER_DIR": str(data_dir / "stickers"),
            "STICKER_AUTO_TAG": "false",
            "STICKER_AUTO_CHANCE": "1",
            "STICKER_COOLDOWN_SECONDS": "0",
            "SUMMARY_TRIGGER": "100",
            "TYPING_DELAY_MIN": "0",
            "TYPING_DELAY_MAX": "0",
            "GROUP_COOLDOWN": "0",
        }
    )

    proc = subprocess.Popen(
        [sys.executable, "bot.py"],
        cwd=ROOT,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
    )

    def collect_output() -> None:
        assert proc.stdout is not None
        for line in proc.stdout:
            STATE.bot_output.append(line.rstrip())

    output_thread = threading.Thread(target=collect_output, daemon=True)
    output_thread.start()

    try:
        await asyncio.wait_for(STATE.connected.wait(), timeout=20)
        await asyncio.sleep(0.5)
        assert STATE.connection is not None
        cursor = 0

        # 1. First @ activates the group and gets a normal reply.
        await send_event(STATE.connection, group_event(1, USER_A, "在吗", at=True))
        notice, cursor = await wait_for_send(lambda item: "已开启" in item["text"], start=cursor)
        assert notice["group_id"] == GROUP_ID
        reply, cursor = await wait_for_send(lambda item: item["text"] == "测试回复", start=cursor)
        assert reply["group_id"] == GROUP_ID

        # 2. Name mention works without @ after activation.
        await send_event(STATE.connection, group_event(2, USER_A, "大肥鱼 在吗"))
        reply, cursor = await wait_for_send(lambda item: item["text"] == "测试回复", start=cursor)

        # 3. An unrelated group member is ignored in smart mode.
        before = len(STATE.sent)
        await send_event(STATE.connection, group_event(3, USER_B, "今天天气不错"))
        await asyncio.sleep(1.0)
        assert len(STATE.sent) == before, STATE.sent[before:]

        # 4. Same-user follow-up within the window is answered.
        await send_event(STATE.connection, group_event(4, USER_A, "继续"))
        reply, cursor = await wait_for_send(lambda item: item["text"] == "测试回复", start=cursor)

        # 5. Status is readable by a normal member.
        await send_event(STATE.connection, group_event(5, USER_B, "/状态"))
        status, cursor = await wait_for_send(lambda item: "群聊：已开启" in item["text"], start=cursor)

        # 6. Full mode and switching back to smart mode.
        await send_event(STATE.connection, group_event(20, USER_A, "/模式 全量"))
        _, cursor = await wait_for_send(lambda item: "模式切到全量" in item["text"], start=cursor)
        await send_event(STATE.connection, group_event(21, USER_B, "全量测试"))
        _, cursor = await wait_for_send(lambda item: item["text"] == "测试回复", start=cursor)
        await send_event(STATE.connection, group_event(22, USER_A, "/模式 智能"))
        _, cursor = await wait_for_send(lambda item: "模式切到智能" in item["text"], start=cursor)
        before = len(STATE.sent)
        await send_event(STATE.connection, group_event(23, USER_A, "智能模式下的闲聊"))
        await asyncio.sleep(1.0)
        assert len(STATE.sent) == before, STATE.sent[before:]

        # 7. A non-activator cannot close the group.
        await send_event(STATE.connection, group_event(6, USER_B, "/关闭"))
        denied, cursor = await wait_for_send(lambda item: "群主" in item["text"], start=cursor)

        # 8. The activator can close it; ordinary messages are then ignored.
        await send_event(STATE.connection, group_event(7, USER_A, "/关闭"))
        closed, cursor = await wait_for_send(lambda item: "好，我闭嘴" in item["text"], start=cursor)
        before = len(STATE.sent)
        await send_event(STATE.connection, group_event(8, USER_A, "还在吗"))
        await asyncio.sleep(1.0)
        assert len(STATE.sent) == before, STATE.sent[before:]

        # 9. @ re-activates after /关闭.
        await send_event(STATE.connection, group_event(9, USER_A, "回来", at=True))
        await wait_for_send(lambda item: "已开启" in item["text"], start=cursor)
        _, cursor = await wait_for_send(lambda item: item["text"] == "测试回复", start=cursor)

        # 10. An image message is downloaded and attached to the vision request.
        image_url = f"http://127.0.0.1:{API_PORT}/image.png"
        await send_event(STATE.connection, group_event(15, USER_A, "看图", at=True, image_url=image_url))
        await wait_for_send(lambda item: item["text"] == "测试回复", start=cursor)
        vision_requests = [
            request
            for request in STATE.llm_requests
            if isinstance(request["messages"][-1].get("content"), list)
        ]
        assert vision_requests, "vision request was not sent"
        image_parts = [
            part
            for part in vision_requests[-1]["messages"][-1]["content"]
            if isinstance(part, dict) and part.get("type") == "image_url"
        ]
        assert image_parts, vision_requests[-1]
        assert image_parts[0]["image_url"]["url"].startswith("data:image/png;base64,")

        # 10b. Intentional line breaks produce multiple messages, and a saved sticker
        # is attached to one of them instead of creating an extra message.
        image_url = f"http://127.0.0.1:{API_PORT}/image.png"
        await send_event(
            STATE.connection,
            group_event(16, USER_B, "", image_url=image_url, image_sub_type=1),
        )
        await asyncio.sleep(1.0)
        await send_event(STATE.connection, group_event(17, USER_A, "分条测试", at=True))
        first_line, cursor = await wait_for_send(lambda item: item["text"] == "第一句。", start=cursor)
        second_line, cursor = await wait_for_send(lambda item: item["text"] == "第二句。", start=cursor)
        assert first_line["group_id"] == GROUP_ID
        assert second_line["group_id"] == GROUP_ID

        await send_event(STATE.connection, group_event(18, USER_A, "表情测试", at=True))
        sticker_reply, cursor = await wait_for_send(
            lambda item: "行，给你看。" in item["text"] and "[图片]" in item["text"],
            start=cursor,
        )
        assert "[图片]" in sticker_reply["text"]

        await send_event(STATE.connection, group_event(19, USER_A, "表情分条", at=True))
        split_first, cursor = await wait_for_send(lambda item: item["text"] == "第一句。", start=cursor)
        split_second, cursor = await wait_for_send(
            lambda item: "第二句。" in item["text"] and "[图片]" in item["text"],
            start=cursor,
        )
        assert split_first["group_id"] == GROUP_ID
        assert split_second["group_id"] == GROUP_ID
        await asyncio.sleep(0.5)
        assert not any("第一句。" in item["text"] and "[图片]" in item["text"] for item in STATE.sent), STATE.sent

        # 11. Private chat and private close/open.
        await send_event(STATE.connection, private_event(10, USER_A, "你好"))
        private_reply, cursor = await wait_for_send(
            lambda item: item["user_id"] == USER_A and item["text"] == "测试回复",
            start=cursor,
        )
        await send_event(STATE.connection, private_event(11, USER_A, "/关闭"))
        await wait_for_send(lambda item: "我先不回了" in item["text"], start=cursor)
        before = len(STATE.sent)
        await send_event(STATE.connection, private_event(12, USER_A, "还在吗"))
        await asyncio.sleep(1.0)
        assert len(STATE.sent) == before, STATE.sent[before:]
        await send_event(STATE.connection, private_event(13, USER_A, "/开启"))
        await wait_for_send(lambda item: "好，我在" in item["text"], start=cursor)
        await send_event(STATE.connection, private_event(14, USER_A, "回来"))
        await wait_for_send(lambda item: item["text"] == "测试回复", start=cursor)

        # 12. Skill-gated group management, brain, profile and Qzone.
        # /技能 现在输出分类摘要（技能太多），这里等摘要出现即可。
        await send_event(STATE.connection, group_event(30, USER_A, "/技能", role="admin"))
        await wait_for_send(lambda item: "当前可用" in item["text"] and "个技能" in item["text"], start=cursor)

        await send_event(STATE.connection, group_event(31, USER_A, f"/禁言 {USER_B} 10m", role="admin"))
        await wait_for_send(lambda item: "禁言了" in item["text"], start=cursor)
        assert any(
            item["action"] == "set_group_ban"
            and int(item["params"].get("user_id", 0)) == USER_B
            and int(item["params"].get("duration", 0)) == 600
            for item in STATE.actions
        ), STATE.actions

        await send_event(STATE.connection, group_event(32, USER_A, f"/解禁 {USER_B}", role="admin"))
        await wait_for_send(lambda item: "解禁" in item["text"], start=cursor)

        await send_event(
            STATE.connection,
            group_event(33, USER_A, "/大脑 记 E2E笔记 | 大脑里的测试内容", role="admin"),
        )
        await wait_for_send(lambda item: "记下了" in item["text"], start=cursor)
        await send_event(STATE.connection, group_event(34, USER_A, "/大脑 查 E2E笔记", role="admin"))
        brain_reply, cursor = await wait_for_send(
            lambda item: "E2E笔记" in item["text"] and "大脑里的测试内容" in item["text"],
            start=cursor,
        )
        assert "[group:30001]" in brain_reply["text"]

        await send_event(STATE.connection, group_event(35, USER_OWNER, "/主页 优化"))
        await wait_for_send(lambda item: "E2E大肥鱼" in item["text"], start=cursor)
        assert any(item["action"] == "set_qq_profile" for item in STATE.actions), STATE.actions

        await send_event(STATE.connection, group_event(39, USER_OWNER, "/在线 忙碌"))
        await wait_for_send(lambda item: "在线状态" in item["text"], start=cursor)
        await send_event(STATE.connection, group_event(40, USER_OWNER, "/昵称 E2E大鱼"))
        await wait_for_send(lambda item: "昵称改成" in item["text"], start=cursor)
        await send_event(STATE.connection, group_event(41, USER_OWNER, "/签名 测试签名二"))
        await wait_for_send(lambda item: "签名改成" in item["text"], start=cursor)
        await send_event(STATE.connection, group_event(42, USER_OWNER, f"/点赞 {USER_B} 3"))
        await wait_for_send(lambda item: "点了 3 个赞" in item["text"], start=cursor)
        await send_event(STATE.connection, group_event(43, USER_OWNER, f"/戳 {USER_B}"))
        await wait_for_send(lambda item: "戳了" in item["text"], start=cursor)
        await send_event(
            STATE.connection,
            group_event(
                44,
                USER_OWNER,
                "/头像",
                image_url=f"http://127.0.0.1:{API_PORT}/image.png",
            ),
        )
        await wait_for_send(lambda item: "头像换了" in item["text"], start=cursor)
        await send_event(
            STATE.connection,
            group_event(
                45,
                USER_OWNER,
                "/群头像",
                image_url=f"http://127.0.0.1:{API_PORT}/image.png",
            ),
        )
        await wait_for_send(lambda item: "群头像换了" in item["text"], start=cursor)
        await send_event(STATE.connection, group_event(46, USER_OWNER, f"/群头衔 {USER_B} 测试头衔"))
        await wait_for_send(lambda item: "头衔" in item["text"], start=cursor)
        await send_event(STATE.connection, group_event(47, USER_OWNER, "/群签到"))
        await wait_for_send(lambda item: "群打卡" in item["text"], start=cursor)

        await send_event(STATE.connection, group_event(36, USER_OWNER, "/发空间"))
        qzone_reply, cursor = await wait_for_send(lambda item: "e2e-qzone-1" in item["text"], start=cursor)
        assert "测试回复" in qzone_reply["text"]
        await send_event(STATE.connection, group_event(37, USER_OWNER, "/空间"))
        await wait_for_send(lambda item: "e2e-qzone-1" in item["text"], start=cursor)
        await send_event(STATE.connection, group_event(38, USER_OWNER, "/删空间 e2e-qzone-1"))
        await wait_for_send(lambda item: "删了" in item["text"], start=cursor)
        await send_event(STATE.connection, group_event(39, USER_OWNER, "/频道 状态"))
        await wait_for_send(lambda item: "频道后端" in item["text"], start=cursor)

        # 13. Autonomy: bot may refuse, console can override.
        await send_event(
            STATE.connection,
            group_event(48, USER_OWNER, "/大脑 记 E2E拒绝 | 这条不该写进去"),
        )
        await wait_for_send(lambda item: "我不想干" in item["text"], start=cursor)

        def admin_get(path: str) -> Any:
            request = urllib.request.Request(
                f"http://127.0.0.1:{HTTP_PORT}{path}",
                headers={"X-Admin-Token": "e2e-admin-token"},
            )
            with urllib.request.urlopen(request, timeout=10) as response:
                return json.loads(response.read().decode("utf-8"))

        def admin_post(path: str, payload: dict[str, Any]) -> Any:
            request = urllib.request.Request(
                f"http://127.0.0.1:{HTTP_PORT}{path}",
                data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                headers={
                    "X-Admin-Token": "e2e-admin-token",
                    "Content-Type": "application/json",
                },
                method="POST",
            )
            with urllib.request.urlopen(request, timeout=10) as response:
                return json.loads(response.read().decode("utf-8"))

        def admin_put(path: str, payload: dict[str, Any]) -> Any:
            request = urllib.request.Request(
                f"http://127.0.0.1:{HTTP_PORT}{path}",
                data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                headers={
                    "X-Admin-Token": "e2e-admin-token",
                    "Content-Type": "application/json",
                },
                method="PUT",
            )
            with urllib.request.urlopen(request, timeout=10) as response:
                return json.loads(response.read().decode("utf-8"))

        admin_put(
            "/admin/api/autonomy",
            {"enabled": False, "directive": "E2E 控制台放行"},
        )
        await send_event(
            STATE.connection,
            group_event(49, USER_OWNER, "/大脑 记 E2E放行 | 控制台决定优先"),
        )
        await wait_for_send(lambda item: "记下了" in item["text"], start=cursor)

        # 14. Explore mode: read-only probes plus max autonomy.
        admin_put(
            "/admin/api/autonomy",
            {"enabled": True, "directive": "", "mode": "explore"},
        )
        assert admin_get("/admin/api/autonomy")["mode"] == "explore"
        await send_event(STATE.connection, group_event(50, USER_OWNER, "/探索 跑"))
        await wait_for_send(lambda item: "探索了一轮" in item["text"], start=cursor)
        explore = admin_get("/admin/api/explore")
        assert explore["enabled"] is True
        assert any(item["action"] == "get_supported_actions" for item in explore["findings"])

        # 15. Admin APIs render the new panels and keep the old panel working.

        admin_status = admin_get("/admin/api/status")
        assert admin_status["usage_7d"]["totals"]["calls"] > 0
        assert admin_status["brain"]["enabled"] is True
        assert admin_get("/admin/api/usage?days=7")["totals"]["calls"] > 0
        assert any(item["id"] == "group.mute" for item in admin_get("/admin/api/skills")["items"])
        assert admin_get("/admin/api/brain")["items"]
        assert admin_get("/admin/api/presence")["profile"]["nickname"] == "E2E大肥鱼"
        assert admin_get("/admin/api/autonomy")["enabled"] is True
        assert admin_get("/admin/api/autonomy")["mode"] == "explore"
        guild_status = admin_get("/admin/api/guild")
        assert guild_status["backend"] in {"auto", "plugin", "official", "off"}
        assert "plugin_available" in guild_status

        # 统一能力表 / 功能中心 / 风格进化
        actions = admin_get("/admin/api/actions")
        assert actions["total"] >= 100
        assert any(item["executor"] == "onebot" for item in actions["actions"])
        assert any(item["executor"] == "qzone" for item in actions["actions"])
        assert any(item["executor"] == "guild" for item in actions["actions"])
        action_before = len(STATE.actions)
        try:
            # to_thread：同步 urllib 会阻塞本进程事件循环，而 fake NapCat 就跑在
            # 这个循环里；不托管的话 call_api 的响应永远回不来，形成死锁。
            action_run = await asyncio.to_thread(
                admin_post, "/admin/api/actions/api.get_version_info/run", {}
            )
        except Exception as exc:
            tail = "\n".join(STATE.bot_output[-25:])
            raise AssertionError(
                f"action run failed: {exc}\n"
                f"actions seen after start: {STATE.actions[action_before:]}\n"
                f"bot output tail:\n{tail}"
            ) from exc
        assert action_run["ok"] is True
        assert action_run["result"]["app_name"] == "NapCat"
        evolution = admin_get("/admin/api/style/evolution")
        assert evolution["ok"] is True
        assert "deai_enabled" in evolution["config"]
        assert admin_get("/admin/api/style/deai")["ok"] is True
        assert admin_get("/admin/api/style/feedback")["ok"] is True

        with urllib.request.urlopen(f"http://127.0.0.1:{HTTP_PORT}/admin", timeout=10) as response:  # noqa: ASYNC210
            html = response.read().decode("utf-8")
        assert "Token 消耗" in html
        assert "自主权限" in html
        assert "探索模式" in html
        assert "技能权限" in html
        assert "大脑" in html
        assert "主页与空间" in html
        assert "QQ 频道" in html
        assert "功能中心" in html
        assert "风格进化" in html

        # 14. The fake DeepSeek was actually called with the persona.
        assert STATE.llm_requests, "DeepSeek was never called"
        first = STATE.llm_requests[0]
        assert first["messages"][0]["role"] == "system"
        assert "大肥鱼" in first["messages"][0]["content"]
        assert first["max_tokens"] == 120
        assert not first.get("stream")
        assert first["thinking"] == {"type": "disabled"}

        print("E2E OK")
        print(f"group sends: {len([x for x in STATE.sent if x['group_id']])}")
        print(f"private sends: {len([x for x in STATE.sent if x['user_id'] and not x['group_id']])}")
        print(f"llm calls: {len(STATE.llm_requests)}")
        print(f"vision calls: {len(vision_requests)}")
    except Exception:
        print("E2E FAILED")
        print("--- recent bot output ---")
        for line in STATE.bot_output[-80:]:
            print(line)
        print("--- sent messages ---")
        print(json.dumps(STATE.sent, ensure_ascii=False, indent=2))
        raise
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
        output_thread.join(timeout=2)
        ws_server.close()
        await ws_server.wait_closed()
        http_server.shutdown()
        shutil.rmtree(data_dir, ignore_errors=True)


if __name__ == "__main__":
    asyncio.run(main())
