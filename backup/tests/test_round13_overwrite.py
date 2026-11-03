"""第 13 轮：`--strategy overwrite` 只换了内容，没换来源登记。"""

import pytest

from backup.core.models import BackupRecord, BackupStatus
from backup.core.restore_engine import RestoreEngine
from backup.core.verifier import compute_checksum
from datetime import datetime


def _record(db, backup_id="bk_new", group_id=100, checksum=""):
    db.save_record(
        BackupRecord(
            backup_id=backup_id,
            group_id=group_id,
            status=BackupStatus.COMPLETED,
            started_at=datetime.now(),
            message_count=3,
            checksum=checksum,
        )
    )


async def test_overwrite_also_takes_over_source_registry(db):
    """覆盖写入后，老备份不能再说「这条我覆盖过」——否则按来源导出拿到的还是旧内容。"""
    from backup.tests.conftest import make_message

    msgs = [make_message(i) for i in range(3)]
    for m in msgs:
        db.save_message(m, "bk_new")
    db.add_sources("bk_new", msgs)

    # 同一条消息被另一份备份接管，内容也换成了旧的
    stale = make_message(1)
    stale.content = "旧内容"
    db.save_message(stale, "bk_old")
    db.add_sources("bk_old", [stale])
    _record(db, "bk_new", checksum=compute_checksum(msgs))

    await RestoreEngine(db).restore("bk_new", 100, strategy="overwrite", confirm=True)

    row = db.query_one(
        "SELECT content, backup_id FROM messages WHERE group_id = 100 AND message_id = 'm1'"
    )
    assert row["content"] == "消息内容 1"
    assert row["backup_id"] == "bk_new"
    sources = {
        r["backup_id"]
        for r in db.query(
            "SELECT backup_id FROM backup_sources WHERE group_id = 100 AND message_id = 'm1'"
        )
    }
    assert sources == {"bk_new"}
    # 按来源导出取到的必须也是新内容
    assert [m.content for m in db.get_source_messages("bk_new", 100)][1] == "消息内容 1"


async def test_skip_strategy_leaves_source_registry_alone(db):
    """skip 什么都没写，来源登记不该被动过。"""
    from backup.tests.conftest import make_message

    msg = make_message(1)
    db.save_message(msg, "bk_new")
    db.add_sources("bk_new", [msg])
    db.add_sources("bk_old", [msg])
    _record(db, "bk_new", checksum=compute_checksum([msg]))

    await RestoreEngine(db).restore("bk_new", 100, strategy="skip", confirm=True)
    # skip 不会替老备份撤掉登记：它本来就没覆盖过内容
    assert {r["backup_id"] for r in db.query("SELECT backup_id FROM backup_sources")} == {
        "bk_new",
        "bk_old",
    }
