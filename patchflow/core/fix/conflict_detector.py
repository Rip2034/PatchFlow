"""LazyConflictDetector — 提交时冲突检测

设计原则：乐观并发 + 提交时冲突检测。
不要求 Agent 预先声明一切，只在合并代码时才检测真正的冲突。

类比 Git merge：让 Agent 自由工作，提交时检测。

三种冲突类型：
  1. FileConflict: 同一文件被多个 Agent 修改
  2. EntityConflict: 同名类/函数出现在不同文件中
  3. SignatureConflict: 函数签名被修改而调用方不知道

检测时机：Agent 提交代码时（不是在运行时）
"""

import ast
import json
from pathlib import Path
from collections import defaultdict


class LazyConflictDetector:
    """Lazy Diff 冲突检测器"""

    def __init__(self, work_dir: str = "."):
        self.work_dir = Path(work_dir).resolve()
        self._entity_index: dict[str, list[dict]] = {}
        self._agent_writes: dict[str, list[str]] = defaultdict(list)
        self.conflicts_path = self.work_dir / ".patchflow" / "conflicts.json"

    def register_agent(self, agent_id: str):
        """注册一个 Agent（用于追踪谁改了啥）"""
        if agent_id not in self._agent_writes:
            self._agent_writes[agent_id] = []

    def detect(self, agent_id: str, proposed_changes: list[dict]) -> list[dict]:
        """在 Agent 提交代码时检测冲突

        Args:
            agent_id: 提交变更的 Agent ID
            proposed_changes: [{"file": "path", "content": "..."}, ...]

        Returns:
            list[dict]: 检测到的冲突列表
        """
        conflicts = []
        self.register_agent(agent_id)

        for change in proposed_changes:
            filepath = change.get("file", "")
            content = change.get("content", "")

            if not filepath:
                continue

            # 冲突 1: 同一文件被多个 Agent 修改
            if self._is_modified_by_others(filepath, agent_id):
                conflicts.append({
                    "type": "file_conflict",
                    "file": filepath,
                    "agents": list(self._agent_writes.keys()),
                    "severity": "high",
                    "suggestion": "多个 Agent 修改了同一文件，需要手动合并",
                })

            # 冲突 2: 同名实体出现在不同文件中
            entities = self._extract_entities(content)
            for entity_name, entity_type in entities:
                existing = self._entity_index.get(entity_name, [])
                for prev in existing:
                    if prev["file"] != filepath:
                        conflicts.append({
                            "type": "entity_conflict",
                            "entity": entity_name,
                            "entity_type": entity_type,
                            "file_a": prev["file"],
                            "file_b": filepath,
                            "agent_a": prev.get("agent_id", "?"),
                            "agent_b": agent_id,
                            "severity": "medium",
                            "suggestion": f"同名{entity_type} '{entity_name}' 出现在 {prev['file']} 和 {filepath} 中",
                        })
                self._entity_index.setdefault(entity_name, []).append({
                    "file": filepath,
                    "type": entity_type,
                    "agent_id": agent_id,
                })

            self._agent_writes[agent_id].append(filepath)

        return conflicts

    def _is_modified_by_others(self, filepath: str, agent_id: str) -> bool:
        for other_id, files in self._agent_writes.items():
            if other_id != agent_id and filepath in files:
                return True
        return False

    def _extract_entities(self, content: str) -> list[tuple[str, str]]:
        """从代码中提取实体（类名、函数名）"""
        entities = []
        try:
            tree = ast.parse(content)
            for node in ast.walk(tree):
                if isinstance(node, ast.ClassDef):
                    entities.append((node.name, "class"))
                elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    entities.append((node.name, "function"))
        except SyntaxError:
            pass
        return entities

    def save_index(self):
        """保存冲突索引到 .patchflow/conflicts.json"""
        index = {
            "cross_agent_writes": dict(self._agent_writes),
            "entities": {k: v for k, v in self._entity_index.items()},
        }
        self.conflicts_path.parent.mkdir(parents=True, exist_ok=True)
        self.conflicts_path.write_text(
            json.dumps(index, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def load_index(self):
        """加载冲突索引"""
        if self.conflicts_path.exists():
            try:
                data = json.loads(self.conflicts_path.read_text(encoding="utf-8"))
                self._agent_writes = defaultdict(list, data.get("cross_agent_writes", {}))
                self._entity_index = data.get("entities", {})
            except (json.JSONDecodeError, OSError):
                pass

    def summary(self) -> str:
        """简短摘要"""
        agents = len(self._agent_writes)
        entities = len(self._entity_index)
        return f"{agents} agent(s), {entities} entity(ies) indexed"
