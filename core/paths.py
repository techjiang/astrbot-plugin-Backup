"""存储路径安全：默认落在宿主给插件的数据目录，且拒绝落在 Web 根之内。

插件装在上架规范允许的位置——`data/plugin_data/<插件名>`。自定义 `storage_dir`
仍按配置解析，但必须不在 `storage.web_roots` 内，否则启动即抛 `UnsafeStoragePath`。
"""

import os
from pathlib import Path

from astrbot.api import logger

#: 插件在宿主里的名字（`metadata.yaml` 的 `name`）。默认数据目录按它命名。
PLUGIN_NAME = "backup"

#: 0.9 及以前版本把数据放在家目录下这个目录里。只用于启动时提醒，不再写它。
LEGACY_ROOT = Path.home() / ".astrbot-backup"


class UnsafeStoragePath(RuntimeError):
    """存储目录落到了 Web 可访问路径，拒绝启动。"""


def _expand(value: str | Path) -> Path:
    return Path(os.path.expandvars(str(value))).expanduser().resolve()


def is_inside(path: Path, root: Path) -> bool:
    try:
        Path(path).resolve().relative_to(Path(root).resolve())
        return True
    except ValueError:
        return False


def _plugin_data_parent(plugin_dir: str | Path) -> Path:
    """宿主放插件持久化数据的目录（`data/plugin_data`）。

    插件装在哪，数据就跟着去哪：`<data>/plugins/<目录名>` 的父目录名字就是现成的
    路标，比猜 CWD 可靠。装到别处（开发态）才问宿主的路径工具，再不行按 CWD 兜底。
    """
    root = Path(plugin_dir).resolve()
    if root.parent.name == "plugins":
        return root.parent.parent / "plugin_data"
    try:
        from astrbot.core.utils.astrbot_path import get_astrbot_plugin_data_path

        return Path(get_astrbot_plugin_data_path())
    except Exception:
        return Path.cwd() / "data" / "plugin_data"


def default_root(plugin_dir: str | Path) -> Path:
    """默认数据根：宿主插件数据目录下的 `backup/`。"""
    return (_plugin_data_parent(plugin_dir) / PLUGIN_NAME).resolve()


def resolve_storage_root(config: dict | None, plugin_dir: str | Path) -> Path:
    """算出数据根目录。

    规则：
    - 没配用默认的插件数据目录（`data/plugin_data/backup`）
    - 配了绝对路径就用它
    - 配了相对路径，相对插件目录解析（保持旧行为，但会告警）
    """
    cfg = config or {}
    raw = cfg.get("storage_dir")
    if raw:
        expanded = Path(os.path.expandvars(str(raw))).expanduser()
        if expanded.is_absolute():
            root = expanded.resolve()
            base = default_root(plugin_dir)
            if not is_inside(root, base):
                logger.warning(
                    "storage_dir 指向插件数据目录之外（%s），数据不会随插件一起迁移或清理；"
                    "想跟着宿主走就留空",
                    root,
                )
        else:
            # 相对路径相对插件目录解析。若按 CWD 解析会随启动位置漂移
            root = (Path(plugin_dir).resolve() / expanded).resolve()
            logger.warning(
                "storage_dir 是相对路径，会落在插件目录 %s 下；建议改成绝对路径",
                root,
            )
    else:
        root = default_root(plugin_dir)
        if LEGACY_ROOT.is_dir() and any(LEGACY_ROOT.iterdir()):
            logger.warning(
                "旧版数据还留在 %s，当前默认目录是 %s；把 storage_dir 指过去才会用到它",
                LEGACY_ROOT,
                root,
            )
    root.mkdir(parents=True, exist_ok=True)
    return root


def assert_writable_target(root: Path, web_roots: list[str] | None = None) -> Path:
    """数据根不能落在任何 Web 根目录内，否则备份等于挂在公网。"""
    for item in web_roots or []:
        if not str(item).strip():
            continue
        base = _expand(item)
        if is_inside(root, base):
            raise UnsafeStoragePath(
                f"storage 目录 {root} 落在 Web 可访问路径 {base} 内，"
                f"请改 storage_dir 或调整 storage.web_roots"
            )
    return root
