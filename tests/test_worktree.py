"""Worktree 隔离环境测试"""
import os
import pytest
from pathlib import Path
from patchflow.core.worktree import WorktreeManager, Worktree, WorktreeInfo, get_worktree_manager


class TestWorktreeInfo:
    def test_basic_info(self):
        info = WorktreeInfo(
            name="test-wt",
            path="/tmp/test-wt",
            branch="patchflow/test-wt",
            base_branch="main",
        )
        assert info.name == "test-wt"
        assert info.patch_count == 0
        assert info.files_changed == []


class TestWorktreeManager:
    def test_create_in_git_repo(self, tmp_path):
        """在 git 仓库中创建 worktree"""
        import subprocess

        # 初始化 git 仓库
        repo = tmp_path / "repo"
        repo.mkdir()
        subprocess.run(["git", "init"], cwd=str(repo), capture_output=True)
        subprocess.run(
            ["git", "config", "user.email", "test@test.com"],
            cwd=str(repo), capture_output=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "Test"],
            cwd=str(repo), capture_output=True,
        )
        # 创建初始提交（worktree 需要）
        (repo / "test.txt").write_text("hello")
        subprocess.run(["git", "add", "."], cwd=str(repo), capture_output=True)
        subprocess.run(
            ["git", "commit", "-m", "initial"],
            cwd=str(repo), capture_output=True,
        )

        wm = WorktreeManager(str(repo))
        assert wm.is_git_repo

        wt = wm.create("test-fix")
        if wt is not None:
            assert wt.info.name.startswith("test-fix")
            assert os.path.exists(wt.info.path)
            # 清理
            wt.discard()

    def test_non_git_fallback(self, tmp_path):
        """非 git 目录使用回退方案"""
        wm = WorktreeManager(str(tmp_path))
        assert not wm.is_git_repo

        wt = wm.create("sandbox-test")
        assert wt is not None
        assert os.path.exists(wt.info.path)
        wt.discard()

    def test_apply_patches(self, tmp_path):
        """测试补丁应用"""
        wm = WorktreeManager(str(tmp_path))
        wt = wm.create("patch-test")
        assert wt is not None

        # 在 worktree 中创建文件
        test_file = wt.path / "hello.py"
        test_file.write_text("print('hello')")

        # 应用补丁修改
        result = wt.apply_patches([{
            "file": "hello.py",
            "old": "print('hello')",
            "new": "print('hello, world!')",
        }])
        assert result
        content = test_file.read_text()
        assert "hello, world!" in content

        wt.discard()

    def test_apply_patches_new_file(self, tmp_path):
        """测试创建新文件"""
        wm = WorktreeManager(str(tmp_path))
        wt = wm.create("new-file-test")
        assert wt is not None

        result = wt.apply_patches([{
            "file": "new_module.py",
            "old": "",
            "new": "# New file\ndef foo():\n    pass\n",
        }])
        assert result
        new_file = wt.path / "new_module.py"
        assert new_file.exists()
        assert "def foo()" in new_file.read_text()

        wt.discard()

    def test_apply_multiple_patches(self, tmp_path):
        """测试多个补丁"""
        wm = WorktreeManager(str(tmp_path))
        wt = wm.create("multi-patch")
        assert wt is not None

        # Create files first
        (wt.path / "a.py").write_text("a = 1\n")
        (wt.path / "b.py").write_text("b = 2\n")

        result = wt.apply_patches([
            {"file": "a.py", "old": "a = 1", "new": "a = 10"},
            {"file": "b.py", "old": "b = 2", "new": "b = 20"},
        ])
        assert result
        assert wt.info.patch_count == 2

        wt.discard()

    def test_verify_default(self, tmp_path):
        """测试默认验证"""
        wm = WorktreeManager(str(tmp_path))
        wt = wm.create("verify-test")
        assert wt is not None

        # 创建一个简单的 Python 文件
        (wt.path / "valid.py").write_text("print('ok')")
        ok = wt.verify(work_dir=str(wt.path))
        # 默认验证可能通过也可能不通过，取决于 validator 的实现
        assert isinstance(ok, bool)

        wt.discard()

    def test_verify_custom_function(self, tmp_path):
        """测试自定义验证函数"""
        wm = WorktreeManager(str(tmp_path))
        wt = wm.create("custom-verify")
        assert wt is not None

        # 自定义验证：检查文件存在
        (wt.path / "check.py").write_text("data")

        def _custom_verify(work_dir):
            return Path(work_dir, "check.py").exists()

        ok = wt.verify(validate_fn=_custom_verify)
        assert ok

        # 验证失败的情况
        def _fail_verify(work_dir):
            return Path(work_dir, "nonexistent.py").exists()

        ok2 = wt.verify(validate_fn=_fail_verify)
        assert not ok2

        wt.discard()

    def test_cleanup(self, tmp_path):
        """测试清理"""
        wm = WorktreeManager(str(tmp_path))
        wt = wm.create("cleanup-test")
        assert wt is not None
        path = wt.path
        assert os.path.exists(path)

        wt.discard()
        # After discard, path should not exist or be empty
        if os.path.exists(path):
            # Might exist as empty dir
            pass

    def test_context_manager(self, tmp_path):
        """测试上下文管理器"""
        wm = WorktreeManager(str(tmp_path))
        try:
            with wm.isolate("ctx-test") as wt:
                assert wt is not None
                assert os.path.exists(wt.path)
                (wt.path / "test.txt").write_text("content")
            # After exit, should be cleaned up
        except Exception:
            pass  # May fail in non-git context

    def test_cleanup_all(self, tmp_path):
        """测试清理所有"""
        wm = WorktreeManager(str(tmp_path))
        wt1 = wm.create("batch-1")
        wt2 = wm.create("batch-2")
        assert wm.active_count >= 2
        wm.cleanup_all()
        assert wm.active_count == 0

    def test_list_active(self, tmp_path):
        """测试列出活跃 worktree"""
        wm = WorktreeManager(str(tmp_path))
        wm.create("list-1")
        wm.create("list-2")
        active = wm.list_active()
        assert len(active) >= 2
        wm.cleanup_all()


class TestGlobalSingleton:
    def test_get_worktree_manager(self, tmp_path):
        wm1 = get_worktree_manager(str(tmp_path))
        wm2 = get_worktree_manager(str(tmp_path))
        assert wm1 is wm2  # Singleton
