"""修复策略选择器 — 硬约束范围（V0.5 自适应增强）

不同错误类型采用不同的修复策略。这里的"策略"不是给 LLM 的"建议"，
而是程序级的硬约束：LLM 只能看到策略允许的文件，物理上无法修改其他文件。

V0.5 增强：
  - 自适应策略选择：根据 MemoryBank 中该错误类型的历史成功率调整策略权重
  - 策略推荐：返回最佳策略 + 备选策略（含历史成功率）
  - 降级防护：如果某策略失败率 > 50%，自动降级到更保守的策略

设计文档中的策略矩阵：
  错误类型     | 修复策略   | 修复范围    | 策略说明
  ------------|-----------|------------|-----------------------------------
  语法错误     | 行级修补   | 单行/单文件  | 最小变更，不改逻辑
  类型错误     | 类型追溯   | 2-3 文件    | 追溯类型定义，补类型注解/转换
  运行时异常   | 调用链修复 | 调用链文件   | 从根因开始，逐层加防护
  名称错误     | 定义补全   | 单文件      | 补变量定义或修正拼写
  逻辑错误     | 业务修正   | 可能广范围  | 先分析业务预期，再修正
  测试失败     | 对比修正   | 业务逻辑文件 | 以测试为锚，反推修正

策略升级机制（strategy_sequence）：
  当一个策略失败时，自动升级到下一个更宽范围的策略。
  升级顺序：line → chain → callchain → business
  这避免了"一开始就大改"的风险。
"""

from patchflow.utils import logger

# ── 基础策略矩阵 ──
_STRATEGIES = {
    "syntax": {
        "scope": "line",
        "files": 1,
        "rewrite": False,
        "description": "行级修补：最小变更，不改逻辑",
    },
    "type": {
        "scope": "chain",
        "files": 3,
        "rewrite": False,
        "description": "类型追溯：补类型注解",
    },
    "runtime": {
        "scope": "callchain",
        "files": 0,
        "rewrite": False,
        "description": "调用链修复：从根因开始，逐层加防护",
    },
    "import": {
        "scope": "callchain",
        "files": 2,
        "rewrite": False,
        "description": "导入修复：补依赖或修正路径",
    },
    "name": {
        "scope": "line",
        "files": 1,
        "rewrite": False,
        "description": "名称修复：补变量定义或修正拼写",
    },
    "attribute": {
        "scope": "callchain",
        "files": 2,
        "rewrite": False,
        "description": "属性修复：检查对象属性",
    },
    "logic": {
        "scope": "business",
        "files": -1,
        "rewrite": True,
        "description": "业务理解后修正：可能较广范围",
    },
    "test_fail": {
        "scope": "logic",
        "files": 2,
        "rewrite": False,
        "description": "以测试为锚，反推修正",
    },
    "key_error": {
        "scope": "callchain",
        "files": 2,
        "rewrite": False,
        "description": "键值错误：检查字典访问",
    },
    "index_error": {
        "scope": "callchain",
        "files": 2,
        "rewrite": False,
        "description": "索引错误：检查列表边界",
    },
    "value_error": {
        "scope": "callchain",
        "files": 2,
        "rewrite": False,
        "description": "值错误：检查输入值合法性",
    },
    "file_error": {
        "scope": "callchain",
        "files": 2,
        "rewrite": False,
        "description": "文件错误：检查路径和权限",
    },
    "assertion": {
        "scope": "business",
        "files": 2,
        "rewrite": False,
        "description": "断言失败：检查业务逻辑",
    },
}

# ── 策略升级序列 ──
_STRATEGY_SEQUENCES = {
    "syntax": ["line", "callchain", "business"],
    "type": ["chain", "callchain", "business"],
    "runtime": ["callchain", "business"],
    "import": ["callchain", "business"],
    "name": ["line", "callchain", "business"],
    "attribute": ["callchain", "business"],
    "key_error": ["callchain", "business"],
    "index_error": ["callchain", "business"],
    "value_error": ["callchain", "business"],
    "file_error": ["callchain", "business"],
    "assertion": ["logic", "business"],
    "test_fail": ["logic", "business"],
}


def _get_strategy_success_rate(memory_bank, error_type: str,
                                strategy_scope: str) -> float | None:
    """从 MemoryBank 查询某策略对该错误类型的历史成功率

    Returns:
        成功率 (0.0–1.0) 或 None（无数据）
    """
    if memory_bank is None:
        return None
    try:
        # 利用 memory bank 中按 error_type 和 strategy_used 统计
        total = 0
        success = 0
        for m in memory_bank._entries:
            if m.error_type == error_type and m.strategy_used:
                # strategy_used 格式如 "line/line" 或 "chain/chain"
                if strategy_scope in m.strategy_used:
                    total += 1
                    if m.success:
                        success += 1
        if total >= 3:  # 至少要有 3 条数据才可信
            return success / total
        return None
    except Exception:
        return None


def select_strategy(analysis_type: str, impact_file_count: int = 0,
                    memory_bank=None) -> dict:
    """根据错误类型选择修复策略（V0.5 自适应增强）

    Args:
        analysis_type: 错误分析类型（syntax / type / runtime / logic / ...）
        impact_file_count: 受影响文件数（用于运行时类型的动态策略）
        memory_bank: FixMemoryBank 实例（可选，用于自适应策略选择）

    Returns:
        dict: {
            "scope": "line" | "chain" | "callchain" | "business",
            "files": 最大文件数（-1 表示不限制）,
            "rewrite": 是否允许重写,
            "description": 策略描述,
            "confidence": 策略的预期成功率（0.0–1.0，V0.5 新增）,
        }
    """
    result = dict(_STRATEGIES.get(analysis_type, _STRATEGIES["runtime"]))
    if analysis_type == "runtime":
        result["files"] = max(impact_file_count, 1)

    # ── V0.5 自适应调整 ──
    result["confidence"] = 0.7  # 默认置信度
    if memory_bank is not None:
        scope = result["scope"]
        success_rate = _get_strategy_success_rate(memory_bank, analysis_type, scope)
        if success_rate is not None:
            result["confidence"] = success_rate
            # 如果首选策略成功率过低（< 0.3），尝试降级到更保守的策略
            if success_rate < 0.3:
                alt_scopes = ["line", "chain", "callchain", "business"]
                current_idx = alt_scopes.index(scope) if scope in alt_scopes else 2
                for alt_scope in alt_scopes[current_idx - 1::-1] if current_idx > 0 else []:
                    alt_rate = _get_strategy_success_rate(memory_bank, analysis_type, alt_scope)
                    if alt_rate is not None and alt_rate > 0.5:
                        logger.info(f"[StrategySelector] 自适应降级: {scope}({success_rate:.0%}) → "
                                    f"{alt_scope}({alt_rate:.0%})")
                        result["scope"] = alt_scope
                        result["confidence"] = alt_rate
                        result["description"] += f" (自适应降级: {scope}→{alt_scope}, {alt_rate:.0%}成功率)"
                        break

    return result


def get_strategy_stats(memory_bank) -> dict:
    """V0.5: 获取所有策略的历史统计（用于调试和监控）"""
    if memory_bank is None:
        return {}
    stats: dict[str, dict] = {}
    for m in memory_bank._entries:
        key = f"{m.error_type}:{m.strategy_used}"
        if key not in stats:
            stats[key] = {"total": 0, "success": 0}
        stats[key]["total"] += 1
        if m.success:
            stats[key]["success"] += 1
    result = {}
    for key, s in stats.items():
        if s["total"] >= 2:
            result[key] = {
                "total": s["total"],
                "success_rate": s["success"] / s["total"],
            }
    return result


def strategy_sequence(analysis_type: str) -> list[str]:
    """获取策略升级序列（从窄到宽）

    当一个策略失败时，自动升级到下一个更宽范围的策略。
    对应的 scope 值从窄到宽：line → chain → callchain → business

    设计文档：策略失败自动升级
    def run_with_fallback(analysis):
        strategies = ["line", "callchain", "business"]
        for scope in strategies:
            ...
    """
    return list(_STRATEGY_SEQUENCES.get(analysis_type, ["callchain", "business"]))
