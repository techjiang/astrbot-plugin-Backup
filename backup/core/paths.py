"""存储路径安全：红线 3「备份文件不要放在 Web 可访问的目录」。

插件目录常落在 AstrBot 的 data 下，面板若把 data 静态托管，`storage/` 就是公开的。
默认把数据放到用户家目录下的 `.astrbot-backup`，并拒绝落在配置声明的 Web 根之内。
"""

import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

DEFAULT_ROOT = Path.home() / ".astrbot-backup"


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


def resolve_storage_root(config: dict | None, plugin_dir: str | Path) -> Path:
    """算出数据根目录。

    规则：
    - 配了绝对路径就用它
    - 配了相对路径，相对插件目录解析（保持旧行为，但会告警）
    - 没配用默认家目录，避开 Web 目录
    """
    cfg = config or {}
    raw = cfg.get("storage_dir")
    if raw:
        expanded = Path(os.path.expandvars(str(raw))).expanduser()
        if expanded.is_absolute():
            root = expanded.resolve()
        else:
            # 相对路径相对插件目录解析。若按 CWD 解析会随启动位置漂移
            root = (Path(plugin_dir).resolve() / expanded).resolve()
            logger.warning(
                "storage_dir 是相对路径，会落在插件目录 %s 下；建议改成绝对路径",
                root,
            )
    else:
        root = DEFAULT_ROOT
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
