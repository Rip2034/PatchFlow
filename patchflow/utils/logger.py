"""日志系统 — 统一的终端输出（无 emoji 版）

为什么不用 emoji？
  Windows 终端默认 GBK 编码，emoji 会触发 UnicodeEncodeError。
  改为英文标记，跨平台兼容性更好。

设计原则：
  - 统一接口：所有模块通过这里输出
  - 不加 emoji：避免 Windows 编码问题
  - 输出到 stderr：和 rich 的 Console 输出（stdout）分离
"""

import sys
from datetime import datetime


def _timestamp():
    return datetime.now().strftime("%H:%M:%S")


def info(msg: str):
    print(f"[{_timestamp()}]  INFO  {msg}", file=sys.stderr)


def success(msg: str):
    print(f"[{_timestamp()}]  OK    {msg}", file=sys.stderr)


def error(msg: str):
    print(f"[{_timestamp()}]  ERROR {msg}", file=sys.stderr)


def debug(msg: str):
    print(f"[{_timestamp()}]  DEBUG {msg}", file=sys.stderr)


def warn(msg: str):
    print(f"[{_timestamp()}]  WARN  {msg}", file=sys.stderr)


def step(msg: str):
    print(f"[{_timestamp()}]  STEP  {msg}", file=sys.stderr)


def llm(msg: str):
    """LLM 调用日志"""
    print(f"[{_timestamp()}]  LLM   {msg}", file=sys.stderr)
