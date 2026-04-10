"""Worker — clones repo, implements issue, creates PR."""
import json
import os
import re
import tempfile
import uuid
from typing import Optional

import anthropic

import prompts
from config import HiveConfig
from cost import BudgetTracker
from github_client import GitHubClient
from logger import get_logger
from slack import SlackNotifier
from state import HiveState

log = get_logger("worker")


class Worker:
    def __init__(self, config: HiveConfig, state: HiveState, budget: BudgetTracker,
                 github: GitHubClient, slack: SlackNotifier, persona=None,
                 dry_run: bool = False):
        self.config = config
        self.state = state
        self.budget = budget
        self.github = github
        self.slack = slack
        self.persona = persona
        self.client = anthropic.Anthropic(api_key=config.anthropic_api_key)
        self.worker_id = f"worker-{uuid.uuid4().hex[:6]}"
        self.dry_run = dry_run

    # ── Public API ──

    def run(self, issue: dict):
        """Implement a single GitHub issue end-to-end."""
        issue_id = issue["number"]
        title = issue["title"]
        body = issue.get("body") or ""

        log.info(f"[{self.worker_id}] Picking up #{issue_id}: {title}"
                 + (" (DRY RUN)" if self.dry_run else ""))

        # Atomic claim — bail if another worker grabbed it.
        # In dry-run mode we don't touch DDB at all (so the task stays pending for a real run later).
        if not self.dry_run:
            if not self.state.assign_task(issue_id, self.worker_id):
                log.info(f"[{self.worker_id}] #{issue_id} was claimed by another worker, skipping")
                return
            self.slack.issue_picked(issue_id, title, self.config.repo)

        try:
            with tempfile.TemporaryDirectory() as workdir:
                # 1. Clone and branch
                log.info(f"[{self.worker_id}] Cloning {self.config.repo}...")
                repo_dir = self.github.clone_repo(workdir)
                branch = f"dave/{issue_id}-{self._slugify(title)}"
                self.github.create_branch(repo_dir, branch)
                if not self.dry_run:
                    self.state.heartbeat_task(issue_id, self.worker_id)

                # 2. Smart context: pick relevant files via Haiku, then read them
                log.info(f"[{self.worker_id}] Selecting relevant files...")
                tracked_files = self.github.list_tracked_files(repo_dir)
                relevant_paths = self._select_relevant_files(issue_id, title, body, tracked_files)
                file_contents = self._read_files(repo_dir, relevant_paths)
                log.info(f"[{self.worker_id}] Loaded {len(file_contents)} files into context")
                if not self.dry_run:
                    self.state.heartbeat_task(issue_id, self.worker_id)

                # 3. Gather supplementary context
                file_tree = self.github.get_file_tree(repo_dir)
                readme = self.github.get_readme(repo_dir)
                lessons = self._fetch_relevant_lessons(title, body)
                lessons_text = self._format_lessons(lessons)

                # 4. Pick model (escalate to Opus for high-priority issues if configured)
                model = self._pick_model(issue_id)

                # 5. Implement
                log.info(f"[{self.worker_id}] Calling {model} to implement #{issue_id}...")
                implementation = self._implement(
                    issue_id, title, body, file_tree, readme,
                    file_contents, lessons_text, model,
                )
                if not self.dry_run:
                    self.state.heartbeat_task(issue_id, self.worker_id)

                if not implementation or not implementation.get("files"):
                    raise RuntimeError(
                        "Claude returned no file changes. Summary: "
                        + (implementation.get("summary", "(none)") if implementation else "(empty response)")
                    )

                # 6. Apply changes
                self._apply_changes(implementation, repo_dir)

                if self.dry_run:
                    log.info(f"[{self.worker_id}] DRY RUN — would commit and open PR. Plan:")
                    log.info(f"  {implementation.get('plan', '(no plan)')}")
                    log.info(f"  Files: {[f.get('path') for f in implementation.get('files', [])]}")
                    log.info(f"  Summary: {implementation.get('summary', '(no summary)')}")
                    return

                # 7. Commit and push
                committed = self.github.commit_and_push(
                    repo_dir, branch,
                    f"dave: implement #{issue_id} — {title}",
                )
                if not committed:
                    raise RuntimeError("Nothing to commit — patches may have all missed their search strings")

                # 8. Create PR
                pr_body = self._build_pr_body(issue_id, title, implementation)
                pr = self.github.create_pr(branch, f"dave: {title}", pr_body)
                pr_url = pr["html_url"]
                pr_number = pr["number"]

                # 9. Mark complete + notify (Slack gets the Sonnet-generated Dave summary verbatim)
                summary_text = implementation.get("summary", "")
                self.state.complete_task(issue_id, pr_url, summary_text)
                self.slack.pr_created(issue_id, pr_url, title, self.config.repo, summary=summary_text)
                self._extract_lessons(issue_id, implementation)

                log.info(f"[{self.worker_id}] PR created: {pr_url}")

                # 10. Auto-merge if enabled — closes the loop without a human bottleneck
                if self.config.auto_merge:
                    log.info(f"[{self.worker_id}] Attempting auto-merge ({self.config.auto_merge_method})...")
                    merge_result = self.github.merge_pr(pr_number, method=self.config.auto_merge_method)
                    if merge_result.get("merged"):
                        log.info(f"[{self.worker_id}] Auto-merged PR #{pr_number}")
                        self.slack.pr_merged(issue_id, self.config.repo)
                    else:
                        log.warning(f"[{self.worker_id}] Auto-merge skipped: "
                                    f"{merge_result.get('reason', 'unknown')}")

        except Exception as e:
            error_msg = str(e)[:500]
            log.error(f"[{self.worker_id}] Failed on #{issue_id}: {error_msg}")
            self.state.fail_task(issue_id, error_msg)
            self.slack.error(issue_id, error_msg, self.config.repo)
            try:
                self.github.comment_on_issue(
                    issue_id,
                    f"🔧 Dave hit a snag on this one:\n```\n{error_msg}\n```",
                )
            except Exception as comment_err:
                log.warning(f"Could not post failure comment: {comment_err}")

    # ── Smart context loading ──

    def _select_relevant_files(self, issue_id: int, title: str, body: str,
                                all_files: list) -> list:
        """Use Haiku to pick the files needed to implement this issue."""
        if not all_files:
            return []

        # Cap the file list size for the prompt
        max_files_in_prompt = 300
        if len(all_files) > max_files_in_prompt:
            file_list = "\n".join(all_files[:max_files_in_prompt])
            file_list += f"\n... ({len(all_files) - max_files_in_prompt} more files truncated)"
        else:
            file_list = "\n".join(all_files)

        prompt = prompts.render(
            "select_files",
            issue_id=issue_id,
            title=title,
            body=body[:1500] or "(no description)",
            repo=self.config.repo,
            file_list=file_list,
        )
        try:
            response = self.client.messages.create(
                model=self.config.triage_model,
                max_tokens=1024,
                messages=[{"role": "user", "content": prompt}],
            )
            self.budget.log_call(
                self.config.triage_model,
                response.usage.input_tokens,
                response.usage.output_tokens,
                f"select_files #{issue_id}",
            )
            text = self._strip_to_json(response.content[0].text)
            result = json.loads(text)
            picked = result.get("files", [])
            tracked_set = set(all_files)
            valid = [p for p in picked if p in tracked_set][:10]
            if not valid:
                log.warning(f"File selection returned no valid paths for #{issue_id}, falling back")
                return self._keyword_fallback(f"{title} {body}", all_files)
            return valid
        except Exception as e:
            log.warning(f"File selection failed for #{issue_id}: {e}; using keyword fallback")
            return self._keyword_fallback(f"{title} {body}", all_files)

    @staticmethod
    def _keyword_fallback(text: str, all_files: list, limit: int = 5) -> list:
        """Cheap heuristic if Haiku selection fails: rank files by issue keyword overlap."""
        words = {w.lower() for w in re.findall(r'\w+', text) if len(w) > 3}
        if not words:
            return []
        scored = []
        for f in all_files:
            f_lower = f.lower()
            score = sum(1 for w in words if w in f_lower)
            if score:
                scored.append((score, f))
        scored.sort(reverse=True)
        return [f for _, f in scored[:limit]]

    @staticmethod
    def _read_files(repo_dir: str, paths: list, max_total_bytes: int = 30_000) -> dict:
        """Read selected files. Caps total content size to keep the implementation prompt manageable."""
        contents = {}
        total = 0
        for path in paths:
            full = os.path.join(repo_dir, path)
            if not os.path.isfile(full):
                continue
            try:
                with open(full, encoding="utf-8", errors="replace") as f:
                    content = f.read()
                if total + len(content) > max_total_bytes:
                    content = content[: max_total_bytes - total]
                contents[path] = content
                total += len(content)
                if total >= max_total_bytes:
                    break
            except Exception as e:
                log.warning(f"Could not read {path}: {e}")
        return contents

    # ── Lessons retrieval ──

    def _fetch_relevant_lessons(self, title: str, body: str, limit: int = 8) -> list:
        """Pull lessons from DDB, preferring ones whose tags match keywords from the issue."""
        text = f"{title} {body}".lower()
        words = {w for w in re.findall(r'\w+', text) if len(w) > 3}
        all_lessons = self.state.get_lessons(self.config.repo, limit=50)
        if not all_lessons:
            return []

        scored = []
        for lesson in all_lessons:
            tags = lesson.get("tags") or []
            tag_hits = sum(1 for t in tags if t.lower() in words)
            scored.append((tag_hits, lesson))
        scored.sort(key=lambda x: x[0], reverse=True)
        return [l for _, l in scored[:limit]]

    @staticmethod
    def _format_lessons(lessons: list) -> str:
        if not lessons:
            return "No lessons yet."
        lines = []
        for l in lessons:
            tags = l.get("tags") or []
            tag_str = f" [{', '.join(tags)}]" if tags else ""
            lines.append(f"- ({l.get('category', '?')}){tag_str} {l['lesson']}")
        return "\n".join(lines)

    # ── Model selection ──

    def _pick_model(self, issue_id: int) -> str:
        """Use the escalate_model if this task's priority qualifies."""
        if not self.config.escalate_priority:
            return self.config.worker_model
        task = self.state.get_task(issue_id)
        if not task:
            return self.config.worker_model
        try:
            priority = int(task.get("priority", 99))
        except (TypeError, ValueError):
            return self.config.worker_model
        if priority <= self.config.escalate_priority:
            log.info(f"[{self.worker_id}] Escalating #{issue_id} (P{priority}) to {self.config.escalate_model}")
            return self.config.escalate_model
        return self.config.worker_model

    # ── Implementation ──

    def _implement(self, issue_id: int, title: str, body: str, file_tree: str,
                   readme: str, file_contents: dict, lessons: str, model: str) -> dict:
        """Call Claude to plan and implement the issue. Returns parsed JSON."""

        # Render file_contents into a markdown-ish block
        if file_contents:
            file_blocks = [f"#### `{path}`\n```\n{content}\n```"
                           for path, content in file_contents.items()]
            file_contents_text = "\n\n".join(file_blocks)
        else:
            file_contents_text = "(no source files preloaded — work from the file tree alone)"

        prompt = prompts.render(
            "implement",
            issue_id=issue_id,
            title=title,
            body=body or "(no description)",
            repo=self.config.repo,
            file_tree=file_tree[:3000],
            readme=readme[:2000] if readme else "(no README)",
            file_contents=file_contents_text,
            lessons=lessons,
        )

        # Inject persona voice instructions so the plan/summary/lessons come back in character.
        if self.persona is not None:
            prompt = self.persona.inject_into_prompt(prompt)

        response = self.client.messages.create(
            model=model,
            max_tokens=8192,
            messages=[{"role": "user", "content": prompt}],
        )
        self.budget.log_call(
            model,
            response.usage.input_tokens,
            response.usage.output_tokens,
            f"implement #{issue_id}",
        )

        text = self._strip_to_json(response.content[0].text)
        try:
            return json.loads(text)
        except json.JSONDecodeError as e:
            log.error(f"Could not parse Claude response as JSON: {e}")
            log.debug(f"Raw response (first 1KB): {text[:1000]}")
            raise RuntimeError(f"Claude returned invalid JSON: {e}")

    @staticmethod
    def _strip_to_json(text: str) -> str:
        """Extract a JSON object/array from a Claude response.

        Handles ```json fences, accidental prose preamble, and trailing chatter.
        """
        text = text.strip()
        text = re.sub(r'^```(?:json)?\s*', '', text)
        text = re.sub(r'\s*```$', '', text)
        # Find first { or [
        starts = [text.find(c) for c in '{[' if text.find(c) >= 0]
        if starts:
            text = text[min(starts):]
        # Trim anything after the final closing bracket
        last_close = max(text.rfind(']'), text.rfind('}'))
        if last_close > 0:
            text = text[: last_close + 1]
        return text

    def _apply_changes(self, implementation: dict, repo_dir: str):
        """Apply file changes from Claude's implementation."""
        for file_op in implementation.get("files", []):
            path_rel = file_op.get("path", "")
            if not path_rel:
                log.warning("  file op missing path, skipping")
                continue
            # Refuse path traversal
            if ".." in path_rel.split("/") or path_rel.startswith("/"):
                log.warning(f"  refusing suspicious path: {path_rel}")
                continue
            path = os.path.join(repo_dir, path_rel)
            action = file_op.get("action", "")

            if action == "create":
                parent = os.path.dirname(path)
                if parent:
                    os.makedirs(parent, exist_ok=True)
                with open(path, "w") as f:
                    f.write(file_op.get("content") or "")
                log.info(f"  created {path_rel}")

            elif action == "edit":
                if not os.path.exists(path):
                    log.warning(f"  edit target not found: {path_rel}")
                    continue
                with open(path) as f:
                    content = f.read()
                search = file_op.get("search") or ""
                replace = file_op.get("replace") or ""
                if not search or search not in content:
                    log.warning(f"  search string not found in {path_rel}")
                    continue
                if content.count(search) > 1:
                    log.warning(f"  search string is non-unique in {path_rel}; using first match")
                content = content.replace(search, replace, 1)
                with open(path, "w") as f:
                    f.write(content)
                log.info(f"  edited {path_rel}")

            elif action == "delete":
                if os.path.exists(path):
                    os.remove(path)
                    log.info(f"  deleted {path_rel}")

            else:
                log.warning(f"  unknown action '{action}' for {path_rel}")

    def _build_pr_body(self, issue_id: int, title: str, implementation: dict) -> str:
        summary = implementation.get("summary", "No summary provided.")
        plan = implementation.get("plan", "")
        files = implementation.get("files", [])
        file_list = "\n".join(f"- `{f.get('path','?')}` ({f.get('action','?')})" for f in files)

        persona_name = (self.persona.name if self.persona is not None and self.persona.enabled
                        else "Dave")
        return f"""## 🔧 {persona_name}'s Garage — Implementation

Closes #{issue_id}

### Plan
{plan}

### Summary
{summary}

### Files Changed
{file_list}

---
*Automated by [Dave](https://github.com/JackFurton/daves-garage) — autonomous coding loop with personality.*
"""

    def _extract_lessons(self, issue_id: int, implementation: dict):
        """Store structured lessons from this implementation for future context."""
        for lesson in implementation.get("lessons", []):
            if isinstance(lesson, dict):
                text = (lesson.get("lesson") or "").strip()
                if text and len(text) > 10:
                    self.state.store_lesson(
                        lesson=text,
                        source_issue=issue_id,
                        repo=self.config.repo,
                        category=lesson.get("category", "uncategorized"),
                        tags=lesson.get("tags") or [],
                    )
            elif isinstance(lesson, str) and len(lesson) > 10:
                # Backward-compat: unstructured lessons still get stored
                self.state.store_lesson(
                    lesson=lesson,
                    source_issue=issue_id,
                    repo=self.config.repo,
                )

    @staticmethod
    def _slugify(text: str) -> str:
        slug = re.sub(r'[^a-z0-9]+', '-', text.lower())
        return slug[:40].strip('-')
