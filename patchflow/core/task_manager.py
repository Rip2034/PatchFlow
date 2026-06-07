"""TaskManager — 结构化任务管理系统

从 Claude Code 的 TaskCreate/TaskUpdate/TaskGet/TaskList 移植：
  - 任务状态流转: pending → in_progress → completed
  - 依赖管理: blockedBy / blocks
  - 并行调度: 无依赖的任务自动并行执行
  - 状态持久化: 可选保存到 .patchflow/tasks.json

与 PlanExecutor 配合使用，升级 Plan 执行能力。

使用方式：
    from patchflow.core.task_manager import TaskManager, Task

    tm = TaskManager()
    tm.add("Analyze app.py", "Find syntax errors", files=["app.py"])
    tm.add("Fix app.py", "Apply the fix", files=["app.py"]).blocked_by("Analyze app.py")
    tm.add("Verify", "Run tests").blocked_by("Fix app.py")

    results = tm.execute_all(parallel=True)
"""

import json
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from patchflow.utils import logger


# ═══════════════════════════════════════════════════════════
# 数据结构
# ═══════════════════════════════════════════════════════════

@dataclass
class Task:
    """单个任务"""
    id: str = ""
    subject: str = ""
    description: str = ""
    status: str = "pending"  # pending | in_progress | completed | failed | skipped
    files_expected: list[str] = field(default_factory=list)
    files_written: list[str] = field(default_factory=list)
    error: str = ""
    result: dict | None = None
    metadata: dict = field(default_factory=dict)
    created_at: float = 0.0
    started_at: float = 0.0
    completed_at: float = 0.0

    # 依赖关系
    blocked_by: set[str] = field(default_factory=set)  # 等待这些任务完成
    blocks: set[str] = field(default_factory=set)  # 这些任务在等待我

    @property
    def is_ready(self) -> bool:
        """检查是否可以开始（所有依赖已满足）"""
        return self.status == "pending" and len(self.blocked_by) == 0

    @property
    def is_active(self) -> bool:
        return self.status == "in_progress"

    @property
    def is_done(self) -> bool:
        return self.status in ("completed", "failed", "skipped")

    @property
    def duration_ms(self) -> float:
        if self.started_at and self.completed_at:
            return (self.completed_at - self.started_at) * 1000
        if self.started_at:
            return (time.time() - self.started_at) * 1000
        return 0.0

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "subject": self.subject,
            "description": self.description,
            "status": self.status,
            "files_expected": self.files_expected,
            "files_written": self.files_written,
            "error": self.error,
            "blocked_by": sorted(self.blocked_by),
            "blocks": sorted(self.blocks),
            "metadata": self.metadata,
            "created_at": self.created_at,
        }

    @staticmethod
    def from_dict(data: dict) -> "Task":
        return Task(
            id=data.get("id", ""),
            subject=data.get("subject", ""),
            description=data.get("description", ""),
            status=data.get("status", "pending"),
            files_expected=data.get("files_expected", []),
            files_written=data.get("files_written", []),
            error=data.get("error", ""),
            blocked_by=set(data.get("blocked_by", [])),
            blocks=set(data.get("blocks", [])),
            metadata=data.get("metadata", {}),
            created_at=data.get("created_at", 0.0),
        )

    def depends_on(self, *task_ids: str) -> "Task":
        """声明依赖项（链式调用）"""
        for tid in task_ids:
            self.blocked_by.add(tid)
        return self


# ═══════════════════════════════════════════════════════════
# TaskManager
# ═══════════════════════════════════════════════════════════

class TaskManager:
    """结构化任务管理器

    管理任务的生命周期、依赖关系和并行执行。
    """

    def __init__(self, work_dir: str = ".", auto_save: bool = False):
        self._tasks: dict[str, Task] = {}
        self._lock = threading.Lock()
        self._order_counter = 0
        self._work_dir = Path(work_dir)
        self._auto_save = auto_save
        self._callbacks: list[Callable] = []

    # ── CRUD ──────────────────────────────────────────

    def add(
        self,
        subject: str,
        description: str = "",
        files: list[str] | None = None,
        metadata: dict | None = None,
        task_id: str = "",
    ) -> Task:
        """添加新任务

        Args:
            subject: 任务标题（祈使句）
            description: 详细描述
            files: 预期涉及的文件
            metadata: 附加元数据
            task_id: 自定义 ID（默认自动生成）

        Returns:
            新创建的 Task 实例（支持链式调用 .blocked_by()）
        """
        with self._lock:
            self._order_counter += 1
            if not task_id:
                task_id = f"task-{self._order_counter}"
            # 确保 ID 唯一
            while task_id in self._tasks:
                self._order_counter += 1
                task_id = f"task-{self._order_counter}"

            task = Task(
                id=task_id,
                subject=subject,
                description=description,
                files_expected=files or [],
                metadata=metadata or {},
                created_at=time.time(),
            )
            self._tasks[task_id] = task
            self._maybe_save()
            return task

    def get(self, task_id: str) -> Task | None:
        """获取任务详情"""
        with self._lock:
            return self._tasks.get(task_id)

    def update(
        self,
        task_id: str,
        status: str = "",
        error: str = "",
        files_written: list[str] | None = None,
        result: dict | None = None,
        metadata: dict | None = None,
    ) -> Task | None:
        """更新任务状态

        Args:
            task_id: 任务 ID
            status: 新状态
            error: 错误信息（status=failed 时）
            files_written: 实际写入的文件
            result: 结果数据
            metadata: 附加元数据（merge 模式）
        """
        with self._lock:
            task = self._tasks.get(task_id)
            if task is None:
                return None

            if status:
                old_status = task.status
                task.status = status
                if status == "in_progress" and not task.started_at:
                    task.started_at = time.time()
                elif status in ("completed", "failed", "skipped"):
                    task.completed_at = time.time()
                self._notify_status_change(task, old_status, status)

            if error:
                task.error = error
            if files_written is not None:
                task.files_written = files_written
            if result is not None:
                task.result = result
            if metadata:
                task.metadata.update(metadata)

            # 如果状态变为 completed，解除依赖
            if status == "completed":
                self._unblock_dependents(task_id)
            elif status == "failed":
                # 失败的任务：可选择跳过依赖项或传播失败
                self._handle_failed_dependencies(task_id)

            self._maybe_save()
            return task

    def delete(self, task_id: str) -> bool:
        """删除任务"""
        with self._lock:
            if task_id not in self._tasks:
                return False
            task = self._tasks.pop(task_id)
            # 清理依赖关系
            for other in self._tasks.values():
                other.blocked_by.discard(task_id)
                other.blocks.discard(task_id)
            self._maybe_save()
            return True

    # ── 依赖管理 ────────────────────────────────────

    def _unblock_dependents(self, completed_id: str):
        """任务完成后，解除所有依赖它的任务的阻塞"""
        for task in self._tasks.values():
            if completed_id in task.blocked_by:
                task.blocked_by.discard(completed_id)
                logger.debug(
                    f"[TaskManager] '{task.id}' unblocked "
                    f"(dependency '{completed_id}' completed)"
                )

    def _handle_failed_dependencies(self, failed_id: str):
        """处理失败任务的依赖项——标记为 skipped"""
        for task in self._tasks.values():
            if failed_id in task.blocked_by:
                task.status = "skipped"
                task.error = f"Skipped: dependency '{failed_id}' failed"
                logger.warn(f"[TaskManager] '{task.id}' skipped due to failed '{failed_id}'")

    # ── 查询 ────────────────────────────────────────

    def list_all(self) -> list[Task]:
        """列出所有任务（按创建顺序）"""
        with self._lock:
            return sorted(
                self._tasks.values(),
                key=lambda t: t.created_at,
            )

    def list_ready(self) -> list[Task]:
        """列出所有可立即开始的任务（无未满足的依赖）"""
        with self._lock:
            return [
                t for t in self._tasks.values()
                if t.is_ready
            ]

    def list_active(self) -> list[Task]:
        with self._lock:
            return [t for t in self._tasks.values() if t.is_active]

    def list_done(self) -> list[Task]:
        with self._lock:
            return [t for t in self._tasks.values() if t.is_done]

    @property
    def all_completed(self) -> bool:
        with self._lock:
            return all(t.is_done for t in self._tasks.values())

    @property
    def has_failures(self) -> bool:
        with self._lock:
            return any(t.status == "failed" for t in self._tasks.values())

    @property
    def count(self) -> int:
        return len(self._tasks)

    def summary(self) -> str:
        """任务状态摘要"""
        with self._lock:
            statuses = {}
            for t in self._tasks.values():
                statuses[t.status] = statuses.get(t.status, 0) + 1
            parts = [f"{v} {k}" for k, v in sorted(statuses.items())]
            return f"Tasks ({self.count}): " + ", ".join(parts)

    # ── 执行 ────────────────────────────────────────

    def execute_all(
        self,
        executor_fn: Callable[[Task], bool],
        parallel: bool = True,
        on_progress: Callable[[Task, str], None] | None = None,
    ) -> dict[str, bool]:
        """执行所有任务（尊重依赖关系）

        Args:
            executor_fn: 执行函数 (task) -> success: bool
            parallel: 是否并行执行无依赖关系的任务
            on_progress: 进度回调 (task, old_status)

        Returns:
            {task_id: success}
        """
        import concurrent.futures

        results: dict[str, bool] = {}

        if self.count == 0:
            logger.warn("[TaskManager] No tasks to execute")
            return results

        logger.info(f"[TaskManager] Executing {self.count} tasks "
                     f"({'parallel' if parallel else 'sequential'})")

        while not self.all_completed:
            ready = self.list_ready()
            if not ready:
                # 检查死锁
                pending = [t for t in self._tasks.values() if t.status == "pending"]
                if pending:
                    stuck = [f"{t.id}(waiting: {t.blocked_by})" for t in pending]
                    logger.error(f"[TaskManager] Deadlock detected: {stuck}")
                    for t in pending:
                        t.status = "failed"
                        t.error = "Deadlock: dependencies never satisfied"
                        results[t.id] = False
                break

            if parallel and len(ready) > 1:
                # 并行执行
                with concurrent.futures.ThreadPoolExecutor(
                    max_workers=min(len(ready), 4)
                ) as ex:
                    futures = {}
                    for task in ready:
                        self.update(task.id, status="in_progress")
                        if on_progress:
                            on_progress(task, "in_progress")
                        futures[ex.submit(executor_fn, task)] = task

                    for future in concurrent.futures.as_completed(futures):
                        task = futures[future]
                        try:
                            success = future.result()
                            status = "completed" if success else "failed"
                            results[task.id] = success
                        except Exception as e:
                            status = "failed"
                            results[task.id] = False
                            self.update(task.id, error=str(e))
                        self.update(task.id, status=status)
                        if on_progress:
                            on_progress(task, status)
            else:
                # 顺序执行
                for task in ready:
                    self.update(task.id, status="in_progress")
                    if on_progress:
                        on_progress(task, "in_progress")
                    try:
                        success = executor_fn(task)
                        status = "completed" if success else "failed"
                        results[task.id] = success
                    except Exception as e:
                        status = "failed"
                        results[task.id] = False
                        self.update(task.id, error=str(e))
                    self.update(task.id, status=status)
                    if on_progress:
                        on_progress(task, status)

        # 汇总
        completed = sum(1 for v in results.values() if v)
        logger.success(
            f"[TaskManager] Done: {completed}/{len(results)} tasks succeeded"
        )
        return results

    def execute_plan_steps(
        self,
        plan,
        model: str | None = None,
        work_dir: str = ".",
    ) -> dict[str, bool]:
        """从 Plan 对象创建任务并执行

        与 PlanExecutor 配合：将 Plan.steps 转换为 Task 列表。

        Args:
            plan: Plan 对象（来自 planner.py）
            model: LLM 模型
            work_dir: 工作目录

        Returns:
            {task_id: success}
        """
        from patchflow.core.fix.generator import generate, write_files

        # 从 Plan 创建任务
        plan_tasks: dict[int, str] = {}  # step_index → task_id
        for i, step in enumerate(plan.steps):
            task = self.add(
                subject=step.title,
                description=step.description,
                files=step.files_expected,
            )
            plan_tasks[i] = task.id

        # 设置依赖（默认顺序依赖：step N 依赖 step N-1）
        for i in range(1, len(plan.steps)):
            self._tasks[plan_tasks[i]].blocked_by(plan_tasks[i-1])

        def _executor(task: Task) -> bool:
            # 从 task 描述中提取步骤信息
            step = None
            for i, s in enumerate(plan.steps):
                if plan_tasks.get(i) == task.id:
                    step = s
                    break

            if step is None:
                return False

            from patchflow.core.project.context_collector import (
                ContextCollector, build_context_prompt,
            )
            collector = ContextCollector(work_dir)
            ctx = collector.collect(use_cache=True)
            context_prompt = build_context_prompt(ctx)

            files = generate(
                step.task,
                model=model,
                project_context=context_prompt,
            )
            if files is None:
                return False

            written = write_files(files, work_dir=work_dir)
            self.update(task.id, files_written=written)
            return len(written) > 0

        return self.execute_all(_executor, parallel=False)

    # ── 回调 ────────────────────────────────────────

    def on_status_change(self, callback: Callable):
        """注册状态变更回调"""
        self._callbacks.append(callback)

    def _notify_status_change(self, task: Task, old_status: str, new_status: str):
        for cb in self._callbacks:
            try:
                cb(task, old_status, new_status)
            except Exception:
                pass

    # ── 持久化 ──────────────────────────────────────

    def _maybe_save(self):
        if self._auto_save:
            self.save()

    def save(self, path: str = ""):
        """保存任务列表到文件"""
        target = Path(path) if path else (
            self._work_dir / ".patchflow" / "tasks.json"
        )
        target.parent.mkdir(parents=True, exist_ok=True)
        with self._lock:
            data = {
                "tasks": [t.to_dict() for t in self._tasks.values()],
                "order_counter": self._order_counter,
            }
        target.write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def load(self, path: str = ""):
        """从文件加载任务列表"""
        target = Path(path) if path else (
            self._work_dir / ".patchflow" / "tasks.json"
        )
        if not target.exists():
            return
        with self._lock:
            try:
                data = json.loads(
                    target.read_text(encoding="utf-8", errors="replace")
                )
                self._tasks = {}
                for item in data.get("tasks", []):
                    task = Task.from_dict(item)
                    self._tasks[task.id] = task
                self._order_counter = data.get("order_counter", 0)
                logger.info(f"[TaskManager] Loaded {len(self._tasks)} tasks")
            except (json.JSONDecodeError, KeyError) as e:
                logger.warn(f"[TaskManager] Load failed: {e}")

    def clear(self):
        with self._lock:
            self._tasks.clear()
            self._order_counter = 0


# ═══════════════════════════════════════════════════════════
# 便捷工具
# ═══════════════════════════════════════════════════════════

def create_task_chain(
    tm: TaskManager,
    steps: list[tuple[str, str, list[str] | None]],
) -> list[Task]:
    """快速创建任务链（每步依赖前一步）

    Args:
        tm: TaskManager 实例
        steps: [(subject, description, files), ...]

    Returns:
        创建的 Task 列表
    """
    tasks = []
    prev_id = None
    for subject, description, files in steps:
        task = tm.add(subject, description, files)
        if prev_id:
            task.depends_on(prev_id)
        tasks.append(task)
        prev_id = task.id
    return tasks
