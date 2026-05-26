"""Live Pipeline Dashboard — 基于 rich.live.Live 的实时流水线仪表盘

在支持 ANSI 的终端中原地刷新面板，显示所有 Agent 的实时状态。
不支持时自动回退到逐行 Rich 输出。

Windows 检测：支持 Windows Terminal (WT_SESSION)、VS Code、JetBrains 等现代终端。
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


def _detect_live_support() -> bool:
    """检测终端是否支持 rich.live.Live 原地刷新"""
    if not sys.stdout.isatty():
        return False
    if os.environ.get("CI", "") == "true":
        return False
    if os.environ.get("NO_COLOR", ""):
        return False
    if sys.platform == "win32":
        term = os.environ.get("TERM", "")
        if term in ("xterm-256color", "xterm", "screen-256color", "screen"):
            return True
        # Windows Terminal / VS Code / JetBrains 内置终端
        if os.environ.get("WT_SESSION", ""):
            return True
        if os.environ.get("TERM_PROGRAM", "") in ("vscode", "WezTerm"):
            return True
        return False
    return True


class LivePipelineDashboard:
    """实时流水线仪表盘

    与 AgentPipelineDisplay 共享完全相同的公共 API，
    可在 AgentOrchestrator 中透明替换。

    支持模式：
    - Live 模式（默认，终端支持时）：原地刷新 Panel，含 spinner / 进度条
    - 逐行模式（Live 不支持或异常时）：每步打印一行 Rich 输出
    """

    def __init__(self, blackboard=None):
        self.steps: list[dict] = []
        self._start_time = time.time()
        self._blackboard = blackboard
        self._live = None
        self._console = None
        self._live_supported = _detect_live_support()
        self._use_rich = sys.stdout.isatty()

    # ── 内部方法 ──────────────────────────────────────────

    def _ensure_console(self):
        if self._console is None:
            from rich.console import Console
            import shutil
            w = min(shutil.get_terminal_size((80, 24)).columns, 100)
            self._console = Console(width=w, emoji_variant="text", color_system="auto")
        return self._console

    def _ensure_live(self):
        if self._live is not None or not self._live_supported:
            return
        from rich.live import Live
        self._console = self._ensure_console()
        self._live = Live(
            self._build_panel(),
            console=self._console,
            refresh_per_second=8,
            transient=False,
        )
        self._live.start()

    def _spinner_char(self) -> str:
        """当前时刻的 spinner 字符"""
        return _SPINNER_FRAMES[int(time.time() * 10) % len(_SPINNER_FRAMES)]

    def _progress_bar(self, elapsed: float, max_est: float = 60.0) -> str:
        """生成迷你进度条（基于预估最长时间）"""
        pct = min(elapsed / max_est, 0.95)
        width = 8
        filled = int(pct * width)
        bar = "█" * filled + "░" * (width - filled)
        return f"[dim]{bar}[/dim]"

    def _build_panel(self):
        """构建实时面板 — 含表头、进度、状态"""
        from rich.panel import Panel
        from rich.table import Table
        from rich.text import Text

        table = Table(box=None, show_header=True, padding=(0, 1), expand=True, collapse_padding=True)
        table.add_column("#", width=3, justify="center", style="dim")
        table.add_column("Agent", width=15)
        table.add_column("Status", ratio=1)
        table.add_column("Time", width=14, justify="right")

        for i, step in enumerate(self.steps):
            no = str(i + 1)
            agent_str = f"[bold]{step['label']}[/bold]\n [dim]{step['model']}[/dim]"

            status = step["status"]
            if status == "running":
                spinner = self._spinner_char()
                detail = step.get("detail", "thinking...")
                elapsed = time.time() - step["start_time"] if step.get("start_time") else 0
                bar = self._progress_bar(elapsed)
                status_text = f"[bold cyan]{spinner}[/bold cyan] {detail[:60]} {bar}"
            elif status == "completed":
                summary = step.get("summary", "")
                if summary:
                    status_text = f"[green]{summary[:70]}[/green]"
                else:
                    status_text = "[green]Done[/green]"
            elif status == "failed":
                reason = step.get("summary", "Error")
                status_text = f"[red]{reason[:70]}[/red]"
            elif status == "retry":
                retry_n = step.get("retry", 1)
                status_text = f"[yellow]Retrying (#{retry_n})...[/yellow]"
            else:
                status_text = "[dim]Waiting...[/dim]"

            if status in ("completed", "failed") and step.get("duration"):
                time_str = f"[dim]{step['duration']:.1f}s[/dim]"
            elif status == "running" and step.get("start_time"):
                elapsed = time.time() - step["start_time"]
                time_str = f"[cyan]{elapsed:.1f}s[/cyan]"
            else:
                time_str = "[dim]--[/dim]"

            icon = _ICON.get(status, "")
            table.add_row(f"{no}\n{icon}", agent_str, status_text, time_str)

        elapsed = time.time() - self._start_time

        # 构建 footer 状态栏
        all_done = all(s["status"] in ("completed", "failed") for s in self.steps)
        status_summary_parts = []
        for s in self.steps:
            icon = _ICON.get(s["status"], "")
            status_summary_parts.append(f"{icon} {s['label']}")
        footer = "  ".join(status_summary_parts)
        footer += f"    [dim]Elapsed: {elapsed:.1f}s[/dim]"

        # token 统计
        bb = self._blackboard
        if bb:
            total_tokens = bb.get("total_tokens", 0)
            if total_tokens:
                footer += f"  [dim]Tokens: {total_tokens:,}[/dim]"

        panel_title = Text("🤖 Agent Pipeline", style="bold cyan")
        return Panel(
            table,
            title=panel_title,
            subtitle=footer,
            border_style="cyan",
            padding=(1, 2),
        )

    def _refresh(self):
        try:
            if self._live:
                self._live.update(self._build_panel())
        except Exception:
            logger.debug("Live refresh failed, falling back to line mode")
            self._live = None
            self._live_supported = False

    def _print_line(self, step: dict):
        try:
            c = self._ensure_console()
            icon = _ICON.get(step["status"], "")
            dur = ""
            if step["status"] in ("completed", "failed") and step.get("duration"):
                dur = f" [dim]({step['duration']:.1f}s)[/dim]"
            elif step["status"] == "running" and step.get("start_time"):
                elapsed = time.time() - step["start_time"]
                dur = f" [cyan]({elapsed:.1f}s)[/cyan]"
            summary = step.get("summary", "")
            if summary:
                summary = f" — {summary[:80]}"
            retry = f" [bold yellow]#{step['retry']}[/bold yellow]" if step.get("retry") else ""
            c.print(f"  {icon} [bold]{step['label']}[/bold]{retry}{dur}{summary}")
        except (UnicodeEncodeError, UnicodeDecodeError):
            pass

    # ── 公共 API（与 AgentPipelineDisplay 相同）────────────

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
        if self._live_supported:
            self._ensure_live()
        elif self._use_rich:
            c = self._ensure_console()
            c.print()
            from rich.panel import Panel
            from rich.text import Text
            lines = []
            for i, s in enumerate(self.steps):
                lines.append(f" {i+1}. [bold]{s['label']}[/bold]  [dim]{s['model']}[/dim]")
                if s.get("detail"):
                    lines.append(f"    [dim]{s['detail'][:80]}[/dim]")
            c.print(Panel("\n".join(lines), title=Text("🤖 Agent Pipeline", style="bold cyan"), border_style="cyan"))

    def set_running(self, step_index: int):
        step = self.steps[step_index]
        step["status"] = "running"
        step["start_time"] = time.time()
        logger.info(f"[Pipeline] [{step['label']}] 开始运行...")
        if self._live_supported:
            self._refresh()
        elif self._use_rich:
            self._print_line(step)

    def set_completed(self, step_index: int, summary: str = ""):
        step = self.steps[step_index]
        step["status"] = "completed"
        if step["start_time"]:
            step["duration"] = time.time() - step["start_time"]
        step["summary"] = summary
        logger.info(f"[Pipeline] [{step['label']}] 完成 ({step['duration']:.1f}s){' — ' + summary if summary else ''}")
        if self._live_supported:
            self._refresh()
        else:
            self._print_line(step)

    def set_failed(self, step_index: int, reason: str = ""):
        step = self.steps[step_index]
        step["status"] = "failed"
        if step["start_time"]:
            step["duration"] = time.time() - step["start_time"]
        step["summary"] = reason
        logger.error(f"[Pipeline] [{step['label']}] 失败{': ' + reason if reason else ''}")
        if self._live_supported:
            self._refresh()
        else:
            self._print_line(step)

    def set_retry(self, step_index: int):
        step = self.steps[step_index]
        step["retry"] += 1
        step["status"] = "running"
        step["start_time"] = time.time()
        step["summary"] = ""
        logger.info(f"[Pipeline] [{step['label']}] 重试 #{step['retry']}...")
        if self._live_supported:
            self._refresh()
        else:
            self._print_line(step)

    def set_detail(self, step_index: int, text: str, color: str = ""):
        step = self.steps[step_index] if step_index < len(self.steps) else None
        if step:
            step["detail"] = text
        if self._live_supported and step and step["status"] == "running":
            self._refresh()

    def finish(self, success: bool):
        elapsed = time.time() - self._start_time
        result = "成功" if success else "失败"
        logger.info(f"[Pipeline] 流水线{result} (总耗时 {elapsed:.1f}s)")

        if self._live:
            try:
                self._live.stop()
            except Exception:
                pass
            self._live = None

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
