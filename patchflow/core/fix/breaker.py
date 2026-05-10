"""FixLoopBreaker — 修复循环熔断器

从 Orchestrator 提取的独立熔断器，负责判断何时应该停止重试。
借鉴 Claude Code 的 Denial 熔断器机制。

熔断规则：
  1. 达到最大重试次数 → 熔断
  2. 同样的错误（类型+根因相同）连续出现 N 次 → 熔断
  3. 同一策略连续失败 M 次 → 自动升级策略
"""



class FixLoopBreaker:
    """修复循环熔断器"""

    MAX_RETRIES = 3
    SIMILAR_FAILURES_THRESHOLD = 2
    MAX_FAILURES_PER_STRATEGY = 1

    def __init__(self, max_retries: int = 3):
        self.max_retries = max_retries
        self.turn = 0
        self.error_history: list[dict] = []
        self.strategy_failures: dict[str, int] = {}

    def should_retry(self, error_type: str, root_cause: str,
                     strategy_name: str | None = None) -> tuple[bool, str]:
        """判断是否应该继续重试

        Args:
            error_type: 错误类型（syntax/runtime/type/...）
            root_cause: 错误根因描述
            strategy_name: 当前使用的策略名（可选）

        Returns:
            (should_retry: bool, reason: str)
            should_retry=False 时，reason 说明原因
        """
        self.turn += 1

        if self.turn > self.max_retries:
            return False, f"max_retries_exceeded ({self.max_retries})"

        error_key = f"{error_type}:{root_cause[:80]}"
        same_count = sum(1 for e in self.error_history if e.get("key") == error_key)
        if same_count >= self.SIMILAR_FAILURES_THRESHOLD:
            return False, f"same_error_repeated ({same_count + 1} times)"

        if strategy_name:
            self.strategy_failures[strategy_name] = self.strategy_failures.get(strategy_name, 0) + 1
            if self.strategy_failures[strategy_name] > self.MAX_FAILURES_PER_STRATEGY:
                return False, f"strategy_failed_too_often ({strategy_name})"

        return True, ""

    def record_failure(self, error_type: str, root_cause: str):
        """记录一次失败"""
        self.error_history.append({
            "key": f"{error_type}:{root_cause[:80]}",
            "type": error_type,
            "root_cause": root_cause,
            "turn": self.turn,
        })

    def record_strategy_failure(self, strategy_name: str):
        self.strategy_failures[strategy_name] = self.strategy_failures.get(strategy_name, 0) + 1

    def reset(self):
        self.turn = 0
        self.error_history.clear()
        self.strategy_failures.clear()

    @property
    def is_broken(self) -> bool:
        """判断熔断器是否已触发（无副作用，不改变内部状态）"""
        if self.turn > self.max_retries:
            return True
        # 检查是否有同一错误重复出现超过阈值
        from collections import Counter
        key_counts = Counter(e.get("key") for e in self.error_history)
        for count in key_counts.values():
            if count > self.SIMILAR_FAILURES_THRESHOLD:
                return True
        return False
