import asyncio
import base64
import inspect
import json
import os
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from maibot_sdk import (
    CONFIG_RELOAD_SCOPE_SELF,
    Command,
    Field,
    HookHandler,
    MaiBotPlugin,
    PluginConfigBase,
    Tool,
)
try:
    from .pinzi_auth import AuthManager, AuthError
except ImportError:
    from pinzi_auth import AuthManager, AuthError

from maibot_sdk.types import ErrorPolicy, HookMode, ToolParameterInfo, ToolParamType

DEFAULT_KEYWORDS = ["有房吗", "码字房", "拼字房"]
DEFAULT_TRIGGER_MODES = ["keyword", "command", "tool"]
DEFAULT_REPLY_TEMPLATE = (
    "【{creator_name}】邀请你加入{target_words}字拼字。\n"
    "{room_link}\n"
    "或进入作家助手-拼字星球，搜索房间号：\n"
    "{room_number}"
)
DEFAULT_SHARE_BASE_URL = "https://m.write.qq.com/portal/spellInvite"

# 工具内部已通过 ctx.send.text 直接把内容发到会话；返回给 LLM 的 content
# 只说明“已发送、不要再回复”，避免模型再生成一条重复消息。
TOOL_ALREADY_SENT_ROOM_LINK = "拼字房房间号与邀请链接已直接发送到当前会话，请不要再生成文字回复或调用 reply。"
TOOL_ALREADY_SENT_ROOM_DATA = "拼字房房间数据已直接发送到当前会话，请不要再生成文字回复或调用 reply。"
TOOL_ALREADY_SENT_FAILURE = "拼字房查询提示已直接发送到当前会话，请不要再生成文字回复或调用 reply。"
TOOL_SUPPRESSED_AFTER_LINK = "房间邀请链接已在最近窗口期内直接发送到当前会话，本次房间数据查询无需重复发送；请不要再生成文字回复或调用 reply。"

DEFAULT_CREATE_DEFAULTS: Dict[str, Any] = {
    "homeType": 2,
    "numbersLimit": 50,
    "worldLimit": 30000,
    "timeLimit": 60,
    "visible": "2",
    "laststartminute": "5",
    "halfWayJoin": "1",
    "homeThemeType": 1,
    "inviteRobot": "1",
    "syncLiveMsg": 0,
}


class RoomShareError(RuntimeError):
    pass


class PluginSectionConfig(PluginConfigBase):
    """插件基础配置。"""

    __ui_label__ = "插件"
    __ui_icon__ = "package"
    __ui_order__ = 0

    enabled: bool = Field(default=True, description="是否启用插件")
    config_version: str = Field(default="0.1.0", description="配置版本")


class ZuojiaRoomConfig(PluginConfigBase):
    plugin: PluginSectionConfig = Field(default_factory=PluginSectionConfig)
    group_whitelist: List[str] = Field(
        default_factory=list, description="允许响应的群号白名单"
    )
    admin_user_ids: List[str] = Field(
        default_factory=list, description="允许私聊扫码授权的管理员 QQ 号；建房权限设为 admins 时也用于限制建房"
    )
    trigger_modes: List[str] = Field(
        default_factory=lambda: list(DEFAULT_TRIGGER_MODES),
        description="触发方式列表，可选 keyword / command / tool",
    )
    keyword_reply_mode: str = Field(
        default="llm_first",
        description="关键词触发模式：always=必答并中止LLM（旧行为）；llm_first=LLM优先，命中关键词后等待 LLM 回复，LLM 静默超时才由插件兜底自动回复；tool_only=关键词不自动回复，仅靠 LLM 工具",
    )
    keyword_cooldown_seconds: float = Field(
        default=120.0,
        description="同一会话两次关键词自动回复的最小间隔（秒），防止重复触发；0 表示关闭",
    )
    fallback_delay_seconds: float = Field(
        default=20.0,
        description="llm_first 模式：命中关键词后等待 LLM 出站回复的时间窗（秒），超时无回复才由插件兜底自动回复",
    )
    reply_dedup_seconds: float = Field(
        default=10.0,
        description="出站去重窗口（秒）：同会话窗口内已发过同文本则拦截（覆盖关键词/工具/LLM 三路），防止同一话题双发",
    )
    keyword_patterns: List[str] = Field(
        default_factory=list,
        description="可选正则关键词列表，与 keywords 子串匹配任一命中即触发；用于收紧“房间”等宽子串误触",
    )
    ignore_at_to_others: bool = Field(
        default=True,
        description="群聊中消息 @ 了别人（at 目标不是本机器人）时，关键词 hook 不自动回复/不兜底，避免误触（如群友 @作家助手 问房间）；@本机器人仍正常触发",
    )
    private_chat_enabled: bool = Field(
        default=True, description="是否允许私聊触发"
    )
    private_user_ids: List[str] = Field(
        default_factory=list, description="允许私聊触发的 QQ 号，留空表示启用私聊时全部放行"
    )
    keywords: List[str] = Field(
        default_factory=lambda: list(DEFAULT_KEYWORDS), description="触发关键词"
    )
    keyword_must_reply: bool = Field(
        default=True,
        description="命中关键词后无论查询成功与否都必须回复并中止 LLM 流程；关闭后查询失败时放行给 LLM 自主判断",
    )
    keyword_group_failure_reply: str = Field(
        default="现在没有房哦",
        description="群聊中非管理员命中关键词但查询失败时的基础回复文案；留空则放行给 LLM 自主回复",
    )
    keyword_failure_llm_reply: bool = Field(
        default=True,
        description="群聊普通成员查询失败时是否用 LLM 以基础文案为底生成更自然的回复；LLM 异常/超时/空结果回退基础文案",
    )
    reply_template: str = Field(
        default=DEFAULT_REPLY_TEMPLATE,
        description="回复模板，支持 {room_number}、{room_link}、{creator_name}、{target_words}、{target_minutes}",
    )
    no_room_message: str = Field(
        default="暂时没有拼字房，管理员可以创建一个。",
        description="没有房间时的回复",
    )
    not_authorized_message: str = Field(
        default="你没有创建拼字房的权限。",
        description="没有创建权限时的回复",
    )
    api_base_url: str = Field(
        default="https://pcwrite.yuewen.com", description="作家助手 API 基础地址"
    )
    author_id: str = Field(
        default="", description="作家助手作者 ID（cauthorid），分享链接需要用到"
    )
    current_room_path: str = Field(
        default="/ccauthorweb/desk/spelling/getCauthorHomeid",
        description="查询当前是否在房间的接口路径",
    )
    home_user_list_path: str = Field(
        default="/ccauthorweb/desk/spelling/getHomeUserList",
        description="房间成员列表接口路径",
    )
    home_detail_path: str = Field(
        default="/ccauthorweb/desk/spelling/getSpellingHomeDetailPage",
        description="房间详情/榜单接口路径",
    )
    join_home_path: str = Field(
        default="/ccauthorweb/desk/spelling/joinHome",
        description="加入房间接口路径",
    )
    quit_home_path: str = Field(
        default="/ccauthorweb/desk/spelling/quitHome",
        description="退出房间接口路径（创建新房间前用于退出已结束的旧房间）",
    )
    invite_share_path: str = Field(
        default="/ccauthorweb/desk/spelling/getInviteShareId",
        description="获取分享码接口路径",
    )
    create_room_path: str = Field(
        default="/ccauthorweb/desk/spelling/createhome",
        description="创建房间接口路径",
    )
    create_room_defaults: Dict[str, Any] = Field(
        default_factory=lambda: dict(DEFAULT_CREATE_DEFAULTS),
        description="创建房间时的请求体字段",
        json_schema_extra={"hidden": True},
    )
    room_share_base_url: str = Field(
        default=DEFAULT_SHARE_BASE_URL, description="分享链接基础地址"
    )
    default_work_id: str = Field(default="", description="默认作品 ID")
    create_room_if_missing: bool = Field(
        default=True, description="没有房间时是否允许自动创建"
    )
    room_creation_permission: str = Field(
        default="allowed_chats",
        description="建房调用者权限：allowed_chats=已开放聊天中的成员（默认）；admins=仅管理员。与扫码授权权限独立，仍受自动建房开关限制",
    )
    quit_ended_room_before_create: bool = Field(
        default=False,
        description="检测到已结束的旧房间（homeStatus=3）时，创建新房间前先尝试退出旧房间；实测服务端对已结束房间 quitHome 会返回“房间不存在或已结束”，通常无需开启",
    )
    room_data_create_if_missing: bool = Field(
        default=True, description="查询房间数据时没有房间是否允许自动创建"
    )
    room_data_rank_limit: int = Field(
        default=13, description="房间数据榜单最多返回多少名"
    )
    room_data_no_room_message: str = Field(
        default="暂时没有拼字房，管理员可以创建或稍后使用 /拼字房",
        description="查询房间数据且不创建时的回复",
    )
    room_data_suppress_after_link_seconds: float = Field(
        default=120.0,
        description="房间数据查询工具建房抑制窗口（秒）：窗口期内本会话刚成功发出过房间邀请链接时，get_room_data 无房时不再自动建房/不再发送邀请链接（有房仍正常返回榜单）；0 表示关闭",
    )
    room_default_duration_minutes: int = Field(
        default=60, description="创建房间默认时长（分钟），会覆盖 create_room_defaults.timeLimit"
    )
    room_default_word_goal: int = Field(
        default=30000, description="创建房间默认目标字数，会覆盖 create_room_defaults.worldLimit"
    )
    room_default_member_limit: int = Field(
        default=50, description="创建房间默认人数，会覆盖 create_room_defaults.numbersLimit"
    )
    login_timeout_seconds: int = Field(default=180, ge=30, le=300, description="扫码等待秒数")
    auth_health_check_minutes: int = Field(
        default=30, description="登录态健康检查间隔（分钟）"
    )
    auth_expired_notice_template: str = Field(
        default=(
            "❌ 拼字房登录态已失效，房间查询将不可用。"
            "请配置管理员在私聊发送 /拼字登录 重新授权。"
        ),
        description="登录态失效时私聊提醒文案",
    )
    manual_room_number: str = Field(default="", description="手动维护的房间号")
    manual_room_link: str = Field(default="", description="手动维护的房间链接")


def normalize_group_id(value: Any) -> str:
    text = str(value or "").strip()
    if text.startswith("group_"):
        text = text[6:]
    elif text.startswith("gid_"):
        text = text[4:]
    elif text.startswith("qq:"):
        text = text[3:]
    return text


def is_allowed_group(group_id: Any, whitelist: List[str]) -> bool:
    normalized = normalize_group_id(group_id)
    if not normalized:
        return False
    allowed = {normalize_group_id(item) for item in whitelist if item}
    return normalized in allowed


def is_allowed_user(user_id: Any, admin_user_ids: List[str]) -> bool:
    user = str(user_id or "").strip()
    if not admin_user_ids:
        return False
    admins = {str(item).strip() for item in admin_user_ids if item}
    return user in admins


def is_login_user(user_id: Any, config: "ZuojiaRoomConfig") -> bool:
    return is_allowed_user(user_id, config.admin_user_ids)

def is_allowed_chat(group_id: Any, user_id: Any, config: "ZuojiaRoomConfig") -> bool:
    if normalize_group_id(group_id):
        return is_allowed_group(group_id, config.group_whitelist)
    if not config.private_chat_enabled:
        return False
    if not config.private_user_ids:
        return True
    return is_allowed_user(user_id, config.private_user_ids)


def can_create_room(group_id: Any, user_id: Any, config: "ZuojiaRoomConfig") -> bool:
    if not str(user_id or "").strip() or not is_allowed_chat(group_id, user_id, config):
        return False
    policy = config.room_creation_permission.strip().lower()
    if policy == "allowed_chats":
        return True
    return policy == "admins" and is_allowed_user(user_id, config.admin_user_ids)


def trigger_enabled(mode: str, trigger_modes: List[str]) -> bool:
    enabled = {str(item).strip().lower() for item in trigger_modes if item}
    return str(mode).strip().lower() in enabled


def match_keyword(text: Any, keywords: List[str]) -> bool:
    haystack = _as_text(text).lower()
    return any(keyword.lower() in haystack for keyword in keywords if keyword)


def match_keyword_patterns(text: Any, patterns: List[str]) -> bool:
    """用正则列表匹配关键词；单个正则非法时跳过，不影响其它正则。"""
    haystack = _as_text(text)
    for pattern in patterns:
        if not pattern:
            continue
        try:
            if re.search(pattern, haystack):
                return True
        except re.error:
            continue
    return False


def resolve_keyword_reply_mode(config: Any) -> str:
    """归一化关键词触发模式；非法值回退为 always（旧行为）。"""
    mode = (getattr(config, "keyword_reply_mode", "") or "always").strip().lower()
    return mode if mode in ("always", "llm_first", "tool_only") else "always"


def should_dedupe_outbound_repeat(
    text: str,
    sent_text: Optional[str],
    sent_at: Optional[float],
    window_seconds: float,
    now: float,
) -> bool:
    """出站去重判定：同会话在窗口内是否已发过同文本。

    返回值 True 表示应拦截本次发送（窗口内已发过同文本）。
    """
    target = str(text or "").strip()
    if not target:
        return False
    if sent_text is None or sent_at is None:
        return False
    if target != str(sent_text or "").strip():
        return False
    window = max(float(window_seconds or 0.0), 0.0)
    return 0 <= (now - float(sent_at)) < window


def _as_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, list):
        parts = []
        for segment in value:
            if isinstance(segment, dict):
                data = segment.get("data", {})
                if isinstance(data, dict):
                    text = data.get("text") or data.get("content") or data.get("url")
                else:
                    text = data or segment.get("text") or segment.get("content") or segment.get("url")
                parts.append(str(text or ""))
            else:
                parts.append(str(segment))
        return " ".join(part for part in parts if part)
    return str(value)


def extract_message_context(kwargs: Dict[str, Any]) -> Tuple[str, str, str, str]:
    message = kwargs.get("message") or {}
    if not isinstance(message, dict):
        message = {}
    sender = message.get("sender") or {}
    if not isinstance(sender, dict):
        sender = {}
    message_info = message.get("message_info") or {}
    if not isinstance(message_info, dict):
        message_info = {}
    user_info = message_info.get("user_info") or {}
    if not isinstance(user_info, dict):
        user_info = {}
    group_info = message_info.get("group_info") or {}
    if not isinstance(group_info, dict):
        group_info = {}
    additional_config = message_info.get("additional_config") or {}
    if not isinstance(additional_config, dict):
        additional_config = {}

    stream_id = (
        kwargs.get("stream_id")
        or message.get("stream_id")
        or message.get("chat_id")
        or kwargs.get("chat_id")
        or message.get("session_id")
        or additional_config.get("stream_id")
        or additional_config.get("chat_id")
        or additional_config.get("session_id")
        or ""
    )
    chat_id = (
        kwargs.get("chat_id")
        or message.get("chat_id")
        or message.get("group_id")
        or group_info.get("group_id")
        or stream_id
        or ""
    )
    group_id = (
        message.get("group_id")
        or message.get("chat_id")
        or kwargs.get("group_id")
        or group_info.get("group_id")
        or ""
    )
    user_id = (
        message.get("user_id")
        or sender.get("user_id")
        or kwargs.get("user_id")
        or user_info.get("user_id")
        or ""
    )
    raw_message = (
        message.get("processed_plain_text")
        or message.get("text")
        or kwargs.get("processed_plain_text")
        or message.get("raw_message")
        or kwargs.get("raw_message")
        or kwargs.get("text")
        or ""
    )
    return str(stream_id), normalize_group_id(group_id), str(user_id), _as_text(raw_message)


def resolve_self_id(kwargs: Dict[str, Any]) -> str:
    """从 hook 载荷里解析本机器人的账号 ID。"""
    message = kwargs.get("message") or {}
    if not isinstance(message, dict):
        message = {}
    message_info = message.get("message_info") or {}
    if not isinstance(message_info, dict):
        message_info = {}
    additional_config = message_info.get("additional_config") or {}
    if not isinstance(additional_config, dict):
        additional_config = {}
    return str(
        kwargs.get("self_id")
        or message.get("self_id")
        or additional_config.get("self_id")
        or additional_config.get("bot_id")
        or ""
    ).strip()


def is_self_message(kwargs: Dict[str, Any], user_id: str) -> bool:
    """判断当前消息是否由机器人自己发出（适配器可能因配置不回灌，仍做防御）。"""
    if not user_id:
        return False
    return str(user_id) == resolve_self_id(kwargs)


def extract_at_target_ids(kwargs: Dict[str, Any]) -> List[str]:
    """提取当前消息里所有 @ 目标账号 ID（来自 raw_message 的 at 段）。

    兼容 MaiBot 消息段（``data.target_user_id``）与 OneBot 段（``data.qq``）。
    """
    message = kwargs.get("message") or {}
    if not isinstance(message, dict):
        message = {}
    raw_message = message.get("raw_message")
    if isinstance(raw_message, str):
        raw_message = [{"type": "text", "data": raw_message}]
    if not isinstance(raw_message, list):
        return []
    targets: List[str] = []
    for segment in raw_message:
        if not isinstance(segment, dict):
            continue
        if str(segment.get("type") or "").strip() != "at":
            continue
        data = segment.get("data")
        if isinstance(data, dict):
            target = data.get("target_user_id") or data.get("qq") or data.get("user_id")
        else:
            target = data
        target_id = str(target or "").strip()
        if target_id and target_id not in targets:
            targets.append(target_id)
    return targets


def message_addresses_other(kwargs: Dict[str, Any], self_id: str) -> bool:
    """消息是否 @ 了别人（至少一个 at，且没有一个 at 是本机器人）。

    self_id 为空时，只要消息含 at 段就视为 @ 了别人（无法确认是 @ 自己，保守放行）。
    用于“群友 @作家助手/其他人 时关键词 hook 不自动回复”的误触防护。
    """
    targets = extract_at_target_ids(kwargs)
    if not targets:
        return False
    if not self_id:
        return True
    return all(str(t) != str(self_id) for t in targets)


def dig_path(data: Any, path: str) -> Any:
    if not path:
        return None
    current = data
    for part in path.split("."):
        if not isinstance(current, dict):
            return None
        current = current.get(part)
    return current


def parse_room_info(data: Any, number_field: str, link_field: str) -> Optional[Dict[str, str]]:
    if not isinstance(data, dict):
        return None
    room_number = dig_path(data, number_field)
    room_link = dig_path(data, link_field)
    if room_number is None and room_link is None:
        room_number = data.get("room_number") or data.get("room_no") or data.get("room_id")
        room_link = data.get("room_link") or data.get("share_url") or data.get("url")
    if room_number is None and room_link is None:
        return None
    return {
        "room_number": str(room_number or ""),
        "room_link": str(room_link or ""),
        "source": "api",
    }


def build_reply(room_info: Optional[Dict[str, Any]], template: str) -> str:
    room_number = ""
    room_link = ""
    creator_name = ""
    creator_id = ""
    target_words = ""
    target_minutes = ""
    if room_info:
        room_number = str(room_info.get("room_number") or room_info.get("room_id") or "")
        room_link = str(room_info.get("room_link") or room_info.get("share_url") or "")
        creator_name = str(room_info.get("creator_name") or "")
        creator_id = str(room_info.get("creator_id") or room_info.get("creator_cauthorid") or "")
        target_words = str(room_info.get("target_words") or "")
        target_minutes = str(room_info.get("target_minutes") or "")
    return template.format(
        room_number=room_number,
        room_link=room_link,
        creator_name=creator_name,
        creator_id=creator_id,
        target_words=target_words,
        target_minutes=target_minutes,
    )


def extract_room_creator(data: Any) -> Dict[str, str]:
    if not isinstance(data, dict):
        return {}
    result = data.get("result")
    if not isinstance(result, dict):
        return {}
    members = result.get("spellingHomeUserInfoVos")
    if not isinstance(members, list):
        return {}
    for item in members:
        if not isinstance(item, dict):
            continue
        user_flag = str(item.get("userFlag") or "")
        if user_flag != "1":
            continue
        return {
            "creator_id": str(item.get("cauthorid") or ""),
            "creator_name": str(item.get("name") or ""),
            "creator_cauthorid": str(item.get("cauthorid") or ""),
        }
    return {}


def extract_room_details(data: Any) -> Dict[str, str]:
    details = extract_room_creator(data)
    if isinstance(data, dict):
        result = data.get("result")
        if isinstance(result, dict):
            details["target_words"] = str(result.get("targetwords") or "")
            details["target_minutes"] = str(result.get("targetminute") or "")
    return details


MEMBER_WORD_COUNT_FIELDS = (
    "num",
    "wordCount",
    "words",
    "writeWords",
    "codeWords",
    "totalWords",
)


def extract_member_words(item: Any) -> int:
    if not isinstance(item, dict):
        return 0
    for key in MEMBER_WORD_COUNT_FIELDS:
        value = item.get(key)
        if value is None:
            continue
        try:
            return int(value)
        except (TypeError, ValueError):
            continue
    return 0


def extract_room_members(data: Any) -> List[Dict[str, Any]]:
    if not isinstance(data, dict):
        return []
    result = data.get("result")
    if not isinstance(result, dict):
        return []
    members = result.get("spellingHomeUserInfoVos")
    if not isinstance(members, list):
        return []
    parsed = []
    for item in members:
        if not isinstance(item, dict):
            continue
        parsed.append(
            {
                "cauthorid": str(item.get("cauthorid") or ""),
                "name": str(item.get("name") or ""),
                "words": extract_member_words(item),
            }
        )
    return parsed


def extract_room_ranking_members(data: Any) -> List[Dict[str, Any]]:
    if not isinstance(data, dict):
        return []
    result = data.get("result")
    if not isinstance(result, dict):
        return []
    members = result.get("spellingHomeRankingInfoVos")
    if not isinstance(members, list):
        return []
    parsed = []
    for item in members:
        if not isinstance(item, dict):
            continue
        cauthorid = str(item.get("cauthorId") or item.get("cauthorid") or "")
        parsed.append(
            {
                "cauthorid": cauthorid,
                "name": str(item.get("name") or ""),
                "words": extract_member_words(item),
            }
        )
    return parsed


def merge_room_members(
    base_members: List[Dict[str, Any]],
    detail_members: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    merged: List[Dict[str, Any]] = []
    index: Dict[str, int] = {}

    def member_key(member: Dict[str, Any]) -> str:
        cauthorid = str(member.get("cauthorid") or "")
        name = str(member.get("name") or "")
        return f"id:{cauthorid}" if cauthorid else f"name:{name}"

    for member in base_members:
        key = member_key(member)
        index[key] = len(merged)
        merged.append(dict(member))

    for member in detail_members:
        key = member_key(member)
        idx = index.get(key)
        words = int(member.get("words") or 0)
        name = str(member.get("name") or "")
        cauthorid = str(member.get("cauthorid") or "")
        if idx is not None:
            existing_words = int(merged[idx].get("words") or 0)
            if words:
                merged[idx]["words"] = words
            elif not existing_words:
                merged[idx]["words"] = 0
            if not merged[idx].get("name") and name:
                merged[idx]["name"] = name
            if not merged[idx].get("cauthorid") and cauthorid:
                merged[idx]["cauthorid"] = cauthorid
        else:
            index[key] = len(merged)
            merged.append(dict(member))
    return merged


def format_room_rank_list(members: List[Dict[str, Any]], limit: int = 13) -> List[str]:
    ranked = sorted(
        members,
        key=lambda member: (-int(member.get("words") or 0), str(member.get("name") or "")),
    )
    lines: List[str] = []
    shown = 0
    prev_words: Optional[int] = None
    current_rank = 0
    for member in ranked:
        words = int(member.get("words") or 0)
        if prev_words is None or words != prev_words:
            current_rank = shown + 1
            prev_words = words
        shown += 1
        if shown > int(limit or 0):
            break
        lines.append(f"第{current_rank}名--{member.get('name') or ''}--已码{words}字")
    return lines


def extract_home_id_from_current_response(data: Any) -> int:
    if not isinstance(data, dict):
        return 0
    result = data.get("result")
    if not isinstance(result, dict):
        return 0
    try:
        return int(result.get("homeId") or 0)
    except (TypeError, ValueError):
        return 0


HOME_STATUS_ENDED = 3  # 房间已结束（成员破目标字数/到期，客户端显示“再来一轮”或返回大厅）


def extract_home_status_from_current_response(data: Any) -> int:
    """读取 getCauthorHomeid 返回的 homeStatus；缺失或非数字按 0 处理。"""
    if not isinstance(data, dict):
        return 0
    result = data.get("result")
    if not isinstance(result, dict):
        return 0
    try:
        return int(result.get("homeStatus") or 0)
    except (TypeError, ValueError):
        return 0


def is_room_ended(home_status: Any) -> bool:
    """只有明确等于已结束状态(3)才视为已结束；其它值（含未知）保守视为有效，避免误伤进行中房间。"""
    try:
        return int(home_status or 0) == HOME_STATUS_ENDED
    except (TypeError, ValueError):
        return False


def extract_home_id_from_create_response(data: Any) -> Optional[str]:
    if not isinstance(data, dict):
        return None
    result = data.get("result")
    if not isinstance(result, dict):
        result = data
    for key in ("homeid", "homeId", "home_id"):
        value = result.get(key) if isinstance(result, dict) else None
        if value:
            return str(value)
    return None


def build_share_link(base_url: str, share_id: str) -> str:
    base = (base_url or DEFAULT_SHARE_BASE_URL).rstrip("/")
    return f"{base}/{share_id}"


def build_create_room_payload(config: ZuojiaRoomConfig) -> Dict[str, Any]:
    payload = dict(DEFAULT_CREATE_DEFAULTS)
    if config.create_room_defaults:
        payload.update({str(k): v for k, v in config.create_room_defaults.items()})
    if config.room_default_duration_minutes:
        payload["timeLimit"] = config.room_default_duration_minutes
    if config.room_default_word_goal:
        payload["worldLimit"] = config.room_default_word_goal
    if config.room_default_member_limit:
        payload["numbersLimit"] = config.room_default_member_limit
    if config.default_work_id:
        payload.setdefault("workId", config.default_work_id)
    return payload


AUTH_ERROR_MARKERS = ("auth_expired", "登录态", "登录校验", "重新登录", "4001", "接口 HTTP 4")


def is_auth_error_text(text: Any) -> bool:
    """登录态类错误判据。

    平台 4001 至少有两种文案：“登录态失效，请重新登录”与“登录校验异常，请重新登录”
    （2026-10-13 实测）。后者不含“登录态”三字，旧判据会把它落到 no_room_message，
    让用户以为“没有房/创建不了房”。
    """
    value = str(text or "")
    return any(marker in value for marker in AUTH_ERROR_MARKERS)


def _request_success(data: Any) -> bool:
    if not isinstance(data, dict):
        return False
    return_code = data.get("returnCode")
    if return_code is not None and return_code != 2000:
        return False
    code = data.get("code")
    if code is not None and str(code) != "2000":
        return False
    return True








class ZuojiaRoomClient:
    def __init__(self, auth_manager, config, logger):
        self.auth_manager = auth_manager
        self.config = config
        self.logger = logger
        self._operation_lock = asyncio.Lock()

    def _author_id(self):
        return str(self.config.author_id or "").strip()

    async def get_room(self, auto_create: bool, allowed_to_create: bool) -> Dict[str, Any]:
        async with self._operation_lock:
            return await self._get_room(auto_create, allowed_to_create)

    async def _get_room(self, auto_create: bool, allowed_to_create: bool) -> Dict[str, Any]:
        manual = self._manual_room()
        if manual:
            return manual
        if not self.config.api_base_url:
            raise RoomShareError("作家助手接口未配置")

        home_id, home_status = await self.get_current_room()
        ended_home_id = home_id if is_room_ended(home_status) else None
        if home_id and ended_home_id is None:
            return await self._build_room_info(home_id)
        if not auto_create:
            raise RoomShareError("no_room")
        if not allowed_to_create:
            raise RoomShareError("not_authorized")
        return await self._create_room(ended_home_id=ended_home_id)

    async def get_room_data(self, auto_create: bool, allowed_to_create: bool) -> Dict[str, Any]:
        async with self._operation_lock:
            return await self._get_room_data(auto_create, allowed_to_create)

    async def _get_room_data(
        self, auto_create: bool, allowed_to_create: bool
    ) -> Dict[str, Any]:
        if self._manual_room():
            raise RoomShareError("manual_room_no_data")
        if not self.config.api_base_url:
            raise RoomShareError("作家助手接口未配置")

        home_id, home_status = await self.get_current_room()
        ended_home_id = home_id if is_room_ended(home_status) else None
        if home_id and ended_home_id is None:
            data = await self._request_async(
                self.config.home_user_list_path,
                method="GET",
                params={"homeId": str(home_id)},
            )
            members = extract_room_members(data)
            if self.config.home_detail_path:
                try:
                    detail_data = await self._request_async(
                        self.config.home_detail_path,
                        method="GET",
                        params={"homeId": str(home_id)},
                    )
                    members = merge_room_members(
                        members,
                        extract_room_ranking_members(detail_data),
                    )
                except Exception as exc:
                    self.logger.warning("作家助手房间榜单暂不可用")
            return {
                "home_id": home_id,
                "created": False,
                "data": data,
                "members": members,
            }
        if not auto_create:
            raise RoomShareError("no_room")
        if not allowed_to_create:
            raise RoomShareError("not_authorized")
        room = await self._create_room(ended_home_id=ended_home_id)
        return {
            "home_id": room.get("room_number", ""),
            "created": True,
            "room": room,
        }

    def _manual_room(self) -> Optional[Dict[str, str]]:
        if self.config.manual_room_number or self.config.manual_room_link:
            return {
                "room_number": self.config.manual_room_number,
                "room_link": self.config.manual_room_link,
                "source": "manual",
            }
        return None

    async def get_current_room(self) -> Tuple[int, int]:
        """返回 (home_id, home_status)；home_status=3 表示房间已结束。"""
        data = await self._request_async(self.config.current_room_path, method="GET")
        return (
            extract_home_id_from_current_response(data),
            extract_home_status_from_current_response(data),
        )

    async def get_current_room_id(self) -> int:
        home_id, _ = await self.get_current_room()
        return home_id

    async def _build_room_info(self, home_id: Any) -> Dict[str, Any]:
        details = await self._get_room_details(home_id)
        share_id = await self._get_share_id(home_id, details.get("creator_cauthorid", ""))
        target_words = details.get("target_words") or str(self.config.room_default_word_goal or "")
        target_minutes = details.get("target_minutes") or str(self.config.room_default_duration_minutes or "")
        return {
            "room_number": str(home_id),
            "room_link": build_share_link(self.config.room_share_base_url, share_id),
            "source": "api",
            "share_id": share_id,
            "creator_id": details.get("creator_id", ""),
            "creator_name": details.get("creator_name", ""),
            "creator_cauthorid": details.get("creator_cauthorid", ""),
            "target_words": target_words,
            "target_minutes": target_minutes,
        }

    async def _get_room_details(self, home_id: Any) -> Dict[str, str]:
        try:
            data = await self._request_async(
                self.config.home_user_list_path,
                method="GET",
                params={"homeId": str(home_id)},
            )
            return extract_room_details(data)
        except Exception as exc:
            self.logger.warning("作家助手房间详情暂不可用")
            return {}

    async def _get_share_id(self, home_id: Any, creator_id="") -> str:
        author_id = self._author_id() or creator_id
        if not author_id:
            raise RoomShareError("缺少 author_id，请配置作家助手作者 ID")
        data = await self._request_async(
            self.config.invite_share_path,
            method="GET",
            params={"homeId": str(home_id), "authorId": author_id},
        )
        share_id = data.get("result")
        if not share_id:
            raise RoomShareError("获取分享码失败")
        return str(share_id)

    async def _create_room(self, ended_home_id: Optional[int] = None) -> Dict[str, Any]:
        """创建新房间。

        ``ended_home_id`` 为已结束旧房间时，按配置先退出旧房间再创建；
        createhome 响应解析失败时回退仅采用“进行中”的新房间，绝不返回已结束旧房。
        """
        if ended_home_id and self.config.quit_ended_room_before_create:
            await self._quit_ended_room(ended_home_id)
        payload = build_create_room_payload(self.config)
        data = await self._request_async(
            self.config.create_room_path,
            method="POST",
            json_body=payload,
        )
        home_id = extract_home_id_from_create_response(data)
        if not home_id:
            current_id, current_status = await self.get_current_room()
            if current_id and not is_room_ended(current_status):
                home_id = str(current_id)
        if not home_id:
            raise RoomShareError("创建房间失败：未返回房间号")
        if ended_home_id and str(home_id) == str(ended_home_id):
            raise RoomShareError("创建房间失败：仍返回已结束的旧房间")
        return await self._build_room_info(home_id)

    async def _quit_ended_room(self, home_id: Any) -> None:
        """退出已结束的旧房间；仅对已结束房间调用，失败只告警不阻断创建。"""
        try:
            await self._request_async(
                self.config.quit_home_path,
                method="GET",
                params={"homeId": str(home_id)},
            )
        except Exception as exc:
            self.logger.warning("退出已结束房间失败")

    async def _request_async(self, path, method="GET", params=None, json_body=None):
        try:
            body = json.dumps(json_body, ensure_ascii=False) if json_body is not None else None
            parsed = await self.auth_manager.request(method, path, params=params, data=body)
        except AuthError as exc:
            reason = "auth_expired" if exc.code in (4001, -31008) else "request_failed"
            raise RoomShareError(reason) from None
        except Exception:
            raise RoomShareError("request_failed") from None
        if not isinstance(parsed, dict):
            raise RoomShareError("invalid_response")
        if not _request_success(parsed):
            if parsed.get("returnCode", parsed.get("code")) in (4001, -31008):
                raise RoomShareError("auth_expired")
            raise RoomShareError("request_failed")
        return parsed

class ZuojiaRoomPlugin(MaiBotPlugin):
    config_model = ZuojiaRoomConfig

    # 持久化文件名：出站去重记录放到 data_dir，可在插件热重载后存活。
    DEDUP_FILE = "room_reply_dedup.json"

    def __init__(self) -> None:
        super().__init__()
        # 关键词自动回复冷却：stream_id -> 上次自动回复的 time.monotonic()
        self._last_keyword_reply_time: Dict[str, float] = {}
        # 最近一次成功发出房间邀请链接：stream_id -> time.monotonic()
        self._last_room_link_sent: Dict[str, float] = {}
        # 出站去重：stream_id -> {"text": str, "sent_at": float(monotonic)}
        self._recent_reply_sent: Dict[str, Dict[str, Any]] = {}
        # llm_first 兜底：stream_id -> 最近一次出站文本发送时间（供 watchdog 判断 LLM 是否回复）
        self._last_outbound_sent: Dict[str, float] = {}
        # llm_first 兜底挂起任务：stream_id -> (asyncio.Task, 挂起时 armed_at)
        self._pending_fallbacks: Dict[str, Tuple[asyncio.Task, float]] = {}
        # 每会话锁：串行化“推进 + 发送”临界区，消除并发双发
        self._locks: Dict[str, asyncio.Lock] = {}
        self._dedup_file: Optional[Path] = None
        self._login_task = None
        self._auth_health_task = None
        self._login_target = None
        self._terms_pending = {}
        self._last_auth_notice = 0.0
        self._lifecycle_lock = asyncio.Lock()

    async def on_load(self):
        async with self._lifecycle_lock:
            await self._start_services()

    async def _start_services(self):
        self._dedup_file = Path(self.ctx.paths.data_dir) / self.DEDUP_FILE
        self._load_dedup()
        if not self.config.plugin.enabled:
            return
        self.auth_manager = AuthManager(self.ctx.paths.data_dir, self._notify_qr,
            timeout_seconds=self.config.login_timeout_seconds)
        try:
            await self.auth_manager.start()
        except Exception:
            await self.auth_manager.close()
            self.auth_manager = None
            raise RuntimeError("授权服务启动失败，请检查浏览器依赖") from None
        self.client = ZuojiaRoomClient(self.auth_manager, self.config, self.ctx.logger)
        if self.config.auth_health_check_minutes > 0:
            self._auth_health_task = asyncio.create_task(self._auth_health_loop())

    async def _stop_services(self):
        await self._cancel_all_fallbacks()
        await self._cancel_login()
        task = self._auth_health_task
        self._auth_health_task = None
        if task:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        manager = getattr(self, "auth_manager", None)
        if manager:
            await manager.close()
        self.auth_manager = None
        self.client = None
        self._terms_pending.clear()
        self._persist_dedup()

    async def on_unload(self):
        async with self._lifecycle_lock:
            await self._stop_services()

    async def on_config_update(self, scope, config_data, version):
        if scope != CONFIG_RELOAD_SCOPE_SELF:
            return
        async with self._lifecycle_lock:
            await self._stop_services()
            self.set_plugin_config(config_data)
            await self._start_services()

    def _lock_for(self, stream_id: str) -> asyncio.Lock:
        lock = self._locks.get(stream_id)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[stream_id] = lock
        return lock

    def _load_dedup(self) -> None:
        if self._dedup_file is None:
            return
        try:
            if self._dedup_file.is_file():
                data = json.loads(self._dedup_file.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    cleaned: Dict[str, Dict[str, Any]] = {}
                    for sid, rec in data.items():
                        if isinstance(rec, dict) and rec.get("clock") == "unix" and isinstance(rec.get("text"), str):
                            cleaned[str(sid)] = {
                                "text": rec["text"],
                                "sent_at": float(rec.get("sent_at") or 0.0),
                            }
                    self._recent_reply_sent = cleaned
        except Exception as exc:  # noqa: BLE001
            self.ctx.logger.warning("作家助手房间操作失败")
            self._recent_reply_sent = {}

    def _persist_dedup(self) -> None:
        if self._dedup_file is None:
            return
        try:
            self._dedup_file.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                sid: {"text": rec["text"], "sent_at": rec["sent_at"], "clock": "unix"}
                for sid, rec in self._recent_reply_sent.items()
            }
            descriptor = os.open(self._dedup_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                os.chmod(self._dedup_file, 0o600)
                handle.write(json.dumps(payload, ensure_ascii=False))
        except Exception as exc:  # noqa: BLE001
            self.ctx.logger.warning("作家助手房间操作失败")

    def _mark_reply_sent(self, stream_id: str, text: str) -> None:
        clean = str(text or "").strip()
        if not stream_id or not clean:
            return
        self._recent_reply_sent[stream_id] = {"text": clean, "sent_at": time.time()}
        self._persist_dedup()

    def _recent_reply_is(self, stream_id: str, text: str, window_seconds: float) -> bool:
        clean = str(text or "").strip()
        if not clean:
            return False
        rec = self._recent_reply_sent.get(stream_id)
        if not rec:
            return False
        return should_dedupe_outbound_repeat(
            clean, rec.get("text"), rec.get("sent_at"), window_seconds, time.time()
        )

    def _in_keyword_cooldown(self, stream_id: str) -> bool:
        cooldown = float(self.config.keyword_cooldown_seconds or 0.0)
        if cooldown <= 0:
            return False
        last = self._last_keyword_reply_time.get(stream_id)
        return last is not None and 0 <= (time.monotonic() - last) < cooldown

    def _mark_room_link_sent(self, stream_id: str) -> None:
        """记录本会话最近一次成功发出房间邀请链接的时间。"""
        if stream_id:
            self._last_room_link_sent[stream_id] = time.monotonic()

    def _room_link_sent_within_window(self, stream_id: str) -> bool:
        """窗口期内本会话是否刚成功发出过房间邀请链接。"""
        window = float(self.config.room_data_suppress_after_link_seconds or 0.0)
        if window <= 0 or not stream_id:
            return False
        last = self._last_room_link_sent.get(stream_id)
        return last is not None and 0 <= (time.monotonic() - last) < window

    # ── llm_first 静默兜底 watchdog ────────────────────

    async def _cancel_all_fallbacks(self) -> None:
        pending = list(self._pending_fallbacks.items())
        self._pending_fallbacks.clear()
        for _stream_id, (task, _armed_at) in pending:
            if not task.done():
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):  # noqa: BLE001
                    pass

    def _schedule_fallback(self, stream_id: str, user_id: str, group_id: str, raw_message: str) -> bool:
        """llm_first 模式：为命中关键词的会话挂起一个静默兜底任务。

        若该会话已有挂起任务则不重复挂起；冷却期内也不挂起（交给 LLM 自然处理）。
        返回 True 表示已挂起。
        """
        if not stream_id:
            return False
        if stream_id in self._pending_fallbacks:
            return False
        if self._in_keyword_cooldown(stream_id):
            return False
        armed_at = time.monotonic()
        task = asyncio.create_task(
            self._fallback_after(
                stream_id,
                user_id,
                group_id,
                raw_message,
                armed_at,
            )
        )
        self._pending_fallbacks[stream_id] = (task, armed_at)
        return True

    async def _fallback_after(
        self,
        stream_id: str,
        user_id: str,
        group_id: str,
        raw_message: str,
        armed_at: float,
    ) -> None:
        """watchdog：等待 fallback_delay_seconds，若窗口内 LLM 没有任何出站回复，
        则由插件兜底发送确定性回复；已发送过/去重窗口内则跳过。"""
        try:
            delay = max(float(self.config.fallback_delay_seconds or 0.0), 0.0)
            if delay > 0:
                await asyncio.sleep(delay)

            # 到期后：若 LLM 已在 armed_at 之后有过出站文本发送 → 视为 LLM 已处理，放弃兜底
            if self._last_outbound_sent.get(stream_id, 0.0) >= armed_at:
                return

            # 计算兜底回复（此时才查询 API，LLM 已处理的情况下不浪费请求）
            reply = await self._build_keyword_reply(stream_id, group_id, user_id, raw_message)
            if not reply:
                return
            # 构建回复期间 LLM 可能已经回复，再校验一次
            if self._last_outbound_sent.get(stream_id, 0.0) >= armed_at:
                return
            sent = await self._send_auto_reply(stream_id, reply)
            if sent:
                self.ctx.logger.info("作家助手消息去重状态更新")
        finally:
            self._pending_fallbacks.pop(stream_id, None)

    # ── 出站去重收口（对自动回复 / 工具 / 主模型回复三路的最终防线） ──

    @HookHandler(
        "send_service.before_send",
        name="zuojia_room_share_dedup",
        description="记录会话最近出站文本时间，并拦截同会话在去重窗口内重复发送同一回复文本",
        mode=HookMode.BLOCKING,
        error_policy=ErrorPolicy.SKIP,
    )
    async def block_duplicate_reply(self, message: Any = None, **kwargs: Any) -> Optional[Dict[str, Any]]:
        del kwargs
        if not self.config.plugin.enabled:
            return None
        if not isinstance(message, dict):
            return None
        # 只对纯文本出站做去重/记录；图片/表情/空文本一律放行
        if bool(message.get("is_emoji")) or bool(message.get("is_picture")):
            return None
        text = (message.get("processed_plain_text") or "").strip()
        stream_id = str(message.get("session_id") or "")
        if not stream_id or not text:
            return None
        now = time.monotonic()
        # 记录最近出站文本时间：供 llm_first 兜底判断 LLM 是否已回复。
        self._last_outbound_sent[stream_id] = now
        window = float(self.config.reply_dedup_seconds or 0.0)
        if window <= 0:
            return None
        try:
            if self._recent_reply_is(stream_id, text, window):
                self.ctx.logger.info("作家助手消息去重状态更新")
                return {"action": "abort"}
        except Exception as exc:  # noqa: BLE001
            self.ctx.logger.warning("作家助手房间操作失败")
            return None
        return None


    async def _auth_health_loop(self):
        while True:
            await asyncio.sleep(max(1, self.config.auth_health_check_minutes) * 60)
            try:
                await self.auth_manager.renew()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if not isinstance(exc, AuthError) or exc.code not in (4001, -31008):
                    self.ctx.logger.warning("授权续期暂时失败，稍后重试")
                    continue
                now = time.monotonic()
                if self._last_auth_notice and now-self._last_auth_notice < 86400:
                    continue
                self._last_auth_notice = now
                for user in set(self.config.admin_user_ids):
                    try:
                        stream = await self.ctx.chat.get_stream_by_user_id(str(user))
                        if isinstance(stream, dict):
                            stream = stream.get("stream_id") or stream.get("session_id")
                        if stream:
                            await self.ctx.send.text(self.config.auth_expired_notice_template, stream)
                    except Exception:
                        self.ctx.logger.warning("作家助手授权提醒发送失败")

    async def _notify_qr(self, png):
        target = self._login_target
        if not target or not self._login_task or self._login_task.done():
            raise AuthError("登录已取消")
        stream, user = target
        if not is_login_user(user, self.config):
            raise AuthError("授权权限已变更")
        if not isinstance(png, bytes) or not png.startswith(b"\x89PNG\r\n\x1a\n"):
            raise AuthError("二维码格式无效")
        result = await self.ctx.send.image(base64.b64encode(png).decode("ascii"), stream)
        if result is False or (isinstance(result, dict) and result.get("success") is False):
            raise AuthError("二维码发送失败")

    async def _cancel_login(self):
        task = self._login_task
        self._login_task = None
        if task: task.cancel()
        manager = getattr(self, "auth_manager", None)
        if manager: await manager.cancel_login()
        if task: await asyncio.gather(task, return_exceptions=True)
        self._login_target = None

    async def _login_once(self, stream):
        try:
            await self.auth_manager.login()
            await self.ctx.send.text("拼字房授权成功，已验证房间查询。", stream)
        except asyncio.CancelledError:
            raise
        except Exception:
            await self.ctx.send.text("拼字房授权未完成，请稍后重新 /拼字登录。", stream)
        finally:
            self._login_target = None

    @Command("login", pattern=r"^/(拼字登录确认|拼字登录|拼字取消登录|拼字授权状态)$")
    async def handle_login(self, **kwargs):
        stream, group, user, raw = extract_message_context(kwargs)
        if not self.config.plugin.enabled: return True, "插件未启用", 2
        if not trigger_enabled("command", self.config.trigger_modes):
            return True, "命令触发未启用", 2
        if group or not stream or not is_login_user(user, self.config):
            return True, "仅配置管理员可在私聊操作授权。", 2
        command = raw.strip()
        if command == "/拼字取消登录":
            self._terms_pending.pop((user, stream), None)
            await self._cancel_login()
            reply = "已取消拼字登录。"
        elif command == "/拼字授权状态":
            return await self._handle_auth_status(stream, group, user)
        elif command == "/拼字登录确认":
            pending = self._terms_pending.pop((user, stream), 0)
            if not pending or time.monotonic()-pending > 600:
                reply = "请先 /拼字登录 阅读三份官方协议，再发送 /拼字登录确认。"
            elif self._login_task and not self._login_task.done():
                reply = "已有登录正在进行，可先 /拼字取消登录。"
            else:
                self._login_target = (stream, user)
                self._login_task = asyncio.create_task(self._login_once(stream))
                reply = "正在生成二维码，请在本私聊扫码并确认；可用 /拼字取消登录 停止。"
        else:
            self._terms_pending[(user, stream)] = time.monotonic()
            reply = ("请阅读并同意三份官方协议：\n"
                "用户服务协议：https://activity.write.qq.com/noah/202211071\n"
                "隐私政策：https://activity.write.qq.com/noah/202211072\n"
                "作品合作基础协议：https://activity.write.qq.com/noah/142855555\n"
                "同意后在本私聊发送 /拼字登录确认。无需提供密码或凭据。")
        await self.ctx.send.text(reply, stream)
        return True, reply, 2

    async def _handle_auth_status(self, stream_id, group_id, user_id):
        if group_id or not is_login_user(user_id, self.config):
            return True, "仅配置管理员可在私聊查询授权状态。", 2
        status = self.auth_manager.status()
        if inspect.isawaitable(status): status = await status
        authorized = bool(status.get("authorized", status.get("authenticated", status.get("has_auth", False))))
        active = bool(self._login_task and not self._login_task.done())
        reply = f"拼字房授权：{'已保存' if authorized else '未授权或已失效'}；扫码：{'进行中' if active else '未进行'}。"
        await self.ctx.send.text(reply, stream_id)
        return True, reply, 2

    @Tool(
        "zuojia_room_share_get_room_link",
        brief_description="查询作家助手拼字房并返回房间号与分享链接",
        detailed_description="返回当前拼字房信息；如果配置允许且没有房间，会尝试按默认设置创建房间。群聊中只要提到拼字房、码字房、房间、有房吗等拼字相关话题，都应调用本工具。调用成功后工具会直接把房间号和邀请链接发送到当前会话，模型不应再生成文字回复或调用 reply。",
        parameters=[],
    )
    async def get_room_link(self, stream_id: str, **kwargs):
        _, group_id, user_id, _ = extract_message_context({**kwargs, "stream_id": stream_id})
        if not self.config.plugin.enabled or not user_id or not is_allowed_chat(group_id, user_id, self.config):
            return {"success": False, "error": "chat_not_allowed"}
        if not trigger_enabled("tool", self.config.trigger_modes):
            return {"success": False, "error": "tool_trigger_disabled"}
        result = await self._safe_get_room(
            auto_create=self.config.create_room_if_missing,
            allowed_to_create=can_create_room(group_id, user_id, self.config),
        )
        reply = result.get("reply") or self.config.no_room_message
        # 统一走 _send_gated_reply_state：sent=已发出；suppressed=冷却/去重命中（先前已覆盖，
        # 按“已发送”处理避免模型补话）；failed/无会话=把 reply 交回 LLM 自行决定。
        state = "llm"
        if stream_id:
            state = await self._send_gated_reply_state(stream_id, reply)
        handled = state in ("sent", "suppressed")
        if state == "sent" and result.get("success"):
            # 只有真正发出房间邀请链接才记录窗口，避免被冷却/去重抑制时刷新
            self._mark_room_link_sent(stream_id)
        if result.get("success"):
            room = result.get("room") or {}
            if handled:
                return {
                    "success": True,
                    "content": TOOL_ALREADY_SENT_ROOM_LINK,
                    "room_number": room.get("room_number", ""),
                    "room_link": room.get("room_link", ""),
                }
            return {
                "success": True,
                "reply": reply,
                "room_number": room.get("room_number", ""),
                "room_link": room.get("room_link", ""),
            }
        if handled:
            return {
                "success": False,
                "content": TOOL_ALREADY_SENT_FAILURE,
                "error": result.get("error", "room_not_found"),
            }
        return {"success": False, "error": result.get("error", "room_not_found")}

    @Tool(
        "zuojia_room_share_get_room_data",
        brief_description="查询当前拼字房间成员已码字数榜单",
        detailed_description="有房时返回纯成员榜单；无房且配置允许时会自动创建房间并分享官方邀请文案；若本会话刚发过邀请链接且处于抑制窗口，则无房时不再建房/不再发链接（有房仍正常返回榜单）。群聊中只要提到拼字房、码字房、房间、有房吗等拼字相关话题，都应调用本工具。调用成功后工具会直接把房间榜单（或创建房间的邀请文案）发送到当前会话，模型不应再生成文字回复或调用 reply。",
        parameters=[],
    )
    async def get_room_data(self, stream_id: str, **kwargs):
        _, group_id, user_id, _ = extract_message_context({**kwargs, "stream_id": stream_id})
        if not self.config.plugin.enabled or not user_id or not is_allowed_chat(group_id, user_id, self.config):
            return {"success": False, "error": "chat_not_allowed"}
        if not trigger_enabled("tool", self.config.trigger_modes):
            return {"success": False, "error": "tool_trigger_disabled"}
        # 窗口期内禁止“无房自动建房→再发邀请链接”；有房榜单查询不受影响。
        suppress_creation = self._room_link_sent_within_window(stream_id)
        result = await self._safe_get_room_data(
            auto_create=self.config.room_data_create_if_missing and not suppress_creation,
            allowed_to_create=can_create_room(group_id, user_id, self.config),
        )
        if suppress_creation and not result.get("success") and result.get("error") == "no_room":
            self.ctx.logger.info("作家助手消息去重状态更新")
            return {"success": True, "content": TOOL_SUPPRESSED_AFTER_LINK}
        reply = result.get("reply") or self.config.room_data_no_room_message
        # 统一走 _send_gated_reply_state：sent=已发出；suppressed=冷却/去重命中（先前已覆盖，
        # 按“已发送”处理避免模型补话）；failed/无会话=把 reply 交回 LLM 自行决定。
        state = "llm"
        if stream_id:
            state = await self._send_gated_reply_state(stream_id, reply)
        handled = state in ("sent", "suppressed")
        if state == "sent" and result.get("created"):
            # 建房路径实际发出了邀请链接，同样记录窗口，防止后续再建房/再发链接
            self._mark_room_link_sent(stream_id)
        if result.get("success"):
            if result.get("created"):
                room = result.get("room") or {}
                if handled:
                    return {
                        "success": True,
                        "created": True,
                        "content": TOOL_ALREADY_SENT_ROOM_LINK,
                        "room_number": room.get("room_number", ""),
                        "room_link": room.get("room_link", ""),
                    }
                return {
                    "success": True,
                    "created": True,
                    "reply": reply,
                    "room_number": room.get("room_number", ""),
                    "room_link": room.get("room_link", ""),
                }
            if handled:
                return {
                    "success": True,
                    "created": False,
                    "content": TOOL_ALREADY_SENT_ROOM_DATA,
                    "rank_list": result.get("rank_lines", []),
                }
            return {
                "success": True,
                "created": False,
                "reply": reply,
                "rank_list": result.get("rank_lines", []),
            }
        if handled:
            return {
                "success": False,
                "content": TOOL_ALREADY_SENT_FAILURE,
                "error": result.get("error", "room_not_found"),
            }
        return {"success": False, "error": result.get("error", "room_not_found")}

    @Command("room", pattern=r"^/拼字房$")
    async def handle_room(self, **kwargs):
        stream_id, group_id, user_id, raw_message = extract_message_context(kwargs)
        if not self.config.plugin.enabled or not trigger_enabled("command", self.config.trigger_modes):
            return True, "命令触发未启用", 2
        if not is_allowed_chat(group_id, user_id, self.config):
            return True, "不在允许的聊天白名单中", 2
        result = await self._safe_get_room(
            auto_create=self.config.create_room_if_missing,
            allowed_to_create=can_create_room(group_id, user_id, self.config),
        )
        reply = result.get("reply") or self.config.no_room_message
        if stream_id:
            await self.ctx.send.text(reply, stream_id)
        return True, reply, 2

    @Command("room_data", pattern=r"^/拼字榜单$")
    async def handle_room_data(self, **kwargs):
        stream_id, group_id, user_id, _raw_message = extract_message_context(kwargs)
        if not self.config.plugin.enabled or not trigger_enabled("command", self.config.trigger_modes):
            return True, "命令触发未启用", 2
        if not is_allowed_chat(group_id, user_id, self.config):
            return True, "不在允许的聊天白名单中", 2
        result = await self._safe_get_room_data(
            auto_create=self.config.room_data_create_if_missing,
            allowed_to_create=can_create_room(group_id, user_id, self.config),
        )
        reply = result.get("reply") or self.config.room_data_no_room_message
        if stream_id:
            await self.ctx.send.text(reply, stream_id)
        return True, reply, 2


    @HookHandler(
        "chat.receive.before_process",
        name="zuojia_room_keyword_fallback",
        description="命中拼字房关键词：keyword_reply_mode=always 时直接回复并中止 LLM；llm_first 时放行 LLM、静默超时后由插件兜底回复；tool_only 时仅靠 LLM 工具",
        mode=HookMode.BLOCKING,
    )
    async def on_message_before_process(self, **kwargs):
        if not self.config.plugin.enabled:
            return {"action": "continue"}
        stream_id, group_id, user_id, raw_message = extract_message_context(kwargs)
        if is_self_message(kwargs, user_id):
            return {"action": "continue"}
        if not trigger_enabled("keyword", self.config.trigger_modes):
            return {"action": "continue"}
        if not is_allowed_chat(group_id, user_id, self.config):
            return {"action": "continue"}
        # 消息 @ 了别人（如群友 @作家助手 问房间）时不触发关键词自动回复/兜底，
        # 只对 @本机器人 或没有 @ 的消息响应，避免误发固定拼字房文案。
        if self.config.ignore_at_to_others and message_addresses_other(kwargs, resolve_self_id(kwargs)):
            return {"action": "continue"}
        # 斜杠命令交给 Command 处理器，避免关键词子串（如“房间”“拼字房”）吞掉 /拼字房、/拼字榜单 等命令
        if raw_message.lstrip().startswith("/"):
            return {"action": "continue"}
        if not match_keyword(raw_message, self.config.keywords) and not match_keyword_patterns(
            raw_message, self.config.keyword_patterns
        ):
            return {"action": "continue"}

        mode = resolve_keyword_reply_mode(self.config)

        # tool_only：关键词不做自动回复也不中止，交给 LLM 工具路径。
        if mode == "tool_only":
            return {"action": "continue"}

        # llm_first：放行给 LLM；若 LLM 在 fallback_delay_seconds 内没有任何出站回复，
        # 由 watchdog 兜底发送确定性回复（房间链接或失败文案）。
        if mode == "llm_first":
            self._schedule_fallback(stream_id, user_id, group_id, raw_message)
            return {"action": "continue"}

        # always（旧行为）：命中即必答并中止 LLM 流程；冷却期内不再重复自动回复。
        if self._in_keyword_cooldown(stream_id):
            return {"action": "continue"}
        result = await self._safe_get_room(
            auto_create=self.config.create_room_if_missing,
            allowed_to_create=can_create_room(group_id, user_id, self.config),
        )
        # 必答模式：查询成功回复房间链接；失败时按上下文区分文案——
        # 私聊/群内管理员保留详细失败文案（登录态失效/无权限/无房，便于排障），
        # 群聊普通成员以 keyword_group_failure_reply 为基础文案，默认用 LLM 生成更自然的回复
        # （keyword_failure_llm_reply=true，LLM 异常/超时/空结果回退基础文案；留空则放行给 LLM 自主回复），
        # 并中止后续 LLM 流程，保证关键词触发时一定有一条确定回复。
        if result.get("success"):
            reply = result.get("reply") or self.config.no_room_message
            state = await self._send_gated_reply_state(stream_id, reply)
            return {"action": "continue" if state == "failed" else "abort"}
        if self.config.keyword_must_reply:
            is_admin_context = is_auth_error_text(result.get("error")) or not normalize_group_id(group_id) or (
                bool(self.config.admin_user_ids)
                and is_allowed_user(user_id, self.config.admin_user_ids)
            )
            if is_admin_context:
                reply = result.get("reply") or self.config.no_room_message
            else:
                reply = (self.config.keyword_group_failure_reply or "").strip()
                if not reply:
                    return {"action": "continue"}
                if self.config.keyword_failure_llm_reply:
                    try:
                        llm_reply = await self._generate_group_failure_reply(reply, raw_message)
                        if (llm_reply or "").strip():
                            reply = llm_reply.strip()
                    except Exception as exc:
                        self.ctx.logger.warning("房间提示润色失败，使用默认文案")
            state = await self._send_gated_reply_state(stream_id, reply)
            return {"action": "continue" if state == "failed" else "abort"}
        return {"action": "continue"}

    async def _send_gated_reply_state(self, stream_id: str, text: str) -> str:
        """统一带闸门发送：返回三态 'sent' / 'suppressed' / 'failed'。

        在每会话锁内完成：冷却校验 → 出站去重校验 → 发送 → 记录冷却与去重。
        - sent: 本次确实发出
        - suppressed: 冷却或去重命中，未发送（先前已覆盖，调用方应按“已发送”处理）
        - failed: 参数非法 / send.text 异常或返回失败（调用方应把文本交回 LLM 自行决定）
        """
        clean = str(text or "").strip()
        if not stream_id or not clean:
            return "failed"
        async with self._lock_for(stream_id):
            if self._in_keyword_cooldown(stream_id):
                self.ctx.logger.info("作家助手消息去重状态更新")
                return "suppressed"
            if self._recent_reply_is(stream_id, clean, self.config.reply_dedup_seconds):
                self.ctx.logger.info("作家助手消息去重状态更新")
                return "suppressed"
            now = time.monotonic()
            try:
                sent = await self.ctx.send.text(clean, stream_id)
            except Exception as exc:  # noqa: BLE001
                self.ctx.logger.warning("作家助手房间操作失败")
                return "failed"
            if sent is not False:
                self._last_keyword_reply_time[stream_id] = now
                self._mark_reply_sent(stream_id, clean)
            if sent is False:
                self.ctx.logger.warning("作家助手自动回复发送失败")
                return "failed"
            return "sent"

    async def _send_auto_reply(self, stream_id: str, reply: str) -> bool:
        """发送一条确定性自动回复（always 模式 / llm_first 兜底共用）。

        在每会话锁内完成：冷却校验 → 出站去重校验 → 发送 → 记录冷却与去重。
        返回是否真正发送成功（被冷却/去重抑制或发送失败均为 False）。
        """
        return await self._send_gated_reply_state(stream_id, reply) == "sent"

    async def _build_keyword_reply(
        self, stream_id: str, group_id: str, user_id: str, raw_message: str
    ) -> str:
        """为 llm_first 兜底构造确定性回复文案。

        与 always 模式共用同样的成功/失败文案规则：有房返回房间链接；
        失败时私聊/管理员保留详细失败文案，群聊普通成员以 keyword_group_failure_reply 为基础文案
        （这里不再调用 LLM 润色——兜底本身就是 LLM 静默时才触发，避免再次等模型）。
        返回空串表示“无可用回复”（放行，不兜底发送）。
        """
        result = await self._safe_get_room(
            auto_create=self.config.create_room_if_missing,
            allowed_to_create=can_create_room(group_id, user_id, self.config),
        )
        if result.get("success"):
            return result.get("reply") or self.config.no_room_message
        if not self.config.keyword_must_reply:
            return ""
        is_admin_context = is_auth_error_text(result.get("error")) or not normalize_group_id(group_id) or (
            bool(self.config.admin_user_ids)
            and is_allowed_user(user_id, self.config.admin_user_ids)
        )
        if is_admin_context:
            return result.get("reply") or self.config.no_room_message
        return (self.config.keyword_group_failure_reply or "").strip()

    async def _generate_group_failure_reply(self, base_reply: str, user_text: str) -> str:
        """用 LLM 以基础文案为底生成更自然的群聊失败回复。

        超时 12 秒；异常或空结果由调用方回退到 ``base_reply``。
        """
        prompt = (
            "你是群聊里的机器人。有人提到了拼字房相关话题（原话：{user_text}），"
            "但当前没有可用的拼字房。请以「{base_reply}」的意思为基础，"
            "用你平时在群里的说话风格简短回一句（20 字以内），"
            "不要解释原因，不要使用引号或多余标点。"
        ).format(
            user_text=(user_text or "")[:100],
            base_reply=base_reply,
        )
        resp = await asyncio.wait_for(
            self.ctx.llm.generate(prompt, max_tokens=80, temperature=0.8),
            timeout=12,
        )
        return str(resp.get("response") or resp.get("content") or "").strip()

    async def _safe_get_room(self, auto_create: bool, allowed_to_create: bool) -> Dict[str, Any]:
        try:
            room = await self.client.get_room(
                auto_create=auto_create,
                allowed_to_create=allowed_to_create,
            )
            return {
                "success": True,
                "room": room,
                "reply": build_reply(room, self.config.reply_template),
            }
        except RoomShareError as exc:
            self.ctx.logger.warning("作家助手房间操作失败")
            error_text = str(exc)
            if is_auth_error_text(error_text):
                reply = self.config.auth_expired_notice_template
            elif error_text in ("request_failed", "invalid_response", "unexpected"):
                reply = "拼字房查询暂时失败，请稍后重试。"
            elif error_text == "not_authorized":
                reply = self.config.not_authorized_message
            elif error_text == "no_room":
                reply = self.config.no_room_message
            else:
                reply = "拼字房操作暂时失败，请管理员检查配置或稍后重试。"
            return {"success": False, "error": error_text, "reply": reply}
        except Exception as exc:
            self.ctx.logger.warning("作家助手房间操作失败")
            return {"success": False, "error": "unexpected", "reply": "拼字房操作暂时失败，请管理员检查配置或稍后重试。"}

    async def _safe_get_room_data(
        self, auto_create: bool, allowed_to_create: bool
    ) -> Dict[str, Any]:
        try:
            result = await self.client.get_room_data(
                auto_create=auto_create,
                allowed_to_create=allowed_to_create,
            )
            if result.get("created"):
                room = result.get("room") or {}
                return {
                    "success": True,
                    "created": True,
                    "room": room,
                    "reply": build_reply(room, self.config.reply_template),
                }
            members = result.get("members") or []
            rank_lines = format_room_rank_list(members, self.config.room_data_rank_limit)
            reply = "\n".join(rank_lines) if rank_lines else "当前房间暂无可展示的成员榜单。"
            return {
                "success": True,
                "created": False,
                "room": result,
                "rank_lines": rank_lines,
                "reply": reply,
            }
        except RoomShareError as exc:
            self.ctx.logger.warning("作家助手房间操作失败")
            error_text = str(exc)
            if is_auth_error_text(error_text):
                reply = self.config.auth_expired_notice_template
            elif error_text in ("request_failed", "invalid_response", "unexpected"):
                reply = "拼字房查询暂时失败，请稍后重试。"
            elif error_text == "not_authorized":
                reply = self.config.not_authorized_message
            elif error_text == "manual_room_no_data":
                reply = "当前房间为手动维护配置，暂无法查询成员榜单。"
            elif error_text == "no_room":
                reply = self.config.room_data_no_room_message
            else:
                reply = "拼字房操作暂时失败，请管理员检查配置或稍后重试。"
            return {"success": False, "error": error_text, "reply": reply}
        except Exception as exc:
            self.ctx.logger.warning("作家助手房间操作失败")
            return {
                "success": False,
                "error": "unexpected",
                "reply": "拼字房操作暂时失败，请管理员检查配置或稍后重试。",
            }


def create_plugin():
    return ZuojiaRoomPlugin()
