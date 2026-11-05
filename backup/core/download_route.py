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


#: 宿主会把插件路由挂在一层自己的前缀下面，这层前缀不属于我们的路由。按长度
#: 从长到短匹配，削掉命中的那一层：
#:
#: - AstrBot 旧版面板（Quart）：`app.add_url_rule("/api/plug/<path:subpath>")`，
#:   `request.path` 是 `/api/plug/backup/download/<token>`；
#: - AstrBot 新版面板（FastAPI）：注册的 route 带插件名前缀，面板把它挂在
#:   `/api/v1/plugins/extensions/<插件名>/` 下，`request.path` 是
#:   `/api/v1/plugins/extensions/astrbot-plugin-Backup/backup/download/<token>`。
#:   漏了这条，群里点的链接一律 404——而 404 和「链接已失效」在用户眼里是同一句话；
#: - 自建网关可能挂在 `/api`、`/plug`、`/web` 下面。
HOST_PREFIXES = (
    "/api/v1/plugins/extensions",
    "/api/plugins/extensions",
    "/api/plug",
    "/plug",
    "/api",
    "/web",
)

#: 旧版面板给插件路由的统一挂载点。按它算出来的链接**不带插件名**，在新版面板上
#: 必然 404（新版挂在 `/api/v1/plugins/extensions/<插件名>/`），所以 `active_prefix`
#: 记着这次真正注册成功的那一个前缀，拼链接时用它。
LEGACY_PREFIX = "/api/plug"


def _astrbot_plugin_request():
    """AstrBot 新版面板（FastAPI）给插件 Web API 绑定的请求代理。

    新版面板不在 Quart 请求上下文里调插件视图，`from quart import request` 拿到的是
    「没有活动请求」，读一次就抛——视图 500。它自己的入口是 `astrbot.api.web.request`
    这个上下文代理，只在 handler 执行期间可用，取不到会抛 `RuntimeError`。

    这里只认它的**存在性**：拿得到就交给上层按「它和 Flask/Quart 的 request 长得
    一样」去读（`path` / `query_string` / `headers` 都在，`query` 是带 `get` 的
    字典形状）。
    """
    try:  # pragma: no cover - 取决于宿主版本
        from astrbot.api.web import request as plugin_request

        plugin_request.method  # noqa: B018 - 探一下上下文有没有绑上
        return plugin_request
    except Exception:
        return None


def _current_request():
    """拿到当前框架的请求对象。拿不到返回 None（说明不在请求上下文里）。

    顺序：AstrBot 插件请求代理 → Quart → Flask。

    - **必须**先判 AstrBot：新版面板（FastAPI）根本不在 Quart 上下文里调视图；
    - **必须**先判 Quart、后判 Flask：旧面板跑在 Quart 上，而
      `astrbot/dashboard/server.py` 为了序列化 `import flask` 了东西（Werkzeug 也在
      依赖树里）。先试 Flask 的话，`from flask import request` 会成功，拿到的是一个
      「当前没有活动请求」的代理对象——`request.path` 一读就抛
      `RuntimeError: Working outside of request context`，视图 500。
    """
    current = _astrbot_plugin_request()
    if current is not None:
        return current
    try:  # pragma: no cover - 取决于宿主装了哪个框架
        from quart import request as quart_request, has_request_context

        if has_request_context():
            return quart_request
    except Exception:
        pass
    try:  # pragma: no cover
        from flask import request as flask_request, has_request_context as flask_has

        if flask_has():
            return flask_request
    except Exception:
        pass
    return None


def _request_path(request) -> str:
    """请求路径。**只在真有请求上下文时**读，否则返回空串。

    读的属性本身可能是 werkzeug/aiohttp 的代理对象，在上下文外取值直接抛
    RuntimeError。外层只兜 `Exception` 不够——视图里那一句就是 500 的来源。
    """
    if request is None:
        return ""
    for name in ("path", "full_path", "path_qs", "url"):
        try:
            value = getattr(request, name, None)
        except Exception:
            continue
        if isinstance(value, str) and value:
            return value
    return ""


def _strip_host_prefix(path: str) -> str:
    """削掉宿主加的那层前缀，还原成插件自己的路由形状。"""
    for prefix in HOST_PREFIXES:
        if path == prefix or path.startswith(prefix + "/"):
            return path[len(prefix):] or "/"
    return path


def _request_query_token(request) -> str:
    """`?token=`。两套宿主的取法不同：

    - Flask / Quart：`request.args`；
    - AstrBot 新版面板：`request.query`（`PluginMultiDict`，有 `get()`，没有 `args`）。

    原来只认 `args`，新版面板上 query 取不到，只有「路径里带 token」那条能活。
    """
    if request is None:
        return ""
    for name in ("args", "query"):
        try:
            bag = getattr(request, name, None)
        except Exception:
            continue
        if bag is None:
            continue
        try:
            token = bag.get("token")
        except Exception:
            continue
        if token:
            return str(token)
    return ""


def _request_range(request) -> Optional[str]:
    if request is None:
        return None
    try:
        headers = getattr(request, "headers", None)
        if headers is None:
            return None
        return headers.get("Range")
    except Exception:
        return None


def _request_path_with_query(request) -> str:
    """请求路径 + query 串。新版面板的 `request.path` 只到 path，token 在 query 里。"""
    path = _request_path(request)
    try:
        query = getattr(request, "query_string", None)
    except Exception:
        query = None
    if isinstance(query, bytes) and query:
        return f"{path}?{query.decode('utf-8', 'ignore')}"
    if isinstance(query, str) and query:
        return f"{path}?{query}"
    return path


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
            # 带 query 一起取：宿主只挂了固定前缀 `/backup/download` 时，token 只能
            # 从 `?token=` 里拿（新版面板的 `/api/v1/plugins/extensions/...` 就是这种）。
            path = _request_path_with_query(request)
        base, _, _query = path.partition("?")
        # 群里点的是 `/api/backup/download/<token>`，宿主实际挂载在
        # `/api/plug/backup/download/<token>`，`handle_download` 只认
        # `/backup/download/<token>`。中间这一层必须削掉，否则每一次真实的群内
        # 点击都是 404——而 404 和「链接过期」在用户眼里一样。
        return _strip_host_prefix(base), header
    return f"{PREFIX}{token}", header


def read_request(request=MISSING, **kwargs) -> tuple[str, Optional[str]]:
    """公开版：给宿主自己写的视图用。`read_request()` 自动读当前上下文。"""
    return _read_request(request, kwargs)


def _wants_stream(kwargs: dict):
    """视图该回哪种响应形态。

    - `False`：Flask / Werkzeug 能认的 `Response`（旧版面板走这条）；
    - `True`：原始 `DownloadResponse`，调用方自己发（自建网关）；
    - `"starlette"`：FastAPI / Starlette 的 `StreamingResponse`。

    判断顺序：先看**当前请求是谁给的**（AstrBot 新版面板绑的代理 → Starlette），
    再按装了哪个框架猜。上一版只认「装了 Flask 就用 Flask 形态、装了 Quart 就用
    Quart 形态」，而新版面板是 FastAPI——两边都不命中就落到 `False`，把 Werkzeug
    的 `Response` 交给了 Starlette，于是 `_coerce_view_result` 再包一层 JSON，
    下载下来的是 `{}`。
    """
    if kwargs.get("stream") or kwargs.get("raw"):
        return True
    if _astrbot_plugin_request() is not None:
        return "starlette"
    try:
        import flask  # type: ignore # noqa: F401

        return False
    except Exception:
        pass
    try:
        import quart  # type: ignore # noqa: F401

        return True
    except Exception:
        pass
    try:  # pragma: no cover - 取决于宿主装了哪个框架
        from starlette.responses import StreamingResponse  # type: ignore # noqa: F401

        return "starlette"
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


def _as_starlette(response: DownloadResponse):
    """FastAPI / Starlette 宿主（AstrBot 新版面板）能直接 `return` 的形态。

    **不能**回 `DownloadResponse` 数据类，也不能回 `(body, status, headers)`：
    `astrbot/dashboard/api/plugins.py::_coerce_view_result` 对自定义对象原样往下
    传，FastAPI 那一层再把它当响应体序列化——结果是 200 + JSON 化的一个 dict，
    文件一个字节都下不来。错误页同理：404/410 光写在自定义对象的 `status` 上，
    到了 HTTP 层全变 200。

    所以这里自己构造 `StreamingResponse`：状态码、`Content-Type`、
    `Content-Range` / `Content-Length` 都照搬，body 走 `iter_chunks()` 流式读，
    大 ZIP 不进内存。
    """
    try:
        from starlette.responses import StreamingResponse
    except Exception:  # pragma: no cover - 没有 Starlette 的老宿主
        return _as_flask(response)

    headers = dict(response.headers or {})
    headers.pop("Content-Length", None)  # 交给流式响应自己算，切片时按 length
    return StreamingResponse(
        response.iter_chunks(),
        status_code=response.status,
        headers=headers,
    )


def _as_response(response: DownloadResponse, starlette: bool = False):
    """按宿主框架挑返回形态。`starlette=True` 表示当前在 FastAPI 面板里。"""
    if starlette:
        return _as_starlette(response)
    return _as_flask(response)


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
        try:
            path, header = _read_request(request, kwargs)
        except Exception:
            # 宿主框架的请求对象在上下文外一读就抛。视图是直接对着群成员的浏览器
            # 的，抛出去就是 500 错误页——比「链接已失效」更让人摸不着头脑
            path, header = "", None
        response = handle_download(path, exports, header)
        want_stream = _wants_stream(kwargs) if stream is None else stream
        if not want_stream:
            return _as_response(response)
        real_request = request if request is not MISSING else None
        if real_request is not None and hasattr(real_request, "rel_url"):
            return await _send_file_aiohttp(real_request, response)
        if want_stream == "starlette":
            return _as_starlette(response)
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
