"""错误解析器 — 从多语言报错文本中提取结构化信息

每个语言有独特的 traceback 格式和错误类型体系。
解析工作委托给 LanguageRegistry，不再硬编码任何格式。
"""

from patchflow.core.language_registry import LanguageRegistry


class ParsedError:
    """结构化错误信息"""
    def __init__(self, raw: str, file: str = "", line: int = 0,
                 error_type: str = "unknown", message: str = "",
                 call_chain: list[dict] | None = None,
                 language: str = ""):
        self.raw = raw
        self.file = file
        self.line = line
        self.error_type = error_type
        self.message = message
        self.call_chain = call_chain or []
        self.language = language


def parse(raw_error: str, lang_name: str | None = None) -> ParsedError:
    """从原始错误文本中提取结构化信息

    用 LanguageRegistry 选择最合适的语言解析器，
    支持 Python / JavaScript / TypeScript / Java / Go / Rust。

    Args:
        raw_error: 编译器/解释器的完整报错输出
        lang_name: 语言名称（可选），不提供则自动检测

    Returns:
        ParsedError: 结构化的错误信息
    """
    reg = LanguageRegistry()
    lang = reg.get(lang_name) if lang_name else None

    traceback_frames = reg.parse_traceback(raw_error, lang=lang)
    if traceback_frames:
        crash_site = traceback_frames[-1]
        error_file = crash_site.get("file", "")
        error_line = crash_site.get("line", 0)
    else:
        error_file = ""
        error_line = 0

    error_type, root_cause = reg.classify_error(raw_error, lang=lang)

    if lang:
        detected_lang = lang.name
    else:
        detected_lang = ""
        for name, l in reg.all().items():
            for pf in l.project_files:
                from pathlib import Path
                if Path(pf).exists():
                    detected_lang = name
                    break
            if detected_lang:
                break

    message = root_cause or raw_error.strip().split("\n")[-1] if raw_error.strip() else ""

    return ParsedError(
        raw=raw_error,
        file=error_file,
        line=error_line,
        error_type=error_type,
        message=message,
        call_chain=traceback_frames or [{"file": error_file, "line": error_line, "function": "", "role": "crash_site"}] if error_file else [],
        language=detected_lang,
    )
