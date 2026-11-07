"""第 12 轮：上一轮那道「包被改过」的拦截，本身是可以绕过的。

1. 比对基准读的是包里的 `manifest['trusted']`。它和被比对的内容在同一个 ZIP
   里，改完用同一个算法把 `manifest_checksum` 重算一遍，包就自洽了——实测把
   「可信快照」的成员表换成一个人，同机恢复照样通过；
2. 换台机器（本地没有记录）时连这道比对都不跑，改过的成员表直接写进目标群；
3. 包在群名/成员表上「单方面添加」内容也算可疑，不能只看逐项相等。

修法：导出时把快照和它的指纹落进本机库（`export_evidence` 表），恢复时只认
本机这份。包作者能改包，改不了导出当时写进库里的记录。
"""

import asyncio
import json
import zipfile
from datetime import datetime

import pytest

from backup.core.database import Database
from backup.core.export_manager import ExportManager
from backup.core.models import (
    BackupRecord,
    BackupStatus,
    GroupInfo,
    GroupMember,
    Message,
    MessageType,
)
from backup.core.restore_engine import RestoreEngine, RestoreError
from backup.core.verifier import compute_checksum, compute_manifest_checksum, snapshot_evidence

GROUP = 100
OTHER_GROUP = 200
AT_TIME = datetime(2026, 1, 1, 12, 0, 0)


def _seed(db, backup_id="bk_x", count=3):
    msgs = [
        Message(
            message_id=f"m{i}",
            group_id=GROUP,
            sender_id=1,
            sender_name="老王",
            timestamp=AT_TIME,
            message_type=MessageType.TEXT,
            content=f"hello{i}",
            raw_content="",
        )
        for i in range(count)
    ]
    db.save_record(
        BackupRecord(
            backup_id=backup_id,
            group_id=GROUP,
            status=BackupStatus.COMPLETED,
            started_at=AT_TIME,
            finished_at=AT_TIME,
            message_count=count,
            checksum=compute_checksum(msgs),
        )
    )
    for m in msgs:
        db.save_message(m, backup_id)
    db.add_sources(backup_id, msgs)
    db.save_group_info(GroupInfo(group_id=GROUP, group_name="真实群名", owner_id=1))
    db.save_members(
        GROUP,
        [
            GroupMember(user_id=1, group_id=GROUP, nickname="老王", role="owner"),
            GroupMember(user_id=2, group_id=GROUP, nickname="小李", role="member"),
        ],
    )
    return msgs


def _export(db, tmp_path, backup_id="bk_x"):
    return ExportManager(db, tmp_path / "exports", tmp_path / "media", {}).export(
        GROUP, "zip", backup_id
    ).path


def _repack(path, mutate):
    """改包内 manifest，并把指纹重算一遍——模拟「有备而来」的篡改。"""
    with zipfile.ZipFile(path) as zf:
        items = {name: zf.read(name) for name in zf.namelist()}
    manifest = json.loads(items["manifest.json"])
    mutate(manifest)
    manifest["manifest_checksum"] = compute_manifest_checksum(manifest)
    items["manifest.json"] = json.dumps(manifest, ensure_ascii=False).encode()
    forged = path.with_name("forged.zip")
    with zipfile.ZipFile(forged, "w") as zf:
        for name, body in items.items():
            zf.writestr(name, body)
    return forged


FORGED_MEMBER = [
    {
        "user_id": 999,
        "group_id": GROUP,
        "nickname": "攻击者",
        "card": "",
        "role": "owner",
        "join_time": None,
        "last_active": None,
    }
]


class TestSnapshotEvidence:
    def test_export_records_evidence_locally(self, db, tmp_path):
        """导出就把证据落在本机库里，恢复时才有得比。"""
        _seed(db)
        _export(db, tmp_path)
        evidence = db.get_export_evidence("bk_x", GROUP)
        assert evidence is not None
        assert evidence["snapshot"]["group_info"]["group_name"] == "真实群名"
        assert evidence["evidence"] == snapshot_evidence(evidence["snapshot"])

    def test_forged_trusted_snapshot_is_rejected(self, db, tmp_path):
        """包里连「可信快照」一起换掉、指纹重算过——仍然拦住。

        上一轮的实现读的是包里的 trusted，改完重算就自洽了，这条路实测能通过。
        """
        _seed(db)
        path = _export(db, tmp_path)

        def mutate(manifest):
            manifest["trusted"] = {
                "group_id": GROUP,
                "group_info": {"group_name": "真实群名", "owner_id": 1, "member_count": 1},
                "members": FORGED_MEMBER,
            }
            manifest["members"] = FORGED_MEMBER

        forged = _repack(path, mutate)
        with pytest.raises(RestoreError) as err:
            asyncio.run(
                RestoreEngine(db).restore(
                    "bk_x", OTHER_GROUP, "skip", archive=str(forged), confirm=True
                )
            )
        assert "不一致" in str(err.value)
        assert db.get_members(OTHER_GROUP) == []

    def test_fresh_machine_still_restores_honest_package(self, db, tmp_path):
        """换台机器没有证据可查，但包是诚实的——必须放行，否则迁移就走不通。"""
        _seed(db)
        path = _export(db, tmp_path)
        fresh = Database(tmp_path / "fresh.sqlite")
        try:
            result = asyncio.run(
                RestoreEngine(fresh).restore(
                    "bk_x", OTHER_GROUP, "skip", archive=str(path), confirm=True
                )
            )
            assert result.inserted == 3
            assert fresh.get_group_info(OTHER_GROUP).group_name == "真实群名"
            assert len(fresh.get_members(OTHER_GROUP)) == 2
        finally:
            fresh.close()

    def test_forged_evidence_row_is_rejected(self, db, tmp_path):
        """证据行自己被动过（快照和指纹对不上）→ 不能拿它当基准。"""
        _seed(db)
        path = _export(db, tmp_path)
        db.save_export_evidence(
            "bk_x", GROUP, {"group_info": {"group_name": "真实群名"}, "members": []}, "bad"
        )
        with pytest.raises(RestoreError) as err:
            asyncio.run(
                RestoreEngine(db).restore(
                    "bk_x", OTHER_GROUP, "skip", archive=str(path), confirm=True
                )
            )
        assert "自相矛盾" in str(err.value)

    def test_adding_members_to_package_is_rejected(self, db, tmp_path):
        """单方面往包里塞成员：逐项比「包里有的都得对上」是拦不住的。"""
        _seed(db)
        path = _export(db, tmp_path)

        def mutate(manifest):
            manifest["members"] = [
                {"user_id": 1, "group_id": GROUP, "nickname": "老王", "card": "", "role": "owner"},
                {"user_id": 2, "group_id": GROUP, "nickname": "小李", "card": "", "role": "member"},
                {"user_id": 999, "group_id": GROUP, "nickname": "攻击者", "card": "", "role": "owner"},
            ]

        forged = _repack(path, mutate)
        with pytest.raises(RestoreError) as err:
            asyncio.run(
                RestoreEngine(db).restore(
                    "bk_x", OTHER_GROUP, "skip", archive=str(forged), confirm=True
                )
            )
        assert "成员列表" in str(err.value)

    def test_group_name_removed_from_package_is_rejected(self, db, tmp_path):
        """把真群名删空、塞一个自造的，包也「自洽」——同样拦住。"""
        _seed(db)
        path = _export(db, tmp_path)

        def mutate(manifest):
            manifest["group_name"] = "伪造群名"
            manifest["group_info"] = {
                "group_id": GROUP,
                "group_name": "伪造群名",
                "owner_id": 1,
                "member_count": 2,
            }

        forged = _repack(path, mutate)
        with pytest.raises(RestoreError) as err:
            asyncio.run(
                RestoreEngine(db).restore(
                    "bk_x", OTHER_GROUP, "skip", archive=str(forged), confirm=True
                )
            )
        assert "群名" in str(err.value) or "group_name" in str(err.value)

    def test_untouched_package_still_restores(self, db, tmp_path):
        """拦得住假的，也得放得过真的。"""
        _seed(db)
        path = _export(db, tmp_path)
        result = asyncio.run(
            RestoreEngine(db).restore(
                "bk_x", OTHER_GROUP, "skip", archive=str(path), confirm=True
            )
        )
        assert result.inserted == 3
