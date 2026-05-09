"""Tests for validator — language detection and entry file finding."""
import pytest
from pathlib import Path
from patchflow.core.fix.validator import (
    ValidationResult, detect_project_type, validate, _find_entry,
)


class TestValidationResult:
    def test_ok_result(self):
        vr = ValidationResult(ok=True, language="python")
        assert vr.ok
        assert vr.language == "python"
        assert vr.error is None

    def test_fail_result(self):
        vr = ValidationResult(ok=False, message="syntax error", language="python")
        assert not vr.ok

    def test_repr(self):
        vr = ValidationResult(ok=True, language="rust")
        assert "rust" in repr(vr)


class TestFindEntry:
    def test_python_entry(self, tmp_path):
        (tmp_path / "main.py").write_text("print('hello')")
        entry = _find_entry(tmp_path, "python")
        assert entry is not None
        assert entry.name == "main.py"

    def test_python_app_py_priority(self, tmp_path):
        (tmp_path / "app.py").write_text("print('app')")
        (tmp_path / "main.py").write_text("print('main')")
        entry = _find_entry(tmp_path, "python")
        assert entry.name == "app.py"  # app.py checked first

    def test_javascript_entry(self, tmp_path):
        (tmp_path / "index.js").write_text("console.log('hi')")
        entry = _find_entry(tmp_path, "javascript")
        assert entry is not None
        assert entry.name == "index.js"

    def test_typescript_entry(self, tmp_path):
        (tmp_path / "app.ts").write_text("const x = 1")
        entry = _find_entry(tmp_path, "typescript")
        assert entry is not None
        assert entry.name == "app.ts"

    def test_java_entry(self, tmp_path):
        (tmp_path / "Main.java").write_text("class Main {}")
        entry = _find_entry(tmp_path, "java")
        assert entry is not None
        assert entry.name == "Main.java"

    def test_go_entry(self, tmp_path):
        (tmp_path / "main.go").write_text("package main")
        entry = _find_entry(tmp_path, "go")
        assert entry is not None
        assert entry.name == "main.go"

    def test_rust_entry(self, tmp_path):
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "main.rs").write_text("fn main() {}")
        entry = _find_entry(tmp_path, "rust")
        assert entry is not None
        assert entry.name == "main.rs"

    def test_no_entry_returns_none(self, tmp_path):
        entry = _find_entry(tmp_path, "python")
        assert entry is None

    def test_unknown_lang_falls_back_to_python(self, tmp_path):
        (tmp_path / "main.py").write_text("print('hi')")
        entry = _find_entry(tmp_path, "unknown_lang")
        assert entry is not None
        assert entry.name == "main.py"


class TestDetectProjectType:
    def test_python_project(self, tmp_path):
        (tmp_path / "pyproject.toml").write_text("[project]")
        lang = detect_project_type(str(tmp_path))
        assert lang == "python"

    def test_js_project(self, tmp_path):
        (tmp_path / "package.json").write_text("{}")
        lang = detect_project_type(str(tmp_path))
        assert lang == "javascript"

    def test_go_project(self, tmp_path):
        (tmp_path / "go.mod").write_text("module x")
        lang = detect_project_type(str(tmp_path))
        assert lang == "go"

    def test_rust_project(self, tmp_path):
        (tmp_path / "Cargo.toml").write_text("[package]")
        lang = detect_project_type(str(tmp_path))
        assert lang == "rust"

    def test_unknown_project(self, tmp_path):
        lang = detect_project_type(str(tmp_path))
        assert lang == "unknown"


class TestValidatePython:
    def test_valid_python(self, tmp_path):
        (tmp_path / "app.py").write_text("print('hello world')")
        result = validate(str(tmp_path))
        assert result.ok

    def test_syntax_error(self, tmp_path):
        (tmp_path / "app.py").write_text("def broken(")
        result = validate(str(tmp_path))
        assert not result.ok

    def test_unknown_project_skips(self, tmp_path):
        result = validate(str(tmp_path))
        assert result.ok  # skips validation, returns ok
        assert "unknown" in result.language
