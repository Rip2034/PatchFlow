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

ANALYSIS_KEYS = {"error_type", "root_cause", "impact_files", "impact_symbols",
                  "confidence", "summary", "language", "call_chain"}
FIX_PLAN_KEYS = {"patches", "patch_count", "summary"}
REVIEW_KEYS = {"approved", "score", "dimensions", "summary", "issues", "feedback"}

ANALYZER_PROMPT = """You are a Bug Analyzer. Your ONLY job is to analyze the error.
Do NOT suggest fixes. Do NOT write code. Only analyze.

First, identify the programming language from the error output and code.
Then analyze the error in that language's context.

OUTPUT RULES:
- summary MUST be ≤ 150 characters (other agents read this)
- root_cause MUST be ≤ 200 characters
- impact_files: list up to 3 affected files
- call_chain: list the function call sequence from entry to crash site (max 5 frames)
- language: identify the programming language (python, javascript, typescript, java, go, rust, etc.)
- Output ONLY valid JSON

OUTPUT FORMAT:
{
  "error_type": "runtime|syntax|type|logic|test_fail|attribute|import|name",
  "root_cause": "precise root cause (≤200 chars)",
  "impact_files": ["src/file1.java", "src/file2.java"],
  "call_chain": [
    {"file": "main.py", "function": "main", "line": 10},
    {"file": "service.py", "function": "process", "line": 42}
  ],
  "confidence": 0.85,
  "summary": "one-line error summary (≤150 chars)",
  "language": "python"
}"""

ANALYZER_PROMPT_ENHANCED = """You are an expert Bug Analyzer with deep code understanding.

You will receive semantic code structure information (call chains, symbol locations, function signatures)
in addition to the error output. Use this to produce a more precise analysis.

Your ONLY job is to analyze the error. Do NOT suggest fixes. Do NOT write code. Only analyze.

ANALYSIS PROCESS:
1. Read the error output carefully — what exception/error occurred?
2. Study the semantic code structure — which functions are involved?
3. Trace the call chain — who called the crashing function? What data flows through?
4. Identify the root cause — is it a null value? type mismatch? missing import? logic error?
5. Determine impact — which files and symbols are affected?

OUTPUT RULES:
- summary MUST be ≤ 150 characters (other agents read this)
- root_cause MUST be ≤ 200 characters — be specific about the condition that triggers the error
- impact_files: list up to 3 affected files (use paths from the code structure)
- impact_symbols: list up to 5 affected functions/classes/methods (use names from semantic context)
- language: identify the programming language
- confidence: 0.0–1.0 based on how certain you are about the root cause
- Output ONLY valid JSON

OUTPUT FORMAT:
{
  "error_type": "runtime|syntax|type|logic|test_fail|attribute|import|name",
  "root_cause": "precise root cause including trigger condition (≤200 chars)",
  "impact_files": ["src/file1.java", "src/file2.java"],
  "impact_symbols": ["ClassName.methodName", "functionName"],
  "call_chain": [
    {"file": "main.py", "function": "main", "line": 10},
    {"file": "service.py", "function": "process", "line": 42}
  ],
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

REVIEWER_PROMPT_ENHANCED = """You are an expert Code Reviewer. Review the fix from FOUR independent dimensions.

For each dimension, score 1-10 and note specific issues:

## Dimension 1: Correctness
- Does the fix actually resolve the root cause? Not just the symptom?
- Is the logic correct? Are there off-by-one errors, wrong conditions?
- Does it handle the specific error case AND similar cases?

## Dimension 2: Security & Robustness
- Could the fix introduce injection vectors, path traversal, or data leaks?
- Are there null/None reference risks? Unhandled exceptions?
- Is input validation adequate? Are error messages leaking sensitive info?

## Dimension 3: Performance & Efficiency
- Does the fix add unnecessary loops, allocations, or I/O?
- Are there N+1 queries, repeated computations, or memory leaks?
- Is the fix proportional to the problem (no over-engineering)?

## Dimension 4: Edge Cases & Compatibility
- Are boundary conditions handled (empty/null/zero/max values)?
- Does the fix break any existing callers, tests, or dependent modules?
- Does it work correctly with concurrent access, different locales, or unusual inputs?

APPROVAL CRITERIA:
- approved=true if ALL dimensions score ≥ 6 AND overall score ≥ 7
- If any dimension scores < 5, auto-reject regardless of overall score

OUTPUT RULES:
- summary MUST be ≤ 150 characters
- feedback MUST be ≤ 200 characters (actionable direction for Fixer)
- max 5 issues across all dimensions
- Output ONLY valid JSON

OUTPUT FORMAT:
{
  "approved": true,
  "score": 8,
  "dimensions": {
    "correctness": {"score": 8, "note": "fixes root cause correctly"},
    "security": {"score": 9, "note": "no new risks introduced"},
    "performance": {"score": 7, "note": "minor allocation overhead acceptable"},
    "edge_cases": {"score": 6, "note": "should handle empty input case"}
  },
  "summary": "review conclusion (≤150 chars)",
  "issues": ["issue 1", "issue 2"],
  "feedback": "specific improvement direction if rejected (≤200 chars)"
}"""


def _safe_int(value, default: int = 5) -> int:
    """安全转换为 int，处理 LLM 可能返回的非数值（"8.5", "high", None 等）"""
    try:
        return int(value)
    except (ValueError, TypeError):
        try:
            return int(float(value))
        except (ValueError, TypeError):
            return default


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
    "impact_symbols": (data.get("impact_symbols") or [])[:5],
    "call_chain": (data.get("call_chain") or [])[:5],
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
  """校验并补全 Reviewer 输出（支持多维度评分）

  V0.5 fix: 维度拒绝阈值从 < 5 改为 < 6，与 prompt 中
  "ALL dimensions score >= 6" 的约定对齐。
  """
  dimensions = data.get("dimensions", {})
  # 如果提供了多维度评分，用维度平均分校准总分
  if dimensions and isinstance(dimensions, dict):
    dim_scores = []
    for d in ("correctness", "security", "performance", "edge_cases"):
      dim_data = dimensions.get(d, {})
      if isinstance(dim_data, dict):
        s = _safe_int(dim_data.get("score"), 5)
        dim_scores.append(s)
        dimensions[d] = {"score": s, "note": str(dim_data.get("note", ""))[:100]}
    if dim_scores:
      dim_avg = sum(dim_scores) / len(dim_scores)
      raw_score = _safe_int(data.get("score"), 5)
      # 总分取原始分和维度平均分的加权
      calibrated_score = min(10, max(1, round((raw_score + dim_avg) / 2)))
      # 任一维度 < 6 则自动拒绝（与 prompt 的 "ALL dimensions >= 6" 对齐）
      if any(s < 6 for s in dim_scores):
        data["approved"] = False
        if not any("dimension" in str(i).lower() for i in (data.get("issues") or [])):
          low_dims = [d for d, v in dimensions.items() if isinstance(v, dict) and v.get("score", 5) < 6]
          if low_dims:
            data.setdefault("issues", []).insert(0, f"Low dimension score(s): {', '.join(low_dims)}")
  else:
    calibrated_score = _safe_int(data.get("score"), 5)

  result = {
    "approved": bool(data.get("approved", False)),
    "score": min(10, max(1, calibrated_score)),
    "dimensions": dimensions,
    "summary": truncate(str(data.get("summary", "")), 150),
    "issues": (data.get("issues") or [])[:5],
    "feedback": truncate(str(data.get("feedback") or data.get("suggestion", "")), 200),
  }
  if not result["summary"]:
    status = "approved" if result["approved"] else "rejected"
    issue_text = result['issues'][0][:80] if result['issues'] else 'no issues'
    result["summary"] = f"{status} ({result['score']}/10): {issue_text}"
  return result
