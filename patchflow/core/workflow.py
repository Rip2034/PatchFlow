"""Workflow — 通用多 Agent 编排引擎

从 Claude Code 的 Workflow 系统移植的核心理念：
  - pipeline: 流水线模式（无屏障，不同 item 可处于不同阶段）
  - parallel: 并发模式（有屏障，等待全部完成）
  - Judge Panel: 多方案生成 → 独立评分 → 合成最优方案
  - Adversarial Verify: 独立质疑者验证发现
  - Loop-until-dry: 持续发现直到无新内容

与现有 AgentOrchestrator 的关系：
  AgentOrchestrator 是特化实现（Analyzer→Fixer→Reviewer 单链）
  Workflow 是通用引擎，可以编排任意数量和拓扑的 Agent

使用示例：
    from patchflow.core.workflow import Workflow, pipeline, parallel

    # 流水线：多项任务流经多个阶段
    results = pipeline(
        ["task1.py", "task2.py"],
        lambda f: agent_analyze(f),       # Stage 1: 分析
        lambda analysis: agent_fix(analysis),  # Stage 2: 修复
        lambda fix: agent_review(fix),     # Stage 3: 审查
    )

    # Judge Panel：生成多个方案，选出最优
    winner = Workflow.judge_panel(
        task="修复 app.py 的性能问题",
        generators=3,
        judges=3,
    )
"""

import concurrent.futures
import threading
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Callable, TypeVar

from patchflow.utils import logger

T = TypeVar("T")
R = TypeVar("R")


# ═══════════════════════════════════════════════════════════
# 核心数据结构
# ═══════════════════════════════════════════════════════════

@dataclass
class AgentResult:
    """单个 Agent 的执行结果"""
    label: str = ""
    data: dict | None = None
    text: str = ""
    error: str = ""
    duration_ms: float = 0.0
    model: str = ""
    success: bool = True

    @property
    def is_ok(self) -> bool:
        return self.success and self.error == ""


@dataclass
class PipelineStage:
    """流水线阶段的元信息"""
    name: str
    index: int
    item_count: int = 0
    completed: int = 0
    failed: int = 0
    start_time: float = 0.0
    duration_ms: float = 0.0


@dataclass
class WorkflowResult:
    """Workflow 执行结果"""
    success: bool = False
    results: list[AgentResult] = field(default_factory=list)
    stages: list[PipelineStage] = field(default_factory=list)
    total_duration_ms: float = 0.0
    summary: str = ""


# ═══════════════════════════════════════════════════════════
# 并发基础设施
# ═══════════════════════════════════════════════════════════

class ConcurrencyGate:
    """并发门控——控制同时运行的 Agent 数量上限

    默认上限 = min(8, cpu_cores - 1)，避免 LLM API 限流和系统过载。
    """

    def __init__(self, max_concurrency: int | None = None):
        import os
        if max_concurrency is None:
            cpu_count = os.cpu_count() or 4
            max_concurrency = min(8, max(cpu_count - 1, 1))
        self._semaphore = threading.Semaphore(max_concurrency)
        self.max_concurrency = max_concurrency

    def acquire(self):
        self._semaphore.acquire()

    def release(self):
        self._semaphore.release()


# ═══════════════════════════════════════════════════════════
# 核心编排函数
# ═══════════════════════════════════════════════════════════

def parallel(thunks: list[Callable[[], Any]],
             max_concurrency: int | None = None,
             on_progress: Callable[[int, int, Any], None] | None = None,
             ) -> list[Any]:
    """并发执行多个任务（有屏障——等全部完成才返回）

    这是 Workflow 中最基础的并发原语。所有 thunk 同时启动，
    等待全部完成后返回结果列表。

    与 pipeline 的区别：parallel 有屏障（等最慢的那个），
    pipeline 没有屏障（快速路径可以提前进入下一阶段）。

    Args:
        thunks: 要并发执行的无参函数列表
        max_concurrency: 最大并发数（默认自动检测）
        on_progress: 进度回调 (index, total, result)

    Returns:
        结果列表（与 thunks 顺序对应），失败的项为 None
    """
    if not thunks:
        return []

    gate = ConcurrencyGate(max_concurrency)
    results: list[Any] = [None] * len(thunks)
    errors: dict[int, str] = {}
    lock = threading.Lock()

    def _run(index: int, thunk: Callable[[], Any]) -> None:
        gate.acquire()
        try:
            result = thunk()
            with lock:
                results[index] = result
                if on_progress:
                    on_progress(index, len(thunks), result)
        except Exception as e:
            with lock:
                errors[index] = str(e)
                logger.warn(f"[Workflow.parallel] Task #{index} failed: {e}")
        finally:
            gate.release()

    with concurrent.futures.ThreadPoolExecutor(
        max_workers=gate.max_concurrency
    ) as executor:
        futures = [
            executor.submit(_run, i, thunk)
            for i, thunk in enumerate(thunks)
        ]
        concurrent.futures.wait(futures)

    # 标记失败项为 None
    for idx in errors:
        results[idx] = None

    return results


def pipeline(items: list[T],
             *stages: Callable[[T], Any],
             max_concurrency: int | None = None,
             ) -> list[list[Any]]:
    """流水线模式——各项独立流经所有阶段，无屏障

    每个 item 独立地通过所有 stages。Item A 可以在 stage 3 时，
    Item B 还在 stage 1。Wall-clock 时间 = 最慢的单 item 链，
    而不是各阶段耗时之和。

    Args:
        items: 待处理的数据项列表
        *stages: 阶段函数列表，每个函数签名为 (prev_result[, original_item, index]) -> Any
        max_concurrency: 最大并发 item 数

    Returns:
        [[item0_stage0, item0_stage1, ...], [item1_stage0, ...], ...]
        每个 item 的结果列表（失败阶段的后续结果均为 None）
    """
    if not items or not stages:
        return []

    n_items = len(items)
    n_stages = len(stages)
    results: list[list[Any]] = [[None] * n_stages for _ in range(n_items)]
    gate = ConcurrencyGate(max_concurrency)

    def _process_item(item_index: int):
        gate.acquire()
        try:
            item = items[item_index]
            current = item
            for stage_idx, stage_fn in enumerate(stages):
                try:
                    # 支持 (prev_result, original_item, index) 签名
                    import inspect
                    sig = inspect.signature(stage_fn)
                    n_params = len(sig.parameters)
                    if n_params >= 3:
                        current = stage_fn(current, item, item_index)
                    elif n_params == 2:
                        current = stage_fn(current, item)
                    else:
                        current = stage_fn(current)
                    results[item_index][stage_idx] = current
                except Exception as e:
                    logger.warn(
                        f"[Workflow.pipeline] Item#{item_index} "
                        f"Stage#{stage_idx} failed: {e}"
                    )
                    results[item_index][stage_idx] = None
                    break  # 跳过该项的剩余阶段
        finally:
            gate.release()

    with concurrent.futures.ThreadPoolExecutor(
        max_workers=gate.max_concurrency
    ) as executor:
        futures = [
            executor.submit(_process_item, i)
            for i in range(n_items)
        ]
        concurrent.futures.wait(futures)

    return results


def _infer_sig_params(fn: Callable) -> int:
    """推断函数签名的参数个数"""
    try:
        import inspect
        sig = inspect.signature(fn)
        return len(sig.parameters)
    except Exception:
        return 1


# ═══════════════════════════════════════════════════════════
# Workflow 类 — 高层编排
# ═══════════════════════════════════════════════════════════

class Workflow:
    """通用多 Agent 编排器

    提供高级编排模式：
      - judge_panel: 生成多个方案 → 评分 → 合成最优
      - adversarial_verify: 独立质疑者验证
      - multi_modal_sweep: 多角度并行搜索
      - loop_until_dry: 持续发现直到无新内容

    所有方法都是静态的，不维护内部状态。
    """

    # ── Judge Panel ──────────────────────────────────────

    @staticmethod
    def judge_panel(
        task: str,
        blackboard,
        generators: int = 3,
        judges: int = 2,
        model: str | None = None,
        synthesis_fn: Callable | None = None,
    ) -> dict | None:
        """Judge Panel 模式：N 个 Fixer 独立生成方案 → M 个 Judge 评分 → 合成最优

        Args:
            task: 任务描述
            blackboard: Blackboard 实例
            generators: 独立生成方案的数量
            judges: 每个方案的评审人数
            model: LLM 模型
            synthesis_fn: 合成函数（默认取评分最高的方案）

        Returns:
            最优方案 dict，或 None（全部失败）
        """
        from patchflow.agents.fixer_agent import agent_fix

        logger.info(f"[Workflow.JudgePanel] 启动: {generators} generators × {judges} judges")

        # Phase 1: 并行生成多个独立方案
        def _generate(idx: int):
            bb = blackboard.clone()
            bb["_panel_index"] = idx
            plan = agent_fix(bb, model=model)
            return {
                "index": idx,
                "plan": plan,
                "bb": bb,
            }

        gen_thunks = [lambda i=i: _generate(i) for i in range(generators)]
        gen_results = parallel(gen_thunks)
        candidates = [r for r in gen_results if r is not None and r.get("plan")]
        logger.info(f"[Workflow.JudgePanel] 生成 {len(candidates)}/{generators} 个有效方案")

        if not candidates:
            return None

        # Phase 2: 每个方案由多个 Judge 独立评分
        from patchflow.agents.reviewer import agent_review

        def _judge(candidate: dict, judge_idx: int):
            bb = candidate["bb"]
            review = agent_review(bb, model=model)
            return {
                "candidate_index": candidate["index"],
                "judge_index": judge_idx,
                "score": review.get("score", 0),
                "approved": review.get("approved", False),
                "review": review,
            }

        all_scores = []
        for candidate in candidates:
            judge_thunks = [
                lambda c=candidate, j=j: _judge(c, j)
                for j in range(judges)
            ]
            judge_results = parallel(judge_thunks)
            valid = [j for j in judge_results if j is not None]
            if valid:
                avg_score = sum(j["score"] for j in valid) / len(valid)
                candidate["avg_score"] = avg_score
                candidate["judgments"] = valid
                all_scores.append(avg_score)
                logger.info(
                    f"[Workflow.JudgePanel] Candidate #{candidate['index']}: "
                    f"avg score {avg_score:.1f}/10 ({len(valid)} judges)"
                )

        if not candidates:
            return None

        # Phase 3: 选出最优
        best = max(candidates, key=lambda c: c.get("avg_score", 0))
        logger.success(
            f"[Workflow.JudgePanel] 最优方案: #{best['index']} "
            f"(score: {best['avg_score']:.1f}/10)"
        )

        # 合成阶段（可选）
        if synthesis_fn:
            return synthesis_fn(best, candidates)

        return best["plan"]

    # ── Adversarial Verify ───────────────────────────────

    @staticmethod
    def adversarial_verify(
        claim: str,
        blackboard,
        skeptics: int = 3,
        refute_threshold: int = 2,
        model: str | None = None,
    ) -> dict:
        """Adversarial Verify 模式：N 个独立质疑者尝试反驳一个结论

        Args:
            claim: 待验证的结论
            blackboard: Blackboard 实例
            skeptics: 质疑者数量
            refute_threshold: 只要 ≥N 人认为可反驳，就判定为不成立
            model: LLM 模型

        Returns:
            {
                "survives": bool,        # 结论是否经得起质疑
                "refute_count": int,      # 质疑成功数
                "votes": list[dict],      # 每个质疑者的判断
                "consensus": str,         # 多数意见
            }
        """
        logger.info(
            f"[Workflow.AdversarialVerify] {skeptics} skeptics "
            f"examining: {claim[:100]}"
        )

        SKEPTIC_PROMPT = """You are a code review skeptic. Your job is to REFUTE this claim.
Be critical — look for edge cases, hidden bugs, performance issues, and logical flaws.
If the claim is solid and you cannot refute it, say so clearly.

Claim: {claim}

Output JSON:
{{
  "refuted": true/false,
  "severity": "critical/major/minor/none",
  "reason": "one-line reason",
  "evidence": "specific code or logic showing why"
}}"""

        def _skeptic(idx: int):
            from patchflow.core.llm_client import call_llm
            result = call_llm(
                system_prompt=SKEPTIC_PROMPT.format(claim=claim),
                user_message=f"Examine this claim critically (skeptic #{idx + 1}).",
                model=model,
            )
            return {
                "index": idx,
                "refuted": result.get("refuted", False) if result else True,
                "severity": result.get("severity", "major") if result else "critical",
                "reason": result.get("reason", "verification failed") if result else "LLM call failed",
            }

        thunks = [lambda i=i: _skeptic(i) for i in range(skeptics)]
        votes = parallel(thunks)
        votes = [v for v in votes if v is not None]

        refute_count = sum(1 for v in votes if v.get("refuted", False))
        survives = refute_count < refute_threshold

        logger.info(
            f"[Workflow.AdversarialVerify] Result: "
            f"{refute_count}/{len(votes)} refuted → "
            f"{'SURVIVES' if survives else 'REJECTED'}"
        )

        return {
            "survives": survives,
            "refute_count": refute_count,
            "total_skeptics": len(votes),
            "votes": votes,
            "consensus": "refuted" if refute_count >= refute_threshold else "survives",
        }

    # ── Multi-modal Sweep ────────────────────────────────

    @staticmethod
    def multi_modal_sweep(
        task: str,
        blackboard,
        lenses: list[dict] | None = None,
        model: str | None = None,
    ) -> dict:
        """Multi-modal Sweep 模式：从多个角度并行分析同一问题

        Args:
            task: 任务描述
            blackboard: Blackboard 实例
            lenses: 分析视角列表 [{"name": "...", "prompt": "..."}]
            model: LLM 模型

        Returns:
            {
                "findings": [...],         # 合并去重后的发现
                "per_lens": {lens: [...]}, # 每个视角的原始发现
                "unique_count": int,       # 去重后的数量
            }
        """
        from patchflow.agents.analyzer import agent_analyze

        if lenses is None:
            lenses = [
                {"name": "correctness", "prompt": "Focus on correctness bugs and logic errors."},
                {"name": "performance", "prompt": "Focus on performance issues and inefficiencies."},
                {"name": "security", "prompt": "Focus on security vulnerabilities."},
                {"name": "maintainability", "prompt": "Focus on code quality and maintainability."},
            ]

        logger.info(f"[Workflow.MultiModalSweep] {len(lenses)} lenses analyzing...")

        def _analyze(lens: dict):
            bb = blackboard.clone()
            bb["_sweep_lens"] = lens["name"]
            bb["_sweep_focus"] = lens["prompt"]
            analysis = agent_analyze(bb, model=model)
            return {
                "lens": lens["name"],
                "analysis": analysis,
                "error_type": analysis.get("error_type", ""),
                "root_cause": analysis.get("root_cause", ""),
                "impact_files": analysis.get("impact_files", []),
            }

        thunks = [lambda l=lens: _analyze(lens) for lens in lenses]
        results = parallel(thunks)
        results = [r for r in results if r is not None]

        # 按 root_cause 去重
        seen = set()
        all_findings = []
        per_lens: dict[str, list] = defaultdict(list)

        for r in results:
            lens = r["lens"]
            key = r["root_cause"][:80]
            per_lens[lens].append(r)
            if key not in seen:
                seen.add(key)
                all_findings.append(r)

        logger.info(
            f"[Workflow.MultiModalSweep] {len(all_findings)} unique findings "
            f"from {len(results)} total across {len(lenses)} lenses"
        )

        return {
            "findings": all_findings,
            "per_lens": dict(per_lens),
            "unique_count": len(all_findings),
            "total_count": len(results),
        }

    # ── Loop-until-dry ───────────────────────────────────

    @staticmethod
    def loop_until_dry(
        task: str,
        blackboard,
        finder_fn: Callable,
        max_rounds: int = 5,
        dry_threshold: int = 2,
        model: str | None = None,
        on_round: Callable[[int, list], None] | None = None,
    ) -> list[dict]:
        """Loop-until-dry 模式：持续发现直到连续 N 轮无新内容

        Args:
            task: 任务描述
            blackboard: Blackboard 实例
            finder_fn: 发现函数 (blackboard, model) -> list[dict]
            max_rounds: 最大轮数
            dry_threshold: 连续 dry_threshold 轮无新发现就停止
            model: LLM 模型
            on_round: 每轮回调 (round_index, new_findings)

        Returns:
            所有发现（按发现顺序）
        """
        seen = set()
        all_findings: list[dict] = []
        dry_rounds = 0
        total_found = 0

        logger.info(f"[Workflow.LoopUntilDry] Starting (max {max_rounds} rounds, "
                     f"dry_threshold={dry_threshold})")

        for round_idx in range(max_rounds):
            new_findings = finder_fn(blackboard, model)

            # 去重
            fresh = []
            for f in new_findings:
                key = f.get("root_cause", f.get("description", str(f)))[:100]
                if key not in seen:
                    seen.add(key)
                    fresh.append(f)

            total_found += len(fresh)
            all_findings.extend(fresh)

            logger.info(
                f"[Workflow.LoopUntilDry] Round {round_idx + 1}: "
                f"{len(fresh)} new findings (total: {total_found})"
            )

            if on_round:
                on_round(round_idx, fresh)

            if fresh:
                dry_rounds = 0
            else:
                dry_rounds += 1
                if dry_rounds >= dry_threshold:
                    logger.info(
                        f"[Workflow.LoopUntilDry] {dry_rounds} dry rounds, stopping"
                    )
                    break

        logger.success(
            f"[Workflow.LoopUntilDry] Complete: {total_found} findings "
            f"in {min(round_idx + 1, max_rounds)} rounds"
        )
        return all_findings

    # ── Parallel Fix (Worktree-based) ────────────────────

    @staticmethod
    def parallel_fix(
        task: str,
        work_dir: str,
        candidates: int = 3,
        model: str | None = None,
    ) -> dict | None:
        """并行修复模式：在隔离 worktree 中尝试多种修复，选验证通过的最优方案

        Args:
            task: 任务描述
            work_dir: 工作目录
            candidates: 并行尝试的方案数
            model: LLM 模型

        Returns:
            最优修复结果 dict，或 None（全部失败）
        """
        from patchflow.core.agent_orchestrator import AgentOrchestrator

        logger.info(
            f"[Workflow.ParallelFix] {candidates} parallel fix attempts"
        )

        def _try_fix(idx: int):
            import tempfile
            import os
            import shutil

            # 复制项目到临时目录（模拟 worktree 隔离）
            tmp_dir = tempfile.mkdtemp(prefix=f"pf_fix_{idx}_")
            try:
                # 复制项目文件
                shutil.copytree(
                    work_dir, tmp_dir,
                    dirs_exist_ok=True,
                    ignore=shutil.ignore_patterns(
                        ".git", ".patchflow", "__pycache__",
                        ".venv", "node_modules", "venv"
                    ),
                )
                orch = AgentOrchestrator(model=model, work_dir=tmp_dir)
                success = orch.run_from_task(
                    f"{task} [attempt #{idx + 1}]", work_dir=tmp_dir
                )
                return {
                    "index": idx,
                    "success": success,
                    "work_dir": tmp_dir,
                    "patches": orch.diff_tracker.patches if success else [],
                }
            except Exception as e:
                logger.warn(f"[Workflow.ParallelFix] Attempt #{idx} crashed: {e}")
                return None

        thunks = [lambda i=i: _try_fix(i) for i in range(candidates)]
        results = parallel(thunks)
        successful = [r for r in results if r is not None and r.get("success")]

        if successful:
            logger.success(
                f"[Workflow.ParallelFix] {len(successful)}/{candidates} attempts succeeded"
            )
            return successful[0]
        else:
            logger.error(f"[Workflow.ParallelFix] All {candidates} attempts failed")
            return None


# ═══════════════════════════════════════════════════════════
# 便捷函数
# ═══════════════════════════════════════════════════════════

def run_agents(
    agent_fns: list[tuple[str, Callable]],
    max_concurrency: int | None = None,
) -> list[AgentResult]:
    """便捷函数：并行运行多个不同 Agent 函数

    Args:
        agent_fns: [(label, callable), ...] 列表
        max_concurrency: 最大并发数

    Returns:
        AgentResult 列表
    """
    def _wrap(label: str, fn: Callable) -> Callable[[], AgentResult]:
        def _inner():
            t0 = time.time()
            try:
                result = fn()
                return AgentResult(
                    label=label,
                    data=result if isinstance(result, dict) else None,
                    text=str(result) if not isinstance(result, dict) else "",
                    duration_ms=(time.time() - t0) * 1000,
                    success=True,
                )
            except Exception as e:
                return AgentResult(
                    label=label,
                    error=str(e),
                    duration_ms=(time.time() - t0) * 1000,
                    success=False,
                )
        return _inner

    thunks = [_wrap(label, fn) for label, fn in agent_fns]
    results = parallel(thunks, max_concurrency=max_concurrency)
    return [r for r in results if r is not None]


def dedupe_by_key(items: list[dict], key_fn: Callable[[dict], str]) -> list[dict]:
    """去重工具：按自定义 key 函数去重，保留首次出现"""
    seen = set()
    result = []
    for item in items:
        k = key_fn(item)
        if k not in seen:
            seen.add(k)
            result.append(item)
    return result
