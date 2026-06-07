"""Memory — 文件式持久化记忆系统

从 Claude Code 的 Memory 系统移植：
  - 每个记忆是一个文件（Markdown + YAML frontmatter）
  - MEMORY.md 索引文件
  - 支持类型: user / feedback / project / reference
  - 引用链接: [[other-memory-name]]

与 FixMemoryBank 的关系：
  FixMemoryBank: 专注 fix 模式（错误签名→策略），适合机器匹配
  Memory: 通用记忆（用户偏好、项目约定、参考链接），适合 LLM 上下文

目录结构：
  .patchflow/memory/
  ├── MEMORY.md           # 索引文件
  ├── project-style.md    # 代码风格约定
  ├── user-prefs.md       # 用户偏好
  └── fix-recipe-xxx.md   # 修复配方

使用方式：
    from patchflow.core.memory import MemoryStore

    store = MemoryStore(work_dir=".")
    store.remember("project-style", "Use snake_case for Python", type="project")
    store.recall("style")  # 模糊搜索
"""

import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

from patchflow.utils import logger


# ═══════════════════════════════════════════════════════════
# 数据结构
# ═══════════════════════════════════════════════════════════

@dataclass
class Memory:
    """单条记忆"""
    name: str = ""                # short-kebab-case-slug
    description: str = ""         # one-line summary (用于搜索匹配)
    type: str = "project"         # user | feedback | project | reference
    content: str = ""             # 正文（Markdown）
    metadata: dict = field(default_factory=dict)
    created_at: float = 0.0
    updated_at: float = 0.0

    def to_markdown(self) -> str:
        """序列化为 Markdown + YAML frontmatter"""
        lines = [
            "---",
            f"name: {self.name}",
            f"description: {self.description}",
            f"type: {self.type}",
        ]
        if self.metadata:
            lines.append("metadata:")
            for k, v in self.metadata.items():
                lines.append(f"  {k}: {json.dumps(v)}")
        lines.extend([
            "---",
            "",
            self.content,
        ])
        return "\n".join(lines)

    @staticmethod
    def from_markdown(text: str) -> "Memory | None":
        """从 Markdown + YAML frontmatter 反序列化"""
        if not text.startswith("---"):
            return None

        parts = text.split("---", 2)
        if len(parts) < 3:
            return None

        frontmatter_text = parts[1].strip()
        content = parts[2].strip()

        # 简易 YAML 解析
        meta = {}
        for line in frontmatter_text.split("\n"):
            line = line.strip()
            if ":" in line:
                key, _, value = line.partition(":")
                meta[key.strip()] = value.strip()
            elif line.startswith("  "):
                # 缩进继续（metadata 子字段）
                if ":" in line:
                    k, _, v = line.strip().partition(":")
                    if "metadata" not in meta:
                        meta["metadata"] = {}
                    try:
                        meta["metadata"][k.strip()] = json.loads(v.strip())
                    except json.JSONDecodeError:
                        meta["metadata"][k.strip()] = v.strip()

        if "name" not in meta:
            return None

        return Memory(
            name=meta.get("name", ""),
            description=meta.get("description", ""),
            type=meta.get("type", "project"),
            content=content,
            metadata=meta.get("metadata", {}),
            created_at=time.time(),
            updated_at=time.time(),
        )

    def to_llm_context(self) -> str:
        """格式化为 LLM 上下文摘要"""
        return f"[{self.type}] {self.description}: {self.content[:300]}"


# ═══════════════════════════════════════════════════════════
# MemoryStore
# ═══════════════════════════════════════════════════════════

class MemoryStore:
    """文件式记忆存储

    每个记忆一个 .md 文件，MEMORY.md 作为索引。
    """

    MAX_MEMORIES = 50
    MAX_CONTENT_SIZE = 5000  # 单条记忆最大字符数

    def __init__(self, work_dir: str = "."):
        self._work_dir = Path(work_dir).resolve()
        self._memory_dir = self._work_dir / ".patchflow" / "memory"
        self._memory_dir.mkdir(parents=True, exist_ok=True)
        self._index: dict[str, Memory] = {}
        self._load_index()

    @property
    def count(self) -> int:
        return len(self._index)

    # ── 索引 ──────────────────────────────────────────

    def _index_path(self) -> Path:
        return self._memory_dir / "MEMORY.md"

    def _load_index(self):
        """从 MEMORY.md 加载索引"""
        idx_path = self._index_path()
        if not idx_path.exists():
            return

        try:
            text = idx_path.read_text(encoding="utf-8", errors="replace")
            for line in text.split("\n"):
                # 格式: - [Title](file.md) — hook
                m = re.match(r'-\s+\[([^\]]+)\]\(([^)]+\.md)\)\s*—?\s*(.*)', line)
                if m:
                    title, filename, description = m.groups()
                    slug = filename.replace(".md", "")
                    memory_file = self._memory_dir / filename
                    if memory_file.exists():
                        mem = Memory.from_markdown(
                            memory_file.read_text(encoding="utf-8", errors="replace")
                        )
                        if mem:
                            self._index[slug] = mem
                            continue
                    # 文件不存在，用索引信息构建
                    self._index[slug] = Memory(
                        name=slug,
                        description=description.strip() or title,
                        type="project",
                        content="",
                    )

            logger.debug(f"[Memory] Loaded {len(self._index)} memories from index")
        except Exception as e:
            logger.warn(f"[Memory] Index load failed: {e}")

    def _save_index(self):
        """更新 MEMORY.md 索引"""
        lines = []
        for slug, mem in sorted(self._index.items()):
            short_desc = mem.description[:60].replace("\n", " ")
            lines.append(f"- [{mem.description[:40] or slug}]({slug}.md) — {short_desc}")

        self._index_path().write_text(
            "\n".join(lines) + "\n",
            encoding="utf-8",
        )

    def _memory_path(self, name: str) -> Path:
        """获取记忆文件路径"""
        safe_name = re.sub(r'[^a-zA-Z0-9_.-]', '-', name.lower())
        return self._memory_dir / f"{safe_name}.md"

    # ── CRUD ──────────────────────────────────────────

    def remember(
        self,
        name: str,
        content: str,
        description: str = "",
        type: str = "project",
        metadata: dict | None = None,
    ) -> Memory:
        """写入一条记忆

        Args:
            name: 短名（kebab-case slug）
            content: 正文内容（Markdown）
            description: 一行摘要（用于搜索匹配）
            type: user | feedback | project | reference
            metadata: 附加元数据
        """
        # 检查是否已存在同名记忆 → 更新
        existing = self._find_by_name(name)
        if existing:
            existing.description = description or existing.description
            existing.content = content[:self.MAX_CONTENT_SIZE]
            existing.type = type
            existing.updated_at = time.time()
            if metadata:
                existing.metadata.update(metadata)
            self._write_memory_file(existing)
            self._save_index()
            logger.info(f"[Memory] Updated: '{name}'")
            return existing

        # 检查数量上限
        if len(self._index) >= self.MAX_MEMORIES:
            # 移除最旧的
            oldest = min(
                self._index.values(),
                key=lambda m: m.updated_at or m.created_at,
            )
            self.forget(oldest.name)
            logger.info(f"[Memory] Evicted oldest: '{oldest.name}'")

        mem = Memory(
            name=name,
            description=description or content[:60].strip(),
            type=type,
            content=content[:self.MAX_CONTENT_SIZE],
            metadata=metadata or {},
            created_at=time.time(),
            updated_at=time.time(),
        )

        self._write_memory_file(mem)
        self._index[name] = mem
        self._save_index()

        logger.info(f"[Memory] Stored: '{name}' ({type})")
        return mem

    def recall(self, query: str, limit: int = 5) -> list[Memory]:
        """模糊搜索记忆

        搜索范围: name + description + content 的前 200 字符
        匹配方式: 分词 + 包含匹配

        Args:
            query: 搜索词
            limit: 返回数量

        Returns:
            按相关度排序的记忆列表
        """
        query_lower = query.lower()
        query_words = set(query_lower.split())

        scored = []
        for mem in self._index.values():
            score = 0

            # name 精确匹配
            if query_lower == mem.name.lower():
                score += 100
            elif query_lower in mem.name.lower():
                score += 50

            # description 匹配
            if query_lower in mem.description.lower():
                score += 40
            for word in query_words:
                if word in mem.description.lower():
                    score += 15

            # content 匹配
            content_lower = mem.content[:300].lower()
            if query_lower in content_lower:
                score += 30
            for word in query_words:
                if word in content_lower:
                    score += 8

            # type 匹配
            if query_lower in mem.type:
                score += 10

            # 解析内容中的 [[links]]
            linked = re.findall(r'\[\[([^\]]+)\]\]', mem.content)
            if any(query_lower in link.lower() for link in linked):
                score += 20

            if score > 0:
                scored.append((score, mem))

        scored.sort(key=lambda x: x[0], reverse=True)
        return [m for _, m in scored[:limit]]

    def get(self, name: str) -> Memory | None:
        """按名称获取记忆"""
        return self._find_by_name(name)

    def forget(self, name: str) -> bool:
        """删除记忆"""
        mem = self._find_by_name(name)
        if mem is None:
            return False

        # 删除文件
        file_path = self._memory_path(name)
        if file_path.exists():
            file_path.unlink(missing_ok=True)

        # 从索引移除
        del self._index[mem.name]
        self._save_index()

        logger.info(f"[Memory] Forgotten: '{name}'")
        return True

    def list_by_type(self, mem_type: str) -> list[Memory]:
        """按类型列出记忆"""
        return [m for m in self._index.values() if m.type == mem_type]

    def recall_all(self, limit: int = 20) -> list[Memory]:
        """召回最近更新的记忆"""
        return sorted(
            self._index.values(),
            key=lambda m: m.updated_at or m.created_at,
            reverse=True,
        )[:limit]

    # ── LLM 上下文 ────────────────────────────────────

    def compile_context(self, query: str = "", max_chars: int = 3000) -> str:
        """编译为 LLM 上下文（注入到 system prompt）

        Args:
            query: 搜索词（空 = 最近记忆）
            max_chars: 最大字符数

        Returns:
            格式化的上下文字符串
        """
        if query:
            memories = self.recall(query, limit=10)
        else:
            memories = self.recall_all(lookup=10)

        if not memories:
            return ""

        parts = ["## Relevant Memories"]
        total = 0
        for mem in memories:
            entry = f"- [{mem.type}] {mem.description}"
            if mem.content:
                snippet = mem.content[:200].replace("\n", " ")
                entry += f": {snippet}"
            if total + len(entry) > max_chars:
                parts.append(f"... ({len(memories) - len(parts) + 1} more)")
                break
            parts.append(entry)
            total += len(entry)

        return "\n".join(parts)

    # ── 内部方法 ──────────────────────────────────────

    def _find_by_name(self, name: str) -> Memory | None:
        """按 name 精确查找"""
        # 直接匹配
        if name in self._index:
            return self._index[name]
        # 标准化匹配
        safe = re.sub(r'[^a-zA-Z0-9_.-]', '-', name.lower())
        if safe in self._index:
            return self._index[safe]
        return None

    def _write_memory_file(self, mem: Memory):
        """写入记忆文件"""
        file_path = self._memory_path(mem.name)
        file_path.write_text(mem.to_markdown(), encoding="utf-8")
