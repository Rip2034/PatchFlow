"""Orchestrator — 核心调度器，PatchFlow 的"心脏"

串联所有子模块：
  "项目感知 → 生成 → 验证 → 精准分析 → 策略选择 → 约束修复 → 再验证"

Phase 4 增强（V0.4）：
  5. FixLoopBreaker — 独立熔断器（同错误重复/策略失败/超限熔断）
  6. Diff 报告 — 修复完成后展示变更
  7. LLM 重试 — 指数退避，网络/限流错误自动重试
"""

from pathlib import Path

from patchflow.core.fix.generator import generate, write_files
from patchflow.core.fix.validator import validate
from patchflow.core.analysis.error_analyzer import analyze as analyze_error
from patchflow.core.fix.scope_calculator import DepGraph, calculate as calculate_scope
from patchflow.core.analysis.strategy_selector import select_strategy, strategy_sequence
from patchflow.core.fix.fixer import fix, apply_fix
from patchflow.core.fix.snapshot_manager import SnapshotManager
from patchflow.core.project.context_collector import ContextCollector, build_context_prompt
from patchflow.core.fix.breaker import FixLoopBreaker
from patchflow.utils import logger
from patchflow.utils.diff import diff_text, format_summary


class Orchestrator:
    """主调度器"""

    def __init__(self, model: str | None = None,
                 max_retries: int = 3, work_dir: str = "."):
        from patchflow.core.config import get_model
        self.model = model or get_model()
        self.max_retries = max_retries
        self.work_dir = work_dir
        self.snapshot = SnapshotManager(work_dir)

        self.dep_graph = DepGraph(work_dir)
        self._context_prompt = None
        self.breaker = FixLoopBreaker(max_retries=max_retries)
        self._diff_report = []

        self.state = {
            "turn": 0,
            "transition": None,
            "snapshot_id": None,
            "files_written": [],
            "strategy_tried": [],
        }

    def _get_context_prompt(self) -> str:
        if self._context_prompt is None:
            collector = ContextCollector(self.work_dir)
            ctx = collector.collect(use_cache=True)
            self._context_prompt = build_context_prompt(ctx)
            logger.info(f"项目上下文: {ctx}")
        return self._context_prompt

    @property
    def diff_summary(self) -> str:
        return "; ".join(self._diff_report) if self._diff_report else "no changes"

    def run(self, task: str) -> bool:
        """执行完整的"项目感知 → 生成 → 验证 → 修复"闭环"""
        logger.info("=" * 50)
        logger.info("PatchFlow Orchestrator V0.4 启动")
        logger.info(f"  任务: {task}")
        logger.info(f"  模型: {self.model}")
        logger.info(f"  最大重试: {self.max_retries}")
        logger.info("=" * 50)

        context_prompt = self._get_context_prompt()
        logger.info(f"  Context: {self.work_dir}")

        # ── Phase 1: 生成代码（注入上下文）──
        files = generate(task, model=self.model, project_context=context_prompt)
        if files is None:
            logger.error("代码生成失败，终止")
            return False

        written = write_files(files, work_dir=self.work_dir)
        self.state["files_written"] = written

        # ── Phase 2: 保存快照（记录原始文件内容用于 diff）──
        self.state["snapshot_id"] = self.snapshot.save(written)
        original_files = {}
        for f in written:
            p = Path(f)
            if p.exists():
                original_files[f] = p.read_text(encoding="utf-8")

        # ── Phase 3: 验证 + 修复循环 ──
        while self.breaker.turn < self.max_retries:

            result = validate(work_dir=self.work_dir)

            if result.ok:
                self.snapshot.commit(self.state["snapshot_id"])
                self._generate_diff_report(original_files)
                logger.success(f"验证通过！经过 {self.breaker.turn} 轮修复")
                if self._diff_report:
                    logger.info(f"  变更: {self.diff_summary}")
                return True

            logger.warn(f"第 {self.breaker.turn + 1} 轮验证失败")

            error = result.error
            if error is None:
                logger.error("验证失败但没有错误信息，无法继续")
                self.snapshot.rollback(self.state["snapshot_id"])
                return False

            # ── 精准分析 ──
            analysis = analyze_error(error.raw, work_dir=self.work_dir)
            logger.info(f"  ErrorAnalyzer: {analysis.type} (置信度: {analysis.confidence})")
            logger.info(f"  根因: {analysis.root_cause}")

            # ── 熔断检查（FixLoopBreaker）──
            should_retry, reason = self.breaker.should_retry(
                analysis.type, analysis.root_cause
            )
            if not should_retry:
                logger.error(f"熔断: {reason}")
                self.snapshot.rollback(self.state["snapshot_id"])
                return False

            # ── 计算范围 + 选择策略 ──
            scope_strategies = strategy_sequence(analysis.type)
            current_strategy_scope = self.state.get("strategy_level", 0)

            if current_strategy_scope >= len(scope_strategies):
                logger.error("所有策略都已尝试，放弃修复")
                self.snapshot.rollback(self.state["snapshot_id"])
                return False

            preferred_scope = scope_strategies[current_strategy_scope]

            if self.state["turn"] == 0:
                try:
                    self.dep_graph.build()
                except Exception:
                    pass

            scope_result = calculate_scope(analysis, dep_graph=self.dep_graph)
            logger.info(f"  ScopeCalculator: {scope_result.strategy} ({len(scope_result.files)} 文件)")

            strategy = select_strategy(analysis.type, impact_file_count=len(scope_result.files))
            strategy_name = f"{preferred_scope}/{strategy['scope']}"
            logger.info(f"  StrategySelector: {strategy['scope']} 范围")

            # ── 确定修复目标 ──
            target_file = ""
            if scope_result.files:
                target_file = scope_result.files[0]
            if not target_file:
                target_file = analysis.impact_files[0] if analysis.impact_files else ""
            if not target_file:
                target_file = written[0] if written else ""

            if not target_file:
                logger.error("无法确定修复目标文件")
                self.snapshot.rollback(self.state["snapshot_id"])
                return False

            # ── 执行修复（注入上下文 + Scope 硬约束）──
            fix_result = fix(
                error_text=analysis.raw,
                file_path=target_file,
                model=self.model,
                scope=scope_result,
                project_context=context_prompt,
            )

            if fix_result is None:
                logger.warn(f"修复生成失败，升级策略 [{strategy_name}]")
                self.state["strategy_level"] = current_strategy_scope + 1
                self.state["strategy_tried"].append(strategy_name)
                self.breaker.record_failure(analysis.type, analysis.root_cause)
                self.snapshot.rollback(self.state["snapshot_id"])
                self.state["snapshot_id"] = self.snapshot.save(written)
                continue

            if not apply_fix(fix_result, work_dir=self.work_dir):
                logger.warn(f"修复应用失败，升级策略 [{strategy_name}]")
                self.state["strategy_level"] = current_strategy_scope + 1
                self.state["strategy_tried"].append(strategy_name)
                self.breaker.record_failure(analysis.type, analysis.root_cause)
                self.snapshot.rollback(self.state["snapshot_id"])
                self.state["snapshot_id"] = self.snapshot.save(written)
                continue

            self.state["turn"] += 1
            self.state["transition"] = "next_turn"
            self.state["strategy_tried"].append(strategy_name)
            self.state["error_history"] = self.breaker.error_history
            logger.info(f"进入第 {self.breaker.turn + 1} 轮 (策略: {strategy_name})...")

        logger.error(f"已达到最大重试次数 ({self.max_retries})，回滚并退出")
        if self.state.get("strategy_tried"):
            logger.info(f"已尝试策略: {', '.join(self.state['strategy_tried'])}")
        self.snapshot.rollback(self.state["snapshot_id"])
        return False

    def _generate_diff_report(self, original_files: dict[str, str]):
        """生成修复前后的 diff 报告"""
        self._diff_report = []
        for filepath, original in original_files.items():
            current = Path(filepath).read_text(encoding="utf-8") if Path(filepath).exists() else ""
            diff = diff_text(original, current, context_lines=2)
            if diff.strip():
                summary = format_summary(diff)
                self._diff_report.append(f"{Path(filepath).name}: {summary}")
