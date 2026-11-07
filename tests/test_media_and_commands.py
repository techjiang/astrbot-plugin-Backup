"""补媒体下载（假 session）与命令文案的覆盖。"""

import asyncio
from datetime import datetime

import pytest

from backup.core.commands import render_backup_list, render_progress, render_status, human_duration
from backup.core.media_downloader import MediaDownloader, guess_ext
from backup.core.models import BackupRecord, BackupStatus

from .conftest import make_message


class FakeContent:
    def __init__(self, chunks):
        self.chunks = chunks

    async def iter_chunked(self, size):
        for c in self.chunks:
            yield c


class FakeResp:
    def __init__(self, chunks, status=200, headers=None):
        self.content = FakeContent(chunks)
        self.status = status
        self.headers = headers or {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False


class FakeSession:
    def __init__(self, resp_factory):
        self.resp_factory = resp_factory
        self.closed = False

    def get(self, url):
        return self.resp_factory()

    async def close(self):
        self.closed = True


class TestMediaDownload:
    async def test_download_writes_file(self, tmp_path):
        dl = MediaDownloader(tmp_path, {"max_file_size": "1MB", "retry_count": 1})
        session = FakeSession(lambda: FakeResp([b"abc", b"def"], headers={"Content-Length": "6"}))
        path = await dl.download("http://x/a.png", "m1", datetime(2024, 5, 1), session=session)
        assert open(path, "rb").read() == b"abcdef"

    async def test_oversize_rejected(self, tmp_path):
        dl = MediaDownloader(tmp_path, {"max_file_size": "1KB", "retry_count": 1})
        session = FakeSession(lambda: FakeResp([b"x" * 4096]))
        with pytest.raises(ValueError):
            await dl.download("http://x/big.bin", "m2", datetime(2024, 5, 1), session=session)

    async def test_http_error_raises_and_no_partial(self, tmp_path):
        dl = MediaDownloader(tmp_path, {"retry_count": 1})
        session = FakeSession(lambda: FakeResp([], status=404))
        with pytest.raises(RuntimeError):
            await dl.download("http://x/404.png", "m3", datetime(2024, 5, 1), session=session)
        assert not any(p.suffix == ".part" for p in tmp_path.rglob("*"))

    async def test_invalid_url(self, tmp_path):
        dl = MediaDownloader(tmp_path, {})
        with pytest.raises(ValueError):
            await dl.download("ftp://x/a", "m4", datetime.now())

    def test_guess_ext(self):
        assert guess_ext("http://x/a.png") == ".png"
        assert guess_ext("http://x/noext", "image/jpeg") == ".jpg"
        assert guess_ext("http://x/noext") == ".bin"

    def test_sha256_of(self, tmp_path):
        f = tmp_path / "a.bin"
        f.write_bytes(b"hello")
        assert len(MediaDownloader.sha256_of(f)) == 64


class TestCommandRendering:
    def test_render_progress(self):
        assert "已备份 5 条" in render_progress(5)
        bar = render_progress(5, 10)
        assert "5/10" in bar and "█" in bar

    def test_human_duration(self):
        assert human_duration(30) == "30 秒"
        assert human_duration(90) == "1 分 30 秒"
        assert human_duration(3700).startswith("1 小时")

    def test_render_backup_list(self, db):
        assert "还没有任何备份记录" in render_backup_list(db)
        db.save_record(
            BackupRecord(
                backup_id="bk1",
                group_id=100,
                status=BackupStatus.COMPLETED,
                started_at=datetime.now(),
                message_count=3,
                total_size=2048,
            )
        )
        text = render_backup_list(db, 100)
        assert "bk1" in text and "2.0KB" in text and "<全量>" in text

    def test_render_status(self, db):
        db.save_message(make_message(0), "bk1")
        text = render_status(db, 100)
        assert "消息总数：1" in text
        assert "还没有完成过备份" in text
