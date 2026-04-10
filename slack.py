"""Slack webhook notifications.

Each notification method goes through three layers, in order of preference:

  1. Persona generation — if a Persona is wired in and enabled, ask Haiku to write a
     fresh in-voice line for this event. This is what makes Dave's posts unique
     instead of templated.
  2. Static custom messages — random pick from `slack_messages.on_<event>` in config,
     with template variable substitution ({issue_id}, {pr_url}, {title}, {repo}, etc.).
  3. Plain default — boring fallback so the loop never silently swallows a notification.

Emoji selection works the same way: persona.emoji_for() if available, otherwise the
default per-event emoji.
"""
import random
from typing import Optional

import requests

from logger import get_logger

log = get_logger("slack")


class SlackNotifier:
    def __init__(self, webhook_url: Optional[str] = None,
                 custom_messages: Optional[dict] = None,
                 persona=None):
        self.webhook_url = webhook_url
        self.custom_messages = custom_messages or {}
        self.persona = persona
        self.enabled = webhook_url is not None

    # ── Internals ──

    def _pick_static(self, event: str, **vars) -> Optional[str]:
        """Pick a random custom message for an event from config; format with vars."""
        options = self.custom_messages.get(f"on_{event}")
        if not options:
            return None
        choice = random.choice(options)
        try:
            return choice.format(**vars)
        except (KeyError, IndexError):
            return choice

    def _generate(self, event: str, default: str, **vars) -> str:
        """Pick a message: persona-generated > config-static > formatted default.

        Auto-injects {repo_url} whenever {repo} is present, so any template or persona
        prompt can reference the full GitHub URL of the target repo.
        """
        if "repo" in vars and "repo_url" not in vars:
            vars["repo_url"] = f"https://github.com/{vars['repo']}"

        if self.persona is not None and self.persona.enabled:
            msg = self.persona.generate(event, vars, default)
            if msg:
                return msg
        static = self._pick_static(event, **vars)
        if static:
            return static
        try:
            return default.format(**vars)
        except (KeyError, IndexError):
            return default

    def _emoji(self, event: str, default: str) -> str:
        if self.persona is not None and self.persona.enabled:
            return self.persona.emoji_for(event, default)
        return default

    def _post(self, text: str, emoji: str):
        if not self.enabled:
            return
        try:
            requests.post(self.webhook_url, json={
                "text": f"{emoji} {text}",
                "unfurl_links": False,
            }, timeout=10)
        except Exception as e:
            log.warning(f"Slack post failed: {e}")

    # ── Lifecycle ──

    def startup(self, repo: str, config_summary: str):
        # Use the persona's first_message template if it has one — that's the iconic line.
        if self.persona is not None and self.persona.enabled and self.persona.first_message_tpl:
            text = self.persona.first_message(repo)
        else:
            text = self._generate(
                "startup",
                "Dave online for {repo}\n{summary}",
                repo=repo, summary=config_summary,
            )
        self._post(text, self._emoji("startup", "🐝"))

    def shutdown(self, repo: str, reason: str):
        text = self._generate(
            "shutdown",
            "Shutting down: {reason}",
            repo=repo, reason=reason,
        )
        self._post(text, self._emoji("shutdown", "🛑"))

    # ── Issue lifecycle ──

    def issue_picked(self, issue_id: int, title: str, repo: str):
        text = self._generate(
            "issue_picked",
            "Working on #{issue_id}: {title}",
            issue_id=issue_id, title=title, repo=repo,
        )
        self._post(f"*[{repo}]* {text}", self._emoji("issue_picked", "🎯"))

    def pr_created(self, issue_id: int, pr_url: str, title: str, repo: str,
                    summary: Optional[str] = None):
        """Post about a newly-opened PR.

        If `summary` is provided (the Sonnet-generated implementation summary in Dave's voice),
        we use it verbatim — it's higher quality than anything Haiku would generate fresh, and
        it's already in character because the persona was injected into the worker's prompt.
        Falls back to live persona generation only if no summary was passed.
        """
        if summary and summary.strip():
            text = summary.strip()
        else:
            text = self._generate(
                "pr_created",
                "PR ready for #{issue_id}: {title}",
                issue_id=issue_id, pr_url=pr_url, title=title, repo=repo,
            )
        self._post(f"*[{repo}]* {text}\n{pr_url}", self._emoji("pr_created", "🔨"))

    def pr_merged(self, issue_id: int, repo: str, pr_url: Optional[str] = None,
                  pr_number: Optional[int] = None):
        """Post about a merged PR.

        Important: this event is about the PR, not the issue — the persona should
        say "I just merged PR #N" not "I just merged #issue_id". To prevent the model
        from confusing the two numbers, we only put `issue_id` in the context when
        `pr_number` is missing (legacy fallback). When `pr_number` is present, the
        persona only sees the PR number and will narrate accordingly.
        """
        if pr_number:
            default = "Shipped PR #{pr_number}!"
            vars = {"repo": repo, "pr_number": pr_number, "pr_url": pr_url or ""}
        else:
            default = "Shipped issue #{issue_id}!"
            vars = {"repo": repo, "issue_id": issue_id, "pr_url": pr_url or ""}

        text = self._generate("pr_merged", default, **vars)

        # Append the PR link so the merge post is clickable in Slack
        body = f"*[{repo}]* {text}"
        if pr_url:
            body = f"{body}\n{pr_url}"
        self._post(body, self._emoji("pr_merged", "✅"))

    def error(self, issue_id: int, error: str, repo: str):
        text = self._generate(
            "error",
            "Failed on #{issue_id}: {error}",
            issue_id=issue_id, error=error[:200], repo=repo,
        )
        self._post(f"*[{repo}]* {text}", self._emoji("error", "💀"))

    def budget_warning(self, spent: float, limit: float):
        text = self._generate(
            "budget_warning",
            "Daily spend at ${spent:.2f} / ${limit:.2f}",
            spent=spent, limit=limit,
        )
        self._post(text, self._emoji("budget_warning", "💸"))

    def custom(self, text: str, emoji: str = "🐝"):
        """Send any arbitrary message — for trolling, cron pings, whatever."""
        self._post(text, emoji)
