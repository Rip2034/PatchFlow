"""Context Collector — 项目上下文收集器（三层架构 · 多语言）

核心问题：AI 不了解项目全貌时，生成的代码要么风格不匹配，
要么使用不存在的依赖，要么文件结构不对。

解决方案：三层架构（借鉴自设计文档）

  Layer 1: 确定性扫描（程序做，永远不错）
    文件结构 / 依赖列表 / 代码风格（缩进、命名约定、import 风格）/ 入口点
    → 从 pyproject.toml / package.json / AST / os.walk 来
    → 不依赖 AI，100% 确定

  Layer 2: AI 语义建模（AI 做，需要人把关）
    项目是做什么的？模块间的业务关系？
    → AI 基于 Layer 1 的事实推断
    → 目前 Phase 4 尚未实现，留作扩展

  Layer 3: 用户确认（人做）
    AI 模型展示 → 用户审查、修改、确认
    → 确认后缓存到 .patchflow/context.json
    → 项目结构大变才重新询问（_should_rebuild 判断）

当前实现只做了 Layer 1（确定性扫描），Layer 2/3 预留接口。
"""

import json
import os
import re
import ast
from pathlib import Path
from datetime import datetime

from patchflow.core.language_registry import LanguageRegistry
from patchflow.utils import logger

IGNORE_DIRS = {
    ".git", "node_modules", "__pycache__", ".idea", ".vscode",
    ".venv", "venv", ".env", "build", "dist", ".next", ".nuxt",
    ".turbo", "target", ".tox", ".eggs", "*.egg-info",
    ".patchflow", ".mypy_cache", ".pytest_cache",
    "vendor", "bundle", ".bundle",
}


class ProjectContext:
    """结构化的项目上下文"""

    def __init__(self):
        self.project = {
            "name": "",
            "language": "python",
            "framework": "",
            "package_manager": "",
            "python_version": "",
        }
        self.structure = {
            "modules": [],
            "entry_point": "",
            "test_dir": "",
            "total_files": 0,
            "total_dirs": 0,
            "source_extensions": [],
        }
        self.dependencies = {
            "runtime": [],
            "dev": [],
        }
        self.code_style = {
            "indent": 4,
            "naming": "snake_case",
            "import_style": "absolute",
        }
        self.business = {}
        self._raw = {}

    def to_dict(self) -> dict:
        return {
            "project": self.project,
            "structure": self.structure,
            "dependencies": self.dependencies,
            "code_style": self.code_style,
            "business": self.business,
        }

    def to_prompt(self) -> str:
        """格式化为 LLM 可读的上下文文本"""
        parts = []
        p = self.project
        parts.append(f"Language: {p['language']}")
        if p["framework"]:
            parts.append(f"Framework: {p['framework']}")
        if p["package_manager"]:
            parts.append(f"Package Manager: {p['package_manager']}")
        if p["python_version"]:
            parts.append(f"Python: {p['python_version']}")
        if p["name"]:
            parts.append(f"Project: {p['name']}")

        s = self.structure
        if s["modules"]:
            parts.append(f"Modules: {', '.join(s['modules'][:10])}")
        if s["entry_point"]:
            parts.append(f"Entry: {s['entry_point']}")
        if s["total_files"]:
            parts.append(f"Files: {s['total_files']}")

        if self.dependencies["runtime"]:
            deps = ", ".join(self.dependencies["runtime"][:15])
            parts.append(f"Dependencies: {deps}")

        if self.business:
            domain = self.business.get("domain", "")
            if domain:
                parts.append(f"Domain: {domain}")

        return "\n".join(parts)

    def __repr__(self):
        n = self.project.get("name", "?")
        f = self.project.get("framework", "?")
        m = len(self.structure.get("modules", []))
        return f"ProjectContext({n}, {f}, {m} modules)"


def _find_project_meta(work_dir: Path, lang_name: str) -> dict:
    """从项目配置文件读取元数据（多语言）"""
    meta = {"name": "", "python_version": "", "package_manager": ""}

    if lang_name == "python":
        pyproject = work_dir / "pyproject.toml"
        if pyproject.exists():
            meta["package_manager"] = "poetry/pdm"
            content = pyproject.read_text(encoding="utf-8")
            m = re.search(r'name\s*=\s*"(.+?)"', content)
            if m:
                meta["name"] = m.group(1)
            m = re.search(r'requires-python\s*=\s*"(.+?)"', content)
            if m:
                meta["python_version"] = m.group(1)

        if not meta["name"]:
            setup_py = work_dir / "setup.py"
            if setup_py.exists():
                meta["package_manager"] = "setuptools"
                content = setup_py.read_text(encoding="utf-8")
                m = re.search(r'name\s*=\s*["\'](.+?)["\']', content)
                if m:
                    meta["name"] = m.group(1)

        req = work_dir / "requirements.txt"
        if req.exists():
            if not meta["package_manager"]:
                meta["package_manager"] = "pip"

        pipfile = work_dir / "Pipfile"
        if pipfile.exists():
            meta["package_manager"] = "pipenv"

    elif lang_name in ("javascript", "typescript"):
        pkg = work_dir / "package.json"
        if pkg.exists():
            meta["package_manager"] = "npm/yarn"
            try:
                content = json.loads(pkg.read_text(encoding="utf-8"))
                meta["name"] = content.get("name", "")
            except (json.JSONDecodeError, OSError):
                pass

    elif lang_name == "java":
        if (work_dir / "pom.xml").exists():
            meta["package_manager"] = "maven"
            content = (work_dir / "pom.xml").read_text(encoding="utf-8")
            m = re.search(r'<name>(.+?)</name>', content)
            if m:
                meta["name"] = m.group(1)
        elif (work_dir / "build.gradle").exists():
            meta["package_manager"] = "gradle"

    elif lang_name == "go":
        go_mod = work_dir / "go.mod"
        if go_mod.exists():
            meta["package_manager"] = "go modules"
            first_line = go_mod.read_text(encoding="utf-8").split("\n")[0]
            m = re.match(r'module\s+(\S+)', first_line)
            if m:
                meta["name"] = m.group(1)

    elif lang_name == "rust":
        cargo = work_dir / "Cargo.toml"
        if cargo.exists():
            meta["package_manager"] = "cargo"
            content = cargo.read_text(encoding="utf-8")
            m = re.search(r'name\s*=\s*"(.+?)"', content)
            if m:
                meta["name"] = m.group(1)

    return meta


def _detect_framework(work_dir: Path, deps: list[str], lang_name: str) -> str:
    """根据依赖推断框架（多语言）"""
    framework_keywords = {
        "python": {
            "fastapi": "fastapi",
            "django": "django",
            "flask": "flask",
            "aiohttp": "aiohttp",
            "tornado": "tornado",
            "sanic": "sanic",
        },
        "javascript": {
            "express": "express",
            "react": "react",
            "next": "next.js",
            "vue": "vue",
            "angular": "angular",
            "svelte": "svelte",
        },
        "typescript": {
            "express": "express",
            "react": "react",
            "next": "next.js",
            "vue": "vue",
            "angular": "angular",
            "svelte": "svelte",
            "nestjs": "nestjs",
        },
        "java": {
            "spring": "spring",
            "spring-boot": "spring boot",
            "spring-boot-starter": "spring boot",
            "jakarta": "jakarta ee",
            "javax": "java ee",
        },
        "rust": {
            "actix": "actix",
            "axum": "axum",
            "rocket": "rocket",
            "tokio": "tokio",
        },
        "go": {
            "gin": "gin",
            "echo": "echo",
            "fiber": "fiber",
            "chi": "chi",
        },
    }

    keywords = framework_keywords.get(lang_name, {})
    for dep in deps:
        dep_lower = dep.lower()
        for kw, framework in keywords.items():
            if kw in dep_lower:
                return framework

    files = [f.name.lower() for f in work_dir.iterdir() if f.is_file()]
    if lang_name == "python":
        if "manage.py" in files:
            return "django"
        if "app.py" in files or "main.py" in files:
            return None
    elif lang_name == "javascript":
        if "next.config.js" in files or "next.config.ts" in files:
            return "next.js"
    elif lang_name == "typescript":
        if "next.config.ts" in files or "next.config.js" in files:
            return "next.js"

    return None


def _parse_dependencies(work_dir: Path, lang_name: str) -> tuple[list[str], list[str]]:
    """解析项目依赖（多语言）"""
    runtime = []
    dev = []

    if lang_name == "python":
        pyproject = work_dir / "pyproject.toml"
        if pyproject.exists():
            content = pyproject.read_text(encoding="utf-8")
            in_runtime = False
            in_dev = False
            for line in content.split("\n"):
                stripped = line.strip()
                if stripped.startswith("dependencies"):
                    in_runtime = True
                    in_dev = False
                    continue
                if stripped.startswith("[tool"):
                    in_runtime = False
                    in_dev = False
                if stripped.startswith("dev") and "[" in stripped:
                    in_dev = True
                    in_runtime = False
                    continue
                if in_runtime:
                    m = re.match(r'["\']([\w-]+)', stripped)
                    if m:
                        runtime.append(m.group(1))
                if in_dev:
                    m = re.match(r'["\']([\w-]+)', stripped)
                    if m:
                        dev.append(m.group(1))

        req = work_dir / "requirements.txt"
        if req.exists() and not runtime:
            for line in req.read_text(encoding="utf-8").split("\n"):
                stripped = line.strip()
                if stripped and not stripped.startswith("#"):
                    pkg = re.split(r"[=<>~!]", stripped)[0].strip()
                    if pkg:
                        runtime.append(pkg)

    elif lang_name in ("javascript", "typescript"):
        pkg = work_dir / "package.json"
        if pkg.exists():
            try:
                content = json.loads(pkg.read_text(encoding="utf-8"))
                runtime.extend(content.get("dependencies", {}).keys())
                dev.extend(content.get("devDependencies", {}).keys())
            except (json.JSONDecodeError, OSError):
                pass

    elif lang_name == "rust":
        cargo = work_dir / "Cargo.toml"
        if cargo.exists():
            content = cargo.read_text(encoding="utf-8")
            for line in content.split("\n"):
                m = re.match(r'^([\w-]+)\s*=', line.strip())
                if m:
                    runtime.append(m.group(1))

    return runtime, dev


def _detect_import_style(work_dir: Path, lang_name: str) -> str:
    """从源码中检测 import 风格"""
    if lang_name == "python":
        relative_count = 0
        absolute_count = 0
        for py_file in work_dir.rglob("*.py"):
            if any(part.startswith(".") for part in py_file.parts):
                continue
            try:
                tree = ast.parse(py_file.read_text(encoding="utf-8"))
                for node in ast.walk(tree):
                    if isinstance(node, ast.ImportFrom) and node.module:
                        if any(p.startswith(".") for p in (node.module.split(".") if node.module else [])):
                            relative_count += 1
                        else:
                            absolute_count += 1
            except (SyntaxError, UnicodeDecodeError, OSError):
                continue
        return "relative" if relative_count > absolute_count else "absolute"
    return "absolute"


def _detect_naming_convention(work_dir: Path, lang_name: str) -> str:
    """检测命名约定（多语言）"""
    if lang_name == "python":
        snake = 0
        camel = 0
        for py_file in work_dir.rglob("*.py"):
            if any(part.startswith(".") for part in py_file.parts):
                continue
            try:
                tree = ast.parse(py_file.read_text(encoding="utf-8"))
                for node in ast.walk(tree):
                    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        if re.match(r"^[a-z][a-z0-9_]*$", node.name):
                            snake += 1
                        elif re.match(r"^[A-Z][a-zA-Z0-9]*$", node.name):
                            camel += 1
            except (SyntaxError, UnicodeDecodeError, OSError):
                continue
        return "snake_case" if snake >= camel else "camelCase"
    return "camelCase"


def _detect_indent(work_dir: Path, lang_name: str) -> int:
    """检测缩进风格（多语言）"""
    reg = LanguageRegistry()
    lang = reg.get(lang_name)
    exts = lang.extensions if lang else {".py"}

    spaces_count = 0
    tab_count = 0
    for f in work_dir.rglob("*"):
        if f.suffix.lower() not in exts:
            continue
        if any(part.startswith(".") for part in f.parts):
            continue
        try:
            for line in f.read_text(encoding="utf-8").split("\n"):
                stripped = line.rstrip()
                if not stripped:
                    continue
                leading = line[:len(line) - len(stripped)]
                if "\t" in leading:
                    tab_count += 1
                else:
                    spaces_count += len(leading)
        except (OSError, UnicodeDecodeError):
            continue
    if tab_count > spaces_count // 4:
        return 0
    avg_spaces = spaces_count / max(1, spaces_count)
    if avg_spaces > 3:
        return 4
    return 2


def _scan_structure(work_dir: Path, lang_name: str) -> dict:
    """扫描项目文件结构（多语言）"""
    reg = LanguageRegistry()
    lang = reg.get(lang_name)
    exts = lang.extensions if lang else {".py"}

    modules = set()
    total_files = 0
    total_dirs = 0
    entry_points = []

    def _walk(d: Path, depth: int = 0):
        nonlocal total_files, total_dirs
        if depth > 5:
            return
        try:
            entries = sorted(d.iterdir())
        except PermissionError:
            return
        for entry in entries:
            name = entry.name
            if name.startswith(".") and name not in (".env", ".gitignore", ".gitattributes"):
                continue
            if entry.is_dir():
                if name in IGNORE_DIRS:
                    continue
                total_dirs += 1
                _walk(entry, depth + 1)
            elif entry.suffix.lower() in exts:
                total_files += 1
                rel = entry.relative_to(work_dir)
                parts = rel.parts
                if len(parts) > 1:
                    modules.add(parts[0])

    _walk(work_dir)

    # 入口文件检测（语言相关）
    entry_checks = {
        "python": ["main.py", "app.py", "manage.py", "cli.py"],
        "javascript": ["index.js", "app.js", "server.js", "main.js"],
        "typescript": ["index.ts", "app.ts", "server.ts", "main.ts"],
        "java": ["Main.java", "Application.java", "App.java"],
        "go": ["main.go"],
        "rust": ["main.rs", "lib.rs"],
    }
    for name in entry_checks.get(lang_name, ["main.py", "app.py"]):
        if (work_dir / name).exists():
            entry_points.append(name)
            break

    test_dir = ""
    test_dir_names = {
        "python": ["tests", "test"],
        "javascript": ["test", "tests", "__tests__", "spec"],
        "typescript": ["test", "tests", "__tests__", "spec"],
        "java": ["src/test", "test"],
        "go": ["test", "tests"],
    }
    for name in test_dir_names.get(lang_name, ["tests", "test"]):
        if (work_dir / name).is_dir():
            test_dir = name
            break

    return {
        "modules": sorted(modules),
        "entry_point": entry_points[0] if entry_points else "",
        "entry_points": entry_points,
        "test_dir": test_dir,
        "total_files": total_files,
        "total_dirs": total_dirs,
        "source_extensions": list(exts),
    }


def _should_rebuild(cached: dict, fresh: dict) -> bool:
    """判断项目结构是否大变，决定是否重建缓存"""
    cached_modules = set(cached.get("structure", {}).get("modules", []))
    fresh_modules = set(fresh.get("structure", {}).get("modules", []))

    cached_files = cached.get("structure", {}).get("total_files", 0)
    fresh_files = fresh.get("structure", {}).get("total_files", 0)

    added = fresh_modules - cached_modules
    removed = cached_modules - fresh_modules

    threshold = max(1, len(cached_modules) * 0.3)
    if len(added) > threshold or len(removed) > threshold:
        return True

    if abs(fresh_files - cached_files) > max(10, cached_files * 0.2):
        return True

    return False


class ContextCollector:
    """项目上下文收集器（多语言）"""

    def __init__(self, work_dir: str = "."):
        self.work_dir = Path(work_dir).resolve()
        self.cache_path = self.work_dir / ".patchflow" / "context.json"
        self._detected_lang = None

    @property
    def language(self) -> str:
        if self._detected_lang is None:
            reg = LanguageRegistry()
            lang = reg.detect(str(self.work_dir))
            self._detected_lang = lang.name if lang else ""
        return self._detected_lang

    def collect(self, use_cache: bool = True) -> ProjectContext:
        """收集项目上下文

        Args:
            use_cache: 是否使用缓存的上下文

        Returns:
            结构化的项目上下文
        """
        facts = self._scan_layer1()

        if use_cache:
            cached = self._load_cache()
            if cached and not _should_rebuild(cached, facts):
                ctx = ProjectContext()
                ctx.__dict__.update(cached)
                return ctx

        ctx = ProjectContext()
        for key, value in facts.items():
            setattr(ctx, key, value)

        self._save_cache(ctx)
        return ctx

    def _scan_layer1(self) -> dict:
        """Layer 1: 确定性扫描"""
        lang_name = self.language
        meta = _find_project_meta(self.work_dir, lang_name)
        runtime_deps, dev_deps = _parse_dependencies(self.work_dir, lang_name)
        framework = _detect_framework(self.work_dir, runtime_deps, lang_name)
        structure = _scan_structure(self.work_dir, lang_name)

        code_style = {
            "indent": _detect_indent(self.work_dir, lang_name),
            "naming": _detect_naming_convention(self.work_dir, lang_name),
            "import_style": _detect_import_style(self.work_dir, lang_name),
        }

        return {
            "project": {
                "name": meta["name"],
                "language": lang_name,
                "framework": framework or "",
                "package_manager": meta["package_manager"],
                "python_version": meta["python_version"],
            },
            "structure": structure,
            "dependencies": {
                "runtime": runtime_deps,
                "dev": dev_deps,
            },
            "code_style": code_style,
            "business": {},
        }

    def _load_cache(self) -> dict | None:
        if not self.cache_path.exists():
            return None
        try:
            return json.loads(self.cache_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return None

    def _save_cache(self, ctx: ProjectContext):
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        data = ctx.to_dict()
        data["_cached_at"] = datetime.now().isoformat()
        self.cache_path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        logger.info(f"上下文已缓存: {self.cache_path}")


def build_context_prompt(context: ProjectContext) -> str:
    """把项目上下文格式化为 LLM prompt 段"""
    parts = []
    p = context.project

    lines = ["=== Project Context ==="]
    lines.append(f"Language: {p['language']}")
    if p.get("name"):
        lines.append(f"Project: {p['name']}")
    if p.get("framework"):
        lines.append(f"Framework: {p['framework']}")
    if p.get("package_manager"):
        lines.append(f"Package Manager: {p['package_manager']}")
    if p.get("python_version"):
        lines.append(f"Python: {p['python_version']}")

    s = context.structure
    if s.get("entry_point"):
        lines.append(f"Entry Point: {s['entry_point']}")
    if s.get("modules"):
        lines.append(f"Modules: {', '.join(s['modules'][:10])}")
    if s.get("test_dir"):
        lines.append(f"Tests: {s['test_dir']}")

    deps = context.dependencies.get("runtime", [])
    if deps:
        lines.append(f"Dependencies: {', '.join(deps[:15])}{'...' if len(deps) > 15 else ''}")

    cs = context.code_style
    indent_str = "tab" if cs.get("indent") == 0 else f"{cs.get('indent', 4)} spaces"
    lines.append(f"Style: {indent_str}, {cs.get('naming', 'snake_case')}, {cs.get('import_style', 'absolute')} imports")

    bus = context.business
    if bus.get("domain"):
        lines.append(f"Domain: {bus['domain']}")

    lines.append("=== End Context ===\n")
    return "\n".join(lines)
