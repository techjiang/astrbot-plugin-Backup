from datetime import datetime, timedelta

import pytest

from backup.core.auto_trigger import AutoTrigger
from backup.core.commands import PermissionChecker, human_size, parse_group_arg, render_diff
from backup.core.media_downloader import MediaDownloader, parse_size, safe_filename
from backup.core.parser import parse_group_members, parse_message
from backup.core.scheduler import BackupScheduler
from backup.core.verifier import compute_checksum

from .conftest import make_message


class TestParser:
    def test_text_and_media(self):
        msg = parse_message(
            {
                "message_id": "1",
                "group_id": 9,
                "sender": {"user_id": 7, "nickname": "A"},
                "timestamp": 1700000000,
                "message": [
                    {"type": "text", "data": {"text": "你好"}},
                    {"type": "image", "data": {"url": "http://x/a.png"}},
                ],
            }
        )
        assert msg.message_type.value == "mixed"
        assert "你好" in msg.content
        assert msg.media_paths == ["http://x/a.png"]

    def test_reply_and_at(self):
        msg = parse_message(
            {
                "message_id": "2",
                "group_id": 9,
                "sender_id": 7,
                "timestamp": 1700000000,
                "message": [
                    {"type": "reply", "data": {"id": "99"}},
                    {"type": "at", "data": {"name": "小明"}},
                    {"type": "text", "data": {"text": "在吗"}},
                ],
            }
        )
        assert msg.reply_to == "99"
        assert "@小明" in msg.content

    def test_forward_chain(self):
        msg = parse_message(
            {
                "message_id": "3",
                "group_id": 9,
                "timestamp": 1700000000,
                "message": [
                    {
                        "type": "forward",
                        "data": {
                            "content": [
                                {"sender": {"nickname": "甲"}, "content": "第一句"},
                                {"sender": {"nickname": "乙"}, "content": "第二句"},
                            ]
                        },
                    }
                ],
            }
        )
        assert msg.is_forward is True
        assert len(msg.forward_chain) == 2

    def test_garbage_does_not_raise(self):
        msg = parse_message({"weird": object()})
        assert msg.message_type.value == "unknown"
        assert msg.message_id  # 有兜底 ID

    def test_members(self):
        members = parse_group_members(
            [{"user_id": 1, "nickname": "甲", "card": "卡片", "role": "owner"}], 100
        )
        assert members[0].display_name == "卡片"


class TestMediaDownloader:
    def test_parse_size(self):
        assert parse_size("100MB") == 100 * 1024**2
        assert parse_size("1.5GB") == int(1.5 * 1024**3)
        assert parse_size(2048) == 2048

    def test_safe_filename_blocks_traversal(self):
        assert safe_filename("../../etc/passwd") == "passwd"
        assert safe_filename("..\\..\\win.ini") == "win.ini"
        assert safe_filename("a/b/c.png") == "c.png"
        assert safe_filename("") == "file"

    def test_target_path_inside_media_dir(self, tmp_path):
        dl = MediaDownloader(tmp_path, {})
        p = dl.target_path("http://x/a.png", "../../evil", datetime(2024, 5, 1))
        assert tmp_path in p.parents

    async def test_dedupe_by_url(self, tmp_path):
        dl = MediaDownloader(tmp_path, {"retry_count": 1})
        first = tmp_path / "d" / "x.png"
        first.parent.mkdir(parents=True)
        first.write_bytes(b"data")
        dl._url_index["http://x/a.png"] = first
        again = await dl.download("http://x/a.png", "m1", datetime.now())
        assert again == str(first)


class TestVerifier:
    def test_checksum_order_independent(self):
        a = [make_message(i) for i in range(5)]
        b = list(reversed(a))
        assert compute_checksum(a) == compute_checksum(b)

    def test_checksum_changes_with_content(self):
        a = [make_message(0)]
        b = [make_message(0, content="别的内容")]
        assert compute_checksum(a) != compute_checksum(b)


class TestCommands:
    def test_parse_group_arg(self):
        assert parse_group_arg("123456") == 123456
        assert parse_group_arg("abc") is None
        assert parse_group_arg(None) is None

    def test_human_size(self):
        assert human_size(0) == "0B"
        assert human_size(2048) == "2.0KB"

    def test_render_diff(self, db):
        # 消息按 (message_id, group_id) 唯一，后写入的备份接管归属，
        # 所以两次备份要比的是「各自现在挂着哪些消息」，不能靠重复插入造数据
        for m in [make_message(i) for i in range(3)]:
            db.save_message(m, "bk1")
        for m in [make_message(i) for i in range(3)]:
            db.save_message(m, "bk2")
        db.save_message(make_message(3), "bk2")
        db.save_message(make_message(4), "bk2")
        from backup.core.models import BackupRecord

        for bid in ("bk1", "bk2"):
            db.save_record(
                BackupRecord(
                    backup_id=bid,
                    group_id=100,
                    status=__import__("backup.core.models", fromlist=["BackupStatus"]).BackupStatus.COMPLETED,
                    started_at=datetime.now(),
                )
            )
        text = render_diff(db, "bk1", "bk2")
        # bk1 的 3 条被 bk2 接管，bk1 现在挂着 0 条；bk2 是 5 条
        assert "- `bk1`：0 条" in text
        assert "仅在 bk1：0 条" in text
        assert "仅在 bk2：5 条" in text

    def test_permission_checker(self):
        class Admin:
            def is_admin(self):
                return True

        class Guest:
            sender_role = "member"

        assert PermissionChecker(True).check_event(Admin()).allowed is True
        assert PermissionChecker(True).check_event(Guest()).allowed is False
        assert PermissionChecker(False).check_event(Guest()).allowed is True


class TestAutoTrigger:
    def test_mass_leave_threshold(self):
        trigger = AutoTrigger({"mass_leave_threshold": 3}, cooldown_seconds=0)
        now = datetime.now()
        for _ in range(3):
            trigger.record_leave(100, now)
        assert trigger.should_trigger_mass_leave(100, now) is True

    def test_cooldown_blocks_repeat(self):
        trigger = AutoTrigger({"mass_leave_threshold": 1}, cooldown_seconds=600)
        now = datetime.now()
        trigger.record_leave(100, now)
        assert trigger.should_trigger_mass_leave(100, now) is True
        assert trigger.should_trigger_mass_leave(100, now) is False

    def test_window_expires(self):
        trigger = AutoTrigger({"mass_leave_threshold": 2}, cooldown_seconds=0)
        now = datetime.now()
        trigger.record_leave(100, now - timedelta(minutes=30))
        trigger.record_leave(100, now)
        assert trigger.should_trigger_mass_leave(100, now) is False


class TestScheduler:
    def test_validate_cron(self, db):
        sched = BackupScheduler(db, lambda gid: None)
        assert sched.validate_cron("0 3 * * *") is True
        assert sched.validate_cron("not a cron") is False

    def test_add_and_remove_persists(self, db):
        async def run(gid):
            return None

        sched = BackupScheduler(db, run)
        assert sched.add_job(100, "0 3 * * *") is True
        assert sched.list_jobs()[0]["group_id"] == 100
        sched.remove_job(100)
        assert sched.list_jobs() == []
        assert sched.add_job(100, "bad cron") is False


class TestConfigAccess:
    CONFIG = {
        "scope": {"download_media": True},
        "media": {"timeout": 30, "max_file_size": "100MB"},
    }

    def test_get_nested(self):
        from backup.core.commands import _config_get

        assert _config_get(self.CONFIG, "scope.download_media") is True
        assert _config_get(self.CONFIG, "media.timeout") == 30

    def test_get_missing_returns_none(self):
        from backup.core.commands import _config_get

        assert _config_get(self.CONFIG, "nope.nope") is None
        assert _config_get(self.CONFIG, "scope.deep.deeper") is None

    def test_set_bool(self):
        from backup.core.commands import _config_get, _config_set

        cfg = {"scope": {"download_media": True}}
        assert _config_set(cfg, "scope.download_media", "false")[0]
        assert _config_get(cfg, "scope.download_media") is False

    def test_set_int(self):
        from backup.core.commands import _config_set

        cfg = {"media": {"timeout": 30}}
        assert _config_set(cfg, "media.timeout", "60")[0]
        assert cfg["media"]["timeout"] == 60

    def test_set_rejects_type_change(self):
        from backup.core.commands import _config_set

        cfg = {"media": {"timeout": 30}}
        ok, reason = _config_set(cfg, "media.timeout", "很久")
        assert ok is False
        assert "int" in reason

    def test_set_rejects_unknown_key(self):
        from backup.core.commands import _config_set

        cfg = {"media": {"timeout": 30}}
        ok, reason = _config_set(cfg, "media.time_out", "60")
        assert ok is False
        assert "不存在" in reason

    def test_coerce_keeps_string_when_not_numeric(self):
        from backup.core.commands import _config_set

        cfg = {"export": {"default_format": "sqlite"}}
        assert _config_set(cfg, "export.default_format", "html")[0]
        assert cfg["export"]["default_format"] == "html"
