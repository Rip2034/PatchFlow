"""PatchApplicator — 增量片段级修补

从 fixer_agent.py 的 apply_agent_patches() 抽取出来变成独立工具，
供 Pipeline A（Orchestrator）和 Pipeline B（AgentOrchestrator）共用。

6 级修补策略：
  1. 文件不存在 → 创建
  2. old snippet 精确匹配 → 文本替换
  3. new > 60% 原文件 → 完整覆盖
  4. 宽松匹配（忽略空白）→ 替换
  5. new 太小（<200B, <20%）→ 拒绝
  6. 兜底覆盖（带警告）
"""

import threading
from dataclasses import dataclass
from pathlib import Path

from patchflow.utils import logger


@dataclass
class SnippetPatch:
    file: str
    old: str
    new: str
    reason: str = ""


@dataclass
class LineChange:
    file: str
    line_start: int
    line_end: int
    old_lines: str
    new_lines: str

    def diff_hunk(self) -> str:
        lines = [
            f"--- {self.file}:{self.line_start}-{self.line_end}",
            f"+++ {self.file}:{self.line_start}",
        ]
        for line in self.old_lines.split("\n"):
            lines.append(f"-{line}")
        for line in self.new_lines.split("\n"):
            lines.append(f"+{line}")
        return "\n".join(lines)


class DiffTracker:
    MAX_STORED_PATCHES = 50

    def __init__(self):
        self._patches: list[LineChange] = []
        self._before_state: dict[str, str] = {}
        self._lock = threading.RLock()

    def record(self, file: str, old_content: str, new_content: str) -> list[LineChange]:
        with self._lock:
            if file not in self._before_state:
                self._before_state[file] = old_content
            changes = _compute_line_changes(old_content, new_content, file)
            for ch in changes:
                self._patches.append(ch)
                if len(self._patches) > self.MAX_STORED_PATCHES:
                    self._patches.pop(0)
            return changes

    def get_diff_context(self, file: str, context_lines: int = 3) -> str:
        with self._lock:
            recent = [p for p in self._patches[-10:] if p.file == file]
            if not recent:
                return ""
            parts = []
            for p in recent[-3:]:
                parts.append(p.diff_hunk())
            return "\n\n".join(parts)

    def rollback_patch(self, file: str) -> bool:
        with self._lock:
            for i in range(len(self._patches) - 1, -1, -1):
                if self._patches[i].file == file:
                    self._patches.pop(i)
                    if file in self._before_state:
                        return self.restore_file(file)
                    return True
            return False

    def restore_file(self, file: str) -> bool:
        with self._lock:
            if file not in self._before_state:
                return False
            try:
                Path(file).write_text(self._before_state[file], encoding="utf-8")
                logger.info(f"DiffTracker 已恢复: {file}")
                return True
            except OSError as e:
                logger.error(f"DiffTracker 恢复失败 {file}: {e}")
                return False

    @property
    def recent_changes_summary(self) -> str:
        with self._lock:
            if not self._patches:
                return "no recent changes"
            latest = self._patches[-3:]
            parts = []
            for p in latest:
                parts.append(f"{p.file}:{p.line_start} ({p.old_lines[:40].strip()} → {p.new_lines[:40].strip()})")
            return "; ".join(parts)


class PatchApplicator:
    """静态方法类：将 snippet 补丁应用到磁盘文件（文件级并发安全）"""

    @staticmethod
    def apply(file_path: str, patches: list[SnippetPatch],
              work_dir: str = ".", diff_tracker: DiffTracker | None = None) -> bool:
        if not patches:
            return False
        from patchflow.core.concurrency import AtomicWrite, get_file_lock_manager

        wd = Path(work_dir)
        target = wd / file_path
        target.parent.mkdir(parents=True, exist_ok=True)

        flm = get_file_lock_manager()
        with flm.lock(file_path):
            existing = ""
            if target.exists():
                try:
                    existing = target.read_text(encoding="utf-8")
                except UnicodeDecodeError:
                    pass

            for patch in patches:
                if not patch.new:
                    continue

                # Strategy 1: new file
                if not existing:
                    if diff_tracker:
                        diff_tracker.record(file_path, "", patch.new)
                    AtomicWrite.write(str(target), patch.new)
                    logger.info(f"Patcher 新建: {file_path}")
                    return True

                # Strategy 2: exact snippet match
                if patch.old and patch.old in existing:
                    replaced = existing.replace(patch.old, patch.new, 1)
                    if diff_tracker:
                        diff_tracker.record(file_path, existing, replaced)
                    AtomicWrite.write(str(target), replaced)
                    logger.info(f"Patcher 精确替换: {file_path}")
                    return True

                # Strategy 3: new content close to full file
                len_ratio = len(patch.new) / max(len(existing), 1)
                if len_ratio > 0.6:
                    if diff_tracker:
                        diff_tracker.record(file_path, existing, patch.new)
                    AtomicWrite.write(str(target), patch.new)
                    logger.info(f"Patcher 全文覆盖: {file_path}")
                    return True

                # Strategy 4: stripped match (preserve original whitespace)
                if patch.old:
                    old_stripped = patch.old.strip()
                    existing_stripped = existing.strip()
                    if old_stripped and old_stripped in existing_stripped:
                        replaced = _replace_in_original(existing, old_stripped, patch.new.strip())
                        if diff_tracker:
                            diff_tracker.record(file_path, existing, replaced)
                        AtomicWrite.write(str(target), replaced)
                        logger.info(f"Patcher 宽松替换: {file_path}")
                        return True

                # Strategy 5: too small to trust
                if len_ratio < 0.2 and len(patch.new) < 200:
                    logger.warn(f"Patcher 拒绝覆盖 {file_path}: 内容过小 ({len(patch.new)}B)")
                    return False

            # Strategy 6: fallback with last patch (safety-checked)
            last = patches[-1]
            final_ratio = len(last.new) / max(len(existing), 1)
            if final_ratio < 0.2 and len(last.new) < 200:
                logger.error(f"Patcher 拒绝兜底覆盖 {file_path}: 内容过小 ({len(last.new)}B, {final_ratio:.1%})")
                return False
            if diff_tracker:
                diff_tracker.record(file_path, existing, last.new)
            AtomicWrite.write(str(target), last.new)
            logger.warn(f"Patcher 兜底覆盖: {file_path} ({len(last.new)}B)")
            return True

    @staticmethod
    def apply_all(patches: list[SnippetPatch], work_dir: str = ".",
                  diff_tracker: DiffTracker | None = None) -> tuple[int, int]:
        success = 0
        fail = 0
        files_seen: set[str] = set()
        for patch in patches:
            if patch.file in files_seen:
                continue
            files_seen.add(patch.file)
            file_patches = [p for p in patches if p.file == patch.file]
            if PatchApplicator.apply(patch.file, file_patches, work_dir, diff_tracker):
                success += 1
            else:
                fail += 1
        logger.info(f"Patcher 应用: {success} 成功, {fail} 失败 (共 {len(files_seen)} 文件)")
        return success, fail

    @staticmethod
    def detect_patches(old_content: str, new_content: str,
                       file_path: str) -> list[SnippetPatch]:
        import difflib
        if old_content == new_content:
            return []
        patches = []
        matcher = difflib.SequenceMatcher(None, old_content, new_content)
        for tag, i1, i2, j1, j2 in matcher.get_opcodes():
            if tag == "equal":
                continue
            old = old_content[i1:i2]
            new = new_content[j1:j2]
            patches.append(SnippetPatch(file=file_path, old=old, new=new,
                                        reason=f"{tag}: L{i1}-{i2}"))
        return patches


def _replace_in_original(original: str, old_stripped: str, new_stripped: str) -> str:
    """在原文本中定位旧片段并替换，保留原文件的空白/缩进"""
    import re
    escaped = re.escape(old_stripped)
    m = re.search(escaped, original)
    if m:
        return original[:m.start()] + new_stripped + original[m.end():]

    # 兜底：逐行找
    lines = original.split("\n")
    for i, line in enumerate(lines):
        if old_stripped in line.strip():
            indent = line[:len(line) - len(line.lstrip())]
            new_indented = "\n".join(
                (indent + ln) if ln.strip() else ln
                for ln in new_stripped.split("\n")
            )
            lines[i] = new_indented
            return "\n".join(lines)

    return original


def _compute_line_changes(old: str, new: str, file: str) -> list[LineChange]:
    import difflib
    changes = []
    old_lines = old.split("\n")
    new_lines = new.split("\n")
    matcher = difflib.SequenceMatcher(None, old_lines, new_lines)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            continue
        changes.append(LineChange(
            file=file,
            line_start=i1 + 1,
            line_end=i2,
            old_lines="\n".join(old_lines[i1:i2]),
            new_lines="\n".join(new_lines[j1:j2]),
        ))
    return changes
