"""定时备份调度。APScheduler 延迟导入：未安装时插件仍可手动备份。"""

import logging
import re
from datetime import datetime
from typing import Awaitable, Callable, Optional

from .database import Database

logger = logging.getLogger(__name__)

# 标准 5 段 cron：分 时 日 月 周。允许 * / , - 和数字
_CRON_FIELD = re.compile(r"^[\d*/,\-]+$")
_CRON_BOUNDS = ((0, 59), (0, 23), (1, 31), (1, 12), (0, 7))


def _valid_field(part: str, low: int, high: int) -> bool:
    """单段校验：数字在界内，步长非零，区间不反着写。"""
    if not part or not _CRON_FIELD.match(part):
        return False
    for num in re.findall(r"\d+", part):
        if not low <= int(num) <= high:
            return False
    for step in re.findall(r"/(\d+)", part):
        if int(step) == 0:  # */0 会让 APScheduler 建任务时直接抛错
            return False
    for start, end in re.findall(r"(\d+)-(\d+)", part):
        if int(start) > int(end):  # 5-3 是写反了，不是「每 3 天」
            return False
    return True


def _normalize_cron(cron: str) -> str:
    """把周字段的 7（周日）改写成 0，其余原样。

    两种写法在 cron 里等价，但 APScheduler 的 `from_crontab` 只接受 0-6：
    校验通过了、真去挂任务时才抛，`schedule set` 先回「✅ 已设置」再炸掉。
    """
    parts = str(cron or "").split()
    if len(parts) == 5 and parts[4] == "7":
        parts[4] = "0"
        return " ".join(parts)
    return cron


def _impossible(parts: list[str]) -> bool:
    """这个表达式是不是「格式合法但永远不会触发」。

    `0 0 31 2 *`（2 月 31 日）、`0 0 30 2 *` 单看每一段都在界内，APScheduler 也
    会收下，然后建出一个永远算不出下次触发时间的任务：`/backup schedule set`
    回一句「✅ 已设置」，之后一辈子不跑，`list` 里还明晃晃挂着它。
    APScheduler 自己算不出来时 `get_next_fire_time` 返回 None——那正是「这个
    任务永远不会跑」的判据，比手写日历规则可靠。
    """
    try:
        from apscheduler.triggers.cron import CronTrigger
    except ImportError:  # 缺依赖时退回手写规则
        return _impossible_by_calendar(parts)
    # 周字段里 7 = 周日，和 0 等价。`CronTrigger.from_crontab` 只接受 0-6，
    # 把 `0 0 * * 7` 直接递过去会抛 ValueError——那是个完全合法的写法，
    # 不能因为上游的死板就判成非法。
    try:
        trigger = CronTrigger.from_crontab(_normalize_cron(" ".join(parts)))
        return trigger.get_next_fire_time(None, datetime.now()) is None
    except (ValueError, TypeError):
        # APScheduler 自己都解析不了，就是非法表达式
        return True


def _impossible_by_calendar(parts: list[str]) -> bool:
    """APScheduler 不在时的兜底：只拦「固定到某月某日」这种能一眼算死的写法。"""
    day, month = parts[2], parts[3]
    if not (day.isdigit() and month.isdigit()):
        return False
    month_days = {1: 31, 2: 29, 3: 31, 4: 30, 5: 31, 6: 30, 7: 31, 8: 31, 9: 30, 10: 31, 11: 30, 12: 31}
    return int(day) > month_days.get(int(month), 31)


def validate_cron(cron: str) -> bool:
    """不依赖 APScheduler 的 cron 校验，缺依赖时也能判断参数是否合法。"""
    parts = str(cron or "").split()
    if len(parts) != 5:
        return False
    if not all(
        _valid_field(part, low, high) for part, (low, high) in zip(parts, _CRON_BOUNDS)
    ):
        return False
    return not _impossible(parts)


class BackupScheduler:
    def __init__(
        self,
        db: Database,
        run_backup: Callable[[int], Awaitable],
        default_cron: str = "0 3 * * *",
        enabled: bool = True,
    ):
        self.db = db
        self.run_backup = run_backup
        self.default_cron = default_cron
        self.enabled = enabled
        self._scheduler = None

    def _ensure_scheduler(self):
        if self._scheduler is not None:
            return self._scheduler
        try:
            from apscheduler.schedulers.asyncio import AsyncIOScheduler
            from apscheduler.triggers.cron import CronTrigger
        except ImportError:  # pragma: no cover - 缺依赖时降级
            logger.warning("未安装 apscheduler，定时备份不可用")
            return None
        self._scheduler = AsyncIOScheduler()
        try:
            self._scheduler.start()
        except RuntimeError:
            # 没有运行中的事件循环（如纯命令行/测试场景）：任务已落库，等下次启动时恢复
            logger.warning("当前无事件循环，定时任务仅保存不调度")
            self._scheduler = None
            return None
        self._CronTrigger = CronTrigger
        return self._scheduler

    @staticmethod
    def _job_id(group_id: int) -> str:
        return f"backup_group_{group_id}"

    def validate_cron(self, cron: str) -> bool:
        return validate_cron(cron)

    def add_job(self, group_id: int, cron: str) -> bool:
        if not self.validate_cron(cron):
            return False
        self.db.save_schedule(group_id, cron, enabled=True)
        if not self.enabled:
            logger.info("schedule.enabled=false，群 %s 的任务只保存不调度", group_id)
            return True
        sched = self._ensure_scheduler()
        if sched is None:
            return True
        from apscheduler.triggers.cron import CronTrigger

        sched.add_job(
            self._run,
            # 周字段 7 在这里转成 0：`CronTrigger.from_crontab` 只认 0-6，
            # 直接递 7 会在建任务时抛错，而 `0 0 * * 7` 是合法的「每周日」
            CronTrigger.from_crontab(_normalize_cron(cron)),
            args=[group_id],
            id=self._job_id(group_id),
            replace_existing=True,
            max_instances=1,
            coalesce=True,
            misfire_grace_time=600,
        )
        return True

    async def _run(self, group_id: int) -> None:
        try:
            await self.run_backup(group_id)
        except Exception as e:  # 定时任务里的异常不能抛给调度器
            logger.error("定时备份失败 group=%s: %s", group_id, type(e).__name__)

    def remove_job(self, group_id: int) -> None:
        self.db.delete_schedule(group_id)
        if self._scheduler is not None:
            try:
                self._scheduler.remove_job(self._job_id(group_id))
            except Exception:
                pass

    def list_jobs(self) -> list[dict]:
        return self.db.list_schedules()

    def restore_jobs(self) -> int:
        """启动时把库里的定时任务重新挂回调度器。

        `schedule.enabled` 是总闸：关掉时一条都不挂，库里已存的 cron 保留着，
        下次打开就全回来。之前这个开关写了没人读。
        """
        count = 0
        if not self.enabled:
            return 0
        for job in self.db.list_schedules():
            if job["enabled"] and self.add_job(job["group_id"], job["cron"]):
                count += 1
        return count

    def shutdown(self) -> None:
        if self._scheduler is not None:
            try:
                self._scheduler.shutdown(wait=False)
            except Exception:
                pass
            self._scheduler = None
