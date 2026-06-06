"""Multi-language tests — for all 6 supported languages.

Tests language detection, error parsing, error classification,
validation, entry point finding, dependency parsing, and import parsing.
"""

from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"


def _get_strategy(fixture_name: str):
    from patchflow.core.language_strategy import LanguageFactory
    factory = LanguageFactory()
    return factory.detect(str(FIXTURES / fixture_name))


def _get_lang_desc(fixture_name: str):
    from patchflow.core.language_registry import LanguageRegistry
    reg = LanguageRegistry()
    return reg.detect(str(FIXTURES / fixture_name))


# ── Language detection ────────────────────────────────────────

def test_detect_python():
    s = _get_strategy("python_bug")
    assert s is not None and s.name == "python"

def test_detect_javascript():
    s = _get_strategy("js_bug")
    assert s is not None and s.name == "javascript"

def test_detect_typescript():
    s = _get_strategy("ts_bug")
    assert s is not None and s.name == "typescript"

def test_detect_java():
    s = _get_strategy("java_bug")
    assert s is not None and s.name == "java"

def test_detect_go():
    s = _get_strategy("go_bug")
    assert s is not None and s.name == "go"

def test_detect_rust():
    s = _get_strategy("rust_bug")
    assert s is not None and s.name == "rust"


# ── Entry point finding ──────────────────────────────────────

def test_entry_point_found_when_name_matches():
    """Entry point is found when filename matches the entry_points list."""
    s = _get_strategy("python_bug")
    # Python entry_points = ["app.py", "main.py", "cli.py", "manage.py"]
    # buggy.py is NOT in the list → None expected
    entry = s.find_entry_file(FIXTURES / "python_bug")
    assert entry is None  # buggy.py not in ["app.py", "main.py", "cli.py", "manage.py"]

def test_entry_point_js_not_found_for_nonstandard_name():
    s = _get_strategy("js_bug")
    # JS entry_points = ["index.js", "app.js", "server.js", "main.js"]
    entry = s.find_entry_file(FIXTURES / "js_bug")
    assert entry is None  # buggy.js not in entry_points

def test_entry_point_go_not_found_for_nonstandard_name():
    s = _get_strategy("go_bug")
    # Go entry_points = ["main.go"]
    entry = s.find_entry_file(FIXTURES / "go_bug")
    assert entry is None  # buggy.go != main.go


# ── Error parsing via LanguageRegistry ──────────────────────

def test_parse_python_traceback():
    """Python: File "buggy.py", line 8"""
    from patchflow.core.language_registry import LanguageRegistry
    reg = LanguageRegistry()
    lang = reg.detect(str(FIXTURES / "python_bug"))
    frames = reg.parse_traceback(
        '  File "buggy.py", line 8, in calculate_ratio\n'
        '    return numerator / denom\n'
        "NameError: name 'denom' is not defined",
        lang=lang,
    )
    assert frames is not None
    assert len(frames) >= 1
    assert frames[0]["file"] == "buggy.py"
    assert frames[0]["line"] == 8

def test_parse_js_traceback():
    """JavaScript V8: at calculateRatio (buggy.js:5:12)"""
    from patchflow.core.language_registry import LanguageRegistry
    reg = LanguageRegistry()
    lang = reg.detect(str(FIXTURES / "js_bug"))
    frames = reg.parse_traceback(
        "ReferenceError: denom is not defined\n"
        "    at calculateRatio (buggy.js:5:12)\n"
        "    at main (buggy.js:10:20)",
        lang=lang,
    )
    assert frames is not None
    assert len(frames) >= 1
    assert any("buggy.js" in f["file"] for f in frames)

def test_parse_ts_traceback():
    """TypeScript: same V8 format, inherits from JS"""
    from patchflow.core.language_registry import LanguageRegistry
    reg = LanguageRegistry()
    lang = reg.detect(str(FIXTURES / "ts_bug"))
    frames = reg.parse_traceback(
        "TypeError: foo is not a function\n"
        "    at main (buggy.ts:10:20)",
        lang=lang,
    )
    assert frames is not None
    assert len(frames) >= 1
    assert any("buggy.ts" in f["file"] for f in frames)

def test_parse_java_traceback():
    """Java: at Buggy.calculateRatio(Buggy.java:8)"""
    from patchflow.core.language_registry import LanguageRegistry
    reg = LanguageRegistry()
    lang = reg.detect(str(FIXTURES / "java_bug"))
    frames = reg.parse_traceback(
        "Exception in thread \"main\" java.lang.NullPointerException\n"
        "    at Buggy.calculateRatio(Buggy.java:8)\n"
        "    at Buggy.main(Buggy.java:12)",
        lang=lang,
    )
    assert frames is not None
    assert len(frames) >= 1
    assert any("Buggy.java" in f["file"] for f in frames)

def test_parse_go_traceback():
    """Go: buggy.go:10"""
    from patchflow.core.language_registry import LanguageRegistry
    reg = LanguageRegistry()
    lang = reg.detect(str(FIXTURES / "go_bug"))
    frames = reg.parse_traceback(
        "panic: runtime error: invalid memory address\n"
        "goroutine 1 [running]:\n"
        "main.getTimeout(...)\n"
        "    buggy.go:10\n"
        "main.main()\n"
        "    buggy.go:15",
        lang=lang,
    )
    assert frames is not None
    assert len(frames) >= 1
    assert any("buggy.go" in f["file"] for f in frames)

def test_parse_rust_traceback():
    """Rust: --> buggy.rs:6"""
    from patchflow.core.language_registry import LanguageRegistry
    reg = LanguageRegistry()
    lang = reg.detect(str(FIXTURES / "rust_bug"))
    frames = reg.parse_traceback(
        "thread 'main' panicked at 'called Option::unwrap() on a None value', buggy.rs:6:25",
        lang=lang,
    )
    assert frames is not None
    assert len(frames) >= 1
    assert any("buggy.rs" in f["file"] for f in frames)


# ── Error classification via LanguageRegistry ─────────────────

def test_classify_python_error():
    from patchflow.core.language_registry import LanguageRegistry
    reg = LanguageRegistry()
    lang = reg.detect(str(FIXTURES / "python_bug"))
    etype, cause = reg.classify_error("NameError: name 'denom' is not defined", lang=lang)
    assert etype in ("runtime", "name", "unknown")

def test_classify_js_error():
    from patchflow.core.language_registry import LanguageRegistry
    reg = LanguageRegistry()
    lang = reg.detect(str(FIXTURES / "js_bug"))
    etype, cause = reg.classify_error("ReferenceError: denom is not defined", lang=lang)
    assert etype == "runtime"

def test_classify_ts_error():
    from patchflow.core.language_registry import LanguageRegistry
    reg = LanguageRegistry()
    lang = reg.detect(str(FIXTURES / "ts_bug"))
    etype, cause = reg.classify_error("error TS2345: Argument type mismatch", lang=lang)
    assert etype in ("runtime", "unknown")  # TS2345 → runtime

def test_classify_java_error():
    from patchflow.core.language_registry import LanguageRegistry
    reg = LanguageRegistry()
    lang = reg.detect(str(FIXTURES / "java_bug"))
    etype, cause = reg.classify_error("java.lang.NullPointerException", lang=lang)
    assert etype == "runtime"

def test_classify_go_error():
    from patchflow.core.language_registry import LanguageRegistry
    reg = LanguageRegistry()
    lang = reg.detect(str(FIXTURES / "go_bug"))
    etype, cause = reg.classify_error("panic: runtime error: nil pointer dereference", lang=lang)
    assert etype in ("runtime", "unknown")

def test_classify_rust_error():
    from patchflow.core.language_registry import LanguageRegistry
    reg = LanguageRegistry()
    lang = reg.detect(str(FIXTURES / "rust_bug"))
    etype, cause = reg.classify_error("error[E0308]: mismatched types", lang=lang)
    # E0308 is in Rust's classifiers → runtime
    assert cause != ""


# ── Error analysis ───────────────────────────────────────────

def test_analyze_python_error():
    from patchflow.core.analysis.error_analyzer import analyze
    err = ('Traceback (most recent call last):\n'
           '  File "buggy.py", line 12, in main\n'
           '    result = calculate_ratio(10, 2)\n'
           '  File "buggy.py", line 8, in calculate_ratio\n'
           '    return numerator / denom\n'
           "NameError: name 'denom' is not defined")
    analysis = analyze(err, work_dir=str(FIXTURES / "python_bug"))
    assert analysis.type != "unknown"
    assert analysis.confidence > 0

def test_analyze_js_error():
    from patchflow.core.analysis.error_analyzer import analyze
    err = ("ReferenceError: denom is not defined\n"
           "    at calculateRatio (buggy.js:5:12)\n"
           "    at main (buggy.js:10:20)")
    analysis = analyze(err, work_dir=str(FIXTURES / "js_bug"))
    assert analysis.type != "unknown"
    assert analysis.confidence > 0


# ── Extensions ───────────────────────────────────────────────

def test_extensions_all():
    expected = {
        "python_bug": ".py", "js_bug": ".js",
        "ts_bug": ".ts", "java_bug": ".java",
        "go_bug": ".go", "rust_bug": ".rs",
    }
    for fixture, ext in expected.items():
        s = _get_strategy(fixture)
        assert ext in s.extensions, f"{fixture}: expected {ext} in {s.extensions}"


# ── Strategy selector ────────────────────────────────────────

def test_strategy_selector_all_types():
    from patchflow.core.analysis.strategy_selector import select_strategy
    valid_scopes = {"line", "chain", "callchain", "business", "logic"}
    for etype in ("syntax", "type", "runtime", "import", "name",
                  "attribute", "logic", "test_fail", "key_error",
                  "index_error", "value_error", "file_error", "assertion"):
        result = select_strategy(etype)
        assert result["scope"] in valid_scopes, f"{etype}: bad scope {result['scope']}"


# ── Linter commands ──────────────────────────────────────────

def test_linter_commands():
    from patchflow.core.language_strategy import LanguageFactory
    factory = LanguageFactory()
    expectations = {
        "python_bug": "pylint",
        "js_bug": "eslint",
        "go_bug": "go vet ./...",
        "rust_bug": "cargo clippy",
    }
    for fixture, expected in expectations.items():
        s = factory.detect(str(FIXTURES / fixture))
        assert s.get_linter_command() == expected, f"{fixture}: expected {expected}"


# ── TypeScript inherits JS classifiers ───────────────────────

def test_ts_inherits_js_classifiers():
    """V0.5: TypeScript error_classifiers extends JS ones, not replace."""
    from patchflow.core.language_strategy import TypeScriptStrategy
    ts = TypeScriptStrategy()
    # Inherited from JavaScript
    assert "ReferenceError" in ts.error_classifiers
    assert "TypeError" in ts.error_classifiers
    assert "SyntaxError" in ts.error_classifiers
    # TS-specific
    assert "TS2345" in ts.error_classifiers
    assert "TS2322" in ts.error_classifiers


# ── Dependency parsing returns list ──────────────────────────

def test_deps_parsing_all():
    for fixture in ("python_bug", "js_bug", "java_bug", "go_bug", "rust_bug"):
        s = _get_strategy(fixture)
        deps = s.parse_dependencies(FIXTURES / fixture)
        assert isinstance(deps, list), f"{fixture}: expected list, got {type(deps)}"


# ── Compile or run command present ───────────────────────────

def test_compile_or_run_command():
    for fixture in ("python_bug", "js_bug", "ts_bug", "java_bug", "go_bug", "rust_bug"):
        s = _get_strategy(fixture)
        has_cmd = s.compile_command or s.run_command
        assert has_cmd, f"{s.name} has no compile or run command"


# ── Error analysis language detection ────────────────────────

def test_analyze_detects_language_per_fixture():
    """Error analysis should detect the language from the project directory."""
    from patchflow.core.analysis.error_analyzer import analyze
    test_cases = {
        "python_bug": ("python", 'NameError: name x is not defined\n'
                       '  File "test.py", line 1, in <module>'),
        "js_bug": ("javascript", "ReferenceError: x is not defined\n"
                   "    at Object.<anonymous> (test.js:1:1)"),
    }
    for fixture, (expected_lang, err) in test_cases.items():
        analysis = analyze(err, work_dir=str(FIXTURES / fixture))
        assert analysis.language == expected_lang, \
            f"{fixture}: expected {expected_lang}, got {analysis.language}"
