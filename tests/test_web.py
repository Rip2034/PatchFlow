"""Web 信息检索层测试"""
import pytest
from patchflow.core.web.web_search import (
    web_search, SearchResult, search_for_fix,
    format_search_context, _cache_key, _cache_get, _cache_set,
)
from patchflow.core.web.web_fetch import (
    web_fetch, fetch_docs, _html_to_text, _extract_title,
    FetchResult,
)
from patchflow.core.web.deep_research import (
    _generate_query_variants,
    ResearchReport,
)


class TestSearchResult:
    def test_basic(self):
        sr = SearchResult(title="Test", url="https://test.com", snippet="A test result")
        assert sr.title == "Test"
        assert sr.url == "https://test.com"

    def test_to_context(self):
        sr = SearchResult(title="T", url="https://u.com", snippet="snippet")
        ctx = sr.to_context()
        assert "T" in ctx
        assert "https://u.com" in ctx
        assert "snippet" in ctx


class TestSearchCache:
    def test_cache_set_get(self):
        results = [SearchResult(title="R", url="https://x.com")]
        _cache_set("test query", "duckduckgo", 5, results)
        cached = _cache_get("test query", "duckduckgo", 5)
        assert cached is not None
        assert len(cached) == 1
        assert cached[0].title == "R"

    def test_cache_key_differentiates_params(self):
        r1 = [SearchResult(title="A", url="https://a.com")]
        r2 = [SearchResult(title="B", url="https://b.com")]
        _cache_set("q", "backend1", 5, r1)
        _cache_set("q", "backend2", 5, r2)
        assert _cache_get("q", "backend1", 5)[0].title == "A"
        assert _cache_get("q", "backend2", 5)[0].title == "B"

    def test_cache_miss(self):
        assert _cache_get("nonexistent_query_xyz", "auto", 5) is None


class TestWebSearch:
    def test_empty_query(self):
        results = web_search("")
        assert results == []

    def test_short_query(self):
        results = web_search("a")
        assert results == []

    def test_valid_query(self):
        results = web_search("python asyncio gather", limit=3)
        # Should get some results (may be empty if offline)
        assert isinstance(results, list)
        for r in results:
            assert isinstance(r, SearchResult)
            assert r.title
            assert r.url

    def test_cache_hit(self):
        # First call populates cache
        r1 = web_search("python unique test query 12345", limit=2)
        # Second call should hit cache
        r2 = web_search("python unique test query 12345", limit=2)
        assert len(r1) == len(r2)

    def test_search_for_fix(self):
        results = search_for_fix("SyntaxError", "invalid syntax", "python", limit=2)
        assert isinstance(results, list)


class TestFormatSearchContext:
    def test_empty(self):
        ctx = format_search_context([])
        assert "no search results" in ctx.lower()

    def test_formats_results(self):
        results = [
            SearchResult(title="T1", url="https://a.com", snippet="S1"),
            SearchResult(title="T2", url="https://b.com", snippet="S2"),
        ]
        ctx = format_search_context(results)
        assert "T1" in ctx
        assert "T2" in ctx
        assert "https://a.com" in ctx
        assert "S1" in ctx

    def test_truncation(self):
        results = [SearchResult(title="T", url="https://u.com", snippet="x" * 500) for _ in range(10)]
        ctx = format_search_context(results, max_chars=200)
        assert len(ctx) <= 300  # allow some overhead


class TestHtmlToText:
    def test_basic_conversion(self):
        html = "<html><body><h1>Hello</h1><p>World</p></body></html>"
        text = _html_to_text(html)
        assert "Hello" in text
        assert "World" in text

    def test_removes_script(self):
        html = "<html><script>alert('xss')</script><p>Safe</p></html>"
        text = _html_to_text(html)
        assert "alert" not in text
        assert "Safe" in text

    def test_removes_style(self):
        html = "<html><style>.red{color:red}</style><p>Text</p></html>"
        text = _html_to_text(html)
        assert ".red" not in text
        assert "Text" in text

    def test_link_conversion(self):
        html = '<a href="https://example.com">Click here</a>'
        text = _html_to_text(html)
        assert "[Click here](https://example.com)" in text

    def test_code_block(self):
        html = "<pre><code>print('hello')</code></pre>"
        text = _html_to_text(html)
        assert "```" in text
        assert "print('hello')" in text

    def test_entities_decoded(self):
        html = "<p>&amp; &lt; &gt;</p>"
        text = _html_to_text(html)
        assert "&" in text
        assert "<" in text or "&lt;" not in text  # decoded


class TestExtractTitle:
    def test_extracts_title(self):
        html = "<html><head><title>My Page</title></head><body></body></html>"
        assert _extract_title(html) == "My Page"

    def test_no_title(self):
        assert _extract_title("<html><body>No title</body></html>") == ""


class TestWebFetch:
    def test_fetch_valid_url(self):
        result = web_fetch("https://httpbin.org/get", max_chars=5000)
        if not result.is_ok:
            pytest.skip(f"httpbin.org unavailable (status={result.status_code})")
        assert result.status_code == 200

    def test_fetch_invalid_url(self):
        result = web_fetch("https://this-domain-does-not-exist-12345.com")
        assert not result.is_ok
        assert result.error

    def test_fetch_http_upgrade(self):
        result = web_fetch("http://httpbin.org/get")
        if not result.is_ok:
            pytest.skip("httpbin.org unavailable")
        # Should have been upgraded to HTTPS
        assert result.url.startswith("https://")

    def test_fetch_result_llm_context(self):
        result = FetchResult(url="https://test.com", title="Test", markdown="Content")
        ctx = result.llm_context(max_chars=100)
        assert "Test" in ctx
        assert "Content" in ctx
        assert "https://test.com" in ctx

    def test_fetch_result_summary(self):
        result = FetchResult(url="https://test.com", text="Hello World")
        assert "Hello" in result.summary()

    def test_fetch_error_summary(self):
        result = FetchResult(url="https://test.com", error="Connection refused")
        assert "Connection refused" in result.summary()


class TestGenerateQueryVariants:
    def test_basic_variants(self):
        variants = _generate_query_variants("python async")
        assert any("how to" in v.lower() for v in variants)
        assert len(variants) <= 6

    def test_error_query(self):
        variants = _generate_query_variants("fix KeyError in dict")
        assert any("solution" in v.lower() for v in variants)
        assert any("fix" in v.lower() for v in variants)


class TestResearchReport:
    def test_basic_report(self):
        from patchflow.core.web.deep_research import SourceItem
        report = ResearchReport(
            question="Test?",
            summary="Test summary",
            findings=[{"claim": "Something", "evidence": "Proof", "references": [1]}],
            sources=[SourceItem(title="Src", url="https://s.com", snippet="...")],
            verified=True,
            confidence=0.85,
        )
        md = report.to_markdown()
        assert "Test?" in md
        assert "Test summary" in md
        assert "Something" in md
        assert "85%" in md or "0.85" in md

    def test_llm_context(self):
        report = ResearchReport(
            question="Q",
            summary="S",
            findings=[{"claim": "C", "evidence": "E"}],
        )
        ctx = report.llm_context()
        assert "Q" in ctx
        assert "S" in ctx
        assert "C" in ctx

    def test_empty_report(self):
        report = ResearchReport(question="?")
        assert report.summary == ""
        assert report.findings == []
        assert report.sources == []
        assert report.confidence == 0.0
