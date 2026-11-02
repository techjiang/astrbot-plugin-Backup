import zipfile
from pathlib import Path

import pytest

from backup.core.database import Database
from backup.core.export_manager import ExportManager
from backup.core.models import BackupStatus
from backup.core.restore_engine import RestoreEngine, RestoreError
from backup.core.verifier import compute_checksum, verify_zip

from .conftest import make_message


async def seed(db, n=5, group_id=100, backup_id="bk1"):
    msgs = [make_message(i, group_id=group_id) for i in range(n)]
    for m in msgs:
        db.save_message(m, backup_id)
    return msgs


class TestRestoreEngine:
    async def test_restore_validates_checksum(self, db, tmp_path):
        msgs = await seed(db)
        from backup.core.models import BackupRecord
        from datetime import datetime

        db.save_record(
            BackupRecord(
                backup_id="bk1",
                group_id=100,
                status=BackupStatus.COMPLETED,
                started_at=datetime.now(),
                finished_at=datetime.now(),
                message_count=len(msgs),
                checksum=compute_checksum(msgs),
            )
        )
        engine = RestoreEngine(db)
        assert engine.verify_backup("bk1")[0] is True

    async def test_restore_rejects_corrupted_backup(self, db, tmp_path):
        from backup.core.models import BackupRecord
        from datetime import datetime

        db.save_record(
            BackupRecord(
                backup_id="bk1",
                group_id=100,
                status=BackupStatus.COMPLETED,
                started_at=datetime.now(),
                finished_at=datetime.now(),
                checksum="deadbeef",
            )
        )
        await seed(db)
        archive = tmp_path / "broken.zip"
        archive.write_bytes(b"not a zip at all")
        engine = RestoreEngine(db)
        with pytest.raises(RestoreError):
            engine.verify_backup("bk1", archive)
        with pytest.raises(RestoreError):
            await engine.restore("bk1", archive=archive, confirm=True)

    async def test_restore_to_different_group(self, db):
        from backup.core.models import BackupRecord
        from datetime import datetime

        await seed(db, n=4)
        db.save_record(
            BackupRecord(
                backup_id="bk1",
                group_id=100,
                status=BackupStatus.COMPLETED,
                started_at=datetime.now(),
                finished_at=datetime.now(),
                message_count=4,
            )
        )
        result = await RestoreEngine(db).restore("bk1", target_group_id=200, confirm=True)
        assert result.inserted == 4
        assert db.count_messages(200) == 4
        assert db.count_messages(100) == 4

    async def test_restore_requires_confirm(self, db):
        await seed(db)
        with pytest.raises(RestoreError):
            await RestoreEngine(db).restore("bk1")

    async def test_restore_skip_strategy(self, db):
        from backup.core.models import BackupRecord
        from datetime import datetime

        await seed(db, n=3)
        db.save_record(
            BackupRecord(
                backup_id="bk1",
                group_id=100,
                status=BackupStatus.COMPLETED,
                started_at=datetime.now(),
                finished_at=datetime.now(),
            )
        )
        result = await RestoreEngine(db).restore("bk1", strategy="skip", confirm=True)
        assert result.inserted == 0
        assert result.skipped == 3

    async def test_restore_rejects_unknown_backup(self, db):
        with pytest.raises(RestoreError):
            await RestoreEngine(db).restore("nope", confirm=True)

    async def test_restore_from_zip_archive(self, db, tmp_path):
        for m in [make_message(i) for i in range(3)]:
            db.save_message(m, "bk1")
        manager = ExportManager(db, tmp_path / "exports", tmp_path / "media")
        result = manager.export(100, "zip", backup_id="bk1")

        fresh = Database(tmp_path / "fresh.sqlite")
        from backup.core.models import BackupRecord
        from datetime import datetime

        fresh.save_record(
            BackupRecord(
                backup_id="bk1",
                group_id=100,
                status=BackupStatus.COMPLETED,
                started_at=datetime.now(),
                finished_at=datetime.now(),
                checksum="",  # 包内 manifest 校验和未知，跳过比对
            )
        )
        restored = await RestoreEngine(fresh).restore(
            "bk1", target_group_id=777, archive=result.path, confirm=True
        )
        assert restored.inserted == 3
        fresh.close()

    def test_verify_zip_detects_manifest_mismatch(self, tmp_path):
        archive = tmp_path / "x.zip"
        with zipfile.ZipFile(archive, "w") as zf:
            zf.writestr("manifest.json", '{"checksum": "abc"}')
        ok, reason = verify_zip(archive, "def")
        assert ok is False and "校验和" in reason
        ok, _ = verify_zip(archive, "abc")
        assert ok is True
