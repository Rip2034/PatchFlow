"""修复策略选择器 — 硬约束范围

不同错误类型采用不同的修复策略。这里的"策略"不是给 LLM 的"建议"，
而是程序级的硬约束：LLM 只能看到策略允许的文件，物理上无法修改其他文件。

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


def select_strategy(analysis_type: str, impact_file_count: int = 0) -> dict:
    """根据错误类型选择修复策略

    Args:
        analysis_type: 错误分析类型（syntax / type / runtime / logic / ...）
        impact_file_count: 受影响文件数（用于运行时类型的动态策略）

    Returns:
        dict: {
            "scope": "line" | "chain" | "callchain" | "business",
            "files": 最大文件数（-1 表示不限制）,
            "rewrite": 是否允许重写,
            "description": 策略描述,
        }
    """
    strategies = {
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

    result = dict(strategies.get(analysis_type, strategies["runtime"]))
    if analysis_type == "runtime":
        result["files"] = max(impact_file_count, 1)
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
    sequences = {
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
    return sequences.get(analysis_type, ["callchain", "business"])
