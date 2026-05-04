"""错误分析器 — 精准定位问题根因和调用链（多语言支持）

这是"验证 → 修复"闭环中的"诊断"环节。
当 Validator 发现代码报错时，ErrorAnalyzer 负责解析错误信息，
定位根因和影响范围。

设计要点：
  1. 通过 LanguageRegistry 将原生 traceback 解析委托给对应语言的处理器
  2. 不再硬编码 Python traceback 格式（支持 Python / JS / TS / Java / Go / Rust）
  3. 结构化输出 ErrorAnalysis，包含：错误类型、根因、调用链、影响文件
  4. 兜底机制：如果语言特定解析失败，回退到通用解析器

支持的语言：Python / JavaScript / TypeScript / Java / Go / Rust
"""

from patchflow.core.language_registry import LanguageRegistry
from patchflow.core.analysis.error_parser import parse as fallback_parse


class ErrorAnalysis:
    """结构化错误分析结果"""
    def __init__(
        self,
        type: str = "unknown",
        root_cause: str = "",
        call_chain: list[dict] | None = None,
        impact_files: list[str] | None = None,
        suggestion: str = "",
        confidence: float = 0.0,
        raw: str = "",
        language: str = "",
    ):
        self.type = type
        self.root_cause = root_cause
        self.call_chain = call_chain or []
        self.impact_files = impact_files or []
        self.suggestion = suggestion
        self.confidence = confidence
        self.raw = raw
        self.language = language


def analyze(error_text: str, work_dir: str = ".") -> ErrorAnalysis:
    """解析错误文本，返回结构化分析结果

    用 LanguageRegistry 自动检测项目语言，然后用该语言的
    traceback 模式和错误分类器解析。

    Args:
        error_text: 编译器/解释器的完整报错输出
        work_dir: 工作目录（用于语言检测）

    Returns:
        ErrorAnalysis: 结构化的分析结果
    """
    reg = LanguageRegistry()
    lang = reg.detect(work_dir)
    lang_name = lang.name if lang else ""

    traceback_frames = reg.parse_traceback(error_text, lang=lang)
    if traceback_frames:
        return _build_from_frames(error_text, traceback_frames, lang_name)

    fallback = fallback_parse(error_text, lang_name=lang_name)
    return _build_from_fallback(fallback)


def _build_from_frames(error_text: str, frames: list[dict], lang_name: str) -> ErrorAnalysis:
    """从解析出的栈帧构建分析结果"""
    call_chain = frames
    impact_files = list(dict.fromkeys(f["file"] for f in frames))
    crash_site = frames[-1] if frames else {}

    error_type, root_cause = _extract_error_type(error_text)

    suggestion = _generate_suggestion(error_type, root_cause, crash_site)

    return ErrorAnalysis(
        type=error_type,
        root_cause=root_cause,
        call_chain=call_chain,
        impact_files=impact_files,
        suggestion=suggestion,
        confidence=0.9,
        raw=error_text,
        language=lang_name,
    )


def _build_from_fallback(fallback) -> ErrorAnalysis:
    """从兜底的 error_parser 结果构建"""
    return ErrorAnalysis(
        type=fallback.error_type,
        root_cause=fallback.message,
        call_chain=fallback.call_chain,
        impact_files=[fallback.file] if fallback.file else [],
        suggestion=f"{fallback.file} 第{fallback.line}行: {fallback.message}" if fallback.file else fallback.message,
        confidence=0.6,
        raw=fallback.raw,
        language=fallback.language,
    )


def _extract_error_type(error_text: str) -> tuple[str, str]:
    """从错误文本中提取错误类型和根因描述

    使用 LanguageRegistry 进行分类。
    """
    reg = LanguageRegistry()
    etype, root_cause = reg.classify_error(error_text)
    return (etype, root_cause)


def _generate_suggestion(error_type: str, root_cause: str, crash_site: dict) -> str:
    """根据错误类型和崩溃点生成修复建议"""
    crash_file = crash_site.get("file", "?")
    crash_line = crash_site.get("line", "?")

    suggestions = {
        "syntax": f"{crash_file} 第{crash_line}行存在语法错误，请检查拼写、括号匹配、缩进",
        "type": f"{crash_file} 第{crash_line}行存在类型错误，请检查 None 值或类型不匹配",
        "import": f"{crash_file} 缺少依赖模块，请检查导入路径或安装依赖",
        "name": f"{crash_file} 第{crash_line}行使用了未定义的变量",
        "attribute": f"{crash_file} 第{crash_line}行访问了不存在的属性",
        "key_error": f"{crash_file} 第{crash_line}行访问了不存在的键",
        "index_error": f"{crash_file} 第{crash_line}行索引越界",
        "value_error": f"{crash_file} 第{crash_line}行值错误",
        "file_error": f"{crash_file} 第{crash_line}行文件操作失败",
        "assertion": f"{crash_file} 第{crash_line}行断言失败",
        "zero_division": f"{crash_file} 第{crash_line}行除以零",
        "runtime": f"{crash_file} 第{crash_line}行触发了运行时异常",
    }

    return suggestions.get(error_type, f"{crash_file} 第{crash_line}行发生{error_type}错误")
