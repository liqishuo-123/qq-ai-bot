"""Install the optional NapCat guild bridge.

NapCat only loads plugins whose package name is in its internal official
whitelist. This installer copies the bridge and adds its package name to that
whitelist, with a timestamped backup of napcat.mjs. It never runs automatically;
run it without arguments first to see a dry-run, then add --apply.

The NapCat directory is NOT hardcoded: pass --napcat-home or set NAPCAT_HOME.

Usage:
    python scripts/install_napcat_guild_plugin.py --napcat-home "C:\\NapCatQQ"
    python scripts/install_napcat_guild_plugin.py --napcat-home "C:\\NapCatQQ" --apply

这是可选步骤：不装插件也能正常聊天，频道功能会退回官方频道机器人。
"""

from __future__ import annotations

import argparse
import os
import shutil
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "napcat_plugins" / "qq-guild-bridge"
DEFAULT_HOME = Path(os.environ.get("NAPCAT_HOME", "NapCatQQ"))
PLUGIN_ID = "qq-guild-bridge"
ANCHOR = '"napcat-plugin-qce"'


def patch_whitelist(napcat_mjs: Path) -> bool:
    text = napcat_mjs.read_text(encoding="utf-8")
    if f'"{PLUGIN_ID}"' in text:
        return False
    if ANCHOR not in text:
        raise SystemExit("找不到 NapCat 插件白名单锚点，未修改 napcat.mjs")
    backup = napcat_mjs.with_name(f"napcat.mjs.bak-{datetime.now():%Y%m%d-%H%M%S}")
    shutil.copy2(napcat_mjs, backup)
    text = text.replace(ANCHOR, f'{ANCHOR}, "{PLUGIN_ID}"', 1)
    napcat_mjs.write_text(text, encoding="utf-8")
    print(f"已备份：{backup.name}")
    return True


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--napcat-home", type=Path, default=DEFAULT_HOME)
    parser.add_argument("--apply", action="store_true", help="实际写入并修改 napcat.mjs")
    args = parser.parse_args()
    home = args.napcat_home
    target = home / "plugins" / PLUGIN_ID
    if not (home / "napcat.mjs").exists():
        raise SystemExit(f"NapCat 目录不对：{home}")
    print(f"源：{SOURCE}")
    print(f"目标：{target}")
    if not args.apply:
        print("当前是 dry-run；确认后加 --apply。")
        return 0
    target.mkdir(parents=True, exist_ok=True)
    for name in ("index.mjs", "package.json"):
        shutil.copy2(SOURCE / name, target / name)
    changed = patch_whitelist(home / "napcat.mjs")
    print("插件已复制。" + ("白名单已修改。" if changed else "白名单已包含该插件。"))
    print("需要重启 NapCat（或 WebUI 重载插件）后生效；重启可能要求重新扫码。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
