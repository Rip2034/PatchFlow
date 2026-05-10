"""Agent 隔离沙箱

多 Agent 协作的安全机制，三层防护：

  1. 上下文隔离 — 每个 Agent 拥有独立的上下文副本（已有）
  2. 文件系统护栏 — 阻止路径遍历、敏感文件访问、越界写入
  3. 命令安全 — 集成 runner.py 的危险命令分级

安全策略：
  - 默认全隔离 — 每个 Agent 拥有独立的上下文副本
  - 显式 opt-in 共享 — Agent 必须明确声明要共享什么
  - 文件系统边界 — 所有 I/O 限制在项目根目录内
  - 敏感路径阻断 — .env、credentials、系统目录等直接拦截
"""

import re
from copy import deepcopy
from pathlib import Path

from patchflow.utils import logger

# ═══════════════════════════════════════════════════════════
# 敏感路径模式 — 不论项目根在哪，这些路径一律阻断
# ═══════════════════════════════════════════════════════════

_SENSITIVE_PATTERNS: list[str] = [
    # 凭证/密钥
    r'(^|[/\\])\.env($|\.)',
    r'(^|[/\\])credentials',
    r'(^|[/\\])\.?secret',
    r'(^|[/\\])\.?api_key',
    r'(^|[/\\])\.?token',
    r'(^|[/\\])\.?password',
    r'(^|[/\\])\.ssh/',
    r'(^|[/\\])\.gnupg/',
    # 系统目录
    r'^/etc/',
    r'^/boot/',
    r'^/sys/',
    r'^/proc/',
    r'^/dev/',
    r'^C:\\Windows',
    r'^C:\\Program Files',
    r'^C:\\ProgramData',
    r'^[A-Z]:\\Windows',
    # 系统文件
    r'/etc/passwd$',
    r'/etc/shadow$',
    r'/etc/hosts$',
    r'boot\.ini$',
    r'ntldr$',
    r'\\System32\\',
    # 版本控制（只读允许，写入阻断在路径护栏处理）
    r'(^|[/\\])\.git/',
    r'(^|[/\\])\.svn/',
    # patchflow 自身状态
    r'(^|[/\\])\.patchflow/config\.json$',
    r'(^|[/\\])\.patchflow/safe\.json$',
]

# 危险文件扩展名 — 防止写入可执行文件
_BLOCKED_EXTENSIONS = {".exe", ".dll", ".so", ".dylib", ".bin", ".sh", ".bat", ".cmd", ".ps1"}

# 最大文件大小（读取/写入）
MAX_FILE_SIZE = 5 * 1024 * 1024  # 5 MB


class SandboxViolation(Exception):
    """沙箱违规异常"""

    def __init__(self, reason: str, path: str = ""):
        self.reason = reason
        self.path = path
        super().__init__(f"Sandbox: {reason}" + (f" ({path})" if path else ""))


class PathGuard:
    """文件路径护栏 — 验证所有文件操作在安全边界内"""

    def __init__(self, project_root: str):
        self.root = Path(project_root).resolve()
        self._sensitive_re = [re.compile(p, re.IGNORECASE) for p in _SENSITIVE_PATTERNS]

    def resolve(self, filepath: str) -> Path:
        """安全解析路径，失败则抛出 SandboxViolation"""
        raw = Path(filepath)

        # 1. 拒绝绝对路径（强制相对于项目根）
        if raw.is_absolute():
            raise SandboxViolation("绝对路径不被允许", filepath)

        # 2. 检测路径遍历攻击
        parts = raw.parts
        traversal_depth = 0
        for part in parts:
            if part == "..":
                traversal_depth += 1
            elif part != ".":
                break
        # 允许 ../ 但最终必须在项目根内
        try:
            resolved = (self.root / raw).resolve()
        except (OSError, ValueError) as e:
            raise SandboxViolation(f"路径解析失败: {e}", filepath)

        # 3. 必须在项目根内
        try:
            resolved.relative_to(self.root)
        except ValueError:
            raise SandboxViolation("路径越界：目标不在项目根内", filepath)

        return resolved

    def check_sensitive(self, filepath: str) -> str | None:
        """检查路径是否命中敏感模式，返回原因或 None"""
        normalized = filepath.replace("\\", "/")
        for i, pattern in enumerate(self._sensitive_re):
            if pattern.search(normalized):
                # 提取模式名
                return f"命中敏感路径模式: {_SENSITIVE_PATTERNS[i]}"
        return None

    def check_extension(self, filepath: str) -> str | None:
        """检查扩展名是否被禁止写入"""
        ext = Path(filepath).suffix.lower()
        if ext in _BLOCKED_EXTENSIONS:
            return f"禁止写入 {ext} 文件（可执行/二进制）"
        return None

    def validate_read(self, filepath: str) -> Path:
        """验证读取操作：路径解析 + 越界检查（敏感路径读取仅警告）"""
        resolved = self.resolve(filepath)
        sensitive = self.check_sensitive(filepath)
        if sensitive:
            logger.warn(f"[Sandbox] 读取敏感路径: {sensitive}")
        return resolved

    def validate_write(self, filepath: str, content_size: int = 0) -> Path:
        """验证写入操作：路径解析 + 越界 + 敏感 + 大小检查"""
        resolved = self.resolve(filepath)

        # 敏感路径禁止写入
        sensitive = self.check_sensitive(filepath)
        if sensitive:
            raise SandboxViolation(sensitive, filepath)

        # 危险扩展名禁止写入
        ext_block = self.check_extension(filepath)
        if ext_block:
            raise SandboxViolation(ext_block, filepath)

        # 文件大小限制
        if content_size > MAX_FILE_SIZE:
            raise SandboxViolation(
                f"文件过大 ({content_size} > {MAX_FILE_SIZE} bytes)", filepath
            )

        return resolved


class CommandGuard:
    """命令安全护栏 — 集成 runner.py 的危险命令分级"""

    @staticmethod
    def validate(command: str) -> tuple[bool, str]:
        """验证命令安全性

        Returns:
            (allowed: bool, reason: str)
        """
        from patchflow.utils.runner import classify_command

        action, reason = classify_command(command)
        if action == "block":
            return False, f"命令被拦截: {reason}"
        if action == "confirm":
            return False, f"命令需二次确认: {reason}"
        return True, ""

    @staticmethod
    def is_long_running(command: str) -> bool:
        from patchflow.utils.runner import is_long_running
        return is_long_running(command)


class ResourceLimiter:
    """Agent 资源限制器"""

    def __init__(self, max_file_writes: int = 50, max_file_reads: int = 200,
                 max_commands: int = 30, max_command_seconds: int = 300):
        self.max_file_writes = max_file_writes
        self.max_file_reads = max_file_reads
        self.max_commands = max_commands
        self.max_command_seconds = max_command_seconds
        self._writes = 0
        self._reads = 0
        self._commands = 0
        self._command_seconds = 0.0

    def track_read(self) -> str | None:
        self._reads += 1
        if self._reads > self.max_file_reads:
            return f"文件读取超限 ({self.max_file_reads})"
        return None

    def track_write(self) -> str | None:
        self._writes += 1
        if self._writes > self.max_file_writes:
            return f"文件写入超限 ({self.max_file_writes})"
        return None

    def track_command(self, elapsed_seconds: float = 0) -> str | None:
        self._commands += 1
        self._command_seconds += elapsed_seconds
        if self._commands > self.max_commands:
            return f"命令执行次数超限 ({self.max_commands})"
        if self._command_seconds > self.max_command_seconds:
            return f"命令累计耗时超限 ({self._command_seconds:.0f}s > {self.max_command_seconds}s)"
        return None

    def reset(self):
        self._writes = 0
        self._reads = 0
        self._commands = 0
        self._command_seconds = 0.0

    @property
    def stats(self) -> dict:
        return {
            "writes": f"{self._writes}/{self.max_file_writes}",
            "reads": f"{self._reads}/{self.max_file_reads}",
            "commands": f"{self._commands}/{self.max_commands}",
            "cmd_seconds": f"{self._command_seconds:.0f}/{self.max_command_seconds}",
        }


class AgentSandbox:
    """Agent 隔离沙箱

    用法:
        sandbox = AgentSandbox(project_root="/path/to/project")
        guard = sandbox.create_guard(agent_id="fixer_1")

        # 文件操作
        target = guard.validate_write("src/app.py", len(new_content))
        target.write_text(new_content)

        # 命令执行
        ok, reason = guard.validate_command("python main.py")
    """

    def __init__(self, project_root: str = "."):
        self.project_root = str(Path(project_root).resolve())
        self.path_guard = PathGuard(self.project_root)
        self._agents: dict[str, ResourceLimiter] = {}
        self._global_limits = ResourceLimiter()

    def create_context(self, parent_ctx: dict | None = None,
                       overrides: dict | None = None) -> dict:
        """创建隔离的子 Agent 上下文"""
        overrides = overrides or {}
        parent = parent_ctx or {}
        agent_id = overrides.get("agent_id", "unknown")

        # 为每个 Agent 分配独立的资源限制器
        if agent_id not in self._agents:
            self._agents[agent_id] = ResourceLimiter()

        return {
            "file_cache": deepcopy(parent.get("file_cache", {})),
            "my_changes": [],
            "set_global_state": overrides.get("share_state", lambda _: None),
            "task_registry": parent.get("task_registry", {}),
            "abort_signal": parent.get("abort_signal"),
            "agent_id": agent_id,
            "sandbox": self,
        }

    def record_change(self, ctx: dict, filepath: str, content: str):
        """记录 Agent 的变更（用于冲突检测）"""
        if "my_changes" in ctx:
            ctx["my_changes"].append({
                "file": filepath,
                "content": content,
                "agent_id": ctx.get("agent_id", "unknown"),
            })

    # ── 文件系统护栏 ──

    def validate_read(self, filepath: str) -> Path:
        """验证文件读取权限"""
        return self.path_guard.validate_read(filepath)

    def validate_write(self, filepath: str, content_size: int = 0) -> Path:
        """验证文件写入权限"""
        return self.path_guard.validate_write(filepath, content_size)

    # ── 命令安全 ──

    def validate_command(self, command: str) -> tuple[bool, str]:
        """验证命令安全性"""
        return CommandGuard.validate(command)

    # ── 资源追踪 ──

    def track_read(self, agent_id: str) -> str | None:
        limiter = self._agents.get(agent_id, self._global_limits)
        return limiter.track_read()

    def track_write(self, agent_id: str) -> str | None:
        limiter = self._agents.get(agent_id, self._global_limits)
        return limiter.track_write()

    def track_command(self, agent_id: str, elapsed: float = 0) -> str | None:
        limiter = self._agents.get(agent_id, self._global_limits)
        return limiter.track_command(elapsed)

    def agent_stats(self, agent_id: str) -> dict:
        limiter = self._agents.get(agent_id)
        if limiter:
            return limiter.stats
        return {}

    def reset_agent(self, agent_id: str):
        limiter = self._agents.get(agent_id)
        if limiter:
            limiter.reset()


# 全局单例（进程级）
_sandbox: AgentSandbox | None = None


def get_sandbox(project_root: str = ".") -> AgentSandbox:
    global _sandbox
    if _sandbox is None:
        _sandbox = AgentSandbox(project_root)
    return _sandbox


def reset_sandbox():
    global _sandbox
    _sandbox = None
