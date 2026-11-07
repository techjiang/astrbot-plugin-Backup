"""round27：适配器参数名对不上，整条拉取路径静默失败。

这一轮是从「备份报 completed、消息 0 条」倒查回去的。根因不在业务逻辑，
而在 `PlatformAdapter._call` 的探测方式：它只按**参数名**硬送 `group_id=`，
适配器上叫 `gid` 就每个候选都 TypeError，最后返回 None——上层看到的是
「这个平台没有消息」。
"""

import pytest

from backup.core.backup_engine import BackupEngine, PlatformAdapter, _adapt_kwargs
from backup.core.database import Database


class GidPlatform:
    """真适配器的样子：历史消息参数叫 gid（OneBot 那套），不是 group_id。"""

    def __init__(self, messages=None):
        self.messages = messages or []

    async def get_group_info(self, gid):
        return {"group_id": gid, "group_name": "群"}

    async def get_group_members(self, gid):
        return []

    async def get_group_history(self, gid, count=50, offset=0):
        return self.messages if offset == 0 else []

    async def get_group_announcements(self, gid):
        return None

    async def get_group_files(self, gid):
        return None


def _raw(i, group_id=100):
    return {
        "message_id": f"m{i}",
        "group_id": group_id,
        "sender": {"user_id": 1, "nickname": "用户"},
        "timestamp": 1714557600 + i,
        "message": [{"type": "text", "data": {"text": f"消息 {i}"}}],
    }


@pytest.mark.asyncio
async def test_gid_signature_is_matched_not_skipped():
    """`get_group_history(gid, ...)` 要被调通，不能因为参数名不同就放弃。"""
    adapter = PlatformAdapter(GidPlatform([_raw(0)]))
    assert await adapter.get_group_history(100) == [_raw(0)]


@pytest.mark.asyncio
async def test_backup_actually_collects_messages_from_gid_platform(db):
    """回归：这条路径之前一条消息都拉不到，备份还报 completed。"""
    engine = BackupEngine(db, db.path.parent / "media", {})
    record = await engine.backup_group(
        100, GidPlatform([_raw(0), _raw(1)]), download_media=False
    )
    assert record.message_count == 2
    assert record.status.value == "completed"
    assert db.count_sources(record.backup_id) == 2


@pytest.mark.asyncio
async def test_unusable_history_api_is_not_reported_as_success(db):
    """接口名/参数名全对不上 → 必须 failed，不能假装「这个群是空的」。

    空群和「插件备份不了这个平台」在返回值上都是空列表，混为一谈等于骗人。
    """

    class Broken:
        async def get_group_info(self, gid):
            return {"group_id": gid, "group_name": "群"}

        async def get_group_members(self, gid):
            return []

        async def get_group_history(self, chat_key, count=50, offset=0):
            return []

        async def get_group_announcements(self, gid):
            return None

        async def get_group_files(self, gid):
            return None

    engine = BackupEngine(db, db.path.parent / "media", {})
    record = await engine.backup_group(100, Broken(), download_media=False)
    assert record.status.value == "failed"
    assert "历史消息接口" in (record.error or "")


@pytest.mark.asyncio
async def test_empty_group_still_completes(db):
    """真空群不该被误判成失败：接口调通了、返回空，就是 completed。"""
    engine = BackupEngine(db, db.path.parent / "media", {})
    record = await engine.backup_group(100, GidPlatform([]), download_media=False)
    assert record.status.value == "completed"
    assert record.message_count == 0


def test_adapt_kwargs_maps_aliases():
    async def fn(gid, count=50, offset=0):
        return []

    assert _adapt_kwargs(fn, {"group_id": 1, "count": 5, "offset": 0}) == {
        "gid": 1,
        "count": 5,
        "offset": 0,
    }


def test_adapt_kwargs_refuses_unknown_parameter():
    """它不收的参数就别硬送——那正是 TypeError 的来源。"""

    async def fn(gid):
        return []

    assert _adapt_kwargs(fn, {"group_id": 1, "message_seq": "a"}) is None
    assert _adapt_kwargs(fn, {"offset": 0}) is None


def test_adapt_kwargs_honours_kwargs_sink():
    """收 `**kwargs` 的实现（OneBot 客户端那样）按原样送。"""

    async def fn(**kwargs):
        return []

    assert _adapt_kwargs(fn, {"group_id": 1}) == {"group_id": 1}
