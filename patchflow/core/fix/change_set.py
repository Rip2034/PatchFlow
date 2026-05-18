"""ChangeSet — 原子跨文件变更协调

当修复一个文件可能影响多个文件时（如修改函数签名），ChangeSet 保证：
  1. 所有相关文件一起修改，或一起回滚
  2. 自动通过 DepGraph 发现受影响的文件并扩展 scope
  3. 失败时整体回滚到修改前的状态
"""

from dataclasses import dataclass
from pathlib import Path

from patchflow.core.fix.snapshot_manager import SnapshotManager
from patchflow.utils import logger


@dataclass
class FileChange:
    file: str
    old_content: str = ""
    new_content: str = ""
    reason: str = ""

    def is_new_file(self) -> bool:
        return not self.old_content

    def entities_changed(self) -> list[tuple[str, str]]:
        old_entities = _extract_entity_names(self.old_content)
        new_entities = _extract_entity_names(self.new_content)
        changed = []
        for name, etype in new_entities:
            if (name, etype) not in old_entities:
                changed.append((name, etype))
        for name, etype in old_entities:
            if (name, etype) not in new_entities:
                changed.append((name, etype))
        return changed


class ChangeSet:
    MAX_CHANGES = 10

    def __init__(self, work_dir: str = ".", dep_graph=None):
        self.work_dir = Path(work_dir).resolve()
        self.changes: list[FileChange] = []
        self.dep_graph = dep_graph
        self.snapshot = SnapshotManager(str(self.work_dir))
        self._current_snapshot_id: str | None = None

    def add(self, file: str, new_content: str, reason: str = "") -> FileChange:
        full_path = self.work_dir / file
        old_content = ""
        if full_path.exists():
            try:
                old_content = full_path.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                pass
        change = FileChange(file=file, old_content=old_content, new_content=new_content, reason=reason)
        for i, c in enumerate(self.changes):
            if c.file == file:
                self.changes[i] = change
                return change
        self.changes.append(change)
        if len(self.changes) > self.MAX_CHANGES:
            self.changes = self.changes[-self.MAX_CHANGES:]
        return change

    def expand_with_dependents(self) -> list[str]:
        if not self.dep_graph:
            return []
        newly_added = []
        for change in self.changes:
            callers = set()
            for entity_name, _ in change.entities_changed():
                type_file = self.dep_graph.find_type(entity_name)
                if type_file:
                    for caller in self.dep_graph.direct_callers(type_file):
                        callers.add(caller)
            for caller_file in callers:
                if caller_file not in [c.file for c in self.changes] and caller_file not in newly_added:
                    newly_added.append(caller_file)
        for f in newly_added:
            self.add(f, self._read_current(f), "auto-expanded: caller of modified entity")
        if newly_added:
            logger.info(f"ChangeSet 扩展: +{len(newly_added)} 依赖文件 {newly_added}")
        return newly_added

    def begin(self) -> str:
        files = [c.file for c in self.changes if not c.is_new_file()]
        if not files:
            self._current_snapshot_id = "_empty_"
            return self._current_snapshot_id
        self._current_snapshot_id = self.snapshot.save(files)
        logger.info(f"ChangeSet 快照已创建: {self._current_snapshot_id} ({len(files)} 文件)")
        return self._current_snapshot_id

    def commit(self) -> None:
        if self._current_snapshot_id and self._current_snapshot_id != "_empty_":
            self.snapshot.commit(self._current_snapshot_id)
            logger.info("ChangeSet 已提交")
        self._current_snapshot_id = None

    def rollback(self) -> None:
        if self._current_snapshot_id and self._current_snapshot_id != "_empty_":
            self.snapshot.rollback(self._current_snapshot_id)
            logger.info("ChangeSet 已回滚")
        self._current_snapshot_id = None
        self.changes.clear()

    def apply_all(self) -> int:
        from patchflow.core.concurrency import AtomicWrite, get_file_lock_manager

        flm = get_file_lock_manager()
        applied = 0
        for change in self.changes:
            target = self.work_dir / change.file
            with flm.lock(change.file):
                try:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    AtomicWrite.write(str(target), change.new_content)
                    applied += 1
                except OSError as e:
                    logger.error(f"ChangeSet 写入失败 {change.file}: {e}")
        logger.info(f"ChangeSet 应用: {applied}/{len(self.changes)} 文件")
        return applied

    @property
    def files(self) -> list[str]:
        return [c.file for c in self.changes]

    @property
    def summary(self) -> str:
        if not self.changes:
            return "no changes"
        parts = [f"{len(self.changes)} file(s)"]
        for c in self.changes[:3]:
            parts.append(f"  {c.file}: {c.reason[:60]}")
        if len(self.changes) > 3:
            parts.append(f"  ... +{len(self.changes) - 3} more")
        return "\n".join(parts)

    def _read_current(self, file: str) -> str:
        target = self.work_dir / file
        if not target.exists():
            return ""
        try:
            return target.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            return ""


def _extract_entity_names(content: str) -> list[tuple[str, str]]:
    """从代码内容中提取实体名称和类型（轻量正则，不依赖 tree-sitter）"""
    import re
    entities = []
    patterns = [
        (r'(?:^|\n)\s*class\s+(\w+)', 'class'),
        (r'(?:^|\n)\s*def\s+(\w+)', 'function'),
        (r'(?:^|\n)\s*async\s+def\s+(\w+)', 'function'),
        (r'(?:public|private|protected)?\s*class\s+(\w+)', 'class'),
        (r'(?:public|private|protected)?\s*interface\s+(\w+)', 'interface'),
        (r'function\s+(\w+)\s*\(', 'function'),
        (r'type\s+(\w+)\s+struct', 'go_struct'),
        (r'type\s+(\w+)\s+interface', 'go_interface'),
        (r'pub\s+struct\s+(\w+)', 'rust_struct'),
        (r'pub\s+enum\s+(\w+)', 'rust_enum'),
    ]
    for pattern, etype in patterns:
        for m in re.finditer(pattern, content, re.MULTILINE):
            name = m.group(1)
            if (name, etype) not in entities:
                entities.append((name, etype))
    return entities
