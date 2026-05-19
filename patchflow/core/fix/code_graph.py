"""CodeGraph — 语义代码图谱

基于 tree-sitter-language-pack v2 的 process() API 构建函数/类级别的代码图谱，
替代原有的文件级 DepGraph。

核心能力：
  - 函数/类/方法级别的符号提取
  - 跨文件调用关系追踪
  - 语义作用域计算（调用链、被调用链）
  - Token 预算感知的语义分块
"""

import re
from collections import defaultdict
from pathlib import Path

from patchflow.utils import logger

# tree-sitter-language-pack v2 — Rust-native binding
try:
    from tree_sitter_language_pack.api import ProcessConfig, ProcessResult
    from tree_sitter_language_pack.api import process as ts_process
    _TS_AVAILABLE = True
except ImportError:
    _TS_AVAILABLE = False
    ProcessResult = None  # type: ignore


class SymbolNode:
    """语义符号节点 — 函数/类/方法"""

    def __init__(self, uid: str, name: str, kind: str, file_rel: str,
                 start_line: int, end_line: int, signature: str = "",
                 parent_uid: str = ""):
        self.uid = uid                      # "relpath::symbol_name" 或 "relpath::Class.method"
        self.name = name                    # 符号名
        self.kind = kind                    # function / class / method / module
        self.file_rel = file_rel            # 相对项目根的文件路径
        self.start_line = start_line
        self.end_line = end_line
        self.signature = signature          # 函数签名
        self.parent_uid = parent_uid        # 父符号 uid（方法所属类等）
        self.children: list[str] = []       # 子符号 uid 列表
        self.refs: set[str] = set()         # 引用的其他符号 uid

    @property
    def span(self) -> tuple[int, int]:
        return (self.start_line, self.end_line)

    @property
    def line_count(self) -> int:
        return self.end_line - self.start_line + 1

    def to_dict(self) -> dict:
        return {
            "uid": self.uid,
            "name": self.name,
            "kind": self.kind,
            "file": self.file_rel,
            "start_line": self.start_line,
            "end_line": self.end_line,
            "signature": self.signature,
            "parent": self.parent_uid,
            "children": self.children,
            "refs": sorted(self.refs),
        }


class FileNode:
    """文件节点 — 一个源文件及其包含的符号"""

    def __init__(self, relpath: str, language: str):
        self.relpath = relpath
        self.language = language
        self.symbols: list[str] = []        # 符号 uid 列表
        self.imports: list[dict] = []       # [{source, items, alias}]
        self.exports: list[str] = []        # 导出符号名
        self.metrics: dict = {}             # 文件度量


class CodeGraph:
    """语义代码图谱

    使用 tree-sitter-language-pack 的 process() API 解析所有源文件，
    构建函数/类级别的依赖关系图。

    使用方式：
        from patchflow.core.language_registry import LanguageRegistry
        lang = LanguageRegistry().detect(".")
        graph = CodeGraph(".", lang)
        chain = graph.trace_call_chain("src/app.py::login", depth=3)
    """

    def __init__(self, work_dir: str, lang):
        from patchflow.core.language_registry import LanguageDescriptor
        self.work_dir = Path(work_dir).resolve()
        self.lang: LanguageDescriptor = lang
        self.files: dict[str, FileNode] = {}       # relpath → FileNode
        self.symbols: dict[str, SymbolNode] = {}    # uid → SymbolNode
        self.call_graph: dict[str, set[str]] = defaultdict(set)    # caller_uid → {callee_uids}
        self.reverse_call_graph: dict[str, set[str]] = defaultdict(set)  # callee_uid → {caller_uids}

        if _TS_AVAILABLE and lang:
            self._build()
        else:
            logger.info("[CodeGraph] tree-sitter not available or lang not supported, "
                        "falling back to file-level graph")
            self._build_fallback()

    # ── build ─────────────────────────────────────────────

    def _build(self):
        """主构建流程 — 解析所有源文件并构建图谱"""
        import time
        ts_lang = self.lang.name
        extensions = self.lang.extensions
        max_files = 2000
        start_time = time.time()

        # 第一遍：收集所有符号
        count = 0
        for fpath in self._iter_source_files(extensions):
            if count >= max_files:
                logger.info(f"[CodeGraph] reached max files ({max_files}), stopping parse")
                break
            try:
                self._parse_file(fpath, ts_lang)
                count += 1
                if count % 500 == 0:
                    elapsed = time.time() - start_time
                    logger.info(f"[CodeGraph] parsed {count} files ({elapsed:.1f}s)")
            except Exception as e:
                logger.debug(f"[CodeGraph] parse error for {fpath}: {e}")

        # 第二遍：解析符号间引用关系
        self._resolve_references()

        logger.info(f"[CodeGraph] built: {len(self.files)} files, "
                    f"{len(self.symbols)} symbols, {sum(len(v) for v in self.call_graph.values())} edges, "
                    f"{time.time() - start_time:.1f}s")

    def _build_fallback(self):
        """回退方案 — 仅建立文件级别的依赖"""
        wd = self.work_dir
        registry = __import__('patchflow.core.language_registry', fromlist=['LanguageRegistry'])
        lang_reg = registry.LanguageRegistry()
        import_parser = lang_reg.get_import_parser(self.lang)

        for fpath in self._iter_source_files(self.lang.extensions):
            rel = str(fpath.relative_to(wd)).replace("\\", "/")
            fn = FileNode(rel, self.lang.name)
            imports = import_parser(str(fpath), str(wd))
            fn.imports = [{"source": i, "items": [], "alias": None} for i in imports]
            uid = f"{rel}::module"
            sym = SymbolNode(uid, fpath.stem, "module", rel, 1, 1)
            fn.symbols.append(uid)
            self.files[rel] = fn
            self.symbols[uid] = sym

            for imp in imports:
                target_uid = f"{imp}::module"
                self.call_graph[uid].add(target_uid)
                self.reverse_call_graph[target_uid].add(uid)

    def _iter_source_files(self, extensions: set[str]):
        """遍历项目目录中匹配语言扩展名的源文件"""
        wd = self.work_dir
        for ext in extensions:
            for fpath in wd.rglob(f"*{ext}"):
                if fpath.is_file() and not fpath.name.startswith("."):
                    # 跳过常见排除目录
                    parts = fpath.relative_to(wd).parts
                    if any(p in ("node_modules", "__pycache__", ".git", "target",
                                 "build", "dist", "venv", ".venv", "vendor", ".tox",
                                 "bundle", ".bundle", ".gradle", "gradle",
                                 "bower_components", "__generated__", "generated", "gen",
                                 "Pods", "Carthage", ".terraform", ".serverless", "cdk.out")
                           for p in parts):
                        continue
                    yield fpath

    def _parse_file(self, fpath: Path, ts_lang: str):
        """用 tree-sitter 解析单个文件，提取符号和结构"""
        wd = self.work_dir
        rel = str(fpath.relative_to(wd)).replace("\\", "/")

        try:
            source = fpath.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            return

        try:
            config = ProcessConfig(
                language=ts_lang,
                structure=True,
                imports=True,
                exports=True,
                symbols=True,
                diagnostics=True,
            )
            result: ProcessResult = ts_process(source, config)
        except Exception as e:
            logger.debug(f"[CodeGraph] process failed for {rel}: {e}")
            return

        fn = FileNode(rel, ts_lang)
        fn.metrics = {
            "total_lines": result.metrics.total_lines if result.metrics else 0,
            "code_lines": result.metrics.code_lines if result.metrics else 0,
        }

        # 处理 imports
        for imp in result.imports or []:
            fn.imports.append({
                "source": imp.source,
                "items": list(imp.items or []),
                "alias": imp.alias,
            })

        # 处理 exports
        for exp in result.exports or []:
            fn.exports.append(exp.name)

        # 递归提取结构中的符号
        parent_uid = f"{rel}::module"
        module_sym = SymbolNode(parent_uid, fpath.stem, "module", rel, 1, 1)
        self.symbols[parent_uid] = module_sym
        fn.symbols.append(parent_uid)

        for item in result.structure or []:
            self._extract_structure(item, rel, parent_uid, fn)

        self.files[rel] = fn

    def _extract_structure(self, item, rel: str, parent_uid: str, fn: FileNode):
        """递归提取 StructureItem 中的符号"""
        kind_map = {
            "Function": "function", "Method": "method", "Class": "class",
            "Struct": "class", "Interface": "interface", "Enum": "enum",
            "Module": "module", "Trait": "interface", "Impl": "class",
            "Namespace": "module",
        }
        kind = kind_map.get(item.kind.type if hasattr(item.kind, 'type') else str(item.kind), "unknown")
        name = item.name or f"<anonymous_{kind}>"

        # 构建 uid
        parent_name = parent_uid.split("::")[-1]
        if kind == "method":
            uid = f"{rel}::{parent_name}.{name}"
        else:
            uid = f"{rel}::{name}"

        start = item.span.start_line if item.span else 0
        end = item.span.end_line if item.span else 0
        sig = item.signature or ""

        sym = SymbolNode(uid, name, kind, rel, start, end, sig, parent_uid)

        # 注册父子关系
        if parent_uid in self.symbols:
            self.symbols[parent_uid].children.append(uid)

        self.symbols[uid] = sym
        fn.symbols.append(uid)

        # 递归处理子结构
        for child in item.children or []:
            self._extract_structure(child, rel, uid, fn)

    def _resolve_references(self):
        """解析符号间的引用关系

        以文件为单位遍历，每个文件只读一次，对其中的所有符号做一次引用匹配。
        """
        # 构建 name → [uids] 快速索引（排除同名 module uid）
        name_index: dict[str, list[str]] = defaultdict(list)
        for uid, sym in self.symbols.items():
            if sym.kind != "module":
                name_index[sym.name].append(uid)

        wd = self.work_dir
        # 按文件分组符号
        file_symbols: dict[str, list[tuple[str, SymbolNode]]] = defaultdict(list)
        for uid, sym in self.symbols.items():
            if sym.kind != "module":
                file_symbols[sym.file_rel].append((uid, sym))

        for file_rel, symbols in file_symbols.items():
            filepath = wd / file_rel
            try:
                source = filepath.read_text(encoding="utf-8")
                source_lines = source.split("\n")
            except (UnicodeDecodeError, OSError):
                continue

            for uid, sym in symbols:
                body = "\n".join(source_lines[sym.start_line - 1:sym.end_line])

                for other_name, other_uids in name_index.items():
                    if other_name == sym.name:
                        continue
                    if _is_symbol_referenced(body, other_name):
                        for other_uid in other_uids:
                            if other_uid != uid:
                                sym.refs.add(other_uid)
                                self.call_graph[uid].add(other_uid)
                                self.reverse_call_graph[other_uid].add(uid)

    # ── query ─────────────────────────────────────────────

    def get_symbol(self, uid: str) -> SymbolNode | None:
        return self.symbols.get(uid)

    def find_symbols(self, file_rel: str, name: str | None = None,
                     kind: str | None = None) -> list[SymbolNode]:
        """在文件中查找符号"""
        results = []
        fn = self.files.get(file_rel)
        if not fn:
            return results
        for uid in fn.symbols:
            sym = self.symbols.get(uid)
            if sym is None:
                continue
            if name and sym.name != name:
                continue
            if kind and sym.kind != kind:
                continue
            results.append(sym)
        return results

    def find_symbol_by_location(self, file_rel: str, line: int) -> SymbolNode | None:
        """根据文件+行号定位符号"""
        fn = self.files.get(file_rel)
        if not fn:
            return None
        best = None
        for uid in fn.symbols:
            sym = self.symbols.get(uid)
            if sym is None or sym.kind == "module":
                continue
            if sym.start_line <= line <= sym.end_line:
                if best is None or sym.line_count < best.line_count:
                    best = sym
        return best

    def trace_call_chain(self, uid: str, depth: int = 3,
                         direction: str = "down") -> list[dict]:
        """追踪调用链

        Args:
            uid: 起始符号 uid
            depth: 追踪深度
            direction: "down" 追踪被调用方，"up" 追踪调用方，"both" 双向

        Returns:
            [{uid, name, kind, file, depth}] 按深度排序的调用链节点
        """
        visited: set[str] = set()
        chain: list[dict] = []

        def _trace(current: str, d: int):
            if d > depth or current in visited:
                return
            visited.add(current)
            sym = self.symbols.get(current)
            if sym:
                chain.append({
                    "uid": sym.uid, "name": sym.name, "kind": sym.kind,
                    "file": sym.file_rel, "depth": d,
                })
            if direction in ("down", "both"):
                for callee in self.call_graph.get(current, set()):
                    _trace(callee, d + 1)
            if direction in ("up", "both"):
                for caller in self.reverse_call_graph.get(current, set()):
                    _trace(caller, d + 1)

        _trace(uid, 0)
        return chain

    def semantic_scope(self, uid: str) -> dict:
        """计算符号的语义作用域

        Returns:
            {
                symbol: {...},
                direct_callers: [{uid, name, file}],
                direct_callees: [{uid, name, file}],
                same_file_symbols: [{uid, name, kind}],
                parent_symbol: {...} or None,
            }
        """
        sym = self.symbols.get(uid)
        if not sym:
            return {}

        def _brief(u: str) -> dict:
            s = self.symbols.get(u)
            if not s:
                return {"uid": u, "name": "?", "file": ""}
            return {"uid": s.uid, "name": s.name, "kind": s.kind, "file": s.file_rel}

        return {
            "symbol": sym.to_dict(),
            "direct_callers": [_brief(c) for c in self.reverse_call_graph.get(uid, set())],
            "direct_callees": [_brief(c) for c in self.call_graph.get(uid, set())],
            "same_file_symbols": [
                _brief(u) for u in self.files.get(sym.file_rel, FileNode("", "")).symbols
                if u != uid
            ],
            "parent_symbol": _brief(sym.parent_uid) if sym.parent_uid else None,
        }

    def chunk_context(self, uid: str, budget_tokens: int = 2000) -> list[dict]:
        """为指定符号生成 Token 预算感知的语义上下文

        优先级：符号自身 > 直接调用者/被调用者 > 同文件符号 > 导入依赖

        Returns:
            [{uid, name, kind, file, lines, content}] 按优先级排序
        """
        chunks: list[dict] = []
        seen: set[str] = {uid}
        wd = self.work_dir
        char_budget = budget_tokens * 3  # 粗略 token → char 换算

        def _add_chunk(target_uid: str) -> bool:
            """添加符号的代码块，返回是否成功添加"""
            if target_uid in seen:
                return False
            sym = self.symbols.get(target_uid)
            if not sym or sym.kind == "module":
                return False
            try:
                filepath = wd / sym.file_rel
                source = filepath.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                return False
            lines = source.split("\n")[sym.start_line - 1:sym.end_line]
            content = "\n".join(lines)
            chunks.append({
                "uid": sym.uid, "name": sym.name, "kind": sym.kind,
                "file": sym.file_rel, "lines": f"{sym.start_line}-{sym.end_line}",
                "content": content, "priority": 0,
            })
            seen.add(target_uid)
            return True

        def _remaining_chars() -> int:
            used = sum(len(c["content"]) for c in chunks)
            return max(0, char_budget - used)

        # Level 0: 符号自身
        _add_chunk(uid)

        # Level 1: 直接调用者和被调用者
        related = list(self.call_graph.get(uid, set())) + list(self.reverse_call_graph.get(uid, set()))
        for ruid in related:
            if _remaining_chars() < 200:
                break
            _add_chunk(ruid)

        # Level 2: 同文件其他符号
        sym = self.symbols.get(uid)
        if sym:
            fn = self.files.get(sym.file_rel)
            if fn:
                for fuid in fn.symbols:
                    if _remaining_chars() < 200:
                        break
                    _add_chunk(fuid)

        # 标记优先级
        for i, c in enumerate(chunks):
            if i == 0:
                c["priority"] = 0
            elif i < 1 + len(related):
                c["priority"] = 1
            else:
                c["priority"] = 2

        return chunks

    def get_file_context(self, file_rel: str) -> dict | None:
        """获取文件的完整语义上下文摘要"""
        fn = self.files.get(file_rel)
        if not fn:
            return None
        return {
            "file": file_rel,
            "language": fn.language,
            "metrics": fn.metrics,
            "symbols": [
                self.symbols[uid].to_dict()
                for uid in fn.symbols
                if uid in self.symbols
            ],
            "imports": fn.imports,
            "exports": fn.exports,
        }


def _is_symbol_referenced(body: str, name: str) -> bool:
    """检查函数体中是否引用了某个符号名

    用词边界匹配，避免子串误匹配（如 'foo' 匹配 'foobar'）。
    """
    pattern = rf'\b{re.escape(name)}\b'
    return bool(re.search(pattern, body))
