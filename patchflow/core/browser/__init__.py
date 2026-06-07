"""Browser — 浏览器自动化验证层

从 Claude Code 的 Chrome DevTools MCP 移植：
  - browser_verify: 启动浏览器 → 加载页面 → 截图 + 检测错误
  - console_monitor: 监控 JS 控制台错误
  - lighthouse: Lighthouse 审计（SEO/可访问性/最佳实践）

为 PatchFlow 的 Web 项目修复提供可视化验证能力。
"""

from patchflow.core.browser.verify import browser_verify, BrowserVerifyResult
from patchflow.core.browser.console_monitor import ConsoleMonitor, ConsoleError
from patchflow.core.browser.lighthouse import run_lighthouse, LighthouseResult

__all__ = [
    "browser_verify", "BrowserVerifyResult",
    "ConsoleMonitor", "ConsoleError",
    "run_lighthouse", "LighthouseResult",
]
