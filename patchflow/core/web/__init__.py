"""Web — 信息检索层

从 Claude Code 的 WebSearch/WebFetch 移植：
  - web_search: 搜索 API（支持多个后端）
  - web_fetch: URL 抓取 + HTML→Markdown + LLM 摘要
  - deep_research: 多源搜索 → 交叉验证 → 引用报告

为 PatchFlow 的 generate/fix 流程提供"先查后写"能力。
"""

from patchflow.core.web.web_search import (
    web_search, SearchResult,
    search_for_fix, format_search_context,
)
from patchflow.core.web.web_fetch import web_fetch, FetchResult, fetch_docs
from patchflow.core.web.deep_research import deep_research, ResearchReport

__all__ = [
    "web_search", "SearchResult",
    "search_for_fix", "format_search_context",
    "web_fetch", "FetchResult", "fetch_docs",
    "deep_research", "ResearchReport",
]
