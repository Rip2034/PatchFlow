"""Semantic context builder — shared between Fixer and Analyzer (V0.5)

Extracts common CodeGraph-based context building logic that was previously
duplicated in fixer.py (`_build_semantic_context`) and analyzer.py
(`_build_semantic_analysis_context`).

Unified API:
  - build_context_from_files()    — for Fixer (file-path-based lookup)
  - build_context_from_error()    — for Analyzer (error-text-based lookup)
  - build_context_for_symbol()    — generic: given a crash symbol UID
"""

import re
from pathlib import Path


def build_context_from_files(code_graph, scope_files: list[str],
                              error_text: str = "") -> str:
    """Build semantic context for a set of scope files (used by Fixer).

    Locates the most likely crash symbol in the scope files and returns
    a formatted context block with call chain, related symbols, and
    token-budgeted code snippets.
    """
    if code_graph is None:
        return ""

    parts = []

    # 1. Locate crash symbol across scope files
    crash_symbol = _locate_crash_symbol(code_graph, scope_files)

    if crash_symbol:
        parts.extend(_format_symbol_context(code_graph, crash_symbol, include_chunks=True))
    elif scope_files:
        # No crash symbol found — list available symbols per file
        parts.append("## Files in Scope")
        for fpath in scope_files[:3]:
            file_ctx = code_graph.get_file_context(fpath)
            if file_ctx:
                sym_names = [s.get("name", "?") for s in file_ctx.get("symbols", [])[:10]]
                parts.append(f"{fpath}: {', '.join(sym_names)}")

    return "\n".join(parts)


def build_context_from_error(code_graph, error_text: str) -> str:
    """Build semantic context from error text (used by Analyzer).

    Parses traceback-format error output to locate the crash file and line,
    then builds context around that location.
    """
    if code_graph is None:
        return ""

    parts = []
    crash_info = _parse_crash_location(error_text)

    if crash_info:
        crash_file, crash_line = crash_info
        # Match to code_graph file using path component comparison
        for fpath in code_graph.files:
            if _paths_match(crash_file, fpath):
                crash_symbol = code_graph.find_symbol_by_location(fpath, crash_line)
                if crash_symbol:
                    parts.extend(_format_symbol_context(
                        code_graph, crash_symbol, include_chunks=False
                    ))
                    break
    else:
        # No parseable traceback — list file structure overview
        parts.append("## Code Structure Overview")
        for fpath in sorted(code_graph.files.keys())[:10]:
            fn = code_graph.files.get(fpath)
            if fn:
                symbols = []
                for uid in fn.symbols[:8]:
                    s = code_graph.symbols.get(uid)
                    if s and s.kind != "module":
                        symbols.append(f"{s.name}({s.kind})")
                if symbols:
                    parts.append(f"{fpath}: {', '.join(symbols)}")

    return "\n".join(parts)


# ── Internal helpers ──────────────────────────────────────────


def _locate_crash_symbol(code_graph, scope_files: list[str]):
    """Find the most likely crash symbol across scope files.

    Prefers functions/methods; falls back to any non-module symbol.
    """
    for fpath in scope_files:
        syms = code_graph.find_symbols(fpath)
        if not syms:
            continue
        # Prefer function/method symbols, newest first
        for s in reversed(syms):
            if s.kind in ("function", "method"):
                return s
        # Fallback: any non-module symbol
        for s in reversed(syms):
            if s.kind != "module":
                return s
    return None


def _parse_crash_location(error_text: str) -> tuple[str, int] | None:
    """Extract (file, line) from error traceback text."""
    # Python: File "x.py", line N
    m = re.search(r'File "(.+?)", line (\d+)', error_text)
    if m:
        return (m.group(1), int(m.group(2)))
    # JS/V8: at func (file.js:N:M)
    m = re.search(r'at\s+(?:\w+\s+)?\(?(.+?):(\d+):\d+\)?', error_text)
    if m:
        fp = m.group(1)
        if fp.startswith("node:") or fp.startswith("<"):
            return None
        return (fp, int(m.group(2)))
    return None


def _paths_match(crash_file: str, fpath: str) -> bool:
    """Compare two file paths by suffix components.

    V0.5: 要求至少匹配 2 个组件，或短路径被完全消费。
    避免 `src/main.py` 误匹配 `other/main.py`（仅文件名相同）。
    """
    crash_parts = crash_file.replace("\\", "/").rstrip("/").split("/")
    fpath_parts = fpath.replace("\\", "/").rstrip("/").split("/")
    match_len = min(len(crash_parts), len(fpath_parts))
    if match_len <= 0:
        return False
    if crash_parts[-match_len:] != fpath_parts[-match_len:]:
        return False
    # 至少匹配 2 层，或短路径被完全消费
    shorter_len = min(len(crash_parts), len(fpath_parts))
    return match_len >= 2 or match_len == shorter_len


def _format_symbol_context(code_graph, crash_symbol,
                            include_chunks: bool = True) -> list[str]:
    """Format a crash symbol's context into markdown sections."""
    parts = []

    # ── Section 1: Crash site info ──
    parts.append("## Semantic Crash Analysis")
    parts.append(
        f"Crash symbol: {crash_symbol.name} ({crash_symbol.kind}) "
        f"in {crash_symbol.file_rel}:{crash_symbol.start_line}-{crash_symbol.end_line}"
    )
    if crash_symbol.signature:
        parts.append(f"Signature: {crash_symbol.signature}")

    # ── Section 2: Semantic scope ──
    scope_info = code_graph.semantic_scope(crash_symbol.uid)
    if scope_info:
        callers = scope_info.get("direct_callers", [])
        if callers:
            parts.append(f"\nDirect callers ({len(callers)}):")
            for c in callers[:5]:
                parts.append(f"  ← {c.get('name','?')} ({c.get('kind','?')}) in {c.get('file','?')}")

        callees = scope_info.get("direct_callees", [])
        if callees:
            parts.append(f"\nDirect callees ({len(callees)}):")
            for c in callees[:5]:
                parts.append(f"  → {c.get('name','?')} ({c.get('kind','?')}) in {c.get('file','?')}")

    # ── Section 3: Call chain trace ──
    chain = code_graph.trace_call_chain(crash_symbol.uid, depth=3, direction="up")
    if chain and len(chain) > 1:
        parts.append("\n## Call Chain (from entry to crash)")
        for node in chain:
            depth_indent = "  " * node.get("depth", 0)
            parts.append(
                f"{depth_indent}{node.get('name','?')} "
                f"({node.get('kind','?')}) in {node.get('file','?')}"
            )

    # ── Section 4: Same-file related symbols ──
    same_file = scope_info.get("same_file_symbols", []) if scope_info else []
    if same_file:
        parts.append("\n## Related Symbols (same file)")
        for s in same_file[:8]:
            parts.append(f"  {s.get('name','?')} ({s.get('kind','?')})")

    # ── Section 5: Token-budgeted code chunks (for Fixer) ──
    if include_chunks:
        chunks = code_graph.chunk_context(crash_symbol.uid, budget_tokens=2500)
        if chunks:
            parts.append("\n## Relevant Code Snippets (semantic chunks)")
            for chunk in chunks:
                prio_label = ["CRASH SITE", "CALLER/CALLEE", "SAME FILE"][
                    min(chunk.get("priority", 0), 2)
                ]
                parts.append(
                    f"\n### [{prio_label}] {chunk.get('name','?')} "
                    f"({chunk.get('kind','?')}) — {chunk.get('file','?')}:{chunk.get('lines','?')}"
                )
                parts.append(f"```\n{chunk.get('content','')}\n```")

    return parts
