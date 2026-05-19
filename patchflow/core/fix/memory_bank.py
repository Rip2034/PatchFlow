"""FixMemoryBank — 跨会话修复记忆库

将修复结果持久化到 .patchflow/fix_memory.json，让系统从历史修复中学习：
  - 查询相似历史修复作为 LLM 上下文
  - 避免重复已知失败的策略
  - LRU 驱逐：最多 100 条，失败记录优先清除
"""

import json
import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from patchflow.utils import logger


@dataclass
class FixMemory:
    error_signature: str
    error_type: str
    fix_pattern: str
    file_context: list[str] = field(default_factory=list)
    success: bool = False
    strategy_used: str = ""
    timestamp: float = 0.0
    access_count: int = 0


class FixMemoryBank:
    MAX_ENTRIES = 100
    STORAGE_FILENAME = "fix_memory.json"

    def __init__(self, work_dir: str = "."):
        self.work_dir = Path(work_dir).resolve()
        self.storage_path = self.work_dir / ".patchflow" / self.STORAGE_FILENAME
        self._entries: list[FixMemory] = []
        self._lock = threading.Lock()

    def generate_signature(self, error_type: str, root_cause: str) -> str:
        tokens = re.findall(r'[a-zA-Z]{3,}', root_cause.lower())
        key_tokens = tokens[:3]
        if not key_tokens:
            key_tokens = [re.sub(r'[^a-zA-Z]+', '', root_cause.lower())[:20]]
        return f"{error_type}:{'_'.join(key_tokens)}"

    def add(self, error_type: str, root_cause: str, fix_pattern: str,
            file_paths: list[str], success: bool, strategy_used: str = "") -> FixMemory:
        with self._lock:
            sig = self.generate_signature(error_type, root_cause)
            memory = FixMemory(
                error_signature=sig,
                error_type=error_type,
                fix_pattern=fix_pattern[:150],
                file_context=file_paths[:3],
                success=success,
                strategy_used=strategy_used,
                timestamp=time.time(),
                access_count=0,
            )
            self._entries.append(memory)
            while len(self._entries) > self.MAX_ENTRIES:
                self._evict_lru()
            return memory

    def query(self, error_type: str, root_cause: str, limit: int = 5) -> list[FixMemory]:
        with self._lock:
            sig = self.generate_signature(error_type, root_cause)
            sig_parts = set(sig.split(":")[-1].split("_"))
            scored = []
            for m in self._entries:
                score = 0
                if m.error_type == error_type:
                    score += 10
                m_parts = set(m.error_signature.split(":")[-1].split("_"))
                overlap = len(sig_parts & m_parts)
                score += overlap * 3
                if m.success:
                    score += 8  # 成功的模式大幅加分
                else:
                    score -= 5  # 失败的模式降权
                if score > 0:
                    scored.append((score, m.timestamp, m))
            scored.sort(key=lambda x: (x[0], x[1]), reverse=True)
            results = []
            for _, _, m in scored[:limit]:
                m.access_count += 1
                results.append(m)
            return results

    def should_skip(self, error_type: str, root_cause: str,
                    strategy_name: str = "") -> tuple[bool, str]:
        with self._lock:
            sig = self.generate_signature(error_type, root_cause)
            failures = [m for m in self._entries
                         if m.error_signature == sig and not m.success
                         and (not strategy_name or m.strategy_used == strategy_name)]
            if len(failures) >= 2:
                return True, f"跨会话已有 {len(failures)} 次失败: {sig} (strategy={strategy_name})"
            return False, ""

    def get_avoid_patterns(self, error_type: str, root_cause: str) -> list[str]:
        """返回应避免的修复模式（从历史失败中提取）"""
        with self._lock:
            sig = self.generate_signature(error_type, root_cause)
            sig_parts = set(sig.split(":")[-1].split("_"))
            avoid = []
            for m in self._entries:
                if m.success:
                    continue
                m_parts = set(m.error_signature.split(":")[-1].split("_"))
                overlap = len(sig_parts & m_parts)
                if m.error_type == error_type or overlap >= 2:
                    avoid.append(m.fix_pattern[:100])
            return avoid[:3]

    def load(self) -> None:
        with self._lock:
            if not self.storage_path.exists():
                return
            try:
                data = json.loads(self.storage_path.read_text(encoding="utf-8"))
                self._entries = []
                for item in data:
                    self._entries.append(FixMemory(
                        error_signature=item.get("error_signature", ""),
                        error_type=item.get("error_type", ""),
                        fix_pattern=item.get("fix_pattern", "")[:150],
                        file_context=item.get("file_context", [])[:3],
                        success=item.get("success", False),
                        strategy_used=item.get("strategy_used", ""),
                        timestamp=item.get("timestamp", 0.0),
                        access_count=item.get("access_count", 0),
                    ))
                logger.info(f"MemoryBank 加载: {len(self._entries)} 条记录")
            except (json.JSONDecodeError, KeyError, OSError) as e:
                logger.warn(f"MemoryBank 加载失败: {e}，重置为空")
                self._entries = []

    def save(self) -> None:
        # 先快照数据，再写文件（减少锁持有时间）
        with self._lock:
            data = []
            for m in self._entries:
                data.append({
                    "error_signature": m.error_signature,
                    "error_type": m.error_type,
                    "fix_pattern": m.fix_pattern[:150],
                    "file_context": m.file_context[:3],
                    "success": m.success,
                    "strategy_used": m.strategy_used,
                    "timestamp": m.timestamp,
                    "access_count": m.access_count,
                })
        self.storage_path.parent.mkdir(parents=True, exist_ok=True)
        self.storage_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    def _evict_lru(self) -> None:
        if not self._entries:
            return
        self._entries.sort(key=lambda m: (m.timestamp, m.success))
        self._entries.pop(0)

    def summary(self) -> str:
        with self._lock:
            if not self._entries:
                return "memory bank: empty"
            success_count = sum(1 for m in self._entries if m.success)
            fail_count = len(self._entries) - success_count
            return f"memory bank: {len(self._entries)} entries ({success_count} success, {fail_count} fail)"

    @property
    def entry_count(self) -> int:
        with self._lock:
            return len(self._entries)

    def clear(self) -> None:
        with self._lock:
            self._entries = []
            if self.storage_path.exists():
                self.storage_path.unlink(missing_ok=True)
