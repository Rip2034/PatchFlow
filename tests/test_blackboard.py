"""Tests for Blackboard — multi-agent shared data structure."""
import pytest
from patchflow.agents.blackboard import Blackboard
from patchflow.agents.schema import ANALYZER_PROMPT_ENHANCED


class TestBlackboardBasic:
    def test_init_defaults(self):
        bb = Blackboard()
        assert bb["task"] == ""
        assert bb["error"] == ""
        assert bb["context"] == {}
        assert bb["code"] == {}
        assert bb["analysis"] is None

    def test_init_with_data(self):
        bb = Blackboard(
            task="fix bug",
            context={"lang": "python"},
            code={"app.py": "print(1)"},
            error="NameError: x",
        )
        assert bb["task"] == "fix bug"
        assert bb["context"] == {"lang": "python"}
        assert bb["code"]["app.py"] == "print(1)"
        assert bb["error"] == "NameError: x"

    def test_get_set(self):
        bb = Blackboard()
        bb["key"] = "value"
        assert bb.get("key") == "value"
        assert bb.get("missing", "default") == "default"

    def test_contains(self):
        bb = Blackboard(task="test")
        assert "task" in bb
        assert "nonexistent" not in bb


class TestBlackboardAgentTracking:
    def test_set_current_agent(self):
        bb = Blackboard()
        bb.set_current_agent("analyzer")
        assert bb.get_current_agent() == "analyzer"

    def test_activity_logging(self):
        bb = Blackboard()
        bb.set_current_agent("analyzer")
        bb["analysis"] = {"type": "runtime"}  # triggers read + write via dict
        activity = bb.get_activity()
        assert any(a["agent"] == "analyzer" for a in activity)

    def test_activity_summary(self):
        bb = Blackboard()
        bb.set_current_agent("fixer")
        bb.set_fix_plan({"patches": [], "summary": "test"})
        summary = bb.get_activity_summary()
        assert "fixer" in summary

    def test_clear_activity(self):
        bb = Blackboard()
        bb.set_current_agent("reviewer")
        bb["key"] = "val"
        bb.clear_activity()
        assert bb.get_activity() == []


class TestBlackboardAnalysisFixReview:
    def test_set_analysis_validates(self):
        bb = Blackboard()
        bb.set_analysis({
            "error_type": "runtime",
            "root_cause": "null pointer",
            "impact_files": ["a.py"],
            "confidence": 0.9,
            "summary": "NPE in a.py",
            "language": "python",
        })
        a = bb["analysis"]
        assert a["error_type"] == "runtime"
        assert a["confidence"] == 0.9

    def test_set_analysis_defaults(self):
        bb = Blackboard()
        bb.set_analysis({})
        a = bb["analysis"]
        assert a["error_type"] == "unknown"
        assert a["confidence"] == 0.0
        # summary is auto-generated from error_type + root_cause
        assert len(a["summary"]) > 0

    def test_set_fix_plan(self):
        bb = Blackboard()
        bb.set_fix_plan({
            "summary": "added null check",
            "patches": [
                {"file": "a.py", "old": "x.y", "new": "x.y if x else 0",
                 "reason": "null safety"}
            ],
        })
        fp = bb["fix_plan"]
        assert fp["summary"] == "added null check"
        assert len(fp["patches"]) == 1

    def test_set_review(self):
        bb = Blackboard()
        bb.set_review({
            "approved": True,
            "score": 8,
            "summary": "looks good",
            "dimensions": {
                "correctness": {"score": 8, "note": "ok"},
                "security": {"score": 9, "note": "fine"},
                "performance": {"score": 7, "note": "minor"},
                "edge_cases": {"score": 8, "note": "covered"},
            },
        })
        r = bb["review"]
        assert r["approved"]
        assert r["score"] == 8

    def test_review_dimension_rejection(self):
        """Low dimension score triggers auto-reject."""
        bb = Blackboard()
        bb.set_review({
            "approved": True,
            "score": 8,
            "dimensions": {
                "correctness": {"score": 3, "note": "wrong fix"},
                "security": {"score": 9, "note": "ok"},
                "performance": {"score": 8, "note": "ok"},
                "edge_cases": {"score": 7, "note": "ok"},
            },
        })
        r = bb["review"]
        assert not r["approved"]  # auto-rejected due to correctness=3 < 6


class TestBlackboardCompress:
    def test_compress_for_analyzer(self):
        bb = Blackboard(task="fix", code={"a.py": "print(1)"}, error="err")
        result = bb.compress_for("analyzer")
        assert "code" in result
        assert "task" in result

    def test_compress_for_fixer(self):
        bb = Blackboard(task="fix", error="err")
        bb.set_analysis({
            "error_type": "runtime", "root_cause": "NPE",
            "impact_files": ["a.py"], "confidence": 0.8,
            "summary": "npe", "language": "python",
        })
        result = bb.compress_for("fixer")
        assert "analysis" in result
        assert "error_type" in result["analysis"]

    def test_compress_for_reviewer(self):
        bb = Blackboard(task="fix", error="err")
        bb.set_analysis({
            "error_type": "runtime", "root_cause": "NPE",
            "impact_files": ["a.py"], "confidence": 0.8,
            "summary": "npe", "language": "python",
        })
        bb.set_fix_plan({
            "summary": "fix", "patches": [
                {"file": "a.py", "old": "x", "new": "y", "reason": "fix"}
            ]
        })
        result = bb.compress_for("reviewer")
        assert "analysis" in result
        assert "fix_plan" in result


class TestBlackboardCode:
    def test_get_code_without_codegraph(self, tmp_path):
        bb = Blackboard(code={"a.py": "print(1)", "b.py": "print(2)"})
        result = bb.get_code(["a.py"])
        assert "print(1)" in result
        assert "a.py" in result

    def test_get_code_missing_file(self):
        bb = Blackboard(code={"a.py": "print(1)"})
        result = bb.get_code(["nonexistent.py"])
        assert "no files available" in result

    def test_get_code_truncation(self, tmp_path):
        """Large files are truncated by the max_per_file limit."""
        big_content = "x" * 10000
        bb = Blackboard(code={"big.py": big_content})
        result = bb.get_code(["big.py"], max_per_file=100, max_total=500)
        assert len(result) < 10000  # should be truncated
        assert "truncated" in result.lower()

    def test_get_callchain_code(self):
        bb = Blackboard(code={"a.py": "def f(): pass", "b.py": "def g(): f()"})
        result = bb.get_callchain_code()
        assert "def f" in result or "def g" in result

    def test_summary(self):
        bb = Blackboard(task="fix bug")
        bb.set_analysis({
            "error_type": "runtime", "root_cause": "x is None",
            "impact_files": ["a.py"], "confidence": 0.9,
            "summary": "null reference", "language": "python",
        })
        s = bb.summary()
        assert "fix bug" in s
        assert "null reference" in s


class TestBlackboardThreadSafety:
    def test_concurrent_read_write(self):
        import threading
        bb = Blackboard()

        def writer():
            for i in range(50):
                bb[f"key_{i}"] = f"val_{i}"

        def reader():
            for _ in range(50):
                _ = bb.get("task", "")

        threads = [threading.Thread(target=writer), threading.Thread(target=reader)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert bb.get("key_0") == "val_0"
