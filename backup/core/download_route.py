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
    if not path:
        return _error(404, "not found")
    # 宿主注册路由时一般会把前缀吃掉，传进来的可能只是 `/download/<token>`。
    # 只认完整前缀的话，这种接法一律 404——而 404 和「链接过期」在用户眼里是
    # 同一个意思：都下不了。两种认法都接住，其余路径照旧 404，别把无关请求
    # 也当成本插件的一条链接（那会变成 410「已失效」，把人误导到别处去）。
    target_path = path.split("?", 1)[0]
    token = ""
    for prefix in (PREFIX, "/download/"):
        if target_path.startswith(prefix):
            token = target_path[len(prefix):]
            break
    token = token.split("/", 1)[0].strip()
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


# ---------------------------------------------------------------------------
# 视图层：把 handle_download 接到宿主的 Web 框架上
# ---------------------------------------------------------------------------
#
# 上一版这里返回的是一个普通的 `DownloadResponse` 数据类，然后把
# `context.register_web_api("/backup/download", view, ...)` 当成接好了。真跑起来
# 是两处硬伤：
#
# 1. **视图函数的参数对不上。** AstrBot 4.14 的 `dashboard/server.py` 里，
#    `srv_plug_route(subpath, *args, **kwargs)` 是用 `await view_handler(*args,
#    **kwargs)` 调的，`args` 来自 Flask 的 url 变量，`/api/plug/<path:subpath>`
#    只有一个 `subpath`，而那个已经被 srv_plug_route 自己吃掉当 route 匹配用了。
#    所以视图函数收到的是**零个**位置参数，签名里写 `path` / `request` 都是
#    TypeError → 面板里点下载是 500。
# 2. **返回类型宿主不认。** Flask 只认 str / bytes / dict / Response 之类，
#    返回一个自定义 dataclass 会在序列化那一步炸掉。
#
# 所以视图必须自己去找「当前请求」：Flask/Quart 走 `flask.request` 上下文，
# aiohttp 走 `web.Request` 形参。找一个不该由调用方操心的事，放在这里做。
#
# 另有一处不是我们能改的：AstrBot 4.14 把 `/api/backup/download` 写进了
# `auth_middleware` 的白名单，**按前缀**放行——`/api/backup/download/<token>`
# 上的所有路径都不需要面板 JWT。于是群里点链接这件事其实做得成：
# 注册一条固定前缀路由，token 走路径尾段。上一版「4.14 做不到」的结论下错了。

MISSING = object()


def _current_request():
    """拿到当前框架的请求对象。拿不到返回 None（说明不在请求上下文里）。"""
    # Flask / Quart：`ctx.request` 在应用上下文外会抛异常
    try:  # pragma: no cover - 取决于宿主装了哪个框架
        from flask import request as flask_request  # type: ignore

        if flask_request is not None:
            return flask_request
    except Exception:
        pass
    try:
        from quart import request as quart_request  # type: ignore

        return quart_request
    except Exception:
        pass
    return None


def _request_path(request) -> str:
    if request is None:
        return ""
    for name in ("path", "full_path", "path_qs", "url"):
        value = getattr(request, name, None)
        if isinstance(value, str) and value:
            return value
    return ""


def _request_query_token(request) -> str:
    if request is None:
        return ""
    args = getattr(request, "args", None)
    if args is None:
        return ""
    try:
        return str(args.get("token") or "")
    except Exception:
        return ""


def _request_range(request) -> Optional[str]:
    if request is None:
        return None
    headers = getattr(request, "headers", None)
    if headers is None:
        return None
    try:
        return headers.get("Range")
    except Exception:
        return None


def _read_request(request, kwargs: Optional[dict] = None) -> tuple[str, Optional[str]]:
    """把「第几次请求、要哪一段」拼出来。返回 (用于兑换 token 的路径, Range 头)。"""
    kwargs = kwargs or {}
    request = request if request is not MISSING else _current_request()
    header = kwargs.get("range_header") or _request_range(request)

    token = str(kwargs.get("token") or "")
    if not token:
        token = _request_query_token(request)
    if not token:
        path = str(kwargs.get("path") or "")
        if not path:
            path = _request_path(request)
        base, _, query = path.partition("?")
        # 群里点的是 `/api/backup/download/<token>`，不是 `/backup/download/<token>`。
        # `handle_download` 只认后一种，前面那层 `/api` 前缀得在这里削掉，否则
        # 每一次真实的群内点击都会 404——而 404 和「链接过期」在用户眼里一样。
        short = base
        if short.startswith("/api/"):
            short = short[4:]
        return short, header
    return f"{PREFIX}{token}", header


def read_request(request=MISSING, **kwargs) -> tuple[str, Optional[str]]:
    """公开版：给宿主自己写的视图用。`read_request()` 自动读当前上下文。"""
    return _read_request(request, kwargs)


def _wants_stream(kwargs: dict) -> bool:
    """调用方要不要原始 `DownloadResponse`。

    Quart 的 `send_file` 我们要手写，那条路径要自己拼 Response，所以给它原始
    对象；Flask 那边能直接 `return`，走框架友好形态。
    """
    if kwargs.get("stream") or kwargs.get("raw"):
        return True
    try:
        import flask  # type: ignore # noqa: F401

        return False
    except Exception:
        pass
    try:
        import quart  # type: ignore # noqa: F401

        return True
    except Exception:
        return False


def _as_flask(response: DownloadResponse):
    """Flask / 任何 WSGI 宿主能直接 `return` 的形态。

    **不能**回 `(body, status, headers)` 元组：`DownloadResponse.body` 在正常
    下载里是空的（文件躺在 `file` 上），元组会被 Flask 当成「响应体就是这 0 个
    字节」，`Content-Length: 0`——状态 200，下载下来的 ZIP 是空文件。必须自己
    构造一个「可迭代的响应体」，让 WSGI 一层层把块吐出去。
    """
    try:
        from werkzeug.wrappers import Response as WerkzeugResponse  # Flask 的底座

        return WerkzeugResponse(
            response.iter_chunks(),
            status=response.status,
            headers=response.headers,
        )
    except Exception:
        # 没有 Werkzeug：至少把状态和头带出去，别把状态码吞掉
        return response


async def _send_file_aiohttp(request, response: DownloadResponse):
    from aiohttp import web  # 宿主是 aiohttp 时 ia 一定有

    if response.file is None:
        return web.Response(
            status=response.status, body=response.body, headers=response.headers
        )
    return web.FileResponse(
        path=response.file,
        status=response.status,
        headers=response.headers,
    )


async def _send_file_quart(response: DownloadResponse):
    """Quart 宿主：手拼一个 Response（`send_file` 要 async 且带 conditional）。

    没有 Quart 也没有 aiohttp 的环境（脚本、单测）不能因此炸掉——退回原始
    `DownloadResponse`，调用方爱怎么发怎么发。
    """
    try:
        from quart import Response as QuartResponse
    except Exception:
        return response
    return QuartResponse(
        response.iter_chunks(),
        status=response.status,
        headers=response.headers,
    )


def make_view(exports, range_header_getter=None, stream: Optional[bool] = None):
    """生成一个不依赖具体框架的视图函数，直接交给 `register_web_api`。

    - 形参全是可选的：宿主少传、多传参数都能跑（`srv_plug_route` 一个都不传）
    - 返回值按宿主框架走：Flask 回元组，Quart 回 Response，aiohttp 回 web.Response
    - 也可以传 `path=` / `token=` / `range_header=` 手动指定，便于单测和自建网关
    """

    async def view(request=MISSING, **kwargs):
        if range_header_getter is not None and not kwargs.get("range_header"):
            try:
                kwargs["range_header"] = range_header_getter()
            except Exception:
                pass
        path, header = _read_request(request, kwargs)
        response = handle_download(path, exports, header)
        want_stream = _wants_stream(kwargs) if stream is None else stream
        if not want_stream:
            return _as_flask(response)
        real_request = request if request is not MISSING else None
        if real_request is not None and hasattr(real_request, "rel_url"):
            return await _send_file_aiohttp(real_request, response)
        return await _send_file_quart(response)

    view.full_prefix = PREFIX  # type: ignore[attr-defined]
    view.short_prefix = "/download/"  # type: ignore[attr-defined]
    return view


async def stream_view(request, exports) -> DownloadResponse:
    """aiohttp 形状的视图：`async def handler(request)`。宿主自建网关时用它。"""
    path, header = _read_request(request)
    return handle_download(path, exports, header)


def aiohttp_view(exports):
    """包成 aiohttp 能直接注册的协程（返回 web.Response）。"""

    async def view(request):
        response = await stream_view(request, exports)
        return await _send_file_aiohttp(request, response)

    return view
