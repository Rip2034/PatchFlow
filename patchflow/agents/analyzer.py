"""Analyzer Agent — 问题定位

职责：只说问题在哪，不提修复方案。
职责单一防止思维污染 —— 不知道谁修、怎么修。

输出格式统一用 schema.py 定义的标准合约。
"""

from patchflow.agents.schema import ANALYZER_PROMPT, validate_analysis
from patchflow.core.llm_client import call_llm
from patchflow.utils import logger


def agent_analyze(blackboard, model: str | None = None, model_alias: str | None = None) -> dict:
    """Analyzer Agent：分析错误，定位根因

    Args:
        blackboard: Blackboard 实例（包含 error, context, code）
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

    user_message = f"""Error Output:
{blackboard["error"][:2000]}

Project Context:
{context_str or "(not available)"}

Relevant Code:
{code_context or "(not available)"}

Analyze the error. Identify the programming language from the error and code above.
Output ONLY the JSON."""

    result = call_llm(
        system_prompt=ANALYZER_PROMPT,
        user_message=user_message,
        model=model,
        model_alias=model_alias,
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
