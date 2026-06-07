"""Cron Scheduler — 定时任务调度

从 Claude Code 的 CronCreate/CronDelete/CronList 移植：
  - 创建定时任务（cron 表达式 + prompt）
  - 持久化到 .patchflow/cron_tasks.json
  - 支持 one-shot 和 recurring 两种模式
  - 自动清理过期任务

使用方式：
    from patchflow.core.cron_scheduler import CronScheduler

    sched = CronScheduler()
    sched.add("*/30 * * * *", "fix: check lint errors", recurring=True)
    sched.add("0 9 * * 1-5", "patchflow analyze", recurring=True, durable=True)

CLI 集成：
    patchflow cron add "*/30 * * * *" "fix: check lint"
    patchflow cron list
    patchflow cron remove <id>
    patchflow cron run-once  # 手动触发一次待执行任务
"""

import json
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from patchflow.utils import logger


@dataclass
class CronTask:
    """定时任务"""
    id: str = ""
    cron: str = ""                 # 5-field cron 表达式
    prompt: str = ""               # 触发时执行的 prompt
    recurring: bool = True         # True=重复, False=一次性
    durable: bool = False          # True=持久化到磁盘
    created_at: float = 0.0
    last_run_at: float = 0.0
    next_run_at: float = 0.0
    run_count: int = 0
    enabled: bool = True

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "cron": self.cron,
            "prompt": self.prompt,
            "recurring": self.recurring,
            "durable": self.durable,
            "created_at": self.created_at,
            "last_run_at": self.last_run_at,
            "next_run_at": self.next_run_at,
            "run_count": self.run_count,
            "enabled": self.enabled,
        }

    @staticmethod
    def from_dict(data: dict) -> "CronTask":
        return CronTask(
            id=data.get("id", ""),
            cron=data.get("cron", ""),
            prompt=data.get("prompt", ""),
            recurring=data.get("recurring", True),
            durable=data.get("durable", False),
            created_at=data.get("created_at", 0.0),
            last_run_at=data.get("last_run_at", 0.0),
            next_run_at=data.get("next_run_at", 0.0),
            run_count=data.get("run_count", 0),
            enabled=data.get("enabled", True),
        )

    @property
    def is_expired(self) -> bool:
        """重复任务 7 天后自动过期"""
        if not self.recurring:
            return self.run_count > 0
        return (time.time() - self.created_at) > 7 * 24 * 3600


class CronScheduler:
    """轻量级 Cron 任务调度器

    职责：
      1. 管理 cron 任务（增删查）
      2. 持久化到 .patchflow/cron_tasks.json
      3. 计算下次执行时间
      4. 触发执行（配合 agent 调度器）
    """

    STORAGE_FILE = "cron_tasks.json"
    MAX_TASKS = 50

    def __init__(self, work_dir: str = "."):
        self._work_dir = Path(work_dir).resolve()
        self._tasks: dict[str, CronTask] = {}
        self._lock = threading.Lock()
        self._running = False
        self._thread: threading.Thread | None = None
        self._callback = None  # 执行回调

    @property
    def task_count(self) -> int:
        return len(self._tasks)

    @property
    def storage_path(self) -> Path:
        return self._work_dir / ".patchflow" / self.STORAGE_FILE

    def set_callback(self, callback):
        """设置任务执行回调 callback(task: CronTask) -> bool"""
        self._callback = callback

    # ── CRUD ──────────────────────────────────────────

    def add(
        self,
        cron: str,
        prompt: str,
        recurring: bool = True,
        durable: bool = False,
    ) -> CronTask | None:
        """添加定时任务

        Args:
            cron: 5-field cron 表达式（分 时 日 月 周）
            prompt: 触发时执行的 prompt
            recurring: True=重复, False=一次性
            durable: True=持久化到磁盘

        Returns:
            创建的 CronTask，失败返回 None
        """
        # 验证 cron 表达式
        if not _validate_cron(cron):
            logger.error(f"[CronScheduler] Invalid cron: '{cron}'")
            return None

        with self._lock:
            if len(self._tasks) >= self.MAX_TASKS:
                logger.error(f"[CronScheduler] Max tasks ({self.MAX_TASKS}) reached")
                return None

            task_id = f"cron_{uuid.uuid4().hex[:8]}"
            now = time.time()

            task = CronTask(
                id=task_id,
                cron=cron.strip(),
                prompt=prompt.strip(),
                recurring=recurring,
                durable=durable,
                created_at=now,
                next_run_at=_next_run_time(cron, now),
                enabled=True,
            )
            self._tasks[task_id] = task

            if durable:
                self._save()

            logger.info(
                f"[CronScheduler] Added task '{task_id}': "
                f"{cron} → '{prompt[:60]}' "
                f"({'recurring' if recurring else 'one-shot'})"
            )
            return task

    def get(self, task_id: str) -> CronTask | None:
        with self._lock:
            return self._tasks.get(task_id)

    def remove(self, task_id: str) -> bool:
        with self._lock:
            if task_id not in self._tasks:
                return False
            task = self._tasks.pop(task_id)
            if task.durable:
                self._save()
            logger.info(f"[CronScheduler] Removed task '{task_id}'")
            return True

    def list_all(self) -> list[CronTask]:
        with self._lock:
            return sorted(
                self._tasks.values(),
                key=lambda t: t.next_run_at,
            )

    def enable(self, task_id: str) -> bool:
        with self._lock:
            task = self._tasks.get(task_id)
            if task:
                task.enabled = True
                return True
            return False

    def disable(self, task_id: str) -> bool:
        with self._lock:
            task = self._tasks.get(task_id)
            if task:
                task.enabled = False
                return True
            return False

    # ── 调度 ──────────────────────────────────────────

    def tick(self) -> list[CronTask]:
        """检查并返回所有待执行的任务

        Returns:
            待执行的任务列表（已自动更新下次执行时间）
        """
        now = time.time()
        due = []

        with self._lock:
            # 清理过期任务
            expired = [tid for tid, t in self._tasks.items() if t.is_expired]
            for tid in expired:
                self._tasks.pop(tid)
                logger.info(f"[CronScheduler] Expired task removed: '{tid}'")

            for task in self._tasks.values():
                if not task.enabled:
                    continue
                if now >= task.next_run_at:
                    due.append(task)
                    task.last_run_at = now
                    task.run_count += 1
                    if task.recurring:
                        task.next_run_at = _next_run_time(
                            task.cron, now
                        )
                    else:
                        task.next_run_at = float("inf")  # 一次性任务

            if due:
                self._save()

        return due

    def run_once(self) -> int:
        """手动触发一次所有待执行任务

        Returns:
            执行的任务数
        """
        due = self.tick()
        for task in due:
            logger.info(f"[CronScheduler] Running: {task.prompt[:60]}")
            if self._callback:
                try:
                    self._callback(task)
                except Exception as e:
                    logger.error(f"[CronScheduler] Callback failed: {e}")

        # 清理一次性任务
        with self._lock:
            for task in due:
                if not task.recurring and task.run_count > 0:
                    self._tasks.pop(task.id, None)

        return len(due)

    def start_background(self, interval_seconds: int = 60):
        """启动后台调度线程

        Args:
            interval_seconds: 检查间隔（秒）
        """
        if self._running:
            return

        self._running = True

        def _loop():
            while self._running:
                try:
                    self.run_once()
                except Exception as e:
                    logger.error(f"[CronScheduler] Background error: {e}")
                time.sleep(interval_seconds)

        self._thread = threading.Thread(target=_loop, daemon=True)
        self._thread.start()
        logger.info(f"[CronScheduler] Background scheduler started (every {interval_seconds}s)")

    def stop_background(self):
        """停止后台调度线程"""
        self._running = False
        if self._thread:
            self._thread.join(timeout=5)
            self._thread = None
        logger.info("[CronScheduler] Background scheduler stopped")

    # ── 持久化 ──────────────────────────────────────

    def _save(self):
        """保存到磁盘（只保存 durable 任务）"""
        path = self.storage_path
        path.parent.mkdir(parents=True, exist_ok=True)

        durable_tasks = [
            t.to_dict() for t in self._tasks.values() if t.durable
        ]
        path.write_text(
            json.dumps({"tasks": durable_tasks}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def load(self):
        """从磁盘加载"""
        path = self.storage_path
        if not path.exists():
            return

        with self._lock:
            try:
                data = json.loads(
                    path.read_text(encoding="utf-8", errors="replace")
                )
                loaded = 0
                for item in data.get("tasks", []):
                    task = CronTask.from_dict(item)
                    self._tasks[task.id] = task
                    loaded += 1
                logger.info(f"[CronScheduler] Loaded {loaded} durable tasks")
            except (json.JSONDecodeError, KeyError) as e:
                logger.warn(f"[CronScheduler] Load failed: {e}")


# ═══════════════════════════════════════════════════════════
# Cron 解析工具
# ═══════════════════════════════════════════════════════════

def _validate_cron(cron: str) -> bool:
    """验证 5-field cron 表达式"""
    fields = cron.strip().split()
    if len(fields) != 5:
        return False

    valid_ranges = [
        (0, 59),  # minute
        (0, 23),  # hour
        (1, 31),  # day of month
        (1, 12),  # month
        (0, 7),   # day of week (0/7=Sunday)
    ]

    for i, field in enumerate(fields):
        min_val, max_val = valid_ranges[i]
        # 处理 * */N 等特殊值
        if field == "*":
            continue
        # 处理 */N
        if field.startswith("*/"):
            try:
                step = int(field[2:])
                if step < 1:
                    return False
                continue
            except ValueError:
                return False
        # 处理逗号分隔: 1,2,3
        for part in field.split(","):
            part = part.strip()
            # 处理范围+步长: 0-30/5
            step = 1
            if "/" in part:
                part, step_str = part.split("/", 1)
                try:
                    step = int(step_str)
                except ValueError:
                    return False
            # 处理范围: 1-5
            if "-" in part:
                try:
                    start, end = part.split("-", 1)
                    s, e = int(start), int(end)
                    if not (min_val <= s <= max_val and min_val <= e <= max_val):
                        return False
                    if s > e:
                        return False
                except ValueError:
                    return False
            else:
                try:
                    val = int(part)
                    if not (min_val <= val <= max_val):
                        return False
                except ValueError:
                    return False

    return True


def _next_run_time(cron: str, from_time: float) -> float:
    """计算从 from_time 开始的下次执行时间

    简化实现：解析 cron 的 minute 和 hour 字段进行计算。
    完整实现需要 cron 库（如 croniter），这里做基础计算。
    """
    try:
        from croniter import croniter
        from datetime import datetime
        dt = datetime.fromtimestamp(from_time)
        it = croniter(cron, dt)
        return it.get_next()
    except ImportError:
        pass

    # 回退：简易计算（只处理常见的 minute/hour 模式）
    import datetime

    fields = cron.strip().split()
    minute_str, hour_str = fields[0], fields[1]

    def _parse_minutes(f: str) -> set[int]:
        if f == "*":
            return set(range(60))
        if f.startswith("*/"):
            step = int(f[2:])
            return set(range(0, 60, step))
        result = set()
        for part in f.split(","):
            part = part.strip()
            if "-" in part:
                start, end = part.split("-", 1)
                result.update(range(int(start), int(end) + 1))
            else:
                result.add(int(part))
        return result

    def _parse_hours(f: str) -> set[int]:
        if f == "*":
            return set(range(24))
        if f.startswith("*/"):
            step = int(f[2:])
            return set(range(0, 24, step))
        result = set()
        for part in f.split(","):
            part = part.strip()
            if "-" in part:
                start, end = part.split("-", 1)
                result.update(range(int(start), int(end) + 1))
            else:
                result.add(int(part))
        return result

    valid_minutes = _parse_minutes(minute_str)
    valid_hours = _parse_hours(hour_str)

    dt = datetime.datetime.fromtimestamp(from_time)
    # 从当前分钟开始找
    current_minute = dt.minute
    current_hour = dt.hour

    # 找今天接下来的时间
    for h_offset in range(24):
        check_hour = (current_hour + h_offset) % 24
        if check_hour not in valid_hours:
            continue
        min_start = current_minute if h_offset == 0 else 0
        for m in sorted(valid_minutes):
            if m >= min_start:
                target = dt.replace(
                    hour=check_hour, minute=m, second=0, microsecond=0
                )
                if h_offset == 0 and m < current_minute:
                    continue
                if target <= dt:
                    target += datetime.timedelta(days=1)
                return target.timestamp()

    # 回退到明天
    tomorrow = dt + datetime.timedelta(days=1)
    return tomorrow.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
