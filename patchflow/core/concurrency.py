"""Concurrency — 并发安全基础设施

当多个 Agent 被并行调度时（如同时修复不同文件），这些工具保证：
  1. 同一时刻只有一个 Agent 修改同一个文件（文件级互斥）
  2. 共享状态（Blackboard、MemoryBank 等）的读写不会产生竞态
  3. 每个 Agent 的临界区操作是原子的

核心组件：
  FileLockManager — 按文件路径分发的互斥锁（不同文件可并行，同文件串行）
  ThreadSafeMixin  — 给已有类加上 threading.Lock 的 mixin
"""

import threading
from contextlib import contextmanager
from pathlib import Path


class FileLockManager:
    """文件级互斥锁管理器

    保证同一文件的并发写入串行化，不同文件之间不互相阻塞。
    用法：
        flm = FileLockManager()
        with flm.lock("src/main.py"):
            Path("src/main.py").write_text(new_content)
    """

    def __init__(self):
        self._locks: dict[str, threading.Lock] = {}
        self._refcount: dict[str, int] = {}
        self._global = threading.Lock()

    def _key(self, file_path: str) -> str:
        return str(Path(file_path).resolve())

    @contextmanager
    def lock(self, file_path: str):
        key = self._key(file_path)
        with self._global:
            if key not in self._locks:
                self._locks[key] = threading.Lock()
                self._refcount[key] = 0
            self._refcount[key] += 1
            lock = self._locks[key]

        lock.acquire()
        try:
            yield
        finally:
            lock.release()
            with self._global:
                self._refcount[key] -= 1
                if self._refcount[key] <= 0:
                    self._locks.pop(key, None)
                    self._refcount.pop(key, None)

    @contextmanager
    def lock_many(self, file_paths: list[str]):
        """对多个文件加锁（按路径排序防死锁）"""
        keys = sorted(self._key(p) for p in file_paths)
        locks = []
        for key in keys:
            with self._global:
                if key not in self._locks:
                    self._locks[key] = threading.Lock()
                    self._refcount[key] = 0
                self._refcount[key] += 1
                locks.append(self._locks[key])

        for lk in locks:
            lk.acquire()
        try:
            yield
        finally:
            for lk in reversed(locks):
                lk.release()
            with self._global:
                for key in keys:
                    self._refcount[key] -= 1
                    if self._refcount[key] <= 0:
                        self._locks.pop(key, None)
                        self._refcount.pop(key, None)


# 全局单例 — 整个进程共享同一个 FileLockManager
_global_flm: FileLockManager | None = None


def get_file_lock_manager() -> FileLockManager:
    global _global_flm
    if _global_flm is None:
        _global_flm = FileLockManager()
    return _global_flm


class AtomicWrite:
    """原子文件写入 — 先写临时文件，再原子替换

    防止并发写入导致文件内容损坏（读到一半写完的内容）。
    """

    @staticmethod
    def write(file_path: str | Path, content: str, encoding: str = "utf-8") -> None:
        import os
        import tempfile
        target = Path(file_path)
        target.parent.mkdir(parents=True, exist_ok=True)

        fd, tmp = tempfile.mkstemp(dir=str(target.parent), prefix=f".{target.name}.")
        try:
            os.write(fd, content.encode(encoding))
            os.fsync(fd)
            os.close(fd)
            os.replace(tmp, str(target))
        except Exception:
            os.close(fd)
            Path(tmp).unlink(missing_ok=True)
            raise
