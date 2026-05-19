"""自动修复器 — 根据错误信息修改代码

当 Validator 发现代码报错时，Fixer 接收错误信息和当前代码，
调用 LLM 生成修复后的代码。

设计要点（借鉴文档"硬约束"设计）：
  1. 错误驱动：只给 LLM 错误信息 + 当前代码，让它专注于修复
  2. 只修文件列表：V0.2 支持 Scope 硬约束，LLM 无法修改不在范围内的文件
  3. 不改整体逻辑：Prompt 要求"只修错误，不改其他"
  4. 输出 JSON：程序需要结构化地知道修复了哪个文件
"""

from pathlib import Path

from patchflow.core.llm_client import call_llm
from patchflow.utils import logger

FIX_SYSTEM_PROMPT = """You are a debugging agent. Your ONLY job is to fix the specific error in the code.

RULES:
- Output ONLY valid JSON, no other text
- Make the MINIMAL change needed to fix the error
- Do NOT rewrite the entire file
- Do NOT add new features or "improve" the code
- Keep the same coding style as the original
- Match the project's existing framework, dependencies, and style

OUTPUT FORMAT:
{
  "file": "app.py or the actual file path",
  "content": "the complete fixed file content"
}"""


def fix(error_text: str, file_path: str, model: str | None = None,
        scope: object | None = None,
        project_context: str | None = None) -> dict | None:
    """根据错误信息修复指定文件

    Args:
        error_text: 完整的错误输出（stdout + stderr）
        file_path:  需要修复的文件路径
        model:      LLM 模型
        scope:      Scope 对象（硬约束，指定可修改的文件范围）
        project_context: 项目上下文文本（Phase 3）

    Returns:
        dict 或 None:
            成功 → {"file": "<file_path>", "content": "fixed code"}
            失败 → None
    """
    logger.step(f"Fixer: 正在修复 {file_path}...")

    p = Path(file_path)
    if not p.exists():
        logger.error(f"Fixer: 文件不存在: {file_path}")
        return None

    current_code = p.read_text(encoding="utf-8")

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

    ext = p.suffix.lower()
    lang_tag = ext.lstrip(".") or "text"
    user_message = f"""{context_block}Error:
{error_text}

Current code in {file_path}:
```{lang_tag}
{current_code}
```
{scope_note}
Fix the error. Output ONLY the JSON with the fixed file content."""

    result = call_llm(
        system_prompt=FIX_SYSTEM_PROMPT,
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

    if scope_files and fixed_file not in scope_files:
        logger.error(f"Fixer: LLM 尝试修改不在范围内的文件: {fixed_file}")
        logger.error(f"  允许范围: {scope_files}")
        logger.error("  拒绝修复，返回 None 触发策略升级")
        return None

    logger.success(f"Fixer: 生成修复方案 for {result['file']}")
    return result


FIX_SYSTEM_PROMPT_MULTI = """You are a debugging agent. Fix the specific error in the code.

RULES:
- Output ONLY valid JSON, no other text
- Make MINIMAL changes to fix the error
- Do NOT add new features or "improve" the code
- Keep the same coding style as the original
- You MAY fix multiple files if the error spans across them

OUTPUT FORMAT:
{
  "changes": [
    {"file": "app.py", "content": "fixed file content", "reason": "why (<=100 chars)"}
  ]
}"""

FIX_SNIPPET_PROMPT = """You are a debugging agent. Fix the specific error with minimal snippet changes.

RULES:
- Output ONLY valid JSON, no other text
- Only provide the specific lines that need to change (NOT the whole file)
- Make the MINIMAL change needed to fix the error
- Keep the same coding style

OUTPUT FORMAT:
{
  "patches": [
    {"file": "app.py", "old": "the exact lines to replace",
     "new": "the replacement lines", "reason": "why (<=100 chars)"}
  ],
  "summary": "one-line fix summary (<=150 chars)"
}"""


def fix_multi(error_text: str, scope_files: list[str], model: str | None = None,
              project_context: str | None = None) -> list[dict]:
    code_blocks = ""
    for f in scope_files:
        p = Path(f)
        if p.exists():
            try:
                content = p.read_text(encoding="utf-8")
                ext = p.suffix.lower().lstrip(".") or "text"
                code_blocks += f"\n```{ext}\n{content}\n```\n"
            except (UnicodeDecodeError, OSError):
                pass

    context_block = f"{project_context}\n" if project_context else ""
    user_message = f"""{context_block}Error:
{error_text}

Files to fix:
{code_blocks}

You may fix any of these files: {', '.join(scope_files)}
Output ONLY the JSON with changes array."""

    result = call_llm(
        system_prompt=FIX_SYSTEM_PROMPT_MULTI,
        user_message=user_message,
        model=model,
    )
    if result is None:
        return []
    return result.get("changes", [])


def fix_snippets(error_text: str, file_path: str, model: str | None = None,
                 diff_context: str = "") -> list[dict]:
    p = Path(file_path)
    if not p.exists():
        return []
    current_code = p.read_text(encoding="utf-8")
    ext = p.suffix.lower().lstrip(".") or "text"

    diff_block = f"\nRecent changes (for context):\n{diff_context}\n" if diff_context else ""
    user_message = f"""Error:
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
    from patchflow.core.concurrency import AtomicWrite, get_file_lock_manager

    wd = Path(work_dir)
    file_path = wd / fix_result["file"]
    content = fix_result["content"]

    try:
        file_path.parent.mkdir(parents=True, exist_ok=True)
        flm = get_file_lock_manager()
        with flm.lock(fix_result["file"]):
            AtomicWrite.write(str(file_path), content)
        logger.info(f"应用修复: {file_path} ({len(content)} 字符)")
        return True
    except Exception as e:
        logger.error(f"写入修复文件失败: {e}")
        return False
