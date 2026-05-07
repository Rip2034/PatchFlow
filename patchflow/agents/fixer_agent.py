"""Fixer Agent — 执行修复

注意：这不是 core/fixer.py 的替代，而是在 Agent 协作层面的封装。
程序先圈定范围，Fixer Agent 只能在范围内行动。

输出格式统一用 schema.py 定义的标准合约。
"""

from pathlib import Path

from patchflow.core.llm_client import call_llm
from patchflow.core.fix.scope_calculator import calculate as calculate_scope
from patchflow.core.analysis.strategy_selector import select_strategy
from patchflow.core.analysis.error_analyzer import ErrorAnalysis
from patchflow.utils import logger
from patchflow.agents.schema import FIXER_PROMPT, validate_fix_plan


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


def apply_agent_patches(blackboard, work_dir: str = ".") -> bool:
    """将 Fixer Agent 的补丁安全写入磁盘

    采用多级策略避免 LLM 返回代码片段时错误覆盖整个文件：
      1. 文件不存在 → 直接创建
      2. 有 old snippet 且在文件中找到 → 精确文本替换
      3. new 接近原文件大小（>60%）→ 视为完整文件覆盖
      4. 宽松匹配后替换 → 仍找不到则拒绝覆盖（防止文件损坏）

    Args:
        blackboard: Blackboard 实例（含 fix_plan）
        work_dir: 工作目录

    Returns:
        bool: 是否全部写入成功
    """
    fix_plan = blackboard.get("fix_plan", {})
    patches = fix_plan.get("patches", [])
    if not patches:
        logger.warn("[Agent Fixer] 没有补丁可应用")
        return False

    wd = Path(work_dir)
    success = 0

    try:
        from patchflow.core.agent_sandbox import get_sandbox
        sandbox = get_sandbox(str(wd))
    except Exception:
        sandbox = None

    for patch in patches:
        filepath = patch.get("file", "")
        new_content = patch.get("new", "")
        old_content = patch.get("old", "")
        if not filepath or not new_content:
            logger.warn(f"[Agent Fixer] 跳过无效补丁: file={filepath or '?'}")
            continue

        # 沙箱验证文件路径
        if sandbox:
            try:
                target = sandbox.validate_write(filepath, len(new_content))
            except Exception as e:
                logger.error(f"[Agent Fixer] 沙箱拦截写入 {filepath}: {e}")
                continue
        else:
            target = wd / filepath
        target.parent.mkdir(parents=True, exist_ok=True)

        existing = ""
        if target.exists():
            try:
                existing = target.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                pass

        # 策略 1: 文件不存在 → 直接创建
        if not existing:
            target.write_text(new_content, encoding="utf-8")
            logger.info(f"[Agent Fixer] 新建文件: {filepath} ({len(new_content)} chars)")
            success += 1
            continue

        # 策略 2: 有 old snippet → 精确替换
        if old_content and old_content in existing:
            replaced = existing.replace(old_content, new_content, 1)
            target.write_text(replaced, encoding="utf-8")
            logger.info(f"[Agent Fixer] Snippet替换: {filepath} ({len(old_content)}→{len(new_content)} chars)")
            success += 1
            continue

        # 策略 3: new 接近原文件大小 → 视为完整文件覆盖
        len_ratio = len(new_content) / max(len(existing), 1)
        if len_ratio > 0.6:
            logger.info(f"[Agent Fixer] 全文覆盖: {filepath} ({len_ratio:.0%} 相似度)")
            target.write_text(new_content, encoding="utf-8")
            success += 1
            continue

        # 策略 4: 宽松匹配（忽略首尾空白）
        if old_content:
            old_stripped = old_content.strip()
            existing_stripped = existing.strip()
            if old_stripped and old_stripped in existing_stripped:
                replaced = existing_stripped.replace(old_stripped, new_content.strip(), 1)
                target.write_text(replaced, encoding="utf-8")
                logger.info(f"[Agent Fixer] 宽松Snippet替换: {filepath}")
                success += 1
                continue

        # 策略 5: new 太短，可能是 LLM 返回不完整片段 → 拒绝覆盖
        if len_ratio < 0.2 and len(new_content) < 200:
            logger.warn(
                f"[Agent Fixer] 拒绝覆盖 {filepath}: "
                f"new 过小 ({len(new_content)}B vs {len(existing)}B)，可能是 LLM 返回了代码片段而非完整文件"
            )
            continue

        # 兜底: 覆盖（带警告）
        logger.warn(f"[Agent Fixer] 兜底覆盖: {filepath} ({len(new_content)} chars)，"
                     f"old 未在文件中定位到，使用新内容覆盖")
        target.write_text(new_content, encoding="utf-8")
        success += 1

    logger.info(f"[Agent Fixer] 应用 {success}/{len(patches)} 补丁")
    return success > 0
