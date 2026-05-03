"""命令执行器 — 封装 subprocess

三层执行模式：
  1. run()        同步阻塞 + 捕获全部输出（短命令，如 python app.py）
  2. run_live()   实时流式输出（中等命令，需看过程，如 pip install）
  3. run_bg()     后台执行（长驻命令，如 npm run dev）

危险命令分级：
  - LEVEL_1: 直接拦截（rm -rf /, format C:）
  - LEVEL_2: 需二次确认（del, rd /s, 关机等破坏性操作）

黑白名单：
  - 白名单命令跳过所有安全检查
  - 黑名单命令直接拦截，无需询问
"""

import subprocess
import sys
import threading
import time
import signal
from typing import Callable

from patchflow.utils import logger


DANGEROUS_LEVEL_1 = [
    "rm -rf /", "rm -rf /*",
    "del /f /s", "del /f /s /q",
    "format ", "format:",
    "shutdown", "halt", "poweroff",
    "> /dev/sda", "> /dev/sdb",
    "dd if=", "mkfs.",
    ":(){ :|:& };:",  # fork bomb
    "chmod 777 /", "chmod -R 777 /",
]

DANGEROUS_LEVEL_2 = [
    "del ", "rd ", "rmdir ", "rm -rf",
    "chmod ", "chown ",
    "> ", ">> ",
    "mv ", "move ",
    "sudo ", "runas ",
    "taskkill", "kill ",
    "reg ", "regedit",
    "net user", "net localgroup",
    "diskpart", "fdisk",
]

# ═══════════════════════════════════════════════════════════
# 黑白名单系统（持久化）
# ═══════════════════════════════════════════════════════════

_whitelist: set[str] = set()
_blacklist: set[str] = set()
_CANCELLED = threading.Event()
_SAFE_FILE: str | None = None


def _get_safe_file() -> str:
    global _SAFE_FILE
    if _SAFE_FILE is None:
        from pathlib import Path
        home = Path.home() / ".patchflow"
        home.mkdir(parents=True, exist_ok=True)
        _SAFE_FILE = str(home / "safe.json")
    return _SAFE_FILE


def _load_safe_list():
    import json
    f = _get_safe_file()
    try:
        with open(f, "r", encoding="utf-8") as fp:
            data = json.load(fp)
            _whitelist.clear()
            _whitelist.update(data.get("whitelist", []))
            _blacklist.clear()
            _blacklist.update(data.get("blacklist", []))
    except (FileNotFoundError, json.JSONDecodeError):
        pass


def _save_safe_list():
    import json
    f = _get_safe_file()
    with open(f, "w", encoding="utf-8") as fp:
        json.dump({
            "whitelist": sorted(_whitelist),
            "blacklist": sorted(_blacklist),
        }, fp, ensure_ascii=False, indent=2)


def _normalize_cmd(command: str) -> str:
    """归一化命令，用于黑白名单匹配"""
    return command.lower().strip()


def is_whitelisted(command: str) -> bool:
    if not _whitelist:
        _load_safe_list()
    norm = _normalize_cmd(command)
    for wl in _whitelist:
        if norm.startswith(wl):
            return True
    return False


def is_blacklisted(command: str) -> bool:
    if not _blacklist:
        _load_safe_list()
    norm = _normalize_cmd(command)
    for bl in _blacklist:
        if norm == bl or norm.startswith(bl):
            return True
    return False


def add_to_whitelist(command: str):
    _load_safe_list()
    _whitelist.add(_normalize_cmd(command))
    _save_safe_list()


def add_to_blacklist(command: str):
    _load_safe_list()
    _blacklist.add(_normalize_cmd(command))
    _save_safe_list()


# 启动时加载
_load_safe_list()

LONG_RUNNING_PATTERNS = [
    "npm run dev", "npm start", "npm run serve",
    "yarn dev", "yarn start",
    "pnpm dev", "pnpm start",
    "bun dev",
    "nodemon",
    "vue-cli-service serve",
    "ng serve", "ng build --watch",
    "webpack-dev-server", "webpack --watch",
    "vite", "vite dev",
    "python -m http.server",
    "python manage.py runserver",
    "flask run",
    "uvicorn ", "gunicorn ",
    "tail -f", "journalctl -f",
    "watch ",
    "docker compose up", "docker-compose up",
    "ping ", "tracert ",
    "spring-boot:run", "mvn spring-boot:run",
    "gradle bootRun",
    "nodemon", "ts-node-dev",
    "live-server",
    "start /b ", "start /B ",
]


class RunResult:
    """命令执行结果"""
    def __init__(self, exit_code: int, stdout: str, stderr: str):
        self.exit_code = exit_code
        self.stdout = stdout
        self.stderr = stderr

    @property
    def ok(self):
        return self.exit_code == 0

    def __repr__(self):
        return f"RunResult(exit={self.exit_code}, ok={self.ok})"


def classify_command(command: str) -> tuple[str, str | None]:
    """对命令进行安全分级

    检查顺序：黑名单 → 白名单 → LEVEL_1 → LEVEL_2 → allow

    Returns:
        (action, reason):
            action: "block" | "confirm" | "allow"
            reason: 说明文字（block/confirm 时）
    """
    # 黑名单优先
    if is_blacklisted(command):
        return ("block", f"该命令已被用户加入黑名单")

    # 白名单跳过检查
    if is_whitelisted(command):
        return ("allow", None)

    cmd_lower = command.lower().strip()

    for pattern in DANGEROUS_LEVEL_1:
        if pattern in cmd_lower:
            return ("block", f"高危命令已被拦截: {pattern}")

    for pattern in DANGEROUS_LEVEL_2:
        if cmd_lower.startswith(pattern) or f" {pattern}" in cmd_lower:
            return ("confirm", f"该操作可能具有破坏性: {pattern}")

    return ("allow", None)


def is_long_running(command: str) -> bool:
    """判断是否为长驻命令，需要后台运行"""
    cmd_lower = command.lower().strip()
    for pattern in LONG_RUNNING_PATTERNS:
        if pattern in cmd_lower:
            return True
    return False


def run(command: str, cwd: str = ".", timeout: int = 30) -> RunResult:
    """执行一条 shell 命令并返回结构化结果（同步阻塞）"""
    try:
        result = subprocess.run(
            command,
            shell=True,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
            encoding=_detect_encoding(),
            errors="replace",
        )
        return RunResult(
            exit_code=result.returncode,
            stdout=result.stdout,
            stderr=result.stderr,
        )
    except subprocess.TimeoutExpired:
        return RunResult(
            exit_code=-1,
            stdout="",
            stderr=f"Command timed out after {timeout}s: {command}",
        )


def _detect_encoding() -> str:
    """检测 Windows 系统编码，避免 GBK→UTF-8 乱码"""
    if sys.platform == "win32":
        import ctypes
        try:
            codepage = ctypes.windll.kernel32.GetACP()
            return f"cp{codepage}"
        except Exception:
            return "utf-8"
    return "utf-8"


def run_live(command: str, cwd: str = ".",
             timeout: int = 60,
             on_stdout: Callable[[str], None] | None = None,
             on_stderr: Callable[[str], None] | None = None) -> RunResult:
    """执行命令并实时输出（通过回调）

    使用 Popen + 线程逐行读取 stdout/stderr，通过回调实时通知调用方。
    超时后强制 kill 进程树。
    """
    stdout_lines = []
    stderr_lines = []

    process = subprocess.Popen(
        command,
        shell=True,
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding=_detect_encoding(),
        errors="replace",
        creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
    )

    def _read_stream(stream, is_stderr: bool):
        for raw_line in iter(stream.readline, ""):
            line = raw_line.rstrip("\n\r")
            if not line:
                continue
            if is_stderr:
                stderr_lines.append(line)
                if on_stderr:
                    on_stderr(line)
            else:
                stdout_lines.append(line)
                if on_stdout:
                    on_stdout(line)
        stream.close()

    t_out = threading.Thread(target=_read_stream, args=(process.stdout, False), daemon=True)
    t_err = threading.Thread(target=_read_stream, args=(process.stderr, True), daemon=True)
    t_out.start()
    t_err.start()

    deadline = time.time() + timeout
    while time.time() < deadline:
        if _CANCELLED.is_set():
            _CANCELLED.clear()
            _kill_process_tree(process.pid)
            process.wait()
            return RunResult(
                exit_code=-1,
                stdout="\n".join(stdout_lines),
                stderr="\nCancelled by user",
            )
        try:
            process.wait(timeout=0.5)
            break
        except subprocess.TimeoutExpired:
            continue
    else:
        _kill_process_tree(process.pid)
        process.wait()
        return RunResult(
            exit_code=-1,
            stdout="\n".join(stdout_lines),
            stderr="\n".join(stderr_lines) + f"\nCommand timed out after {timeout}s",
        )

    t_out.join()
    t_err.join()

    return RunResult(
        exit_code=process.returncode,
        stdout="\n".join(stdout_lines),
        stderr="\n".join(stderr_lines),
    )


# ═══════════════════════════════════════════════════════════
# 后台进程管理
# ═══════════════════════════════════════════════════════════

class BackgroundProcess:
    """后台运行的进程"""

    def __init__(self, pid: int, command: str, cwd: str, process: subprocess.Popen):
        self.pid = pid
        self.command = command
        self.cwd = cwd
        self.process = process
        self.stdout_log: list[str] = []
        self.stderr_log: list[str] = []
        self.start_time = time.time()
        self._lock = threading.Lock()

    @property
    def running(self) -> bool:
        return self.process.poll() is None

    @property
    def exit_code(self) -> int | None:
        return self.process.returncode

    @property
    def elapsed(self) -> float:
        return time.time() - self.start_time

    def append_stdout(self, line: str):
        with self._lock:
            self.stdout_log.append(line)

    def append_stderr(self, line: str):
        with self._lock:
            self.stderr_log.append(line)

    def get_logs(self, tail: int = 20) -> str:
        with self._lock:
            lines = []
            for l in self.stdout_log:
                lines.append(l)
            for l in self.stderr_log:
                lines.append(f"[stderr] {l}")
            if tail and len(lines) > tail:
                lines = lines[-tail:]
            return "\n".join(lines)

    def stop(self):
        _kill_process_tree(self.pid)
        self.process.wait()


_processes: dict[int, BackgroundProcess] = {}
_next_pid = [1]


def start_background(command: str, cwd: str = ".") -> BackgroundProcess:
    """在后台启动一个长驻命令"""
    is_win = sys.platform == "win32"

    process = subprocess.Popen(
        command,
        shell=True,
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        stdin=subprocess.DEVNULL,
        text=True,
        encoding=_detect_encoding(),
        errors="replace",
        creationflags=subprocess.CREATE_NO_WINDOW if is_win else 0,
    )

    pid = _next_pid[0]
    _next_pid[0] += 1

    bg = BackgroundProcess(pid, command, cwd, process)
    _processes[pid] = bg

    def _read_stdout():
        for line in iter(process.stdout.readline, ""):
            bg.append_stdout(line.rstrip("\n\r"))
        process.stdout.close()

    def _read_stderr():
        for line in iter(process.stderr.readline, ""):
            bg.append_stderr(line.rstrip("\n\r"))
        process.stderr.close()

    threading.Thread(target=_read_stdout, daemon=True).start()
    threading.Thread(target=_read_stderr, daemon=True).start()

    return bg


def stop_background(pid: int) -> bool:
    """停止一个后台进程"""
    bg = _processes.get(pid)
    if bg is None:
        return False
    if bg.running:
        bg.stop()
    return True


def list_processes() -> list[BackgroundProcess]:
    """列出所有后台进程（含已结束的）"""
    return list(_processes.values())


def get_background_process(pid: int) -> BackgroundProcess | None:
    return _processes.get(pid)


def cleanup_finished_processes():
    """清理已结束的进程记录"""
    dead = [pid for pid, bg in list(_processes.items()) if not bg.running]
    for pid in dead:
        del _processes[pid]


def _kill_process_tree(pid: int):
    """跨平台 kill 进程树"""
    if sys.platform == "win32":
        try:
            subprocess.run(
                f"taskkill /F /T /PID {pid}",
                shell=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except Exception:
            pass
    else:
        try:
            os_kill = signal.SIGKILL if hasattr(signal, "SIGKILL") else signal.SIGTERM
            os_kill_pid(pid, os_kill)
        except Exception:
            pass


try:
    from os import kill as os_kill_pid
except ImportError:
    def os_kill_pid(pid, sig):
        pass
