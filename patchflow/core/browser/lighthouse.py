"""Lighthouse — 网页质量审计

从 Claude Code 的 lighthouse_audit 移植：
  - 可访问性 (a11y)
  - SEO
  - 最佳实践
  - 性能（可选，需要 Chrome DevTools Protocol）

使用方式：
    from patchflow.core.browser import run_lighthouse
    result = run_lighthouse("http://localhost:3000")
    print(f"SEO score: {result.scores.get('seo', 0)}")
"""

import json
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

from patchflow.utils import logger


@dataclass
class LighthouseResult:
    """Lighthouse 审计结果"""
    url: str = ""
    scores: dict[str, float] = field(default_factory=dict)
    # accessibility, seo, best-practices, performance
    issues: dict[str, list[dict]] = field(default_factory=dict)
    report_path: str = ""
    ok: bool = False
    error: str = ""
    duration_ms: float = 0.0

    def summary(self) -> str:
        if self.error:
            return f"Lighthouse FAILED: {self.error}"
        score_parts = [
            f"{cat}: {score:.0f}/100"
            for cat, score in self.scores.items()
        ]
        return " | ".join(score_parts) if score_parts else "No scores"

    def is_passing(self, threshold: float = 80.0) -> bool:
        """所有类别都超过阈值"""
        return all(s >= threshold for s in self.scores.values()) if self.scores else False

    def weakest_category(self) -> str:
        """返回分数最低的类别"""
        if not self.scores:
            return ""
        return min(self.scores, key=self.scores.get)


def run_lighthouse(
    url: str = "http://localhost:3000",
    categories: list[str] | None = None,
    output_dir: str = "",
    device: str = "desktop",
    use_cli: bool = True,
    timeout_ms: int = 60000,
) -> LighthouseResult:
    """运行 Lighthouse 审计

    Args:
        url: 页面 URL
        categories: 审计类别（默认 ["accessibility", "seo", "best-practices"]）
        output_dir: 报告输出目录
        device: "desktop" 或 "mobile"
        use_cli: 是否使用 lighthouse CLI（优先），否则用 Playwright 回退
        timeout_ms: 超时

    Returns:
        LighthouseResult
    """
    t0 = time.time()
    result = LighthouseResult(url=url)

    if categories is None:
        categories = ["accessibility", "seo", "best-practices"]

    # 先确保 URL 可访问
    if not _check_url_accessible(url):
        result.error = f"URL not accessible: {url}"
        result.duration_ms = (time.time() - t0) * 1000
        return result

    if output_dir:
        Path(output_dir).mkdir(parents=True, exist_ok=True)
        report_path = str(Path(output_dir) / f"lighthouse_{int(time.time())}.json")
    else:
        import tempfile
        report_path = str(
            Path(tempfile.gettempdir()) / f"pf_lighthouse_{int(time.time())}.json"
        )

    # 优先使用 Lighthouse CLI
    if use_cli:
        cli_result = _run_lighthouse_cli(url, categories, report_path, device, timeout_ms)
        if cli_result is not None:
            result.ok = True
            result.scores = cli_result.get("scores", {})
            result.issues = cli_result.get("issues", {})
            result.report_path = report_path
            result.duration_ms = (time.time() - t0) * 1000
            logger.info(f"[Lighthouse] CLI audit: {result.summary()}")
            return result

    # 回退：Playwright + 内建检查
    fallback_result = _run_inline_audit(url, categories)
    result.ok = True
    result.scores = fallback_result
    result.duration_ms = (time.time() - t0) * 1000
    logger.info(f"[Lighthouse] Inline audit: {result.summary()}")
    return result


def _check_url_accessible(url: str) -> bool:
    try:
        import urllib.request
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "PatchFlow-Lighthouse/1.0"},
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.getcode() < 500
    except Exception:
        return False


def _run_lighthouse_cli(
    url: str,
    categories: list[str],
    report_path: str,
    device: str,
    timeout_ms: int,
) -> dict | None:
    """使用 lighthouse CLI 执行审计"""
    try:
        cat_str = ",".join(categories)
        device_flag = "--preset=desktop" if device == "desktop" else ""

        cmd = [
            "npx", "lighthouse", url,
            "--output=json",
            f"--output-path={report_path}",
            f"--only-categories={cat_str}",
            "--chrome-flags=--headless --no-sandbox",
            "--quiet",
        ]
        if device_flag:
            cmd.append(device_flag)

        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout_ms / 1000,
        )

        if result.returncode != 0:
            logger.debug(f"[Lighthouse] CLI failed: {result.stderr[:200]}")
            return None

        # 解析输出 JSON
        data = json.loads(Path(report_path).read_text(encoding="utf-8"))

        scores = {}
        issues: dict[str, list[dict]] = {}

        for cat_id in categories:
            cat_data = data.get("categories", {}).get(cat_id, {})
            if cat_data and "score" in cat_data:
                scores[cat_id] = cat_data["score"] * 100
            # 收集可操作的问题
            audit_refs = cat_data.get("auditRefs", [])
            cat_issues = []
            for ref in audit_refs:
                audit = data.get("audits", {}).get(ref.get("id", ""), {})
                if audit.get("score") is not None and audit["score"] < 1:
                    cat_issues.append({
                        "id": ref.get("id", ""),
                        "title": audit.get("title", ""),
                        "description": audit.get("description", "")[:200],
                        "score": audit["score"],
                    })
            if cat_issues:
                issues[cat_id] = cat_issues

        return {"scores": scores, "issues": issues}

    except FileNotFoundError:
        logger.debug("[Lighthouse] lighthouse CLI not found (npx not available)")
        return None
    except (json.JSONDecodeError, KeyError) as e:
        logger.debug(f"[Lighthouse] Output parse error: {e}")
        return None
    except Exception as e:
        logger.warn(f"[Lighthouse] CLI error: {e}")
        return None


def _run_inline_audit(url: str, categories: list[str]) -> dict[str, float]:
    """内建审计（无需 lighthouse CLI）

    使用 HTTP 请求 + 正则做基础检查。
    """
    scores = {}

    try:
        import urllib.request
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "PatchFlow-Lighthouse/1.0"},
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            html = resp.read().decode("utf-8", errors="replace")
            headers = dict(resp.headers)
    except Exception:
        return {cat: 0.0 for cat in categories}

    import re

    # SEO 检查
    if "seo" in categories:
        seo_score = 100.0
        has_title = bool(re.search(r'<title[^>]*>', html, re.IGNORECASE))
        has_meta_desc = bool(re.search(
            r'<meta[^>]*name=["\']description["\']', html, re.IGNORECASE
        ))
        has_h1 = bool(re.search(r'<h1[^>]*>', html, re.IGNORECASE))
        has_alt = bool(re.search(r'alt=["\']', html, re.IGNORECASE))
        has_canonical = bool(re.search(
            r'<link[^>]*rel=["\']canonical["\']', html, re.IGNORECASE
        ))

        if not has_title:
            seo_score -= 20
        if not has_meta_desc:
            seo_score -= 15
        if not has_h1:
            seo_score -= 10
        if not has_alt:
            seo_score -= 10
        if not has_canonical:
            seo_score -= 5

        scores["seo"] = max(0, seo_score)

    # 可访问性检查
    if "accessibility" in categories:
        a11y_score = 100.0
        has_lang = bool(re.search(r'<html[^>]*lang=["\']', html, re.IGNORECASE))
        has_viewport = bool(re.search(
            r'<meta[^>]*name=["\']viewport["\']', html, re.IGNORECASE
        ))
        has_aria = bool(re.search(r'aria-', html, re.IGNORECASE))
        has_semantic = bool(re.search(
            r'<(header|main|nav|footer|article|section)', html, re.IGNORECASE
        ))

        if not has_lang:
            a11y_score -= 25
        if not has_viewport:
            a11y_score -= 15
        if not has_semantic:
            a11y_score -= 15
        if not has_aria:
            a11y_score -= 10

        scores["accessibility"] = max(0, a11y_score)

    # 最佳实践
    if "best-practices" in categories:
        bp_score = 100.0
        has_doctype = html.strip().lower().startswith("<!doctype")
        has_https = url.startswith("https://")
        has_charset = bool(re.search(
            r'<meta[^>]*charset=', html, re.IGNORECASE
        ))
        no_inline_script = "<script>" not in html.lower()

        if not has_doctype:
            bp_score -= 20
        if not has_https:
            bp_score -= 25
        if not has_charset:
            bp_score -= 10
        if not no_inline_script:
            bp_score -= 15

        scores["best-practices"] = max(0, bp_score)

    return scores
