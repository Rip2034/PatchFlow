"""Agent Pipeline Display — 多 Agent 协作的终端进度显示

支持两种模式：
- Rich Panel 模式（TTY 环境自动启用，非 CI）
- Logger 纯文本模式（CI / 非 TTY / NO_COLOR 环境回退）
"""

import os
import sys
import time

from patchflow.utils import logger

STEP_NAMES = {"analyzer": "Analyzer", "fixer": "Fixer", "reviewer": "Reviewer"}

_ICON = {
    "pending":   "[dim]○[/dim]",
    "running":   "[bold cyan]◉[/bold cyan]",
    "completed": "[bold green]✓[/bold green]",
    "failed":    "[bold red]✗[/bold red]",
    "retry":     "[bold yellow]↻[/bold yellow]",
}

_SPINNER_FRAMES = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]


def _get_model_display(alias: str | None, fallback: str) -> str:
    if not alias:
        return fallback
    from patchflow.core.config import list_models
    models = list_models()
    cfg = models.get(alias)
    if cfg:
        return cfg.get("model", alias)
    return alias


def _detect_tty() -> bool:
    """检测当前环境是否支持 Rich 输出"""
    if not sys.stdout.isatty():
        return False
    if os.environ.get("CI", "") == "true":
        return False
    if os.environ.get("NO_COLOR", ""):
        return False
    return True


class AgentPipelineDisplay:
    """多 Agent 流水线进度显示

    在 TTY 环境下使用 Rich Panel 美化输出，非 TTY 环境回退到 logger。
    """

    def __init__(self, blackboard=None, use_rich: bool | None = None):
        self.steps: list[dict] = []
        self._start_time = time.time()
        self._blackboard = blackboard
        self._console = None
        self._use_rich = use_rich if use_rich is not None else _detect_tty()

    # ── 内部方法 ──────────────────────────────────────────

    def _ensure_console(self):
        if self._console is None:
            from rich.console import Console
            import shutil
            w = min(shutil.get_terminal_size((80, 24)).columns, 100)
            self._console = Console(width=w, emoji_variant="text", color_system="auto")
        return self._console

    def _spinner_char(self) -> str:
        return _SPINNER_FRAMES[int(time.time() * 10) % len(_SPINNER_FRAMES)]

    def _fmt_time(self, seconds: float | None) -> str:
        if seconds is None:
            return ""
        return f" [dim]({seconds:.1f}s)[/dim]"

    # ── 公共 API ──────────────────────────────────────────

    def add_step(self, role: str, model: str, detail: str = ""):
        self.steps.append({
            "role": role,
            "label": STEP_NAMES.get(role, role.title()),
            "model": model,
            "status": "pending",
            "summary": "",
            "detail": detail,
            "start_time": None,
            "duration": 0.0,
            "retry": 0,
        })

    def start(self):
        self._start_time = time.time()
        labels = " → ".join(s["label"] for s in self.steps)
        models = ", ".join(f"{s['label']}: {s['model']}" for s in self.steps)
        logger.info(f"[Pipeline] 启动多 Agent 流水线: {labels}")
        logger.info(f"[Pipeline] 模型分配: {models}")
        if self._use_rich:
            c = self._ensure_console()
            from rich.panel import Panel
            from rich.text import Text
            lines = []
            for i, s in enumerate(self.steps):
                lines.append(f" {i+1}. [bold]{s['label']}[/bold]  [dim]{s['model']}[/dim]")
                if s.get("detail"):
                    lines.append(f"    [dim]{s['detail'][:80]}[/dim]")
            c.print()
            c.print(Panel("\n".join(lines), title=Text("🤖 Agent Pipeline", style="bold cyan"), border_style="cyan"))

    def set_running(self, step_index: int):
        step = self.steps[step_index]
        step["status"] = "running"
        step["start_time"] = time.time()
        logger.info(f"[Pipeline] [{step['label']}] 开始运行...")
        if self._use_rich:
            try:
                c = self._ensure_console()
                c.print(f"  {_ICON['running']} [bold]{step['label']}[/bold] [cyan]{self._spinner_char()}[/cyan] running...")
            except (UnicodeEncodeError, UnicodeDecodeError):
                pass

    def set_completed(self, step_index: int, summary: str = ""):
        step = self.steps[step_index]
        step["status"] = "completed"
        if step["start_time"]:
            step["duration"] = time.time() - step["start_time"]
        step["summary"] = summary
        logger.info(f"[Pipeline] [{step['label']}] 完成 ({step['duration']:.1f}s){' — ' + summary if summary else ''}")
        if self._use_rich:
            try:
                c = self._ensure_console()
                dur = self._fmt_time(step.get("duration"))
                summ = f" — [green]{summary[:80]}[/green]" if summary else ""
                c.print(f"  {_ICON['completed']} [bold]{step['label']}[/bold] done{dur}{summ}")
            except (UnicodeEncodeError, UnicodeDecodeError):
                pass

    def set_failed(self, step_index: int, reason: str = ""):
        step = self.steps[step_index]
        step["status"] = "failed"
        if step["start_time"]:
            step["duration"] = time.time() - step["start_time"]
        step["summary"] = reason
        logger.error(f"[Pipeline] [{step['label']}] 失败{': ' + reason if reason else ''}")
        if self._use_rich:
            try:
                c = self._ensure_console()
                r = f" — [red]{reason[:80]}[/red]" if reason else ""
                dur = self._fmt_time(step.get("duration"))
                c.print(f"  {_ICON['failed']} [bold]{step['label']}[/bold] failed{dur}{r}")
            except (UnicodeEncodeError, UnicodeDecodeError):
                pass

    def set_retry(self, step_index: int):
        step = self.steps[step_index]
        step["retry"] += 1
        step["status"] = "running"
        step["start_time"] = time.time()
        step["summary"] = ""
        logger.info(f"[Pipeline] [{step['label']}] 重试 #{step['retry']}...")
        if self._use_rich:
            try:
                c = self._ensure_console()
                c.print(f"  {_ICON['retry']} [bold]{step['label']}[/bold] retry [bold yellow]#{step['retry']}[/bold yellow]")
            except (UnicodeEncodeError, UnicodeDecodeError):
                pass

    def set_detail(self, step_index: int, text: str, color: str = ""):
        step = self.steps[step_index] if step_index < len(self.steps) else None
        if step:
            step["detail"] = text

    def finish(self, success: bool):
        elapsed = time.time() - self._start_time
        result = "成功" if success else "失败"
        logger.info(f"[Pipeline] 流水线{result} (总耗时 {elapsed:.1f}s)")
        if self._use_rich:
            c = self._ensure_console()
            from rich.panel import Panel
            from rich.text import Text
            status_icon = _ICON["completed"] if success else _ICON["failed"]
            status_text = "SUCCESS" if success else "FAILED"
            border = "green" if success else "red"
            lines = []
            for s in self.steps:
                icon = _ICON.get(s["status"], "")
                dur = ""
                if s["status"] in ("completed", "failed") and s.get("duration"):
                    dur = f" [dim]({s['duration']:.1f}s)[/dim]"
                summary = s.get("summary", "")
                if summary:
                    summary = f"  [dim]{summary[:80]}[/dim]"
                lines.append(f"  {icon} [bold]{s['label']}[/bold]{dur}{summary}")
            lines.append("  " + "─" * 40)
            lines.append(f"  {status_icon} [bold]Total: {elapsed:.1f}s  |  {status_text}[/bold]")
            c.print()
            c.print(Panel("\n".join(lines), title=Text("🤖 Agent Pipeline Results", style=f"bold {border}"), border_style=border))
