"""Worktree — Git Worktree 隔离环境

从 Claude Code 的 EnterWorktree/ExitWorktree 移植：
  - 在独立的 git worktree 中执行修复（不影响原始工作区）
  - 支持并行多个 worktree（同时尝试不同修复方案）
  - 自动清理和合并

与 SnapshotManager 的区别：
  SnapshotManager: 文件复制 → 回滚（慢、不可靠、占用磁盘）
  Worktree: git worktree 原生隔离（快、利用 git 的完整性保证）

使用方式：
    from patchflow.core.worktree import WorktreeManager

    wm = WorktreeManager(work_dir=".")
    wt = wm.create("fix-attempt-1")
    try:
        # 在隔离环境中修改文件
        wt.apply_patches([...])
        if wt.verify():
            wt.merge()  # 合并回主分支
        else:
            wt.discard()  # 丢弃
    finally:
        wt.cleanup()
"""

import os
import shutil
import subprocess
import tempfile
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from patchflow.utils import logger


@dataclass
class WorktreeInfo:
    """Worktree 元信息"""
    name: str = ""
    path: str = ""
    branch: str = ""
    base_branch: str = ""
    created_at: float = 0.0
    committed: bool = False
    patch_count: int = 0
    files_changed: list[str] = field(default_factory=list)


class Worktree:
    """单个 Git Worktree 实例

    代表一个隔离的工作空间副本。
    """

    def __init__(self, info: WorktreeInfo, work_dir: str):
        self.info = info
        self._work_dir = work_dir
        self._applied_patches: list[dict] = []
        self._verified = False

    @property
    def path(self) -> Path:
        return Path(self.info.path)

    @property
    def branch(self) -> str:
        return self.info.branch

    def apply_patches(self, patches: list[dict]) -> bool:
        """在 worktree 中应用补丁

        Args:
            patches: [{"file": "src/main.py", "old": "...", "new": "..."}, ...]

        Returns:
            True if all patches applied successfully
        """
        applied = []
        try:
            for patch in patches:
                file_path = patch.get("file", "")
                new_content = patch.get("new", "")
                if not file_path:
                    continue

                target = self.path / file_path
                target.parent.mkdir(parents=True, exist_ok=True)

                # 如果文件不存在，创建
                if not target.exists():
                    target.write_text(new_content or "", encoding="utf-8")
                else:
                    old = patch.get("old", "")
                    current = target.read_text(encoding="utf-8", errors="replace")
                    if old and old in current:
                        target.write_text(
                            current.replace(old, new_content or ""),
                            encoding="utf-8",
                        )
                    else:
                        # 整文件替换
                        target.write_text(new_content or "", encoding="utf-8")

                applied.append(file_path)
                logger.debug(f"[Worktree] Applied patch to: {file_path}")

            self._applied_patches = patches
            self.info.files_changed = applied
            self.info.patch_count = len(applied)
            return True

        except Exception as e:
            logger.error(f"[Worktree] Failed to apply patches: {e}")
            return False

    def verify(self, validate_fn=None, work_dir: str | None = None) -> bool:
        """在 worktree 中运行验证

        Args:
            validate_fn: 自定义验证函数 (worktree_path) -> bool
            work_dir: worktree 路径（供 validate_fn 使用）
        """
        wd = work_dir or str(self.path)

        if validate_fn:
            try:
                self._verified = validate_fn(wd)
            except Exception as e:
                logger.warn(f"[Worktree] Verification failed: {e}")
                self._verified = False
        else:
            # 默认验证：使用 PatchFlow 的 validator
            try:
                from patchflow.core.fix.validator import validate
                result = validate(work_dir=wd)
                self._verified = result.ok
            except Exception as e:
                logger.warn(f"[Worktree] Default verification failed: {e}")
                self._verified = False

        return self._verified

    def commit_changes(self, message: str = "") -> bool:
        """提交 worktree 中的变更到分支"""
        try:
            self._git("add", "-A")
            commit_msg = message or f"patchflow: fix attempt ({self.info.name})"
            self._git("commit", "-m", commit_msg, allow_empty=False)
            self.info.committed = True
            logger.info(f"[Worktree] Committed changes: {commit_msg}")
            return True
        except subprocess.CalledProcessError:
            # 没有变更可提交
            logger.debug("[Worktree] No changes to commit")
            return False
        except Exception as e:
            logger.warn(f"[Worktree] Commit failed: {e}")
            return False

    def merge(self, message: str = "") -> bool:
        """合并 worktree 分支到原始分支

        先提交 worktree 变更，然后合并到 base_branch。
        """
        if not self.info.committed:
            if not self.commit_changes(message):
                logger.warn("[Worktree] Nothing to merge (no commits)")
                return False

        try:
            # 切换回主分支
            self._git("checkout", self.info.base_branch)
            # 合并 worktree 分支
            self._git("merge", self.info.branch, "--no-ff", "-m",
                      f"Merge worktree '{self.info.name}': {message or 'patchflow fix'}")
            # 删除 worktree 分支
            self._git("branch", "-d", self.info.branch)
            logger.success(f"[Worktree] Merged '{self.info.name}' into '{self.info.base_branch}'")
            return True
        except subprocess.CalledProcessError as e:
            logger.error(f"[Worktree] Merge failed: {e}")
            # 尝试 abort
            try:
                self._git("merge", "--abort")
            except Exception:
                pass
            return False

    def discard(self):
        """丢弃 worktree 及其分支"""
        logger.info(f"[Worktree] Discarding worktree '{self.info.name}'")
        # 只有在 git worktree 时才尝试 git 命令
        if self.info.branch and self.info.branch != "(no-git)":
            try:
                self._git("worktree", "remove", "--force", str(self.path))
            except subprocess.CalledProcessError as e:
                logger.debug(f"[Worktree] git remove failed (non-fatal): {e}")
            try:
                self._git("branch", "-D", self.info.branch)
            except Exception:
                pass
        # 删除目录
        if self.path.exists():
            shutil.rmtree(str(self.path), ignore_errors=True)

    def cleanup(self):
        """清理 worktree（如果尚未处理）"""
        if not self.path.exists():
            return
        if self.info.branch and self.info.branch != "(no-git)":
            try:
                self._git("worktree", "prune")
            except Exception:
                pass
        # 非 git 或 git 清理失败 → 直接删除
        if self.path.exists():
            shutil.rmtree(str(self.path), ignore_errors=True)

    def _git(self, *args, allow_empty: bool = False,
             cwd: str | None = None, **kwargs) -> subprocess.CompletedProcess:
        """在 worktree 中执行 git 命令"""
        cmd = ["git"] + list(args)
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                cwd=cwd or str(self.path),
                check=False,
                timeout=30,
            )
            if result.returncode != 0:
                stderr = result.stderr.strip()
                # "nothing to commit" 不是真正的错误
                if "nothing to commit" in stderr and allow_empty:
                    pass
                elif "nothing to commit" in stderr:
                    raise subprocess.CalledProcessError(
                        result.returncode, cmd, result.stdout, result.stderr
                    )
                elif stderr:
                    raise subprocess.CalledProcessError(
                        result.returncode, cmd, result.stdout, result.stderr
                    )
            return result
        except subprocess.TimeoutExpired:
            raise subprocess.CalledProcessError(-1, cmd, "", "timeout")

    def diff(self) -> str:
        """获取 worktree 中的 diff"""
        try:
            result = self._git("diff", "--stat", allow_empty=True)
            return result.stdout.strip()
        except Exception:
            return ""


# ═══════════════════════════════════════════════════════════
# WorktreeManager
# ═══════════════════════════════════════════════════════════

class WorktreeManager:
    """Git Worktree 管理器

    管理多个 worktree 实例的创建、跟踪和清理。
    """

    def __init__(self, work_dir: str = "."):
        self._work_dir = str(Path(work_dir).resolve())
        self._active: dict[str, Worktree] = {}
        self._git_repo = self._find_git_root()

    @property
    def is_git_repo(self) -> bool:
        return self._git_repo is not None

    @property
    def git_root(self) -> str:
        return self._git_repo or self._work_dir

    @property
    def active_count(self) -> int:
        return len(self._active)

    def _find_git_root(self) -> str | None:
        """查找 git 根目录"""
        try:
            result = subprocess.run(
                ["git", "rev-parse", "--show-toplevel"],
                capture_output=True, text=True,
                cwd=self._work_dir, timeout=10,
            )
            if result.returncode == 0:
                return result.stdout.strip()
        except Exception:
            pass
        return None

    def _git(self, *args, **kwargs) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["git"] + list(args),
            capture_output=True, text=True,
            cwd=self.git_root, timeout=30, check=False,
            **kwargs,
        )

    def create(
        self,
        name: str = "",
        base_ref: str = "",
    ) -> Worktree | None:
        """创建一个新的 git worktree

        Args:
            name: worktree 名称（用于分支名和目录名）
            base_ref: 基础引用（默认从当前 HEAD 创建）

        Returns:
            Worktree 实例，失败返回 None
        """
        if not self.is_git_repo:
            logger.warn("[Worktree] Not a git repository, using temp directory fallback")
            return self._create_fallback(name)

        if not name:
            name = f"pf-{uuid.uuid4().hex[:8]}"

        branch_name = f"patchflow/{name}"
        worktree_path = os.path.join(
            self.git_root, ".patchflow", "worktrees", name
        )

        # 确保唯一
        suffix = 0
        original_name = name
        while os.path.exists(worktree_path) or self._branch_exists(branch_name):
            suffix += 1
            name = f"{original_name}-{suffix}"
            branch_name = f"patchflow/{name}"
            worktree_path = os.path.join(
                self.git_root, ".patchflow", "worktrees", name
            )

        # 确定 base ref
        if not base_ref:
            # 从当前 HEAD 创建
            try:
                head_result = self._git("rev-parse", "--abbrev-ref", "HEAD")
                base_ref = head_result.stdout.strip()
            except Exception:
                base_ref = "HEAD"

        try:
            # 创建 worktree
            result = self._git(
                "worktree", "add", "-b", branch_name,
                worktree_path, base_ref,
            )
            if result.returncode != 0:
                logger.error(f"[Worktree] Failed to create: {result.stderr.strip()}")
                return self._create_fallback(name)

            import time
            info = WorktreeInfo(
                name=name,
                path=worktree_path,
                branch=branch_name,
                base_branch=self._current_branch() or base_ref,
                created_at=time.time(),
            )
            wt = Worktree(info, self.git_root)
            self._active[name] = wt
            logger.info(
                f"[Worktree] Created '{name}' at {worktree_path} "
                f"(branch: {branch_name})"
            )
            return wt

        except Exception as e:
            logger.warn(f"[Worktree] Create failed: {e}")
            return self._create_fallback(name)

    def _create_fallback(self, name: str = "") -> Worktree:
        """回退方案：非 git 项目的目录复制隔离"""
        import time
        if not name:
            name = f"pf-{uuid.uuid4().hex[:8]}"

        fallback_dir = os.path.join(
            self._work_dir, ".patchflow", "sandboxes", name
        )
        if os.path.exists(fallback_dir):
            shutil.rmtree(fallback_dir, ignore_errors=True)

        # 复制项目文件（排除 .git, .patchflow, venv 等）
        shutil.copytree(
            self._work_dir, fallback_dir,
            dirs_exist_ok=True,
            ignore=shutil.ignore_patterns(
                ".git", ".patchflow", "__pycache__",
                ".venv", "venv", "node_modules",
                "*.pyc", ".DS_Store",
            ),
        )

        info = WorktreeInfo(
            name=name,
            path=fallback_dir,
            branch="(no-git)",
            base_branch="(no-git)",
            created_at=time.time(),
        )
        wt = Worktree(info, self._work_dir)
        self._active[name] = wt
        logger.info(f"[Worktree] Created fallback sandbox at {fallback_dir}")
        return wt

    def _branch_exists(self, branch_name: str) -> bool:
        try:
            result = self._git("branch", "--list", branch_name)
            return branch_name in result.stdout
        except Exception:
            return False

    def _current_branch(self) -> str | None:
        try:
            result = self._git("rev-parse", "--abbrev-ref", "HEAD")
            return result.stdout.strip()
        except Exception:
            return None

    def get(self, name: str) -> Worktree | None:
        """获取已创建的 worktree"""
        return self._active.get(name)

    def list_active(self) -> list[WorktreeInfo]:
        """列出活跃的 worktree"""
        return [wt.info for wt in self._active.values()]

    def cleanup_all(self):
        """清理所有活跃的 worktree"""
        for name in list(self._active.keys()):
            wt = self._active.pop(name)
            try:
                wt.discard()
            except Exception as e:
                logger.warn(f"[Worktree] Failed to clean up '{name}': {e}")

    @contextmanager
    def isolate(self, name: str = "", base_ref: str = ""):
        """上下文管理器：创建 worktree → yield → 自动清理

        Usage:
            with wm.isolate("fix-1") as wt:
                wt.apply_patches(patches)
                if wt.verify():
                    wt.merge()
        """
        wt = self.create(name, base_ref)
        if wt is None:
            raise RuntimeError(f"Failed to create worktree '{name}'")

        try:
            yield wt
        except Exception as e:
            logger.error(f"[Worktree] Exception in isolation '{wt.info.name}': {e}")
            raise
        finally:
            if not wt.info.committed:
                wt.discard()
            self._active.pop(wt.info.name, None)


# ═══════════════════════════════════════════════════════════
# 全局单例
# ═══════════════════════════════════════════════════════════

_global_wm: WorktreeManager | None = None


def get_worktree_manager(work_dir: str = ".") -> WorktreeManager:
    """获取全局 WorktreeManager 单例"""
    global _global_wm
    if _global_wm is None:
        _global_wm = WorktreeManager(work_dir)
    return _global_wm
