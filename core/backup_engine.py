"""备份引擎。一次备份 = 一次完整的数据快照（全量或增量）。"""

import asyncio
import inspect
import json
import logging
import mimetypes
import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional

from .database import REALTIME_BACKUP_ID, Database
from .incremental import IncrementalTracker
from .media_downloader import MediaDownloader
from .models import BackupRecord, BackupStatus, Message
from .parser import (
    parse_announcements,
    parse_group_files,
    parse_group_info,
    parse_group_members,
    parse_message,
)
from .verifier import compute_checksum

logger = logging.getLogger(__name__)

PAGE_SIZE = 50
#: 单页失败的退避表（秒）。备份是长任务，一页抖掉就整轮作废太亏：500 条的群
#: 第 5 页断一次，前面 200 条白跑，而下一轮又从头翻一遍。
PAGE_RETRY_DELAYS = (1, 3, 5)


def _raw_id(raw) -> str:
    """平台原始消息的 ID，用于判断翻页是否原地打转。取不到就退化成整条内容的指纹。"""
    if isinstance(raw, dict):
        value = raw.get("message_id") or raw.get("id") or raw.get("message_seq")
        if value:
            return str(value)
        return json.dumps(raw, ensure_ascii=False, sort_keys=True, default=str)[:512]
    value = getattr(raw, "message_id", None) or getattr(raw, "id", None)
    return str(value) if value else repr(raw)[:512]


def _raw_key(raw) -> Optional[float]:
    """这条消息的「先后」标量：优先时间戳，退到 message_id / message_seq。

    只用来比较同一页内谁更旧，不要求和平台时钟一致——所以解析不出来就返回
    None，让调用方退化成「按数组位置猜」。
    """
    if isinstance(raw, dict):
        values = (raw.get("timestamp"), raw.get("time"), raw.get("message_id"), raw.get("message_seq"))
    else:
        values = (
            getattr(raw, "timestamp", None),
            getattr(raw, "time", None),
            getattr(raw, "message_id", None),
        )
    for value in values:
        try:
            return float(value)
        except (TypeError, ValueError):
            continue
    return None


def _unwrap_messages(result) -> Optional[list]:
    """把历史消息接口的返回值拆成「消息列表」，拆不出来返回 None。

    OneBot v11 的 `data` 长这样：`{"messages": [...]}`，而 `_call` 原样交回。
    上一轮 `_fetch_page` 只在 `result` 是 dict 时取 `.get("messages")`——可那条
    分支**永远走不到**：`_call` 在 return 之前先判 `asyncio.iscoroutine`，dict
    不是协程，直接 `return result`；调用方拿到 dict，`list(result or [])` 得到
    的是**键的名字** `["messages"]`。一个 dict 于是被当成「一页 1 条消息」，
    `_raw_id` 退化成 `json.dumps("messages")` 的指纹，翻页判定彻底失效。

    这里把三种常见外壳都拆掉：`{"messages": [...]}` / `{"data": [...]}` /
    `{"data": {"messages": [...]}}`，并接受直接是 list 的情况。
    """
    if result is None:
        return None
    if isinstance(result, list):
        return result
    if isinstance(result, dict):
        for key in ("messages", "data"):
            if key not in result:
                continue
            value = result[key]
            inner = _unwrap_messages(value)
            if inner is not None:
                return inner
        # `{}` 是「这个群没有文件/公告」的正常回执，等同于空列表
        if not result:
            return []
    return None


def _seq_of(raw) -> str:
    """这条消息在平台侧的**翻页序号**（OneBot 的 `message_seq`）。

    和 `_raw_id` 是两个东西：`message_id` 是全局唯一串（OneBot 里常是
    `-1234567890` 这种负数或带前缀的串），`message_seq` 才是能当锚点回传的
    序号。`_oldest_id` 原先把 `_raw_id` 的结果当锚点，而 `_raw_id` 优先取
    `message_id`——于是把 `m99` 喂回平台的 `message_seq` 参数，平台侧
    `int('m99')` 直接抛 `ValueError`，异常一路冒出引擎，整次备份判 failed：
    消息一条没拉到，日志里只有一句看不懂的 `ValueError`。

    取不到序号时退回 `message_id`：有些不走 OneBot 的适配器只有这一个 ID，
    而它确实能当锚点用（测试替身、部分私有实现）。真正要防的是**混用**——
    消息上同时有两个字段时，必须取 `message_seq`；只有 `message_id` 时才用它。
    """
    def pick(obj, key):
        if isinstance(obj, dict):
            return obj.get(key)
        return getattr(obj, key, None)

    for key in ("message_seq", "message_id", "id"):
        value = pick(raw, key)
        if value is not None and value != "":
            return str(value)
    return ""


def _oldest_id(batch: list, newest_first: bool = False) -> str:
    """一页消息里最旧那条的**翻页序号**，用来当下一页的锚点。

    原来默认「第一个元素就是最旧的」，可 OneBot 的 `get_group_msg_history`
    返回的是**新→旧**（`message_seq` 是「从这条往回数 count 条」）。于是锚点
    取到的是这一页里最新那条，下一页原地再要一遍同样的 50 条——120 条的群
    永远只备到 50 条，状态还是 completed。

    有可比较的时间/ID 就以它为准（一眼就能看出朝向，不依赖平台约定）；
    全都取不到才退化成按数组位置，用调用方给的朝向提示。
    """
    if not batch:
        return ""
    first = batch[0]
    keyed = [(raw, _raw_key(raw)) for raw in batch]
    keyed = [(raw, key) for raw, key in keyed if key is not None]
    if not keyed:
        return _seq_of(batch[-1] if newest_first else first)
    return _seq_of(min(keyed, key=lambda item: item[1])[0])


#: 平台接口的常见参数别名。引擎统一用 `group_id`，但适配器上可能是 `gid`、
#: `group`、`chat_id`——签名不匹配就换名字这个策略挡不住「名字只有一个、
#: 参数名却不同」的情况：一个个试过去全是 TypeError，最后返回 None，
#: 上层看到的是「拉取失败，沿用上次快照」。
#:
#: 这个 bug 藏了很久：假替身的 `get_group_history(gid, ...)` 与真适配器写法一致，
#: 而 `_call` 只传 `group_id=`，于是**每一次**翻页都静默失败——备份报成功、
#: 消息 0 条。用例一直是绿的，因为它测的是「平台返回什么」，不是「参数怎么送进去」。
_PARAM_ALIASES = {
    "group_id": ("group_id", "gid", "group", "chat_id"),
    "user_id": ("user_id", "uid", "uin"),
    "count": ("count", "limit", "page_size"),
}


def _adapt_kwargs(fn, kwargs: dict) -> Optional[dict]:
    """按目标函数的签名重映射参数名。对不上返回 None（这个函数不用试了）。

    只看**显式声明**的参数，`**kwargs` 不算——OneBot 客户端靠 `__getattr__`
    造方法，签名恒为 `(*args, **kwargs)`，把它当成「什么都收」会让真正的别名
    映射失效（也可能把一个本该失败的调用蒙对）。
    """
    try:
        sig = inspect.signature(fn)
    except (TypeError, ValueError):
        return None
    params = sig.parameters
    # 收 `**kwargs` 不等于「什么都收」：很常见的写法是
    # `async def get_group_history(self, gid, count=50, offset=0, **kw)` —— 显式
    # 参数叫 `gid`，尾部再挂一个 `**kw` 兜底。原来见到 `**kwargs` 就原样透传，
    # 于是 `group_id=` 落进 `**kw`、`gid` 永远缺参，每次调用都 TypeError，
    # 整条历史消息拉取路径静默失败（备份报成功、0 条）。
    # 所以别名映射必须照做，`**kwargs` 只负责兜住「映射不到、但对方也不明着拒绝」
    # 的额外参数（比如 `message_seq`）。
    accepts_extra = any(
        p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values()
    )
    mapped: dict = {}
    for key, value in kwargs.items():
        if key in params:
            mapped[key] = value
            continue
        for alias in _PARAM_ALIASES.get(key, ()):
            if alias in params:
                mapped[alias] = value
                break
        else:
            if accepts_extra:
                # 对方收 **kwargs：别名映射不上也送进去，它自己会挑。但只限
                # 「我们确实要传」的参数，不能凭这个把任意关键字都当合法
                mapped[key] = value
                continue
            # 这个参数它根本不收。硬送进去就是 TypeError，换名字才有意义
            return None
    required = [
        name
        for name, p in params.items()
        if p.default is inspect.Parameter.empty
        and p.kind
        in (inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY)
        and name not in mapped
    ]
    if required:
        return None
    return mapped


def _close_unawaited(value) -> None:
    """关掉一个「调了但没 await」的 coroutine。

    参数名对不上的调用在 Python 里有个阴险的地方：**函数体根本没执行**，但
    协程对象已经建出来了，只是永远不会被 await——它既不报错也不干活，
    只留一条 `coroutine was never awaited` 的 RuntimeWarning。多次探测下来，
    这种半成品协程会堆在事件循环上。探测失败就得把它关掉，别留给下一个人。
    """
    if asyncio.iscoroutine(value):
        try:
            value.close()
        except Exception:  # pragma: no cover - 关不掉也不该影响主流程
            pass


class PlatformAdapter:
    """把 AstrBot 平台对象适配成备份引擎要的几个方法。

    真实的 AstrBot 适配器（以 aiocqhttp 为例）并不直接提供
    `get_group_history` / `get_group_members` 这些方法：它只做「收消息、转成事件」，
    拉数据要靠 OneBot API，而 OneBot 客户端对象的 `__getattr__` 会把任意属性名变成
    `functools.partial(call_action, 名字)`。于是 `getattr(bot, "get_group_info")`
    看起来是个可调用对象、调下去也真的会发请求——这一类「长得像」的接口最容易
    骗过探测式代码，所以这里分三层取：

    1. 事件/适配器自己实现了同名方法 → 直接用；
    2. 找到客户端（`get_client()` / `bot` / `api`）→ 走 OneBot `call_action`；
    3. 都没有 → 返回 None，由上层明确降级，而不是假装成功。

    `get_group_history` 还要额外承担一件事：**把 OneBot 的 message_seq 翻页
    翻译成引擎认的「一页 count 条」**。OneBot 的 `get_group_msg_history` 只认
    「从哪条开始」，不认 offset；不翻译的话引擎第二次翻页拿到的是同一批
    最新消息，去重兜底立刻判定「分页到头」，于是每个群只备份最新的一页，
    状态还写 `completed`。
    """

    # 内部补齐一页的最多轮次：平台一次给不满 count 时继续往前要，
    # 上限防止平台永远只回固定几条时死循环
    MAX_FILL_ROUNDS = 10

    def __init__(self, platform):
        self.platform = platform
        self._anchor: dict[int, str] = {}
        #: 每个群的历史消息朝向（True = 新→旧），第一次拿到整页时定下来
        self._newest_first: dict[int, bool] = {}
        #: 历史消息接口有没有真的调通过一次。
        #:
        #: 「适配器上每个候选名字都对不上签名」和「这个群确实一条消息都没有」
        #: 在返回值上是同一个东西（空列表），但后果天差地别：前者是插件根本备份
        #: 不了这个平台，后者是正常的空群。原来两者都报 `completed`、`0 条`——
        #: 用户以为备份好了，其实一条都没拉到。第一次调用失败就置位，由引擎在
        #: 收尾时把状态降成 failed 并说清原因。
        self.history_broken = False

    def _client(self):
        """真正的 OneBot/aiohttp 客户端对象，取不到就退回平台对象本身。

        注意 `CQHttp` 的 `__getattr__` 把**任何**属性名都变成
        `functools.partial(call_action, 名字)`：拿它当「有没有这个方法」的证据，
        连 `bot` / `api` 这种探测都会变成一次真实的 OneBot API 调用（名字还不
        存在），日志里多出一堆看不懂的失败，探测本身也不再是免费的。所以先认
        「它自己就是客户端」——有 `call_action` 的那个。
        """
        platform = self.platform
        if callable(getattr(platform, "call_action", None)):
            return platform
        fn = getattr(platform, "get_client", None)
        if callable(fn):
            try:
                client = fn()
            except Exception:
                client = None
            if client is not None:
                return client
        for name in ("bot", "api", "client"):
            if isinstance(platform, dict):
                candidate = platform.get(name)
            else:
                # 绕开 `__getattr__` 造出来的假属性：只认对象上真有的
                try:
                    inspected = inspect.getattr_static(platform, name)
                except AttributeError:
                    continue
                candidate = getattr(platform, name, None) if not isinstance(inspected, property) else None
            if candidate is not None and not isinstance(candidate, type(lambda: 0)):
                return candidate
        return platform

    async def _call(self, names, *args, **kwargs):
        """按名字顺序试，逐个真的调一次。

        不能「取到第一个可调用对象就认为找到了」：适配器上常有同名但签名不同的
        方法（或参数名不叫 `group_id` 的），一调就 TypeError，而这个 TypeError
        会被上层当成「公告拉取失败，沿用上次快照」悄悄吞掉——功能看着在，实际
        一次都没成功过。签名不匹配就换下一个名字，全都不行才返回 None。
        """
        client = self._client()
        targets = [self.platform]
        if client is not None and client is not self.platform:
            targets.append(client)
        unsuitable: list[str] = []
        for target in targets:
            for name in names:
                fn = getattr(target, name, None)
                if not callable(fn):
                    continue
                # 先按签名重映射参数名，再决定要不要调。原来只有「调下去抛
                # TypeError 就换下一个名字」这一条路：适配器的 `get_group_history`
                # 参数叫 gid，引擎送的是 group_id，每一次都 TypeError，而下一个
                # 名字根本不存在——整条拉取路径静默返回 None，备份报成功、0 条。
                mapped = _adapt_kwargs(fn, kwargs)
                if mapped is None:
                    unsuitable.append(name)
                    continue
                try:
                    result = fn(*args, **mapped)
                except TypeError as e:
                    # 签名对了却还是 TypeError：多半是函数体里的类型问题（比如
                    # group_id 该是字符串却给了 int）。换名字没用，但也不该把
                    # 上层炸掉，记下来交给下一个候选
                    unsuitable.append(f"{name}({e})")
                    continue
                except Exception as e:
                    # ActionFailed / 网络错误：这不是「签名不对」，是平台真的拒绝
                    # 或失败了。继续试下一个名字没有意义，直接交给上层降级
                    logger.debug("平台接口 %s 调用失败: %s", name, type(e).__name__)
                    return None
                if asyncio.iscoroutine(result):
                    result = await result
                return result
        if unsuitable:
            logger.debug("平台接口无匹配签名 %s: %s", ",".join(names), "; ".join(unsuitable))
        return None

    async def _action(self, action: str, **params):
        """直接发一个 OneBot action。客户端不支持时返回 None。"""
        client = self._client()
        fn = getattr(client, "call_action", None)
        if not callable(fn):
            return None
        try:
            result = fn(action=action, **params)
            if asyncio.iscoroutine(result):
                result = await result
        except Exception as e:
            logger.debug("OneBot action %s 失败: %s", action, type(e).__name__)
            return None
        return result

    async def get_group_info(self, group_id: int):
        result = await self._call(
            ("get_group_info", "get_group"), group_id=group_id
        )
        if result is None:
            result = await self._action("get_group_info", group_id=group_id)
        return result or {"group_id": group_id}

    async def get_group_members(self, group_id: int) -> list:
        result = await self._call(
            ("get_group_member_list", "get_group_members", "get_members"), group_id=group_id
        )
        if result is None:
            result = await self._action("get_group_member_list", group_id=group_id)
        return result or []

    async def get_group_announcements(self, group_id: int) -> list:
        """群公告。OneBot 里叫 `_get_group_notice`（Lagrange/NapCat）或 `get_group_notice`。"""
        result = await self._call(
            (
                "get_group_notice",
                "get_group_notices",
                "get_group_announcements",
                "_get_group_notice",
            ),
            group_id=group_id,
        )
        if result is None:
            result = await self._action("_get_group_notice", group_id=group_id)
        return result or []

    async def get_group_files(self, group_id: int) -> list:
        result = await self._call(
            ("get_group_files", "get_group_file_list", "get_files", "_get_group_files"),
            group_id=group_id,
        )
        if result is None:
            # OneBot v11 的标准名字是两个：root 与文件夹内两种
            result = await self._action("get_group_root_files", group_id=group_id)
        return result or []

    async def get_group_history(
        self, group_id: int, count: int = PAGE_SIZE, offset: int = 0
    ) -> list:
        """取一页历史消息，**保证尽量给满 `count` 条**。

        `offset == 0` 表示从头开始（引擎每一轮备份都从 0 起），顺便重置锚点。
        OneBot 只认 `message_seq`（从哪条开始往回翻），于是这里自己维护「上一页
        最旧那条」当锚点，并反复补齐——引擎是按「本页不足半页就收工」判到尾的，
        一次只给 20 条的平台会让它备份 20 条就报成功。
        """
        # 有 offset 语义的实现（适配器自己写的方法、测试替身）就照旧按 offset 翻页：
        # 引擎那套「连续两页都是翻过的才收工」的判定本来就是为它写的
        if self._supports("offset"):
            return self._orient(await self._fetch_page(group_id, count, offset, None))

        if offset == 0:
            self._anchor.pop(group_id, None)
        collected: list = []
        seen: set[str] = set()
        anchor = self._anchor.get(group_id)
        newest_first = self._newest_first.get(group_id)
        for _ in range(self.MAX_FILL_ROUNDS):
            want = max(count - len(collected), 1)
            page = await self._fetch_page(group_id, want, offset, anchor)
            if not page:
                break
            fresh = [raw for raw in page if _raw_id(raw) not in seen]
            if not fresh:
                break  # 平台不认锚点，原地打转
            for raw in fresh:
                seen.add(_raw_id(raw))
                collected.append(raw)
            # 朝向在第一次拿到整页时定下来，之后一直用同一个：同一轮里不会变
            if newest_first is None:
                newest_first = self._detect_newest_first(fresh)
                self._newest_first[group_id] = newest_first
            # 锚点必须取「最旧那条」：OneBot 的 message_seq 语义是「从这条往回数」，
            # 给成这一页最新那条，下一页原地再取一遍同样的消息——120 条的群只备到
            # 50 条，状态还是 completed。
            # 收工判据只看「凑够 count 没有」，不看「这一页给满没有」：平台一次只给
            # 20 条时（很多实现不认 count），按「给满没给满」判定会让补齐在第一轮
            # 就退出——引擎拿到的永远是 20 条，120 条的群只备 20 条。补齐就是为了
            # 对付这种平台，判据用错等于没补。
            oldest = _oldest_id(fresh, newest_first=bool(newest_first))
            if not oldest or oldest == anchor:
                break  # 翻不动了，锚点没推进
            anchor = oldest
            # 按「本页实际拿到多少条」推进，不是 `len(fresh)`：`fresh` 是「本页里
            # 还没见过的」，第二页之后它通常整页都是新的，但两页有重叠时就不是——
            # 拿它当偏移量会原地打转，一次 SQL 都不往前走。
            offset += len(page)
            self._anchor[group_id] = anchor
            if len(collected) >= count:
                break
        # 不能截断到 count：补齐时最后一轮往往整页吐回来，截掉的几条（比如
        # 补到 60 条只留 50）锚点已经推过去了，之后就再也翻不回来——群里那一小段
        # 消息永久丢失，而备份报 completed。宁可这一页多给几条，让调用方自己按
        # 条数判断，也不能把已拿到的消息扔掉。
        # 交出去之前统一成「新→旧」：引擎的翻页判定和去重都以顺序稳定为前提。
        return self._orient(collected)

    @staticmethod
    def _detect_newest_first(page: list) -> bool:
        """这一页是「新→旧」还是「旧→新」。

        有两条以上带时间/ID 的消息就直接比首尾；只有一条或都取不到时，按 OneBot
        v11 的约定猜「新→旧」（`get_group_msg_history` 的 `message_seq` 是从给定
        那条往回数）。
        """
        first_key = _raw_key(page[0]) if page else None
        last_key = _raw_key(page[-1]) if page else None
        if first_key is not None and last_key is not None and first_key != last_key:
            return first_key > last_key
        return True

    @staticmethod
    def _orient(page: list) -> list:
        """按内容朝向把一页排成「新→旧」。

        引擎的翻页判定（「这一页是不是又全是翻过的」）和去重都建立在顺序稳定的
        前提上；适配器给「旧→新」时，`本页不足半页就收工` 之外的兜底条件会被
        自己搅乱，锚点也取错方向。
        """
        if len(page) < 2:
            return list(page)
        first_key = _raw_key(page[0])
        last_key = _raw_key(page[-1])
        if first_key is None or last_key is None:
            return list(page)
        return list(page) if first_key >= last_key else list(reversed(page))

    def _supports(self, param: str) -> bool:
        """平台的历史消息接口接不接受这个参数名。

        只看**显式声明**的参数：`**kwargs` 什么都不算——OneBot 客户端靠
        `__getattr__` 造方法，签名永远是 `(*args, **kwargs)`，它并不认 offset。
        """
        for target in (self.platform, self._client()):
            if target is None:
                continue
            for name in ("get_group_msg_history", "get_group_history", "get_history"):
                fn = getattr(target, name, None)
                if not callable(fn):
                    continue
                try:
                    sig = inspect.signature(fn)
                except (TypeError, ValueError):
                    continue
                if param in sig.parameters:
                    return True
        return False

    async def _fetch_page(
        self, group_id: int, count: int, offset: int, anchor: Optional[str]
    ) -> list:
        """发一次历史消息请求。"""
        params = {"group_id": group_id, "count": count}
        # `count` 和 `message_seq` 在 OneBot 里是两种互斥语义：给了 seq 就「从它
        # 往回数 count 条」，再带一个 offset 只会让实现在两者之间挑一个，行为随
        # 平台而变。有锚点时把 count 一起收进 params 就够了，offset 不再出场。
        if anchor:
            params["message_seq"] = anchor
        elif self._supports("offset"):
            params["offset"] = offset
        # 既不认 offset 也不给锚点（补齐路径要的第一页）：那就什么都不传，
        # 等于「给我最新一页」。原来无条件塞 `offset`，只认 `message_seq` 的实现
        # 判定「无匹配签名」，补齐路径一步都没走出去，整个群报 `failed`、0 条。
        result = await self._call(
            ("get_group_msg_history", "get_group_history", "get_history"), **params
        )
        if result is None:
            result = await self._action("get_group_msg_history", **params)
        if result is None:
            # 两条路都没拿到东西：不是「空页」，是「这个接口根本调不通」。
            # 两者在返回值上都是 None，但后果完全不同——不区分的话，接一个
            # 参数名对不上的适配器，备份会一路报 completed、0 条
            self.history_broken = True
            return []
        unwrapped = _unwrap_messages(result)
        if unwrapped is None:
            # 拆不出消息列表（比如平台回了一句 `{"error": ...}`）：这是接口调不通，
            # 不是「空页」。当成空页处理就会静默收工、报 completed
            self.history_broken = True
            return []
        return list(unwrapped)


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
        #: 单页失败的退避表。做成实例属性，测试里可以置零——否则一条「重试也用尽」
        #: 的用例要真等 9 秒。
        self.page_retry_delays = PAGE_RETRY_DELAYS

    # ---------- 内部：一页 + 重试 ----------
    async def _fetch_page_with_retry(self, adapter, group_id: int, offset: int) -> list:
        """拉一页，失败退避重试几次；都不行才把异常交给调用方。

        `PAGE_RETRY_DELAYS` 三档（1/3/5 秒）覆盖的是「网络抖一下」「平台那一刻
        在重启」，不是「平台根本不支持这个接口」。后者重试多少次都一样，所以
        次数很少、总时长最多 9 秒——备份任务本身跑在后台，用户不会盯着这 9 秒。
        """
        delays = tuple(self.page_retry_delays or ())
        last: Optional[Exception] = None
        for attempt in range(len(delays) + 1):
            try:
                return await adapter.get_group_history(
                    group_id, count=PAGE_SIZE, offset=offset
                )
            except Exception as e:
                last = e
                if attempt >= len(delays):
                    break
                delay = delays[attempt]
                logger.warning(
                    "分页拉取失败，%s 秒后重试 group=%s attempt=%s reason=%s",
                    delay,
                    group_id,
                    attempt + 1,
                    type(e).__name__,
                )
                if delay:
                    await asyncio.sleep(delay)
        raise last if last is not None else RuntimeError("page_fetch_failed")

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
        # 每次备份都从「这次没拉到公告/文件清单」开始算，否则上一轮的条目会被
        # 当成本轮快照写进这份备份的 bundle 里
        self.announcements: list[dict] = []
        self.group_files: list[dict] = []
        incremental_enabled = incremental and bool(
            (self.config.get("incremental") or {}).get("enabled", True)
        )
        base = self.db.latest_completed_record(group_id) if incremental_enabled else None
        # 水位只认「谁都不管」的行能不能被收编，而不是「哪个时间点之后不再看」。
        # 原来的写法取 backup_meta 增量点和库里最新非实时归档消息的较大值，把两个
        # 不同维度的东西混成了一个数：
        # - backup_meta 推进过、而消息一条都不剩（`/backup clean` 淘汰了记录）时，
        #   水位留在天上，平台照旧返回的那批消息全被判成「太旧」。备份报成功、
        #   消息 0 条、校验和是空数据的——恢复时一条也取不出来；
        # - 续传点要防的是「半截数据重拉一遍」，这件事由「这条消息在不在库里」直接
        #   回答，不需要再猜一个时间点。
        # 所以现在两条都只用来判断「增量盘跑过没有」，真正的跳过条件是
        # 「库里已有且无主」。库里有主说明它已经在某次备份的来源清单里，跳过没有
        # 损失；库里没有、或归属的备份已经消失（淘汰/迁移残留）→ 一律重新收进本次
        # 备份。重复下载由 `message_exists` 之外的所有路径各自去重。
        stored_point = self.tracker.get_stored_point(group_id)

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

        # 备份范围开关。放在这里取，因为下面每一步都要读它
        scope = self.config.get("scope") or {}
        # 群信息/成员拉不到不等于备份失败：权限受限的适配器给不出成员列表，但历史
        # 消息照样能拉。原来任何一个抛错都会把整次备份标成 failed，一条消息都不留。
        info = None
        members: list = []
        auxiliary_errors: list[str] = []
        try:
            info = parse_group_info(await adapter.get_group_info(group_id), group_id)
            if not info.group_name:
                # OneBot 的 `get_group_info` 有时只回 group_id / member_count，没有
                # group_name。直接存下去会把已有群名洗成空串，导出包的 `<title>` 和
                # HTML 头栏跟着变空。拿不到就沿用上次的
                previous = self.db.get_group_info(group_id)
                if previous is not None and previous.group_name:
                    info.group_name = previous.group_name
            self.db.save_group_info(info)
        except Exception as e:
            auxiliary_errors.append("群信息")
            logger.warning("群信息拉取失败 group=%s: %s", group_id, type(e).__name__)
        try:
            # `scope.group_members` 原来是个从没人读的开关：schema 和 README 都写着
            # 「成员列表」，关掉它照样每轮去拉一次全员名单。2000 人的群这是一次
            # 几千条的请求，用户关它的理由（省时间/省配额/平台不给权限）一条都没兑现。
            if scope.get("group_members", True):
                raw_members = await adapter.get_group_members(group_id)
                members = parse_group_members(raw_members, group_id)
            # 成员列表是「当前状态」：整群替换，退群的人才会真的从名单里消失。
            # 只做 upsert 的话名单只会越攒越大，`/backup status` 的成员数永远不降。
            # 拉回来是空的（权限不足的适配器常见）就当没拉到，不能拿空表把快照清空。
            if members:
                self.db.replace_members(group_id, members)
        except Exception as e:
            auxiliary_errors.append("成员列表")
            logger.warning("成员列表拉取失败 group=%s: %s", group_id, type(e).__name__)
        # 拿不到新的就沿用上次快照，别让导出里的群名/成员数变成空白
        if info is None:
            info = self.db.get_group_info(group_id)
        if not members:
            members = self.db.get_members(group_id)

        # 群公告 / 群文件：开关在配置里，之前没人读。同样不影响主流程成败
        # 拉回来的条目挂在实例上，下面写 bundle 时要一并存进「这份备份的快照」。
        # 只进 `group_announcements`（当前状态表）是不够的：导出包读的是
        # `get_announcements(ctx.group_id)`，而这条路径对**历史上任意一份备份**
        # 都取当前状态——换机器恢复、或恢复一份老备份时，公告和文件清单就变成
        # 「这个群现在有什么」，不是「那时候有什么」，备份等于没带走。
        notices: list[dict] = []
        files: list[dict] = []
        if scope.get("group_announcements", True):
            try:
                raw_notices = await adapter.get_group_announcements(group_id)
                notices = parse_announcements(raw_notices, group_id)
                self.db.save_announcements(group_id, notices)
                self.announcements = notices
            except Exception as e:
                auxiliary_errors.append("群公告")
                logger.warning("群公告拉取失败 group=%s: %s", group_id, type(e).__name__)
        if scope.get("group_files", True):
            try:
                raw_files = await adapter.get_group_files(group_id)
                files = parse_group_files(raw_files, group_id)
                self.db.save_group_files(group_id, files)
                self.group_files = files
            except Exception as e:
                auxiliary_errors.append("群文件")
                logger.warning("群文件拉取失败 group=%s: %s", group_id, type(e).__name__)

        # 提到 try 之前：备份中途失败时，这份列表里是「已经写进库但还没登记来源
        # 清单」的消息，失败分支要用它补登记
        collected: list[Message] = []
        try:

            # 3-5. 分页拉消息，解析、下载媒体、入库
            page = 0
            offset = 0
            failed_media = 0
            skipped = 0
            seen_on_pages: set[str] = set()
            stale_pages = 0
            own_ids: list[str] = []
            owned_seen: list[Message] = []
            page_error: Optional[str] = None
            while True:
                try:
                    batch = await self._fetch_page_with_retry(adapter, group_id, offset)
                except Exception as e:
                    # 一页拉不下来，不等于整轮白跑。已经写进库的那部分是真数据，
                    # 回执要说清「停在第几页、保住了多少」，状态降到 partial。
                    # 原来直接冒到外层 except，整轮判 failed：库里躺着 200 条，
                    # 回执只有一句 `RuntimeError`，用户既不知道丢了多少、也
                    # 不知道该不该再跑一遍。
                    #
                    # 第一页就失败是另一回事：一条都没拿到，没有「保住的部分」
                    # 可言，交回外层按 failed 处理，错误原话一并留着。
                    if page == 0 and not collected and not skipped:
                        raise
                    page_error = (
                        f"第 {page + 1} 页拉取失败（{type(e).__name__}），"
                        f"已保住前 {page} 页"
                    )
                    logger.warning(
                        "备份分页中断 group=%s page=%s reason=%s",
                        group_id,
                        page + 1,
                        type(e).__name__,
                    )
                    break
                if not batch:
                    break
                # 有些适配器根本不认 offset，每页都回同一批消息。此时库里已经有它们，
                # 循环永远不会因为「本页无新消息」而结束——群越大越像卡死。连续两页
                # 都是翻过的消息才说明分页真的到头了（见下）。
                fresh = [raw for raw in batch if str(_raw_id(raw)) not in seen_on_pages]
                if not fresh:
                    # 「这一页全翻过」不等于「后面没有了」。每次都从最新的往回翻的
                    # 平台（很多适配器就是这样）在第二次备份时会出现这种页：库里
                    # 早有这些消息，但后面还有更旧的一大段。原地打转的证据是
                    # **连续两页**都翻不出新东西——只有一页时先接着往下要
                    if stale_pages >= 1:
                        logger.info(
                            "群 %s 连续两页都是翻过的消息，平台可能不支持偏移，停止翻页",
                            group_id,
                        )
                        break
                    stale_pages += 1
                    offset += len(batch)
                    page += 1
                    continue
                stale_pages = 0
                seen_on_pages.update(str(_raw_id(raw)) for raw in fresh)
                # 偏移量按平台真正吐回来的条数推进，不能按页码 × 页大小：
                # 平台过滤掉一部分（撤回/系统消息不计入）时，按页码算会整段跳过，
                # 那些消息永远不会被备份，而备份报成功
                offset += len(batch)
                # 一页一次事务。逐行提交时每行都要 fsync 一遍（`journal_mode=
                # DELETE` + `synchronous=FULL`），3000 条 21 秒、2 万条近 3 分钟；
                # 按页成批之后同一批数据 0.5 秒。断在一页中间就整页回滚，库里
                # 永远是「整页的整数倍」，下一轮从断点接着翻。
                #
                # 注意**媒体下载在事务外**：`await` 期间事件循环会把别的协程切
                # 进来，它们顺着可重入锁进到同一个事务里，一起提交或一起回滚。
                pending: list[Message] = []
                for raw in fresh:
                    msg = parse_message(raw, group_id)
                    if self.db.message_exists(msg.message_id, group_id):
                        # 已在库里的消息不改内容，但「归属的备份已经不存在」的那些
                        # 要收编（见上面水位的说明）：不收编就永远进不了任何来源
                        # 清单。实时归档的归属也不动——改成归给本次备份，等于把
                        # 「群被炸时的兜底数据」从实时归档入口上摘掉。
                        if self.db.adopt_orphans(
                            group_id, record.backup_id, [str(msg.message_id)]
                        ):
                            own_ids.append(str(msg.message_id))
                        else:
                            owned_seen.append(msg)
                        skipped += 1
                        continue
                    if download_media and msg.media_paths:
                        ok, bad = await self._download_media(msg)
                        record.media_count += ok
                        failed_media += bad
                    pending.append(msg)
                saved: list[Message] = []
                with self.db.batch():
                    for msg in pending:
                        self.db.save_message(msg, record.backup_id)
                        saved.append(msg)
                # 提交成功之后才计入 `collected`：整页回滚时这几条其实不在库里，
                # 提前记进去会让失败分支的「补登记来源清单」写进一批影子行
                collected.extend(saved)
                record.message_count += len(saved)
                page += 1
                if progress_callback:
                    await progress_callback(record.message_count)
                # 翻到底的判据只有两条：**平台回空页**，或者**连续两页翻不出
                # 新东西**（上面那个 `stale_pages`）。「这一页给得少」不能当判据。
                #
                # 第 31 轮把 `len(batch) < PAGE_SIZE` 放宽成 `< PAGE_SIZE // 2`，
                # 只解决了「每页 30 条」；平台封顶 20 条时（要 50 给 20，很多
                # 实现就是这样）第一页 20 < 25，当场收工——120 条的群备到 20 条，
                # 状态 `completed`。这类「半页」判据永远只是把阈值挪一挪，而平台
                # 的封顶值可以比任何固定阈值都小。多问一次空页的代价是一次请求，
                # 少问一次的代价是一整段历史永久缺失，且没人知道。

            # 6. 来源清单 + 快照
            # 来源清单和归属是两回事：归属会被后续备份接手，这里记的不会。
            # 除了本轮新收的，还要带上「遍历到、已在库且归属仍是某份现存备份」的
            # 消息——它们确实是这一轮覆盖到的。只登记新消息会得到一个空清单，
            # 校验和跟着退化成空数据的哈希，恢复时按来源取出一整批再比对必然失败。
            self.db.add_sources(record.backup_id, collected)
            self.db.add_sources(record.backup_id, owned_seen)
            covered = self.db.get_source_messages(record.backup_id, group_id)
            if own_ids:
                logger.info("群 %s 收编 %s 条无主消息", group_id, len(own_ids))
            # 记录里的 message_count 名义上是「这次存进库的新消息数」，增量备份跑一轮
            # 全是已在库消息时报 0 条——`/backup list` 里明晃晃写着「覆盖 0 条」，
            # 像是备份了个寂寞，而这轮实实在在覆盖了 50 条。
            # 现在它回答「这份备份保住了多少条消息」：按来源清单算。新增了多少由
            # `new_count` 单独交代，两者都得能看见——只改 message_count 的话，
            # 「这次又存进来几条」这个信息就没了。
            record.new_count = len(collected)
            if covered:
                record.message_count = len(covered)
            # 群信息/成员快照每轮都存：一条新消息都没有的那轮，导出仍然需要群名
            if covered or info is not None or members:
                self.db.save_bundle(
                    record.backup_id,
                    group_id,
                    info,
                    members,
                    announcements=notices or None,
                    group_files=files or None,
                )

            # 7. 增量点与校验和
            # 增量点只用于「下次从哪儿开始翻」的提示，不再参与跳过判断（见上方水位
            # 那段）；时间戳乱序的实时归档不该被它带偏，而 covered 里混不进实时归档。
            if covered:
                self.tracker.advance(group_id, covered)
            # 算「这次备份覆盖的全部消息」，不是「这次新拉到的消息」：增量为 0 条时
            # 只算 collected 会得到空数据的校验和，恢复时按来源取出一整批消息再比对
            # 必然对不上——数据一个字节没坏，却被判成「备份可能已损坏」。
            record.checksum = compute_checksum(covered) if covered else compute_checksum(collected)
            logger.debug(
                "备份 group=%s 新增=%s 已在库=%s", group_id, record.message_count, skipped
            )
            record.total_size = self._compute_size(group_id, covered or collected)
            record.status = BackupStatus.PARTIAL if failed_media else BackupStatus.COMPLETED
            notes = []
            if failed_media:
                notes.append(f"{failed_media} 个媒体文件下载失败")
            if page_error:
                # 分页中断也是 partial：已经拿到的是真数据，剩下的下一轮从断点
                # 继续（增量只看「库里有没有」，不认水位）
                record.status = BackupStatus.PARTIAL
                notes.insert(0, page_error)
            if auxiliary_errors:
                notes.append("；".join(auxiliary_errors) + "拉取失败，沿用上次快照")
            # 历史消息接口一次都没调通：这条不能报 completed。空群和「接口根本
            # 用不了」在返回值上都是空列表，混为一谈等于告诉用户「备份好了」而
            # 实际一条都没拉到。降成 failed 并把原因写清，用户才知道该看什么
            broken_history = getattr(adapter, "history_broken", False)
            if broken_history and not covered:
                record.status = BackupStatus.FAILED
                notes.insert(
                    0,
                    "历史消息接口调用失败（平台适配器的接口名或参数名与本插件不匹配），"
                    "本次未拉取到任何消息",
                )
            record.error = " · ".join(notes) if notes else None
            record.finished_at = datetime.now()
        except Exception as e:
            logger.error("备份失败 group=%s: %s", group_id, type(e).__name__)
            record.status = BackupStatus.FAILED
            record.error = f"{type(e).__name__}: {e}"
            record.finished_at = datetime.now()
            # 失败前已经落库的那批消息必须登记进来源清单，否则它们永远进不了任何
            # 一次备份：唯一键 (message_id, group_id) 被占着，下一轮备份看到
            # 「已在库里」直接跳过，而 `adopt_orphans` 只收编「归属的备份已消失」
            # 的行——这次失败备份的记录是在失败**之后**才写进 backup_records 的，
            # 所以归属看起来完全正常，谁也不会来认领它们。
            # 一条都没登记的话，`/backup list` 说覆盖 0 条、恢复取出 0 条，
            # 而聊天记录明明就在库里。
            try:
                self.db.add_sources(record.backup_id, collected)
                record.new_count = len(collected)
                record.message_count = max(
                    record.message_count, len(collected)
                )
            except Exception as inner:  # 登记失败不能盖掉原本的失败原因
                logger.warning(
                    "备份失败后登记来源清单出错 group=%s: %s",
                    group_id,
                    type(inner).__name__,
                )
        self.db.save_record(record)
        return record

    async def _download_media(self, msg: Message) -> tuple[int, int]:
        """下载消息媒体，返回 (成功数, 失败数)。单个失败不影响整体。"""
        ok = failed = 0
        for i, url in enumerate(list(msg.media_paths)):
            if not isinstance(url, str) or not url.startswith("http"):
                continue
            # 先问库：这条消息的这个 URL 是不是已经下过了。内存里那份 URL 索引
            # 重启就空，只靠它会让同一张图每重启一次就多落一份文件。
            known = self.db.find_media_by_url(msg.group_id, msg.message_id, url)
            if known:
                msg.media_paths[i] = known
                ok += 1
                self._ensure_media_row(msg, url, known)
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

    def _ensure_media_row(self, msg: Message, url: str, local: str) -> None:
        """文件已在盘上、库里那行却没了：补登记，别让它成为没人认领的孤儿。"""
        p = Path(local)
        try:
            size = p.stat().st_size
        except OSError:
            return
        self.db.save_media(
            message_id=msg.message_id,
            group_id=msg.group_id,
            original_url=url,
            local_path=local,
            filename=p.name,
            file_size=size,
            mime_type=mimetypes.guess_type(p.name)[0] or "",
            sha256=self.media.sha256_of(p),
        )

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

    def _compute_size(self, group_id: int, messages=None) -> int:
        """这条记录覆盖的消息所占的媒体体积。

        以前是「这个群所有媒体加起来」，于是每条记录的 total_size 都等于全群总量，
        `/backup list` 里 10 条记录的体积完全相同、加起来是实际的十倍。按记录覆盖
        的消息算才是它自己的账。
        """
        if messages is None:
            messages = self.db.get_messages(group_id=group_id)
        ids = {str(m.message_id) for m in messages}
        if not ids:
            return 0
        total = 0
        for row in self.db.query(
            "SELECT message_id, local_path FROM media_files WHERE group_id = ?", (group_id,)
        ):
            if str(row["message_id"]) not in ids:
                continue
            p = Path(row["local_path"] or "")
            if p.is_file():
                total += p.stat().st_size
        return total

    @staticmethod
    def _gen_backup_id(group_id: int) -> str:
        return f"bk_{group_id}_{datetime.now():%Y%m%d%H%M%S}_{uuid.uuid4().hex[:6]}"
