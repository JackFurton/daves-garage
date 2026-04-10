"""Persona — the narration layer that puts a voice on top of Dave's actions.

A Persona wraps a config block (name, style, emojis, first_message) and exposes
two main entry points:

  1. generate(event, context, default)
       Asks Haiku to write a short Slack-ready line in the persona's voice for an
       event. Live generation per-event keeps every message fresh and unique.
       Falls back to `default` on any error so the loop never breaks because of
       narration.

  2. inject_into_prompt(base_prompt)
       Prepends a "voice" instruction to the worker's Sonnet prompt so the
       plan/summary/lessons/PR body all come back in Dave's voice — without
       affecting the actual code Sonnet writes.

Persona is fully optional. If config.persona is missing or has no name/style,
all methods degrade to defaults and no Haiku calls are made.
"""
import json
from typing import Optional

import anthropic

from logger import get_logger

log = get_logger("persona")


class Persona:
    def __init__(self, config: Optional[dict], client: anthropic.Anthropic,
                 model: str, budget=None):
        self.config = config or {}
        self.client = client
        self.model = model
        self.budget = budget

        self.name = self.config.get("name", "")
        self.style = self.config.get("style", "")
        self.emojis = self.config.get("emojis", {}) or {}
        self.first_message_tpl = self.config.get("first_message", "")

    @property
    def enabled(self) -> bool:
        return bool(self.name and self.style)

    # ── Emoji ──

    def emoji_for(self, event: str, default: str) -> str:
        return self.emojis.get(event, default)

    # ── Slack message generation ──

    def first_message(self, repo: str) -> str:
        """Render the persona's first-startup message template.

        Available template vars: {repo}, {repo_url}, {name}.
        """
        repo_url = f"https://github.com/{repo}"
        if not self.first_message_tpl:
            return f"{self.name} is online for {repo_url}."
        try:
            return self.first_message_tpl.format(
                repo=repo,
                repo_url=repo_url,
                name=self.name,
            )
        except KeyError:
            return self.first_message_tpl

    def generate(self, event: str, context: dict, default: str) -> str:
        """Ask the persona's model to write a short Slack line for an event.

        On any failure, returns `default` — narration must never break the loop.
        """
        if not self.enabled:
            return default

        # Build a compact context block for the prompt
        try:
            context_str = json.dumps(context, indent=2, default=str)
        except Exception:
            context_str = str(context)

        prompt = (
            f"You are {self.name}, an autonomous AI coding agent. You JUST performed an action "
            f"on a GitHub repository and you need to tell your team about it in a Slack message, "
            f"in YOUR own voice, in FIRST PERSON.\n\n"
            f"## Your voice\n{self.style}\n\n"
            f"## What you (yes, YOU) just did\n"
            f"Event type: `{event}`\n"
            f"Context: {context_str}\n\n"
            f"## CRITICAL RULES — read these carefully\n"
            f"1. **You are the actor.** YOU just opened that PR. YOU just picked up that issue. "
            f"YOU just found that error. Use first-person pronouns ('I', 'we'). Never refer to "
            f"the work as having been done by someone else.\n"
            f"2. **The repo owner is NOT a person you're talking to or about.** If the context "
            f"contains a `repo` like 'JackFurton/CarlsGarage' or a `repo_url` / `pr_url` "
            f"containing a username — that's the GitHub account that owns the repository. It is "
            f"NOT a collaborator, NOT a teammate, NOT a person who did the work. Do not say "
            f"things like 'Jack just opened a PR' — YOU opened the PR. The username in the URL "
            f"is just where the repo lives.\n"
            f"3. **Stay in character.** Voice on, persona on, every message.\n"
            f"4. **Write ONE Slack message, 1–3 sentences.** No markdown formatting. No quotes "
            f"around it. No prefix or label. Emoji is added separately, don't add one yourself. "
            f"Just the raw message line.\n"
        )

        try:
            response = self.client.messages.create(
                model=self.model,
                max_tokens=300,
                messages=[{"role": "user", "content": prompt}],
            )
            if self.budget:
                try:
                    self.budget.log_call(
                        self.model,
                        response.usage.input_tokens,
                        response.usage.output_tokens,
                        f"persona/{event}",
                    )
                except Exception as budget_err:
                    # Budget overshoot during persona generation shouldn't crash narration.
                    log.debug(f"Budget log skipped for persona/{event}: {budget_err}")
            text = response.content[0].text.strip()
            # Strip surrounding quotes if Claude added them.
            if len(text) >= 2 and text[0] == text[-1] and text[0] in ('"', "'"):
                text = text[1:-1]
            return text or default
        except Exception as e:
            log.warning(f"Persona generation failed for event '{event}': {e}")
            return default

    # ── Worker prompt injection ──

    def inject_into_prompt(self, base_prompt: str) -> str:
        """Prepend a voice instruction to a worker prompt.

        The voice applies to free-text fields (plan, summary, lessons, PR body).
        Code itself stays clean and professional — only the prose is in character.
        """
        if not self.enabled:
            return base_prompt
        voice_block = (
            f"## Voice\n"
            f"You are writing as **{self.name}**. {self.style}\n\n"
            f"Apply this voice to any free-text fields you produce — `plan`, `summary`, `lessons`, "
            f"and any prose that ends up in PR descriptions or issue comments. Stay in character.\n\n"
            f"**Important:** the voice applies ONLY to prose. The actual code you write must be "
            f"clean, idiomatic, and professional — match the existing style of the repo. Never let "
            f"the persona leak into source files, function names, comments, or commit messages "
            f"beyond what the issue requires.\n\n"
            f"---\n\n"
        )
        return voice_block + base_prompt
