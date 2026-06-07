"""Cron Scheduler 测试"""
import pytest
import time
from patchflow.core.cron_scheduler import (
    CronScheduler, CronTask,
    _validate_cron, _next_run_time,
)


class TestCronValidation:
    def test_valid_expressions(self):
        assert _validate_cron("* * * * *")
        assert _validate_cron("*/5 * * * *")
        assert _validate_cron("0 9 * * 1-5")
        assert _validate_cron("30 14 15 6 *")
        assert _validate_cron("0,15,30,45 * * * *")
        assert _validate_cron("0-30/5 * * * *")

    def test_invalid_expressions(self):
        assert not _validate_cron("")
        assert not _validate_cron("invalid")
        assert not _validate_cron("* * * *")  # 4 fields
        assert not _validate_cron("* * * * * *")  # 6 fields
        assert not _validate_cron("60 * * * *")  # minute > 59
        assert not _validate_cron("* 24 * * *")  # hour > 23
        assert not _validate_cron("* * 32 * *")  # day > 31
        assert not _validate_cron("* * * 13 *")  # month > 12


class TestNextRunTime:
    def test_returns_future(self):
        now = time.time()
        next_time = _next_run_time("*/5 * * * *", now)
        assert next_time > now

    def test_specific_time(self):
        now = time.time()
        next_time = _next_run_time("0 * * * *", now)  # Every hour at :00
        assert next_time > now

    def test_midnight(self):
        now = time.time()
        next_time = _next_run_time("0 0 * * *", now)
        assert next_time > now


class TestCronTask:
    def test_basic(self):
        t = CronTask(
            id="c1", cron="*/5 * * * *",
            prompt="test", recurring=True,
        )
        assert t.id == "c1"
        assert t.enabled
        assert t.recurring
        assert t.run_count == 0

    def test_serialization(self):
        t = CronTask(
            id="c1", cron="0 9 * * 1-5",
            prompt="analyze", recurring=True,
            durable=True, created_at=12345.0,
        )
        d = t.to_dict()
        assert d["id"] == "c1"
        assert d["cron"] == "0 9 * * 1-5"
        assert d["prompt"] == "analyze"

        t2 = CronTask.from_dict(d)
        assert t2.id == t.id
        assert t2.cron == t.cron
        assert t2.prompt == t.prompt
        assert t2.durable

    def test_is_expired_recurring(self):
        # Created 8 days ago
        t = CronTask(
            id="c1", cron="* * * * *",
            prompt="test", recurring=True,
            created_at=time.time() - 8 * 24 * 3600,
        )
        assert t.is_expired

    def test_not_expired_recent(self):
        t = CronTask(
            id="c1", cron="* * * * *",
            prompt="test", recurring=True,
            created_at=time.time(),
        )
        assert not t.is_expired

    def test_one_shot_expired_after_run(self):
        t = CronTask(
            id="c1", cron="* * * * *",
            prompt="test", recurring=False,
            created_at=time.time(),
            run_count=1,
        )
        assert t.is_expired


class TestCronScheduler:
    def test_add_task(self):
        sched = CronScheduler()
        t = sched.add("*/10 * * * *", "test task")
        assert t is not None
        assert sched.task_count == 1
        assert t.recurring

    def test_add_one_shot(self):
        sched = CronScheduler()
        t = sched.add("0 12 31 12 *", "yearly", recurring=False)
        assert t is not None
        assert not t.recurring

    def test_add_invalid_cron(self):
        sched = CronScheduler()
        t = sched.add("invalid", "test")
        assert t is None
        assert sched.task_count == 0

    def test_remove_task(self):
        sched = CronScheduler()
        t = sched.add("* * * * *", "test")
        assert sched.task_count == 1
        assert sched.remove(t.id)
        assert sched.task_count == 0

    def test_remove_missing(self):
        sched = CronScheduler()
        assert not sched.remove("nonexistent")

    def test_disable_enable(self):
        sched = CronScheduler()
        t = sched.add("* * * * *", "test")
        assert sched.disable(t.id)
        task = sched.get(t.id)
        assert not task.enabled
        assert sched.enable(t.id)
        task = sched.get(t.id)
        assert task.enabled

    def test_list_all(self):
        sched = CronScheduler()
        sched.add("0 0 * * *", "midnight")
        sched.add("0 12 * * *", "noon")
        tasks = sched.list_all()
        assert len(tasks) == 2

    def test_tick_runs_due_tasks(self):
        sched = CronScheduler()
        # Create a task that's due immediately
        t = sched.add("* * * * *", "immediate")
        # Force it to be due
        t.next_run_at = time.time() - 10
        due = sched.tick()
        assert len(due) >= 1

    def test_run_once_with_callback(self):
        sched = CronScheduler()
        executed = []

        def _cb(task):
            executed.append(task.id)

        sched.set_callback(_cb)
        t = sched.add("* * * * *", "test")
        t.next_run_at = time.time() - 10  # force due

        count = sched.run_once()
        assert count >= 1

    def test_max_tasks(self):
        sched = CronScheduler()
        for i in range(55):
            sched.add(f"*/{10 + i} * * * *", f"task-{i}")
        assert sched.task_count <= sched.MAX_TASKS

    def test_persistence(self, tmp_path):
        sched = CronScheduler(str(tmp_path))
        t = sched.add("0 9 * * 1-5", "weekday check", durable=True)
        sched._save()

        sched2 = CronScheduler(str(tmp_path))
        sched2.load()
        loaded = sched2.get(t.id)
        assert loaded is not None
        assert loaded.prompt == "weekday check"
