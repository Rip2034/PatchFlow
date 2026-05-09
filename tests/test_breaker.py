"""Tests for FixLoopBreaker circuit breaker logic."""
import pytest
from patchflow.core.fix.breaker import FixLoopBreaker


class TestFixLoopBreakerBasic:
    def test_initial_state(self):
        br = FixLoopBreaker(max_retries=3)
        assert br.turn == 0
        assert br.is_broken is False

    def test_should_retry_within_limit(self):
        br = FixLoopBreaker(max_retries=3)
        ok, reason = br.should_retry("syntax", "missing colon", "line_fix")
        assert ok
        assert reason == ""

    def test_max_retries_exceeded(self):
        br = FixLoopBreaker(max_retries=1)
        br.should_retry("syntax", "error1")
        ok, reason = br.should_retry("syntax", "error2")
        assert not ok
        assert "max_retries_exceeded" in reason

    def test_is_broken_after_max_retries(self):
        br = FixLoopBreaker(max_retries=1)
        # First retry is within limit (turn=1, max_retries=1)
        br.should_retry("syntax", "error1")
        assert br.is_broken is False
        # Second retry exceeds limit (turn=2 > max_retries=1)
        br.should_retry("syntax", "error2")
        assert br.is_broken is True

    def test_same_error_repeated(self):
        br = FixLoopBreaker(max_retries=5)
        br.record_failure("type", "int + str")
        br.record_failure("type", "int + str")
        br.record_failure("type", "int + str")
        ok, reason = br.should_retry("type", "int + str")
        assert not ok
        assert "same_error_repeated" in reason

    def test_is_broken_after_repeated_errors(self):
        br = FixLoopBreaker(max_retries=5)
        br.record_failure("type", "int + str")
        br.record_failure("type", "int + str")
        br.record_failure("type", "int + str")
        assert br.is_broken is True

    def test_strategy_auto_upgrade(self):
        br = FixLoopBreaker(max_retries=5)
        br.should_retry("syntax", "err", "line_fix")
        ok, reason = br.should_retry("syntax", "err2", "line_fix")
        assert not ok
        assert "strategy_failed_too_often" in reason

    def test_reset_clears_state(self):
        br = FixLoopBreaker(max_retries=3)
        br.should_retry("syntax", "error")
        br.record_failure("syntax", "error")
        br.reset()
        assert br.turn == 0
        assert len(br.error_history) == 0
        assert br.is_broken is False

    def test_is_broken_no_side_effects(self):
        """Property should not change internal state when queried."""
        br = FixLoopBreaker(max_retries=3)
        br.should_retry("syntax", "error")
        turn_before = br.turn
        _ = br.is_broken
        assert br.turn == turn_before

    def test_different_errors_dont_cross_count(self):
        br = FixLoopBreaker(max_retries=5)
        br.record_failure("syntax", "missing paren")
        br.record_failure("type", "int + str")
        br.record_failure("runtime", "null pointer")
        assert br.is_broken is False
