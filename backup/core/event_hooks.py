"""事件钩子：把 AstrBot 事件翻译成「实时归档 / 触发式备份」。

不 import astrbot，全部按鸭子类型取字段，便于单测与多适配器共用。
真实适配器字段不一致时，改这一个文件即可，主流程不动。
"""

import logging
from datetime import datetime
from typing import Any, Optional

from .auto_trigger import AutoTrigger
from .models import Message

logger = logging.getLogger(__name__)

# 各平台「机器人被踢 / 群解散」通知的子类型名，命中即做最后一次备份
KICK_SUBTYPES = {"kick", "kick_off", "bot_kicked", "bot_leave", "leave"}
DISBAND_SUBTYPES = {"disband", "dismiss", "group_disband", "group_dismiss"}
# 成员变动类通知：群里正常有人退群也走这里，先聚合再判断
DECREASE_SUBTYPES = {"decrease", "leave", "quit", "member_leave", "kick"}


def _get(obj: Any, name: str, default=None):
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def extract_group_id(event) -> Optional[int]:
    """从事件里取群号，兼容 group_id / get_group_id() / 嵌套 message_obj。"""
    for name in ("group_id", "group"):
        value = _get(event, name)
        if isinstance(value, int) and value > 0:
            return value
        if isinstance(value, str) and value.isdigit():
            return int(value)
    fn = _get(event, "get_group_id")
    if callable(fn):
        try:
            value = fn()
            if value:
                return int(value)
        except Exception:
            pass
    for holder in ("message_obj", "raw_message", "message"):
        inner = _get(event, holder)
        gid = _get(inner, "group_id") if inner is not None else None
        if isinstance(gid, int) and gid > 0:
            return gid
    return None


def parse_event_message(event, group_id: int) -> Optional[Message]:
    """把一条实时消息事件转成 Message。取不到 message_id 就返回 None。

    AstrBot 的 message_str 是已解析的纯文本，message_obj 里则是原始消息链；
    两者都给 parse_message，它会优先用消息链拿媒体与引用。
    """
    from .parser import parse_message

    raw = _get(event, "message_obj") or _get(event, "raw_message")
    text = _get(event, "message_str") or ""
    if raw is None and not text:
        return None

    msg = parse_message(raw if raw is not None else text, group_id)
    if not msg.message_id or not msg.content and not msg.media_paths:
        return None
    sender_id = _get(event, "sender_id") or _get(event, "user_id")
    if sender_id and isinstance(sender_id, int):
        msg.sender_id = sender_id
    name = _get(event, "sender_name") or _get(event, "nickname")
    if isinstance(name, str) and name:
        msg.sender_name = name
    return msg


def classify_group_event(payload) -> tuple[Optional[int], str]:
    """从原始通知里认 (群号, 事件类型)。认不出返回 (None, '')。"""
    group_id = None
    for name in ("group_id", "group", "target_id"):
        value = _get(payload, name)
        if isinstance(value, int) and value > 0:
            group_id = value
            break
        if isinstance(value, str) and value.isdigit():
            group_id = int(value)
            break
    subtype = (
        _get(payload, "sub_type") or _get(payload, "subtype") or _get(payload, "notice_type")
    )
    return group_id, str(subtype or "").lower()


class EventHooks:
    """事件钩子的纯逻辑部分。main.py 只负责把 AstrBot 事件喂进来。"""

    def __init__(
        self,
        db,
        auto_trigger: AutoTrigger,
        archive: bool = True,
        on_leave: bool = True,
        on_disband: bool = True,
        cooldown_seconds: int = 1800,
    ):
        self.db = db
        self.trigger = auto_trigger
        self.archive = archive
        self.on_leave = on_leave
        self.on_disband = on_disband
        self.cooldown = cooldown_seconds
        self._last_kick_backup: dict[int, datetime] = {}

    # ---------- 实时归档 ----------
    def archive_message(self, event, backup_id: str = "realtime") -> Optional[Message]:
        """把一条实时消息写进库。不下载媒体：实时归档只求「消息不丢」。"""
        if not self.archive:
            return None
        group_id = extract_group_id(event)
        if group_id is None:  # 私聊归档暂未支持，留出口给后续
            return None
        msg = parse_event_message(event, group_id)
        if msg is None:
            return None
        if self.db.message_exists(msg.message_id, group_id):
            return None
        self.db.save_message(msg, backup_id)
        return msg

    async def archive_with_media(self, event, downloader=None, backup_id: str = "realtime"):
        """实时归档 + 顺带补下载媒体。

        默认路径不下载：消息量大的群里同步下载会拖慢每条消息的处理。
        这里给一个显式入口，定时备份之外的「重要的那几条」可以走它。
        """
        msg = self.archive_message(event, backup_id)
        if msg is None or downloader is None or not msg.media_paths:
            return msg
        for i, url in enumerate(list(msg.media_paths)):
            if not isinstance(url, str) or not url.startswith("http"):
                continue
            try:
                msg.media_paths[i] = await downloader.download(
                    url, msg.message_id, msg.timestamp
                )
            except Exception as e:  # 单文件失败不阻塞（红线 2）
                msg.media_paths[i] = f"[下载失败] {url}"
                logger.warning("实时归档媒体失败 msg=%s err=%s", msg.message_id, type(e).__name__)
        self.db.update_message_media(msg.message_id, msg.group_id, msg.media_paths)
        return msg

    # ---------- 触发式备份：退群聚合 ----------
    def note_group_decrease(self, group_id: int) -> bool:
        """群人数下降一次，返回是否应当立即备份。"""
        count = self.trigger.record_leave(group_id)
        return self.trigger.should_trigger_mass_leave(group_id)

    # ---------- 触发式备份：解散 / 被踢 ----------
    def handle_notice(self, payload, event=None) -> tuple[Optional[int], str, bool]:
        """统一处理一条通知事件。返回 (群号, 事件类型, 是否应当立即备份)。

        拆成三段是为了让 main.py 只做接线：
        认群号 → 退群聚合 → 解散/被踢判定。
        """
        group_id, subtype = classify_group_event(payload)
        if group_id is None and event is not None:
            group_id = extract_group_id(event)
        if group_id is None:
            return None, "", False
        if subtype in DECREASE_SUBTYPES and subtype not in DISBAND_SUBTYPES:
            return group_id, subtype, self.note_group_decrease(group_id)
        return group_id, subtype, self.note_group_event(group_id, subtype)

    def note_group_event(self, group_id: int, subtype: str = "") -> bool:
        subtype = (subtype or "").lower()
        if subtype in DISBAND_SUBTYPES and self.on_disband:
            return self.trigger.on_disband(group_id)
        if subtype in KICK_SUBTYPES and self.on_leave:
            last = self._last_kick_backup.get(group_id)
            now = datetime.now()
            if last and (now - last).total_seconds() < self.cooldown:
                return False
            self._last_kick_backup[group_id] = now
            return True
        return False
