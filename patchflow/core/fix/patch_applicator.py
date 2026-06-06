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
        """Apply all patches for a single file sequentially.

        V0.5 fix: previously returned after the first successful patch,
        silently dropping subsequent patches for the same file.
        Now applies all patches in order, updating the file content
        after each successful match so later patches see the latest state.
        """
        if not patches:
            return False
        from patchflow.core.concurrency import get_file_lock_manager
        from patchflow.core.fs import relative_path, resolve_write_path, safe_atomic_write

        wd = Path(work_dir)
        try:
            rel = relative_path(wd, file_path)
            target = resolve_write_path(wd, rel, max(len((p.new or "").encode("utf-8", errors="replace")) for p in patches))
        except Exception as e:
            logger.error(f"Patcher rejected unsafe path {file_path}: {e}")
            return False

        flm = get_file_lock_manager()
        with flm.lock(rel):
            existing = ""
            if target.exists():
                try:
                    existing = target.read_text(encoding="utf-8", errors="replace")
                except UnicodeDecodeError:
                    pass

            original_content = existing
            any_applied = False

            for patch in patches:
                if not patch.new:
                    continue

                applied_this = False

                # Strategy 1: new file
                if not existing:
                    existing = patch.new
                    applied_this = True
                    logger.info(f"Patcher 新建: {file_path}")

                # Strategy 2: exact snippet match
                elif patch.old and patch.old in existing:
                    existing = existing.replace(patch.old, patch.new, 1)
                    applied_this = True
                    logger.info(f"Patcher 精确替换: {file_path}")

                # ── V0.5: Strategy 2b+2c: fuzzy match, then ratio-guided ──
                elif patch.old:
                    # 2b: fuzzy line-based match
                    fuzzy_result = _fuzzy_line_match(existing, patch.old, patch.new)
                    if fuzzy_result is not None:
                        existing = fuzzy_result
                        applied_this = True
                        logger.info(f"Patcher 模糊行匹配替换: {file_path}")
                    else:
                        # 2c: diff-ratio approximate match (fallback)
                        ratio_result = _ratio_guided_match(existing, patch.old, patch.new)
                        if ratio_result is not None:
                            existing = ratio_result
                            applied_this = True
                            logger.info(f"Patcher 相似度引导替换: {file_path}")

                # Strategy 3: new content close to full file
                if not applied_this:
                    len_ratio = len(patch.new) / max(len(existing), 1)
                    if len_ratio > 0.6:
                        existing = patch.new
                        applied_this = True
                        logger.info(f"Patcher 全文覆盖: {file_path}")

                # Strategy 4: stripped match (preserve original whitespace)
                if not applied_this and patch.old:
                    old_stripped = patch.old.strip()
                    existing_stripped = existing.strip()
                    if old_stripped and old_stripped in existing_stripped:
                        existing = _replace_in_original(existing, old_stripped, patch.new.strip())
                        applied_this = True
                        logger.info(f"Patcher 宽松替换: {file_path}")

                # Strategy 5: too small to trust — skip this patch, continue
                if not applied_this:
                    len_ratio = len(patch.new) / max(len(existing), 1)
                    if len_ratio < 0.2 and len(patch.new) < 30:
                        logger.warn(f"Patcher 跳过过小补丁 {file_path}: ({len(patch.new)}B)")
                        continue  # skip this patch, try next

                if applied_this:
                    any_applied = True

            # Strategy 6: fallback — if nothing matched, use last patch (safety-checked)
            if not any_applied:
                last = patches[-1]
                final_ratio = len(last.new) / max(len(existing), 1)
                if final_ratio < 0.2 and len(last.new) < 30:
                    logger.error(f"Patcher 拒绝兜底覆盖 {file_path}: 内容过小 ({len(last.new)}B, {final_ratio:.1%})")
                    return False
                existing = last.new
                logger.warn(f"Patcher 兜底覆盖: {file_path} ({len(last.new)}B)")
                any_applied = True

            # Write final result once (V0.5: single write for all patches)
            if any_applied and existing != original_content:
                if diff_tracker:
                    diff_tracker.record(file_path, original_content, existing)
                safe_atomic_write(wd, rel, existing)
                logger.info(f"Patcher 应用 {file_path}: {len(patches)} patches, "
                           f"{len(original_content)}→{len(existing)} chars")
                return True

            return False

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


def _fuzzy_line_match(existing: str, old_snippet: str, new_snippet: str) -> str | None:
    """V0.5: 模糊行匹配 — 逐行比对，容忍空白/缩进差异

    当精确匹配失败时，尝试：
      1. 将 old_snippet 按行拆分
      2. 在 existing 中找每个 old 行的最佳匹配（用 SequenceMatcher）
      3. 如果所有行都能匹配（ratio > 0.8），执行替换
    """
    import difflib

    old_lines = old_snippet.strip().split("\n")
    new_lines = new_snippet.strip().split("\n")
    existing_lines = existing.split("\n")

    if len(old_lines) > len(existing_lines):
        return None

    # 用 SequenceMatcher 找最佳匹配区域
    # 将 old 视为查询，在 existing 中找最相似的连续块
    existing_flat = existing.strip()
    old_flat = old_snippet.strip()

    sm = difflib.SequenceMatcher(None, existing_flat, old_flat)
    # 找到最长匹配块
    matching_blocks = sm.get_matching_blocks()
    if not matching_blocks or matching_blocks[0].size < len(old_flat) * 0.5:
        return None

    # 用逐行匹配
    best_start = -1
    best_end = -1
    best_ratio = 0.0

    for i in range(len(existing_lines) - len(old_lines) + 1):
        window = "\n".join(existing_lines[i:i + len(old_lines)])
        ratio = difflib.SequenceMatcher(None, old_snippet.strip(), window.strip()).ratio()
        if ratio > best_ratio:
            best_ratio = ratio
            best_start = i
            best_end = i + len(old_lines)

    # 阈值：0.75 相似度
    if best_ratio >= 0.75 and best_start >= 0:
        # 保留原缩进
        orig_indent = ""
        if existing_lines[best_start]:
            orig_indent = existing_lines[best_start][:len(existing_lines[best_start]) - len(existing_lines[best_start].lstrip())]
        indented_new = []
        for line in new_lines:
            if line.strip():
                indented_new.append(orig_indent + line.strip())
            else:
                indented_new.append("")
        result_lines = existing_lines[:best_start] + indented_new + existing_lines[best_end:]
        logger.info(f"Patcher 模糊行匹配: ratio={best_ratio:.2f}, lines {best_start}-{best_end}")
        return "\n".join(result_lines)

    return None


def _ratio_guided_match(existing: str, old_snippet: str, new_snippet: str) -> str | None:
    """V0.5: 相似度引导匹配 — 用内部行匹配（适合 old/new 行数不同）

    当 LLM 输出的 old 和 new 行数不同时（如 old=3行, new=5行），
    用最佳行匹配找到替换位置。
    """
    import difflib

    old_lines = old_snippet.strip().split("\n")
    new_lines = new_snippet.strip().split("\n")
    existing_lines = existing.split("\n")

    if len(old_lines) > len(existing_lines):
        return None

    # 找 best anchor line（old 的第一行非空行）
    anchor = old_lines[0].strip()
    for line in old_lines:
        if line.strip():
            anchor = line.strip()
            break

    # 在 existing 中找 anchor line 的最佳匹配
    best_i = -1
    best_r = 0.0
    for i, eline in enumerate(existing_lines):
        r = difflib.SequenceMatcher(None, anchor, eline.strip()).ratio()
        if r > best_r and r > 0.6:
            best_r = r
            best_i = i

    if best_i < 0:
        return None

    # 找到 anchor 后，对比周围行
    search_end = min(best_i + len(old_lines), len(existing_lines))
    window = existing_lines[best_i:search_end]
    window_str = "\n".join(w for w in window)
    old_str = "\n".join(l.strip() for l in old_lines[:len(window)])

    ratio = difflib.SequenceMatcher(None, old_str, window_str.strip()).ratio()
    if ratio >= 0.7:
        # 保留原缩进
        orig_indent = ""
        if existing_lines[best_i]:
            orig_indent = existing_lines[best_i][:len(existing_lines[best_i]) - len(existing_lines[best_i].lstrip())]
        indented_new = []
        for line in new_lines:
            if line.strip():
                indented_new.append(orig_indent + line.strip())
            else:
                indented_new.append("")
        result_lines = existing_lines[:best_i] + indented_new + existing_lines[search_end:]
        logger.info(f"Patcher 相似度引导匹配: ratio={ratio:.2f}, anchor_line={best_i}")
        return "\n".join(result_lines)

    return None


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
