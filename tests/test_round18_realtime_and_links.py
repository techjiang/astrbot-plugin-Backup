"""第 18 轮：实时归档洗掉已有媒体、过期 token 清不掉、备份重复回复、
`count_realtime_only` 恒为 0、下载路由只认完整前缀、记录删了证据还在。

每个场景都先写成脚本复现，改完同一个脚本重跑看着它变正确，再固化在这里。
"""

import time

from backup.core.auto_trigger import AutoTrigger
from backup.core.database import Database
from backup.core.download_route import PREFIX, handle_download
from backup.core.event_hooks import EventHooks
from backup.core.export_manager import ExportManager
from backup.core.models import Message, MessageType

from .conftest import BASE_TIME

GROUP = 100


def _msg(mid: str = "m1", name: str = "甲", media=None) -> Message:
    msg = Message(
        message_id=mid,
        group_id=GROUP,
        sender_id=1,
        sender_name=name,
        timestamp=BASE_TIME,
        message_type=MessageType.TEXT,
        content="看图",
        raw_content="{}",
    )
    msg.media_paths = list(media or [])
    return msg


def test_realtime_archive_keeps_media_saved_by_full_backup(tmp_path):
    """实时归档不下载媒体，不该把上一次完整备份存下的路径洗成空。

    归档与完整备份都会写同一条消息；按「后写覆盖」处理时，实时归档那份空的
    media_paths 会把图片路径抹掉。从此那条消息导出不带媒体，`media_files` 里
    还留着文件，但没人再引用它。
    """
    db = Database(tmp_path / "x.sqlite")
    db.save_message(_msg(media=["/media/m1_a.png", "/media/m1_b.png"]), "bk1")

    db.save_message(_msg(media=["/media/m1_b.png"]), "realtime")
    row = db.query_one("SELECT media_paths FROM messages WHERE message_id = 'm1'")
    assert sorted(__import__("json").loads(row["media_paths"])) == [
        "/media/m1_a.png",
        "/media/m1_b.png",
    ]

    db.save_message(_msg(), "realtime")
    row = db.query_one("SELECT media_paths FROM messages WHERE message_id = 'm1'")
    assert len(__import__("json").loads(row["media_paths"])) == 2


def test_realtime_archive_keeps_sender_name(tmp_path):
    """解析不出昵称时不能用空串盖掉已知的名字。"""
    db = Database(tmp_path / "x.sqlite")
    db.save_message(_msg(name="甲"), "bk1")
    db.save_message(_msg(name=""), "realtime")
    row = db.query_one("SELECT sender_name FROM messages WHERE message_id = 'm1'")
    assert row["sender_name"] == "甲"


def test_clean_exports_purges_expired_tokens(tmp_path):
    """过期 token 必须真的被清掉。

    `clean_exports` 原来把「导出文件的 mtime 截止刻」当成了「当前时间」，比真实
    时间小一个保留期，于是 `expires < cutoff` 恒不成立，`download_tokens` 只增
    不减，没人清。
    """
    db = Database(tmp_path / "x.sqlite")
    exports = ExportManager(
        db, tmp_path / "exports", tmp_path / "media", {"export": {"download_link_expire": "1s"}}
    )
    exports.export_dir.mkdir(parents=True, exist_ok=True)
    target = exports.export_dir / "a.zip"
    target.write_bytes(b"x" * 16)

    token, _ = exports._issue_token(target)
    assert db.get_token(token) is not None

    # 只推进「现在」，别真睡：`purge_expired_tokens` 收的是绝对时刻
    assert db.purge_expired_tokens(time.time() + 3600) == 1
    assert db.get_token(token) is None
    # 文件本身在保留期内，不能被顺手删掉
    assert target.exists()


def test_delete_record_drops_export_evidence(tmp_path):
    """记录删了，它的导出证据也得跟着走。

    证据是「包里的群信息对不对」的判据。留着它，下次导出复用到同一个 ID 时，
    恢复端会拿上一世代的证据比对，正常包被判成「被改过」。
    """
    db = Database(tmp_path / "x.sqlite")
    db.save_record(_record("bk1"))
    db.save_export_evidence("bk1", GROUP, {"group_info": {}, "members": []}, "abc")
    db.delete_record("bk1")
    assert db.get_export_evidence("bk1", GROUP) is None


def test_count_realtime_only_counts_archived_messages(tmp_path):
    """实时归档自己也是一份备份，它的消息要算「有主」。

    判据原来是「清单里没有它」——实时归档自己刚登记完清单，条件立刻不成立，
    这个数恒为 0，`/backup status` 永远不说「其中实时归档 N 条」。
    """
    db = Database(tmp_path / "x.sqlite")
    hooks = EventHooks(db, AutoTrigger({}), archive=True)
    event = {
        "group_id": GROUP,
        "message_obj": {
            "message_id": "e1",
            "group_id": GROUP,
            "timestamp": int(BASE_TIME.timestamp() * 1000),
            "sender": {"user_id": 7, "nickname": "阿七"},
            "message": [{"type": "plain", "data": {"text": "你好"}}],
        },
    }
    hooks.archive_message(event)
    assert db.count_realtime_only(GROUP) == 1

    # 全量备份接手之后就不再是「只有实时归档」的那批了
    db.save_record(_record("bk1"))
    db.add_sources("bk1", db.get_messages(group_id=GROUP))
    assert db.count_realtime_only(GROUP) == 0


def test_download_route_accepts_token_without_prefix(tmp_path):
    """宿主注册路由时通常已经吃掉前缀，传进来的只是 `/download/<token>`。

    只认 `/backup/download/` 的话，这种接法一律 404，用户看到的是「下不了」。
    """
    db = Database(tmp_path / "x.sqlite")
    exports = ExportManager(db, tmp_path / "exports", tmp_path / "media", {})
    exports.export_dir.mkdir(parents=True, exist_ok=True)
    target = exports.export_dir / "b.zip"
    target.write_bytes(b"y" * 32)

    token, _ = exports._issue_token(target)
    assert handle_download(f"/download/{token}", exports).status == 200

    token2, _ = exports._issue_token(target)
    assert handle_download(PREFIX + token2, exports).status == 200

    # 别的路径仍然 404：不能把无关请求也当成一条「失效的链接」
    assert handle_download("/other/thing", exports).status == 404


def _record(backup_id: str):
    from backup.core.models import BackupRecord, BackupStatus

    return BackupRecord(
        backup_id=backup_id,
        group_id=GROUP,
        status=BackupStatus.COMPLETED,
        started_at=BASE_TIME,
        finished_at=BASE_TIME,
        message_count=0,
        media_count=0,
        total_size=0,
        is_incremental=False,
        base_backup_id=None,
        checksum="",
        error=None,
    )
