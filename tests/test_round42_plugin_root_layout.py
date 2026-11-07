"""第 42 轮：插件**装不上**——宿主根本看不到它。

这一轮从一件最基础的事开始：不看代码，只按 README 的安装段装一遍。

```bash
cd AstrBot/data/plugins
git clone <本仓库> astrbot-plugin-Backup
```

装完，插件列表里没有它。日志只有一行：

```
插件 astrbot-plugin-Backup 未找到 main.py 或者 astrbot-plugin-Backup.py，跳过。
```

因为 `PluginManager._get_modules` 的判据就这一条：

```python
if os.path.exists(os.path.join(path, d, "main.py")):        module_str = "main"
elif os.path.exists(os.path.join(path, d, d + ".py")):      module_str = d
else:                                                       continue   # 跳过
```

而仓库把这些文件全收在了 `backup/` 子目录里。前面 41 轮 639 条用例没有一条发现
这件事，因为它们**都从「文件在哪」出发**（`Path(__file__).parent.parent`），没有
一条从「宿主怎么找插件」出发。`load_plugin_for_real` 更是直接骗过加载器：它自己
算出目录、自己 `spec_from_file_location`，等于把加载器的唯一判据绕开了。

所以这组用例不复用 `load_plugin_for_real`——它要验的正是那个函数绕开的东西。
"""

import importlib
import os
import sys
import types
from pathlib import Path

import pytest

from .conftest import real_astrbot_available

REPO_ROOT = Path(__file__).resolve().parent.parent

#: 宿主在插件目录根部要找的文件。缺一个，装上去之后就是「少一半功能」或者
#: 「装不上」，而不是报错——加载器不校验这些，它只是找不到就不找。
ROOT_FILES = [
    "main.py",
    "metadata.yaml",
    "_conf_schema.json",
    "requirements.txt",
    "config.yaml",
    "logo.png",
    "templates/chat.html",
    "assets/logo.svg",
]


@pytest.mark.parametrize("name", ROOT_FILES)
def test_host_visible_files_live_at_the_plugin_root(name):
    """宿主直接读的东西必须在插件根。旧布局上这一组 8 条全红。"""
    assert (REPO_ROOT / name).is_file(), (
        f"{name} 不在插件根（{REPO_ROOT}）。AstrBot 加载插件时只认根部的 "
        "main.py / metadata.yaml / _conf_schema.json / requirements.txt，"
        "收进子目录等于没装。"
    )


def test_no_stale_package_subdir_shadows_the_root():
    """旧的 `backup/` 目录不能再回来。

    它一旦回来，`import backup.core...` 会优先命中**那个目录**（而不是宿主给插件
    分配的模块名），于是同一份代码在进程里存在两份：一份是宿主加载的，一份是
    用例 import 的。改配置、改状态、注册表全对不上，且很难看出为什么。
    """
    assert not (REPO_ROOT / "backup").exists(), (
        "插件根下不该再有 backup/ 子目录：它是旧布局的残留，会让同一个模块"
        "在进程里出现两份"
    )


def test_loader_actually_sees_the_plugin(tmp_path, monkeypatch):
    """把仓库按用户的装法放好，让**宿主自己的** `_get_modules` 判一遍。

    这条是整轮的核心：它调的就是那段 `os.path.exists(...main.py)`，不经过任何
    测试侧的计算。旧布局上它返回 `[]`。
    """
    if not real_astrbot_available():
        pytest.skip("需要真 AstrBot（pip install astrbot==4.14.6）")

    import shutil

    plugins_dir = tmp_path / "plugins"
    plugin_dir = plugins_dir / "astrbot-plugin-Backup"
    shutil.copytree(
        REPO_ROOT,
        plugin_dir,
        ignore=shutil.ignore_patterns(
            "__pycache__", "tests", ".git", "storage", "data", ".pytest_cache"
        ),
    )

    import astrbot.api.event  # noqa: F401  正门，避开循环导入
    from astrbot.core.star.star_manager import PluginManager

    found = PluginManager._get_modules(str(plugins_dir))
    assert [m["pname"] for m in found] == ["astrbot-plugin-Backup"], (
        f"宿主在这个目录里没找到插件：{found}"
    )
    assert found[0]["module"] == "main"


def test_metadata_is_valid_for_the_host():
    """`metadata.yaml` 要能被宿主解析：name/desc/version/author 缺一就抛。"""
    import yaml

    raw = yaml.safe_load((REPO_ROOT / "metadata.yaml").read_text(encoding="utf-8"))
    assert isinstance(raw, dict)
    # 宿主允许 desc 与 description 二选一（`_load_plugin_metadata` 里补的）
    assert raw.get("desc") or raw.get("description")
    for key in ("name", "version", "author"):
        assert raw.get(key), f"metadata.yaml 缺 {key}，宿主会拒绝加载"


def test_schema_covers_every_key_the_code_reads():
    """`_conf_schema.json` 里有的项，`config.yaml` 里也得有。

    面板那份配置由 schema 摊出默认值，`config.yaml` 是给「手改文件」和「没装面板」
    两种场景兜底的。两边漂开的表现是：面板里看到的是 A，文件里写着 B，
    实际生效的取决于哪边先被读——这类漂移没法靠读代码发现。
    """
    import json

    import yaml

    schema = json.loads((REPO_ROOT / "_conf_schema.json").read_text(encoding="utf-8"))
    conf = yaml.safe_load((REPO_ROOT / "config.yaml").read_text(encoding="utf-8"))["backup"]

    def flatten(node, prefix=""):
        out = set()
        for key, spec in node.items():
            path = f"{prefix}{key}"
            out.add(path)
            if spec.get("type") == "object":
                out |= flatten(spec.get("items") or {}, f"{path}.")
        return out

    def flatten_conf(node, prefix=""):
        out = set()
        for key, value in node.items():
            path = f"{prefix}{key}"
            out.add(path)
            if isinstance(value, dict):
                out |= flatten_conf(value, f"{path}.")
        return out

    missing = flatten(schema) - flatten_conf(conf)
    assert not missing, f"config.yaml 缺这些项：{sorted(missing)}"


def test_no_test_writes_into_the_repository_config():
    """整个测试期间，仓库里的 `config.yaml` / `_conf_schema.json` 一个字节都不许变。

    这是我这轮踩的坑：新加的那条「填完 base_url 后回执链接能下载」没有注入面板
    配置，`_save_config` 于是退回写 `PLUGIN_DIR / config.yaml`——跑一次用例就把
    默认配置改成了测试当时的值，还把注释全洗掉了。表现是**另外 15 条用例一起变红**，
    而那 15 条验的是「默认配置该是什么」，它们读的正是被污染的那个文件。

    这类污染比用例失败更麻烦：失败信息指向的是被改坏的默认值，而不是「谁改的」。
    断言方式很土但有效——拿文件内容跟 git 里的版本比（没有 git 就跳过）。
    """
    import subprocess

    files = ["config.yaml", "_conf_schema.json", "metadata.yaml"]
    try:
        out = subprocess.run(
            ["git", "status", "--porcelain", "--"] + files,
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except Exception:
        pytest.skip("没有 git，无法判断是否被改过")
    if out.returncode != 0:
        pytest.skip("不是 git 工作区")
    # 注意：这条用例只在「跑完整轮之后」才有意义，放到文件末尾靠 pytest 的顺序。
    # 单独跑它时结果始终是干净的，不会假失败。
    assert out.stdout.strip() == "", (
        f"这些文件在测试期间被改写了：\n{out.stdout}\n"
        "测试不许写仓库里的配置文件：给插件注入一份配置（dict + save_config），"
        "或者 monkeypatch 掉 PLUGIN_DIR。"
    )
