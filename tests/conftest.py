import importlib.util
import sys
import types
from datetime import datetime, timedelta
from pathlib import Path

import pytest

#: 插件目录 = 仓库根。宿主（AstrBot 的 `PluginManager._get_modules`）只认
#: `<插件目录>/main.py` 或 `<插件目录>/<目录名>.py`，所以这些文件必须摊在根上，
#: 不能收进子目录。代价是「插件包」的模块名由宿主决定（`data.plugins.<目录名>`），
#: 而用例需要一个写死的名字才能 import。
#:
#: 所以这里把根目录**按固定名 `backup` 注册进 `sys.modules`**：`__path__` 指向
#: 仓库根，`backup.core.database` 这类导入于是和以前一模一样。不去 `sys.path` 里
#: 找 `backup`——安装后的目录名是 `astrbot-plugin-Backup`（带连字符，import 语句
#: 写不出来），测试时是本地的任意目录名，两者都不该成为用例的前提。
_PLUGIN_ROOT = Path(__file__).resolve().parent.parent
if "backup" not in sys.modules:
    _spec = importlib.util.spec_from_file_location(
        "backup",
        _PLUGIN_ROOT / "__init__.py",
        submodule_search_locations=[str(_PLUGIN_ROOT)],
    )
    _pkg = importlib.util.module_from_spec(_spec)
    sys.modules["backup"] = _pkg
    _spec.loader.exec_module(_pkg)
sys.path.insert(0, str(_PLUGIN_ROOT.parent))

from backup.core.database import Database  # noqa: E402
from backup.core.models import GroupInfo, GroupMember, Message, MessageType  # noqa: E402

BASE_TIME = datetime(2024, 5, 1, 10, 0, 0)


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "db" / "backup.sqlite")
    yield database
    database.close()


def make_message(i: int, group_id: int = 100, sender: int = 1, content: str = None, mtype=None) -> Message:
    return Message(
        message_id=f"m{i}",
        group_id=group_id,
        sender_id=sender,
        sender_name=f"用户{sender}",
        timestamp=BASE_TIME + timedelta(minutes=i),
        message_type=mtype or MessageType.TEXT,
        content=content if content is not None else f"消息内容 {i}",
        raw_content="{}",
    )


@pytest.fixture
def messages():
    return [make_message(i) for i in range(5)]


class FakePlatform:
    """假平台：可配置历史消息和失败行为。"""

    def __init__(self, history=None, members=None, info=None, page_size=50):
        self.history = history or []
        self.members = members or []
        self.info = info or {"group_id": 100, "group_name": "测试群"}
        self.page_size = page_size
        self.history_calls = 0

    async def get_group_info(self, group_id):
        return self.info

    async def get_group_members(self, group_id):
        return self.members

    async def get_group_history(self, group_id, count=50, offset=0):
        self.history_calls += 1
        return self.history[offset : offset + count]


def raw_history(n: int, group_id: int = 100) -> list[dict]:
    """构造平台原始消息 dict。"""
    out = []
    for i in range(n):
        out.append(
            {
                "message_id": f"m{i}",
                "group_id": group_id,
                "sender": {"user_id": 1 + i % 3, "nickname": f"用户{i % 3}"},
                "timestamp": (BASE_TIME + timedelta(minutes=i)).timestamp(),
                "message": [{"type": "text", "data": {"text": f"消息 {i}"}}],
            }
        )
    return out


# ---- AstrBot 替身：让 main.py 在容器里也能 import（没有真 AstrBot）----


def install_real_astrbot(monkeypatch):
    """若装了 AstrBot，删掉替身、让真包生效；否则装替身。

    `sys.modules` 的优先级高于 `sys.path`：一旦装了替身，后面再 import 拿到的
    还是替身。所以这里不"二选一铺一套"，只做一件事——**装了真的就用真的**。
    """
    if _real_astrbot_importable():
        # 真货能用就**整组**卸掉替身。只摘掉带 `__astrbot_stub__` 标记的那几个
        # 不够：`astrbot.core` 下还有 `star` / `star.filter` / `star.filter.custom_filter`
        # 这些子模块名，真包一旦被摘掉，剩下的假子模块就变成「父包不在」的孤儿，
        # 下一次 `import astrbot.core.star.star` 直接 ModuleNotFoundError——
        # 而「真货装着」这个判断还是真的。用例于是全炸在 helper 里。
        unload_astrbot_modules()
        return None
    return install_fake_astrbot(monkeypatch)


def _real_astrbot_importable() -> bool:
    """真货**能不能拉起来**，而不是「包在不在」。

    `find_spec` 在这里会骗人：装了 astrbot 但它的依赖不全时，本文件末尾那份
    `astrbot.api` 替身躺在 `sys.path[0]`，`find_spec("astrbot")` 找到的是替身，
    判成「真货在」，结果整轮用例倒在一个根本不存在的宿主上。判据只能是
    「import 得动吗」。
    """
    import importlib
    import importlib.machinery
    import sys

    unload_astrbot_modules()
    # 先弄清「载入器」是什么：`astrbot` 底下挂了 pytest 的 AssertionRewritingHook
    # 之后，`sys.modules` 里的空壳模块和被我们清掉的真模块混在一起，真包进来时
    # 会有一半子模块已经 import 过——`astrbot.core` 于是「存在但没有 `__path__`」，
    # `import astrbot.core.star.star` 报「No module named」。
    # 探测期间换成标准 `PathFinder`，把真包的路径先摸出来，摸完就还原。
    path_finder = importlib.machinery.PathFinder
    meta_path = sys.meta_path[:]
    sys.meta_path[:] = [f for f in meta_path if f is not path_finder]
    sys.meta_path.append(path_finder)
    try:
        _settle_with_expected_error(importlib.import_module, "astrbot")
        importlib.import_module("astrbot.api.event")
        importlib.import_module("astrbot.core.star.star_handler")
        return True
    except Exception:
        unload_astrbot_modules()
        return False
    finally:
        sys.meta_path[:] = meta_path


#: 替身模块自己的名字。`unload_astrbot_modules` 只认这个集合——
#: 不能靠 `sys.modules` 里的当前状态：替身被摘掉之后，嵌在其中的子模块对象
#: 还挂在别处（`backup.main` 里那句 `from astrbot.core.star.filter.custom_filter
#: import CustomFilter` 已经把假模块绑进自己的名字空间），下一轮它们被重新
#: 装回 `sys.modules` 时就不带标记了，真包于是半真半假。
_STUB_MODULE_NAMES: set[str] = set()


def install_fake_astrbot(monkeypatch):
    class AstrMessageEvent:  # noqa: N801
        pass

    class Context:
        def __init__(self):
            self.registered = []

        def register_web_api(self, route, view, methods, desc):  # noqa: ARG002
            self.registered.append(route)

    class Star:
        def __init__(self, context):
            self.context = context

    def register(*_a, **_k):
        def deco(cls):
            return cls

        return deco

    class Filter:
        class EventMessageType:
            ALL = "ALL"

        @staticmethod
        def command(_name):
            return lambda fn: fn

        @staticmethod
        def event_message_type(_kind):
            return lambda fn: fn

        @staticmethod
        def custom_filter(_filter):
            # 真 AstrBot 里 `register_custom_filter` 会 `custom_filter(raise_error)`
            # 再把它挂到 handler 上；替身只要保证「是个装饰器」即可
            return lambda fn: fn

    api_event = types.ModuleType("astrbot.api.event")
    api_event.AstrMessageEvent = AstrMessageEvent
    api_event.filter = Filter
    api_star = types.ModuleType("astrbot.api.star")
    api_star.Context = Context
    api_star.Star = Star
    api_star.register = register
    class CustomFilter:
        """`main.py` 里 `import` 了它（`_hook_filter` 用它造认领过滤器）。

        替身里原本没有 `astrbot.core`，于是**装了 astrbot 但依赖不全**的机器上
        `import backup.main` 会炸在这一行：`astrbot.core` 是包自己带的，只有
        `astrbot.api` 有替身。结果是整轮 pytest 在收集阶段中断。
        """

        kind = ""

        def __init__(self, kind=""):
            self.kind = kind

        def filter(self, event, cfg):  # noqa: ARG002
            return True

    pkg = types.ModuleType("astrbot")
    pkg.__path__ = []
    core = types.ModuleType("astrbot.core")
    core.__path__ = []
    core_star = types.ModuleType("astrbot.core.star")
    core_star.__path__ = []
    core_filter = types.ModuleType("astrbot.core.star.filter")
    core_filter.__path__ = []
    custom_filter = types.ModuleType("astrbot.core.star.filter.custom_filter")
    custom_filter.CustomFilter = CustomFilter
    # 打上标记：`install_real_astrbot` 靠它认出「这是替身，真货来了就撤」
    for _mod in (pkg, core, core_star, core_filter, custom_filter):
        _mod.__astrbot_stub__ = True
        _STUB_MODULE_NAMES.add(_mod.__name__)
    api = types.ModuleType("astrbot.api")
    api.__path__ = []
    api.__astrbot_stub__ = True
    api.event = api_event
    api.star = api_star
    for name, mod in (
        ("astrbot", pkg),
        ("astrbot.api", api),
        ("astrbot.api.event", api_event),
        ("astrbot.api.star", api_star),
        ("astrbot.core", core),
        ("astrbot.core.star", core_star),
        ("astrbot.core.star.filter", core_filter),
        ("astrbot.core.star.filter.custom_filter", custom_filter),
    ):
        monkeypatch.setitem(sys.modules, name, mod)
    return Context


#: 这几条走的是「**替身**契约」：它们验的是插件自己的接线，`astrbot.api` 只要
#: 契约对得上就够了。真 AstrBot 的 `Context()` 要 11 个位置参数，`filter.command`
#: 还带真实注册语义——拿真货跑这些用例，验的就不再是插件了。
FAKE_HOST_FILES = frozenset(
    {
        "test_round20_plugin_wiring.py",
        "test_round21_gateway_and_config.py",
        "test_round22_link_and_deps.py",
        "test_round24_real_quart_and_notice.py",
        "test_round25_event_and_platform.py",
    }
)


def unload_astrbot_modules() -> None:
    """摘掉替身，让真包在**干净**的 `sys.modules` 下重新加载。

    不能一律 `pop`：真包一旦已经 import 过（`astrbot.core.db.po` 里有 SQLAlchemy
    表定义），再执行一遍 `__init__` 就是 `Table 'platform_stats' is already
    defined`。所以只清「替身登记过的名字」。
    """
    import sys

    for name in [n for n in list(sys.modules) if n == "astrbot" or n.startswith("astrbot.")]:
        sys.modules.pop(name, None)


def _ensure_real_host(importlib) -> None:
    """保证 `sys.modules` 里现在躺的是**完整**的真 AstrBot。

    为什么必须每轮检查、而且检查得这么仔细：走「替身契约」的那些文件会整包换掉
    `sys.modules["astrbot"]`，而 `backup.tests` 里的文件互相 import —— 一次替身
    会把**后面所有真加载用例**一起带歪。判据不看标记（假模块被重新装回去时不带
    标记），只看两件事：顶层 `astrbot` 有 `__file__`，且 `astrbot.core` 也真的
    从磁盘上加载过。两者缺一，就按「从头来一遍」处理。

    `astrbot.core.__init__` 里 `os.makedirs` 幂等，所以从头来一遍是安全的；
    真正不能碰的是**已经 import 完成的** `astrbot.core.db.po`（SQLAlchemy 表
    重复定义），所以那里只在「父包本身就是残的」时候才摘。
    """
    import sys

    top = sys.modules.get("astrbot")
    if top is not None and getattr(top, "__file__", None):
        core = sys.modules.get("astrbot.core")
        try:
            importlib.import_module("astrbot.core.star.star_handler")
            return
        except Exception:
            pass
    _settle_with_expected_error(importlib.import_module, "astrbot")


def _settle_with_expected_error(importer, name: str) -> None:
    """把 `sys.modules` 里的替身拆干净，然后**重新** import 真包。

    为什么不能只 `import astrbot` 了事：前面用替身的用例把这些名字占住了
    （`astrbot`、`astrbot.core`、`astrbot.core.star` …）。占着的位置上真包不会
    重跑 `__init__`，于是 `astrbot.core.message` 这类「父包顺手 import 又 bind
    上去」的子模块永久缺失——`import astrbot.core.message.components` 会报
    「cannot import name 'message' from astrbot.core」。

    所以：
    1. 摘掉所有**替身登记过的名字**（不碰已经 import 完成的真模块，
       `astrbot.core.db.po` 里的 SQLAlchemy 表定义不能再来一遍）；
    2. 把 `astrbot.core` 也摘掉——它要是「替身当道时跑的 `__init__`」，上面
       提到的那些绑定永远补不回来；
    3. 从磁盘重新走一遍 `astrbot` 和 `astrbot.api.event`。
    """
    import sys

    unload_astrbot_modules()
    top = sys.modules.get("astrbot")
    if top is not None and not getattr(top, "__file__", None):
        sys.modules.pop("astrbot", None)
    if "astrbot.core" in sys.modules:
        sys.modules.pop("astrbot.core", None)
        _forget_sqlalchemy_tables()
    try:
        importer(name)
        importer("astrbot.api.event")
    except Exception:
        # 重建失败（表重复、插件装一半……）就退回替身。这些用例要的是「宿主契约
        # 在那儿」，不是「宿主必须是真的」——真货那几个文件自己有 skip 判据。
        unload_astrbot_modules()
        _forget_sqlalchemy_tables()
        install_fake_astrbot(_SysModulesProxy())
        return


def _forget_sqlalchemy_tables() -> None:
    """清掉 `astrbot.core.db.po` 留在 MetaData 上的表定义（重复 import 用）。

    注意 `po` 可能**已经不在** `sys.modules` 里了（上一次清理摘掉过），但表还
    留在全局 `SQLModel.metadata` 上——所以清理动作不能依赖 `po` 在不在。
    """
    import sys

    po = sys.modules.pop("astrbot.core.db.po", None)
    if po is None:
        try:
            from sqlmodel import SQLModel

            SQLModel.metadata.clear()
        except Exception:
            pass
        return
    # astrbot 用的是 SQLModel：表定义挂在 `SQLModel.metadata` 这个**全局**元数据上，
    # 不是 `po.Base`。重新执行一遍 `po.py` 就是「同一张表定义两次」。
    try:
        from sqlmodel import SQLModel

        # 先把 `sqlmodel` 自己记着的表摘掉：`metadata.clear()` 只清元数据，
        # `sqlmodel.main` 里还有一份 `_sqlmodel_relationships` / 类注册表，
        # 重新执行 `po.py` 时 `Table(...)` 仍会撞已有名字。
        for table in list(SQLModel.metadata.tables.values()):
            try:
                SQLModel.metadata.remove(table)
            except Exception:
                pass
        SQLModel.metadata.clear()
    except Exception:
        pass
    # SQLModel 生成的具体表类（`PlatformStat` 等）继承 `TimestampMixin`，`__table__`
    # 已经挂上去了；清掉，让重新执行 `po.py` 时能重新建。
    for value in list(vars(po).values()):
        if isinstance(value, type) and "__table__" in getattr(value, "__dict__", {}):
            try:
                del value.__table__
            except Exception:
                pass
    sys.modules.pop("astrbot.core.db.po", None)


def _drop_half_real_astrbot() -> None:
    """摘掉「半真半假」的 astrbot。

    典型形状：`astrbot` 是真包（有 `__file__`），`astrbot.core` 却是替身空壳
    （`__file__` 是 None）。此时 `from astrbot.core import astrbot_config` 直接
    ImportError，而 `real_astrbot_available()` 还认为真货在。规则很简单——
    **没有 `__file__` 的就是假的**（替身都是 `types.ModuleType` 现造的）。
    """
    import sys

    for name in [n for n in list(sys.modules) if n == "astrbot" or n.startswith("astrbot.")]:
        module = sys.modules.get(name)
        if module is None:
            continue
        if getattr(module, "__file__", None) is None:
            sys.modules.pop(name, None)


def preload_host_for_lineage() -> bool:
    """这些文件验的是**插件自己的接线**，有真货用真货，没有就退回替身。

    为什么要走 `sys.modules` 而不是 `import`：装了 AstrBot 但它的依赖不全时
    `import astrbot` 直接抛，而这些文件里那句 `import backup.main` 会跟着炸在
    **收集阶段**——整轮 pytest 一条用例都不跑。替身是同一个 `astrbot.api`
    契约的一份实现，这些用例本来就只依赖那个契约。

    真货在的时候绝不装替身：那样「真加载器」的用例才会验到真东西。
    """
    import sys

    caller = Path(sys._getframe(1).f_globals.get("__file__", "")).name
    if caller not in FAKE_HOST_FILES and real_astrbot_available():
        return True

    # 顺序问题：前面某个用例装过替身、后面又有人 `install_real_astrbot` 撤掉，
    # 那种「半真半假」的状态会留到下一个用例（`astrbot.core` 是替身、`astrbot`
    # 是真货 → `cannot import name 'astrbot_config'`）。这里先把残留清干净。
    unload_astrbot_modules()
    install_fake_astrbot(_SysModulesProxy())
    # 让下一次 `import backup.main` 重新按新的 `sys.modules` 走一遍
    sys.modules.pop("backup.main", None)
    return False


class _SysModulesProxy:
    """`install_fake_astrbot` 要的是一个 `monkeypatch`，这里只借用它的
    `setitem` —— 用例结束不还原，因为替身要活到整轮结束。"""

    def setitem(self, mapping, key, value):
        mapping[key] = value


# ---- 真 AstrBot 才走的分支 ----

#: 真 AstrBot 的 `register_command` 用 `get_handler_full_name`（`<module>_<name>`）
#: 做注册表键，**而且命中已有条目时直接返回旧的**（`get_handler_or_create`）。
#: 两份不同的插件代码撞进同一条用例进程时，第二次注册拿回的是上一份代码的
#: handler —— 用例于是永远测不到新函数。所以「真 AstrBot 下加载插件」这件事
#: 统一走这个 reset。
def reset_star_registries():
    # 先走正门把真包拉起来：`astrbot.core.star.star` 直接 import 时，``astrbot.core.star``
    # 的 `__init__` 会一环环 import 下去，撞上「父包还没装完」的循环导入。
    import astrbot.api.event  # noqa: F401
    import importlib

    module = importlib.import_module("astrbot.core.star.star")
    star_map = module.star_map
    star_registry = module.star_registry
    star_handlers_registry = importlib.import_module(
        "astrbot.core.star.star_handler"
    ).star_handlers_registry

    star_map.clear()
    star_registry.clear()
    star_handlers_registry.star_handlers_map.clear()
    star_handlers_registry._handlers.clear()


def real_astrbot_available() -> bool:
    """能 import astrbot 的**核心模块**才算「这个环境验得了」。

    原来只判 `find_spec("astrbot") is not None`——那是「包在不在」，不是「能不能
    用」。两种情况下这个判据是骗人的：

    - 先跑了 `install_fake_astrbot`（它往 `sys.modules` 里塞了替身），`find_spec`
      随后就能找到那个假包，真货其实没装；
    - 装了 astrbot 但版本不对/依赖缺，`astrbot.core.star.star_handler` 拉不起来。

    两种都让整组用例在**收集阶段**炸掉，整轮 pytest 直接中断——一条用例都不跑，
    比 skip 更糟：看上去像代码坏了。
    """
    import importlib

    # 先 `astrbot.api.event`：直接 import `astrbot.core.star.star_handler` 会撞上
    # AstrBot 自己的循环导入（`astrbot.core.star.__init__` → register → 回读
    # `StarMetadata`），只有从 `api.event` 那条正门进来才拉得顺。
    for name in ("astrbot.api.event", "astrbot.core.star.star_handler"):
        try:
            module = importlib.import_module(name)
        except Exception:
            return False
        if getattr(module, "__astrbot_stub__", False):
            return False
    return True


#: 真货在这一轮里已经拉起来过没有（见 `load_plugin_for_real`）
_REAL_HOST_READY = False


@pytest.fixture(scope="session", autouse=True)
def _lineage_env_ready():
    """整轮开头先按真 AstrBot 的样子把宿主拉起来（如果装得动）。

    为什么要在这里做：`backup/tests` 里的文件互相 import（`test_round32` 从
    `test_round28` 借 helper），于是**收集阶段**就会有 `import astrbot...`。
    而那些走「替身契约」的文件（round20–25）也在收集阶段往 `sys.modules` 里塞
    假包。真货一旦被假包顶掉、事后再摘，`astrbot.core` 这个**已经 import 过**的
    包不会重跑 `__init__`，`astrbot.core.db.po` 里的 SQLAlchemy 表定义再来一遍
    就是 `Table 'platform_stats' is already defined`——整组用例全炸。

    所以顺序定死：**第一个**碰 `sys.modules['astrbot']` 的必须是真货。
    """
    if _real_astrbot_importable():
        global _REAL_HOST_READY
        import importlib

        importlib.import_module("astrbot.api.event")
        importlib.import_module("astrbot.core.star.star_handler")
        _REAL_HOST_READY = True
    yield


def load_plugin_for_real(monkeypatch, plugin_dir=None, config=None):
    """用真 AstrBot 的 `Star`/`Context`/注册表把插件**真加载一遍**。

    返回值 `(plugin, context, module)`。加载方式照 `star_manager` 的做法：
    以目录名当模块名（`metadata.yaml` 的 `name: backup`）import 那个包。

    `plugin_dir` 默认指向仓库里的 `backup/`；想造场景（比如改名、加一个坏文件）
    就传一份临时 copy 的路径。
    """
    import importlib
    import importlib.util
    import sys
    from pathlib import Path

    plugin_dir = Path(plugin_dir or Path(__file__).resolve().parent.parent)
    # 前面用替身的用例在 `sys.modules` 里留了一份假 `astrbot`（`astrbot.core` 是个
    # 空壳模块）。假货不摘，`import backup_under_test.main` 拿到的是替身；
    # 直接摘，真包又可能把已经 import 过的子模块（sqlalchemy 表定义一类）重新
    # 执行一遍——`Table 'platform_stats' is already defined`。所以**同一批里只
    # 摘一次**：本文件第一次进这里摘干净，之后不再动。
    # 每轮都确认一遍「现在的 `sys.modules` 里是真货」：前面那些走替身契约的用例
    # 会把整包换掉，而 `_REAL_HOST_READY` 是模块级变量，记的是「这轮以前弄好过」，
    # 不是「现在还好着」。只在 import 失败时才动手（`module_from_spec` /
    # `exec_module` 都认 `sys.modules`），成功就一行不碰——真包已经 import 过的
    # 子模块（sqlalchemy 表定义）绝不能重跑。
    _ensure_real_host(importlib)
    reset_star_registries()

    name = "backup_under_test"
    for mod_name in [m for m in list(sys.modules) if m == name or m.startswith(name + ".")]:
        del sys.modules[mod_name]

    spec = importlib.util.spec_from_file_location(
        name,
        plugin_dir / "__init__.py",
        submodule_search_locations=[str(plugin_dir)],
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    # 插件包自己的 import 一旦抛，这个半成品模块会留在 `sys.modules` 里，
    # 后面所有用例拿到的都是「加载失败的那一份」——真包于是怎么都拉不起来。
    # 失败就把自己和它带进去的子模块一起摘掉，原样往上抛。
    try:
        spec.loader.exec_module(module)
    except BaseException:
        for mod_name in [m for m in list(sys.modules) if m == name or m.startswith(name + ".")]:
            del sys.modules[mod_name]
        raise
    main_mod = importlib.import_module(f"{name}.main")

    from astrbot.core.star.context import Context

    context = Context(
        event_queue=None,
        config=_astrbot_host_config(),
        db=None,
        provider_manager=None,
        platform_manager=_FakePlatformManager(),
        conversation_manager=None,
        message_history_manager=None,
        persona_manager=None,
        astrbot_config_mgr=None,
        knowledge_base_manager=None,
        cron_manager=None,
    )
    plugin = main_mod.BackupPlugin(context=context, config=config)

    # `star_manager` 在实例化插件之后会做一次绑定（`star_manager.py` 里
    # 「Restore decorator-registered callables before binding the current
    # instance」那段）：把注册表里的**裸函数**取回来，再
    # `functools.partial(raw_handler, instance)`。
    # 不做这一步，注册表里躺着的是 `BackupPlugin.backup` 这个**未绑定**函数，
    # `call_handler` 调 `handler(event)` 就少一个 `self`——
    # `TypeError: missing 1 required positional argument: 'event'`。
    # 这段不补，本文件搭的场景和真实加载差着最后一步，测出来的东西不可信。
    import functools
    from astrbot.core.star.star_handler import star_handlers_registry

    for handler in star_handlers_registry.get_handlers_by_module_name(f"{name}.main"):
        raw = handler.handler.func if isinstance(handler.handler, functools.partial) else handler.handler
        handler.handler = functools.partial(raw, plugin)

    return plugin, context, main_mod


def _astrbot_host_config():
    from astrbot.core import astrbot_config

    return astrbot_config


class _FakePlatformManager:
    """`Context.get_platform_inst` 会遍历它。空的是真的要测的那一种情形。"""

    platform_insts: list = []

    def get_insts(self):
        return list(self.platform_insts)
