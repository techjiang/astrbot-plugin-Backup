"""备份引擎。一次备份 = 一次完整的数据快照（全量或增量）。"""

import asyncio
import json
import logging
import mimetypes
import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional

from .database import REALTIME_BACKUP_ID, Database
from .incremental import IncrementalTracker
from .media_downloader import MediaDownloader
from .models import BackupRecord, BackupStatus, Message
from .parser import (
    parse_announcements,
    parse_group_files,
    parse_group_info,
    parse_group_members,
    parse_message,
)
from .verifier import compute_checksum

logger = logging.getLogger(__name__)

PAGE_SIZE = 50


def _raw_id(raw) -> str:
    """平台原始消息的 ID，用于判断翻页是否原地打转。取不到就退化成整条内容的指纹。"""
    if isinstance(raw, dict):
        value = raw.get("message_id") or raw.get("id") or raw.get("message_seq")
        if value:
            return str(value)
        return json.dumps(raw, ensure_ascii=False, sort_keys=True, default=str)[:512]
    value = getattr(raw, "message_id", None) or getattr(raw, "id", None)
    return str(value) if value else repr(raw)[:512]


class PlatformAdapter:
    """把 AstrBot 平台对象适配成备份引擎要的四个方法。

    平台 API 名字因适配器而异，这里做多名字兜底，取不到就让上层明确失败。
    """

    def __init__(self, platform):
        self.platform = platform

    async def _call(self, names, *args, **kwargs):
        for name in names:
            fn = getattr(self.platform, name, None)
            if callable(fn):
                result = fn(*args, **kwargs)
                if asyncio.iscoroutine(result):
                    result = await result
                return result
        return None

    async def get_group_info(self, group_id: int):
        return await self._call(
            ("get_group_info", "get_group", "call_action"),
            group_id=group_id,
        ) or {"group_id": group_id}

    async def get_group_members(self, group_id: int) -> list:
        result = await self._call(
            ("get_group_member_list", "get_group_members", "get_members"), group_id=group_id
        )
        return result or []

    async def get_group_announcements(self, group_id: int) -> list:
        """群公告。适配器多半叫 _get_group_notice，取不到就返回空表。"""
        return await self._call(
            ("get_group_notice", "get_group_announcements", "get_group_notices"),
            group_id=group_id,
        ) or []

    async def get_group_files(self, group_id: int) -> list:
        result = await self._call(
            ("get_group_files", "get_group_file_list", "get_files"),
            group_id=group_id,
        )
        return result or []

    async def get_group_history(
        self, group_id: int, count: int = PAGE_SIZE, offset: int = 0
    ) -> list:
        result = await self._call(
            ("get_group_msg_history", "get_group_history", "get_history"),
            group_id=group_id,
            count=count,
            offset=offset,
        )
        if isinstance(result, dict):
            result = result.get("messages") or result.get("data") or []
        return result or []


class BackupEngine:
    def __init__(self, db: Database, media_dir: str | Path, config: Optional[dict] = None):
        self.db = db
        self.config = config or {}
        self.media_dir = Path(media_dir)
        self.media = MediaDownloader(
            self.media_dir,
            (self.config.get("media") or {}),
        )
        self.tracker = IncrementalTracker(db)
        self.last_error: Optional[str] = None

    # ---------- 对外主流程 ----------
    async def backup_group(
        self,
        group_id: int,
        platform,
        incremental: bool = True,
        download_media: bool = True,
        progress_callback=None,
    ) -> BackupRecord:
        adapter = platform if isinstance(platform, PlatformAdapter) else PlatformAdapter(platform)
        incremental_enabled = incremental and bool(
            (self.config.get("incremental") or {}).get("enabled", True)
        )
        base = self.db.latest_completed_record(group_id) if incremental_enabled else None
        # 水位只认「谁都不管」的行能不能被收编，而不是「哪个时间点之后不再看」。
        # 原来的写法取 backup_meta 增量点和库里最新非实时归档消息的较大值，把两个
        # 不同维度的东西混成了一个数：
        # - backup_meta 推进过、而消息一条都不剩（`/backup clean` 淘汰了记录）时，
        #   水位留在天上，平台照旧返回的那批消息全被判成「太旧」。备份报成功、
        #   消息 0 条、校验和是空数据的——恢复时一条也取不出来；
        # - 续传点要防的是「半截数据重拉一遍」，这件事由「这条消息在不在库里」直接
        #   回答，不需要再猜一个时间点。
        # 所以现在两条都只用来判断「增量盘跑过没有」，真正的跳过条件是
        # 「库里已有且无主」。库里有主说明它已经在某次备份的来源清单里，跳过没有
        # 损失；库里没有、或归属的备份已经消失（淘汰/迁移残留）→ 一律重新收进本次
        # 备份。重复下载由 `message_exists` 之外的所有路径各自去重。
        stored_point = self.tracker.get_stored_point(group_id)

        # 只要库里有这个群的消息，这一轮就不是「首次全量」：哪怕增量点是空的，
        # 也只会退化成从最新一条往前翻。报成「全量」会让人以为把历史重备了一遍。
        has_history = self.db.count_messages(group_id) > 0
        record = BackupRecord(
            backup_id=self._gen_backup_id(group_id),
            group_id=group_id,
            status=BackupStatus.RUNNING,
            started_at=datetime.now(),
            is_incremental=(incremental_enabled and stored_point is not None) or has_history,
            base_backup_id=base.backup_id if base else None,
        )
        self.db.save_record(record)

        # 群信息/成员拉不到不等于备份失败：权限受限的适配器给不出成员列表，但历史
        # 消息照样能拉。原来任何一个抛错都会把整次备份标成 failed，一条消息都不留。
        info = None
        members: list = []
        auxiliary_errors: list[str] = []
        try:
            info = parse_group_info(await adapter.get_group_info(group_id), group_id)
            self.db.save_group_info(info)
        except Exception as e:
            auxiliary_errors.append("群信息")
            logger.warning("群信息拉取失败 group=%s: %s", group_id, type(e).__name__)
        try:
            raw_members = await adapter.get_group_members(group_id)
            members = parse_group_members(raw_members, group_id)
            # 成员列表是「当前状态」：整群替换，退群的人才会真的从名单里消失。
            # 只做 upsert 的话名单只会越攒越大，`/backup status` 的成员数永远不降。
            # 拉回来是空的（权限不足的适配器常见）就当没拉到，不能拿空表把快照清空。
            if members:
                self.db.replace_members(group_id, members)
        except Exception as e:
            auxiliary_errors.append("成员列表")
            logger.warning("成员列表拉取失败 group=%s: %s", group_id, type(e).__name__)
        # 拿不到新的就沿用上次快照，别让导出里的群名/成员数变成空白
        if info is None:
            info = self.db.get_group_info(group_id)
        if not members:
            members = self.db.get_members(group_id)

        # 群公告 / 群文件：开关在配置里，之前没人读。同样不影响主流程成败
        scope = self.config.get("scope") or {}
        if scope.get("group_announcements", True):
            try:
                raw_notices = await adapter.get_group_announcements(group_id)
                self.db.save_announcements(group_id, parse_announcements(raw_notices, group_id))
            except Exception as e:
                auxiliary_errors.append("群公告")
                logger.warning("群公告拉取失败 group=%s: %s", group_id, type(e).__name__)
        if scope.get("group_files", True):
            try:
                raw_files = await adapter.get_group_files(group_id)
                self.db.save_group_files(group_id, parse_group_files(raw_files, group_id))
            except Exception as e:
                auxiliary_errors.append("群文件")
                logger.warning("群文件拉取失败 group=%s: %s", group_id, type(e).__name__)

        try:

            # 3-5. 分页拉消息，解析、下载媒体、入库
            page = 0
            offset = 0
            collected: list[Message] = []
            failed_media = 0
            skipped = 0
            seen_on_pages: set[str] = set()
            stale_pages = 0
            own_ids: list[str] = []
            owned_seen: list[Message] = []
            while True:
                batch = await adapter.get_group_history(
                    group_id, count=PAGE_SIZE, offset=offset
                )
                if not batch:
                    break
                # 有些适配器根本不认 offset，每页都回同一批消息。此时库里已经有它们，
                # 循环永远不会因为「本页无新消息」而结束——群越大越像卡死。连续两页
                # 都是翻过的消息才说明分页真的到头了（见下）。
                fresh = [raw for raw in batch if str(_raw_id(raw)) not in seen_on_pages]
                if not fresh:
                    # 「这一页全翻过」不等于「后面没有了」。每次都从最新的往回翻的
                    # 平台（很多适配器就是这样）在第二次备份时会出现这种页：库里
                    # 早有这些消息，但后面还有更旧的一大段。原地打转的证据是
                    # **连续两页**都翻不出新东西——只有一页时先接着往下要
                    if stale_pages >= 1:
                        logger.info(
                            "群 %s 连续两页都是翻过的消息，平台可能不支持偏移，停止翻页",
                            group_id,
                        )
                        break
                    stale_pages += 1
                    offset += len(batch)
                    page += 1
                    continue
                stale_pages = 0
                seen_on_pages.update(str(_raw_id(raw)) for raw in fresh)
                # 偏移量按平台真正吐回来的条数推进，不能按页码 × 页大小：
                # 平台过滤掉一部分（撤回/系统消息不计入）时，按页码算会整段跳过，
                # 那些消息永远不会被备份，而备份报成功
                offset += len(batch)
                for raw in fresh:
                    msg = parse_message(raw, group_id)
                    if self.db.message_exists(msg.message_id, group_id):
                        # 已在库里的消息不改内容，但「归属的备份已经不存在」的那些
                        # 要收编（见上面水位的说明）：不收编就永远进不了任何来源
                        # 清单。实时归档的归属也不动——改成归给本次备份，等于把
                        # 「群被炸时的兜底数据」从实时归档入口上摘掉。
                        if self.db.adopt_orphans(
                            group_id, record.backup_id, [str(msg.message_id)]
                        ):
                            own_ids.append(str(msg.message_id))
                        else:
                            owned_seen.append(msg)
                        skipped += 1
                        continue
                    if download_media and msg.media_paths:
                        ok, bad = await self._download_media(msg)
                        record.media_count += ok
                        failed_media += bad
                    self.db.save_message(msg, record.backup_id)
                    record.message_count += 1
                    collected.append(msg)
                page += 1
                if progress_callback:
                    await progress_callback(record.message_count)
                # 只按「平台吐回来多少」判断翻页到头是错的：适配器不认 count、每页
                # 固定给 30 条时，第一页就小于 PAGE_SIZE，循环当场退出，剩下 3 倍
                # 的消息一条没备，备份还报 completed。低于半页才算到头（正常的最后
                # 一页），否则继续要下一页。收工条件由「翻过的消息集合」兜底，
                # 不会因为平台始终不认 offset 而死循环。
                if len(batch) < PAGE_SIZE // 2:
                    break

            # 6. 来源清单 + 快照
            # 来源清单和归属是两回事：归属会被后续备份接手，这里记的不会。
            # 除了本轮新收的，还要带上「遍历到、已在库且归属仍是某份现存备份」的
            # 消息——它们确实是这一轮覆盖到的。只登记新消息会得到一个空清单，
            # 校验和跟着退化成空数据的哈希，恢复时按来源取出一整批再比对必然失败。
            self.db.add_sources(record.backup_id, collected)
            self.db.add_sources(record.backup_id, owned_seen)
            covered = self.db.get_source_messages(record.backup_id, group_id)
            if own_ids:
                logger.info("群 %s 收编 %s 条无主消息", group_id, len(own_ids))
            # 记录里的 message_count 名义上是「这次存进库的新消息数」，增量备份跑一轮
            # 全是已在库消息时报 0 条——`/backup list` 里明晃晃写着「覆盖 0 条」，
            # 像是备份了个寂寞，而这轮实实在在覆盖了 50 条。
            # 现在它回答「这份备份保住了多少条消息」：按来源清单算。新增了多少由
            # `new_count` 单独交代，两者都得能看见——只改 message_count 的话，
            # 「这次又存进来几条」这个信息就没了。
            record.new_count = len(collected)
            if covered:
                record.message_count = len(covered)
            # 群信息/成员快照每轮都存：一条新消息都没有的那轮，导出仍然需要群名
            if covered or info is not None or members:
                self.db.save_bundle(record.backup_id, group_id, info, members)

            # 7. 增量点与校验和
            # 增量点只用于「下次从哪儿开始翻」的提示，不再参与跳过判断（见上方水位
            # 那段）；时间戳乱序的实时归档不该被它带偏，而 covered 里混不进实时归档。
            if covered:
                self.tracker.advance(group_id, covered)
            # 算「这次备份覆盖的全部消息」，不是「这次新拉到的消息」：增量为 0 条时
            # 只算 collected 会得到空数据的校验和，恢复时按来源取出一整批消息再比对
            # 必然对不上——数据一个字节没坏，却被判成「备份可能已损坏」。
            record.checksum = compute_checksum(covered) if covered else compute_checksum(collected)
            logger.debug(
                "备份 group=%s 新增=%s 已在库=%s", group_id, record.message_count, skipped
            )
            record.total_size = self._compute_size(group_id, covered or collected)
            record.status = BackupStatus.PARTIAL if failed_media else BackupStatus.COMPLETED
            notes = []
            if failed_media:
                notes.append(f"{failed_media} 个媒体文件下载失败")
            if auxiliary_errors:
                notes.append("；".join(auxiliary_errors) + "拉取失败，沿用上次快照")
            record.error = " · ".join(notes) if notes else None
            record.finished_at = datetime.now()
        except Exception as e:
            logger.error("备份失败 group=%s: %s", group_id, type(e).__name__)
            record.status = BackupStatus.FAILED
            record.error = f"{type(e).__name__}: {e}"
            record.finished_at = datetime.now()
        self.db.save_record(record)
        return record

    async def _download_media(self, msg: Message) -> tuple[int, int]:
        """下载消息媒体，返回 (成功数, 失败数)。单个失败不影响整体。"""
        ok = failed = 0
        for i, url in enumerate(list(msg.media_paths)):
            if not isinstance(url, str) or not url.startswith("http"):
                continue
            # 先问库：这条消息的这个 URL 是不是已经下过了。内存里那份 URL 索引
            # 重启就空，只靠它会让同一张图每重启一次就多落一份文件。
            known = self.db.find_media_by_url(msg.group_id, msg.message_id, url)
            if known:
                msg.media_paths[i] = known
                ok += 1
                self._ensure_media_row(msg, url, known)
                continue
            try:
                local = await self.media.download(url, msg.message_id, msg.timestamp)
                msg.media_paths[i] = local
                p = Path(local)
                self.db.save_media(
                    message_id=msg.message_id,
                    group_id=msg.group_id,
                    original_url=url,
                    local_path=local,
                    filename=p.name,
                    file_size=p.stat().st_size,
                    mime_type=mimetypes.guess_type(p.name)[0] or "",
                    sha256=self.media.sha256_of(p),
                )
                ok += 1
            except Exception as e:  # 红线 2：失败跳过并记录
                failed += 1
                logger.warning(
                    "媒体下载失败 message=%s reason=%s", msg.message_id, type(e).__name__
                )
                msg.media_paths[i] = f"[下载失败] {url}"
        return ok, failed

    def _ensure_media_row(self, msg: Message, url: str, local: str) -> None:
        """文件已在盘上、库里那行却没了：补登记，别让它成为没人认领的孤儿。"""
        p = Path(local)
        try:
            size = p.stat().st_size
        except OSError:
            return
        self.db.save_media(
            message_id=msg.message_id,
            group_id=msg.group_id,
            original_url=url,
            local_path=local,
            filename=p.name,
            file_size=size,
            mime_type=mimetypes.guess_type(p.name)[0] or "",
            sha256=self.media.sha256_of(p),
        )

    def watermark(self, group_id: int) -> tuple[datetime, str]:
        """上次备份停在哪 —— 增量点与库里最新一条消息取更靠后的那个。

        备份中途断掉时增量点不会推进（只有整轮跑完才 advance），但库里已经有半截
        数据。续传若只认增量点，会把这半截整段重拉、媒体整段重下，媒体记录还会
        重复插入。以库里的水位做下界，续传就真的只是往下跑。
        """
        point = self.tracker.get_incremental(group_id)
        latest = self.db.latest_message_key(group_id)
        if latest is None:
            return point or (datetime.min, "")
        if point is None:
            return latest
        return max(point, latest)

    def _compute_size(self, group_id: int, messages=None) -> int:
        """这条记录覆盖的消息所占的媒体体积。

        以前是「这个群所有媒体加起来」，于是每条记录的 total_size 都等于全群总量，
        `/backup list` 里 10 条记录的体积完全相同、加起来是实际的十倍。按记录覆盖
        的消息算才是它自己的账。
        """
        if messages is None:
            messages = self.db.get_messages(group_id=group_id)
        ids = {str(m.message_id) for m in messages}
        if not ids:
            return 0
        total = 0
        for row in self.db.query(
            "SELECT message_id, local_path FROM media_files WHERE group_id = ?", (group_id,)
        ):
            if str(row["message_id"]) not in ids:
                continue
            p = Path(row["local_path"] or "")
            if p.is_file():
                total += p.stat().st_size
        return total

    @staticmethod
    def _gen_backup_id(group_id: int) -> str:
        return f"bk_{group_id}_{datetime.now():%Y%m%d%H%M%S}_{uuid.uuid4().hex[:6]}"
