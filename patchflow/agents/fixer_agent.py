"""Fixer Agent — 执行修复

注意：这不是 core/fixer.py 的替代，而是在 Agent 协作层面的封装。
程序先圈定范围，Fixer Agent 只能在范围内行动。

输出格式统一用 schema.py 定义的标准合约。
"""

from patchflow.agents.schema import FIXER_PROMPT, validate_fix_plan
from patchflow.core.analysis.error_analyzer import ErrorAnalysis
from patchflow.core.analysis.strategy_selector import select_strategy
from patchflow.core.fix.scope_calculator import calculate as calculate_scope
from patchflow.core.llm_client import call_llm
from patchflow.utils import logger


def agent_fix(blackboard, dep_graph=None, model: str | None = None, model_alias: str | None = None) -> dict:
    """Fixer Agent：根据分析和策略执行修复

    程序先通过 Scope Calculator + Strategy Selector 确定范围，
    Fixer Agent 只能在范围内行动。

    Args:
        blackboard: Blackboard 实例
        dep_graph: 可选的依赖图（用于 Scope Calculator）
        model: LLM 模型
        model_alias: 模型别名（如 "deepseek"、"claude"），指定后覆盖认证配置

    Returns:
        dict: 修复方案（已通过 set_fix_plan 写入 blackboard）
    """
    blackboard.set_current_agent("fixer")
    logger.step("[Agent Fixer] 正在执行修复...")

    analysis = blackboard.get("analysis", {})
    context = blackboard.get("context", {})

    strategy = select_strategy(
        analysis.get("error_type", "runtime"),
        impact_file_count=len(analysis.get("impact_files", [])),
    )
    logger.info(f"[Agent Fixer] 策略: {strategy['scope']} 范围 | 改写: {strategy['rewrite']}")

    if dep_graph:
        try:
            scope_analysis = ErrorAnalysis(
                type=analysis.get("error_type", "runtime"),
                root_cause=analysis.get("root_cause", ""),
                call_chain=analysis.get("call_chain", []),
                impact_files=analysis.get("impact_files", []),
            )
            scope_result = calculate_scope(scope_analysis, dep_graph=dep_graph)
            allowed_files = scope_result.files
        except Exception as e:
            logger.warn(f"[Agent Fixer] Scope 计算失败: {e}，使用 impact_files 回退")
            allowed_files = analysis.get("impact_files", [])
    else:
        allowed_files = analysis.get("impact_files", []) or ["app.py"]

    code_context = blackboard.get_code(allowed_files)

    review_feedback = blackboard.get("review_feedback", "")

    context_str = ""
    if isinstance(context, dict):
        ctx_parts = []
        for k, v in context.items():
            if isinstance(v, dict):
                items = []
                for vk, vv in v.items():
                    if isinstance(vv, (str, int, float)):
                        items.append(f"{vk}={vv}")
                ctx_parts.append(f"{k}: {', '.join(items)}")
            elif isinstance(v, list):
                ctx_parts.append(f"{k}: {', '.join(str(x) for x in v[:5])}")
            elif isinstance(v, str):
                ctx_parts.append(f"{k}: {v[:100]}")
        context_str = "\n".join(ctx_parts[:8])

    user_message = f"""Project Context:
{context_str or "(not available)"}

Error Analysis:
Type: {analysis.get("error_type","?")}
Root Cause: {analysis.get("root_cause","?")}
Summary: {analysis.get("summary","")}
Language: {analysis.get("language", "")}

Impact Files: {", ".join(allowed_files)}

Fix Constraints:
Scope: {strategy['scope']}
Max Files: {strategy['files']}
Rewrite Allowed: {strategy['rewrite']}

Review Feedback from previous round:
{review_feedback or "N/A (first attempt)"}

FILES YOU CAN MODIFY:
{code_context or "(no files available)"}

Fix the code. Output ONLY the JSON with the patches."""

    result = call_llm(
        system_prompt=FIXER_PROMPT,
        user_message=user_message,
        model=model,
        model_alias=model_alias,
    )

    if result is None:
        logger.error("[Agent Fixer] LLM 调用失败")
        result = {"patches": []}

    validated = validate_fix_plan(result)
    blackboard.set_fix_plan(validated)
    logger.info(f"[Agent Fixer] {validated['summary']}")
    return validated


def apply_agent_patches(blackboard, work_dir: str = ".", diff_tracker=None) -> bool:
    fix_plan = blackboard.get("fix_plan", {})
    patches = fix_plan.get("patches", [])
    if not patches:
        logger.warn("[Agent Fixer] 没有补丁可应用")
        return False

    from patchflow.core.fix.patch_applicator import PatchApplicator, SnippetPatch
    snippet_patches = [SnippetPatch(
        file=p.get("file", ""),
        old=p.get("old", ""),
        new=p.get("new", ""),
        reason=p.get("reason", ""),
    ) for p in patches]

    success, fail = PatchApplicator.apply_all(snippet_patches, work_dir=work_dir, diff_tracker=diff_tracker)
    if diff_tracker and success > 0:
        logger.info(f"[Agent Fixer] DiffTracker 已记录: {diff_tracker.recent_changes_summary}")
    return success > 0
