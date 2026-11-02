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

    @property
    def ok(self) -> bool:
        return self.status == 200

    def iter_chunks(self, size: int = CHUNK):
        """流式读，避免大 ZIP 一次性进内存。"""
        if self.file is None:
            yield self.body
            return
        with open(self.file, "rb") as fh:
            for chunk in iter(lambda: fh.read(size), b""):
                yield chunk


def content_type_for(path: Path) -> str:
    return MEDIA_TYPES.get(path.suffix.lower(), "application/octet-stream")


def _error(status: int, message: str) -> DownloadResponse:
    return DownloadResponse(
        status=status,
        body=message.encode("utf-8"),
        headers={"Content-Type": "text/plain; charset=utf-8", "Cache-Control": "no-store"},
    )


def handle_download(path: str, exports) -> DownloadResponse:
    """处理一次下载请求。expires 由 exports.resolve_token 判定。

    状态码：404 无 token/文件不存在，410 已过期，400 路径非法。
    """
    if not path or not path.startswith(PREFIX):
        return _error(404, "not found")
    token = path[len(PREFIX):].split("?", 1)[0].split("/", 1)[0].strip()
    if not token:
        return _error(404, "not found")

    target = exports.resolve_token(token)
    if target is None:
        return _error(410, "链接已失效或不存在")

    stat = target.stat()
    return DownloadResponse(
        status=200,
        file=target,
        headers={
            "Content-Type": content_type_for(target),
            "Content-Length": str(stat.st_size),
            "Content-Disposition": f'attachment; filename="{target.name}"',
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )


def expires_header(expires_at: datetime) -> dict:
    return {"X-Expires-At": expires_at.isoformat(timespec="seconds")}
