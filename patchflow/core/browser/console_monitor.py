"""Console Monitor — JS 控制台错误监控

从 Claude Code 的 list_console_messages / get_console_message 移植：
  - 启动浏览器页面 → 收集所有 console 消息
  - 分类统计（error / warning / log）
  - 错误聚类（相同错误去重）

使用方式：
    from patchflow.core.browser import ConsoleMonitor
    monitor = ConsoleMonitor()
    errors = monitor.check("http://localhost:3000")
    for err in errors:
        print(f"[{err.level}] {err.text}")
"""

import time
from dataclasses import dataclass, field
from collections import defaultdict

from patchflow.utils import logger


@dataclass
class ConsoleError:
    """单条控制台错误"""
    text: str = ""
    level: str = "error"  # error | warning | log | info | debug
    source: str = ""
    line: int = 0
    column: int = 0
    count: int = 1  # 出现次数（聚类后）
    timestamp: float = 0.0

    @property
    def fingerprint(self) -> str:
        """错误指纹——用于去重"""
        # 移除数字和 URL 参数
        import re
        fp = re.sub(r'\d+', 'N', self.text)
        fp = re.sub(r'https?://\S+', 'URL', fp)
        return fp[:120]


class ConsoleMonitor:
    """JavaScript 控制台错误监控器"""

    def __init__(self):
        self._errors: list[ConsoleError] = []
        self._warnings: list[ConsoleError] = []

    def check(
        self,
        url: str = "http://localhost:3000",
        timeout_ms: int = 15000,
        collect_all: bool = False,
    ) -> list[ConsoleError]:
        """检查页面的控制台错误

        Args:
            url: 页面 URL
            timeout_ms: 超时
            collect_all: 是否收集所有消息（不只是 error）

        Returns:
            ConsoleError 列表（去重后）
        """
        self._errors.clear()
        self._warnings.clear()

        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            logger.warn("[ConsoleMonitor] Playwright not installed")
            return self._basic_check(url, timeout_ms)

        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True)
                context = browser.new_context()
                page = context.new_page()

                def _on_console(msg):
                    err = ConsoleError(
                        text=msg.text,
                        level=msg.type,
                        source=msg.location.get("url", ""),
                        timestamp=time.time(),
                    )
                    if msg.type == "error":
                        self._errors.append(err)
                    elif msg.type == "warning":
                        self._warnings.append(err)
                    elif collect_all:
                        self._errors.append(err)

                page.on("console", _on_console)

                try:
                    page.goto(url, wait_until="networkidle",
                              timeout=timeout_ms)
                    # 等待额外 2 秒让异步错误出现
                    page.wait_for_timeout(2000)
                except Exception as e:
                    logger.warn(f"[ConsoleMonitor] Page load error: {e}")

                browser.close()

        except Exception as e:
            logger.warn(f"[ConsoleMonitor] Playwright error: {e}")

        return self.cluster_errors()

    def _basic_check(self, url: str, timeout_ms: int) -> list[ConsoleError]:
        """无 Playwright 时的基础检查（HTTP status）"""
        try:
            import urllib.request
            req = urllib.request.Request(
                url,
                headers={"User-Agent": "PatchFlow-ConsoleMonitor/1.0"},
            )
            with urllib.request.urlopen(req, timeout=timeout_ms / 1000) as resp:
                if resp.getcode() >= 400:
                    return [ConsoleError(
                        text=f"HTTP {resp.getcode()}",
                        level="error",
                    )]
        except Exception as e:
            return [ConsoleError(
                text=f"Connection failed: {e}",
                level="error",
            )]
        return []

    def cluster_errors(self) -> list[ConsoleError]:
        """错误聚类——相同错误合并计数"""
        clusters: dict[str, ConsoleError] = {}

        for err in self._errors:
            fp = err.fingerprint
            if fp in clusters:
                clusters[fp].count += 1
            else:
                clusters[fp] = err

        # 按出现次数降序
        return sorted(
            clusters.values(),
            key=lambda e: e.count,
            reverse=True,
        )

    @property
    def error_count(self) -> int:
        return len(self._errors)

    @property
    def warning_count(self) -> int:
        return len(self._warnings)

    def summary(self) -> str:
        """错误摘要"""
        clustered = self.cluster_errors()
        if not clustered:
            return "No console errors detected"
        lines = [f"{len(clustered)} unique errors ({self.error_count} total):"]
        for e in clustered[:10]:
            lines.append(f"  [{e.level}] (x{e.count}) {e.text[:120]}")
        return "\n".join(lines)

    def get_errors_by_level(self, level: str) -> list[ConsoleError]:
        return [e for e in self._errors if e.level == level]

    def to_markdown(self) -> str:
        """生成 Markdown 格式的错误报告"""
        clustered = self.cluster_errors()
        if not clustered:
            return "## Console: Clean ✅\n\nNo console errors detected."

        lines = [
            f"## Console: {len(clustered)} errors ⚠️\n",
            f"| Count | Level | Message |",
            f"|-------|-------|---------|",
        ]
        for e in clustered[:20]:
            text = e.text[:100].replace("|", "\\|")
            lines.append(f"| {e.count} | {e.level} | {text} |")
        return "\n".join(lines)
