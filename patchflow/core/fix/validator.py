"""验证系统 — 判断代码是否"真的可用"（多语言）

这是 PatchFlow 的核心质量关卡。LLM 输出的代码"看起来对"不算数，
必须真正跑起来通过验证。

所有语言相关的验证逻辑已迁移至 LanguageStrategy 子类中。
"""

from pathlib import Path

from patchflow.core.analysis.error_parser import ParsedError
from patchflow.core.language_strategy import LanguageFactory
from patchflow.utils import logger


class ValidationResult:
    """验证结果"""
    def __init__(self, ok: bool, error: ParsedError | None = None,
                 message: str = "", language: str = ""):
        self.ok = ok
        self.error = error
        self.message = message
        self.language = language

    def __repr__(self):
        return f"ValidationResult(ok={self.ok}, lang={self.language})"


def detect_project_type(work_dir: str = ".") -> str:
    """检测项目类型（基于 LanguageFactory 自动检测）"""
    factory = LanguageFactory()
    strategy = factory.detect(work_dir)
    return strategy.name if strategy else "unknown"


def validate(work_dir: str = ".") -> ValidationResult:
    """对工作目录中的代码执行验证

    自动检测项目类型，通过 LanguageStrategy 多态分发到对应语言验证器。
    """
    wd = str(Path(work_dir).resolve())
    factory = LanguageFactory()
    strategy = factory.detect(wd)

    if strategy is None:
        logger.info("项目类型: unknown，无法确定语言，跳过验证")
        return ValidationResult(ok=True, message="未知项目类型，跳过验证", language="unknown")

    logger.info(f"项目类型: {strategy.name}，使用 {strategy.run_command or strategy.compile_command or 'N/A'} 验证")
    return strategy.validate(wd)
