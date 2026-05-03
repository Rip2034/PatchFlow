"""代码 diff 工具 — 展示修复前后的变更

用于在修复完成后向用户展示具体改了哪些内容。
基于 difflib 实现，纯确定性代码。
"""

import difflib
from pathlib import Path


def diff_text(old_text: str, new_text: str, context_lines: int = 3) -> str:
    """生成统一的 diff 文本

    Args:
        old_text: 原始内容
        new_text: 新内容
        context_lines: 上下文行数

    Returns:
        unified diff 格式的文本
    """
    old_lines = old_text.splitlines(keepends=True)
    new_lines = new_text.splitlines(keepends=True)

    diff = difflib.unified_diff(
        old_lines,
        new_lines,
        fromfile="original",
        tofile="modified",
        n=context_lines,
    )
    return "".join(diff)


def diff_files(old_path: str, new_path: str, context_lines: int = 3) -> str:
    """对比两个文件的差异

    Args:
        old_path: 原始文件路径
        new_path: 新文件路径
        context_lines: 上下文行数

    Returns:
        unified diff 文本，文件不存在时返回空字符串
    """
    old_p = Path(old_path)
    new_p = Path(new_path)

    old_text = old_p.read_text(encoding="utf-8") if old_p.exists() else ""
    new_text = new_p.read_text(encoding="utf-8") if new_p.exists() else ""

    return diff_text(old_text, new_text, context_lines)


def format_summary(diff_text: str) -> str:
    """从 diff 文本中提取变更摘要

    统计新增/删除行数，返回简洁摘要。

    Returns:
        如 "+10 additions, -3 deletions"
    """
    additions = 0
    deletions = 0
    for line in diff_text.split("\n"):
        if line.startswith("+") and not line.startswith("+++"):
            additions += 1
        elif line.startswith("-") and not line.startswith("---"):
            deletions += 1
    parts = []
    if additions:
        parts.append(f"+{additions} additions")
    if deletions:
        parts.append(f"-{deletions} deletions")
    return ", ".join(parts) if parts else "no changes"


def has_changes(old_text: str, new_text: str) -> bool:
    """快速检查是否有变更"""
    return old_text != new_text
