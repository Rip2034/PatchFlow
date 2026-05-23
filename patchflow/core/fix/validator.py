"""Validation entry point for project code."""

from pathlib import Path

from patchflow.core.analysis.error_parser import ParsedError
from patchflow.core.language_strategy import LanguageFactory
from patchflow.utils import logger


class ValidationResult:
    """Result of a validation attempt.

    status values:
    - passed: validation ran and succeeded
    - failed: validation ran and failed
    - skipped: validation was intentionally skipped by a language strategy
    - unsupported: no suitable validator could be selected
    """

    def __init__(self, ok: bool | None = None, error: ParsedError | None = None,
                 message: str = "", language: str = "", status: str | None = None):
        self.status = status or ("passed" if ok else "failed")
        self.ok = self.status == "passed"
        self.error = error
        self.message = message
        self.language = language

    def __repr__(self):
        return f"ValidationResult(status={self.status}, lang={self.language})"


def detect_project_type(work_dir: str = ".") -> str:
    factory = LanguageFactory()
    strategy = factory.detect(work_dir)
    return strategy.name if strategy else "unknown"


def validate(work_dir: str = ".") -> ValidationResult:
    """Validate code in work_dir via the detected language strategy."""
    wd = str(Path(work_dir).resolve())
    factory = LanguageFactory()
    strategy = factory.detect(wd)

    if strategy is None:
        logger.info("Project type: unknown; validation unsupported")
        return ValidationResult(
            status="unsupported",
            message="Unknown project type; validation unsupported",
            language="unknown",
        )

    command = strategy.run_command or strategy.compile_command or "N/A"
    logger.info(f"Project type: {strategy.name}; validator: {command}")
    return strategy.validate(wd)
