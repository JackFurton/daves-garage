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
    aws_profile: str = "default"
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
    auto_merge: bool = False
    # If a worker holds a task longer than this without a heartbeat, it's reclaimed.
    stale_task_minutes: int = 30
    # When the issue queue is empty AND auto_propose is true, the controller asks Claude
    # to propose new issues for the repo. Off by default — opt-in for the truly infinite loop.
    auto_propose: bool = False

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
