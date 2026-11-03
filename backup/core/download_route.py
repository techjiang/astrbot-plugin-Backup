"""把 export 的临时链接真正接上：token → 文件流。

红线 4：链接必须带 token 且过期，不能是永久链接。
AstrBot 不同版本暴露静态/自定义路由的方式不同，这里只做纯逻辑：
给一个路径，回 (状态码, 头部, 文件路径或文本)。接入层照抄即可。
"""

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

PREFIX = "/backup/download/"
CHUNK = 1024 * 256

MEDIA_TYPES = {
    ".zip": "application/zip",
    ".sqlite": "application/vnd.sqlite3",
    ".db": "application/vnd.sqlite3",
    ".html": "text/html; charset=utf-8",
    ".htm": "text/html; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".csv": "text/csv; charset=utf-8",
    ".txt": "text/plain; charset=utf-8",
    ".md": "text/markdown; charset=utf-8",
}


@dataclass
class DownloadResponse:
    status: int
    body: bytes = b""
    headers: dict = field(default_factory=dict)
    file: Optional[Path] = None
    start: int = 0
    length: Optional[int] = None

    @property
    def ok(self) -> bool:
        return self.status == 200

    def iter_chunks(self, size: int = CHUNK):
        """流式读，避免大 ZIP 一次性进内存。按 [start, start+length) 取，支持续传。"""
        if self.file is None:
            yield self.body
            return
        with open(self.file, "rb") as fh:
            fh.seek(self.start)
            remaining = self.length
            while True:
                chunk = fh.read(size if remaining is None else min(size, remaining))
                if not chunk:
                    return
                yield chunk
                if remaining is not None:
                    remaining -= len(chunk)
                    if remaining <= 0:
                        return


def content_type_for(path: Path) -> str:
    return MEDIA_TYPES.get(path.suffix.lower(), "application/octet-stream")


def _error(status: int, message: str) -> DownloadResponse:
    return DownloadResponse(
        status=status,
        body=message.encode("utf-8"),
        headers={"Content-Type": "text/plain; charset=utf-8", "Cache-Control": "no-store"},
    )


def _parse_range(header: Optional[str], total: int) -> Optional[tuple[int, int]]:
    """解析 `Range: bytes=start-end`。只认单段、绝对偏移；不合法返回 None（按整份发）。"""
    if not header or not header.lower().startswith("bytes="):
        return None
    spec = header[6:].strip().split(",")[0].strip()
    if not spec or spec.startswith("-"):
        return None  # 后缀区间（bytes=-500）对 ZIP 没意义，忽略
    start_s, _, end_s = spec.partition("-")
    if not start_s.isdigit():
        return None
    start = int(start_s)
    if start >= total:
        return None
    end = int(end_s) if end_s.isdigit() else total - 1
    end = min(end, total - 1)
    if end < start:
        return None
    return start, end - start + 1


def handle_download(path: str, exports, range_header: Optional[str] = None) -> DownloadResponse:
    """处理一次下载请求。expires 由 exports.resolve_token 判定，token 一次性。

    状态码：404 无 token/文件不存在，410 已过期，416 区间不可满足。
    链接是分享给群成员点的，浏览器下载大文件时常常先发一个 Range 探测、再续传，
    所以这里必须回 206：回不了续传，用户拿到的是断掉的半个 ZIP。
    """
    if not path or not path.startswith(PREFIX):
        return _error(404, "not found")
    token = path[len(PREFIX):].split("?", 1)[0].split("/", 1)[0].strip()
    if not token:
        return _error(404, "not found")

    # token 一次性，但「一次性」指的是只能领走一份，不是「只准发一个请求」。
    # 浏览器下载大文件一定先发 `Range: bytes=0-` 探测再续传；原来每次兑换都删
    # token，探测就成了唯一一次，真正要数据的那次请求拿到 410——
    # 用户看到的是「下了一半断了」，而不是「链接过期」。
    # 所以区间请求按「同一次下载的后续片段」处理，只有整份取走才作废。
    partial = bool(range_header)
    target = exports.resolve_token(token, shared=partial)
    if target is None:
        return _error(410, "链接已失效或不存在")

    stat = target.stat()
    total = stat.st_size
    headers = {
        "Content-Type": content_type_for(target),
        "Content-Disposition": f'attachment; filename="{target.name}"',
        "Cache-Control": "no-store",
        "X-Content-Type-Options": "nosniff",
        "Accept-Ranges": "bytes",
    }
    span = _parse_range(range_header, total)
    if span is None:
        if range_header:
            return _error(416, "range not satisfiable")
        headers["Content-Length"] = str(total)
        return DownloadResponse(status=200, file=target, headers=headers)

    start, length = span
    headers["Content-Length"] = str(length)
    headers["Content-Range"] = f"bytes {start}-{start + length - 1}/{total}"
    return DownloadResponse(status=206, file=target, headers=headers, start=start, length=length)


def expires_header(expires_at: datetime) -> dict:
    return {"X-Expires-At": expires_at.isoformat(timespec="seconds")}
