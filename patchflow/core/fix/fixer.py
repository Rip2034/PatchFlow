"""自动修复器 — 根据错误信息修改代码

当 Validator 发现代码报错时，Fixer 接收错误信息和当前代码，
调用 LLM 生成修复后的代码。

设计要点（借鉴文档"硬约束"设计）：
  1. 错误驱动：只给 LLM 错误信息 + 当前代码，让它专注于修复
  2. 只修文件列表：V0.2 支持 Scope 硬约束，LLM 无法修改不在范围内的文件
  3. 不改整体逻辑：Prompt 要求"只修错误，不改其他"
  4. 输出 JSON：程序需要结构化地知道修复了哪个文件

V0.5 增强：
  - 语义上下文注入：CodeGraph 调用链 + 符号信息 + 类型定义
  - 历史记忆注入：相似修复的成功/失败模式
  - 分层推理：先理解 → 再定位 → 最后修复
"""

from pathlib import Path

from patchflow.core.llm_client import call_llm
from patchflow.utils import logger

FIX_SYSTEM_PROMPT = """You are an expert debugging agent. Your job is to fix the specific error in the code.

THINK IN THREE STEPS before generating the fix:
1. UNDERSTAND: What is the error saying? What is the root cause?
2. LOCATE: Which exact lines/functions are involved? Trace the data flow.
3. FIX: What is the minimal change that resolves the root cause?

RULES:
- Output ONLY valid JSON, no other text
- Make the MINIMAL change needed to fix the error
- Do NOT rewrite the entire file
- Do NOT add new features or "improve" the code
- Keep the same coding style as the original
- Match the project's existing framework, dependencies, and style
- If the error involves a call chain across functions/files, trace it carefully
- Consider edge cases: null/None values, empty collections, boundary conditions

OUTPUT FORMAT:
{
  "file": "app.py or the actual file path",
  "content": "the complete fixed file content",
  "reason": "brief explanation of the fix (≤100 chars)"
}"""

FIX_SYSTEM_PROMPT_ENHANCED = """You are an expert debugging agent with deep code understanding.

CONTEXT YOU WILL RECEIVE:
- Error output with full traceback
- Current code with file paths
- Semantic call chain: which functions call which, from crash site upward
- Related symbols: nearby functions/classes that may be relevant
- Historical fix patterns: what worked and what failed for similar errors
- Scope constraints: which files you are allowed to modify

THINK IN THREE STEPS before generating the fix:
1. UNDERSTAND: Read the error, traceback, and call chain. Identify the exact root cause.
2. LOCATE: Pinpoint the specific lines/functions. Check related symbols for side effects.
3. FIX: Design the minimal change. Verify it doesn't break callers or callees.

CRITICAL RULES:
- Output ONLY valid JSON, no other text
- Make the MINIMAL change — prefer 1-line fixes over rewrites
- Do NOT add features, refactor, or "improve" unrelated code
- Keep the exact same coding style as the original
- NEVER modify files outside the scope list
- Consider: null safety, index bounds, type coercion, import availability
- If a historical fix FAILED for this error pattern, do NOT repeat it

OUTPUT FORMAT:
{
  "file": "relative/path/to/file.py",
  "content": "the complete fixed file content",
  "reason": "what was wrong and how you fixed it (≤100 chars)"
}"""


def _build_semantic_context(code_graph, file_path: str, error_text: str,
                            scope_files: list[str]) -> str:
    """从 CodeGraph 构建语义上下文块（委托给共享模块）

    file_path 参数保留用于向后兼容，实际未使用（委托给共享模块处理）。
    """
    from patchflow.core.fix.semantic_context import build_context_from_files
    return build_context_from_files(code_graph, scope_files, error_text)


def _build_memory_context(memory_bank, error_type: str, root_cause: str) -> str:
    """从 MemoryBank 构建历史修复上下文"""
    if memory_bank is None:
        return ""
    similar = memory_bank.query(error_type, root_cause)
    avoid = memory_bank.get_avoid_patterns(error_type, root_cause)
    if not similar and not avoid:
        return ""
    parts = ["## Historical Fix Memory"]
    if similar:
        parts.append("Similar past fixes (learn from these):")
        for m in similar[:3]:
            status = "✅ SUCCESS" if m.success else "❌ FAILED"
            parts.append(f"  [{status}] strategy={m.strategy_used} | {m.fix_pattern[:100]}")
    if avoid:
        parts.append("\n⚠️  AVOID these approaches (they failed before):")
        for a in avoid:
            parts.append(f"  - DO NOT: {a}")
    return "\n".join(parts)


def fix(error_text: str, file_path: str, model: str | None = None,
        scope: object | None = None,
        project_context: str | None = None,
        code_graph=None,
        memory_bank=None,
        analysis: dict | None = None) -> dict | None:
    """根据错误信息修复指定文件

    Args:
        error_text: 完整的错误输出（stdout + stderr）
        file_path:  需要修复的文件路径
        model:      LLM 模型
        scope:      Scope 对象（硬约束，指定可修改的文件范围）
        project_context: 项目上下文文本（Phase 3）
        code_graph: CodeGraph 语义图谱（Phase 5 增强）
        memory_bank: FixMemoryBank 跨会话记忆（Phase 5 增强）
        analysis:   错误分析结果 dict（含 error_type, root_cause 等）

    Returns:
        dict 或 None:
            成功 → {"file": "<file_path>", "content": "fixed code", "reason": "..."}
            失败 → None
    """
    logger.step(f"Fixer: 正在修复 {file_path}...")

    p = Path(file_path)
    if not p.exists():
        logger.error(f"Fixer: 文件不存在: {file_path}")
        return None

    current_code = p.read_text(encoding="utf-8", errors="replace")

    context_block = ""
    if project_context:
        context_block = f"{project_context}\n"

    scope_note = ""
    scope_files = []
    if scope is not None:
        scope_files = scope.files
        scope_note = (
            f"\nFIX SCOPE (hard constraint):\n"
            f"Strategy: {scope.strategy}\n"
            f"You may ONLY modify these files: {', '.join(scope_files)}\n"
            f"Do NOT modify any file outside this list.\n"
        )

    # ── V0.5 增强：语义上下文 ──
    semantic_block = ""
    if code_graph is not None and scope_files:
        try:
            semantic_block = _build_semantic_context(
                code_graph, file_path, error_text, scope_files
            )
            if semantic_block:
                logger.info(f"Fixer: 语义上下文注入 ({len(semantic_block)} 字符)")
        except Exception as e:
            logger.debug(f"Fixer: 语义上下文构建跳过: {e}")

    # ── V0.5 增强：历史记忆 ──
    memory_block = ""
    if memory_bank is not None and analysis:
        try:
            memory_block = _build_memory_context(
                memory_bank,
                analysis.get("error_type", ""),
                analysis.get("root_cause", ""),
            )
            if memory_block:
                logger.info(f"Fixer: 历史记忆注入 ({len(memory_block)} 字符)")
        except Exception as e:
            logger.debug(f"Fixer: 历史记忆构建跳过: {e}")

    ext = p.suffix.lower()
    lang_tag = ext.lstrip(".") or "text"

    # 选择 prompt：有语义上下文时用增强版
    system_prompt = FIX_SYSTEM_PROMPT_ENHANCED if semantic_block else FIX_SYSTEM_PROMPT

    user_message = f"""{context_block}{semantic_block}{memory_block}
Error:
{error_text}

Current code in {file_path}:
```{lang_tag}
{current_code}
```
{scope_note}
Fix the error. Output ONLY the JSON with the fixed file content."""

    result = call_llm(
        system_prompt=system_prompt,
        user_message=user_message,
        model=model,
    )

    if result is None:
        logger.error("Fixer: LLM 调用失败")
        return None

    if "file" not in result or "content" not in result:
        logger.error("Fixer: LLM 返回格式不正确")
        return None

    fixed_file = result["file"]

    if scope_files:
        # V0.5: 路径归一化后再比较
        # 使用 re.sub 去除 ./ 前缀（避免 lstrip 把 .patchflow 变成 patchflow）
        import re as _re
        def _norm(p: str) -> str:
            p = p.replace("\\", "/")
            return _re.sub(r'^(\./)+', '', p)
        normalized_fixed = _norm(fixed_file)
        normalized_scope = [_norm(f) for f in scope_files]
        if normalized_fixed not in normalized_scope:
            logger.error(f"Fixer: LLM 尝试修改不在范围内的文件: {fixed_file}")
            logger.error(f"  允许范围: {scope_files}")
            logger.error("  拒绝修复，返回 None 触发策略升级")
            return None

    logger.success(f"Fixer: 生成修复方案 for {result['file']}")
    return result


FIX_SYSTEM_PROMPT_MULTI = """You are a debugging agent. Fix the specific error in the code.

THINK IN THREE STEPS:
1. UNDERSTAND: What is the error? Trace through the call chain.
2. LOCATE: Which files and functions are involved?
3. FIX: What minimal changes resolve the root cause?

RULES:
- Output ONLY valid JSON, no other text
- Make MINIMAL changes to fix the error
- Do NOT add new features or "improve" the code
- Keep the same coding style as the original
- You MAY fix multiple files if the error spans across them
- Consider edge cases: null/None, empty collections, boundary conditions

OUTPUT FORMAT:
{
  "changes": [
    {"file": "app.py", "content": "fixed file content", "reason": "why (≤100 chars)"}
  ]
}"""

FIX_SYSTEM_PROMPT_MULTI_ENHANCED = """You are an expert debugging agent with deep code understanding.

CONTEXT YOU WILL RECEIVE:
- Error output with full traceback
- Semantic call chain showing function relationships
- Related symbols and their locations
- Historical fix patterns for similar errors
- Files you are allowed to modify (hard constraint)

THINK IN THREE STEPS:
1. UNDERSTAND: Read the error and traceback. Study the call chain and related symbols.
2. LOCATE: Pinpoint exact lines across all relevant files. Check for side effects.
3. FIX: Design minimal changes. Verify callers and callees are not broken.

CRITICAL RULES:
- Output ONLY valid JSON, no other text
- Make MINIMAL changes — prefer targeted edits over rewrites
- Do NOT add features, refactor, or "improve" unrelated code
- Keep the exact same coding style as the original
- NEVER modify files outside the scope list
- If a historical fix FAILED for this error, do NOT repeat it
- Consider: null safety, index bounds, type coercion, import availability

OUTPUT FORMAT:
{
  "changes": [
    {"file": "app.py", "content": "fixed file content", "reason": "why (≤100 chars)"}
  ]
}"""

FIX_SNIPPET_PROMPT = """You are a debugging agent. Fix the specific error with minimal snippet changes.

RULES:
- Output ONLY valid JSON, no other text
- Only provide the specific lines that need to change (NOT the whole file)
- Make the MINIMAL change needed to fix the error
- Keep the same coding style
- Consider edge cases: null/None, empty collections, boundary conditions

OUTPUT FORMAT:
{
  "patches": [
    {"file": "app.py", "old": "the exact lines to replace",
     "new": "the replacement lines", "reason": "why (≤100 chars)"}
  ],
  "summary": "one-line fix summary (≤150 chars)"
}"""


def fix_multi(error_text: str, scope_files: list[str], model: str | None = None,
              project_context: str | None = None,
              code_graph=None, memory_bank=None,
              analysis: dict | None = None) -> list[dict]:
    code_blocks = ""
    for f in scope_files:
        p = Path(f)
        if p.exists():
            try:
                content = p.read_text(encoding="utf-8", errors="replace")
                ext = p.suffix.lower().lstrip(".") or "text"
                code_blocks += f"\n### {f}\n```{ext}\n{content}\n```\n"
            except (UnicodeDecodeError, OSError):
                pass

    # ── V0.5 增强：语义上下文 ──
    semantic_block = ""
    if code_graph is not None and scope_files:
        try:
            # 对 scope 中的第一个文件构建语义上下文
            for fpath in scope_files:
                semantic_block = _build_semantic_context(
                    code_graph, fpath, error_text, scope_files
                )
                if semantic_block:
                    break
            if semantic_block:
                logger.info(f"Fixer(Multi): 语义上下文注入 ({len(semantic_block)} 字符)")
        except Exception as e:
            logger.debug(f"Fixer(Multi): 语义上下文构建跳过: {e}")

    # ── V0.5 增强：历史记忆 ──
    memory_block = ""
    if memory_bank is not None and analysis:
        try:
            memory_block = _build_memory_context(
                memory_bank,
                analysis.get("error_type", ""),
                analysis.get("root_cause", ""),
            )
        except Exception as e:
            logger.debug(f"Fixer(Multi): 历史记忆构建跳过: {e}")

    context_block = f"{project_context}\n" if project_context else ""
    system_prompt = FIX_SYSTEM_PROMPT_MULTI_ENHANCED if (semantic_block or memory_block) else FIX_SYSTEM_PROMPT_MULTI

    user_message = f"""{context_block}{semantic_block}{memory_block}
Error:
{error_text}

Files to fix:
{code_blocks}

You may fix any of these files: {', '.join(scope_files)}
Output ONLY the JSON with changes array."""

    result = call_llm(
        system_prompt=system_prompt,
        user_message=user_message,
        model=model,
    )
    if result is None:
        return []
    return result.get("changes", [])


def fix_snippets(error_text: str, file_path: str, model: str | None = None,
                 diff_context: str = "",
                 code_graph=None, analysis: dict | None = None) -> list[dict]:
    p = Path(file_path)
    if not p.exists():
        return []
    current_code = p.read_text(encoding="utf-8", errors="replace")
    ext = p.suffix.lower().lstrip(".") or "text"

    # ── V0.5 增强：语义上下文 ──
    semantic_block = ""
    if code_graph is not None:
        try:
            semantic_block = _build_semantic_context(
                code_graph, file_path, error_text, [file_path]
            )
        except Exception as e:
            logger.debug(f"Fixer(Snippet): 语义上下文跳过: {e}")

    diff_block = f"\nRecent changes (for context):\n{diff_context}\n" if diff_context else ""
    user_message = f"""{semantic_block}Error:
{error_text}

Current code in {file_path}:
```{ext}
{current_code}
```
{diff_block}
Fix the error with minimal snippet changes. Output ONLY the JSON."""

    result = call_llm(
        system_prompt=FIX_SNIPPET_PROMPT,
        user_message=user_message,
        model=model,
    )
    if result is None:
        return []
    return result.get("patches", [])


def apply_fix(fix_result: dict, work_dir: str = ".") -> bool:
    """把 Fixer 的修复方案写入磁盘（文件级并发安全）

    Args:
        fix_result: {"file": "app.py", "content": "fixed code"}
        work_dir:   工作目录

    Returns:
        bool: 写入是否成功
    """
    from patchflow.core.concurrency import get_file_lock_manager
    from patchflow.core.fs import relative_path, safe_atomic_write

    wd = Path(work_dir)
    content = fix_result["content"]

    try:
        rel = relative_path(wd, fix_result["file"])
        flm = get_file_lock_manager()
        with flm.lock(rel):
            file_path = safe_atomic_write(wd, rel, content)
        logger.info(f"应用修复: {file_path} ({len(content)} 字符)")
        return True
    except Exception as e:
        logger.error(f"写入修复文件失败: {e}")
        return False
