"""Scope Calculator — 基于代码依赖图的影响范围计算（多语言）

核心职责：
  根据 Error Analyzer 的分析结果和代码依赖图，
  精确计算需要修复的文件范围（硬约束）。

依赖图构建：
  - 按项目语言选择合适的 import 解析器
  - 每个节点是一个文件，每条边是一个 import 关系
  - Python：AST 解析（最精确）
  - JS/TS：正则解析（处理 import/require）
  - 其他语言：通用解析

Scope 计算公式（来自设计文档）：
  Scope(files) = crash_node
    ∪ backward_reach(crash_node, depth=K)
    ∪ type_def_chain(crash_node)
    ∪ forward_reach(crash_node, depth=0)

实际计算时：
  - 语法错误 → 只需修复出问题的文件（单文件）
  - 类型错误 → 追溯类型定义链（2-3 文件）
  - 运行时异常 → 调用链上所有相关文件
  - 名称错误 → 单文件单行
  - 测试失败 → 以测试文件为锚点反推
"""

from pathlib import Path

from patchflow.core.language_registry import LanguageRegistry


class Scope:
    """修复范围结果"""
    def __init__(self, files: list[str], lines: list[int] | None = None,
                 strategy: str = "line", description: str = "",
                 symbols: list[dict] | None = None):
        self.files = files
        self.lines = lines or []
        self.strategy = strategy
        self.description = description
        self.symbols = symbols or []  # 语义级符号列表 [{uid, name, kind, file}]


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
            extensions = {
                ".py", ".js", ".jsx", ".ts", ".tsx", ".java", ".go", ".rs", ".rb", ".php", ".cs", ".swift", ".kt"
            }

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
        if lang and lang.type_search_patterns:
            patterns = [f"{p}{cleaned}" for p in lang.type_search_patterns]
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


def calculate(analysis, dep_graph: DepGraph | None = None,
              code_graph=None) -> Scope:
    """根据错误分析和依赖图计算修复范围

    Args:
        analysis: Error Analyzer 输出的 ErrorAnalysis 对象
        dep_graph: 代码依赖图（可选，没有则退化为基于 trace 文件列表）
        code_graph: CodeGraph 语义代码图谱（优先使用，实现函数级精度）

    Returns:
        Scope: 包含修复范围（文件列表、行号、策略标识、符号列表）
    """
    # 优先使用语义代码图谱
    if code_graph is not None:
        return calculate_semantic(analysis, code_graph)
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


def calculate_semantic(analysis, code_graph) -> Scope:
    """基于语义代码图谱的精确范围计算

    相比 file-level DepGraph 的优势：
      - 精确到函数/方法级别，而非整个文件
      - 通过调用链追踪，找到真正需要修改的符号
      - 自动计算符号级别的语义上下文

    Args:
        analysis: ErrorAnalyzer 输出的 ErrorAnalysis 对象
        code_graph: CodeGraph 实例

    Returns:
        Scope: 包含 symbol 级别的修复范围
    """
    crash = analysis.call_chain[-1] if analysis.call_chain else {}
    crash_file = crash.get("file", "")
    crash_line = crash.get("line", 0)
    error_type = analysis.type

    # ── 核心增强：定位到具体符号 ──
    crash_symbol = None
    if crash_file and crash_line:
        crash_symbol = code_graph.find_symbol_by_location(crash_file, crash_line)
    elif crash_file:
        # 没有行号 → 取 crash 文件中最后一个函数（最可能的位置）
        syms = code_graph.find_symbols(crash_file)
        if syms:
            crash_symbol = syms[-1]

    crash_uid = crash_symbol.uid if crash_symbol else ""

    def _sym_dict(uid: str) -> dict:
        s = code_graph.get_symbol(uid)
        if not s:
            return {"uid": uid, "name": "?", "kind": "?", "file": ""}
        return {"uid": s.uid, "name": s.name, "kind": s.kind, "file": s.file_rel}

    # ── 按错误类型计算语义级范围 ──

    if error_type == "syntax":
        syms = []
        if crash_symbol:
            syms = [_sym_dict(crash_symbol.uid)]
        return Scope(
            files=analysis.impact_files[:1],
            lines=[crash_line],
            strategy="line",
            description="语法错误：行级单符号修复",
            symbols=syms,
        )

    if error_type == "type":
        syms = []
        if crash_symbol:
            chain = code_graph.trace_call_chain(crash_symbol.uid, depth=3, direction="up")
            syms = [{k: v for k, v in n.items() if k != "depth"} for n in chain[:5]]
        return Scope(
            files=analysis.impact_files[:3],
            strategy="chain",
            description="类型错误：语义追溯类型定义链修复",
            symbols=syms,
        )

    if error_type == "runtime":
        syms = []
        if crash_symbol:
            scope_info = code_graph.semantic_scope(crash_symbol.uid)
            syms = [scope_info["symbol"]] if scope_info.get("symbol") else []
            for caller in scope_info.get("direct_callers", [])[:3]:
                syms.append(caller)
            for callee in scope_info.get("direct_callees", [])[:3]:
                syms.append(callee)
        return Scope(
            files=analysis.impact_files,
            strategy="callchain",
            description="运行时异常：语义调用链符号修复",
            symbols=syms,
        )

    if error_type in ("import", "attribute", "key_error", "index_error",
                       "value_error", "file_error"):
        syms = []
        if crash_symbol:
            syms = [_sym_dict(crash_symbol.uid)]
            # 加上同一文件的相关符号
            fn = code_graph.files.get(crash_file)
            if fn:
                for uid in fn.symbols[:5]:
                    if uid != crash_symbol.uid:
                        syms.append(_sym_dict(uid))
        return Scope(
            files=analysis.impact_files,
            strategy="callchain",
            description=f"{error_type}错误：语义符号级修复",
            symbols=syms,
        )

    if error_type == "name":
        syms = []
        if crash_symbol:
            syms = [_sym_dict(crash_symbol.uid)]
        return Scope(
            files=analysis.impact_files[:1],
            lines=[crash_line],
            strategy="line",
            description="名称错误：语义单符号单行修复",
            symbols=syms,
        )

    if error_type == "test_fail":
        syms = []
        if crash_symbol:
            chain = code_graph.trace_call_chain(crash_symbol.uid, depth=2, direction="down")
            syms = [{k: v for k, v in n.items() if k != "depth"} for n in chain[:5]]
        return Scope(
            files=analysis.impact_files[:2],
            strategy="logic",
            description="测试失败：语义以测试函数为锚反推修正",
            symbols=syms,
        )

    return Scope(
        files=analysis.impact_files,
        strategy="callchain",
        description=f"{error_type}错误：语义默认调用链修复",
    )
