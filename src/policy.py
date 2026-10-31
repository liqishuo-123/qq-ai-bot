from __future__ import annotations

import re

from .safety import contains_sensitive

# 账号能力边界：这些动作永远不实现，避免以后误加。
FORBIDDEN_ACCOUNT_ACTIONS = frozenset(
    {
        "friend.add",
        "friend.delete",
        "payment.pay",
        "payment.transfer",
        "payment.redpacket",
        "wallet.balance",
    }
)

_ILLEGAL_RE = re.compile(
    r"赌博|博彩|下注|洗钱|跑分|毒品|冰毒|枪支|弹药|代开发票|偷税|逃税|"
    r"诈骗|杀猪盘|资金盘|传销|裸聊|色情交易|约炮|招嫖",
    re.IGNORECASE,
)
_SCAM_RE = re.compile(
    r"刷单|返利|日赚|稳赚|包赔|兼职日结|高额回报|内部渠道|"
    r"点击链接.*(?:领|奖|返)|扫码.*(?:领|返|付)|私聊.*(?:福利|领取)|"
    r"加(?:微信|V|v).*(?:领|返|赚)|免费领.*(?:皮肤|红包|会员)",
    re.IGNORECASE,
)
_PAYMENT_RE = re.compile(
    r"收款码|付款码|转账|代付|充值|提现|银行卡号|支付宝|微信支付|"
    r"USDT|TRC20|ERC20|二维码.*(?:收款|付款|支付)",
    re.IGNORECASE,
)
_AD_RE = re.compile(
    r"广告位|招商|代理加盟|引流|推广返佣|优惠券|"
    r"加(?:微信|V|v).*(?:咨询|购买|合作)|QQ群.*(?:福利|资源)",
    re.IGNORECASE,
)
_URL_RE = re.compile(r"https?://|www\.", re.IGNORECASE)
_LONG_DIGITS_RE = re.compile(r"\d{5,}")
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


class PolicyViolation(ValueError):
    """Raised when account behavior would cross a hard safety rule."""


def check_account_action(action: str) -> None:
    if action in FORBIDDEN_ACCOUNT_ACTIONS:
        raise PolicyViolation(f"该账号动作被禁止：{action}")


def can_send_proactive_group(role: str | None) -> bool:
    """主动往群里发消息时，机器人必须是群主或管理员。"""
    return str(role or "").lower() in {"owner", "admin"}


def can_send_proactive_private() -> bool:
    """不主动私聊别人；私聊只允许作为对用户消息的回复。"""
    return False


def check_chat_text(text: str) -> str:
    """普通聊天出口：只拦截高置信度的违法、诈骗和支付内容。"""
    value = (text or "").strip()
    if not value:
        raise PolicyViolation("内容为空")
    if _CONTROL_RE.search(value):
        raise PolicyViolation("内容包含控制字符")
    if _ILLEGAL_RE.search(value) or _SCAM_RE.search(value):
        raise PolicyViolation("内容涉及违法或诈骗")
    if _PAYMENT_RE.search(value):
        raise PolicyViolation("不能发送支付相关内容")
    return value


def check_public_text(text: str, *, max_chars: int = 300) -> str:
    """空间动态、主页、群公告等公开内容：更严格的广告和隐私过滤。"""
    value = check_chat_text(text)
    if len(value) > max_chars:
        raise PolicyViolation(f"内容超过 {max_chars} 字")
    if _URL_RE.search(value) or _LONG_DIGITS_RE.search(value):
        raise PolicyViolation("公开内容里不能带链接或长串数字")
    if _AD_RE.search(value):
        raise PolicyViolation("公开内容不能发广告")
    if contains_sensitive(value):
        raise PolicyViolation("公开内容包含敏感信息")
    return value


def check_group_notice(text: str) -> str:
    return check_public_text(text, max_chars=500)
