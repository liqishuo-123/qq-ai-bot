from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass
from typing import Any

import httpx
from nonebot import get_bots
from nonebot.adapters.onebot.v11 import Bot

from .db import Database
from .policy import PolicyViolation, check_public_text
from .settings import Settings

logger = logging.getLogger(__name__)
_ID_RE = re.compile(r"^\d{1,30}$")
_BACKENDS = {"auto", "plugin", "official", "off"}
_OFFICIAL_HOST = "https://api.sgroup.qq.com"
_TOKEN_URL = "https://bots.qq.com/app/getAppAccessToken"


class GuildError(RuntimeError):
    pass


@dataclass(slots=True)
class GuildConfig:
    backend: str
    guild_id: str
    channel_id: str


class OfficialGuildClient:
    """QQ 官方频道机器人 API。

    这是同账号 NapCat 插件之外的兜底。频道里显示的是频道机器人，不是
    登录 NapCat 的那个 QQ 号；需要 appid + secret（或现成 token）。
    """

    def __init__(
        self,
        *,
        appid: str,
        secret: str,
        token: str = "",
        timeout: float = 15.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.appid = appid.strip()
        self.secret = secret.strip()
        self.static_token = token.strip()
        self.timeout = timeout
        self._client = client
        self._owns_client = client is None
        self._token = ""
        self._expires_at = 0.0
        self._lock = asyncio.Lock()

    async def close(self) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None

    async def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self.timeout, follow_redirects=False)
        return self._client

    async def _access_token(self) -> str:
        if self.static_token:
            return self.static_token
        now = time.monotonic()
        if self._token and self._expires_at > now:
            return self._token
        if not self.appid or not self.secret:
            raise GuildError("官方频道机器人未配置 appid/secret")
        async with self._lock:
            if self._token and self._expires_at > now:
                return self._token
            try:
                client = await self._http()
                response = await client.post(
                    _TOKEN_URL,
                    json={"appId": self.appid, "clientSecret": self.secret},
                )
            except httpx.HTTPError as exc:
                raise GuildError(f"官方频道取 token 失败：{exc}") from exc
            if response.status_code >= 400:
                raise GuildError(f"官方频道取 token 失败：HTTP {response.status_code}")
            try:
                payload = response.json()
            except ValueError as exc:
                raise GuildError("官方频道 token 返回不是 JSON") from exc
            token = str((payload or {}).get("access_token") or "").strip()
            if not token:
                raise GuildError("官方频道没有返回 access_token")
            try:
                expires = int((payload or {}).get("expires_in") or 7200)
            except (TypeError, ValueError):
                expires = 7200
            self._token = token
            self._expires_at = now + max(60, expires - 60)
            return token

    async def request(self, method: str, path: str, **kwargs: Any) -> Any:
        if not path.startswith("/"):
            raise GuildError("官方频道路径不合法")
        token = await self._access_token()
        headers = {
            "Authorization": f"QQBot {token}",
            "X-Union-Appid": self.appid,
            "Content-Type": "application/json",
        }
        try:
            client = await self._http()
            response = await client.request(
                method,
                f"{_OFFICIAL_HOST}{path}",
                headers=headers,
                **kwargs,
            )
        except httpx.HTTPError as exc:
            raise GuildError(f"官方频道网络请求失败：{exc}") from exc
        if response.status_code >= 400:
            detail = response.text.strip()[:300]
            raise GuildError(f"官方频道 HTTP {response.status_code}：{detail}")
        if not response.content:
            return None
        try:
            return response.json()
        except ValueError as exc:
            raise GuildError("官方频道返回不是 JSON") from exc

    async def guilds(self) -> list[dict[str, Any]]:
        payload = await self.request("GET", "/users/@me/guilds")
        if isinstance(payload, list):
            return [item for item in payload if isinstance(item, dict)]
        return []

    async def channels(self, guild_id: str) -> list[dict[str, Any]]:
        payload = await self.request("GET", f"/guilds/{guild_id}/channels")
        if isinstance(payload, list):
            return [item for item in payload if isinstance(item, dict)]
        return []

    async def send_message(self, channel_id: str, content: str) -> dict[str, Any]:
        payload = await self.request(
            "POST",
            f"/channels/{channel_id}/messages",
            json={"content": content},
        )
        return payload if isinstance(payload, dict) else {}

    async def messages(self, channel_id: str, limit: int = 20) -> list[dict[str, Any]]:
        payload = await self.request(
            "GET",
            f"/channels/{channel_id}/messages",
            params={"limit": max(1, min(int(limit), 50))},
        )
        if isinstance(payload, list):
            return [item for item in payload if isinstance(item, dict)]
        return []


class GuildService:
    def __init__(
        self,
        settings: Settings,
        db: Database,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.settings = settings
        self.db = db
        self._official: OfficialGuildClient | None = None
        self._client = client
        self._last_write = 0.0
        self._lock = asyncio.Lock()
        self.last_error = ""

    async def close(self) -> None:
        if self._official is not None:
            await self._official.close()
            self._official = None

    def _bot(self) -> Bot:
        bots = get_bots()
        bot = next(iter(bots.values()), None) if bots else None
        if bot is None:
            raise GuildError("NapCat 没有连接")
        return bot

    async def config(self) -> GuildConfig:
        backend = str(await self.db.get_social_state("guild.backend") or self.settings.guild_backend)
        guild_id = str(await self.db.get_social_state("guild.id") or self.settings.guild_id or "")
        channel_id = str(
            await self.db.get_social_state("guild.channel_id") or self.settings.guild_channel_id or ""
        )
        backend = backend.strip().lower()
        if backend not in _BACKENDS:
            backend = "auto"
        return GuildConfig(
            backend=backend,
            guild_id=guild_id.strip(),
            channel_id=channel_id.strip(),
        )

    async def set_config(
        self,
        *,
        backend: str | None = None,
        guild_id: str | None = None,
        channel_id: str | None = None,
    ) -> GuildConfig:
        if backend is not None:
            value = backend.strip().lower()
            if value not in _BACKENDS:
                raise GuildError("频道后端只能是 auto/plugin/official/off")
            await self.db.set_social_state("guild.backend", value)
        if guild_id is not None:
            value = guild_id.strip()
            if value and not _ID_RE.fullmatch(value):
                raise GuildError("guild_id 格式不对")
            await self.db.set_social_state("guild.id", value)
        if channel_id is not None:
            value = channel_id.strip()
            if value and not _ID_RE.fullmatch(value):
                raise GuildError("channel_id 格式不对")
            await self.db.set_social_state("guild.channel_id", value)
        return await self.config()

    @property
    def official_configured(self) -> bool:
        return bool(
            self.settings.guild_bot_token
            or (self.settings.guild_bot_appid and self.settings.guild_bot_secret)
        )

    def _official_client(self) -> OfficialGuildClient:
        if not self.official_configured:
            raise GuildError("官方频道机器人未配置 GUILD_BOT_APPID/SECRET 或 TOKEN")
        if self._official is None:
            self._official = OfficialGuildClient(
                appid=self.settings.guild_bot_appid,
                secret=self.settings.guild_bot_secret,
                token=self.settings.guild_bot_token,
                timeout=float(self.settings.guild_timeout),
                client=self._client,
            )
        return self._official

    async def _plugin_call(self, bot: Bot, action: str, **params: Any) -> Any:
        try:
            return await asyncio.wait_for(
                bot.call_api(action, **params),
                timeout=float(self.settings.guild_timeout),
            )
        except Exception as exc:
            raise GuildError(f"NapCat 频道插件调用失败：{exc}") from exc

    async def plugin_available(self, bot: Bot | None = None) -> bool:
        active = bot or self._bot()
        try:
            result = await self._plugin_call(active, "nc_guild_status")
        except GuildError:
            return False
        return bool(result.get("available")) if isinstance(result, dict) else bool(result)

    async def status(self, bot: Bot | None = None, *, check_plugin: bool = True) -> dict[str, Any]:
        config = await self.config()
        plugin = False
        plugin_error = ""
        if bot is None and check_plugin:
            bots = get_bots()
            bot = next(iter(bots.values()), None) if bots else None
        if bot is not None and check_plugin:
            try:
                plugin = await self.plugin_available(bot)
            except Exception as exc:
                plugin_error = str(exc)
        return {
            "backend": config.backend,
            "guild_id": config.guild_id,
            "channel_id": config.channel_id,
            "configured": bool(config.guild_id and config.channel_id),
            "plugin_available": plugin,
            "plugin_checked": bool(check_plugin),
            "plugin_error": plugin_error,
            "official_configured": self.official_configured,
            "official_appid": self.settings.guild_bot_appid,
            "last_error": self.last_error,
        }

    async def _before_write(self) -> None:
        rate = float(self.settings.guild_rate_seconds)
        async with self._lock:
            wait = rate - (time.monotonic() - self._last_write)
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_write = time.monotonic()

    async def send(
        self,
        bot: Bot | None,
        content: str,
        *,
        channel_id: str | None = None,
    ) -> dict[str, Any]:
        config = await self.config()
        target = (channel_id or config.channel_id).strip()
        if not _ID_RE.fullmatch(target):
            raise GuildError("没有可用的 channel_id，请先 /频道 设置")
        if not config.guild_id:
            raise GuildError("没有可用的 guild_id，请先 /频道 设置")
        try:
            text = check_public_text(content.strip(), max_chars=2000)
        except PolicyViolation as exc:
            raise GuildError(str(exc)) from exc
        if config.backend == "off":
            raise GuildError("频道功能已关闭")
        await self._before_write()
        plugin_error: Exception | None = None
        if config.backend in {"auto", "plugin"}:
            active_bot = bot or self._bot()
            try:
                result = await self._plugin_call(
                    active_bot,
                    "nc_guild_send",
                    guild_id=config.guild_id,
                    channel_id=target,
                    content=text,
                )
                if isinstance(result, dict) and result.get("ok") is False:
                    raise GuildError(str(result.get("error") or "频道插件返回失败"))
                self.last_error = ""
                return {
                    "backend": "plugin",
                    "guild_id": config.guild_id,
                    "channel_id": target,
                    "content": text,
                    "message_id": str((result or {}).get("message_id") or "") if isinstance(result, dict) else "",
                }
            except GuildError as exc:
                plugin_error = exc
                self.last_error = str(exc)
                if config.backend == "plugin" or not self.official_configured:
                    raise
        if not self.official_configured:
            raise GuildError(str(plugin_error or "频道后端不可用：没有 NapCat 插件，也没有官方机器人配置"))
        result = await self._official_client().send_message(target, text)
        self.last_error = ""
        return {
            "backend": "official",
            "guild_id": config.guild_id,
            "channel_id": target,
            "content": text,
            "message_id": str(result.get("id") or ""),
        }

    async def history(
        self,
        bot: Bot | None,
        *,
        channel_id: str | None = None,
        limit: int = 20,
    ) -> dict[str, Any]:
        config = await self.config()
        target = (channel_id or config.channel_id).strip()
        if not _ID_RE.fullmatch(target):
            raise GuildError("没有可用的 channel_id，请先 /频道 设置")
        if config.backend in {"auto", "plugin"}:
            active_bot = bot or self._bot()
            try:
                result = await self._plugin_call(
                    active_bot,
                    "nc_guild_history",
                    guild_id=config.guild_id,
                    channel_id=target,
                    limit=max(1, min(int(limit), 50)),
                )
                if isinstance(result, dict):
                    return {"backend": "plugin", **result}
            except GuildError as exc:
                self.last_error = str(exc)
                if config.backend == "plugin" or not self.official_configured:
                    raise
        if not self.official_configured:
            raise GuildError("频道历史需要 NapCat 插件或官方机器人")
        items = await self._official_client().messages(target, limit=limit)
        return {"backend": "official", "messages": items}

    async def list_channels(self, bot: Bot | None = None) -> dict[str, Any]:
        config = await self.config()
        if config.backend in {"auto", "plugin"}:
            active_bot = bot or self._bot()
            try:
                result = await self._plugin_call(
                    active_bot,
                    "nc_guild_list",
                    guild_id=config.guild_id,
                    channel_id=config.channel_id,
                )
                if isinstance(result, dict) and result.get("channels"):
                    return {"backend": "plugin", **result}
            except GuildError as exc:
                self.last_error = str(exc)
                if config.backend == "plugin" and not self.official_configured:
                    raise
        if not self.official_configured:
            if config.guild_id and config.channel_id:
                return {
                    "backend": "plugin",
                    "guilds": [],
                    "channels": [
                        {
                            "id": config.channel_id,
                            "name": "配置的频道",
                            "guild_id": config.guild_id,
                        }
                    ],
                }
            raise GuildError("频道列表需要 NapCat 插件或官方机器人")
        official = self._official_client()
        guilds = await official.guilds()
        channels: list[dict[str, Any]] = []
        for guild in guilds:
            guild_id = str(guild.get("id") or "")
            if not guild_id:
                continue
            if config.guild_id and guild_id != config.guild_id:
                continue
            try:
                channels.extend(await official.channels(guild_id))
            except GuildError:
                continue
        return {"backend": "official", "guilds": guilds, "channels": channels}
