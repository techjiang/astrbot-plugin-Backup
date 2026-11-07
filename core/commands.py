"""命令层：解析参数、拼回复文案。与 AstrBot 解耦，便于单测。"""

import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional

from .database import REALTIME_BACKUP_ID, Database
from .models import PRIVATE_ARCHIVE_GROUP

SIZE_UNITS = {"B": 1, "KB": 1024, "MB": 1024**2, "GB": 1024**3}

HELP_TEXT = """**Backup** · 命令速查

`/backup help` 帮助
`/backup now [群号]` 立即备份
`/backup schedule set <群号> "<cron>"` 设置定时备份
`/backup schedule list` 查看定时任务
`/backup schedule remove <群号>` 移除定时任务
`/backup list [群号]` 备份列表
`/backup export [群号] <格式>` 导出（sqlite/json/csv/txt/markdown/html/zip）
`/backup restore <备份ID|备份包路径> [目标群号] [--strategy skip|overwrite|merge] --confirm` 恢复
`/backup status [群号]` 当前群备份状态
`/backup config get|set <键> [值]` 配置查看/修改
`/backup clean [天数]` 清理旧备份
`/backup verify <备份ID>` 校验完整性
`/backup diff <备份ID1> <备份ID2>` 对比差异"""


def _config_get(config: dict, key: str):
    node = config
    for part in key.split("."):
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node


def _coerce(raw: str):
    text = raw.strip()
    low = text.lower()
    if low in ("true", "yes", "on"):
        return True
    if low in ("false", "no", "off"):
        return False
    if re.fullmatch(r"-?\d+", text):
        return int(text)
    try:
        return float(text)
    except ValueError:
        return text


def _validate_value(key: str, new):
    """类型之外还得管语义：type 对不上只是格式问题，值不合法会让插件当场坏掉。"""
    if isinstance(new, str):
        if key.endswith("max_file_size"):
            from .media_downloader import parse_size

            text = new.strip().upper()
            if not re.fullmatch(r"[\d.]+\s*[KMGT]?B", text):
                return f"`{key}` 需要形如 100MB 的大小，收到 `{new}`"
            if parse_size(new) <= 0:
                return f"`{key}` 必须大于 0"
        elif key.endswith("download_link_expire"):
            from .export_manager import parse_expire

            text = new.strip().lower()
            if not re.fullmatch(r"\d+(\.\d+)?[smhd]?", text) or parse_expire(new) <= 0:
                return f"`{key}` 需要形如 24h / 30m，收到 `{new}`"
    if isinstance(new, int) and key.split(".")[-1] in (
        "max_concurrent",
        "retry_count",
        "keep_days",
        "keep_count",
        "mass_leave_threshold",
    ):
        if new <= 0:
            return f"`{key}` 必须大于 0"
    return ""


def _config_set(config: dict, key: str, raw: str) -> tuple[bool, str]:
    """改已知键。拼错的键、类型不对、值不合法的都拒绝，避免配置里长出一堆废条目。"""
    parts = key.split(".")
    node = config
    for part in parts[:-1]:
        if not isinstance(node.get(part), dict):
            return False, f"配置项 `{key}` 不存在"
        node = node[part]
    leaf = parts[-1]
    if leaf not in node:
        return False, f"配置项 `{key}` 不存在"
    old = node[leaf]
    new = _coerce(raw)
    if type(old) is not type(new) and not (isinstance(old, float) and isinstance(new, int)):
        return False, f"`{key}` 需要 {type(old).__name__} 类型的值"
    problem = _validate_value(key, new)
    if problem:
        return False, problem
    node[leaf] = new
    return True, ""


def human_size(size: int) -> str:
    num = float(size or 0)
    for unit in ("B", "KB", "MB", "GB"):
        if num < 1024 or unit == "GB":
            return f"{num:.1f}{unit}" if unit != "B" else f"{int(num)}B"
        num /= 1024
    return f"{num:.1f}GB"


def parse_group_arg(token: Optional[str]) -> Optional[int]:
    """把命令里的群号参数解析成 int。认不出返回 None。

    两个容易踩的地方：

    - 群号下限写成 5 位，于是 `-1`（私聊归档的虚拟群号）永远解析不出来。
      私聊消息照 `scope.private_messages` 落进库了，`/backup export -1 zip`
      却回一句「请指定群号」——数据在库里，没有任何命令能把它导出来。
    - 上界 20 位是给平台 ID 留的余量（Discord 的 snowflake 已经 19 位）。
    """
    text = (token or "").strip()
    if not re.fullmatch(r"-?\d{1,20}", text):
        return None
    if text.lstrip("-") == "0":
        return None
    return int(text)


@dataclass
class Permission:
    allowed: bool
    reason: str = ""


ADMIN_ROLES = ("admin", "owner", "administrator")


def _call_or_value(event, name):
    """取属性；是方法就调一次。抛异常当作「没有这个信息」。

    AstrBot 的 `is_private_chat()` / `is_admin()` / `get_group_id()` 都是**方法**，
    原来用 `getattr(...)` 拿到的是一堆 bound method，`isinstance(value, bool)`
    恒为 False——于是每一项探测都判「没有这个标记」。
    """
    value = getattr(event, name, None)
    if value is None:
        return None
    if callable(value):
        try:
            return value()
        except Exception:
            return None
    return value


def _role_of(node):
    """一个节点（事件 / sender / 成员对象或 dict）上记着的角色。

    两种形态都要吃：`{"role": "admin"}` 这种 dict，和 `member.role = "member"`
    这种对象。返回原值（True / "admin" / "member" / None），由调用方判定。
    """
    if node is None:
        return None
    if isinstance(node, dict):
        return node.get("role")
    return _call_or_value(node, "role")


def _is_private_event(event) -> bool:
    """判断是不是私聊事件。

    只认明确的私聊标记，没有就按群聊处理——「认不出＝私聊」会让一个只实现
    is_admin() 的群聊适配器悄悄跳过权限检查，等于把门开着。
    """
    for name in ("is_private_chat", "is_private", "is_direct"):
        value = _call_or_value(event, name)
        if isinstance(value, bool):
            return value
    # AstrBot 不给 `message_type` 这个属性，真正的类型在 `message_obj.type` 上，
    # 取值是 `MessageType.FRIEND_MESSAGE`（枚举）。两种形态都认。
    scope = getattr(event, "message_type", None)
    if scope is None:
        inner = getattr(event, "message_obj", None)
        scope = getattr(inner, "type", None) if inner is not None else None
    # `MessageType` 是 `class MessageType(Enum)`，`.value` 才是
    # `"GroupMessage"` / `"FriendMessage"`；不是 str 枚举，`str()` 会给出
    # `"MessageType.GROUP_MESSAGE"`，`getattr(scope, "value", scope)` 那一步
    # 在 scope 是裸字符串时也拿不到 value。两种形态都在这里收干净
    scope_value = getattr(scope, "value", scope)
    if isinstance(scope_value, str):
        scope_value = scope_value.replace("MessageType.", "").replace("_", "").replace(" ", "")
        scope_value = scope_value.lower()
    if scope_value in ("private", "friend", "direct", "friendmessage"):
        return True
    if scope_value in ("group", "groupmessage", "othermessage"):
        return False
    # 有群号就是群聊。AstrBot 的群号只从 `get_group_id()` 拿得到（返回字符串），
    # 私聊时是空串；`event.group_id` 这个属性根本不存在。
    for name in ("get_group_id", "group_id", "group"):
        value = _call_or_value(event, name)
        if isinstance(value, int) and value > 0:
            return False
        if isinstance(value, str) and value.strip().isdigit() and int(value) > 0:
            return False
    inner = getattr(event, "message_obj", None)
    gid = _call_or_value(inner, "group_id") if inner is not None else None
    if isinstance(gid, int) and gid > 0:
        return False
    if isinstance(gid, str) and gid.strip().isdigit() and int(gid) > 0:
        return False
    # 没有任何私聊标记，也没有群号：按群聊处理，走进正常的角色检查
    return False


class PermissionChecker:
    """仅群主/管理员可执行备份操作（可配置关闭）。

    适配器的 `is_admin()` 常常没实现、直接抛异常。抛异常就当「没权限」会让所有人
    都被挡在门外，所以还要接着看角色字段。角色也认不出时才拒绝。
    """

    def __init__(self, require_admin: bool = True, allow_private: bool = True):
        self.require_admin = require_admin
        self.allow_private = allow_private

    def check_event(self, event) -> Permission:
        # 私聊里没有「群主/管理员」这个概念。allow_private 关掉就一律拒绝，
        # 开着就跳过角色检查，否则私聊会永远被挡在「仅群主或管理员」外面
        if _is_private_event(event):
            if not self.allow_private:
                return Permission(False, "私聊已禁用备份操作（permission.allow_private）")
            return Permission(True)
        if not self.require_admin:
            return Permission(True)
        # AstrBot 的 `is_admin()` 是**方法**：原来只判 `isinstance(value, bool)`，
        # 拿到 bound method 恒判否，管理员发命令也被一句「仅群主或管理员」挡回去。
        # 返回 False 也要继续看角色字段：有的适配器只填 role 不填这几个。
        for name in ("is_admin", "is_group_admin", "is_owner"):
            if _call_or_value(event, name) is True:
                return Permission(True)
        if self._role_is_admin(event):
            return Permission(True)
        return Permission(False, "仅群主或管理员可执行备份操作")

    #: AstrBot 4.x 的 `AstrBotMessage.group` 上带群主/管理员名单，值是字符串 ID
    GROUP_ADMIN_FIELDS = ("group_owner", "owner_id")
    GROUP_ADMIN_LISTS = ("group_admins", "admin_ids")

    @classmethod
    def _role_is_admin(cls, event) -> bool:
        """从事件里找「这个人是群管吗」。字段名各适配器不同，逐个试。

        AstrBot 4.14 的 `AstrMessageEvent.role` 只有两个取值：`member` / `admin`，
        而 `admin` 是**拿 `admins_id` 配置比出来的**——也就是「机器人主人」，
        不是「这个群的群主/管理员」。所以群主在群里发 `/backup now` 会被判成
        没权限，而这条命令本该由群主执行（配置项写的是 require_admin）。

        真正的群管身份在各平台适配器的 `message_obj.group` 上：`group_owner`（群主
        QQ）和 `group_admins`（管理员 QQ 列表）。这里拿发送者 ID 去比对——
        比「猜 role 字符串」可靠，因为它不依赖适配器怎么填。
        """
        for name in ("sender_role", "role", "member_role", "user_role"):
            role = getattr(event, name, None)
            if isinstance(role, str) and role.lower() in ADMIN_ROLES:
                return True
        # 有的适配器把角色挂在 sender / message_obj 里。`_call_or_value` 而不是
        # 裸 `getattr`：AstrBot 的 `AstrMessageEvent` 没有 `sender` 属性，真正的
        # 发送者在 `message_obj.sender` 上；裸 getattr 拿到的是 None，这两级
        # 探测从来没生效过
        for holder, nested in (("sender", None), ("message_obj", "sender")):
            node = _call_or_value(event, holder)
            if nested:
                node = _call_or_value(node, nested) if node is not None else None
            role = _role_of(node)
            if role is True or (isinstance(role, str) and role.lower() in ADMIN_ROLES):
                return True
        return cls._sender_in_group_admins(event)

    @classmethod
    def _sender_in_group_admins(cls, event) -> bool:
        """发送者是不是这个群的群主/管理员（按 `message_obj.group` 里的名单比）。"""
        sender = _call_or_value(event, "get_sender_id")
        if sender in (None, ""):
            sender = _call_or_value(event, "sender_id")
        inner = getattr(event, "message_obj", None)
        if sender in (None, "") and inner is not None:
            sender = _call_or_value(getattr(inner, "sender", None), "user_id")
        if sender in (None, ""):
            return False
        # AstrBot 的 `MessageMember.user_id` / `Group.group_owner` /
        # `Group.group_admins` 都是**字符串**，而这里原来两边不做归一：
        # OneBot 回 `owner_id` 是 int、`group_admins` 是 int 列表时（适配器自己
        # 组装的 Group 常见这种），`"123" != 123` 会让群主被判成没权限。
        # 统一按字符串去空白后比。
        sender = str(sender).strip()
        group = getattr(inner, "group", None) if inner is not None else None
        if group is None:
            return False
        for name in cls.GROUP_ADMIN_FIELDS:
            if str(_call_or_value(group, name) or "").strip() == sender:
                return True
        for name in cls.GROUP_ADMIN_LISTS:
            values = _call_or_value(group, name)
            if isinstance(values, (list, tuple, set)) and any(
                str(v).strip() == sender for v in values
            ):
                return True
        # 前两项都没有的适配器常把「这个人什么身份」写在发送者自己身上
        role = _role_of(getattr(inner, "sender", None) if inner is not None else None)
        return role is True or (isinstance(role, str) and role.lower() in ADMIN_ROLES)


def format_record_line(record) -> str:
    """一行说清这次备份干了什么：保住了多少条、其中新存了几条。

    「覆盖 5 条」是这份备份的体量，「新 0 条」是这轮没遇到没备份过的消息——
    两个数一起看才知道重跑一次到底是空转还是真干活。
    """
    base = "<增量>" if record.is_incremental else "<全量>"
    return (
        f"`{record.backup_id}` {base} 覆盖 {record.message_count} 条"
        f"（新 {record.new_count} 条）/ "
        f"{human_size(record.total_size)} / {record.status.value}"
    )


def render_backup_result(record) -> str:
    """一次备份的回执。

    纯函数：手动命令才把它发给群里，触发式备份（退群/解散/被踢）不回话——
    那时候回执要么发不出去，要么等于把事故现场再点一次。
    """
    state = {"completed": "✅", "partial": "⚠️", "failed": "❌"}.get(record.status.value, "•")
    lines = [
        f"{state} 备份 {'增量' if record.is_incremental else '全量'}结束",
        f"消息 {record.message_count} 条（新 {record.new_count} 条） · "
        f"媒体 {record.media_count} 个 · {human_size(record.total_size)}",
        f"备份 ID：`{record.backup_id}`",
    ]
    if record.error:
        lines.append(f"备注：{record.error}")
    return "\n".join(lines)


def render_backup_list(db: Database, group_id: Optional[int] = None, limit: int = 10) -> str:
    """`group_id` 为空时列全部备份，每条带上群号。

    多群运行时只列当前群，等于让用户在群里发消息却看不到别的群备过什么；而备份 ID
    又是恢复需要的唯一凭据，找不到就等于无法恢复。
    """
    records = db.list_records(group_id, limit=limit)
    if not records:
        return "还没有任何备份记录。"
    head = f"最近的 {len(records)} 条备份："
    show_group = group_id is None
    lines = [head]
    for r in records:
        prefix = f"[群 {r.group_id}] " if show_group else ""
        lines.append(f"- {prefix}{format_record_line(r)}")
    return "\n".join(lines)


def group_label(group_id: int, info=None) -> str:
    """给一个群号配个能读的名字。

    私聊归档落在 `-1` 这个虚拟群号下，回执直接写「**群 -1**（-1）」等于把内部
    约定甩给用户看。真实群号照旧显示群名，取不到群名就显示群号。
    """
    if group_id == PRIVATE_ARCHIVE_GROUP:
        return "私聊归档"
    name = info.group_name if info is not None and info.group_name else group_id
    return f"群 {name}（{group_id}）"


def render_status(db: Database, group_id: int) -> str:
    info = db.get_group_info(group_id)
    # 取「最近一次真正落了消息的备份」，而不是列表第一行：
    # 空跑一次会留下 0 条的记录，按第一行取会把实绩藏起来
    record = db.latest_completed_record(group_id)
    members = db.get_members(group_id)
    lines = [f"**{group_label(group_id, info)}**"]
    lines.append(f"消息总数：{db.count_messages(group_id)}")
    lines.append(f"成员数：{len(members)}")
    if record:
        # 记录里的 message_count 是「那次备份覆盖了多少」，不是「新增了多少」：
        # 一轮全是已在库消息的增量备份 message_count 是 0，却实实在在覆盖了整批。
        # 写「这次新增：0 条」，用户看到的就是「备份了个寂寞」。
        lines.append(f"最近备份：{record.started_at:%Y-%m-%d %H:%M:%S}")
        covered = db.count_sources(record.backup_id)
        lines.append(
            f"这次覆盖：{covered or record.message_count} 条（新 {record.new_count} 条）· "
            f"媒体 {record.media_count} 个 · {human_size(record.total_size)}"
        )
    else:
        lines.append("还没有完成过备份")
    # 定时备份开着的时候，群消息其实一直在进库（实时归档），只报「没备份过」会让人
    # 以为一条都没存下来
    realtime = db.count_realtime_only(group_id)
    if realtime:
        lines.append(f"其中实时归档 {realtime} 条（还没被任何一次备份接手）")
    return "\n".join(lines)


def _record_message_ids(db: Database, record) -> set[str]:
    """一份备份覆盖过的消息 ID：来源清单优先，老库退回按归属。"""
    ids = {m.message_id for m in db.get_source_messages(record.backup_id, record.group_id)}
    if not ids:
        ids = {
            m.message_id
            for m in db.get_messages(group_id=record.group_id, backup_id=record.backup_id)
        }
    return ids


def render_diff(db: Database, id1: str, id2: str) -> str:
    """比两次备份各自留下了哪些消息。

    按「来源清单」取，不能按 `messages.backup_id`：那个归属只有一个值，记的是
    最后一次归档这条消息的备份。一次全量重跑就把整群消息都划到新备份名下，
    老备份按归属取出来是空的——`/backup diff` 于是永远报「A 0 条 / 相同 0 条 /
    仅在 B N 条」，比什么都长这样。来源清单是后加的，升级前的老库没有这份
    登记，此时才退回按归属取。
    """
    r1, r2 = db.get_record(id1), db.get_record(id2)
    if not r1 or not r2:
        return "备份 ID 不存在，无法对比。"
    m1 = _record_message_ids(db, r1)
    m2 = _record_message_ids(db, r2)
    only1, only2 = m1 - m2, m2 - m1
    return (
        f"**备份对比**\n- `{id1}`：{len(m1)} 条\n- `{id2}`：{len(m2)} 条\n"
        f"- 仅在 {id1}：{len(only1)} 条\n- 仅在 {id2}：{len(only2)} 条\n"
        f"- 相同：{len(m1 & m2)} 条"
    )


def render_progress(done: int, total_hint: int = 0) -> str:
    if total_hint:
        ratio = min(done / max(total_hint, 1), 1.0)
        bars = int(ratio * 10)
        return f"[{'█' * bars}{'░' * (10 - bars)}] {done}/{total_hint}"
    return f"已备份 {done} 条…"


def human_duration(seconds: float) -> str:
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds} 秒"
    if seconds < 3600:
        return f"{seconds // 60} 分 {seconds % 60} 秒"
    return f"{seconds // 3600} 小时 {(seconds % 3600) // 60} 分"


def retention_cutoff(days: int) -> datetime:
    return datetime.now() - timedelta(days=days)
