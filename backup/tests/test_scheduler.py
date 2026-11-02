"""定时调度：cron 校验、无事件循环降级、任务增删与恢复。"""

import pytest

from backup.core.scheduler import BackupScheduler


async def _noop(group_id):
    return None


@pytest.fixture
def scheduler(db):
    return BackupScheduler(db, _noop)


class TestCronValidation:
    @pytest.mark.parametrize("cron", ["0 3 * * *", "*/5 * * * *", "0 0 1 1 *"])
    def test_valid_cron(self, scheduler, cron):
        assert scheduler.validate_cron(cron) is True

    @pytest.mark.parametrize("cron", ["", "每天三点", "* * *", "99 99 * * *"])
    def test_invalid_cron(self, scheduler, cron):
        assert scheduler.validate_cron(cron) is False

    def test_add_job_rejects_bad_cron_without_persisting(self, scheduler, db):
        assert scheduler.add_job(100, "not-a-cron") is False
        assert db.list_schedules() == []


class TestJobLifecycle:
    def test_add_persists_even_without_event_loop(self, scheduler, db):
        assert scheduler.add_job(100, "0 3 * * *") is True
        jobs = db.list_schedules()
        assert len(jobs) == 1
        assert jobs[0]["group_id"] == 100

    def test_remove_deletes_schedule(self, scheduler, db):
        scheduler.add_job(100, "0 3 * * *")
        scheduler.remove_job(100)
        assert db.list_schedules() == []

    def test_list_jobs_reflects_storage(self, scheduler):
        scheduler.add_job(100, "0 3 * * *")
        scheduler.add_job(200, "0 4 * * *")
        assert {j["group_id"] for j in scheduler.list_jobs()} == {100, 200}

    def test_restore_jobs_restores_enabled_only(self, scheduler, db):
        db.save_schedule(100, "0 3 * * *", enabled=True)
        db.save_schedule(200, "0 4 * * *", enabled=False)
        assert scheduler.restore_jobs() == 1

    def test_shutdown_is_idempotent(self, scheduler):
        scheduler.shutdown()
        scheduler.shutdown()


class TestDegrade:
    def test_missing_apscheduler_degrades_to_storage_only(self, scheduler, db, monkeypatch):
        import builtins

        real_import = builtins.__import__

        def fake_import(name, *args, **kwargs):
            if name.startswith("apscheduler"):
                raise ImportError("no apscheduler")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", fake_import)
        assert scheduler._ensure_scheduler() is None
        assert scheduler.add_job(100, "0 3 * * *") is True  # 仍然落库
        scheduler.shutdown()

    async def test_run_swallows_exceptions(self, db):
        async def boom(group_id):
            raise RuntimeError("fail")

        s = BackupScheduler(db, boom)
        await s._run(100)  # 不抛
