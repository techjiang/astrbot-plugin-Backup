"""第 9 轮：备份「什么都没新拉到」和「库里有主不在任何备份」这两种状态。

两条都会让备份看起来成功、恢复却是空的：
- 校验和只算本轮新拉到的消息，增量为 0 时等于空数据的校验和；
- 上一轮淘汰掉的记录留下无主消息，占着唯一键，永远进不了任何来源清单。
"""

import pytest

from backup.core.backup_engine import BackupEngine
from backup.core.database import REALTIME_BACKUP_ID
from backup.core.restore_engine import RestoreEngine
from backup.core.verifier import compute_checksum

from .conftest import raw_history


@pytest.fixture
def engine(db, tmp_path):
    return BackupEngine(db, tmp_path / "media", {"scope": {}})


class TestRepeatBackupChecksum:
    async def test_second_backup_without_new_messages_keeps_checksum(self, db, engine):
        """同一批数据再备一次：校验和必须和第一次一致。

        只算「本轮新拉到的消息」时，第二轮的 collected 是空的，校验和会退化成空
        数据的 SHA-256；恢复按来源取出一整批消息再比对，必然判成「备份已损坏」。
        """
        history = raw_history(5)
        first = await engine.backup_group(100, _platform(history), download_media=False)
        second = await engine.backup_group(100, _platform(history), download_media=False)

        assert first.status.value == "completed"
        # 第二轮一条没新增（new_count 0），但它覆盖的还是这 5 条
        assert second.new_count == 0
        assert second.message_count == 5
        assert second.checksum == first.checksum
        assert second.checksum != compute_checksum([])

    async def test_restore_after_repeat_backup_still_works(self, db, engine):
        history = raw_history(5)
        await engine.backup_group(100, _platform(history), download_media=False)
        second = await engine.backup_group(100, _platform(history), download_media=False)

        result = await RestoreEngine(db).restore(second.backup_id, 999, "skip", confirm=True)
        assert result.inserted == 5


class TestAdoptOrphans:
    async def test_messages_left_by_pruned_record_come_back(self, db, engine):
        """记录被淘汰后留下的消息行必须被下一次备份收编。

        不收编的话它们占着唯一键 (message_id, group_id)，备份看到「已在库里」跳过，
        于是永远进不了任何来源清单——备份报成功，恢复是空的。
        """
        history = raw_history(5)
        first = await engine.backup_group(100, _platform(history), download_media=False)
        db.delete_record(first.backup_id)
        # 模拟「记录已不在、消息还在」：老库/迁移残留会长这样
        for i in range(5):
            raw = history[i]
            db.execute(
                """INSERT INTO messages
                   (message_id, group_id, sender_id, timestamp, message_type, content, backup_id)
                   VALUES (?,?,?,?,?,?,?)""",
                (raw["message_id"], 100, 1, "2024-05-01 10:00:00", "text", "x", "bk_已淘汰"),
            )
        assert db.get_record("bk_已淘汰") is None
        assert db.count_messages(100) == 5

        second = await engine.backup_group(100, _platform(history), download_media=False)

        assert db.count_sources(second.backup_id) == 5
        result = await RestoreEngine(db).restore(second.backup_id, 999, "skip", confirm=True)
        assert result.inserted == 5

    async def test_owned_messages_are_not_stolen(self, db, engine):
        """归属还在的备份名下的消息不能被后续备份抢走。"""
        history = raw_history(3)
        first = await engine.backup_group(100, _platform(history), download_media=False)
        await engine.backup_group(100, _platform(history), download_media=False)

        for i in range(3):
            assert db.message_backup_id(f"m{i}", 100) == first.backup_id

    async def test_realtime_messages_are_taken_over(self, db, engine):
        """实时归档的归属可以被备份接手：它们本来就该进某一份备份的来源清单。"""
        history = raw_history(2)
        for raw in history:
            db.execute(
                """INSERT INTO messages
                   (message_id, group_id, sender_id, timestamp, message_type, content, backup_id)
                   VALUES (?,?,?,?,?,?,?)""",
                (raw["message_id"], 100, 1, "2024-05-01 10:00:00", "text", "x", REALTIME_BACKUP_ID),
            )

        record = await engine.backup_group(100, _platform(history), download_media=False)

        assert db.count_sources(record.backup_id) == 2
        assert db.message_backup_id("m0", 100) != REALTIME_BACKUP_ID


class TestDeleteRecord:
    async def test_delete_record_removes_its_messages(self, db, engine):
        """显式删记录要连消息一起删：留着只会在唯一键上占位。"""
        history = raw_history(4)
        record = await engine.backup_group(100, _platform(history), download_media=False)
        db.delete_record(record.backup_id)

        assert db.count_messages(100) == 0
        # 紧接着的备份能重新把消息收进来
        again = await engine.backup_group(100, _platform(history), download_media=False)
        assert again.message_count == 4


def _platform(history):
    """只包一层：BackupEngine 需要 PlatformAdapter 认得的方法名。"""

    class _P:
        async def get_group_info(self, group_id=None, **kwargs):
            return {"group_id": group_id, "group_name": "测试群"}

        async def get_group_member_list(self, group_id=None, **kwargs):
            return []

        async def get_group_notice(self, group_id=None, **kwargs):
            return []

        async def get_group_files(self, group_id=None, **kwargs):
            return []

        async def get_group_msg_history(self, group_id=None, count=50, offset=0, **kwargs):
            return history[offset : offset + count]

    return _P()
