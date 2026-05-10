"""验证系统 — 判断代码是否"真的可用"（多语言）

这是 PatchFlow 的核心质量关卡。LLM 输出的代码"看起来对"不算数，
必须真正跑起来通过验证。

支持语言：Python / JavaScript / TypeScript / Java / Go / Rust
"""

from pathlib import Path

from patchflow.core.analysis.error_parser import ParsedError, parse
from patchflow.core.language_registry import LanguageRegistry
from patchflow.utils import logger
from patchflow.utils.runner import run


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
    """检测项目类型（基于 LanguageRegistry 自动检测）

    Returns language name, or "unknown" if cannot determine.
    """
    reg = LanguageRegistry()
    lang = reg.detect(work_dir)
    return lang.name if lang else "unknown"


def validate(work_dir: str = ".") -> ValidationResult:
    """对工作目录中的代码执行验证

    自动检测项目类型，根据语言选择合适的验证方式：
      - Python: compile() + python <entry>
      - JavaScript: node <entry>
      - TypeScript: tsc (编译) + node <entry> (运行)
      - Java: javac (编译) + java (运行)
      - Go: go build (编译)
      - Rust: cargo build (编译)

    Args:
        work_dir: 工作目录

    Returns:
        ValidationResult
    """
    wd = Path(work_dir)
    reg = LanguageRegistry()
    lang = reg.detect(str(wd))
    lang_name = lang.name if lang else "unknown"

    if lang_name == "unknown":
        logger.info("项目类型: unknown，无法确定语言，跳过验证")
        return ValidationResult(ok=True, message="未知项目类型，跳过验证", language=lang_name)

    if lang_name == "python":
        return _validate_python(wd, lang)

    logger.info(f"项目类型: {lang_name}，使用 {lang.run_command or lang.compile_command or 'N/A'} 验证")

    # Rust: cargo build / cargo run 不接受文件参数，直接运行
    if lang_name == "rust":
        return _validate_rust(wd, lang)

    # Java: 编译用文件名(javac Main.java)，运行用类名(java Main)
    if lang_name == "java":
        return _validate_java(wd, lang)

    # TypeScript: 编译 .ts → .js，然后 node 运行 .js
    if lang_name == "typescript":
        return _validate_typescript(wd, lang)

    # JavaScript / Go: 通用方式
    return _validate_generic(wd, lang, lang_name)


def _validate_python(wd: Path, lang) -> ValidationResult:
    """Python 专用验证（保持原有的 compile + run 双阶段）"""
    entry = _find_entry(wd, "python")
    if entry is None:
        return ValidationResult(
            ok=False,
            error=parse("No entry file found (app.py or main.py)"),
            language="python",
        )

    logger.step(f"验证入口文件: {entry.name}")

    try:
        source = entry.read_text(encoding="utf-8")
        compile(source, str(entry), "exec")
        logger.info("编译验证通过")
    except SyntaxError as e:
        logger.error(f"编译验证失败: {e}")
        return ValidationResult(ok=False, error=parse(str(e), lang_name="python"), language="python")

    result = run(f"python {entry.name}", cwd=str(wd))

    if result.ok:
        logger.success("运行验证通过")
        return ValidationResult(ok=True, language="python")

    error_text = result.stderr.strip() or result.stdout.strip() or "Unknown runtime error"
    logger.error(f"运行验证失败 (exit={result.exit_code})")
    return ValidationResult(ok=False, error=parse(error_text, lang_name="python"), language="python")


def _find_entry(work_dir: Path, lang_name: str) -> Path | None:
    """根据语言查找入口文件"""
    entry_checks = {
        "python": ["app.py", "main.py"],
        "javascript": ["index.js", "app.js", "server.js", "main.js"],
        "typescript": ["index.ts", "app.ts", "server.ts", "main.ts"],
        "java": ["Main.java", "Application.java", "App.java"],
        "go": ["main.go"],
        "rust": ["src/main.rs", "src/lib.rs"],
    }
    for name in entry_checks.get(lang_name, ["app.py", "main.py"]):
        candidate = work_dir / name
        if candidate.exists():
            return candidate
    return None


def _validate_rust(wd: Path, lang) -> ValidationResult:
    """Rust 验证：cargo build + cargo run（不接受文件参数）"""
    if lang.compile_command:
        logger.info(f"编译: {lang.compile_command}")
        result = run(lang.compile_command, cwd=str(wd))
        if not result.ok:
            error_text = result.stderr.strip() or result.stdout.strip() or "Compilation failed"
            logger.error("编译验证失败")
            return ValidationResult(ok=False, error=parse(error_text, lang_name="rust"), language="rust")

    if lang.run_command:
        logger.info(f"运行: {lang.run_command}")
        result = run(lang.run_command, cwd=str(wd))
        if result.ok:
            logger.success("运行验证通过")
            return ValidationResult(ok=True, language="rust")
        error_text = result.stderr.strip() or result.stdout.strip() or f"Runtime error (exit={result.exit_code})"
        logger.error(f"运行验证失败 (exit={result.exit_code})")
        return ValidationResult(ok=False, error=parse(error_text, lang_name="rust"), language="rust")

    logger.success("验证通过（仅编译检查，无需运行）")
    return ValidationResult(ok=True, language="rust")


def _validate_java(wd: Path, lang) -> ValidationResult:
    """Java 验证：javac Main.java 编译，java Main 运行（类名不带 .java）"""
    entry = _find_entry(wd, "java")
    if entry is None:
        return ValidationResult(
            ok=False,
            error=parse("No entry point found for Java"),
            language="java",
        )

    logger.step(f"验证入口文件: {entry.name}")

    if lang.compile_command:
        compile_cmd = f"{lang.compile_command} {entry.name}"
        logger.info(f"编译: {compile_cmd}")
        result = run(compile_cmd, cwd=str(wd))
        if not result.ok:
            error_text = result.stderr.strip() or result.stdout.strip() or "Compilation failed"
            logger.error("编译验证失败")
            return ValidationResult(ok=False, error=parse(error_text, lang_name="java"), language="java")

    if lang.run_command:
        run_cmd = f"{lang.run_command} {entry.stem}"
        logger.info(f"运行: {run_cmd}")
        result = run(run_cmd, cwd=str(wd))
        if result.ok:
            logger.success("运行验证通过")
            return ValidationResult(ok=True, language="java")
        error_text = result.stderr.strip() or result.stdout.strip() or f"Runtime error (exit={result.exit_code})"
        logger.error(f"运行验证失败 (exit={result.exit_code})")
        return ValidationResult(ok=False, error=parse(error_text, lang_name="java"), language="java")

    logger.success("验证通过（仅编译检查，无需运行）")
    return ValidationResult(ok=True, language="java")


def _validate_typescript(wd: Path, lang) -> ValidationResult:
    """TypeScript 验证：tsc 编译 .ts → .js，然后 node 运行 .js"""
    entry = _find_entry(wd, "typescript")
    if entry is None:
        return ValidationResult(
            ok=False,
            error=parse("No entry point found for TypeScript"),
            language="typescript",
        )

    logger.step(f"验证入口文件: {entry.name}")

    if lang.compile_command:
        compile_cmd = f"{lang.compile_command} {entry.name}"
        logger.info(f"编译: {compile_cmd}")
        result = run(compile_cmd, cwd=str(wd))
        if not result.ok:
            error_text = result.stderr.strip() or result.stdout.strip() or "Compilation failed"
            logger.error("编译验证失败")
            return ValidationResult(ok=False, error=parse(error_text, lang_name="typescript"), language="typescript")

    if lang.run_command:
        js_entry = entry.stem + ".js"
        run_cmd = f"{lang.run_command} {js_entry}"
        logger.info(f"运行: {run_cmd}")
        result = run(run_cmd, cwd=str(wd))
        if result.ok:
            logger.success("运行验证通过")
            return ValidationResult(ok=True, language="typescript")
        error_text = result.stderr.strip() or result.stdout.strip() or f"Runtime error (exit={result.exit_code})"
        logger.error(f"运行验证失败 (exit={result.exit_code})")
        return ValidationResult(ok=False, error=parse(error_text, lang_name="typescript"), language="typescript")

    logger.success("验证通过（仅编译检查，无需运行）")
    return ValidationResult(ok=True, language="typescript")


def _validate_generic(wd: Path, lang, lang_name: str) -> ValidationResult:
    """通用验证（JavaScript / Go 等）—— 编译 + 运行入口文件"""
    entry = _find_entry(wd, lang_name)
    if entry is None:
        return ValidationResult(
            ok=False,
            error=parse(f"No entry point found for {lang_name}"),
            language=lang_name,
        )

    logger.step(f"验证入口文件: {entry.name}")

    if lang.compile_command:
        compile_cmd = f"{lang.compile_command} {entry.name}"
        logger.info(f"编译: {compile_cmd}")
        result = run(compile_cmd, cwd=str(wd))
        if not result.ok:
            error_text = result.stderr.strip() or result.stdout.strip() or "Compilation failed"
            logger.error("编译验证失败")
            return ValidationResult(ok=False, error=parse(error_text, lang_name=lang_name), language=lang_name)

    if lang.run_command:
        run_cmd = f"{lang.run_command} {entry.name}"
        logger.info(f"运行: {run_cmd}")
        result = run(run_cmd, cwd=str(wd))
        if result.ok:
            logger.success("运行验证通过")
            return ValidationResult(ok=True, language=lang_name)
        error_text = result.stderr.strip() or result.stdout.strip() or f"Runtime error (exit={result.exit_code})"
        logger.error(f"运行验证失败 (exit={result.exit_code})")
        return ValidationResult(ok=False, error=parse(error_text, lang_name=lang_name), language=lang_name)

    logger.success("验证通过（仅编译检查，无需运行）")
    return ValidationResult(ok=True, language=lang_name)
