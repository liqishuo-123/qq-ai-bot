"""模型服务商预设。

这份表同时被两处使用：
  - `src/settings.py`：没写 LLM_BASE_URL / LLM_MODEL 时按 provider 兜底；
  - `scripts/setup_wizard.py`：生成选择菜单、连通测试时取默认值和提示。

刻意保持"零第三方依赖"，配置向导才能在依赖没装全时也导入它。

模型名只是**兜底值**。向导会优先拉服务商的 `GET /models`，让用户从真实可用
的模型里选——厂商换代的频率远高于本文件的更新频率，写死一定会过期。
"""

from __future__ import annotations

from dataclasses import dataclass

# thinking（思考模式）方言：
#   type    -> {"thinking": {"type": "disabled" / "enabled"}}
#   enable  -> {"enable_thinking": false / true}
#   effort  -> {"reasoning_effort": "none" / "high"}
#   none    -> 不发任何字段（自定义网关，保守策略）
DIALECT_TYPE = "type"
DIALECT_ENABLE = "enable"
DIALECT_EFFORT = "effort"
DIALECT_NONE = "none"


@dataclass(frozen=True)
class ProviderPreset:
    id: str
    name: str
    url: str
    model: str
    needs_key: bool
    hint: str
    dialect: str
    # 关闭思考时发的字面值。空字符串 = 不发送该字段。
    # 例：Gemini 3/4 关不掉思考，OpenAI 默认就不是思考模型，都不该发。
    thinking_off: str = "disabled"
    # 本地服务没有公网地址，向导会跳过连通性检测。
    local: bool = False


PROVIDERS: tuple[ProviderPreset, ...] = (
    ProviderPreset(
        id="dashscope",
        name="阿里云百炼",
        url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        model="qwen3.6-plus",
        needs_key=True,
        hint="bailian.console.aliyun.com → API-KEY 管理",
        dialect=DIALECT_ENABLE,
        thinking_off="false",
    ),
    ProviderPreset(
        id="deepseek",
        name="DeepSeek 官方",
        url="https://api.deepseek.com",
        model="deepseek-flash",
        needs_key=True,
        hint="platform.deepseek.com/api_keys",
        dialect=DIALECT_TYPE,
    ),
    ProviderPreset(
        id="siliconflow",
        name="硅基流动",
        url="https://api.siliconflow.cn/v1",
        model="",
        needs_key=True,
        hint="cloud.siliconflow.cn → API 密钥（一个 key 通吃多家模型）",
        dialect=DIALECT_ENABLE,
        thinking_off="false",
    ),
    ProviderPreset(
        id="zhipu",
        name="智谱 GLM",
        url="https://open.bigmodel.cn/api/paas/v4",
        model="glm-5.1",
        needs_key=True,
        hint="bigmodel.cn → 用户中心 → API Keys",
        dialect=DIALECT_TYPE,
    ),
    ProviderPreset(
        id="moonshot",
        name="月之暗面 Kimi",
        url="https://api.moonshot.cn/v1",
        model="kimi-k2.6",
        needs_key=True,
        hint="platform.moonshot.cn → API Key 管理",
        dialect=DIALECT_TYPE,
    ),
    ProviderPreset(
        id="ark",
        name="火山方舟（豆包）",
        url="https://ark.cn-beijing.volces.com/api/v3",
        model="doubao-seed-2-0-pro",
        needs_key=True,
        hint="console.volcengine.com/ark → API Key 管理",
        dialect=DIALECT_TYPE,
    ),
    ProviderPreset(
        id="hunyuan",
        name="腾讯混元",
        url="https://api.hunyuan.cloud.tencent.com/v1",
        model="hunyuan-turbos-latest",
        needs_key=True,
        hint="console.cloud.tencent.com → 混元 → API Key",
        dialect=DIALECT_TYPE,
    ),
    ProviderPreset(
        id="minimax",
        name="MiniMax",
        url="https://api.minimaxi.com/v1",
        model="MiniMax-M2.7",
        needs_key=True,
        hint="platform.minimaxi.com → 接口密钥",
        dialect=DIALECT_TYPE,
    ),
    ProviderPreset(
        id="openai",
        name="OpenAI",
        url="https://api.openai.com/v1",
        model="gpt-5.1",
        needs_key=True,
        hint="platform.openai.com/api-keys（国内需要代理或中转）",
        dialect=DIALECT_EFFORT,
        thinking_off="",
    ),
    ProviderPreset(
        id="gemini",
        name="Google Gemini",
        url="https://generativelanguage.googleapis.com/v1beta/openai",
        model="gemini-3.8-flash",
        needs_key=True,
        hint="aistudio.google.com/apikey（有免费额度，国内需要代理）",
        dialect=DIALECT_EFFORT,
        thinking_off="",
    ),
    ProviderPreset(
        id="ollama",
        name="本地 Ollama",
        url="http://127.0.0.1:11434/v1",
        model="",
        needs_key=False,
        hint="不用 key，但要先 ollama pull 一个模型",
        dialect=DIALECT_EFFORT,
        thinking_off="none",
        local=True,
    ),
    ProviderPreset(
        id="custom",
        name="自定义（中转 / OneAPI）",
        url="",
        model="",
        needs_key=True,
        hint="填你自己的 OpenAI 兼容地址，一般以 /v1 结尾",
        dialect=DIALECT_NONE,
        thinking_off="",
    ),
)

PROVIDER_BY_ID: dict[str, ProviderPreset] = {item.id: item for item in PROVIDERS}

DEFAULT_PROVIDER_ID = "deepseek"

# base_url 里出现这些片段就认为属于哪一家，用于老 .env 没写 LLM_PROVIDER 时反推。
BASE_URL_HINTS: tuple[tuple[str, str], ...] = (
    ("dashscope.aliyuncs.com", "dashscope"),
    ("api.deepseek.com", "deepseek"),
    ("siliconflow", "siliconflow"),
    ("bigmodel.cn", "zhipu"),
    ("moonshot", "moonshot"),
    ("volces.com", "ark"),
    ("hunyuan", "hunyuan"),
    ("minimax", "minimax"),
    ("api.openai.com", "openai"),
    ("generativelanguage.googleapis.com", "gemini"),
    ("127.0.0.1", "ollama"),
    ("localhost", "ollama"),
)


def provider_name(provider_id: str) -> str:
    preset = PROVIDER_BY_ID.get(provider_id)
    return preset.name if preset else "自定义模型服务"


def guess_provider(base_url: str) -> str:
    """从 base_url 反推 provider id，推不出来就当自定义。"""
    low = (base_url or "").lower()
    for fragment, provider_id in BASE_URL_HINTS:
        if fragment in low:
            return provider_id
    return "custom"


def thinking_payload(provider_id: str, enabled: bool, *, style: str = "auto") -> dict:
    """把「要不要思考」翻译成各家认识的写法。

    `style` 是向导实测结果（auto / type / enable / effort / none）。实测值优先，
    因为内置表可能比厂商的接口变更慢。
    """
    preset = PROVIDER_BY_ID.get(provider_id)
    dialect = preset.dialect if preset else DIALECT_NONE
    if style != "auto":
        dialect = style

    if dialect == DIALECT_TYPE:
        off_value = preset.thinking_off if preset and preset.thinking_off else "disabled"
        return {"thinking": {"type": "enabled" if enabled else off_value}}
    if dialect == DIALECT_ENABLE:
        return {"enable_thinking": bool(enabled)}
    if dialect == DIALECT_EFFORT:
        if enabled:
            return {"reasoning_effort": "high"}
        # 空字符串表示这家没有「关闭」写法，不发字段就是最弱的思考。
        return {"reasoning_effort": preset.thinking_off} if preset and preset.thinking_off else {}
    return {}
