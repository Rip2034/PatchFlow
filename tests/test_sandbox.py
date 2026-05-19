"""Tests for AgentSandbox path guard, command guard, and resource limiter."""
import pytest

from patchflow.core.agent_sandbox import (
    CommandGuard,
    PathGuard,
    ResourceLimiter,
    SandboxViolation,
    get_sandbox,
    reset_sandbox,
)


class TestPathGuard:
    def setup_method(self):
        self.guard = PathGuard(".")

    def test_normal_path(self):
        resolved = self.guard.resolve("src/app.py")
        assert resolved.is_absolute()

    def test_absolute_path_rejected(self):
        with pytest.raises(SandboxViolation):
            self.guard.resolve("/etc/passwd")

    def test_path_traversal_blocked(self):
        with pytest.raises(SandboxViolation, match="越界"):
            self.guard.resolve("../../../etc/passwd")

    def test_sensitive_env_file(self):
        reason = self.guard.check_sensitive(".env")
        assert reason is not None

    def test_sensitive_credentials(self):
        reason = self.guard.check_sensitive("config/credentials.json")
        assert reason is not None

    def test_sensitive_ssh(self):
        reason = self.guard.check_sensitive(".ssh/id_rsa")
        assert reason is not None

    def test_normal_py_file_not_sensitive(self):
        reason = self.guard.check_sensitive("src/main.py")
        assert reason is None

    def test_blocked_extension_exe(self):
        reason = self.guard.check_extension("payload.exe")
        assert reason is not None

    def test_blocked_extension_sh(self):
        reason = self.guard.check_extension("install.sh")
        assert reason is not None

    def test_allowed_extension(self):
        reason = self.guard.check_extension("app.py")
        assert reason is None

    def test_validate_write_sensitive_raises(self):
        with pytest.raises(SandboxViolation):
            self.guard.validate_write(".env", 100)

    def test_validate_write_large_file_raises(self):
        with pytest.raises(SandboxViolation, match="过大"):
            self.guard.validate_write("output.txt", 10 * 1024 * 1024)

    def test_validate_read_sensitive_warns_but_returns(self):
        resolved = self.guard.validate_read(".env")
        assert resolved.is_absolute()

    def test_validate_write_normal(self):
        resolved = self.guard.validate_write("src/app.py", 500)
        assert resolved.is_absolute()
        assert "src" in str(resolved)


class TestCommandGuard:
    def test_normal_command_allowed(self):
        ok, reason = CommandGuard.validate("python app.py")
        assert ok

    def test_dangerous_level1_blocked(self):
        ok, reason = CommandGuard.validate("rm -rf /")
        assert not ok

    def test_dangerous_level2_confirm(self):
        ok, reason = CommandGuard.validate("sudo python app.py")
        assert not ok  # needs confirmation

    def test_long_running_detection(self):
        assert CommandGuard.is_long_running("npm run dev")


class TestResourceLimiter:
    def test_initial_state(self):
        rl = ResourceLimiter()
        assert rl.stats["writes"] == "0/50"
        assert rl.stats["reads"] == "0/200"

    def test_track_read_within_limit(self):
        rl = ResourceLimiter(max_file_reads=5)
        for _ in range(5):
            assert rl.track_read() is None
        assert rl.track_read() is not None  # exceeded

    def test_track_write_within_limit(self):
        rl = ResourceLimiter(max_file_writes=3)
        for _ in range(3):
            assert rl.track_write() is None
        assert rl.track_write() is not None

    def test_track_command_time(self):
        rl = ResourceLimiter(max_command_seconds=10)
        assert rl.track_command(5) is None
        assert rl.track_command(6) is not None  # 11 > 10

    def test_reset(self):
        rl = ResourceLimiter(max_file_reads=5)
        for _ in range(3):
            rl.track_read()
        rl.reset()
        assert rl.stats["reads"] == "0/5"

    def test_track_command_count(self):
        rl = ResourceLimiter(max_commands=2)
        assert rl.track_command(0.1) is None
        assert rl.track_command(0.1) is None
        assert rl.track_command(0.1) is not None


class TestAgentSandbox:
    def setup_method(self):
        reset_sandbox()

    def test_singleton(self):
        s1 = get_sandbox(".")
        s2 = get_sandbox(".")
        assert s1 is s2

    def test_create_context(self):
        sandbox = get_sandbox(".")
        ctx = sandbox.create_context(
            parent_ctx={"file_cache": {"a.py": "code"}},
            overrides={"agent_id": "fixer_1"},
        )
        assert ctx["agent_id"] == "fixer_1"
        assert ctx["file_cache"]["a.py"] == "code"
        assert ctx["my_changes"] == []

    def test_record_change(self):
        sandbox = get_sandbox(".")
        ctx = sandbox.create_context(overrides={"agent_id": "fixer_1"})
        sandbox.record_change(ctx, "app.py", "new content")
        assert len(ctx["my_changes"]) == 1
        assert ctx["my_changes"][0]["file"] == "app.py"

    def test_validate_read(self):
        sandbox = get_sandbox(".")
        resolved = sandbox.validate_read("README.md")
        assert resolved.is_absolute()

    def test_validate_write_rejects_sensitive(self):
        sandbox = get_sandbox(".")
        with pytest.raises(SandboxViolation):
            sandbox.validate_write(".env", 10)

    def test_agent_resource_tracking(self):
        sandbox = get_sandbox(".")
        sandbox.create_context(overrides={"agent_id": "fixer_1"})
        assert sandbox.track_read("fixer_1") is None
        stats = sandbox.agent_stats("fixer_1")
        assert stats["reads"] == "1/200"
