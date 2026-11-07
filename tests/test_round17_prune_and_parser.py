"""第 17 轮：媒体淘汰误删别群的文件、群号兜底、快照条数撒谎、清单不随记录走。

每个场景都先写成脚本复现，改完同一个脚本重跑看着它变正确，再固化在这里。
"""

from datetime import datetime, timedelta

from backup.core.database import Database
from backup.core.models import Message, MessageType
from backup.core.parser import (
    parse_announcements,
    parse_group_files,
    parse_group_info,
    parse_group_members,
)

from .conftest import BASE_TIME

GROUP = 100


def _msg(mid: str, group_id: int, when: datetime, content: str = "hi") -> Message:
    return Message(
        message_id=mid,
        group_id=group_id,
        sender_id=1,
        sender_name="甲",
        timestamp=when,
        message_type=MessageType.TEXT,
        content=content,
        raw_content="{}",
    )


def _register_media(db: Database, mid: str, group_id: int, path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * 16)
    db.save_media(
        message_id=mid,
        group_id=group_id,
        original_url=f"https://example.com/{mid}",
        local_path=str(path),
        filename=path.name,
        file_size=16,
        mime_type="application/octet-stream",
        sha256="0" * 64,
    )


def test_prune_media_keeps_file_still_used_by_another_group(tmp_path):
    """同一个 message_id 在别的群是新消息时，媒体文件不能被当过期清掉。

    媒体按 `{message_id}_{urlhash}.{ext}` 落盘，两个群共用同一个 message_id 时
    落的是同一个文件。只看「旧消息」就删，会把还在用的那份删掉，那个群从此
    导不出图，`media_files` 里那行还指着不存在的路径。
    """
    db = Database(tmp_path / "x.sqlite")
    media_file = tmp_path / "media" / "m1_abcd1234.jpg"

    db.save_message(_msg("m1", 100, BASE_TIME - timedelta(days=30)), "bk1")
    db.save_message(_msg("m1", 200, datetime.now()), "bk2")
    _register_media(db, "m1", 100, media_file)

    db.prune_media(1)

    assert media_file.exists(), "别群还在用的媒体文件被删了"
    assert db.query_one("SELECT COUNT(*) AS c FROM media_files")["c"] == 1


def test_prune_media_removes_expired_file(tmp_path):
    """没人再用的过期媒体照删——上一条不是把淘汰整个关掉。"""
    db = Database(tmp_path / "x.sqlite")
    media_file = tmp_path / "media" / "m9_abcd1234.jpg"

    db.save_message(_msg("m9", 100, BASE_TIME - timedelta(days=30)), "bk1")
    _register_media(db, "m9", 100, media_file)

    db.prune_media(1)

    assert not media_file.exists()
    assert db.query_one("SELECT COUNT(*) AS c FROM media_files")["c"] == 0


def test_parser_group_id_zero_falls_back_to_group():
    """显式写出 `group_id: 0` 也要落到真实群号上。

    原来的 `or group_id` 把 0 原样留下，于是群公告/群文件/成员全写进群 0：
    备份看着成功，导出包里这几份清单是空的，目标群查不到。
    """
    assert parse_group_info({"group_id": 0}, GROUP).group_id == GROUP
    assert [m.group_id for m in parse_group_members([{"user_id": 5}], GROUP)] == [GROUP]
    assert parse_group_files([{"file_id": "f1", "name": "a.txt"}], GROUP)[0]["group_id"] == GROUP
    assert parse_announcements([{"content": "公告"}], GROUP)[0]["group_id"] == GROUP


def test_save_group_files_reports_rows_actually_stored(tmp_path):
    """重复 file_id 只留一条，返回值也得是一，不是输入条数。"""
    db = Database(tmp_path / "x.sqlite")

    saved = db.save_group_files(
        GROUP, [{"file_id": "f1", "name": "a.txt"}, {"file_id": "f1", "name": "b.txt"}]
    )

    assert saved == len(db.get_group_files(GROUP)) == 1


def test_delete_record_also_drops_its_sources(tmp_path):
    """淘汰记录时来源清单跟着走，别留一份指向不存在记录的登记。"""
    db = Database(tmp_path / "x.sqlite")
    msg = _msg("m1", GROUP, BASE_TIME)
    db.save_message(msg, "bk1")
    db.add_sources("bk1", [msg])
    db.save_message(_msg("m2", GROUP, BASE_TIME), "bk2")
    db.add_sources("bk2", [_msg("m2", GROUP, BASE_TIME)])

    db.delete_record("bk1")

    assert db.count_sources("bk1") == 0
    assert db.count_sources("bk2") == 1
