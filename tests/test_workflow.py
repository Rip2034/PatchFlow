"""Workflow 编排引擎测试"""
import pytest
import time
from patchflow.core.workflow import (
    parallel, pipeline, dedupe_by_key, run_agents, AgentResult,
    Workflow, ConcurrencyGate,
)


class TestParallel:
    def test_basic_parallel(self):
        results = parallel([
            lambda: 1,
            lambda: 2,
            lambda: 3,
        ])
        assert results == [1, 2, 3]

    def test_parallel_order_preserved(self):
        results = parallel([
            lambda: "a",
            lambda: "b",
            lambda: "c",
            lambda: "d",
        ])
        assert results == ["a", "b", "c", "d"]

    def test_parallel_with_failures(self):
        def _fail():
            raise ValueError("boom")

        results = parallel([
            lambda: 1,
            _fail,
            lambda: 3,
        ])
        assert results[0] == 1
        assert results[1] is None  # failed
        assert results[2] == 3

    def test_parallel_empty(self):
        results = parallel([])
        assert results == []

    def test_parallel_single(self):
        results = parallel([lambda: 42])
        assert results == [42]

    def test_parallel_concurrency_gate(self):
        """验证并发门控不会丢失结果"""
        counter = [0]

        def _inc():
            for _ in range(10):
                counter[0] += 1
            return counter[0]

        results = parallel([_inc for _ in range(5)], max_concurrency=2)
        assert len(results) == 5
        assert all(r is not None for r in results)

    def test_parallel_progress_callback(self):
        progress = []

        def _on_progress(idx, total, result):
            progress.append((idx, result))

        results = parallel(
            [lambda i=i: i * 10 for i in range(3)],
            on_progress=_on_progress,
        )
        assert len(progress) == 3
        assert results == [0, 10, 20]


class TestPipeline:
    def test_basic_pipeline(self):
        items = [1, 2, 3]
        results = pipeline(
            items,
            lambda x: x * 2,      # Stage 1: double
            lambda x: x + 1,      # Stage 2: +1
        )
        # [1,2,3] → [2,4,6] → [3,5,7]
        assert results == [[2, 3], [4, 5], [6, 7]]

    def test_pipeline_with_index(self):
        items = ["a", "b"]

        def stage_with_index(prev, original, idx):
            return f"{original}-{idx}"

        results = pipeline(items, stage_with_index)
        assert results[0][0] == "a-0"
        assert results[1][0] == "b-1"

    def test_pipeline_stage_failure_skips_remaining(self):
        items = [1, 2, 3]

        def _maybe_fail(x):
            if x == 2:
                raise ValueError("fail on 2")
            return x * 10

        results = pipeline(items, _maybe_fail, lambda x: x + 1)
        # Item 0: 1→10→11
        # Item 1: 2→fail→None (skips stage 2)
        # Item 2: 3→30→31
        assert results[0] == [10, 11]
        assert results[1][0] is None
        assert results[1][1] is None
        assert results[2] == [30, 31]

    def test_pipeline_empty(self):
        assert pipeline([]) == []
        assert pipeline([1]) == []


class TestDedupe:
    def test_dedupe_by_key(self):
        items = [
            {"id": "a", "val": 1},
            {"id": "b", "val": 2},
            {"id": "a", "val": 3},  # dup
        ]
        result = dedupe_by_key(items, lambda x: x["id"])
        assert len(result) == 2
        assert result[0]["val"] == 1  # keeps first

    def test_dedupe_empty(self):
        assert dedupe_by_key([], lambda x: x) == []

    def test_dedupe_all_unique(self):
        items = [{"id": "a"}, {"id": "b"}, {"id": "c"}]
        result = dedupe_by_key(items, lambda x: x["id"])
        assert len(result) == 3


class TestRunAgents:
    def test_basic(self):
        def _ok():
            return {"status": "ok"}

        def _fail():
            raise RuntimeError("fail")

        results = run_agents([
            ("ok_agent", _ok),
            ("fail_agent", _fail),
        ])
        assert len(results) == 2
        ok_r = [r for r in results if r.label == "ok_agent"][0]
        fail_r = [r for r in results if r.label == "fail_agent"][0]
        assert ok_r.success
        assert not fail_r.success
        assert "fail" in fail_r.error

    def test_duration_tracking(self):
        results = run_agents([
            ("fast", lambda: time.sleep(0.01)),
        ])
        assert len(results) == 1
        assert results[0].duration_ms > 0


class TestConcurrencyGate:
    def test_creates_with_auto_limit(self):
        gate = ConcurrencyGate()
        assert gate.max_concurrency >= 1

    def test_creates_with_custom_limit(self):
        gate = ConcurrencyGate(max_concurrency=3)
        assert gate.max_concurrency == 3

    def test_acquire_release(self):
        gate = ConcurrencyGate(max_concurrency=2)
        gate.acquire()
        gate.acquire()
        # Third acquire would block, so don't test that
        gate.release()
        gate.release()


class TestWorkflowJudgePanel:
    """Judge Panel 不在此做端到端测试（需要 LLM），只验证 API 签名"""
    def test_methods_exist(self):
        assert hasattr(Workflow, 'judge_panel')
        assert hasattr(Workflow, 'adversarial_verify')
        assert hasattr(Workflow, 'multi_modal_sweep')
        assert hasattr(Workflow, 'loop_until_dry')
        assert hasattr(Workflow, 'parallel_fix')
