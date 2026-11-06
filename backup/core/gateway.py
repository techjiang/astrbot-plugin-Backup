"""内置下载网关：让群里那条链接真的能点开。

为什么要自己起一个 HTTP 口，见 `download_route.py` 末尾那段注释——AstrBot 4.14.6
的插件路由既匹配不到带 token 的路径，又整条落在面板鉴权之外（`/api/plug/*` 不在
白名单里），所以「把链接挂在宿主面板上」这件事在那版宿主上做不到。

设计约束：

- **只监听回环地址**。对外暴露是反代/穿透的事，网关不自己上公网。
- **默认不开**。开了但没配对外地址，拼出来的还是点不动的链接；与其给人一条假
  链接，不如让它显式地说「我没开」。
- **跑在线程里**。`http.server` 是阻塞的，塞进事件循环会拖住机器人聊天，所以
  用 `ThreadingHTTPServer` + 守护线程。
- **只认 `/backup/download/<token>`**。其余路径一律 404，不暴露任何目录。
"""

import errno
import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Optional

from .download_route import PREFIX, DownloadResponse, handle_download

logger = logging.getLogger(__name__)

#: 网关自己的路径前缀，和面板路由同形
GATEWAY_PREFIX = PREFIX


class _Handler(BaseHTTPRequestHandler):
    """每个请求交给 `exports` 兑换 token，然后按块吐文件。"""

    protocol_version = "HTTP/1.1"
    server_version = "AstrBotBackupGateway"
    sys_version = ""

    #: 由 `DownloadGateway.start()` 注入
    exports = None

    def log_message(self, fmt, *args):  # noqa: A003 - 覆盖父类
        # 默认实现把整行请求（含 token）打到 stderr。token 等于一把钥匙，
        # 不该留在日志里——这里只记状态码。
        logger.debug("下载网关 %s", fmt % args)

    def _reply(self, response: DownloadResponse) -> None:
        self.send_response(response.status)
        for name, value in response.headers.items():
            self.send_header(name, value)
        if not response.headers.get("Content-Length") and response.file is None:
            self.send_header("Content-Length", str(len(response.body)))
        self.end_headers()
        try:
            for chunk in response.iter_chunks():
                self.wfile.write(chunk)
        except (BrokenPipeError, ConnectionResetError):
            # 用户点了取消。不是错误，也不该在日志里刷屏
            pass

    def do_HEAD(self):  # noqa: N802 - BaseHTTPRequestHandler 的约定
        self._respond(body=False)

    def do_GET(self):  # noqa: N802
        self._respond()

    def _respond(self, body: bool = True) -> None:
        try:
            response = handle_download(
                self.path,
                self.exports,
                self.headers.get("Range") if self.headers else None,
            )
        except Exception as e:  # 网关是最外层，任何异常都得变成一句话
            logger.warning("下载网关处理失败：%s", type(e).__name__)
            response = DownloadResponse(
                status=500,
                body=b"internal error",
                headers={"Content-Type": "text/plain; charset=utf-8"},
            )
        if not body:
            self.send_response(response.status)
            for name, value in response.headers.items():
                self.send_header(name, value)
            self.send_header("Content-Length", str(len(response.body) or 0))
            self.end_headers()
            return
        self._reply(response)


class DownloadGateway:
    """内置网关的生命周期。起不来就返回失败，不抛给插件调用方。"""

    def __init__(self, exports, host: str = "127.0.0.1", port: int = 0):
        self.exports = exports
        self.host = host or "127.0.0.1"
        self.port = int(port or 0)
        self._httpd: Optional[ThreadingHTTPServer] = None
        self._thread: Optional[threading.Thread] = None
        #: 上次 `start()` 失败的原因。`running` 只说「没起来」，这个说「为什么」。
        #: 宿主重载插件时，旧实例若没被 `terminate()`，新实例绑同一个端口必然失败
        #: ——而配置里 `enabled` 明明是 true，光看配置会查到错的地方去。
        self.last_error: str = ""

    def _explain(self, error: OSError) -> str:
        """把绑定失败翻成一句能照着办的话。

        只有 `EADDRINUSE` 是「端口被占」——那多半是宿主重载插件时旧实例还活着
        （它的 `terminate()` 没被调到），或者部署里有两个进程配了同一个端口。
        其余情况（主机名解析不了、端口越界、没权限绑低端口）不是「被占」，
        混成一句话会让人去查错的那一项。

        端口写 0 时 `self.port` 是「让系统挑」，把它打进这句话毫无意义，所以
        只打真正想绑的那个端口。
        """
        where = f"{self.host}:{self.port}" if self.port else self.host
        detail = str(error) or type(error).__name__
        if getattr(error, "errno", None) == errno.EADDRINUSE:
            return (
                f"{where} 端口已被占用（{detail}）。"
                "常见原因：宿主重载插件时旧实例还在（它的 terminate() 没被调到），"
                "或有两个进程配了同一个 gateway.port"
            )
        return f"{where} 绑定失败（{detail}）"

    @property
    def running(self) -> bool:
        return self._httpd is not None

    @property
    def bound_port(self) -> int:
        if self._httpd is None:
            return 0
        return int(self._httpd.server_address[1])

    def start(self) -> bool:
        if self._httpd is not None:
            return True
        if self.exports is None:
            self.last_error = "没有导出管理器，网关无事可做"
            return False
        handler = type("_BoundHandler", (_Handler,), {"exports": self.exports})
        try:
            httpd = ThreadingHTTPServer((self.host, self.port), handler)
        except OSError as e:
            # 端口被占、地址不合法——都只是「这条链接点不动」，不该让插件起不来。
            # 但要说清是哪一种：`errno` 之外，`str(e)` 里就是系统原话（"Address
            # already in use"），那是唯一能让人一眼定位的信息，原来被丢掉了。
            self.last_error = self._explain(e)
            logger.warning("下载网关启动失败：%s", self.last_error)
            return False
        httpd.daemon_threads = True
        thread = threading.Thread(
            target=httpd.serve_forever,
            name="backup-download-gateway",
            daemon=True,
        )
        thread.start()
        self._httpd = httpd
        self._thread = thread
        logger.info("下载网关已启动：http://%s:%s%s", self.host, self.bound_port, GATEWAY_PREFIX)
        return True

    def stop(self) -> None:
        httpd, self._httpd = self._httpd, None
        if httpd is None:
            return
        self.last_error = ""
        try:
            httpd.shutdown()
        except Exception:
            pass
        try:
            httpd.server_close()
        except Exception:
            pass
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.join(timeout=2)

    def url_for(self, token: str, base_url: str = "") -> str:
        """完整链接。`base_url` 是对外地址（反代/穿透），空则退回本机回环。"""
        root = (base_url or f"http://{self.host}:{self.bound_port}").rstrip("/")
        return f"{root}{GATEWAY_PREFIX}{token}"
