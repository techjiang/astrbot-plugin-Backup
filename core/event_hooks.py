"""事件钩子：把 AstrBot 事件翻译成「实时归档 / 触发式备份」。

除了 `astrbot.api.logger`，不依赖 astrbot：事件字段全部按鸭子类型取，便于单测与
多适配器共用。真实适配器字段不一致时，改这一个文件即可，主流程不动。
"""

import inspect
from datetime import datetime, timedelta
from typing import Any, Optional

from astrbot.api import logger

from .auto_trigger import AutoTrigger
from .parser import is_event_payload
from .commands import PermissionChecker
from .models import PRIVATE_ARCHIVE_GROUP, Message


# 「机器人自己被踢」的通知子类型。OneBot 协议里这个值是 kick_me，不叫 kick_off。
# 注意不能把 `leave` 放进来：那是「有成员退群」，被踢的可以是别人。
KICK_SUBTYPES = {"kick_me", "kick_self", "bot_kicked", "bot_leave", "self_kick"}
DISBAND_SUBTYPES = {"disband", "dismiss", "group_disband", "group_dismiss"}
# 「有人退群」的通知子类型：先聚合，达到阈值再备份
DECREASE_SUBTYPES = {"decrease", "leave", "quit", "member_leave", "member_kick"}
# 通知里出现这些 type（而非 sub_type）时，按消息重投处理：同一条通知换个投递
# 路径再进来一次，不能当成「又退了一个人」
NOTICE_ECHO_TYPES = {"message", "message_sent", "send", "echo"}

# 适配器探测：只记「有没有这个字段」，不记值——值里可能是消息内容
EVENT_FIELDS = (
    "group_id",
    "group",
    "message_obj",
    "raw_message",
    "message_str",
    "sender_id",
    "user_id",
    "sender_name",
    "nickname",
)
NOTICE_FIELDS = ("post_type", "notice_type", "sub_type", "subtype", "group_id", "group")

# 私聊消息在库里的 group_id（负数）定义挪到了 models 里，这里仍然是同一个名字：
# `from .models import PRIVATE_ARCHIVE_GROUP` 之后它对 import 本模块的人照旧可见。

#: 同一条通知被两个 handler 各处理一次时的去重窗口。AstrBot 的处理器分发是
#: 广播式的（见 `pipeline/process_stage` 里对 `star_handlers_registry` 的遍历），
#: `on_message` 与 `on_notice` 都挂在 `EventMessageType.ALL` 上，同一条事件会
#: 依次进两个函数。窗口只需覆盖「同一次分发」，不需要覆盖分钟级。
NOTICE_DEDUPE_SECONDS = 120


def event_fingerprint(event) -> str:
    """给一条事件算个去重指纹：只取消息/通知的 ID 与群号，不含正文。

    取不到 message_id 就退回「群号 + 类型 + 发送者 + 时间戳」，仍不碰内容——红线 1。
    """
    obj = _attr(event, "message_obj")
    parts = []
    for holder in (obj, event):
        if holder is None:
            continue
        for name in ("message_id", "message_seq"):
            value = _attr(holder, name)
            if value not in (None, ""):
                parts.append(f"{name}={value}")
        if parts:
            break
    if not parts:
        for name in ("timestamp", "time"):
            value = _attr(obj, name) if obj is not None else None
            if value not in (None, ""):
                parts.append(f"{name}={value}")
                break
        sender = _attr(_attr(obj, "sender"), "user_id") if obj is not None else None
        if sender not in (None, ""):
            parts.append(f"from={sender}")
    group = None
    for holder in (obj, event):
        value = _attr(holder, "group_id") if holder is not None else None
        if value not in (None, ""):
            group = value
            break
    kind = ""
    for holder in (obj, event):
        value = _attr(holder, "type") if holder is not None else None
        value = getattr(value, "value", value)
        if value:
            kind = str(value)
            break
    parts.append(f"group={group}")
    parts.append(f"type={kind}")
    return "|".join(parts)


def _get(obj: Any, name: str, default=None):
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


# 从事件里摊出来的「结构字段」。只搬类型和号码，绝不碰消息内容——
# 红线 1：消息是隐私，日志与中间结构里都不该出现正文。
NOTICE_KEYS = (
    "post_type",
    "notice_type",
    "message_type",
    "sub_type",
    "subtype",
    "group_id",
    "user_id",
    "target_id",
    "operator_id",
)


def _attr(obj, name, default=None):
    """取属性但绕开 property，理由见 parser._safe_get。"""
    if isinstance(obj, dict):
        return obj.get(name, default)
    try:
        inspected = inspect.getattr_static(obj, name)
    except AttributeError:
        return default
    if isinstance(inspected, property):
        return default
    return getattr(obj, name, default)


def notice_payload(event) -> dict:
    """把 AstrBot 事件里的通知结构摊成 dict，供 `handle_notice` 认类型。

    AstrBot 不会把 `notice_type` / `sub_type` 挂到事件对象上：它把 OneBot 原始
    事件整份塞进 `message_obj.raw_message`（aiocqhttp 的 `Event` 就是个 dict 子类）。
    原来的写法只读 `event.post_type` / `event.message_obj.post_type`，在这两个位置
    都是 None——于是每条通知都摊成 `{'group_id': '100'}`，`sub_type` 恒空。
    退群聚合、群解散、机器人被踢三个功能因此一次都没触发过，日志里也看不出问题。
    """
    out: dict = {}

    def collect(node) -> None:
        if node is None:
            return
        for key in NOTICE_KEYS:
            if key in out:
                continue
            value = node.get(key) if isinstance(node, dict) else _attr(node, key)
            if value is None or value == "":
                continue
            out[key] = value

    for holder in ("raw_message", "message_obj"):
        inner = _attr(event, holder)
        collect(inner)
        # 关键的一层：OneBot 原始事件藏在 message_obj.raw_message 里
        collect(_attr(inner, "raw_message"))
    collect(event)
    return out


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
    # `raw_message` 是 OneBot 原始事件 dict，**不是**消息本体：它只有
    # `raw_message: "你好"` 这一个字符串字段，消息链根本不在里头。把它当消息解析，
    # 会走进 parser 的 `_segments` 兜底分支 `return [raw]`，整份原始事件被当成
    # 「一段」，扣出一个既不像消息 ID、也不像群号的正文写进库；真正文、媒体、
    # 引用全丢。所以只有在 `raw_message` 本身就是一条聊天消息、而且事件上没有任何
    # 结构化消息可用时，才把它当消息的来源。
    if is_event_payload(raw):
        raw = None
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
    """从原始通知里认 (群号, 事件类型)。认不出返回 (None, '')。

    OneBot v11 把类型放在 `notice_type`（group_decrease / group_disband ……），
    具体原因放在 `sub_type`（leave / kick / kick_me ……）。反过来取会把
    `group_decrease` 当成类型，那样所有退群通知都认不出来，聚合永远不触发。
    """
    group_id = None
    for name in ("group_id", "group", "target_id"):
        value = _get(payload, name)
        if isinstance(value, int) and value > 0:
            group_id = value
            break
        if isinstance(value, str) and value.isdigit():
            group_id = int(value)
            break
    subtype = _get(payload, "sub_type") or _get(payload, "subtype") or _get(payload, "notice_type")
    return group_id, str(subtype or "").lower()


def notice_candidates(payload) -> list:
    """一条通知可能被包了一层（`{'post_type': 'notice', 'notice': {...}}`）。"""
    if payload is None:
        return []
    inner = payload.get("notice") if isinstance(payload, dict) else _get(payload, "notice")
    return [payload, inner] if inner is not None and inner is not payload else [payload]


def is_message_echo(payload) -> bool:
    """这条通知其实是消息的重投（post_type=message），不是群事件。"""
    for item in notice_candidates(payload):
        if str(_get(item, "post_type", "") or "").lower() in NOTICE_ECHO_TYPES:
            return True
    return False


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
        archive_private: bool = False,
        private_group_id: int = PRIVATE_ARCHIVE_GROUP,
        kick_cooldown_seconds: int = 86400,
    ):
        self.db = db
        self.trigger = auto_trigger
        self.archive = archive
        # scope.private_messages 之前也是空头支票：配置项写着「私聊（默认关）」，
        # 代码里私聊事件连归档都不进。开着它，私聊消息落在一个虚拟群号下，
        # 与真正的群号（正整数）不会撞车，导出/恢复流程一行都不用改
        self.archive_private = archive_private
        self.private_group_id = private_group_id
        self.on_leave = on_leave
        self.on_disband = on_disband
        self.cooldown = cooldown_seconds
        # 被踢是一辈子一次的事。原来这里跟退群聚合共用 30 分钟冷却：AstrBot 会
        # 把同一条 notice 投给 `on_message` 和 `on_notice` 两个 handler，而
        # `on_message` 那条路径认不出「这是通知」也会走到触发判定——第一次跑完
        # 备份，半小时后同一个事件再来一遍，机器人都已经不在群里了，
        # `get_group_msg_history` 只会失败，回执写进日志，备份记录多一条 failed。
        # 冷却窗口放到一天，等于「被踢只备一次」。
        self.kick_cooldown = kick_cooldown_seconds
        self._last_kick_backup: dict[int, datetime] = {}
        # 同一条通知会同时进 on_message 和 on_notice。退群聚合按「次数」计数，
        # 一次退群被数两遍 → 阈值提前一半就到 → 备份多跑一轮；解散/被踢的冷却
        # 窗口是按事件间隔算的，两次调用几乎同时，第二次能命中冷却，所以只有
        # 退群聚合会真的翻倍。这里记「这条通知已经处理过」，第二个 handler 直接跳过
        self._handled_notices: dict[str, datetime] = {}

    def reconfigure(self, config: Optional[dict] = None) -> None:
        """把 `scope` / `auto_trigger` 读进运行参数。构造和 `config set` 共用。

        原来这些开关只在 `BackupPlugin.__init__` 里读一次并传进来，
        `/backup config set scope.group_messages false` 回执「✅ 已设置」，而钩子
        照旧归档每条群消息——「关掉了还在存」比「关不掉」更糟，用户以为自己已经
        让它别再记了。
        """
        scope = (config or {}).get("scope") or {}
        self.archive = bool(scope.get("group_messages", True))
        self.archive_private = bool(scope.get("private_messages", False))

    def reconfigure_triggers(self, config: Optional[dict] = None) -> None:
        """`auto_trigger` 里与钩子相关的开关。"""
        triggers = (config or {}).get("auto_trigger") or {}
        self.on_leave = bool(triggers.get("on_bot_kicked", True))
        self.on_disband = bool(triggers.get("on_group_disband", True))

    # ---------- 实时归档 ----------
    def archive_message(self, event, backup_id: str = "realtime") -> Optional[Message]:
        """把一条实时消息写进库。不下载媒体：实时归档只求「消息不丢」。"""
        if not self.archive:
            return None
        group_id = extract_group_id(event)
        if group_id is None:
            if not self.archive_private:
                return None
            group_id = self.private_group_id
        if is_event_payload(_attr(event, "message_obj")):
            # 通知/请求事件：不是聊天消息，不进消息库（理由见 parser.is_event_payload）
            return None
        msg = parse_event_message(event, group_id)
        if msg is None:
            return None
        existed = self.db.message_exists(msg.message_id, group_id)
        # 已在库的消息也要再写一次：同一条消息被投递两次时，第二次解析出的媒体路径
        # 和昵称不该因为「库里有了」就丢掉（`save_message` 的更新分支只取并集，
        # 不会覆盖内容）。只是不再返回，调用方拿到的「新归档」必须真的是新的。
        self.db.save_message(msg, backup_id)
        if not existed:
            # 新消息才有「从没覆盖过」这回事。已在库的不登记：那份备份早就登记过，
            # 重复登记只会让清单无谓变大
            self.db.add_sources(backup_id, [msg])
        return None if existed else msg

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
    def seen_notice(self, event, now: Optional[datetime] = None) -> bool:
        """这条通知是不是刚被另一个 handler 处理过。第一次返回 False。

        不这么做的话，`on_message` 和 `on_notice` 会各数一次退群：阈值 10 的群实际
        只退了 5 个人就触发备份，而且退群窗口里的人数永远比真实值多一倍。
        """
        key = event_fingerprint(event)
        if not key or key == "group=None|type=":
            return False
        now = now or datetime.now()
        cutoff = now - timedelta(seconds=NOTICE_DEDUPE_SECONDS)
        for old in [k for k, t in self._handled_notices.items() if t < cutoff]:
            self._handled_notices.pop(old, None)
        last = self._handled_notices.get(key)
        self._handled_notices[key] = now
        return last is not None and last >= cutoff

    def handle_notice(self, payload, event=None, group_id: Optional[int] = None):
        """统一处理一条通知事件。返回 (群号, 事件类型, 是否应当立即备份, 是否计入聚合)。

        拆成三段是为了让 main.py 只做接线：
        认群号 → 退群聚合 → 解散/被踢判定。

        第四个返回值是给接线层用的：退群这条路径**已经**数过一个人了。而 AstrBot
        会把同一条 notice 广播给 `on_message` 和 `on_notice` 两个 handler（两个都
        挂在 `EventMessageType.ALL` 上，见 `pipeline/process_stage` 的实现），
        第二个调用方不能再数一遍——否则阈值 10 的群退了 5 个人就触发。
        去重指纹在这里拿不到（这里只有一个 dict），所以由接线层把 `group_id`
        传进来、用它加指纹。
        """
        if is_message_echo(payload):
            # on_message 已经归档过了。同一条消息再走一遍退群聚合，等于凭空
            # 造出 N 个退群者，一次普通聊天就能把「大量退群」的阈值顶满
            return None, "", False, False
        found, subtype = None, ""
        for item in notice_candidates(payload):
            found, subtype = classify_group_event(item)
            if found is not None or subtype:
                break
        if found is None:
            found = group_id if group_id is not None else extract_group_id(event)
        if found is None:
            return None, "", False, False
        if subtype in DECREASE_SUBTYPES and subtype not in DISBAND_SUBTYPES:
            return found, subtype, self.note_group_decrease(found), True
        return found, subtype, self.note_group_event(found, subtype), False

    def note_group_event(self, group_id: int, subtype: str = "") -> bool:
        subtype = (subtype or "").lower()
        if subtype in DISBAND_SUBTYPES and self.on_disband:
            return self.trigger.on_disband(group_id)
        if subtype in KICK_SUBTYPES and self.on_leave:
            last = self._last_kick_backup.get(group_id)
            now = datetime.now()
            if last and (now - last).total_seconds() < self.kick_cooldown:
                return False
            self._last_kick_backup[group_id] = now
            return True
        return False

    # ---------- 适配器探测 ----------
    #: 这些字段是对象/列表，`_get` 会把它们整个读出来；日志里只记存在性
    PROBE_FIELDS = EVENT_FIELDS + ("role", "message", "type", "group_owner", "group_admins")

    def describe_event(self, event) -> str:
        """第一次收到真实事件时打一行字段清单（只列字段名，不含值与内容）。

        只能实机确认的那几处——群号从哪个字段读、退群/被踢的 sub_type 到底叫什么
        ——靠这行日志对齐，不用靠猜。

        `_get` 是 `getattr`，读 `message` / `message_obj` / `group` 这类字段会把整个
        消息对象捞出来。这里只判断「有没有」并且 `repr` 里绝不带值——红线 1 说消息
        内容不进日志，而日志里出现对象 repr 就等于把正文写进去了。
        """
        present = []
        for name in self.PROBE_FIELDS:
            value = _get(event, name)
            if value in (None, "", [], {}):
                continue
            present.append(name)
        group_id = extract_group_id(event)
        is_admin = PermissionChecker(True).check_event(event).allowed
        return (
            f"事件字段：{'、'.join(dict.fromkeys(present)) or '（无）'}"
            f" | 认出群号：{group_id if group_id is not None else '失败'}"
            f" | 权限判定：{'通过' if is_admin else '拒绝'}"
        )

    def describe_notice(self, payload) -> str:
        present = []
        for item in notice_candidates(payload):
            present += [n for n in NOTICE_FIELDS if _get(item, n) not in (None, "", [], {})]
        group_id, subtype = classify_group_event(payload)
        return (
            f"通知字段：{'、'.join(dict.fromkeys(present)) or '（无）'}"
            f" | 认出：群 {group_id if group_id is not None else '失败'}"
            f" / 类型 {subtype or '未知'}"
        )
