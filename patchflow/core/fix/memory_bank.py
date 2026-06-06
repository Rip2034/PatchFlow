"""FixMemoryBank — 跨会话修复记忆库（V0.5 语义增强版）

将修复结果持久化到 .patchflow/fix_memory.json，让系统从历史修复中学习：
  - 查询相似历史修复作为 LLM 上下文
  - 避免重复已知失败的策略
  - LRU 驱逐：最多 100 条，失败记录优先清除

V0.5 增强：
  - 语义签名：基于错误类型 + 结构化 token + 归一化的模式
  - 编辑距离匹配：用 Levenshtein 距离计算 root_cause 相似度
  - 文件路径相似度：相同文件的修复优先匹配
  - 模式分组：同类型错误的同类 root_cause 归入一个模式组
"""

import json
import re
import threading
import time
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from patchflow.utils import logger


@dataclass
class FixMemory:
    error_signature: str
    error_pattern: str = ""  # V0.5: 归一化错误模式
    error_type: str = ""
    root_cause: str = ""     # V0.5: 存储原始 root_cause 用于编辑距离计算
    fix_pattern: str = ""
    file_context: list[str] = field(default_factory=list)
    success: bool = False
    strategy_used: str = ""
    timestamp: float = 0.0
    access_count: int = 0
    score: float = 0.0


class FixMemoryBank:
    MAX_ENTRIES = 100
    STORAGE_FILENAME = "fix_memory.json"

    def __init__(self, work_dir: str = "."):
        self.work_dir = Path(work_dir).resolve()
        self.storage_path = self.work_dir / ".patchflow" / self.STORAGE_FILENAME
        self._entries: list[FixMemory] = []
        self._lock = threading.Lock()

    @staticmethod
    def _normalize_text(text: str) -> str:
        """归一化文本：小写、去标点、去引号内容、去数字"""
        # 移除引号中的具体值（'user_id' → 'KEY', "123" → NUM）
        text = re.sub(r"'[^']*'", "'KEY'", text)
        text = re.sub(r'"[^"]*"', '"KEY"', text)
        # 替换数字为 NUM
        text = re.sub(r'\b\d+\b', 'NUM', text)
        # 小写、去标点
        text = re.sub(r'[^\w\s]', ' ', text.lower())
        # 合并空白
        text = re.sub(r'\s+', ' ', text).strip()
        return text

    @staticmethod
    def _edit_distance(a: str, b: str) -> float:
        """计算归一化后的 Levenshtein 距离相似度 (0.0–1.0)"""
        if not a or not b:
            return 0.0
        a = FixMemoryBank._normalize_text(a)
        b = FixMemoryBank._normalize_text(b)
        if not a or not b:
            return 0.0

        # Levenshtein distance
        m, n = len(a), len(b)
        if m == 0 or n == 0:
            return 0.0
        # 只用单行 dp，限制空间复杂度
        prev = list(range(n + 1))
        curr = [0] * (n + 1)
        for i in range(1, m + 1):
            curr[0] = i
            for j in range(1, n + 1):
                cost = 0 if a[i - 1] == b[j - 1] else 1
                curr[j] = min(prev[j] + 1, curr[j - 1] + 1, prev[j - 1] + cost)
            prev, curr = curr, prev
        max_len = max(m, n)
        return 1.0 - (prev[n] / max_len)

    def generate_signature(self, error_type: str, root_cause: str) -> str:
        """V0.5: 生成结构化语义签名

        签名包含：
          - 错误类型（精确匹配）
          - 错误模式（归一化后的错误描述，用于相似度匹配）
          - 核心关键词（用于快速召回）
        """
        normalized = self._normalize_text(root_cause)
        # 提取错误模式（如 KeyError → key_error:KEY）
        error_class = ""
        m = re.search(r'(\w+Error|\w+Exception|\w+Warning)', error_type + " " + root_cause, re.IGNORECASE)
        if m:
            error_class = m.group(1).lower()
        # 提取关键概念词（长度 ≥ 3 的非停用词）
        stop_words = {"the", "and", "for", "not", "was", "has", "had", "did", "can", "are",
                      "that", "this", "with", "from", "when", "where", "which", "what",
                      "line", "file", "error", "code", "num"}
        tokens = re.findall(r'[a-zA-Z]{3,}', normalized)
        key_tokens = [t for t in tokens if t not in stop_words][:4]
        if not key_tokens:
            key_tokens = [normalized[:20].replace(" ", "_") or "unknown"]
        return f"{error_type}:{error_class}:{'_'.join(key_tokens)}"

    def add(self, error_type: str, root_cause: str, fix_pattern: str,
            file_paths: list[str], success: bool, strategy_used: str = "") -> FixMemory:
        with self._lock:
            sig = self.generate_signature(error_type, root_cause)
            pattern = self._normalize_text(root_cause)[:120]
            memory = FixMemory(
                error_signature=sig,
                error_pattern=pattern,
                error_type=error_type,
                root_cause=root_cause[:200],
                fix_pattern=fix_pattern[:150],
                file_context=file_paths[:5],
                success=success,
                strategy_used=strategy_used,
                timestamp=time.time(),
                access_count=0,
                score=0.0,
            )
            self._entries.append(memory)
            while len(self._entries) > self.MAX_ENTRIES:
                self._evict_lru()
            return memory

    def query(self, error_type: str, root_cause: str, limit: int = 5,
              file_paths: list[str] | None = None) -> list[FixMemory]:
        """V0.5: 语义增强查询

        评分因素（权重累加）：
          1. 错误类型精确匹配: +15
          2. root_cause 编辑距离相似度: 0–20 分（归一化后计算）
          3. 错误模式关键词重叠: 每个重叠 +5
          4. 文件路径相似度: 0–10 分（相同文件/目录加分）
          5. 成功记忆加分: +12，失败记忆扣分: -8
          6. 时间衰减: 最近 1 小时内的 +5，24h 内 +3，7d+ 不衰减
        """
        with self._lock:
            query_sig = self.generate_signature(error_type, root_cause)
            query_normalized = self._normalize_text(root_cause)
            sig_parts = set(query_sig.split(":")[-1].split("_"))
            now = time.time()

            scored = []
            MIN_SCORE = 10.0
            for m in self._entries:
                score = 0.0
                type_matched = False

                # 1. 错误类型匹配（核心过滤条件）
                if m.error_type == error_type:
                    score += 15
                    type_matched = True
                elif m.error_type and error_type:
                    type_sim = self._edit_distance(m.error_type, error_type)
                    if type_sim > 0.5:
                        score += type_sim * 8
                        type_matched = True

                # 2. root_cause 编辑距离相似度
                if m.root_cause:
                    edit_sim = self._edit_distance(m.root_cause, root_cause)
                    score += edit_sim * 20
                elif m.error_pattern:
                    edit_sim = self._edit_distance(m.error_pattern, query_normalized)
                    score += edit_sim * 15

                # ── 质量门控：类型不匹配 且 root_cause 相似度 < 0.3 → 跳过 ──
                if not type_matched:
                    cause_sim = self._edit_distance(m.root_cause or "", root_cause)
                    if cause_sim < 0.3:
                        continue

                # 3. 关键词重叠
                m_parts = set(m.error_signature.split(":")[-1].split("_")) if m.error_signature else set()
                overlap = len(sig_parts & m_parts)
                score += overlap * 5

                # 4. 文件路径相似度
                if file_paths and m.file_context:
                    path_overlap = len(set(file_paths) & set(m.file_context))
                    score += path_overlap * 10
                    if path_overlap == 0:
                        dirs_query = {Path(f).parent for f in file_paths if f}
                        dirs_mem = {Path(f).parent for f in m.file_context if f}
                        if dirs_query & dirs_mem:
                            score += 3

                # 5. 成功/失败加权（类型不匹配时降权）
                if m.success:
                    score += 12 if type_matched else 6
                else:
                    score -= 8 if type_matched else 3

                # 6. 时间衰减
                age_hours = (now - m.timestamp) / 3600
                if age_hours < 1:
                    score += 5 if type_matched else 2
                elif age_hours < 24:
                    score += 3 if type_matched else 1

                # 7. 访问频次微调
                score += min(m.access_count * 0.5, 3)

                if score >= MIN_SCORE:
                    m.score = score
                    scored.append((score, m.timestamp, m))

            scored.sort(key=lambda x: (x[0], x[1]), reverse=True)
            results = []
            for _, _, m in scored[:limit]:
                m.access_count += 1
                results.append(m)
            return results

    def should_skip(self, error_type: str, root_cause: str,
                    strategy_name: str = "") -> tuple[bool, str]:
        """V0.5: 用编辑距离判断是否应跳过（不只是精确签名匹配）"""
        with self._lock:
            sig = self.generate_signature(error_type, root_cause)
            # 精确签名匹配的失败
            exact_failures = [m for m in self._entries
                              if m.error_signature == sig and not m.success
                              and (not strategy_name or m.strategy_used == strategy_name)]
            if len(exact_failures) >= 2:
                return True, f"跨会话已有 {len(exact_failures)} 次精确匹配失败: {sig} (strategy={strategy_name})"

            # 高相似度失败（编辑距离 > 0.85）
            similar_failures = []
            for m in self._entries:
                if m.success:
                    continue
                if m.error_type != error_type:
                    continue
                if strategy_name and m.strategy_used != strategy_name:
                    continue
                if m.root_cause:
                    sim = self._edit_distance(m.root_cause, root_cause)
                    if sim > 0.85:
                        similar_failures.append((sim, m))
            if len(similar_failures) >= 3:
                return True, f"跨会话已有 {len(similar_failures)} 次高相似度失败 (sim>{0.85})"
            return False, ""

    def get_avoid_patterns(self, error_type: str, root_cause: str) -> list[str]:
        """V0.5: 返回应避免的修复模式（用编辑距离找相似失败）"""
        with self._lock:
            sig_parts = set(self.generate_signature(error_type, root_cause).split(":")[-1].split("_"))
            avoid = []
            for m in self._entries:
                if m.success:
                    continue
                if m.error_type != error_type:
                    continue
                # 用编辑距离 + 关键词重叠评估
                edit_sim = self._edit_distance(m.root_cause, root_cause) if m.root_cause else 0
                m_parts = set(m.error_signature.split(":")[-1].split("_")) if m.error_signature else set()
                overlap = len(sig_parts & m_parts)
                if edit_sim > 0.7 or overlap >= 2:
                    avoid.append(m.fix_pattern[:100])
            return avoid[:3]

    def load(self) -> None:
        with self._lock:
            if not self.storage_path.exists():
                return
            try:
                data = json.loads(self.storage_path.read_text(encoding="utf-8", errors="replace"))
                self._entries = []
                for item in data:
                    self._entries.append(FixMemory(
                        error_signature=item.get("error_signature", ""),
                        error_pattern=item.get("error_pattern", ""),
                        error_type=item.get("error_type", ""),
                        root_cause=item.get("root_cause", ""),
                        fix_pattern=item.get("fix_pattern", "")[:150],
                        file_context=item.get("file_context", [])[:5],
                        success=item.get("success", False),
                        strategy_used=item.get("strategy_used", ""),
                        timestamp=item.get("timestamp", 0.0),
                        access_count=item.get("access_count", 0),
                        score=item.get("score", 0.0),
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
                    "error_pattern": m.error_pattern,
                    "error_type": m.error_type,
                    "root_cause": m.root_cause,
                    "fix_pattern": m.fix_pattern[:150],
                    "file_context": m.file_context[:5],
                    "success": m.success,
                    "strategy_used": m.strategy_used,
                    "timestamp": m.timestamp,
                    "access_count": m.access_count,
                    "score": m.score,
                })
        self.storage_path.parent.mkdir(parents=True, exist_ok=True)
        self.storage_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    def _evict_lru(self) -> None:
        """V0.5 fix: 失败记录优先清除，同状态内按时间排序（旧的先删）"""
        if not self._entries:
            return
        # 排序：失败记录在前（not success=True），同状态内旧记录在前
        self._entries.sort(key=lambda m: (m.success, m.timestamp))
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
