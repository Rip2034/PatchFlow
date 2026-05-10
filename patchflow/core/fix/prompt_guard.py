"""Prompt 注入防御 — 检测和过滤用户输入中的恶意 prompt

三种攻击面：
  1. 用户任务描述 — 直接的 prompt 注入
  2. 代码内容 — 代码注释中的隐藏指令
  3. 文件路径 — 文件名中的注入 payload

防御层次：
  Level 1: 检测 → 标记可疑内容（warn + 继续）
  Level 2: 清洗 → 转义危险模式（中性化后继续）
  Level 3: 阻断 → 拒绝明显恶意内容（hard block）

设计原则：
  - 宁可漏报（false negative）也不能误杀正常代码
  - 代码中的 "system" 等词是合法的，只在明确注入模式时才告警
  - 所有用户内容必须包裹在明确的分隔符内
"""

import re
from pathlib import Path

from patchflow.utils import logger

# ═══════════════════════════════════════════════════════════
# Level 1: 明确注入模式（高置信度 — 直接阻断）
# ═══════════════════════════════════════════════════════════

_HARD_BLOCK_PATTERNS = [
    # 直接指令覆盖
    r'(?i)ignore\s+(all\s+)?(previous|above|prior)\s+(instructions?|prompts?|messages?)',
    r'(?i)disregard\s+(all\s+)?(previous|above|prior)\s+(instructions?|prompts?)',
    r'(?i)override\s+(all\s+)?(system|instructions?|prompts?)',

    # 角色劫持
    r'(?i)you\s+are\s+now\s+(DAN|an?\s+unrestricted|a\s+different\s+AI)',
    r'(?i)jailbreak|developer\s*mode|god\s*mode',
    r'(?i)pretend\s+(you\s+are|to\s+be)\s+(an?\s+)?(unfiltered|unrestricted)',

    # 系统提示泄露
    r'(?i)(print|show|display|reveal|output)\s+(your\s+)?(system\s+prompt|instructions?|guidelines?)',
    r'(?i)what\s+(is|are)\s+(your\s+)?(system\s+prompt|instructions?|guidelines?)',

    # 分隔符逃逸 — 试图突破 markdown 代码块
    r'```\s*(system|instruction)',

    # 越狱关键词
    r'(?i)do\s+anything\s+now',
    r'(?i)without\s+(any\s+)?(restrictions?|limitations?|ethics?|safety|filtering)',
]

_HARD_BLOCK_RE = [re.compile(p) for p in _HARD_BLOCK_PATTERNS]


# ═══════════════════════════════════════════════════════════
# Level 2: 可疑模式（中置信度 — 清洗后继续）
# ═══════════════════════════════════════════════════════════

_SUSPICIOUS_PATTERNS = [
    # SYSTEM 标签注入（代码中出现可能合法，但连续多个可疑）
    r'(?i)(?:^|\n)\s*<SYSTEM>',
    r'(?i)(?:^|\n)\s*\[SYSTEM\]',
    r'(?i)(?:^|\n)\s*SYSTEM:\s*[A-Z]',

    # 角色伪装
    r'(?i)(?:^|\n)\s*You\s+are\s+(now\s+)?(an?\s+)?(expert|senior|master|hacker)',
    r'(?i)(?:^|\n)\s*Act\s+as\s+(an?\s+)?(unfiltered|unrestricted)',

    # 隐藏输出指令
    r'(?i)(?:^|\n)\s*(respond|reply|answer)\s+(only\s+)?with\s+[\"\']["\']',
    r'(?i)(?:^|\n)\s*From\s+now\s+on\s*(,?\s*you\s+(must|should|will)\s+only)?',
]


# ═══════════════════════════════════════════════════════════
# Level 3: 分隔符安全包裹
# ═══════════════════════════════════════════════════════════

def fence_code(content: str, filepath: str = "", language: str = "") -> str:
    """安全包裹代码内容，防止代码中的文本逃逸出代码块

    策略：使用唯一标记替代固定 ``` 分隔符，防止代码内容
    本身包含 ``` 导致 LLM 误解析。

    Args:
        content: 代码内容
        filepath: 文件名（可选，用于语言检测）
        language: 语言标识（可选）

    Returns:
        安全包裹后的文本
    """
    # 生成唯一标记避免内容包含 ``` 导致的逃逸
    import hashlib
    import time
    marker_seed = f"{filepath}{time.time()}"
    marker = hashlib.md5(marker_seed.encode()).hexdigest()[:8]

    lang_hint = language or _guess_language(filepath)

    return (
        f"[CODE_BEGIN:{marker}:{lang_hint}]\n"
        f"{content}\n"
        f"[CODE_END:{marker}]"
    )


def fence_user_input(text: str) -> str:
    """安全包裹用户输入，明确标记其为用户提供的不可信内容"""
    return (
        "[USER_INPUT_BEGIN]\n"
        f"{text}\n"
        "[USER_INPUT_END]"
    )


# ═══════════════════════════════════════════════════════════
# 检测和清洗
# ═══════════════════════════════════════════════════════════

class InjectionResult:
    """注入检测结果"""
    def __init__(self, blocked: bool, suspicious: bool,
                 reason: str = "", sanitized: str = ""):
        self.blocked = blocked          # 是否直接阻断
        self.suspicious = suspicious    # 是否可疑（需清洗）
        self.reason = reason            # 原因描述
        self.sanitized = sanitized      # 清洗后的文本（如果可疑）


def scan(text: str, source: str = "user_input") -> InjectionResult:
    """扫描文本中的 prompt 注入

    Args:
        text: 要扫描的文本
        source: 来源标识 ("user_input", "code", "filepath")

    Returns:
        InjectionResult
    """
    if not text or not text.strip():
        return InjectionResult(blocked=False, suspicious=False)

    # Level 1: 明确注入
    for i, pattern in enumerate(_HARD_BLOCK_RE):
        if pattern.search(text):
            reason = f"检测到明确注入模式 [{source}]: {_HARD_BLOCK_PATTERNS[i]}"
            logger.error(f"[PromptGuard] {reason}")
            return InjectionResult(blocked=True, suspicious=True, reason=reason)

    # Level 2: 可疑模式（仅对 user_input 源严格检查）
    if source == "user_input":
        suspicious_count = 0
        for sp in _SUSPICIOUS_PATTERNS:
            if re.search(sp, text):
                suspicious_count += 1

        if suspicious_count >= 2:
            reason = f"检测到 {suspicious_count} 个可疑注入模式 [{source}]"
            logger.warn(f"[PromptGuard] {reason}")
            return InjectionResult(
                blocked=False, suspicious=True, reason=reason,
                sanitized=_sanitize(text),
            )

    return InjectionResult(blocked=False, suspicious=False)


def scan_code(content: str, filepath: str = "") -> InjectionResult:
    """扫描代码内容（比用户输入更宽松 — 代码中的 "system" 等是合法的）"""
    return scan(content, source=f"code:{filepath}" if filepath else "code")


def scan_filepath(filepath: str) -> InjectionResult:
    """扫描文件路径（检测路径中的注入 payload）"""
    # 只对明显异常的路径名告警
    if len(filepath) > 500:
        return InjectionResult(blocked=True, suspicious=True,
                              reason=f"文件路径过长 ({len(filepath)} chars)")
    if re.search(r'(?i)(ignore|bypass|override|system)\s+(instructions?|prompts?)', filepath):
        return InjectionResult(blocked=True, suspicious=True,
                              reason=f"文件路径包含注入模式: {filepath}")
    return scan(filepath, source="filepath")


def _sanitize(text: str) -> str:
    """清洗可疑内容 — 用 Unicode 同形字替换危险关键词

    用全角字符或零宽空格破坏 prompt 注入结构，
    同时保持人类可读性。
    """
    # 在 SYSTEM / INSTRUCTION 等关键词中插入零宽空格
    sanitized = text
    for word in ("SYSTEM", "INSTRUCTION", "PROMPT", "IGNORE", "OVERRIDE"):
        pattern = re.compile(rf'\b{word}\b', re.IGNORECASE)
        sanitized = pattern.sub(lambda m: m.group()[0] + '​' + m.group()[1:], sanitized)

    # 转义 ``` 防止分隔符逃逸
    sanitized = sanitized.replace("```", "`​`​`")

    return sanitized


# ═══════════════════════════════════════════════════════════
# 辅助
# ═══════════════════════════════════════════════════════════

def _guess_language(filepath: str) -> str:
    ext = Path(filepath).suffix.lower()
    lang_map = {
        ".py": "python", ".pyw": "python",
        ".js": "javascript", ".jsx": "javascript", ".mjs": "javascript",
        ".ts": "typescript", ".tsx": "typescript",
        ".java": "java",
        ".go": "go",
        ".rs": "rust",
        ".c": "c", ".cpp": "cpp", ".h": "c", ".hpp": "cpp",
        ".cs": "csharp",
        ".rb": "ruby",
        ".php": "php",
        ".swift": "swift",
        ".kt": "kotlin", ".kts": "kotlin",
    }
    return lang_map.get(ext, "")


def is_safe(text: str) -> bool:
    """快速检查：文本是否安全（无注入风险）"""
    result = scan(text)
    return not result.blocked
