"""Browser Verify — 浏览器验证

启动浏览器 → 加载页面 → 截图 → 检测错误 → 返回结果。

使用方式：
    from patchflow.core.browser import browser_verify
    result = browser_verify("http://localhost:3000")
    if result.ok:
        print("Page loaded successfully!")
    else:
        print(f"Errors: {result.console_errors}")
"""

import json
import time
import webbrowser
from dataclasses import dataclass, field
from pathlib import Path

from patchflow.utils import logger


@dataclass
class BrowserVerifyResult:
    """浏览器验证结果"""
    url: str = ""
    ok: bool = False
    loaded: bool = False
    status_code: int = 0
    title: str = ""
    console_errors: list[str] = field(default_factory=list)
    console_warnings: list[str] = field(default_factory=list)
    network_errors: list[str] = field(default_factory=list)
    screenshot_path: str = ""
    page_text: str = ""
    duration_ms: float = 0.0
    error: str = ""

    def summary(self) -> str:
        if self.error:
            return f"Browser verify FAILED: {self.error}"
        if not self.loaded:
            return f"Page not loaded (HTTP {self.status_code})"
        parts = [f"Page loaded: '{self.title}'"]
        if self.console_errors:
            parts.append(f"{len(self.console_errors)} console errors")
        if self.network_errors:
            parts.append(f"{len(self.network_errors)} network errors")
        return " | ".join(parts)


def browser_verify(
    url: str = "http://localhost:3000",
    wait_for_text: str = "",
    screenshot_dir: str = "",
    timeout_ms: int = 15000,
    use_playwright: bool = True,
) -> BrowserVerifyResult:
    """使用浏览器加载页面并验证

    优先使用 Playwright（完整功能），
    回退到 HTTP 请求 + webbrowser（基础验证）。

    Args:
        url: 要测试的页面 URL
        wait_for_text: 等待特定文本出现
        screenshot_dir: 截图保存目录
        timeout_ms: 超时（毫秒）
        use_playwright: 是否尝试使用 Playwright

    Returns:
        BrowserVerifyResult
    """

    t0 = time.time()
    result = BrowserVerifyResult(url=url)

    # 先做快速 HTTP 检查
    http_ok, status_code, body = _http_check(url, timeout_ms)
    result.status_code = status_code

    if not http_ok:
        result.error = f"HTTP check failed (status {status_code})"
        result.duration_ms = (time.time() - t0) * 1000
        return result

    # 尝试 Playwright
    if use_playwright:
        try:
            pw_result = _verify_with_playwright(
                url, wait_for_text, screenshot_dir, timeout_ms
            )
            result.loaded = True
            result.title = pw_result.get("title", "")
            result.console_errors = pw_result.get("console_errors", [])
            result.console_warnings = pw_result.get("console_warnings", [])
            result.network_errors = pw_result.get("network_errors", [])
            result.screenshot_path = pw_result.get("screenshot", "")
            result.page_text = pw_result.get("text", "")
            result.ok = len(result.console_errors) == 0
            result.duration_ms = (time.time() - t0) * 1000
            logger.info(f"[BrowserVerify] Playwright OK: {result.summary()}")
            return result
        except ImportError:
            logger.debug("[BrowserVerify] Playwright not installed, using basic check")
        except Exception as e:
            logger.warn(f"[BrowserVerify] Playwright failed: {e}")

    # 回退：基础 HTTP 验证
    result.loaded = http_ok
    result.title = _extract_title(body)
    result.ok = status_code < 400
    result.duration_ms = (time.time() - t0) * 1000
    logger.info(f"[BrowserVerify] Basic check: {result.summary()}")
    return result


def _http_check(url: str, timeout_ms: int) -> tuple[bool, int, str]:
    """HTTP 请求检查"""
    try:
        import urllib.request
        timeout_sec = timeout_ms / 1000
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "PatchFlow-BrowserVerify/1.0"},
        )
        with urllib.request.urlopen(req, timeout=timeout_sec) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            return True, resp.getcode(), body
    except urllib.error.HTTPError as e:
        try:
            body = e.read().decode("utf-8", errors="replace")
        except Exception:
            body = ""
        return False, e.code, body
    except Exception as e:
        return False, 0, str(e)


def _extract_title(html: str) -> str:
    import re
    m = re.search(r'<title[^>]*>(.*?)</title>', html, re.DOTALL | re.IGNORECASE)
    if m:
        return re.sub(r'<[^>]+>', '', m.group(1)).strip()
    return ""


def _verify_with_playwright(
    url: str,
    wait_for_text: str = "",
    screenshot_dir: str = "",
    timeout_ms: int = 15000,
) -> dict:
    """Playwright 完整验证"""

    from playwright.sync_api import sync_playwright

    console_errors = []
    console_warnings = []
    network_errors = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            viewport={"width": 1280, "height": 720},
        )
        page = context.new_page()

        # 监听 console
        page.on("console", lambda msg: (
            console_errors.append(msg.text)
            if msg.type == "error"
            else console_warnings.append(msg.text)
            if msg.type == "warning"
            else None
        ))

        # 监听请求失败
        page.on("requestfailed", lambda req: (
            network_errors.append(f"{req.method} {req.url}: {req.failure}")
        ))

        try:
            page.goto(url, wait_until="networkidle",
                      timeout=timeout_ms)

            if wait_for_text:
                page.wait_for_selector(
                    f"text={wait_for_text}",
                    timeout=timeout_ms,
                )

            title = page.title()
            text = page.inner_text("body")[:5000]

            # 截图
            screenshot = ""
            if screenshot_dir:
                ss_dir = Path(screenshot_dir)
                ss_dir.mkdir(parents=True, exist_ok=True)
                screenshot = str(
                    ss_dir / f"verify_{int(time.time())}.png"
                )
                page.screenshot(path=screenshot, full_page=True)

        except Exception as e:
            logger.warn(f"[BrowserVerify] Playwright page error: {e}")
            title = ""
            text = ""
            screenshot = ""

        browser.close()

        return {
            "title": title,
            "console_errors": console_errors[:50],
            "console_warnings": console_warnings[:50],
            "network_errors": network_errors[:20],
            "text": text,
            "screenshot": screenshot,
        }
