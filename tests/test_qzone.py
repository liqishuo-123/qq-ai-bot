from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pytest

from src.qzone import QzoneError, QzoneService, _parse_json
from src.settings import Settings

COOKIE = "uin=o10001; p_skey=secret-p_skey; skey=secret-skey"
FEED = {
    "code": 0,
    "data": {
        "msglist": [
            {
                "tid": "tid-001",
                "unikey": "unikey-001",
                "content": "今天摸鱼<br/>很开心 &amp; 很饱",
                "created_time": 1760000000,
                "pic": [{"url1": "https://example.com/a.jpg"}],
                "likes": [{"uin": 10001}, {"uin": 20002}],
                "commentlist": [
                    {
                        "commentid": "c-1",
                        "uin": 20002,
                        "name": "小红",
                        "content": "带我一个",
                        "create_time": 1760000100,
                    }
                ],
            }
        ]
    },
}


class FakeBot:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def call_api(self, action: str, **kwargs: Any) -> Any:
        self.calls.append((action, kwargs))
        if action == "get_login_info":
            return {"user_id": 10001, "nickname": "大肥鱼"}
        if action == "get_cookies":
            return {"cookies": COOKIE, "bkn": "123456"}
        if action == "get_csrf_token":
            return {"token": 123456}
        if action == "send_qzone_msg":
            return {"tid": "tid-new"}
        return {}


def make_settings(tmp_path: Path, **overrides: Any) -> Settings:
    values = {
        "_env_file": None,
        "data_dir": tmp_path,
        "qzone_rate_seconds": 0,
        "qzone_comment_hourly_limit": 100,
        "qzone_request_timeout": 5,
    }
    values.update(overrides)
    return Settings(**values)


def make_client(handler: Any) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


@pytest.mark.asyncio
async def test_qzone_list_parses_posts(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        assert request.url.host == "user.qzone.qq.com"
        if request.url.path.endswith("emotion_cgi_msglist_v6"):
            return httpx.Response(200, json=FEED)
        return httpx.Response(404)

    client = make_client(handler)
    service = QzoneService(make_settings(tmp_path), client=client)
    bot = FakeBot()
    try:
        posts = await service.list_posts(bot, limit=10)
        assert len(posts) == 1
        post = posts[0]
        assert post.tid == "tid-001"
        assert post.unikey == "unikey-001"
        assert post.content == "今天摸鱼 很开心 & 很饱"
        assert post.images == ["https://example.com/a.jpg"]
        assert post.like_count == 2
        assert post.liked is True
        assert post.comments[0].comment_id == "c-1"
        assert post.comments[0].nickname == "小红"
        assert seen
        assert "p_skey=secret-p_skey" not in caplog.text
        assert "secret-skey" not in caplog.text
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_qzone_like_comment_and_delete_comment(tmp_path: Path) -> None:
    forms: list[dict[str, list[str]]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            forms.append(dict(httpx.QueryParams(request.content.decode("utf-8"))))
        path = request.url.path
        if path.endswith("internal_dolike_app"):
            return httpx.Response(200, json={"ret": 0, "msg": "success"})
        if path.endswith("emotion_cgi_re_feeds"):
            return httpx.Response(200, json={"ret": 0, "commentid": "c-9"})
        if path.endswith("emotion_cgi_delcomment"):
            return httpx.Response(200, json={"ret": 0})
        return httpx.Response(404)

    client = make_client(handler)
    service = QzoneService(make_settings(tmp_path), client=client)
    bot = FakeBot()
    try:
        liked = await service.set_like(bot, tid="tid-001", unikey="unikey-001", active=True)
        assert liked["active"] is True
        assert forms[-1]["active"] == "0"
        assert forms[-1]["unikey"] == "unikey-001"

        await service.set_like(bot, tid="tid-001", unikey="unikey-001", active=False)
        assert forms[-1]["active"] == "1"

        result = await service.comment(bot, tid="tid-001", content="来啦")
        assert result["comment_id"] == "c-9"
        assert forms[-1]["content"] == "来啦"

        await service.delete_comment(bot, tid="tid-001", comment_id="c-9")
        assert forms[-1]["commentId"] == "c-9"
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_qzone_rejects_unsafe_comment_and_missing_unikey(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("emotion_cgi_msglist_v6"):
            return httpx.Response(200, json={"code": 0, "data": {"msglist": []}})
        return httpx.Response(200, json={"ret": 0})

    client = make_client(handler)
    service = QzoneService(make_settings(tmp_path), client=client)
    bot = FakeBot()
    try:
        with pytest.raises(QzoneError):
            await service.comment(bot, tid="tid-001", content="看 https://example.com")
        with pytest.raises(QzoneError):
            await service.set_like(bot, tid="tid-001", active=True)
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_qzone_http_error_is_readable(tmp_path: Path) -> None:
    client = make_client(
        lambda request: httpx.Response(403, text="forbidden")
    )
    service = QzoneService(make_settings(tmp_path), client=client)
    bot = FakeBot()
    try:
        with pytest.raises(QzoneError) as caught:
            await service.list_posts(bot)
        assert "登录态" in str(caught.value) or "HTTP 403" in str(caught.value)
    finally:
        await service.close()


def test_qzone_html_callback_and_computed_unikey() -> None:
    payload = _parse_json(
        '<html><body><script>frameElement.callback( {"ret":0, "msg":"succ" } );</script></body></html>'
    )
    assert payload["ret"] == 0
    post = QzoneService._parse_post(
        {"tid": "tid-001", "content": "test"},
        self_uin="10001",
        owner_uin="20002",
    )
    assert post is not None
    assert post.unikey == "http://user.qzone.qq.com/20002/mood/tid-001"
