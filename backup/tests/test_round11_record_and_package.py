"""第 11 轮：三处「表面正常、实际不对」的问题。

1. 增量备份跑一轮全是已在库的消息，记录里写「覆盖 0 条」；
2. `/backup diff` 按归属取数，一次全量重跑之后两次备份永远「完全相同」；
3. 下载链接的 Range 探测把一次性 token 用掉，真正取数据那次 410；
4. 备份包里的群名/成员表改掉之后，恢复照样写进目标群。
"""

import asyncio
import json
import zipfile
from datetime import datetime

import pytest

from backup.core.commands import render_diff
from backup.core.database import Database
from backup.core.download_route import handle_download
from backup.core.export_manager import ExportManager, snapshot_of
from backup.core.models import (
    BackupRecord,
    BackupStatus,
    GroupInfo,
    GroupMember,
    Message,
    MessageType,
)
from backup.core.restore_engine import RestoreEngine, RestoreError
from backup.core.verifier import compute_checksum, compute_manifest_checksum
from backup.tests.conftest import BASE_TIME, FakePlatform, make_message, raw_history

GROUP = 100


def _record(db, backup_id, messages, checksum=None):
    db.save_record(
        BackupRecord(
            backup_id=backup_id,
            group_id=messages[0].group_id if messages else GROUP,
            status=BackupStatus.COMPLETED,
            started_at=datetime.now(),
            finished_at=datetime.now(),
            message_count=len(messages),
            checksum=checksum if checksum is not None else compute_checksum(messages),
        )
    )


def _store(db, backup_id, messages, own=True):
    _record(db, backup_id, messages)
    for m in messages:
        db.save_message(m, backup_id if own else "other")
    db.add_sources(backup_id, messages)


class TestRecordCounts:
    async def test_repeat_backup_reports_coverage_not_new(self, db, tmp_path):
        """重跑一轮没有新消息：new_count 是 0，但它覆盖的仍然是整批。"""
        from backup.core.backup_engine import BackupEngine

        engine = BackupEngine(db, tmp_path / "media", {"media": {}})
        history = raw_history(5)
        first = await engine.backup_group(GROUP, FakePlatform(history=history), download_media=False)
        again = await engine.backup_group(GROUP, FakePlatform(history=history), download_media=False)

        assert (first.new_count, first.message_count) == (5, 5)
        assert (again.new_count, again.message_count) == (0, 5)
        # 落库之后再读一遍，别让「只有内存里对」蒙混过去
        stored = db.get_record(again.backup_id)
        assert (stored.new_count, stored.message_count) == (0, 5)

    def test_old_database_gets_new_count_column(self, tmp_path):
        """升级前建的库没有 new_count 列，打开时得补上，否则读记录就崩。"""
        import sqlite3

        path = tmp_path / "legacy.sqlite"
        conn = sqlite3.connect(str(path))
        conn.executescript(
            """
            CREATE TABLE backup_records (
              backup_id TEXT PRIMARY KEY, group_id INTEGER NOT NULL, status TEXT NOT NULL,
              started_at DATETIME, finished_at DATETIME, message_count INTEGER DEFAULT 0,
              media_count INTEGER DEFAULT 0, total_size INTEGER DEFAULT 0,
              is_incremental BOOLEAN DEFAULT 0, base_backup_id TEXT, checksum TEXT, error TEXT
            );
            INSERT INTO backup_records (backup_id, group_id, status, message_count)
              VALUES ('bk_old', 100, 'completed', 5);
            """
        )
        conn.commit()
        conn.close()

        legacy = Database(path)
        try:
            record = legacy.get_record("bk_old")
            assert record.message_count == 5
            assert record.new_count == 0
            legacy.save_record(record)  # 能写回去，说明列真的补上了
        finally:
            legacy.close()


class TestDiffUsesSources:
    def test_diff_sees_messages_taken_over_by_rerun(self, db):
        """全量重跑把归属都接手了，老备份仍能按来源清单比出差异。"""
        old = [make_message(i) for i in range(2)]
        new = [make_message(i) for i in range(4)]
        _store(db, "bk1", old)
        _store(db, "bk2", new)
        # 再跑一次全量：整群消息的归属全划到 bk2
        for m in new:
            db.assign_message_backup(m.message_id, GROUP, "bk2")

        text = render_diff(db, "bk1", "bk2")
        assert "`bk1`：2 条" in text
        assert "仅在 bk2：2 条" in text
        assert "相同：2 条" in text

    def test_count_backup_source_does_not_follow_ownership(self, db):
        """老备份保住了几条，不因为归属被接手而变成 0。"""
        msgs = [make_message(i) for i in range(3)]
        _store(db, "bk1", msgs)
        for m in msgs:
            db.assign_message_backup(m.message_id, GROUP, "bk2")
        assert db.count_backup_source("bk1") == 3


class TestDownloadRange:
    def test_probe_then_resume_keeps_token_alive(self, db, tmp_path):
        """浏览器先发 Range 探测、再续传：探测不能把一次性 token 吃掉。"""
        db.save_message(make_message(0, content="x" * 50000), "bk")
        exports = ExportManager(db, tmp_path / "exports", tmp_path / "media", {})
        result = exports.export_latest(GROUP, "zip")
        path = "/backup/download/" + result.token

        probe = handle_download(path, exports, "bytes=0-")
        assert probe.status == 206

        resume = handle_download(path, exports, "bytes=1024-")
        assert resume.status == 206
        assert resume.headers["Content-Range"].endswith(
            f"/{result.path.stat().st_size}"
        )

        # 真正取走整份之后才作废
        assert handle_download(path, exports, None).status == 200
        assert handle_download(path, exports, None).status == 410


def _export_zip(db, tmp_path, backup_id="bk_x"):
    msgs = [make_message(i) for i in range(3)]
    _record(db, backup_id, msgs)
    for m in msgs:
        db.save_message(m, backup_id)
    db.add_sources(backup_id, msgs)
    db.save_group_info(GroupInfo(group_id=GROUP, group_name="真实群名", owner_id=1, member_count=2))
    db.save_members(
        GROUP,
        [
            GroupMember(user_id=1, group_id=GROUP, nickname="老王", role="owner"),
            GroupMember(user_id=2, group_id=GROUP, nickname="小李", role="member"),
        ],
    )
    exports = ExportManager(db, tmp_path / "exports", tmp_path / "media", {})
    return msgs, exports.export(GROUP, "zip", backup_id).path


class TestPackageSnapshot:
    def test_manifest_carries_trusted_snapshot(self, db, tmp_path):
        _, path = _export_zip(db, tmp_path)
        with zipfile.ZipFile(path) as zf:
            manifest = json.loads(zf.read("manifest.json"))
        assert manifest["trusted"]["group_info"]["group_name"] == "真实群名"
        assert len(manifest["trusted"]["members"]) == 2

    def test_repacked_group_name_is_rejected(self, db, tmp_path):
        """群名和成员表被改、清单指纹重算过——恢复端仍要拦住。"""
        _, path = _export_zip(db, tmp_path)
        with zipfile.ZipFile(path) as zf:
            items = {name: zf.read(name) for name in zf.namelist()}
        manifest = json.loads(items["manifest.json"])
        manifest["group_name"] = "伪造群名"
        manifest["group_info"]["group_name"] = "伪造群名"
        manifest["members"] = [
            {"user_id": 9, "group_id": GROUP, "nickname": "攻击者", "card": "", "role": "owner"}
        ]
        manifest.pop("manifest_checksum", None)
        manifest["manifest_checksum"] = compute_manifest_checksum(manifest)
        items["manifest.json"] = json.dumps(manifest, ensure_ascii=False).encode()

        forged = tmp_path / "forged.zip"
        with zipfile.ZipFile(forged, "w") as zf:
            for name, body in items.items():
                zf.writestr(name, body)

        target = Database(tmp_path / "target.sqlite")
        try:
            _record(target, "bk_x", [make_message(i) for i in range(3)])
            with pytest.raises(RestoreError) as err:
                asyncio.run(
                    RestoreEngine(target).restore(
                        "bk_x", GROUP, "skip", archive=str(forged), confirm=True
                    )
                )
            assert "可信快照" in str(err.value)
            assert target.get_group_info(GROUP) is None
        finally:
            target.close()

    def test_honest_package_still_restores(self, db, tmp_path):
        """拦得住假的，也得放得过真的——包括换台机器（本地没有记录）。"""
        _, path = _export_zip(db, tmp_path)
        fresh = Database(tmp_path / "fresh.sqlite")
        try:
            result = asyncio.run(
                RestoreEngine(fresh).restore("bk_x", None, "skip", archive=str(path), confirm=True)
            )
            assert result.inserted == 3
            assert fresh.get_group_info(result.target_group_id).group_name == "真实群名"
        finally:
            fresh.close()

    def test_snapshot_of_normalizes_member_order(self):
        a = snapshot_of(
            GroupInfo(group_id=GROUP, group_name="群"),
            [GroupMember(user_id=2, group_id=GROUP), GroupMember(user_id=1, group_id=GROUP)],
        )
        b = snapshot_of(
            GroupInfo(group_id=GROUP, group_name="群"),
            [GroupMember(user_id=1, group_id=GROUP), GroupMember(user_id=2, group_id=GROUP)],
        )
        assert a == b
