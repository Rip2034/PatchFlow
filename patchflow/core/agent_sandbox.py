"""Agent 隔离沙箱

这是多 Agent 协作的安全机制。借鉴 Claude Code 的 createSubagentContext()：

核心原则：
  1. 默认全隔离 — 每个 Agent 拥有独立的上下文副本
  2. 显式 opt-in 共享 — Agent 必须明确声明要共享什么
  3. 变更追踪 — 记录每个 Agent 的修改，用于冲突检测

隔离策略：
  - 文件缓存：隔离（深拷贝父级副本）
  - 写入回调：默认 no-op（静默丢弃，不影响文件系统）
  - 基础设施：必须穿透（task_registry, abort_signal 等全局信号）
"""

from copy import deepcopy


class AgentSandbox:
    """Agent 隔离沙箱"""

    def create_context(self, parent_ctx: dict | None = None,
                       overrides: dict | None = None) -> dict:
        """创建隔离的子 Agent 上下文

        Args:
            parent_ctx: 父级上下文（可选）
            overrides: 覆盖配置（可选，如 share_state）

        Returns:
            dict: 隔离的子上下文
        """
        overrides = overrides or {}
        parent = parent_ctx or {}

        return {
            "file_cache": deepcopy(parent.get("file_cache", {})),
            "my_changes": [],
            "set_global_state": overrides.get(
                "share_state", lambda _: None
            ),
            "task_registry": parent.get("task_registry", {}),
            "abort_signal": parent.get("abort_signal"),
            "agent_id": overrides.get("agent_id", "unknown"),
        }

    def record_change(self, ctx: dict, filepath: str, content: str):
        """记录 Agent 的变更（用于冲突检测）"""
        if "my_changes" in ctx:
            ctx["my_changes"].append({
                "file": filepath,
                "content": content,
                "agent_id": ctx.get("agent_id", "unknown"),
            })
