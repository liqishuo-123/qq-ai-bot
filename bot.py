from __future__ import annotations

from pathlib import Path

import nonebot
from nonebot.adapters.onebot.v11 import Adapter

ROOT = Path(__file__).resolve().parent


def main() -> None:
    nonebot.init(_env_file=ROOT / ".env")
    driver = nonebot.get_driver()
    driver.register_adapter(Adapter)
    nonebot.load_plugin("src.plugins.chat")
    nonebot.run()


if __name__ == "__main__":
    main()
