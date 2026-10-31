"""QQ AI Bot 交互式配置向导。

由「一键启动.bat」自动调用，也可以单独运行：

    .\\.venv\\Scripts\\python.exe scripts\\setup_wizard.py
    .\\.venv\\Scripts\\python.exe scripts\\setup_wizard.py --check

它只做一件事：把 .env 里那几个最容易写错的配置问清楚，然后按正确格式写回去。

为什么需要它：
  - SUPERUSERS / NICKNAME 必须是 JSON 数组（["123456"]），写成裸值会让
    nonebot.init() 直接抛 SettingsError，程序起不来；
  - 各家模型服务的接口地址、模型名都不一样，手填很容易漏 /v1 或者用过期的模型名。

设计约束（刻意为之）：
  - 只用标准库，不依赖项目依赖，坏了也能跑；
  - 逐行替换，注释、空行、未知配置原样保留，不重排整个文件；
  - 写前备份，写后自检，失败自动回滚；
  - 已有 ADMIN_TOKEN 不覆盖，避免浏览器里存的登录态失效。
"""

from __future__ import annotations

import argparse
import gzip
import json
import re
import secrets
import shutil
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterable, Sequence
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.llm_presets import (  # noqa: E402
    DEFAULT_PROVIDER_ID,
    PROVIDER_BY_ID,
    PROVIDERS,
    ProviderPreset,
    guess_provider,
)

ENV_PATH = ROOT / ".env"
EXAMPLE_PATH = ROOT / ".env.example"

PLACEHOLDER_QQ = "你的QQ号"

# 旧版键名 → 新键名。写入时把这四行改成注释，不删除：
# 老用户回退到旧版本时 .env.bak-* 还在，不至于两眼一抹黑。
LEGACY_KEYS: dict[str, str] = {
    "DEEPSEEK_API_KEY": "LLM_API_KEY",
    "DEEPSEEK_BASE_URL": "LLM_BASE_URL",
    "DEEPSEEK_MODEL": "LLM_MODEL",
    "DEEPSEEK_TIMEOUT": "LLM_TIMEOUT",
}

# 新键往哪儿插：老 .env 没有 LLM_* 时，插到旧键前面，避免追加到文件末尾。
LLM_ANCHORS: tuple[str, ...] = ("# 模型服务商", "LLM_PROVIDER", "DEEPSEEK_API_KEY", "# DeepSeek")

# 模型列表里的噪音：向量、语音、审核这些填进去也没法聊天。
_NOISE_PARTS = ("embed", "rerank", "whisper", "tts", "asr", "moderation", "image", "video")
# 聊天模型常见的字样，排前面，少翻几屏。
_PREFER_PARTS = ("chat", "instruct", "flash", "pro", "max", "plus")


# --------------------------------------------------------------------------- #
# 输出
# --------------------------------------------------------------------------- #
def _setup_stdio() -> None:
    """Windows 控制台默认 GBK，强制 UTF-8 才能正常显示中文。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


def _line(char: str = "=", width: int = 52) -> None:
    print(char * width)


def _title(text: str) -> None:
    print()
    _line()
    print(f"  {text}")
    _line()


# --------------------------------------------------------------------------- #
# .env 读写（逐行，保留注释与顺序）
# --------------------------------------------------------------------------- #
def load_lines() -> list[str]:
    """读现有 .env；没有就从 .env.example 复制一份作为基础。"""
    if ENV_PATH.exists():
        return ENV_PATH.read_text(encoding="utf-8").splitlines()
    if EXAMPLE_PATH.exists():
        return EXAMPLE_PATH.read_text(encoding="utf-8").splitlines()
    return []


def get_value(lines: list[str], key: str) -> str | None:
    pattern = re.compile(rf"^\s*{re.escape(key)}\s*=\s*(.*)$")
    for line in lines:
        match = pattern.match(line)
        if match:
            return match.group(1).strip()
    return None


def get_first(lines: list[str], keys: Iterable[str]) -> str:
    """按顺序取第一个非空的键值，用于「新键优先、旧键兜底」。"""
    for key in keys:
        value = (get_value(lines, key) or "").strip()
        if value:
            return value
    return ""


def _find_anchor(lines: list[str], anchors: Sequence[str]) -> int | None:
    for anchor in anchors:
        for index, line in enumerate(lines):
            if anchor.startswith("#"):
                if line.strip().startswith(anchor):
                    return index
            elif re.match(rf"^\s*{re.escape(anchor)}\s*=", line):
                return index
    return None


def set_value(
    lines: list[str],
    key: str,
    value: str,
    *,
    anchor: Sequence[str] | str | None = None,
    before: bool = False,
) -> list[str]:
    """替换第一处 KEY=...，并删掉重复的。

    键不存在时插到 anchor 附近，而不是无脑追加到文件末尾——否则每跑一次向导，
    .env 就多一截脱离分组的配置（现有 .env 里 ADMIN_TOKEN 跑到第 74 行就是这么来的）。
    """
    pattern = re.compile(rf"^\s*{re.escape(key)}\s*=")
    out: list[str] = []
    replaced = False
    for line in lines:
        if pattern.match(line):
            if not replaced:
                out.append(f"{key}={value}")
                replaced = True
            continue
        out.append(line)
    if replaced:
        return out

    if anchor:
        anchors = (anchor,) if isinstance(anchor, str) else tuple(anchor)
        index = _find_anchor(out, anchors)
        if index is not None:
            out.insert(index if before else index + 1, f"{key}={value}")
            return out

    out.append(f"{key}={value}")
    return out


def comment_out(lines: list[str], keys: Iterable[str]) -> list[str]:
    """把旧版配置行改成注释。值不保留，避免 .env 里出现两份 key。"""
    names = {key.upper(): key for key in keys}
    pattern = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=")
    out: list[str] = []
    for line in lines:
        if line.lstrip().startswith("#"):
            out.append(line)
            continue
        match = pattern.match(line)
        if match and match.group(1).upper() in names:
            name = match.group(1).upper()
            out.append(f"# {name} 已迁移到 {LEGACY_KEYS[name]}，保留此行只为兼容旧版本")
            continue
        out.append(line)
    return out


def parse_json_list(raw: str | None) -> list[str]:
    """容错解析数组配置。裸值也认，用于给向导提供默认值。"""
    if not raw:
        return []
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return [raw.strip().strip("\"'")] if raw.strip() else []
    if isinstance(value, list):
        return [str(item) for item in value if str(item).strip()]
    return [str(value)]


def as_json_list(values: list[str]) -> str:
    """生成 NoneBot 需要的 JSON 数组写法。"""
    return json.dumps(values, ensure_ascii=False)


# --------------------------------------------------------------------------- #
# 输入
# --------------------------------------------------------------------------- #
def ask(prompt: str, default: str = "", *, allow_empty: bool = False) -> str:
    suffix = f" [{default}]" if default else ""
    while True:
        try:
            raw = input(f"{prompt}{suffix}: ").strip()
        except EOFError:
            return default
        if not raw:
            if default:
                return default
            if allow_empty:
                return ""
            print("  这一项不能为空，请重新输入。")
            continue
        return raw


def ask_qq(default: str = "") -> str:
    while True:
        value = ask("你的 QQ 号（当管理员用，不是机器人号）", default)
        if re.fullmatch(r"\d{5,12}", value):
            return value
        print("  只能是 5~12 位数字，比如 12345678。多试一次。")


def ask_provider(current: str = "") -> str:
    print("  用哪家模型服务？一个 key 只能在对应的那家使用。")
    for index, preset in enumerate(PROVIDERS, 1):
        mark = "   ← 当前在用" if preset.id == current else ""
        print(f"    [{index:>2}] {preset.name}{mark}")
    print("         选错了没关系，以后双击「配置向导.bat」随时能改。")

    default = str(next((i for i, p in enumerate(PROVIDERS, 1) if p.id == current), 2))
    while True:
        choice = ask("  选择", default)
        if choice.isdigit() and 1 <= int(choice) <= len(PROVIDERS):
            return PROVIDERS[int(choice) - 1].id
        if choice in PROVIDER_BY_ID:
            return choice
        print(f"  请输入 1~{len(PROVIDERS)} 之间的数字。")


def ask_key(preset: ProviderPreset, current: str = "") -> str:
    if not preset.needs_key:
        print(f"  {preset.name} 不需要 API Key，跳过。")
        return ""
    print(f"  API Key 去哪里拿：{preset.hint}")
    if current:
        print("  （已配置好，直接回车保留原值）")
    value = ask("  粘贴 Key", "", allow_empty=True)
    return value.strip().strip("\"'") if value else current


def ask_base_url(preset: ProviderPreset, current: str = "") -> str:
    default = current or preset.url
    while True:
        value = ask("  接口地址（一般以 /v1 结尾）", default)
        if re.match(r"^https?://", value):
            return value.rstrip("/")
        print("  必须以 http:// 或 https:// 开头。")


# --------------------------------------------------------------------------- #
# 联网探测（标准库）
# --------------------------------------------------------------------------- #
def _open(request: urllib.request.Request, timeout: float):
    """打开请求。本地地址强制绕过系统代理。"""
    host = (urllib.parse.urlsplit(request.full_url).hostname or "").lower()
    if host in {"127.0.0.1", "localhost", "::1"}:
        # 本机服务走系统代理会被拦成 502，Ollama 就永远测不通。
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        return opener.open(request, timeout=timeout)
    return urllib.request.urlopen(request, timeout=timeout)


def _request_json(
    url: str, key: str, *, payload: dict | None = None, timeout: float = 8.0
) -> tuple[int, object | None]:
    """返回 (状态码, 解析后的 JSON)。状态码 0 = 连不上，-1 = 通了但不是 JSON。"""
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(url, data=data, method="POST" if data else "GET")
    request.add_header("Accept", "application/json")
    # 必须设 UA：Python 默认的 Python-urllib/3.x 会被部分 CDN 直接 403。
    request.add_header("User-Agent", "qq-ai-bot-setup/1.0")
    if data:
        request.add_header("Content-Type", "application/json")
    if key:
        request.add_header("Authorization", f"Bearer {key}")
    try:
        with _open(request, timeout) as response:
            raw = response.read()
            if response.headers.get("Content-Encoding", "").lower() == "gzip":
                raw = gzip.decompress(raw)
            return response.status, json.loads(raw.decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:
        return exc.code, None
    except (urllib.error.URLError, TimeoutError, OSError):
        return 0, None
    except json.JSONDecodeError:
        # JSONDecodeError 是 ValueError 的子类，必须排在它前面。
        return -1, None
    except ValueError:
        return -1, None


def _rank_models(model_ids: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    kept: list[str] = []
    for model_id in model_ids:
        low = model_id.lower()
        if not model_id or model_id in seen or any(part in low for part in _NOISE_PARTS):
            continue
        seen.add(model_id)
        kept.append(model_id)
    # 稳定排序：像 chat / flash / pro 的排前面，纯排序偏好，不改变可用性。
    kept.sort(key=lambda item: 0 if any(part in item.lower() for part in _PREFER_PARTS) else 1)
    return kept


def fetch_models(base_url: str, key: str) -> tuple[list[str], int]:
    """拉服务商的模型列表。返回 (模型 id 列表, 状态码)。"""
    status, data = _request_json(f"{base_url.rstrip('/')}/models", key)
    if status != 200 or data is None:
        return [], status
    items: object
    if isinstance(data, dict):
        items = data.get("data") or data.get("models") or []
    else:
        items = data
    if not isinstance(items, list):
        return [], status
    ids = [
        str(item["id"])
        for item in items
        if isinstance(item, dict) and item.get("id")
    ]
    return _rank_models(ids), status


def _explain_status(status: int) -> str:
    if status == 401:
        return "Key 不对，或者这个 Key 没开通权限"
    if status == 402:
        return "账户余额不足"
    if status == 403:
        return "被拒绝（可能 IP 受限）"
    if status == 404:
        return "接口地址不对，检查是不是少了 /v1"
    if status == 429:
        return "请求太频繁或额度用完了"
    if status == 0:
        return "连不上这个地址"
    if status == -1:
        return "返回的不是 JSON，地址可能指到了网页"
    if status in {500, 502, 503, 504}:
        return f"服务暂时不可用（HTTP {status}），也可能是代理拦了"
    return f"HTTP {status}"


def probe_chat(base_url: str, key: str, model: str) -> tuple[bool, str]:
    """发一个极小的真实请求，确认 key / 地址 / 模型三者都对得上。"""
    status, _ = _request_json(
        f"{base_url.rstrip('/')}/chat/completions",
        key,
        payload={
            "model": model,
            "messages": [{"role": "user", "content": "hi"}],
            "max_tokens": 8,
            "stream": False,
        },
        # 思考类模型首次响应慢，给足时间；只发 8 个 token，不会真的等满。
        timeout=25.0,
    )
    if status == 200:
        return True, ""
    return False, _explain_status(status)


def choose_model(preset: ProviderPreset, base_url: str, key: str, current: str = "") -> str:
    """先问服务商要模型列表，让用户从真实可用的里面选。

    模型名换代很快（DeepSeek 的 deepseek-chat 已经在 2026/07 弃用），写死在代码里
    必然过期。拉列表失败就退回手填，不阻断。
    """
    print(f"  正在获取 {preset.name} 的模型列表 ...")
    models, status = fetch_models(base_url, key)
    if not models:
        if not preset.needs_key:
            print(f"  （{_explain_status(status)}）")
        else:
            print(f"  （{_explain_status(status)}，改成手动填写）")
        return ask("  模型名", current or preset.model or "")

    keyword = ""
    while True:
        pool = [item for item in models if keyword in item.lower()] if keyword else models
        shown = pool[:12]
        if not shown:
            print(f"  没有匹配「{keyword}」的模型，换个关键字或直接输入完整名字。")
            keyword = ""
            continue

        print(f"  这个 Key 能用 {len(models)} 个模型，先列前 {len(shown)} 个：")
        for index, model_id in enumerate(shown, 1):
            print(f"    [{index:>2}] {model_id}")
        if len(pool) > len(shown):
            print(f"         还有 {len(pool) - len(shown)} 个没显示，可以输入关键字过滤")

        default = ""
        if current in shown:
            default = str(shown.index(current) + 1)
        raw = ask("  选序号 / 输入关键字 / 直接粘贴模型名", default)

        if raw.isdigit() and 1 <= int(raw) <= len(shown):
            return shown[int(raw) - 1]
        if raw in models:
            return raw
        keyword = raw.lower()


# --------------------------------------------------------------------------- #
# 校验
# --------------------------------------------------------------------------- #
def _resolve_llm(lines: list[str]) -> tuple[str, str, str, str]:
    """按 新键 → 旧键 → provider 预设 的顺序，算出实际生效的配置。"""
    provider = (get_value(lines, "LLM_PROVIDER") or "").strip().lower()
    base_url = get_first(lines, ("LLM_BASE_URL", "DEEPSEEK_BASE_URL"))
    model = get_first(lines, ("LLM_MODEL", "DEEPSEEK_MODEL"))
    key = get_first(lines, ("LLM_API_KEY", "DEEPSEEK_API_KEY"))

    if provider not in PROVIDER_BY_ID:
        provider = guess_provider(base_url) if base_url else DEFAULT_PROVIDER_ID
    preset = PROVIDER_BY_ID[provider]
    return provider, key, (base_url or preset.url), (model or preset.model)


def validate(lines: list[str]) -> tuple[list[str], list[str]]:
    """返回 (阻塞问题, 提醒)。阻塞问题为空就认为配置可用。"""
    problems: list[str] = []
    warnings: list[str] = []

    superusers = parse_json_list(get_value(lines, "SUPERUSERS"))
    superusers = [user for user in superusers if user != PLACEHOLDER_QQ]
    if not superusers:
        problems.append("SUPERUSERS 还没填你自己的 QQ 号")
    elif not all(user.isdigit() for user in superusers):
        problems.append(f"SUPERUSERS 里有非数字内容：{superusers}")

    if not parse_json_list(get_value(lines, "NICKNAME")):
        problems.append("NICKNAME 还没填机器人名字")

    provider, key, base_url, model = _resolve_llm(lines)
    preset = PROVIDER_BY_ID[provider]
    if preset.needs_key and not key:
        problems.append("LLM_API_KEY 为空（能启动，但不会回话）")
    if not base_url:
        problems.append("LLM_BASE_URL 为空（不知道往哪发请求）")
    elif not re.match(r"^https?://", base_url):
        problems.append("LLM_BASE_URL 必须以 http:// 或 https:// 开头")
    if not model:
        problems.append("LLM_MODEL 为空（不知道调哪个模型）")

    if not (get_value(lines, "LLM_PROVIDER") or "").strip():
        warnings.append("LLM_PROVIDER 没写，按接口地址自动判断")
    elif provider not in PROVIDER_BY_ID:
        warnings.append(f"LLM_PROVIDER={provider} 不认识，按自定义服务处理")

    if not (get_value(lines, "ADMIN_TOKEN") or "").strip():
        problems.append("ADMIN_TOKEN 为空（控制台会打不开数据）")

    for key_name in ("SUPERUSERS", "NICKNAME"):
        raw = get_value(lines, key_name)
        if raw:
            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError:
                problems.append(
                    f'{key_name} 不是合法 JSON 数组，应该写成 {key_name}=["123456"] 这种格式'
                )
                continue
            if not isinstance(parsed, list):
                problems.append(f'{key_name} 必须是数组，比如 {key_name}=["123456"]')
    return problems, warnings


# --------------------------------------------------------------------------- #
# 写入
# --------------------------------------------------------------------------- #
def backup(path: Path) -> Path | None:
    if not path.exists():
        return None
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    target = path.with_name(f"{path.name}.bak-{stamp}")
    shutil.copy2(path, target)
    return target


def write_env(lines: list[str]) -> None:
    backup(ENV_PATH)
    ENV_PATH.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8", newline="\n")


def self_check() -> str | None:
    """用项目自己的 Settings 再读一遍，确认没把配置写坏。"""
    try:
        sys.path.insert(0, str(ROOT))
        from src.settings import Settings  # noqa: PLC0415

        settings = Settings()
        if settings.superuser_ids and not any(u.isdigit() for u in settings.superuser_ids):
            return "SUPERUSERS 解析结果不是数字"
        if not settings.llm_base_url:
            return "LLM_BASE_URL 解析为空"
        return None
    except ImportError:
        # 依赖还没装好时跳过这一步，不阻断配置
        return None
    except Exception as exc:  # noqa: BLE001
        return str(exc)


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #
def run_check() -> int:
    lines = load_lines()
    if not lines:
        print("[X] 找不到 .env，也没有 .env.example")
        return 1
    problems, warnings = validate(lines)

    if warnings:
        print("[!] 提醒（不影响启动）：")
        for item in warnings:
            print(f"    - {item}")

    if problems:
        print("[X] 配置还不完整：")
        for item in problems:
            print(f"    - {item}")
        return 1

    provider, key, base_url, model = _resolve_llm(lines)
    preset = PROVIDER_BY_ID[provider]
    print("[OK] 配置检查通过")
    print(f"     管理员 QQ：{', '.join(parse_json_list(get_value(lines, 'SUPERUSERS')))}")
    print(f"     机器人名字：{', '.join(parse_json_list(get_value(lines, 'NICKNAME')))}")
    print(f"     模型服务：{preset.name}")
    print(f"     模型：{model}")
    print(f"     接口：{base_url}")
    print(f"     API Key：{'已填' if key else '未填（这家不需要）'}")
    return 0


def _apply(lines: list[str], *, qq: str, name: str, provider: str, key: str, base_url: str,
           model: str, token: str) -> list[str]:
    lines = set_value(lines, "SUPERUSERS", as_json_list([qq]))
    lines = set_value(lines, "NICKNAME", as_json_list([name]))

    lines = set_value(lines, "LLM_PROVIDER", provider, anchor=LLM_ANCHORS, before=True)
    lines = set_value(lines, "LLM_API_KEY", key, anchor="LLM_PROVIDER")
    lines = set_value(lines, "LLM_BASE_URL", base_url, anchor="LLM_API_KEY")
    lines = set_value(lines, "LLM_MODEL", model, anchor="LLM_BASE_URL")
    # 超时保留用户调过的值，没调过才用 60。
    timeout = get_first(lines, ("LLM_TIMEOUT", "DEEPSEEK_TIMEOUT")) or "60"
    lines = set_value(lines, "LLM_TIMEOUT", timeout, anchor="LLM_MODEL")

    lines = set_value(lines, "ADMIN_TOKEN", token, anchor="NICKNAME")

    # 这两项是连 NapCat 的关键，缺了补上，已有就不动
    if not get_value(lines, "DRIVER"):
        lines = set_value(lines, "DRIVER", "~fastapi+~websockets", anchor="ADMIN_TOKEN")
    if not get_value(lines, "ONEBOT_WS_URLS"):
        lines = set_value(
            lines, "ONEBOT_WS_URLS", as_json_list(["ws://127.0.0.1:3001"]), anchor="DRIVER"
        )

    # 旧键最后注释掉：上面写 LLM_* 时还要靠 DEEPSEEK_API_KEY 定位插入点。
    return comment_out(lines, LEGACY_KEYS)


def run_wizard(non_interactive: dict[str, str] | None = None) -> int:
    lines = load_lines()
    interactive = non_interactive is None

    old_qq = [u for u in parse_json_list(get_value(lines, "SUPERUSERS")) if u != PLACEHOLDER_QQ]
    old_name = parse_json_list(get_value(lines, "NICKNAME"))
    old_key = get_first(lines, ("LLM_API_KEY", "DEEPSEEK_API_KEY"))
    old_base = get_first(lines, ("LLM_BASE_URL", "DEEPSEEK_BASE_URL"))
    old_model = get_first(lines, ("LLM_MODEL", "DEEPSEEK_MODEL"))
    old_provider = (get_value(lines, "LLM_PROVIDER") or "").strip().lower()
    if old_provider not in PROVIDER_BY_ID:
        old_provider = guess_provider(old_base) if old_base else ""
    old_token = (get_value(lines, "ADMIN_TOKEN") or "").strip()

    if interactive:
        _title("QQ AI Bot 配置向导")
        print("  直接回车 = 用方括号里的默认值。填错也没关系，")
        print("  以后随时可以双击「配置向导.bat」重来。")
        print()

    try:
        if non_interactive:
            qq = str(non_interactive.get("qq") or (old_qq[0] if old_qq else ""))
            name = non_interactive.get("name") or (old_name[0] if old_name else "大肥鱼")
            provider = (non_interactive.get("provider") or old_provider or DEFAULT_PROVIDER_ID).lower()
            if provider not in PROVIDER_BY_ID:
                print(f"[X] 不认识的服务商：{provider}")
                print(f"    可选：{', '.join(p.id for p in PROVIDERS)}")
                return 1
            preset = PROVIDER_BY_ID[provider]
            same_provider = provider == old_provider
            base_url = (
                non_interactive.get("base_url")
                or (old_base if same_provider else "")
                or preset.url
            )
            model = (
                non_interactive.get("model")
                or (old_model if same_provider else "")
                or preset.model
            )
            # 换服务商时不继承旧 Key：把 A 家的 key 发给 B 家的服务器既不安全也用不了。
            key = non_interactive.get("api_key") or (old_key if same_provider else "")
            token = non_interactive.get("admin_token") or old_token
        else:
            qq = ask_qq(old_qq[0] if old_qq else "")
            print()
            name = ask("机器人叫什么名字", old_name[0] if old_name else "大肥鱼")
            print()
            provider = ask_provider(old_provider)
            preset = PROVIDER_BY_ID[provider]
            same_provider = provider == old_provider

            base_url = old_base if (same_provider and old_base) else preset.url
            if provider == "custom":
                base_url = ask_base_url(preset, base_url)
            print()
            key = ask_key(preset, old_key if same_provider else "")
            print()
            model = choose_model(preset, base_url, key, old_model if same_provider else "")
            print()
            print("  控制台密码（浏览器打开 http://127.0.0.1:8080/admin 时用）")
            token = ask("  密码（直接回车保留原值/自动生成）", "", allow_empty=True)
    except KeyboardInterrupt:
        print("\n\n已取消，.env 没有任何改动。")
        return 130

    if token:
        token_generated = False
    elif old_token:
        token = old_token
        token_generated = False
    else:
        token = secrets.token_urlsafe(18)
        token_generated = True

    lines = _apply(
        lines,
        qq=qq,
        name=name,
        provider=provider,
        key=key,
        base_url=base_url,
        model=model,
        token=token,
    )

    write_env(lines)

    error = self_check()
    if error:
        candidates = sorted(ENV_PATH.parent.glob(f"{ENV_PATH.name}.bak-*"))
        if candidates:
            shutil.copy2(candidates[-1], ENV_PATH)
        print(f"\n[X] 写入后自检失败，已还原成原来的 .env。原因：{error}")
        return 1

    _title("配置完成")
    print(f"  你的 QQ 号   {qq}   （管理员）")
    print(f"  机器人名字   {name}")
    print(f"  模型服务     {preset.name}")
    print(f"  模型         {model}")
    print(f"  接口         {base_url}")
    print(f"  API Key      {'已填' if key else '不需要 / 未填'}")

    if interactive:
        print()
        print("  正在测试能不能连上（大约 3~20 秒）...")
        ok, reason = probe_chat(base_url, key, model)
        if ok:
            print("  [OK] 连接正常，这个模型可以用了。")
        else:
            print(f"  [!] 连不上：{reason}")
            print("      配置已经保存，但机器人多半不会回话。")
            print("      可以重新双击「配置向导.bat」检查 Key 和接口地址。")

    if token_generated:
        print()
        print("  控制台密码（自动生成的，记一下）：")
        print(f"      {token}")
        print("  这个密码也保存在 .env 的 ADMIN_TOKEN 里，忘了可以打开看。")
    print()
    print(f"  配置文件：{ENV_PATH}")
    return 0


def main(argv: list[str] | None = None) -> int:
    _setup_stdio()
    parser = argparse.ArgumentParser(description="QQ AI Bot 配置向导")
    parser.add_argument("--check", action="store_true", help="只检查 .env 是否完整，不交互")
    parser.add_argument("--non-interactive", action="store_true", help="不提问，用参数或现有值直接写")
    parser.add_argument("--qq", help="管理员 QQ 号")
    parser.add_argument("--name", help="机器人名字")
    parser.add_argument("--provider", help=f"服务商 id：{', '.join(p.id for p in PROVIDERS)}")
    parser.add_argument("--api-key", dest="api_key", help="模型 API Key")
    parser.add_argument("--base-url", dest="base_url", help="接口地址，默认取该服务商的预设")
    parser.add_argument("--model", help="模型名，默认取该服务商的预设")
    parser.add_argument("--admin-token", dest="admin_token", help="控制台密码")
    args = parser.parse_args(argv)

    if args.check:
        return run_check()

    if args.non_interactive:
        return run_wizard(
            {
                "qq": args.qq or "",
                "name": args.name or "",
                "provider": args.provider or "",
                "api_key": args.api_key or "",
                "base_url": args.base_url or "",
                "model": args.model or "",
                "admin_token": args.admin_token or "",
            }
        )

    return run_wizard()


if __name__ == "__main__":
    raise SystemExit(main())
