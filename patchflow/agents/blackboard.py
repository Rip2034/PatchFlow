"""Blackboard — 多 Agent 共享数据结构

所有 Agent 通过 Blackboard 交换信息，不直接通信。
每个 Agent 写入自己的那部分数据，其他 Agent 读取。

通信合约（所有 Agent 输出的标准格式见 schema.py）：
  1. 每个 Agent 输出必须有 summary 字段（≤150 字符）
  2. 其他 Agent 优先读 summary，需要细节再读完整字段
  3. 传递时用 compress_for(role) 自动压缩

字段说明：
  task:        用户原始需求
  context:     Context Collector 输出（确定性）
  code:        当前代码文件内容 { filename: content }
  error:       Validator 输出的错误信息

  analysis:       Agent 1 (Analyzer) 写入（格式见 schema.ANALYSIS_KEYS）
  fix_plan:       Agent 2 (Fixer) 写入（格式见 schema.FIX_PLAN_KEYS）
  review:         Agent 3 (Reviewer) 写入（格式见 schema.REVIEW_KEYS）
  review_feedback: Reviewer 驳回时的简短反馈

活动追踪（Activity Tracking）：
  Blackboard 自动记录每个 Agent 的读写操作，可通过 set_current_agent() 声明身份。
  所有 get() / set_*() 调用自动打日志，用于 CLI 可视化。
"""

import json
import time
from copy import deepcopy

from patchflow.agents.schema import validate_analysis, validate_fix_plan, validate_review
from patchflow.agents.schema import ANALYSIS_KEYS, FIX_PLAN_KEYS, REVIEW_KEYS


class Blackboard:
    """多 Agent 共享黑板"""

    def __init__(self, task: str = "", context: dict | None = None,
                 code: dict[str, str] | None = None,
                 error: str = ""):
        self.data = {
            "task": task,
            "context": context or {},
            "code": code or {},
            "error": error,
            "analysis": None,
            "fix_plan": None,
            "review": None,
            "review_feedback": None,
        }
        self._current_agent: str | None = None
        self._activity: list[dict] = []
        self._read_set: set[str] = set()
        self._write_set: set[str] = set()

    def set_current_agent(self, agent: str):
        """声明当前操作的 Agent 身份，后续所有 get/set 自动追踪到此 Agent"""
        self._current_agent = agent
        self._read_set.clear()
        self._write_set.clear()
        self._log("enter", agent)

    def get_current_agent(self) -> str | None:
        return self._current_agent

    def __getitem__(self, key):
        self._log("read", key)
        return self.data[key]

    def __setitem__(self, key, value):
        self.data[key] = value
        self._log("write", key)

    def __contains__(self, key):
        return key in self.data

    def get(self, key, default=None):
        self._log("read", key)
        return self.data.get(key, default)

    def set_analysis(self, raw: dict):
        """写入并校验 Analyzer 输出"""
        self._log("write", "analysis")
        self.data["analysis"] = validate_analysis(raw)

    def set_fix_plan(self, raw: dict):
        """写入并校验 Fixer 输出"""
        self._log("write", "fix_plan")
        self.data["fix_plan"] = validate_fix_plan(raw)

    def set_review(self, raw: dict):
        """写入并校验 Reviewer 输出"""
        self._log("write", "review")
        self.data["review"] = validate_review(raw)

    def _log(self, action: str, field: str):
        if self._current_agent:
            self._activity.append({
                "agent": self._current_agent,
                "action": action,
                "field": field,
                "time": time.time(),
            })
            if action == "read":
                self._read_set.add(field)
            elif action == "write":
                self._write_set.add(field)

    def get_activity(self, n: int = 6) -> list[dict]:
        """获取最近 n 条活动记录"""
        return self._activity[-n:]

    def get_activity_summary(self) -> dict[str, dict[str, set]]:
        """按 Agent 汇总读写字段：{agent: {read: {fields}, write: {fields}}}"""
        summary: dict[str, dict[str, set]] = {}
        for entry in self._activity:
            if entry["action"] not in ("read", "write"):
                continue
            agent = entry["agent"]
            if agent not in summary:
                summary[agent] = {"read": set(), "write": set()}
            summary[agent][entry["action"]].add(entry["field"])
        return summary

    def clear_activity(self):
        self._activity.clear()
        self._read_set.clear()
        self._write_set.clear()

    def compress_for(self, role: str) -> dict:
        """为指定角色压缩 Blackboard（只保留对方需要的字段）

        Args:
            role: "analyzer" | "fixer" | "reviewer"

        Returns:
            压缩后的 dict（不含完整代码，只有摘要+必需字段）
        """
        analysis = self.data.get("analysis") or {}
        fix_plan = self.data.get("fix_plan") or {}
        review = self.data.get("review") or {}

        base = {
            "task": self.data.get("task", ""),
            "error": self.data.get("error", "")[:500],
        }

        if role == "analyzer":
            # Analyzer 需要看 error + task + code（全量代码由外部传入）
            return {**base, "code": self.data.get("code", {})}

        if role == "fixer":
            # Fixer 需要 analysis 摘要 + 错误 + 可选的 review 反馈
            result = {**base}
            if analysis:
                result["analysis"] = {k: analysis.get(k) for k in ANALYSIS_KEYS if k in analysis}
            if review and not review.get("approved", True):
                result["review_feedback"] = review.get("feedback", "")
            return result

        if role == "reviewer":
            # Reviewer 需要 analysis 摘要 + fix_plan 摘要
            result = {**base}
            if analysis:
                result["analysis"] = {k: analysis.get(k) for k in ANALYSIS_KEYS if k in analysis}
            if fix_plan:
                result["fix_plan"] = {"summary": fix_plan.get("summary", ""), "patches": fix_plan.get("patches", [])}
            return result

        return dict(self.data)

    def get_callchain_code(self) -> str:
        """获取调用链上所有文件的代码（给 Analyzer/Fixer 用）"""
        self._log("read", "code")
        analysis = self.data.get("analysis")
        if not analysis or not analysis.get("impact_files") and not analysis.get("call_chain"):
            return "\n".join(self.data["code"].values())

        impact = analysis.get("impact_files") or []
        call_chain = analysis.get("call_chain") or []
        call_files = set(f["file"] for f in call_chain) | set(impact)
        parts = []
        for filepath in call_files:
            content = self.data["code"].get(filepath, "")
            if content:
                parts.append(f"# === {filepath} ===\n{content}")
        return "\n\n".join(parts) if parts else "\n".join(self.data["code"].values())

    def get_code(self, allowed_files: list[str]) -> str:
        """获取指定文件的代码（受策略限制）"""
        self._log("read", "code")
        parts = []
        for filepath in allowed_files:
            content = self.data["code"].get(filepath, "")
            if content:
                parts.append(f"# === {filepath} ===\n{content}")
        return "\n\n".join(parts) if parts else "(no files available)"

    def summary(self) -> str:
        """生成 Blackboard 状态摘要（一行）"""
        parts = [f"Task: {self.data['task'][:60]}"]
        a = self.data.get("analysis") or {}
        if a.get("summary"):
            parts.append(f"Analysis: {a['summary'][:60]}")
        elif a.get("error_type"):
            parts.append(f"Analysis: {a['error_type']} | {str(a.get('root_cause',''))[:40]}")
        fp = self.data.get("fix_plan") or {}
        if fp.get("summary"):
            parts.append(f"Fix: {fp['summary'][:60]}")
        elif fp.get("patches"):
            parts.append(f"Fix: {len(fp['patches'])} patch(es)")
        r = self.data.get("review") or {}
        if r.get("summary"):
            parts.append(f"Review: {r['summary'][:60]}")
        elif r.get("approved") is not None:
            parts.append(f"Review: {'approved' if r['approved'] else 'rejected'} ({r.get('score','?')}/10)")
        return " | ".join(parts)

    def clone(self) -> "Blackboard":
        n = Blackboard()
        n.data = deepcopy(self.data)
        return n
