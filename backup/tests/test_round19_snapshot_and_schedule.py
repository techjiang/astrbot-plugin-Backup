"""第 19 轮：公告/文件快照没跟着备份走、适配器签名不匹配被静默吞掉、
永远不触发的 cron 能装上、恢复回执里的条数在撒谎。

每个场景都先写成脚本复现，改完同一个脚本重跑看着它变正确，再固化在这里。
"""

import zipfile

import pytest

from backup.core.backup_engine import BackupEngine, PlatformAdapter
from backup.core.export_manager import ExportManager
from backup.core.parser import parse_announcements, parse_group_files
from backup.core.restore_engine import RestoreEngine
from backup.core.scheduler import BackupScheduler, validate_cron

from .conftest import BASE_TIME, FakePlatform, raw_history

GROUP = 100


class _NoticePlatform(FakePlatform):
    """同时提供公告与文件列表的假平台。"""

    async def get_group_notice(self, group_id):
        return [{"content": "重要公告", "sender": {"user_id": 1, "nickname": "管理员"}}]

    async def get_group_file_list(self, group_id):
        return [{"file_id": "f1", "file_name": "资料.zip", "file_size": 123}]


def _engine(tmp_path, db=None):
    from backup.core.database import Database

    database = db or Database(tmp_path / "db.sqlite")
    return database, BackupEngine(database, tmp_path / "media", {"media": {}})


@pytest.mark.asyncio
async def test_announcements_ride_along_with_the_backup(tmp_path):
    """公告 / 文件清单必须是「这份备份当时的样子」。

    导出包原来读的是当前状态表：导一份上个月的备份，带出去的却是这个群今天的
    公告——那不是备份，是现状。换台机器恢复后公告也全空白。
    """
    db, engine = _engine(tmp_path)
    record = await engine.backup_group(
        GROUP, _NoticePlatform(history=raw_history(2)), download_media=False
    )
    assert record.error is None, record.error
    assert len(db.get_announcements(GROUP)) == 1

    # 群里后来换了公告；老备份导出的包仍应是当时那一条
    db.save_announcements(GROUP, [{"group_id": GROUP, "content": "新公告"}])
    exports = ExportManager(db, tmp_path / "exports", tmp_path / "media", {"export": {}})
    result = exports.export(GROUP, "zip", record.backup_id)
    with zipfile.ZipFile(result.path) as zf:
        import json

        manifest = json.loads(zf.read("manifest.json"))
    contents = [a.get("content") for a in manifest.get("announcements") or []]
    assert contents == ["重要公告"]
    assert len(manifest.get("group_files") or []) == 1
    db.close()


@pytest.mark.asyncio
async def test_restore_writes_announcements_and_files(tmp_path):
    """包里带了公告和文件清单，恢复完目标群里得有它们。"""
    db, engine = _engine(tmp_path)
    record = await engine.backup_group(
        GROUP, _NoticePlatform(history=raw_history(2)), download_media=False
    )
    exports = ExportManager(db, tmp_path / "exports", tmp_path / "media", {"export": {}})
    archive = exports.export(GROUP, "zip", record.backup_id).path

    target = db.__class__(tmp_path / "other.sqlite")
    await RestoreEngine(target).restore(
        record.backup_id, 777, confirm=True, archive=archive
    )
    assert [a["content"] for a in target.get_announcements(777)] == ["重要公告"]
    assert [f["name"] for f in target.get_group_files(777)] == ["资料.zip"]
    target.close()
    db.close()


def test_repository_snapshot_timestamps_do_not_crash(tmp_path):
    """快照里的时间是 datetime，当前状态表里读出来是字符串，两条路径都得吃得下。

    直接往 sqlite3 递 datetime 会抛 InterfaceError——整次备份在「存公告」这一步
    崩掉，而消息明明已经全部落库（备份却报 failed）。
    """
    from backup.core.database import Database

    db = Database(tmp_path / "db.sqlite")
    items = parse_announcements([{"content": "x", "publish_time": 1714000000}], GROUP)
    assert db.save_announcements(GROUP, items) == 1
    # 导出包里读回来的形态：时间已经是个 ISO 串
    db.save_announcements(GROUP, [{"group_id": GROUP, "content": "y", "published_at": "2024-05-01T10:00:00"}])
    assert len(db.get_announcements(GROUP)) == 1
    files = parse_group_files([{"file_id": "f1", "file_name": "a.zip"}], GROUP)
    assert db.save_group_files(GROUP, files) == 1
    db.close()


@pytest.mark.asyncio
async def test_adapter_skips_methods_with_wrong_signature():
    """同名但签名不对的方法要被跳过，不能一路抛到最后被当「拉取失败」吞掉。

    `get_group_notice(group)` 这种写法在适配器上很常见；原来取到第一个可调用
    对象就调，TypeError 直接冒到备份流程，功能看着在、其实一次都没成功过。
    """

    class Platform:
        async def get_group_notice(self, group):  # 参数名不叫 group_id
            return [{"content": "x"}]

        async def get_group_notices(self, group_id):
            return [{"content": "y"}]

    adapter = PlatformAdapter(Platform())
    assert await adapter.get_group_announcements(1) == [{"content": "y"}]


def test_cron_that_never_fires_is_rejected():
    """`0 0 31 2 *` 每段都合法，但它一辈子不会触发。

    以前校验只比数值范围，这种表达式一路通过：`schedule set` 回「✅ 已设置」，
    任务挂在库里永远不跑。
    """
    assert not validate_cron("0 0 31 2 *")
    assert not validate_cron("0 0 30 2 *")
    assert validate_cron("0 0 29 2 *")  # 闰年还有
    assert validate_cron("0 3 * * *")
    assert validate_cron("0 0 * * 7")  # 周日写 7 是合法写法
    assert not validate_cron("0 0 * * 8")


def test_sunday_as_seven_is_normalized_for_apscheduler(tmp_path):
    """APScheduler 的 from_crontab 只认 0-6，7 得改写成 0 才不会在挂任务时炸。"""
    from backup.core.database import Database

    db = Database(tmp_path / "db.sqlite")
    scheduler = BackupScheduler(db, lambda gid: None, enabled=False)
    assert scheduler.add_job(GROUP, "0 0 * * 7") is True
    assert db.list_schedules()[0]["cron"] == "0 0 * * 7"
    db.close()


@pytest.mark.asyncio
async def test_export_records_how_many_messages_the_package_holds(tmp_path):
    """导出包带的是「备份 + 只在实时归档里的消息」，条数天然不等于 message_count。

    记录里不写「包里几条」，恢复端只能拿 message_count 说「包里 N 条」——
    用户数得出三倍，只会以为包坏了。
    """
    db, engine = _engine(tmp_path)
    record = await engine.backup_group(
        GROUP, FakePlatform(history=raw_history(3)), download_media=False
    )
    exports = ExportManager(db, tmp_path / "exports", tmp_path / "media", {"export": {}})
    result = exports.export_latest(GROUP, "zip")
    assert db.get_record(record.backup_id).package_count == result.message_count
    db.close()


@pytest.mark.asyncio
async def test_restore_receipt_reports_total(tmp_path):
    """恢复回执要说清「一共多少条」，只说写入/跳过会让人以为包是坏的。"""
    db, engine = _engine(tmp_path)
    record = await engine.backup_group(
        GROUP, FakePlatform(history=raw_history(3)), download_media=False
    )
    result = await RestoreEngine(db).restore(record.backup_id, 888, confirm=True)
    assert result.total == 3
    assert result.inserted == 3
    db.close()
