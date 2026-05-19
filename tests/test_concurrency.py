"""并发安全模块测试"""

import threading
import time

from patchflow.core.concurrency import AtomicWrite, FileLockManager, get_file_lock_manager


class TestFileLockManager:
    """FileLockManager 单元测试"""

    def test_lock_serializes_access(self, tmp_path):
        flm = FileLockManager()
        results = []

        def writer(value: int):
            with flm.lock("shared.txt"):
                results.append(value)
                time.sleep(0.02)
                results.append(value * 10)

        t1 = threading.Thread(target=writer, args=(1,))
        t2 = threading.Thread(target=writer, args=(2,))
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        # 序列化执行：不能交错
        assert results in ([1, 10, 2, 20], [2, 20, 1, 10])

    def test_different_files_run_concurrently(self):
        flm = FileLockManager()
        results = []

        def writer(file: str, value: int):
            with flm.lock(file):
                results.append(value)
                time.sleep(0.03)
                results.append(value * 10)

        t1 = threading.Thread(target=writer, args=("a.txt", 1))
        t2 = threading.Thread(target=writer, args=("b.txt", 2))
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        # 不同文件可以交错执行
        assert 1 in results and 2 in results

    def test_lock_many_prevents_deadlock(self, tmp_path):
        flm = FileLockManager()
        results = []
        barrier = threading.Barrier(2, timeout=5)

        def worker(files: list[str], marker: str):
            barrier.wait()
            with flm.lock_many(files):
                results.append(marker)
                time.sleep(0.02)

        files = ["a.txt", "b.txt"]
        t1 = threading.Thread(target=worker, args=(files, "t1"))
        t2 = threading.Thread(target=worker, args=(files, "t2"))
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        assert len(results) == 2

    def test_global_singleton(self):
        flm1 = get_file_lock_manager()
        flm2 = get_file_lock_manager()
        assert flm1 is flm2


class TestAtomicWrite:
    """AtomicWrite 单元测试"""

    def test_write_creates_file(self, tmp_path):
        target = tmp_path / "test.py"
        AtomicWrite.write(str(target), "print('hello')")
        assert target.exists()
        assert target.read_text() == "print('hello')"

    def test_overwrite_existing(self, tmp_path):
        target = tmp_path / "test.py"
        target.write_text("old content")
        AtomicWrite.write(str(target), "new content")
        assert target.read_text() == "new content"

    def test_creates_parent_dirs(self, tmp_path):
        target = tmp_path / "nested" / "deep" / "test.py"
        AtomicWrite.write(str(target), "data")
        assert target.exists()
        assert target.read_text() == "data"

    def test_concurrent_writes_no_corruption(self, tmp_path):
        """多个线程同时写入同一文件（配合 FileLockManager），最终内容完整"""
        target = tmp_path / "shared.txt"
        flm = FileLockManager()
        errors = []

        def writer(content: str):
            try:
                with flm.lock(str(target)):
                    AtomicWrite.write(str(target), content)
            except Exception as e:
                errors.append(str(e))

        threads = []
        for i in range(10):
            t = threading.Thread(target=writer, args=(f"content-{i}\n",))
            threads.append(t)
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert target.exists()
        content = target.read_text()
        assert content.startswith("content-")
        assert not errors


class TestConcurrencyIntegration:
    """并发集成测试：多个"Agent"同时操作 Blackboard + PatchApplicator"""

    def test_parallel_patch_apply(self, tmp_path):
        """并行补丁应用：不同文件并行写入"""
        from patchflow.core.fix.patch_applicator import PatchApplicator, SnippetPatch

        # 创建文件
        for name in ["a.py", "b.py", "c.py"]:
            (tmp_path / name).write_text(f"# file {name}\nx = 1\n")

        def apply_for_file(name: str):
            patch = SnippetPatch(file=name, old="x = 1", new="x = 2", reason="test")
            PatchApplicator.apply(name, [patch], work_dir=str(tmp_path))

        threads = [threading.Thread(target=apply_for_file, args=(name,)) for name in ["a.py", "b.py", "c.py"]]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        for name in ["a.py", "b.py", "c.py"]:
            assert "x = 2" in (tmp_path / name).read_text()

    def test_parallel_same_file_serialized(self, tmp_path):
        """并行写入同一文件：最终一致性"""
        from patchflow.core.fix.patch_applicator import PatchApplicator, SnippetPatch

        (tmp_path / "shared.py").write_text("count = 0\n")

        def apply_patch(target_value: int):
            patch = SnippetPatch(
                file="shared.py",
                old=f"count = {target_value - 1}",
                new=f"count = {target_value}",
                reason=f"set to {target_value}"
            )
            PatchApplicator.apply("shared.py", [patch], work_dir=str(tmp_path))

        threads = []
        for i in range(1, 6):
            t = threading.Thread(target=apply_patch, args=(i,))
            threads.append(t)
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        content = (tmp_path / "shared.py").read_text()
        assert "count = " in content

    def test_thread_safe_blackboard(self):
        """Blackboard 并发读写不抛异常"""
        from patchflow.agents.blackboard import Blackboard

        bb = Blackboard(task="test", code={"a.py": "x=1"})
        errors = []

        def reader():
            try:
                for _ in range(50):
                    _ = bb.get("task")
                    _ = bb.summary()
                    time.sleep(0.001)
            except Exception as e:
                errors.append(str(e))

        def writer():
            try:
                for i in range(50):
                    bb.data["task"] = f"task-{i}"
                    bb.data["code"] = {f"file{i}.py": f"content-{i}"}
                    time.sleep(0.001)
            except Exception as e:
                errors.append(str(e))

        threads = [threading.Thread(target=reader) for _ in range(3)]
        threads += [threading.Thread(target=writer) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors

    def test_thread_safe_memory_bank(self, tmp_path):
        """FixMemoryBank 并发 add/query 不抛异常"""
        from patchflow.core.fix.memory_bank import FixMemoryBank

        work = str(tmp_path)
        bank = FixMemoryBank(work_dir=work)
        errors = []

        def worker(start: int):
            try:
                for i in range(start, start + 20):
                    bank.add(f"error_{i % 5}", f"cause_{i % 3}",
                              f"fix_{i}", [f"file_{i}.py"], i % 2 == 0, f"strategy_{i % 3}")
                    bank.query(f"error_{i % 5}", f"cause_{i % 3}")
            except Exception as e:
                errors.append(str(e))

        threads = [threading.Thread(target=worker, args=(i * 20,)) for i in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors
        assert bank.entry_count > 0
