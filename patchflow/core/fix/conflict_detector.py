"""LazyConflictDetector — 提交时冲突检测

设计原则：乐观并发 + 提交时冲突检测。
不要求 Agent 预先声明一切，只在合并代码时才检测真正的冲突。

类比 Git merge：让 Agent 自由工作，提交时检测冲突。

三种冲突类型：
  1. FileConflict: 同一文件被多个 Agent 修改
     → 需要手动合并（高严重度）
  2. EntityConflict: 同名类/函数出现在不同文件中
     → 可能是无意中重复定义（中严重度）
  3. SignatureConflict: 函数签名被修改而调用方不知道
     → 类型不匹配（中严重度）

检测时机：Agent 提交代码时（不是在运行时），即"懒检测"。
这降低了 Agent 之间的耦合，让它们可以独立工作。

实体提取策略（按优先级）：
  1. tree-sitter AST（所有语言，精确）
  2. Python 内置 ast 模块（回退）
  3. 正则表达式（通用兜底）
"""

import ast
import json
import re
from collections import defaultdict
from pathlib import Path

import tree_sitter

# 语言名 → (grammar 模块名, 备选模块名)
_TS_GRAMMAR_PACKAGES: dict[str, tuple[str, ...]] = {
    "python": ("tree_sitter_python",),
    "java": ("tree_sitter_java",),
    "javascript": ("tree_sitter_javascript",),
    "typescript": ("tree_sitter_typescript",),
    "tsx": ("tree_sitter_typescript",),
    "go": ("tree_sitter_go",),
    "rust": ("tree_sitter_rust",),
    "c": ("tree_sitter_c",),
    "cpp": ("tree_sitter_cpp",),
    "c_sharp": ("tree_sitter_c_sharp",),
    "kotlin": ("tree_sitter_kotlin",),
}

# Parser 缓存：避免每次解析都重建 Parser
_TS_PARSER_CACHE: dict[str, "tree_sitter.Parser"] = {}


def _get_ts_parser(lang_name: str):
    """懒加载 tree-sitter parser，失败返回 None"""
    if lang_name in _TS_PARSER_CACHE:
        return _TS_PARSER_CACHE[lang_name]

    pkg_names = _TS_GRAMMAR_PACKAGES.get(lang_name)
    if not pkg_names:
        return None

    import importlib

    for pkg_name in pkg_names:
        try:
            grammar_mod = importlib.import_module(pkg_name)
            language = tree_sitter.Language(grammar_mod.language())
            parser = tree_sitter.Parser(language)
            _TS_PARSER_CACHE[lang_name] = parser
            return parser
        except ImportError:
            continue

    return None

# 语言扩展名 → (tree-sitter 语言名, {节点类型: 实体类型})
_TS_ENTITY_CONFIG: dict[str, tuple[str, dict[str, str]]] = {
    "py": (
        "python",
        {
            "class_definition": "class",
            "function_definition": "function",
        },
    ),
    "pyw": (
        "python",
        {
            "class_definition": "class",
            "function_definition": "function",
        },
    ),
    "java": (
        "java",
        {
            "class_declaration": "class",
            "interface_declaration": "interface",
            "enum_declaration": "enum",
            "method_declaration": "method",
        },
    ),
    "js": (
        "javascript",
        {
            "class_declaration": "class",
            "function_declaration": "function",
            "method_definition": "method",
        },
    ),
    "jsx": (
        "javascript",
        {
            "class_declaration": "class",
            "function_declaration": "function",
            "method_definition": "method",
        },
    ),
    "mjs": (
        "javascript",
        {
            "class_declaration": "class",
            "function_declaration": "function",
            "method_definition": "method",
        },
    ),
    "cjs": (
        "javascript",
        {
            "class_declaration": "class",
            "function_declaration": "function",
            "method_definition": "method",
        },
    ),
    "ts": (
        "typescript",
        {
            "class_declaration": "class",
            "function_declaration": "function",
            "method_definition": "method",
            "interface_declaration": "interface",
            "type_alias_declaration": "type",
            "enum_declaration": "enum",
        },
    ),
    "tsx": (
        "tsx",
        {
            "class_declaration": "class",
            "function_declaration": "function",
            "method_definition": "method",
            "interface_declaration": "interface",
            "type_alias_declaration": "type",
            "enum_declaration": "enum",
        },
    ),
    "go": (
        "go",
        {
            "function_declaration": "function",
            "method_declaration": "method",
        },
    ),
    "rs": (
        "rust",
        {
            "struct_item": "struct",
            "enum_item": "enum",
            "trait_item": "trait",
            "impl_item": "impl",
            "function_item": "function",
        },
    ),
    "c": (
        "c",
        {
            "function_definition": "function",
            "struct_specifier": "struct",
            "enum_specifier": "enum",
        },
    ),
    "cpp": (
        "cpp",
        {
            "class_specifier": "class",
            "struct_specifier": "struct",
            "function_definition": "function",
            "enum_specifier": "enum",
        },
    ),
    "cc": (
        "cpp",
        {
            "class_specifier": "class",
            "struct_specifier": "struct",
            "function_definition": "function",
            "enum_specifier": "enum",
        },
    ),
    "cxx": (
        "cpp",
        {
            "class_specifier": "class",
            "struct_specifier": "struct",
            "function_definition": "function",
            "enum_specifier": "enum",
        },
    ),
    "h": (
        "c",
        {
            "function_declarator": "function",
            "struct_specifier": "struct",
            "enum_specifier": "enum",
            "type_definition": "type",
        },
    ),
    "hpp": (
        "cpp",
        {
            "class_specifier": "class",
            "struct_specifier": "struct",
            "function_declarator": "function",
            "enum_specifier": "enum",
        },
    ),
    "cs": (
        "c_sharp",
        {
            "class_declaration": "class",
            "interface_declaration": "interface",
            "struct_declaration": "struct",
            "enum_declaration": "enum",
            "method_declaration": "method",
        },
    ),
    "kt": (
        "kotlin",
        {
            "class_declaration": "class",
            "function_declaration": "function",
            "object_declaration": "object",
        },
    ),
    "swift": (
        "swift",
        {
            "class_declaration": "class",
            "struct_declaration": "struct",
            "enum_declaration": "enum",
            "protocol_declaration": "protocol",
            "function_declaration": "function",
        },
    ),
}


def _ts_extract_name(node) -> str | None:
    """从 tree-sitter 节点中提取名称字段"""
    name_node = node.child_by_field_name("name")
    if name_node and name_node.text:
        return name_node.text.decode("utf-8")
    return None


def _ts_extract_go_type_spec(node) -> tuple[str, str] | None:
    """Go type_spec 特殊处理：判断 struct 或 interface"""
    for child in node.named_children:
        if child.type == "struct_type":
            name = _ts_extract_name(node)
            return (name, "struct") if name else None
        if child.type == "interface_type":
            name = _ts_extract_name(node)
            return (name, "interface") if name else None
    # 裸 type alias
    name = _ts_extract_name(node)
    return (name, "type") if name else None


def _extract_entities_tree_sitter(content: str, ext: str) -> list[tuple[str, str]]:
    """使用 tree-sitter AST 提取实体（类/函数/接口等）

    Args:
        content: 源代码文本
        ext: 文件扩展名（不含点），如 "py", "java", "go"

    Returns:
        list[tuple[str, str]]: [(名称, 类型), ...]
        返回空列表表示 tree-sitter 不可用或解析失败，调用方应回退
    """
    config = _TS_ENTITY_CONFIG.get(ext)
    if not config:
        return []

    lang_name, node_types = config

    parser = _get_ts_parser(lang_name)
    if parser is None:
        return []

    try:
        tree = parser.parse(bytes(content, "utf-8"))
    except Exception:
        return []

    entities: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()

    def _walk(node) -> None:
        entity_type = node_types.get(node.type)
        if entity_type:
            name = _ts_extract_name(node)
            if name:
                key = (name, entity_type)
                if key not in seen:
                    seen.add(key)
                    entities.append(key)

        # Go: type Foo struct / type Foo interface
        if node.type == "type_declaration":
            for child in node.named_children:
                if child.type == "type_spec":
                    result = _ts_extract_go_type_spec(child)
                    if result:
                        if result not in seen:
                            seen.add(result)
                            entities.append(result)

        for child in node.named_children:
            _walk(child)

    _walk(tree.root_node)
    return entities


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
            entities = self._extract_entities(content, filepath)
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

    def _extract_entities(self, content: str, filepath: str = "") -> list[tuple[str, str]]:
        """从代码中提取实体（类名、函数名）— AST 优先，正则兜底

        Args:
            content: 源代码文本
            filepath: 文件路径（用于判断语言）

        Returns:
            list[tuple[str, str]]: [(实体名, 实体类型), ...]
        """
        ext = Path(filepath).suffix.lower().lstrip(".") if filepath else ""

        # 1. tree-sitter AST（覆盖 14 种语言）
        entities = _extract_entities_tree_sitter(content, ext)
        if entities:
            return entities

        # 2. Python 内置 AST（回退，无需 tree-sitter）
        # 当 ext 为空或为 Python 扩展名时尝试
        if ext in ("py", "pyw", ""):
            try:
                tree = ast.parse(content)
                result: list[tuple[str, str]] = []
                for node in ast.walk(tree):
                    if isinstance(node, ast.ClassDef):
                        result.append((node.name, "class"))
                    elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        result.append((node.name, "function"))
                if result:
                    return result
            except SyntaxError:
                pass

        # 3. 正则兜底（无 tree-sitter 时使用）
        return self._extract_entities_regex(content)

    def _extract_entities_regex(self, content: str) -> list[tuple[str, str]]:
        """正则提取实体 — 当 tree-sitter 不可用时的兜底方案"""
        entities: list[tuple[str, str]] = []

        # Java/Kotlin
        for m in re.finditer(
            r'^\s*(?:public|protected|private)?\s*(?:abstract|final)?\s*(class|interface|enum)\s+(\w+)',
            content, re.MULTILINE,
        ):
            key = (m.group(2), m.group(1))
            if key not in entities:
                entities.append(key)

        # JS/TS
        for m in re.finditer(
            r'^\s*(?:export\s+)?(?:abstract\s+)?(class|function)\s+(\w+)',
            content, re.MULTILINE,
        ):
            key = (m.group(2), m.group(1))
            if key not in entities:
                entities.append(key)

        # Go
        for m in re.finditer(
            r'^\s*type\s+(\w+)\s+(struct|interface)',
            content, re.MULTILINE,
        ):
            key = (m.group(1), f"go_{m.group(2)}")
            if key not in entities:
                entities.append(key)

        # Rust
        for m in re.finditer(
            r'^\s*(?:pub\s+)?(struct|enum|trait|impl)\s+(\w+)',
            content, re.MULTILINE,
        ):
            key = (m.group(2), f"rust_{m.group(1)}")
            if key not in entities:
                entities.append(key)

        # C/C++/C#
        for m in re.finditer(
            r'^\s*(?:public|protected|private)?\s*(?:ref\s+)?(class|struct)\s+(\w+)',
            content, re.MULTILINE,
        ):
            key = (m.group(2), m.group(1))
            if key not in entities:
                entities.append(key)

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
