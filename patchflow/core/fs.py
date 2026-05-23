"""Filesystem safety helpers for project-scoped file operations."""

from pathlib import Path

from patchflow.core.agent_sandbox import PathGuard, SandboxViolation
from patchflow.core.concurrency import AtomicWrite


def project_root(work_dir: str | Path = ".") -> Path:
    return Path(work_dir).resolve()


def relative_path(work_dir: str | Path, filepath: str | Path) -> str:
    """Return a normalized project-relative path, rejecting paths outside work_dir."""
    root = project_root(work_dir)
    raw = Path(filepath)

    if raw.is_absolute():
        try:
            rel = raw.resolve().relative_to(root)
        except ValueError as exc:
            raise SandboxViolation("path outside project root", str(filepath)) from exc
    else:
        rel = raw

    if not str(rel) or str(rel) == ".":
        raise SandboxViolation("empty path", str(filepath))

    resolved = (root / rel).resolve()
    try:
        rel = resolved.relative_to(root)
    except ValueError as exc:
        raise SandboxViolation("path outside project root", str(filepath)) from exc

    return rel.as_posix()


def resolve_read_path(work_dir: str | Path, filepath: str | Path) -> Path:
    rel = relative_path(work_dir, filepath)
    return PathGuard(str(project_root(work_dir))).validate_read(rel)


def resolve_write_path(work_dir: str | Path, filepath: str | Path, content_size: int = 0) -> Path:
    rel = relative_path(work_dir, filepath)
    return PathGuard(str(project_root(work_dir))).validate_write(rel, content_size)


def safe_atomic_write(work_dir: str | Path, filepath: str | Path, content: str, encoding: str = "utf-8") -> Path:
    target = resolve_write_path(work_dir, filepath, len(content.encode(encoding)))
    AtomicWrite.write(target, content, encoding=encoding)
    return target
