"""Backup 高级备份 · AstrBot 插件入口。

作者：科技酱 · https://www.asoe.cn
"""

import logging
from pathlib import Path
from typing import Optional

from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register

from .core.backup_engine import BackupEngine
from .core.commands import (
    HELP_TEXT,
    PermissionChecker,
    _config_get,
    _config_set,
    human_size,
    parse_group_arg,
    render_backup_list,
    render_diff,
    render_status,
)
from .core.database import Database
from .core.download_route import handle_download
from .core.export_manager import ExportManager
from .exporters import SUPPORTED_FORMATS
from .core.auto_trigger import AutoTrigger
from .core.event_hooks import EventHooks, extract_group_id
from .core.paths import assert_writable_target, resolve_storage_root
from .core.restore_engine import RestoreEngine, RestoreError
from .core.scheduler import BackupScheduler
from .core.verifier import verify_zip

logger = logging.getLogger(__name__)

PLUGIN_DIR = Path(__file__).resolve().parent


def _load_config() -> dict:
    try:
        import yaml

        data = yaml.safe_load((PLUGIN_DIR / "config.yaml").read_text(encoding="utf-8")) or {}
        return data.get("backup", data) or {}
    except Exception:
        return {}


def _save_config(config: dict) -> bool:
    """把改过的配置写回 config.yaml。失败就只留在内存里，不影响本次运行。"""
    try:
        import yaml

        target = PLUGIN_DIR / "config.yaml"
        payload = {"backup": config}
        tmp = target.with_suffix(".yaml.tmp")
        tmp.write_text(
            yaml.safe_dump(payload, allow_unicode=True, sort_keys=False), encoding="utf-8"
        )
        tmp.replace(target)
        return True
    except Exception as e:
        logger.warning("配置写回失败：%s", type(e).__name__)
        return False


@register(
    "backup",
    "科技酱",
    "群聊数据高级备份插件：消息/媒体/成员全量或增量备份，多格式导出与一键恢复",
    "1.0.0",
)
class BackupPlugin(Star):
    def __init__(self, context: Context):
        super().__init__(context)
        self.config = _load_config()
        storage = assert_writable_target(
            resolve_storage_root(self.config, PLUGIN_DIR),
            (self.config.get("storage") or {}).get("web_roots") or [],
        )
        self.storage = storage
        self.db = Database(storage / "db" / "backup.sqlite")
        self.engine = BackupEngine(self.db, storage / "media", self.config)
        self.exports = ExportManager(
            self.db,
            storage / "exports",
            storage / "media",
            self.config,
        )
        self.restore_engine = RestoreEngine(self.db)
        permission_cfg = self.config.get("permission") or {}
        self.permission = PermissionChecker(
            permission_cfg.get("require_admin", True),
            permission_cfg.get("allow_private", True),
        )
        self.auto_trigger = AutoTrigger(self.config.get("auto_trigger") or {})
        self.scheduler = BackupScheduler(
            self.db,
            self._scheduled_backup,
            (self.config.get("schedule") or {}).get("default_cron", "0 3 * * *"),
            enabled=bool((self.config.get("schedule") or {}).get("enabled", True)),
        )
        self.hooks = EventHooks(
            self.db,
            self.auto_trigger,
            archive=bool((self.config.get("scope") or {}).get("group_messages", True)),
            on_leave=bool((self.config.get("auto_trigger") or {}).get("on_bot_kicked", True)),
            on_disband=bool((self.config.get("auto_trigger") or {}).get("on_group_disband", True)),
            archive_private=bool((self.config.get("scope") or {}).get("private_messages", False)),
        )
        self.scheduler.restore_jobs()
        if self.config.get("retention", {}).get("auto_clean"):
            self.exports.clean_exports(
                int(self.config.get("retention", {}).get("keep_days", 90))
            )

    # ---------- 运行时 ----------
    async def _scheduled_backup(self, group_id: int) -> None:
        """定时任务入口：没有事件，用全局平台对象。"""
        await self._backup(group_id)

    def _deny(self, event) -> Optional[str]:
        perm = self.permission.check_event(event)
        if not perm.allowed:
            return perm.reason
        return None

    @filter.command("backup")
    async def backup(self, event: AstrMessageEvent):
        """Backup 高级备份命令入口"""
        args = [a for a in event.message_str.strip().split() if a]
        if args and args[0].lower() == "backup":
            args = args[1:]
        if not args or args[0] in ("help", "-h", "--help"):
            yield event.plain_result(HELP_TEXT)
            return

        denied = self._deny(event)
        if denied:
            yield event.plain_result(f"⚠️ {denied}")
            return

        sub = args[0].lower()
        handler = {
            "now": self._cmd_now,
            "list": self._cmd_list,
            "status": self._cmd_status,
            "export": self._cmd_export,
            "restore": self._cmd_restore,
            "verify": self._cmd_verify,
            "clean": self._cmd_clean,
            "diff": self._cmd_diff,
            "schedule": self._cmd_schedule,
            "config": self._cmd_config,
        }.get(sub)
        if handler is None:
            yield event.plain_result(f"未知子命令：{sub}\n\n{HELP_TEXT}")
            return
        async for result in handler(event, args[1:]):
            yield result

    async def _cmd_now(self, event, args):
        group_id = parse_group_arg(args[0]) if args else getattr(event, "group_id", None)
        if not group_id:
            yield event.plain_result("请在群内使用，或指定群号：/backup now 123456")
            return
        yield event.plain_result(f"开始备份群 {group_id}…")
        record = await self._backup(group_id, event)
        if record is None:
            yield event.plain_result("❌ 当前没有可用的平台适配器，无法拉取历史消息")
            return
        state = {"完成": "✅", "partial": "⚠️", "failed": "❌"}.get(record.status.value, "•")
        yield event.plain_result(
            f"{state} 备份 {'增量' if record.is_incremental else '全量'}结束\n"
            f"消息 {record.message_count} 条 · 媒体 {record.media_count} 个 · "
            f"{human_size(record.total_size)}\n备份 ID：`{record.backup_id}`"
            + (f"\n备注：{record.error}" if record.error else "")
        )

    async def _cmd_list(self, event, args):
        group_id = parse_group_arg(args[0]) if args else None
        yield event.plain_result(render_backup_list(self.db, group_id))

    async def _cmd_status(self, event, args):
        group_id = parse_group_arg(args[0]) if args else getattr(event, "group_id", None)
        if not group_id:
            yield event.plain_result("请指定群号：/backup status 123456")
            return
        yield event.plain_result(render_status(self.db, group_id))

    async def _cmd_export(self, event, args):
        """导出某个群。

        默认走 `export_latest`：最近一次备份覆盖的消息 + 只在实时归档里的那些。
        只按某次 backup_id 取数会正好漏掉群被炸前最后几条——那几条往往只走了实时
        归档，不属于任何一次备份。两个参数位置都认（群号在格式前或后）。
        """
        if not args:
            fmt = (self.config.get("export") or {}).get("default_format", "sqlite")
            args = [str(fmt)]
        formats = set(SUPPORTED_FORMATS) | {"md", "htm"}
        group_id = None
        fmt = ""
        for token in args:
            low = token.lower()
            if low in formats:
                fmt = low
            elif group_id is None:
                group_id = parse_group_arg(token)
        if not fmt:
            yield event.plain_result("请指定导出格式：sqlite/json/csv/txt/markdown/html/zip")
            return
        group_id = group_id or getattr(event, "group_id", None)
        if not group_id:
            yield event.plain_result("请指定群号：/backup export 123456 sqlite")
            return
        own = getattr(event, "self_id", None) or getattr(event, "bot_id", None)
        try:
            result = self.exports.export_latest(group_id, fmt, own_user_id=own)
        except Exception as e:
            yield event.plain_result(f"导出失败：{e}")
            return
        yield event.plain_result(
            f"✅ 导出完成（{result.format}）\n消息 {result.message_count} 条 · "
            f"{human_size(result.size)}\n临时链接（{result.expires_at:%H:%M} 过期，仅可使用一次）：{result.link}"
        )

    async def _cmd_restore(self, event, args):
        if not args:
            yield event.plain_result(
                "用法：/backup restore <备份ID> [目标群号] [--strategy skip|overwrite|merge] --confirm"
            )
            return
        backup_id = args[0]
        target = parse_group_arg(args[1]) if len(args) > 1 else None
        strategy = "skip"
        if "--strategy" in args:
            idx = args.index("--strategy")
            if idx + 1 < len(args):
                strategy = args[idx + 1]
        if "--confirm" not in args:
            yield event.plain_result(
                f"⚠️ 恢复是危险操作，会写入群 {target or '原群'} 的消息库。\n"
                f"确认无误请重发并追加 `--confirm`"
            )
            return
        try:
            result = await self.restore_engine.restore(
                backup_id, target, strategy, confirm=True
            )
        except RestoreError as e:
            yield event.plain_result(f"❌ {e}")
            return
        yield event.plain_result(
            f"✅ {result.message}\n写入 {result.inserted} 条 · 跳过 {result.skipped} 条 · "
            f"覆盖 {result.overwritten} 条 → 群 {result.target_group_id}"
        )

    async def _cmd_verify(self, event, args):
        if not args:
            yield event.plain_result("用法：/backup verify <备份ID> [备份包路径]")
            return
        record = self.db.get_record(args[0])
        if not record:
            yield event.plain_result("备份 ID 不存在")
            return
        if len(args) > 1:
            ok, reason = verify_zip(args[1], record.checksum or None)
        else:
            ok, reason = True, "数据库记录校验和：" + (record.checksum[:16] or "（空）")
        yield event.plain_result(f"{'✅' if ok else '❌'} {reason}")

    async def _cmd_clean(self, event, args):
        """清理旧备份。

        以前这里是「删记录 → 连本备份名下的消息一起删」。消息只归属最后一次归档它
        的备份，所以删一条老记录会顺带抹掉还可能被别的备份用到的聊天记录。现在
        只淘汰记录、过期导出文件、超出保留期的媒体，消息本身不动。
        """
        retention = self.config.get("retention") or {}
        days = int(args[0]) if args and args[0].isdigit() else int(retention.get("keep_days", 90) or 0)
        keep_count = int(retention.get("keep_count", 0) or 0)
        records = self.db.prune_records(keep_count=keep_count, keep_days=days)
        media = self.db.prune_media(days)
        exports = self.exports.clean_exports(days)
        yield event.plain_result(
            f"🧹 已清理：备份记录 {records} 条 · 过期导出 {exports} 个 · "
            f"早于 {days} 天的媒体 {media} 个\n消息本身不删，仍可按备份 ID 导出"
        )

    async def _cmd_diff(self, event, args):
        if len(args) < 2:
            yield event.plain_result("用法：/backup diff <备份ID1> <备份ID2>")
            return
        yield event.plain_result(render_diff(self.db, args[0], args[1]))

    async def _cmd_schedule(self, event, args):
        if not args:
            yield event.plain_result("用法：/backup schedule set|list|remove")
            return
        action = args[0].lower()
        if action == "list":
            jobs = self.scheduler.list_jobs()
            if not jobs:
                yield event.plain_result("还没有定时备份任务。")
                return
            yield event.plain_result(
                "\n".join(f"- 群 {j['group_id']}：`{j['cron']}`" for j in jobs)
            )
        elif action == "set":
            if len(args) < 3:
                yield event.plain_result('用法：/backup schedule set <群号> "0 3 * * *"')
                return
            group_id = parse_group_arg(args[1])
            cron = " ".join(args[2:]).strip().strip('"').strip("'")
            if not group_id or not self.scheduler.add_job(group_id, cron):
                yield event.plain_result("群号或 cron 表达式不合法")
                return
            yield event.plain_result(f"✅ 已为群 {group_id} 设置定时备份：`{cron}`")
        elif action == "remove":
            group_id = parse_group_arg(args[1]) if len(args) > 1 else None
            if not group_id:
                yield event.plain_result("用法：/backup schedule remove <群号>")
                return
            self.scheduler.remove_job(group_id)
            yield event.plain_result(f"已移除群 {group_id} 的定时备份")
        else:
            yield event.plain_result("未知操作，支持 set / list / remove")

    async def _cmd_config(self, event, args):
        """配置查看/修改。写回 config.yaml，只改已知键，避免拼错就多出一条废配置。"""
        if not args or args[0].lower() not in ("get", "set"):
            yield event.plain_result("用法：/backup config get <键> / config set <键> <值>")
            return
        action = args[0].lower()
        if action == "get":
            if len(args) < 2:
                yield event.plain_result("用法：/backup config get <键>，如 scope.download_media")
                return
            key = args[1]
            value = _config_get(self.config, key)
            if value is None:
                yield event.plain_result(f"配置项 `{key}` 不存在")
                return
            yield event.plain_result(f"`{key}` = `{value}`")
            return

        if len(args) < 3:
            yield event.plain_result("用法：/backup config set <键> <值>")
            return
        key, raw = args[1], " ".join(args[2:])
        ok, message = _config_set(self.config, key, raw)
        if not ok:
            yield event.plain_result(f"⚠️ {message}")
            return
        saved = _save_config(self.config)
        yield event.plain_result(
            f"✅ `{key}` = `{_config_get(self.config, key)}`" + ("" if saved else "（仅本次运行生效）")
        )

    # ---------- 事件钩子 ----------
    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_message(self, event: AstrMessageEvent):
        """实时归档：消息进群就落库，之后增量备份不必再回捞。

        只写库不下载媒体，也不回复任何内容——群聊里静默干活。
        """
        try:
            self.hooks.archive_message(event)
        except Exception as e:  # 归档失败不能影响机器人正常聊天
            logger.error("实时归档失败 group=%s: %s", extract_group_id(event), type(e).__name__)

    async def _on_notice_impl(self, event):
        payload = getattr(event, "message_obj", None) or getattr(event, "raw_message", None)
        group_id, subtype, should_backup = self.hooks.handle_notice(payload, event)
        if group_id is None or not should_backup:
            return
        logger.warning("群 %s 事件 %s，触发备份", group_id, subtype or "unknown")
        try:
            await self._backup(group_id, event)
        except Exception as e:
            logger.error("触发式备份失败 group=%s: %s", group_id, type(e).__name__)

    async def _backup(self, group_id: int, event=None):
        """统一的备份入口：优先用事件自带的平台对象，退回全局平台。"""
        platform = getattr(event, "bot", None) if event is not None else None
        if platform is None:
            platform = getattr(self.context, "get_platform", lambda: None)()
        if platform is None:
            return None
        return await self.engine.backup_group(
            group_id,
            platform,
            incremental=(self.config.get("incremental") or {}).get("enabled", True),
            download_media=(self.config.get("scope") or {}).get("download_media", True),
        )

    # ---------- 下载路由 ----------
    def handle_download_request(self, path: str):
        """给宿主的路由/静态服务调用：token 兑换成文件流。"""
        return handle_download(path, self.exports)

    async def terminate(self):
        self.scheduler.shutdown()
        self.db.close()
