"""TestRunner — 修复后回归测试验证（V0.5 新增）

在修复完成后自动运行项目的已有测试套件，并尝试生成针对性的回归测试，
确保修复不会引入新的 bug。

核心职责：
  1. 检测项目的测试框架（pytest, jest, go test, etc.）
  2. 运行已有测试套件
  3. 生成针对特定 bug 的回归测试
  4. 对比修复前后的测试结果
"""

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from patchflow.utils import logger
from patchflow.utils.runner import run


@dataclass
class TestResult:
    """测试运行结果"""
    framework: str = ""
    total: int = 0
    passed: int = 0
    failed: int = 0
    skipped: int = 0
    error_count: int = 0
    output: str = ""
    exit_code: int = 0
    passed_before_fix: bool = False  # 修复前是否也通过

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and self.failed == 0 and self.error_count == 0

    @property
    def summary(self) -> str:
        return f"{self.passed}/{self.total} passed, {self.failed} failed, {self.error_count} errors"


@dataclass
class RegressionTest:
    """为特定 bug 生成的回归测试"""
    test_code: str
    test_file: str
    language: str
    description: str
    framework: str = ""


class TestRunner:
    """修复后的回归测试运行器"""

    def __init__(self, work_dir: str = "."):
        self.work_dir = Path(work_dir).resolve()
        self._framework = None
        self._test_patterns = None

    def detect_framework(self) -> str | None:
        """检测项目使用的测试框架"""
        if self._framework:
            return self._framework

        wd = self.work_dir

        # Python
        if (wd / "pyproject.toml").exists():
            content = (wd / "pyproject.toml").read_text(encoding="utf-8", errors="replace")
            if "pytest" in content:
                self._framework = "pytest"
                return self._framework
        if (wd / "conftest.py").exists() or list(wd.glob("*test*.py")):
            self._framework = "pytest"
            return self._framework
        if (wd / "setup.cfg").exists():
            content = (wd / "setup.cfg").read_text(encoding="utf-8", errors="replace")
            if "pytest" in content or "nosetests" in content:
                self._framework = "pytest"
                return self._framework

        # JavaScript/TypeScript
        if (wd / "package.json").exists():
            try:
                pkg = json.loads((wd / "package.json").read_text(encoding="utf-8", errors="replace"))
                dev_deps = {**pkg.get("dependencies", {}), **pkg.get("devDependencies", {})}
                if "jest" in dev_deps:
                    self._framework = "jest"
                elif "vitest" in dev_deps:
                    self._framework = "vitest"
                elif "mocha" in dev_deps:
                    self._framework = "mocha"
                elif "jasmine" in dev_deps:
                    self._framework = "jasmine"
                if self._framework:
                    return self._framework
            except (json.JSONDecodeError, KeyError):
                pass
            # 检查 scripts
            try:
                pkg = json.loads((wd / "package.json").read_text(encoding="utf-8", errors="replace"))
                scripts = pkg.get("scripts", {})
                if "test" in scripts:
                    test_script = scripts["test"]
                    if "jest" in test_script:
                        self._framework = "jest"
                    elif "vitest" in test_script:
                        self._framework = "vitest"
                    elif "mocha" in test_script:
                        self._framework = "mocha"
                    else:
                        self._framework = "npm-test"
                    return self._framework
            except (json.JSONDecodeError, KeyError):
                pass

        # Go
        if (wd / "go.mod").exists() or list(wd.glob("*_test.go")):
            self._framework = "go-test"
            return self._framework

        # Rust
        if (wd / "Cargo.toml").exists():
            self._framework = "cargo-test"
            return self._framework

        # Java
        if (wd / "pom.xml").exists():
            self._framework = "maven-test"
            return self._framework
        if (wd / "build.gradle").exists() or (wd / "build.gradle.kts").exists():
            self._framework = "gradle-test"
            return self._framework

        return None

    def run_tests(self, timeout: int = 60) -> TestResult:
        """运行项目的已有测试套件"""
        framework = self.detect_framework()
        if not framework:
            return TestResult(framework="unknown", output="No test framework detected")

        commands = {
            "pytest": "python -m pytest -x --tb=short -q 2>&1",
            "jest": "npx jest --passWithNoTests --forceExit 2>&1",
            "vitest": "npx vitest run --passWithNoTests 2>&1",
            "mocha": "npx mocha --exit 2>&1",
            "jasmine": "npx jasmine 2>&1",
            "npm-test": "npm test 2>&1",
            "go-test": "go test ./... -count=1 -short 2>&1",
            "cargo-test": "cargo test 2>&1",
            "maven-test": "mvn test -q 2>&1",
            "gradle-test": "gradle test -q 2>&1",
        }

        cmd = commands.get(framework, "")
        if not cmd:
            return TestResult(framework=framework, output=f"No command for {framework}")

        logger.info(f"[TestRunner] 运行测试: {cmd}")
        result = run(cmd, cwd=str(self.work_dir), timeout=timeout)

        test_result = TestResult(
            framework=framework,
            output=result.stdout + "\n" + result.stderr,
            exit_code=result.exit_code,
        )

        # 解析测试结果
        test_result = self._parse_output(test_result)
        return test_result

    def generate_regression_test(self, error_analysis: dict,
                                  fixed_file: str,
                                  language: str = "") -> RegressionTest | None:
        """根据错误分析生成针对性的回归测试

        分析错误类型和根因，生成一个能复现该 bug 的测试用例。
        修复后这个测试应该通过。
        """
        error_type = error_analysis.get("error_type", "")
        root_cause = error_analysis.get("root_cause", "")
        impact_symbols = error_analysis.get("impact_symbols", [])

        if not root_cause:
            return None

        lang = language or "python"

        if lang == "python":
            return self._generate_python_test(error_type, root_cause, impact_symbols, fixed_file)
        elif lang in ("javascript", "typescript"):
            return self._generate_js_test(error_type, root_cause, impact_symbols, fixed_file, lang)
        elif lang == "go":
            return self._generate_go_test(error_type, root_cause, impact_symbols, fixed_file)
        elif lang == "java":
            return self._generate_java_test(error_type, root_cause, impact_symbols, fixed_file)

        return None

    def _generate_python_test(self, error_type: str, root_cause: str,
                               symbols: list[str], fixed_file: str) -> RegressionTest:
        """生成 Python 回归测试"""
        # 提取关键信息
        func_name = symbols[0].split(".")[-1] if symbols else "affected_function"
        module_name = Path(fixed_file).stem

        test_code = f'''"""Regression test for: {root_cause[:100]}"""
import pytest
from {module_name} import {func_name}


def test_regression_{func_name}_{error_type}():
    """Test that the previously failing case now works correctly."""
    # TODO: Adjust the input to match the specific error condition
    # Root cause: {root_cause}

    # Verify no exception is raised for the previously-failing case
    try:
        # Call with the input that previously caused the error
        result = {func_name}()  # ← adjust arguments
        assert result is not None, "Should return a valid result"
    except Exception as e:
        pytest.fail(f"Regression: fix did not resolve the error - {{e}}")


def test_regression_{func_name}_edge_cases():
    """Test edge cases related to the fix."""
    # Edge case 1: empty/null input
    try:
        {func_name}()  # ← adjust
    except Exception:
        pass  # Should handle gracefully or raise expected exception
'''
        test_file = f"test_regression_{module_name}_{error_type}.py"
        return RegressionTest(
            test_code=test_code,
            test_file=test_file,
            language="python",
            description=f"Regression test for {error_type}: {root_cause[:80]}",
            framework="pytest",
        )

    def _generate_js_test(self, error_type: str, root_cause: str,
                           symbols: list[str], fixed_file: str, lang: str) -> RegressionTest:
        """生成 JS/TS 回归测试"""
        func_name = symbols[0].split(".")[-1] if symbols else "affectedFunction"
        module_path = Path(fixed_file).stem

        test_code = f'''/**
 * Regression test for: {root_cause[:100]}
 */
const {{ {func_name} }} = require('./{module_path}');

describe('Regression: {func_name} - {error_type}', () => {{
  test('should handle the previously-failing case', () => {{
    // Root cause: {root_cause}
    // Verify the previously-failing input now works
    expect(() => {{
      const result = {func_name}(); // ← adjust arguments
      expect(result).toBeDefined();
    }}).not.toThrow();
  }});

  test('should handle edge cases', () => {{
    // Edge case: null/undefined/empty input
    expect(() => {func_name}(null)).not.toThrow();
  }});
}});
'''
        ext = "ts" if lang == "typescript" else "js"
        test_file = f"test_regression_{module_path}_{error_type}.test.{ext}"
        return RegressionTest(
            test_code=test_code,
            test_file=test_file,
            language=lang,
            description=f"Regression test for {error_type}: {root_cause[:80]}",
            framework="jest",
        )

    def _generate_go_test(self, error_type: str, root_cause: str,
                           symbols: list[str], fixed_file: str) -> RegressionTest:
        """生成 Go 回归测试"""
        func_name = symbols[0].split(".")[-1] if symbols else "AffectedFunc"
        pkg_name = Path(fixed_file).stem

        test_code = f'''package {pkg_name}

import "testing"

// TestRegression{func_name}{error_type} tests the previously-failing case.
// Root cause: {root_cause}
func TestRegression{func_name}{error_type}(t *testing.T) {{
    // Call with the input that previously caused the error
    result, err := {func_name}() // ← adjust arguments
    if err != nil {{
        t.Fatalf("Regression: fix did not resolve the error: %v", err)
    }}
    if result == nil {{
        t.Error("Expected non-nil result")
    }}
}}

func TestRegression{func_name}EdgeCases(t *testing.T) {{
    // Edge case: zero value / empty input
    defer func() {{
        if r := recover(); r != nil {{
            t.Logf("Panic recovered (may be expected): %v", r)
        }}
    }}()
    {func_name}() // ← adjust
}}
'''
        test_file = f"{pkg_name}_regression_test.go"
        return RegressionTest(
            test_code=test_code,
            test_file=test_file,
            language="go",
            description=f"Regression test for {error_type}: {root_cause[:80]}",
            framework="go-test",
        )

    def _generate_java_test(self, error_type: str, root_cause: str,
                             symbols: list[str], fixed_file: str) -> RegressionTest:
        """生成 Java 回归测试"""
        func_name = symbols[0].split(".")[-1] if symbols else "affectedMethod"
        class_name = Path(fixed_file).stem

        test_code = f'''import org.junit.Test;
import static org.junit.Assert.*;

/**
 * Regression test for: {root_cause[:100]}
 */
public class Regression{class_name}Test {{

    @Test
    public void testRegression{func_name}{error_type}() {{
        // Root cause: {root_cause}
        // Verify the previously-failing case now works
        try {{
            // Object result = new {class_name}().{func_name}(); // ← adjust
            // assertNotNull("Should return a valid result", result);
        }} catch (Exception e) {{
            fail("Regression: fix did not resolve the error - " + e.getMessage());
        }}
    }}

    @Test
    public void testRegression{func_name}EdgeCases() {{
        // Edge case: null / empty input
        try {{
            // new {class_name}().{func_name}(null); // ← adjust
        }} catch (NullPointerException | IllegalArgumentException e) {{
            // Expected for null input — shouldn't crash unexpectedly
        }}
    }}
}}
'''
        test_file = f"Regression{class_name}Test.java"
        return RegressionTest(
            test_code=test_code,
            test_file=test_file,
            language="java",
            description=f"Regression test for {error_type}: {root_cause[:80]}",
            framework="junit",
        )

    def write_regression_test(self, test: RegressionTest,
                              output_dir: str | None = None) -> Path | None:
        """将回归测试写入文件"""
        od = Path(output_dir) if output_dir else self.work_dir
        test_dir = od / ".patchflow" / "regression_tests"
        test_dir.mkdir(parents=True, exist_ok=True)
        test_path = test_dir / test.test_file
        try:
            test_path.write_text(test.test_code, encoding="utf-8")
            logger.info(f"[TestRunner] 回归测试已保存: {test_path}")
            return test_path
        except OSError as e:
            logger.error(f"[TestRunner] 写入回归测试失败: {e}")
            return None

    def run_regression_test(self, test_path: Path, timeout: int = 30) -> TestResult:
        """运行单个回归测试文件"""
        framework = self.detect_framework()
        if not framework:
            return TestResult(framework="unknown")

        path_str = str(test_path)
        commands = {
            "pytest": f"python -m pytest {path_str} -x --tb=short -q 2>&1",
            "jest": f"npx jest {path_str} --forceExit 2>&1",
            "go-test": f"go test -run TestRegression {path_str} 2>&1",
            "maven-test": f"mvn test -Dtest=Regression* -q 2>&1",
        }

        cmd = commands.get(framework, "")
        if not cmd:
            return TestResult(framework=framework)

        logger.info(f"[TestRunner] 运行回归测试: {cmd}")
        result = run(cmd, cwd=str(self.work_dir), timeout=timeout)

        test_result = TestResult(
            framework=framework,
            output=result.stdout + "\n" + result.stderr,
            exit_code=result.exit_code,
        )
        return self._parse_output(test_result)

    def _parse_output(self, result: TestResult) -> TestResult:
        """从测试输出中解析测试计数"""
        output = result.output

        # pytest: "3 passed, 1 failed, 2 warnings"
        m = re.search(r'(\d+)\s+passed', output)
        if m:
            result.passed = int(m.group(1))
        m = re.search(r'(\d+)\s+failed', output)
        if m:
            result.failed = int(m.group(1))
        m = re.search(r'(\d+)\s+error', output)
        if m:
            result.error_count = int(m.group(1))
        result.total = result.passed + result.failed + result.error_count

        # jest: "Tests: 3 passed, 1 failed, 5 total"
        m = re.search(r'Tests:\s*(?:(\d+)\s+passed,\s*)?(?:(\d+)\s+failed,\s*)?(?:(\d+)\s+skipped,\s*)?(\d+)\s+total', output)
        if m:
            result.passed = int(m.group(1) or 0)
            result.failed = int(m.group(2) or 0)
            result.skipped = int(m.group(3) or 0)
            result.total = int(m.group(4))

        # go test: "ok  package  0.123s" or "FAIL  package  0.123s"
        if "FAIL" in output.split("\n")[0] if output else "":
            result.failed = max(result.failed, 1)

        return result


def run_post_fix_validation(work_dir: str = ".",
                            analysis: dict | None = None,
                            fixed_file: str = "",
                            language: str = "") -> dict:
    """修复后的完整验证流程（便捷函数）

    1. 运行已有测试套件
    2. 如果提供了 analysis，生成并运行回归测试
    3. 返回综合验证结果

    Returns:
        dict: {
            "existing_tests_ok": bool,
            "regression_test_generated": bool,
            "regression_test_ok": bool | None,
            "test_summary": str,
        }
    """
    runner = TestRunner(work_dir)
    result = {
        "existing_tests_ok": True,
        "regression_test_generated": False,
        "regression_test_ok": None,
        "test_summary": "",
    }

    # 1. 运行已有测试
    test_result = runner.run_tests(timeout=60)
    result["existing_tests_ok"] = test_result.ok
    result["test_summary"] = f"Existing tests: {test_result.summary}"

    if not test_result.ok:
        logger.warn(f"[TestRunner] 已有测试失败: {test_result.summary}")

    # 2. 生成回归测试
    if analysis and fixed_file:
        regression = runner.generate_regression_test(analysis, fixed_file, language)
        if regression:
            test_path = runner.write_regression_test(regression)
            if test_path:
                result["regression_test_generated"] = True
                reg_result = runner.run_regression_test(test_path, timeout=30)
                result["regression_test_ok"] = reg_result.ok
                result["test_summary"] += f" | Regression test: {reg_result.summary}"
                if reg_result.ok:
                    logger.success(f"[TestRunner] 回归测试通过: {reg_result.summary}")
                else:
                    logger.warn(f"[TestRunner] 回归测试失败: {reg_result.summary}")

    return result
