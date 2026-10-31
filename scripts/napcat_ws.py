"""检查/修复 NapCat 的 OneBot WebSocket 配置。

机器人要连上 NapCat，NapCat 里必须有一个 127.0.0.1:3001 的正向 WebSocket 服务端。
这个脚本负责三件事：

  1. 检查（默认）：告诉你配没配好，不改任何文件；
  2. --apply：没有才追加一条，写前备份、写后校验、坏了自动还原；
  3. 全程拒绝在 NapCat 运行时改配置（改了也会被内存里的旧配置覆盖）。

用法：
    python scripts/napcat_ws.py --home "D:\\NapCatQQ\\NapCat.52230.Shell"
    python scripts/napcat_ws.py --home "D:\\NapCatQQ\\NapCat.52230.Shell" --apply
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

PORT = 3001
HOST = "127.0.0.1"

WS_ENTRY = {
    "enable": True,
    "token": "",
    "reportSelfMessage": False,
    "enableForcePushEvent": True,
    "messagePostFormat": "array",
    "name": "QQAIBot",
    "debug": False,
    "host": HOST,
    "port": PORT,
    "heartInterval": 30000,
}


def log(message: str = "") -> None:
    print(message)


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def writable_configs(home: Path) -> list[Path]:
    """onebot11.json 是模板，onebot11_<QQ>.json 是账号级；两个都要看。"""
    config_dir = home / "config"
    if not config_dir.is_dir():
        return []
    return sorted(config_dir.glob("onebot11*.json"))


def find_server(data: dict) -> dict | None:
    network = data.get("network")
    if not isinstance(network, dict):
        return None
    servers = network.get("websocketServers")
    if not isinstance(servers, list):
        return None
    for item in servers:
        if isinstance(item, dict) and str(item.get("port")) == str(PORT):
            return item
    return None


def napcat_running() -> bool:
    try:
        result = subprocess.run(
            ["tasklist", "/fi", "imagename eq QQ.exe", "/nh"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return "QQ.exe" in (result.stdout or "").lower()


def check(home: Path) -> tuple[bool, list[Path]]:
    """返回（是否已配好, 需要处理的文件列表）。"""
    configs = writable_configs(home)
    if not configs:
        log(f"[X] 在 {home / 'config'} 里没找到 onebot11*.json")
        log("    先启动一次 NapCat 并登录 QQ，让它自己生成配置，然后再来。")
        return False, []

    ok = False
    pending: list[Path] = []
    for path in configs:
        try:
            data = read_json(path)
        except (OSError, json.JSONDecodeError) as exc:
            log(f"[!] 读不了 {path.name}：{exc}")
            continue

        server = find_server(data)
        is_account = path.stem != "onebot11"

        if server is None:
            tag = "账号配置" if is_account else "模板配置"
            log(f"[..] {path.name}（{tag}）：没有 {PORT} 端口的服务")
            pending.append(path)
            continue

        enabled = bool(server.get("enable"))
        token = str(server.get("token") or "")
        if enabled:
            log(f"[OK] {path.name}：已有 {PORT} 端口的 WebSocket 服务")
            ok = True
            if token:
                log(f"     注意：这个服务设了 token（长度 {len(token)}），")
                log("     需要在 .env 里填 ONEBOT_ACCESS_TOKEN，否则会连不上。")
        else:
            log(f"[!] {path.name}：有 {PORT} 条目但没启用，去 NapCat 的 WebUI 里打开它")

    return ok, pending


def apply_patch(path: Path) -> bool:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = path.with_name(f"{path.name}.bak-{stamp}")
    original = path.read_text(encoding="utf-8-sig")
    shutil.copy2(path, backup)

    try:
        data = json.loads(original)
    except json.JSONDecodeError as exc:
        log(f"[X] {path.name} 本身就不是合法 JSON，不敢动：{exc}")
        return False

    network = data.setdefault("network", {})
    if not isinstance(network, dict):
        log(f"[X] {path.name} 的 network 字段不是对象，不敢动")
        return False
    servers = network.setdefault("websocketServers", [])
    if not isinstance(servers, list):
        log(f"[X] {path.name} 的 websocketServers 不是数组，不敢动")
        return False

    servers.append(dict(WS_ENTRY))
    text = json.dumps(data, ensure_ascii=False, indent=2)

    try:
        path.write_text(text + "\n", encoding="utf-8", newline="\r\n")
        json.loads(path.read_text(encoding="utf-8-sig"))  # 回读校验
    except (OSError, json.JSONDecodeError) as exc:
        shutil.copy2(backup, path)
        log(f"[X] 写入失败，已还原成原来的文件：{exc}")
        return False

    log(f"[OK] 已写入 {path.name}（备份：{backup.name}）")
    return True


def main(argv: list[str] | None = None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

    parser = argparse.ArgumentParser(description="检查或修复 NapCat 的 OneBot WS 配置")
    parser.add_argument("--home", required=True, help="NapCat 安装目录，含 config 子目录")
    parser.add_argument("--apply", action="store_true", help="缺少 3001 时自动补上（会先备份）")
    args = parser.parse_args(argv)

    home = Path(args.home).expanduser()
    if not home.is_dir():
        log(f"[X] 目录不存在：{home}")
        return 2

    ok, pending = check(home)

    if not pending:
        if ok:
            log()
            log("[OK] 配置没问题，可以直接启动 NapCat。")
            return 0
        log()
        log("[!] 没找到可用的 3001 服务，但每个文件都有需要人工看一眼的问题。")
        return 1

    if not args.apply:
        log()
        log(f"[!] 有 {len(pending)} 个文件缺 {PORT} 端口的服务。")
        log("    加 --apply 可以让脚本自动补上（会先备份）。")
        return 1

    if napcat_running():
        log()
        log("[X] NapCat 正在运行，现在改配置不会生效，退出时还可能把改动覆盖掉。")
        log("    先关掉 NapCat（双击 NapCat 目录里的 KillQQ.bat），再运行本脚本。")
        return 3

    log()
    log("开始写入 ...")
    changed = sum(1 for path in pending if apply_patch(path))
    log()
    if changed:
        log(f"[OK] 共修改 {changed} 个文件。重启 NapCat 后生效。")
        return 0
    log("[X] 一个都没改成，看看上面的报错。")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
