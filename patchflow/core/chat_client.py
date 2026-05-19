"""对话客户端 — 工具调用 + 流式输出(Claude Code 风格)

AI 可以直接 write_file，read，edit_file，run_code，list，search，grep，review_code。
支持流式输出 —— 一个字一个字显示，不是等完了才一起出来。
"""

import json
import os
import re
import time
from collections.abc import Callable
from pathlib import Path

from anthropic import Anthropic
from openai import OpenAI

from patchflow.core.config import get_config, get_normalized_provider
from patchflow.core.language_strategy import LanguageFactory
from patchflow.core.project.context_manager import compress
from patchflow.utils import logger
from patchflow.utils.diff import diff_text, format_summary

# ═══════════════════════════════════════════════════════════
# 开发任务意图检测 — 用于智能记忆
#
# 核心思想：不猜用户说了什么，看 LLM 做了什么。
#   - LLM 调用了工具（write/run/delete/rename）→ 开发任务 → 持久化
#   - LLM 只回复文本（没调工具）→ 查询 → 跳过
#   - 用户粘贴了代码块（```）→ 代码相关 → 保留
# ═══════════════════════════════════════════════════════════

_log_tag = "[SmartMemory]"

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "创建或覆盖一个文件。如果文件所在的目录不存在，会自动创建。NEVER use run_code to create files (echo/cat/printf redirection) — use this tool instead.",
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
            "name": "read",
            "description": "读取文件内容。接受单个文件路径或路径列表，一个调用覆盖所有读文件场景。大文件自动截断（保留首尾），单文件可用 offset/limit 分页精读。NEVER use run_code (cat/type/python -c/node -e) to read files.",
            "parameters": {
                "type": "object",
                "properties": {
                    "files": {
                        "anyOf": [
                            {"type": "string", "description": "单个文件路径"},
                            {"type": "array", "items": {"type": "string"}, "description": "多个文件路径"}
                        ],
                        "description": "要读取的文件路径，如 'app.py' 或 ['app.py', 'utils.py']"
                    },
                    "offset": {"type": "integer", "description": "起始行号（0-based），默认 0，仅单文件时有效"},
                    "limit": {"type": "integer", "description": "最大读取行数，默认全部（大文件自动截断）"},
                },
                "required": ["files"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "rename_file",
            "description": "移动或重命名文件/目录。用于整理项目结构，如把文件移到子目录、给文件改名等。如果目标父目录不存在会自动创建。NEVER use run_code (mv/ren) to move files — use this tool.",
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
            "name": "delete_file",
            "description": "删除文件。用于清理临时文件、废弃代码或不需要的产物。每次删除都会确认。NEVER use run_code (rm/del) to delete files — use this tool.",
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
            "description": "运行一条合法命令（编译、测试、安装依赖等）。长驻命令（如 npm run dev）会自动转入后台运行。NEVER use this to read files (cat/type/python -c/node -e) — use read. NEVER use this to write files (echo >/printf) — use write_file. NEVER run hex dumps or byte checks.",
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
            "name": "list",
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
            "name": "search",
            "description": "搜索项目代码。自动判断查询类型：含正则元字符（\\\\ ^ $ * + ? [ ] ( ) |）→正则匹配代码，不含→语义搜索文件。用于查找函数定义、类引用、或按概念找文件。",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "搜索词或正则表达式，如 '认证'、'def processPayment' 或 'class\\s+User\\b'"},
                    "path_filter": {"type": "string", "description": "可选：只搜索路径包含此字符串的文件，如 'service'"},
                },
                "required": ["query"]
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
    "You have tools: write_file, read, delete_file, rename_file, run_code, list, search, review_code.\n\n"
    "CRITICAL — READ BEFORE RESPONDING:\n"
    "When a user message contains tool_result blocks, those are the output of "
    "tools you just called. Your reply MUST be based on what those results ACTUALLY show.\n"
    "If list returned 'index.html, package.json, src/', then SAY you see those files.\n"
    "NEVER say 'the directory is empty' or contradict the tool output.\n\n"
    "WHEN run_code FAILS — FOLLOW THIS DIAGNOSTIC FLOW:\n"
    "  Step 1: Look at the stderr (it's in the tool result). DO NOT run diagnostic commands.\n"
    "  Step 2: If the error is about file content/syntax, use read to check the file.\n"
    "  Step 3: Fix the actual issue. Do NOT run hex dumps, byte checks, or test files.\n"
    "  Example: 'exit: 1\\nstdout:\\n\\nstderr:\\nReferenceError: x is not defined'\n"
    "    → This tells you exactly what's wrong. Read the relevant file, find 'x', fix it.\n"
    "  NEVER run node -e to check bytes/encoding. NEVER create test files.\n"
    "  NEVER inspect raw bytes. The error message tells you what's wrong.\n\n"
    "AFTER write_file — ALWAYS read to verify:\n"
    "  read(files=filename) after write_file to check the file was written correctly.\n"
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
    "  Action: call list(path='backend', max_depth=2) to explore a major subsystem.\n"
    "  Do NOT read any files yet. Just figure out what exists.\n\n"
    "PHASE 2 — FOCUS (based on user task + Phase 1 findings):\n"
    "  Goal: narrow down to relevant files. At most 3-5 calls.\n"
    "  Action: identify which directories/files are relevant to the user's task.\n"
    "  - Use list(path='specific/dir', max_depth=2) to inspect a subsystem.\n"
    "  - Use search(query) to find specific classes, functions, or concept files.\n"
    "  Only read files AFTER you've identified the right ones.\n\n"
    "PHASE 3 — DEEP DIVE (only after Phase 2):\n"
    "  Goal: read specific files, make changes, run code.\n"
    "  IMPORTANT: read ALL needed files in ONE read call with an array.\n"
    "  Do NOT read files one-by-one.\n"
    "  - read(files=['a.js','b.js','c.js']) reads 3 files in 1 call.\n"
    "  - Single read with offset/limit is ONLY for paginating large files.\n"
    "  - Use review_code after reading to check for issues.\n"
    "  - Only write files when you're sure about the changes.\n\n"
    "CRITICAL — NEVER skip phases. Do NOT read files during Phase 1.\n"
    "Do NOT call review_code during Phase 1 or 2. Only in Phase 3.\n"
    "ALWAYS use read with array for 2+ files. One-by-one read wastes budget.\n\n"
    "READ TIPS:\n"
    "- First read gives a truncated view (head+tail). Use this to get the structure.\n"
    "- For deep analysis, use offset/limit to read specific sections.\n"
    "- Example: read(files='User.java', offset=150, limit=300) reads lines 150-449.\n"
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
    "Use read instead. "
    "Do NOT run cat, type, node -e, python -c, or any other command just to read a file. "
    "read is designed for this purpose and works correctly across all platforms.\n"
    "CRITICAL — NEVER create temp/utility/bridge scripts in the project. "
    "Do NOT write .mjs, .sh, .bat, .ps1, or any other helper files. "
    "All fixes must be done directly with write_file/read. "
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
    ".venv", "venv", "env", ".env", "build", "dist", ".next", ".nuxt",
    ".turbo", "target", ".tox", ".eggs", "*.egg-info",
    ".patchflow", ".mypy_cache", ".pytest_cache",
    "vendor", "bundle", ".bundle",
    ".gradle", "gradle", "bower_components",
    "__generated__", "generated", "gen",
    "Pods", "Carthage", ".terraform", ".serverless", "cdk.out",
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
        │   └── src/main/resources/...
        ├── frontend/ (Vue.js)
        │   ├── src/...
        │   └── package.json
        └── README.md
    """
    root = Path(work_dir).resolve()
    if not root.is_dir():
        return "(project root not found)"

    lines = ["Project Skeleton:"]
    line_count = 0
    max_lines = 30

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
            if line_count >= max_lines:
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
                if line_count >= max_lines:
                    break

        elif e.is_file() and e.suffix in (".json", ".toml", ".yml", ".yaml", ".xml", ".gradle", ".properties"):
            lines.append(f"  📄 {e.name}")
            line_count += 1
            if line_count >= max_lines:
                break

    # 根部 README
    for readme in ("README.md", "README", "README.txt", "README.rst"):
        if (root / readme).exists():
            lines.append(f"  📄 {readme}")
            break

    if project_type_hints:
        lines.insert(1, f"  ({', '.join(project_type_hints)})")
        lines.insert(2, "")

    if line_count >= max_lines:
        lines.append("  ... (truncated, use list for details)")

    return "\n".join(lines)


def _detect_tech(dir_path: Path, sub_entries: list) -> str:
    """快速识别目录的技术栈 — 委托给 LanguageFactory"""
    strategy = LanguageFactory().detect(str(dir_path))
    if strategy is None:
        return ""
    lang_name = strategy.name.capitalize()
    fw_info = strategy.detect_framework(dir_path, [])
    if fw_info:
        return f"{lang_name}/{fw_info['name']}"
    return lang_name


# ═══════════════════════════════════════════════════════════
# 工具执行
# ═══════════════════════════════════════════════════════════

# 全对话工具调用预算 — 防止 LLM 过度调用导致上下文爆炸
_TOOL_BUDGET = {
    "review_code":     {"max": 8,  "count": 0},
    "read":            {"max": 40, "count": 0},
    "rename_file":     {"max": 10, "count": 0},
    "delete_file":     {"max": 5,  "count": 0},
    "run_code":        {"max": 15, "count": 0},
    "search":          {"max": 8,  "count": 0},
    "_total":          {"max": 70, "count": 0},
}

# 已读文件缓存 — 同一文件第二次读直接提示已读，避免 LLM 重复读
# 跨轮次持久化（仅在新对话开始时清空）
_READ_CACHE: set[str] = set()

# 运行命令缓存 — 相同命令返回缓存结果，避免 LLM 重复运行
_RUN_CACHE: dict[str, str] = {}

# 运行命令计数 — 检测重复模式（loop detection）
_RUN_COUNTER: dict[str, int] = {}

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


# ═══════════════════════════════════════════════════════════
# 模型能力分级 — 决定是否需要写/删确认
#
# 原则：不以 provider 划线，以模型实际能力为准。
#   - 已知强模型（在生产级 tool calling 上表现稳定）→ 直接执行
#   - 未知模型（新模型、小众模型）→ 写/删前需用户确认（安全默认）
#   - 运行时降级：强模型如果在本次会话中被 _check_command_abuse
#     拦截 ≥3 次，自动降级为需要确认模式
# ═══════════════════════════════════════════════════════════

# 已知强模型标识符（忽略大小写，子串匹配）
_STRONG_MODEL_PATTERNS = [
    # Anthropic
    "claude-4", "claude-opus-4", "claude-sonnet-4", "claude-3.5", "claude-3-5",
    # OpenAI
    "gpt-4", "gpt-4o", "gpt-4.1", "gpt-4-", "o1", "o3", "o4",
    # DeepSeek V3/R1/V4 系列（在 tool calling benchmark 中表现优秀）
    "deepseek-v3", "deepseek-v4", "deepseek-r1", "deepseek-chat",
    # Google
    "gemini-2", "gemini-pro-2",
    # 其他经过验证的强模型
    "qwq-32b", "qwen3",
]

# 运行时状态
_tool_abuse_count: int = 0
_ABUSE_DOWNGRADE_THRESHOLD = 3
_model_trusted: bool = True  # 默认信任，_resolve_model_trust 在 ChatClient.__init__ 中设置


def _resolve_model_trust(model_name: str) -> bool:
    """根据模型名判断是否属于已知强模型

    逻辑：
      1. 模型名命中 _STRONG_MODEL_PATTERNS → 信任
      2. 未命中 → 不信任（安全默认，新模型需要用户确认）

    这是一致性优于完备性的判断 —— 宁可让一个强模型多确认一次，
    也不让一个弱模型悄无声息地写坏文件。
    """
    lowered = model_name.lower()
    for pattern in _STRONG_MODEL_PATTERNS:
        if pattern in lowered:
            return True
    return False


def _on_tool_abuse() -> bool:
    """记录一次工具误用，返回是否触发降级"""
    global _tool_abuse_count, _model_trusted
    _tool_abuse_count += 1
    if _model_trusted and _tool_abuse_count >= _ABUSE_DOWNGRADE_THRESHOLD:
        _model_trusted = False
        return True
    return _model_trusted is False


def _requires_confirm() -> bool:
    """当前模型是否需要对写/删操作做用户确认"""
    return not _model_trusted


def _suggest_files(filename: str, max_suggestions: int = 5) -> str:
    """文件不存在时，搜索项目给出相似文件建议"""
    name = Path(filename).name
    name_lower = name.lower()
    suggestions: list[str] = []
    noise = {"__pycache__", ".git", "node_modules", ".patchflow", "build", "dist",
             "target", "venv", ".venv", "vendor", ".tox"}
    # 先搜当前目录下同名文件
    for match in Path(".").rglob(name):
        if len(suggestions) >= max_suggestions:
            break
        if len(str(match)) >= 200:
            continue
        if any(p in noise for p in match.parts):
            continue
        suggestions.append(str(match))
    # 再搜名字部分匹配的
    if len(suggestions) < max_suggestions:
        for match in Path(".").rglob(f"*{name}*"):
            if len(suggestions) >= max_suggestions:
                break
            if len(str(match)) >= 200:
                continue
            if any(p in noise for p in match.parts):
                continue
            lowered = match.name.lower()
            if name_lower in lowered or lowered in name_lower:
                path_str = str(match)
                if path_str not in suggestions:
                    suggestions.append(path_str)
    if suggestions:
        return "\nDid you mean:\n  " + "\n  ".join(suggestions[:max_suggestions])
    return ""


def _safe_path(filename: str) -> str | None:
    """校验路径安全性，防止路径穿越攻击。

    用 Path.resolve() 而非简单的 ".." 检测，可以防住：
      - 混用斜杠 (foo/..\\bar)
      - NTFS 流 (file:::$DATA)
      - 符号链接逃逸

    Returns:
        错误消息字符串（不安全时），或 None（安全时）
    """
    resolved = os.path.abspath(filename)
    cwd = os.getcwd()
    if not resolved.startswith(cwd + os.sep) and resolved != cwd:
        return f"ERROR: path traversal blocked: {filename}"
    return None


def _check_command_abuse(command: str) -> str | None:
    """检测模型是否在滥用 run_code 来替代专用工具

    每次拦截都会调用 _on_tool_abuse() 记录一次滥用。
    累计 ≥3 次后，即使是已知强模型也会被降级为需要确认模式。

    Returns:
        引导消息字符串（需要拦截），或 None（允许执行）
    """
    import re

    cmd = command.strip().lower()
    result = None

    # 读文件模式：cat / type / more / less / head / tail
    if re.match(r'^(cat|type)\s+', cmd):
        result = "BLOCKED: this looks like reading a file. Use read instead."
    elif re.match(r'^(more|less|head|tail)\s+', cmd):
        result = "BLOCKED: this looks like reading a file. Use read instead."
    # Python/Node 单行脚本 → 经常用来读文件或做字节检查
    elif re.match(r'^(python3?|python)\s+-c\s+', cmd):
        if any(kw in cmd for kw in ("pip", "pytest", "unittest", "setup.py")):
            return None
        result = (
            "BLOCKED: python -c is typically used to read or inspect file contents. "
            "Use read instead. If you need to run actual Python code, "
            "write it to a .py file with write_file and run that."
        )
    elif re.match(r'^node\s+-e\s+', cmd):
        result = (
            "BLOCKED: node -e is typically used to read or inspect file contents. "
            "Use read instead."
        )
    # 写入文件模式：echo/printf 重定向
    elif re.search(r'(echo|printf)\s+.*\s*>', cmd):
        result = "BLOCKED: this looks like writing a file via shell. Use write_file instead."
    # 删除文件模式：rm / del
    elif re.match(r'^(rm|del)\s+', cmd):
        result = "BLOCKED: this looks like deleting a file. Use delete_file instead."
    # 重命名模式：mv / ren
    elif re.match(r'^(mv|ren)\s+', cmd):
        result = "BLOCKED: this looks like moving/renaming a file. Use rename_file instead."
    # 二进制/编码诊断命令
    elif re.match(r'^(hexdump|xxd|od|file)\s+', cmd):
        result = (
            "BLOCKED: byte-level inspection is unnecessary. "
            "Read the error message in stderr — it tells you what's wrong."
        )

    if result:
        downgraded = _on_tool_abuse()
        logger.warn(f"[ToolAbuse] #{_tool_abuse_count} 滥用: {command[:120]}")
        if downgraded:
            logger.warn("[ToolAbuse] 滥用 ≥3 次，模型降级为需确认模式")

    return result


def _execute_tool(name: str, args: dict,
                  on_run_output: Callable[[str], None] | None = None) -> str:
    from patchflow.utils.runner import (
        add_to_blacklist,
        add_to_whitelist,
        classify_command,
        is_long_running,
        run_live,
        start_background,
    )

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
        if raw.is_absolute() or _safe_path(filename):
            filename = raw.name

        # 不信任模型 → 写文件前确认
        if _requires_confirm() and _confirm_run_callback:
            decision = _confirm_run_callback(
                f"write {filename}", f"模型请求写入 {filename} ({len(content)} chars)"
            )
            if decision == "reject":
                return f"USER_REJECTED: 用户拒绝写入 {filename}"

        p = Path(filename)
        p.parent.mkdir(parents=True, exist_ok=True)

        # 记录原始内容用于 diff
        original = p.read_text(encoding="utf-8") if p.exists() else ""

        # AI 标记：如果文件被修改或新建，添加注释头
        strategy = LanguageFactory().detect_by_extension(p.suffix.lower())
        if strategy is not None:
            comment_char = strategy.comment_syntax
            # 只在文件头部没有 AI marker 时添加
            first_line = content.split("\n")[0].strip() if content else ""
            if comment_char not in first_line or "ai" not in first_line.lower():
                ai_marker = f"{comment_char} AI-generated (PatchFlow)\n"
                content = ai_marker + content

        p.write_text(content, encoding="utf-8")

        # 显示 diff
        if original and original != content:
            diff = diff_text(original, content, context_lines=2)
            summary = format_summary(diff)
            diff_lines = diff.split("\n")
            if len(diff_lines) > 60:
                diff = "\n".join(diff_lines[:60]) + f"\n... ({len(diff_lines) - 60} more lines)"
            logger.info(f"write_file: {filename} ({len(content)} chars, {summary})\n{diff}")
        elif not original:
            logger.info(f"write_file: {filename} ({len(content)} chars, new file)")
        else:
            logger.info(f"write_file: {filename} ({len(content)} chars, no changes)")

        return f"OK: wrote {len(content)} chars to {filename}"

    elif name == "delete_file":
        filename = args.get("filename", "")
        if not filename:
            return "ERROR: delete_file — no filename provided"
        fn = str(Path(filename.strip()).as_posix())
        p = Path(fn)

        # 不信任模型 → 删文件前确认
        if _requires_confirm() and _confirm_run_callback:
            decision = _confirm_run_callback(
                f"delete {fn}", f"模型请求删除 {fn}"
            )
            if decision == "reject":
                return f"USER_REJECTED: 用户拒绝删除 {fn}"

        if not p.exists():
            return f"ERROR: file not found: {fn}"
        if not p.is_file():
            return f"ERROR: not a file: {fn}"
        traversal = _safe_path(fn)
        if traversal:
            return traversal
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
        traversal = _safe_path(source) or _safe_path(dest)
        if traversal:
            return traversal
        dst.parent.mkdir(parents=True, exist_ok=True)
        src.rename(dst)
        logger.info(f"rename_file: {source} -> {dest}")
        return f"OK: moved {source} -> {dest}"

    elif name == "read":
        raw_files = args.get("files", "")
        if not raw_files:
            return "ERROR: read — no files provided"
        # 标准化：字符串→[字符串]，数组→保持
        files: list[str] = [raw_files] if isinstance(raw_files, str) else list(raw_files)
        if not files:
            return "ERROR: read — files must be a non-empty string or list"
        offset = int(args.get("offset", 0))
        limit = int(args.get("limit", 0))

        # 单文件 + offset/limit → 旧 read_file 精读路径
        if len(files) == 1 and (offset > 0 or limit > 0):
            filename = str(Path(files[0].strip()).as_posix())
            traversal = _safe_path(filename)
            if traversal:
                return traversal
            p = Path(filename)
            if not p.exists():
                hint = _suggest_files(filename)
                return f"ERROR: file not found: {filename}{hint}"
            if not p.is_file():
                return f"ERROR: not a file: {filename}"
            content = p.read_text(encoding="utf-8")
            logger.info(f"read (paginated): {filename} ({len(content)} chars)")
            _READ_CACHE.add(filename)
            lines = content.split("\n")
            total_lines = len(lines)
            if offset < 0:
                offset = 0
            if offset >= total_lines:
                return f"(file is {total_lines} lines, offset {offset} is out of range)"
            if limit > 0:
                end = min(offset + limit, total_lines)
                sliced = "\n".join(lines[offset:end])
                header = f"[lines {offset}-{end-1} of {total_lines}]\n" if offset > 0 or end < total_lines else ""
                return f"{header}{sliced}"
            # limit==0 但 offset>0 → 从 offset 到尾
            return "\n".join(lines[offset:])

        # 批量读取（无 offset/limit，或单文件无分页）
        parts = []
        for f in files:
            normal = str(Path(f.strip()).as_posix())
            if normal in _READ_CACHE:
                parts.append(f"# === {f} ===\n(already read — in context)")
                continue
            fp = Path(normal)
            if not fp.exists():
                hint = _suggest_files(normal)
                parts.append(f"# === {f} ===\n(not found{hint})")
                continue
            if not fp.is_file():
                parts.append(f"# === {f} ===\n(not a file — is a directory)")
                continue
            content = fp.read_text(encoding="utf-8")
            _READ_CACHE.add(normal)
            logger.info(f"read: {normal} ({len(content)} chars)")
            # 截断大文件
            if len(content) > 5000:
                lines = content.split("\n")
                total_lines = len(lines)
                head_lines, tail_lines = 150, 50
                if total_lines > head_lines + tail_lines:
                    head = "\n".join(lines[:head_lines])
                    tail = "\n".join(lines[-tail_lines:])
                    omitted = total_lines - head_lines - tail_lines
                    content = (
                        f"[lines 0-{head_lines-1} of {total_lines}]\n{head}\n\n"
                        f"# ... [truncated {omitted} lines — use read(files='{normal}', offset={head_lines},limit=N) to continue] ...\n\n"
                        f"[lines {total_lines-tail_lines}-{total_lines-1} of {total_lines}]\n{tail}"
                    )
            parts.append(f"# === {f} ===\n{content}")
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

        # ── 工具误用检测：模型用 run_code 读文件 → 拦截并引导 ──
        redirect = _check_command_abuse(command)
        if redirect:
            logger.warn(f"run_code ABUSE DETECTED: {command[:120]} → {redirect}")
            return redirect

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
                    return "BLOCKED: 已将该命令加入黑名单，后续自动拦截"
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

        # ── 命令缓存：相同命令返回缓存结果 ──
        cmd_key = command.strip()
        if cmd_key in _RUN_CACHE:
            logger.info(f"run_code (cached): {command[:80]}")
            return _RUN_CACHE[cmd_key]

        # ── 循环检测：同一命令模式重复 3+ 次 → 警告 LLM ──
        cmd_pattern = re.sub(r'[\d"]+', '', cmd_key)[:80]
        count = _RUN_COUNTER.get(cmd_pattern, 0) + 1
        _RUN_COUNTER[cmd_pattern] = count
        if count >= 3:
            logger.warn(f"[LoopDetect] 命令模式已重复 {count} 次: {cmd_pattern}...")

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
            out = f"exit: 0\nstdout:\n{output_text}{truncated}"
        else:
            out = f"exit: {result.exit_code}\nstdout:\n{output_text[:1500]}{truncated}\nstderr:\n{result.stderr[:1500]}"
        _RUN_CACHE[cmd_key] = out
        return out

    elif name == "list":
        dirpath = args.get("path", ".") or "."
        max_depth = args.get("max_depth", 4)
        p = Path(dirpath)
        if not p.exists():
            return f"ERROR: path not found: {dirpath}"

        ignore_dirs = {
            ".git", "node_modules", "__pycache__", ".idea", ".vscode",
            ".venv", "venv", ".env", "build", "dist", ".next", ".nuxt",
            ".turbo", "target", ".tox", ".eggs", "*.egg-info",
        }

        def _should_ignore(name: str, is_dir: bool) -> bool:
            if name.startswith(".") and name not in (".env", ".gitignore", ".gitattributes"):
                return True
            if is_dir and name in ignore_dirs:
                return True
            return False

        tree_lines = []
        dir_limit = 5
        file_head_var = 3
        file_tail_var = 2
        max_tree_lines = 25
        line_count = [0]

        def _add(line: str):
            tree_lines.append(line)
            line_count[0] += 1

        def _walk(d: Path, prefix: str, depth: int):
            if depth > max_depth or line_count[0] >= max_tree_lines:
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

            items = list(dirs[:dir_limit])
            omitted_dirs = len(dirs) - dir_limit if len(dirs) > dir_limit else 0

            if len(files) <= file_head_var + file_tail_var:
                items.extend(files)
                omitted_files = 0
            else:
                items.extend(files[:file_head_var])
                omitted_files = len(files) - file_head_var - file_tail_var
                items.append("__OMIT__")
                items.extend(files[-file_tail_var:])

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
        if line_count[0] >= max_tree_lines:
            tree_lines.append(f"  ... ({line_count[0] - 1}+ items, showing first {max_tree_lines})")
        return "\n".join(tree_lines)

    elif name == "search":
        query = args.get("query", "")
        if not query:
            return "ERROR: search — no query provided"
        path_filter = args.get("path_filter", "")
        idx = _get_index(".")

        # 自动判断：含正则元字符 → 代码搜索，否则 → 语义搜索
        _regex_meta = re.compile(r'[\\\[\](){}.*+?^$|]')
        if _regex_meta.search(query):
            return idx.search_code(query, path_filter=path_filter)

        results = idx.search_files(query, top_k=10)
        if not results:
            return "(未找到相关文件，请尝试用其他关键词，或先用 list 了解项目结构)"
        lines = []
        for i, r in enumerate(results):
            lines.append(f"{i + 1}. {r['summary']}")
        return "\n".join(lines)

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

    def __init__(self, model: str | None = None, work_dir: str = ".", memory_enabled: bool = True,
                 thinking_budget: int = 0):
        cfg = get_config()
        self.provider = get_normalized_provider()
        self.api_key = cfg["api_key"]
        self.api_base = cfg["api_base"]
        self.model = model or cfg["model"]

        if not self.api_key:
            raise ValueError("未配置 API Key")

        # 模型能力分级：已知强模型可信，未知模型写/删前需确认
        global _model_trusted, _tool_abuse_count
        _model_trusted = _resolve_model_trust(self.model)
        _tool_abuse_count = 0
        if not _model_trusted:
            logger.info(f"模型 {self.model} 不在已知强模型列表中，写/删操作需确认")

        if self.provider == "anthropic":
            base_url = self.api_base or None
            if base_url:
                base_url = base_url.rstrip("/")
                if base_url.endswith("/v1/messages"):
                    base_url = base_url[:-len("/v1/messages")]
                elif base_url.endswith("/v1"):
                    base_url = base_url[:-len("/v1")]
            self._anthropic = Anthropic(api_key=self.api_key, timeout=120, base_url=base_url)
            self._openai = None
        else:
            base_url = self.api_base or None
            self._openai = OpenAI(
                api_key=self.api_key,
                base_url=base_url,
                timeout=120,
                max_retries=2,
            )
            self._anthropic = None

        self.messages: list[dict] = []
        self._memory_enabled = memory_enabled
        self._has_dev_activity = False
        self._max_rounds = 30
        self._work_dir = Path(work_dir).resolve()
        self._memory_path = self._work_dir / ".patchflow" / "memory.json"
        self._session_boundary_added = False
        self._memory_summary: list[str] = []
        self._thinking_budget = thinking_budget  # 0 = 禁用，>0 = 启用扩展思考
        if self._memory_enabled:
            self._load_memory()
            self._compress_old_messages()  # 加载后立即压缩，避免旧消息撑爆上下文

    # ── streaming chat（给 REPL 用）──

    def chat_stream(self, user_input: str,
                     on_run_output: Callable[[str], None] | None = None):
        # ── 首次对话：自动注入项目骨架图 + 项目规则 + 重置读缓存 ──
        if not self.messages:
            _READ_CACHE.clear()
            _RUN_CACHE.clear()
            _RUN_COUNTER.clear()
            t0 = time.time()
            skeleton = _get_project_skeleton(".")
            logger.info(f"[perf] skeleton scan: {time.time() - t0:.2f}s")

            rules_text = ""
            rules_file = Path(".patchflow/rules.md")
            if rules_file.exists():
                try:
                    rules_text = rules_file.read_text(encoding="utf-8").strip()
                except Exception as e:
                    logger.debug(f"读取规则文件失败: {e}")
            if rules_text:
                enhanced_input = f"{skeleton}\n\nProject Rules:\n{rules_text}\n\n{user_input}"
            else:
                enhanced_input = f"{skeleton}\n\n{user_input}"
            self.messages.append({"role": "user", "content": enhanced_input})
            t0 = time.time()
            self._save_memory()
            logger.info(f"[perf] save_memory: {time.time() - t0:.2f}s")
        else:
            bg_info = ""
            try:
                from patchflow.utils.runner import list_processes
                running = [p for p in list_processes() if p.running]
                if running:
                    bg_info = "\n[Background processes running: " + ", ".join(f"PID {p.pid}: {p.command[:40]}" for p in running) + "]\n"
            except Exception as e:
                logger.debug(f"后台进程列表获取失败: {e}")
            self.messages.append({"role": "user", "content": bg_info + user_input})
            self._save_memory()
        all_tool_calls: list = []
        session_usage = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0, "calls": 0}
        _reset_tool_budget()

        for _ in range(self._max_rounds):
            recent = compress(self.messages)
            t0 = time.time()
            input_tokens_est = sum(len(m.get("content", "")) for m in recent if isinstance(m.get("content"), str))

            if self._openai:
                text, tcs, usage = self._call_openai_stream(recent)
                thinking_text = ""
            else:
                text, tcs, usage, thinking_text = self._call_anthropic(recent)
            logger.info(f"[perf] LLM call: {time.time() - t0:.2f}s, "
                         f"messages={len(recent)}, input_est={input_tokens_est // 1000}K chars")

            session_usage["input_tokens"] += usage.get("input_tokens", 0)
            session_usage["output_tokens"] += usage.get("output_tokens", 0)
            session_usage["total_tokens"] += usage.get("total_tokens", 0)
            session_usage["calls"] += 1

            if thinking_text:
                yield ("thinking", thinking_text)

            if text:
                yield ("text", text)

            if not tcs:
                self._append_assistant(text, tcs)
                self._save_memory()
                yield "usage", dict(session_usage)
                yield "done", all_tool_calls
                return

            yield "usage", dict(session_usage)

            # 执行工具
            for tc in tcs:
                fn_info = {
                    "name": tc["function"]["name"],
                    "args": _safe_json_parse(tc["function"]["arguments"]),
                }
                yield "tool_start", fn_info

                if fn_info["name"] == "run_code" and on_run_output:
                    yield "run_output", f"$ {fn_info['args'].get('command', '')}"

                result = _execute_tool(
                    fn_info["name"], fn_info["args"],
                    on_run_output=on_run_output,
                )
                fn_info["result"] = result
                fn_info["id"] = tc["id"]
                all_tool_calls.append(fn_info)
                yield "tool_result", fn_info

                if fn_info["name"] == "review_code":
                    first_line = result.split("\n")[0]
                    tc["result"] = first_line[:200]
                else:
                    tc["result"] = result

            self._append_assistant(text, tcs)
            self._save_memory()

        yield "usage", dict(session_usage)
        yield "hint", "round_limit"
        yield "done", all_tool_calls

    def _append_assistant(self, text, tcs):
        """把 assistant 回复 + 工具结果追加到消息历史"""
        max_result_chars = 1000

        def _truncate(content: str) -> str:
            if len(content) <= max_result_chars:
                return content
            head = content[:500]
            tail = content[-250:]
            return f"{head}\n\n... [truncated {len(content) - max_result_chars} chars, full result in REPL] ...\n\n{tail}"

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
        """Anthropic 原生工具调用，支持扩展思考"""
        kwargs: dict = {
            "model": self.model,
            "max_tokens": 2048,
            "system": SYSTEM_PROMPT,
            "messages": messages,
            "tools": _get_anthropic_tools(),
        }
        if self._thinking_budget > 0 and self.provider == "anthropic":
            kwargs["thinking"] = {"type": "enabled", "budget_tokens": self._thinking_budget}

        try:
            response = self._anthropic.messages.create(**kwargs)
        except Exception as e:
            error_msg = f"[API 请求失败: {e}]"
            logger.error(f"Anthropic API 调用异常: {e}")
            return error_msg, [], {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}, ""

        text_parts = []
        thinking_parts = []
        tcs = []

        for block in response.content:
            if block.type == "text":
                text_parts.append(block.text)
            elif block.type == "thinking":
                thinking_parts.append(block.thinking)
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

        thinking_text = "\n".join(thinking_parts).strip()
        return "\n".join(text_parts).strip(), tcs, usage, thinking_text

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
            stream_deadline = time.time() + 180
            stream_client = OpenAI(
                api_key=self.api_key,
                base_url=self.api_base,
                timeout=120,
                max_retries=0,
            )
            response = stream_client.chat.completions.create(
                model=self.model,
                messages=api_messages,
                tools=TOOLS,
                tool_choice="auto",
                max_tokens=4096,
                stream=True,
                stream_options={"include_usage": True},
            )

            time.time()
            for chunk in response:
                if time.time() - stream_deadline > 0:
                    logger.warn("OpenAI streaming total time > 180s, discarding partial tool calls")
                    tool_calls_acc.clear()
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
            return error_msg, [], usage

        final_text = "".join(text_parts).strip()
        final_tcs = list(tool_calls_acc.values()) if tool_calls_acc else []

        return final_text, final_tcs, usage

    # ── 非流式 chat（给 build 命令用）──

    def chat(self, user_input: str) -> tuple[str, list[ToolUse]]:
        all_tool_calls: list = []
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
        self._memory_summary = []
        self._has_dev_activity = False
        # 直接清空记忆文件，不经过 _save_memory（它会因为"非开发任务"跳过写盘）
        if self._memory_enabled and self._memory_path.exists():
            try:
                self._memory_path.write_text(json.dumps({"summary": [], "messages": []}, ensure_ascii=False), encoding="utf-8")
            except OSError:
                pass
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

    _MAX_MEMORY_BYTES = 80_000    # 80 KB，总内存上限
    _TOOL_COMPRESS_BYTES = 50_000  # 50 KB，超此值先压缩工具结果
    _HEAVY_COMPRESS_BYTES = 70_000  # 70 KB，超此值压缩旧 assistant 回复
    _MIN_KEEP_TURNS = 2           # 压缩时最少保留最近 2 个完整对话回合
    _MAX_L1_SUMMARIES = 20        # L1 摘要超过此数触发 LLM 蒸馏
    _MAX_L2_SUMMARIES = 10        # L2 摘要超过此数触发 LLM 蒸馏 → L3

    def _check_dev_activity(self) -> bool:
        """扫描当前对话，判断是否有开发活动

        不猜测用户意图，而是观察 LLM 的实际行为：
          - LLM 调用了写/运行/删除/重命名工具 → 明确是开发任务
          - 用户消息中包含代码块（```）→ 代码相关
        一旦检测到，标记 _has_dev_activity = True（后续不再重新扫描）
        """
        if self._has_dev_activity:
            return True
        for msg in self.messages:
            if msg.get("tool_calls"):
                for tc in msg["tool_calls"]:
                    name = tc.get("function", {}).get("name", "")
                    if name in ("write_file", "delete_file", "rename_file", "run_code"):
                        self._has_dev_activity = True
                        return True
            if not self._has_dev_activity and "```" in str(msg.get("content", "")):
                self._has_dev_activity = True
                return True
        return False

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

    def _strip_tool_results(self, messages: list[dict]) -> list[dict]:
        """将消息列表中的冗余工具结果替换为紧凑元信息

        不修改原始消息，返回新的消息列表（用于保存到磁盘）。
        原始 messages 列表保持不变（LLM 需要完整上下文）。
        """
        stripped = []
        for msg in messages:
            role = msg.get("role", "")
            content = msg.get("content", "")

            # 跳过冗余的 system prompt（每轮工具调用后自动插入的提示）
            if role == "user" and isinstance(content, str):
                if content.startswith("(system: you just called"):
                    continue
                if content.startswith("(session resuming"):
                    stripped.append(msg)
                    continue

            # 压缩工具结果
            if role in ("tool", "user") and isinstance(content, (str, list)):
                compressed = self._compress_tool_content(content)
                if compressed is not None:
                    new_msg = dict(msg)
                    new_msg["content"] = compressed
                    stripped.append(new_msg)
                    continue

            stripped.append(msg)
        return stripped

    def _compress_tool_content(self, content) -> str | list | None:
        """压缩单个工具结果内容，返回 None 表示不需要压缩"""
        if isinstance(content, str):
            result = self._summarize_tool_string(content)
            return result if result != content else None
        if isinstance(content, list):
            changed = False
            new_blocks = []
            for block in content:
                if isinstance(block, dict):
                    text = block.get("text", "")
                    if isinstance(text, str) and len(text) > 400:
                        summary = self._summarize_tool_string(text)
                        new_blocks.append({**block, "text": summary})
                        changed = True
                    else:
                        new_blocks.append(block)
                else:
                    new_blocks.append(block)
            return new_blocks if changed else None
        return None

    @staticmethod
    def _summarize_tool_string(text: str) -> str:
        """将工具结果字符串替换为紧凑摘要"""
        if not text or len(text) <= 400:
            return text
        first_line = text.split("\n")[0].strip()

        # read 结果
        if first_line.startswith("[lines "):
            n_lines = text.count("\n")
            return f"[read: {first_line.split(']')[0]}] — {n_lines} lines]"
        if text.startswith("[toolu_bdrk_") or text.startswith("[tool output:"):
            return text[:200]

        # write_file 结果
        if text.startswith("OK: wrote"):
            return text.split("\n")[0]

        # run_code 结果
        if text.startswith("exit:"):
            exit_line = text.split("\n")[0]
            stderr_idx = text.find("stderr:")
            if stderr_idx > 0:
                stderr_preview = text[stderr_idx:stderr_idx + 200].replace("\n", " ")
                return f"{exit_line} | {stderr_preview}"
            return exit_line

        # list 结果
        if "├──" in text or "└──" in text:
            n_items = sum(1 for line in text.split("\n") if line.strip().startswith(("├", "└")))
            return f"[listed: {n_items} items]"

        # search 结果
        if "matches" in first_line.lower() or "files" in first_line.lower():
            return first_line[:200]

        # review_code 结果
        if first_line.endswith("— 发现") or "未发现明显问题" in text:
            return text[:300]

        # 兜底：截断
        if len(text) > 400:
            return text[:200] + f"\n... [{len(text) - 400} chars trimmed]"
        return text

    def _distill_summaries(self):
        """LLM 驱动的摘要蒸馏：L1 → L2 → L3

        L1 摘要 > _MAX_L1_SUMMARIES → 蒸馏最早 10 条为 L2 叙述
        L2 摘要 > _MAX_L2_SUMMARIES → 蒸馏最早 5 条为 L3 知识条目
        """
        if not self._memory_summary:
            return

        from patchflow.core.llm_client import call_llm

        # L1 → L2: 单轮摘要合并为会话级叙述
        if len(self._memory_summary) > self._MAX_L1_SUMMARIES:
            batch = self._memory_summary[:10]
            prompt = (
                "将以下 PatchFlow 对话摘要列表提炼为一段简洁的叙述（≤300 字符）。\n"
                "保留：项目名、技术栈、关键决策、修复过的 bug 类型、创建/修改的文件。\n"
                "丢弃：日常问候、重复内容、非技术闲聊。\n\n"
                + "\n".join(f"- {s}" for s in batch)
                + "\n\nOutput ONLY the distilled summary text, no JSON, no markdown."
            )
            try:
                result = call_llm(
                    system_prompt="You are a knowledge distiller. Output ONLY the distilled summary text.",
                    user_message=prompt,
                    max_tokens=256,
                )
                if result and isinstance(result, dict):
                    distilled = result.get("content", "") or str(result)
                elif result and isinstance(result, str):
                    distilled = result
                else:
                    distilled = "; ".join(batch)[:300]
                # 存入 L2（用特殊前缀标记）
                l2_entry = f"[L2] {distilled[:300]}"
                self._memory_summary = self._memory_summary[10:]
                self._memory_summary.insert(0, l2_entry)
                logger.info(f"记忆蒸馏 L1→L2: {len(batch)} 条 → 1 条叙述 ({len(l2_entry)} 字符)")
            except Exception as e:
                logger.warn(f"记忆蒸馏 L1→L2 失败: {e}")

        # L2 → L3: 会话摘要合并为知识条目
        l2_entries = [s for s in self._memory_summary if s.startswith("[L2]")]
        if len(l2_entries) > self._MAX_L2_SUMMARIES:
            batch = l2_entries[:5]
            prompt = (
                "将以下 PatchFlow 会话摘要提炼为结构化知识条目（≤200 字符）。\n"
                "只保留可复用的知识：项目结构决策、修复模式、工具链配置。\n\n"
                + "\n".join(f"- {s}" for s in batch)
                + "\n\nOutput ONLY the distilled knowledge entry, no JSON."
            )
            try:
                result = call_llm(
                    system_prompt="You are a knowledge distiller. Output ONLY the knowledge entry text.",
                    user_message=prompt,
                    max_tokens=200,
                )
                if result and isinstance(result, dict):
                    distilled = result.get("content", "") or str(result)
                elif result and isinstance(result, str):
                    distilled = result
                else:
                    distilled = "; ".join(batch)[:200]
                l3_entry = f"[L3] {distilled[:200]}"
                # 移除被蒸馏的 L2 条目，插入 L3
                self._memory_summary = [s for s in self._memory_summary if s not in batch]
                self._memory_summary.insert(0, l3_entry)
                logger.info(f"记忆蒸馏 L2→L3: {len(batch)} 条 → 1 条知识 ({len(l3_entry)} 字符)")
            except Exception as e:
                logger.warn(f"记忆蒸馏 L2→L3 失败: {e}")

    def _save_memory(self):
        """将当前对话持久化到磁盘（仅当检测到开发活动时）

        保存前：
          1. 剪枝工具结果（替换为紧凑元信息）
          2. 压缩旧消息为摘要
          3. LLM 蒸馏摘要（L1→L2→L3）
        如果对话不涉及开发任务（纯查询/闲聊），跳过磁盘写入。
        """
        if not self._memory_enabled:
            return
        if not self._check_dev_activity():
            logger.debug(f"{_log_tag} 非开发任务，跳过持久化")
            self._compress_old_messages()
            return

        try:
            self._compress_old_messages()
            self._distill_summaries()
            # 剪枝工具结果后保存（不修改内存中的 messages，LLM 需要完整上下文）
            stripped = self._strip_tool_results(self.messages)
            self._memory_path.parent.mkdir(parents=True, exist_ok=True)
            data = {
                "summary": self._memory_summary,
                "messages": stripped,
                "_saved_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            }
            raw = json.dumps(data, ensure_ascii=False, indent=2)
            self._memory_path.write_text(raw, encoding="utf-8")
            logger.info(f"{_log_tag} 已持久化 ({len(self._memory_summary)} 摘要 + {len(stripped)} 消息, "
                        f"{len(raw.encode('utf-8')) // 1024}KB)")
        except OSError as e:
            logger.warn(f"记忆保存失败: {e}")

    def _compress_old_messages(self):
        """将消息列表中的旧消息压缩为摘要

        策略：从最早的消息开始，一旦 total > MAX_MEMORY_BYTES 或消息数 > 500，
        就把最早的一整轮对话提取关键信息，追加到 _memory_summary，然后丢弃原始消息。

        始终保留至少 _MIN_KEEP_TURNS 轮完整的原始消息。
        如果消息数仍超 500，降到 1 轮。
        """
        if not self.messages:
            return

        _max_msg_count = 500

        test = json.dumps({"messages": self.messages, "summary": self._memory_summary},
                          ensure_ascii=False)
        size_ok = len(test.encode("utf-8")) <= self._MAX_MEMORY_BYTES
        count_ok = len(self.messages) <= _max_msg_count
        if size_ok and count_ok:
            return

        # 找到所有非摘要 user 消息的索引
        user_indices = [i for i, m in enumerate(self.messages)
                        if m.get("role") == "user" and not m.get("_memory_summary")]

        # 消息数过多 → 只保留 1 轮
        if len(self.messages) > _max_msg_count:
            keep_turns = 1
        else:
            keep_turns = self._MIN_KEEP_TURNS

        keep_count = min(keep_turns, len(user_indices))
        if keep_count <= 0:
            return
        earliest_keep = user_indices[-keep_count]

        # 从最早的消息开始压缩，直到大小和数量都达标
        while earliest_keep > 0:
            candidate = self.messages[earliest_keep:]
            test = json.dumps({"messages": candidate, "summary": self._memory_summary},
                              ensure_ascii=False)
            size_ok = len(test.encode("utf-8")) <= self._MAX_MEMORY_BYTES
            count_ok = len(candidate) <= _max_msg_count
            if size_ok and count_ok:
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
            keep_count = min(keep_turns, len(user_indices))
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
        current: list[dict] = []
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

    @property
    def max_memory_bytes(self):
        return self._MAX_MEMORY_BYTES

    @property
    def memory_summary(self):
        return self._memory_summary
