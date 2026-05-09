"""Tests for TokenBudget session tracking."""
import pytest
from patchflow.core.fix.budget import (
    TokenBudget, BudgetExceeded,
    start_session_budget, get_session_budget, end_session_budget,
)


class TestTokenBudgetBasic:
    def test_initial_state(self):
        tb = TokenBudget(limit=10000)
        assert tb.total_used == 0
        assert tb.remaining == 10000
        assert not tb.is_exhausted
        assert tb.usage_ratio == 0.0

    def test_track_call(self):
        tb = TokenBudget(limit=10000)
        tb.track_call("analyzer", input_tokens=2000, output_tokens=500)
        assert tb.total_used == 2500
        assert tb.remaining == 7500
        assert len(tb.calls) == 1

    def test_multiple_calls(self):
        tb = TokenBudget(limit=10000)
        tb.track_call("analyzer", 1000, 200)
        tb.track_call("fixer", 2000, 500)
        tb.track_call("reviewer", 500, 100)
        assert tb.total_used == 4300
        assert len(tb.calls) == 3

    def test_exhausted(self):
        tb = TokenBudget(limit=1000)
        tb.track_call("analyzer", 800, 300)
        assert tb.is_exhausted

    def test_exhausted_exact(self):
        tb = TokenBudget(limit=1000)
        tb.track_call("analyzer", 500, 500)
        assert tb.total_used == 1000
        assert tb.is_exhausted

    def test_check_blocks_when_exhausted(self):
        tb = TokenBudget(limit=1000)
        tb.track_call("analyzer", 800, 200)
        blocked = tb.check(100)
        assert blocked is not None

    def test_check_allows_when_budget_available(self):
        tb = TokenBudget(limit=10000)
        tb.track_call("analyzer", 1000, 500)
        blocked = tb.check(4000)
        assert blocked is None

    def test_check_blocks_when_estimate_exceeds_remaining(self):
        tb = TokenBudget(limit=5000)
        tb.track_call("analyzer", 3000, 500)
        blocked = tb.check(3000)
        assert blocked is not None

    def test_usage_ratio(self):
        tb = TokenBudget(limit=10000)
        tb.track_call("analyzer", 6000, 2000)
        assert tb.usage_ratio == 0.8

    def test_is_warning_at_80_percent(self):
        tb = TokenBudget(limit=10000)
        tb.track_call("analyzer", 7000, 1000)
        assert tb.is_warning

    def test_not_warning_below_80(self):
        tb = TokenBudget(limit=10000)
        tb.track_call("analyzer", 5000, 1000)
        assert not tb.is_warning

    def test_estimate_tokens(self):
        tb = TokenBudget(limit=10000)
        assert tb.estimate_tokens("") == 0
        assert tb.estimate_tokens("hello world") > 0
        assert tb.estimate_tokens("x" * 400) == 100

    def test_reset(self):
        tb = TokenBudget(limit=10000)
        tb.track_call("analyzer", 3000, 1000)
        tb.reset()
        assert tb.total_used == 0
        assert len(tb.calls) == 0

    def test_summary(self):
        tb = TokenBudget(limit=10000)
        tb.track_call("analyzer", 2000, 500)
        summary = tb.summary()
        assert "2500/10000" in summary

    def test_detailed_summary(self):
        tb = TokenBudget(limit=10000)
        tb.track_call("analyzer", 2000, 500, model="deepseek")
        detailed = tb.detailed_summary()
        assert "deepseek" in detailed
        assert "in=2000" in detailed


class TestSessionBudget:
    def teardown_method(self):
        end_session_budget()

    def test_start_and_get(self):
        tb = start_session_budget(limit=50000)
        assert get_session_budget() is tb
        assert tb.limit == 50000

    def test_end_session(self):
        start_session_budget(limit=10000)
        old = end_session_budget()
        assert old is not None
        assert old.limit == 10000
        assert get_session_budget() is None

    def test_default_limit_from_config(self):
        tb = start_session_budget()
        assert tb.limit > 0
