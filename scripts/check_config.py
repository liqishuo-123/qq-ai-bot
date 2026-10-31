"""Validate local config without connecting to QQ.

Usage:
    .\\.venv\\Scripts\\python.exe scripts/check_config.py
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.db import Database  # noqa: E402
from src.persona import load_persona  # noqa: E402
from src.plugin_loader import PluginRegistry  # noqa: E402
from src.settings import get_settings  # noqa: E402


async def main() -> None:
    settings = get_settings()
    print(f"data_dir: {settings.data_dir}")
    print(f"db_path: {settings.db_path}")
    print(f"provider: {settings.llm_provider} ({settings.llm_provider_name})")
    print(f"base_url: {settings.llm_base_url or 'EMPTY'}")
    print(f"llm_key: {'set' if settings.llm_api_key else 'MISSING'}")
    print(f"model: {settings.llm_model or 'EMPTY'}")
    print(f"superusers: {sorted(settings.superuser_ids) or 'EMPTY'}")

    persona = load_persona(settings.persona_file)
    print(f"persona: {persona.display_name} ({persona.id})")

    db = Database(settings.db_path)
    await db.connect()
    await db.close()
    print("sqlite: ok")

    registry = PluginRegistry(settings)
    print("optional plugins:")
    print(registry.summary())


if __name__ == "__main__":
    asyncio.run(main())
