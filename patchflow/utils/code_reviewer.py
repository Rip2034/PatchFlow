"""代码审查器 — 主动发现代码潜在问题

不依赖运行代码，通过 linter + 模式匹配发现常见问题。
AI 可以在阅读代码后主动调用，而不是等运行报错。

审查方式（按优先级）：
  1. 语言专用 Linter（pylint、eslint 等）
  2. 通用模式匹配（硬编码密钥、空指针隐患、TODO 残留等）
"""

import re
from pathlib import Path
from typing import Optional

from patchflow.utils.runner import run
from patchflow.utils import logger


LINT_CACHE: dict[str, list[dict]] = {}
REVIEW_ISSUES: list[dict] = []
# 项目级 eslint 禁用缓存 — 如果一次失败，整个对话不再试
_ESLINT_DISABLED: set[str] = set()


def review_file(filepath: str, work_dir: str = ".") -> str:
    """审查单个文件，返回结构化问题列表

    Returns:
        格式化字符串，列出所有发现的问题
    """
    wd = Path(work_dir)
    fp = wd / filepath
    if not fp.exists():
        return f"ERROR: 文件不存在: {filepath}"

    content = fp.read_text(encoding="utf-8", errors="replace")
    ext = fp.suffix.lower()
    lang = _detect_lang(ext)
    issues: list[dict] = []

    # 1. Linter 检查（尽力而为，失败不阻断）
    linter_issues = _run_linter(filepath, ext, work_dir)
    issues.extend(linter_issues)

    # 2. 模式匹配检查
    pattern_issues = _pattern_check(content, ext, filepath)
    issues.extend(pattern_issues)

    if not issues:
        return f"review_code: {filepath} — 未发现明显问题"

    # 格式化输出（最多显示 5 个问题，防止上下文爆炸）
    MAX_SHOWN = 5
    lines = [f"{filepath} — 发现 {len(issues)} 个问题:"]
    for i, issue in enumerate(issues[:MAX_SHOWN], 1):
        severity = issue.get("severity", "info")
        sev_mark = "🔴" if severity == "error" else "🟡" if severity == "warning" else "🔵"
        line = issue.get("line", "?")
        msg = issue.get("message", "")
        suggestion = issue.get("suggestion", "")
        lines.append(f"  [{i}] {sev_mark} L{line} {msg}")
        if suggestion:
            lines.append(f"      建议: {suggestion}")
        if issue.get("code"):
            lines.append(f"      代码: {issue['code']}")
    if len(issues) > MAX_SHOWN:
        lines.append(f"  ... 还有 {len(issues) - MAX_SHOWN} 个问题（已截断）")

    return "\n".join(lines)


def _detect_lang(ext: str) -> str:
    mapping = {
        ".py": "python",
        ".js": "javascript", ".jsx": "javascript", ".mjs": "javascript", ".cjs": "javascript",
        ".ts": "typescript", ".tsx": "typescript",
        ".java": "java", ".kt": "kotlin",
        ".go": "go", ".rs": "rust",
        ".rb": "ruby", ".php": "php",
        ".c": "c", ".cpp": "cpp", ".h": "c", ".hpp": "cpp",
        ".cs": "csharp",
        ".swift": "swift",
        ".vue": "vue", ".svelte": "svelte",
        ".yaml": "yaml", ".yml": "yaml", ".json": "json", ".xml": "xml",
        ".sql": "sql",
        ".sh": "shell", ".bash": "shell", ".ps1": "powershell",
        ".md": "markdown", ".html": "html", ".css": "css", ".scss": "scss",
    }
    return mapping.get(ext, "text")


def _run_linter(filepath: str, ext: str, work_dir: str) -> list[dict]:
    """尝试运行语言专用 linter（带硬超时，超时则降级）"""
    import threading

    # 项目级禁用检查
    if ext in (".js", ".jsx", ".ts", ".tsx") and work_dir in _ESLINT_DISABLED:
        return []

    results = []
    thread = None

    if ext == ".py":
        thread = threading.Thread(target=lambda: results.extend(_run_pylint(filepath, work_dir)))
    elif ext in (".js", ".jsx", ".ts", ".tsx"):
        thread = threading.Thread(target=lambda: results.extend(_run_eslint(filepath, work_dir)))

    if thread is None:
        return []

    thread.start()
    thread.join(timeout=5)

    if thread.is_alive():
        logger.warn(f"[Linter] eslint 超时（>5s），禁用 eslint: {filepath}")
        _ESLINT_DISABLED.add(work_dir)
        return []

    return results


def _run_pylint(filepath: str, work_dir: str) -> list[dict]:
    """运行 pylint，失败则静默降级"""
    try:
        import pylint
    except ImportError:
        return _basic_python_check(filepath, work_dir)

    result = run(f"python -m pylint --output-format=text --score=n --disable=C0114,C0115,C0116,C0103 {filepath}", cwd=work_dir)
    if result.exit_code == 0 or result.exit_code == 2:
        return _parse_pylint_output(result.stdout)
    return []


def _parse_pylint_output(output: str) -> list[dict]:
    issues = []
    for line in output.split("\n"):
        m = re.match(r"(.+?):(\d+):(\d+): (\w+): (.+)", line)
        if m:
            sev_map = {"E": "error", "W": "warning", "C": "convention", "R": "refactor", "F": "fatal"}
            code = m.group(4)
            severity = sev_map.get(code[0], "info")
            issues.append({
                "line": int(m.group(2)),
                "severity": severity,
                "message": m.group(5),
                "suggestion": "",
            })
    return issues


def _run_eslint(filepath: str, work_dir: str) -> list[dict]:
    cache_key = f"{work_dir}:{filepath}"
    cached = LINT_CACHE.get(cache_key)
    if cached is not None:
        return cached
    result = run(f"npx eslint --format=json {filepath}", cwd=work_dir, timeout=10)
    if result.exit_code > 1:
        LINT_CACHE[cache_key] = []
        return []
    import json
    try:
        data = json.loads(result.stdout)
        issues = []
        for file_result in data:
            for msg in file_result.get("messages", []):
                issues.append({
                    "line": msg.get("line", 0),
                    "severity": "error" if msg.get("severity", 0) >= 2 else "warning",
                    "message": msg.get("message", ""),
                    "suggestion": "",
                })
        issues = issues[:10]
        LINT_CACHE[cache_key] = issues
        return issues
    except (json.JSONDecodeError, KeyError):
        LINT_CACHE[cache_key] = []
        return []


def _basic_python_check(filepath: str, work_dir: str) -> list[dict]:
    """无 pylint 时用 compile() 做基础检查"""
    fp = Path(work_dir) / filepath
    if not fp.exists():
        return []
    content = fp.read_text(encoding="utf-8")
    issues = []
    try:
        compile(content, filepath, "exec")
    except SyntaxError as e:
        issues.append({
            "line": e.lineno or 0,
            "severity": "error",
            "message": f"语法错误: {e.msg}",
            "suggestion": f"检查第 {e.lineno} 行的语法",
        })
    return issues


_COMMON_PATTERNS: list[dict] = [
    {
        "name": "hardcoded_secret",
        "severity": "error",
        "patterns": [
            r'(?i)(password|secret|api_key|apikey|token|private_key)\s*[=:]\s*["\'][^"\'\s]{8,}["\']',
        ],
        "message": "检测到疑似硬编码密钥/密码",
        "suggestion": "将密钥移到环境变量或配置中心",
    },
    {
        "name": "todo",
        "severity": "info",
        "patterns": [r"(?i)#\s*(TODO|FIXME|HACK|XXX|BUG)\b"],
        "message": "包含 TODO/FIXME 标记",
        "suggestion": "评估是否需要处理",
    },
    {
        "name": "print_console_log",
        "severity": "info",
        "patterns": [
            r"(?i)\bprint\s*\(",  # Python/Ruby/PHP
            r"(?i)console\.(log|warn|error)\s*\(",  # JS/TS
            r"(?i)System\.out\.println\s*\(",  # Java
        ],
        "message": "在生产代码中保留了调试输出",
        "suggestion": "使用日志框架替代，或确认是临时调试代码",
    },
    {
        "name": "empty_catch",
        "severity": "warning",
        "patterns": [
            r"(?i)catch\s*\(.*?\)\s*\{\s*\}",
            r"(?i)except\s+\w+\s*:\s*\n\s*(?:pass|#)",
        ],
        "message": "空的 catch/except 块会吞掉错误",
        "suggestion": "至少记录错误日志，或处理特定异常",
    },
    {
        "name": "magic_number",
        "severity": "info",
        "patterns": [
            r"(?<![.\w])[0-9]{3,}(?![.\w])",
        ],
        "message": "检测到魔法数字（无命名的常量）",
        "suggestion": "考虑使用命名常量替换",
    },
    {
        "name": "long_function",
        "severity": "info",
        "pattern_check": True,
        "message": "函数/方法过长",
        "suggestion": "考虑拆分为多个小函数",
        "max_lines": 80,
    },
    {
        "name": "null_check",
        "severity": "warning",
        "pattern_check": True,
        "message": "可能缺少空值检查",
        "suggestion": "添加 null/None 检查后再访问属性",
    },
]


def _pattern_check(content: str, ext: str, filepath: str) -> list[dict]:
    """模式匹配检查"""
    issues = []
    lines = content.split("\n")
    file_lang = _detect_lang(ext)
    issues_set = set()

    for pattern in _COMMON_PATTERNS:
        if pattern.get("pattern_check"):
            continue  # 复杂检查单独处理
        name = pattern["name"]
        for regex in pattern.get("patterns", []):
            for m in re.finditer(regex, content):
                line_no = content[:m.start()].count("\n") + 1
                key = (name, line_no)
                if key not in issues_set:
                    issues_set.add(key)
                    issues.append({
                        "line": line_no,
                        "severity": pattern["severity"],
                        "message": pattern["message"],
                        "suggestion": pattern["suggestion"],
                        "code": m.group().strip()[:80],
                    })

    # 长函数检测
    if file_lang in ("python", "javascript", "typescript", "java", "go", "rust"):
        current_func = ""
        func_start = 0
        brace_count = 0
        func_lines = 0
        for i, line in enumerate(lines, 1):
            stripped = line.strip()
            fdef = re.match(r"^\s*(?:def |async def |function |public |private |protected |fun |func )", stripped)
            if fdef and not current_func:
                current_func = stripped[:50]
                func_start = i
                func_lines = 0
                brace_count = stripped.count("{") - stripped.count("}")
            elif current_func:
                func_lines += 1
                brace_count += stripped.count("{") - stripped.count("}")
                if brace_count <= 0 and stripped.rstrip().endswith(("}", ":", "```")):
                    key = ("long_function", func_start)
                    if func_lines > 80 and key not in issues_set:
                        issues_set.add(key)
                        issues.append({
                            "line": func_start,
                            "severity": "info",
                            "message": f"函数过长 ({func_lines} 行，建议 ≤80 行): {current_func}",
                            "suggestion": "拆分为多个小函数",
                        })
                    current_func = ""
                    func_lines = 0

        # 加括号语言（Java/JS/TS）用花括号检测
        if not current_func and file_lang in ("java", "javascript", "typescript", "go"):
            for i, line in enumerate(lines, 1):
                stripped = line.strip()
                if re.match(r"^\s*(?:public|private|protected)\s", stripped) and "(" in stripped and ")" in stripped and "{" not in stripped:
                    pass

    # 空值检查检测（简化：找 .xxx() 或 .xxx 前面没有 null/None check 的行）
    if file_lang in ("java", "javascript", "typescript", "python", "go"):
        for i, line in enumerate(lines, 1):
            stripped = line.strip()
            # 跳过注释、import、定义行
            if stripped.startswith(("#", "//", "/*", "*", "import ", "package ")):
                continue
            # 检测链式调用 .getXxx() .xxx() 等
            if re.search(r"\.\w+\s*\(", stripped) and not re.search(r"(null|None|nullptr|optional|\.getOrDefault|if\s+\(?\w+\s*!=?\s*(null|None)|System\.(out|err)|logger\.)", stripped, re.IGNORECASE):
                if i < len(lines) - 1:
                    prev = lines[i - 1].strip()
                    if re.search(r"(null|None|nullptr|!=|==|is\s+not\s+None)", prev, re.IGNORECASE):
                        continue
                key = ("null_check", i)
                if key not in issues_set and not stripped.startswith("//"):
                    issues_set.add(key)
                    issues.append({
                        "line": i,
                        "severity": "warning",
                        "message": f"潜在的空指针访问: {stripped[:60]}",
                        "suggestion": "调用前添加 null/None 检查",
                        "code": stripped[:80],
                    })
                    break  # 每行最多一条

    return issues
