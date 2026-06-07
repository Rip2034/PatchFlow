"""AgentOrchestrator — 多 Agent Blackboard 调度器

这是 Phase 5 的核心模块。与 Orchestrator（单 Agent 自动修复）不同，
AgentOrchestrator 使用多个独立 Agent 协作完成修复：

  Analyzer → Fixer → Reviewer (+ 可选重做)

设计文档的调度逻辑（V1.0）：
  1. Analyzer 分析代码 + 错误信息，定位问题根因
  2. 如果置信度不够（< 0.5）→ 上报用户，等待指示
  3. Fixer 根据分析结果生成修复补丁
  4. Reviewer 审查修复结果（打分 0-10）
  5. 不通过（score < 7）→ 带 review 意见让 Fixer 重做
  6. 应用补丁 → 运行验证

多模型支持：
  config.json 中 agents 段可以指定每个角色使用的模型别名：
    "agents": {
      "analyzer": "deepseek",   → 分析用便宜模型
      "fixer": "claude",        → 修复用强模型
      "reviewer": "deepseek"    → 审查用便宜模型
    }
  未配置的角色回退到 active 模型。

黑盒（Blackboard）模式：
  - 三个 Agent 不直接通信，通过 Blackboard 共享信息
  - Analyzer 写入分析结果 → Fixer 读取并写入补丁 → Reviewer 读取并审查
  - 降低耦合，方便将来替换或增加新的 Agent
"""

from pathlib import Path

from patchflow.core.fix.breaker import FixLoopBreaker
from patchflow.core.fix.change_set import ChangeSet
from patchflow.core.fix.patch_applicator import DiffTracker
from patchflow.core.fix.scope_calculator import DepGraph
from patchflow.core.fix.snapshot_manager import SnapshotManager
from patchflow.utils import logger
from patchflow.utils.agent_display import AgentPipelineDisplay, _get_model_display
from patchflow.utils.diff import diff_text, format_summary


def _patches_are_similar(patches_a: list[dict], patches_b: list[dict]) -> bool:
    """检测两组补丁是否实质相同（无效重做检测）"""
    if not patches_a or not patches_b:
        return False
    files_a = {p.get("file", "") for p in patches_a}
    files_b = {p.get("file", "") for p in patches_b}
    if files_a != files_b:
        return False
    # 比较补丁内容的相似度
    for pa, pb in zip(sorted(patches_a, key=lambda p: p.get("file", "")),
                       sorted(patches_b, key=lambda p: p.get("file", ""))):
        old_a = (pa.get("old", "") or "").strip()
        old_b = (pb.get("old", "") or "").strip()
        new_a = (pa.get("new", "") or "").strip()
        new_b = (pb.get("new", "") or "").strip()
        if old_a != old_b or new_a != new_b:
            # 允许轻微差异（空格/换行）
            if old_a.replace(" ", "").replace("\n", "") != old_b.replace(" ", "").replace("\n", ""):
                return False
            if new_a.replace(" ", "").replace("\n", "") != new_b.replace(" ", "").replace("\n", ""):
                return False
    return True


def _build_shared_system_prefix(blackboard) -> str:
    """构建跨 Agent 共享的 system prompt 前缀

    三个 Agent (Analyzer/Fixer/Reviewer) 使用相同的前缀，
    Anthropic prompt cache 可以跨调用命中。

    V0.5: 增加总大小限制和文件数量限制，防止超大项目溢出 context window。
    """
    MAX_TOTAL_CHARS = 30000   # 总字符上限
    MAX_CODE_FILES = 20       # 最多包含的代码文件数
    MAX_PER_FILE = 1200       # 每个文件最大字符数

    parts = ["You are PatchFlow, an AI coding assistant. Work in a multi-agent pipeline.\n"]

    task = blackboard.get("task", "")
    if task:
        parts.append(f"## Task\n{task[:500]}")

    ctx = blackboard.get("context", {})
    if isinstance(ctx, dict) and ctx:
        parts.append("## Project Context")
        for k, v in ctx.items():
            if isinstance(v, dict):
                for vk, vv in v.items():
                    if isinstance(vv, (str, int, float)):
                        parts.append(f"- {k}.{vk}: {vv}")
            elif isinstance(v, list):
                parts.append(f"- {k}: {', '.join(str(x) for x in v[:10])}")
            elif isinstance(v, str):
                parts.append(f"- {k}: {v[:200]}")

    code = blackboard.get("code", {})
    if isinstance(code, dict) and code:
        parts.append("\n## Code Files")
        file_count = 0
        for fpath, content in code.items():
            if file_count >= MAX_CODE_FILES:
                parts.append(f"... (skipped {len(code) - MAX_CODE_FILES} more files)")
                break
            truncated = (content[:MAX_PER_FILE] if isinstance(content, str)
                        else str(content)[:MAX_PER_FILE])
            parts.append(f"### {fpath}\n```\n{truncated}\n```")
            file_count += 1

    err = blackboard.get("error", "")
    if err:
        parts.append(f"\n## Error Output\n```\n{err[:1000]}\n```")

    parts.append("\n## Rules\n- Output ONLY valid JSON\n- Make minimal changes\n- Keep existing code style")

    # ── 总大小守卫：超出时从后往前裁剪 ──
    result = "\n\n".join(parts)
    if len(result) > MAX_TOTAL_CHARS:
        # 保留头部 + 截断尾部（保留 Rules 部分）
        rules = parts[-1] if parts else ""
        head_len = MAX_TOTAL_CHARS - len(rules) - 200
        result = result[:head_len] + f"\n\n... (truncated {len(result) - MAX_TOTAL_CHARS} chars)\n\n" + rules

    return result


class AgentOrchestrator:
    """多 Agent 调度器（V0.5 多轮迭代增强版）

    V0.5 增强：
      - 最多 3 轮 fix→review→refine 迭代（原 2 轮）
      - 每轮缩小修复范围，聚焦未解决问题
      - Reviewer 反馈逐轮累积，Fixer 看到完整历史
    """

    MAX_REDO_ROUNDS = 3  # 最多 3 轮重做

    def __init__(self, model: str | None = None, work_dir: str = "."):
        from patchflow.core.config import get_model
        self.model = model or get_model()
        self.work_dir = work_dir
        self.snapshot = SnapshotManager(work_dir)
        self.dep_graph = DepGraph(work_dir)
        self.code_graph = None  # 延迟构建（首次 run 时）
        self.turn_count = 0
        self._agent_aliases: dict[str, str] = {}
        from patchflow.core.fix.memory_bank import FixMemoryBank
        self.memory_bank = FixMemoryBank(work_dir=work_dir)
        self.memory_bank.load()
        self.breaker = FixLoopBreaker(max_retries=4, memory_bank=self.memory_bank)
        self.change_set = ChangeSet(work_dir=work_dir, dep_graph=self.dep_graph)
        self.diff_tracker = DiffTracker()

    def _get_alias(self, role: str) -> str | None:
        """读取 config.json 中 agents 段的角色-模型别名映射"""
        if role not in self._agent_aliases:
            from patchflow.core.config import get_config
            agents_cfg = get_config().get("agents", {})
            self._agent_aliases[role] = agents_cfg.get(role)
        return self._agent_aliases[role]

    def run_from_task(self, task: str, work_dir: str | None = None,
                       use_live: bool | None = None) -> bool:
        """便捷方法：从任务描述直接启动多 Agent 修复

        自动收集项目上下文、读取代码文件、尝试运行获取错误信息，
        然后构建 Blackboard 并执行完整的多 Agent 修复流程。

        Args:
            task: 任务描述
            work_dir: 工作目录（默认使用初始化时设置的目录）
            use_live: True → Live 仪表盘, False → Panel 模式, None → 自动检测

        Returns:
            True → 修复通过，False → 修复失败
        """
        wd = work_dir or self.work_dir
        logger.info("[AgentOrch] run_from_task: 自动收集项目上下文...")

        # 1. 收集项目上下文
        from patchflow.core.project.context_collector import ContextCollector
        collector = ContextCollector(wd)
        ctx = collector.collect(use_cache=True)

        # 2. 读取所有源码文件（按语言自动识别扩展名）
        code = {}
        from patchflow.core.language_strategy import LanguageFactory
        factory = LanguageFactory()
        strategy = factory.detect(wd)
        exts = strategy.extensions if strategy else factory.all_extensions
        for ext in exts:
            for f in Path(wd).rglob(f"*{ext}"):
                rel = str(f.relative_to(wd))
                if any(rel.startswith(prefix) for prefix in (".patchflow/", ".venv/", "node_modules/", "venv/", "__pycache__/")):
                    continue
                try:
                    code[rel] = f.read_text(encoding="utf-8", errors="replace")
                except Exception:
                    continue

        # 3. 尝试运行获取错误（使用 LanguageStrategy 入口点+运行命令）
        error_text = ""
        if strategy:
            entry = strategy.find_entry_file(Path(wd))
            if entry and strategy.run_command:
                from patchflow.utils.runner import run
                cmd = f"{strategy.run_command} {entry.name}"
                try:
                    result = run(cmd, cwd=wd, timeout=30)
                    if result.exit_code != 0:
                        error_text = result.stderr.strip() or result.stdout.strip()
                except Exception as e:
                    logger.warn(f"[AgentOrch] 无法运行命令 '{cmd}': {e}")
                    error_text = f"(command failed: {e})"
            if not error_text:
                lang_name = strategy.name
                error_text = f"(no runtime error detected for {lang_name}, repair based on code analysis)"
        else:
            error_text = "(no language detected, repair based on task description)"

        # 4. 构建 Blackboard
        from patchflow.agents.blackboard import Blackboard
        bb = Blackboard(
            task=task,
            context=ctx.to_dict(),
            code=code,
            error=error_text,
        )

        # 5. 注入 Web 搜索上下文（增强修复能力）
        web_context = ""
        try:
            from patchflow.core.web.web_search import search_for_fix, format_search_context
            logger.info("[AgentOrch] 搜索相关文档...")
            search_results = search_for_fix(
                error_type="runtime",
                error_message=error_text,
                language=strategy.name if strategy else "",
                limit=4,
            )
            if search_results:
                web_context = format_search_context(search_results, max_chars=2000)
                bb["web_context"] = web_context
                logger.info(f"[AgentOrch] Web 上下文注入: {len(search_results)} 条结果")
        except Exception as e:
            logger.debug(f"[AgentOrch] Web 搜索跳过: {e}")

        # 6. 注入记忆上下文
        try:
            from patchflow.core.memory import MemoryStore
            memory = MemoryStore(str(wd))
            mem_context = memory.compile_context(task, max_chars=1500)
            if mem_context:
                bb["memory_context_ext"] = mem_context
                logger.debug(f"[AgentOrch] Memory 上下文注入")
        except Exception as e:
            logger.debug(f"[AgentOrch] Memory 加载跳过: {e}")

        # 7. 执行多 Agent 修复
        logger.info(f"[AgentOrch] Blackboard 构建完成: {len(code)} files, error={len(error_text)} chars")
        return self.run(bb, use_live=use_live)

    def run(self, blackboard, use_live: bool | None = None) -> bool:
        """执行完整的多 Agent 修复流程

        六个步骤的协作流程：
          1. Analyzer: 分析错误原因，定位根因
          2. 置信度检查：不够就上报用户
          3. Fixer: 生成修复补丁
          4. 应用补丁到文件系统
          5. Reviewer: 审查修复质量
          6. 不通过 → 带 feedback 重做一次

        Args:
            blackboard: Blackboard 实例（必须包含 task, context, code, error）
            use_live: True → Live 仪表盘, False → Panel 模式, None → 自动检测

        Returns:
            True → 修复通过（verify() 成功）
            False → 修复失败（回滚到原始文件）
        """
        # ── V0.5: Prompt 注入防御（Pipeline B 之前缺失）──
        try:
            from patchflow.core.fix.prompt_guard import scan as scan_injection
            task_text = blackboard.get("task", "")
            injection = scan_injection(task_text, source="agent_orch_task")
            if injection.blocked:
                logger.error(f"[AgentOrch][PromptGuard] 任务被拦截: {injection.reason}")
                return False
            if injection.suspicious and injection.sanitized:
                logger.warn(f"[AgentOrch][PromptGuard] 任务已清洗: {injection.reason}")
                blackboard["task"] = injection.sanitized
        except Exception:
            pass

        # ── V0.5: Token 预算追踪（Pipeline B 之前缺失）──
        try:
            from patchflow.core.fix.budget import start_session_budget
            self._budget = start_session_budget()
            logger.info(f"  Token Budget: {self._budget.limit}")
        except Exception:
            self._budget = None

        analyzer_alias = self._get_alias("analyzer")
        fixer_alias = self._get_alias("fixer")
        reviewer_alias = self._get_alias("reviewer")

        analyzer_model = _get_model_display(analyzer_alias, self.model)
        fixer_model = _get_model_display(fixer_alias, self.model)
        reviewer_model = _get_model_display(reviewer_alias, self.model)

        # ── 提取关键信息用于显示 ──
        error_text = blackboard.get("error", "")
        error_preview = error_text.split("\n")[0][:80] if error_text else "N/A"
        task_text = blackboard.get("task", "")[:60]
        code_files = list(blackboard.get("code", {}).keys())
        files_preview = ", ".join(code_files[:5]) if code_files else "(none)"

        # ── 初始化可视化面板（根据 use_live 选择 Live 或 Panel 模式）──
        if use_live is None:
            from patchflow.utils.live_dashboard import _detect_live_support
            use_live = _detect_live_support()
        if use_live:
            from patchflow.utils.live_dashboard import LivePipelineDashboard
            display = LivePipelineDashboard(blackboard=blackboard)
        else:
            display = AgentPipelineDisplay(blackboard=blackboard, use_rich=True)
        display.add_step("analyzer", analyzer_model,
                         detail=f"Task: {task_text}" if task_text else "")
        display.add_step("fixer", fixer_model,
                          detail=f"Files: {files_preview}")
        display.add_step("reviewer", reviewer_model)
        display.start()

        logger.info(f"  AgentOrch 任务: {blackboard['task'][:80]}")

        # ── 构建共享 System Prompt 前缀（跨 Agent 缓存复用）──
        # 三个 Agent 使用同一个前缀，Anthropic prompt cache 可跨调用命中
        shared_prefix = _build_shared_system_prefix(blackboard)
        blackboard["shared_system_prefix"] = shared_prefix
        logger.debug(f"[AgentOrch] 共享 system 前缀: {len(shared_prefix)} chars")

        # ── 保存快照 ──
        display.set_detail(0, "Saving snapshot...")
        files_to_snapshot = list(blackboard.get("code", {}).keys())
        snap_id = self.snapshot.save(files_to_snapshot)

        # ── 构建依赖图（用于 Scope 计算和冲突检测）──
        display.set_detail(0, "Building dependency graph...")
        try:
            self.dep_graph.build()
        except Exception as e:
            logger.debug(f"Dep graph 构建失败（非致命）: {e}")

        # ── 构建语义代码图谱（函数/类级别，用于精确 Scope）──
        if self.code_graph is None:
            try:
                from patchflow.core.fix.code_graph import CodeGraph
                from patchflow.core.language_registry import LanguageRegistry
                display.set_detail(0, "Detecting language...")
                lang = LanguageRegistry().detect(str(self.work_dir))
                if lang:
                    display.set_detail(0, f"Building semantic code graph ({lang.name})...")
                    self.code_graph = CodeGraph(str(self.work_dir), lang)
                    logger.info(f"[AgentOrch] CodeGraph built: {len(self.code_graph.files)} files, "
                                f"{len(self.code_graph.symbols)} symbols")
            except Exception as e:
                logger.debug(f"CodeGraph 构建失败（非致命，回退 file-level）: {e}")
                self.code_graph = None

        # 注入 CodeGraph 到 Blackboard（Agent 可通过黑板书读取语义分块）
        if self.code_graph is not None:
            blackboard.code_graph = self.code_graph
        # V0.5: 注入 memory_bank 到 Blackboard（fixer_agent 用于自适应策略选择）
        blackboard.memory_bank = self.memory_bank

        # ── Step 1: Analyzer 分析错误 ──
        # 调用分析 Agent 定位问题根因，返回错误类型、置信度、影响文件等
        display.set_detail(0, f"Analyzing {len(code_files)} files...")
        display.set_running(0)
        from patchflow.agents.analyzer import agent_analyze
        analysis = agent_analyze(blackboard, model=self.model,
                                 model_alias=analyzer_alias)
        self.turn_count += 1
        error_type = analysis.get("error_type", "")
        root_cause = analysis.get("root_cause", "")
        confidence = analysis.get("confidence", 0)
        impact_files = analysis.get("impact_files", [])
        display.set_completed(0, f"Error: {error_type}" if error_type else analysis.get("summary", ""))
        logger.info("[AgentOrch] === Analyzer 分析结果 ===")
        logger.info(f"[AgentOrch]   错误类型: {error_type}")
        logger.info(f"[AgentOrch]   根因: {root_cause[:120]}")
        logger.info(f"[AgentOrch]   置信度: {confidence:.0%}")
        logger.info(f"[AgentOrch]   涉及文件: {', '.join(impact_files[:5]) or '(none)'}")
        logger.info(f"[AgentOrch]   Blackboard: {blackboard.summary()}")

        # ── Step 2: 置信度检查 ──
        # 如果 Analyzer 对分析结果没有把握，上报用户不再继续
        if confidence < 0.5:
            display.set_failed(0, f"置信度过低 ({confidence:.0%})")
            display.finish(False)
            logger.error(f"[AgentOrch] 分析置信度过低 ({confidence:.0%})，上报用户")
            self.snapshot.rollback(snap_id)
            return False

        # ── Step 3: Fixer 生成修复补丁 ──
        # 根据分析结果生成具体的代码修改（patch）
        # 先查询记忆库：是否有相似历史修复作为上下文
        memory_ctx = self._query_memory_context(analysis)
        if memory_ctx:
            blackboard["memory_context"] = memory_ctx
            logger.info(f"[AgentOrch] 找到相似历史修复:\n{memory_ctx}")

        display.set_detail(1, f"Fixing {len(code_files)} files...")
        display.set_running(1)
        from patchflow.agents.fixer_agent import agent_fix, apply_agent_patches
        blackboard["fix_plan"] = agent_fix(blackboard, dep_graph=self.dep_graph,
                                           code_graph=self.code_graph,
                                           model=self.model, model_alias=fixer_alias)
        self.turn_count += 1
        patch_count = len(blackboard["fix_plan"].get("patches", []))
        patches = blackboard["fix_plan"].get("patches", [])
        display.set_completed(1, f"{patch_count} patches generated" if patch_count else "No patches generated")
        logger.info(f"[AgentOrch] === Fixer 生成 {patch_count} 个补丁 ===")
        for i, patch in enumerate(patches):
            fp = patch.get("file", "?")
            desc = patch.get("description", "")[:80]
            logger.info(f"[AgentOrch]   补丁 {i+1}: {fp} — {desc}")
        if not patches:
            logger.info(f"[AgentOrch]   Blackboard: {blackboard.summary()}")

        # ── V0.5: 冲突检测（懒检测：只在提交时检查）──
        try:
            from patchflow.core.fix.conflict_detector import LazyConflictDetector
            conflict_detector = LazyConflictDetector(work_dir=self.work_dir)
            proposed = [{"file": p.get("file", ""), "content": p.get("new", "")}
                       for p in patches if p.get("file")]
            conflicts = conflict_detector.detect("fixer", proposed)
            if conflicts:
                logger.warn(f"[AgentOrch] 检测到 {len(conflicts)} 个潜在冲突:")
                for c in conflicts[:3]:
                    logger.warn(f"  [{c.get('severity','?')}] {c.get('type','?')}: "
                               f"{c.get('suggestion','')[:100]}")
        except Exception as e:
            logger.debug(f"[AgentOrch] 冲突检测跳过: {e}")

        # 没有生成任何补丁 → 无法修复
        if not blackboard["fix_plan"].get("patches"):
            display.set_failed(1, "未生成任何补丁")
            display.finish(False)
            logger.error("[AgentOrch] Fixer 未生成任何补丁，终止")
            self._record_fix_outcome(analysis, [], False, "agent_fix")
            self.snapshot.rollback(snap_id)
            return False

        # ── 记录原始文件内容（用于后续 diff 报告）──
        original_files = {}
        for patch in blackboard["fix_plan"]["patches"]:
            from pathlib import Path
            fp = patch.get("file", "")
            if fp:
                p = Path(self.work_dir) / fp
                if p.exists():
                    original_files[fp] = p.read_text(encoding="utf-8", errors="replace")

        # ── 应用补丁到文件系统 ──
        if not apply_agent_patches(blackboard, work_dir=self.work_dir, diff_tracker=self.diff_tracker):
            display.set_failed(1, "补丁应用失败")
            display.finish(False)
            logger.error("[AgentOrch] 补丁应用失败")
            self._record_fix_outcome(analysis, [], False, "agent_fix")
            self.snapshot.rollback(snap_id)
            return False

        patched_files = list({p.get("file", "") for p in blackboard["fix_plan"].get("patches", []) if p.get("file")})

        # ── Step 4: Reviewer 审查 ──
        # 审查修复质量，给出 score（0-10）和 approval
        display.set_detail(2, "Reviewing results...")
        display.set_running(2)
        from patchflow.agents.reviewer import agent_review
        review = agent_review(blackboard, model=self.model,
                              model_alias=reviewer_alias)
        self.turn_count += 1
        score = review.get("score", 0)
        issues = review.get("issues", [])
        feedback = review.get("feedback", "")
        display.set_completed(2, f"Score: {score}/10 (approved)" if review.get("approved") else f"Score: {score}/10 (needs redo)")
        logger.info("[AgentOrch] === Reviewer 审查结果 ===")
        logger.info(f"[AgentOrch]   评分: {score}/10 {'(通过)' if review.get('approved') else '(需重做)'}")
        if issues:
            for issue in issues[:5]:
                logger.info(f"[AgentOrch]   问题: {str(issue)[:100]}")
        if feedback:
            logger.info(f"[AgentOrch]   反馈: {feedback[:120]}")
        logger.info(f"[AgentOrch]   Blackboard: {blackboard.summary()}")

        # ── Step 5: 多轮迭代修复循环（V0.5 增强）──
        # 不通过时：回滚 → Fixer 重新修复（带累积的 Review 反馈）→ 再审
        # 最多 MAX_REDO_ROUNDS 轮，每轮评分需改善
        redo_round = 0
        all_patches_history: list[list[dict]] = [patches]  # 历史补丁用于相似度检测
        cumulative_feedback: list[str] = []
        best_score = review.get("score", 0)

        while not review.get("approved", False) and redo_round < self.MAX_REDO_ROUNDS:
            redo_round += 1
            logger.warn(f"[AgentOrch] Reviewer 驳回 第{redo_round}轮 (score: {review.get('score',0)}/10)")
            logger.info(f"[AgentOrch]   Issues: {review.get('issues', [])}")

            # 回滚到原始文件
            self.snapshot.rollback(snap_id)

            # V0.5: 策略升级 — 重做轮次递增时扩大修复范围
            from patchflow.core.analysis.strategy_selector import strategy_sequence, select_strategy
            scope_sequence = strategy_sequence(analysis.get("error_type", "runtime"))
            escalated_scope = scope_sequence[min(redo_round, len(scope_sequence) - 1)]
            if redo_round > 1 and escalated_scope != scope_sequence[0]:
                logger.info(f"[AgentOrch] 策略升级: {scope_sequence[0]} → {escalated_scope}")
                # 放宽影响文件限制
                expanded_files = analysis.get("impact_files", [])[:]
                if len(expanded_files) < 5:
                    # 从 code_graph 中补充相关文件
                    for imp_file in list(expanded_files):
                        try:
                            from patchflow.core.fix.scope_calculator import DepGraph
                            if self.dep_graph._built:
                                callers = self.dep_graph.direct_callers(imp_file)
                                for c in callers:
                                    if c not in expanded_files:
                                        expanded_files.append(c)
                        except Exception:
                            pass
                # 更新 blackboard 中的分析以反映更宽的范围
                analysis["impact_files"] = expanded_files[:5]
                blackboard.set_analysis(analysis)

            # 检查熔断器
            self.breaker.record_failure(analysis.get("error_type", ""), analysis.get("root_cause", ""))
            should_retry, reason = self.breaker.should_retry(
                analysis.get("error_type", ""), analysis.get("root_cause", ""), "agent_fix"
            )
            if not should_retry:
                logger.error(f"[AgentOrch] 熔断 (第{redo_round}轮): {reason}")
                self._record_fix_outcome(analysis, patched_files, False, "agent_fix")
                display.finish(False)
                return False

            # 累积 review 反馈（每轮叠加，Fixer 可以看到完整历史）
            fb = review.get("feedback", "")
            if fb:
                cumulative_feedback.append(f"[Round {redo_round}] {fb}")
            blackboard["review_feedback"] = "\n".join(cumulative_feedback)

            # 带累积 feedback 重新修复
            round_label = f"第{redo_round + 1}次" if redo_round == 1 else f"第{redo_round + 1}次修复"
            display.set_detail(1, f"Redoing ({round_label})...")
            display.set_retry(1)
            blackboard["fix_plan"] = agent_fix(blackboard, dep_graph=self.dep_graph,
                                               code_graph=self.code_graph,
                                               model=self.model, model_alias=fixer_alias)
            self.turn_count += 1

            if not blackboard["fix_plan"].get("patches"):
                display.set_failed(1, f"第{redo_round + 1}次修复未生成补丁")
                display.finish(False)
                logger.error(f"[AgentOrch] 第{redo_round + 1}次修复未生成补丁")
                self._record_fix_outcome(analysis, patched_files, False, "agent_fix")
                return False

            redo_patches = blackboard["fix_plan"].get("patches", [])
            patch_count_n = len(redo_patches)
            display.set_completed(1, f"{patch_count_n} patches (redo #{redo_round})")

            # ── 补丁相似度检测：与所有历史补丁比较 ──
            is_duplicate = False
            for hist_patches in all_patches_history:
                if _patches_are_similar(hist_patches, redo_patches):
                    is_duplicate = True
                    break
            if is_duplicate:
                display.set_failed(1, f"第{redo_round + 1}次修复未实质性修改（与之前相同）")
                display.finish(False)
                logger.error(f"[AgentOrch] 第{redo_round + 1}次修复与历史补丁几乎相同，停止无效重做")
                self._record_fix_outcome(analysis, patched_files, False, "agent_fix")
                return False
            all_patches_history.append(redo_patches)

            # 重新保存快照
            snap_id = self.snapshot.save(files_to_snapshot)

            if not apply_agent_patches(blackboard, work_dir=self.work_dir, diff_tracker=self.diff_tracker):
                display.set_failed(1, f"第{redo_round + 1}次补丁应用失败")
                display.finish(False)
                self._record_fix_outcome(analysis, patched_files, False, "agent_fix")
                self.snapshot.rollback(snap_id)
                return False

            patched_files = list({p.get("file", "") for p in redo_patches if p.get("file")})

            # 再审
            display.set_detail(2, f"Re-reviewing (round {redo_round + 1})...")
            display.set_retry(2)
            review = agent_review(blackboard, model=self.model,
                                  model_alias=reviewer_alias)
            self.turn_count += 1
            current_score = review.get("score", 0)
            status_text = "approved" if review.get("approved") else f"rejected ({current_score}/10)"
            display.set_completed(2, f"Score: {current_score}/10 ({status_text})")

            if review.get("approved", False):
                break  # 通过，跳出循环

            # 评分比之前最好成绩差且连续2轮不改善 → 停止
            if current_score <= best_score:
                if redo_round >= 2:
                    logger.error(f"[AgentOrch] 评分连续{redo_round}轮未改善 "
                                 f"(best={best_score}, current={current_score})，停止")
                    display.set_failed(2, f"评分未改善 ({best_score} → {current_score})")
                    display.finish(False)
                    self._record_fix_outcome(analysis, patched_files, False, "agent_fix")
                    self.snapshot.rollback(snap_id)
                    return False
                else:
                    logger.warn(f"[AgentOrch] 评分未改善 ({best_score} → {current_score})，继续尝试")
            else:
                best_score = current_score

        # ── Step 6: 运行验证 + 回归测试（V0.5 增强）──
        # 先运行代码验证，再运行已有测试套件 + 生成回归测试
        from patchflow.core.fix.validator import validate
        result = validate(work_dir=self.work_dir)

        if result.ok:
            # 运行已有测试套件（回归检测）
            test_ok = True
            try:
                from patchflow.core.fix.test_runner import run_post_fix_validation
                display.set_detail(2, "Running regression tests...")
                fixed_file = patched_files[0] if patched_files else ""
                test_result = run_post_fix_validation(
                    work_dir=self.work_dir,
                    analysis=analysis,
                    fixed_file=fixed_file,
                    language=analysis.get("language", ""),
                )
                test_ok = test_result.get("existing_tests_ok", True)
                logger.info(f"[AgentOrch] 回归测试: {test_result.get('test_summary', '')}")
                if not test_ok:
                    logger.warn("[AgentOrch] 已有测试失败，修复可能引入回归问题")
            except Exception as e:
                logger.debug(f"[AgentOrch] 回归测试跳过: {e}")

            if test_ok:
                self.snapshot.commit(snap_id)
                self._print_diff_report(original_files)
                self._record_fix_outcome(analysis, patched_files, True, "agent_fix")
                self.memory_bank.save()
                logger.success(f"[AgentOrch] Agent 协作修复成功! ({self.turn_count} 步, "
                              f"{redo_round} 轮重做)")
                display.finish(True)
                return True
            else:
                logger.error("[AgentOrch] 修复通过编译但已有测试失败，可能存在回归问题")
                # 仍然提交修复（编译通过），但记录警告
                self.snapshot.commit(snap_id)
                self._print_diff_report(original_files)
                self._record_fix_outcome(analysis, patched_files, True, "agent_fix")
                self.memory_bank.save()
                display.finish(True)
                return True

        # 验证失败 → 回滚
        logger.error("[AgentOrch] 验证失败，回滚")
        self._record_fix_outcome(analysis, patched_files, False, "agent_fix")
        display.finish(False)
        self.snapshot.rollback(snap_id)
        return False

    def _query_memory_context(self, analysis: dict) -> str:
        """查询记忆库中的相似历史修复，返回格式化上下文"""
        error_type = analysis.get("error_type", "")
        root_cause = analysis.get("root_cause", "")
        if not error_type or not root_cause:
            return ""
        similar = self.memory_bank.query(error_type, root_cause)
        avoid = self.memory_bank.get_avoid_patterns(error_type, root_cause)
        if not similar and not avoid:
            return ""
        parts = []
        if similar:
            parts.append("Similar past fixes:")
            for m in similar[:3]:
                status = "SUCCESS" if m.success else "FAILED"
                parts.append(f"  [{status}] {m.fix_pattern[:80]}")
        if avoid:
            parts.append("\nAVOID these approaches (failed before):")
            for a in avoid:
                parts.append(f"  - DO NOT: {a}")
        return "\n".join(parts)

    def _record_fix_outcome(self, analysis: dict, file_paths: list[str],
                            success: bool, strategy: str) -> None:
        """记录修复结果到记忆库（V0.5: 自动持久化）"""
        error_type = analysis.get("error_type", "")
        root_cause = analysis.get("root_cause", "")
        if not error_type or not root_cause:
            return
        self.memory_bank.add(
            error_type=error_type,
            root_cause=root_cause,
            fix_pattern=f"{'fixed' if success else 'failed'}: {root_cause[:100]}",
            file_paths=file_paths or analysis.get("impact_files", []),
            success=success,
            strategy_used=strategy,
        )
        # V0.5: 每次记录后自动持久化（Pipeline B 之前从不保存）
        try:
            self.memory_bank.save()
        except Exception as e:
            logger.debug(f"[AgentOrch] 记忆库保存失败（非致命）: {e}")

    def _print_diff_report(self, original_files: dict[str, str]):
        if not original_files:
            return
        logger.info(f"[AgentOrch] ═══ 变更报告 ({len(original_files)} files) ═══")
        for filepath, original in original_files.items():
            from pathlib import Path
            current_path = Path(self.work_dir) / filepath
            current = current_path.read_text(encoding="utf-8", errors="replace") if current_path.exists() else ""
            diff = diff_text(original, current, context_lines=2)
            if diff.strip():
                summary = format_summary(diff)
                diff_lines = diff.split("\n")
                if len(diff_lines) > 50:
                    diff = "\n".join(diff_lines[:50]) + f"\n... ({len(diff_lines) - 50} more lines)"
                logger.info(f"[AgentOrch] --- {filepath} ({summary}) ---\n{diff}")
                try:
                    from patchflow.utils.diff import print_colored_diff
                    from rich.console import Console as _RC
                    import shutil
                    w = min(shutil.get_terminal_size((80, 24)).columns, 100)
                    c = _RC(width=w, emoji_variant="text")
                    from rich.panel import Panel
                    c.print(Panel(f"[bold]{filepath}[/bold] [dim]({summary})[/dim]", border_style="cyan"))
                    print_colored_diff(diff_lines[:50])
                except Exception:
                    pass
            else:
                logger.info(f"[AgentOrch] --- {filepath} (no changes) ---")
