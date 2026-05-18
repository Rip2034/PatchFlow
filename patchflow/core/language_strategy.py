"""Language Strategy — 工厂模式统一语言差异

所有语言相关的数据和行为集中在此文件中。
新增语言只需添加一个 LanguageStrategy 子类并在 _BUILTINS 中注册即可。
"""

import ast
import json
import platform
import re
import shlex
from abc import ABC, abstractmethod
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from patchflow.core.analysis.error_parser import ParsedError


# ── helpers ──────────────────────────────────────────────────

def _is_windows() -> bool:
    return platform.system() == "Windows"


# ── traceback line parsers (per-language regex) ──────────────

def _python_traceback_line(line: str) -> tuple[str, int, str] | None:
    """Python: File "app.py", line 42, in login"""
    m = re.match(r'\s*File "(.+?)", line (\d+)(?:, in (\w+))?', line.strip())
    if m:
        return (m.group(1), int(m.group(2)), m.group(3) or "")
    return None


def _js_traceback_line(line: str) -> tuple[str, int, str] | None:
    """V8: at login (auth.js:42:15)"""
    m = re.match(r'\s*at\s+(?:\w+\s+)?\(?(.+?):(\d+):(\d+)\)?', line.strip())
    if m:
        filepath = m.group(1)
        if filepath.startswith("node:") or filepath.startswith("<"):
            return None
        return (filepath, int(m.group(2)), "")
    return None


def _java_traceback_line(line: str) -> tuple[str, int, str] | None:
    """Java: at com.example.App.main(App.java:10)"""
    m = re.match(r'\s*at\s+(\S+)\.(\S+)\((\S+)\.java:(\d+)\)', line.strip())
    if m:
        return (f"{m.group(3)}.java", int(m.group(4)), f"{m.group(1)}.{m.group(2)}")
    return None


def _go_traceback_line(line: str) -> tuple[str, int, str] | None:
    """Go: main.go:42: main.func()"""
    m = re.match(r'\s*(.+\.go):(\d+)(?::\s*(.+))?', line.strip())
    if m:
        return (m.group(1), int(m.group(2)), (m.group(3) or "").strip())
    return None


def _rust_traceback_line(line: str) -> tuple[str, int, str] | None:
    """Rust: --> src/main.rs:42"""
    m = re.match(r'\s*-->\s+(.+\.rs):(\d+)', line.strip())
    if m:
        return (m.group(1), int(m.group(2)), "")
    return None


# ── base strategy ────────────────────────────────────────────

class LanguageStrategy(ABC):
    """语言策略基类 — 定义所有语言必须/可选实现的数据和行为

    子类只需声明类属性（数据）和覆写方法（行为）。
    """

    # ── 必须声明的类属性 ──
    name: str = ""
    extensions: set[str] = set()
    project_files: list[str] = []
    entry_points: list[str] = []
    test_dirs: list[str] = []
    framework_keywords: dict[str, str] = {}
    comment_syntax: str = "//"
    type_search_patterns: list[str] = []
    traceback_patterns: list[re.Pattern] = []
    error_classifiers: dict[str, str] = {}
    run_command: str | None = None
    compile_command: str | None = None

    # ── 行为方法（子类覆写）──

    def parse_imports(self, filepath: str, work_dir: str) -> list[str]:
        """解析文件的 import/依赖 语句 → 依赖文件列表"""
        return []

    def validate(self, work_dir: str) -> "ValidationResult":
        """验证代码是否可用（编译 + 运行）— 子类必须覆写"""
        from patchflow.core.fix.validator import ValidationResult
        return ValidationResult(ok=True, message=f"No validator for {self.name}", language=self.name)

    def parse_project_meta(self, work_dir: Path) -> dict:
        """从项目配置文件读取元数据（名称、版本、包管理器等）"""
        return {"name": "", "python_version": "", "package_manager": ""}

    def parse_dependencies(self, work_dir: Path) -> list[str]:
        """解析项目依赖列表"""
        return []

    def detect_framework(self, work_dir: Path, deps: list[str]) -> dict | None:
        """检测使用的框架"""
        for kw, fw in self.framework_keywords.items():
            for d in deps:
                if kw in d.lower():
                    return {"name": fw, "language": self.name}
        return None

    def detect_import_style(self, work_dir: Path) -> str:
        """检测 import 风格 (absolute / relative)"""
        return "absolute"

    def detect_naming_convention(self, work_dir: Path) -> str:
        """检测命名惯例 (snake_case / camelCase)"""
        return "camelCase"

    def get_linter_command(self) -> str | None:
        """返回代码检查命令，不支持返回 None"""
        return None

    def find_entry_file(self, work_dir: Path) -> Path | None:
        """查找项目入口文件"""
        for name in self.entry_points:
            candidates = list(work_dir.rglob(name))
            if candidates:
                return candidates[0]
        return None


# ── Python ───────────────────────────────────────────────────

class PythonStrategy(LanguageStrategy):
    name = "python"
    extensions = {".py", ".pyw"}
    project_files = ["pyproject.toml", "setup.py", "requirements.txt", "Pipfile"]
    entry_points = ["app.py", "main.py", "cli.py", "manage.py"]
    test_dirs = ["tests", "test"]
    framework_keywords = {
        "django": "Django", "flask": "Flask", "fastapi": "FastAPI",
        "aiohttp": "Aiohttp", "starlette": "Starlette", "tornado": "Tornado",
        "pytest": "pytest", "unittest": "unittest",
    }
    comment_syntax = "#"
    type_search_patterns = ["class ", "def ", "async def "]
    traceback_patterns = [
        re.compile(r'File "(.+?)", line (\d+)(?:, in (\w+))?'),
        re.compile(r'Traceback\s*\(most recent call last\)'),
    ]
    error_classifiers = {
        "SyntaxError": "syntax", "IndentationError": "syntax",
        "ImportError": "dependency", "ModuleNotFoundError": "dependency",
        "NameError": "runtime", "TypeError": "runtime", "ValueError": "runtime",
        "AttributeError": "runtime", "KeyError": "runtime", "IndexError": "runtime",
        "ZeroDivisionError": "runtime", "FileNotFoundError": "runtime",
        "PermissionError": "runtime", "RuntimeError": "runtime",
    }
    run_command = "python"
    compile_command = None

    def parse_imports(self, filepath: str, work_dir: str) -> list[str]:
        try:
            tree = ast.parse(Path(filepath).read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError, OSError):
            return []
        imports = []
        wd = Path(work_dir).resolve()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    module_path = alias.name.replace(".", "/")
                    for c in [Path(filepath).parent / f"{module_path}.py", wd / f"{module_path}.py"]:
                        try:
                            imports.append(str(c.resolve().relative_to(wd)).replace("\\", "/"))
                            break
                        except (ValueError, OSError):
                            continue
            elif isinstance(node, ast.ImportFrom) and node.module:
                module_path = node.module.replace(".", "/")
                for c in [Path(filepath).parent / f"{module_path}.py", wd / f"{module_path}.py"]:
                    try:
                        rel = str(c.resolve().relative_to(wd)).replace("\\", "/")
                        if rel not in imports:
                            imports.append(rel)
                        break
                    except (ValueError, OSError):
                        continue
        return imports

    def validate(self, work_dir: str) -> "ValidationResult":
        from patchflow.core.analysis.error_parser import parse
        from patchflow.core.fix.validator import ValidationResult
        from patchflow.utils import logger
        from patchflow.utils.runner import run

        wd = Path(work_dir)
        entry = self.find_entry_file(wd)
        if entry is None:
            return ValidationResult(ok=False, error=parse("No entry file found (app.py or main.py)"), language="python")
        logger.step(f"验证入口文件: {entry.name}")
        try:
            source = entry.read_text(encoding="utf-8")
            compile(source, str(entry), "exec")
            logger.info("编译验证通过")
        except SyntaxError as e:
            logger.error(f"编译验证失败: {e}")
            return ValidationResult(ok=False, error=parse(str(e), lang_name="python"), language="python")
        result = run(f"python {shlex.quote(entry.name)}", cwd=str(wd))
        if result.ok:
            logger.success("运行验证通过")
            return ValidationResult(ok=True, language="python")
        error_text = result.stderr.strip() or result.stdout.strip() or "Unknown runtime error"
        logger.error(f"运行验证失败 (exit={result.exit_code})")
        return ValidationResult(ok=False, error=parse(error_text, lang_name="python"), language="python")

    def parse_project_meta(self, work_dir: Path) -> dict:
        meta = {"name": "", "python_version": "", "package_manager": ""}
        pyproject = work_dir / "pyproject.toml"
        if pyproject.exists():
            meta["package_manager"] = "poetry/pdm"
            content = pyproject.read_text(encoding="utf-8")
            for pat, key in [(r'name\s*=\s*"(.+?)"', "name"), (r'requires-python\s*=\s*"(.+?)"', "python_version")]:
                m = re.search(pat, content)
                if m:
                    meta[key] = m.group(1)
        if not meta["name"]:
            setup_py = work_dir / "setup.py"
            if setup_py.exists():
                meta["package_manager"] = "setuptools"
                content = setup_py.read_text(encoding="utf-8")
                m = re.search(r'name\s*=\s*["\'](.+?)["\']', content)
                if m:
                    meta["name"] = m.group(1)
        if (work_dir / "requirements.txt").exists() and not meta["package_manager"]:
            meta["package_manager"] = "pip"
        if (work_dir / "Pipfile").exists():
            meta["package_manager"] = "pipenv"
        return meta

    def parse_dependencies(self, work_dir: Path) -> list[str]:
        deps = []
        pyproject = work_dir / "pyproject.toml"
        if pyproject.exists():
            content = pyproject.read_text(encoding="utf-8")
            deps.extend(re.findall(r'(?:^|\s)([\w-]+)\s*[>=<]', content))
        req = work_dir / "requirements.txt"
        if req.exists():
            deps.extend(re.findall(r'^([\w-]+)', req.read_text(encoding="utf-8"), re.MULTILINE))
        return sorted(set(deps))

    def detect_framework(self, work_dir: Path, deps: list[str]) -> dict | None:
        if (work_dir / "manage.py").exists():
            return {"name": "Django", "language": "python"}
        return super().detect_framework(work_dir, deps)

    def detect_import_style(self, work_dir: Path) -> str:
        py_files = list(work_dir.rglob("*.py"))
        if not py_files:
            return "absolute"
        rel_count = abs_count = 0
        for fp in py_files[:50]:
            try:
                for line in fp.read_text(encoding="utf-8").split("\n")[:10]:
                    line = line.strip()
                    if line.startswith("from ."):
                        rel_count += 1
                    elif line.startswith("from ") or line.startswith("import "):
                        abs_count += 1
            except (UnicodeDecodeError, OSError):
                continue
        return "relative" if rel_count > abs_count else "absolute"

    def detect_naming_convention(self, work_dir: Path) -> str:
        py_files = list(work_dir.rglob("*.py"))
        if not py_files:
            return "snake_case"
        snake = camel = 0
        for fp in py_files[:30]:
            try:
                for line in fp.read_text(encoding="utf-8").split("\n")[:20]:
                    for m in re.finditer(r'\bdef\s+(\w+)', line):
                        name = m.group(1)
                        if "_" in name:
                            snake += 1
                        else:
                            camel += 1
            except (UnicodeDecodeError, OSError):
                continue
        return "snake_case" if snake >= camel else "camelCase"

    def get_linter_command(self) -> str | None:
        return "pylint"


# ── JavaScript ───────────────────────────────────────────────

class JavaScriptStrategy(LanguageStrategy):
    name = "javascript"
    extensions = {".js", ".jsx", ".mjs", ".cjs"}
    project_files = ["package.json"]
    entry_points = ["index.js", "app.js", "server.js", "main.js"]
    test_dirs = ["test", "tests", "__tests__", "spec"]
    framework_keywords = {
        "react": "React", "vue": "Vue.js", "angular": "Angular",
        "express": "Express", "koa": "Koa", "next": "Next.js",
        "nuxt": "Nuxt.js", "svelte": "Svelte",
        "jest": "Jest", "mocha": "Mocha", "vitest": "Vitest",
    }
    comment_syntax = "//"
    type_search_patterns = ["class ", "function ", "const ", "let ", "var "]
    traceback_patterns = [
        re.compile(r'\s*at\s+(?:\w+\s+)?\(?(.+?):(\d+):(\d+)\)?'),
    ]
    error_classifiers = {
        "SyntaxError": "syntax", "ReferenceError": "runtime",
        "TypeError": "runtime", "RangeError": "runtime",
        "URIError": "runtime", "EvalError": "runtime",
    }
    run_command = "node"
    compile_command = None

    def parse_imports(self, filepath: str, work_dir: str) -> list[str]:
        try:
            content = Path(filepath).read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            return []
        imports = []
        wd = Path(work_dir).resolve()
        for m in re.finditer(r"""(?:import|require)\s*\(?['"]([^'"]+)['"]\)?""", content):
            raw = m.group(1)
            if raw.startswith(".") or raw.startswith("/"):
                resolved = (Path(filepath).parent / raw).resolve()
                for ext in (".js", ".jsx", ".ts", ".tsx", "/index.js", "/index.jsx", "/index.ts", "/index.tsx"):
                    candidate = resolved.parent / f"{resolved.name}{ext}"
                    try:
                        rel = candidate.resolve().relative_to(wd)
                        if rel not in imports:
                            imports.append(str(rel).replace("\\", "/"))
                    except (ValueError, OSError):
                        pass
        return imports

    def validate(self, work_dir: str) -> "ValidationResult":
        from patchflow.core.analysis.error_parser import parse
        from patchflow.core.fix.validator import ValidationResult
        from patchflow.utils import logger
        from patchflow.utils.runner import run

        wd = Path(work_dir)
        entry = self.find_entry_file(wd)
        if entry is None:
            return ValidationResult(ok=False, error=parse("No entry file found for JavaScript"), language="javascript")
        logger.step(f"验证入口文件: {entry.name}")
        result = run(f"node {shlex.quote(entry.name)}", cwd=str(wd))
        if result.ok:
            logger.success("运行验证通过")
            return ValidationResult(ok=True, language="javascript")
        error_text = result.stderr.strip() or result.stdout.strip() or f"Runtime error (exit={result.exit_code})"
        logger.error(f"运行验证失败 (exit={result.exit_code})")
        return ValidationResult(ok=False, error=parse(error_text, lang_name="javascript"), language="javascript")

    def parse_project_meta(self, work_dir: Path) -> dict:
        meta = {"name": "", "python_version": "", "package_manager": ""}
        pkg = work_dir / "package.json"
        if pkg.exists():
            meta["package_manager"] = "npm/yarn"
            try:
                content = json.loads(pkg.read_text(encoding="utf-8"))
                meta["name"] = content.get("name", "")
            except (json.JSONDecodeError, OSError):
                pass
        return meta

    def parse_dependencies(self, work_dir: Path) -> list[str]:
        pkg = work_dir / "package.json"
        if not pkg.exists():
            return []
        try:
            data = json.loads(pkg.read_text(encoding="utf-8"))
            deps = list(data.get("dependencies", {}).keys())
            deps.extend(data.get("devDependencies", {}).keys())
            return sorted(set(deps))
        except (json.JSONDecodeError, OSError):
            return []

    def detect_framework(self, work_dir: Path, deps: list[str]) -> dict | None:
        for f in ["next.config.js", "next.config.ts"]:
            if (work_dir / f).exists():
                return {"name": "Next.js", "language": "javascript"}
        return super().detect_framework(work_dir, deps)

    def get_linter_command(self) -> str | None:
        return "eslint"


# ── TypeScript ───────────────────────────────────────────────

class TypeScriptStrategy(JavaScriptStrategy):
    name = "typescript"
    extensions = {".ts", ".tsx"}
    entry_points = ["index.ts", "app.ts", "server.ts", "main.ts"]
    traceback_patterns = [
        re.compile(r'\s*at\s+(?:\w+\s+)?\(?(.+?):(\d+):(\d+)\)?'),
    ]
    error_classifiers = {
        "TS2304": "dependency", "TS2339": "runtime", "TS2345": "runtime",
    }
    run_command = "node"
    compile_command = "tsc"

    def validate(self, work_dir: str) -> "ValidationResult":
        from patchflow.core.analysis.error_parser import parse
        from patchflow.core.fix.validator import ValidationResult
        from patchflow.utils import logger
        from patchflow.utils.runner import run

        wd = Path(work_dir)
        entry = self.find_entry_file(wd)
        if entry is None:
            return ValidationResult(ok=False, error=parse("No entry file found for TypeScript"), language="typescript")
        logger.step(f"验证入口文件: {entry.name}")
        if self.compile_command:
            result = run(f"{self.compile_command} {shlex.quote(entry.name)}", cwd=str(wd))
            if not result.ok:
                error_text = result.stderr.strip() or result.stdout.strip() or "Compilation failed"
                logger.error("编译验证失败")
                return ValidationResult(ok=False, error=parse(error_text, lang_name="typescript"), language="typescript")
        if self.run_command:
            js_entry = entry.stem + ".js"
            result = run(f"{self.run_command} {shlex.quote(js_entry)}", cwd=str(wd))
            if result.ok:
                logger.success("运行验证通过")
                return ValidationResult(ok=True, language="typescript")
            error_text = result.stderr.strip() or result.stdout.strip() or f"Runtime error (exit={result.exit_code})"
            logger.error(f"运行验证失败 (exit={result.exit_code})")
            return ValidationResult(ok=False, error=parse(error_text, lang_name="typescript"), language="typescript")
        return ValidationResult(ok=True, language="typescript")


# ── Java ─────────────────────────────────────────────────────

class JavaStrategy(LanguageStrategy):
    name = "java"
    extensions = {".java"}
    project_files = ["pom.xml", "build.gradle", "build.gradle.kts", "settings.gradle"]
    entry_points = ["Main.java", "Application.java", "App.java"]
    test_dirs = ["src/test", "test"]
    framework_keywords = {
        "spring-boot": "Spring Boot", "spring": "Spring",
        "quarkus": "Quarkus", "micronaut": "Micronaut",
        "junit": "JUnit", "mockito": "Mockito",
    }
    comment_syntax = "//"
    type_search_patterns = ["class ", "interface ", "enum ", "@interface "]
    traceback_patterns = [
        re.compile(r'\s*at\s+(\S+)\.(\S+)\((\S+)\.java:(\d+)\)'),
    ]
    error_classifiers = {
        "NullPointerException": "runtime", "ClassCastException": "runtime",
        "ArrayIndexOutOfBoundsException": "runtime", "IllegalArgumentException": "runtime",
        "ClassNotFoundException": "dependency", "NoClassDefFoundError": "dependency",
    }
    run_command = "java"
    compile_command = "javac"

    def parse_imports(self, filepath: str, work_dir: str) -> list[str]:
        try:
            content = Path(filepath).read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            return []
        imports = []
        wd = Path(work_dir).resolve()
        for m in re.finditer(r'^import\s+([\w.]+)', content, re.MULTILINE):
            fqcn = m.group(1)
            path = fqcn.replace(".", "/") + ".java"
            for src_dir in ("src/main/java", "src/test/java", "src"):
                candidate = wd / src_dir / path
                if candidate.exists():
                    try:
                        imports.append(str(candidate.relative_to(wd)).replace("\\", "/"))
                    except ValueError:
                        pass
            pkg_match = re.search(r'^package\s+([\w.]+)', content, re.MULTILINE)
            if pkg_match:
                simple_name = fqcn.split(".")[-1] + ".java"
                candidate = Path(filepath).parent / simple_name
                if candidate.exists():
                    try:
                        imports.append(str(candidate.resolve().relative_to(wd)).replace("\\", "/"))
                    except (ValueError, OSError):
                        pass
        return imports

    def validate(self, work_dir: str) -> "ValidationResult":
        from patchflow.core.analysis.error_parser import parse
        from patchflow.core.fix.validator import ValidationResult
        from patchflow.utils import logger
        from patchflow.utils.runner import run

        wd = Path(work_dir)
        entry = self.find_entry_file(wd)

        # Maven 项目
        if (wd / "pom.xml").exists():
            logger.info("检测到 Maven 项目，使用 mvn compile 验证")
            result = run("mvn compile -q", cwd=str(wd))
            if result.ok:
                logger.success("Maven 编译验证通过")
                return ValidationResult(ok=True, language="java")
            error_text = result.stderr.strip() or result.stdout.strip() or "Maven compilation failed"
            logger.error(f"Maven 编译验证失败: {error_text[:200]}")
            return ValidationResult(ok=False, error=parse(error_text, lang_name="java"), language="java")

        # Gradle 项目
        if (wd / "build.gradle").exists() or (wd / "build.gradle.kts").exists():
            logger.info("检测到 Gradle 项目，使用 gradle compileJava 验证")
            gradle_cmd = "gradlew.bat compileJava" if _is_windows() else "./gradlew compileJava"
            result = run(gradle_cmd, cwd=str(wd))
            if result.ok:
                logger.success("Gradle 编译验证通过")
                return ValidationResult(ok=True, language="java")
            result = run("gradle compileJava -q", cwd=str(wd))
            if result.ok:
                logger.success("Gradle 编译验证通过")
                return ValidationResult(ok=True, language="java")
            error_text = result.stderr.strip() or result.stdout.strip() or "Gradle compilation failed"
            logger.error(f"Gradle 编译验证失败: {error_text[:200]}")
            return ValidationResult(ok=False, error=parse(error_text, lang_name="java"), language="java")

        # 无构建工具，用 javac/java
        if entry is None:
            logger.info("无 Maven/Gradle 且无入口文件，跳过验证")
            return ValidationResult(ok=True, message="无构建工具，跳过验证", language="java")

        entry_rel = str(entry.relative_to(wd))
        logger.step(f"验证入口文件: {entry_rel}")

        if self.compile_command:
            result = run(f"{self.compile_command} {shlex.quote(entry_rel)}", cwd=str(wd))
            if not result.ok:
                error_text = result.stderr.strip() or result.stdout.strip() or "Compilation failed"
                logger.error("编译验证失败")
                return ValidationResult(ok=False, error=parse(error_text, lang_name="java"), language="java")
        if self.run_command:
            result = run(f"{self.run_command} {shlex.quote(entry.stem)}", cwd=str(wd))
            if result.ok:
                logger.success("运行验证通过")
                return ValidationResult(ok=True, language="java")
            error_text = result.stderr.strip() or result.stdout.strip() or f"Runtime error (exit={result.exit_code})"
            logger.error(f"运行验证失败 (exit={result.exit_code})")
            return ValidationResult(ok=False, error=parse(error_text, lang_name="java"), language="java")
        return ValidationResult(ok=True, language="java")

    def parse_project_meta(self, work_dir: Path) -> dict:
        meta = {"name": "", "python_version": "", "package_manager": ""}
        if (work_dir / "pom.xml").exists():
            meta["package_manager"] = "maven"
            content = (work_dir / "pom.xml").read_text(encoding="utf-8")
            m = re.search(r'<name>(.+?)</name>', content)
            if m:
                meta["name"] = m.group(1)
        elif (work_dir / "build.gradle").exists():
            meta["package_manager"] = "gradle"
        return meta


# ── Go ───────────────────────────────────────────────────────

class GoStrategy(LanguageStrategy):
    name = "go"
    extensions = {".go"}
    project_files = ["go.mod", "go.sum"]
    entry_points = ["main.go"]
    test_dirs = ["test", "tests"]
    framework_keywords = {
        "gin": "Gin", "echo": "Echo", "fiber": "Fiber",
        "chi": "Chi", "gorilla": "Gorilla",
    }
    comment_syntax = "//"
    type_search_patterns = ["type ", "func ", "func (", "struct "]
    traceback_patterns = [
        re.compile(r'\s*(.+\.go):(\d+)(?::\s*(.+))?'),
    ]
    error_classifiers = {
        "undefined": "dependency", "cannot use": "runtime",
        "index out of range": "runtime", "nil pointer": "runtime",
        "cannot unmarshal": "runtime",
    }
    run_command = "go run"
    compile_command = "go build"

    def parse_imports(self, filepath: str, work_dir: str) -> list[str]:
        try:
            content = Path(filepath).read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            return []
        imports = []
        wd = Path(work_dir).resolve()
        for m in re.finditer(r'"([^"]+)"', content):
            imp = m.group(1)
            if "/" in imp and not imp.startswith("."):
                parts = imp.split("/")
                local_dir = wd / parts[-1]
                if local_dir.is_dir():
                    for f in local_dir.rglob("*.go"):
                        try:
                            imports.append(str(f.relative_to(wd)).replace("\\", "/"))
                        except ValueError:
                            pass
        return imports

    def validate(self, work_dir: str) -> "ValidationResult":
        from patchflow.core.analysis.error_parser import parse
        from patchflow.core.fix.validator import ValidationResult
        from patchflow.utils import logger
        from patchflow.utils.runner import run

        wd = Path(work_dir)
        if self.compile_command:
            result = run(self.compile_command, cwd=str(wd))
            if not result.ok:
                error_text = result.stderr.strip() or result.stdout.strip() or "Compilation failed"
                logger.error("编译验证失败")
                return ValidationResult(ok=False, error=parse(error_text, lang_name="go"), language="go")
        logger.success("Go build 验证通过")
        return ValidationResult(ok=True, language="go")

    def parse_project_meta(self, work_dir: Path) -> dict:
        meta = {"name": "", "python_version": "", "package_manager": ""}
        go_mod = work_dir / "go.mod"
        if go_mod.exists():
            meta["package_manager"] = "go modules"
            first_line = go_mod.read_text(encoding="utf-8").split("\n")[0]
            m = re.match(r'module\s+(\S+)', first_line)
            if m:
                meta["name"] = m.group(1)
        return meta

    def detect_framework(self, work_dir: Path, deps: list[str]) -> dict | None:
        for kw, fw in self.framework_keywords.items():
            if kw in str(deps).lower():
                return {"name": fw, "language": self.name}
        return None


# ── Rust ─────────────────────────────────────────────────────

class RustStrategy(LanguageStrategy):
    name = "rust"
    extensions = {".rs"}
    project_files = ["Cargo.toml"]
    entry_points = ["src/main.rs", "src/lib.rs"]
    test_dirs = ["tests"]
    framework_keywords = {
        "actix": "Actix", "rocket": "Rocket", "axum": "Axum",
        "warp": "Warp", "tokio": "Tokio", "serde": "Serde",
    }
    comment_syntax = "//"
    type_search_patterns = ["struct ", "enum ", "trait ", "impl ", "fn "]
    traceback_patterns = [
        re.compile(r'\s*-->\s+(.+\.rs):(\d+)'),
    ]
    error_classifiers = {
        "E0432": "dependency", "E0433": "dependency",
        "E0308": "runtime", "E0599": "runtime",
    }
    run_command = "cargo run"
    compile_command = "cargo build"

    def parse_imports(self, filepath: str, work_dir: str) -> list[str]:
        try:
            content = Path(filepath).read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            return []
        imports = []
        wd = Path(work_dir).resolve()
        for m in re.finditer(r'^mod\s+(\w+)', content, re.MULTILINE):
            mod_name = m.group(1)
            for cand in [Path(filepath).parent / f"{mod_name}.rs",
                         Path(filepath).parent / mod_name / "mod.rs"]:
                try:
                    if cand.exists():
                        imports.append(str(cand.resolve().relative_to(wd)).replace("\\", "/"))
                except (ValueError, OSError):
                    pass
        for m in re.finditer(r'use\s+crate::(\S+)', content):
            parts = m.group(1).split("::")
            candidate = wd / "src" / "/".join(parts[:-1]) / f"{parts[-1]}.rs"
            try:
                if candidate.exists():
                    imports.append(str(candidate.relative_to(wd)).replace("\\", "/"))
            except (ValueError, OSError):
                pass
        return imports

    def validate(self, work_dir: str) -> "ValidationResult":
        from patchflow.core.analysis.error_parser import parse
        from patchflow.core.fix.validator import ValidationResult
        from patchflow.utils import logger
        from patchflow.utils.runner import run

        wd = Path(work_dir)
        if self.compile_command:
            result = run(self.compile_command, cwd=str(wd))
            if not result.ok:
                error_text = result.stderr.strip() or result.stdout.strip() or "Compilation failed"
                logger.error("编译验证失败")
                return ValidationResult(ok=False, error=parse(error_text, lang_name="rust"), language="rust")
        if self.run_command:
            result = run(self.run_command, cwd=str(wd))
            if result.ok:
                logger.success("运行验证通过")
                return ValidationResult(ok=True, language="rust")
            error_text = result.stderr.strip() or result.stdout.strip() or f"Runtime error (exit={result.exit_code})"
            logger.error(f"运行验证失败 (exit={result.exit_code})")
            return ValidationResult(ok=False, error=parse(error_text, lang_name="rust"), language="rust")
        return ValidationResult(ok=True, language="rust")

    def parse_project_meta(self, work_dir: Path) -> dict:
        meta = {"name": "", "python_version": "", "package_manager": ""}
        cargo = work_dir / "Cargo.toml"
        if cargo.exists():
            meta["package_manager"] = "cargo"
            content = cargo.read_text(encoding="utf-8")
            m = re.search(r'name\s*=\s*"(.+?)"', content)
            if m:
                meta["name"] = m.group(1)
        return meta

    def parse_dependencies(self, work_dir: Path) -> list[str]:
        cargo = work_dir / "Cargo.toml"
        if not cargo.exists():
            return []
        deps = []
        in_deps = False
        for line in cargo.read_text(encoding="utf-8").split("\n"):
            line = line.strip()
            if line.startswith("[dependencies"):
                in_deps = True
                continue
            if in_deps and line.startswith("["):
                break
            if in_deps and "=" in line:
                deps.append(line.split("=")[0].strip().replace('"', ''))
        return sorted(set(deps))

    def detect_import_style(self, work_dir: Path) -> str:
        return "absolute"

    def detect_naming_convention(self, work_dir: Path) -> str:
        return "snake_case"


# ── factory ──────────────────────────────────────────────────

class LanguageFactory:
    """语言工厂 — 所有语言策略的注册和查找入口

    使用方式:
        factory = LanguageFactory()
        strategy = factory.get("java")           # 按名称获取
        strategy = factory.detect("/path/to/repo")  # 自动检测
    """

    _instance: "LanguageFactory | None" = None

    def __init__(self):
        self._strategies: dict[str, LanguageStrategy] = {}
        self._ext_index: dict[str, str] = {}  # ext → lang_name
        for cls in _BUILTIN_STRATEGIES:
            self._register(cls())

    def _register(self, strategy: LanguageStrategy):
        self._strategies[strategy.name] = strategy
        for ext in strategy.extensions:
            self._ext_index[ext] = strategy.name

    @classmethod
    def instance(cls) -> "LanguageFactory":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def get(self, name: str) -> LanguageStrategy | None:
        return self._strategies.get(name)

    def detect(self, work_dir: str) -> LanguageStrategy | None:
        """根据项目文件检测语言"""
        wd = Path(work_dir)
        # 1. 按项目描述文件检测 (pom.xml → java, package.json → js/ts, etc.)
        for strategy in self._strategies.values():
            for pf in strategy.project_files:
                if (wd / pf).exists():
                    return strategy
        # 2. 按源文件扩展名检测（启发式）
        ext_counts: dict[str, int] = {}
        for ext, lang_name in self._ext_index.items():
            count = sum(1 for _ in wd.rglob(f"*{ext}"))
            if count > 0:
                ext_counts[lang_name] = ext_counts.get(lang_name, 0) + count
        if ext_counts:
            return self._strategies.get(max(ext_counts, key=ext_counts.get))
        return None

    def detect_by_extension(self, filepath: str) -> LanguageStrategy | None:
        """根据单个文件扩展名检测语言"""
        if filepath.startswith("."):
            ext = filepath.lower()
        else:
            ext = Path(filepath).suffix.lower()
        name = self._ext_index.get(ext)
        return self._strategies.get(name) if name else None

    def all_names(self) -> list[str]:
        return list(self._strategies.keys())

    def all_strategies(self) -> list[LanguageStrategy]:
        return list(self._strategies.values())

    def get_linter_map(self) -> dict[str, str]:
        """返回 extension → linter 映射（用于 code_reviewer）"""
        result: dict[str, str] = {}
        for strategy in self._strategies.values():
            linter = strategy.get_linter_command()
            if linter:
                for ext in strategy.extensions:
                    result[ext] = linter
        return result

    def is_source_extension(self, ext: str) -> bool:
        return ext.lower() in self._ext_index

    @property
    def all_extensions(self) -> set[str]:
        return set(self._ext_index.keys())

    @property
    def source_extensions(self) -> set[str]:
        """优先扫描的源代码扩展名（不含配置文件扩展名）"""
        return {ext for s in self._strategies.values() for ext in s.extensions}


# ── builtin registry ─────────────────────────────────────────

_BUILTIN_STRATEGIES = [
    PythonStrategy,
    JavaScriptStrategy,
    TypeScriptStrategy,
    JavaStrategy,
    GoStrategy,
    RustStrategy,
]

# 树状解析器预热所需的语言列表
WARM_LANGS = ["python", "javascript", "typescript", "java", "c", "cpp", "go", "rust"]
