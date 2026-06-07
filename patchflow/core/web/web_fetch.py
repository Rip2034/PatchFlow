"""Web Fetch — URL 抓取与分析

从 Claude Code 的 WebFetch 移植：
  - 抓取 URL 内容
  - HTML → Markdown 转换
  - 可选 LLM 摘要/分析

使用方式：
    from patchflow.core.web import web_fetch
    result = web_fetch("https://docs.python.org/3/library/asyncio.html")
    print(result.markdown[:500])
"""

import re
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
import html as html_mod

from patchflow.utils import logger

# 缓存
_fetch_cache: dict[str, tuple["FetchResult", float]] = {}
FETCH_CACHE_TTL = 900  # 15 分钟


@dataclass
class FetchResult:
    """URL 抓取结果"""
    url: str = ""
    title: str = ""
    markdown: str = ""
    text: str = ""
    status_code: int = 0
    error: str = ""
    content_type: str = ""
    fetched_at: float = 0.0

    @property
    def is_ok(self) -> bool:
        return self.status_code == 200 and self.error == ""

    def summary(self, max_chars: int = 500) -> str:
        """简短摘要"""
        if self.error:
            return f"[Fetch Error] {self.error[:max_chars]}"
        return self.text[:max_chars]

    def llm_context(self, max_chars: int = 4000) -> str:
        """格式化为 LLM 上下文"""
        parts = [f"## {self.title or 'Untitled'}", f"Source: {self.url}"]
        if self.markdown:
            parts.append(self.markdown[:max_chars])
        else:
            parts.append(self.text[:max_chars])
        return "\n\n".join(parts)


# ═══════════════════════════════════════════════════════════
# HTML → Markdown 转换（轻量实现，不依赖外部库）
# ═══════════════════════════════════════════════════════════

def _html_to_text(html_str: str) -> str:
    """HTML → 纯文本（简易转换器）

    优先级：如果安装了 markdownify 就用它，否则用内建简易转换。
    """
    try:
        from markdownify import markdownify as md
        return md(html_str, heading_style="ATX", strip=["script", "style", "nav", "footer"])
    except ImportError:
        pass

    # 内建简易转换
    text = html_str

    # 移除 script / style / nav / footer
    for tag in ("script", "style", "nav", "footer", "header", "aside"):
        text = re.sub(
            rf'<{tag}[^>]*>.*?</{tag}>',
            '',
            text,
            flags=re.DOTALL | re.IGNORECASE,
        )

    # 标题 → markdown 标题
    for level in range(1, 7):
        text = re.sub(
            rf'<h{level}[^>]*>(.*?)</h{level}>',
            rf'\n\n{"#" * level} \1\n\n',
            text,
            flags=re.DOTALL | re.IGNORECASE,
        )

    # 段落
    text = re.sub(r'<p[^>]*>(.*?)</p>', r'\n\n\1\n\n', text, flags=re.DOTALL | re.IGNORECASE)

    # 换行
    text = re.sub(r'<br\s*/?>', '\n', text, flags=re.IGNORECASE)

    # 链接: <a href="...">text</a> → [text](href)
    text = re.sub(
        r'<a[^>]*href=["\']([^"\']*)["\'][^>]*>(.*?)</a>',
        r'[\2](\1)',
        text,
        flags=re.DOTALL | re.IGNORECASE,
    )

    # 粗体/斜体
    text = re.sub(r'<(strong|b)[^>]*>(.*?)</\1>', r'**\2**', text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r'<(em|i)[^>]*>(.*?)</\1>', r'*\2*', text, flags=re.DOTALL | re.IGNORECASE)

    # 代码块
    text = re.sub(
        r'<pre[^>]*><code[^>]*>(.*?)</code></pre>',
        r'\n```\n\1\n```\n',
        text,
        flags=re.DOTALL | re.IGNORECASE,
    )
    text = re.sub(
        r'<code[^>]*>(.*?)</code>',
        r'`\1`',
        text,
        flags=re.DOTALL | re.IGNORECASE,
    )

    # 列表项
    text = re.sub(r'<li[^>]*>(.*?)</li>', r'\n- \1', text, flags=re.DOTALL | re.IGNORECASE)

    # 移除剩余 HTML 标签
    text = re.sub(r'<[^>]+>', '', text)

    # HTML 实体解码
    text = html_mod.unescape(text)

    # 清理多余空白
    text = re.sub(r'\n{3,}', '\n\n', text)
    text = re.sub(r' {2,}', ' ', text)

    return text.strip()


def _extract_title(html_str: str) -> str:
    """从 HTML 中提取 <title>"""
    m = re.search(r'<title[^>]*>(.*?)</title>', html_str, re.DOTALL | re.IGNORECASE)
    if m:
        return html_mod.unescape(re.sub(r'<[^>]+>', '', m.group(1))).strip()
    return ""


# ═══════════════════════════════════════════════════════════
# 主接口
# ═══════════════════════════════════════════════════════════

def web_fetch(
    url: str,
    analyze_prompt: str = "",
    model: str | None = None,
    use_cache: bool = True,
    max_chars: int = 50000,
) -> FetchResult:
    """抓取 URL 内容并返回 FetchResult

    Args:
        url: 目标 URL（HTTP 自动升级为 HTTPS）
        analyze_prompt: 可选的分析 prompt（如 "summarize the key APIs"）
        model: LLM 模型（分析时使用）
        use_cache: 是否使用缓存
        max_chars: 最大抓取字符数

    Returns:
        FetchResult 包含 title, markdown, text 等
    """
    url = url.strip()
    if url.startswith("http://"):
        url = url.replace("http://", "https://", 1)

    if use_cache and url in _fetch_cache:
        result, timestamp = _fetch_cache[url]
        if time.time() - timestamp < FETCH_CACHE_TTL:
            logger.debug(f"[WebFetch] Cache hit: {url[:80]}")
            return result

    logger.debug(f"[WebFetch] Fetching: {url[:100]}")

    result = FetchResult(url=url, fetched_at=time.time())

    try:
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0.0.0 Safari/537.36"
                ),
                "Accept": "text/html,application/xhtml+xml,text/plain;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9,zh-CN;q=0.8",
            },
        )
        with urllib.request.urlopen(req, timeout=20) as resp:
            result.status_code = resp.getcode()
            result.content_type = resp.headers.get("Content-Type", "")
            raw = resp.read()

        # 尝试解码
        for encoding in ("utf-8", "gbk", "latin-1"):
            try:
                html_str = raw.decode(encoding)
                break
            except UnicodeDecodeError:
                continue
        else:
            html_str = raw.decode("utf-8", errors="replace")

        if len(html_str) > max_chars:
            html_str = html_str[:max_chars]

        result.title = _extract_title(html_str)
        result.markdown = _html_to_text(html_str)
        result.text = re.sub(r'\s+', ' ', result.markdown).strip()[:10000]

        logger.debug(
            f"[WebFetch] OK: {len(result.markdown)} chars, "
            f"title='{result.title[:60]}'"
        )

    except urllib.error.HTTPError as e:
        result.status_code = e.code
        result.error = f"HTTP {e.code}: {e.reason}"
        logger.debug(f"[WebFetch] HTTP error: {result.error}")
    except urllib.error.URLError as e:
        result.error = f"URL error: {e.reason}"
        logger.debug(f"[WebFetch] URL error: {result.error}")
    except Exception as e:
        result.error = f"Unexpected error: {e}"
        logger.debug(f"[WebFetch] Error: {result.error}")

    # 缓存
    if use_cache and result.is_ok:
        _fetch_cache[url] = (result, time.time())
        if len(_fetch_cache) > 200:
            oldest = min(_fetch_cache.items(), key=lambda x: x[1][1])
            del _fetch_cache[oldest[0]]

    # 可选的 LLM 分析
    if analyze_prompt and result.is_ok and result.markdown:
        result.markdown = _analyze_with_llm(
            result, analyze_prompt, model
        )

    return result


def _analyze_with_llm(
    result: FetchResult, prompt: str, model: str | None
) -> str:
    """使用 LLM 分析抓取的内容"""
    try:
        from patchflow.core.llm_client import call_llm

        context = result.markdown[:8000]
        llm_result = call_llm(
            system_prompt="You are a technical documentation analyst. "
                          "Extract the relevant information based on the user's prompt.",
            user_message=f"{prompt}\n\nContent:\n{context}",
            model=model,
        )
        if llm_result and isinstance(llm_result, dict):
            return llm_result.get("analysis", llm_result.get("summary", result.markdown[:2000]))
    except Exception as e:
        logger.debug(f"[WebFetch] LLM analysis failed: {e}")
    return result.markdown


def fetch_docs(
    query: str,
    language: str = "python",
    max_results: int = 3,
) -> str:
    """快速查文档——搜索 + 抓取 + 格式化为 LLM 上下文

    Args:
        query: 查询（如 "asyncio.gather"）
        language: 编程语言
        max_results: 最大抓取文档页数

    Returns:
        格式化的文档上下文字符串
    """
    from patchflow.core.web.web_search import web_search

    search_query = f"{language} {query} documentation official"
    results = web_search(search_query, limit=max_results + 2)

    # 优先选官方文档
    official_domains = {
        "python": ["docs.python.org", "python.org"],
        "javascript": ["developer.mozilla.org", "nodejs.org"],
        "go": ["pkg.go.dev", "go.dev"],
        "rust": ["doc.rust-lang.org"],
        "java": ["docs.oracle.com"],
        "typescript": ["www.typescriptlang.org"],
    }
    preferred = official_domains.get(language, [])

    fetched = []
    for r in results:
        if len(fetched) >= max_results:
            break
        # 优先抓官方文档
        if preferred and not any(d in r.url for d in preferred):
            continue
        fr = web_fetch(r.url)
        if fr.is_ok and fr.markdown:
            fetched.append(fr)

    # 如果官方文档不够，补充其他来源
    if len(fetched) < max_results:
        for r in results:
            if len(fetched) >= max_results:
                break
            if any(f.url == r.url for f in fetched):
                continue
            fr = web_fetch(r.url)
            if fr.is_ok and fr.markdown:
                fetched.append(fr)

    if not fetched:
        from patchflow.core.web.web_search import format_search_context
        return format_search_context(results, max_chars=2000)

    parts = ["## Fetched Documentation\n"]
    for f in fetched:
        parts.append(f.llm_context(max_chars=2000))
        parts.append("\n---\n")

    return "\n".join(parts)
