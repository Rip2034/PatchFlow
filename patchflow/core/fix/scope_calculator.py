"""Scope Calculator — 基于代码依赖图的影响范围计算（多语言）

核心职责：
  根据 Error Analyzer 的分析结果和代码依赖图，
  精确计算需要修复的文件范围（硬约束）。

依赖图构建：
  - 按项目语言选择合适的 import 解析器
  - 每个节点是一个文件，每条边是一个 import 关系
  - Python 用 AST 解析，JS/TS 用正则解析，其他语言通用解析

Scope 计算公式（来自设计文档）：
  Scope(files) = crash_node
    ∪ backward_reach(crash_node, depth=K)
    ∪ type_def_chain(crash_node)
    ∪ forward_reach(crash_node, depth=0)
"""

from pathlib import Path

from patchflow.core.language_registry import LanguageRegistry


class Scope:
    """修复范围结果"""
    def __init__(self, files: list[str], lines: list[int] | None = None,
                 strategy: str = "line", description: str = ""):
        self.files = files
        self.lines = lines or []
        self.strategy = strategy
        self.description = description


class DepGraph:
    """轻量级代码依赖图（文件级）

    从 import/require 语句解析构建，不依赖 AI。
    支持多语言：Python (AST) / JavaScript/TypeScript (正则) / 其他（通用）
    """

    def __init__(self, work_dir: str = "."):
        self.work_dir = Path(work_dir).resolve()
        self.graph: dict[str, set[str]] = {}
        self.reverse: dict[str, set[str]] = {}
        self._built = False
        self._lang = None
        self._import_parser = None

    def _get_import_parser(self):
        if self._import_parser is None:
            reg = LanguageRegistry()
            self._lang = reg.detect(str(self.work_dir))
            self._import_parser = reg.get_import_parser(self._lang)
        return self._import_parser, self._lang

    def build(self, extensions: set[str] | None = None) -> "DepGraph":
        parser, lang = self._get_import_parser()
        if extensions is None and lang:
            extensions = lang.extensions
        if extensions is None:
            extensions = {".py"}

        for filepath in self.work_dir.rglob("*"):
            if filepath.suffix.lower() not in extensions:
                continue
            rel_str = str(filepath.relative_to(self.work_dir)).replace("\\", "/")
            if rel_str.startswith(".patchflow/") or rel_str.startswith(".venv/"):
                continue

            imports = parser(str(filepath), str(self.work_dir))

            self.graph[rel_str] = set(imports)
            if rel_str not in self.reverse:
                self.reverse[rel_str] = set()
            for target in imports:
                if target not in self.reverse:
                    self.reverse[target] = set()
                self.reverse[target].add(rel_str)

        self._built = True
        return self

    def neighbors(self, file: str, depth: int = 1) -> set[str]:
        if file not in self.graph:
            return set()

        result = set()
        visited = {file}
        current = {file}

        for _ in range(depth):
            next_nodes = set()
            for node in current:
                for neighbor in self.graph.get(node, set()):
                    if neighbor not in visited:
                        visited.add(neighbor)
                        next_nodes.add(neighbor)
                        result.add(neighbor)
                for neighbor in self.reverse.get(node, set()):
                    if neighbor not in visited:
                        visited.add(neighbor)
                        next_nodes.add(neighbor)
                        result.add(neighbor)
            current = next_nodes

        return result

    def direct_callers(self, file: str) -> set[str]:
        return self.reverse.get(file, set())

    def find_type(self, type_name: str) -> str | None:
        cleaned = type_name.split(":")[0].strip().split()[-1] if " " in type_name else type_name
        _, lang = self._get_import_parser()
        patterns = []
        if lang:
            if lang.name == "python":
                patterns = [f"class {cleaned}", f"type {cleaned}"]
            elif lang.name in ("javascript", "typescript"):
                patterns = [f"class {cleaned}", f"function {cleaned}", f"const {cleaned}"]
            elif lang.name == "java":
                patterns = [f"class {cleaned}", f"interface {cleaned}"]
            elif lang.name == "go":
                patterns = [f"type {cleaned}"]
            elif lang.name == "rust":
                patterns = [f"struct {cleaned}", f"enum {cleaned}", f"trait {cleaned}"]
            else:
                patterns = [f"class {cleaned}", f"type {cleaned}"]
        else:
            patterns = [f"class {cleaned}", f"type {cleaned}"]

        for filepath in self.graph:
            try:
                content = (self.work_dir / filepath).read_text(encoding="utf-8")
                for pat in patterns:
                    if pat in content:
                        return filepath
            except (OSError, UnicodeDecodeError):
                continue
        return None

    def trace_type_chain(self, type_file: str | None) -> list[str]:
        if type_file is None:
            return []
        chain = [type_file]
        callers = self.direct_callers(type_file)
        chain.extend(list(callers)[:2])
        return chain

    def trace_from_test(self, test_file: str) -> list[str]:
        result = [test_file]
        callees = self.graph.get(test_file, set())
        result.extend(list(callees)[:3])
        return result


def calculate(analysis, dep_graph: DepGraph | None = None) -> Scope:
    """根据错误分析和依赖图计算修复范围

    Args:
        analysis: Error Analyzer 输出的 ErrorAnalysis 对象
        dep_graph: 代码依赖图（可选，没有则退化为基于 trace 文件列表）

    Returns:
        Scope: 包含修复范围（文件列表、行号、策略标识）
    """
    crash = analysis.call_chain[-1] if analysis.call_chain else {}
    error_type = analysis.type

    if error_type == "syntax":
        return Scope(
            files=analysis.impact_files[:1],
            lines=[crash.get("line", 0)],
            strategy="line",
            description="语法错误：行级单文件修复",
        )

    if error_type == "type":
        if dep_graph:
            type_node = dep_graph.find_type(analysis.root_cause)
            chain = dep_graph.trace_type_chain(type_node)
        else:
            chain = analysis.impact_files[:3]
        return Scope(
            files=chain[:3],
            strategy="chain",
            description="类型错误：追溯类型定义链修复",
        )

    if error_type == "runtime":
        scope = set(analysis.impact_files)
        if dep_graph:
            for f in list(scope):
                scope.update(dep_graph.direct_callers(f))
        return Scope(
            files=list(scope),
            strategy="callchain",
            description="运行时异常：调用链涉及文件修复",
        )

    if error_type in ("import", "attribute", "key_error", "index_error", "value_error", "file_error"):
        scope = set(analysis.impact_files)
        if dep_graph:
            for f in list(scope):
                scope.update(dep_graph.direct_callers(f))
        return Scope(
            files=list(scope),
            strategy="callchain",
            description=f"{error_type}错误：调用链涉及文件修复",
        )

    if error_type == "name":
        return Scope(
            files=analysis.impact_files[:1],
            lines=[crash.get("line", 0)],
            strategy="line",
            description="名称错误：单行单文件修复",
        )

    if error_type == "test_fail":
        if dep_graph and analysis.impact_files:
            test_file = next((f for f in analysis.impact_files if "test" in f.lower()), analysis.impact_files[0])
            scope = dep_graph.trace_from_test(test_file)
        else:
            scope = analysis.impact_files[:2]
        return Scope(
            files=scope,
            strategy="logic",
            description="测试失败：以测试为锚，反推修正",
        )

    return Scope(
        files=analysis.impact_files,
        strategy="callchain",
        description=f"{error_type}错误：默认调用链修复",
    )
