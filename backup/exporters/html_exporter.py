"""HTML 导出：自包含单文件，仿聊天界面，含内嵌 SVG logo 与作者信息。"""

import base64
import html
import mimetypes
from pathlib import Path
from typing import Optional

from .base import BaseExporter, ExportContext

TEMPLATE_PATH = Path(__file__).resolve().parent.parent / "templates" / "chat.html"
#: 单张图片内联上限。超过就只留链接——一张 40MB 的 PNG 塞进 HTML，文件从 3MB
#: 涨到 56MB，本地打开要等半分钟。
MAX_INLINE_BYTES = 6 * 1024 * 1024
#: 整份 HTML 的内联预算。单张都合格、但一天两百张图加起来照样能把页面撑到几百 MB，
#: 浏览器直接卡死——所以逐张累计，超了就从这里开始只留链接。
MAX_INLINE_TOTAL_BYTES = 96 * 1024 * 1024

IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp")

#: 媒体下载失败时，引擎在 `media_paths` 里留下的标记（见 `backup_engine._download_media`）
_FAILED_MARK = "[下载失败]"

_TYPE_LABEL = {
    "image": "[图片]",
    "voice": "[语音]",
    "video": "[视频]",
    "file": "[文件]",
    "forward": "[合并转发]",
}


class HtmlExporter(BaseExporter):
    extension = ".html"
    format_name = "html"

    def __init__(
        self,
        template_path: Optional[Path] = None,
        own_user_id: Optional[int] = None,
        max_inline_total: int = MAX_INLINE_TOTAL_BYTES,
    ):
        self.template_path = Path(template_path) if template_path else TEMPLATE_PATH
        # 导出者视角：没有自己的 user_id 时全部靠左（对方），避免乱猜
        self.own_user_id = own_user_id
        self.max_inline_total = int(max_inline_total)
        self._inlined = 0

    def export(self, ctx: ExportContext, output_path: str | Path) -> Path:
        path = self.ensure_path(output_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self.render(ctx), encoding="utf-8")
        return path

    def render(self, ctx: ExportContext) -> str:
        # 同一个实例可能被连着重渲染两次（导出一次、打包时再写一份 chat.html），
        # 预算必须每次从头算，否则第二份会因为上一份用过而突然全部不内联
        self._inlined = 0
        template = self.template_path.read_text(encoding="utf-8")
        messages = sorted(ctx.messages, key=lambda m: (m.timestamp, str(m.message_id)))
        days = self.group_by_date(messages)
        participants = self.participants(messages)

        body_parts: list[str] = []
        for day, day_messages in days:
            body_parts.append(
                f'<div class="day" id="day-{html.escape(day)}">{html.escape(day)} · {len(day_messages)} 条</div>'
            )
            for m in day_messages:
                body_parts.append(self._render_message(m, ctx))

        time_range = ""
        if messages:
            time_range = (
                f"{messages[0].timestamp:%Y-%m-%d %H:%M} ~ {messages[-1].timestamp:%Y-%m-%d %H:%M}"
            )

        replacements = {
            "GROUP_NAME": html.escape(ctx.group_name),
            "GROUP_ID": str(ctx.group_id),
            "BACKUP_LABEL": f"备份 {html.escape(ctx.backup_id)}" if ctx.backup_id else "",
            "MEMBER_COUNT": str(len(ctx.members)),
            "MESSAGE_COUNT": str(len(messages)),
            "TIME_RANGE": html.escape(time_range),
            "SENDER_OPTIONS": "".join(
                f'<option value="{p["user_id"]}">{html.escape(p["name"])}（{p["count"]}）</option>'
                for p in participants
            ),
            "DAY_NAV": "".join(
                f'<a href="#day-{html.escape(day)}">{html.escape(day)}<span>{len(msgs)}</span></a>'
                for day, msgs in days
            ),
            "MESSAGES_HTML": "\n".join(body_parts) or '<div class="empty">该备份没有任何消息</div>',
        }
        out = template
        for key, value in replacements.items():
            out = out.replace("{{" + key + "}}", value)
        return out

    # ---------- 单条消息 ----------
    def _render_message(self, m, ctx: ExportContext) -> str:  # noqa: C901 - 单条消息的分支就是多
        mine = self.own_user_id is not None and m.sender_id == self.own_user_id
        initial = html.escape((m.sender_name or str(m.sender_id))[:1].upper())

        bubble_parts: list[str] = []
        if m.reply_to:
            bubble_parts.append(f'<div class="quote">引用 {html.escape(str(m.reply_to))}</div>')
        media_html = [self._render_media(media, ctx) for media in m.media_paths]
        media_html = [h for h in media_html if h]
        if m.content:
            bubble_parts.append(html.escape(m.content))
        elif not media_html and m.message_type.value in _TYPE_LABEL:
            # `[图片]` 这种占位只在**什么都没有**的时候补。只要媒体位置里有内容
            # （图、链接、甚至一句「本机找不到」），再补一句「[图片]」就是同一件事
            # 说两遍
            bubble_parts.append(_TYPE_LABEL[m.message_type.value])
        # 搜索词也要能搜到「谁说的」：侧栏写着「搜索昵称或内容…」，而 `data-text`
        # 原来只放正文，搜「阿七」一条都搜不到。文件名也放进去——那种只发了个
        # 附件、正文空白的消息，否则整条消息在搜索里是隐形的。

        if m.is_forward and m.forward_chain:
            nodes = "".join(
                f'<div class="node"><strong>{html.escape(str(n.get("sender_name", "")))}</strong>：'
                f'{html.escape(str(n.get("content", "")))}</div>'
                for n in m.forward_chain
            )
            bubble_parts.append(
                f'<details class="forward"><summary>合并转发 {len(m.forward_chain)} 条</summary>{nodes}</details>'
            )

        bubble_parts.extend(media_html)

        searchable = " ".join(
            [
                m.sender_name or str(m.sender_id),
                m.content or "",
                *(_display_name(media, ctx) for media in m.media_paths if media),
            ]
        ).lower()
        text_for_search = html.escape(searchable)

        return (
            f'<div class="msg{" mine" if mine else ""}" data-sender="{m.sender_id}" data-text="{text_for_search}">'
            f'<div class="avatar">{initial}</div>'
            f'<div class="body"><div class="who"><span>{html.escape(m.sender_name or str(m.sender_id))}</span>'
            f'<span>{m.timestamp:%H:%M:%S}</span></div>'
            f'<div class="bubble">{"".join(bubble_parts)}</div></div></div>'
        )

    def _render_media(self, media: str, ctx: Optional[ExportContext] = None) -> str:
        """一条媒体怎么显示。

        三种素材源，按可信度排：

        - **本机文件**（`media_index` 里查得到，或库里的路径正好存在）→ 图片内联成
          `data:`，其它类型给个链接（链接只能来自 `ctx.media_links`，见下）；
        - **下载失败**：消息里留的是 `[下载失败] <原始 URL>`。这不是文件，是一个
          带标记的字符串——原来直接把它当 href 塞进 `<a>`，点开是一串乱码路径。
          原始 URL 才是唯一还能用的线索，做成链接；
        - **只剩一个字符串**：既不在本机、也没有可用地址。给一个不可点的标签
          并说明原因，比给一条 404 链接诚实。
        """
        if not media:
            return ""
        failed = _failed_url(media)
        name = html.escape(_display_name(media, ctx))
        if failed:
            if failed.startswith(("http://", "https://")):
                return (
                    f'<a class="file failed" href="{html.escape(failed)}" '
                    f'target="_blank" rel="noopener nofollow">↗ {name}'
                    f'<span class="hint">原始链接</span></a>'
                )
            return f'<span class="file failed">{name}<span class="hint">下载失败</span></span>'

        p = self._local_path(media, ctx)
        is_image = p.suffix.lower() in IMAGE_SUFFIXES
        href = ""
        if ctx is not None:
            href = str(ctx.media_links.get(media) or ctx.media_links.get(str(media)) or "")

        has_local = p.is_file()

        if is_image:
            data_uri = self._inline(p) if has_local else None
            if data_uri:
                return f'<img class="media" src="{data_uri}" alt="{name}" loading="lazy">'
            if href:
                # 没内联（超过单张上限或整份预算）、但包里带着：给包内相对链接，
                # 解压后点得动
                return (
                    f'<a class="file" href="{html.escape(href)}" target="_blank" '
                    f'rel="noopener">🖼 {name}</a>'
                )
            if has_local:
                return (
                    f'<span class="file missing">🖼 {name}'
                    f'<span class="hint">体积过大，未内联</span></span>'
                )
            return (
                f'<span class="file missing">🖼 {name}'
                f'<span class="hint">本机找不到这个文件</span></span>'
            )

        if href:
            return (
                f'<a class="file" href="{html.escape(href)}" target="_blank" '
                f'rel="noopener">📎 {name}</a>'
            )
        if has_local:
            # 单文件 HTML 带不走非图片附件。说清楚，别让人对着一条点不动的
            # 文件名猜「是不是坏了」——要附件就让 ta 用 zip 导出
            return (
                f'<span class="file missing">📎 {name}'
                f'<span class="hint">未随本页导出，需要附件请用 zip</span></span>'
            )
        return (
            f'<span class="file missing">📎 {name}'
            f'<span class="hint">本机找不到这个文件</span></span>'
        )

    @staticmethod
    def _local_path(media: str, ctx: Optional[ExportContext]) -> Path:
        """媒体在导出机上的真实路径。

        库里存的是下载机的绝对路径（`~/.astrbot-backup/media/...`）。换台机器导出
        时那个路径不存在，所以先用 `ctx.media_index`（按原始路径索引的本地副本），
        再退回原路径。**这个路径只用来读文件，不能写进 HTML 当链接**——它在别人
        机器上必然打不开（第 38 轮的 bug 就是这么来的，见 DESIGN.md）。
        """
        if ctx is not None and ctx.media_index:
            hit = ctx.media_index.get(media) or ctx.media_index.get(str(media))
            if hit:
                return Path(hit)
        return Path(media)

    def _inline(self, path: Path) -> Optional[str]:
        """把一张图变成 `data:` URI。超单张上限或整份预算时返回 None。

        预算按**原始字节**累计（base64 会再涨 1/3），只要还在预算里就继续内联。
        早退回 None 不是失败，调用方会退化成链接——所以这里绝不能抛。
        """
        try:
            size = path.stat().st_size if path.is_file() else -1
        except OSError:
            return None
        if size < 0 or size > MAX_INLINE_BYTES:
            return None
        if self._inlined + size > self.max_inline_total:
            return None
        try:
            mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
            b64 = base64.b64encode(path.read_bytes()).decode("ascii")
        except OSError:
            return None
        self._inlined += size
        return f"data:{mime};base64,{b64}"


def _failed_url(media: str) -> Optional[str]:
    """媒体字符串是不是「下载失败」的标记；是就返回它记着的原始 URL。"""
    text = str(media or "")
    if text.startswith(_FAILED_MARK):
        return text[len(_FAILED_MARK):].strip() or None
    return None


def _display_name(media: str, ctx: Optional[ExportContext] = None) -> str:
    """给人看的文件名。

    路径可能是本机绝对路径、包内相对路径、或者一个原始 URL。三种都只取最后一段，
    且不能因为取不到就把井号/问号带出来（`http://x/a.png?sign=...` 会变成
    `a.png?sign=...`，导出页上很难看）。
    """
    failed = _failed_url(media)
    if failed:
        media = failed
    if ctx is not None and ctx.media_index:
        hit = ctx.media_index.get(media) or ctx.media_index.get(str(media))
        if hit:
            return Path(hit).name
    text = str(media or "")
    base = text.split("?", 1)[0].split("#", 1)[0].rstrip("/").replace("\\", "/")
    return base.rsplit("/", 1)[-1] or text or "附件"
