from __future__ import annotations

import importlib.metadata
import importlib.util
import json
import logging
import platform
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

import nonebot
import yaml
from packaging.requirements import InvalidRequirement, Requirement
from packaging.specifiers import InvalidSpecifier, SpecifierSet

from .settings import Settings

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class PluginSpec:
    name: str
    module: str
    dist: str
    enabled: bool = False
    optional: bool = True
    requires_python: str = ""
    requires_nonebot: str = ""


@dataclass(slots=True)
class PluginState:
    name: str
    module: str
    status: str
    reason: str = ""
    version: str = ""
    updated_at: str = ""


class PluginRegistry:
    """Load optional plugins one by one.

    A broken/old plugin is marked disabled and skipped.  Core chat never
    imports these plugins, so a plugin failure cannot stop the bot from
    starting.
    """

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.state_path = settings.data_dir / "plugin_state.json"
        self.specs = self._load_specs(settings.plugins_file)
        self.states: dict[str, PluginState] = self._load_state()

    @staticmethod
    def _load_specs(path: Path) -> list[PluginSpec]:
        if not path.is_file():
            return []
        try:
            raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except (OSError, yaml.YAMLError) as exc:
            logger.error("Failed to read plugin config %s: %s", path, exc)
            return []
        if not isinstance(raw, dict):
            logger.error("Plugin config must be a mapping: %s", path)
            return []
        items = raw.get("plugins") or []
        if not isinstance(items, list):
            logger.error("Plugin config field 'plugins' must be a list: %s", path)
            return []
        specs: list[PluginSpec] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            module = str(item.get("module") or "").strip()
            if not module:
                continue
            specs.append(
                PluginSpec(
                    name=str(item.get("name") or module),
                    module=module,
                    dist=str(item.get("dist") or module.replace("_", "-")),
                    enabled=bool(item.get("enabled", False)),
                    optional=bool(item.get("optional", True)),
                    requires_python=str(item.get("requires_python") or ""),
                    requires_nonebot=str(item.get("requires_nonebot") or ""),
                )
            )
        return specs

    def _load_state(self) -> dict[str, PluginState]:
        if not self.state_path.is_file():
            return {}
        try:
            raw = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        if not isinstance(raw, dict):
            return {}
        result: dict[str, PluginState] = {}
        for name, item in raw.items():
            if not isinstance(item, dict):
                continue
            result[name] = PluginState(
                name=name,
                module=str(item.get("module", "")),
                status=str(item.get("status", "unknown")),
                reason=str(item.get("reason", "")),
                version=str(item.get("version", "")),
                updated_at=str(item.get("updated_at", "")),
            )
        return result

    def _save_state(self) -> None:
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            payload = {name: asdict(state) for name, state in self.states.items()}
            self.state_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError:
            logger.exception("Failed to save plugin state")

    @staticmethod
    def _now() -> str:
        return datetime.now(UTC).isoformat(timespec="seconds")

    def _set_state(self, spec: PluginSpec, status: str, reason: str = "", version: str = "") -> None:
        self.states[spec.name] = PluginState(
            name=spec.name,
            module=spec.module,
            status=status,
            reason=reason,
            version=version,
            updated_at=self._now(),
        )

    def _check(self, spec: PluginSpec) -> str:
        if spec.requires_python:
            try:
                if not SpecifierSet(spec.requires_python).contains(platform.python_version()):
                    raise RuntimeError(
                        f"需要 Python {spec.requires_python}，当前是 {platform.python_version()}"
                    )
            except InvalidSpecifier as exc:
                raise RuntimeError(f"requires_python 配置无效：{spec.requires_python}") from exc

        try:
            nb_version = importlib.metadata.version("nonebot2")
        except importlib.metadata.PackageNotFoundError as exc:
            raise RuntimeError("没有安装 nonebot2") from exc

        if spec.requires_nonebot:
            try:
                if not SpecifierSet(spec.requires_nonebot).contains(nb_version):
                    raise RuntimeError(f"需要 NoneBot {spec.requires_nonebot}，当前是 {nb_version}")
            except InvalidSpecifier as exc:
                raise RuntimeError(f"requires_nonebot 配置无效：{spec.requires_nonebot}") from exc

        try:
            version = importlib.metadata.version(spec.dist)
        except importlib.metadata.PackageNotFoundError as exc:
            raise RuntimeError(f"未安装 {spec.dist}") from exc

        # 再读一次包元数据里的 Requires-Python / nonebot2 要求，防止配置写错。
        try:
            metadata = importlib.metadata.metadata(spec.dist)
            requires_python = metadata.get("Requires-Python")
            if requires_python and not SpecifierSet(requires_python).contains(platform.python_version()):
                raise RuntimeError(f"{spec.dist} 要求 Python {requires_python}")
            for requirement_text in metadata.get_all("Requires-Dist") or []:
                try:
                    requirement = Requirement(requirement_text)
                except InvalidRequirement:
                    continue
                if requirement.name.lower() in {"nonebot2", "nonebot"} and not requirement.specifier.contains(nb_version):
                    raise RuntimeError(f"{spec.dist} 要求 NoneBot {requirement.specifier}")
        except importlib.metadata.PackageNotFoundError:
            pass

        if importlib.util.find_spec(spec.module) is None:
            raise RuntimeError(f"找不到模块 {spec.module}")
        return version

    def load_all(self) -> None:
        for spec in self.specs:
            if not spec.enabled:
                self._set_state(spec, "disabled", "配置里 enabled=false")
                continue
            try:
                version = self._check(spec)
                plugin = nonebot.load_plugin(spec.module)
                if plugin is None:
                    raise RuntimeError("NoneBot 返回了空插件对象")
                self._set_state(spec, "loaded", "", version)
                logger.info("Optional plugin loaded: %s %s", spec.name, version)
            except Exception as exc:
                self._set_state(spec, "disabled", str(exc))
                if spec.optional:
                    logger.warning("Optional plugin skipped: %s (%s)", spec.name, exc)
                else:
                    logger.exception("Required plugin failed: %s", spec.name)
        self._save_state()

    def list_states(self) -> list[PluginState]:
        return [self.states[spec.name] for spec in self.specs if spec.name in self.states]

    def summary(self) -> str:
        states = self.list_states()
        if not states:
            return "没有配置可选插件。"
        lines = []
        for state in states:
            suffix = f"（{state.reason}）" if state.reason else ""
            lines.append(f"- {state.name}: {state.status}{suffix}")
        return "\n".join(lines)
