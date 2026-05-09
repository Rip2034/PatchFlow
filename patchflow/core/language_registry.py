"""语言注册中心 — 多语言支持抽象层

这是 PatchFlow 多语言支持的基石。所有语言相关的特质
（扩展名、traceback 格式、项目文件、构建命令、错误分类等）
集中注册到这里。各模块通过 LanguageRegistry 获取当前项目语言能力。

设计思路：
  不再硬编码 Python 是第一公民。
  新增语言只需在 _BUILTINS 中注册一个 LanguageDescriptor 即可。

内置支持的语言：
  - Python（完整支持：AST 解析、compile + run 验证）
  - JavaScript / TypeScript（V8 traceback 解析、node 运行验证）
  - Java（JVM traceback 解析、javac + java 验证）
  - Go（go mod 检测、go run/build 验证）
  - Rust（cargo 检测、cargo build 验证）

LanguageDescriptor 的核心能力：
  - match_file(): 判断文件是否属于该语言
  - parse_traceback(): 解析该语言的错误栈
  - classify_error(): 识别错误类型（syntax/type/runtime/...）
  - 提供运行/编译命令
"""

import re
import ast
from pathlib import Path


class LanguageDescriptor:
    """语言描述符 — 声明一种语言的全部特质"""

    def __init__(
        self,
        name: str,
        extensions: set[str],
        project_files: list[str],
        traceback_patterns: list[re.Pattern],
        error_classifiers: dict[str, str],
        comment_syntax: str,
        run_command: str | None,
        compile_command: str | None,
    ):
        self.name = name
        self.extensions = extensions
        self.project_files = project_files
        self.traceback_patterns = traceback_patterns
        self.error_classifiers = error_classifiers
        self.comment_syntax = comment_syntax
        self.run_command = run_command
        self.compile_command = compile_command

    def match_file(self, filepath: str) -> bool:
        ext = Path(filepath).suffix.lower()
        return ext in self.extensions

    def parse_traceback(self, error_text: str) -> list[dict] | None:
        """用本语言的 traceback 模式解析错误文本，返回栈帧列表

        每帧格式: {"file": str, "line": int, "function": str}
        解析失败返回 None
        """
        for pattern in self.traceback_patterns:
            frames = []
            for line in error_text.split("\n"):
                m = pattern.search(line)
                if m:
                    frames.append({
                        "file": m.group(1),
                        "line": int(m.group(2)),
                        "function": m.group(3) if m.lastindex >= 3 else "",
                    })
            if frames:
                return frames
        return None

    def classify_error(self, error_text: str) -> tuple[str, str]:
        """从错误文本识别错误类型

        Returns:
            (error_type, root_cause)
            error_type 取值: syntax / type / runtime / import / name /
                             attribute / key_error / index_error /
                             value_error / file_error / assertion / unknown
        """
        for keyword, etype in self.error_classifiers.items():
            if keyword in error_text:
                # 提取错误行（最后一行匹配的行）
                for line in reversed(error_text.strip().split("\n")):
                    if keyword in line:
                        return (etype, line.strip()[:200])
                return (etype, error_text.strip().split("\n")[-1][:200])

        return ("unknown", error_text.strip().split("\n")[-1][:200])


def _python_traceback_line(line: str) -> tuple[str, int, str] | None:
    """Python: File "app.py", line 42, in login"""
    m = re.match(r'\s*File "(.+?)", line (\d+)(?:, in (\w+))?', line.strip())
    if m:
        return (m.group(1), int(m.group(2)), m.group(3) or "")
    return None


def _js_traceback_line(line: str) -> tuple[str, int, str] | None:
    """V8: at login (auth.js:42:15) 或 at auth.js:42:15"""
    m = re.match(r'\s*at\s+(?:\w+\s+)?\(?(.+?):(\d+):(\d+)\)?', line.strip())
    if m:
        # 排除 node:internal 和 <anonymous>
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


def _parse_generic_traceback(frames: list[dict]) -> list[dict] | None:
    """对通用解析结果赋予角色（entry / propagator / crash_site）"""
    if not frames:
        return None
    for i, frame in enumerate(frames):
        if i == len(frames) - 1:
            frame["role"] = "crash_site"
        elif i == 0:
            frame["role"] = "entry"
        else:
            frame["role"] = "propagator"
    return frames


def _collect_python_imports(filepath: str, work_dir: str) -> list[str]:
    """AST 解析 Python 文件的 import 语句"""
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
                candidates = [
                    Path(filepath).parent / f"{module_path}.py",
                    wd / f"{module_path}.py",
                ]
                for c in candidates:
                    try:
                        rel = c.resolve().relative_to(wd)
                        imports.append(str(rel).replace("\\", "/"))
                        break
                    except (ValueError, OSError):
                        continue
        elif isinstance(node, ast.ImportFrom) and node.module:
            module_path = node.module.replace(".", "/")
            candidates = [
                Path(filepath).parent / f"{module_path}.py",
                wd / f"{module_path}.py",
            ]
            for c in candidates:
                try:
                    rel = c.resolve().relative_to(wd)
                    if rel not in imports:
                        imports.append(str(rel).replace("\\", "/"))
                    break
                except (ValueError, OSError):
                    continue
    return imports


def _collect_js_imports(filepath: str, work_dir: str) -> list[str]:
    """正则解析 JS/TS 文件的 import/require"""
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
            for ext in (".js", ".jsx", ".ts", ".tsx", "/index.js",
                        "/index.jsx", "/index.ts", "/index.tsx"):
                candidate = resolved.parent / f"{resolved.name}{ext}"
                try:
                    rel = candidate.resolve().relative_to(wd)
                    if rel not in imports:
                        imports.append(str(rel).replace("\\", "/"))
                except (ValueError, OSError):
                    pass
    return imports


def _collect_java_imports(filepath: str, work_dir: str) -> list[str]:
    """正则解析 Java 文件的 import 语句"""
    try:
        content = Path(filepath).read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        return []

    imports = []
    wd = Path(work_dir).resolve()
    for m in re.finditer(r'^import\s+([\w.]+)', content, re.MULTILINE):
        fqcn = m.group(1)
        # 转换 com.example.utils.StringUtil → src/main/java/com/example/utils/StringUtil.java
        path = fqcn.replace(".", "/") + ".java"
        for src_dir in ("src/main/java", "src/test/java", "src"):
            candidate = wd / src_dir / path
            if candidate.exists():
                try:
                    rel = candidate.relative_to(wd)
                    imports.append(str(rel).replace("\\", "/"))
                except ValueError:
                    pass
        # 同包内的类引用也添加
        pkg_match = re.search(r'^package\s+([\w.]+)', content, re.MULTILINE)
        if pkg_match:
            pkg = pkg_match.group(1).replace(".", "/")
            simple_name = fqcn.split(".")[-1] + ".java"
            candidate = Path(filepath).parent / simple_name
            if candidate.exists():
                try:
                    rel = candidate.resolve().relative_to(wd)
                    imports.append(str(rel).replace("\\", "/"))
                except (ValueError, OSError):
                    pass
    return imports


def _collect_go_imports(filepath: str, work_dir: str) -> list[str]:
    """正则解析 Go 文件的 import 语句"""
    try:
        content = Path(filepath).read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        return []

    imports = []
    wd = Path(work_dir).resolve()
    for m in re.finditer(r'"([^"]+)"', content):
        pkg = m.group(1)
        if not pkg.startswith("github.") and not pkg.startswith("golang.") and "/" not in pkg:
            continue
        # 本地包引用 → 映射为文件路径
        for candidate_dir in wd.rglob(f"*{pkg.split('/')[-1]}"):
            if candidate_dir.is_dir() and any(candidate_dir.glob("*.go")):
                for gf in candidate_dir.glob("*.go"):
                    try:
                        rel = gf.relative_to(wd)
                        imports.append(str(rel).replace("\\", "/"))
                    except ValueError:
                        pass
    return imports


def _collect_rust_imports(filepath: str, work_dir: str) -> list[str]:
    """解析 Rust 文件的 mod/use 声明"""
    try:
        content = Path(filepath).read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        return []

    imports = []
    wd = Path(work_dir).resolve()
    file_dir = Path(filepath).parent

    # mod xxx; → 同目录下的 xxx.rs 或 xxx/mod.rs
    for m in re.finditer(r'^\s*(?:pub\s+)?mod\s+(\w+)', content, re.MULTILINE):
        mod_name = m.group(1)
        for candidate in (
            file_dir / f"{mod_name}.rs",
            file_dir / mod_name / "mod.rs",
        ):
            try:
                if candidate.resolve().is_file():
                    rel = candidate.resolve().relative_to(wd)
                    imports.append(str(rel).replace("\\", "/"))
            except (ValueError, OSError):
                pass

    # use crate::xxx::yyy → 映射为文件路径
    for m in re.finditer(r'use\s+crate::([\w:]+)', content):
        crate_path = m.group(1).replace("::", "/")
        candidate = wd / "src" / f"{crate_path}.rs"
        if candidate.exists():
            try:
                rel = candidate.relative_to(wd)
                imports.append(str(rel).replace("\\", "/"))
            except ValueError:
                pass
    return imports


# ── 内置语言注册 ──

_BUILTINS: dict[str, LanguageDescriptor] = {}

def _register_builtins():
    global _BUILTINS

    _BUILTINS["python"] = LanguageDescriptor(
        name="python",
        extensions={".py", ".pyw"},
        project_files=["pyproject.toml", "setup.py", "requirements.txt", "Pipfile"],
        traceback_patterns=[
            re.compile(r'\s*File "(.+?)", line (\d+)(?:, in (\w+))?'),
        ],
        error_classifiers={
            "SyntaxError": "syntax",
            "IndentationError": "syntax",
            "TypeError": "type",
            "ImportError": "import",
            "ModuleNotFoundError": "import",
            "NameError": "name",
            "AttributeError": "attribute",
            "KeyError": "key_error",
            "IndexError": "index_error",
            "ValueError": "value_error",
            "ZeroDivisionError": "zero_division",
            "FileNotFoundError": "file_error",
            "FileExistsError": "file_error",
            "AssertionError": "assertion",
            "RuntimeError": "runtime",
            "Exception": "runtime",
        },
        comment_syntax="#",
        run_command="python",
        compile_command=None,
    )

    _BUILTINS["javascript"] = LanguageDescriptor(
        name="javascript",
        extensions={".js", ".jsx", ".mjs", ".cjs"},
        project_files=["package.json"],
        traceback_patterns=[
            # at login (auth.js:42:15) — group(1)=file, group(2)=line
            re.compile(r'\s*at\s+(?:.+?\s+)?\(?([^:()]+):(\d+):\d+\)?'),
        ],
        error_classifiers={
            "TypeError": "type",
            "ReferenceError": "name",
            "SyntaxError": "syntax",
            "RangeError": "value_error",
            "URIError": "runtime",
            "EvalError": "runtime",
        },
        comment_syntax="//",
        run_command="node",
        compile_command=None,
    )

    _BUILTINS["typescript"] = LanguageDescriptor(
        name="typescript",
        extensions={".ts", ".tsx"},
        project_files=["tsconfig.json", "package.json"],
        traceback_patterns=[
            # Same V8 format as JS: at login (auth.ts:42:15) — group(1)=file, group(2)=line
            re.compile(r'\s*at\s+(?:.+?\s+)?\(?([^:()]+):(\d+):\d+\)?'),
        ],
        error_classifiers={
            "TS": "syntax",
            "TypeError": "type",
            "ReferenceError": "name",
        },
        comment_syntax="//",
        run_command="node",
        compile_command="tsc",
    )

    _BUILTINS["java"] = LanguageDescriptor(
        name="java",
        extensions={".java"},
        project_files=["pom.xml", "build.gradle", "build.gradle.kts"],
        traceback_patterns=[
            # at com.example.App.main(App.java:10) — group(1)=App.java, group(2)=10
            re.compile(r'\s*at\s+(?:.+?\s+)?\(?([^:()]+):(\d+)\)?'),
        ],
        error_classifiers={
            "NullPointerException": "attribute",
            "ArrayIndexOutOfBoundsException": "index_error",
            "ClassNotFoundException": "import",
            "NoClassDefFoundError": "import",
            "ArithmeticException": "zero_division",
            "IllegalArgumentException": "value_error",
            "NumberFormatException": "value_error",
        },
        comment_syntax="//",
        run_command="java",
        compile_command="javac",
    )

    _BUILTINS["go"] = LanguageDescriptor(
        name="go",
        extensions={".go"},
        project_files=["go.mod", "go.sum"],
        traceback_patterns=[
            re.compile(r'\s*(.+\.go):(\d+)(?::\s*(.+))?'),
        ],
        error_classifiers={
            "nil pointer": "attribute",
            "index out of range": "index_error",
            "panic": "runtime",
            "syntax error": "syntax",
            "undefined": "name",
        },
        comment_syntax="//",
        run_command="go run",
        compile_command="go build",
    )

    _BUILTINS["rust"] = LanguageDescriptor(
        name="rust",
        extensions={".rs"},
        project_files=["Cargo.toml"],
        traceback_patterns=[
            re.compile(r'\s*-->\s+(.+\.rs):(\d+)'),
        ],
        error_classifiers={
            "error[E0425]": "name",
            "error[E0308]": "type",
            "error[E0432]": "import",
            "panic": "runtime",
        },
        comment_syntax="//",
        run_command="cargo run",
        compile_command="cargo build",
    )


_register_builtins()


class LanguageRegistry:
    """语言注册中心 — 单例，管理所有已注册语言"""

    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._languages = dict(_BUILTINS)
        return cls._instance

    def register(self, lang: LanguageDescriptor):
        self._languages[lang.name] = lang

    def get(self, name: str) -> LanguageDescriptor | None:
        return self._languages.get(name)

    def all(self) -> dict[str, LanguageDescriptor]:
        return dict(self._languages)

    def detect(self, work_dir: str = ".") -> LanguageDescriptor | None:
        """Auto-detect project language by scanning project files

        检测逻辑（优先级）：
          1. 通过 project_files 匹配（如 pyproject.toml → Python）
          2. 按文件扩展名出现频率判断
          3. 都检测不到 → 返回 None（让 LLM 自己判断）

        Returns:
            LanguageDescriptor | None — None 表示无法确定语言
        """
        wd = Path(work_dir).resolve()

        scored = []
        for name, lang in self._languages.items():
            score = 0
            for pf in lang.project_files:
                if (wd / pf).exists():
                    score += 1
            if score > 0:
                scored.append((score, name, lang))

        if scored:
            scored.sort(key=lambda x: -x[0])
            return scored[0][2]

        ext_count: dict[str, int] = {}
        for f in wd.rglob("*"):
            if f.is_file() and not f.name.startswith("."):
                ext = f.suffix.lower()
                if ext:
                    ext_count[ext] = ext_count.get(ext, 0) + 1

        if ext_count:
            best_ext = max(ext_count, key=ext_count.get)
            for lang in self._languages.values():
                if best_ext in lang.extensions:
                    return lang

        return None

    def detect_from_files(self, files: list[str]) -> LanguageDescriptor | None:
        """从文件列表检测语言"""
        ext_count: dict[str, int] = {}
        for f in files:
            ext = Path(f).suffix.lower()
            if ext:
                ext_count[ext] = ext_count.get(ext, 0) + 1

        if ext_count:
            best_ext = max(ext_count, key=ext_count.get)
            for lang in self._languages.values():
                if best_ext in lang.extensions:
                    return lang
        return None

    def parse_traceback(self, error_text: str, lang: LanguageDescriptor | None = None) -> list[dict] | None:
        """用语言感知方式解析错误 traceback

        如果提供了 lang，优先用该语言的模式。
        否则依次尝试所有已注册语言，取第一个匹配的。
        """
        if lang:
            frames = lang.parse_traceback(error_text)
            if frames:
                return _parse_generic_traceback(frames)

        for l in self._languages.values():
            if lang and l.name == lang.name:
                continue
            frames = l.parse_traceback(error_text)
            if frames:
                return _parse_generic_traceback(frames)
        return None

    def classify_error(self, error_text: str, lang: LanguageDescriptor | None = None) -> tuple[str, str]:
        """用语言感知方式分类错误"""
        if lang:
            etype, msg = lang.classify_error(error_text)
            if etype != "unknown":
                return (etype, msg)

        # 兜底：尝试所有语言
        for l in self._languages.values():
            if lang and l.name == lang.name:
                continue
            etype, msg = l.classify_error(error_text)
            if etype != "unknown":
                return (etype, msg)
        return ("unknown", error_text.strip().split("\n")[-1][:200])

    def get_import_parser(self, lang: LanguageDescriptor | None):
        """获取对应语言的 import 解析函数

        返回: callable(filepath, work_dir) -> list[str]
        """
        if lang is None:
            return _collect_python_imports
        parsers = {
            "python": _collect_python_imports,
            "javascript": _collect_js_imports,
            "typescript": _collect_js_imports,
            "java": _collect_java_imports,
            "go": _collect_go_imports,
            "rust": _collect_rust_imports,
        }
        return parsers.get(lang.name, lambda fp, wd: [])
