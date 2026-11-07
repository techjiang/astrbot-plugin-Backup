"""第 38 轮：拿真导出包在真浏览器里点一遍。

前 37 轮验导出，验的都是「文件生成了、里面有这句话」。没人**点**过它。
这轮把真 Chrome 打上去，按群成员的动作走：解压、翻记录、点图片、点附件、
在搜索框里敲昵称。四处全挂，而且都在 HTML 导出这条最显眼的产出上。

1. **附件链接指着导出机的绝对路径。** ZIP 里的 `chat.html` 写的是
   `/root/.astrbot-backup/media/20240501/m4_ghi.pdf`——解压到任何别的机器上
   都是一条死链。包的卖点是「解开就能看」，而附件是包里唯一带不走的证据。
2. **「点击放大」用的是 `window.open(data:...)`。** 主流浏览器一律按弹窗拦掉，
   点了毫无反应；缩略图本身又小，等于图片只能看个大概。
3. **搜索框写着「搜索昵称或内容」，`data-text` 里只有内容。** 搜「阿七」一条
   都搜不出来。更糟的是 `hidden` 属性被 `.msg{display:flex}` 盖掉：消息一条
   没消失，中间却冒出一句「没有匹配的消息」。
4. **`[下载失败] <url>` 被当成文件路径塞进 `<a href>`。** 点开是一串乱码。

另外补一条预算：单张 6MB 的上限挡不住「一天两百张合格的图」，整份 HTML 可以
涨到几百 MB，浏览器直接卡死。
"""

import re
import zipfile
from datetime import datetime
from pathlib import Path

import pytest

from backup.core.export_manager import ExportManager
from backup.core.models import BackupRecord, BackupStatus, GroupInfo, GroupMember, Message, MessageType
from backup.exporters.base import ExportContext
from backup.exporters import html_exporter as _html_exporter
from backup.exporters.html_exporter import HtmlExporter

#: 用 `getattr` 而不是直接 import：这几个名字是新加的，直接 import 会让旧代码上
#: **收集期**就 ImportError，整轮用例一条都不跑——那样「旧代码上是红的」这句话
#: 就变成了「旧代码上根本跑不了」，看不出到底哪条契约没兑现。
MAX_INLINE_TOTAL_BYTES = getattr(_html_exporter, "MAX_INLINE_TOTAL_BYTES", None)


PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 64


def _exporter(**kwargs):
    """造 exporter。旧代码不认 `max_inline_total`：那就明确报「这个契约没实现」。"""
    try:
        return HtmlExporter(**kwargs)
    except TypeError:
        pytest.fail("HtmlExporter 还不接受 max_inline_total —— 整份内联预算没实现")


def _png(tmp: Path, name: str = "a.png") -> Path:
    path = tmp / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(PNG)
    return path


def _ctx(tmp: Path, media: dict[str, str] | None = None):
    """三条消息：一张图、一个语音、一个文件，媒体都在本机。"""
    image = _png(tmp / "media" / "20240501", "m1.png")
    voice = tmp / "media" / "20240501" / "m2.amr"
    voice.write_bytes(b"#!AMR\n" + b"\x00" * 32)
    pdf = tmp / "media" / "20240501" / "m3.pdf"
    pdf.write_bytes(b"%PDF-1.4 fake")
    base = datetime(2024, 5, 1, 10, 0, 0)
    messages = [
        Message(
            message_id="m1", group_id=100, sender_id=1, sender_name="阿七", timestamp=base,
            message_type=MessageType.IMAGE, content="", raw_content="{}",
            media_paths=[str(image)],
        ),
        Message(
            message_id="m2", group_id=100, sender_id=2, sender_name="小满", timestamp=base,
            message_type=MessageType.VOICE, content="", raw_content="{}",
            media_paths=[str(voice)],
        ),
        Message(
            message_id="m3", group_id=100, sender_id=2, sender_name="小满", timestamp=base,
            message_type=MessageType.FILE, content="", raw_content="{}",
            media_paths=[str(pdf)],
        ),
    ]
    ctx = ExportContext(
        messages=messages,
        group_info=GroupInfo(group_id=100, group_name="测试群"),
        members=[GroupMember(user_id=1, group_id=100, nickname="阿七")],
        media_index={str(m.media_paths[0]): Path(m.media_paths[0]) for m in messages},
        backup_id="bk1",
    )
    # `media_links` 是第 38 轮加的字段。用 setattr 而不是构造参数：旧代码上
    # 也能把这个上下文跑进渲染，失败会落在**断言**上（链接指错地方），
    # 而不是 `TypeError: unexpected keyword argument`——后者只说明测试自己塌了。
    ctx.media_links = dict(media or {})
    return ctx


# ------------------------------------------------------------ 1. ZIP 里的链接


def test_zip_chat_html_links_point_inside_the_package(tmp_path, db):
    """ZIP 里 `chat.html` 的附件链接必须是**包内相对路径**。

    旧代码把 `media_paths` 的原值当 href 写进 HTML——那是导出机的绝对路径，
    `/root/.astrbot-backup/media/...`。解压到别人机器上，点开就是
    「无法访问此文件」，而文件明明就躺在 `media/` 目录里。
    """
    ctx = _ctx(tmp_path)
    for m in ctx.messages:
        db.save_message(m, "bk1")
    db.save_record(
        BackupRecord(
            backup_id="bk1", group_id=100, status=BackupStatus.COMPLETED,
            started_at=datetime(2024, 5, 1), finished_at=datetime(2024, 5, 1),
            message_count=3, media_count=3, total_size=10, is_incremental=False,
            base_backup_id=None, checksum="", error=None,
        )
    )
    manager = ExportManager(db, tmp_path / "exports", tmp_path / "media", {})
    result = manager.export(100, "zip", backup_id="bk1")

    with zipfile.ZipFile(result.path) as zf:
        html = zf.read("chat.html").decode("utf-8")
        names = set(zf.namelist())

    hrefs = re.findall(r'<a class="file" href="([^"]+)"', html)
    assert hrefs, "包里的 chat.html 连一个附件链接都没有"
    for href in hrefs:
        assert not href.startswith(("/", "file:")), f"附件链接还是绝对路径：{href}"
        assert href in names, f"附件链接指向包内不存在的成员：{href}"
    # 导出机的绝对路径一个字节都不该留在页面里
    assert str(tmp_path) not in html


def test_single_file_html_never_links_to_local_absolute_paths(tmp_path):
    """单文件导出带不走附件，那就**说清楚**，不能给一条别人打不开的链接。"""
    ctx = _ctx(tmp_path)
    html = HtmlExporter().render(ctx)
    assert str(tmp_path) not in html
    assert "/media/20240501" not in html
    assert "本机找不到这个文件" not in html  # 文件其实在，只是带不走
    assert html.count("未随本页导出") >= 2


# ------------------------------------------------------------ 2. 点图片


def test_template_does_not_use_window_open_for_media(tmp_path):
    """「点击放大」不能靠 `window.open(data:...)`。

    浏览器把 `window.open('data:...')` 当弹窗拦掉——点了毫无反应，用户以为
    图片坏了。模板改成自建覆盖层之后，这条断言守住它别被改回去。
    """
    text = (Path(__file__).resolve().parent.parent / "templates" / "chat.html").read_text(encoding="utf-8")
    assert "window.open" not in text
    assert 'id="lightbox"' in text
    assert ".on" in text


def test_template_hidden_attribute_actually_hides(tmp_path):
    """`hidden` 必须真的隐藏。

    `.msg{display:flex}` 的优先级高于 UA 样式表里的 `[hidden]{display:none}`，
    所以 `el.hidden = true` 写了等于没写。搜索时整页消息还在，中间却挂一句
    「没有匹配的消息」——自查里最难查的那种：JS 和 CSS 各自都对。
    """
    text = (Path(__file__).resolve().parent.parent / "templates" / "chat.html").read_text(encoding="utf-8")
    assert re.search(r"\[hidden\]\s*\{[^}]*display\s*:\s*none", text), "缺少 [hidden]{display:none}"


# ------------------------------------------------------------ 3. 搜索昵称


def test_data_text_covers_sender_name(tmp_path):
    """搜索框写着「搜索昵称或内容」，那就得连昵称一起搜。"""
    ctx = _ctx(tmp_path)
    html = HtmlExporter().render(ctx)
    values = re.findall(r'data-text="([^"]*)"', html)
    assert values, "一条 data-text 都没有"
    joined = " ".join(values)
    assert "阿七" in joined and "小满" in joined


def test_data_text_covers_attachment_name(tmp_path):
    """只发了个附件、正文空白的消息，搜索里不能是隐形的。"""
    ctx = _ctx(tmp_path)
    html = HtmlExporter().render(ctx)
    joined = " ".join(re.findall(r'data-text="([^"]*)"', html))
    assert "m3.pdf" in joined


def test_data_text_is_still_escaped(tmp_path):
    """加昵称不能顺手把转义丢了：昵称是用户可控的自由文本。"""
    msg = Message(
        message_id="x", group_id=100, sender_id=1,
        sender_name='<img src=x onerror="alert(1)">',
        timestamp=datetime(2024, 5, 1), message_type=MessageType.TEXT,
        content="正文", raw_content="{}",
    )
    html = HtmlExporter().render(
        ExportContext(messages=[msg], group_info=GroupInfo(group_id=100, group_name="群"))
    )
    assert '<img src=x' not in html   # 标签没被拼出来
    assert "&lt;img" in html           # 被转义成了文本
    assert 'onerror="' not in html     # 引号必须转义，否则能把属性截断


# ------------------------------------------------------------ 4. 下载失败的媒体


def test_failed_media_becomes_the_original_url(tmp_path):
    """`[下载失败] <url>` 是标记不是路径。链接要给原始 URL。"""
    msg = Message(
        message_id="f1", group_id=100, sender_id=1, sender_name="阿七",
        timestamp=datetime(2024, 5, 1), message_type=MessageType.IMAGE, content="",
        raw_content="{}",
        media_paths=["[下载失败] http://cdn.example.com/a.png?sign=abc"],
    )
    html = HtmlExporter().render(
        ExportContext(messages=[msg], group_info=GroupInfo(group_id=100, group_name="群"))
    )
    hrefs = re.findall(r'href="([^"]+)"', html)
    assert "http://cdn.example.com/a.png?sign=abc" in hrefs
    assert not any(h.startswith("[下载失败]") for h in hrefs)
    # 显示的是文件名，不是带签名参数的整条 URL
    label = re.search(r'<a class="file failed"[^>]*>(.*?)</a>', html).group(1)
    assert "a.png" in label
    assert "sign=" not in label and "http" not in label


def test_failed_media_without_url_is_not_clickable(tmp_path):
    msg = Message(
        message_id="f2", group_id=100, sender_id=1, sender_name="阿七",
        timestamp=datetime(2024, 5, 1), message_type=MessageType.FILE, content="",
        raw_content="{}", media_paths=["[下载失败] "],
    )
    html = HtmlExporter().render(
        ExportContext(messages=[msg], group_info=GroupInfo(group_id=100, group_name="群"))
    )
    assert "<a class=\"file" not in html
    assert "file failed" in html or "file missing" in html


# ------------------------------------------------------------ 5. 内联预算


def test_inline_budget_caps_the_page_size(tmp_path):
    """单张合格、加起来炸掉的情况。预算用完后剩下的图只留链接。"""
    big = b"\x89PNG\r\n\x1a\n" + b"x" * 4096
    messages = []
    index = {}
    for i in range(6):
        path = tmp_path / "media" / f"m{i}.png"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(big)
        index[str(path)] = path
        messages.append(
            Message(
                message_id=f"m{i}", group_id=100, sender_id=1, sender_name="阿七",
                timestamp=datetime(2024, 5, 1), message_type=MessageType.IMAGE,
                content="", raw_content="{}", media_paths=[str(path)],
            )
        )
    ctx = ExportContext(
        messages=messages,
        group_info=GroupInfo(group_id=100, group_name="群"),
        media_index=index,
    )
    ctx.media_links = {k: f"media/x{i}.png" for i, k in enumerate(index)}
    # 每张 4104 字节：预算 12288 正好放过两张，第三张开始退化成链接
    html = _exporter(max_inline_total=12288).render(ctx)
    assert html.count("<img class=\"media\"") == 2
    assert html.count('class="file"') >= 4
    # 超预算的那几张，链接必须指向包里那份，而不是本机路径
    assert str(tmp_path) not in html


def test_inline_budget_resets_between_renders(tmp_path):
    """同一个 exporter 连着渲染两次，第二份不能因为「上一份用掉了预算」而全部退化成链接。"""
    ctx = _ctx(tmp_path)
    exporter = HtmlExporter()
    first = exporter.render(ctx)
    second = exporter.render(ctx)
    assert first.count("<img class=\"media\"") == second.count("<img class=\"media\"") == 1


def test_inline_budget_default_is_a_real_number():
    """预算得是个具体的数，不能是 None——`None` 会让内联永不放行，图片全变链接。"""
    assert isinstance(MAX_INLINE_TOTAL_BYTES, int) and MAX_INLINE_TOTAL_BYTES > 0
