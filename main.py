"""Backup 高级备份 · AstrBot 插件入口。

作者：科技酱 · https://www.asoe.cn
"""

import json
import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register
from astrbot.core.star.filter.custom_filter import CustomFilter

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
from .core.download_route import LEGACY_PREFIX, handle_download, make_view
from .core.gateway import GATEWAY_PREFIX, DownloadGateway
from .core.export_manager import ExportManager
from .exporters import SUPPORTED_FORMATS
from .core.auto_trigger import AutoTrigger
from .core.event_hooks import (
    NOTICE_DEDUPE_SECONDS,
    EventHooks,
    extract_group_id,
    is_message_echo,
    notice_payload,
)
from .core.paths import assert_writable_target, resolve_storage_root
from .core.restore_engine import RestoreEngine, RestoreError
from .core.scheduler import BackupScheduler
from .core.verifier import compute_checksum, read_manifest, verify_zip

logger = logging.getLogger(__name__)

PLUGIN_DIR = Path(__file__).resolve().parent


#: 事件上记「这条已经被哪个钩子认领过」的键。`event.set_extra` 是 AstrBot 自己
#: 传参用的同一个通道（`activated_handlers` / `parsed_params` 都在里面），同一条
#: 事件在整条管道里是同一个对象，所以这个标记两个钩子都看得见。
_HOOK_CLAIM = "__backup_hook_claim"


def _looks_like_notice(event) -> bool:
    """这条事件是不是群通知（退群/踢人/解散/上传…）。

    `post_type` 是权威字段，只在 OneBot 原始事件里有，所以要摊一层
    （`notice_payload` 会顺着 `message_obj.raw_message` 往下找）。
    `poke` 例外：它是「有人戳了你一下」，是消息不是通知，AstrBot 甚至把它塞进了
    消息链（`message=[Poke(...)]`），走归档那条路才对。
    """
    payload = notice_payload(event)
    post_type = str(payload.get("post_type") or "").lower()
    subtype = str(payload.get("sub_type") or payload.get("subtype") or "").lower()
    if post_type == "notice" and subtype != "poke":
        return True
    return False


class _HookClaimFilter(CustomFilter):
    """给两个钩子用的自定义过滤器（`@filter.custom_filter`）。

    为什么非要有它：钩子必须能在**任何**事件上被调到，而不是「有人敲了某条命令」
    才跑。`@filter.command` 那套匹配是 `message_str.startswith("<命令名>")`，挂在
    钩子上等于给它判死刑——实时归档、退群聚合、解散/被踢备份三个功能全成空谈。

    用法上有个坑必须写下来：`register_custom_filter` 对**裸指令**做的是
    `custom_filter = custom_filter(raise_error)`——它**调**传进来的那个对象。
    所以：

    - 传**类** → 得到一个实例，`filter.filter(event, cfg)` 正常；
    - 传**实例** → `TypeError: object is not callable`，插件加载当场失败；
    - 传**工厂函数** → 得到的是工厂函数自己的返回值，而调用处写的是
      `filter.filter(...)`，会在每条事件上 `AttributeError: 'function' object
      has no attribute 'filter'`，`WakingCheckStage` 记一条错误并停止事件传播。

    后两种我都写出来过。所以这里固定用 `type(...)` 造一个「绑好 kind 的子类」
    再把它交给装饰器，既是类、又能带上参数。

    过滤器只做两件事：判断这条事件归哪个钩子、以及同一条事件别被两个钩子都认领。
    不做权限判断——归档和触发式备份是后台任务，不该因为发送者不是管理员就跳过。
    """

    def __init__(self, kind: str = "", raise_error: bool = True):
        super().__init__(raise_error)
        self.kind = kind

    def filter(self, event, _config=None):
        try:
            if event.get_extra(_HOOK_CLAIM):
                return False  # 另一个钩子已经认领，别走两遍
            if (self.kind == "notice") != _looks_like_notice(event):
                return False
            event.set_extra(_HOOK_CLAIM, self.kind)
            return True
        except Exception:  # 过滤器抛错会让整条消息的处理停摆，绝不能抛
            return False

    def __call__(self, event, cfg=None):
        return self.filter(event, cfg)

    def __str__(self):
        return self.__class__.__name__

    __repr__ = __str__


def _hook_filter(kind: str):
    """造一个绑好 `kind` 的过滤器**类**（不是实例，见 `_HookClaimFilter` 注释）。"""
    return type(
        f"_HookClaimFilter_{kind}",
        (_HookClaimFilter,),
        {"__init__": _init_with_kind(kind)},
    )


def _init_with_kind(kind: str):
    def __init__(self, raise_error: bool = True, *args, **kwargs):
        CustomFilter.__init__(self, raise_error)
        self.kind = kind

    return __init__


def _looks_like_archive(token: str) -> bool:
    """这个参数像不像一个备份包路径。

    备份 ID 的形状是 `bk_100_20261104052000_ab12cd`，不带扩展名；恢复命令的
    第一个参数有可能是包路径，靠扩展名区分，不靠「文件在不在」——路径打错时
    也得走「备份不存在」那条正常报错，而不是被当成备份 ID 报一句话。
    """
    text = str(token or "")
    return text.lower().endswith(".zip") or "/" in text or "\\" in text


#: 拼下载链接时一律不认的本机/内网地址：拼出来发到群里没人点得动
_PRIVATE_HOST_HINTS = (
    "localhost",
    "127.",
    "0.0.0.0",
    "10.",
    "192.168.",
    "169.254.",
    "[::1]",
)


def _header(headers, name: str) -> str:
    """大小写不敏感地取一个请求头。dict / Werkzeug Headers / aiohttp 都吃。"""
    try:
        value = headers.get(name)
        if value:
            return str(value)
    except Exception:
        pass
    try:
        for key, value in headers.items():  # type: ignore[union-attr]
            if str(key).lower() == name.lower():
                return str(value)
    except Exception:
        pass
    return ""


def _is_public_host(host: str) -> bool:
    bare = host.split("/")[0].strip().lower()
    if not bare:
        return False
    hostname = bare.rsplit(":", 1)[0] if bare.count(":") == 1 else bare
    if any(hostname.startswith(hint) for hint in _PRIVATE_HOST_HINTS):
        return False
    for prefix in ("172.16.", "172.17.", "172.18.", "172.19.", "172.2", "172.30.", "172.31."):
        if hostname.startswith(prefix):
            return False
    return True


def _astrbot_extensions_mount() -> bool:
    """宿主的插件路由是不是挂在 `/api/v1/plugins/extensions/<插件名>/` 下。

    新版面板（FastAPI）是这样；旧版（Quart）是 `/api/plug/<尾巴>`，没有插件名那层。
    判断依据只能是宿主自己有没有那条路由——两边都写着「插件路由」，形状却不同，
    猜错一边，群里点下载就是 404。

    读不到（没有宿主、版本更老）就当旧版，那是更早的既定行为。
    """
    try:
        from astrbot.dashboard.api import plugins as plugins_api  # type: ignore
    except Exception:
        return False
    router = getattr(plugins_api, "router", None)
    for route in getattr(router, "routes", None) or []:
        path = str(getattr(route, "path", "") or "")
        if "plugins/extensions" in path:
            return True
    return False


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
        # 上一次进程死在备份中途时，库里会留下一条永远 `running` 的记录：列表里
        # 看着像还在跑，恢复被「状态为 running，不可恢复」挡回去，而它其实
        # 已经保住了上千条消息。启动时把「开跑时刻早于本次启动」的那些收成
        # partial 并登记来源清单（见 `Database.reconcile_stale_records`）。
        # 宽限两分钟：`__init__` 与第一条命令之间正常就差几秒，把阈值卡在
        # 「现在」反而会把别处正在跑的那一轮误伤。
        try:
            stale = self.db.reconcile_stale_records(datetime.now() - timedelta(minutes=2))
            if stale:
                logger.warning("收编 %s 条中断的备份记录（宿主重启/进程退出）", stale)
        except Exception as e:  # 修不了旧记录也不能让插件起不来
            logger.warning("收编中断备份记录失败：%s", type(e).__name__)
        self.engine = BackupEngine(self.db, storage / "media", self.config)
        self.exports = ExportManager(
            self.db,
            storage / "exports",
            storage / "media",
            self.config,
        )
        # 媒体目录一并交给恢复引擎：从 ZIP 包恢复时媒体要落回本机，否则换台机器
        # 恢复完，聊天记录里每张图都是「本机找不到这个文件」，再导出一次就永久丢
        self.restore_engine = RestoreEngine(self.db, storage / "media")
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
        self._notice_keys: dict[str, datetime] = {}
        self._event_probe_done = False
        # 通知钩子里触发的那次「最后备份」不回话：群解散/被踢的现场，回复既发不出去
        # （群没了），也可能把被踢这件事再暴露给在场的人。手动 `/backup` 才回话
        self._silent_backup = False
        self.scheduler.restore_jobs()
        # 群内下载链接：宿主面板那条路在 4.14 上走不通（见 core/gateway.py），
        # 所以由内置网关提供。仍尝试登记面板路由——新版本宿主可能已经修好匹配逻辑。
        self.gateway = DownloadGateway(
            self.exports,
            (self.config.get("gateway") or {}).get("host") or "127.0.0.1",
            (self.config.get("gateway") or {}).get("port") or 0,
        )
        self.register_download_api()
        self._start_gateway()
        if self.config.get("retention", {}).get("auto_clean"):
            self.exports.clean_exports(
                int(self.config.get("retention", {}).get("keep_days", 90))
            )

    # ---------- 运行时 ----------
    async def _scheduled_backup(self, group_id: int) -> None:
        """定时任务入口：没有事件，用全局平台对象。"""
        await self._backup(group_id)

    @staticmethod
    def _silence_default_llm(event) -> None:
        """钩子把 `is_at_or_wake_command` 置真之后，得亲手把默认 LLM 链路关掉。

        `ProcessStage` 跑完所有被激活的 handler 之后，只要「没发过消息 + 被唤醒 +
        没禁 LLM」就接着走 `AgentRequestSubStage`。钩子为了被调用必须置真前两个
        标记（见上面 ④），于是第三个得由钩子自己关——否则归档一条群消息的代价是
        机器人回一句话，一条退群/撤回通知也一样。

        `should_call_llm(False)` 是宿主为此留的口子：只挡默认 LLM 请求，不挡插件
        自己的调用。老宿主/替身上没有这个方法时静默跳过——那是「禁不掉」，不是
        「必须出错」。
        """
        try:
            event.should_call_llm(False)
        except Exception:
            pass

    def _note_notice_once(self, event) -> bool:
        """第一次见到这条通知返回 True；重复投递返回 False。

        AstrBot 的分发是广播式的：`on_message` 与 `on_notice` 都挂着
        `EventMessageType.ALL`，同一条退群通知必然进两个函数。退群聚合是按
        **次数**累加的，所以必须在这里拦住第二次。

        原来这道闸在 `EventHooks.seen_notice` 里，用的是「事件对象上凑出来的
        指纹」。它的兜底逻辑是「指纹算不出来就直接放行」——而真实事件上算出来的
        就是空的（`message_obj.group_id` 是个 property，`_attr` 按设计绕开
        property 读，于是读到 None）。闸判断「没有指纹」，每次放行，等于没装。

        所以指纹改从 `notice_payload()` 摊出来的字段里取：那里已经有群号、
        事件类型、发送者。这些是**结构字段**，不含正文（红线 1）。

        只认「用户维度」的变化（`user_id` / `target_id`）——同一个人退两次是两次
        事件，得数两次；`operator_id` 不进指纹：一个管理员连踢 10 个人，10 条
        通知的操作者是同一个，拿它当归一化键会把 10 次退群并成 1 次。
        """
        from .core.event_hooks import notice_payload as _payload

        payload = _payload(event)
        group_id = payload.get("group_id")
        subtype = payload.get("sub_type") or payload.get("subtype") or ""
        who = payload.get("user_id") or payload.get("target_id") or ""
        kind = payload.get("notice_type") or payload.get("post_type") or ""
        if group_id in (None, "") and not subtype:
            return True  # 连「是什么」都不知道，别拦
        key = f"{group_id}|{kind}|{subtype}|{who}"
        now = datetime.now()
        cutoff = now - timedelta(seconds=NOTICE_DEDUPE_SECONDS)
        for old_key in [k for k, t in self._notice_keys.items() if t < cutoff]:
            self._notice_keys.pop(old_key, None)
        last = self._notice_keys.get(key)
        self._notice_keys[key] = now
        return last is None or last < cutoff

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
        link = self.download_link(result.token, event)
        hint = ""
        if link.startswith("/"):
            # 裸路径 = 发进群里没人点得动。两种原因分开说，别让人去查错的那一项
            if getattr(self, "gateway", None) is not None and self.gateway.running:
                hint = (
                    f"\n⚠️ 这条链接只有本机能用（网关监听在 {self.gateway.host}），"
                    "发到群里点不动。把 `export.base_url` 填成网关的**对外地址**"
                    "（反代/内网穿透的域名），或让宿主面板这一层反代到网关端口，再重新导出"
                )
            elif (self.config.get("gateway") or {}).get("enabled", True) and getattr(
                self.gateway, "last_error", ""
            ):
                # 配了 enabled=true 却没起来：这时再说「把 gateway.enabled 打开」
                # 就是叫人去查一项本来就对的东西。网关自己记着原因（端口被占 /
                # 主机名写错），直接搬过来。
                hint = f"\n⚠️ 内置网关没起来：{self.gateway.last_error}"
            else:
                hint = (
                    "\n⚠️ 没有可用的下载入口：内置网关没起来，也没配 `export.base_url`。"
                    "把 `gateway.enabled` 打开、或把 `export.base_url` 填成网关的对外地址"
                    "（反代/内网穿透）后重新导出"
                )
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
        # 「第一个参数是包」这件事只看形状，不看 `target` 有没有解析出来。
        # 原来写的是 `if target is None and _looks_like_archive(backup_id)`，于是
        # `/backup restore <包> <群号>` —— 换机器恢复时**最常写的那条**——两头落空：
        # target 有值（不是 None），第一个条件不成立；`len(tokens) > 2` 也不成立
        # （只有两个参数）。结果 `archive` 留在 None，包路径被当成备份 ID 去查库，
        # 报一句「备份 …zip 不存在（本机没有这条记录，也没有给备份包）」——
        # 而那个包就躺在回执给的那个路径上。
        if _looks_like_archive(backup_id):
            # 第一个参数是包：`<包>` 或 `<包> <群号>`，两种都从包里取备份 ID。
            # 唯一不能取的情况是包里的 manifest 也读不出来——那时 `read_manifest`
            # 会给一份空 dict，落到文件名当 ID，恢复时按「包不完整」拦下
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
        # 媒体跟着回来了几条必须说：只说「写入 3 条」，用户打开导出的 HTML 发现
        # 全是「本机找不到这个文件」，会以为消息恢复了、附件丢了
        if result.media_restored:
            parts.append(f"媒体 {result.media_restored} 个已落回本机")
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
            yield event.plain_result(f"{'✅' if ok else '❌'} {reason}")
            return
        # 不给包时，原来这里只回一句「本机记录校验和：<16 位十六进制>」外加一个
        # ✅。那个 ✅ 什么都没验——它只是把库里存着的字符串截了一半打出来。
        # 而「重新算一遍再比」在这里是完全做得到的：来源清单 + 现存的库就是
        # 备份当时那批数据。所以现在真比：对不上就直接说哪几类不一致。
        messages = self.db.get_source_messages(record.backup_id, record.group_id)
        if not messages:
            # 来源清单空着不等于数据被删。老记录（前几轮建的）没有清单，
            # `checksum` 也是空串；把它判成「损坏」会误伤一大批正常备份。
            if not record.checksum:
                yield event.plain_result(
                    "⚠️ 这条记录没有校验和（早期版本建的备份），无法比对完整性。"
                    "可以用 `/backup verify <备份ID> <备份包路径>` 校验手里的包"
                )
                return
            yield event.plain_result(
                f"❌ 备份 `{record.backup_id}` 登记过消息，本机库里一条都没剩下；"
                "消息可能已被清理。如果你手里有导出包，用 "
                "`/backup verify <备份ID> <备份包路径>` 校验那个包"
            )
            return
        actual = compute_checksum(messages)
        if actual == record.checksum:
            yield event.plain_result(
                f"✅ 校验通过：`{record.backup_id}` 的 {len(messages)} 条消息与备份时的校验和一致"
            )
            return
        yield event.plain_result(
            f"❌ 校验不一致：`{record.backup_id}` 现在库里的 {len(messages)} 条消息"
            f"重算得 `{actual[:16]}`，记录里是 `{record.checksum[:16]}`"
            "——消息内容被改过、或被别的读改写路径覆盖过（例如 overwrite 恢复）。"
            "校验的是消息本身，不是备份包；包请用 `--` 后面的路径参数单独验"
        )

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
        # 改完立刻让运行中的组件跟着变。不做这一步，回执上那句「✅ 已设置」只说
        # 配置字典变了：`media.timeout` 还是 30 秒、`export.base_url` 还是空、
        # `schedule.enabled=false` 关不掉已经挂上的任务——15 个配置项无一例外，
        # 全都得重载插件才生效，而回执里一个字都没提。
        note = self._apply_config()
        tail = "" if saved else "（仅本次运行生效）"
        yield event.plain_result(
            f"✅ `{key}` = `{_config_get(self.config, key)}`{tail}{note}"
        )

    # ---------- 配置生效 ----------
    def _apply_config(self) -> str:
        """把 self.config 的当前值灌进所有运行中的组件，返回要写进回执的说明。

        为什么要有这一步：`__init__` 只在插件加载时读一次配置，之后每个组件都把
        值抄进自己的属性里（信号量、token 有效期、超时、阈值、开关……）。改配置
        字典对这些属性没有任何作用——`config set` 于是成了一句「已写在纸上」，
        而干活的还是老参数。

        处理不了的只有一处：存储目录。它牵扯正在打开的 SQLite 连接和已经落盘的
        媒体/导出文件，热切等于把库和文件劈成两半，所以只提示「重启才生效」。
        """
        notes: list[str] = []

        self.engine.media.reconfigure(self.config.get("media") or {})
        self.exports.reconfigure(self.config)

        permission_cfg = self.config.get("permission") or {}
        self.permission.require_admin = bool(permission_cfg.get("require_admin", True))
        self.permission.allow_private = bool(permission_cfg.get("allow_private", True))

        self.hooks.reconfigure(self.config)
        self.hooks.reconfigure_triggers(self.config)
        self.auto_trigger.reconfigure(self.config.get("auto_trigger") or {})

        schedule_cfg = self.config.get("schedule") or {}
        self.scheduler.reconfigure(
            bool(schedule_cfg.get("enabled", True)),
            str(schedule_cfg.get("default_cron") or ""),
        )
        # 网关的开关和端口是「绑定/解绑一个 socket」，不是读个值：关掉要真的
        # shutdown，打开要真的 listen，改端口还得先关再开。同样在这里一次做完
        notes.extend(self._apply_gateway())

        # 存储目录：改不了，只能如实说
        target = resolve_storage_root(self.config, PLUGIN_DIR)
        try:
            same = target.resolve() == self.storage.resolve()
        except OSError:
            same = False
        if not same:
            notes.append(
                f"\n⚠️ `storage_dir` 改成 {target} 要重启插件才生效"
                f"（当前仍在使用 {self.storage}，库和媒体文件不会自动搬家）"
            )

        return "".join(notes)

    def _apply_gateway(self) -> list[str]:
        """按当前配置启停内置网关。返回需要写进回执的说明。"""
        cfg = self.config.get("gateway") or {}
        want = bool(cfg.get("enabled", True))
        host = str(cfg.get("host") or "127.0.0.1")
        port = int(cfg.get("port") or 0)
        current = self.gateway

        if not want:
            if current is not None and current.running:
                current.stop()
            return []
        if current is not None and current.running:
            if current.host == host and current.port == port:
                return []
            # 换地址/端口：先下线，否则新实例绑不上（旧 socket 还占着）
            current.stop()
        self.gateway = DownloadGateway(self.exports, host, port)
        if self.gateway.start():
            return []
        return [f"\n⚠️ 内置网关没起来：{self.gateway.last_error}"]

    # ---------- 事件钩子 ----------
    #
    # 这两个钩子前几轮一直是「代码在、单测绿、一次都没跑过」。下面这四条都是
    # 真装 AstrBot 4.14.6、跑它的 `WakingCheckStage` / `StarRequestSubStage`
    # 才看见的，按踩到的顺序记：
    #
    # ① 只挂 `@filter.event_message_type`：`WakingCheckStage` 对「event_filters
    #    为空」的 handler 直接 `continue`，钩子永远不被调用。
    # ② 补 `@filter.command("__backup_archive")` 当登记位：handler 进得了注册表，
    #    但命令过滤是 `message_str.startswith("__backup_archive")`，真实聊天里
    #    永远不成立——钩子仍旧不跑。
    # ③ 补 `@filter.custom_filter(...)` 并把它放在 `command` 下面：过滤器到位了，
    #    可 `CommandFilter` 还在——它一样会把整条事件判掉。所以最后把
    #    `@filter.command` 整个撤掉，钩子自报家门。
    # ④ 最后一步：`event.is_at_or_wake_command = True`。`WakingCheckStage` 只把
    #    这个标记为真的 handler 交给 `StarRequestSubStage`，而真实事件里它是
    #    False（没人 @ 机器人、也没人敲命令）。钩子是后台任务，不靠人唤醒。
    #
    # 装饰器顺序：`command` 在最上（先建出 handler_md）→ `custom_filter`
    # （把认领过滤器追加到它身上，放 `command` 上面会追加到上一个 handler）→
    # `event_message_type` 在最下（最先执行、先登记事件类型）。
    @filter.custom_filter(_hook_filter("archive"))
    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_message(self, event: AstrMessageEvent):
        """实时归档：消息进群就落库，之后增量备份不必再回捞。

        只写库不下载媒体，也不回复任何内容——群聊里静默干活。
        """
        # `WakingCheckStage` 只把 `is_at_or_wake_command` 为真的 handler 交给
        # ProcessStage，而真实群聊里没人 @ 机器人也不会敲命令——不补这一行，
        # 钩子只会在注册表里躺着。归档是后台任务，不靠人唤醒。
        event.is_at_or_wake_command = True
        # 「被唤醒」是宿主的状态位，不只是我们自己的开关。没人 @ 机器人、也没敲
        # 唤醒词，群友只是正常聊天：这个标记留着 True，`ProcessStage` 结束掉所有
        # handler 后会接着把这条消息送进默认 LLM 链路——备份插件于是对**每一句
        # 群聊**插嘴。钩子自己伪造出来的唤醒，得自己关掉。
        self._silence_default_llm(event)
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

    @filter.custom_filter(_hook_filter("notice"))
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
        # 同 `on_message`：没人 @ 机器人时这条标记是 False，钩子不会被交给
        # ProcessStage。通知处理是后台任务，不靠人唤醒。
        event.is_at_or_wake_command = True
        event.is_wake = True
        # 更要紧的是这里：退群、撤回、戳一戳这些通知宿主也包成事件丢进管道，
        # 置真唤醒标记后默认 LLM 链路照跑——机器人会对着一条**退群通知**现编一段
        # 话发进群。
        self._silence_default_llm(event)
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
        # 数之前先问一句「这条通知是不是刚被另一个 handler 数过」。问在
        # `handle_notice` **之前**：退群聚合是累加的，数完再撤不回来。
        if not self._note_notice_once(event):
            logger.debug("同一条通知已被处理过，跳过：%s", extract_group_id(event))
            return
        group_id, subtype, should_backup, _counted = self.hooks.handle_notice(
            payload, event
        )
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

    #: 插件名。新版面板要求注册的路由**以插件名开头**，否则匹配不上：
    #: `_match_registered_web_api` 拿 `/api/v1/plugins/extensions/<插件名>/<尾巴>`
    #: 里的尾巴去比 `/<插件名>/...`，只写 `/backup/download` 的永远命中不了。
    PLUGIN_NAME = "backup"

    #: 要登记的下载路由，顺序即优先级（宿主按顺序精确匹配）
    #:
    #: 两条形态各来一份：
    #: - `/<插件名>/backup/download`：新版面板（FastAPI）按插件名挂在
    #:   `/api/v1/plugins/extensions/<插件名>/` 下，只有这个前缀能命中；
    #: - `/backup/download`：旧版面板（Quart）按 `route == f"/{subpath}"` 精确比，
    #:   没有任何插件名前缀。
    #:
    #: 尖括号那条（`<token>`）两条形态都留着——旧面板比不中它，新面板能，无害。
    DOWNLOAD_ROUTES = (
        "/backup/download",
        "/backup/download/<token>",
        "/download/<token>",
        f"/{PLUGIN_NAME}/backup/download",
        f"/{PLUGIN_NAME}/backup/download/<token>",
    )

    #: 能拉取群历史消息的平台类型。备份的全部数据都从这几个平台来
    HISTORY_CAPABLE = ("aiocqhttp", "telegram", "discord", "slack", "wecom")

    def _platform_by(self, key, by_id: bool):
        """按 id / 名字 / 「随便哪个」找平台实例。

        原来没有 `key` 时直接 `return insts[0]`。而 AstrBot 的 `platform_insts`
        第一位永远是 **webchat**（`PlatformManager.load_webchat_platform` 先装它，
        排在所有真实 IM 适配器前面）——于是定时备份、以及任何走「全局找平台」
        的路径，拉到的都是 webchat，`get_user_info` 一类动作直接抛，备份一条
        也拉不到，报的还是「当前没有可用的平台适配器」这种看不懂的话。

        现在没给 key 时按「这个平台有没有客户端」挑：`aiocqhttp` 的
        `CQHttp`、telegram 的 `client` 都有，webchat 没有。一个都没有的部署
        （纯 webchat）才退回第一个。
        """
        ctx = self.context
        fn = getattr(ctx, "get_platform_inst", None)
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
        return self._pick_by_capability(insts)

    def _pick_by_capability(self, insts: list):
        """挑一个真能拉数据的平台：先看有没有客户端，再看平台类型。"""
        with_client = []
        for inst in insts:
            getter = getattr(inst, "get_client", None)
            client = getter() if callable(getter) else None
            if client is None:
                client = getattr(inst, "client", None)
            if client is not None:
                with_client.append(inst)
        candidates = with_client or insts
        for inst in candidates:
            meta = inst.meta() if callable(getattr(inst, "meta", None)) else None
            name = str(getattr(meta, "name", "") or "").lower()
            if name in self.HISTORY_CAPABLE:
                return inst
        return candidates[0]

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
        """尝试把下载视图登记到宿主面板上。返回值只说明「登记成功」，不代表能下。

        AstrBot 4.14.6 的 `dashboard/server.py` 里有两条同时成立的硬约束：

        1. `srv_plug_route` 是
               if route == f"/{subpath}" and request.method in methods:
           我们注册的是**字面量**，`subpath` 是真实尾巴。`"/backup/download/<token>"`
           永远不等于任何具体路径——尖括号不参与比较，它只在注册时被当作可用路径
           登记，宿主这边却是拿来做字符串相等的。所以带 token 的路径**匹配不到**。
        2. `auth_middleware` 的白名单是源码里的常量，按前缀放行
           `/api/backup/download`；插件能挂的 `/api/plug/*` **不在其中**，一律要面板
           JWT。而群里点链接的人没有 JWT。唯一免鉴权的那条前缀又被宿主自己的
           `BackupRoute.download_backup` 占了。

        上一版我据「白名单里有 /api/backup/download」推断「群里点链接是通的」，那是
        又拿一半事实当全部：那条前缀属于宿主，不属于插件。`make_view` 留着，是因为
        这两条约束都是**宿主版本相关**的——哪天匹配逻辑改成 startswith、或者白名单
        开放给插件，这里就能直接生效。真正兜底的是 `core/gateway.py` 的内置网关。

        登记的是固定路径 `/backup/download`：它是唯一可能在 `route ==` 精确比较下
        命中的形状（`/api/plug/backup/download`），token 从 `?token=` 取。命中之后
        仍然过不了鉴权——所以这条路在当前版本上只能算「尽力」。
        """
        registrar = getattr(self.context, "register_web_api", None)
        if not callable(registrar):
            logger.debug("宿主没有 register_web_api，下载链接改由内置网关提供")
            return False
        view = make_view(self.exports)
        registered = []
        for route in self.DOWNLOAD_ROUTES:
            try:
                registrar(route, view, ["GET"], "Backup 导出文件下载（需 token）")
                registered.append(route)
            except Exception as e:  # 宿主版本差异，注册不上不影响备份本身
                logger.warning("下载路由 %s 注册失败：%s", route, type(e).__name__)
        if registered:
            logger.info("下载路由已登记：%s", "、".join(registered))
        # 前缀记账：即使当前宿主下走不通，链接拼装仍是「宿主给的挂载点 + 尾段」
        self.exports.set_route_prefix(*self._host_mount())
        return bool(registered)

    def _host_mount(self) -> tuple:
        """宿主把插件路由挂在哪个前缀下。返回 (前缀, route 尾段)。

        - 新版面板：`/api/v1/plugins/extensions/<插件名>/` + `/backup/download`；
        - 旧版面板：`/api/plug/` + `/backup/download`。

        前缀和尾段都**不带 token**——`/backup/download` 这条固定路径是两种挂载都
        认的形状（路由匹配时 token 从路径或 query 里取，见 `download_route`）。
        """
        if _astrbot_extensions_mount():
            return (
                f"/api/v1/plugins/extensions/{self.PLUGIN_NAME}",
                "/backup/download",
            )
        return LEGACY_PREFIX, "/backup/download"

    @staticmethod
    def _headers_of(source) -> dict:
        """从一个「可能是 request、也可能是事件」的对象上取请求头。

        `/backup export` 拿得到的是 `AstrMessageEvent`，不是 HTTP 请求：事件上
        没有 `request`，请求头藏在平台的原始事件里（`message_obj.raw_message`
        的 `headers`，或者某些适配器直接挂在事件上）。取不到就返回空字典，
        调用方按「不知道这条命令从哪来」处理。
        """
        if source is None:
            return {}
        for getter in (
            lambda o: o.headers,                      # 真请求
            lambda o: o.request.headers,              # 包了一层的
            lambda o: o.message_obj.raw_message.get("headers")
            if isinstance(getattr(o.message_obj, "raw_message", None), dict)
            else None,                                # OneBot 原始事件
            lambda o: getattr(o, "request_headers", None),
        ):
            try:
                headers = getter(source)
            except Exception:
                continue
            if headers:
                return headers
        return {}

    @classmethod
    def _host_from_request(cls, request) -> str:
        """从"这条消息是从哪个地址进来的"反推公网源。拿不到返回空串。

        反代后面 `Host` 头是内网地址、真实域名在 `X-Forwarded-Host` 里；协议同理
        看 `X-Forwarded-Proto`。内网/本机地址一律不认——`192.168.x.x` 拼出来的
        链接发到群里没人点得动，不如老实说"没配公网地址"。
        """
        headers = cls._headers_of(request)
        if not headers:
            return ""
        host = _header(headers, "X-Forwarded-Host") or _header(headers, "Host")
        host = str(host or "").split(",")[0].strip()
        if not host or not _is_public_host(host):
            return ""
        proto = str(_header(headers, "X-Forwarded-Proto") or "").split(",")[0].strip()
        if proto not in ("http", "https"):
            proto = "https" if str(getattr(request, "scheme", "") or "") == "https" else "http"
        return f"{proto}://{host}"

    def _start_gateway(self) -> bool:
        """起内置网关。没开或起不来都只影响「链接点不点得动」，不影响备份本身。"""
        cfg = self.config.get("gateway") or {}
        if not cfg.get("enabled", True):
            logger.info("内置下载网关未启用（gateway.enabled=false）")
            return False
        return self.gateway.start()

    def download_link(self, token: str, request=None) -> str:
        """给用户看的完整链接。

        顺序（前面能定就不再往后找）：

        1. `export.base_url`——你明确指定的对外地址；
        2. **这条命令是从哪个地址进来的**：`Host` / `X-Forwarded-Host`，
           内网与本机地址一律不认（见 `_host_from_request`）；
        3. 网关照旧给本机回环地址。

        第 2 条修的是「`request` 收进来后一次都没用」（第 33 轮）。这轮把
        `_public_base_url` 整个删了——它是**第二个**「写全了、零调用点」的：
        拼的是面板那条路的链接前缀，而那条路在 4.14 上已被证伪（见
        `download_route.py` 末尾）。面板路由登记仍然留着（宿主版本会变），但
        链接前缀只有这一个来源：`<对外地址><GATEWAY_PREFIX><token>`。

        全都没有时退回裸路径——明确是「点不动」，不是假装能用。
        """
        export_cfg = self.config.get("export") or {}
        explicit = str(export_cfg.get("base_url") or "").strip()
        if explicit:
            # `base_url` 是**对外域名**：网关只监听回环，后面那层反代/穿透负责
            # 把它接出去，链接的路径就是网关自己的 `/backup/download/<token>`。
            return f"{explicit.rstrip('/')}{GATEWAY_PREFIX}{token}"
        public = self._host_from_request(request)
        if public:
            # 网关只监听回环，对外那层是反代。请求本身是从群成员点得到的域名
            # 进来的，那个域名就是唯一现成的真相
            return f"{public}{GATEWAY_PREFIX}{token}"
        if getattr(self, "gateway", None) is not None and self.gateway.running:
            # 网关起来了但只有回环地址：`http://127.0.0.1:<端口>/...` 发进群，
            # 群成员点的是**他自己电脑**的 127.0.0.1，一辈子点不开，而回执上
            # 看起来像条正常链接。这就是第 30 轮「装真 AstrBot 才看见」的那个
            # bug 的原形，上一版在「没有网关」时修了，有网关时又原样漏着。
            # 链子只回裸路径，让上层明说「点不动」。
            return f"/backup/download/{token}"
        # 没有网关也没有对外地址：给裸路径，让上层打「点不动」的提示。
        # 这里刻意不拼宿主的挂载前缀（`/api/plug/backup/download`）——那条路在
        # 4.14 上既比不中带 token 的路径、又要面板 JWT。给一条看着像能用、实际
        # 404/401 的链接，比给裸路径更糟。
        return f"/backup/download/{token}"

    async def terminate(self):
        try:
            if getattr(self, "gateway", None) is not None:
                self.gateway.stop()
        except Exception:
            pass
        self.scheduler.shutdown()
        self.db.close()
