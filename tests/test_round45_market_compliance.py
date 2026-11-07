"""第 45 轮：上架审核退回的两条——日志记录器、数据落点。

审核意见（1.0.0 被拒）说得比它看起来更具体：

1. `main.py` 和 `core/` 下八个模块写着 `import logging` +
   `logging.getLogger(__name__)`。宿主的日志面板只认 `astrbot.api.logger`
   （带插件名过滤、带面板队列），自建 logger 记的东西用户在面板上一条也看不到——
   等于把「备份失败了」这句话写在了一个没人读的本子里。
2. `core/paths.py` 的默认根是 `~/.astrbot-backup`。上架规范要求插件数据落在
   宿主给的插件数据目录，理由是那个位置才跟着迁移、备份、清理走。
   放在家目录下，用户卸掉插件之后那堆备份留在谁也不知道的地方。

两条都改完了，这一轮把「改过」钉成「不许回退」：

- 日志只能从 `astrbot.api` 拿。连 `import logging` 本身都不许有——
  有它就一定有人再写一句 `getLogger`。
- 默认目录必须落在 `<data>/plugin_data/backup`，且**装在哪都能找到那个 data**
  （`<data>/plugins/<目录名>` 是唯一可靠的线索，不能靠 CWD）。
- 文档、面板 schema、`config.yaml` 里的默认值不许再说 `~/.astrbot-backup`：
  三处说一套、代码做另一套，用户按文档找不到自己的备份。
"""

import ast
import json
import re
from pathlib import Path

import pytest
import yaml

from backup.core.paths import LEGACY_ROOT, PLUGIN_NAME, default_root, resolve_storage_root

REPO_ROOT = Path(__file__).resolve().parent.parent

#: 插件自己的 Python 源文件（`tests/` 不算：那是给这套用例自己用的）。
PLUGIN_SOURCES = sorted(
    [REPO_ROOT / "main.py"]
    + [p for p in (REPO_ROOT / "core").glob("*.py")]
    + [p for p in (REPO_ROOT / "exporters").glob("*.py")]
)


def _plugin_sources():
    assert PLUGIN_SOURCES
    for path in PLUGIN_SOURCES:
        if path.name == "__init__.py":
            continue
        yield path


def test_no_module_builds_its_own_logger():
    """`import logging` + `getLogger` 是审核点名的写法，一个都不许留。

    用 AST 找 `import logging`、`from logging import ...`、以及任何
    `logging.getLogger(...)` 调用——注释和文档字符串里提一嘴不算违规，
    真的 import 了才算。
    """
    offenders = []
    for path in _plugin_sources():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name == "logging" or alias.name.startswith("logging."):
                        offenders.append(f"{path.relative_to(REPO_ROOT)}:{node.lineno} import logging")
            elif isinstance(node, ast.ImportFrom) and (node.module or "").startswith("logging"):
                offenders.append(f"{path.relative_to(REPO_ROOT)}:{node.lineno} from logging import")
            elif isinstance(node, ast.Call):
                func = node.func
                if isinstance(func, ast.Attribute) and func.attr == "getLogger":
                    offenders.append(f"{path.relative_to(REPO_ROOT)}:{node.lineno} getLogger()")
    assert not offenders, "自建 logger 在宿主面板上一条也看不到：\n" + "\n".join(offenders)


def test_every_module_that_logs_imports_the_host_logger():
    """用 `logger.` 的模块必须有 `from astrbot.api import logger`，反之亦然。"""
    missing = []
    for path in _plugin_sources():
        source = path.read_text(encoding="utf-8")
        uses = re.search(r"^\s*logger\.", source, flags=re.M) is not None
        imports = "from astrbot.api import logger" in source
        if uses and not imports:
            missing.append(f"{path.relative_to(REPO_ROOT)} 用了 logger 却没从 astrbot.api 取")
        if imports and not uses:
            missing.append(f"{path.relative_to(REPO_ROOT)} import 了 logger 却一次没用")
    assert not missing, "\n".join(missing)


# ---------------------------------------------------------------- 数据落点


def _installed_plugin_dir(tmp_path, plugin_name="astrbot-plugin-Backup"):
    """照宿主装法摆一个目录：`<data>/plugins/<插件目录名>`。"""
    plugin_dir = tmp_path / "data" / "plugins" / plugin_name
    plugin_dir.mkdir(parents=True)
    return plugin_dir


def test_default_storage_root_is_the_plugin_data_dir(tmp_path):
    """默认根 = `<data>/plugin_data/backup`，不是家目录。"""
    plugin_dir = _installed_plugin_dir(tmp_path)
    root = resolve_storage_root({}, plugin_dir)
    assert root == (tmp_path / "data" / "plugin_data" / PLUGIN_NAME).resolve()
    assert root.is_dir()


def test_default_root_follows_wherever_the_host_puts_plugins(tmp_path):
    """换个 data 位置（docker 挂载、多实例）也得跟着走，不能靠 CWD 猜。"""
    data = tmp_path / "srv" / "astrbot" / "data"
    plugin_dir = data / "plugins" / "astrbot-plugin-Backup"
    plugin_dir.mkdir(parents=True)
    assert default_root(plugin_dir) == (data / "plugin_data" / PLUGIN_NAME).resolve()


def test_plugin_data_dir_name_comes_from_one_place():
    """目录名和 `metadata.yaml` 的 `name` 是同一个字符串，改一处必须改另一处。"""
    meta = yaml.safe_load((REPO_ROOT / "metadata.yaml").read_text(encoding="utf-8"))
    assert PLUGIN_NAME == meta["name"]


def test_legacy_home_root_is_reference_only():
    """旧目录只留作提醒：`~/.astrbot-backup` 不许再当默认值写进去。"""
    assert LEGACY_ROOT == Path.home() / ".astrbot-backup"
    # 代码里除了这条提醒，不该再有人读它
    source = (REPO_ROOT / "core" / "paths.py").read_text(encoding="utf-8")
    assert source.count("LEGACY_ROOT") >= 2, "旧目录至少要定义 + 提醒各一处"


def test_custom_storage_dir_still_wins(tmp_path):
    """规范允许自定义位置——只要不在 Web 根内。"""
    plugin_dir = _installed_plugin_dir(tmp_path)
    target = tmp_path / "mnt" / "big-disk"
    assert resolve_storage_root({"storage_dir": str(target)}, plugin_dir) == target.resolve()


# ---------------------------------------------------------------- 文案


def test_docs_do_not_advertise_the_legacy_default():
    """README / schema / config.yaml 不许再把旧目录写成默认值。

    「代码改了、文档没改」在这里是最贵的一种：用户按文档去 `~/.astrbot-backup`
    找备份，找不到，然后以为备份没成功。
    """
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    schema = json.loads((REPO_ROOT / "_conf_schema.json").read_text(encoding="utf-8"))
    conf = yaml.safe_load((REPO_ROOT / "config.yaml").read_text(encoding="utf-8"))["backup"]

    for where, text in (
        ("_conf_schema.json", schema["storage_dir"]["description"]),
        ("config.yaml", (REPO_ROOT / "config.yaml").read_text(encoding="utf-8")),
    ):
        assert "plugin_data" in text, f"{where} 没写新的默认目录"
        assert "~/.astrbot-backup" not in text or "早期" in text, (
            f"{where} 还把 ~/.astrbot-backup 当默认值"
        )

    assert "data/plugin_data/backup" in readme, "README 没写数据落在插件数据目录"
    assert conf.get("storage_dir") == "", "config.yaml 的 storage_dir 必须留空才走默认"
