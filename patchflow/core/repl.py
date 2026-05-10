"""REPL — 交互式对话循环

这是用户与 PatchFlow 交互的主界面。类似 Claude Code 的 CLI 风格。

核心功能：
  1. Markdown 渲染 → 代码块语法高亮（用 Rich 库）
  2. 工具调用摘要 → [$ command] 风格，紧凑单行显示
  3. 确认弹窗 → 交互式选择器（方向键/数字选择）
  4. 流式输出 → AI 回复逐字显示，不是等完了才一起出来
  5. 后台进程管理 → /stop /ps 命令
  6. 跨会话记忆 → 退出后重启能恢复之前的对话

可用命令：
  /help    显示帮助
  /exit    退出
  /clear   清空对话历史
  /plan    分步骤生成代码
  /build   一次性生成代码
  /fix     多 Agent 修复
  /context 查看上下文状态
  /model   切换模型
  /stop    停止后台进程
  /ps      查看后台进程
"""

import json
import os
import shutil
import sys
import threading
import time
from pathlib import Path

if sys.platform == "win32":
    os.environ["PYTHONIOENCODING"] = "utf-8"
    os.environ["PYTHONUTF8"] = "1"
    import subprocess
    subprocess.run(
        "chcp 65001",
        shell=True,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )

from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.text import Text

from patchflow.core.chat_client import ChatClient, set_confirm_callback
from patchflow.core.config import get_config
from patchflow.utils import logger
from patchflow.utils.runner import _CANCELLED

_term_w, _ = shutil.get_terminal_size((80, 24))
_console_width = min(_term_w, 100)
console = Console(width=_console_width, emoji_variant="text", color_system="auto")


HELP_TEXT = """[bold bright_white]可用命令:[/bold bright_white]

  [cyan]/help[/cyan]    显示此帮助
  [cyan]/exit[/cyan]    退出 PatchFlow
  [cyan]/quit[/cyan]    同上
  [cyan]/clear[/cyan]   清空对话历史
  [cyan]/history[/cyan] 显示对话统计
  [cyan]/memory[/cyan]  显示记忆状态（智能检测，自动记忆开发任务）
  [cyan]/model[/cyan]   列出/切换可用模型
  [cyan]/plan[/cyan]    制定计划后分步骤生成代码（AI 先出计划，用户确认后执行）
  [cyan]/build[/cyan]   一次性生成代码并自动验证
  [cyan]/fix[/cyan]     多 Agent 协作修复代码问题（Analyzer → Fixer → Reviewer）
  [cyan]/context[/cyan] 查看当前对话上下文（消息数、token、最近消息内容）
  [cyan]/init[/cyan]    创建项目级 PatchFlow 规则文件 (.patchflow/rules.md)
  [cyan]/stop[/cyan]    停止后台进程，如 /stop 2
  [cyan]/ps[/cyan]      查看所有后台进程

也可以直接输入任意问题或任务描述。"""


def _interactive_select(options: list[tuple[str, str, str]]) -> str:
    """交互式选择器 — 方向键上下切换，回车确认

    如果终端不支持交互（非 TTY），30 秒后自动选第一个（继续）。

    Args:
        options: [(返回值, 标签, 颜色), ...]

    Returns:
        选中项的返回值
    """
    # 非交互终端 → 直接选第一个
    if not sys.stdin.isatty():
        print("  [dim]（非交互终端，自动继续）[/dim]")
        return options[0][0]

    selected = 0
    _first_render = [True]
    n_opts = len(options)
    start = time.time()

    def _render():
        if _first_render[0]:
            _first_render[0] = False
            for i, (_, label, color) in enumerate(options):
                _print_option(i, label, color, i == selected)
        else:
            print(f"\033[{n_opts}A", end="", flush=True)
            for i, (_, label, color) in enumerate(options):
                _print_option(i, label, color, i == selected)

    def _print_option(i: int, label: str, color: str, is_selected: bool):
        col = "32" if color == "green" else "31" if color == "red" else "0"
        if is_selected:
            print(f"\r  \033[1;{col}m❯ {i + 1}. {label}\033[0m\033[K")
        else:
            print(f"\r  \033[2m{i + 1}. {label}\033[0m\033[K")

    _render()

    while True:
        # 30 秒无输入自动继续
        if time.time() - start > 30:
            print("\n  [dim]（等待超时 30s，自动继续）[/dim]")
            return options[0][0]

        key = _getch(timeout=1)
        if key is None:
            continue
        if key == "UP":
            selected = (selected - 1) % n_opts
            _render()
            start = time.time()
        elif key == "DOWN":
            selected = (selected + 1) % n_opts
            _render()
            start = time.time()
        elif key == "ENTER":
            return options[selected][0]
        elif key in ("1", "2", "3", "4", "5", "6", "7", "8", "9"):
            idx = int(key) - 1
            if 0 <= idx < n_opts:
                return options[idx][0]

def _getch(timeout: float = 0) -> str | None:
    """跨平台获取单字符输入，返回 UP/DOWN/ENTER/数字

    Args:
        timeout: 超时秒数（0 = 无限等待）

    Returns:
        按键对应的字符串，超时返回 None
    """
    if sys.platform == "win32":
        import msvcrt
        deadline = time.time() + timeout if timeout > 0 else float("inf")
        while True:
            if time.time() > deadline:
                return None
            if msvcrt.kbhit():
                ch = msvcrt.getch()
                if ch == b"\xe0":
                    ch2 = msvcrt.getch()
                    if ch2 == b"H":
                        return "UP"
                    elif ch2 == b"P":
                        return "DOWN"
                elif ch == b"\r":
                    return "ENTER"
                elif ch in (b"1", b"2", b"3", b"4", b"5", b"6", b"7", b"8", b"9"):
                    return ch.decode()
            time.sleep(0.05)
    else:
        import select
        import termios
        import tty
        fd = sys.stdin.fileno()
        old = termios.tcgetattr(fd)
        try:
            tty.setraw(fd)
            if timeout > 0:
                r, _, _ = select.select([sys.stdin], [], [], timeout)
                if not r:
                    return None
            ch = sys.stdin.read(1)
            if ch == "\x1b":
                ch2 = sys.stdin.read(2)
                if ch2 == "[A":
                    return "UP"
                elif ch2 == "[B":
                    return "DOWN"
            elif ch == "\r":
                return "ENTER"
            elif ch in "123456789":
                return ch
            return None
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old)


class REPL:
    """交互式对话循环"""

    def __init__(self, model: str | None = None):
        cfg = get_config()
        self.model = model or cfg["model"]
        self.client: ChatClient | None = None
        self._history: list[str] = []
        self._hist_idx: int = 0

    def run(self):
        """启动 REPL 主循环

        流程：
          1. 首次运行检查（没有 API Key 则显示引导信息）
          2. 设置终端标题
          3. 确保 .patchflow/ 被 Git 忽略
          4. 显示欢迎面板（Logo + 模型信息 + 提示）
          5. 进入 while True 循环：
             - 读取用户输入
             - 检查是否是命令（以 / 开头）
             - 命令 → _handle_cmd()
             - 普通输入 → _chat()
          6. KeyboardInterrupt / EOFError → 退出
        """
        try:
            sys.stdin.reconfigure(encoding="utf-8")
        except (AttributeError, OSError):
            pass
        cfg = get_config()

        # ── 检查配置状态 ──
        first_run = not cfg.get("api_key")
        if first_run:
            outer_w = min(_term_w, 80)
            inner_w = outer_w - 4
            console.print()
            console.print(Panel(
                "[bold]欢迎使用 PatchFlow![/bold]\n\n"
                "看起来是首次运行，需要先配置 API Key：\n\n"
                "  快速配置（推荐）:\n"
                "    [cyan]patchflow config init[/cyan]  → 交互式引导\n\n"
                "  或手动配置:\n"
                "    [cyan]patchflow config set api_key <你的 API Key>[/cyan]\n"
                "    [cyan]patchflow config set provider anthropic|openai|deepseek[/cyan]\n"
                "    [cyan]patchflow config set model <模型名>[/cyan]\n\n"
                f"[dim]{'-' * (inner_w - 4)}[/dim]\n"
                "常用模型：\n"
                "  1. [bold]Anthropic Claude[/bold]  (默认)\n"
                "     → 只需配 api_key\n"
                "  2. [bold]OpenAI[/bold]\n"
                "     → 还需: provider=openai, model=gpt-4o\n"
                "  3. [bold]DeepSeek[/bold]\n"
                "     → 还需: provider=deepseek, model=deepseek-chat\n\n"
                "配置后重启即可。查看配置: [cyan]patchflow config show[/cyan]",
                border_style="yellow",
                padding=(1, 2),
            ))
            return

        # ── 设置终端标题 ──
        if sys.platform == "win32":
            os.system("title 🦉 PatchFlow")
        else:
            sys.stdout.write("\x1b]0;🦉 PatchFlow\x07")
            sys.stdout.flush()

        # ── Git 忽略 ──
        try:
            from patchflow.core.project.codebase_index import _ensure_gitignore
            _ensure_gitignore()
        except Exception as e:
            logger.debug(f"Gitignore 设置失败: {e}")

        console.print()

        inner_w = _console_width - 4

        content = Text.from_markup(
            "\n"
            "  [bold cyan]██████╗  █████╗ ████████╗ ██████╗██╗  ██╗███████╗\n"
            "  [bold cyan]██╔══██╗██╔══██╗╚══██╔══╝██╔════╝██║  ██║██╔════╝\n"
            "  [bold cyan]██████╔╝███████║   ██║   ██║     ███████║█████╗  \n"
            "  [bold cyan]██╔═══╝ ██╔══██║   ██║   ██║     ██╔══██║██╔══╝  \n"
            "  [bold cyan]██║     ██║  ██║   ██║   ╚██████╗██║  ██║██║     \n"
            "  [bold cyan]╚═╝     ╚═╝  ╚═╝   ╚═╝    ╚═════╝╚═╝  ╚═╝╚═╝     [/bold cyan]\n"
            "\n"
            f"  [cyan]模型:[/cyan] {cfg.get('model', 'deepseek-chat')}\n"
            "  [dim]记忆: 智能模式（自动记忆开发任务）[/dim]\n"
            f"  [dim]{os.getcwd()}[/dim]\n"
            "\n"
            "  [bold]Tips for getting started[/bold]\n"
            "  Run [bold]/init[/bold] to create instructions for PatchFlow in your project\n"
            "  Run [bold]/ps[/bold] to see running servers, [bold]/stop <pid>[/bold] to stop them\n"
            "  Run [bold]/memory[/bold] to check memory status\n"
            f"  [dim]{'-' * (inner_w - 4)}[/dim]\n"
            "  [bold]What's new[/bold]\n"
            "  Check the changelog for updates\n"
        )

        console.print(Panel(content, border_style="cyan", padding=(1, 2)))

        set_confirm_callback(self._confirm_dangerous_command)

        while True:
            try:
                user_input = self._read_input()
                if user_input is None:
                    self._do_exit()
                    return

                user_input = user_input.strip()
                if not user_input:
                    continue

                if user_input.startswith("/"):
                    if self._handle_cmd(user_input):
                        return
                    continue

                self._chat(user_input)

            except KeyboardInterrupt:
                _CANCELLED.set()
                console.print("\n  [yellow]已取消[/yellow] [dim](输入 /exit 退出)[/dim]")
                continue
            except EOFError:
                self._do_exit()
                return

    def _read_input(self) -> str | None:
        try:
            import msvcrt
        except ImportError:
            try:
                line = input("\033[36mPatchFlow >>\033[0m ")
                return line
            except (KeyboardInterrupt, EOFError):
                return None

        prompt_str = "\033[36mPatchFlow >>\033[0m "
        prompt_w = 13

        def _width(s: str) -> int:
            return sum(2 if ord(c) > 0x2e80 else 1 for c in s)

        chars: list[str] = []
        cursor = 0
        hist_idx = self._hist_idx
        last_w = 0
        sys.stdout.write(prompt_str)
        sys.stdout.flush()
        while True:
            ch = msvcrt.getwch()
            if ch == "\r" or ch == "\n":
                console.print()
                break
            if ch == "\b" or ch == "\x7f":
                if cursor > 0:
                    cursor -= 1
                    chars.pop(cursor)
                    text = "".join(chars)
                    w = _width(text)
                    sys.stdout.write("\r" + " " * max(prompt_w + w, prompt_w + last_w) + "\r" + prompt_str + text)
                    last_w = w
                    after = _width(text[cursor:])
                    if after:
                        sys.stdout.write("\b" * after)
                    sys.stdout.flush()
                continue
            if ch == "\x03":
                raise KeyboardInterrupt
            if ch == "\x1a":
                raise EOFError
            if ch == "\xe0":
                nxt = msvcrt.getwch()
                if nxt == "K":
                    if cursor > 0:
                        cursor -= 1
                        sys.stdout.write("\b")
                        sys.stdout.flush()
                elif nxt == "M":
                    if cursor < len(chars):
                        sys.stdout.write(chars[cursor])
                        cursor += 1
                        sys.stdout.flush()
                elif nxt == "H":
                    if self._history and hist_idx > 0:
                        hist_idx -= 1
                        chars = list(self._history[hist_idx])
                        cursor = len(chars)
                        text = "".join(chars)
                        w = _width(text)
                        sys.stdout.write("\r" + " " * max(prompt_w + w, prompt_w + last_w) + "\r" + prompt_str + text)
                        last_w = w
                        sys.stdout.flush()
                elif nxt == "P":
                    if hist_idx < len(self._history) - 1:
                        hist_idx += 1
                        chars = list(self._history[hist_idx])
                    else:
                        hist_idx = len(self._history)
                        chars = []
                    cursor = len(chars)
                    text = "".join(chars)
                    w = _width(text)
                    sys.stdout.write("\r" + " " * max(prompt_w + w, prompt_w + last_w) + "\r" + prompt_str + text)
                    last_w = w
                    sys.stdout.flush()
                continue
            chars.insert(cursor, ch)
            cursor += 1
            text = "".join(chars)
            w = _width(text)
            sys.stdout.write("\r" + " " * max(prompt_w + w, prompt_w + last_w) + "\r" + prompt_str + text)
            last_w = w
            after = _width(text[cursor:])
            if after:
                sys.stdout.write("\b" * after)
            sys.stdout.flush()

        self._hist_idx = hist_idx
        result = "".join(chars)
        if result:
            self._history.append(result)
            self._hist_idx = len(self._history)
        return result

    def _confirm_dangerous_command(self, command: str, reason: str) -> str:
        display_cmd = command if len(command) < 70 else command[:67] + "..."
        console.print()
        console.print(Panel(
            f"[yellow]⚡ {reason}[/yellow]\n\n[dim]{display_cmd}[/dim]",
            border_style="yellow",
            padding=(1, 2),
        ))
        console.print("  (方向键 ↑↓ 切换, 回车确认, 或按数字快速选择)")
        result = _interactive_select([
            ("allow",    "允许本次",       "green"),
            ("reject",   "拒绝",           "red"),
            ("whitelist","加入白名单",     "green"),
            ("blacklist","加入黑名单",     "red"),
        ])
        if result == "allow":
            console.print("[green]  ✓ 已允许[/green]")
        elif result == "reject":
            console.print("[red]  ✗ 已拒绝[/red]")
        elif result == "whitelist":
            console.print("[green]  ✓ 已加入白名单[/green]")
        elif result == "blacklist":
            console.print("[red]  ✓ 已加入黑名单[/red]")
        return result

    def _handle_cmd(self, raw: str) -> bool:
        raw = raw.strip()
        cmd = raw.split()[0].lower()
        arg = raw[len(cmd):].strip()

        if cmd == "/exit" or cmd == "/quit":
            return True
        elif cmd == "/help":
            for line in HELP_TEXT.strip().split("\n"):
                console.print(Text.from_markup(line))
        elif cmd == "/clear":
            if self.client:
                self.client.clear_history()
                console.print("[green]对话历史已清空[/green]")
            else:
                console.print("[dim]还没有对话历史[/dim]")
        elif cmd == "/history":
            if self.client:
                console.print(f"[dim]{self.client.get_summary()}[/dim]")
            else:
                console.print("[dim]还没有对话历史[/dim]")
        elif cmd == "/model":
            self._cmd_model(arg)
        elif cmd == "/build":
            if not arg:
                console.print("[yellow]用法: /build <任务描述>[/yellow]")
            else:
                self._do_build(arg)
        elif cmd == "/plan":
            if not arg:
                console.print("[yellow]用法: /plan <任务描述>[/yellow]")
                console.print("  [dim]示例: /plan 创建一个 FastAPI TODO 应用[/dim]")
            else:
                self._do_plan(arg)
        elif cmd == "/fix":
            if not arg:
                console.print("[yellow]用法: /fix <任务描述>[/yellow]")
            else:
                self._do_fix(arg)
        elif cmd == "/context":
            self._cmd_context()
        elif cmd == "/memory":
            self._cmd_memory()
        elif cmd == "/init":
            self._cmd_init()
        elif cmd == "/stop":
            self._cmd_stop(arg)
        elif cmd == "/ps":
            self._cmd_ps()
        else:
            console.print(f"[red]未知命令: {cmd}[/red] [dim]输入 /help 查看帮助[/dim]")

        return False

    def _chat(self, user_input: str):
        """处理用户输入，启动流式对话

        这是 REPL 的核心方法。处理流程：
          1. 首次对话时创建 ChatClient 实例
          2. 启动 spinner 动画（表示"思考中"）
          3. 消费 chat_stream() 的事件流：
             - text → 流式输出文本
             - tool_start → 显示工具调用摘要（如 [read file.py]）
             - tool_result → 显示工具调用结果（成功/失败/行数）
             - usage → 更新 token 用量统计
             - hint → 轮数限制提示（用户选择继续/停止）
             - done → 对话结束
          4. 显示最终结果：Markdown 渲染、diff、搜索等
          5. 自动修复：如果检测到运行失败且有写文件，启动多 Agent 修复
        """
        _CANCELLED.clear()
        if self.client is None:
            try:
                from patchflow.core.config import get_model as cfg_model
                self.model = cfg_model()
                self.client = ChatClient(model=self.model, work_dir=os.getcwd())
            except ValueError as e:
                if "API Key" in str(e):
                    console.print()
                    console.print(Panel(
                        "[yellow]未配置 API Key[/yellow]\n\n"
                        "首次使用需要配置 API Key：\n\n"
                        "  [cyan]patchflow config set api_key <你的 API Key>[/cyan]\n\n"
                        "如果用非 Anthropic 的模型：\n"
                        "  [cyan]patchflow config set provider openai|deepseek|anthropic[/cyan]\n"
                        "  [cyan]patchflow config set model <模型名>[/cyan]\n\n"
                        "配置完成后重新运行即可。",
                        border_style="yellow",
                        padding=(1, 2),
                    ))
                else:
                    console.print(f"[red]错误: {e}[/red]")
                return

        files_written = []
        run_failures = []
        all_tool_calls = []
        streaming_text = ""
        tree_outputs: list[str] = []
        search_outputs: list[tuple[str, str]] = []
        session_usage = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0, "calls": 0}

        stop_spinner = [False]
        spinner_start_time = [0.0]
        spinner_warning_shown = [False]

        def _start_spinner(stop: list[bool]) -> None:
            frames = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]
            i = 0
            start = time.time()
            spinner_start_time[0] = start
            spinner_warning_shown[0] = False
            while not stop[0]:
                elapsed = time.time() - start
                if elapsed > 60 and not spinner_warning_shown[0]:
                    spinner_warning_shown[0] = True
                if elapsed > 60:
                    msg = f"等待中... ({int(elapsed)}s)"
                    print(f"\r\033[33m{frames[i]}\033[0m \033[33m{msg}\033[0m \033[2m按 Ctrl+C 取消\033[0m", end="", flush=True)
                elif elapsed > 20:
                    msg = f"思考中... ({int(elapsed)}s)"
                    print(f"\r\033[36m{frames[i]}\033[0m \033[33m{msg}\033[0m", end="", flush=True)
                else:
                    print(f"\r\033[36m{frames[i]}\033[0m \033[2m思考中...\033[0m", end="", flush=True)
                i = (i + 1) % len(frames)
                threading.Event().wait(0.12)
            clear_len = 55 if spinner_warning_shown[0] else 30
            print("\r" + " " * clear_len + "\r", end="", flush=True)

        spinner_thread = threading.Thread(target=_start_spinner, args=(stop_spinner,), daemon=True)
        spinner_thread.start()

        try:
            _stream_done = [False]
            _current_input = user_input

            while not _stream_done[0]:
                for evt, data in self.client.chat_stream(_current_input):
                    stop_spinner[0] = True
                    spinner_thread.join()

                    if evt == "text":
                        streaming_text = data

                    elif evt == "tool_start":
                        name = data["name"]
                        args = data["args"]
                        if name == "read_file":
                            fn = args.get("filename", "?")
                            console.print(f"  [cyan]read[/cyan]  [dim]{fn}[/dim]")
                        elif name == "write_file":
                            fn = args.get("filename", "")
                            console.print(f"  [green]✎ AI[/green] [dim]{fn}[/dim]")
                        elif name == "list_files":
                            p = args.get("path", ".")
                            console.print(f"  [cyan]scan[/cyan]  [dim]{p}[/dim]")
                        elif name == "delete_file":
                            fn = args.get("filename", "")
                            console.print(f"  [red]del[/red]  [dim]{fn}[/dim]")
                        elif name == "rename_file":
                            src = args.get("source", "")
                            dst = args.get("dest", "")
                            console.print(f"  [blue]mv[/blue]   [dim]{src} -> {dst}[/dim]")
                        elif name == "run_code":
                            cmd = args.get("command", "?")
                            display = cmd if len(cmd) < 80 else cmd[:77] + "..."
                            console.print(f"  [yellow]$[/yellow] [bold]{display}[/bold]")
                        elif name == "search_files":
                            q = args.get("query", "?")
                            console.print(f"  [magenta]search[/magenta] [dim]{q}[/dim]")
                        elif name == "search_code":
                            p = args.get("pattern", "?")
                            console.print(f"  [magenta]grep[/magenta]  [dim]{p}[/dim]")
                        elif name == "review_code":
                            fn = args.get("filepath", "?")
                            console.print(f"  [yellow]review[/yellow] [dim]{fn}[/dim]")
                        elif name == "batch_read_files":
                            files = args.get("files", [])
                            files_preview = ", ".join(files[:3])
                            if len(files) > 3:
                                files_preview += f" (+{len(files)-3} more)"
                            console.print(f"  [cyan]batch[/cyan] [dim]{files_preview}[/dim]")

                    elif evt == "tool_result":
                        name = data["name"]
                        result = data.get("result", "")
                        if result.startswith("BUDGET:"):
                            msg = result[len("BUDGET: "):] if result.startswith("BUDGET: ") else result
                            console.print(f"    [yellow]⚠ {msg}[/yellow]")
                            all_tool_calls.append(data)
                            stop_spinner[0] = False
                            spinner_thread = threading.Thread(target=_start_spinner, args=(stop_spinner,), daemon=True)
                            spinner_thread.start()
                            continue
                        if name == "read_file":
                            if result.startswith("ERROR"):
                                console.print("    [red]✘[/red] [dim]read failed[/dim]")
                            elif result == "(tool skipped — budget exhausted)":
                                console.print("    [dim](skipped — budget exhausted)[/dim]")
                            elif result.startswith("(already read"):
                                console.print(f"    [dim]{result}[/dim]")
                            else:
                                n = len(result)
                                console.print(f"    [dim]{n} chars[/dim]")
                        elif name == "write_file":
                            content = data["args"].get("content", "")
                            n = len(content.split("\n"))
                            console.print(f"    [dim]{n} lines[/dim]")
                        elif name == "list_files":
                            n = sum(1 for line in result.split("\n") if line.strip().startswith(("├", "└")))
                            if n:
                                console.print(f"    [dim]{n} items[/dim]")
                            tree_outputs.append(result)
                        elif name == "run_code":
                            if result.startswith("BLOCKED:"):
                                reason = result[len("BLOCKED: "):]
                                console.print(f"    [red]✘ blocked[/red] [dim]{reason}[/dim]")
                            elif result.startswith("USER_REJECTED:"):
                                console.print("    [yellow]✘ rejected[/yellow]")
                            elif result.startswith("BACKGROUND_STARTED:"):
                                pid_line = result.split("\n")[0]
                                pid = pid_line.split("=")[-1]
                                cmd = result.split("\n")[1].replace("Command: ", "") if "Command:" in result else ""
                                console.print(f"    [green]✔ background[/green] [dim](PID {pid})[/dim]")
                                if cmd:
                                    console.print(f"      [dim]/stop {pid} 停止进程[/dim]")
                            elif result == "(tool skipped — budget exhausted)":
                                console.print("    [dim](skipped — budget exhausted)[/dim]")
                            else:
                                ok = result.startswith("exit: 0")
                                if ok:
                                    console.print("    [green]✔ success[/green]")
                                else:
                                    exit_code = result.split("\n")[0].replace("exit: ", "") if result.startswith("exit:") else "?"
                                    stderr_part = result.split("stderr:")[-1].strip() if "stderr:" in result else ""
                                    stdout_part = result.split("stdout:")[-1].split("stderr:")[0].strip() if "stdout:" in result else ""
                                    error_line = (stderr_part or stdout_part)[:80].replace("\n", " ")
                                    if error_line:
                                        console.print(f"    [red]✘ exit {exit_code}[/red] [dim]{error_line}[/dim]")
                                    else:
                                        console.print(f"    [red]✘ exit {exit_code}[/red]")
                        elif name == "search_files":
                            n = result.count("\n") + 1 if result.strip() else 0
                            if "未找到" in result:
                                console.print("    [yellow]no results[/yellow]")
                            else:
                                console.print(f"    [magenta]{n}[/magenta] [dim]files[/dim]")
                                search_outputs.append((f"  search: {data['args'].get('query', '')}", result))
                        elif name == "search_code":
                            n = result.count("\n") + 1 if result.strip() else 0
                            if "no matches" in result:
                                console.print("    [yellow]no matches[/yellow]")
                            else:
                                console.print(f"    [magenta]{n}[/magenta] [dim]matches[/dim]")
                                preview = "\n".join(result.split("\n")[:8])
                                more = f"\n  ... (total {n} matches)" if n > 8 else ""
                                search_outputs.append((f"  grep: {data['args'].get('pattern', '')[:50]}", f"{preview}{more}"))

                        all_tool_calls.append(data)
                        if name == "write_file":
                            files_written.append(data["args"].get("filename", ""))
                        elif name == "run_code" and (result.startswith("exit:") and not result.startswith("exit: 0")):
                            run_failures.append(data)
                        elif name == "review_code":
                            if result.startswith("ERROR"):
                                console.print("    [red]✘ review failed[/red]")
                            elif result == "(tool skipped — budget exhausted)":
                                console.print("    [dim](skipped — budget exhausted)[/dim]")
                            elif "未发现明显问题" in result:
                                console.print("    [green]✔ 无问题[/green]")
                            else:
                                first = result.split("\n")[0]
                                console.print(f"    [yellow]{first}[/yellow]")
                        elif name == "batch_read_files":
                            file_count = len(data["args"].get("files", []))
                            console.print(f"    [dim]{file_count} files[/dim]")

                    elif evt == "usage":
                        session_usage = data

                    elif evt == "hint":
                        console.print()
                        console.print(Panel(
                            "[yellow]本轮已用 30 轮工具调用，任务尚未完成[/yellow]\n\n"
                            "[dim]选择后续操作:[/dim]",
                            border_style="yellow",
                            padding=(1, 2),
                        ))
                        choice = _interactive_select([
                            ("continue", "继续（再给 30 轮）",  "green"),
                            ("stop",     "到此为止，看结果",    "red"),
                            ("always",   "不再限制（本次对话）", "green"),
                        ])
                        if choice == "continue":
                            console.print("[green]  ✓ 继续执行[/green]")
                            _current_input = "请继续，从上一次中断的地方开始。"
                            break
                        elif choice == "always":
                            console.print("[green]  ✓ 已取消轮数限制[/green]")
                            self.client._max_rounds = 999
                            _current_input = "请继续，从上一次中断的地方开始。"
                            break
                        else:
                            console.print("[yellow]  ✓ 停止，看结果[/yellow]")
                            _stream_done[0] = True
                            break

                    elif evt == "done":
                        _stream_done[0] = True
                        break

                    stop_spinner[0] = False
                    spinner_thread = threading.Thread(target=_start_spinner, args=(stop_spinner,), daemon=True)
                    spinner_thread.start()

        except BaseException as e:
            stop_spinner[0] = True
            try:
                spinner_thread.join()
            except RuntimeError:
                pass
            if isinstance(e, KeyboardInterrupt):
                _CANCELLED.set()
                console.print("\n  [yellow]已取消[/yellow]")
                streaming_text = ""
            else:
                streaming_text = f"请求失败: {e}"

        stop_spinner[0] = True
        try:
            spinner_thread.join()
        except RuntimeError:
            pass

        if tree_outputs:
            for tree in tree_outputs:
                for line in tree.split("\n"):
                    console.print(Text(line, style="dim"))
            console.print()

        if search_outputs:
            for title, content in search_outputs:
                console.print(Text(title, style="bold cyan"))
                for line in content.split("\n"):
                    display = line[:_console_width - 4] if len(line) > _console_width - 4 else line
                    console.print(Text(f"  {display}", style="dim"))
            console.print()

        if session_usage["calls"] > 0:
            u = session_usage
            console.print(f"  [dim]━━━ Token: {u['total_tokens']} total "
                          f"({u['input_tokens']} in + {u['output_tokens']} out) "
                          f"· {u['calls']} LLM call{'s' if u['calls'] > 1 else ''}"
                          f" · /context 查看详情[/dim]")

        if streaming_text.strip():
            # 在 AI 回复前显示上下文摘要
            if self.client and len(self.client.messages) > 2:
                from patchflow.core.project.context_manager import estimate_message_tokens
                msgs = self.client.messages
                tok = sum(estimate_message_tokens(m) for m in msgs)
                console.print(f"  [dim]上下文: {len(msgs)} 条消息, ~{tok} token[/dim]")

        if run_failures and files_written:
            task = streaming_text[:200] if streaming_text else "修复运行错误"
            console.print(f"  [bold yellow]检测到 {len(run_failures)} 个运行失败，启动多 Agent 修复...[/bold yellow]")
            self._auto_fix(run_failures, files_written, task)

        if streaming_text.strip():
            safe_text = streaming_text.encode("utf-8", errors="replace").decode("utf-8")
            md = Markdown(safe_text, code_theme="monokai")
            try:
                console.print(md)
            except UnicodeEncodeError:
                console.print(safe_text)
            console.print()

    def _auto_fix(self, run_failures: list[dict], files_written: list[str], task: str):
        from patchflow.agents.blackboard import Blackboard
        from patchflow.core.agent_orchestrator import AgentOrchestrator

        error_summary = "\n".join(
            f"$ {tc['args'].get('command', '')}\n{tc['result'][:500]}"
            for tc in run_failures
        )

        # 读取已写入的文件内容
        files_filtered = [f for f in files_written if f and f.strip() != "."]
        file_contents = {}
        for fp in files_filtered:
            p = Path(fp)
            if p.exists():
                try:
                    file_contents[fp] = p.read_text(encoding="utf-8")
                except Exception as e:
                    logger.debug(f"读取文件失败 {fp}: {e}")

        console.print(f"  [bold yellow]⚡ 启动多 Agent 修复: {task[:80]}[/bold yellow]")

        bb = Blackboard(
            task=task,
            context={"files_changed": files_filtered},
            code=file_contents,
            error=error_summary,
        )

        orch = AgentOrchestrator(model=self.model, work_dir=".")
        success = orch.run(bb)

        if success:
            console.print(f"  [green]  ✅ 多 Agent 修复成功 (共 {orch.turn_count} 步)[/green]")
        else:
            console.print("  [red]  ❌ 多 Agent 修复失败，已回滚[/red]")

    def _do_exit(self):
        console.print("[dim]再见![/dim]")
        self.client = None

    def _cmd_model(self, arg: str):
        from patchflow.core.config import list_models, set_active_model

        models = list_models()

        if arg:
            if set_active_model(arg):
                console.print(f"[green]已切换到模型: {arg}[/green]")
                self.client = None
            else:
                console.print(f"[red]未知模型: {arg}[/red]")
            return

        if not models:
            console.print("[yellow]未配置任何模型[/yellow]")
            console.print("[dim]使用 patchflow config set api_key <key> 添加[/dim]")
            return

        for alias, info in models.items():
            console.print(f"  [cyan]{alias}[/cyan] [dim]({info.get('provider', '?')})[/dim]")

    def _do_build(self, task: str):
        from patchflow.core.orchestrator import Orchestrator

        console.print(f"[dim]任务: {task}[/dim]")
        console.print(f"[dim]模型: {self.model}[/dim]")

        orchestrator = Orchestrator(model=self.model)
        success = orchestrator.run(task)

        if success:
            console.print(f"[green]成功完成! (修复 {orchestrator.state['turn']} 轮)[/green]")
        else:
            console.print("[red]构建失败, 请重试或检查模型配置[/red]")
            console.print()

    def _do_fix(self, task: str):
        from patchflow.core.agent_orchestrator import AgentOrchestrator

        console.print(f"[dim]任务: {task}[/dim]")
        console.print(f"[dim]模型: {self.model}[/dim]")
        console.print("[yellow]启动多 Agent 协作模式 (Analyzer → Fixer → Reviewer)...[/yellow]")

        orch = AgentOrchestrator(model=self.model, work_dir=".")
        success = orch.run_from_task(task)

        if success:
            console.print(f"[green]多 Agent 协作修复成功! (共 {orch.turn_count} 步)[/green]")
        else:
            console.print("[red]修复失败, 请重试或检查模型配置[/red]")
            console.print()

    def _do_plan(self, task: str):
        """制定计划后分步骤生成代码"""
        from rich.table import Table

        from patchflow.core.planner import PlanExecutor

        console.print(f"[dim]任务: {task}[/dim]")
        console.print(f"[dim]模型: {self.model}[/dim]")
        console.print()

        executor = PlanExecutor(model=self.model, work_dir=".")

        plan = executor.generate_plan(task)
        if plan is None or not plan.steps:
            console.print("[red]计划生成失败[/red]")
            return

        # ── 显示计划 ──
        table = Table(title=f"Plan: {plan.summary}", title_style="bold cyan", border_style="cyan")
        table.add_column("#", style="dim", width=3)
        table.add_column("Step", style="bold", width=30)
        table.add_column("Description", style="dim", width=60)

        for s in plan.steps:
            files_hint = ", ".join(s.files_expected[:3])
            if len(s.files_expected) > 3:
                files_hint += "..."
            desc = f"{s.description} [dim]({files_hint})[/dim]" if files_hint else s.description
            table.add_row(str(s.step), s.title, desc)

        console.print(table)
        console.print()

        # ── 确认 ──
        console.print("[bold]是否按此计划执行?[/bold]")
        console.print("  [green]y[/green] — 开始执行")
        console.print("  [red]n[/red] — 取消")

        try:
            confirm = input().strip().lower()
        except (EOFError, KeyboardInterrupt):
            confirm = "n"
            console.print("[dim]已取消[/dim]")

        if confirm != "y" and confirm != "yes":
            console.print("[yellow]计划已取消[/yellow]")
            return

        # ── 执行 ──
        console.print()
        console.print("[bold cyan]开始执行计划...[/bold cyan]")
        console.print()

        total = len(plan.steps)
        all_ok = True

        for i, step in enumerate(plan.steps):
            step_num = f"[{i + 1}/{total}]"
            console.print(f"  {step_num} [bold]{step.title}[/bold]")
            console.print(f"       [dim]{step.description}[/dim]")
            console.print("       [cyan]⠋ 生成中...[/cyan]")

            ok = executor.execute_step(i)

            if ok:
                files_str = ", ".join(step.files_written[:3])
                extra = f" (+{len(step.files_written) - 3} files)" if len(step.files_written) > 3 else ""
                console.print(f"\r  {step_num} [green]v[/green] [bold]{step.title}[/bold]")
                if files_str:
                    console.print(f"       [dim]{files_str}{extra}[/dim]")
                console.print()
            else:
                console.print(f"\r  {step_num} [red]x[/red] [bold]{step.title}[/bold]")
                console.print(f"       [red]{step.error or '步骤失败'}[/red]")
                console.print()
                all_ok = False
                break

        # ── 最终验证 ──
        if all_ok:
            console.print("[bold cyan]执行完成, 正在最终验证...[/bold cyan]")
            from patchflow.core.fix.validator import validate
            result = validate(work_dir=".")
            if result.ok:
                console.print("[green]v 验证通过[/green]")
                console.print(f"[green bold]成功完成! ({total} 步)[/green bold]")
            else:
                console.print(f"[yellow]验证: {result.message or '未通过'}[/yellow]")
                console.print(f"[green bold]执行完成 ({total} 步), 但验证未完全通过[/green bold]")
        else:
            console.print(f"[red]执行中断 (完成 {i + 1}/{total} 步)[/red]")

        console.print()

    def _cmd_context(self):
        """显示当前对话上下文的详细结构"""
        if self.client is None:
            console.print("[yellow]还没有对话，无上下文[/yellow]")
            return

        preview = self.client.get_context_preview()
        if not preview.strip():
            console.print("[yellow]上下文为空[/yellow]")
            return

        lines = preview.split("\n")
        console.print()
        console.print(f"  [bold]上下文总览[/bold]  [dim]{lines[0]}[/dim]")

        if len(lines) > 1 and lines[1].startswith("（"):
            console.print(f"  [dim]{lines[1]}[/dim]")
            start = 2
        else:
            start = 1

        console.print()
        for line in lines[start:]:
            line = line.strip()
            if not line:
                continue
            parts = line.split(None, 3)
            if len(parts) >= 4:
                idx = parts[0]
                tok = parts[1]
                rest = " ".join(parts[3:])
                console.print(f"  {idx}  [yellow]{tok}[/yellow]  {rest}")
        console.print()

    def _cmd_memory(self):
        """显示记忆状态（智能检测模式）"""
        if not self.client:
            console.print("  [dim]记忆: 智能模式（开始对话后自动检测）[/dim]")
            return

        memory_path = Path(".patchflow/memory.json")
        has_dev = self.client._has_dev_activity
        has_file = memory_path.exists()
        is_persisted = has_file and has_dev

        console.print("  [bold]记忆状态: 智能模式[/bold]")
        if is_persisted:
            console.print("  [green]● 已记忆[/green] [dim]— 检测到开发任务，对话已持久化[/dim]")
        elif has_file and not has_dev:
            console.print("  [yellow]○ 历史记忆存在[/yellow] [dim]— 当前会话尚未触发开发任务[/dim]")
        else:
            console.print("  [dim]○ 待检测 — 开始对话后将自动识别开发任务[/dim]")

        console.print(f"  {self.client.get_summary()}")

        if has_file:
            size = len(memory_path.read_bytes())
            limit_kb = self.client._MAX_MEMORY_BYTES // 1024
            if size > 1024:
                console.print(f"  文件: [dim].patchflow/memory.json ({size // 1024} KB / {limit_kb} KB)[/dim]")
            else:
                console.print(f"  文件: [dim].patchflow/memory.json ({size} B / {limit_kb} KB)[/dim]")
            pct = size / self.client._MAX_MEMORY_BYTES * 100
            if pct > 80:
                console.print(f"  状态: [yellow]已用 {pct:.0f}%，旧消息将被自动压缩为摘要[/yellow]")
            else:
                console.print(f"  状态: [green]{pct:.0f}% 已使用[/green]")
            summaries = self.client._memory_summary
            if summaries:
                console.print(f"  [bold]摘要预览 (最近 {min(3, len(summaries))} 条):[/bold]")
                for s in summaries[-3:]:
                    short = s[:80] + "..." if len(s) > 80 else s
                    console.print(f"    [dim]▪[/dim] {short}")
            boundary_count = sum(1 for m in self.client.messages if m.get("_session_boundary"))
            if boundary_count:
                console.print(f"  会话: {boundary_count} 次跨会话续聊")
        else:
            console.print("  文件: [dim](尚无持久化记忆)[/dim]")

        if getattr(self.client, '_session_boundary_added', False):
            console.print("  会话: [yellow]跨会话续聊（恢复自之前保存的记忆）[/yellow]")

        console.print("  [dim]说明: 系统自动检测开发意图（写代码/修bug/调试等），仅开发任务会被持久化[/dim]")

    def _cmd_init(self):
        """创建项目级 PatchFlow 指令文件"""

        rules_dir = Path(".patchflow")
        rules_file = rules_dir / "rules.md"
        pkg_file = Path("package.json")
        pyproject_file = Path("pyproject.toml")
        req_file = Path("requirements.txt")
        go_mod = Path("go.mod")
        cargo = Path("Cargo.toml")

        if rules_file.exists():
            console.print(f"  [yellow]规则文件已存在: {rules_file}[/yellow]")
            content = rules_file.read_text(encoding="utf-8")
            console.print("  [dim]当前内容:[/dim]")
            for line in content.strip().split("\n"):
                console.print(f"    [dim]{line}[/dim]")
            console.print()
            console.print(f"  [dim]直接编辑 {rules_file} 来修改规则[/dim]")
            return

        # 检测项目类型
        project_info = []
        if pkg_file.exists():
            try:
                pkg = json.loads(pkg_file.read_text(encoding="utf-8"))
                name = pkg.get("name", "")
                desc = pkg.get("description", "")
                deps = list(pkg.get("dependencies", {}).keys())[:5]
                dev_deps = list(pkg.get("devDependencies", {}).keys())[:5]
                project_info.append("- 类型: Node.js/JavaScript 项目")
                if name:
                    project_info.append(f"- 名称: {name}")
                if desc:
                    project_info.append(f"- 描述: {desc}")
                if deps:
                    project_info.append(f"- 依赖: {', '.join(deps)}")
                if dev_deps:
                    project_info.append(f"- 开发依赖: {', '.join(dev_deps)}")
            except Exception:
                project_info.append("- 类型: Node.js/JavaScript 项目")
        elif pyproject_file.exists():
            project_info.append("- 类型: Python 项目")
        elif req_file.exists():
            project_info.append("- 类型: Python 项目")
        elif go_mod.exists():
            project_info.append("- 类型: Go 项目")
        elif cargo.exists():
            project_info.append("- 类型: Rust 项目")
        else:
            project_info.append("- 类型: 未知（自动检测）")

        rules_content = f"""# PatchFlow Project Rules

{chr(10).join(project_info)}

## Convention

- Use existing code style and patterns
- Follow the project's existing directory structure
- Keep consistent naming conventions

## Guidelines

- Write clean, readable code
- Add necessary error handling
- Keep functions focused and concise

*Edit this file to customize PatchFlow's behavior in this project.*
"""
        rules_dir.mkdir(parents=True, exist_ok=True)
        rules_file.write_text(rules_content, encoding="utf-8")
        console.print(f"  [green]规则文件已创建: {rules_file}[/green]")
        console.print("  [dim]内容:[/dim]")
        for line in rules_content.strip().split("\n"):
            if line.startswith("#"):
                console.print(f"    [cyan]{line}[/cyan]")
            elif line.startswith("-"):
                console.print(f"    [green]{line}[/green]")
            elif line.strip():
                console.print(f"    [dim]{line}[/dim]")
        console.print()
        console.print(f"  [yellow]编辑 {rules_file} 可以自定义项目规则[/yellow]")
        console.print("  [yellow]PatchFlow 每次对话会自动注入这些规则[/yellow]")

    def _cmd_stop(self, arg: str):
        from patchflow.utils.runner import list_processes, stop_background
        if not arg:
            procs = [p for p in list_processes() if p.running]
            if not procs:
                console.print("  [yellow]没有运行中的后台进程[/yellow]")
                return
            console.print("  [yellow]用法: /stop <pid>[/yellow]")
            console.print("  [dim]运行中的进程:[/dim]")
            for p in procs:
                console.print(f"    [cyan]PID {p.pid}[/cyan] [dim]{p.command[:60]}[/dim]")
            return
        try:
            pid = int(arg.strip())
        except ValueError:
            console.print(f"  [red]无效 PID: {arg}[/red]")
            return
        if stop_background(pid):
            console.print(f"  [green]已停止 [/green] [dim]PID {pid}[/dim]")
        else:
            console.print(f"  [red]未找到进程: PID {pid}[/red]")

    def _cmd_ps(self):
        from patchflow.utils.runner import list_processes
        procs = list_processes()
        if not procs:
            console.print("  [yellow]没有后台进程[/yellow]")
            return
        console.print("  [bold]后台进程列表[/bold]")
        for p in procs:
            status = "[green]运行中[/green]" if p.running else "[dim]已结束[/dim]"
            cmd_short = p.command[:60] + "..." if len(p.command) > 60 else p.command
            console.print(f"  PID [cyan]{p.pid}[/cyan]  {status}  [dim]{cmd_short}[/dim]")
            if p.running:
                console.print(f"       [dim]/stop {p.pid} 停止此进程[/dim]")


def start_repl(model: str | None = None):
    """启动 REPL — 给 CLI 入口调用

    Args:
        model: 使用的 LLM 模型别名
    """
    repl = REPL(model=model)
    repl.run()
