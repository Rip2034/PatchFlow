"""Agent 通信合约 — 标准化三个 Agent 之间的数据交换格式

设计原则：
  1. 每个 Agent 输出必须有 summary（≤150 字符）— 其他 Agent 优先读这个
  2. 每个字段都有明确的字符预算，防止 token 滥用
  3. Blackboard 按角色压缩时只保留对方需要看的字段

字段预算约定（字符数）：
  summary          ≤ 150   一句话总结
  root_cause       ≤ 200   根因描述
  reason           ≤ 100   每个 patch 的修改理由
  impact_files     ≤ 3 个   最多列 3 个文件
  issues           ≤ 3 条   最多列 3 个问题
  feedback         ≤ 200   Reviewer 驳回反馈
  suggestion       ≤ 300   Reviewer 改进建议
"""

ANALYSIS_KEYS = {"error_type", "root_cause", "impact_files", "confidence", "summary", "language"}
FIX_PLAN_KEYS = {"patches", "patch_count", "summary"}
REVIEW_KEYS = {"approved", "score", "summary", "issues", "feedback"}

ANALYZER_PROMPT = """You are a Bug Analyzer. Your ONLY job is to analyze the error.
Do NOT suggest fixes. Do NOT write code. Only analyze.

First, identify the programming language from the error output and code.
Then analyze the error in that language's context.

OUTPUT RULES:
- summary MUST be ≤ 150 characters (other agents read this)
- root_cause MUST be ≤ 200 characters
- impact_files: list up to 3 affected files
- language: identify the programming language (python, javascript, typescript, java, go, rust, etc.)
- Output ONLY valid JSON

OUTPUT FORMAT:
{
  "error_type": "runtime|syntax|type|logic|test_fail|attribute|import|name",
  "root_cause": "precise root cause (≤200 chars)",
  "impact_files": ["src/file1.java", "src/file2.java"],
  "confidence": 0.85,
  "summary": "one-line error summary (≤150 chars)",
  "language": "python"
}"""

FIXER_PROMPT = """You are a Code Fixer. Fix ONLY the listed files.

RULES:
- Output ONLY valid JSON
- Make MINIMAL changes
- Do NOT rewrite the entire file
- Keep the same coding style
- reason MUST be ≤ 100 characters per patch

OUTPUT FORMAT:
{
  "summary": "one-line fix summary (≤150 chars)",
  "patches": [
    {
      "file": "service.py or actual path",
      "old": "original code snippet",
      "new": "fixed code snippet",
      "reason": "why this change (≤100 chars)"
    }
  ]
}"""

REVIEWER_PROMPT = """You are a Code Reviewer. Review the fix independently.

Check:
1. Does the fix address the root cause? (not just the symptom)
2. Will this break existing tests or other modules?
3. Does the fix match the project's code style?
4. Is there a simpler fix that would work?
5. Could this introduce new bugs?

OUTPUT RULES:
- summary MUST be ≤ 150 characters (read by Fixer if rejected)
- feedback MUST be ≤ 200 characters (read by Fixer on redo)
- max 3 issues
- Output ONLY valid JSON

OUTPUT FORMAT:
{
  "approved": true,
  "score": 8,
  "summary": "review conclusion (≤150 chars)",
  "issues": ["issue 1", "issue 2"],
  "feedback": "improvement direction if rejected (≤200 chars)"
}"""


def truncate(text: str, max_chars: int) -> str:
  if not text:
    return ""
  if len(text) <= max_chars:
    return text
  return text[:max_chars - 3] + "..."


def validate_analysis(data: dict) -> dict:
  """校验并补全 Analyzer 输出"""
  result = {
    "error_type": data.get("error_type", "unknown"),
    "root_cause": truncate(str(data.get("root_cause", "")), 200),
    "impact_files": (data.get("impact_files") or data.get("impact") or [])[:3],
    "confidence": float(data.get("confidence", 0.0)),
    "summary": truncate(str(data.get("summary", data.get("root_cause", ""))), 150),
    "language": str(data.get("language", "")),
  }
  if not result["summary"]:
    result["summary"] = f"{result['error_type']}: {result['root_cause'][:120]}"
  return result


def validate_fix_plan(data: dict) -> dict:
  """校验并补全 Fixer 输出"""
  raw_patches = data.get("patches", [])
  patches = []
  for p in raw_patches:
    patches.append({
      "file": str(p.get("file", "")),
      "old": str(p.get("old", "")),
      "new": str(p.get("new", "")),
      "reason": truncate(str(p.get("reason", "")), 100),
    })
  result = {
    "patches": patches,
    "summary": truncate(str(data.get("summary", "")), 150),
  }
  if not result["summary"]:
    result["summary"] = f"{len(patches)} patch(es): {patches[0]['reason'][:80] if patches else 'no changes'}"
  return result


def validate_review(data: dict) -> dict:
  """校验并补全 Reviewer 输出"""
  result = {
    "approved": bool(data.get("approved", False)),
    "score": min(10, max(1, int(data.get("score", 5)))),
    "summary": truncate(str(data.get("summary", "")), 150),
    "issues": (data.get("issues") or [])[:3],
    "feedback": truncate(str(data.get("feedback") or data.get("suggestion", "")), 200),
  }
  if not result["summary"]:
    status = "approved" if result["approved"] else "rejected"
    result["summary"] = f"{status} ({result['score']}/10): {result['issues'][0][:80] if result['issues'] else 'no issues'}"
  return result
