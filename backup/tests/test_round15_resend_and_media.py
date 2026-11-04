"""第 15 轮：三处「平时看不出来、真出事时才知道」的问题。

- 同一个备份包恢复第二次被自己的篡改拦截拒掉（换机器恢复后重试的常见动作）；
- 媒体去重只在内存里，重启后同一张图再下一遍；
- 认不出的消息段解析出来是空内容——备份插件最不该做的就是存下一条空消息。
"""

import asyncio
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from backup.core.backup_engine import BackupEngine
from backup.core.export_manager import ExportManager
from backup.core.models import (
    BackupRecord,
    BackupStatus,
    GroupInfo,
    Message,
    MessageType,
)
from backup.core.parser import parse_message
from backup.core.restore_engine import RestoreEngine
from backup.core.verifier import compute_checksum

from .conftest import BASE_TIME, make_message


def _seed(db, n=5, backup_id="bk1", group_id=100):
    msgs = [make_message(i, group_id=group_id) for i in range(n)]
    for m in msgs:
        db.save_message(m, backup_id)
    db.add_sources(backup_id, msgs)
    db.save_group_info(GroupInfo(group_id=group_id, group_name="老群", member_count=1))
    db.save_record(
        BackupRecord(
            backup_id=backup_id,
            group_id=group_id,
            status=BackupStatus.COMPLETED,
            started_at=BASE_TIME,
            finished_at=BASE_TIME,
            message_count=n,
            checksum=compute_checksum(msgs),
        )
    )
    return msgs


class TestRestoreTwice:
    async def test_same_package_restores_twice(self, db, tmp_path):
        """同一个包恢复两次，第二次应该「跳过 N 条」，不是「含不属于该备份的消息」。

        第一次恢复会把消息登记成本次恢复的来源。第二次的消息 ID 在库里认得、
        归属却是记录里的原群号——只按归属判夹带，第二个一模一样的命令就被拒了。
        """
        _seed(db)
        archive = ExportManager(db, tmp_path / "exports", tmp_path / "media", {}).export(
            100, "zip", "bk1"
        ).path

        fresh = type(db)(tmp_path / "other.sqlite")
        engine = RestoreEngine(fresh)
        first = await engine.restore("bk1", 999, archive=archive, confirm=True)
        second = await engine.restore("bk1", 999, archive=archive, confirm=True)

        assert first.inserted == 5
        assert second.skipped == 5
        assert len(fresh.get_messages(group_id=999)) == 5
        fresh.close()

    async def test_rebuilt_record_gets_sources_on_source_group(self, db, tmp_path):
        """包恢复重建的记录，来源清单要落在源群上——按目标群写等于宣布「这份
        备份属于目标群」，之后按源群取数一条都取不到。"""
        _seed(db)
        archive = ExportManager(db, tmp_path / "exports", tmp_path / "media", {}).export(
            100, "zip", "bk1"
        ).path

        fresh = type(db)(tmp_path / "other.sqlite")
        await RestoreEngine(fresh).restore("bk1", 999, archive=archive, confirm=True)

        assert fresh.count_sources("bk1") == 5
        assert fresh.source_group_ids("bk1") == [100]
        assert fresh.get_record("bk1").group_id == 100
        fresh.close()

    async def test_foreign_message_in_package_still_rejected(self, db, tmp_path):
        """放宽「按来源清单认账」之后，真正的夹带仍要拦下。"""
        import json
        import zipfile

        from backup.core.verifier import compute_manifest_checksum

        _seed(db)
        archive = ExportManager(db, tmp_path / "exports", tmp_path / "media", {}).export(
            100, "zip", "bk1"
        ).path

        forged = tmp_path / "forged.zip"
        with zipfile.ZipFile(archive) as zf:
            data = {n: zf.read(n) for n in zf.namelist()}
        names = [n for n in data if n.startswith("json/daily/")]
        items = json.loads(data[names[0]].decode())
        items.append(
            {
                "message_id": "evil",
                "group_id": 100,
                "sender_id": 9,
                "sender_name": "攻击者",
                "timestamp": "2024-05-01 10:00:00",
                "message_type": "text",
                "content": "伪造",
            }
        )
        data[names[0]] = json.dumps(items, ensure_ascii=False).encode()
        manifest = json.loads(data["manifest.json"].decode())
        manifest["manifest_checksum"] = compute_manifest_checksum(manifest)
        data["manifest.json"] = json.dumps(manifest, ensure_ascii=False).encode()
        with zipfile.ZipFile(forged, "w") as zf:
            for name, blob in data.items():
                zf.writestr(name, blob)

        fresh = type(db)(tmp_path / "other.sqlite")
        engine = RestoreEngine(fresh)
        await engine.restore("bk1", 999, archive=archive, confirm=True)
        with pytest.raises(Exception):
            await engine.restore("bk1", 999, archive=forged, confirm=True)
        fresh.close()


class _MediaPlatform:
    def __init__(self, url):
        self.url = url

    async def get_group_info(self, group_id=None, **kw):
        return {"group_id": group_id, "group_name": "测试群"}

    async def get_group_members(self, group_id=None, **kw):
        return [{"user_id": 1, "nickname": "张三"}]

    async def get_group_history(self, group_id=None, count=50, offset=0, **kw):
        if offset:
            return []
        return [
            {
                "message_id": "m0",
                "group_id": group_id,
                "sender": {"user_id": 1, "nickname": "张三"},
                "timestamp": BASE_TIME.timestamp(),
                "message": [{"type": "image", "data": {"url": self.url}}],
            }
        ]


class TestMediaDedupAcrossRestart:
    async def test_downloaded_media_is_not_fetched_again_after_restart(self, db, tmp_path):
        """去重原来只有进程内那张 URL → 路径的字典，重启即空：同一条消息的同一张
        图会被再下一遍、落成新文件，旧文件没人认领（`prune_media` 也删不到它）。"""
        root = tmp_path / "storage"
        media_root = root / "media"
        cfg = {"scope": {"group_messages": True}, "incremental": {"enabled": True}}

        engine = BackupEngine(db, media_root, cfg)
        calls = []

        async def fetch(url, path, session=None):
            calls.append(url)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"x" * 100)

        engine.media._fetch = fetch
        await engine.backup_group(100, _MediaPlatform("http://example.com/a.png"))

        # 「重启」：消息行被清掉、媒体文件还在盘上，平台重投了同一条消息
        db.delete_sources(100, ["m0"])
        db.delete_messages(100, ["m0"])
        restarted = BackupEngine(db, media_root, cfg)
        restarted.media._fetch = fetch
        await restarted.backup_group(100, _MediaPlatform("http://example.com/a.png"))

        assert len(calls) == 1, "重启后又把同一张图下了一遍"
        assert len([p for p in media_root.rglob("*") if p.is_file()]) == 1

    async def test_known_file_is_re_registered_when_row_missing(self, db, tmp_path):
        """文件在盘上、库里那行没了（迁移/手工清库）：要补登记，别落成孤儿文件。"""
        media_root = tmp_path / "media"
        day = media_root / BASE_TIME.strftime("%Y%m%d")
        day.mkdir(parents=True)
        blob = day / "m0_deadbeef.png"
        blob.write_bytes(b"x" * 32)

        db.save_media(
            message_id="m0",
            group_id=100,
            original_url="http://example.com/a.png",
            local_path=str(blob),
            filename=blob.name,
            file_size=32,
            mime_type="image/png",
            sha256="x",
        )
        db.execute("DELETE FROM media_files")

        engine = BackupEngine(db, media_root, {"scope": {"group_messages": True}})
        msg = Message(
            message_id="m0",
            group_id=100,
            sender_id=1,
            sender_name="张三",
            timestamp=BASE_TIME,
            message_type=MessageType.IMAGE,
            content="[图片]",
            raw_content="{}",
            media_paths=["http://example.com/a.png"],
        )
        engine._ensure_media_row(msg, "http://example.com/a.png", str(blob))

        assert db.get_media_for_message("m0")


class TestNestedSegmentsAreNotDropped:
    def test_text_nested_in_data_list(self):
        """`{'type':'text','data':[{'text':...}]}` 这种段原来解析成空内容。"""
        msg = parse_message(
            {
                "message_id": "a",
                "group_id": 1,
                "sender_id": 1,
                "timestamp": BASE_TIME.timestamp(),
                "message": [{"type": "text", "data": [{"text": "嵌在 data 列表里的字"}]}],
            },
            1,
        )
        assert msg.content == "嵌在 data 列表里的字"

    def test_unknown_segment_keeps_its_text(self):
        """认不出的段类型不能把内容一起丢掉——那是「备份了条消息，内容是空的」。"""
        msg = parse_message(
            {
                "message_id": "b",
                "group_id": 1,
                "sender_id": 1,
                "timestamp": BASE_TIME.timestamp(),
                "message": [{"type": "unsupported", "data": {"content": [{"text": "富文本"}]}}],
            },
            1,
        )
        assert msg.content == "富文本"

    def test_metadata_is_not_mistaken_for_content(self):
        """从消息链往下抠，而不是从整条 raw：否则 message_id / 昵称会被当正文。"""
        msg = parse_message(
            {
                "message_id": "m-12345",
                "group_id": 1,
                "sender": {"user_id": 7, "nickname": "不该出现在内容里"},
                "timestamp": BASE_TIME.timestamp(),
                "message": [{"type": "unsupported", "data": {"file": "abc.image"}}],
            },
            1,
        )
        assert msg.content == ""
        assert "不该出现在内容里" not in msg.content
        assert "m-12345" not in msg.content


class TestMergeStrategy:
    """`/backup restore --strategy merge` 原来等同 skip——文档写着三种策略，
    实际只有两种行为。"""

    async def test_merge_fills_missing_fields_only(self, db, tmp_path):
        msgs = _seed(db, n=2)
        archive = ExportManager(db, tmp_path / "exports", tmp_path / "media", {}).export(
            100, "zip", "bk1"
        ).path
        # 本地这两条：一条内容空了、一条有自己的内容
        db.execute(
            "UPDATE messages SET content = '', sender_name = '' WHERE group_id = 100 AND message_id = 'm0'"
        )
        db.execute(
            "UPDATE messages SET content = '本地已有内容' WHERE group_id = 100 AND message_id = 'm1'"
        )

        result = await RestoreEngine(db).restore(
            "bk1", 100, strategy="merge", archive=archive, confirm=True
        )

        assert result.merged == 1
        assert result.skipped == 1
        rows = {m.message_id: m for m in db.get_messages(group_id=100)}
        assert rows["m0"].content == msgs[0].content, "空内容应该被补齐"
        assert rows["m1"].content == "本地已有内容", "本地已有的内容不能被覆盖"
