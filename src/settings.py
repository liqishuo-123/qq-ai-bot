from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from src.llm_presets import (
    DEFAULT_PROVIDER_ID,
    PROVIDER_BY_ID,
    guess_provider,
)

BASE_DIR = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    """Application settings loaded from .env.

    NoneBot reads the same file for its own settings (SUPERUSERS, NICKNAME,
    COMMAND_START, ONEBOT_WS_URLS, ...).  This class only holds bot-specific
    configuration.
    """

    model_config = SettingsConfigDict(
        env_file=BASE_DIR / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ---- 模型服务（OpenAI 兼容，任选一家）----
    # LLM_PROVIDER 决定"用哪家的方言发请求"，不只是个标签，必须落盘。
    llm_provider: str = ""
    llm_api_key: str = ""
    llm_base_url: str = ""
    llm_model: str = ""
    llm_timeout: float = 60.0
    # 思考模式的写法：auto 查内置表；其余为配置向导实测出来的结果。
    llm_thinking_style: str = "auto"

    max_output_tokens: int = 120
    temperature: float = 0.9
    thinking: bool = False
    stream_replies: bool = False

    # ---- v0.1 的旧键，仅用于读取兼容 ----
    # 默认值必须为空：如果这里还写着默认地址，下面的合并逻辑会一直命中它，
    # 把用户新选的服务商又改回 DeepSeek。
    deepseek_api_key: str = Field(default="", exclude=True)
    deepseek_base_url: str = Field(default="", exclude=True)
    deepseek_model: str = Field(default="", exclude=True)
    deepseek_timeout: float | None = Field(default=None, exclude=True)

    superusers: list[str] = Field(default_factory=list)
    nickname: list[str] = Field(default_factory=lambda: ["大肥鱼"])
    command_start: list[str] = Field(default_factory=lambda: ["/"])
    admin_token: str = ""

    data_dir: Path = BASE_DIR / "data"
    persona_file: Path = BASE_DIR / "config" / "personas" / "dafeiyu.yaml"
    plugins_file: Path = BASE_DIR / "config" / "plugins.yaml"
    sticker_dir: Path = BASE_DIR / "data" / "stickers"
    brain_dir: Path = BASE_DIR / "data" / "brain"

    context_messages: int = 8
    summary_trigger: int = 30
    summary_keep: int = 12
    max_context_chars: int = 8000

    group_followup_window: int = 90
    group_cooldown: float = 2.0
    reply_max_bubbles: int = 3
    reply_max_chars: int = 500
    reply_bubble_delay: float = 0.35
    typing_delay_min: float = 0.1
    typing_delay_max: float = 0.5

    memory_enabled: bool = True
    memory_extract_every: int = 12
    memory_limit: int = 3
    memory_ttl_days: int = 90

    style_learning_enabled: bool = True
    style_auto_apply: bool = True
    style_group_enabled: bool = True
    style_private_enabled: bool = True
    style_learn_every: int = 50
    style_min_samples: int = 20
    style_max_samples: int = 120
    style_max_per_user: int = 40
    style_min_confidence: float = 0.35
    style_inject_max_chars: int = 700
    style_retention_versions: int = 20

    skills_enabled: bool = True
    autonomy_enabled: bool = True
    autonomy_cache_seconds: int = 45

    explore_enabled: bool = False
    explore_probe_limit: int = 20
    explore_timeout: float = 15.0
    explore_interval_minutes: int = 180
    explore_startup_delay_seconds: int = 60

    brain_enabled: bool = True
    brain_capture_enabled: bool = True
    brain_capture_max_memories: int = 20
    brain_context_chars: int = 700
    brain_search_limit: int = 3
    brain_max_notes: int = 2000
    brain_max_note_bytes: int = 65536

    social_auto_enabled: bool = True
    profile_auto_enabled: bool = False
    profile_auto_apply: bool = True
    profile_update_days: int = 7
    profile_first_delay_minutes: int = 30
    qzone_auto_enabled: bool = False
    qzone_interval_hours: int = 24
    qzone_max_per_day: int = 1
    qzone_first_delay_minutes: int = 60
    qzone_quiet_start: int = 0
    qzone_quiet_end: int = 8
    qzone_ugc_right: int = 1
    qzone_max_chars: int = 300
    qzone_web_enabled: bool = True
    qzone_rate_seconds: float = 3.0
    qzone_comment_hourly_limit: int = 20
    qzone_request_timeout: float = 15.0

    guild_backend: str = "auto"
    guild_id: str = ""
    guild_channel_id: str = ""
    guild_bot_appid: str = ""
    guild_bot_secret: str = ""
    guild_bot_token: str = ""
    guild_timeout: float = 15.0
    guild_rate_seconds: float = 3.0

    image_enabled: bool = True
    image_max_bytes: int = 8_000_000
    image_allow_private_hosts: bool = False

    deai_enabled: bool = True
    deai_light_threshold: int = 20
    deai_heavy_threshold: int = 50
    deai_timeout: float = 8.0
    deai_record_only: bool = False

    feedback_enabled: bool = True
    feedback_observe_seconds: int = 300
    feedback_private_enabled: bool = True

    selfreview_enabled: bool = True
    selfreview_interval_messages: int = 120
    selfreview_min_gap_minutes: int = 30
    selfreview_neg_trigger: int = 3

    style_evolution_brain: bool = True

    sticker_library_enabled: bool = True
    sticker_ingest_enabled: bool = True
    sticker_auto_tag: bool = True
    sticker_auto_send: bool = True
    sticker_auto_chance: float = 0.35
    sticker_cooldown_seconds: int = 300
    sticker_hourly_limit: int = 4
    sticker_daily_limit: int = 12
    sticker_max_count: int = 300
    sticker_max_bytes: int = 3_000_000
    sticker_catalog_limit: int = 30

    daily_token_budget: int = 0
    token_warn_ratio: float = 0.8

    @field_validator(
        "data_dir",
        "persona_file",
        "plugins_file",
        "sticker_dir",
        "brain_dir",
        mode="after",
    )
    @classmethod
    def _resolve_path(cls, value: Path) -> Path:
        if value.is_absolute():
            return value
        return (BASE_DIR / value).resolve()

    @field_validator(
        "style_learn_every",
        "style_min_samples",
        "style_max_samples",
        "style_max_per_user",
        "style_inject_max_chars",
        mode="before",
    )
    @classmethod
    def _positive_style_ints(cls, value: int) -> int:
        return max(1, int(value))

    @field_validator("style_retention_versions", mode="before")
    @classmethod
    def _style_versions(cls, value: int) -> int:
        return max(0, int(value))

    @field_validator("style_min_confidence", mode="before")
    @classmethod
    def _style_confidence(cls, value: float) -> float:
        return max(0.0, min(1.0, float(value)))

    @field_validator(
        "brain_capture_max_memories",
        "brain_context_chars",
        "brain_search_limit",
        "brain_max_notes",
        "brain_max_note_bytes",
        "profile_update_days",
        "profile_first_delay_minutes",
        "qzone_interval_hours",
        "qzone_max_per_day",
        "qzone_first_delay_minutes",
        "qzone_comment_hourly_limit",
        "explore_probe_limit",
        "explore_interval_minutes",
        "explore_startup_delay_seconds",
        "selfreview_interval_messages",
        mode="before",
    )
    @classmethod
    def _positive_social_ints(cls, value: int) -> int:
        return max(1, int(value))

    @field_validator("deai_light_threshold", "deai_heavy_threshold", mode="before")
    @classmethod
    def _deai_thresholds(cls, value: int) -> int:
        return max(0, min(100, int(value)))

    @field_validator("deai_timeout", mode="before")
    @classmethod
    def _deai_timeout(cls, value: float) -> float:
        return max(3.0, min(float(value), 30.0))

    @field_validator("feedback_observe_seconds", mode="before")
    @classmethod
    def _feedback_window(cls, value: int) -> int:
        return max(30, min(int(value), 3600))

    @field_validator("selfreview_min_gap_minutes", mode="before")
    @classmethod
    def _selfreview_gap(cls, value: int) -> int:
        return max(1, min(int(value), 1440))

    @field_validator("selfreview_neg_trigger", mode="before")
    @classmethod
    def _selfreview_neg(cls, value: int) -> int:
        return max(1, min(int(value), 20))

    @field_validator("explore_probe_limit", mode="after")
    @classmethod
    def _explore_limit(cls, value: int) -> int:
        return max(1, min(int(value), 20))

    @field_validator("explore_timeout", mode="before")
    @classmethod
    def _explore_timeout(cls, value: float) -> float:
        return max(3.0, min(float(value), 60.0))

    @field_validator("qzone_quiet_start", "qzone_quiet_end", mode="before")
    @classmethod
    def _quiet_hours(cls, value: int) -> int:
        return max(0, min(23, int(value)))

    @field_validator("qzone_ugc_right", mode="before")
    @classmethod
    def _qzone_ugc_right(cls, value: int) -> int:
        allowed = {1, 4, 64}
        parsed = int(value)
        return parsed if parsed in allowed else 1

    @field_validator("qzone_max_chars", mode="before")
    @classmethod
    def _qzone_chars(cls, value: int) -> int:
        return max(40, min(1000, int(value)))

    @field_validator("qzone_rate_seconds", "guild_rate_seconds", mode="before")
    @classmethod
    def _social_rate(cls, value: float) -> float:
        return max(0.0, min(float(value), 60.0))

    @field_validator("qzone_request_timeout", "guild_timeout", mode="before")
    @classmethod
    def _social_timeout(cls, value: float) -> float:
        return max(3.0, min(float(value), 60.0))

    @field_validator("guild_backend", mode="before")
    @classmethod
    def _guild_backend(cls, value: str) -> str:
        parsed = str(value or "auto").strip().lower()
        return parsed if parsed in {"auto", "plugin", "official", "off"} else "auto"

    @model_validator(mode="after")
    def _style_thresholds(self) -> Settings:
        if self.style_learn_every < self.style_min_samples:
            self.style_learn_every = self.style_min_samples
        if self.style_max_samples < self.style_min_samples:
            self.style_max_samples = self.style_min_samples
        return self

    @field_validator("llm_thinking_style", mode="before")
    @classmethod
    def _llm_thinking_style(cls, value: str) -> str:
        parsed = str(value or "auto").strip().lower()
        return parsed if parsed in {"auto", "type", "enable", "effort", "none"} else "auto"

    @field_validator("llm_timeout", mode="before")
    @classmethod
    def _llm_timeout(cls, value: float) -> float:
        return max(3.0, min(float(value), 300.0))

    @model_validator(mode="after")
    def _merge_llm_config(self) -> Settings:
        """把旧 DEEPSEEK_* 键、provider 预设合并成一套可用的配置。

        顺序很重要：新键 → 旧键 → provider 预设。每一步都只在"还没有值"时才填，
        绝不覆盖用户手写的值。
        """
        # 1) 新键没写，才回头看旧键
        if not self.llm_api_key.strip() and self.deepseek_api_key.strip():
            self.llm_api_key = self.deepseek_api_key.strip()
        if not self.llm_base_url.strip() and self.deepseek_base_url.strip():
            self.llm_base_url = self.deepseek_base_url.strip()
        if not self.llm_model.strip() and self.deepseek_model.strip():
            self.llm_model = self.deepseek_model.strip()
        if self.deepseek_timeout is not None:
            self.llm_timeout = float(self.deepseek_timeout)

        self.llm_api_key = self.llm_api_key.strip()
        self.llm_model = self.llm_model.strip()
        self.llm_base_url = self.llm_base_url.strip().rstrip("/")

        # 2) provider 没写或写错，就从地址反推；地址也没有则沿用历史默认
        provider = self.llm_provider.strip().lower()
        if provider not in PROVIDER_BY_ID:
            provider = guess_provider(self.llm_base_url) if self.llm_base_url else DEFAULT_PROVIDER_ID
        self.llm_provider = provider

        # 3) 地址、模型还空着，才用该服务商的预设兜底
        preset = PROVIDER_BY_ID.get(provider)
        if preset is not None:
            if not self.llm_base_url:
                self.llm_base_url = preset.url
            if not self.llm_model:
                self.llm_model = preset.model
        return self

    @property
    def llm_ready(self) -> bool:
        """地址和模型都齐了才可能发得出请求。"""
        return bool(self.llm_base_url and self.llm_model)

    @property
    def llm_needs_key(self) -> bool:
        """本地 Ollama 这类服务不需要 key。"""
        preset = PROVIDER_BY_ID.get(self.llm_provider)
        return preset.needs_key if preset is not None else True

    @property
    def llm_provider_name(self) -> str:
        preset = PROVIDER_BY_ID.get(self.llm_provider)
        return preset.name if preset is not None else "自定义模型服务"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "bot.db"

    def ensure_dirs(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.sticker_dir.mkdir(parents=True, exist_ok=True)
        self.brain_dir.mkdir(parents=True, exist_ok=True)

    @property
    def superuser_ids(self) -> set[str]:
        return {str(item) for item in self.superusers}


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    settings = Settings()
    settings.ensure_dirs()
    return settings
