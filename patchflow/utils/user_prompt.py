"""User Prompt — 结构化用户提问

从 Claude Code 的 AskUserQuestion 移植：
  - 支持单选/多选/文本输入
  - 自动验证和默认值
  - Rich 终端美化渲染

使用方式：
    from patchflow.utils.user_prompt import ask, Question

    lang = ask(Question(
        "language", "Choose a language",
        options=["Python", "TypeScript", "Go"],
        default="Python",
    ))
"""

import os
import sys
from dataclasses import dataclass, field
from typing import Any

from patchflow.utils import logger


@dataclass
class Question:
    """单个问题定义"""
    key: str = ""
    text: str = ""
    header: str = ""
    options: list[str] = field(default_factory=list)
    multi_select: bool = False
    default: str = ""
    required: bool = True
    validate_fn: Any = None  # Callable[[str], bool|str] — True/None=ok, str=error msg


def ask(
    *questions: Question,
    use_rich: bool = True,
) -> dict[str, str | list[str]]:
    """向用户提出结构化问题

    Args:
        *questions: 问题列表
        use_rich: 是否使用 Rich 美化输出

    Returns:
        {key: answer} 字典
    """
    answers: dict[str, str | list[str]] = {}

    for q in questions:
        if use_rich:
            answer = _ask_rich(q)
        else:
            answer = _ask_plain(q)
        answers[q.key] = answer

    return answers


def _ask_rich(question: Question) -> str | list[str]:
    """Rich 终端渲染的问题"""
    try:
        from rich.console import Console
        from rich.panel import Panel
        from rich.prompt import Prompt, Confirm

        console = Console()

        header = question.header or question.key.replace("_", " ").title()
        console.print()
        console.print(Panel(
            question.text,
            title=header,
            border_style="cyan",
        ))

        if question.options:
            # 显示选项
            for i, opt in enumerate(question.options, 1):
                default_mark = " [dim](default)[/dim]" if opt == question.default else ""
                console.print(f"  {i}. {opt}{default_mark}")

            # 获取选择
            if question.multi_select:
                prompt_text = "Select (comma-separated numbers)"
                raw = Prompt.ask(prompt_text, default="")
                indices = _parse_indices(raw, len(question.options))
                return [question.options[i - 1] for i in indices]

            else:
                default_idx = ""
                if question.default and question.default in question.options:
                    default_idx = str(
                        question.options.index(question.default) + 1
                    )
                prompt_text = "Select (number)"
                raw = Prompt.ask(prompt_text, default=default_idx)
                try:
                    idx = int(raw) - 1
                    if 0 <= idx < len(question.options):
                        return question.options[idx]
                except ValueError:
                    pass
                return question.default or question.options[0]

        else:
            # 文本输入
            default = question.default
            answer = Prompt.ask("Enter", default=default)

            # 验证
            if question.validate_fn:
                validation = question.validate_fn(answer)
                if isinstance(validation, str):
                    console.print(f"[red]{validation}[/red]")
                    if question.required:
                        return _ask_rich(question)  # 递归重试

            return answer

    except ImportError:
        return _ask_plain(question)


def _ask_plain(question: Question) -> str | list[str]:
    """纯文本终端的问题"""
    header = question.header or question.key.replace("_", " ").title()
    print(f"\n{'=' * 50}")
    print(f"  {header}")
    print(f"  {question.text}")
    print(f"{'=' * 50}")

    if question.options:
        for i, opt in enumerate(question.options, 1):
            default_mark = " (default)" if opt == question.default else ""
            print(f"  {i}. {opt}{default_mark}")

        if question.multi_select:
            raw = input("  Select (comma-separated numbers): ").strip()
            indices = _parse_indices(raw, len(question.options))
            return [question.options[i - 1] for i in indices]
        else:
            default_idx = ""
            if question.default and question.default in question.options:
                default_idx = str(
                    question.options.index(question.default) + 1
                )
            prompt = f"  Select (number) [{default_idx}]: "
            raw = input(prompt).strip() or default_idx
            try:
                idx = int(raw) - 1
                if 0 <= idx < len(question.options):
                    return question.options[idx]
            except ValueError:
                pass
            return question.default or question.options[0]
    else:
        default = question.default
        prompt = f"  Enter [{default}]: " if default else "  Enter: "
        raw = input(prompt).strip() or default

        if question.validate_fn and question.required:
            validation = question.validate_fn(raw)
            if isinstance(validation, str):
                print(f"  Error: {validation}")
                return _ask_plain(question)

        return raw


def _parse_indices(raw: str, max_count: int) -> list[int]:
    """解析逗号分隔的索引字符串"""
    indices = []
    for part in raw.split(","):
        part = part.strip()
        try:
            idx = int(part)
            if 1 <= idx <= max_count:
                indices.append(idx)
        except ValueError:
            # 尝试范围: "1-3"
            if "-" in part:
                try:
                    start, end = part.split("-", 1)
                    for i in range(int(start), int(end) + 1):
                        if 1 <= i <= max_count:
                            indices.append(i)
                except ValueError:
                    pass
    return sorted(set(indices)) or [1]


# ═══════════════════════════════════════════════════════════
# 预定义问题模板
# ═══════════════════════════════════════════════════════════

def ask_project_type() -> str:
    """询问项目类型"""
    result = ask(Question(
        "project_type",
        "What type of project are you building?",
        header="Project Type",
        options=["Web App", "CLI Tool", "API Server", "Library", "Script"],
        default="Web App",
    ))
    return result.get("project_type", "Web App")


def ask_framework(language: str = "python") -> str:
    """询问框架选择"""
    frameworks = {
        "python": ["FastAPI", "Flask", "Django", "None"],
        "javascript": ["React", "Vue", "Express", "Next.js", "None"],
        "typescript": ["React", "Vue", "Express", "Next.js", "NestJS", "None"],
        "go": ["Gin", "Echo", "Fiber", "None"],
        "rust": ["Actix", "Axum", "Rocket", "None"],
    }
    options = frameworks.get(language, ["None"])

    result = ask(Question(
        "framework",
        f"Choose a framework for this {language} project:",
        header="Framework",
        options=options,
        default=options[0],
    ))
    return result.get("framework", options[0])


def confirm_plan(plan_summary: str, step_count: int) -> bool:
    """确认执行计划"""
    from rich.console import Console
    from rich.panel import Panel

    console = Console()
    console.print()
    console.print(Panel(
        f"{plan_summary}\n\nSteps: {step_count}",
        title="Confirm Plan",
        border_style="yellow",
    ))

    try:
        raw = input("  Proceed? [Y/n]: ").strip().lower()
        return raw in ("", "y", "yes")
    except (EOFError, KeyboardInterrupt):
        return False
