"""触发式备份：大量退群、解散预警、机器人被踢。"""

from collections import deque
from datetime import datetime, timedelta
from typing import Deque, Optional


class AutoTrigger:
    def __init__(self, config: Optional[dict] = None, cooldown_seconds: int = 1800):
        self.cooldown = timedelta(seconds=cooldown_seconds)
        self.reconfigure(config)
        self._leaves: dict[int, Deque[datetime]] = {}
        self._last_trigger: dict[int, datetime] = {}

    def reconfigure(self, config: Optional[dict] = None) -> None:
        """把配置读进运行参数。构造和 `/backup config set` 共用。

        原来只在构造时读一次，`/backup config set auto_trigger.mass_leave_threshold 3`
        回执「✅ 已设置」，实际还按 10 个人才触发。
        """
        cfg = config or {}
        self.mass_leave_threshold = max(int(cfg.get("mass_leave_threshold", 10) or 1), 1)
        self.on_group_disband = bool(cfg.get("on_group_disband", True))

    def record_leave(self, group_id: int, now: Optional[datetime] = None) -> int:
        """记一次退群，返回窗口内退群数。"""
        now = now or datetime.now()
        window = self._leaves.setdefault(group_id, deque())
        window.append(now)
        cutoff = now - timedelta(minutes=10)
        while window and window[0] < cutoff:
            window.popleft()
        return len(window)

    def should_trigger_mass_leave(self, group_id: int, now: Optional[datetime] = None) -> bool:
        window = self._leaves.get(group_id) or deque()
        if len(window) < self.mass_leave_threshold:
            return False
        return self._check_cooldown(group_id, now)

    def _check_cooldown(self, group_id: int, now: Optional[datetime] = None) -> bool:
        now = now or datetime.now()
        last = self._last_trigger.get(group_id)
        if last and now - last < self.cooldown:
            return False
        self._last_trigger[group_id] = now
        return True

    def on_disband(self, group_id: int, now: Optional[datetime] = None) -> bool:
        if not self.on_group_disband:
            return False
        return self._check_cooldown(group_id, now)

    def reset(self, group_id: int) -> None:
        self._leaves.pop(group_id, None)

