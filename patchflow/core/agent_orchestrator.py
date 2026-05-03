"""AgentOrchestrator — 多 Agent Blackboard 调度器

这是 Phase 5 的核心。调度三个 Agent 的协作流程：
  Analyzer → Fixer → Reviewer (+ 可选重做)

设计文档的调度逻辑（V1.0）：
  1. Analyzer 定位问题
  2. 置信度不够 → 上报用户
  3. Fixer 修复
  4. Reviewer 审查
  5. 不通过 → 带 review 意见让 Fixer 重做
  6. 应用 patch → 验证

多模型支持：
  config.json 中 agents 段可以指定每个角色使用的模型别名：
    "agents": {
      "analyzer": "deepseek",   → 分析用便宜模型
      "fixer": "claude",        → 修复用强模型
      "reviewer": "deepseek"    → 审查用便宜模型
    }
  未配置的角色回退到 active 模型。
"""

import os
from pathlib import Path

from patchflow.utils import logger
from patchflow.utils.diff import diff_text, format_summary
from patchflow.utils.agent_display import AgentPipelineDisplay, _get_model_display
from patchflow.core.fix.snapshot_manager import SnapshotManager
from patchflow.core.fix.scope_calculator import DepGraph


class AgentOrchestrator:
    """多 Agent 调度器"""

    def __init__(self, model: str | None = None, work_dir: str = "."):
        from patchflow.core.config import get_model
        self.model = model or get_model()
        self.work_dir = work_dir
        self.snapshot = SnapshotManager(work_dir)
        self.dep_graph = DepGraph(work_dir)
        self.turn_count = 0
        self._agent_aliases: dict[str, str] = {}

    def _get_alias(self, role: str) -> str | None:
        """读取 config.json 中 agents 段的角色-模型别名映射"""
        if role not in self._agent_aliases:
            from patchflow.core.config import get_config
            agents_cfg = get_config().get("agents", {})
            self._agent_aliases[role] = agents_cfg.get(role)
        return self._agent_aliases[role]

    def run_from_task(self, task: str, work_dir: str | None = None) -> bool:
        """便捷方法：从任务描述直接启动多 Agent 修复

        自动收集项目上下文、读取代码文件、尝试运行获取错误信息，
        然后构建 Blackboard 并执行完整的多 Agent 修复流程。

        Args:
            task: 任务描述
            work_dir: 工作目录（默认使用初始化时设置的目录）

        Returns:
            True → 修复通过，False → 修复失败
        """
        wd = work_dir or self.work_dir
        logger.info(f"[AgentOrch] run_from_task: 自动收集项目上下文...")

        # 1. 收集项目上下文
        from patchflow.core.project.context_collector import ContextCollector
        collector = ContextCollector(wd)
        ctx = collector.collect(use_cache=True)

        # 2. 读取所有 Python 文件
        code = {}
        for f in Path(wd).glob("**/*.py"):
            if any(part.startswith(".") or part.startswith("_") and part in ("__pycache__",)
                   for part in f.relative_to(wd).parts):
                if f.name != "__init__.py":
                    continue
            rel = str(f.relative_to(wd))
            try:
                code[rel] = f.read_text(encoding="utf-8")
            except Exception:
                continue

        # 3. 尝试运行获取错误
        error_text = ""
        for entry in ("app.py", "main.py", "cli.py", "manage.py"):
            entry_path = Path(wd, entry)
            if entry_path.exists():
                from patchflow.utils.runner import run
                result = run(f"python {entry}", cwd=wd, timeout=30)
                if result.exit_code != 0:
                    error_text = result.stderr.strip() or result.stdout.strip()
                    break
        else:
            error_text = "(no entry point found, repair based on task description)"

        # 4. 构建 Blackboard
        from patchflow.agents.blackboard import Blackboard
        bb = Blackboard(
            task=task,
            context=ctx.to_dict(),
            code=code,
            error=error_text,
        )

        # 5. 执行多 Agent 修复
        logger.info(f"[AgentOrch] Blackboard 构建完成: {len(code)} files, error={len(error_text)} chars")
        return self.run(bb)

    def run(self, blackboard) -> bool:
        """执行完整的多 Agent 修复流程

        Args:
            blackboard: Blackboard 实例（必须包含 task, context, code, error）

        Returns:
            True → 修复通过
            False → 修复失败
        """
        analyzer_alias = self._get_alias("analyzer")
        fixer_alias = self._get_alias("fixer")
        reviewer_alias = self._get_alias("reviewer")

        analyzer_model = _get_model_display(analyzer_alias, self.model)
        fixer_model = _get_model_display(fixer_alias, self.model)
        reviewer_model = _get_model_display(reviewer_alias, self.model)

        # ── 提取关键信息用于显示 ──
        error_text = blackboard.get("error", "")
        error_first_line = error_text.split("\n")[0][:80] if error_text else "N/A"
        task_text = blackboard.get("task", "")[:60]
        code_files = list(blackboard.get("code", {}).keys())
        files_preview = ", ".join(code_files[:5]) if code_files else "(none)"

        # ── 初始化可视化面板（绑定 Blackboard 以实时显示 Agent 读写活动） ──
        display = AgentPipelineDisplay(blackboard=blackboard)
        display.add_step("analyzer", analyzer_model,
                         detail=f"Task: {task_text}" if task_text else "")
        display.add_step("fixer", fixer_model,
                          detail=f"Files: {files_preview}")
        display.add_step("reviewer", reviewer_model)
        display.start()

        logger.info(f"  AgentOrch 任务: {blackboard['task'][:80]}")

        # ── 保存快照 ──
        files_to_snapshot = list(blackboard.get("code", {}).keys())
        snap_id = self.snapshot.save(files_to_snapshot)

        # ── 构建依赖图 ──
        try:
            self.dep_graph.build()
        except Exception:
            pass

        # ── Step 1: Analyzer 定位问题 ──
        display.set_detail(0, f"Analyzing {len(code_files)} files...")
        display.set_running(0)
        from patchflow.agents.analyzer import agent_analyze
        analysis = agent_analyze(blackboard, model=self.model,
                                 model_alias=analyzer_alias)
        self.turn_count += 1
        error_type = analysis.get("error_type", "")
        display.set_completed(0, f"Error: {error_type}" if error_type else analysis.get("summary", ""))
        logger.info(f"  Analyzer: {blackboard.summary()}")

        # ── Step 2: 置信度不够 → 上报 ──
        if analysis.get("confidence", 0) < 0.5:
            display.set_failed(0, f"置信度过低 ({analysis['confidence']})")
            display.finish(False)
            logger.error(f"[AgentOrch] 分析置信度过低 ({analysis['confidence']})，上报用户")
            self.snapshot.rollback(snap_id)
            return False

        # ── Step 3: Fixer 修复 ──
        display.set_detail(1, f"Fixing {len(code_files)} files...")
        display.set_running(1)
        from patchflow.agents.fixer_agent import agent_fix, apply_agent_patches
        blackboard["fix_plan"] = agent_fix(blackboard, dep_graph=self.dep_graph,
                                           model=self.model, model_alias=fixer_alias)
        self.turn_count += 1
        patch_count = len(blackboard["fix_plan"].get("patches", []))
        display.set_completed(1, f"{patch_count} patches generated" if patch_count else "No patches generated")
        logger.info(f"  Fixer: {blackboard.summary()}")

        if not blackboard["fix_plan"].get("patches"):
            display.set_failed(1, "未生成任何补丁")
            display.finish(False)
            logger.error("[AgentOrch] Fixer 未生成任何补丁，终止")
            self.snapshot.rollback(snap_id)
            return False

        # ── 记录原版内容用于 diff ──
        original_files = {}
        for patch in blackboard["fix_plan"]["patches"]:
            from pathlib import Path
            fp = patch.get("file", "")
            if fp:
                p = Path(self.work_dir) / fp
                if p.exists():
                    original_files[fp] = p.read_text(encoding="utf-8")

        # ── 应用补丁 ──
        if not apply_agent_patches(blackboard, work_dir=self.work_dir):
            display.set_failed(1, "补丁应用失败")
            display.finish(False)
            logger.error("[AgentOrch] 补丁应用失败")
            self.snapshot.rollback(snap_id)
            return False

        # ── Step 4: Reviewer 审查 ──
        display.set_detail(2, "Reviewing results...")
        display.set_running(2)
        from patchflow.agents.reviewer import agent_review
        review = agent_review(blackboard, model=self.model,
                              model_alias=reviewer_alias)
        self.turn_count += 1
        score = review.get("score", 0)
        display.set_completed(2, f"Score: {score}/10 (approved)" if review.get("approved") else f"Score: {score}/10 (needs redo)")
        logger.info(f"  Reviewer: {blackboard.summary()}")

        # ── Step 5: 不通过 → 带意见重做一次 ──
        if not review.get("approved", False):
            logger.warn(f"[AgentOrch] Reviewer 驳回 (score: {review.get('score',0)}/10)")
            logger.info(f"[AgentOrch]   Issues: {review.get('issues', [])}")

            # 回滚到原始文件
            self.snapshot.rollback(snap_id)

            # 带 feedback 重新修复
            blackboard["review_feedback"] = review.get("feedback", "")
            display.set_detail(1, "Redoing based on review feedback...")
            display.set_retry(1)
            blackboard["fix_plan"] = agent_fix(blackboard, dep_graph=self.dep_graph,
                                               model=self.model, model_alias=fixer_alias)
            self.turn_count += 1

            if not blackboard["fix_plan"].get("patches"):
                display.set_failed(1, "二次修复未生成补丁")
                display.finish(False)
                logger.error("[AgentOrch] 二次修复未生成补丁")
                return False

            patch_count2 = len(blackboard["fix_plan"].get("patches", []))
            display.set_completed(1, f"{patch_count2} patches (redo)")

            # 重新保存快照对二次修复后的内容
            snap_id = self.snapshot.save(files_to_snapshot)

            if not apply_agent_patches(blackboard, work_dir=self.work_dir):
                display.set_failed(1, "二次补丁应用失败")
                display.finish(False)
                self.snapshot.rollback(snap_id)
                return False

            # 再审
            display.set_detail(2, "Re-reviewing...")
            display.set_retry(2)
            review = agent_review(blackboard, model=self.model,
                                  model_alias=reviewer_alias)
            self.turn_count += 1
            score2 = review.get("score", 0)
            display.set_completed(2, f"Score: {score2}/10 (approved)" if review.get("approved") else f"Score: {score2}/10 (rejected)")

            if not review.get("approved", False):
                display.set_failed(2, f"二次审查未通过 ({review.get('score',0)})")
                display.finish(False)
                logger.error(f"[AgentOrch] 二次审查仍未通过 (score: {review.get('score',0)})")
                self.snapshot.rollback(snap_id)
                return False

        # ── Step 6: 验证 ──
        from patchflow.core.fix.validator import validate
        result = validate(work_dir=self.work_dir)

        if result.ok:
            self.snapshot.commit(snap_id)
            self._print_diff_report(original_files)
            logger.success(f"[AgentOrch] Agent 协作修复成功! ({self.turn_count} 步)")
            display.finish(True)
            return True

        # 运行验证失败
        logger.error(f"[AgentOrch] 验证失败，回滚")
        display.finish(False)
        self.snapshot.rollback(snap_id)
        return False

    def _print_diff_report(self, original_files: dict[str, str]):
        diff_lines = []
        for filepath, original in original_files.items():
            from pathlib import Path
            current = Path(self.work_dir / filepath).read_text(encoding="utf-8") if Path(self.work_dir / filepath).exists() else ""
            diff = diff_text(original, current, context_lines=2)
            if diff.strip():
                summary = format_summary(diff)
                diff_lines.append(f"{filepath}: {summary}")
        if diff_lines:
            logger.info("  Diff: " + "; ".join(diff_lines))
