"""Web Search — 网络搜索

支持多个搜索后端：
  - duckduckgo: 免费，无需 API Key（默认，通过 DuckDuckGo HTML）
  - brave: Brave Search API（需要 API Key）
  - serpapi: SerpAPI（需要 API Key）

使用方式：
    from patchflow.core.web import web_search
    results = web_search("python asyncio best practices")
    for r in results:
        print(r.title, r.url)
"""

import html as _html_mod
import json
import re
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from patchflow.utils import logger


@dataclass
class SearchResult:
    """单条搜索结果"""
    title: str = ""
    url: str = ""
    snippet: str = ""
    source: str = ""  # 搜索后端名称
    relevance: float = 0.0

    def to_context(self) -> str:
        """格式化为 LLM 可用的上下文"""
        return f"[{self.title}]({self.url}): {self.snippet}"


# ═══════════════════════════════════════════════════════════
# 缓存
# ═══════════════════════════════════════════════════════════

_search_cache: dict[str, tuple[list[SearchResult], float]] = {}
CACHE_TTL_SECONDS = 900  # 15 分钟


def _cache_key(query: str, backend: str, limit: int) -> str:
    return f"{backend}:{query}:{limit}"


def _cache_get(query: str, backend: str, limit: int) -> list[SearchResult] | None:
    key = _cache_key(query, backend, limit)
    if key in _search_cache:
        results, timestamp = _search_cache[key]
        if time.time() - timestamp < CACHE_TTL_SECONDS:
            return results
        del _search_cache[key]
    return None


def _cache_set(query: str, backend: str, limit: int, results: list[SearchResult]):
    key = _cache_key(query, backend, limit)
    _search_cache[key] = (results, time.time())
    # 防止缓存无限增长
    if len(_search_cache) > 200:
        oldest = min(_search_cache.items(), key=lambda x: x[1][1])
        del _search_cache[oldest[0]]


# ═══════════════════════════════════════════════════════════
# 后端实现
# ═══════════════════════════════════════════════════════════

def _is_low_quality_url(url: str) -> bool:
    """过滤低质量 URL（广告、DDG 内部链接等）"""
    junk = ("duckduckgo.com/y.js", "duckduckgo.com/l/", "udemy.com",
            "facebook.com/", "/ads?", "doubleclick", "googleadservices",
            "codecademy.com", "coursera.org")
    return any(j in url.lower() for j in junk)


def _extract_real_url(ddg_url: str) -> str:
    """从 DuckDuckGo 跳转链接中提取真实目标 URL

    DuckDuckGo 的 HTML 版返回的 href 格式：
      //duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com&rut=...

    需要从中提取 uddg 参数的值并 URL 解码。
    """
    if not ddg_url:
        return ddg_url

    # 处理 // 开头的协议相对 URL
    if ddg_url.startswith("//"):
        ddg_url = "https:" + ddg_url

    # 提取 uddg 参数
    m = re.search(r'[?&]uddg=([^&]+)', ddg_url)
    if m:
        decoded = urllib.parse.unquote(m.group(1))
        # 清理 rut 等残留参数
        decoded = re.sub(r'&rut=.*$', '', decoded)
        return decoded

    # 不是 DuckDuckGo 跳转链接 → 原样返回
    return ddg_url


def _search_searx(query: str, limit: int = 8) -> list[SearchResult]:
    """通过 SearXNG 公共实例搜索（免费、无 API Key、JSON 接口）

    SearXNG 是开源元搜索引擎，公共实例遍布全球。
    JSON API 格式稳定，不会被反爬。
    """
    # 多个公共实例自动容错
    instances = [
        "https://search.sapti.me/search",
        "https://search.bus-hit.me/search",
        "https://searx.be/search",
        "https://search.rhscz.eu/search",
    ]

    import random
    random.shuffle(instances)

    for base_url in instances:
        try:
            api_url = base_url + "?" + urllib.parse.urlencode({
                "q": query,
                "format": "json",
                "categories": "general,it",
                "language": "auto",
                "pageno": "1",
            })
            req = urllib.request.Request(
                api_url,
                headers={"User-Agent": "PatchFlow/1.0"},
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode("utf-8", errors="replace"))

            results = []
            for item in data.get("results", [])[:limit]:
                title = _html_mod.unescape(item.get("title", ""))
                url = item.get("url", "")
                snippet = _clean_snippet(item.get("content", ""))
                if title and url:
                    results.append(SearchResult(
                        title=title[:120],
                        url=url,
                        snippet=snippet[:200],
                        source=f"searxng",
                    ))
            if results:
                return results
        except Exception:
            continue  # 尝试下一个实例

    return []


def _search_duckduckgo(query: str, limit: int = 8) -> list[SearchResult]:
    """DuckDuckGo 搜索（免费，无需 API Key）

    优先使用 Instant Answer API（JSON），被限时回退到 HTML 版。
    """
    # ── 方案 1: Instant Answer API（JSON，不会被反爬）──
    try:
        api_url = (
            "https://api.duckduckgo.com/?"
            + urllib.parse.urlencode({
                "q": query,
                "format": "json",
                "no_html": "1",
                "skip_disambig": "1",
                "t": "patchflow",
            })
        )
        req = urllib.request.Request(
            api_url,
            headers={"User-Agent": "PatchFlow/1.0"},
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8", errors="replace"))
    except Exception as e:
        logger.debug(f"[WebSearch] DDG API failed: {e}")
        data = {}

    results = []

    # 提取 RelatedTopics（DDG API 的主要结果）
    related = data.get("RelatedTopics", [])
    for topic in related[:limit]:
        if isinstance(topic, dict):
            title = topic.get("Text", "")
            url = topic.get("FirstURL", "")
            snippet = topic.get("Text", "")
            if title and url:
                results.append(SearchResult(
                    title=_html_mod.unescape(re.sub(r'<[^>]+>', '', title)).strip()[:120],
                    url=url,
                    snippet=_clean_snippet(snippet)[:200],
                    source="duckduckgo-api",
                ))

    # 也检查 Abstract（百科摘要）
    abstract = data.get("AbstractText", "")
    abstract_url = data.get("AbstractURL", "")
    if abstract and abstract_url and len(results) < limit:
        results.append(SearchResult(
            title=data.get("Heading", query),
            url=abstract_url,
            snippet=abstract[:200],
            source="duckduckgo-api",
        ))

    if results:
        return results

    # ── 方案 2: HTML 版回退 ──
    try:
        url = f"https://html.duckduckgo.com/html/?q={urllib.parse.quote(query)}"
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                              "AppleWebKit/537.36 (KHTML, like Gecko) "
                              "Chrome/120.0.0.0 Safari/537.36",
                "Accept": "text/html,application/xhtml+xml",
            },
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            html = resp.read().decode("utf-8", errors="replace")
    except Exception as e:
        logger.debug(f"[WebSearch] DDG HTML failed: {e}")
        return _search_fallback(query, limit)

    # 检查是否被反爬
    if resp.getcode() != 200 or "result__a" not in html:
        return _search_fallback(query, limit)

    # 解析 HTML
    link_pattern = re.compile(
        r'<a[^>]*class="result__a"[^>]*href="([^"]*)"[^>]*>(.*?)</a>',
        re.DOTALL | re.IGNORECASE,
    )
    snippet_pattern = re.compile(
        r'<a[^>]*class="result__snippet"[^>]*>(.*?)</a>',
        re.DOTALL | re.IGNORECASE,
    )

    links = link_pattern.findall(html)
    snippets = snippet_pattern.findall(html)

    for i, (href, title) in enumerate(links[:limit]):
        title_clean = _html_mod.unescape(re.sub(r'<[^>]+>', '', title)).strip()
        if not title_clean:
            continue
        real_url = _extract_real_url(href)
        snippet_clean = ""
        if i < len(snippets):
            snippet_clean = _clean_snippet(re.sub(r'<[^>]+>', '', snippets[i]))
        results.append(SearchResult(
            title=title_clean,
            url=real_url,
            snippet=snippet_clean,
            source="duckduckgo",
        ))

    if results:
        return results

    return _search_fallback(query, limit)


def _search_fallback(query: str, limit: int = 8) -> list[SearchResult]:
    """回退方案：使用 DuckDuckGo 的 lite 版本或返回空"""
    try:
        url = f"https://lite.duckduckgo.com/lite/?q={urllib.parse.quote(query)}"
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                              "AppleWebKit/537.36",
            },
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            html = resp.read().decode("utf-8", errors="replace")
    except Exception as e:
        logger.warn(f"[WebSearch] Fallback also failed: {e}")
        return []

    results = []
    # Lite 版本格式: <a href="...">title</a> 后面跟着 <span class="snippet">...</span>
    link_pattern = re.compile(
        r'<a[^>]*href="(https?://[^"]+)"[^>]*>(.*?)</a>',
        re.DOTALL | re.IGNORECASE,
    )
    snippet_pattern = re.compile(
        r'<span[^>]*class="snippet"[^>]*>(.*?)</span>',
        re.DOTALL | re.IGNORECASE,
    )

    links = link_pattern.findall(html)
    snippets = snippet_pattern.findall(html)

    for i, (href, title) in enumerate(links[:limit]):
        title_clean = _html_mod.unescape(re.sub(r'<[^>]+>', '', title)).strip()
        if not title_clean or "duckduckgo" in href:
            continue
        snippet_clean = ""
        if i < len(snippets):
            snippet_clean = _clean_snippet(re.sub(r'<[^>]+>', '', snippets[i]))
        results.append(SearchResult(
            title=title_clean,
            url=_extract_real_url(href),
            snippet=snippet_clean,
            source="duckduckgo-lite",
        ))

    return results


def _clean_snippet(text: str) -> str:
    """清理摘要文本：解码 HTML 实体、合并空白"""
    if not text:
        return ""
    text = _html_mod.unescape(text)
    text = re.sub(r'\s+', ' ', text).strip()
    return text


# ═══════════════════════════════════════════════════════════
# 主接口
# ═══════════════════════════════════════════════════════════

def web_search(
    query: str,
    limit: int = 8,
    backend: str = "auto",
    use_cache: bool = True,
) -> list[SearchResult]:
    """搜索网络并返回结果列表

    Args:
        query: 搜索查询
        limit: 最大结果数
        backend: "auto" / "duckduckgo" / "brave" / "serpapi"
        use_cache: 是否使用缓存（15 分钟 TTL）

    Returns:
        SearchResult 列表
    """
    query = query.strip()
    if not query or len(query) < 2:
        logger.warn("[WebSearch] Query too short, skipping")
        return []

    if use_cache:
        cached = _cache_get(query, backend, limit)
        if cached is not None:
            logger.debug(f"[WebSearch] Cache hit: {query[:60]}")
            return cached

    logger.debug(f"[WebSearch] Searching: {query[:80]}")

    if backend == "auto":
        # SearXNG 优先（JSON API，稳定），DDG 回退
        results = _search_searx(query, limit)
        if not results:
            results = _search_duckduckgo(query, limit)
    elif backend == "duckduckgo":
        results = _search_duckduckgo(query, limit)
    elif backend == "searx":
        results = _search_searx(query, limit)
    else:
        logger.debug(f"[WebSearch] Unknown backend '{backend}', using auto")
        results = _search_searx(query, limit)
        if not results:
            results = _search_duckduckgo(query, limit)

    # 过滤广告和低质量 URL
    results = [r for r in results if not _is_low_quality_url(r.url)]

    if use_cache:
        _cache_set(query, backend, limit, results)

    logger.debug(f"[WebSearch] Found {len(results)} results for: {query[:60]}")
    return results


def search_for_fix(
    error_type: str,
    error_message: str,
    language: str = "",
    limit: int = 5,
) -> list[SearchResult]:
    """为修复任务定制的搜索——优化查询词

    Args:
        error_type: 错误类型（如 "SyntaxError"）
        error_message: 错误消息的关键部分
        language: 编程语言
        limit: 结果数

    Returns:
        搜索结果
    """
    # 构造优化查询
    parts = [language, error_type] if language else [error_type]

    # 提取错误消息中的关键部分（去掉文件路径和行号）
    clean_msg = re.sub(r'File ".*?", line \d+', '', error_message)
    clean_msg = re.sub(r'\b0x[0-9a-fA-F]+\b', '', clean_msg)
    clean_msg = clean_msg.strip()[:150]

    if clean_msg:
        parts.append(clean_msg[:100])

    parts.append("fix solution")
    query = " ".join(p for p in parts if p)

    return web_search(query, limit=limit)


def format_search_context(results: list[SearchResult],
                          max_chars: int = 3000) -> str:
    """将搜索结果格式化为 LLM 上下文

    Args:
        results: 搜索结果列表
        max_chars: 最大字符数

    Returns:
        格式化的上下文字符串
    """
    if not results:
        return "(no search results)"

    lines = ["## Web Search Results\n"]
    total = 0
    for i, r in enumerate(results, 1):
        entry = f"{i}. **{r.title}**\n   {r.url}\n   {r.snippet}\n"
        if total + len(entry) > max_chars:
            lines.append(f"... (truncated, {len(results) - i + 1} more results)")
            break
        lines.append(entry)
        total += len(entry)

    return "\n".join(lines)
