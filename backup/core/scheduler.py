"""定时备份调度。APScheduler 延迟导入：未安装时插件仍可手动备份。"""

import logging
import re
from typing import Awaitable, Callable, Optional

from .database import Database

logger = logging.getLogger(__name__)

# 标准 5 段 cron：分 时 日 月 周。允许 * / , - 和数字
_CRON_FIELD = re.compile(r"^[\d*/,\-]+$")
_CRON_BOUNDS = ((0, 59), (0, 23), (1, 31), (1, 12), (0, 7))


def validate_cron(cron: str) -> bool:
    """不依赖 APScheduler 的 cron 校验，缺依赖时也能判断参数是否合法。"""
    parts = str(cron or "").split()
    if len(parts) != 5:
        return False
    for part, (low, high) in zip(parts, _CRON_BOUNDS):
        if not part or not _CRON_FIELD.match(part):
            return False
        for num in re.findall(r"\d+", part):
            if not low <= int(num) <= high:
                return False
    return True


class BackupScheduler:
    def __init__(
        self,
        db: Database,
        run_backup: Callable[[int], Awaitable],
        default_cron: str = "0 3 * * *",
    ):
        self.db = db
        self.run_backup = run_backup
        self.default_cron = default_cron
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
        sched = self._ensure_scheduler()
        if sched is None:
            return True
        from apscheduler.triggers.cron import CronTrigger

        sched.add_job(
            self._run,
            CronTrigger.from_crontab(cron),
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
        """启动时把库里的定时任务重新挂回调度器。"""
        count = 0
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
