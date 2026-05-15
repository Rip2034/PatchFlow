"""Reviewer Agent — 审查修复方案

独立的审查视角，不是自己审自己。
只审查不修复 —— 批改作业但不能替写。

输出格式统一用 schema.py 定义的标准合约。
"""

from patchflow.agents.schema import REVIEWER_PROMPT, validate_review
from patchflow.core.llm_client import call_llm
from patchflow.utils import logger


def agent_review(blackboard, model: str | None = None, model_alias: str | None = None) -> dict:
    """Reviewer Agent：审查修复方案

    Args:
        blackboard: Blackboard 实例（包含 error, analysis, fix_plan）
        model: LLM 模型
        model_alias: 模型别名（如 "deepseek"、"claude"），指定后覆盖认证配置

    Returns:
        dict: 审查结果（已通过 set_review 写入 blackboard）
    """
    blackboard.set_current_agent("reviewer")
    logger.step("[Agent Reviewer] 正在审查修复方案...")

    analysis = blackboard.get("analysis", {})
    fix_plan = blackboard.get("fix_plan", {})
    review_feedback = blackboard.get("review_feedback", "")

    user_message = f"""Original Error:
{blackboard["error"][:500]}

Analysis:
Type: {analysis.get("error_type","?")}
Root Cause: {analysis.get("root_cause","?")}
Summary: {analysis.get("summary","?")}
Language: {analysis.get("language", "")}

Fix Plan:
{_format_fix_plan(fix_plan)}

Review Feedback from previous round:
{review_feedback or "N/A (first review)"}

Review this fix. Output ONLY the JSON."""

    result = call_llm(
        system_prompt=REVIEWER_PROMPT,
        user_message=user_message,
        model=model,
        model_alias=model_alias,
    )

    if result is None:
        logger.error("[Agent Reviewer] LLM 调用失败，拒绝通过（安全熔断）")
        result = {
            "approved": False, "score": 0, "issues": ["LLM call failed — unable to review"],
            "summary": "LLM call failed, review cannot proceed (fail-closed)", "feedback": "LLM 不可用，无法审查修复方案",
        }

    validated = validate_review(result)
    blackboard.set_review(validated)
    status = "approved" if validated.get("approved") else "rejected"
    logger.info(f"[Agent Reviewer] {status} ({validated.get('score',0)}/10) | {validated.get('summary','')[:60]}")
    return validated


def _format_fix_plan(fix_plan: dict) -> str:
    if not fix_plan:
        return "(no fix plan)"
    patches = fix_plan.get("patches", [])
    if not patches:
        return "(no patches)"
    parts = [f"Summary: {fix_plan.get('summary', '')}"]
    for p in patches:
        parts.append(f"  File: {p.get('file','?')} | {p.get('reason','')[:80]}")
    return "\n".join(parts)
