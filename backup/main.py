"""Backup 高级备份 · AstrBot 插件入口。

作者：科技酱 · https://www.asoe.cn
"""

import json
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
    render_backup_result,
    render_diff,
    render_status,
)
from .core.database import Database
from .core.download_route import handle_download, make_view
from .core.export_manager import ExportManager
from .exporters import SUPPORTED_FORMATS
from .core.auto_trigger import AutoTrigger
from .core.event_hooks import (
    EventHooks,
    extract_group_id,
    is_message_echo,
    notice_payload,
)
from .core.paths import assert_writable_target, resolve_storage_root
from .core.restore_engine import RestoreEngine, RestoreError
from .core.scheduler import BackupScheduler
from .core.verifier import read_manifest, verify_zip

logger = logging.getLogger(__name__)

PLUGIN_DIR = Path(__file__).resolve().parent


def _looks_like_archive(token: str) -> bool:
    """这个参数像不像一个备份包路径。

    备份 ID 的形状是 `bk_100_20261104052000_ab12cd`，不带扩展名；恢复命令的
    第一个参数有可能是包路径，靠扩展名区分，不靠「文件在不在」——路径打错时
    也得走「备份不存在」那条正常报错，而不是被当成备份 ID 报一句话。
    """
    text = str(token or "")
    return text.lower().endswith(".zip") or "/" in text or "\\" in text


def _event_self_id(event):
    """机器人自己的 QQ/用户 id，用来把「我说的话」放在气泡右侧。"""
    for name in ("get_self_id", "self_id", "bot_id"):
        value = getattr(event, name, None)
        if callable(value):
            try:
                value = value()
            except Exception:
                continue
        if value not in (None, ""):
            try:
                return int(value)
            except (TypeError, ValueError):
                return None
    inner = getattr(event, "message_obj", None)
    value = getattr(inner, "self_id", None) if inner is not None else None
    try:
        return int(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _schema_defaults() -> dict:
    """`_conf_schema.json` 里的默认值，摊成 config.yaml 那种嵌套 dict。"""
    try:
        schema = json.loads((PLUGIN_DIR / "_conf_schema.json").read_text(encoding="utf-8"))
    except Exception:
        return {}
    out: dict = {}
    for key, spec in (schema or {}).items():
        if not isinstance(spec, dict):
            continue
        if spec.get("type") == "object":
            out[key] = _schema_defaults_from(spec.get("items") or {})
        else:
            out[key] = spec.get("default")
    return out


def _schema_defaults_from(items: dict) -> dict:
    out: dict = {}
    for key, spec in (items or {}).items():
        if not isinstance(spec, dict):
            continue
        if spec.get("type") == "object":
            out[key] = _schema_defaults_from(spec.get("items") or {})
        else:
            out[key] = spec.get("default")
    return out


def _load_config(injected: Optional[dict] = None) -> dict:
    """配置：宿主注入的为准，`config.yaml` 补缺，`_conf_schema.json` 兜底默认值。

    这里原来只读插件目录下的 `config.yaml`，面板改了也不生效——因为 AstrBot 根本
    不看那个文件：`star_manager` 见到 `_conf_schema.json` 就用它建一个
    `AstrBotConfig`，落在 `data/config/<插件名>_config.json`，再以
    `config=` 关键字传给插件类。插件不接这个参数，用户在面板里改的每一项都是
    写给自己的。

    优先级：宿主注入 > config.yaml > schema 默认值。
    """
    merged = _schema_defaults()
    try:
        import yaml

        data = yaml.safe_load((PLUGIN_DIR / "config.yaml").read_text(encoding="utf-8")) or {}
        file_config = data.get("backup", data) or {}
    except Exception:
        file_config = {}
    _deep_update(merged, file_config)
    if isinstance(injected, dict):
        _deep_update(merged, dict(injected))
    return merged


def _deep_update(base: dict, extra: dict) -> dict:
    for key, value in (extra or {}).items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _deep_update(base[key], value)
        else:
            base[key] = value
    return base


def _save_config(config: dict, injected: Optional[dict] = None) -> bool:
    """把改过的配置写回去。失败就只留在内存里，不影响本次运行。

    宿主注入了 `AstrBotConfig`（dict 子类，自己会 `save_config()`）时写它——
    那份才是面板读的文件。没有注入才退回插件目录下的 `config.yaml`。

    注意 `AstrBotConfig` 里存的是扁平结构（`_conf_schema.json` 的默认值摊出来
    就是 `export.base_url` 这一层），而 `config.yaml` 是嵌套的。往面板那份写要
    逐项回填，不能整块塞进去——整块塞会多出 `export` 这样一个嵌套键。
    """
    if isinstance(injected, dict):
        try:
            for key, value in config.items():
                if isinstance(value, dict) and isinstance(injected.get(key), dict):
                    _deep_update(injected[key], value)
                else:
                    injected[key] = value
            saver = getattr(injected, "save_config", None)
            if callable(saver):
                saver()
                return True
        except Exception as e:
            logger.warning("面板配置写回失败：%s", type(e).__name__)
            return False
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
    def __init__(self, context: Context, config: Optional[dict] = None):
        super().__init__(context)
        # AstrBot 会以 `config=` 传进来（`_conf_schema.json` 驱动的那份）
        self.injected_config = config if isinstance(config, dict) else None
        self.config = _load_config(config)
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
        self._notice_probe_done = False
        self._event_probe_done = False
        # 通知钩子里触发的那次「最后备份」不回话：群解散/被踢的现场，回复既发不出去
        # （群没了），也可能把被踢这件事再暴露给在场的人。手动 `/backup` 才回话
        self._silent_backup = False
        self.scheduler.restore_jobs()
        # 面板里能点开下载链接（群成员直接点需要自己的网关，见 register_download_api）
        self.register_download_api()
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
        group_id = parse_group_arg(args[0]) if args else extract_group_id(event)
        if not group_id:
            yield event.plain_result("请在群内使用，或指定群号：/backup now 123456")
            return
        yield event.plain_result(f"开始备份群 {group_id}…")
        record = await self._backup(group_id, event)
        if record is None:
            yield event.plain_result("❌ 当前没有可用的平台适配器，无法拉取历史消息")
            return
        yield event.plain_result(render_backup_result(record))

    async def _cmd_list(self, event, args):
        group_id = parse_group_arg(args[0]) if args else None
        yield event.plain_result(render_backup_list(self.db, group_id))

    async def _cmd_status(self, event, args):
        group_id = parse_group_arg(args[0]) if args else extract_group_id(event)
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
        group_id = group_id or extract_group_id(event)
        if not group_id:
            yield event.plain_result("请指定群号：/backup export 123456 sqlite")
            return
        own = _event_self_id(event)
        try:
            result = self.exports.export_latest(group_id, fmt, own_user_id=own)
        except Exception as e:
            yield event.plain_result(f"导出失败：{e}")
            return
        base = self._public_base_url()
        link = f"{base}/{result.token}" if base else ""
        hint = ""
        if not link:
            link = f"/backup/download/{result.token}"
            hint = "\n⚠️ 没配公网地址，这条链接只能本机用。把 `export.base_url` 填成面板的地址（或自建网关）后重新导出"
        yield event.plain_result(
            f"✅ 导出完成（{result.format}）\n消息 {result.message_count} 条 · "
            f"{human_size(result.size)}\n临时链接（{result.expires_at:%H:%M} 过期，仅可使用一次）：{link}{hint}"
        )

    async def _cmd_restore(self, event, args):
        if not args:
            yield event.plain_result(
                "用法：/backup restore <备份ID> [目标群号] [--strategy skip|overwrite|merge] --confirm"
            )
            return
        archive = None
        # 手里只剩一个包的时候（换机器、原机器没了、群被炸了），命令里第一个参数
        # 就是包路径，而不是备份 ID。README 里那句「/backup restore <备份ID> <包路径>」
        # 也认，两种写法都走同一条路。
        tokens = [t for t in args if not t.startswith("--")]
        backup_id = tokens[0] if tokens else ""
        target = parse_group_arg(tokens[1]) if len(tokens) > 1 else None
        if not backup_id:
            yield event.plain_result(
                "用法：/backup restore <备份ID|备份包路径> [目标群号] [--strategy skip|overwrite|merge] --confirm"
            )
            return
        if target is None and _looks_like_archive(backup_id):
            archive = backup_id
            manifest = read_manifest(archive)
            backup_id = str(manifest.get("backup_id") or Path(archive).stem)
        elif len(tokens) > 2 and _looks_like_archive(tokens[2]):
            archive = tokens[2]
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
                backup_id, target, strategy, archive=archive, confirm=True
            )
        except RestoreError as e:
            yield event.plain_result(f"❌ {e}")
            return
        parts = [
            f"共 {result.total} 条",
            f"写入 {result.inserted} 条",
            f"跳过 {result.skipped} 条",
            f"覆盖 {result.overwritten} 条",
        ]
        if result.merged:
            parts.append(f"补齐 {result.merged} 条")
        yield event.plain_result(
            f"✅ {result.message}\n" + " · ".join(parts) + f" → 群 {result.target_group_id}"
        )

    async def _cmd_verify(self, event, args):
        """校验备份完整性。

        给包的时候**不能**把记录里的 `checksum` 当期望值传下去：那个数覆盖的是
        「这次备份新增/覆盖的消息」，而 `export latest` 的包还含只在实时归档里的
        那部分消息，两个数天然不等。拿它去比包里 manifest 的 `checksum`，结果
        就是自己刚导出的包被自己的校验判成「备份可能被修改」——`verify_zip` 里
        那段注释早就写了这个道理，命令层却一直在传。
        """
        if not args:
            yield event.plain_result("用法：/backup verify <备份ID> [备份包路径]")
            return
        record = self.db.get_record(args[0])
        if not record:
            yield event.plain_result("备份 ID 不存在")
            return
        if len(args) > 1:
            ok, reason = verify_zip(args[1])
            if ok:
                reason += "（包与自身清单一致；群信息/成员表在恢复时才与本机留存的快照比对）"
        else:
            ok, reason = True, "本机记录校验和：" + (record.checksum[:16] or "（空）")
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
        saved = _save_config(self.config, self.injected_config)
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
        if not self._event_probe_done:
            self._event_probe_done = True
            try:
                logger.debug("首次真实事件 %s", self.hooks.describe_event(event))
            except Exception:  # 探测自己出错绝不能影响归档
                pass

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_notice(self, event: AstrMessageEvent):
        """群事件通知（成员变动 / 解散 / 机器人被踢）→ 触发式备份。

        以前 `_on_notice_impl` 写好了却没人调，等于退群聚合、解散预警、离群前
        最后一次备份三个功能从来没上线过。钩子里抛错一律吞掉：备份插件不能因为
        一条通知就把机器人的消息处理链路带崩。

        顺带说清楚一件事：AstrBot 把 OneBot 的 notice 事件也包成**普通消息事件**
        投进管道（`aiocqhttp` 的 `_convert_handle_notice_event` → `handle_msg`），
        `type` 是 `OTHER_MESSAGE`、`message_str` 为空、原始事件在
        `message_obj.raw_message` 里。所以这个钩子和 `on_message` 收的是同一类
        事件，靠 `raw_message.post_type` 区分——这里先判 `post_type`，不是
        notice 就直接返回，别把正常聊天当成退群。
        """
        try:
            await self._handle_notice(event)
        except Exception as e:
            logger.error("通知处理失败: %s", type(e).__name__)

    async def _handle_notice(self, event):
        payload = notice_payload(event)
        if is_message_echo(payload):
            # 普通消息：`on_message` 已经归档过了，不能在这里再走一遍退群聚合，
            # 否则一次普通聊天就能把「大量退群」的阈值顶满
            return
        if not self._notice_probe_done and payload:
            self._notice_probe_done = True
            logger.debug(self.hooks.describe_notice(payload))
        group_id, subtype, should_backup = self.hooks.handle_notice(payload, event)
        if group_id is None or not should_backup:
            return
        logger.warning("群 %s 事件 %s，触发备份", group_id, subtype or "unknown")
        self._silent_backup = True
        try:
            await self._backup(group_id, event)
        except Exception as e:
            logger.error("触发式备份失败 group=%s: %s", group_id, type(e).__name__)
        finally:
            self._silent_backup = False

    def _resolve_platform(self, event=None):
        """找到能拉数据的平台对象。

        原来写的是 `getattr(self.context, "get_platform", lambda: None)()`：
        AstrBot 的 `Context.get_platform` 是**方法**，`getattr` 拿到的是 bound
        method 而不是平台，那句 `if platform is None` 于是永远不成立，接下去
        `PlatformAdapter(bound_method)` 里的每个 `_call` 都打在方法对象上、全返回
        None——`/backup now` 只会回一句「当前没有可用的平台适配器」。

        真实路径是先拿到事件所属平台的 **id**（多开同类适配器时名字会重复，
        id 才是唯一的），再问 Context 要实例；拿不到才退回按名字/第一个。
        """
        if event is not None:
            # aiocqhttp 的事件自带 bot（OneBot 客户端），最直接
            for name in ("bot", "client"):
                candidate = getattr(event, name, None)
                # `bot` 是 OneBot 客户端对象；有些适配器把它做成可调用对象，
                # 那就不是我们要的客户端，跳过
                if candidate is not None and not callable(candidate):
                    return candidate
            for name in ("get_platform_id", "get_platform_name"):
                fn = getattr(event, name, None)
                if not callable(fn):
                    continue
                try:
                    key = fn()
                except Exception:
                    continue
                found = self._platform_by(key, by_id=name == "get_platform_id")
                if found is not None:
                    return found
        # 定时任务没有事件：库里/配置里只有一个平台时就它了
        return self._platform_by(None, by_id=False)

    def _platform_by(self, key, by_id: bool):
        """按 id / 名字 / 「随便哪个」找平台实例。"""
        ctx = self.context
        for name in ("get_platform_inst",):
            fn = getattr(ctx, name, None)
            if by_id and callable(fn) and key:
                try:
                    found = fn(key)
                except Exception:
                    found = None
                if found is not None:
                    return found
        manager = getattr(ctx, "platform_manager", None)
        insts = list(getattr(manager, "platform_insts", []) or [])
        if not insts:
            fn = getattr(ctx, "get_platform", None)
            if callable(fn) and key:
                try:
                    return fn(key)
                except Exception:
                    return None
            return None
        if key:
            for inst in insts:
                meta = inst.meta() if callable(getattr(inst, "meta", None)) else None
                if meta is not None and str(getattr(meta, "id", "")) == str(key):
                    return inst
            for inst in insts:
                meta = inst.meta() if callable(getattr(inst, "meta", None)) else None
                if meta is not None and str(getattr(meta, "name", "")) == str(key):
                    return inst
        return insts[0]

    async def _backup(self, group_id: int, event=None):
        """统一的备份入口：优先用事件自带的平台对象，退回全局平台。

        通知钩子触发的那次带 `_silent_backup`：触发式备份的现场往往来不及再手动
        跑一次导出，这里顺手把包做出来，且不在群里回任何话。
        """
        platform = self._resolve_platform(event)
        if platform is None:
            return None
        record = await self.engine.backup_group(
            group_id,
            platform,
            incremental=(self.config.get("incremental") or {}).get("enabled", True),
            download_media=(self.config.get("scope") or {}).get("download_media", True),
        )
        if self._silent_backup:
            try:
                self.exports.export_latest(
                    group_id,
                    (self.config.get("export") or {}).get("default_format", "zip"),
                )
            except Exception as e:  # 导出失败不能反过来说备份失败
                logger.warning("触发式备份后导出失败 group=%s: %s", group_id, type(e).__name__)
        return record

    # ---------- 下载路由 ----------
    def handle_download_request(self, path: str):
        """给宿主的路由/静态服务调用：token 兑换成文件流（返回 DownloadResponse）。"""
        return handle_download(path, self.exports)

    def register_download_api(self) -> bool:
        """把下载链接真正接到宿主的 Web 上。

        AstrBot 4.14.6 的事实（`dashboard/server.py`）：

        - 插件用 `context.register_web_api(route, view, methods, desc)` 登记路由，
          宿主把 `/api/plug/<path:subpath>` 转给所有插件登记的路由，按
          `route == f"/{subpath}"` **精确匹配**；
        - `auth_middleware` 的白名单是**按前缀**放行的，里面已经有
          `/api/backup/download`，注释写着「备份下载使用 URL 参数传递 token」。

        上一版把「精确匹配」读成了「只能用固定路径 + query token」，还据此写下
        「群里点链接在 4.14 上做不到」。再核一遍源码：插件登记的 route 是**前缀**，
        subpath 是尾巴——`/api/backup/download/<token>` 能匹配到，且整条在面板鉴权
        白名单里。所以群里点链接这条路本来是通的，是我上一轮下错了结论。

        现在登记三条（宿主侧是精确匹配，只能一条一条登记）：

        - `/backup/download/<token>`：完整路径，群里点的就是它；
        - `/backup/download`：固定路径 + query token，面板按钮用；
        - `/download/<token>`：短前缀，宿主如果自己吃掉了前缀，这条兜住。
        """
        registrar = getattr(self.context, "register_web_api", None)
        if not callable(registrar):
            logger.debug("宿主没有 register_web_api，下载链接需要一个自建网关来兑换 token")
            return False
        view = make_view(self.exports)
        registered = []
        for route in ("/backup/download/<token>", "/backup/download", "/download/<token>"):
            try:
                registrar(route, view, ["GET"], "Backup 导出文件下载（需 token）")
                registered.append(route)
            except Exception as e:  # 宿主版本差异，注册不上不影响备份本身
                logger.warning("下载路由 %s 注册失败：%s", route, type(e).__name__)
        if registered:
            logger.info(
                "下载路由已注册：/api/plug/backup/download/<token>（同 /api/backup/download/<token>）"
            )
        return bool(registered)

    def _public_base_url(self) -> str:
        """下载链接的前缀。返回 `<base>`，完整链接是 `<base>/<token>`。

        链接是要发到群里的，前面必须是一个群成员点得到的地址。`export.base_url`
        空着的时候，`/backup export` 原来只给一个裸路径 `/backup/download/<token>`
        ——发到 QQ 群里没人点得动。

        所以空着的时候从面板配置里找公网地址（各种写法都认），拼成
        `http://<面板>/api/backup/download`：这条前缀在 AstrBot 4.14 的
        `auth_middleware` 白名单里，不需要面板 JWT。都找不到就返回空，上层退回
        裸路径并提示去配 `export.base_url`。
        """
        export_cfg = self.config.get("export") or {}
        explicit = str(export_cfg.get("base_url") or "").strip()
        if explicit:
            base = explicit.rstrip("/")
        else:
            dashboard = self.config.get("dashboard") or {}
            host = ""
            for key in ("public_url", "base_url", "webui_url", "host"):
                value = dashboard.get(key)
                if isinstance(value, str) and value.startswith(("http://", "https://")):
                    host = value.rstrip("/")
                    break
            if not host:
                return ""
            base = f"{host}/api/backup/download"
        # base_url 的语义是「链接前缀」。写成 `/api/plug`、`/api/backup/download`、
        # 或者干脆只写了域名，都收成 `<host>/api/backup/download` 这一种形状
        if base.endswith("/api/backup/download"):
            pass
        elif base.endswith("/api/plug"):
            base = f"{base}/backup/download"
        else:
            base = f"{base}/api/backup/download"
        self.exports.set_base_url(base)
        return base

    def download_link(self, token: str) -> str:
        """给用户看的完整链接。没配公网地址时退回裸路径（可下载但点不动）。"""
        base = self._public_base_url()
        if not base:
            return f"/backup/download/{token}"
        return f"{base.rstrip('/')}/{token}"

    async def terminate(self):
        self.scheduler.shutdown()
        self.db.close()
