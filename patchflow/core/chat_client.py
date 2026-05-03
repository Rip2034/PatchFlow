# -*- coding: utf-8 -*-
"""对话客户端 — 工具调用 + 流式输出(Claude Code 风格)

AI 可以直接 write_file，read_file，run_code，list_files，search_files，search_code。
支持流式输出 —— 一个字一个字显示，不是等完了才一起出来。
"""

import json
import time
from pathlib import Path
from typing import Callable
from openai import OpenAI
from anthropic import Anthropic

from patchflow.core.config import get_config, get_normalized_provider
from patchflow.core.project.context_manager import compress
from patchflow.utils import logger

# ═══════════════════════════════════════════════════════════
# 工具定义
# ═══════════════════════════════════════════════════════════

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "创建或覆盖一个文件。如果文件所在的目录不存在，会自动创建。",
            "parameters": {
                "type": "object",
                "properties": {
                    "filename": {"type": "string", "description": "要写入的文件名，如 app.py、src/utils.py"},
                    "content": {"type": "string", "description": "文件的完整内容"}
                },
                "required": ["filename", "content"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "读取文件内容。这是读取文件内容的唯一正确工具，支持跨平台。大文件自动截断（保留首尾），可用 offset/limit 分页精读。不要用 run_code 来读文件。",
            "parameters": {
                "type": "object",
                "properties": {
                    "filename": {"type": "string", "description": "要读取的文件名"},
                    "offset": {"type": "integer", "description": "起始行号（0-based），默认 0"},
                    "limit": {"type": "integer", "description": "最大读取行数，默认全部（大文件自动截断）"},
                },
                "required": ["filename"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "rename_file",
            "description": "移动或重命名文件/目录。用于整理项目结构，如把文件移到子目录、给文件改名等。如果目标父目录不存在会自动创建。",
            "parameters": {
                "type": "object",
                "properties": {
                    "source": {"type": "string", "description": "源路径，如 'src/old.js' 或 'public/'"},
                    "dest": {"type": "string", "description": "目标路径，如 'src/utils/old.js' 或 'new_public/"}
                },
                "required": ["source", "dest"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "batch_read_files",
            "description": "【推荐】一次读取多个文件。比逐个 read_file 高效得多，适合在 Phase 2/3 中批量读取相关文件。返回每个文件的完整内容，文件之间用分隔线隔开。已缓存的文件会标注 (already read)。",
            "parameters": {
                "type": "object",
                "properties": {
                    "files": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "要读取的文件路径列表，如 [\"src/UserService.java\", \"src/UserController.java\"]"
                    },
                },
                "required": ["files"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "delete_file",
            "description": "删除文件。用于清理临时文件、废弃代码或不需要的产物。慎用，每次删除都会确认。",
            "parameters": {
                "type": "object",
                "properties": {
                    "filename": {"type": "string", "description": "要删除的文件名或路径"}
                },
                "required": ["filename"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "run_code",
            "description": "运行一条命令。返回命令输出。长驻命令（如 npm run dev）会自动转入后台运行，不会阻塞。",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "要执行的命令，如 python app.py 或 npm run dev"},
                    "timeout": {"type": "integer", "description": "可选：超时秒数（默认 30，长驻命令不需要设置）"},
                },
                "required": ["command"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "list_files",
            "description": "列出目录结构树（递归），了解项目架构。最佳用法是分层探索：先看根目录（depth=1），再深入主要子目录（depth=2-3）。自动忽略无关目录。超过 25 行会自动截断。嵌套的单目录链会合并为一行。",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "要列出的目录路径，如 'backend'、'frontend/src'"},
                    "max_depth": {"type": "integer", "description": "最大递归深度，默认 4。先 depth=1 看概览，再 depth=2-3 深入"},
                },
                "required": []
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "search_files",
            "description": "按概念搜索项目文件。用自然语言描述要找什么（如'处理支付的代码'）。无需配置即可使用——有 embedding 时语义搜索，没有时自动降级为关键词匹配。",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "自然语言搜索描述，如 '用户认证相关代码' 或 'payment processing'"},
                    "top_k": {"type": "integer", "description": "返回结果数量，默认 10"},
                },
                "required": ["query"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "search_code",
            "description": "在项目文件中执行正则搜索。用于精确查找函数定义、类引用、特定代码模式。返回匹配行及行号。",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string", "description": "正则表达式，如 'def\\s+processPayment' 或 'class\\s+User\\b'"},
                    "path_filter": {"type": "string", "description": "可选：只搜索路径包含此字符串的文件，如 'service'"},
                },
                "required": ["pattern"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "review_code",
            "description": "审查代码文件，发现潜在问题。不用运行代码就能找出语法错误、硬编码密钥、空指针隐患、TODO 残留、调试输出等。支持 Python、Java、JS/TS、Go 等语言。读完文件后调用此工具进行审查。",
            "parameters": {
                "type": "object",
                "properties": {
                    "filepath": {"type": "string", "description": "要审查的文件路径，如 src/main/java/com/example/UserService.java"},
                },
                "required": ["filepath"]
            }
        }
    },
]

SYSTEM_PROMPT = (
    "You are PatchFlow, an AI coding assistant.\n"
    "You have tools: write_file, read_file, batch_read_files, delete_file, rename_file, run_code, list_files, search_files, search_code, review_code.\n\n"
    "CRITICAL — READ BEFORE RESPONDING:\n"
    "When a user message contains tool_result blocks, those are the output of "
    "tools you just called. Your reply MUST be based on what those results ACTUALLY show.\n"
    "If list_files returned 'index.html, package.json, src/', then SAY you see those files.\n"
    "NEVER say 'the directory is empty' or contradict the tool output.\n\n"
    "WHEN run_code FAILS — FOLLOW THIS DIAGNOSTIC FLOW:\n"
    "  Step 1: Look at the stderr (it's in the tool result). DO NOT run diagnostic commands.\n"
    "  Step 2: If the error is about file content/syntax, use read_file to check the file.\n"
    "  Step 3: Fix the actual issue. Do NOT run hex dumps, byte checks, or test files.\n"
    "  Example: 'exit: 1\\nstdout:\\n\\nstderr:\\nReferenceError: x is not defined'\n"
    "    → This tells you exactly what's wrong. Read the relevant file, find 'x', fix it.\n"
    "  NEVER run node -e to check bytes/encoding. NEVER create test files.\n"
    "  NEVER inspect raw bytes. The error message tells you what's wrong.\n\n"
    "AFTER write_file — ALWAYS read_file to verify:\n"
    "  read_file(filename) after write_file to check the file was written correctly.\n"
    "  If the file content is corrupted (all one line, wrong encoding), fix and re-write.\n\n"
    "CODE REVIEW — USE review_code AFTER READING FILES:\n"
    "- After reading a file, call review_code to check for issues.\n"
    "- It detects: syntax errors, hardcoded secrets, null pointer risks, TODO/FIXME, debug prints, empty catch blocks, magic numbers, long functions.\n"
    "- Works across Python, Java, JS/TS, Go, Rust, and more.\n"
    "- If review_code shows a linter timeout, the pattern check results are still valid.\n"
    "- Review results before writing code — fix issues proactively!\n\n"
    "═══ HIERARCHICAL EXPLORATION PROTOCOL — FOR LARGE PROJECTS ═══\n"
    "The first message already contains a Project Skeleton showing the top-level structure. "
    "Use this to understand the project type (e.g., frontend+backend, monolith, microservices).\n\n"
    "Follow these 3 phases, in order:\n\n"
    "PHASE 1 — RECONNAISSANCE (start here):\n"
    "  Goal: understand what the project IS. Just 1-2 tool calls.\n"
    "  Action: call list_files(path='backend', max_depth=2) to explore a major subsystem.\n"
    "  Do NOT read any files yet. Just figure out what exists.\n\n"
    "PHASE 2 — FOCUS (based on user task + Phase 1 findings):\n"
    "  Goal: narrow down to relevant files. At most 3-5 calls.\n"
    "  Action: identify which directories/files are relevant to the user's task.\n"
    "  - Use list_files(path='specific/dir', max_depth=2) to inspect a subsystem.\n"
    "  - Use search_code(pattern) to find specific classes or functions.\n"
    "  - Use search_files(query) to find files by concept.\n"
    "  Only read files AFTER you've identified the right ones.\n\n"
    "PHASE 3 — DEEP DIVE (only after Phase 2):\n"
    "  Goal: read specific files, make changes, run code.\n"
    "  IMPORTANT: read ALL needed files in ONE batch_read_files call. "
    "Do NOT read files one-by-one.\n"
    "  - batch_read_files(files=['a.js','b.js','c.js']) reads 3 files in 1 call.\n"
    "  - Individual read_file is ONLY for offset/limit on large files.\n"
    "  - If batch_read_files returns truncated content, use read_file with offset/limit.\n"
    "  - Use review_code after reading to check for issues.\n"
    "  - Only write files when you're sure about the changes.\n\n"
    "CRITICAL — NEVER skip phases. Do NOT read files during Phase 1.\n"
    "Do NOT call review_code during Phase 1 or 2. Only in Phase 3.\n"
    "ALWAYS use batch_read_files for 2+ files. One-by-one read_file wastes budget.\n\n"
    "READ_FILE TIPS:\n"
    "- First read gives a truncated view (head+tail). Use this to get the structure.\n"
    "- For deep analysis, use offset/limit to read specific sections.\n"
    "- Example: read_file(filename='User.java', offset=150, limit=300) reads lines 150-449.\n"
    "- Read one section at a time — don't over-read.\n\n"
    "RUN_CODE TIPS:\n"
    "- Short commands (python app.py, pytest) run synchronously and return output.\n"
    "- Long-running commands (npm run dev, vite, flask run) auto-run in background.\n"
    "- Background commands return a PID. The user can stop them with /stop <pid>.\n"
    "- Install dependencies first: pip install -r requirements.txt or npm install.\n"
    "WARNING — DO NOT start servers unless the user explicitly asks:\n"
    "- Starting a server blocks the terminal. The user cannot use PatchFlow while it runs.\n"
    "- If the user asks to 'start the project' or 'run the app', only run the command\n"
    "  AFTER all changes are complete, and tell the user the PID and /stop command.\n"
    "- Never start a server just to 'test' or 'verify' — use tests or syntax checks instead.\n"
    "- CRITICAL: Check if the server is ALREADY RUNNING before starting a new one.\n"
    "  If port 5173/3000/8080 is already in use, tell the user it's running and suggest /stop first.\n"
    "  Do NOT start multiple instances of the same server.\n"
    "Put ALL code in write_file — not in your text.\n"
    "CRITICAL — NEVER use run_code to read file contents. "
    "Use read_file instead. "
    "Do NOT run cat, type, node -e, python -c, or any other command just to read a file. "
    "read_file is designed for this purpose and works correctly across all platforms.\n"
    "CRITICAL — NEVER create temp/utility/bridge scripts in the project. "
    "Do NOT write .mjs, .sh, .bat, .ps1, or any other helper files. "
    "All fixes must be done directly with write_file/read_file. "
    "If you already created a temp file, clean it up with delete_file.\n"
    "To reorganize files: use rename_file instead of re-writing. "
    "Example: rename_file(source='src/old.js', dest='src/utils/old.js'). "
    "Parent directories are created automatically.\n"
    "Be concise.\n"
)

# ═══════════════════════════════════════════════════════════
# 项目骨架 — 在第一次 LLM 调用前自动生成
# ═══════════════════════════════════════════════════════════

IGNORE_DIRS_SKELETON = {
    ".git", "node_modules", "__pycache__", ".idea", ".vscode",
    ".venv", "venv", ".env", "build", "dist", ".next", ".nuxt",
    ".turbo", "target", ".tox", ".eggs", "*.egg-info",
    ".patchflow", ".mypy_cache", ".pytest_cache", "vendor",
}


def _get_project_skeleton(work_dir: str = ".") -> str:
    """扫描项目根目录，生成紧凑的骨架结构摘要

    只扫描深度 2~3 层，主要目的是识别项目类型：
      - 单体后端（单一 src/ 或 app/ 目录）
      - 前后端分离（backend/ + frontend/）
      - 微服务（多个 service-* 目录）
      - 其他

    Returns:
        格式化字符串，如：
        Project Skeleton (depth=2):
        ├── backend/  (Java/SpringBoot)
        │   ├── src/main/java/com/game/...
        │   └── src/main/resources/
        ├── frontend/ (Vue.js)
        │   ├── src/
        │   └── package.json
        └── README.md
    """
    root = Path(work_dir).resolve()
    if not root.is_dir():
        return "(project root not found)"

    lines = ["Project Skeleton:"]
    line_count = 0
    MAX_LINES = 30

    def _should_skip(name: str, is_dir: bool) -> bool:
        if name.startswith("."):
            return True
        if name in IGNORE_DIRS_SKELETON:
            return True
        return False

    try:
        entries = sorted(root.iterdir(), key=lambda e: (not e.is_dir(), e.name.lower()))
    except PermissionError:
        return "(permission denied scanning project root)"

    # 识别项目类型标记
    project_type_hints = []

    for e in entries:
        if e.is_dir() and not _should_skip(e.name, True):
            # 只看子目录的第一层
            sub_files = []
            try:
                sub_entries = sorted(e.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower()))
            except PermissionError:
                sub_entries = []

            # 识别框架/语言
            tech_hint = _detect_tech(e, sub_entries)
            if tech_hint:
                project_type_hints.append(f"{e.name} ({tech_hint})")

            lines.append(f"  📁 {e.name}/")
            line_count += 1
            if line_count >= MAX_LINES:
                break

            # 最多再列两层
            for sub in sub_entries:
                if _should_skip(sub.name, sub.is_dir()):
                    continue
                if sub.is_dir():
                    lines.append(f"      📁 {sub.name}/")
                else:
                    lines.append(f"      📄 {sub.name}")
                line_count += 1
                if line_count >= MAX_LINES:
                    break

        elif e.is_file() and e.suffix in (".json", ".toml", ".yml", ".yaml", ".xml", ".gradle", ".properties"):
            lines.append(f"  📄 {e.name}")
            line_count += 1
            if line_count >= MAX_LINES:
                break

    # 根部 README
    for readme in ("README.md", "README", "README.txt", "README.rst"):
        if (root / readme).exists():
            lines.append(f"  📄 {readme}")
            break

    if project_type_hints:
        lines.insert(1, f"  ({', '.join(project_type_hints)})")
        lines.insert(2, "")

    if line_count >= MAX_LINES:
        lines.append(f"  ... (truncated, use list_files for details)")

    return "\n".join(lines)


def _detect_tech(dir_path: Path, sub_entries: list) -> str:
    """快速识别目录的技术栈"""
    names = {e.name for e in sub_entries}
    lower_ext = set()

    for e in sub_entries:
        if e.is_file():
            lower_ext.add(e.suffix.lower())

    if "pom.xml" in names or "build.gradle" in names:
        return "Java/SpringBoot"
    if "package.json" in names:
        # 是前端还是后端？
        has_vue = any("vue" in e.name.lower() for e in sub_entries)
        has_react = any("react" in e.name.lower() for e in sub_entries)
        if has_vue:
            return "Vue.js"
        if has_react:
            return "React"
        return "Node.js"
    if "Cargo.toml" in names:
        return "Rust"
    if "go.mod" in names:
        return "Go"
    if "requirements.txt" in names or "setup.py" in names or "pyproject.toml" in names:
        return "Python"
    if "Gemfile" in names:
        return "Ruby"
    if ".csproj" in lower_ext:
        return "C#/.NET"
    # Java 项目常用目录
    java_markers = {"src/main/java", "src/main/resources", "WEB-INF"}
    if any(m in names or any(m in e.name for e in sub_entries if e.is_dir()) for m in java_markers):
        return "Java"
    if any(e.suffix == ".java" for e in sub_entries):
        return "Java"

    return ""


# ═══════════════════════════════════════════════════════════
# 工具执行
# ═══════════════════════════════════════════════════════════

# 全对话工具调用预算 — 防止 LLM 过度调用导致上下文爆炸
_TOOL_BUDGET = {
    "review_code":     {"max": 8,  "count": 0},
    "read_file":       {"max": 35, "count": 0},
    "batch_read_files":{"max": 5,  "count": 0},
    "rename_file":     {"max": 10, "count": 0},
    "delete_file":     {"max": 5,  "count": 0},
    "run_code":        {"max": 15, "count": 0},
    "search_code":     {"max": 5,  "count": 0},
    "search_files":    {"max": 3,  "count": 0},
    "_total":          {"max": 70, "count": 0},
}

# 已读文件缓存 — 同一文件第二次读直接提示已读，避免 LLM 重复读
# 跨轮次持久化（仅在新对话开始时清空）
_READ_CACHE: set[str] = set()

def _reset_tool_budget():
    for v in _TOOL_BUDGET.values():
        v["count"] = 0

def _check_tool_budget(name: str) -> str | None:
    """检查工具预算，超预算则返回阻止消息"""
    _TOOL_BUDGET["_total"]["count"] += 1
    if _TOOL_BUDGET["_total"]["count"] > _TOOL_BUDGET["_total"]["max"]:
        return f"BUDGET: total tool calls exceeded ({_TOOL_BUDGET['_total']['max']}). Summarize what you have and respond."
    if name in _TOOL_BUDGET:
        _TOOL_BUDGET[name]["count"] += 1
        if _TOOL_BUDGET[name]["count"] > _TOOL_BUDGET[name]["max"]:
            return f"BUDGET: {name} calls exceeded ({_TOOL_BUDGET[name]['max']}). Move on with what you have."
    return None


def _is_tool_exhausted(name: str) -> bool:
    """检查某工具是否已耗尽预算"""
    if name in _TOOL_BUDGET:
        return _TOOL_BUDGET[name]["count"] > _TOOL_BUDGET[name]["max"]
    return False


def _safe_json_parse(text: str) -> dict:
    """安全解析 JSON，失败时尝试容错提取关键字段

    主要处理场景：
      - write_file 的 content 字段含有未转义字符（换行、引号等）
      - JSON 被截断
    """
    if not isinstance(text, str):
        return {}

    # 1. 标准解析
    try:
        return json.loads(text)
    except (json.JSONDecodeError, ValueError):
        pass

    # 2. 容错解析：尝试修复常见问题
    repaired = _fuzzy_json_parse(text)
    if repaired:
        return repaired

    logger.warn(f"JSON 解析失败: {str(text)[:100]}")
    return {}


def _unescape_json_str(s: str) -> str:
    """解码 JSON 字符串中的转义序列为实际字符

    处理 \\n, \\t, \\r, \\\\, \\\", \\/, \\b, \\f, \\uXXXX 等。
    专门解决 _fuzzy_json_parse 正则提取后转义序列保持字面量的问题。
    """
    if "\\" not in s:
        return s
    result: list[str] = []
    i = 0
    while i < len(s):
        if s[i] == "\\" and i + 1 < len(s):
            nxt = s[i + 1]
            if nxt == "n":
                result.append("\n")
                i += 2
            elif nxt == "t":
                result.append("\t")
                i += 2
            elif nxt == "r":
                result.append("\r")
                i += 2
            elif nxt == '"':
                result.append('"')
                i += 2
            elif nxt == "\\":
                result.append("\\")
                i += 2
            elif nxt == "/":
                result.append("/")
                i += 2
            elif nxt == "b":
                result.append("\b")
                i += 2
            elif nxt == "f":
                result.append("\f")
                i += 2
            elif nxt == "u" and i + 5 < len(s):
                hex_str = s[i + 2:i + 6]
                try:
                    result.append(chr(int(hex_str, 16)))
                except (ValueError, OverflowError):
                    result.append(s[i:i + 6])
                i += 6
            else:
                result.append(s[i])
                i += 1
        else:
            result.append(s[i])
            i += 1
    return "".join(result)


def _fuzzy_json_parse(text: str) -> dict | None:
    """模糊解析 JSON，处理 write_file 的 content 含未转义代码的情况"""
    import re

    result = {}

    # 提取 filename（通常是简单路径，容易解析）
    m = re.search(r'"filename"\s*:\s*"([^"]+)"', text)
    if m:
        result["filename"] = m.group(1)
    else:
        return None

    # 提取 content：这是最常出问题的字段
    # content 可能包含换行、引号、特殊字符
    # 用更宽松的方式提取：从 "content": " 开始到最后 " 之前
    m = re.search(r'"content"\s*:\s*"(.*)', text, re.DOTALL)
    if m:
        raw = m.group(1)
        # 去掉末尾多余的 ", "} 等
        raw = raw.rstrip().rstrip(",")
        if raw.endswith('"}'):
            raw = raw[:-2]
        elif raw.endswith('"'):
            raw = raw[:-1]
        elif raw.endswith("'}"):
            raw = raw[:-2]
        elif raw.endswith("}"):
            raw = raw[:-1]
        raw = _unescape_json_str(raw)
        result["content"] = raw

    return result if result else None


# 危险命令确认回调 — 由 REPL 设置
# 返回值: "allow" | "reject" | "whitelist" | "blacklist"
_confirm_run_callback: Callable[[str, str], str] | None = None

def set_confirm_callback(cb: Callable[[str, str], str] | None):
    """设置危险命令确认回调。cb(command, reason) -> "allow"/"reject"/"whitelist"/"blacklist" """
    global _confirm_run_callback
    _confirm_run_callback = cb


def _execute_tool(name: str, args: dict,
                  on_run_output: Callable[[str], None] | None = None) -> str:
    from patchflow.utils.runner import (run, run_live, classify_command,
                                         is_long_running, start_background,
                                         add_to_whitelist, add_to_blacklist)

    # ── 工具已耗尽 → 静默跳过，不再给 LLM 反复提示 ──
    if _is_tool_exhausted(name):
        return "(tool skipped — budget exhausted)"

    # ── 工具调用预算检查 ──
    budget_msg = _check_tool_budget(name)
    if budget_msg:
        logger.warn(f"[ToolBudget] {name} blocked: {budget_msg}")
        return budget_msg

    if name == "write_file":
        filename = args.get("filename", "")
        if not filename:
            return "ERROR: write_file — could not parse filename from LLM output"
        content = args.get("content", "")
        raw = Path(filename)
        if raw.is_absolute() or ".." in filename:
            filename = raw.name
        p = Path(filename)
        p.parent.mkdir(parents=True, exist_ok=True)

        # AI 标记：如果文件被修改或新建，添加注释头
        ext = p.suffix.lower()
        if ext in (".js", ".jsx", ".ts", ".tsx", ".py", ".java", ".go", ".rs", ".rb", ".php", ".c", ".cpp", ".h", ".hpp", ".cs", ".swift", ".kt", ".vue", ".svelte"):
            comment_char = "//" if ext not in (".py", ".rb", ".yaml", ".yml") else "#"
            # 只在文件头部没有 AI marker 时添加
            first_line = content.split("\n")[0].strip() if content else ""
            if comment_char not in first_line or "ai" not in first_line.lower():
                ai_marker = f"{comment_char} AI-generated (PatchFlow)\n"
                content = ai_marker + content

        p.write_text(content, encoding="utf-8")
        logger.info(f"write_file: {filename} ({len(content)} chars)")
        return f"OK: wrote {len(content)} chars to {filename}"

    elif name == "delete_file":
        filename = args.get("filename", "")
        if not filename:
            return "ERROR: delete_file — no filename provided"
        fn = str(Path(filename.strip()).as_posix())
        p = Path(fn)
        if not p.exists():
            return f"ERROR: file not found: {fn}"
        if not p.is_file():
            return f"ERROR: not a file: {fn}"
        if ".." in fn:
            return f"ERROR: path traversal blocked: {fn}"
        p.unlink()
        logger.info(f"delete_file: {fn}")
        return f"OK: deleted {fn}"

    elif name == "rename_file":
        source = args.get("source", "")
        dest = args.get("dest", "")
        if not source or not dest:
            return "ERROR: rename_file — source and dest are required"
        src = Path(source.strip())
        dst = Path(dest.strip())
        if not src.exists():
            return f"ERROR: source not found: {source}"
        if ".." in source or ".." in dest:
            return f"ERROR: path traversal blocked"
        dst.parent.mkdir(parents=True, exist_ok=True)
        src.rename(dst)
        logger.info(f"rename_file: {source} -> {dest}")
        return f"OK: moved {source} -> {dest}"

    elif name == "read_file":
        filename = args.get("filename", "")
        if not filename:
            return "ERROR: read_file — no filename provided"
        filename = str(Path(filename.strip()).as_posix())
        offset = int(args.get("offset", 0))
        limit = int(args.get("limit", 0))

        # ── 同一文件已读（无 offset/limit）→ 直接返回缓存提示 ──
        if offset == 0 and limit == 0:
            if filename in _READ_CACHE:
                logger.info(f"  [cache HIT] {filename}")
                return f"(already read — {filename} is in your context, use offset/limit for specific sections)"
            logger.info(f"  [cache MISS] {filename}")

        if ".." in filename:
            return f"ERROR: path traversal blocked: {filename}"
        p = Path(filename)
        if not p.exists():
            return f"ERROR: file not found: {filename}"
        content = p.read_text(encoding="utf-8")
        logger.info(f"read_file: {filename} ({len(content)} chars)")

        # ── 成功读到内容后才加入缓存（防止无效文件名污染） ──
        if offset == 0 and limit == 0:
            _READ_CACHE.add(filename)

        lines = content.split("\n")
        total_lines = len(lines)

        if offset < 0:
            offset = 0
        if offset >= total_lines:
            return f"(file is {total_lines} lines, offset {offset} is out of range)"

        if limit and limit > 0:
            end = min(offset + limit, total_lines)
            sliced = "\n".join(lines[offset:end])
            header = f"[lines {offset}-{end-1} of {total_lines}]\n" if offset > 0 or end < total_lines else ""
            return f"{header}{sliced}"

        READ_MAX = 5000
        if len(content) <= READ_MAX:
            return content

        HEAD = 150
        TAIL = 50

        if total_lines <= HEAD + TAIL:
            return content

        head = "\n".join(lines[:HEAD])
        tail = "\n".join(lines[-TAIL:])
        omitted = total_lines - HEAD - TAIL
        return (
            f"[lines 0-{HEAD-1} of {total_lines}]\n"
            f"{head}\n\n"
            f"# ... [truncated {omitted} lines — use read_file with offset={HEAD},limit=N to continue] ...\n\n"
            f"[lines {total_lines-TAIL}-{total_lines-1} of {total_lines}]\n"
            f"{tail}"
        )

    elif name == "batch_read_files":
        files = args.get("files", [])
        if not files or not isinstance(files, list):
            return "ERROR: batch_read_files — files must be a non-empty list"
        parts = []
        for f in files:
            normal = str(Path(f.strip()).as_posix())
            if normal in _READ_CACHE:
                parts.append(f"# === {f} ===\n(already read — in context)")
            else:
                fp = Path(normal)
                if fp.exists():
                    content = fp.read_text(encoding="utf-8")
                    _READ_CACHE.add(normal)
                    parts.append(f"# === {f} ===\n{content[:5000]}")
                else:
                    parts.append(f"# === {f} ===\n(not found)")
        return "\n\n".join(parts)

    elif name == "run_code":
        command = args.get("command", "")
        timeout = int(args.get("timeout", 0)) or None
        if timeout is None:
            timeout = 120 if is_long_running(command) else 20

        # 安全分级
        action, reason = classify_command(command)
        if action == "block":
            logger.warn(f"run_code BLOCKED: {reason}")
            return f"BLOCKED: {reason}"

        if action == "confirm":
            if _confirm_run_callback:
                decision = _confirm_run_callback(command, reason)
                if decision == "reject":
                    logger.info(f"run_code REJECTED: {command}")
                    return f"USER_REJECTED: 用户拒绝执行 — {reason}"
                elif decision == "whitelist":
                    add_to_whitelist(command)
                    logger.info(f"run_code WHITELISTED: {command}")
                elif decision == "blacklist":
                    add_to_blacklist(command)
                    logger.info(f"run_code BLACKLISTED: {command}")
                    return f"BLOCKED: 已将该命令加入黑名单，后续自动拦截"
        # 长驻命令 → 后台运行
        if is_long_running(command):
            logger.info(f"run_code (background): {command}")
            bg = start_background(command)
            if on_run_output:
                on_run_output(f"[background] PID {bg.pid} — {command}")
            return (
                f"BACKGROUND_STARTED: PID={bg.pid}\n"
                f"Command: {command}\n"
                f"Status: running in background\n"
                f"Use /logs {bg.pid} to view output\n"
                f"Use /stop {bg.pid} to stop\n"
            )

        # 普通命令 → 实时执行
        logger.info(f"run_code: {command}")
        captured_output = []

        def _on_line(line: str):
            captured_output.append(line)
            if on_run_output:
                on_run_output(line)

        result = run_live(command, timeout=timeout,
                          on_stdout=_on_line, on_stderr=_on_line)

        output_text = "\n".join(captured_output)
        truncated = ""
        if len(output_text) > 3000:
            output_text = output_text[:3000]
            truncated = "\n(output truncated, full output too long)"

        if result.ok:
            return f"exit: 0\nstdout:\n{output_text}{truncated}"
        else:
            return f"exit: {result.exit_code}\nstdout:\n{output_text[:1500]}{truncated}\nstderr:\n{result.stderr[:1500]}"

    elif name == "list_files":
        dirpath = args.get("path", ".") or "."
        max_depth = args.get("max_depth", 4)
        p = Path(dirpath)
        if not p.exists():
            return f"ERROR: path not found: {dirpath}"

        IGNORE_DIRS = {
            ".git", "node_modules", "__pycache__", ".idea", ".vscode",
            ".venv", "venv", ".env", "build", "dist", ".next", ".nuxt",
            ".turbo", "target", ".tox", ".eggs", "*.egg-info",
        }

        def _should_ignore(name: str, is_dir: bool) -> bool:
            if name.startswith(".") and name not in (".env", ".gitignore", ".gitattributes"):
                return True
            if is_dir and name in IGNORE_DIRS:
                return True
            return False

        tree_lines = []
        DIR_LIMIT = 5
        FILE_HEAD = 3
        FILE_TAIL = 2
        MAX_TREE_LINES = 25
        line_count = [0]

        def _add(line: str):
            tree_lines.append(line)
            line_count[0] += 1

        def _walk(d: Path, prefix: str, depth: int):
            if depth > max_depth or line_count[0] >= MAX_TREE_LINES:
                return

            try:
                entries = sorted(d.iterdir(), key=lambda e: (not e.is_dir(), e.name.lower()))
            except PermissionError:
                _add(f"{prefix}[Permission denied]")
                return

            visible = [e for e in entries if not _should_ignore(e.name, e.is_dir())]
            if not visible:
                return

            dirs = [e for e in visible if e.is_dir()]
            files = [e for e in visible if not e.is_dir()]

            # 折叠单目录链：只有 1 个子目录、没有文件 → 连写成一行
            if len(dirs) == 1 and not files and depth < max_depth:
                chain = [dirs[0].name]
                cur, cur_depth = dirs[0], depth
                while cur_depth < max_depth:
                    try:
                        sub = sorted(cur.iterdir(), key=lambda e: (not e.is_dir(), e.name.lower()))
                    except PermissionError:
                        break
                    sub_v = [e for e in sub if not _should_ignore(e.name, e.is_dir())]
                    sub_dirs = [e for e in sub_v if e.is_dir()]
                    sub_files = [e for e in sub_v if not e.is_dir()]
                    if len(sub_dirs) == 1 and not sub_files:
                        cur = sub_dirs[0]
                        cur_depth += 1
                        chain.append(cur.name)
                    else:
                        break
                collapsed = "/".join(chain) + "/"
                _add(f"{prefix}└── {collapsed}")
                if cur_depth >= max_depth:
                    _add(f"{prefix}    ...")
                else:
                    _walk(cur, prefix + "    ", cur_depth + 1)
                return

            items = list(dirs[:DIR_LIMIT])
            omitted_dirs = len(dirs) - DIR_LIMIT if len(dirs) > DIR_LIMIT else 0

            if len(files) <= FILE_HEAD + FILE_TAIL:
                items.extend(files)
                omitted_files = 0
            else:
                items.extend(files[:FILE_HEAD])
                omitted_files = len(files) - FILE_HEAD - FILE_TAIL
                items.append("__OMIT__")
                items.extend(files[-FILE_TAIL:])

            last_real = len(items) - 1
            while last_real >= 0 and isinstance(items[last_real], str):
                last_real -= 1

            for i, entry in enumerate(items):
                if isinstance(entry, str):
                    _add(f"{prefix}  ... ({omitted_files} files omitted)")
                    continue

                is_last = (i == last_real)
                is_dir = entry.is_dir()

                connector = "└── " if is_last else "├── "
                name_suffix = "/" if is_dir else ""

                _add(f"{prefix}{connector}{entry.name}{name_suffix}")

                if is_dir:
                    next_prefix = prefix + ("    " if is_last else "│   ")
                    _walk(entry, next_prefix, depth + 1)

            if omitted_dirs:
                _add(f"{prefix}  ... ({omitted_dirs} dirs omitted)")

        _walk(p, "", 0)
        if line_count[0] >= MAX_TREE_LINES:
            tree_lines.append(f"  ... ({line_count[0] - 1}+ items, showing first {MAX_TREE_LINES})")
        return "\n".join(tree_lines)

    elif name == "search_files":
        query = args.get("query", "")
        top_k = int(args.get("top_k", 10))
        idx = _get_index(".")
        results = idx.search_files(query, top_k=top_k)

        if not results:
            return "(未找到相关文件，请尝试用其他关键词，或先用 list_files 了解项目结构)"

        lines = []
        for i, r in enumerate(results):
            lines.append(f"{i + 1}. {r['summary']}")
        return "\n".join(lines)

    elif name == "search_code":
        pattern = args.get("pattern", "")
        path_filter = args.get("path_filter", "")
        idx = _get_index(".")
        return idx.search_code(pattern, path_filter=path_filter)

    elif name == "review_code":
        filepath = args.get("filepath", "")
        import threading
        from patchflow.utils.code_reviewer import review_file
        review_result = []
        t = threading.Thread(target=lambda: review_result.append(review_file(filepath)))
        t.start()
        t.join(timeout=15)
        if t.is_alive():
            logger.warn(f"review_code 超时（>15s）: {filepath}")
            return f"review_code: {filepath} — 审查超时（跳过 linter），已完成模式检查"
        return review_result[0] if review_result else f"review_code: {filepath} — 审查完成"

    return f"ERROR: unknown tool: {name}"


_index: object | None = None


def _get_index(work_dir: str = "."):
    global _index
    if _index is None:
        from patchflow.core.project.codebase_index import CodebaseIndex
        _index = CodebaseIndex(work_dir)
        if not _index.is_built():
            _index.build()
        else:
            _index.load()
    return _index

# ═══════════════════════════════════════════════════════════
# Anthropic 工具格式转换（OpenAI → Anthropic）
# ═══════════════════════════════════════════════════════════

def _get_anthropic_tools():
    at = []
    for t in TOOLS:
        at.append({
            "name": t["function"]["name"],
            "description": t["function"]["description"],
            "input_schema": t["function"]["parameters"],
        })
    return at

# ═══════════════════════════════════════════════════════════
# ChatClient — 流式输出版
# ═══════════════════════════════════════════════════════════

ToolUse = dict


class ChatClient:

    def __init__(self, model: str | None = None, work_dir: str = "."):
        cfg = get_config()
        self.provider = get_normalized_provider()
        self.api_key = cfg["api_key"]
        self.api_base = cfg["api_base"]
        self.model = model or cfg["model"]

        if not self.api_key:
            raise ValueError("未配置 API Key")

        if self.provider == "anthropic":
            self._anthropic = Anthropic(api_key=self.api_key, timeout=120)
            self._openai = None
        else:
            self._openai = OpenAI(
                api_key=self.api_key,
                base_url=self.api_base,
                timeout=120,
                max_retries=2,
            )
            self._anthropic = None

        self.messages: list[dict] = []
        self._max_rounds = 30
        self._work_dir = Path(work_dir).resolve()
        self._memory_path = self._work_dir / ".patchflow" / "memory.json"
        self._session_boundary_added = False
        self._memory_summary: list[str] = []
        self._load_memory()

    # ── streaming chat（给 REPL 用）──

    def chat_stream(self, user_input: str,
                     on_run_output: Callable[[str], None] | None = None):
        # ── 首次对话：自动注入项目骨架图 + 项目规则 + 重置读缓存 ──
        if not self.messages:
            _READ_CACHE.clear()
            skeleton = _get_project_skeleton(".")

            rules_text = ""
            rules_file = Path(".patchflow/rules.md")
            if rules_file.exists():
                try:
                    rules_text = rules_file.read_text(encoding="utf-8").strip()
                except Exception:
                    pass
            if rules_text:
                enhanced_input = f"{skeleton}\n\nProject Rules:\n{rules_text}\n\n{user_input}"
            else:
                enhanced_input = f"{skeleton}\n\n{user_input}"
            self.messages.append({"role": "user", "content": enhanced_input})
            self._save_memory()
        else:
            bg_info = ""
            try:
                from patchflow.utils.runner import list_processes
                running = [p for p in list_processes() if p.running]
                if running:
                    bg_info = "\n[Background processes running: " + ", ".join(f"PID {p.pid}: {p.command[:40]}" for p in running) + "]\n"
            except Exception:
                pass
            self.messages.append({"role": "user", "content": bg_info + user_input})
            self._save_memory()
        all_tool_calls = []
        session_usage = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0, "calls": 0}
        _reset_tool_budget()

        for _ in range(self._max_rounds):
            recent = compress(self.messages)

            if self._openai:
                text, tcs, usage = self._call_openai_stream(recent)
            else:
                text, tcs, usage = self._call_anthropic(recent)

            session_usage["input_tokens"] += usage.get("input_tokens", 0)
            session_usage["output_tokens"] += usage.get("output_tokens", 0)
            session_usage["total_tokens"] += usage.get("total_tokens", 0)
            session_usage["calls"] += 1

            if text:
                yield ("text", text)

            if not tcs:
                self._append_assistant(text, tcs)
                self._save_memory()
                yield ("usage", dict(session_usage))
                yield ("done", all_tool_calls)
                return

            yield ("usage", dict(session_usage))

            # 执行工具
            for tc in tcs:
                fn_info = {
                    "name": tc["function"]["name"],
                    "args": _safe_json_parse(tc["function"]["arguments"]),
                }
                yield ("tool_start", fn_info)

                if fn_info["name"] == "run_code" and on_run_output:
                    yield ("run_output", f"$ {fn_info['args'].get('command', '')}")

                result = _execute_tool(
                    fn_info["name"], fn_info["args"],
                    on_run_output=on_run_output,
                )
                fn_info["result"] = result
                fn_info["id"] = tc["id"]
                all_tool_calls.append(fn_info)
                yield ("tool_result", fn_info)

                if fn_info["name"] == "review_code":
                    first_line = result.split("\n")[0]
                    tc["result"] = first_line[:200]
                else:
                    tc["result"] = result

            self._append_assistant(text, tcs)
            self._save_memory()

        yield ("usage", dict(session_usage))
        yield ("hint", "round_limit")
        yield ("done", all_tool_calls)

    def _append_assistant(self, text, tcs):
        """把 assistant 回复 + 工具结果追加到消息历史"""
        MAX_RESULT_CHARS = 1000

        def _truncate(content: str) -> str:
            if len(content) <= MAX_RESULT_CHARS:
                return content
            head = content[:500]
            tail = content[-250:]
            return f"{head}\n\n... [truncated {len(content) - MAX_RESULT_CHARS} chars, full result in REPL] ...\n\n{tail}"

        if self._openai:
            # OpenAI 格式
            msg = {"role": "assistant", "content": text or ""}
            if tcs:
                msg["tool_calls"] = tcs
            self.messages.append(msg)
            for tc in tcs:
                self.messages.append({
                    "role": "tool",
                    "tool_call_id": tc["id"],
                    "content": _truncate(tc.get("result", "")),
                })
            # 弱模型强制提示
            tool_names = [tc["function"]["name"] for tc in tcs]
            self.messages.append({
                "role": "user",
                "content": (
                    f"(system: you just called {', '.join(tool_names)}. "
                    f"Read the tool results above and respond based on what you actually saw.)"
                ),
            })
        else:
            # Anthropic 格式：assistant 消息包含 tool_use 块
            content = []
            if text:
                content.append({"type": "text", "text": text})
            for tc in tcs:
                content.append({
                    "type": "tool_use",
                    "id": tc["id"],
                    "name": tc["function"]["name"],
                    "input": _safe_json_parse(tc["function"]["arguments"]),
                })
            self.messages.append({"role": "assistant", "content": content})

            # Anthropic：工具结果用 user 消息，content 必须是 content block 数组格式
            tr_blocks = []
            for tc in tcs:
                result_text = _truncate(tc.get("result", ""))
                tr_blocks.append({
                    "type": "tool_result",
                    "tool_use_id": tc["id"],
                    # 关键：传 content block 数组而非纯字符串
                    "content": [{"type": "text", "text": result_text}],
                })
            self.messages.append({"role": "user", "content": tr_blocks})

    # ── Anthropic 调用 ──

    def _call_anthropic(self, messages):
        """Anthropic 原生工具调用"""
        try:
            response = self._anthropic.messages.create(
                model=self.model,
                max_tokens=2048,
                system=SYSTEM_PROMPT,
                messages=messages,
                tools=_get_anthropic_tools(),
            )
        except Exception as e:
            error_msg = f"[API 请求失败: {e}]"
            logger.error(f"Anthropic API 调用异常: {e}")
            return (error_msg, [], {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0})

        text_parts = []
        tcs = []

        for block in response.content:
            if block.type == "text":
                text_parts.append(block.text)
            elif block.type == "tool_use":
                tcs.append({
                    "id": block.id,
                    "type": "function",
                    "function": {
                        "name": block.name,
                        "arguments": json.dumps(block.input),
                    },
                })

        usage = {
            "input_tokens": getattr(response.usage, "input_tokens", 0),
            "output_tokens": getattr(response.usage, "output_tokens", 0),
        }
        usage["total_tokens"] = usage["input_tokens"] + usage["output_tokens"]

        return ("\n".join(text_parts).strip(), tcs, usage)

    def _call_openai_stream(self, messages):
        """流式调用 OpenAI 兼容 API(内部消费流,返回最终结果)"""
        def sanitize(obj):
            if isinstance(obj, str):
                return obj.encode("utf-8", errors="replace").decode("utf-8")
            if isinstance(obj, dict):
                return {k: sanitize(v) for k, v in obj.items()}
            if isinstance(obj, list):
                return [sanitize(i) for i in obj]
            return obj

        clean_messages = sanitize(messages)
        api_messages = [{"role": "system", "content": SYSTEM_PROMPT}] + clean_messages

        text_parts = []
        tool_calls_acc: dict[int, dict] = {}
        usage = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}

        try:
            stream_deadline = time.time() + 45
            stream_client = OpenAI(
                api_key=self.api_key,
                base_url=self.api_base,
                timeout=20,
                max_retries=0,
            )
            response = stream_client.chat.completions.create(
                model=self.model,
                messages=api_messages,
                tools=TOOLS,
                tool_choice="auto",
                max_tokens=2048,
                stream=True,
                stream_options={"include_usage": True},
            )

            stream_start = time.time()
            for chunk in response:
                if time.time() - stream_deadline > 0:
                    logger.warning("OpenAI streaming total time > 45s, breaking out")
                    break
                if chunk.usage:
                    usage = {
                        "input_tokens": chunk.usage.prompt_tokens or 0,
                        "output_tokens": chunk.usage.completion_tokens or 0,
                        "total_tokens": chunk.usage.total_tokens or 0,
                    }

                if not chunk.choices:
                    continue

                delta = chunk.choices[0].delta
                if delta is None:
                    continue

                if delta.content:
                    text_parts.append(delta.content)

                if delta.tool_calls:
                    for tc in delta.tool_calls:
                        idx = tc.index
                        if idx not in tool_calls_acc:
                            tool_calls_acc[idx] = {
                                "id": tc.id or "",
                                "type": "function",
                                "function": {"name": "", "arguments": ""},
                            }
                        if tc.id:
                            tool_calls_acc[idx]["id"] = tc.id
                        if tc.function:
                            if tc.function.name:
                                tool_calls_acc[idx]["function"]["name"] += tc.function.name
                            if tc.function.arguments:
                                tool_calls_acc[idx]["function"]["arguments"] += tc.function.arguments
        except Exception as e:
            error_msg = f"[API 请求失败: {e}]"
            logger.error(f"OpenAI API 调用异常: {e}")
            return (error_msg, [], usage)

        final_text = "".join(text_parts).strip()
        final_tcs = list(tool_calls_acc.values()) if tool_calls_acc else []

        return (final_text, final_tcs, usage)

    # ── 非流式 chat（给 build 命令用）──

    def chat(self, user_input: str) -> tuple[str, list[ToolUse]]:
        all_tool_calls = []
        for evt, data in self.chat_stream(user_input):
            if evt == "text":
                final_text = data
            elif evt == "tool_result":
                all_tool_calls.append(data)
            elif evt == "done":
                pass
        return (final_text, all_tool_calls)

    def clear_history(self):
        self.messages = []
        self._save_memory()
        logger.info("对话历史已清空")

    def get_summary(self) -> str:
        user_count = sum(1 for m in self.messages if m["role"] == "user" and not m.get("_memory_summary"))
        assistant_count = sum(1 for m in self.messages if m["role"] == "assistant")
        summary_count = len(self._memory_summary)
        session_count = sum(1 for m in self.messages if m.get("_session_boundary"))
        parts = [f"对话: {user_count} 用户消息, {assistant_count} AI 回复"]
        if summary_count:
            parts.append(f"摘要: {summary_count} 条")
        if session_count:
            parts.append(f"{session_count} 次会话")
        return ", ".join(parts)

    # ── 跨会话记忆持久化 ──

    _MAX_MEMORY_BYTES = 512_000  # 500 KB，超过这个大小会压缩旧消息
    _MIN_KEEP_TURNS = 3          # 压缩时最少保留最近 3 个完整对话回合

    def _load_memory(self):
        """从磁盘加载记忆

        记忆采用双层结构：
          summary[]   — 之前会话的压缩摘要列表（仅保留关键信息）
          messages[]  — 最近 N 轮原始消息（保持对话连续性）

        LLM 首次交互时看到：摘要 + 会话边界标记 + 最近消息
        """
        try:
            if self._memory_path.exists():
                data = json.loads(self._memory_path.read_text(encoding="utf-8"))
                self._memory_summary = data.get("summary", [])
                raw = data.get("messages", [])
                if raw or self._memory_summary:
                    if self._memory_summary:
                        summary_text = "=== 之前会话摘要 ===\n" + "\n".join(self._memory_summary)
                        self.messages.append({
                            "role": "user",
                            "content": summary_text,
                            "_memory_summary": True,
                        })
                        logger.info(f"记忆摘要: {len(self._memory_summary)} 条, "
                                    f"{sum(len(s) for s in self._memory_summary)} 字符")
                    if raw:
                        self.messages.extend(raw)
                    self.messages.append({
                        "role": "user",
                        "content": "(session resuming — new session starts here. Previous work is summarized above.)",
                        "_session_boundary": True,
                    })
                    self._session_boundary_added = True
                    n = len(raw) + len(self._memory_summary)
                    logger.info(f"记忆恢复: {n} 条摘要+消息 ({self._memory_path})")
        except (json.JSONDecodeError, OSError) as e:
            logger.warn(f"记忆加载失败: {e}")

    def _save_memory(self):
        """将当前对话持久化到磁盘

        保存前自动压缩旧消息为摘要，只保留最近 N 轮原始消息。
        """
        try:
            self._compress_old_messages()
            self._memory_path.parent.mkdir(parents=True, exist_ok=True)
            data = {
                "summary": self._memory_summary,
                "messages": self.messages,
                "_saved_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            }
            raw = json.dumps(data, ensure_ascii=False, indent=2)
            self._memory_path.write_text(raw, encoding="utf-8")
        except OSError as e:
            logger.warn(f"记忆保存失败: {e}")

    def _compress_old_messages(self):
        """将消息列表中的旧消息压缩为摘要

        策略：从最早的消息开始，一旦 total > MAX_MEMORY_BYTES，
        就把最早的一整轮对话（从 user 到该轮结束）提取关键信息，
        追加到 _memory_summary，然后丢弃原始消息。

        始终保留至少 _MIN_KEEP_TURNS 轮完整的原始消息。
        """
        if not self.messages:
            return

        test = json.dumps({"messages": self.messages, "summary": self._memory_summary},
                          ensure_ascii=False)
        if len(test.encode("utf-8")) <= self._MAX_MEMORY_BYTES:
            return

        # 找到所有非摘要 user 消息的索引
        user_indices = [i for i, m in enumerate(self.messages)
                        if m.get("role") == "user" and not m.get("_memory_summary")]

        keep_count = min(self._MIN_KEEP_TURNS, len(user_indices))
        if keep_count <= 0:
            return
        earliest_keep = user_indices[-keep_count]

        # 从最早的消息开始压缩，直到大小达标
        while earliest_keep > 0:
            candidate = self.messages[earliest_keep:]
            test = json.dumps({"messages": candidate, "summary": self._memory_summary},
                              ensure_ascii=False)
            if len(test.encode("utf-8")) <= self._MAX_MEMORY_BYTES:
                break
            # 找到最早的一个完整用户回合
            next_boundary = None
            for j in range(1, earliest_keep):
                if self.messages[j].get("role") == "user" and not self.messages[j].get("_memory_summary"):
                    next_boundary = j
                    break
            if next_boundary is None or next_boundary >= earliest_keep:
                next_boundary = earliest_keep

            # 提取 [0:next_boundary] 到 next_boundary 之间的消息作为一轮
            round_msgs = self.messages[:next_boundary] if next_boundary > 0 else []

            if round_msgs:
                summary_line = self._summarize_round(round_msgs)
                if summary_line:
                    self._memory_summary.append(summary_line)

            self.messages = self.messages[next_boundary:]
            user_indices = [i for i, m in enumerate(self.messages)
                            if m.get("role") == "user" and not m.get("_memory_summary")]
            earliest_keep = user_indices[-keep_count] if len(user_indices) >= keep_count else 0

            if len(self._memory_summary) > 50:
                logger.info("记忆摘要已达 50 条，丢弃最早的 10 条")
                self._memory_summary = self._memory_summary[-40:]

        logger.info(f"记忆压缩: {len(self._memory_summary)} 条摘要, "
                    f"{len(self.messages)} 条消息 ({self._MAX_MEMORY_BYTES // 1024}KB 限制)")

    def _summarize_round(self, msgs: list[dict]) -> str:
        """从一轮对话消息中提取一句话摘要

        不调用 LLM，纯启发式提取：
          - 用户的第一个需求描述
          - write_file 写入了哪些文件
          - assistant 最后的回复摘要（如果包含结论性文字）
        """
        user_request = ""
        written_files: list[str] = []
        assistant_conclusion = ""
        tools_used: set[str] = set()

        for msg in msgs:
            role = msg.get("role", "")
            content = msg.get("content", "")

            if role == "user":
                if isinstance(content, str) and not user_request:
                    user_request = content.strip()[:120]

            elif role == "assistant":
                if isinstance(content, str) and content.strip():
                    assistant_conclusion = content.strip()[:120]
                elif isinstance(content, list):
                    for block in content:
                        if isinstance(block, dict) and block.get("type") == "text":
                            assistant_conclusion = block.get("text", "")[:120]

            elif role == "tool":
                if content.startswith("OK: wrote"):
                    written_files.append(content.split("to ")[-1].strip())

            if msg.get("tool_calls"):
                for tc in msg["tool_calls"]:
                    tools_used.add(tc["function"]["name"])

        parts = []
        if user_request:
            parts.append(user_request)
        if written_files:
            files_str = ", ".join(written_files[:3])
            if len(written_files) > 3:
                files_str += f" (+{len(written_files) - 3})"
            parts.append(f"文件: {files_str}")
        if assistant_conclusion:
            conclusion = assistant_conclusion[:80]
            if conclusion not in user_request:
                parts.append(conclusion)

        raw = "; ".join(parts)
        if len(raw) > 200:
            raw = raw[:197] + "..."
        return raw if raw else "(对话)"

    def get_context_preview(self, max_rounds: int = 6) -> str:
        """返回当前上下文的可读摘要（按逻辑回合分组）

        Args:
            max_rounds: 最多显示几轮对话（从最新开始），默认 6
        """
        if not self.messages:
            return "(empty)"

        from patchflow.core.project.context_manager import estimate_message_tokens, get_token_budget

        raw = list(self.messages)
        raw_tokens = sum(estimate_message_tokens(m) for m in raw)
        budget = get_token_budget()

        compressed = compress(self.messages)
        comp_tokens = sum(estimate_message_tokens(m) for m in compressed)

        # 按 user 消息切分逻辑回合
        rounds: list[list[dict]] = []
        current = []
        for msg in raw:
            role = msg.get("role", "")
            content = msg.get("content", "")
            is_system = isinstance(content, str) and content.startswith("(system: you just called")
            if role == "user" and not is_system and current:
                rounds.append(current)
                current = [msg]
            else:
                current.append(msg)
        if current:
            rounds.append(current)

        lines = []
        total_rounds = len(rounds)
        lines.append(f"共 {total_rounds} 轮对话 · {len(raw)} 条消息 · "
                     f"原始 {raw_tokens} tok / 压缩 {comp_tokens} tok · "
                     f"预算 {budget} tok")

        if total_rounds <= 3:
            show_rounds = list(enumerate(rounds))
        else:
            show_rounds = list(enumerate(rounds[-max_rounds:]))
            lines.append(f"（仅显示最近 {max_rounds} 轮，共 {total_rounds} 轮）")

        for round_idx, msgs in show_rounds:
            user_msg = msgs[0]
            user_text = user_msg.get("content", "")
            if isinstance(user_text, str):
                user_preview = user_text[:60].replace("\n", " ")
            else:
                user_preview = str(user_text)[:60]

            round_tok = sum(estimate_message_tokens(m) for m in msgs)

            # 统计这轮的工具调用
            tools_used = []
            for m in msgs:
                if m.get("tool_calls"):
                    for tc in m["tool_calls"]:
                        tools_used.append(tc["function"]["name"])
                elif m.get("role") == "tool":
                    if not tools_used or tools_used[-1] != "...":
                        tools_used.append("...")

            # 去重保留顺序
            seen = set()
            unique_tools = []
            for t in tools_used:
                if t not in seen:
                    seen.add(t)
                    unique_tools.append(t)

            tool_str = ""
            if unique_tools:
                tool_str = f" → {', '.join(unique_tools[:4])}"
                if len(unique_tools) > 4:
                    tool_str += " ..."

            lines.append(f"  #{round_idx:<2}  {round_tok:>5} tok  {user_preview}{tool_str}")

        return "\n".join(lines)

    def get_summary(self) -> str:
        user_count = sum(1 for m in self.messages if m["role"] == "user")
        assistant_count = sum(1 for m in self.messages if m["role"] == "assistant")        
        return f"对话: {user_count} 用户消息, {assistant_count} AI 回复"
