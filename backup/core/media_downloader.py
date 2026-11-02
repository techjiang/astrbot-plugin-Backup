"""媒体文件下载器：限速 + 重试 + 去重 + 大小上限。下载失败只记录，不抛出。"""

import asyncio
import hashlib
import mimetypes
import re
from datetime import datetime
from pathlib import Path
from typing import Optional

SIZE_UNITS = {"B": 1, "KB": 1024, "MB": 1024**2, "GB": 1024**3}


def parse_size(value) -> int:
    """'100MB' / '1.5 GB' / 1024 → 字节数。"""
    if isinstance(value, (int, float)):
        return int(value)
    text = str(value).strip().upper()
    m = re.match(r"^([\d.]+)\s*([KMGT]?B)?$", text)
    if not m:
        return 100 * 1024**2
    num = float(m.group(1))
    unit = m.group(2) or "B"
    return int(num * SIZE_UNITS[unit])


def safe_filename(name: str, fallback: str = "file") -> str:
    """只保留文件名本体，防路径穿越。"""
    base = Path(str(name).replace("\\", "/")).name
    base = re.sub(r'[<>:"|?*\x00-\x1f]', "_", base).strip(" .")
    return base or fallback


def guess_ext(url: str, content_type: str = "") -> str:
    if content_type:
        ext = mimetypes.guess_extension(content_type.split(";")[0].strip())
        if ext:
            return ext
    suffix = Path(url.split("?")[0]).suffix
    if suffix and len(suffix) <= 8:
        return suffix
    return ".bin"


class MediaDownloader:
    def __init__(self, media_dir: str | Path, config: Optional[dict] = None):
        self.media_dir = Path(media_dir)
        self.media_dir.mkdir(parents=True, exist_ok=True)
        cfg = config or {}
        self.max_file_size = parse_size(cfg.get("max_file_size", "100MB"))
        self.max_concurrent = int(cfg.get("max_concurrent", 3))
        self.retry_count = int(cfg.get("retry_count", 3))
        self.timeout = int(cfg.get("timeout", 30))
        self._sem = asyncio.Semaphore(self.max_concurrent)
        self._url_index: dict[str, Path] = {}

    def url_hash(self, url: str) -> str:
        return hashlib.sha256(url.encode("utf-8")).hexdigest()[:8]

    def target_path(self, url: str, message_id: str, timestamp: datetime, ext: str = "") -> Path:
        day = timestamp.strftime("%Y%m%d") if timestamp else "unknown"
        name = f"{message_id}_{self.url_hash(url)}{ext}"
        return self.media_dir / day / safe_filename(name)

    def find_existing(self, url: str) -> Optional[Path]:
        """去重：URL 已下载过则直接复用。"""
        cached = self._url_index.get(url)
        if cached and cached.exists():
            return cached
        return None

    async def download(
        self, url: str, message_id: str, timestamp: datetime, session=None
    ) -> str:
        """下载单个文件，返回本地路径字符串。失败抛异常，调用方负责兜底。"""
        if not url or not str(url).startswith("http"):
            raise ValueError("invalid_url")

        existing = self.find_existing(url)
        if existing:
            return str(existing)

        ext = guess_ext(url)
        path = self.target_path(url, message_id, timestamp, ext)
        path.parent.mkdir(parents=True, exist_ok=True)

        async with self._sem:
            last_err: Optional[Exception] = None
            for attempt in range(self.retry_count):
                try:
                    await self._fetch(url, path, session)
                    self._url_index[url] = path
                    return str(path)
                except Exception as e:  # 网络/超时/大小超限都走重试
                    last_err = e
                    if path.exists() and path.stat().st_size == 0:
                        path.unlink(missing_ok=True)
                    if attempt < self.retry_count - 1:
                        await asyncio.sleep([2, 5, 10][min(attempt, 2)])
            raise last_err or RuntimeError("download_failed")

    async def _fetch(self, url: str, path: Path, session=None) -> None:
        own_session = False
        if session is None:
            import aiohttp  # 延迟导入，测试可注入假 session

            session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=self.timeout))
            own_session = True
        try:
            async with session.get(url) as resp:
                if resp.status >= 400:
                    raise RuntimeError(f"http_{resp.status}")
                declared = resp.headers.get("Content-Length")
                if declared and int(declared) > self.max_file_size:
                    raise ValueError("file_too_large")
                size = 0
                tmp = path.with_suffix(path.suffix + ".part")
                with tmp.open("wb") as fh:
                    async for chunk in resp.content.iter_chunked(64 * 1024):
                        size += len(chunk)
                        if size > self.max_file_size:
                            raise ValueError("file_too_large")
                        fh.write(chunk)
                tmp.replace(path)
        finally:
            if own_session:
                await session.close()

    @staticmethod
    def sha256_of(path: str | Path) -> str:
        h = hashlib.sha256()
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(1024 * 256), b""):
                h.update(chunk)
        return h.hexdigest()
