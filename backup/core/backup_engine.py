"""备份引擎。一次备份 = 一次完整的数据快照（全量或增量）。"""

import asyncio
import json
import logging
import mimetypes
import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional

from .database import Database
from .incremental import IncrementalTracker
from .media_downloader import MediaDownloader
from .models import BackupRecord, BackupStatus, Message
from .parser import parse_group_info, parse_group_members, parse_message
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
        # 这一轮往下跑的水位，取两者更靠后的：
        # - 增量点：平台接口还翻得到的老消息不必每轮从头分页；
        # - 库里非实时归档的最新一条：上一轮中途断掉时增量点不会推进（只有整轮
        #   跑完才 advance），但库里已经有半截数据，续传不该把半截再下一遍。
        # 实时归档的行两边都不参与：它按到达顺序写库、时间戳乱序，拿它当水位会
        # 跳过一整个时间段的正常消息
        stored_point = self.tracker.get_stored_point(group_id)
        candidates = [k for k in (stored_point, self.db.latest_backup_message_key(group_id)) if k]
        skip_key = max(candidates) if candidates else (datetime.min, "")

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
            self.db.save_members(group_id, members)
        except Exception as e:
            auxiliary_errors.append("成员列表")
            logger.warning("成员列表拉取失败 group=%s: %s", group_id, type(e).__name__)
        # 拿不到新的就沿用上次快照，别让导出里的群名/成员数变成空白
        if info is None:
            info = self.db.get_group_info(group_id)
        if not members:
            members = self.db.get_members(group_id)

        try:

            # 3-5. 分页拉消息，解析、下载媒体、入库
            page = 0
            collected: list[Message] = []
            failed_media = 0
            skipped = 0
            seen_on_pages: set[str] = set()
            while True:
                batch = await adapter.get_group_history(
                    group_id, count=PAGE_SIZE, offset=page * PAGE_SIZE
                )
                if not batch:
                    break
                # 有些适配器根本不认 offset，每页都回同一批消息。此时库里已经有它们，
                # 循环永远不会因为「本页无新消息」而结束——群越大越像卡死。整页都是
                # 翻过的消息就说明分页到头了，直接收工。
                fresh = [raw for raw in batch if str(_raw_id(raw)) not in seen_on_pages]
                if not fresh:
                    logger.info(
                        "群 %s 第 %s 页与之前重复，平台可能不支持偏移，停止翻页",
                        group_id,
                        page,
                    )
                    break
                seen_on_pages.update(str(_raw_id(raw)) for raw in fresh)
                for raw in fresh:
                    msg = parse_message(raw, group_id)
                    # 两道判断不能合并成「早于水位」：全量重跑时库里可能已经堆了
                    # 实时归档的消息（时间靠后），拿它当水位会把正常消息一起跳掉。
                    # 所以水位只在增量盘跑过之后才生效，库里有没有则逐条问。
                    too_old = stored_point is not None and (
                        (msg.timestamp, str(msg.message_id)) <= skip_key
                    )
                    if too_old or self.db.message_exists(msg.message_id, group_id):
                        skipped += 1
                        continue
                    # 已在库里的消息归属不动——包括实时归档的 backup_id='realtime'。
                    # 改成归给本次备份，等于把「群被炸时的兜底数据」从实时归档那个
                    # 入口上摘掉，`export latest` 从此看不到它
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
                if len(batch) < PAGE_SIZE:
                    break

            # 6. 增量点
            if collected:
                self.tracker.advance(group_id, collected)
                # 来源清单和归属是两回事：归属会被后续备份接手，这里记的不会
                self.db.add_sources(record.backup_id, collected)
                self.db.save_bundle(record.backup_id, group_id, info, members)

            # 7. 校验和
            record.checksum = compute_checksum(collected)
            logger.debug(
                "备份 group=%s 新增=%s 已在库=%s", group_id, record.message_count, skipped
            )
            record.total_size = self._compute_size(group_id)
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

    def _compute_size(self, group_id: int) -> int:
        total = 0
        for row in self.db.query(
            "SELECT local_path FROM media_files WHERE group_id = ?", (group_id,)
        ):
            p = Path(row["local_path"] or "")
            if p.is_file():
                total += p.stat().st_size
        return total

    @staticmethod
    def _gen_backup_id(group_id: int) -> str:
        return f"bk_{group_id}_{datetime.now():%Y%m%d%H%M%S}_{uuid.uuid4().hex[:6]}"
