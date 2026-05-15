"""Planner — 结构化计划生成与分步执行

当用户输入一个复杂的任务（如"创建一个 TODO 应用"），
Planner 先让 LLM 输出分步计划，用户确认后逐步执行。

设计目标：
  - 透明：用户可以看到 AI 的设计思路（不再是黑盒）
  - 可控：每步独立执行，用户可以随时中断或修改
  - 可恢复：某步失败不影响之前已成功的步骤
  - 扩展性：为后续"主 Agent 规划，子 Agent 执行"预留接口

工作流程：
  1. 用户输入任务描述
  2. LLM 生成结构化计划（多步，每步有明确目标、文件列表）
  3. 用户审查并确认计划
  4. 逐步骤执行（每步调用代码生成器）
  5. 全部完成后最终验证
"""

from dataclasses import dataclass, field

from patchflow.core.fix.generator import generate, write_files
from patchflow.core.fix.validator import validate
from patchflow.core.llm_client import call_llm
from patchflow.core.project.context_collector import ContextCollector, build_context_prompt
from patchflow.utils import logger

PLANNER_PROMPT = """You are a software architect. Given a task description, break it down into a structured, step-by-step plan.

Each step should be a concrete, actionable unit of work that can be executed independently.
Steps must be ordered logically (foundations first, then features).

For each step, provide:
- step: sequential number
- title: short step name (≤40 chars)
- description: what this step does (1-2 sentences, ≤120 chars)
- task: detailed instructions for a coding agent to implement this step. Include specific file names, class names, function signatures, data structures.
- files_expected: list of files this step will create or modify

CRITICAL RULES:
- Each step's task MUST be self-contained — the coding agent should NOT need to read other steps' tasks to implement this step.
- Each step produces working code that builds on previous steps.
- At most 8 steps. Prefer fewer, more focused steps.
- Output ONLY valid JSON, no other text.

OUTPUT FORMAT:
{
  "summary": "one-line project summary (≤150 chars)",
  "steps": [
    {
      "step": 1,
      "title": "Project scaffold",
      "description": "Create project skeleton and config",
      "task": "Create pyproject.toml with fastapi+uvicorn deps, create app.py with FastAPI app skeleton including lifespan...",
      "files_expected": ["pyproject.toml", "app.py"]
    }
  ]
}"""


@dataclass
class PlanStep:
    step: int
    title: str
    description: str
    task: str
    files_expected: list[str] = field(default_factory=list)
    status: str = "pending"
    error: str = ""
    files_written: list[str] = field(default_factory=list)


@dataclass
class Plan:
    summary: str
    steps: list[PlanStep]
    task: str = ""


def _format_plan_preview(plan: Plan) -> str:
    """生成计划的文本预览"""
    lines = []
    lines.append(f"  [bold]计划:[/bold] {plan.summary}")
    lines.append(f"  [dim]共 {len(plan.steps)} 步[/dim]")
    lines.append("")
    for s in plan.steps:
        files_hint = f"  → {', '.join(s.files_expected[:3])}" if s.files_expected else ""
        lines.append(f"  {s.step}. {s.title}{files_hint}")
        lines.append(f"     [dim]{s.description}[/dim]")
    return "\n".join(lines)


class PlanExecutor:
    """Plan 执行器 — 生成计划 → 确认 → 逐步骤执行"""

    def __init__(self, model: str | None = None, work_dir: str = "."):
        self.model = model
        self.work_dir = work_dir
        self.plan: Plan | None = None
        self._project_context = None
        self._on_step_change = None

    def on_step_change(self, callback):
        self._on_step_change = callback

    def _get_context(self) -> str:
        if self._project_context is None:
            collector = ContextCollector(self.work_dir)
            ctx = collector.collect(use_cache=True)
            self._project_context = build_context_prompt(ctx)
        return self._project_context

    def generate_plan(self, task: str) -> Plan | None:
        """调用 LLM 生成结构化计划"""
        logger.step("Planner: 正在制定计划...")

        context_prompt = self._get_context()

        user_message = f"""{context_prompt}
Task: {task}

Analyze this task and create a detailed step-by-step plan.
Consider the project's existing language, framework, and structure.
Keep steps concrete and actionable.
Output ONLY the JSON plan."""

        result = call_llm(
            system_prompt=PLANNER_PROMPT,
            user_message=user_message,
            model=self.model,
        )

        if result is None:
            logger.error("Planner: LLM 调用失败")
            return None

        raw_steps = result.get("steps", [])
        if not raw_steps:
            logger.error("Planner: 未生成有效步骤")
            return None

        steps: list = []
        for s in raw_steps:
            steps.append(PlanStep(
                step=s.get("step", len(steps) + 1),
                title=s.get("title", ""),
                description=s.get("description", ""),
                task=s.get("task", ""),
                files_expected=s.get("files_expected", []),
            ))

        self.plan = Plan(
            summary=result.get("summary", task[:100]),
            steps=steps,
            task=task,
        )

        logger.success(f"Planner: 生成了 {len(steps)} 步计划")
        return self.plan

    def execute_step(self, step_index: int) -> bool:
        """执行指定索引的步骤

        Args:
            step_index: 在 self.plan.steps 中的索引

        Returns:
            bool: 是否成功
        """
        if not self.plan or step_index >= len(self.plan.steps):
            return False

        step = self.plan.steps[step_index]
        step.status = "running"
        self._notify_step_change()

        context_prompt = self._get_context()

        files = generate(
            step.task,
            model=self.model,
            project_context=context_prompt,
        )

        if files is None:
            step.status = "failed"
            step.error = "代码生成返回空"
            self._notify_step_change()
            return False

        written = write_files(files, work_dir=self.work_dir)
        step.files_written = written
        step.status = "completed"
        self._notify_step_change()
        return True

    def execute_all(self, on_step_progress=None) -> bool:
        """顺序执行所有步骤

        Args:
            on_step_progress: 可选回调，每步状态变化时调用

        Returns:
            bool: 是否全部成功
        """
        if not self.plan:
            logger.error("Plan: 没有已生成的计划")
            return False

        total = len(self.plan.steps)
        all_ok = True

        for i in range(total):
            ok = self.execute_step(i)
            if on_step_progress:
                on_step_progress(self.plan, i, ok)
            if not ok:
                all_ok = False
                step = self.plan.steps[i]
                logger.error(f"[Plan] 步骤 {i + 1}/{total} 失败: {step.title}")
                logger.error(f"[Plan]  原因: {step.error or '代码生成失败'}")
                break

        if all_ok:
            logger.success(f"[Plan] 全部 {total} 步执行完成")

            result = validate(work_dir=self.work_dir)
            if result.ok:
                logger.success("[Plan] 最终验证通过")
            else:
                logger.warn(f"[Plan] 最终验证未通过: {result.message or '验证失败'}")
                all_ok = False

        return all_ok

    def _notify_step_change(self):
        if self._on_step_change:
            self._on_step_change()
