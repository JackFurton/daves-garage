"""Controller — fetches issues, triages, dispatches workers, optionally proposes new ones."""
import json
import re
import tempfile
from datetime import datetime, timedelta, timezone

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
            # Track when the queue went empty so auto-propose can wait min_idle_minutes
            if not self.state.get_queue_empty_since(self.config.repo):
                self.state.mark_queue_empty_now(self.config.repo)
            # Auto-propose if enabled and the gates allow it
            if self.config.auto_propose and not self.dry_run:
                self._maybe_propose_issue(open_issues=issues)
            return

        # Queue is non-empty — clear the empty marker
        self.state.clear_queue_empty_marker(self.config.repo)

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

    # ── Auto-propose ──

    def _maybe_propose_issue(self, open_issues: list):
        """Decide whether to file a new issue, then file it.

        All gates must pass:
          1. Queue has been empty for at least auto_propose_min_idle_minutes
          2. Fewer than auto_propose_max_open Dave-tagged issues are currently open
          3. Fewer than auto_propose_max_per_day issues filed today
        """
        # Gate 1: idle time
        empty_since = self.state.get_queue_empty_since(self.config.repo)
        if not empty_since:
            return  # Just went empty this cycle, wait
        try:
            empty_dt = datetime.fromisoformat(empty_since)
            idle_minutes = (datetime.now(timezone.utc) - empty_dt).total_seconds() / 60
        except (TypeError, ValueError):
            return
        if idle_minutes < self.config.auto_propose_min_idle_minutes:
            log.info(f"Queue idle for {idle_minutes:.1f}min, waiting for "
                     f"{self.config.auto_propose_min_idle_minutes}min before proposing")
            return

        # Gate 2: how many Dave-tagged issues are already open?
        open_count = len(open_issues)
        if open_count >= self.config.auto_propose_max_open:
            log.info(f"Auto-propose skipped: {open_count} Dave issues already open "
                     f"(max {self.config.auto_propose_max_open})")
            return

        # Gate 3: per-day cap
        today_count = self.state.get_proposed_count_today(self.config.repo)
        if today_count >= self.config.auto_propose_max_per_day:
            log.info(f"Auto-propose skipped: {today_count} issues already proposed today "
                     f"(max {self.config.auto_propose_max_per_day})")
            return

        log.info(f"Auto-proposing issue (idle {idle_minutes:.0f}min, "
                 f"{today_count}/{self.config.auto_propose_max_per_day} today)")

        # Generate the proposal
        proposal = self._generate_proposal()
        if not proposal:
            return
        if proposal.get("skip"):
            log.info(f"Claude declined to propose: {proposal.get('skip_reason', 'no good issue found')}")
            return

        title = proposal.get("title", "").strip()
        body = proposal.get("body", "").strip()
        category = proposal.get("category", "ergonomic")
        if not title or not body:
            log.warning("Proposal missing title or body, skipping")
            return

        # Tag the title with the configured prefix so humans can spot bot-filed issues
        full_title = f"{self.config.auto_propose_title_prefix} {title}"
        body_with_marker = (
            f"{body}\n\n"
            f"---\n"
            f"*This issue was proposed automatically by Dave (auto-propose mode). "
            f"Category: `{category}`. If this isn't useful, just close it — Dave won't refile it.*"
        )

        try:
            issue = self.github.create_issue(
                title=full_title,
                body=body_with_marker,
                labels=[self.config.issue_label, "dave-proposed"],
            )
        except Exception as e:
            log.error(f"Failed to create proposed issue: {e}")
            return

        log.info(f"Proposed issue #{issue['number']} ({category}): {title}")
        self.state.record_proposed_issue(
            self.config.repo, issue["number"], full_title, category=category,
        )
        # Clear the empty marker so the next cycle picks up the new issue without re-proposing
        self.state.clear_queue_empty_marker(self.config.repo)

    def _generate_proposal(self) -> dict:
        """Use Claude to read the repo and propose ONE new issue.

        Clones the repo (shallow) so we can read README + file tree, then calls Haiku.
        """
        try:
            with tempfile.TemporaryDirectory() as workdir:
                repo_dir = self.github.clone_repo(workdir)
                file_tree = self.github.get_file_tree(repo_dir, max_files=100)
                readme = self.github.get_readme(repo_dir)
        except Exception as e:
            log.error(f"Could not clone repo for proposal: {e}")
            return {}

        lessons = self.state.get_lessons(self.config.repo, limit=10)
        lessons_text = "\n".join(f"- {l['lesson']}" for l in lessons) if lessons else "(no lessons yet)"

        recent = self.state.get_recent_proposed_titles(self.config.repo, limit=10)
        recent_text = "\n".join(f"- {t}" for t in recent) if recent else "(none yet)"

        recent_cats = self.state.get_recent_proposed_categories(self.config.repo, limit=5)
        if recent_cats:
            recent_categories_text = "Last 5 proposals (most recent first): " + ", ".join(recent_cats)
        else:
            recent_categories_text = "(none yet — pick whatever fits best)"

        prompt = prompts.render(
            "propose_issue",
            repo=self.config.repo,
            readme=readme[:2500] if readme else "(no README)",
            file_tree=file_tree[:2000],
            lessons=lessons_text,
            recent_proposals=recent_text,
            recent_categories=recent_categories_text,
        )

        try:
            response = self.client.messages.create(
                model=self.config.triage_model,  # cheap — Haiku is fine for proposals
                max_tokens=1500,
                messages=[{"role": "user", "content": prompt}],
            )
        except Exception as e:
            log.error(f"Proposal API call failed: {e}")
            return {}

        try:
            self.budget.log_call(
                self.config.triage_model,
                response.usage.input_tokens,
                response.usage.output_tokens,
                "auto_propose",
            )
        except Exception as e:
            log.warning(f"Budget log skipped for proposal: {e}")

        text = self._strip_to_json(response.content[0].text)
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            log.error(f"Could not parse proposal response: {text[:200]}")
            return {}

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
