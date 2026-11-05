"""第 26 轮：缺失时间戳冒充「现在」，以及 overwrite 的归属边界。"""

import time
from datetime import datetime

from backup.core.models import BackupRecord, BackupStatus
from backup.core.parser import parse_message
from backup.core.restore_engine import RestoreEngine
from backup.core.verifier import compute_checksum
from backup.tests.conftest import make_message


def _record(db, backup_id, msg, group_id=100):
    db.save_record(
        BackupRecord(
            backup_id=backup_id,
            group_id=group_id,
            status=BackupStatus.COMPLETED,
            started_at=datetime.now(),
            message_count=1,
            checksum=compute_checksum([msg]),
        )
    )


class TestOverwriteSemantics:
    """overwrite 的两条边界，防止以后改判据时把语义改歪。

    这两条在改动前就是对的（`overwrite_message` 带 backup_id 时会连归属一起改），
    写下来是为了钉住：
    - 覆盖别人的行，内容和归属都得换；
    - 覆盖自己名下已有的行，来源登记不能被 `delete_sources` 顺手删空。
    """

    async def test_overwrite_takes_over_content_and_owner(self, db, monkeypatch):
        original = make_message(1)  # 「消息内容 1」
        stale = make_message(1)
        stale.content = "编辑前的旧内容"

        _record(db, "bk_good", original)
        db.add_sources("bk_good", [original])
        db.save_message(stale, "bk_other")
        _record(db, "bk_other", stale)

        engine = RestoreEngine(db)
        # 备份包里的那份（`_load_messages` 走 JOIN 会取库里的内容，这里直接给包里那条）
        monkeypatch.setattr(engine, "_load_messages", lambda *a, **k: [make_message(1)])

        result = await engine.restore("bk_good", 100, strategy="overwrite", confirm=True)

        assert result.overwritten == 1
        row = db.query_one(
            "SELECT content, backup_id FROM messages WHERE group_id = 100 AND message_id = 'm1'"
        )
        assert row["content"] == "消息内容 1"
        assert row["backup_id"] == "bk_good"
        sources = {
            r["backup_id"]
            for r in db.query("SELECT backup_id FROM backup_sources WHERE message_id = 'm1'")
        }
        assert sources == {"bk_good"}

    async def test_overwrite_keeps_source_when_row_already_ours(self, db):
        """覆盖自己名下的行时，`delete_sources` 会把刚登记的来源也删掉吗？

        顺序是 overwrite → delete_sources → add_sources，删在前、加在后，所以不会。
        这条钉住顺序：哪天有人把 `add_sources` 挪到前面，来源清单就空了。
        """
        msg = make_message(1)
        db.save_message(msg, "bk")
        db.add_sources("bk", [msg])
        _record(db, "bk", msg)

        result = await RestoreEngine(db).restore("bk", 100, strategy="overwrite", confirm=True)

        assert result.overwritten == 1
        assert db.source_message_ids("bk", 100) == {"m1"}


class TestMissingTimestamp:
    def test_missing_timestamp_is_not_now(self):
        """平台不给时间戳时不能拿「现在」顶上：同一批数据两次解析会得到两个指纹。"""
        raw = {
            "message_id": "m1",
            "group_id": 100,
            "sender": {"user_id": 7},
            "message": [{"type": "plain", "data": {"text": "你好"}}],
        }
        first = parse_message(raw, 100)
        time.sleep(0.05)
        second = parse_message(raw, 100)
        assert first.timestamp == second.timestamp
        assert compute_checksum([first]) == compute_checksum([second])
        assert first.timestamp.year < 2000

    def test_zero_timestamp_is_not_epoch_message(self):
        """`timestamp: 0` 是「没给」，不是「1970 年的消息」。"""
        raw = {
            "message_id": "m1",
            "group_id": 100,
            "timestamp": 0,
            "sender": {"user_id": 7},
            "message": [{"type": "plain", "data": {"text": "你好"}}],
        }
        assert parse_message(raw, 100).timestamp.year < 2000

    def test_real_timestamp_still_parsed(self):
        """真给了时间戳就照常用，别被上面那条兜底顺手废掉。"""
        raw = {
            "message_id": "m1",
            "group_id": 100,
            "timestamp": 1714557600000,  # 毫秒
            "sender": {"user_id": 7},
            "message": [{"type": "plain", "data": {"text": "你好"}}],
        }
        assert parse_message(raw, 100).timestamp.year == 2024
