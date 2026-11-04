"""平台消息归一化：把各平台各异的原始消息解析成统一的 Message。

平台对象可能是 AstrBot 的事件/消息链，也可能是已经序列化好的 dict，
这里对两种形态都做兼容，取不到就退化为 UNKNOWN，绝不抛异常打断备份。
"""

import json
import logging
import time
from datetime import datetime
from typing import Any, Optional

from .models import Message, MessageType

logger = logging.getLogger(__name__)

_TYPE_KEYS = {
    "text": MessageType.TEXT,
    "plain": MessageType.TEXT,
    "image": MessageType.IMAGE,
    "picture": MessageType.IMAGE,
    "face": MessageType.TEXT,
    "mface": MessageType.IMAGE,
    "record": MessageType.VOICE,
    "voice": MessageType.VOICE,
    "audio": MessageType.VOICE,
    "video": MessageType.VIDEO,
    "file": MessageType.FILE,
    "forward": MessageType.FORWARD,
    "node": MessageType.FORWARD,
    "reply": MessageType.REPLY,
    "at": MessageType.AT,
}


def _get(obj: Any, key: str, default=None):
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _to_datetime(value) -> datetime:
    """统一成 naive 本地时间。

    时间戳在库里是 `%Y-%m-%d %H:%M:%S.%f` 字符串，tz-aware 值落库时会连带丢掉
    时区，再读回来就凭空多了几小时——增量水位是 (timestamp, message_id) 比较的，
    脏点一旦写下，那段时间的正常消息全被判成「更旧」而跳过。
    """
    if isinstance(value, datetime):
        return _naive(value)
    if value is None:
        return datetime.now()
    try:
        ts = float(value)
        if ts > 1e11:  # 毫秒
            ts /= 1000
        return datetime.fromtimestamp(ts)
    except (TypeError, ValueError):
        pass
    try:
        return _naive(datetime.fromisoformat(str(value)))
    except ValueError:
        return datetime.now()


def _naive(dt: datetime) -> datetime:
    """带时区的换算成本地时间再去掉时区，保证库里只有一种时间刻度。"""
    if dt.tzinfo is not None:
        return dt.astimezone().replace(tzinfo=None)
    return dt


def _raw_json(obj: Any) -> str:
    """原始消息留档。

    `default=str` 在这条路径上是错的：平台对象变成 `"<astrbot... object at
    0x7f...>"`，看着存下来了，其实什么也没存到。退化成 `repr()` 更糟——对象
    repr 里常带大段正文，红线 1 说了消息内容不进日志/落盘。取不到就存空串。
    """
    try:
        return json.dumps(obj, ensure_ascii=False)
    except (TypeError, ValueError):
        logger.debug("原始消息无法序列化，raw_content 留空：%s", type(obj).__name__)
        return ""


def _segments(raw: Any) -> list:
    """取出消息链，可以是 list / dict['message'] / dict['content'] / 单个 segment。"""
    if raw is None:
        return []
    if isinstance(raw, list):
        return raw
    for key in ("message", "content", "segments", "chain"):
        seg = _get(raw, key)
        if isinstance(seg, list):
            return seg
    return [raw]


def parse_message(raw: Any, group_id: Optional[int] = None) -> Message:
    """解析单条平台原始消息。

    `group_id` 是「兜底群号」：消息自己带群号时以它为准。反过来写（参数优先）
    会让导出包里的 group_id 形同虚设——包里的消息被改成哪个群都能算成原群，
    「恢复时不许夹带别群消息」这条校验直接被绕过去。
    """
    gid = int(_get(raw, "group_id", 0) or 0) or (group_id or 0)
    sender = _get(raw, "sender", {}) or {}
    sender_id = int(_get(raw, "sender_id", _get(sender, "user_id", 0)) or 0)
    sender_name = (
        _get(raw, "sender_name")
        or _get(sender, "card")
        or _get(sender, "nickname")
        or str(sender_id)
    )
    # 导出器把消息摊平成一条 dict（content/message_type 在顶层），从 ZIP 包
    # 恢复时走的就是这条路径。不显式接住的话内容会变成空串，校验和必然对不上。
    flat_content = _get(raw, "content")
    flat_type = _get(raw, "message_type")
    # content 是 list 说明这是消息链形态（`{'content': [...]}`），不是导出摊平后的
    # 纯文本。_segments 已经在上面处理过它，这里不能再来一次，否则同一段话进
    # 两次 content
    if isinstance(flat_content, (list, dict)):
        flat_content = ""
    timestamp = _to_datetime(
        _get(raw, "timestamp", _get(raw, "time", _get(raw, "created_at")))
    )
    message_id = str(_get(raw, "message_id", _get(raw, "id", "")) or "")
    if not message_id:
        message_id = f"local_{sender_id}_{int(timestamp.timestamp() * 1000)}_{int(time.time() * 1000) % 100000}"

    texts: list[str] = []
    media_paths: list[str] = []
    types: list[MessageType] = []
    reply_to = _get(raw, "reply_to") or _get(raw, "reply_id")
    is_forward = False
    forward_chain: list[dict] = []

    for seg in _segments(raw):
        stype = str(_get(seg, "type", "") or "").lower()
        data = _get(seg, "data", seg) or {}
        mtype = _TYPE_KEYS.get(stype)

        if mtype is MessageType.REPLY:
            reply_to = _get(data, "id") or _get(data, "message_id") or reply_to
        elif mtype is MessageType.AT:
            name = _get(data, "name") or _get(data, "qq") or ""
            texts.append(f"@{name}" if name else "@")
        elif mtype is MessageType.FORWARD:
            is_forward = True
            types.append(MessageType.FORWARD)
            chain = _get(data, "content", _get(data, "messages", [])) or []
            if isinstance(chain, list):
                forward_chain = [_forward_node(n) for n in chain]
        elif mtype in (MessageType.IMAGE, MessageType.VOICE, MessageType.VIDEO, MessageType.FILE):
            types.append(mtype)
            url = _get(data, "url") or _get(data, "file") or _get(data, "path") or ""
            if url:
                media_paths.append(str(url))
            label = _get(data, "name") or _get(data, "file_name")
            if mtype is MessageType.FILE and label:
                texts.append(f"[文件] {label}")
        else:
            text = _get(data, "text") or _get(seg, "text")
            if text is None and not stype:
                # 没有 type 的 dict 常见于导出后回灌（message_type/content 平铺），
                # 也见于单段纯文本对象；取不到就算空，不要拼出整段 JSON
                text = _get(seg, "content")
            if text:
                types.append(MessageType.TEXT)
                texts.append(str(text))

    if not types and flat_content and flat_type:
        texts.append(str(flat_content))
        try:
            types.append(MessageType(str(flat_type)))
        except ValueError:
            types.append(MessageType.UNKNOWN)

    if not types:
        # 兜底：raw 本身就是纯文本
        if isinstance(raw, str):
            texts.append(raw)
            types.append(MessageType.TEXT)
        else:
            # 消息链里可能有嵌套段：`{'type':'text','data':[{'text':...}]}`、合并转发
            # 的节点、平台自带的富文本。取不到就记 UNKNOWN，但**内容不能丢**——
            # 备份插件最不该做的就是「备份了条消息，内容是空的」。
            # 从「消息链」往下抠，而不是从 raw 整体：raw 的键里混着 message_id、
            # sender 这类元数据，整份钻下去只会把 ID 和昵称当正文捡回来。
            nested: list[str] = []
            for seg in _segments(raw):
                nested.extend(_nested_texts(seg))
            texts.extend(nested)
            types.append(MessageType.TEXT if texts else MessageType.UNKNOWN)

    if is_forward:
        message_type = MessageType.FORWARD
    elif len(set(types)) > 1:
        message_type = MessageType.MIXED
    else:
        message_type = types[0]
    if reply_to and message_type is MessageType.TEXT:
        message_type = MessageType.REPLY

    return Message(
        message_id=message_id,
        group_id=gid,
        sender_id=sender_id,
        sender_name=str(sender_name),
        timestamp=timestamp,
        message_type=message_type,
        content=_join_texts(texts),
        raw_content=_raw_json(raw),
        reply_to=str(reply_to) if reply_to else None,
        media_paths=media_paths,
        is_forward=is_forward,
        forward_chain=forward_chain,
    )


def _nested_texts(raw: Any, depth: int = 0, in_data: bool = False) -> list[str]:
    """尽力把认不出的结构里的文字抠出来。

    只做两件事：认得的键（text/content/name）取值，值是 list/dict 就往下钻。
    深度有上限，避免平台给个自引用结构就把备份卡死在这里。

    进了 `data` 就只认 text/content——那是消息段自己的数据袋，`name` 和
    `message` 在那里是「文件名」「消息 ID」一类的元数据，捡回来就是给消息内容
    掺假。
    """
    if depth > 4 or raw is None:
        return []
    if isinstance(raw, str):
        if in_data:
            # data 袋里的裸字符串没有语义，多半是文件 ID 或 URL，不当正文
            return []
        return [raw] if raw.strip() else []
    if isinstance(raw, (list, tuple)):
        out: list[str] = []
        for item in raw:
            out.extend(_nested_texts(item, depth + 1, in_data))
        return out
    if not isinstance(raw, dict) and not hasattr(raw, "__dict__"):
        return []
    out = []
    for key in ("text", "content", "message", "data", "name"):
        if in_data and key in ("message", "name", "data"):
            continue
        value = _get(raw, key)
        if isinstance(value, str):
            if value.strip():
                out.append(value)
        elif isinstance(value, (list, tuple, dict)):
            out.extend(_nested_texts(value, depth + 1, in_data or key == "data"))
    return out


def _join_texts(texts: list[str]) -> str:
    """把消息链里的文本段拼回一句话。

    直接 `" ".join()` 会在每个 @ 后面多出一个空格（`@张三  看这个`），中英混排
    的群消息里几乎每条 @ 都会中招。可读性是一方面，更要紧的是同一段话每次
    解析都要得到同一个结果——校验和是按 content 算的。

    规则：段与段相邻都是 ASCII 单词时补一个空格（避免 `hello`+`world` 粘成
    `helloworld`）；原本就以空白结尾/开头的段不重复补。
    """
    out = ""
    for seg in texts:
        if not seg:
            continue
        if out and not out[-1].isspace() and not seg[0].isspace():
            if out[-1].isascii() and out[-1].isalnum() and seg[0].isascii() and seg[0].isalnum():
                out += " "
        out += seg
    return out.strip()


def _forward_node(node: Any) -> dict:
    """合并转发的单个节点，最多保留两层，避免无限递归。"""
    sender = _get(node, "sender", {}) or {}
    inner = parse_message(node) if isinstance(node, (dict, list)) else None
    out = {
        "sender_name": _get(sender, "nickname") or _get(node, "sender_name") or "",
        "timestamp": str(_get(node, "time", _get(node, "timestamp", "")) or ""),
        "content": "",
    }
    if inner:
        out["content"] = inner.content
        if inner.forward_chain:
            out["content"] = out["content"] or f"[合并转发 {len(inner.forward_chain)} 条]"
    else:
        out["content"] = str(_get(node, "content", "") or "")
    return out


def parse_group_members(raw_members: Any, group_id: int) -> list:
    """解析成员列表。"""
    from .models import GroupMember

    members = []
    # 消息 ID 里的群号只是兜底：成员条目常带自己的 group_id，但它可能是 0
    fallback = _group_or({"group_id": group_id}, group_id)
    for m in raw_members or []:
        uid = _get(m, "user_id", _get(m, "uin", _get(m, "qq")))
        if uid is None:
            continue
        members.append(
            GroupMember(
                user_id=int(uid),
                group_id=fallback,
                nickname=str(_get(m, "nickname", "") or ""),
                card=str(_get(m, "card", _get(m, "remark", "")) or ""),
                role=str(_get(m, "role", "member") or "member"),
                join_time=_to_datetime(_get(m, "join_time")) if _get(m, "join_time") else None,
                last_active=_to_datetime(_get(m, "last_active"))
                if _get(m, "last_active")
                else None,
            )
        )
    return members


def parse_group_info(raw: Any, group_id: int):
    """解析群信息。"""
    from .models import GroupInfo

    return GroupInfo(
        group_id=_group_or(raw, group_id),
        group_name=str(_get(raw, "group_name", _get(raw, "name", "")) or ""),
        owner_id=int(_get(raw, "owner_id", _get(raw, "owner", 0)) or 0),
        created_at=_to_datetime(_get(raw, "created_at")) if _get(raw, "created_at") else None,
        member_count=int(_get(raw, "member_count", 0) or 0),
        max_members=int(_get(raw, "max_members", 0) or 0),
        group_level=int(_get(raw, "group_level", _get(raw, "level", 0)) or 0),
    )


def _group_or(raw: Any, fallback: int) -> int:
    """群号兜底。

    显式写出来的 `0` 也是「不知道」——直接 `or` 会把 `{'group_id': 0}` 原样留下，
    于是群公告、群文件、成员这些快照全落到群 0 上：备份看起来成功了，导出包里
    那几份清单是空的，而目标群里查不到。
    """
    try:
        value = int(_get(raw, "group_id", 0) or 0)
    except (TypeError, ValueError):
        value = 0
    return value or int(fallback or 0)


def _unwrap_list(raw: Any, *keys: str) -> list:
    """平台返回的列表可能包一层 dict（{'notices': [...]} / {'files': [...]}）。"""
    if raw is None:
        return []
    if isinstance(raw, list):
        return raw
    for key in keys:
        value = _get(raw, key)
        if isinstance(value, list):
            return value
    for key in ("data", "list", "items"):
        value = _get(raw, key)
        if isinstance(value, list):
            return value
        if isinstance(value, dict):
            for sub in keys:
                inner = _get(value, sub)
                if isinstance(inner, list):
                    return inner
    return []


def parse_announcements(raw: Any, group_id: int) -> list[dict]:
    """解析群公告列表。认不出结构就返回空表，绝不抛异常。"""
    out = []
    for item in _unwrap_list(raw, "notices", "announcements", "feeds"):
        sender = _get(item, "sender", {}) or {}
        content = _get(item, "content") or _get(item, "message") or _get(item, "text") or ""
        # 公告正文有时是消息链形态
        if isinstance(content, list):
            text = " ".join(
                str(_get(_get(seg, "data", seg) or {}, "text") or "")
                for seg in content
            ).strip() or json.dumps(content, ensure_ascii=False, default=str)
        else:
            text = str(content or "")
        published = _get(item, "publish_time") or _get(item, "time") or _get(item, "published_at")
        out.append(
            {
                "group_id": _group_or(item, group_id),
                "content": text,
                "sender_id": _get(item, "sender_id") or _get(sender, "user_id") or 0,
                "sender_name": _get(sender, "card") or _get(sender, "nickname") or "",
                "published_at": _to_datetime(published) if published else None,
                "raw": _raw_json(item),
            }
        )
    return out


def parse_group_files(raw: Any, group_id: int) -> list[dict]:
    """解析群文件列表。"""
    out = []
    for item in _unwrap_list(raw, "files", "file_list"):
        uploader = _get(item, "uploader") or {}
        upload_time = _get(item, "upload_time") or _get(item, "time") or _get(item, "dead_time")
        out.append(
            {
                "group_id": _group_or(item, group_id),
                "file_id": _get(item, "file_id") or _get(item, "id") or "",
                "name": _get(item, "file_name") or _get(item, "name") or "",
                "size": int(_get(item, "file_size", _get(item, "size", 0)) or 0),
                "busid": _get(item, "busid") or 0,
                "upload_time": _to_datetime(upload_time) if upload_time else None,
                "uploader_id": _get(item, "uploader_id") or 0,
                "uploader_name": str(
                    _get(item, "uploader_name") or _get(uploader, "nickname") or ""
                ),
                "raw": _raw_json(item),
            }
        )
    return out
