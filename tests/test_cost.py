"""Tests for cost.calculate_cost and BudgetTracker."""
from unittest.mock import MagicMock

import pytest

import cost


def test_calculate_cost_known_sonnet():
    # Sonnet 4.6: $3 in / $15 out per million tokens
    # 1000 in + 500 out = 0.003 + 0.0075 = 0.0105
    assert cost.calculate_cost("claude-sonnet-4-6", 1000, 500) == pytest.approx(0.0105, rel=1e-9)


def test_calculate_cost_known_haiku():
    # Haiku 4.5: $1 in / $5 out per million
    assert cost.calculate_cost("claude-haiku-4-5-20251001", 1000, 500) == pytest.approx(0.0035, rel=1e-9)


def test_calculate_cost_known_opus():
    # Opus 4.6: $15 in / $75 out per million
    assert cost.calculate_cost("claude-opus-4-6", 1000, 500) == pytest.approx(0.0525, rel=1e-9)


def test_calculate_cost_unknown_model_falls_back_to_default():
    # Default is sonnet-tier ($3 / $15) so unknown models don't underbill
    expected = cost.calculate_cost("claude-sonnet-4-6", 1000, 500)
    assert cost.calculate_cost("some-future-claude-9", 1000, 500) == pytest.approx(expected, rel=1e-9)


def test_calculate_cost_zero_tokens():
    assert cost.calculate_cost("claude-sonnet-4-6", 0, 0) == 0.0


def test_budget_tracker_logs_and_returns_cost():
    state = MagicMock()
    state.log_spend.return_value = 0.05  # post-increment total
    bt = cost.BudgetTracker(state, max_daily_usd=10.0, slack=None)
    actual_cost = bt.log_call("claude-sonnet-4-6", 1000, 500, "test")
    assert actual_cost == pytest.approx(0.0105, rel=1e-9)
    state.log_spend.assert_called_once()


def test_budget_tracker_warns_at_80_percent_once():
    state = MagicMock()
    slack = MagicMock()
    state.log_spend.return_value = 8.50  # 85% of 10
    bt = cost.BudgetTracker(state, max_daily_usd=10.0, slack=slack)

    bt.log_call("claude-haiku-4-5-20251001", 100, 100, "first call")
    assert slack.budget_warning.call_count == 1

    # Second call past 80% should NOT warn again
    state.log_spend.return_value = 8.75
    bt.log_call("claude-haiku-4-5-20251001", 100, 100, "second call")
    assert slack.budget_warning.call_count == 1


def test_budget_tracker_raises_when_exceeded():
    state = MagicMock()
    state.log_spend.return_value = 10.50
    bt = cost.BudgetTracker(state, max_daily_usd=10.0, slack=None)
    with pytest.raises(cost.BudgetExceeded):
        bt.log_call("claude-sonnet-4-6", 1000, 500, "over the line")


def test_budget_tracker_has_budget_and_remaining():
    state = MagicMock()
    state.get_daily_spend.return_value = 3.0
    bt = cost.BudgetTracker(state, max_daily_usd=10.0)
    assert bt.has_budget() is True
    assert bt.remaining() == pytest.approx(7.0)

    state.get_daily_spend.return_value = 11.0
    assert bt.has_budget() is False
    assert bt.remaining() == 0.0
