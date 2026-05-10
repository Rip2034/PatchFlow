"""Token 预算追踪器 — 按修复会话统计 LLM 消耗

在修复循环中追踪累计 token 用量，防止单次修复任务
因循环重试导致费用失控。

使用方式：
    budget = TokenBudget(limit=100000)
    budget.track_call("analyzer", input_tokens=2000, output_tokens=500)
    if budget.is_exhausted:
        raise BudgetExceeded(...)
"""

from patchflow.core.config import get_config
from patchflow.utils import logger


class BudgetExceeded(Exception):
    """Token 预算耗尽"""
    def __init__(self, used: int, limit: int, stage: str = ""):
        self.used = used
        self.limit = limit
        self.stage = stage
        super().__init__(
            f"Token 预算耗尽: {used}/{limit}"
            + (f" (阶段: {stage})" if stage else "")
        )


class TokenBudget:
    """按修复会话的 Token 预算追踪器"""

    def __init__(self, limit: int | None = None):
        cfg = get_config()
        self.limit = limit or cfg.get("token_budget", 80000)
        self.used_input = 0
        self.used_output = 0
        self.calls: list[dict] = []

    @property
    def total_used(self) -> int:
        return self.used_input + self.used_output

    @property
    def remaining(self) -> int:
        return max(0, self.limit - self.total_used)

    @property
    def is_exhausted(self) -> bool:
        return self.total_used >= self.limit

    @property
    def usage_ratio(self) -> float:
        return self.total_used / max(self.limit, 1)

    @property
    def is_warning(self) -> bool:
        """超过 80% 预算时返回 True"""
        return self.usage_ratio >= 0.8

    def track_call(self, agent: str, input_tokens: int = 0,
                   output_tokens: int = 0, model: str = ""):
        """记录一次 LLM 调用的 token 消耗"""
        self.used_input += input_tokens
        self.used_output += output_tokens
        self.calls.append({
            "agent": agent,
            "model": model,
            "input": input_tokens,
            "output": output_tokens,
        })

        if self.is_warning and self.usage_ratio < 0.95:
            logger.warn(
                f"[TokenBudget] {self.total_used}/{self.limit} tokens "
                f"({self.usage_ratio:.0%}) — 接近预算上限"
            )

    def check(self, estimated_tokens: int = 0) -> str | None:
        """检查是否有足够预算用于下一次调用

        Returns:
            None 如果预算充足，否则返回错误描述
        """
        if self.is_exhausted:
            return f"Token 预算已耗尽 ({self.total_used}/{self.limit})"
        if self.total_used + estimated_tokens > self.limit:
            return (
                f"Token 预算不足: 需要 ~{estimated_tokens} tokens，"
                f"剩余 {self.remaining}"
            )
        return None

    def track_from_llm_response(self, agent: str, response: dict,
                                 model: str = ""):
        """从 LLM API 响应中提取 usage 并追踪"""
        usage = response.get("usage", {}) if isinstance(response, dict) else {}
        input_tokens = usage.get("input_tokens") or usage.get("prompt_tokens", 0)
        output_tokens = usage.get("output_tokens") or usage.get("completion_tokens", 0)
        if input_tokens or output_tokens:
            self.track_call(agent, input_tokens, output_tokens, model)

    def estimate_tokens(self, text: str) -> int:
        """粗略估算文本的 token 数（~4 chars/token）"""
        if not text:
            return 0
        return max(1, len(text) // 4)

    def reset(self):
        self.used_input = 0
        self.used_output = 0
        self.calls.clear()

    def summary(self) -> str:
        """简短摘要"""
        return (
            f"TokenBudget: {self.total_used}/{self.limit} "
            f"({self.usage_ratio:.0%}) | {len(self.calls)} 次调用"
        )

    def detailed_summary(self) -> str:
        lines = [self.summary()]
        for i, call in enumerate(self.calls, 1):
            lines.append(
                f"  #{i} [{call['agent']}] "
                f"in={call['input']} out={call['output']}"
                + (f" ({call['model']})" if call.get("model") else "")
            )
        return "\n".join(lines)


# ═══════════════════════════════════════════════════════════
# 会话级全局预算（call_llm 自动检查）
# ═══════════════════════════════════════════════════════════

_session_budget: TokenBudget | None = None


def start_session_budget(limit: int | None = None) -> TokenBudget:
    """开始一个新的修复会话预算追踪"""
    global _session_budget
    _session_budget = TokenBudget(limit=limit)
    return _session_budget


def get_session_budget() -> TokenBudget | None:
    return _session_budget


def end_session_budget() -> TokenBudget | None:
    global _session_budget
    old = _session_budget
    _session_budget = None
    return old
