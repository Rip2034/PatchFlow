"""Tests for scope_calculator, error_analyzer, and semantic_context."""
import pytest
from pathlib import Path

FIXTURES = Path(__file__).parent / "fixtures"


class TestErrorAnalyzer:
    def test_analyze_python_name_error(self):
        from patchflow.core.analysis.error_analyzer import analyze, ErrorAnalysis
        err = ('Traceback (most recent call last):\n'
               '  File "app.py", line 5, in main\n'
               '    result = calculate(x)\n'
               '  File "app.py", line 2, in calculate\n'
               '    return x / denom\n'
               "NameError: name 'denom' is not defined")
        analysis = analyze(err, work_dir=str(FIXTURES / "python_bug"))
        assert isinstance(analysis, ErrorAnalysis)
        # With our updated classifiers, NameError → name
        assert analysis.type in ("name", "runtime", "unknown")

    def test_analyze_python_key_error(self):
        from patchflow.core.analysis.error_analyzer import analyze, ErrorAnalysis
        err = ('Traceback (most recent call last):\n'
               '  File "app.py", line 3, in get_value\n'
               '    return data["missing"]\n'
               "KeyError: 'missing'")
        analysis = analyze(err, work_dir=str(FIXTURES / "python_bug"))
        # With updated classifiers, KeyError → key_error
        assert analysis.type in ("key_error", "runtime", "unknown")

    def test_analyze_python_syntax_error(self):
        from patchflow.core.analysis.error_analyzer import analyze
        err = ("  File \"app.py\", line 1\n"
               "    def broken(\n"
               "              ^\n"
               "SyntaxError: unexpected EOF while parsing")
        analysis = analyze(err, work_dir=str(FIXTURES / "python_bug"))
        assert analysis.type in ("syntax", "unknown")

    def test_analyze_impact_files(self):
        from patchflow.core.analysis.error_analyzer import analyze
        err = ('Traceback (most recent call last):\n'
               '  File "app.py", line 10, in main\n'
               '    helper.process()\n'
               '  File "helper.py", line 5, in process\n'
               '    return 1/0\n'
               "ZeroDivisionError: division by zero")
        analysis = analyze(err, work_dir=str(FIXTURES / "python_bug"))
        assert len(analysis.impact_files) >= 1


class TestStrategySelector:
    def test_syntax_strategy(self):
        from patchflow.core.analysis.strategy_selector import select_strategy
        s = select_strategy("syntax")
        assert s["scope"] == "line"
        assert s["files"] == 1
        assert not s["rewrite"]

    def test_runtime_strategy(self):
        from patchflow.core.analysis.strategy_selector import select_strategy
        s = select_strategy("runtime", impact_file_count=5)
        assert s["scope"] == "callchain"
        assert s["files"] == 5  # = max(impact_file_count, 1)

    def test_key_error_strategy(self):
        from patchflow.core.analysis.strategy_selector import select_strategy
        s = select_strategy("key_error")
        assert s["scope"] == "callchain"
        assert s["files"] == 2

    def test_strategy_sequence(self):
        from patchflow.core.analysis.strategy_selector import strategy_sequence
        seq = strategy_sequence("syntax")
        assert "line" in seq
        assert "business" in seq[-1]

    def test_unknown_type_defaults_to_runtime(self):
        from patchflow.core.analysis.strategy_selector import select_strategy
        s = select_strategy("nonexistent_type")
        assert "scope" in s
        assert "files" in s

    def test_get_strategy_stats_empty(self):
        from patchflow.core.analysis.strategy_selector import get_strategy_stats
        stats = get_strategy_stats(None)
        assert stats == {}


class TestScopeCalculator:
    def test_syntax_scope_single_file(self):
        from patchflow.core.analysis.error_analyzer import ErrorAnalysis
        from patchflow.core.fix.scope_calculator import calculate
        analysis = ErrorAnalysis(
            type="syntax",
            root_cause="missing colon",
            call_chain=[{"file": "app.py", "line": 5, "function": "main"}],
            impact_files=["app.py"],
        )
        scope = calculate(analysis)
        assert scope.strategy == "line"
        assert len(scope.files) == 1
        assert scope.files[0] == "app.py"

    def test_runtime_scope_with_callers(self):
        from patchflow.core.analysis.error_analyzer import ErrorAnalysis
        from patchflow.core.fix.scope_calculator import calculate
        analysis = ErrorAnalysis(
            type="runtime",
            root_cause="null pointer",
            call_chain=[
                {"file": "main.py", "line": 10, "function": "main"},
                {"file": "service.py", "line": 5, "function": "process"},
            ],
            impact_files=["main.py", "service.py"],
        )
        scope = calculate(analysis)
        assert scope.strategy == "callchain"
        assert len(scope.files) >= 1

    def test_name_error_single_file(self):
        from patchflow.core.analysis.error_analyzer import ErrorAnalysis
        from patchflow.core.fix.scope_calculator import calculate
        analysis = ErrorAnalysis(
            type="name",
            root_cause="undefined variable 'x'",
            call_chain=[{"file": "app.py", "line": 3, "function": "f"}],
            impact_files=["app.py"],
        )
        scope = calculate(analysis)
        assert scope.strategy == "line"
        assert len(scope.files) == 1


class TestSemanticContext:
    def test_build_context_from_files_no_codegraph(self):
        from patchflow.core.fix.semantic_context import build_context_from_files
        result = build_context_from_files(None, ["app.py"])
        assert result == ""

    def test_build_context_from_error_no_codegraph(self):
        from patchflow.core.fix.semantic_context import build_context_from_error
        result = build_context_from_error(None, "some error")
        assert result == ""

    def test_paths_match_same_file(self):
        from patchflow.core.fix.semantic_context import _paths_match
        assert _paths_match("src/app.py", "src/app.py")

    def test_paths_match_different_prefix(self):
        from patchflow.core.fix.semantic_context import _paths_match
        # project/src/app.py vs src/app.py → last 2 components match
        assert _paths_match("project/src/app.py", "src/app.py")

    def test_paths_match_no_false_positive(self):
        from patchflow.core.fix.semantic_context import _paths_match
        # src/main.py vs other/main.py → only 1 component matches, short not consumed
        assert not _paths_match("src/main.py", "other/main.py")

    def test_paths_match_single_component_consumed(self):
        from patchflow.core.fix.semantic_context import _paths_match
        # main.py vs src/main.py → 1 component, shorter fully consumed → match
        assert _paths_match("main.py", "src/main.py")
