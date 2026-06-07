"""Deep Research — 深度调研

从 Claude Code 的 deep-research skill 移植：
  - 多源搜索（fan-out 到多个搜索引擎/查询变体）
  - 交叉验证（adversarial verify 每条关键信息）
  - 引用报告（生成带引用来源的结构化报告）

使用方式：
    from patchflow.core.web import deep_research
    report = deep_research("FastAPI vs Flask performance comparison")
    print(report.summary)
"""

import concurrent.futures
import time
from dataclasses import dataclass, field

from patchflow.utils import logger


@dataclass
class SourceItem:
    """单个信息来源"""
    title: str = ""
    url: str = ""
    snippet: str = ""
    content: str = ""  # 抓取后的完整内容摘要
    credibility: float = 0.0  # 可信度评分

    def to_reference(self, index: int) -> str:
        return f"[{index}] {self.title} — {self.url}"


@dataclass
class ResearchReport:
    """深度调研报告"""
    question: str = ""
    summary: str = ""
    findings: list[dict] = field(default_factory=list)
    sources: list[SourceItem] = field(default_factory=list)
    verified: bool = False
    confidence: float = 0.0
    elapsed_ms: float = 0.0

    def to_markdown(self) -> str:
        """生成带引用的 Markdown 报告"""
        lines = [
            f"# Research: {self.question}",
            "",
            f"**Confidence:** {self.confidence:.0%} | "
            f"**Sources:** {len(self.sources)} | "
            f"**Verified:** {'Yes' if self.verified else 'Partial'}",
            "",
            "## Summary",
            self.summary,
            "",
            "## Key Findings",
        ]

        for i, f in enumerate(self.findings, 1):
            claim = f.get("claim", "")
            evidence = f.get("evidence", "")
            refs = f.get("references", [])
            ref_str = ", ".join(f"[{r}]" for r in refs) if refs else ""
            lines.append(f"### Finding {i}: {claim}")
            if evidence:
                lines.append(f"{evidence}")
            if ref_str:
                lines.append(f"*Sources: {ref_str}*")
            lines.append("")

        lines.append("## Sources")
        for i, s in enumerate(self.sources, 1):
            lines.append(f"{i}. [{s.title}]({s.url}) — credibility: {s.credibility:.0%}")
            if s.snippet:
                lines.append(f"   > {s.snippet[:200]}")

        return "\n".join(lines)

    def llm_context(self, max_chars: int = 4000) -> str:
        """格式化为 LLM 可用的紧凑上下文"""
        parts = [
            f"## Research Context: {self.question}",
            f"Summary: {self.summary}",
            "",
            "Key Findings:",
        ]
        for i, f in enumerate(self.findings[:10], 1):
            parts.append(f"{i}. {f.get('claim', '')}")
            if f.get("evidence"):
                parts.append(f"   Evidence: {f['evidence'][:200]}")
        parts.append("")
        parts.append("Sources:")
        for i, s in enumerate(self.sources[:10], 1):
            parts.append(f"{i}. {s.title} ({s.url})")

        result = "\n".join(parts)
        if len(result) > max_chars:
            result = result[:max_chars] + "\n...(truncated)"
        return result


# ═══════════════════════════════════════════════════════════
# 查询变体生成
# ═══════════════════════════════════════════════════════════

def _generate_query_variants(question: str) -> list[str]:
    """为一个问题生成多个搜索查询变体（不同角度）"""
    variants = [question]

    # 添加 "how to" 变体
    if not question.lower().startswith("how"):
        variants.append(f"How to {question}")

    # 添加 "best practice" 变体
    if "best" not in question.lower():
        variants.append(f"{question} best practices")

    # 添加 "vs" 变体（如果是比较类问题）
    if " vs " in question.lower() or " versus " in question.lower():
        parts = question.lower().replace(" versus ", " vs ").split(" vs ")
        variants.append(f"{parts[0].strip()} comparison")
        variants.append(f"{parts[-1].strip()} comparison")

    # 添加 "example" 变体
    if "example" not in question.lower():
        variants.append(f"{question} example")

    # 添加 "fix" / "error" 变体（如果是技术问题）
    if any(w in question.lower() for w in ("error", "bug", "fail", "exception")):
        variants.append(f"fix {question}")
        variants.append(f"{question} solution")

    # 去重
    seen = set()
    unique = []
    for v in variants:
        if v not in seen:
            seen.add(v)
            unique.append(v)

    return unique[:6]  # 最多 6 个变体


# ═══════════════════════════════════════════════════════════
# 主接口
# ═══════════════════════════════════════════════════════════

from patchflow.core.web.web_search import _is_low_quality_url  # noqa: E402


def _rich_progress():
    """创建 Rich 进度条（如果可用），Windows 安全"""
    try:
        from rich.progress import (
            Progress, SpinnerColumn, TextColumn, BarColumn,
            TimeElapsedColumn,
        )
        # 使用 ASCII spinner 避免 Windows GBK 编码问题
        return Progress(
            SpinnerColumn(spinner_name="line"),  # ASCII-only: | / - \\
            TextColumn("[bold blue]{task.description}"),
            BarColumn(),
            TextColumn("[dim]{task.completed}/{task.total}[/dim]"),
            TimeElapsedColumn(),
            transient=False,
        )
    except ImportError:
        return None


def deep_research(
    question: str,
    search_depth: int = 3,
    verify: bool = True,
    model: str | None = None,
    max_sources: int = 8,
    fast: bool = False,
) -> ResearchReport:
    """深度调研：多源搜索 → 抓取 → 交叉验证 → 引用报告

    Args:
        question: 调研问题
        search_depth: 搜索深度（每个变体的搜索结果数）
        verify: 是否进行交叉验证
        model: LLM 模型
        max_sources: 最大来源数
        fast: 快速模式（跳过 LLM 合成，直接返回搜索结果）

    Returns:
        ResearchReport
    """
    from patchflow.core.web.web_search import web_search, SearchResult
    from patchflow.core.web.web_fetch import web_fetch, FetchResult

    t0 = time.time()

    # ── Rich 进度条 ──
    progress = _rich_progress()
    if progress:
        progress.start()

    def _log(desc: str, done: int = 0, total: int = 0):
        """统一日志：有 progress 时更新进度条，否则用 logger"""
        if progress:
            task_id = getattr(_log, "_task_id", None)
            if total > 0:
                # 新的有总量任务 → 替换旧任务
                if task_id is not None:
                    progress.remove_task(task_id)
                _log._task_id = progress.add_task(desc, total=total, completed=done)
            elif done == 0 and total == 0:
                # 纯文本消息 → 只更新描述
                if task_id is not None:
                    progress.update(task_id, description=desc)
                else:
                    _log._task_id = progress.add_task(desc, total=None)
            else:
                if task_id is not None:
                    progress.update(task_id, description=desc, completed=done)
        else:
            logger.info(desc)

    _log._task_id = None

    report = ResearchReport(question=question)

    # ── Phase 1: Fan-out 搜索 ──
    variants = _generate_query_variants(question)
    _log(f"Searching {len(variants)} query variants...")

    all_search_results: list[SearchResult] = []

    def _search_variant(variant: str):
        return web_search(variant, limit=search_depth + 1)

    with concurrent.futures.ThreadPoolExecutor(max_workers=min(len(variants), 4)) as ex:
        futures = [ex.submit(_search_variant, v) for v in variants]
        for f in concurrent.futures.as_completed(futures):
            try:
                all_search_results.extend(f.result())
            except Exception:
                pass

    # 去重 + 过滤低质量
    seen_urls = set()
    unique_search = []
    for sr in all_search_results:
        if sr.url not in seen_urls and not _is_low_quality_url(sr.url):
            seen_urls.add(sr.url)
            unique_search.append(sr)

    to_fetch = unique_search[:max_sources]

    # ── Phase 2: 并行抓取（如果有结果）──
    sources: list[SourceItem] = []

    if to_fetch:
        _log(f"Fetching {len(to_fetch)} pages...", 0, len(to_fetch))

        def _fetch_source(sr: SearchResult):
            fr = web_fetch(sr.url, use_cache=True)
            return SourceItem(
                title=sr.title,
                url=sr.url,
                snippet=sr.snippet,
                content=fr.markdown[:2000] if fr.is_ok else "",
                credibility=0.3 if not fr.is_ok else 0.5,
            )

        with concurrent.futures.ThreadPoolExecutor(max_workers=min(len(to_fetch), 6)) as ex:
            futures = {ex.submit(_fetch_source, sr): sr for sr in to_fetch}
            for f in concurrent.futures.as_completed(futures):
                try:
                    source = f.result()
                    if source.content or source.snippet:
                        sources.append(source)
                except Exception:
                    pass
                if progress and _log._task_id is not None:
                    progress.update(_log._task_id, advance=1,
                                    description=f"Fetching ({len(sources)} ok)...")

    _log(f"Fetched {len(sources)} sources")
    report.sources = sources

    # ── Phase 3: 合成（fast 模式跳过 LLM）──
    if fast or not sources:
        # 快速模式：用 snippets 拼摘要
        snippets = [s.snippet for s in sources if s.snippet][:5]
        report.summary = f"Found {len(sources)} sources about '{question}'."
        report.findings = [
            {"claim": s.title, "evidence": s.snippet[:200] or s.content[:200],
             "references": [i + 1]}
            for i, s in enumerate(sources[:5])
        ]
        report.verified = False
        report.confidence = min(0.7, 0.3 + 0.05 * len(sources))
    else:
        _log("Synthesizing with LLM...")
        synthesis = _synthesize_findings(question, sources, model)
        report.summary = synthesis.get("summary", "")
        report.findings = synthesis.get("findings", [])

        # Phase 4: 交叉验证（可选）
        if verify and report.findings and len(sources) >= 2:
            _log("Cross-verifying...")
            report.verified = _cross_verify(report.findings, sources, model)
            if report.verified:
                for s in sources:
                    s.credibility = min(1.0, s.credibility + 0.3)
            report.confidence = min(0.95, 0.5 + 0.1 * len(sources) + (0.2 if report.verified else 0))
        else:
            report.confidence = min(0.7, 0.3 + 0.05 * len(sources))

    report.elapsed_ms = (time.time() - t0) * 1000

    if progress:
        try:
            progress.stop()
        except (UnicodeEncodeError, UnicodeDecodeError):
            pass  # Windows GBK 编码兼容

    return report


def _synthesize_findings(
    question: str,
    sources: list[SourceItem],
    model: str | None,
) -> dict:
    """使用 LLM 从多个来源中综合关键发现"""
    try:
        from patchflow.core.llm_client import call_llm

        # 来源权重标记
        official_domains = (
            "docs.python.org", "python.org", "developer.mozilla.org",
            "nodejs.org", "pkg.go.dev", "doc.rust-lang.org",
            "fastapi.tiangolo.com", "flask.palletsprojects.com",
            "docs.djangoproject.com", "react.dev", "vuejs.org",
        )

        sources_text = "\n\n---\n\n".join(
            f"Source {i+1}: {s.title}\n"
            f"URL: {s.url}\n"
            f"{'[OFFICIAL DOCS] ' if any(d in s.url.lower() for d in official_domains) else ''}"
            f"{s.content[:800] or s.snippet[:400]}"
            for i, s in enumerate(sources[:8])
        )

        prompt = f"""Synthesize key findings from these sources to answer the question.

Question: {question}

Sources:
{sources_text}

RULES:
- Max 4 findings
- Each finding MUST cite at least 2 source numbers
- Prefer claims supported by [OFFICIAL DOCS] sources
- If sources disagree, note the disagreement in evidence
- For performance/comparison claims, include specific numbers if available

Output ONLY valid JSON:
{{"summary": "≤200 chars", "findings": [{{"claim": "...", "evidence": "explain what sources say, note if they agree/disagree", "references": [1,3], "confidence": "high/medium/low"}}]}}"""

        result = call_llm(
            system_prompt="You are a research analyst. Synthesize findings from multiple sources. Be specific — quote numbers when available. Note disagreements between sources. Prefer official documentation over blog posts.",
            user_message=prompt,
            model=model,
            max_tokens=800,  # 放宽输出限制以容纳更详细结果
        )
        if result and isinstance(result, dict):
            return result
    except Exception as e:
        logger.warn(f"[DeepResearch] Synthesis failed: {e}")

    # 回退：基于 snippets 的简单摘要
    snippets = [s.snippet for s in sources if s.snippet]
    return {
        "summary": f"Found {len(sources)} sources related to '{question}'. "
                   + (snippets[0][:150] if snippets else ""),
        "findings": [
            {"claim": s.snippet[:120], "evidence": s.content[:200] or s.snippet, "references": [i + 1]}
            for i, s in enumerate(sources[:5])
        ],
    }


def _cross_verify(
    findings: list[dict],
    sources: list[SourceItem],
    model: str | None,
) -> bool:
    """交叉验证：每个 finding 从 2 个角度独立验证，要求引述原文证据

    改进版三层校验：
      1. 要求 LLM 引述原文证据（quote），不是只答 yes/no
      2. 每个 finding 从 "事实准确" 和 "来源充分" 两个角度各验证一次
      3. 两次都通过才算该 finding 通过，≥60% findings 通过才算整体 verified
    """
    try:
        from patchflow.core.llm_client import call_llm

        # 来源权重：官方文档 > 技术博客 > 其他
        official_domains = (
            "docs.python.org", "python.org", "developer.mozilla.org",
            "nodejs.org", "pkg.go.dev", "doc.rust-lang.org",
            "fastapi.tiangolo.com", "flask.palletsprojects.com",
            "docs.djangoproject.com", "react.dev", "vuejs.org",
        )

        def _source_weight(s: SourceItem) -> int:
            url = s.url.lower()
            if any(d in url for d in official_domains):
                return 3  # 官方文档权重高
            if any(d in url for d in ("wikipedia.org", "github.com", "stackoverflow.com")):
                return 2
            return 1

        # 扩展的来源上下文（增加到 1200 字符 + 权重标记）
        all_content = "\n\n".join(
            f"[Source {i+1}] [weight={_source_weight(s)}] {s.title}\n"
            f"URL: {s.url}\n"
            f"{s.content[:1200] or s.snippet[:400]}"
            for i, s in enumerate(sources[:8])
        )

        verified_count = 0

        for finding in findings:
            claim = finding.get("claim", "")
            if not claim:
                continue

            # ── 角度 1: 事实准确性 — 来源是否明确说了这个结论 ──
            prompt_accuracy = f"""You are a fact-checker. Verify if the sources SUPPORT or REFUTE this claim.

Claim: "{claim}"

Sources:
{all_content}

CRITICAL RULES:
- ONLY mark as supported if a source explicitly states the claim or equivalent
- If a source is about the topic but doesn't address the claim → NOT supported
- If the claim involves numbers (performance, speed, dates) → must find the EXACT number

Output JSON:
{{
  "supported": true/false,
  "best_source": <source number that best supports, or 0 if none>,
  "quote": "<exact quote from the best source that supports/refutes>",
  "confidence": 0.0-1.0
}}"""

            # ── 角度 2: 来源充分性 — 支持该结论的来源是否足够权威 ──
            prompt_sufficiency = f"""You are a source quality evaluator.
This claim has been flagged as potentially true. Assess source quality.

Claim: "{claim}"

Sources:
{all_content}

Evaluate: Do MULTIPLE credible sources support this claim?
- Official documentation (=weight 3) carries more weight than blogs (=weight 1)
- A single source alone is NOT sufficient

Output JSON:
{{
  "sufficient": true/false,
  "credible_sources": [<list of source numbers that are credible and support the claim>],
  "weakness": "<one-line explanation if insufficient>"
}}"""

            # 先验事实准确性
            acc_result = call_llm(
                system_prompt="You are a strict fact-checker. Only mark supported if the source EXPLICITLY states the claim.",
                user_message=prompt_accuracy,
                model=model,
                max_tokens=300,
            )

            if not acc_result or not isinstance(acc_result, dict):
                continue

            finding["_accuracy"] = acc_result

            if not acc_result.get("supported"):
                continue  # 事实都没过，来源充分性不用验了

            # 再验来源充分性
            suf_result = call_llm(
                system_prompt="You evaluate source credibility. Multiple credible sources are required for high confidence.",
                user_message=prompt_sufficiency,
                model=model,
                max_tokens=300,
            )

            if not suf_result or not isinstance(suf_result, dict):
                continue

            finding["_sufficiency"] = suf_result
            finding["_verified"] = suf_result.get("sufficient", False)

            if suf_result.get("sufficient"):
                verified_count += 1

        return verified_count >= max(1, len(findings) * 0.6)

    except Exception as e:
        logger.debug(f"[DeepResearch] Cross-verify failed: {e}")
        return False


def research_for_task(task: str, language: str = "",
                      model: str | None = None) -> ResearchReport:
    """为代码任务做快速调研

    Args:
        task: 任务描述
        language: 编程语言
        model: LLM 模型

    Returns:
        ResearchReport
    """
    if language:
        question = f"{language} {task}"
    else:
        question = task

    return deep_research(
        question=question,
        search_depth=3,
        verify=False,  # 快速模式不做验证
        max_sources=8,
        model=model,
    )
