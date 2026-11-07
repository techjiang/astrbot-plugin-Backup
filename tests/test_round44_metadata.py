"""第 44 轮：元数据说得好听，和实际能力对不上。

三件事挤在一起：

1. `display_name` 还是「Backup 高级备份」——面板卡片、市场列表里显示的就是它，
   作者要的名字是「Backup」；
2. `support_platforms` 压根没写，`tags` 只有三个泛词，`short_desc` / `astrbot_version`
   都没写。这些宿主都**会读**（`_load_plugin_metadata`），缺了不是报错，是「少一块」：
   面板上没有平台标签，市场里搜不到，版本不兼容时也不会拦；
3. 更要命的是「声明」和「能力」不对齐：插件真正能回捞历史消息的只有 aiocqhttp
   （OneBot 客户端透出 `get_group_msg_history` 等接口），可 `main.py` 里挑平台
   的候选名单里还挂着 telegram / discord / slack / wecom。把这些写进
   `support_platforms` 就等于承诺一个备不出东西的平台。

所以这一轮不只看「字段填全了没有」，还锁死**名单只有一个来源**：
`metadata.yaml::support_platforms` 必须等于 `core/platforms.py::HISTORY_PLATFORMS`。
"""

from __future__ import annotations

import re
import types
from pathlib import Path

import pytest
import yaml

from backup.core.platforms import HISTORY_PLATFORMS, PREFERRED_ORDER
from backup.tests.conftest import preload_host_for_lineage, real_astrbot_available

REPO_ROOT = Path(__file__).resolve().parent.parent
META_PATH = REPO_ROOT / "metadata.yaml"


@pytest.fixture(scope="module")
def meta():
    raw = yaml.safe_load(META_PATH.read_text(encoding="utf-8"))
    assert isinstance(raw, dict), "metadata.yaml 读出来不是字典"
    return raw


def test_display_name_is_just_backup(meta):
    """作者要的名字是「Backup」。`name` 不能跟着改——它同时是模块名和 plugin_id 的一半。"""
    assert meta["display_name"] == "Backup"
    assert meta["name"] == "backup", (
        "name 是宿主 import 用的模块名、也是市场 plugin_id 的前半段，"
        "改名等于换一个插件（已装的会变成两个）"
    )


#: 市场源里实际在用的分类 key。写成别的名字不会报错，只是永远不会被归到那一栏。
MARKET_CATEGORIES = {"entertainment", "utilities", "ai_tools", "integrations", "productivity"}


def test_category_is_the_market_key_for_tools(meta):
    """「分类：工具」在市场里叫 `utilities`。

    `category` 是**单值 key**，不是自由文本。写「工具」看着对，市场那边认不出来——
    这类错误没有报错，只有「分类栏里没有我」这一个症状。
    """
    assert meta.get("category") == "utilities", (
        f"分类该是 utilities（工具），现在是 {meta.get('category')!r}"
    )
    assert meta["category"] in MARKET_CATEGORIES


def test_short_desc_and_tags_are_present_and_sane(meta):
    """`short_desc` 是卡片紧凑布局用的，没有就退化成截断的 desc 长描述。"""
    short = meta.get("short_desc")
    assert isinstance(short, str) and 8 <= len(short) <= 60, (
        f"short_desc 长度该在 8–60 字之间，现在是 {short!r}"
    )
    tags = meta.get("tags")
    assert isinstance(tags, list) and len(tags) >= 5, "tags 太少，市场里搜不到"
    assert all(isinstance(t, str) and t.strip() for t in tags)
    assert len(set(tags)) == len(tags), "tags 有重复"
    assert [t for t in tags if t != t.lower()] == [], (
        "英文标签统一小写，免得同一件事算两个关键词"
    )
    # 市场里中文检索是常态（1300 多个插件里 780 个带中文标签），只给英文等于放弃一半入口
    assert any(any("\u4e00" <= c <= "\u9fff" for c in t) for t in tags), (
        "tags 里没有中文：市场的中文搜索搜不到这个插件"
    )


def test_readme_states_the_market_identity(meta):
    """README 顶上那张「插件信息」表得和 metadata 对得上。

    `category` 写进 yaml 之后，宿主不读、面板不显示——它唯二的出口是市场记录和
    README。市场那条要等发布才生效，README 这条是现在就能给用户看的。两边漂开
    就是文档在说谎，而用户没有任何别的途径发现。
    """
    text = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    assert "## 插件信息" in text, "README 缺「插件信息」一节"
    assert meta["display_name"] in text, f"README 没写插件名 {meta['display_name']!r}"
    assert meta["category"] in text, (
        f"README 没写市场分类 key {meta['category']!r}——用户会以为分类是自由文本"
    )
    for platform in meta["support_platforms"]:
        assert platform in text, f"README 没提支持平台 {platform}"
    assert meta["astrbot_version"] in text, f"README 没写 AstrBot 版本要求 {meta['astrbot_version']!r}"


def test_support_platforms_matches_what_the_code_can_actually_do(meta):
    """声明的平台必须就是 `core/platforms.py` 里那份「真能回捞历史」的名单。

    这是本轮的核心：声明多一个，用户就会在那个平台上看到「备份完成、0 条」；
    声明少一个，面板上不显示，但功能其实是有的。
    """
    declared = meta.get("support_platforms")
    assert isinstance(declared, list) and declared, "support_platforms 不能空着"
    assert tuple(declared) == tuple(HISTORY_PLATFORMS), (
        f"metadata.yaml 声明 {declared}，代码认为能拉历史的是 {list(HISTORY_PLATFORMS)}"
    )


def test_declared_platforms_are_real_adapter_keys(meta):
    """`support_platforms` 的值必须是宿主 `ADAPTER_NAME_2_TYPE` 的 key。

    写个自造的名字，面板上不会报错——只是永远不亮。装了 AstrBot 就拿宿主自己
    那张表比对；没装才退回文档里列的那份常量。
    """
    documented = {
        "aiocqhttp",
        "qq_official",
        "qq_official_webhook",
        "telegram",
        "wecom",
        "wecom_ai_bot",
        "lark",
        "dingtalk",
        "discord",
        "slack",
        "kook",
        "vocechat",
        "weixin_official_account",
        "weixin_oc",
        "satori",
        "misskey",
        "line",
        "matrix",
        "mattermost",
    }
    keys = documented
    if real_astrbot_available():
        from astrbot.core.star.filter.platform_adapter_type import ADAPTER_NAME_2_TYPE

        keys = set(ADAPTER_NAME_2_TYPE)
    unknown = [p for p in meta["support_platforms"] if p not in keys]
    assert not unknown, f"这些平台名宿主不认识：{unknown}"


def test_preferred_order_puts_history_capable_platforms_first():
    """挑平台的偏好顺序：能拉历史的排前面。

    原来 `_pick_by_capability` 只认「有没有客户端」，而 webchat、telegram、
    discord 也有客户端。名单里挂着它们等于告诉调度器「这些也行」，实际一备 0 条。
    """
    assert PREFERRED_ORDER[0] in HISTORY_PLATFORMS
    first_fallback = next(
        (i for i, name in enumerate(PREFERRED_ORDER) if name not in HISTORY_PLATFORMS),
        len(PREFERRED_ORDER),
    )
    assert all(
        name in HISTORY_PLATFORMS for name in PREFERRED_ORDER[:first_fallback]
    ), "能拉历史的平台必须全排在不能拉的前面"
    assert len(set(PREFERRED_ORDER)) == len(PREFERRED_ORDER), "偏好顺序里有重复项"


def test_astrbot_version_is_a_pep440_range_that_admits_the_current_host(meta):
    """`astrbot_version` 写错的表现不是报错，是**插件被拒绝加载**。

    所以既要能被 PEP 440 解析，也要把宿主自己的版本包含进去——写个 `>=4.17`
    而宿主是 4.14，装上去就是「版本不兼容」。
    """
    from packaging.specifiers import SpecifierSet

    spec = SpecifierSet(str(meta["astrbot_version"]))
    assert not str(meta["astrbot_version"]).lstrip().startswith("v"), "不要加 v 前缀"
    if not real_astrbot_available():
        return
    from astrbot.core.config import VERSION

    assert spec.contains(VERSION, prereleases=True), (
        f"metadata 要求 {meta['astrbot_version']}，但当前宿主是 {VERSION}，会拒绝加载"
    )


def test_repo_is_a_market_legal_https_github_url(meta):
    """市场规范：`repo` 必须是 `https://github.com/{owner}/{repo}` 形状。

    SSH、HTTP、非 GitHub、以及指到子目录/issue 的链接都不合法——插件市场会拒收。
    """
    repo = str(meta["repo"])
    pattern = r"^https://github\.com/[A-Za-z0-9_-]+/[A-Za-z0-9_-]+(\.git|/tree/[A-Za-z0-9_-]+)?$"
    assert re.match(pattern, repo), f"repo 不合市场规范：{repo}"


def test_plugin_identity_fields_are_clean_for_the_market(meta):
    """市场拿 `author/name` 当 plugin_id：两段都不能带 `/`、不能为空。"""
    for key in ("author", "name"):
        value = str(meta[key])
        assert value == value.strip() and "/" not in value, f"{key} 不合法：{value!r}"


def test_host_reads_the_fields_this_round_added(meta):
    """让宿主自己的 `_load_plugin_metadata` 读一遍，确认新增字段真被接住了。

    「写进 yaml」和「宿主读进来」是两件事：宿主只搬它认识的字段，写错的键名
    （比如 `short_description`）会被静默丢掉，面板上什么都不变。
    """
    if not real_astrbot_available():
        pytest.skip("需要真 AstrBot")

    preload_host_for_lineage()
    from astrbot.core.star.star_manager import PluginManager

    loaded = PluginManager._load_plugin_metadata(str(REPO_ROOT))
    assert loaded is not None
    assert loaded.name == "backup"
    assert loaded.display_name == "Backup"
    # 只核宿主这个版本**真有**的字段。`short_desc` / `support_platforms` /
    # `astrbot_version` 都是后加的：4.14.6 的 `StarMetadata` 里根本没有它们，
    # 4.16+ 才有。写出「一定读得到」就是把版本差异当成了 bug。
    import dataclasses

    fields = {f.name for f in dataclasses.fields(type(loaded))}
    if "short_desc" in fields:
        assert loaded.short_desc == meta["short_desc"]
    if "support_platforms" in fields:
        assert tuple(loaded.support_platforms) == tuple(meta["support_platforms"])
        # 宿主只保留字符串项，写个 dict 进去会被静默丢掉
        assert all(isinstance(p, str) for p in loaded.support_platforms)
    if "astrbot_version" in fields:
        assert loaded.astrbot_version == meta["astrbot_version"]


def test_brand_assets_use_the_new_name():
    """改名要改到底：banner / logo 的 `<title>` 还是旧名，分享出去就是两套叫法。"""
    for asset in ("assets/banner.svg", "assets/logo.svg"):
        text = (REPO_ROOT / asset).read_text(encoding="utf-8")
        title = re.search(r"<title>([^<]*)</title>", text)
        assert title, f"{asset} 没有 <title>"
        assert "高级备份" not in title.group(1), f"{asset} 标题还是旧名：{title.group(1)}"
        assert title.group(1).startswith("Backup"), f"{asset} 标题该以 Backup 开头"


def test_readme_documents_the_platform_boundary(meta):
    """README 得写清「哪些平台能备份历史、哪些只能实时归档」。

    文档和 `support_platforms` 各说一套，用户只会信那个更好看的。
    """
    text = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    assert "## 支持平台" in text
    for platform in meta["support_platforms"]:
        assert platform in text, f"README 没提 {platform}"
    assert "实时归档" in text, "要说明非 aiocqhttp 平台仍可实时归档，否则等于劝退"


def test_pick_prefers_history_capable_over_other_clients(monkeypatch, tmp_path):
    """真挑一次：telegram / discord 也有客户端，但不能抢在 aiocqhttp 前面。"""

    class _Meta:
        def __init__(self, name):
            self.name, self.id = name, name

    class _Platform:
        def __init__(self, name):
            self._meta = _Meta(name)

        def meta(self):
            return self._meta

        def get_client(self):
            return object()

    preload_host_for_lineage()
    from backup.main import BackupPlugin

    context = types.SimpleNamespace(
        registered=[],
        register_web_api=lambda *a, **k: None,
        platform_manager=types.SimpleNamespace(
            platform_insts=[_Platform("telegram"), _Platform("discord"), _Platform("aiocqhttp")]
        ),
    )
    plugin = BackupPlugin(
        context=context,
        config={
            "storage": {"storage_dir": str(tmp_path / "storage")},
            "retention": {"auto_clean": False},
            "schedule": {"enabled": False},
        },
    )
    assert plugin._resolve_platform(None).meta().name == "aiocqhttp"
