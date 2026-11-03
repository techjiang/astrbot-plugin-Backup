"""第 10 轮：状态快照、翻页、包完整性、换机器恢复。

这一轮找的都是「不报错、只是悄悄不对」的问题：成员名单只增不减、平台少给几条
就少备几截、包里改几个字段照样能恢复。
"""

import json
import zipfile
from pathlib import Path

import pytest

from backup.core.backup_engine import PAGE_SIZE, BackupEngine
from backup.core.database import Database
from backup.core.event_hooks import EventHooks
from backup.core.auto_trigger import AutoTrigger
from backup.core.export_manager import ExportManager
from backup.core.restore_engine import RestoreEngine, RestoreError
from backup.core.verifier import compute_manifest_checksum, read_manifest, verify_zip

from .conftest import BASE_TIME, raw_history


def _raw(count, group_id=100, start=0, base=None):
    import datetime as dt

    base = base or BASE_TIME
    return [
        {
            "message_id": f"m{start + i}",
            "group_id": group_id,
            "sender": {"user_id": 1},
            "timestamp": (base + dt.timedelta(seconds=start + i)).timestamp(),
            "message": [{"type": "text", "data": {"text": f"第{start + i}条"}}],
        }
        for i in range(count)
    ]


class Plat:
    def __init__(self, history, members=None, info=None):
        self.history = history
        self.members = members or []
        self.info = info or {}

    async def get_group_info(self, group_id=None):
        return {"group_id": group_id, "group_name": f"群{group_id}", **self.info}

    async def get_group_members(self, group_id=None):
        return self.members

    async def get_group_history(self, group_id=None, count=50, offset=0):
        return self.history[offset : offset + count]


@pytest.fixture
def engine(db, tmp_path):
    return BackupEngine(
        db,
        tmp_path / "media",
        {"scope": {"group_announcements": False, "group_files": False}},
    )


class TestMemberSnapshot:
    async def test_members_that_left_disappear(self, db, engine):
        """成员列表是当前状态：退群的人必须从名单里消失。

        原来只做 upsert，名单只增不减，`/backup status` 的成员数永远不降。
        """
        everyone = [{"user_id": i, "nickname": f"人{i}"} for i in range(1, 21)]
        await engine.backup_group(100, Plat(_raw(2), everyone), download_media=False)
        assert len(db.get_members(100)) == 20

        left_ten = [{"user_id": i, "nickname": f"人{i}"} for i in range(1, 11)]
        await engine.backup_group(100, Plat(_raw(2, start=2), left_ten), download_media=False)
        assert len(db.get_members(100)) == 10

    async def test_empty_member_list_keeps_previous_snapshot(self, db, engine):
        """拉回来是空表（权限不足）不能拿它把快照清空。"""
        everyone = [{"user_id": i, "nickname": f"人{i}"} for i in range(1, 6)]
        await engine.backup_group(100, Plat(_raw(2), everyone), download_media=False)
        await engine.backup_group(100, Plat(_raw(2, start=2), []), download_media=False)
        assert len(db.get_members(100)) == 5


class TestPaging:
    async def test_short_pages_do_not_end_paging(self, db, engine):
        """适配器每页只给 30 条（不认 count）时，不能只备第一页。

        原来用「本页不足 PAGE_SIZE 就收工」判断到底，第一页 30 条 < 50 就直接退出，
        剩下 90 条一条没备，备份还报 completed。
        """

        class ShortPages(Plat):
            async def get_group_history(self, group_id=None, count=50, offset=0):
                return self.history[offset : offset + 30]

        await engine.backup_group(100, ShortPages(_raw(120)), download_media=False)
        assert db.count_messages(100) == 120

    async def test_offset_follows_returned_count(self, db, engine):
        """平台把 offset 当序号而不是计数时，按真实返回条数推进才不会跳段。"""

        class HalfPages(Plat):
            async def get_group_history(self, group_id=None, count=50, offset=0):
                return self.history[offset : offset + PAGE_SIZE // 2]

        await engine.backup_group(100, HalfPages(_raw(150)), download_media=False)
        assert db.count_messages(100) == 150


class TestTotalSize:
    async def test_record_size_counts_only_its_own_messages(self, db, engine, tmp_path):
        """每条记录的体积只算它覆盖的消息，不是整群媒体的总和。"""
        media = tmp_path / "media" / "20240501"
        media.mkdir(parents=True)
        files = []
        for i in range(3):
            f = media / f"m{i}.bin"
            f.write_bytes(b"x" * (i + 1) * 100)
            files.append(f)

        record = await engine.backup_group(100, Plat(_raw(3)), download_media=False)
        for i, f in enumerate(files):
            db.save_media(
                message_id=f"m{i}",
                group_id=100,
                original_url=f"http://x/{i}",
                local_path=str(f),
                filename=f.name,
                file_size=f.stat().st_size,
                mime_type="",
                sha256="",
            )
        second = await engine.backup_group(
            100, Plat(_raw(3, start=3)), download_media=False
        )
        # 第二轮没有新媒体，体积就是 0，而不是「全群媒体总量」
        assert second.total_size == 0
        assert record.total_size == 0


class TestExportIsolation:
    async def test_export_never_carries_other_groups_messages(self, db, engine, tmp_path):
        await engine.backup_group(100, Plat(_raw(3, group_id=100)), download_media=False)
        await engine.backup_group(200, Plat(_raw(4, group_id=200)), download_media=False)
        backup_100 = [r for r in db.list_records() if r.group_id == 100][0]
        # 来源清单被污染成「也覆盖了别的群的消息」
        db.add_sources(backup_100.backup_id, db.get_messages(group_id=200))

        exports = ExportManager(db, tmp_path / "exp", tmp_path / "media", {})
        ctx = exports.build_context(100, backup_100.backup_id)
        assert {m.group_id for m in ctx.messages} == {100}


class TestPackageIntegrity:
    async def test_tampered_group_name_is_rejected(self, db, engine, tmp_path):
        """群名/成员表不在消息校验和里，改了它原来照样能恢复。"""
        await engine.backup_group(100, Plat(_raw(3)), download_media=False)
        export = ExportManager(db, tmp_path / "exp", tmp_path / "media", {}).export_latest(
            100, "zip"
        )
        path = export.path
        _rewrite(path, lambda mf: mf["group_info"].__setitem__("group_name", "伪造群名"))

        assert verify_zip(path)[0] is False
        fresh = Database(tmp_path / "other.sqlite")
        with pytest.raises(RestoreError):
            await RestoreEngine(fresh).restore("x", None, "skip", archive=str(path), confirm=True)

    async def test_manifest_checksum_covers_members(self):
        base = {"group_id": 1, "group_name": "a", "group_info": {}, "members": []}
        assert compute_manifest_checksum(base) == compute_manifest_checksum(dict(base))
        changed = dict(base, members=[{"user_id": 2}])
        assert compute_manifest_checksum(base) != compute_manifest_checksum(changed)

    async def test_pack_messages_from_other_group_rejected(self, db, engine, tmp_path):
        """把包里的消息改成别群的，恢复时必须拒绝，而不是「成功恢复 0 条」。"""
        await engine.backup_group(100, Plat(_raw(3, group_id=100)), download_media=False)
        await engine.backup_group(200, Plat(_raw(3, group_id=200)), download_media=False)
        export = ExportManager(db, tmp_path / "exp", tmp_path / "media", {}).export_latest(
            100, "zip"
        )
        path = export.path
        _rewrite_messages(path, lambda msgs: [dict(m, group_id=200) for m in msgs])

        with pytest.raises(RestoreError):
            await RestoreEngine(db).restore("x", None, "skip", archive=str(path), confirm=True)


class TestCrossMachineRestore:
    async def test_group_name_and_members_come_back(self, engine, tmp_path):
        """换机器恢复后，群名和成员要能在 status / 导出里看到，不能是空白。"""
        members = [{"user_id": 1, "nickname": "甲", "role": "owner"}]
        await engine.backup_group(
            100, Plat(_raw(3), members, {"group_name": "老群"}), download_media=False
        )
        export = ExportManager(db := engine.db, tmp_path / "exp", tmp_path / "media", {}).export_latest(
            100, "zip"
        )

        fresh = Database(tmp_path / "fresh.sqlite")
        result = await RestoreEngine(fresh).restore(
            "remote", None, "skip", archive=str(export.path), confirm=True
        )
        assert result.inserted == 3
        info = fresh.get_group_info(result.target_group_id)
        assert info is not None and info.group_name == "老群"
        assert [m.nickname for m in fresh.get_members(result.target_group_id)] == ["甲"]


class TestStatusCounts:
    async def test_realtime_archive_stops_being_reported_after_backup(self, db, engine):
        """实时归档的消息被某次备份扫到之后，不该再算「还没被接手」。"""
        hooks = EventHooks(db, AutoTrigger({}), archive=True)
        history = _raw(3)
        for raw in history:
            hooks.archive_message({"group_id": 100, "message_obj": raw})
        assert db.count_realtime_only(100) == 3

        await engine.backup_group(100, Plat(history), download_media=False)
        assert db.count_realtime_only(100) == 0


def _rewrite(path: Path, mutate) -> None:
    tmp = path.with_suffix(".tmp.zip")
    with zipfile.ZipFile(path) as zin, zipfile.ZipFile(tmp, "w") as zout:
        for name in zin.namelist():
            data = zin.read(name)
            if name == "manifest.json":
                manifest = json.loads(data)
                mutate(manifest)
                data = json.dumps(manifest, ensure_ascii=False).encode()
            zout.writestr(name, data)
    tmp.replace(path)


def _rewrite_messages(path: Path, mutate) -> None:
    tmp = path.with_suffix(".tmp.zip")
    with zipfile.ZipFile(path) as zin, zipfile.ZipFile(tmp, "w") as zout:
        for name in zin.namelist():
            data = zin.read(name)
            if name.startswith("json/daily/"):
                data = json.dumps(mutate(json.loads(data)), ensure_ascii=False).encode()
            zout.writestr(name, data)
    tmp.replace(path)
