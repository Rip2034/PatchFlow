"""Orchestrator — 核心调度器，PatchFlow 的"心脏"

这是 PatchFlow 最重要的模块。它串联整个"生成→验证→修复"闭环：

  "项目感知 → 生成 → 验证 → 精准分析 → 策略选择 → 约束修复 → 再验证"

整个流程不需要人工介入（但用户随时可以中断）。

流程详解：
  Phase 1: 生成代码
    - 收集项目上下文（技术栈、依赖、代码风格）
    - 调用 LLM Generator 生成代码文件

  Phase 2: 保存快照
    - 记录修改前的文件内容（安全网，失败后回滚）
    - 生成原始/修改后的 diff 报告

  Phase 3: 验证 + 修复循环（核心）
    1. 运行代码 → 如果通过 → 成功结束
    2. 报错 → ErrorAnalyzer 分析根因
    3. FixLoopBreaker 检查是否应该熔断
    4. ScopeCalculator 计算修复范围
    5. StrategySelector 选择修复策略
    6. Fixer 调用 LLM 修复代码
    7. 回到第 1 步

V0.4 增强特性：
  - FixLoopBreaker — 独立熔断器（同错误重复/策略失败/超限熔断）
  - Diff 报告 — 修复完成后展示变更
  - LLM 重试 — 指数退避，网络/限流错误自动重试
"""

from pathlib import Path

from patchflow.core.analysis.error_analyzer import analyze as analyze_error
from patchflow.core.analysis.strategy_selector import select_strategy, strategy_sequence
from patchflow.core.fix.breaker import FixLoopBreaker
from patchflow.core.fix.change_set import ChangeSet
from patchflow.core.fix.fixer import apply_fix, fix, fix_multi
from patchflow.core.fix.generator import generate, write_files
from patchflow.core.fix.memory_bank import FixMemoryBank
from patchflow.core.fix.patch_applicator import DiffTracker
from patchflow.core.fix.scope_calculator import DepGraph
from patchflow.core.fix.scope_calculator import calculate as calculate_scope
from patchflow.core.fix.snapshot_manager import SnapshotManager
from patchflow.core.fix.validator import validate
from patchflow.core.project.context_collector import ContextCollector, build_context_prompt
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
        self.code_graph = None  # 延迟构建（首次 run 时）
        self._context_prompt = None
        self.memory_bank = FixMemoryBank(work_dir=work_dir)
        self.memory_bank.load()
        self.breaker = FixLoopBreaker(max_retries=max_retries, memory_bank=self.memory_bank)
        self.change_set = ChangeSet(work_dir=work_dir, dep_graph=self.dep_graph)
        self.diff_tracker = DiffTracker()
        self._diff_report: list[str] = []

        self.state: dict = {
            "turn": 0,
            "transition": None,
            "snapshot_id": None,
            "files_written": [],
            "strategy_tried": [],
            "error_history": [],
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
        """执行完整的"项目感知 → 生成 → 验证 → 修复"闭环

        这就是 Orchestrator 的核心流程：

        Phase 1: 项目感知 + 代码生成
          - 收集项目上下文（技术栈、依赖、代码风格）
          - 调用 LLM Generator 生成初始代码

        Phase 2: 快照保存
          - 记录原始文件内容，用于 diff 和回滚

        Phase 3: 验证 + 修复循环
          - 循环执行直到：
            a) 代码通过验证 → 成功
            b) 熔断器触发 → 回滚失败
            c) 所有策略用完 → 回滚失败
        """
        logger.info("=" * 50)
        logger.info("PatchFlow Orchestrator V0.4 启动")
        logger.info(f"  任务: {task}")
        logger.info(f"  模型: {self.model}")
        logger.info(f"  最大重试: {self.max_retries}")
        logger.info("=" * 50)

        # Prompt 注入扫描
        try:
            from patchflow.core.fix.prompt_guard import scan as scan_injection
            injection = scan_injection(task, source="orchestrator_task")
            if injection.blocked:
                logger.error(f"[PromptGuard] 任务被拦截: {injection.reason}")
                return False
            if injection.suspicious and injection.sanitized:
                logger.warn(f"[PromptGuard] 任务已清洗: {injection.reason}")
                task = injection.sanitized
        except Exception:
            pass

        # 启动会话级 Token 预算追踪
        try:
            from patchflow.core.fix.budget import start_session_budget
            self._budget = start_session_budget()
            logger.info(f"  Token Budget: {self._budget.limit}")
        except Exception:
            self._budget = None

        context_prompt = self._get_context_prompt()
        logger.info(f"  Context: {self.work_dir}")

        # ── Phase 1: 生成代码（注入项目上下文，让 AI 了解技术栈和代码风格）──
        files = generate(task, model=self.model, project_context=context_prompt)
        if files is None:
            logger.error("代码生成失败，终止")
            return False

        written = write_files(files, work_dir=self.work_dir)
        self.state["files_written"] = written

        # ── Phase 2: 保存快照（记录原始文件内容，用于后续 diff 和回滚）──
        self.state["snapshot_id"] = self.snapshot.save(written)
        original_files = {}
        for f in written:
            p = Path(f)
            if p.exists():
                original_files[f] = p.read_text(encoding="utf-8")

        # ── Phase 3: 验证 + 修复循环（核心逻辑）──
        # 循环条件：熔断器 turn < max_retries
        # 每次循环：验证 → 分析 → 策略选择 → 修复 → 再验证
        while self.breaker.turn < self.max_retries:

            # Step 1: 运行代码验证（真正执行，不是静态检查）
            result = validate(work_dir=self.work_dir)

            # 验证通过 → 提交快照、生成 diff 报告、成功结束
            if result.ok:
                self.snapshot.commit(self.state["snapshot_id"])
                self._generate_diff_report(original_files)
                logger.success(f"验证通过！经过 {self.breaker.turn} 轮修复")
                if self._diff_report:
                    logger.info(f"  变更: {self.diff_summary}")
                if self._budget:
                    logger.info(f"  {self._budget.summary()}")
                return True

            logger.warn(f"第 {self.breaker.turn + 1} 轮验证失败")

            # 验证失败但没有错误信息 → 无法继续修复
            error = result.error
            if error is None:
                logger.error("验证失败但没有错误信息，无法继续")
                self.snapshot.rollback(self.state["snapshot_id"])
                return False

            # Step 2: 精准错误分析（ErrorAnalyzer）
            # 解析 traceback → 定位错误类型 + 根因 + 影响文件
            analysis = analyze_error(error.raw, work_dir=self.work_dir)
            logger.info(f"  ErrorAnalyzer: {analysis.type} (置信度: {analysis.confidence})")
            logger.info(f"  根因: {analysis.root_cause}")

            # Step 3: 熔断检查（FixLoopBreaker）
            # 检查是否进入死循环（同一错误重复出现 / 策略连续失败）
            should_retry, reason = self.breaker.should_retry(
                analysis.type, analysis.root_cause
            )
            if not should_retry:
                logger.error(f"熔断: {reason}")
                self.memory_bank.save()
                self.snapshot.rollback(self.state["snapshot_id"])
                return False

            # Step 4: 计算修复范围 + 选择修复策略
            # strategy_sequence 返回策略升级序列（从窄到宽：line → chain → callchain → business）
            scope_strategies = strategy_sequence(analysis.type)
            current_strategy_scope = self.state.get("strategy_level", 0)

            # 所有策略都已尝试过 → 放弃修复
            if current_strategy_scope >= len(scope_strategies):
                logger.error("所有策略都已尝试，放弃修复")
                self.memory_bank.save()
                self.snapshot.rollback(self.state["snapshot_id"])
                return False

            preferred_scope = scope_strategies[current_strategy_scope]

            # 首次循环时构建依赖图（用于 Scope 计算）
            if self.state["turn"] == 0:
                try:
                    self.dep_graph.build()
                except Exception as e:
                    logger.debug(f"Dep graph 构建失败（非致命）: {e}")

                # 构建语义代码图谱（函数/类级别，优先使用）
                if self.code_graph is None:
                    try:
                        from patchflow.core.language_registry import LanguageRegistry
                        from patchflow.core.fix.code_graph import CodeGraph
                        lang = LanguageRegistry().detect(str(self.work_dir))
                        if lang:
                            self.code_graph = CodeGraph(str(self.work_dir), lang)
                            logger.info(f"[Orch] CodeGraph built: {len(self.code_graph.files)} files, "
                                        f"{len(self.code_graph.symbols)} symbols")
                    except Exception as e:
                        logger.debug(f"CodeGraph 构建失败（非致命）: {e}")

            # 计算受影响的文件范围（优先使用语义图谱）
            scope_result = calculate_scope(analysis, dep_graph=self.dep_graph,
                                           code_graph=self.code_graph)
            logger.info(f"  ScopeCalculator: {scope_result.strategy} ({len(scope_result.files)} 文件)")

            # 选择具体的修复策略
            strategy = select_strategy(analysis.type, impact_file_count=len(scope_result.files))
            strategy_name = f"{preferred_scope}/{strategy['scope']}"
            logger.info(f"  StrategySelector: {strategy['scope']} 范围")

            # 确定修复目标文件（优先级：Scope > ErrorAnalyzer > 生成的文件）
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

            # 查询记忆库：是否有相似历史修复作为上下文
            memory_context = ""
            similar = self.memory_bank.query(analysis.type, analysis.root_cause)
            if similar:
                parts = []
                for m in similar[:3]:
                    status = "ok" if m.success else "failed"
                    parts.append(f"  [{status}] {m.fix_pattern[:80]}")
                memory_context = "Similar past fixes:\n" + "\n".join(parts)

            # Step 5: 执行修复（Fixer）
            # 多文件 scope 则用 fix_multi，否则用 fix（注入项目上下文 + Scope 硬约束）
            if len(scope_result.files) > 1:
                multi_changes = fix_multi(
                    error_text=analysis.raw,
                    scope_files=scope_result.files,
                    model=self.model,
                    project_context=context_prompt + ("\n" + memory_context if memory_context else ""),
                )
                if multi_changes:
                    self.change_set.changes.clear()
                    for ch in multi_changes:
                        self.change_set.add(ch.get("file", target_file),
                                           ch.get("content", ""),
                                           ch.get("reason", ""))
                    self.change_set.expand_with_dependents()
                    self.change_set.begin()
                    applied = self.change_set.apply_all()
                    fix_success = applied > 0
                else:
                    fix_success = False
            else:
                fix_result = fix(
                    error_text=analysis.raw,
                    file_path=target_file,
                    model=self.model,
                    scope=scope_result,
                    project_context=context_prompt + ("\n" + memory_context if memory_context else ""),
                )
                if fix_result and apply_fix(fix_result, work_dir=self.work_dir):
                    fix_success = True
                    p = Path(target_file)
                    if p.exists():
                        try:
                            self.diff_tracker.record(
                                target_file,
                                fix_result.get("old_content", ""),
                                fix_result["content"]
                            )
                        except Exception:
                            pass
                else:
                    fix_success = False

            # 修复失败 → 升级策略（尝试更广的修复范围）
            if not fix_success:
                logger.warn(f"修复失败，升级策略 [{strategy_name}]")
                self.state["strategy_level"] = current_strategy_scope + 1
                self.state["strategy_tried"].append(strategy_name)
                self.breaker.record_failure(analysis.type, analysis.root_cause)
                self.memory_bank.add(
                    error_type=analysis.type, root_cause=analysis.root_cause,
                    fix_pattern=f"failed: {analysis.root_cause[:100]}",
                    file_paths=scope_result.files,
                    success=False, strategy_used=strategy_name,
                )
                if self.change_set._current_snapshot_id:
                    self.change_set.rollback()
                else:
                    self.snapshot.rollback(self.state["snapshot_id"])
                self.state["snapshot_id"] = self.snapshot.save(written)
                continue

            # 本轮修复成功 → 记录 memory，进入下一轮验证
            self.memory_bank.add(
                error_type=analysis.type, root_cause=analysis.root_cause,
                fix_pattern=f"fixed: {analysis.root_cause[:100]}",
                file_paths=scope_result.files,
                success=True, strategy_used=strategy_name,
            )
            self.memory_bank.save()
            self.change_set.commit()

            self.state["turn"] += 1
            self.state["transition"] = "next_turn"
            self.state["strategy_tried"].append(strategy_name)
            self.state["error_history"] = self.breaker.error_history
            logger.info(f"进入第 {self.breaker.turn + 1} 轮 (策略: {strategy_name})...")

        logger.error(f"已达到最大重试次数 ({self.max_retries})，回滚并退出")
        if self.state.get("strategy_tried"):
            logger.info(f"已尝试策略: {', '.join(self.state['strategy_tried'])}")
        self.memory_bank.save()
        self.snapshot.rollback(self.state["snapshot_id"])
        if self._budget:
            logger.info(f"  {self._budget.summary()}")
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
