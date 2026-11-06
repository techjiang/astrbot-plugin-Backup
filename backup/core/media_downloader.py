"""媒体文件下载器：限速 + 重试 + 去重 + 大小上限。下载失败只记录，不抛出。"""

import asyncio
import hashlib
import mimetypes
import re
import uuid
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


def _discard(path: Path) -> None:
    """一次下载失败后，把「正式文件」那一侧的残渣清掉（空的才删）。

    只碰正式路径，不碰任何 `.part`：临时文件由 `_fetch` 在 `finally` 里收拾
    自己那一份。这里要是顺手 glob 一下 `.part`，并发下同一个 URL 的另一次
    下载正在进行中的临时文件就会被这次失败连带删掉。
    """
    try:
        if path.is_file() and path.stat().st_size == 0:
            path.unlink(missing_ok=True)
    except OSError:
        pass


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
        self._url_index: dict[str, Path] = {}
        self.reconfigure(config)

    def reconfigure(self, config: Optional[dict] = None) -> None:
        """把配置读进运行参数。构造和 `/backup config set` 走同一条路。

        原来只在 `__init__` 里读一次：`/backup config set media.timeout 5` 回执
        写着「✅ 已设置」，可已经建好的下载器还按 30 秒超时干活，直到插件重载。
        `max_concurrent` 还要重建信号量——`asyncio.Semaphore` 的额度在构造时就定死了，
        改属性等于没改。
        """
        cfg = config if config is not None else {}
        self.max_file_size = parse_size(cfg.get("max_file_size", "100MB"))
        self.max_concurrent = max(int(cfg.get("max_concurrent", 3) or 1), 1)
        self.retry_count = max(int(cfg.get("retry_count", 3) or 1), 1)
        self.timeout = max(int(cfg.get("timeout", 30) or 1), 1)
        # 正在跑的下载会继续用它自己那份旧信号量，这没关系：下一个文件就用新的
        self._sem = asyncio.Semaphore(self.max_concurrent)

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
                    _discard(path)
                    if attempt < self.retry_count - 1:
                        await asyncio.sleep([2, 5, 10][min(attempt, 2)])
            raise last_err or RuntimeError("download_failed")

    async def _fetch(self, url: str, path: Path, session=None) -> None:
        """下到一个**本次调用独有**的临时文件，成功了才原子换到正式路径。

        临时名原来固定是 `<正式名>.part`：同一个 URL 被两处同时下（实时补下与
        定时备份撞上、或者用户连点两次 `/backup export` 之后的归档），两个协程
        写同一个文件，先换成功的那次把 `.part` 换走了，另一次 `replace` 找不到
        源文件，直接 `FileNotFoundError`——媒体被记成「下载失败」，而盘上明明
        有一份完整的。加上进程内唯一后缀，谁的临时文件谁收拾。

        超限/失败一律在 `finally` 里删掉自己的临时文件。原来只在「大小为 0」时
        删，超限中断留下的半截 `.part` 永远躺在媒体目录里——大群里那是成百上千
        个没人认领的残渣，`/backup clean` 也认不出它们。
        """
        own_session = False
        if session is None:
            import aiohttp  # 延迟导入，测试可注入假 session

            session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=self.timeout))
            own_session = True
        tmp = path.with_name(f"{path.name}.{uuid.uuid4().hex[:8]}.part")
        done = False
        try:
            async with session.get(url) as resp:
                if resp.status >= 400:
                    raise RuntimeError(f"http_{resp.status}")
                declared = resp.headers.get("Content-Length")
                if declared and int(declared) > self.max_file_size:
                    raise ValueError("file_too_large")
                size = 0
                path.parent.mkdir(parents=True, exist_ok=True)
                with tmp.open("wb") as fh:
                    async for chunk in resp.content.iter_chunked(64 * 1024):
                        size += len(chunk)
                        if size > self.max_file_size:
                            raise ValueError("file_too_large")
                        fh.write(chunk)
                tmp.replace(path)
                done = True
        finally:
            if not done:
                tmp.unlink(missing_ok=True)
            if own_session:
                await session.close()

    @staticmethod
    def sha256_of(path: str | Path) -> str:
        h = hashlib.sha256()
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(1024 * 256), b""):
                h.update(chunk)
        return h.hexdigest()
