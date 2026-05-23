"""日志系统 — 统一的终端输出（无 emoji 版）

为什么不用 emoji？
  Windows 终端默认 GBK 编码，emoji 会触发 UnicodeEncodeError。
  改为英文标记，跨平台兼容性更好。

设计原则：
  - 统一接口：所有模块通过这里输出
  - 不加 emoji：避免 Windows 编码问题
  - 输出到 stderr：和 rich 的 Console 输出（stdout）分离
  - 等级过滤：DEBUG < INFO < WARN < ERROR，默认 INFO
"""

import os
import sys
from datetime import datetime

_levels = {"DEBUG": 10, "INFO": 20, "WARN": 30, "ERROR": 40}
_level = _levels.get(os.environ.get("PATCHFLOW_LOG", "INFO").upper(), 20)


def set_level(level: str) -> None:
    """动态设置日志等级: DEBUG / INFO / WARN / ERROR"""
    global _level
    _level = _levels.get(level.upper(), 20)


def _timestamp():
    return datetime.now().strftime("%H:%M:%S")


def _log(level: str, msg: str):
    if _levels.get(level, 0) >= _level:
        print(f"[{_timestamp()}] {level:<5} {msg}", file=sys.stderr, flush=True)


def info(msg: str):
    _log("INFO", msg)


def success(msg: str):
    _log("INFO", msg)


def error(msg: str):
    _log("ERROR", msg)


def debug(msg: str):
    _log("DEBUG", msg)


def warn(msg: str):
    _log("WARN", msg)


def step(msg: str):
    _log("INFO", msg)


def llm(msg: str):
    """LLM 调用日志 — DEBUG 级别（生产环境不显示）"""
    _log("DEBUG", msg)
