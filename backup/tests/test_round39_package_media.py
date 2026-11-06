"""第 39 轮：把导出包**当成一台新机器**用一遍。

第 38 轮的结论是「导出的东西没人真正打开过」。这轮往前一步：**打开之后呢**。
按「原机器没了、手里只有一个 zip」这个最要紧的场景走：

    解压 → 点开 chat.html → 用 txt/csv/markdown 归档 → 恢复回新机器 → 再导一次

三处都挂。两处是第 38 轮漏掉的同类问题，第三处是这插件存在的唯一理由。

1. **包里另外四份产物还在写导出机的绝对路径。** 第 38 轮只修了 `chat.html`，
   同一个包里的 `messages.txt` / `messages.csv` / `markdown/*.md` / `json/*.json`
   照旧写着 `/root/.astrbot-backup/media/...`。一份包、五个文件，一个能点、
   四个是死链——而 txt 和 markdown 正是「归档进 Git」的推荐格式。
2. **从 ZIP 恢复，媒体一个字节都不落盘。** `RestoreEngine` 只搬消息行，
   `media_paths` 原样保留导出机路径。恢复回执说「写入 3 条」，打开聊天记录
   每张图都是「本机找不到这个文件」。
3. **更糟的是这次恢复把媒体永久弄丢。** 第三次导出时本机找不到文件、
   `media_files` 也是空的，新包里 `media_files: []`、一张图都没有。
   原包一删，群里的图片和文件就真的没了——而「群被炸了靠它找回来」
   正是这个插件存在的理由。

第 1 项的验证方式是「把产物当文本看，里面不许出现导出机的目录」；
第 2、3 项是「在 tmp 目录里造一台新机器，恢复完真的去 `media/` 下找文件」。
"""

import asyncio
import json
import zipfile
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from backup.core.database import Database
from backup.core.export_manager import ExportManager
from backup.core.models import (
    BackupRecord,
    BackupStatus,
    GroupInfo,
    GroupMember,
    Message,
    MessageType,
)
from backup.core.restore_engine import RestoreEngine
from backup.exporters import get_exporter
from backup.exporters.base import ExportContext

# 用 `getattr` 取：这两个助手是这轮新加的。直接 import 会让旧代码在**收集期**就
# ImportError，整轮一条用例都不跑——那样「旧代码上是红的」就变成「旧代码上根本
# 跑不起来」，看不出到底哪条契约没兑现。
_base = __import__("backup.exporters.base", fromlist=["base"])
media_member_name = getattr(
    _base,
    "media_member_name",
    lambda message_id, media: f"media/20240501/{Path(str(media)).name}",
)

GROUP = 100
PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 64
PDF = b"%PDF-1.4 fake"


def _media(tmp: Path) -> tuple[Path, Path]:
    day = tmp / "srcmedia" / "20240501"
    day.mkdir(parents=True, exist_ok=True)
    png = day / "photo.png"
    png.write_bytes(PNG)
    pdf = day / "报告.pdf"
    pdf.write_bytes(PDF)
    return png, pdf


def _seed(db: Database, tmp: Path, backup_id: str = "bk1") -> list[Message]:
    """三条消息：纯文本、一张图、一个附件。媒体文件真在本机。"""
    png, pdf = _media(tmp)
    base = datetime(2024, 5, 1, 10, 0, 0)
    messages = [
        Message(
            message_id="m1", group_id=GROUP, sender_id=1, sender_name="阿七",
            timestamp=base, message_type=MessageType.TEXT,
            content="大家早上好", raw_content="{}",
        ),
        Message(
            message_id="m2", group_id=GROUP, sender_id=2, sender_name="小明",
            timestamp=base + timedelta(minutes=1), message_type=MessageType.IMAGE,
            content="", raw_content="{}", media_paths=[str(png)],
        ),
        Message(
            message_id="m3", group_id=GROUP, sender_id=3, sender_name="小红",
            timestamp=base + timedelta(minutes=2), message_type=MessageType.FILE,
            content="这是报告", raw_content="{}", media_paths=[str(pdf)],
        ),
    ]
    for m in messages:
        db.save_message(m, backup_id)
    db.add_sources(backup_id, messages)
    db.save_record(
        BackupRecord(
            backup_id=backup_id, group_id=GROUP, status=BackupStatus.COMPLETED,
            started_at=base, finished_at=base + timedelta(minutes=5),
            message_count=3, media_count=2, total_size=len(PNG) + len(PDF),
            is_incremental=False, base_backup_id=None, checksum="", error=None,
        )
    )
    db.save_group_info(GroupInfo(group_id=GROUP, group_name="测试群"))
    db.save_members(
        GROUP,
        [GroupMember(user_id=1, group_id=GROUP, nickname="阿七", role="owner")],
    )
    for m in messages:
        for media in m.media_paths:
            path = Path(media)
            db.save_media(
                message_id=m.message_id, group_id=GROUP, original_url=f"http://x/{path.name}",
                local_path=str(path), filename=path.name, file_size=path.stat().st_size,
                mime_type="application/octet-stream", sha256="",
            )
    return messages


@pytest.fixture
def package(db, tmp_path):
    """导出一份真 zip，返回 (zip 路径, 导出机媒体目录)。"""
    _seed(db, tmp_path)
    manager = ExportManager(db, tmp_path / "exports", tmp_path / "srcmedia", {})
    result = manager.export(GROUP, "zip", backup_id="bk1")
    return result.path, tmp_path / "srcmedia"


def _engine(db: Database, media_dir: Path | None = None) -> RestoreEngine:
    """造恢复引擎。旧代码不认 `media_dir`：那就明确报「这个契约没实现」。

    直接让它抛 `TypeError` 也能红，但那是**用例自己塌了**——看不出到底哪条契约
    没兑现，跟「恢复完媒体没落盘」是两件事。
    """
    if media_dir is None:
        return RestoreEngine(db)
    try:
        return RestoreEngine(db, media_dir)
    except TypeError:
        pytest.fail("RestoreEngine 还不接受 media_dir —— 从包恢复时媒体不会落回本机")


def _read(zip_path: Path, name: str) -> str:
    with zipfile.ZipFile(zip_path) as zf:
        return zf.read(name).decode("utf-8")


# ------------------------------------------------ 1. 包里四份产物的媒体地址


def test_txt_media_lines_point_inside_package(package):
    """`messages.txt` 里的附件行必须是包内地址。

    旧代码写 `media_paths` 原值——导出机的绝对路径。这份 txt 是「归档进 Git」
    的推荐格式，换台机器打开、照着这行去找文件，永远找不到。
    """
    zip_path, media_dir = package
    text = _read(zip_path, "messages.txt")
    lines = [ln.strip() for ln in text.splitlines() if "<附件>" in ln]
    assert len(lines) == 2, text
    for line in lines:
        assert not str(media_dir) in line, f"txt 里留着导出机路径：{line}"
        assert "media/20240501/" in line, line


def test_csv_media_column_points_inside_package(package):
    """`messages.csv` 的「媒体文件」列必须能用（Excel 打开找不到原机）。"""
    zip_path, media_dir = package
    text = _read(zip_path, "messages.csv")
    assert not str(media_dir) in text, "CSV 里留着导出机路径"
    assert "media/20240501/" in text


def test_markdown_media_links_are_siblings_of_the_page(package):
    """`markdown/<日期>.md` 里的图片/附件要走 `../` 回到包根。

    少了这层 `../`，地址会被解析成 `markdown/media/...`——一样打不开。
    只把绝对路径换成 `media/...` 是不够的，这一步是这轮里最容易漏的。
    """
    zip_path, media_dir = package
    text = _read(zip_path, "markdown/2024-05-01.md")
    assert not str(media_dir) in text, "markdown 里留着导出机路径"
    assert "](../media/20240501/" in text, text
    assert "media/20240501/" in text


def test_markdown_media_links_resolve_from_its_own_directory(package, tmp_path):
    """真的去「解压后按相对路径找文件」，文件必须在。"""
    zip_path, _ = package
    out = tmp_path / "unzipped"
    with zipfile.ZipFile(zip_path) as zf:
        zf.extractall(out)
    md = out / "markdown" / "2024-05-01.md"
    links = [
        seg.split("](")[1].rstrip(")")
        for seg in md.read_text(encoding="utf-8").splitlines()
        if "](" in seg and "media/" in seg
    ]
    assert links, "markdown 里一条媒体链接都没有"
    for link in links:
        resolved = (md.parent / link).resolve()
        assert resolved.is_file(), f"{link} 从 {md.parent} 出发找不到"


def test_json_media_paths_are_package_relative(package):
    """`json/` 是「适合程序处理」的那份，读它的人也要能定位文件。

    `media_paths` 换成包内地址。只留原值的话，任何照着这份 JSON 读文件的程序
    都只能拿到 ENOENT——而这份 JSON 存在的意义就是给程序读。
    """
    zip_path, media_dir = package
    shard = json.loads(_read(zip_path, "json/daily/2024-05-01.json"))
    by_id = {m["message_id"]: m for m in shard}
    assert not by_id["m1"].get("media_paths"), "没有媒体的消息不该凭空多出一列"
    for mid in ("m2", "m3"):
        paths = by_id[mid].get("media_paths") or []
        assert paths, f"{mid} 没有 media_paths"
        for link in paths:
            assert link.startswith("media/"), link
            assert not str(media_dir) in link


def test_no_producer_writes_the_exporting_machine_path(package):
    """一口气把包内所有文本产物扫一遍：导出机的媒体目录一个字节都不许留。"""
    zip_path, media_dir = package
    with zipfile.ZipFile(zip_path) as zf:
        names = zf.namelist()
        for name in names:
            if not name.endswith((".txt", ".csv", ".md", ".json", ".html")):
                continue
            body = zf.read(name).decode("utf-8")
            assert str(media_dir) not in body, f"{name} 里写着导出机路径"


# ------------------------------------------------ 2. 从 zip 恢复，媒体要落回本机


def test_restore_brings_media_onto_this_machine(package, tmp_path):
    """恢复完，`media/` 下必须真有这两个文件，`media_paths` 指向本机。"""
    zip_path, _ = package
    fresh = Database(tmp_path / "new" / "db" / "b.sqlite")
    media_dir = tmp_path / "new" / "media"
    result = asyncio.run(
        _engine(fresh, media_dir).restore("bk1", GROUP, archive=zip_path, confirm=True)
    )

    files = sorted(p.name for p in media_dir.rglob("*") if p.is_file())
    assert files, "媒体一个都没落盘"
    assert len(files) == 2, files
    assert getattr(result, "media_restored", 0) == 2, f"恢复回执没说媒体回来了：{result}"
    for m in fresh.get_messages(group_id=GROUP):
        for media in m.media_paths:
            assert Path(media).is_file(), f"{m.message_id} 的媒体指向不存在的 {media}"
            assert str(media_dir) in media, f"{m.message_id} 的媒体没改成本机路径：{media}"


def test_restore_media_survives_a_re_export(package, tmp_path):
    """恢复 → 再导一次，新包里必须还有媒体。

    这是最要紧的一条。旧代码恢复后本机没有任何媒体文件、`media_files` 也是空的，
    第二次导出 `media_files: []`、一张图都没有——**原包一删，群里的图片就永久没了**。
    """
    zip_path, _ = package
    fresh = Database(tmp_path / "new" / "db" / "b.sqlite")
    media_dir = tmp_path / "new" / "media"
    asyncio.run(
        _engine(fresh, media_dir).restore("bk1", GROUP, archive=zip_path, confirm=True)
    )

    again = ExportManager(fresh, tmp_path / "new" / "exports", media_dir, {}).export(GROUP, "zip")
    with zipfile.ZipFile(again.path) as zf:
        manifest = json.loads(zf.read("manifest.json"))
        members = [n for n in zf.namelist() if n.startswith("media/")]
    assert len(members) == 2, f"二次导出的包里没有媒体：{members}"
    assert len(manifest["media_files"]) == 2


def test_restore_media_names_do_not_compound(package, tmp_path):
    """恢复→导出→恢复……转几圈，包内文件名不能越滚越长。

    恢复出来的文件本来就叫 `<message_id>_<原名>`，导出时再拼一次前缀就成了
    `m2_m2_m2_photo.png`，每转一圈长一截。
    """
    zip_path, _ = package
    current = zip_path
    for round_no in range(3):
        fresh = Database(tmp_path / f"r{round_no}" / "db" / "b.sqlite")
        media_dir = tmp_path / f"r{round_no}" / "media"
        asyncio.run(
            _engine(fresh, media_dir).restore("bk1", GROUP, archive=current, confirm=True)
        )
        current = ExportManager(fresh, tmp_path / f"r{round_no}" / "exports", media_dir, {}).export(
            GROUP, "zip"
        ).path
        with zipfile.ZipFile(current) as zf:
            members = sorted(n for n in zf.namelist() if n.startswith("media/"))
        assert members == [
            "media/20240501/m2_photo.png",
            "media/20240501/m3_报告.pdf",
        ], f"第 {round_no + 1} 轮：{members}"


def test_restore_registers_media_rows(package, tmp_path):
    """落盘的媒体要登记进 `media_files`，否则 `/backup clean` 认不出它该归谁。"""
    zip_path, _ = package
    fresh = Database(tmp_path / "new" / "db" / "b.sqlite")
    media_dir = tmp_path / "new" / "media"
    asyncio.run(
        _engine(fresh, media_dir).restore("bk1", GROUP, archive=zip_path, confirm=True)
    )
    rows = fresh.query("SELECT local_path FROM media_files")
    assert len(rows) == 2, rows
    for row in rows:
        assert Path(row["local_path"]).is_file()


def test_restore_queryable_media_rows_are_in_media_dir(package, tmp_path):
    """登记进去的路径必须落在恢复端自己的媒体目录里，不能指回原机器。"""
    zip_path, _ = package
    fresh = Database(tmp_path / "new" / "db" / "b.sqlite")
    media_dir = tmp_path / "new" / "media"
    asyncio.run(
        _engine(fresh, media_dir).restore("bk1", GROUP, archive=zip_path, confirm=True)
    )
    for row in fresh.query("SELECT local_path FROM media_files"):
        assert str(media_dir) in row["local_path"], row["local_path"]


def test_restore_without_media_dir_still_restores_messages(package, tmp_path):
    """没给媒体目录时退回旧行为（只恢复消息），不能因此抛错。

    `/backup restore` 之外还有别的调用方（单测、脚本）。多一个必填参数等于把它们
    全弄坏；这个能力是**增强**，缺席时降级而不是失败。
    """
    zip_path, _ = package
    fresh = Database(tmp_path / "new" / "db" / "b.sqlite")
    result = asyncio.run(
        _engine(fresh).restore("bk1", GROUP, archive=zip_path, confirm=True)
    )
    assert result.inserted == 3
    assert getattr(result, "media_restored", 0) == 0


def test_missing_media_in_package_keeps_the_marker(package, tmp_path):
    """包里本来就没有那份媒体时，原值原样留着。

    恢复时把找不到的媒体洗成空列表，等于把「附件丢了」这件事抹掉——用户之后
    再导出，页面上一句提示都没有，看起来一切正常。
    """
    zip_path, _ = package
    # 重新打一个包，抽掉图片，并且**连清单一起改**——不然 `verify_zip` 会在
    # 「包内缺媒体」这一步就直接拦下恢复（那是另一条已经成立的契约）。
    # 这里要验的是「清单没登记、包里也没有」这种更隐蔽的情况
    stripped = tmp_path / "stripped.zip"
    with zipfile.ZipFile(zip_path) as src:
        items = {name: src.read(name) for name in src.namelist()}
    manifest = json.loads(items["manifest.json"])
    manifest["media_files"] = [n for n in manifest["media_files"] if not n.endswith(".png")]
    items["manifest.json"] = json.dumps(manifest, ensure_ascii=False).encode()
    with zipfile.ZipFile(stripped, "w") as dst:
        for name, body in items.items():
            if name.startswith("media/") and name.endswith(".png"):
                continue
            dst.writestr(name, body)

    fresh = Database(tmp_path / "new" / "db" / "b.sqlite")
    media_dir = tmp_path / "new" / "media"
    asyncio.run(
        _engine(fresh, media_dir).restore("bk1", GROUP, archive=stripped, confirm=True)
    )
    image = next(m for m in fresh.get_messages(group_id=GROUP) if m.message_id == "m2")
    assert image.media_paths, "缺失的媒体被洗成了空列表"
    assert not Path(image.media_paths[0]).is_file()


def test_two_messages_sharing_one_media_file_both_resolve(tmp_path, db):
    """两条消息指向同一个媒体文件时，恢复后两条都要找得到。

    导出端按「媒体原值」去重——同一份文件不该在包里存两份。于是包里只有
    `<第一条消息的 id>_<文件名>`，而恢复端按各自 message_id 算成员名，第二条
    算出来的名字在包里不存在，媒体就退化成「本机找不到这个文件」。

    现实里这很常见：同一条消息被实时归档和定时备份各记过一次、或者转发同一张图。
    """
    png = tmp_path / "srcmedia" / "20240501" / "photo.png"
    png.parent.mkdir(parents=True)
    png.write_bytes(PNG)
    base = datetime(2024, 5, 1, 10, 0, 0)
    messages = [
        Message(
            message_id=mid, group_id=GROUP, sender_id=1, sender_name="阿七",
            timestamp=base, message_type=MessageType.IMAGE, content="",
            raw_content="{}", media_paths=[str(png)],
        )
        for mid in ("m2", "m2b")
    ]
    for m in messages:
        db.save_message(m, "bk1")
    db.add_sources("bk1", messages)
    db.save_record(
        BackupRecord(
            backup_id="bk1", group_id=GROUP, status=BackupStatus.COMPLETED,
            started_at=base, finished_at=base, message_count=2, media_count=1,
            total_size=len(PNG), is_incremental=False, base_backup_id=None,
            checksum="", error=None,
        )
    )
    db.save_media(
        message_id="m2", group_id=GROUP, original_url="http://x/photo.png",
        local_path=str(png), filename="photo.png", file_size=len(PNG),
        mime_type="image/png", sha256="",
    )
    package = ExportManager(db, tmp_path / "exports", tmp_path / "srcmedia", {}).export(
        GROUP, "zip", backup_id="bk1"
    ).path

    fresh = Database(tmp_path / "new" / "db" / "b.sqlite")
    media_dir = tmp_path / "new" / "media"
    result = asyncio.run(
        _engine(fresh, media_dir).restore("bk1", GROUP, archive=package, confirm=True)
    )
    assert result.media_restored == 2, f"有一份媒体没落盘：{result}"
    for m in fresh.get_messages(group_id=GROUP):
        assert m.media_paths, f"{m.message_id} 的媒体丢了"
        assert Path(m.media_paths[0]).is_file(), f"{m.message_id} → {m.media_paths[0]}"


# ------------------------------------------------ 3. 命名规则两边必须一致


def test_member_name_is_idempotent():
    """`media_member_name` 对已经带过前缀的名字不再加一次。"""
    assert media_member_name("m2", "/a/20240501/photo.png") == "media/20240501/m2_photo.png"
    assert media_member_name("m2", "/a/20240501/m2_photo.png") == "media/20240501/m2_photo.png"


def test_export_and_restore_agree_on_member_name(db, tmp_path):
    """导出端塞进包里的名字，恢复端必须能按同一个名字找到。

    两边各写一遍是这类 bug 的老家：导出按 A 命名、恢复按 B 去找，找不到就静默
    跳过——包里有文件、库里没路径，看着像「恢复成功」。
    """
    _seed(db, tmp_path)
    result = ExportManager(db, tmp_path / "exports", tmp_path / "srcmedia", {}).export(GROUP, "zip")
    with zipfile.ZipFile(result.path) as zf:
        members = {n for n in zf.namelist() if n.startswith("media/")}
    expected = {
        media_member_name(m.message_id, media)
        for m in db.get_messages(group_id=GROUP)
        for media in m.media_paths
    }
    assert members == expected


# ------------------------------------------------ 4. 单格式导出：没有可用地址时明说


def test_single_file_txt_does_not_invent_a_link(tmp_path):
    """单文件 txt 导出没有包内地址，就该只写文件名，不能编一条假链接。"""
    png = tmp_path / "x.png"
    png.write_bytes(PNG)
    ctx = ExportContext(
        messages=[
            Message(
                message_id="m1", group_id=GROUP, sender_id=1, sender_name="阿七",
                timestamp=datetime(2024, 5, 1), message_type=MessageType.IMAGE,
                content="", raw_content="{}", media_paths=[str(png)],
            )
        ],
        backup_id="bk1",
    )
    path = get_exporter("txt").export(ctx, tmp_path / "out.txt")
    text = path.read_text(encoding="utf-8")
    assert "x.png" in text
    assert str(tmp_path) not in text, "单文件 txt 里写着本机绝对路径"


def test_single_file_markdown_does_not_invent_a_link(tmp_path):
    png = tmp_path / "x.png"
    png.write_bytes(PNG)
    ctx = ExportContext(
        messages=[
            Message(
                message_id="m1", group_id=GROUP, sender_id=1, sender_name="阿七",
                timestamp=datetime(2024, 5, 1), message_type=MessageType.IMAGE,
                content="", raw_content="{}", media_paths=[str(png)],
            )
        ],
        backup_id="bk1",
    )
    out = get_exporter("markdown").export(ctx, tmp_path / "md")
    text = (out / "2024-05-01.md").read_text(encoding="utf-8")
    assert "x.png" in text
    assert str(tmp_path) not in text
    assert "未随本包导出" in text


def test_failed_download_marker_still_shows_original_url(tmp_path):
    """`[下载失败] <url>` 在任何产物里都要留下原始 URL——那是唯一还能追的线索。"""
    ctx = ExportContext(
        messages=[
            Message(
                message_id="m1", group_id=GROUP, sender_id=1, sender_name="阿七",
                timestamp=datetime(2024, 5, 1), message_type=MessageType.FILE,
                content="", raw_content="{}",
                media_paths=["[下载失败] http://cdn/x.pdf?sign=1"],
            )
        ],
        backup_id="bk1",
    )
    path = get_exporter("txt").export(ctx, tmp_path / "out.txt")
    text = path.read_text(encoding="utf-8")
    assert "http://cdn/x.pdf" in text
    assert "sign=1" in text, "原始 URL 不能被截断，签名参数才是能用它下载的那部分"
