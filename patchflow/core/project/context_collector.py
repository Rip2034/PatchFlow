"""Context Collector — 项目上下文收集器（三层架构 · 多语言）

核心问题：AI 不了解项目全貌时，生成的代码要么风格不匹配，
要么使用不存在的依赖，要么文件结构不对。

三层架构：
  Layer 1: 确定性扫描（程序做，永远不错）
  Layer 2: AI 语义建模（AI 做，需要人把关）
  Layer 3: 用户确认（人做）

所有语言相关的逻辑已委托给 LanguageStrategy 子类。
"""

import json
from datetime import datetime
from pathlib import Path

from patchflow.core.language_strategy import LanguageFactory
from patchflow.core.language_registry import LanguageRegistry
from patchflow.utils import logger

IGNORE_DIRS = {
    ".git", "node_modules", "__pycache__", ".idea", ".vscode",
    ".venv", "venv", "env", ".env", "build", "dist", ".next", ".nuxt",
    ".turbo", "target", ".tox", ".eggs", "*.egg-info",
    ".patchflow", ".mypy_cache", ".pytest_cache",
    "vendor", "bundle", ".bundle",
    ".gradle", "gradle", "bower_components",
    "__generated__", "generated", "gen",
    "Pods", "Carthage", ".terraform", ".serverless", "cdk.out",
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


def _detect_indent(work_dir: Path, exts: set[str]) -> int:
    """检测缩进风格"""
    spaces_lines = 0
    spaces_total = 0
    tab_lines = 0
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
                    tab_lines += 1
                else:
                    indent_size = len(leading)
                    if indent_size > 0:
                        spaces_total += indent_size
                        spaces_lines += 1
        except (OSError, UnicodeDecodeError):
            continue
    if tab_lines > spaces_lines:
        return 0
    avg_spaces = spaces_total / max(1, spaces_lines)
    if avg_spaces > 3:
        return 4
    return 2


def _scan_structure(work_dir: Path, strategy) -> dict:
    """扫描项目文件结构"""
    exts = strategy.extensions

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

    # 入口文件检测 — 使用 strategy.entry_points
    entry = strategy.find_entry_file(work_dir)
    if entry:
        entry_points.append(entry.name)

    # 测试目录检测 — 使用 strategy.test_dirs
    test_dir = ""
    for name in strategy.test_dirs:
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
    """项目上下文收集器（多语言）— 所有语言逻辑委托给 LanguageStrategy"""

    def __init__(self, work_dir: str = "."):
        self.work_dir = Path(work_dir).resolve()
        self.cache_path = self.work_dir / ".patchflow" / "context.json"
        self._strategy = None

    @property
    def strategy(self):
        if self._strategy is None:
            factory = LanguageFactory()
            self._strategy = factory.detect(str(self.work_dir))
        return self._strategy

    @property
    def language(self) -> str:
        return self.strategy.name if self.strategy else ""

    def collect(self, use_cache: bool = True) -> ProjectContext:
        """收集项目上下文"""
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
        """Layer 1: 确定性扫描 — 全部委托给 LanguageStrategy"""
        strategy = self.strategy
        wd = self.work_dir
        lang_name = strategy.name if strategy else "unknown"

        # 项目元数据、依赖、框架 — 委托给 Strategy
        if strategy:
            meta = strategy.parse_project_meta(wd)
            deps = strategy.parse_dependencies(wd)
            fw_info = strategy.detect_framework(wd, deps)
            framework = fw_info["name"] if fw_info else ""
            runtime_deps = deps
            dev_deps = []
            import_style = strategy.detect_import_style(wd)
            naming = strategy.detect_naming_convention(wd)
        else:
            meta = {"name": "", "python_version": "", "package_manager": ""}
            framework = ""
            runtime_deps, dev_deps = [], []
            import_style = "absolute"
            naming = "camelCase"

        structure = _scan_structure(wd, strategy) if strategy else {}
        code_style = {
            "indent": _detect_indent(wd, strategy.extensions if strategy else {".py"}),
            "naming": naming,
            "import_style": import_style,
        }

        return {
            "project": {
                "name": meta["name"],
                "language": lang_name,
                "framework": framework,
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
