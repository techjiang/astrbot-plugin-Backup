"""第 14 轮：库里消息被清理过之后，恢复被误拒。

`/backup clean` 只清记录不动消息，但媒体淘汰、手工删行、迁移只搬了一截，
都会让「这条记录名下的消息」少几条。原来的校验一律判「校验和不一致 → 备份
可能已损坏」，于是备份被打扫过一次就再也恢复不了。
"""

import pytest

from backup.core.models import BackupRecord, BackupStatus
from backup.core.restore_engine import RestoreEngine, RestoreError
from backup.core.verifier import compute_checksum

from .conftest import BASE_TIME, make_message


def _seed(db, n=5, backup_id="bk1", group_id=100):
    msgs = [make_message(i, group_id=group_id) for i in range(n)]
    for m in msgs:
        db.save_message(m, backup_id)
    db.add_sources(backup_id, msgs)
    db.save_record(
        BackupRecord(
            backup_id=backup_id,
            group_id=group_id,
            status=BackupStatus.COMPLETED,
            started_at=BASE_TIME,
            message_count=n,
            checksum=compute_checksum(msgs),
        )
    )
    return msgs


async def test_restore_after_partial_prune_recovers_survivors(db):
    """库里少了几条，剩下的照常恢复，不整份拒绝。"""
    _seed(db)
    db.delete_messages(100, ["m4"])

    result = await RestoreEngine(db).restore("bk1", 100, confirm=True)

    assert result.skipped == 4
    assert [m.message_id for m in db.get_messages(group_id=100)] == [
        "m0",
        "m1",
        "m2",
        "m3",
    ]


async def test_source_registry_outlives_pruned_rows(db):
    """来源清单是按「原始行」查的，删掉的消息不会从清单里消失。

    `get_source_messages` 要 JOIN messages，被删的行两边同时消失、差集恒为空；
    判断「原本有几条、后来少了几条」必须直接问清单表。
    """
    _seed(db)
    db.delete_messages(100, ["m4"])

    assert len(db.get_source_messages("bk1", 100)) == 4
    assert db.source_message_ids("bk1", 100) == {"m0", "m1", "m2", "m3", "m4"}


async def test_restore_after_full_prune_says_use_the_package(db):
    """一条都没剩下时，得说清「去用导出包」。"""
    _seed(db)
    db.delete_messages(100, ["m0", "m1", "m2", "m3", "m4"])

    with pytest.raises(RestoreError) as err:
        await RestoreEngine(db).restore("bk1", 100, confirm=True)

    assert "包" in str(err.value)


async def test_untouched_library_still_rejects_checksum_mismatch(db):
    """库里一条没少、校验和却对不上，仍然拒绝——那是库真被改过。"""
    _seed(db)
    db.execute(
        "UPDATE messages SET content = '被改过的内容' WHERE group_id = 100 AND message_id = 'm2'"
    )

    with pytest.raises(RestoreError, match="校验和"):
        await RestoreEngine(db).restore("bk1", 100, confirm=True)
