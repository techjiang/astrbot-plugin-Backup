"""第 32 轮：适配器参数名叫 `gid`，尾部还挂着 `**kwargs`。

这一条是从「真 AstrBot 4.14.6 + 真 pipeline」里跑出来的，不是猜的。
`_adapt_kwargs` 原来见到 `**kwargs` 就原样透传，于是 `group_id=` 落进 `**kw`，
显式参数 `gid` 永远缺参 —— 每次调用都 TypeError，而下一句是「换下一个名字」，
下一个名字根本不存在，整条历史消息拉取路径静默失败：备份报成功、0 条。
"""

import asyncio
import inspect

import pytest

from backup.core.backup_engine import PlatformAdapter, _adapt_kwargs
from backup.core.database import Database
from backup.core.backup_engine import BackupEngine


class GidPlatform:
    """真适配器最常见的一种写法：位置参数叫 gid，尾部挂 **kwargs。"""

    def __init__(self):
        self.calls = []

    async def get_group_info(self, gid, **kw):
        self.calls.append(("info", gid))
        return {"group_id": gid, "group_name": "老群", "member_count": 2}

    async def get_group_members(self, gid, **kw):
        self.calls.append(("members", gid))
        return [{"user_id": 1, "nickname": "a", "role": "owner"}]

    async def get_group_announcements(self, gid, **kw):
        self.calls.append(("ann", gid))
        return []

    async def get_group_files(self, gid, **kw):
        self.calls.append(("files", gid))
        return []

    async def get_group_history(self, gid, count=50, offset=0, **kw):
        self.calls.append(("history", gid, count, offset))
        if offset:
            return []
        return [
            {
                "message_id": f"m{i}",
                "group_id": gid,
                "timestamp": 1700000000 + i,
                "sender": {"user_id": 1, "nickname": "a"},
                "message": [{"type": "text", "data": {"text": f"hi {i}"}}],
            }
            for i in range(3)
        ]


def test_alias_mapped_even_with_kwargs_sink():
    """`gid` + `**kwargs`：`group_id` 必须映射成 `gid`，而不是塞进 **kw。"""

    async def fn(gid, count=50, offset=0, **kw):
        ...

    mapped = _adapt_kwargs(fn, {"group_id": 100, "count": 50, "offset": 0})
    assert mapped is not None
    assert mapped["gid"] == 100
    assert "group_id" not in mapped
    inspect.signature(fn).bind(**mapped)  # 真的能绑上，不只是字面像


def test_extra_param_goes_to_kwargs_sink():
    """映射不到的额外参数（`message_seq`）交给 `**kwargs` 兜，不能一律拒绝。"""

    async def fn(gid, count=50, **kw):
        ...

    mapped = _adapt_kwargs(fn, {"group_id": 7, "count": 10, "message_seq": "42"})
    assert mapped == {"gid": 7, "count": 10, "message_seq": "42"}
    inspect.signature(fn).bind(**mapped)


def test_signature_without_kwargs_still_rejects_unknown():
    """不收 `**kwargs` 的实现，多余参数仍然要判「不匹配」，不能偷偷送进去。"""

    async def fn(group_id, count=50):
        ...

    assert _adapt_kwargs(fn, {"group_id": 1, "message_seq": "x"}) is None


def test_fetch_page_works_against_gid_signature():
    adapter = PlatformAdapter(GidPlatform())
    page = asyncio.run(adapter._fetch_page(100, 50, 0, None))
    assert len(page) == 3
    assert adapter.history_broken is False


def test_full_backup_collects_from_gid_plus_kwargs(tmp_path):
    """端到端：备份真的拉到消息，而不是报 `failed` / 0 条。"""

    platform = GidPlatform()

    async def run():
        db = Database(str(tmp_path / "t.db"))
        engine = BackupEngine(db, str(tmp_path / "media"), {})
        engine.media.download = _no_download

        record = await engine.backup_group(100, platform, incremental=True)
        return record, db

    record, db = asyncio.run(run())
    assert record.message_count == 3
    assert db.count_messages(100) == 3
    assert "历史消息接口调用失败" not in (record.error or "")


async def _no_download(url, message_id, timestamp):
    raise AssertionError("这条用例不该触发媒体下载")
