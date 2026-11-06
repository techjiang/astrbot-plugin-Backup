"""导出调度：统一入口 + ZIP 打包 + 带 token 的临时下载链接。

红线 3：导出文件落在 storage/exports，不放到 Web 可访问目录。
红线 4：链接必须带 token 且过期。
"""

import hashlib
import json
import secrets
import shutil
import time
import zipfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional

from ..exporters import get_exporter
from ..exporters.base import ExportContext
from ..exporters.html_exporter import HtmlExporter
from .database import REALTIME_BACKUP_ID, Database, group_info_from_dict, members_from_list
from .download_route import LEGACY_PREFIX
from .models import Message
from .verifier import (
    compute_checksum,
    compute_manifest_checksum,
    snapshot_evidence,
)

EXPIRY_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400}


def parse_expire(value) -> int:
    """'24h' → 86400 秒。

    写错或写成 0 一律退回 24 小时：0 秒等于「导出完链接就失效」，红线 4 要求的是
    带 token 且有期限，不是「给了个用不了的链接」。'30s' 这类秒级写法也认。
    """
    text = str(value or "24h").strip().lower()
    seconds = 0
    if text.isdigit():
        seconds = int(text)
    else:
        unit = text[-1]
        if unit in EXPIRY_UNITS:
            try:
                seconds = int(float(text[:-1]) * EXPIRY_UNITS[unit])
            except ValueError:
                seconds = 0
    return seconds if seconds > 0 else 24 * 3600


#: 同一次下载内允许的间隔。浏览器「探测 + 续传」、用户连点两下都在这个窗口内；
#: 超出就是第二个人拿着转发来的链接来领。
GRACE_SECONDS = 900


@dataclass
class ExportResult:
    format: str
    path: Path
    size: int
    message_count: int
    token: str
    expires_at: datetime
    base_url: str = ""
    # 这次导出的消息有多少条属于某一份备份（对应备份记录里的 package_count）
    package_record: str = ""

    @property
    def download_path(self) -> str:
        return f"/backup/download/{self.token}"

    @property
    def link(self) -> str:
        """直接可用的下载地址。没配 base_url 时只给路径。"""
        if self.base_url:
            return f"{self.base_url.rstrip('/')}/{self.token}"
        return self.download_path


class ExportManager:
    def __init__(
        self,
        db: Database,
        export_dir: str | Path,
        media_dir: str | Path,
        config: Optional[dict] = None,
    ):
        self.db = db
        self.export_dir = Path(export_dir)
        self.export_dir.mkdir(parents=True, exist_ok=True)
        self.media_dir = Path(media_dir)
        cfg = config or {}
        export_cfg = cfg.get("export") or {}
        self.expire_seconds = parse_expire(export_cfg.get("download_link_expire", "24h"))
        self.base_url = str(export_cfg.get("base_url") or "").rstrip("/")
        # 群里点的链接前缀：面板自己的公网地址 + `/api/backup/download`。插件启动后
        # 由 main.py 填进来（配置里不一定有），导出时 `link` 就用它拼
        self.public_prefix = ""
        # 插件路由在面板上的挂载前缀与尾段，注册路由时由 main.py 写进来
        self.route_prefix = ""
        self.route_suffix = "/backup/download"

    def set_base_url(self, value: str) -> None:
        """设置下载链接前缀（`<前缀>/<token>`）。"""
        self.base_url = str(value or "").rstrip("/")

    def set_route_prefix(self, value: str, suffix: str = "/backup/download") -> None:
        """插件路由在面板上的实际挂载前缀 + 尾段。

        新版面板把插件路由挂在 `/api/v1/plugins/extensions/<插件名>/` 下，旧版挂在
        `/api/plug/`。拼链接时前缀写死成旧版那个，群里点开就是 404——所以这个值由
        注册路由的那一步告诉我，而不是让我猜。
        """
        self.route_prefix = str(value or "").rstrip("/")
        self.route_suffix = str(suffix or "/backup/download")

    @property
    def download_prefix(self) -> str:
        """`<挂载前缀><route 的尾段>`：链接里路径的那一段。

        - 旧版面板：`/api/plug` + `/backup/download`；
        - 新版面板：`/api/v1/plugins/extensions/backup` + `/backup/download`
          （route 本身是 `/backup/backup/download`，插件名那层已经被挂载前缀吃掉）。

        插件名那层不能重复：`_match_registered_web_api` 是把它剥掉之后再去比
        route 的，所以同一个 route 在两种面板下形状不同。
        """
        prefix = str(self.route_prefix or LEGACY_PREFIX).rstrip("/")
        return f"{prefix}{self.route_suffix}"

    def build_context(self, group_id: int, backup_id: Optional[str] = None) -> ExportContext:
        """取导出用的数据。

        给了 backup_id 就按「来源清单」取这次备份覆盖到的消息。不能按
        `messages.backup_id` 取：那只是「最后归档它的那次备份」，一次全量重跑会把
        全群消息的归属全接手，老备份名下就空了——而恢复老备份恰恰要找它。

        群信息/成员优先用这份备份自己的快照（`backup_bundle`），没有才退回当前
        状态。换台机器恢复时靠的就是快照，否则导出包里群名和成员是空的。
        """
        if backup_id is None:
            messages = self.db.get_messages(group_id=group_id)
        else:
            messages = self.db.get_source_messages(backup_id, group_id)
            # 来源清单是「这次备份覆盖过哪些消息」，一次全量备份可能连带把库里别的群
            # 的旧数据也收了进来。导出某群的包却夹着别群的聊天记录，是隐私问题，
            # 不是数据完整性问题——按群筛一遍
            messages = [m for m in messages if m.group_id == group_id]
            if not messages:
                # 来源清单是后加的，升级前的老备份没有这份登记。空手而归说明这
                # 「不是真的没消息」，而是没有清单可查，退回按归属取
                messages = self.db.get_messages(group_id=group_id, backup_id=backup_id)
        group_info = self.db.get_group_info(group_id)
        members = self.db.get_members(group_id)
        if backup_id:
            bundle = self.db.get_bundle(backup_id, group_id)
            snapshot_info = group_info_from_dict(bundle.get("group_info") or {}, group_id)
            snapshot_members = members_from_list(bundle.get("members") or [], group_id)
            group_info = snapshot_info or group_info
            members = snapshot_members or members
        ctx = ExportContext(
            messages=messages,
            group_info=group_info,
            members=members,
            media_index=self._media_index(messages),
            backup_id=backup_id or "",
        )
        # 清单里那份「可信快照」按用户当前看到的群名/成员写：恢复时它会被当成
        # 「原机器上这份备份到底属于谁」来比对，用一份更旧的 bundle 写会跟导出
        # 页面上显示的内容不一致
        ctx.trusted = snapshot_of(group_info, members)
        return ctx

    def _media_index(self, messages: list[Message]) -> dict[str, Path]:
        """库里记录的媒体路径 → 本机真实文件。

        换台机器恢复后（或从导出包回灌）拿到的 `media_paths` 是原机器的绝对路径，
        本机上不存在。HTML/ZIP 之前只认那个路径，于是「自包含单文件」里的图片全成了
        断链。这里按 message_id 去 media_files 表找本机实际落盘的那份。
        """
        index: dict[str, Path] = {}
        for m in messages:
            for media in m.media_paths or []:
                if not media or str(media).startswith("[下载失败]") or str(media).startswith("http"):
                    continue
                if Path(str(media)).is_file():
                    continue
                for row in self.db.get_media_for_message(m.message_id):
                    local = Path(row["local_path"] or "")
                    if local.is_file():
                        index[str(media)] = local
                        break
        return index

    def export_latest(self, group_id: int, fmt: str, own_user_id: Optional[int] = None) -> ExportResult:
        """导出该群「最近一次备份」，外加只在实时归档里的那部分消息。

        群被炸的场景下，最后那几条消息只走了实时归档（backup_id='realtime'），
        它们不属于任何一次备份。只按 backup_id 取整份导出会刚好漏掉最要紧的一段。
        """
        record = self.db.latest_completed_record(group_id)
        ctx = self.build_context(group_id, record.backup_id if record else None)
        owned = {m.message_id for m in ctx.messages}
        extra = []
        if record is not None:
            # 一次备份按群全量跑完时接手了所有消息，更早的备份就只剩空壳；此时若
            # latest 只导自己名下那几条，等于把历史丢了。所以「上次备份名下已经
            # 没有消息」时，退回整群导出，再补上不属于任何备份的那些（实时归档）
            # 判「历史是不是空的」要看别的备份**名下**还剩什么，不是它们覆盖过
            # 什么：来源清单是后加的、且不会被后续备份擦掉，于是每个老备份都
            # 覆盖了满满一批消息，这个条件恒为 False——「上次备份接手了整群、
            # 老备份名下一个字都不剩」这种情况永远认不出来，导出 latest 只剩
            # 最近那两三条新增，看着像把历史导丢了
            history_hollow = not any(
                r.backup_id != record.backup_id
                and self.db.get_messages(group_id=group_id, backup_id=r.backup_id)
                for r in self.db.list_records(group_id)
            )
            for m in self.db.get_messages(group_id=group_id):
                if m.message_id in owned:
                    continue
                if history_hollow or self.db.message_backup_id(
                    m.message_id, group_id
                ) in ("", REALTIME_BACKUP_ID):
                    extra.append(m)
        ctx.messages = sorted(ctx.messages + extra, key=lambda m: (m.timestamp, m.message_id))
        # manifest 里的校验和跟着最终消息集走，否则恢复时会跟包内容对不上
        ctx.checksum = compute_checksum(ctx.messages)
        return self._finish(group_id, fmt, ctx, own_user_id)

    def export(
        self,
        group_id: int,
        fmt: str,
        backup_id: Optional[str] = None,
        own_user_id: Optional[int] = None,
    ) -> ExportResult:
        ctx = self.build_context(group_id, backup_id)
        return self._finish(group_id, fmt, ctx, own_user_id)

    def _finish(
        self,
        group_id: int,
        fmt: str,
        ctx: ExportContext,
        own_user_id: Optional[int] = None,
    ) -> ExportResult:
        fmt = (fmt or "sqlite").lower()
        if not ctx.messages:
            raise ValueError("该群没有可导出的消息")

        # 文件名精确到秒还不够：连点两次导出、或批量导出多个群，同一秒内就会撞在
        # 同一个路径上。后一份直接覆盖前一份，前一个 token 指向的是别人的文件。
        stamp = datetime.now().strftime("%Y%m%d%H%M%S")
        target = self.export_dir / f"{group_id}_{stamp}_{fmt}_{secrets.token_hex(3)}"

        if fmt == "zip":
            path = self._export_zip(ctx, target)
        elif fmt in ("html", "htm"):
            path = HtmlExporter(own_user_id=own_user_id).export(ctx, target.with_suffix(".html"))
        else:
            exporter = get_exporter(fmt)
            if fmt in ("json", "markdown", "md"):
                path = exporter.export(ctx, target)
            else:
                path = exporter.export(ctx, target.with_suffix(exporter.extension))

        if path.is_dir():
            # json / markdown 导出的是**目录**（按日期分片）。下载路由拿到的是一
            # 个目录：`Content-Type` 退成 octet-stream，读流时 `open(dir)` 直接
            # `IsADirectoryError`——浏览器那边就是「下了一半什么都没有」。
            # 用户点 `/backup export <群> json` 拿到的链接必须真能下东西，所以
            # 目录型产物在给链接之前先打成一个 zip；目录本身留着（本地翻着方便）。
            archived = path.with_suffix(".zip")
            with zipfile.ZipFile(archived, "w", zipfile.ZIP_DEFLATED) as zf:
                for file in path.rglob("*"):
                    if file.is_file():
                        zf.write(file, f"{path.name}/{file.relative_to(path)}")
            path = archived
        size = path.stat().st_size if path.is_file() else sum(
            f.stat().st_size for f in path.rglob("*") if f.is_file()
        )
        token, expires = self._issue_token(path)
        return ExportResult(
            format=fmt,
            path=path,
            size=size,
            message_count=len(ctx.messages),
            token=token,
            expires_at=expires,
            base_url=self.base_url,
            package_record=ctx.backup_id or "",
        )

    def _export_zip(self, ctx: ExportContext, base: Path) -> Path:
        """ZIP = 六种格式 + 媒体文件 + manifest。"""
        work = base / "_work"
        if work.exists():
            shutil.rmtree(work)
        work.mkdir(parents=True, exist_ok=True)
        get_exporter("sqlite").export(ctx, work / "messages.sqlite")
        get_exporter("json").export(ctx, work / "json")
        get_exporter("csv").export(ctx, work / "messages.csv")
        get_exporter("txt").export(ctx, work / "messages.txt")
        get_exporter("markdown").export(ctx, work / "markdown")
        # 包内媒体成员先定下来，再渲染 chat.html：页面里的附件链接必须指向**包内
        # 相对路径**。原来渲染用的是 `media_paths` 原值——那是导出机上的绝对路径
        # `/root/.astrbot-backup/media/...`，解压到任何别的机器上都是一条死链，
        # 而这个包的卖点正是「解开就能看」。
        media_members = self._collect_media_members(ctx)
        ctx.media_links = {
            key: member for key, (member, _path) in media_members.items()
        }
        (work / "chat.html").write_text(HtmlExporter().render(ctx), encoding="utf-8")
        # 公告 / 文件清单优先取**这份备份自己的快照**。取当前状态表的话，导出
        # 一份上个月的备份，带出去的却是这个群今天的公告——那不是备份，是现状。
        bundle = self.db.get_bundle(ctx.backup_id, ctx.group_id) if ctx.backup_id else {}
        announcements = [_jsonable(a) for a in (
            bundle.get("announcements") or self.db.get_announcements(ctx.group_id)
        )]
        group_files = [_jsonable(f) for f in (
            bundle.get("group_files") or self.db.get_group_files(ctx.group_id)
        )]
        if announcements:
            (work / "announcements.json").write_text(
                json.dumps(announcements, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        if group_files:
            (work / "group_files.json").write_text(
                json.dumps(group_files, ensure_ascii=False, indent=2), encoding="utf-8"
            )

        zip_path = base.with_suffix(".zip")
        record = self.db.get_record(ctx.backup_id) if ctx.backup_id else None
        manifest = {
            "backup_id": ctx.backup_id,
            # 记录里那份校验和：本地库留着的可信指纹，恢复端靠它认出「包被整体
            # 改写后重新算过校验和」这种情况。包里的 checksum 是包自己的，谁都能改
            "record_checksum": (record.checksum if record else "") or "",
            # ctx.group_id 在没有 group_info 时会退回「第一条消息的群号」，那不是
            # 这份备份的归属群。记录里有就先用记录，保证换机器恢复能定下目标群
            "group_id": (record.group_id if record else ctx.group_id),
            "group_name": ctx.group_name,
            "message_count": len(ctx.messages),
            "member_count": len(ctx.members),
            "exported_at": datetime.now().isoformat(),
            "checksum": ctx.checksum or compute_checksum(ctx.messages),
            # 换台机器恢复时本地库里没有这条记录、也没有这个群的任何行。包必须
            # 自带「我是谁、我给谁备的份」，否则 restore 会先卡在「备份不存在」。
            "record": _record_to_dict(record),
            "group_info": group_info_to_dict(ctx.group_info),
            "members": [member_to_dict(m) for m in ctx.members],
            # 群公告 / 群文件列表。这两样原来是没人采的空配置，现在采了就得能
            # 带走——否则换台机器恢复，公告和文件清单全留在原机上
            "announcements": announcements,
            "group_files": group_files,
            "author": {"name": "科技酱", "homepage": "https://www.asoe.cn"},
        }
        # 群名、群信息、成员表不在消息校验和的覆盖范围内，单独算一个指纹：
        # 否则改掉包里的群名和成员表，清单依然「自洽」，恢复时照单全收。
        #
        # 但指纹证明不了什么——它和包里的内容在同一只手里，改完重算一遍就自洽了。
        # 真正拦得住篡改的是**本机库里另存的那份证据**（`export_evidence` 表）：
        # 包作者能改包，改不了导出当时写进本机库的记录。所以带 backup_id 导出时，
        # 顺手把快照和它的指纹落到本地；恢复端只认本地这份，不认包里的 `trusted`。
        # 这份包到底装了几条消息，得写进**记录**里：导出包带的是「备份 + 只在实时
        # 归档里的那部分」，条数天然不等于记录里的 message_count。恢复端拿记录
        # 说「包里 1 条」，用户数得出 3 条——包没坏，是那句话在撒谎。
        if record is not None:
            self.db.set_package_count(record.backup_id, len(ctx.messages))
        manifest["trusted"] = ctx.trusted or self._trusted_snapshot(record, ctx.group_id)
        manifest["manifest_checksum"] = compute_manifest_checksum(manifest)
        if ctx.backup_id:
            self.db.save_export_evidence(
                ctx.backup_id,
                int(manifest["group_id"] or ctx.group_id),
                manifest["trusted"],
                snapshot_evidence(manifest["trusted"]),
            )
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for file in work.rglob("*"):
                if file.is_file():
                    zf.write(file, file.relative_to(work))
            for member, path in media_members.values():
                zf.write(path, member)
            # 媒体清单写进 manifest，校验时才知道包里该有哪些文件
            manifest["media_files"] = sorted({member for member, _ in media_members.values()})
            zf.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
        shutil.rmtree(work, ignore_errors=True)
        return zip_path

    def _collect_media_members(self, ctx: ExportContext) -> dict[str, tuple[str, Path]]:
        """这次导出要打进包的媒体：`media_paths 里的原值 → (包内成员名, 本机文件)`。

        只收本机真有、且在本插件媒体目录里的文件——`media_files` 表里可能留着
        「文件已经被 `/backup clean` 删了」的路径，收进清单会让校验报「包里缺
        媒体」，而实际只是淘汰了。同一条消息的同一个文件名只收一次。
        """
        out: dict[str, tuple[str, Path]] = {}
        for m in ctx.messages:
            for media in m.media_paths:
                key = str(media)
                if not key or key in out:
                    continue
                path = Path(ctx.media_index.get(key, key))
                if not path.is_file() or not self._is_inside(path, self.media_dir):
                    continue
                # 同名不同目录的媒体（同一天两条消息都叫 image.png）以前会压成
                # 同一个成员名，后写的覆盖前一个，包里的图张冠李戴
                member = str(Path("media") / path.parent.name / f"{m.message_id}_{path.name}")
                out[key] = (member, path)
        return out

    def _trusted_snapshot(self, record, group_id: int) -> dict:
        """导出时记下的「这份备份属于谁」：群名、群信息、成员表。

        只放本机库里的值，它随包一起走。恢复端有这条记录时以记录为准（包改了也
        没用），没有记录时（换机器）只能信包，至少指纹能对上。
        """
        snapshot: dict = {"group_id": int(group_id)}
        bundle = self.db.get_bundle(record.backup_id, group_id) if record else None
        info = group_info_from_dict((bundle or {}).get("group_info") or {}, group_id)
        if info is None:
            info = self.db.get_group_info(group_id)
        members = members_from_list((bundle or {}).get("members") or [], group_id)
        if not members:
            members = self.db.get_members(group_id)
        snapshot["group_info"] = group_info_to_dict(info)
        snapshot["members"] = [member_to_dict(m) for m in members]
        return snapshot

    @staticmethod
    def _is_inside(path: Path, root: Path) -> bool:
        try:
            path.resolve().relative_to(root.resolve())
            return True
        except ValueError:
            return False

    # ---------- 下载 token ----------
    def _issue_token(self, path: Path) -> tuple[str, datetime]:
        token = secrets.token_urlsafe(24)
        expires = datetime.now().timestamp() + self.expire_seconds
        self.db.save_token(token, str(path), expires)
        return token, datetime.fromtimestamp(expires)

    def resolve_token(self, token: str, shared: bool = False) -> Optional[Path]:
        """兑换 token。过期、越界、文件不存在一律返回 None。

        `shared=True` 用于分享给群成员的链接：带宽跑满时浏览器会发 Range 请求续
        传，那是第二次访问同一个 token。默认（下载路由）仍然一次性，链接转发出去
        就不能再被别人重复领走。

        「一次性」的判据是 `opened_at`（第一次兑换的时刻），不是「这个请求是
        整份还是片段」。原来按后者删 token，和续传天然打架：浏览器先发
        `Range: bytes=0-` 探测、再发真正的续传，两次都是片段，两次都不删 token
        ——于是链接转发出去第二个人也能整份下载，「仅可使用一次」是一句空话。
        片段这条路的理由（「探测后真正要数据那次拿到 410」）是对的，但解法错了。
        """
        if not token:
            return None
        row = self.db.get_token(token)
        if row is None:
            return None
        if float(row["expires"]) < time.time():
            self.db.delete_token(token)
            return None
        path = Path(row["path"])
        if not self._is_inside(path, self.export_dir) or not path.exists():
            return None
        if not shared:
            self.db.delete_token(token)
        return path

    def lookup_token(self, token: str) -> Optional[Path]:
        """只查、不占名额。调用方据此算出「这次请求要交付多少字节」。"""
        if not token:
            return None
        row = self.db.peek_token(token)
        if row is None or float(row["expires"]) < time.time():
            return None
        path = Path(row["path"])
        if not self._is_inside(path, self.export_dir) or not path.exists():
            return None
        return path

    def redeem_token(self, token: str, full: bool, now: Optional[float] = None) -> Optional[Path]:
        """兑换一次下载，返回文件路径。None = 该拒（410）。

        `full=True` 表示这次会把整个文件交出去：`DELETE` 就是抢占，只有一个赢家。
        `full=False` 是片段续传：只刷新会话窗口。

        判定全在 `Database.redeem_token` 的一把锁里做完。原来是「查有没有被领过 /
        打时间戳 / 删 token」三步，每步各自加锁，两个线程能交错着全通过——网关
        正是多线程的，链接转发进大群时并发是常态。
        """
        if not token:
            return None
        moment = time.time() if now is None else float(now)
        row = self.db.redeem_token(token, moment, GRACE_SECONDS, bool(full))
        if row is None:
            return None
        path = Path(row["path"])
        if not self._is_inside(path, self.export_dir) or not path.exists():
            return None
        return path

    def open_token(self, token: str, now: Optional[float] = None) -> bool:
        """占一个「片段」名额（不烧掉 token）。返回 False 表示已被别人领过。"""
        return self.redeem_token(token, full=False, now=now) is not None

    def take_token(self, token: str) -> Optional[Path]:
        """兑换并作废。一次性下载用。"""
        path = self.resolve_token(token)
        if path is not None:
            self.db.delete_token(token)
        return path

    def clean_exports(self, max_age_days: int = 1) -> int:
        """`max_age_days <= 0` 表示不淘汰：留空或写 0 的配置不该被理解成「立刻全删」。"""
        if not max_age_days or max_age_days <= 0:
            return 0
        """删过期 token 与过期导出文件。返回删除数量。"""
        now = time.time()
        # token 按真实时间过期。原先传的是「文件 mtime 的截止刻」，比 now 小一个
        # 保留期，于是 `expires < cutoff` 恒不成立，过期 token 一条也清不掉。
        removed = self.db.purge_expired_tokens(now)
        cutoff = now - max_age_days * 86400
        for f in self.export_dir.iterdir():
            try:
                if f.stat().st_mtime < cutoff:
                    shutil.rmtree(f) if f.is_dir() else f.unlink()
                    removed += 1
            except OSError:
                continue
        return removed

    @staticmethod
    def file_checksum(path: Path) -> str:
        h = hashlib.sha256()
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(1024 * 256), b""):
                h.update(chunk)
        return h.hexdigest()


def _jsonable(row: dict) -> dict:
    """sqlite3.Row 里的 datetime 不能直接 json 序列化。

    快照里的条目（`backup_bundle` 存下来的那批）是原样从解析器来的，时间字段
    可能还是 `datetime`；当前状态表读出来的则是字符串。同一条路径要能吃两种，
    否则「公告跟着包走」这条会在 json.dumps 上直接抛 TypeError。
    """
    return {
        k: (v.isoformat(sep=" ") if isinstance(v, datetime) else v)
        for k, v in dict(row).items()
    }


def _record_to_dict(record) -> Optional[dict]:
    if record is None:
        return None
    return {
        "backup_id": record.backup_id,
        "group_id": record.group_id,
        "status": record.status.value,
        "started_at": record.started_at.isoformat() if record.started_at else "",
        "finished_at": record.finished_at.isoformat() if record.finished_at else None,
        "message_count": record.message_count,
        "media_count": record.media_count,
        "total_size": record.total_size,
        "is_incremental": record.is_incremental,
        "base_backup_id": record.base_backup_id,
        "checksum": record.checksum,
    }


def snapshot_of(info, members) -> dict:
    """群信息 + 成员列表 → 可比较的规范化结构。

    导出端写「可信快照」和恢复端比对都用它，保证两边算的是同一件事。
    成员按 user_id 排序：平台返回顺序不稳定，顺序不同不代表内容不同。
    """
    return {
        "group_info": group_info_to_dict(info),
        "members": sorted(
            (member_to_dict(m) for m in members or []), key=lambda m: int(m["user_id"] or 0)
        ),
    }


def group_info_to_dict(info) -> Optional[dict]:
    if info is None:
        return None
    return {
        "group_id": info.group_id,
        "group_name": info.group_name,
        "owner_id": info.owner_id,
        "created_at": info.created_at.isoformat() if info.created_at else None,
        "member_count": info.member_count,
        "max_members": info.max_members,
        "group_level": info.group_level,
    }


def member_to_dict(member) -> dict:
    return {
        "user_id": member.user_id,
        "group_id": member.group_id,
        "nickname": member.nickname,
        "card": member.card,
        "role": member.role,
        "join_time": member.join_time.isoformat() if member.join_time else None,
        "last_active": member.last_active.isoformat() if member.last_active else None,
    }
