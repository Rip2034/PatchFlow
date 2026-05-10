"""Agent Pipeline Display — 多 Agent 协作的可视化终端面板

使用 Rich Live 实现实时动画，展示每个 Agent 的运行状态、耗时和 Blackboard 读写活动。
"""

import time

from rich.console import Console, Group
from rich.live import Live
from rich.panel import Panel
from rich.text import Text

SPINNER_FRAMES = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]

STEP_DEFS = [
    {"role": "analyzer",  "icon": "[A]", "label": "Analyzer",  "color": "cyan"},
    {"role": "fixer",     "icon": "[F]", "label": "Fixer",     "color": "yellow"},
    {"role": "reviewer",  "icon": "[R]", "label": "Reviewer",  "color": "green"},
]

STATUS_STYLES = {
    "pending":   {"char": "o", "style": "dim",  "border": "gray50"},
    "running":   {"char": "",  "style": "cyan bold", "border": "cyan"},
    "completed": {"char": "v", "style": "green bold", "border": "green"},
    "failed":    {"char": "x", "style": "red bold",   "border": "red"},
}


def _get_model_display(alias: str | None, fallback: str) -> str:
    """获取模型的展示名称"""
    if not alias:
        return fallback
    from patchflow.core.config import list_models
    models = list_models()
    cfg = models.get(alias)
    if cfg:
        return cfg.get("model", alias)
    return alias


class AgentPipelineDisplay:
    """多 Agent 流水线可视化面板"""

    def __init__(self, blackboard=None):
        self.steps: list[dict] = []
        self._live: Live | None = None
        self._start_time = time.time()
        self._result: str | None = None
        self._console = Console()
        self._blackboard = blackboard

    def set_blackboard(self, blackboard):
        self._blackboard = blackboard

    def add_step(self, role: str, model: str, detail: str = ""):
        cfg = next((s for s in STEP_DEFS if s["role"] == role), {})
        self.steps.append({
            "role": role,
            "label": cfg.get("label", role.title()),
            "icon": cfg.get("icon", "[?]"),
            "model": model,
            "status": "pending",
            "summary": "",
            "detail": detail,
            "detail_color": "dim",
            "duration": 0.0,
            "start_time": None,
            "retry": 0,
        })

    def start(self):
        self._start_time = time.time()
        self._live = Live(
            self._build_renderable(),
            refresh_per_second=10,
            vertical_overflow="visible",
            console=self._console,
        )
        self._live.start()

    def set_running(self, step_index: int):
        step = self.steps[step_index]
        step["status"] = "running"
        step["start_time"] = time.time()
        self._refresh()

    def set_completed(self, step_index: int, summary: str = ""):
        step = self.steps[step_index]
        step["status"] = "completed"
        step["summary"] = summary
        if step["start_time"]:
            step["duration"] = time.time() - step["start_time"]
        self._refresh()

    def set_failed(self, step_index: int, reason: str = ""):
        step = self.steps[step_index]
        step["status"] = "failed"
        step["summary"] = reason
        if step["start_time"]:
            step["duration"] = time.time() - step["start_time"]
        self._refresh()

    def set_retry(self, step_index: int):
        step = self.steps[step_index]
        step["retry"] += 1
        step["status"] = "running"
        step["start_time"] = time.time()
        step["summary"] = ""
        step["detail"] = "redo..."
        self._refresh()

    def set_detail(self, step_index: int, text: str, color: str = "dim"):
        if 0 <= step_index < len(self.steps):
            self.steps[step_index]["detail"] = text
            self.steps[step_index]["detail_color"] = color
            self._refresh()

    def finish(self, success: bool):
        self._result = "Success" if success else "Failed"
        self._refresh()
        if self._live:
            self._live.stop()
            self._live = None

    def _refresh(self):
        if self._live:
            self._live.update(self._build_renderable())

    def _spinner_char(self, start_time: float | None) -> str:
        if start_time is None:
            return "o"
        elapsed = time.time() - start_time
        frame = int(elapsed * 10) % len(SPINNER_FRAMES)
        return SPINNER_FRAMES[frame]

    def _get_step_activity(self, role: str) -> tuple[list[str], list[str]]:
        """从 Blackboard 获取指定角色的读/写字段列表"""
        if not self._blackboard:
            return [], []
        summary = self._blackboard.get_activity_summary()
        info = summary.get(role)
        if not info:
            return [], []
        reads = sorted(info.get("read", set()))
        writes = sorted(info.get("write", set()))
        return reads, writes

    def _build_renderable(self):
        rows = []

        title = Text("  PatchFlow Multi-Agent Pipeline  ", style="bold cyan")
        rows.append(Panel(title, border_style="cyan", padding=(0, 1)))

        for i, step in enumerate(self.steps):
            rows.append(self._render_step(i, step))

        done_count = sum(1 for s in self.steps if s["status"] in ("completed", "failed"))
        total = len(self.steps)
        elapsed_total = time.time() - self._start_time

        parts = []
        if self._result:
            icon = "v" if "Success" in self._result else "x"
            parts.append(f"{icon} {self._result}")
        else:
            parts.append(f"Step {done_count}/{total}")
        parts.append(f"Total: {elapsed_total:.1f}s")
        if self.steps:
            running = [s for s in self.steps if s["status"] == "running"]
            if running:
                parts.append(f"Active: {running[0]['label']}")

        footer = Text("  " + "  |  ".join(parts), style="bold")
        rows.append(Panel(footer, border_style="gray50", padding=(0, 1)))

        return Group(*rows)

    def _render_step(self, index: int, step: dict) -> Panel:
        status = step["status"]
        ss = STATUS_STYLES.get(status, STATUS_STYLES["pending"])
        icon = step["icon"]
        label = step["label"]
        model = step["model"]
        role = step["role"]

        if step["retry"] > 0:
            label = f"{label} (redo #{step['retry']})"

        content = Text()

        # ── 第一行：状态 + 耗时 ──
        if status == "running":
            sp = self._spinner_char(step["start_time"])
            elapsed = time.time() - step["start_time"]
            status_text = f"{sp} Running  "
            dur_text = f"({elapsed:.1f}s)"
            content.append(f"  {icon}  ", style=step.get("color", ""))
            content.append(Text(status_text, style=ss["style"]))
            content.append(Text(dur_text, style="cyan"))
        elif status == "completed":
            d = step["duration"]
            status_text = f"{ss['char']} Done  "
            dur_text = f"({d:.1f}s)"
            content.append(f"  {icon}  ", style=step.get("color", ""))
            content.append(Text(status_text, style=ss["style"]))
            content.append(Text(dur_text, style="green"))
        elif status == "failed":
            d = step["duration"]
            status_text = f"{ss['char']} Failed  "
            dur_text = f"({d:.1f}s)"
            content.append(f"  {icon}  ", style=step.get("color", ""))
            content.append(Text(status_text, style=ss["style"]))
            content.append(Text(dur_text, style="red"))
        else:
            content.append(f"  {icon}  {ss['char']} Pending  ", style=ss["style"])

        # ── 第二行：模型信息 ──
        if model:
            content.append("\n")
            content.append(f"     Model: {model}", style="dim")

        # ── 第三行：Blackboard 读写活动 ──
        reads, writes = self._get_step_activity(role)
        if reads or writes:
            content.append("\n")
            activity_parts = []
            if reads:
                read_str = ", ".join(reads[:5])
                activity_parts.append(f"read: {read_str}")
            if writes:
                write_str = ", ".join(writes[:3])
                activity_parts.append(f"wrote: {write_str}")
            if activity_parts:
                activity_text = "; ".join(activity_parts)
                content.append(f"     BB: {activity_text}", style="bright_blue")

        # ── 第四行：detail / summary ──
        detail = step.get("detail", "")
        if status == "running" and detail:
            content.append("\n")
            content.append(f"     {detail[:80]}", style=step.get("detail_color", "dim"))

        elif status == "completed" and step["summary"]:
            content.append("\n")
            content.append(f"     {step['summary'][:100]}", style="green")
        elif status == "failed" and step["summary"]:
            content.append("\n")
            content.append(f"     {step['summary'][:100]}", style="red")

        return Panel(content, border_style=ss["border"], padding=(1, 2))
