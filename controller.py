"""Controller — fetches issues, triages, dispatches workers."""
import json
import re

import anthropic

import prompts
from config import HiveConfig
from cost import BudgetTracker
from github_client import GitHubClient
from logger import get_logger
from slack import SlackNotifier
from state import HiveState
from worker import Worker

log = get_logger("controller")


class Controller:
    def __init__(self, config: HiveConfig, state: HiveState, budget: BudgetTracker,
                 github: GitHubClient, slack: SlackNotifier, persona=None):
        self.config = config
        self.state = state
        self.budget = budget
        self.github = github
        self.slack = slack
        self.persona = persona
        self.client = anthropic.Anthropic(api_key=config.anthropic_api_key)
        self.dry_run = False

    def run_cycle(self):
        """One controller cycle: reclaim → fetch → triage → dispatch."""

        # 0. Reclaim stale in_progress tasks (crashed workers, killed processes, etc.)
        reclaimed = self.state.reclaim_stale_tasks(
            stale_after_minutes=self.config.stale_task_minutes,
        )
        if reclaimed:
            log.info(f"Reclaimed {reclaimed} stale tasks")

        # 1. Budget gate
        if not self.budget.has_budget():
            log.info("Daily budget exhausted, skipping cycle")
            return

        # 2. Fetch open issues with the configured label
        issues = self.github.get_issues(self.config.issue_label)
        log.info(f"Found {len(issues)} open issues with label '{self.config.issue_label}'")

        # 3. Triage anything not yet tracked
        if issues:
            tracked = {
                int(t["PK"].split("#")[1])
                for t in self.state.get_pending_tasks()
            }
            new_issues = [i for i in issues if i["number"] not in tracked]
            if new_issues:
                log.info(f"Triaging {len(new_issues)} new issues")
                self._triage(new_issues)

        # 4. Pick top pending task and dispatch a worker
        pending = self.state.get_pending_tasks()
        if not pending:
            log.info("No pending tasks")
            # TODO Phase 5: auto-propose mode kicks in here
            return

        top_task = pending[0]
        issue_id = int(top_task["PK"].split("#")[1])
        try:
            issue = self.github.get_issue(issue_id)
        except Exception as e:
            log.error(f"Could not fetch issue #{issue_id}: {e}")
            return

        worker = Worker(self.config, self.state, self.budget, self.github, self.slack,
                        persona=self.persona, dry_run=self.dry_run)
        worker.run(issue)

    # ── Triage ──

    def _triage(self, issues: list):
        """Use Haiku to prioritize and plan approach for new issues."""

        issues_text = "\n\n".join(
            f"### Issue #{i['number']}: {i['title']}\n{(i.get('body') or '')[:500]}"
            for i in issues[:10]  # Cap at 10 issues per triage call
        )

        prompt = prompts.render(
            "triage",
            repo=self.config.repo,
            issues_text=issues_text,
        )

        try:
            response = self.client.messages.create(
                model=self.config.triage_model,
                max_tokens=2048,
                messages=[{"role": "user", "content": prompt}],
            )
        except Exception as e:
            log.error(f"Triage API call failed: {e}")
            return

        self.budget.log_call(
            self.config.triage_model,
            response.usage.input_tokens,
            response.usage.output_tokens,
            "triage",
        )

        text = self._strip_to_json(response.content[0].text)
        try:
            triage_results = json.loads(text)
        except json.JSONDecodeError:
            log.error(f"Failed to parse triage response: {text[:200]}")
            return

        for result in triage_results:
            if result.get("skip"):
                log.info(f"  skipping #{result['issue_id']}: {result.get('skip_reason', 'too vague')}")
                continue
            issue = next((i for i in issues if i["number"] == result["issue_id"]), None)
            if not issue:
                continue
            created = self.state.put_task(
                issue_id=issue["number"],
                title=issue["title"],
                priority=result.get("priority", 3),
                approach=result.get("approach", ""),
                repo=self.config.repo,
            )
            if created:
                log.info(f"  triaged #{issue['number']} → priority {result.get('priority', 3)}")
            else:
                log.debug(f"  #{issue['number']} already tracked, skipping")

    @staticmethod
    def _strip_to_json(text: str) -> str:
        text = text.strip()
        text = re.sub(r'^```(?:json)?\s*', '', text)
        text = re.sub(r'\s*```$', '', text)
        starts = [text.find(c) for c in '{[' if text.find(c) >= 0]
        if starts:
            text = text[min(starts):]
        last_close = max(text.rfind(']'), text.rfind('}'))
        if last_close > 0:
            text = text[: last_close + 1]
        return text
