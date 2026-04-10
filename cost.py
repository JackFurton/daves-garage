"""Token tracking and daily budget enforcement."""

# Pricing per million tokens. Update when Anthropic changes pricing.
MODEL_PRICING = {
    # Claude 4.5
    "claude-haiku-4-5-20251001": {"input": 1.00, "output": 5.00},
    "claude-haiku-4-5":          {"input": 1.00, "output": 5.00},
    # Claude 4.6
    "claude-sonnet-4-6":         {"input": 3.00, "output": 15.00},
    "claude-opus-4-6":           {"input": 15.00, "output": 75.00},
    # Claude 4 (legacy)
    "claude-sonnet-4-20250514":  {"input": 3.00, "output": 15.00},
    "claude-opus-4-20250514":    {"input": 15.00, "output": 75.00},
}

# Fallback pricing if model not in table — assume Sonnet-tier so we don't underbill.
DEFAULT_PRICING = {"input": 3.00, "output": 15.00}


def calculate_cost(model: str, input_tokens: int, output_tokens: int) -> float:
    """Calculate cost in USD for an API call."""
    pricing = MODEL_PRICING.get(model, DEFAULT_PRICING)
    input_cost = (input_tokens / 1_000_000) * pricing["input"]
    output_cost = (output_tokens / 1_000_000) * pricing["output"]
    return input_cost + output_cost


class BudgetTracker:
    def __init__(self, state, max_daily_usd: float, slack=None):
        self.state = state
        self.max_daily_usd = max_daily_usd
        self.slack = slack
        self._warned_80 = False

    def log_call(self, model: str, input_tokens: int, output_tokens: int, purpose: str) -> float:
        """Log an API call and return its cost. Raises BudgetExceeded if over the daily cap."""
        cost = calculate_cost(model, input_tokens, output_tokens)
        # log_spend returns the post-increment total, atomically — no read-after-write race.
        daily = self.state.log_spend(cost, model, purpose)

        if daily >= self.max_daily_usd * 0.8 and not self._warned_80 and self.slack:
            self._warned_80 = True  # Set BEFORE the slack call to prevent recursive re-entry
            self.slack.budget_warning(daily, self.max_daily_usd)

        if daily >= self.max_daily_usd:
            raise BudgetExceeded(f"Daily budget exceeded: ${daily:.2f} / ${self.max_daily_usd:.2f}")

        return cost

    def has_budget(self) -> bool:
        return self.state.get_daily_spend() < self.max_daily_usd

    def remaining(self) -> float:
        return max(0, self.max_daily_usd - self.state.get_daily_spend())


class BudgetExceeded(Exception):
    pass
