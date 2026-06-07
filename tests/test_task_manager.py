"""Task Manager 测试"""
import pytest
from patchflow.core.task_manager import (
    TaskManager, Task, create_task_chain,
)


class TestTask:
    def test_basic_task(self):
        t = Task(id="t1", subject="Do something", description="Details")
        assert t.id == "t1"
        assert t.subject == "Do something"
        assert t.status == "pending"
        assert t.is_ready
        assert not t.is_active
        assert not t.is_done

    def test_task_serialization(self):
        t = Task(
            id="t1", subject="S", description="D",
            files_expected=["a.py"],
            blocked_by={"t0"},
            metadata={"key": "val"},
        )
        d = t.to_dict()
        assert d["id"] == "t1"
        assert d["subject"] == "S"
        assert "a.py" in d["files_expected"]
        assert "t0" in d["blocked_by"]

        t2 = Task.from_dict(d)
        assert t2.id == t.id
        assert t2.subject == t.subject
        assert "t0" in t2.blocked_by

    def test_depends_on_chain(self):
        t = Task(id="t1", subject="S")
        t.depends_on("t0", "t00")
        assert "t0" in t.blocked_by
        assert "t00" in t.blocked_by

    def test_not_ready_when_blocked(self):
        t = Task(id="t1", subject="S", blocked_by={"t0"})
        assert not t.is_ready

    def test_not_ready_when_active(self):
        t = Task(id="t1", subject="S", status="in_progress")
        assert not t.is_ready

    def test_is_done_completed(self):
        t = Task(id="t1", subject="S", status="completed")
        assert t.is_done

    def test_is_done_failed(self):
        t = Task(id="t1", subject="S", status="failed")
        assert t.is_done


class TestTaskManagerCRUD:
    def test_add_task(self):
        tm = TaskManager()
        t = tm.add("Test task", "Description")
        assert tm.count == 1
        assert t.id.startswith("task-")
        assert t.subject == "Test task"

    def test_add_multiple_tasks(self):
        tm = TaskManager()
        t1 = tm.add("Task 1")
        t2 = tm.add("Task 2")
        t3 = tm.add("Task 3")
        assert tm.count == 3
        assert t1.id != t2.id != t3.id

    def test_get_task(self):
        tm = TaskManager()
        t = tm.add("Test")
        found = tm.get(t.id)
        assert found is not None
        assert found.subject == "Test"

    def test_get_missing(self):
        tm = TaskManager()
        assert tm.get("nonexistent") is None

    def test_update_status(self):
        tm = TaskManager()
        t = tm.add("Test")
        updated = tm.update(t.id, status="in_progress")
        assert updated.status == "in_progress"
        assert updated.started_at > 0

    def test_update_files_written(self):
        tm = TaskManager()
        t = tm.add("Test")
        tm.update(t.id, status="completed", files_written=["a.py", "b.py"])
        found = tm.get(t.id)
        assert found.status == "completed"
        assert found.files_written == ["a.py", "b.py"]
        assert found.completed_at > 0

    def test_delete_task(self):
        tm = TaskManager()
        t = tm.add("Test")
        assert tm.count == 1
        assert tm.delete(t.id)
        assert tm.count == 0

    def test_delete_missing(self):
        tm = TaskManager()
        assert not tm.delete("nonexistent")


class TestTaskManagerDependencies:
    def test_unblock_on_completion(self):
        tm = TaskManager()
        t1 = tm.add("Task 1")
        t2 = tm.add("Task 2")
        t2.depends_on(t1.id)
        assert not t2.is_ready

        tm.update(t1.id, status="completed")
        t2_after = tm.get(t2.id)
        assert t2_after.is_ready

    def test_failed_dependency_skips(self):
        tm = TaskManager()
        t1 = tm.add("Task 1")
        t2 = tm.add("Task 2")
        t2.depends_on(t1.id)

        tm.update(t1.id, status="failed")
        t2_after = tm.get(t2.id)
        assert t2_after.status == "skipped"

    def test_list_ready(self):
        tm = TaskManager()
        t1 = tm.add("Independent")  # ready
        t2 = tm.add("Dependent").depends_on("nonexistent")  # blocked
        t3 = tm.add("Also independent")  # ready
        ready = tm.list_ready()
        assert len(ready) == 2
        ready_ids = {t.id for t in ready}
        assert t1.id in ready_ids
        assert t3.id in ready_ids

    def test_summary(self):
        tm = TaskManager()
        tm.add("A")
        tm.add("B")
        s = tm.summary()
        assert "2 pending" in s


class TestTaskManagerExecution:
    def test_execute_all_sequential(self):
        tm = TaskManager()
        tm.add("Task 1")
        tm.add("Task 2")
        tm.add("Task 3")

        executed = []

        def _executor(task):
            executed.append(task.id)
            return True

        results = tm.execute_all(_executor, parallel=False)
        assert len(results) == 3
        assert all(results.values())
        assert len(executed) == 3

    def test_execute_all_parallel(self):
        tm = TaskManager()
        for i in range(5):
            tm.add(f"Task {i}")

        executed = []

        def _executor(task):
            executed.append(task.id)
            return True

        results = tm.execute_all(_executor, parallel=True)
        assert len(results) == 5
        assert all(results.values())
        assert len(executed) == 5

    def test_execute_with_dependencies(self):
        tm = TaskManager()
        t1 = tm.add("First")
        t2 = tm.add("Second")
        t3 = tm.add("Third")
        t2.depends_on(t1.id)
        t3.depends_on(t2.id)

        order = []

        def _executor(task):
            order.append(task.id)
            return True

        tm.execute_all(_executor, parallel=False)
        assert order[0] == t1.id  # 先执行不依赖的
        assert t3.id in order

    def test_execute_with_failure_skips_dependents(self):
        tm = TaskManager()
        t1 = tm.add("Will fail")
        t2 = tm.add("Depends on t1")
        t2.depends_on(t1.id)

        def _executor(task):
            return task.id != t1.id  # t1 fails

        results = tm.execute_all(_executor, parallel=False)
        assert results[t1.id] is False
        t2_after = tm.get(t2.id)
        assert t2_after.status == "skipped"

    def test_execute_empty(self):
        tm = TaskManager()
        results = tm.execute_all(lambda t: True)
        assert results == {}


class TestCreateTaskChain:
    def test_basic_chain(self):
        tm = TaskManager()
        tasks = create_task_chain(tm, [
            ("Step 1", "Desc 1", ["a.py"]),
            ("Step 2", "Desc 2", ["b.py"]),
            ("Step 3", "Desc 3", None),
        ])
        assert len(tasks) == 3
        # Step 2 depends on Step 1
        assert tasks[0].id in tasks[1].blocked_by
        # Step 3 depends on Step 2
        assert tasks[1].id in tasks[2].blocked_by
        # Step 1 has no dependencies
        assert len(tasks[0].blocked_by) == 0

    def test_single_step_chain(self):
        tm = TaskManager()
        tasks = create_task_chain(tm, [("Only", "Desc", [])])
        assert len(tasks) == 1
        assert tasks[0].is_ready


class TestTaskManagerPersistence:
    def test_save_load(self, tmp_path):
        tm = TaskManager(work_dir=str(tmp_path))
        tm.add("Task 1")
        tm.add("Task 2")
        tm.save()

        tm2 = TaskManager(work_dir=str(tmp_path))
        tm2.load()
        assert tm2.count == 2

    def test_clear(self):
        tm = TaskManager()
        tm.add("A")
        tm.add("B")
        assert tm.count == 2
        tm.clear()
        assert tm.count == 0
