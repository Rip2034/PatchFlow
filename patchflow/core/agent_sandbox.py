"""Agent 隔离沙箱

借鉴 Claude Code 的 createSubagentContext()：
默认全隔离、显式 opt-in 共享。

核心原则：
  - 文件缓存：隔离（clone 父级）
  - 写入回调：默认 no-op（静默丢弃）
  - 基础设施：必须穿透（task_registry, abort_signal）
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
