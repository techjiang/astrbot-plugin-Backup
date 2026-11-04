"""第 23 轮：按 AstrBot 4.14.6 的真实结构核出来的四处。

每一条都先写成可复现脚本（替身照 `astrbot/core/platform/astrbot_message.py`
与 `astr_message_event.py` 的真实形状搭），看着它从「不对」变成「正确」，
再固化成用例。

1. 实时归档的消息被备份收编，兜底数据再也找不到；
2. 备份中途失败，已入库的消息永远进不了任何来源清单；
3. 平台回的群号与我们请求的不一致时，群信息被写到另一个群名下；群名缺失时被洗空；
4. 群主/管理员发 `/backup` 被判没权限（`role` 只认 AstrBot 的 admins_id）。
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from enum import Enum

import pytest

from backup.core.backup_engine import BackupEngine
from backup.core.commands import PermissionChecker
from backup.core.database import REALTIME_BACKUP_ID
from backup.core.event_hooks import EventHooks
from backup.core.auto_trigger import AutoTrigger
from backup.core.models import GroupInfo, Message, MessageType
from backup.core.parser import parse_group_info


# --------------------------------------------------------------------------
# 照 AstrBot 4.14.6 搭的最小替身（只保留被代码读到的字段与形态）
# --------------------------------------------------------------------------


class _Group:
    """`astrbot.core.platform.astrbot_message.Group`"""

    def __init__(self, group_id, group_name=None, group_owner=None, group_admins=None):
        self.group_id = group_id
        self.group_name = group_name
        self.group_owner = group_owner
        self.group_admins = group_admins or []


class _Member:
    """`astrbot.core.platform.astrbot_message.MessageMember`"""

    def __init__(self, user_id, nickname=None):
        self.user_id = user_id
        self.nickname = nickname


class _AstrBotMessage:
    def __init__(self, group, sender, message_id="1", raw=None, timestamp=1700000000):
        self.type = type("T", (), {"value": "GroupMessage"})()
        self.self_id = "10000"
        self.session_id = group.group_id if group else ""
        self.message_id = message_id
        self.group = group
        self.sender = sender
        self.message = []
        self.message_str = ""
        self.raw_message = raw or {}
        self.timestamp = timestamp

    @property
    def group_id(self):
        return self.group.group_id if self.group else ""


class _Event:
    """`astrbot.core.platform.astr_message_event.AstrMessageEvent` 的读法子集"""

    def __init__(self, abm, role="member"):
        self.message_obj = abm
        self.message_str = abm.message_str
        self.role = role

    def get_group_id(self):
        return self.message_obj.group_id

    def get_sender_id(self):
        return self.message_obj.sender.user_id

    def get_sender_name(self):
        return self.message_obj.sender.nickname or ""

    def get_self_id(self):
        return self.message_obj.self_id

    def is_private_chat(self):
        return self.message_obj.type.value == "FriendMessage"


class _Plain:
    """`astrbot.core.message.components.Plain`：`type` 是 `ComponentType.Plain`，
    一个 `str` 枚举。`str(ComponentType.Plain)` 在 3.11 上给出
    `"ComponentType.Plain"`——这是备份插件踩过的第一个坑，替身要照抄这个形态。"""

    def __init__(self, text):
        self.type = _ComponentType.Plain
        self.text = text


class _ComponentType(str, Enum):
    Plain = "Plain"


def _text_message_event(group_id="100", user_id="7", nickname="张三", mid="555"):
    raw = {
        "post_type": "message",
        "message_type": "group",
        "group_id": int(group_id),
        "user_id": int(user_id),
        "message_id": int(mid),
        "message": [{"type": "text", "data": {"text": "大家好"}}],
    }
    abm = _AstrBotMessage(_Group(group_id), _Member(user_id, nickname), mid, raw)
    # 真实的 `_convert_handle_message_event` 会把消息链填进 `abm.message`
    abm.message = [_Plain("大家好")]
    abm.message_str = "大家好"
    return _Event(abm)


def _local_message(mid: str, group_id: int = 100) -> Message:
    return Message(
        message_id=mid,
        group_id=group_id,
        sender_id=1,
        sender_name="A",
        timestamp=datetime(2026, 1, 1, 0, 0, 0),
        message_type=MessageType.TEXT,
        content=mid,
        raw_content="",
    )


def _raw(mid: str, group_id: int = 100) -> dict:
    return {
        "message_id": mid,
        "group_id": group_id,
        "sender": {"user_id": 1, "nickname": "A"},
        "timestamp": 1700000000,
        "message": [{"type": "text", "data": {"text": mid}}],
    }


class _Platform:
    """按签名逐个匹配的假平台。

    关键字参数名必须是 `group_id`：`PlatformAdapter._call` 签名不匹配会换下一个
    名字、全都不行才降级——参数名写成 `gid` 会被当成「平台不支持」静默跳过。
    """

    def __init__(self, history=None, info=None, fail_after_first_page=False):
        self.history = history or []
        self.info = info if info is not None else {"group_id": 100, "group_name": "测试群"}
        self.fail_after_first_page = fail_after_first_page

    async def get_group_info(self, group_id):
        return self.info

    async def get_group_members(self, group_id):
        return []

    async def get_group_history(self, group_id, count=50, offset=0):
        if offset == 0:
            return self.history[:count]
        if self.fail_after_first_page:
            raise TimeoutError("网络断了")
        return self.history[offset : offset + count]


# --------------------------------------------------------------------------
# 1. 实时归档的消息不被备份收编
# --------------------------------------------------------------------------


class TestRealtimeOwnership:
    def test_realtime_message_stays_under_realtime(self, db):
        """备份覆盖到实时归档的消息，但**不改归属**。

        改了归属，`/backup status` 那句「其中实时归档 N 条」永远归零，而实时归档
        存在的理由就是「群被炸时还能按 realtime 找到最后那几条」。
        """
        db.save_message(_local_message("m1"), REALTIME_BACKUP_ID)
        db.add_sources(REALTIME_BACKUP_ID, [_local_message("m1")])

        taken = db.adopt_orphans(100, "bk_new", ["m1"])

        assert taken == 1  # 覆盖被承认（来源清单里登记了）
        assert db.message_backup_id("m1", 100) == REALTIME_BACKUP_ID
        assert db.count_messages(100, backup_id=REALTIME_BACKUP_ID) == 1
        assert "m1" in db.source_message_ids("bk_new", 100)

    def test_orphan_still_adopted(self, db):
        """归属指向已消失备份的行照旧收编——那是另一件事，不能一起放过。"""
        db.save_message(_local_message("m2"), "bk_already_deleted")

        assert db.adopt_orphans(100, "bk_new", ["m2"]) == 1
        assert db.message_backup_id("m2", 100) == "bk_new"

    async def test_backup_over_realtime_keeps_both_findable(self, db):
        """端到端：先实时归档再备份，两条路都还找得到。"""
        hooks = EventHooks(db, AutoTrigger({}))
        archived = hooks.archive_message(_text_message_event())
        assert archived is not None
        engine = BackupEngine(db, db.path.parent / "media", {"scope": {"download_media": False}})

        record = await engine.backup_group(
            100, _Platform([_raw(archived.message_id)]), download_media=False
        )

        # 备份覆盖到了
        assert record.message_count == 1
        assert archived.message_id in db.source_message_ids(record.backup_id, 100)
        # 兜底那条路还在
        assert db.message_backup_id(archived.message_id, 100) == REALTIME_BACKUP_ID
        # 「还没被任何备份覆盖」这个数也应该归零了（覆盖靠清单表达，不靠归属）
        assert db.count_realtime_only(100) == 0


# --------------------------------------------------------------------------
# 2. 备份失败时，已入库的消息要登记进来源清单
# --------------------------------------------------------------------------


class TestFailedBackupSources:
    async def test_partial_messages_are_registered(self, db):
        """第二页超时：第一页那批已经入库，必须登记进这份备份的来源清单。

        不登记的话它们永远进不了任何备份：唯一键 `(message_id, group_id)` 被占着，
        下一轮备份看到「已在库里」直接跳过，而 `adopt_orphans` 只收编「归属的备份
        已经消失」的行——这次失败备份的记录是在失败**之后**才写进
        `backup_records` 的，归属看起来完全正常，谁也不会来认领。
        实测修复前：库里 50 条、来源清单 0 条，恢复和导出都取不到。
        """
        history = [_raw(f"m{i}") for i in range(60)]
        engine = BackupEngine(db, db.path.parent / "media", {"scope": {"download_media": False}})
        platform = _Platform(history, fail_after_first_page=True)

        record = await engine.backup_group(100, platform, download_media=False)

        assert record.status.value == "failed"
        assert db.count_messages(100) == 50  # 第一页整页 PASS_SIZE=50 条
        assert db.count_sources(record.backup_id) == 50
        assert record.message_count == 50

    async def test_failure_reason_is_kept(self, db):
        """补登记不能把「为什么失败」盖掉。"""
        engine = BackupEngine(db, db.path.parent / "media", {"scope": {"download_media": False}})
        platform = _Platform([_raw(f"m{i}") for i in range(60)], fail_after_first_page=True)

        record = await engine.backup_group(100, platform, download_media=False)

        assert record.error and "TimeoutError" in record.error


# --------------------------------------------------------------------------
# 3. 群信息：群号不一致不采信平台那份，群名缺失不清空
# --------------------------------------------------------------------------


class TestGroupInfo:
    def test_mismatched_group_id_is_not_trusted(self):
        """平台回群号 999、我们在备群 100 → 存到群 100 名下。

        `group_info` 按群号做主键，`INSERT OR REPLACE` 会把记录写到**另一个群**，
        于是 `/backup status 100` 查不到群名，导出包里群号也对不上。
        """
        info = parse_group_info({"group_id": 999, "group_name": "别的群"}, 100)

        assert info.group_id == 100

    async def test_mismatched_group_id_does_not_create_other_group(self, db):
        engine = BackupEngine(db, db.path.parent / "media", {"scope": {"download_media": False}})
        platform = _Platform([], info={"group_id": 999, "group_name": "别的群"})

        await engine.backup_group(100, platform, download_media=False)

        assert db.get_group_info(100) is not None
        assert db.get_group_info(999) is None

    async def test_missing_group_name_keeps_previous(self, db):
        """OneBot 的 `get_group_info` 有时只回 group_id / member_count。

        直接存会把已有群名洗成空串，导出包的 `<title>` 和 HTML 头栏跟着变空。
        """
        db.save_group_info(GroupInfo(group_id=100, group_name="老群名"))
        engine = BackupEngine(db, db.path.parent / "media", {"scope": {"download_media": False}})
        platform = _Platform([], info={"group_id": 100, "member_count": 50})

        await engine.backup_group(100, platform, download_media=False)

        assert db.get_group_info(100).group_name == "老群名"


# --------------------------------------------------------------------------
# 4. 群主 / 管理员身份：按 message_obj.group 的名单比
# --------------------------------------------------------------------------


def _event_with_group_roles(sender_id, owner=None, admins=(), role="member"):
    abm = _AstrBotMessage(
        _Group("100", group_owner=owner, group_admins=list(admins)),
        _Member(str(sender_id), "x"),
    )
    return _Event(abm, role=role)


class TestPermissionFromGroupRoles:
    """AstrBot 的 `role` 只认 `admins_id` 配置（机器人主人），不是群管。"""

    def test_group_owner_is_allowed(self):
        event = _event_with_group_roles("7", owner="7", admins=["8"])
        assert PermissionChecker(True).check_event(event).allowed

    def test_group_admin_is_allowed(self):
        event = _event_with_group_roles("8", owner="7", admins=["8"])
        assert PermissionChecker(True).check_event(event).allowed

    def test_plain_member_is_denied(self):
        event = _event_with_group_roles("9", owner="7", admins=["8"])
        verdict = PermissionChecker(True).check_event(event)
        assert not verdict.allowed

    def test_astrbot_admin_role_still_works(self):
        """`role == "admin"`（AstrBot 主人）这条不能丢。"""
        event = _event_with_group_roles("9", role="admin")
        assert PermissionChecker(True).check_event(event).allowed

    def test_no_group_object_falls_back_to_deny(self):
        """没有 group 信息、也没有 role → 照旧拒绝，别把门开着。"""
        abm = _AstrBotMessage(None, _Member("9", "x"))
        abm.group = None
        assert not PermissionChecker(True).check_event(_Event(abm)).allowed
