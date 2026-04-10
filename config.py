"""Dave configuration loader."""
import os
import yaml
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class HiveConfig:
    # Target repo
    repo: str = ""
    github_token: str = ""

    # Anthropic
    anthropic_api_key: str = ""
    triage_model: str = "claude-haiku-4-5-20251001"
    worker_model: str = "claude-sonnet-4-6"
    # Optional: escalate to Opus when an issue is triaged at or below this priority (1=critical).
    # Set to 0 / null to disable.
    escalate_priority: Optional[int] = None
    escalate_model: str = "claude-opus-4-6"
    max_daily_cost_usd: float = 10.00

    # AWS / DynamoDB
    # Two ways to authenticate, in order of preference:
    #   1. Inline keys here (aws_access_key_id + aws_secret_access_key) — simplest
    #   2. AWS profile from ~/.aws/credentials (set aws_profile to the profile name)
    aws_access_key_id: Optional[str] = None
    aws_secret_access_key: Optional[str] = None
    aws_profile: Optional[str] = None
    aws_region: str = "us-east-1"
    dynamodb_table: str = "dave"

    # Slack
    slack_webhook_url: Optional[str] = None
    slack_channel_name: str = "#carls-garage"

    # Persona — gives the bot a voice. Optional. See dave.example.yaml for the Dave persona.
    persona: Optional[dict] = None

    # Loop
    poll_interval_seconds: int = 60
    issue_label: str = "dave"
    # Auto-merge: if true, Dave merges his own PRs immediately after opening them.
    # Combined with auto_propose, this is the "fully autonomous train" mode.
    # Failed merges (conflicts, required reviews, failing checks) are logged and skipped —
    # Dave just moves on to the next issue.
    auto_merge: bool = False
    auto_merge_method: str = "squash"  # 'merge' | 'squash' | 'rebase'
    # If a worker holds a task longer than this without a heartbeat, it's reclaimed.
    stale_task_minutes: int = 30

    # ── Auto-propose mode (the truly infinite loop) ──
    # When the issue queue is empty AND auto_propose is true, Dave asks Claude to read
    # the repo and propose ONE new issue, then files it on GitHub with the issue_label.
    # Off by default — opt-in.
    auto_propose: bool = False
    # Don't propose if there are already this many open Dave-tagged issues.
    auto_propose_max_open: int = 3
    # Hard cap on how many issues Dave can propose in a single UTC day. Belt + suspenders
    # on top of the cost cap.
    auto_propose_max_per_day: int = 5
    # Only propose if the issue queue has been empty for at least this long. Stops Dave
    # from filing a proposal the second your real issues get drained.
    auto_propose_min_idle_minutes: int = 10
    # Issues filed by auto-propose get this prefix in the title so they're easy to spot.
    auto_propose_title_prefix: str = "[dave-proposed]"

    # Observability
    logfile: Optional[str] = "dave.log"
    log_level: str = "INFO"

    # Custom Slack messages — supports template variables {issue_id}, {title}, {pr_url}, {repo}
    slack_messages: dict = field(default_factory=dict)


def load_config(path: str = "dave.yaml") -> HiveConfig:
    """Load config from YAML, with env var fallback for secrets."""
    if not os.path.exists(path):
        raise FileNotFoundError(
            f"Config file not found: {path}\n"
            f"Copy dave.example.yaml to {path} and fill in your values."
        )

    with open(path) as f:
        raw = yaml.safe_load(f) or {}

    config = HiveConfig()
    for key, value in raw.items():
        if hasattr(config, key):
            # Resolve env var references like ${GITHUB_TOKEN}
            if isinstance(value, str) and value.startswith("${") and value.endswith("}"):
                env_key = value[2:-1]
                value = os.environ.get(env_key, "")
            setattr(config, key, value)
        else:
            # Don't fail on unknown keys — but warn so users notice typos.
            print(f"[config] warning: unknown config key '{key}' ignored")

    # Env var fallbacks for secrets
    if not config.github_token:
        config.github_token = os.environ.get("GITHUB_TOKEN", "")
    if not config.anthropic_api_key:
        config.anthropic_api_key = os.environ.get("ANTHROPIC_API_KEY", "")

    return config
