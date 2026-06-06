"""Reviewer Agent — 多维度审查修复方案（V0.5 增强版）

独立的审查视角，不是自己审自己。
只审查不修复 —— 批改作业但不能替写。

V0.5 增强：
  - 4 维度独立评分：correctness / security / performance / edge_cases
  - 任一维度 < 5 自动拒绝
  - 维度评分校准总分

输出格式统一用 schema.py 定义的标准合约。
"""

from patchflow.agents.schema import REVIEWER_PROMPT, REVIEWER_PROMPT_ENHANCED, validate_review
from patchflow.core.llm_client import call_llm
from patchflow.utils import logger


def agent_review(blackboard, model: str | None = None, model_alias: str | None = None) -> dict:
    """Reviewer Agent：多维度审查修复方案

    Args:
        blackboard: Blackboard 实例（包含 error, analysis, fix_plan）
        model: LLM 模型
        model_alias: 模型别名（如 "deepseek"、"claude"），指定后覆盖认证配置

    Returns:
        dict: 审查结果（含 dimensions 维度评分，已通过 set_review 写入 blackboard）
    """
    blackboard.set_current_agent("reviewer")
    logger.step("[Agent Reviewer] 正在多维度审查修复方案...")

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
Impact Symbols: {', '.join(analysis.get("impact_symbols", []))}

Fix Plan:
{_format_fix_plan(fix_plan)}

Previous Review Feedback (cumulative):
{review_feedback or "N/A (first review)"}

Review this fix from FOUR dimensions:
1. Correctness — does it fix the root cause?
2. Security & Robustness — any new risks?
3. Performance — any inefficiencies?
4. Edge Cases — boundary conditions handled?

Score each dimension 1-10. Output ONLY the JSON."""

    result = call_llm(
        system_prompt=REVIEWER_PROMPT_ENHANCED,
        user_message=user_message,
        model=model,
        model_alias=model_alias,
        shared_system_prefix=blackboard.get("shared_system_prefix"),
    )

    if result is None:
        logger.error("[Agent Reviewer] LLM 调用失败，拒绝通过（安全熔断）")
        result = {
            "approved": False, "score": 0,
            "dimensions": {
                "correctness": {"score": 0, "note": "LLM unavailable"},
                "security": {"score": 0, "note": "LLM unavailable"},
                "performance": {"score": 0, "note": "LLM unavailable"},
                "edge_cases": {"score": 0, "note": "LLM unavailable"},
            },
            "issues": ["LLM call failed — unable to review"],
            "summary": "LLM call failed, review cannot proceed (fail-closed)",
            "feedback": "LLM 不可用，无法审查修复方案",
        }

    validated = validate_review(result)
    blackboard.set_review(validated)
    status = "approved" if validated.get("approved") else "rejected"
    dims = validated.get("dimensions", {})
    dim_summary = ""
    if dims:
        dim_parts = []
        for dname in ("correctness", "security", "performance", "edge_cases"):
            d = dims.get(dname, {})
            if isinstance(d, dict):
                dim_parts.append(f"{dname[:4]}={d.get('score','?')}")
        dim_summary = " | " + " ".join(dim_parts)
    logger.info(f"[Agent Reviewer] {status} ({validated.get('score',0)}/10){dim_summary} | {validated.get('summary','')[:60]}")
    return validated


def _format_fix_plan(fix_plan: dict) -> str:
    if not fix_plan:
        return "(no fix plan)"
    patches = fix_plan.get("patches", [])
    if not patches:
        return "(no patches)"
    parts = [f"Summary: {fix_plan.get('summary', '')}"]
    for p in patches:
        old_preview = (p.get("old", "") or "")[:60].replace("\n", "\\n")
        new_preview = (p.get("new", "") or "")[:60].replace("\n", "\\n")
        parts.append(f"  File: {p.get('file','?')} | {p.get('reason','')[:80]}")
        parts.append(f"    old: {old_preview}")
        parts.append(f"    new: {new_preview}")
    return "\n".join(parts)
