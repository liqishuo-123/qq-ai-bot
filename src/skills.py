from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from nonebot.adapters.onebot.v11 import GroupMessageEvent, MessageEvent

from .db import Database
from .settings import Settings

logger = logging.getLogger(__name__)

ROLE_RANK = {"member": 1, "admin": 2, "group_owner": 3, "owner": 4}
ROLE_LABELS = {"member": "群成员", "admin": "管理员", "group_owner": "群主", "owner": "主人"}

EXECUTORS = {"core", "onebot", "qzone", "guild"}


@dataclass(frozen=True, slots=True)
class SkillDef:
    skill_id: str
    name: str
    category: str
    description: str
    risk: str
    default_min_role: str
    group_only: bool = False
    executor: str = "core"
    readonly: bool = False


SKILL_DEFS: dict[str, SkillDef] = {
    item.skill_id: item
    for item in (
        SkillDef("group.mute", "禁言", "群管理", "禁言群成员一段时间", "medium", "member", True),
        SkillDef("group.unmute", "解除禁言", "群管理", "解除群成员的禁言", "medium", "member", True),
        SkillDef("group.recall", "撤回消息", "群管理", "撤回一条群消息", "low", "member", True),
        SkillDef("group.notice", "群公告", "群管理", "发布群公告", "medium", "member", True),
        SkillDef("group.card", "改群名片", "群管理", "修改群成员名片", "low", "member", True),
        SkillDef("group.kick", "踢人", "群管理", "把成员移出群聊", "high", "member", True),
        SkillDef("group.whole_ban", "全员禁言", "群管理", "开启或关闭全员禁言", "high", "member", True),
        SkillDef("group.set_admin", "设管理员", "群管理", "设置或取消群管理员", "high", "member", True),
        SkillDef("group.name", "改群名", "群管理", "修改群名称", "high", "member", True),
        SkillDef("brain.search", "搜索大脑", "大脑", "按关键词搜索长期资料", "low", "member"),
        SkillDef("brain.read", "查看大脑", "大脑", "查看一条长期资料", "low", "member"),
        SkillDef("brain.write", "写入大脑", "大脑", "新增或整理长期资料", "medium", "member"),
        SkillDef("brain.delete", "删除大脑", "大脑", "删除一条长期资料", "high", "member"),
        SkillDef("profile.optimize", "优化主页", "账号", "生成新的主页资料", "medium", "member"),
        SkillDef("profile.apply", "应用主页", "账号", "把主页资料同步到 QQ", "high", "member"),
        SkillDef("qzone.post", "发空间动态", "账号", "生成或发布 QQ 空间动态", "high", "member", executor="qzone"),
        SkillDef("qzone.delete", "删空间动态", "账号", "删除一条 QQ 空间动态", "high", "member", executor="qzone"),
        SkillDef("qzone.list", "空间列表", "账号", "读取 QQ 空间说说和评论", "low", "member", executor="qzone", readonly=True),
        SkillDef("qzone.like", "空间点赞", "账号", "给空间说点赞或取消赞", "low", "member", executor="qzone"),
        SkillDef("qzone.comment", "空间评论", "账号", "给空间说评论", "medium", "member", executor="qzone"),
        SkillDef("qzone.comment_delete", "删空间评论", "账号", "删除空间说下的评论", "medium", "member", executor="qzone"),
        SkillDef("guild.status", "频道状态", "频道", "查看频道后端与配置状态", "low", "member", executor="guild", readonly=True),
        SkillDef("guild.list", "频道列表", "频道", "列出频道或频道配置", "low", "member", executor="guild", readonly=True),
        SkillDef("guild.history", "频道历史", "频道", "读取频道最近消息", "low", "member", executor="guild", readonly=True),
        SkillDef("guild.send", "频道发言", "频道", "向 QQ 频道发送一条消息", "high", "member", executor="guild"),
        SkillDef("guild.config", "频道设置", "频道", "设置频道后端、guild_id 和 channel_id", "high", "owner", executor="guild"),
        SkillDef("account.avatar", "换头像", "账号", "修改机器人自己的 QQ 头像", "medium", "member"),
        SkillDef("account.nickname", "改昵称", "账号", "直接修改机器人昵称，不动签名", "medium", "member"),
        SkillDef("account.signature", "改签名", "账号", "直接修改机器人个性签名", "low", "member"),
        SkillDef("account.status", "在线状态", "账号", "设置在线、离开、隐身、忙碌等状态", "low", "member"),
        SkillDef("account.like", "点赞", "账号", "给指定 QQ 点赞", "low", "member"),
        SkillDef("account.poke", "戳一戳", "账号", "在群里戳指定成员", "low", "member"),
        SkillDef("group.portrait", "改群头像", "群管理", "修改当前群头像", "high", "member", True),
        SkillDef("group.special_title", "群头衔", "群管理", "设置群成员的专属头衔", "medium", "member", True),
        SkillDef("group.sign", "群打卡", "群管理", "替机器人完成群打卡", "low", "member", True),
        SkillDef("group.essence", "精华消息", "群管理", "设置或取消群精华消息", "medium", "member", True),
    )
}


# ---------------------------------------------------------------------------
# NapCat / OneBot 11 统一能力表
# 每个条目：(action, 名称, 类别, 描述, 风险, 是否只读)
# 全部走 SkillService 的权限/审计/控制台矩阵，由 ActionService 以 executor="onebot"
# 直接调用 NapCat 的同名 action。默认最低角色 member、默认开启，控制台可收紧。
# ---------------------------------------------------------------------------
_API_ACTIONS: tuple[tuple[str, str, str, str, str, bool], ...] = (
    # -- 消息发送 --
    ("send_private_msg", "发私聊消息", "消息发送", "向指定好友发送私聊消息", "high", False),
    ("send_group_msg", "发群消息", "消息发送", "向指定群发送消息", "high", False),
    ("send_msg", "发消息", "消息发送", "按消息事件上下文发送消息", "high", False),
    ("send_private_forward_msg", "发私聊合并转发", "消息发送", "发送私聊合并转发消息", "medium", False),
    ("send_group_forward_msg", "发群合并转发", "消息发送", "发送群合并转发消息", "medium", False),
    ("send_forward_msg", "发合并转发", "消息发送", "按上下文发送合并转发消息", "medium", False),
    ("send_poke", "戳一戳", "消息发送", "向指定会话成员发送戳一戳", "low", False),
    ("send_group_poke", "群戳一戳", "消息发送", "在群里戳指定成员", "low", False),
    ("group_poke", "群戳一戳(旧)", "消息发送", "在群里戳指定成员（旧接口）", "low", False),
    ("friend_poke", "好友戳一戳", "消息发送", "戳一戳好友", "low", False),
    ("send_group_sign", "群签到", "消息发送", "替机器人完成指定群打卡", "low", False),
    ("send_group_notice", "发群公告", "消息发送", "向指定群发布群公告", "medium", False),
    ("del_group_notice", "删群公告", "消息发送", "删除指定群的公告", "medium", False),
    ("send_like", "好友赞", "消息发送", "给指定 QQ 点赞", "low", False),
    ("send_profile_like", "名片赞", "消息发送", "给指定 QQ 的名片点赞", "low", False),
    ("set_input_status", "输入状态", "消息发送", "设置对好友的“正在输入”状态", "low", False),
    ("mark_private_msg_as_read", "私聊已读", "消息发送", "标记私聊消息为已读", "low", False),
    ("mark_group_msg_as_read", "群聊已读", "消息发送", "标记群消息为已读", "low", False),
    ("mark_all_as_read", "全部已读", "消息发送", "把所有会话标记为已读", "low", False),
    ("send_qzone_msg", "发说说", "消息发送", "以当前 QQ 发布空间说说", "high", False),
    ("delete_qzone_msg", "删说说", "消息发送", "删除当前 QQ 的一条空间说说", "high", False),
    ("nc_guild_send", "频道发送", "消息发送", "以当前 QQ 向频道发送消息", "high", False),
    # -- 消息读取与管理 --
    ("delete_msg", "撤回消息", "消息管理", "按 message_id 撤回一条消息", "medium", False),
    ("get_msg", "取消息", "消息管理", "按 message_id 读取一条消息", "low", True),
    ("get_forward_msg", "取转发消息", "消息管理", "读取一条合并转发消息的内容", "low", True),
    ("get_friend_msg_history", "好友历史消息", "消息管理", "读取与好友的历史消息", "low", True),
    ("get_group_msg_history", "群历史消息", "消息管理", "读取指定群的历史消息", "low", True),
    ("get_recent_contact", "最近会话", "消息管理", "读取最近的会话列表", "low", True),
    ("get_file", "取文件", "消息管理", "按 file_id 读取文件内容", "low", True),
    ("get_image", "取图片", "消息管理", "按 file_id 读取图片", "low", True),
    ("get_record", "取语音", "消息管理", "按 file_id 读取语音", "low", True),
    ("ocr_image", "图片 OCR", "消息管理", "识别图片里的文字", "low", True),
    # -- 群管理 --
    ("set_group_kick", "群踢人", "群管理", "把成员移出指定群", "high", False),
    ("set_group_ban", "群禁言", "群管理", "禁言指定群成员一段时间", "medium", False),
    ("set_group_anonymous_ban", "匿名禁言", "群管理", "禁言群内匿名用户", "medium", False),
    ("set_group_whole_ban", "全员禁言", "群管理", "开启或关闭全员禁言", "high", False),
    ("set_group_admin", "群设管理", "群管理", "设置或取消群管理员", "high", False),
    ("set_group_anonymous", "匿名开关", "群管理", "开启或关闭群匿名聊天", "medium", False),
    ("set_group_card", "设置群名片", "群管理", "修改指定成员的群名片", "low", False),
    ("set_group_name", "设置群名", "群管理", "修改群名称", "high", False),
    ("set_group_leave", "退出群聊", "群管理", "让机器人退出指定群", "high", False),
    ("set_group_special_title", "群专属头衔", "群管理", "设置成员的群专属头衔", "medium", False),
    ("set_group_avatar", "设置群头像", "群管理", "修改指定群的群头像", "high", False),
    ("set_group_portrait", "设置群头像(旧)", "群管理", "修改指定群的群头像（旧接口）", "high", False),
    ("set_essence", "设精华", "群管理", "设置或取消群精华消息", "medium", False),
    ("set_group_essence", "设精华(旧)", "群管理", "设置或取消群精华消息（旧接口）", "medium", False),
    ("set_group_sign", "群打卡(旧)", "群管理", "替机器人完成群打卡（旧接口）", "low", False),
    ("set_group_robot", "群机器人开关", "群管理", "管理群内机器人开关", "medium", False),
    # -- 群信息（只读） --
    ("get_group_info", "取群信息", "群信息", "读取指定群的基本信息", "low", True),
    ("get_group_list", "取群列表", "群信息", "读取机器人加入的群列表", "low", True),
    ("get_group_info_ex", "取群信息扩展", "群信息", "读取群的扩展信息", "low", True),
    ("get_group_member_info", "取群成员信息", "群信息", "读取指定群成员的资料", "low", True),
    ("get_group_member_list", "取群成员列表", "群信息", "读取指定群的成员列表", "low", True),
    ("get_group_honor_info", "取群荣誉", "群信息", "读取群荣誉（龙王等）信息", "low", True),
    ("get_group_essence_msg_list", "精华列表", "群信息", "读取群精华消息列表", "low", True),
    ("get_group_notices", "群公告列表", "群信息", "读取指定群的公告列表", "low", True),
    ("get_group_shut_list", "禁言列表", "群信息", "读取群内被禁言成员列表", "low", True),
    ("get_group_avatar", "取群头像", "群信息", "获取指定群头像地址", "low", True),
    ("get_group_album_list", "群相册列表", "群信息", "读取指定群相册列表", "low", True),
    ("get_group_file_list", "群文件列表", "群信息", "读取指定群的文件列表", "low", True),
    ("get_group_files", "群文件列表(旧)", "群信息", "读取指定群的文件列表（旧接口）", "low", True),
    ("get_group_fs_list", "群文件系统", "群信息", "读取群文件系统目录", "low", True),
    ("get_group_details", "群详情", "群信息", "读取群的详细信息", "low", True),
    # -- 好友与用户 --
    ("get_login_info", "取登录信息", "好友用户", "读取当前登录 QQ 的信息", "low", True),
    ("get_stranger_info", "取陌生人信息", "好友用户", "读取指定 QQ 的资料", "low", True),
    ("get_friend_list", "取好友列表", "好友用户", "读取好友列表", "low", True),
    ("get_friend_list_with_category", "分类好友列表", "好友用户", "读取带分组的好友列表", "low", True),
    ("get_profile_like", "被赞信息", "好友用户", "读取当前 QQ 的被赞与资料卡信息", "low", True),
    ("get_qq_avatar", "取QQ头像", "好友用户", "获取指定 QQ 的头像地址", "low", True),
    ("get_system_msg", "系统消息", "好友用户", "读取好友申请与邀请通知", "low", True),
    ("set_friend_add_request", "好友申请", "好友用户", "同意或拒绝好友申请", "high", False),
    ("set_group_add_request", "加群申请", "好友用户", "同意或拒绝加群申请", "high", False),
    ("set_friend_remark", "好友备注", "好友用户", "修改好友备注", "low", False),
    ("set_online_status", "在线状态", "好友用户", "设置在线、离开、隐身等状态", "low", False),
    ("set_diy_online_status", "自定义状态", "好友用户", "设置自定义在线状态", "low", False),
    # -- 群文件管理 --
    ("upload_group_file", "上传群文件", "群文件", "向指定群上传文件", "medium", False),
    ("upload_private_file", "上传私聊文件", "群文件", "向好友发送文件", "medium", False),
    ("delete_group_file", "删除群文件", "群文件", "删除群内指定文件", "high", False),
    ("create_group_file_folder", "建群文件夹", "群文件", "在群里创建文件文件夹", "medium", False),
    ("delete_group_file_folder", "删群文件夹", "群文件", "删除群内文件夹", "high", False),
    ("rename_group_file", "重命名群文件", "群文件", "重命名群内文件或文件夹", "medium", False),
    ("move_group_file", "移动群文件", "群文件", "移动群内文件", "medium", False),
    ("transfer_group_file", "转存群文件", "群文件", "把群文件转存到自己的空间", "medium", False),
    # -- 账号资料 --
    ("set_qq_profile", "设置QQ资料", "账号资料", "修改当前 QQ 的昵称、签名等资料", "high", False),
    ("set_qq_avatar", "设置QQ头像", "账号资料", "修改当前 QQ 的头像", "medium", False),
    ("get_self_profile", "取自己资料", "账号资料", "读取当前 QQ 的完整资料", "low", True),
    ("get_user_profile", "取用户资料", "账号资料", "读取指定用户的资料卡", "low", True),
    ("get_robot_uin_range", "机器人号段", "账号资料", "读取官方机器人号段范围", "low", True),
    ("get_rkey", "取rkey", "账号资料", "读取图片下载所需的 rkey", "low", True),
    # -- 媒体能力 --
    ("can_send_image", "能发图片", "媒体能力", "检查当前是否可以发送图片", "low", True),
    ("can_send_record", "能发语音", "媒体能力", "检查当前是否可以发送语音", "low", True),
    ("get_download_file", "下载文件", "媒体能力", "下载指定链接的文件", "low", True),
    ("translate_en2zh", "英译中", "媒体能力", "把英文翻译成中文", "low", True),
    ("translate_zh2en", "中译英", "媒体能力", "把中文翻译成英文", "low", True),
    # -- 频道（NapCat 同账号路径） --
    ("nc_guild_list", "频道列表", "频道能力", "读取当前 QQ 可见的频道列表", "low", True),
    ("nc_guild_history", "频道历史", "频道能力", "读取频道的最近消息", "low", True),
    ("nc_guild_status", "频道插件状态", "频道能力", "探测频道插件可用性", "low", True),
    # -- 系统与状态 --
    ("get_status", "运行状态", "系统", "读取 OneBot 实现的运行状态", "low", True),
    ("get_version_info", "版本信息", "系统", "读取 OneBot 实现的版本信息", "low", True),
    ("get_online_clients", "在线客户端", "系统", "读取当前账号的在线客户端", "low", True),
    ("get_cookies", "取Cookies", "系统", "读取指定域名的登录 Cookies", "high", True),
    ("get_csrf_token", "取CSRF Token", "系统", "读取 bkn / CSRF Token", "high", True),
    ("get_credentials", "取凭证", "系统", "读取 Cookies 与 CSRF Token", "high", True),
    ("set_restart", "重启实现", "系统", "重启 OneBot 实现（NapCat）", "high", False),
    ("clean_cache", "清理缓存", "系统", "清理 OneBot 实现的缓存", "medium", False),
)


def _api_skill_defs() -> tuple[SkillDef, ...]:
    defs: list[SkillDef] = []
    for action, name, category, description, risk, readonly in _API_ACTIONS:
        defs.append(
            SkillDef(
                skill_id=f"api.{action}",
                name=name,
                category=category,
                description=description,
                risk=risk,
                default_min_role="member",
                executor="onebot",
                readonly=readonly,
            )
        )
    return tuple(defs)


for _item in _api_skill_defs():
    SKILL_DEFS.setdefault(_item.skill_id, _item)

API_ACTION_IDS = frozenset(
    item.skill_id.removeprefix("api.") for item in _api_skill_defs()
)


class SkillDenied(PermissionError):
    pass


class SkillService:
    def __init__(self, settings: Settings, db: Database) -> None:
        self.settings = settings
        self.db = db

    async def ensure_defaults(self) -> None:
        for skill in SKILL_DEFS.values():
            if await self.db.get_skill_setting(skill.skill_id) is None:
                await self.db.upsert_skill_setting(
                    skill.skill_id,
                    enabled=True,
                    min_role=skill.default_min_role,
                )

    def role_for_event(self, event: MessageEvent) -> str:
        if str(event.user_id) in self.settings.superuser_ids:
            return "owner"
        if isinstance(event, GroupMessageEvent):
            role = str(getattr(event.sender, "role", "") or "").lower()
            if role == "owner":
                return "group_owner"
            if role == "admin":
                return "admin"
        return "member"

    @staticmethod
    def _normalize_role(role: str) -> str:
        value = str(role or "owner").lower()
        if value not in ROLE_RANK:
            raise SkillDenied("权限角色不合法")
        return value

    @staticmethod
    def _scope_for(event: MessageEvent) -> tuple[str, str]:
        if isinstance(event, GroupMessageEvent):
            return "group", str(event.group_id)
        return "private", str(event.user_id)

    async def effective_setting(self, skill_id: str, scope_type: str, scope_id: str) -> tuple[bool, str]:
        skill = SKILL_DEFS.get(skill_id)
        if skill is None:
            raise SkillDenied("没有这个技能")
        setting = await self.db.get_skill_setting(skill_id)
        enabled = bool(setting.enabled) if setting else True
        min_role = setting.min_role if setting else skill.default_min_role
        if scope_type in {"group", "private"}:
            grant = await self.db.get_skill_grant(scope_type, scope_id, skill_id)
            if grant is not None:
                enabled = bool(grant["enabled"])
                min_role = str(grant["min_role"])
        return enabled, min_role

    async def check(self, skill_id: str, event: MessageEvent) -> SkillDef:
        skill = SKILL_DEFS.get(skill_id)
        if skill is None:
            raise SkillDenied("没有这个技能")
        if not self.settings.skills_enabled:
            raise SkillDenied("技能系统已关闭")
        if skill.group_only and not isinstance(event, GroupMessageEvent):
            raise SkillDenied("这个技能只能在群里使用")
        scope_type, scope_id = self._scope_for(event)
        enabled, min_role = await self.effective_setting(skill_id, scope_type, scope_id)
        if not enabled:
            raise SkillDenied("这个技能在当前范围已关闭")
        role = self.role_for_event(event)
        if ROLE_RANK[role] < ROLE_RANK[min_role]:
            raise SkillDenied(f"权限不够，需要{ROLE_LABELS.get(min_role, min_role)}以上")
        return skill

    async def check_role(
        self,
        skill_id: str,
        *,
        role: str,
        scope_type: str = "private",
        scope_id: str = "self",
    ) -> SkillDef:
        """Permission check without a QQ event (console / internal callers)."""
        skill = SKILL_DEFS.get(skill_id)
        if skill is None:
            raise SkillDenied("没有这个技能")
        if not self.settings.skills_enabled:
            raise SkillDenied("技能系统已关闭")
        enabled, min_role = await self.effective_setting(skill_id, scope_type, scope_id)
        if not enabled:
            raise SkillDenied("这个技能在当前范围已关闭")
        rank = ROLE_RANK.get(str(role), 0)
        if rank < ROLE_RANK.get(min_role, 4):
            raise SkillDenied(f"权限不够，需要{ROLE_LABELS.get(min_role, min_role)}以上")
        return skill

    async def audit(
        self,
        skill_id: str,
        *,
        scope_type: str,
        scope_id: str,
        actor_id: str | None,
        target_id: str | None = None,
        args: str = "",
        status: str,
        error: str = "",
    ) -> None:
        await self.db.add_skill_audit(
            skill_id,
            scope_type=scope_type,
            scope_id=scope_id,
            actor_id=actor_id,
            target_id=target_id,
            args=args,
            status=status,
            error=error,
        )

    async def list_effective(self, scope_type: str = "", scope_id: str = "") -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        for skill in SKILL_DEFS.values():
            setting = await self.db.get_skill_setting(skill.skill_id)
            enabled = bool(setting.enabled) if setting else True
            min_role = setting.min_role if setting else skill.default_min_role
            overridden = False
            if scope_type in {"group", "private"} and scope_id:
                grant = await self.db.get_skill_grant(scope_type, scope_id, skill.skill_id)
                if grant is not None:
                    enabled = bool(grant["enabled"])
                    min_role = str(grant["min_role"])
                    overridden = True
            result.append(
                {
                    "id": skill.skill_id,
                    "name": skill.name,
                    "category": skill.category,
                    "description": skill.description,
                    "risk": skill.risk,
                    "group_only": skill.group_only,
                    "executor": skill.executor,
                    "readonly": skill.readonly,
                    "enabled": enabled,
                    "min_role": min_role,
                    "effective_min_role": min_role,
                    "floor": "",
                    "overridden": overridden,
                }
            )
        return result

    async def set_global(self, skill_id: str, *, enabled: bool, min_role: str) -> dict[str, Any]:
        skill = SKILL_DEFS.get(skill_id)
        if skill is None:
            raise SkillDenied("没有这个技能")
        role = self._normalize_role(min_role)
        setting = await self.db.upsert_skill_setting(skill_id, enabled=enabled, min_role=role)
        return {
            "skill_id": setting.skill_id,
            "enabled": setting.enabled,
            "min_role": setting.min_role,
            "updated_at": setting.updated_at,
        }

    async def set_grant(
        self,
        scope_type: str,
        scope_id: str,
        skill_id: str,
        *,
        enabled: bool,
        min_role: str,
    ) -> dict[str, Any]:
        skill = SKILL_DEFS.get(skill_id)
        if skill is None:
            raise SkillDenied("没有这个技能")
        if scope_type not in {"group", "private"} or not scope_id:
            raise SkillDenied("范围不合法")
        role = self._normalize_role(min_role)
        return await self.db.upsert_skill_grant(
            scope_type,
            scope_id,
            skill_id,
            enabled=enabled,
            min_role=role,
        )

    async def clear_grant(self, scope_type: str, scope_id: str, skill_id: str) -> bool:
        return await self.db.delete_skill_grant(scope_type, scope_id, skill_id)

    def available_for_role(self, role: str) -> list[SkillDef]:
        rank = ROLE_RANK.get(role, 0)
        return [skill for skill in SKILL_DEFS.values() if rank >= ROLE_RANK[skill.default_min_role]]
