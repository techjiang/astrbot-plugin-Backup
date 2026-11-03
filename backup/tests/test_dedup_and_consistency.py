"""消息唯一性带来的连锁行为：重复备份不留重复行、导出只算本次备份、恢复必须对得上校验和。"""

import json
import zipfile
from datetime import datetime

import pytest

from backup.core.backup_engine import BackupEngine
from backup.core.commands import render_diff, render_status
from backup.core.database import Database
from backup.core.download_route import PREFIX, handle_download
from backup.core.export_manager import ExportManager
from backup.core.models import BackupRecord, BackupStatus
from backup.core.restore_engine import RestoreEngine, RestoreError
from backup.core.verifier import compute_checksum

from .conftest import FakePlatform, make_message, raw_history


def record(db, bid, group_id=100, count=0, checksum=None, when=None):
    # 不传 checksum 时按库里这批消息算一遍，跟真实备份记录的语义一致：
    # 记录里的校验和永远是「那次备份留下的消息集」的指纹
    if checksum is None:
        checksum = compute_checksum(db.get_messages(group_id=group_id, backup_id=bid))
    db.save_record(
        BackupRecord(
            backup_id=bid,
            group_id=group_id,
            status=BackupStatus.COMPLETED,
            started_at=when or datetime.now(),
            finished_at=when or datetime.now(),
            message_count=count,
            checksum=checksum,
        )
    )


class TestMessageUniqueness:
    def test_same_message_saved_twice_leaves_one_row(self, db):
        msg = make_message(0)
        assert db.save_message(msg, "bk1") is True
        assert db.save_message(msg, "bk2") is False  # 第二次不算新增
        assert db.count_messages(100) == 1

    def test_later_backup_takes_ownership(self, db):
        db.save_message(make_message(0), "bk1")
        db.save_message(make_message(0), "bk2")
        assert db.count_backup_source("bk1") == 0
        assert db.count_backup_source("bk2") == 1
        assert db.list_backup_ids_with_messages(100) == ["bk2"]

    def test_different_group_same_message_id_kept_separate(self, db):
        db.save_message(make_message(0, group_id=100), "bk1")
        db.save_message(make_message(0, group_id=200), "bk1")
        assert db.count_messages() == 2


class TestRepeatBackup:
    """全量重跑、增量重跑都不该让库里的消息数膨胀。"""

    async def test_repeated_full_backup_does_not_duplicate(self, db, tmp_path):
        engine = BackupEngine(db, tmp_path / "media", {"media": {}})
        platform = FakePlatform(history=raw_history(5))
        await engine.backup_group(100, platform, incremental=False, download_media=False)
        await engine.backup_group(100, platform, incremental=False, download_media=False)

        # 库里不膨胀就够。第二次全量一条也没新增，它不该再记 5 条，
        # 「这次备份覆盖了哪些消息」由 backup_id 归属来回答
        assert db.count_messages(100) == 5
        records = db.list_records(100)
        assert records[0].message_count == 0
        assert db.count_backup_source(records[0].backup_id) == 0
        assert db.count_backup_source(records[1].backup_id) == 5

    async def test_empty_backup_does_not_mask_previous(self, db, tmp_path):
        engine = BackupEngine(db, tmp_path / "media", {"media": {}})
        await engine.backup_group(
            100, FakePlatform(history=raw_history(4)), incremental=False, download_media=False
        )
        first = db.list_records(100)[0]

        await engine.backup_group(100, FakePlatform(history=[]), incremental=False, download_media=False)

        # 空跑一次不该顶掉上一次的实绩，否则「最近备份」永远显示 0 条
        assert db.latest_completed_record(100).backup_id == first.backup_id
        assert "这次覆盖：4 条" in render_status(db, 100)


class TestExportOwnership:
    def test_backup_export_only_covers_its_own_messages(self, db, tmp_path):
        for i in range(4):
            db.save_message(make_message(i), "bk1")
        for i in range(4, 6):
            db.save_message(make_message(i), "bk2")
        record(db, "bk1", count=4)
        record(db, "bk2", count=2)

        manager = ExportManager(db, tmp_path / "exports", tmp_path / "media")
        # 增量备份 bk2 只导自己新增的 2 条，不能把 bk1 的 4 条再算一遍
        assert len(manager.build_context(100, "bk2").messages) == 2
        assert len(manager.build_context(100, "bk1").messages) == 4
        assert len(manager.build_context(100).messages) == 6  # 不指定就是整群

    def test_backup_id_argument_is_not_swallowed(self, db, tmp_path):
        """未指定 backup_id 时不能退化成「整群」——早期写法会这样。"""
        db.save_message(make_message(0), "bk1")
        db.save_message(make_message(1), "bk2")
        manager = ExportManager(db, tmp_path / "exports", tmp_path / "media")
        assert [m.message_id for m in manager.build_context(100, "bk1").messages] == ["m0"]

    def test_export_latest_uses_latest_record(self, db, tmp_path):
        db.save_message(make_message(0), "bk1")
        record(db, "bk1", count=1, when=datetime(2024, 5, 1))
        db.save_message(make_message(1), "bk2")
        record(db, "bk2", count=1, when=datetime(2024, 5, 2))

        manager = ExportManager(db, tmp_path / "exports", tmp_path / "media")
        result = manager.export_latest(100, "txt")
        assert result.message_count == 1
        assert "消息内容 1" in result.path.read_text(encoding="utf-8")
        assert "消息内容 0" not in result.path.read_text(encoding="utf-8")

    def test_export_latest_keeps_realtime_only_messages(self, db, tmp_path):
        """群被炸时最后几条只在实时归档里，导最近一次备份不能把它们漏掉。"""
        db.save_message(make_message(0), "bk1")
        record(db, "bk1", count=1, when=datetime(2024, 5, 1))
        db.save_message(make_message(1), "realtime")  # 备份之后才进群的消息

        manager = ExportManager(db, tmp_path / "exports", tmp_path / "media")
        result = manager.export_latest(100, "txt")
        assert result.message_count == 2
        assert "消息内容 1" in result.path.read_text(encoding="utf-8")

    def test_html_lists_backup_id(self, db, tmp_path):
        db.save_message(make_message(0), "bk_visible")
        record(db, "bk_visible", count=1)
        manager = ExportManager(db, tmp_path / "exports", tmp_path / "media")
        html = manager.export(100, "html", backup_id="bk_visible").path.read_text(encoding="utf-8")
        assert "bk_visible" in html
        # 没指定备份时不该冒出空的「备份 ID：」
        whole = manager.export(100, "txt").path.read_text(encoding="utf-8")
        assert "bk_visible" not in whole


class TestRestoreChecksum:
    """恢复必须校验到内容本身，不能只看包自己的 manifest。"""

    async def _seeded(self, db):
        msgs = [make_message(i) for i in range(3)]
        for m in msgs:
            db.save_message(m, "bk1")
        record(db, "bk1", count=3, checksum=compute_checksum(msgs))
        return msgs

    async def test_tampered_database_rejected(self, db):
        await self._seeded(db)
        db.execute("UPDATE messages SET content = '被改过' WHERE message_id = 'm1'")
        with pytest.raises(RestoreError):
            await RestoreEngine(db).restore("bk1", 999, confirm=True)

    async def test_tampered_zip_with_self_consistent_manifest_rejected(self, db, tmp_path):
        await self._seeded(db)
        forged = make_message(0, content="替换后的内容")
        archive = tmp_path / "forged.zip"
        with zipfile.ZipFile(archive, "w") as zf:
            zf.writestr(
                "messages.json",
                json.dumps([{"message_id": forged.message_id, "group_id": 100,
                             "sender_id": 1, "sender_name": "u",
                             "timestamp": forged.timestamp.isoformat(),
                             "message_type": "text", "content": forged.content}], ensure_ascii=False),
            )
            # 攻击者按算法重算 manifest，包自身自洽，但和记录里的校验和对不上
            zf.writestr("manifest.json", json.dumps({"checksum": compute_checksum([forged])}))

        with pytest.raises(RestoreError):
            await RestoreEngine(db).restore("bk1", 888, archive=archive, confirm=True)
        assert db.count_messages(888) == 0

    async def test_zip_restore_reads_daily_json(self, db, tmp_path):
        msgs = await self._seeded(db)
        archive = tmp_path / "good.zip"
        with zipfile.ZipFile(archive, "w") as zf:
            zf.writestr(
                "json/daily/2024-05-01.json",
                json.dumps(
                    [
                        {"message_id": m.message_id, "group_id": 100, "sender_id": m.sender_id,
                         "sender_name": m.sender_name, "timestamp": m.timestamp.isoformat(),
                         "message_type": "text", "content": m.content}
                        for m in msgs
                    ],
                    ensure_ascii=False,
                ),
            )
            zf.writestr("manifest.json", json.dumps({"checksum": compute_checksum(msgs)}))

        result = await RestoreEngine(db).restore("bk1", 777, archive=archive, confirm=True)
        assert result.inserted == 3
        assert db.count_messages(777) == 3


class TestMediaCount:
    async def test_media_count_counts_successes_only(self, db, tmp_path, monkeypatch):
        history = raw_history(3)
        for raw in history:
            raw["message"] = [{"type": "image", "data": {"url": f"https://cdn/{raw['message_id']}.png"}}]
        engine = BackupEngine(db, tmp_path / "media", {"media": {}})

        async def ok(url, message_id, timestamp, session=None):
            p = tmp_path / "media" / f"{message_id}.png"
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(b"x")
            return str(p)

        monkeypatch.setattr(engine.media, "download", ok)
        first = await engine.backup_group(100, FakePlatform(history=history), incremental=False)
        assert first.media_count == 3
        assert first.status is BackupStatus.COMPLETED

    async def test_partial_records_failed_count(self, db, tmp_path, monkeypatch):
        history = raw_history(2)
        for raw in history:
            raw["message"] = [{"type": "image", "data": {"url": "https://cdn/x.png"}}]
        engine = BackupEngine(db, tmp_path / "media", {"media": {}})

        async def boom(*a, **kw):
            raise RuntimeError("down")

        monkeypatch.setattr(engine.media, "download", boom)
        rec = await engine.backup_group(100, FakePlatform(history=history), incremental=False)
        assert rec.media_count == 0
        assert rec.status is BackupStatus.PARTIAL
        assert "2 个媒体文件下载失败" in rec.error


class TestLegacyDatabaseMigration:
    """升级前建的库：唯一键带 backup_id，直接沿用会继续堆重复消息。"""

    def _legacy_db(self, path):
        import sqlite3

        conn = sqlite3.connect(path)
        conn.executescript(
            """
            CREATE TABLE messages (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              message_id TEXT NOT NULL,
              group_id INTEGER NOT NULL,
              sender_id INTEGER NOT NULL,
              sender_name TEXT,
              timestamp DATETIME NOT NULL,
              message_type TEXT NOT NULL,
              content TEXT,
              raw_content TEXT,
              reply_to TEXT,
              is_forward BOOLEAN DEFAULT 0,
              media_paths TEXT,
              forward_chain TEXT,
              backup_id TEXT NOT NULL,
              created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
              UNIQUE(message_id, group_id, backup_id)
            );
            """
        )
        rows = [
            ("m0", 100, "bk1", "旧内容"),
            ("m0", 100, "bk2", "旧内容"),
            ("m1", 100, "bk1", "第二条"),
        ]
        conn.executemany(
            """INSERT INTO messages
               (message_id, group_id, sender_id, sender_name, timestamp, message_type,
                content, raw_content, backup_id)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            [(mid, gid, 1, "u", "2024-05-01 10:00:00.000000", "text", c, "{}", bid) for mid, gid, bid, c in rows],
        )
        conn.commit()
        conn.close()

    def test_duplicates_collapsed_and_new_constraint_applied(self, tmp_path):
        path = tmp_path / "legacy.sqlite"
        self._legacy_db(path)

        db = Database(path)
        try:
            assert db.count_messages(100) == 2  # m0 的两行合并
            db.save_message(make_message(0), "bk3")
            assert db.count_messages(100) == 2  # 新键生效，不再新增一行
            assert db.count_backup_source("bk3") == 1
        finally:
            db.close()

    def test_migration_is_idempotent(self, tmp_path):
        path = tmp_path / "legacy.sqlite"
        self._legacy_db(path)
        Database(path).close()
        db = Database(path)  # 第二次打开不该再重建表
        try:
            assert db.count_messages(100) == 2
        finally:
            db.close()


class TestParserRoundTrip:
    """ZIP 恢复读的是导出器摊平后的 dict，解析必须还原出同样的内容。"""

    def test_flat_dict_keeps_content_and_type(self):
        from backup.core.parser import parse_message

        msg = parse_message(
            {
                "message_id": "m0",
                "group_id": 100,
                "sender_id": 1,
                "sender_name": "用户1",
                "timestamp": "2024-05-01T10:00:00",
                "message_type": "text",
                "content": "消息内容 0",
            }
        )
        assert msg.content == "消息内容 0"
        assert msg.message_type.value == "text"

    def test_segment_without_type_does_not_dump_json(self):
        from backup.core.parser import parse_message

        msg = parse_message({"message_id": "m1", "group_id": 1, "content": "裸内容"})
        assert msg.content == "裸内容"
        assert "{" not in msg.content

    async def test_export_then_restore_checksum_matches(self, db, tmp_path):
        """导出 → 打包 → 恢复，内容与校验和都得对得上。"""
        msgs = [make_message(i) for i in range(3)]
        for m in msgs:
            db.save_message(m, "bk1")
        record(db, "bk1", count=3, checksum=compute_checksum(msgs))

        manager = ExportManager(db, tmp_path / "exports", tmp_path / "media")
        exported = manager.export(100, "zip", backup_id="bk1")

        fresh = Database(tmp_path / "fresh.sqlite")
        try:
            record(fresh, "bk1", count=3, checksum=compute_checksum(msgs))
            restored = await RestoreEngine(fresh).restore(
                "bk1", 777, archive=exported.path, confirm=True
            )
            assert restored.inserted == 3
            assert {m.content for m in fresh.get_messages(777)} == {m.content for m in msgs}
        finally:
            fresh.close()


class TestRealtimeArchiveOwnership:
    """实时归档是「群被炸时的兜底」，后续备份不能把它挪走或漏掉。"""

    def _archive(self, db, message_id="m50", minute=50):
        from backup.core.auto_trigger import AutoTrigger
        from backup.core.event_hooks import EventHooks

        payload = {
            "message_id": message_id,
            "group_id": 100,
            "sender": {"user_id": 1, "nickname": "u"},
            "timestamp": (datetime(2024, 5, 1, 10, minute)).timestamp(),
            "message": [{"type": "text", "data": {"text": "最后一句"}}],
        }

        # 用实例属性而不是类属性：类属性的 dict 是共享的，改一次污染所有用例
        class Event:
            group_id = 100
            sender_id = 1
            sender_name = "u"
            message_str = "最后一句"

        event = Event()
        event.message_obj = payload
        EventHooks(db, AutoTrigger({})).archive_message(event)

    async def test_full_backup_keeps_realtime_ownership(self, db, tmp_path):
        self._archive(db)
        engine = BackupEngine(db, tmp_path / "media", {"media": {}})
        # 平台接口只回得到 0..9 分钟，实时归档那条（50 分钟）平台已经取不到了
        await engine.backup_group(
            100, FakePlatform(history=raw_history(10)), incremental=False, download_media=False
        )

        assert db.count_messages(100) == 11
        assert db.message_backup_id("m50", 100) == "realtime"

    async def test_realtime_message_survives_export_latest(self, db, tmp_path):
        self._archive(db)
        engine = BackupEngine(db, tmp_path / "media", {"media": {}})
        await engine.backup_group(
            100, FakePlatform(history=raw_history(10)), incremental=False, download_media=False
        )
        result = ExportManager(db, tmp_path / "exports", tmp_path / "media").export_latest(100, "txt")
        assert result.message_count == 11
        assert "最后一句" in result.path.read_text(encoding="utf-8")

    async def test_realtime_archive_does_not_advance_incremental_point(self, db, tmp_path):
        """实时归档按到达顺序写库，时间戳乱序，不能拿它当增量点。"""
        engine = BackupEngine(db, tmp_path / "media", {"media": {}})
        await engine.backup_group(
            100, FakePlatform(history=raw_history(5)), incremental=False, download_media=False
        )
        self._archive(db, message_id="m50", minute=50)  # 值夜班的人抢先归档了一条

        platform = FakePlatform(history=raw_history(8))
        record = await engine.backup_group(100, platform, incremental=True, download_media=False)
        # 增量点仍停在 m4，m5..m7 必须补进来
        assert record.message_count == 3
        assert db.count_messages(100) == 9


class TestResumeAfterInterruption:
    """备份中途断掉后重跑，不该把下半截重下、重计。"""

    async def test_rerun_does_not_redownload_media(self, db, tmp_path):
        history = raw_history(3)
        history[0]["message"] = [
            {"type": "image", "data": {"url": "https://example.invalid/a.png"}}
        ]
        engine = BackupEngine(db, tmp_path / "media", {"media": {}})
        downloads = []

        async def fake_download(url, message_id, timestamp, session=None):
            downloads.append(message_id)
            path = tmp_path / "media" / f"{message_id}.png"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"x")
            return str(path)

        engine.media.download = fake_download
        platform = FakePlatform(history=history)
        await engine.backup_group(100, platform, incremental=False, download_media=True)
        second = await engine.backup_group(100, platform, incremental=False, download_media=True)

        assert downloads == ["m0"]
        assert second.message_count == 0
        assert second.media_count == 0
        assert db.count_messages(100) == 3

    async def test_resume_collects_only_missing_half(self, db, tmp_path):
        """模拟上半截已入库、增量点没推进：续传只该补剩下的。"""
        engine = BackupEngine(db, tmp_path / "media", {"media": {}})
        full = FakePlatform(history=raw_history(120))
        await engine.backup_group(100, FakePlatform(history=raw_history(60)), incremental=False, download_media=False)

        record = await engine.backup_group(100, full, incremental=True, download_media=False)

        assert record.message_count == 60
        assert db.count_messages(100) == 120
