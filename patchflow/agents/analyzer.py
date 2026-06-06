"""Analyzer Agent — 问题定位（V0.5 语义增强版）

职责：只说问题在哪，不提修复方案。
职责单一防止思维污染 —— 不知道谁修、怎么修。

V0.5 增强：
  - CodeGraph 语义上下文注入：调用链、符号信息、类型定义
  - 更精确的根因定位：从"哪行出错"到"哪个函数、哪个变量、什么条件"
  - 影响范围更准确：不只是文件列表，还有具体的函数/类

输出格式统一用 schema.py 定义的标准合约。
"""

from patchflow.agents.schema import ANALYZER_PROMPT, ANALYZER_PROMPT_ENHANCED, validate_analysis
from patchflow.core.llm_client import call_llm
from patchflow.utils import logger


def _build_semantic_analysis_context(blackboard) -> str:
    """从 CodeGraph 构建语义分析上下文（委托给共享模块）"""
    code_graph = getattr(blackboard, "code_graph", None)
    if code_graph is None:
        return ""
    error_text = blackboard.get("error", "")
    from patchflow.core.fix.semantic_context import build_context_from_error
    return build_context_from_error(code_graph, error_text)


def agent_analyze(blackboard, model: str | None = None, model_alias: str | None = None) -> dict:
    """Analyzer Agent：分析错误，定位根因

    Args:
        blackboard: Blackboard 实例（包含 error, context, code，可选 code_graph）
        model: LLM 模型
        model_alias: 模型别名（如 "deepseek"、"claude"），指定后覆盖认证配置

    Returns:
        dict: 分析结果（已通过 set_analysis 写入 blackboard）
    """
    blackboard.set_current_agent("analyzer")
    logger.step("[Agent Analyzer] 正在分析错误...")

    context = blackboard.get("context", {})
    context_str = ""
    if isinstance(context, dict):
        ctx_parts = []
        for k, v in context.items():
            if isinstance(v, dict):
                ctx_parts.append(f"{k}: {', '.join(str(x) for x in v.values() if isinstance(x, str))}")
            elif isinstance(v, list):
                ctx_parts.append(f"{k}: {', '.join(str(x) for x in v[:5])}")
            else:
                ctx_parts.append(f"{k}: {v}")
        context_str = "\n".join(ctx_parts[:10])

    code_context = blackboard.get_callchain_code()[:3000]

    # ── V0.5 增强：CodeGraph 语义上下文 ──
    semantic_context = ""
    try:
        semantic_context = _build_semantic_analysis_context(blackboard)
        if semantic_context:
            logger.info(f"[Agent Analyzer] 语义上下文注入 ({len(semantic_context)} 字符)")
    except Exception as e:
        logger.debug(f"[Agent Analyzer] 语义上下文构建跳过: {e}")

    system_prompt = ANALYZER_PROMPT_ENHANCED if semantic_context else ANALYZER_PROMPT

    user_message = f"""Error Output:
{blackboard["error"][:2000]}

Project Context:
{context_str or "(not available)"}

Relevant Code:
{code_context or "(not available)"}

{semantic_context}

Analyze the error. Identify the programming language from the error and code above.
Output ONLY the JSON."""

    result = call_llm(
        system_prompt=system_prompt,
        user_message=user_message,
        model=model,
        model_alias=model_alias,
        shared_system_prefix=blackboard.get("shared_system_prefix"),
    )

    if result is None:
        logger.error("[Agent Analyzer] LLM 调用失败，使用回退分析")
        result = _fallback_analysis(blackboard["error"])

    validated = validate_analysis(result)
    blackboard.set_analysis(validated)
    logger.info(f"[Agent Analyzer] {validated.get('error_type','?')} | {validated.get('summary','')[:60]}")
    return validated


def _fallback_analysis(error_text: str) -> dict:
    """LLM 调用失败时返回空分析，不编造默认值"""
    return {
        "error_type": "unknown",
        "root_cause": "",
        "impact_files": [],
        "confidence": 0.0,
        "summary": "LLM analysis failed, no fallback available",
        "language": "",
    }
