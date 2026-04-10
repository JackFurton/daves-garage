# Dave — Architecture

> A top-down explanation of how Dave works, written so a future reader (human or AI) can understand the entire system from one file. Read this before touching anything.

---

## 1. What Dave is

Dave is a **portable autonomous coding loop**. You point him at any GitHub repo, tag some issues with a configurable label (default `dave`), and he gets to work — clones the repo, reads the relevant source files, calls Claude Sonnet to implement the fix, opens a PR, optionally merges it, and posts to Slack in the voice of "Dave Plummer" (the real retired Microsoft engineer who runs the *Dave's Garage* YouTube channel). When the issue queue empties, an optional auto-propose mode has Dave read the repo and file his own next issues, making the loop genuinely self-fueling.

The only thing that ever stops Dave is a daily $ cap (`max_daily_cost_usd`). Everything else — heartbeats, conditional updates, retries, stale-task reclaim — exists to keep him alive *until* the budget cap is the natural stop.

The repo lives at `JackFurton/daves-garage`. Dave is designed to be cloned, configured, and deployed on a small Linux VM (Hetzner / Graviton / Lightsail / etc.) as a long-lived `systemd` unit. Multiple instances pointed at different repos are a first-class use case.

---

## 2. The dream — why this exists

This is the clean, generic rewrite of an autonomous loop the project owner originally built inside a 3,700-line trading-bot codebase. The trading bot proved the controller-+-workers-+-DynamoDB-+-Slack pattern works. Dave is the lift-the-pattern, drop-the-domain pass: a portable agent that can be pointed at any GitHub repository.

**Design constraints to honor:**

- **Generic > clever.** Resist hardcoding anything that ties Dave to a specific repo or workflow.
- **Multi-instance is a first-class assumption.** DDB writes need to be safe under concurrent runs, not just "works on my laptop." Two Daves pointed at the same DDB table must never grab the same issue.
- **Cost cap is THE kill switch.** It must be reliable. No other implicit "stop" condition (no max-iterations, no max-runtime) unless explicitly opted in.
- **Crash resilience matters.** Long-lived systemd loops will have workers die mid-issue, so stale-task reclaim and heartbeats are not optional.
- **Persona is half feature, half running joke.** It must be rich enough to enable jokes (see [persona section](#7-the-persona-system)) and route through Haiku for cost reasons.
- **The infinite loop is what makes it Dave.** Without auto-propose mode, "infinity loop" is really "infinity sleep until a human files an issue."

---

## 3. Component map

```
                         dave.py (entry point)
                                │
                ┌───────────────┼─────────────────┐
                │               │                 │
        load config       setup logging      handle signals
                │               │                 │
                ▼               ▼                 ▼
        ┌──────────────────────────────────────────────┐
        │              run_loop()                       │
        │                                               │
        │  while running:                               │
        │      controller.run_cycle()                   │
        │      sleep(poll_interval_seconds)             │
        └──────────────────────────────────────────────┘
                            │
                            ▼
        ┌──────────────────────────────────────────────┐
        │           controller.run_cycle()              │
        │                                               │
        │  1. state.reclaim_stale_tasks()               │
        │  2. budget.has_budget()  (gate)               │
        │  3. github.get_issues()  (label filter)       │
        │  4. _triage(new_issues)  (Haiku)              │
        │  5. pending = state.get_pending_tasks()       │
        │  6. if pending: dispatch worker on pending[0] │
        │     else: maybe_propose_issue() (auto-propose)│
        └──────────────────────────────────────────────┘
                            │
                            ▼
        ┌──────────────────────────────────────────────┐
        │             worker.run(issue)                 │
        │                                               │
        │  1. state.assign_task() (conditional)         │
        │  2. github.clone_repo()                       │
        │  3. _select_relevant_files()  (Haiku)         │
        │  4. _read_files()                             │
        │  5. _fetch_relevant_lessons()                 │
        │  6. _implement()  (Sonnet, persona-injected)  │
        │  7. _apply_changes() (create/edit/delete)     │
        │  8. github.commit_and_push()                  │
        │  9. github.create_pr()                        │
        │ 10. state.complete_task()                     │
        │ 11. slack.pr_created() (Sonnet summary)       │
        │ 12. _extract_lessons()  (DDB write)           │
        │ 13. github.merge_pr()  (if auto_merge)        │
        │ 14. slack.pr_merged()                         │
        └──────────────────────────────────────────────┘
                            │
                            ▼
                       (back to controller, sleeps, repeats)
```

The controller and worker are deliberately separate concerns. The controller decides *what* to work on next; the worker does *the work*. They share state via DynamoDB.

---

## 4. The control flow — one full cycle, top to bottom

A single cycle of `controller.run_cycle()` looks like this:

### Step 0: Reclaim stale tasks

`state.reclaim_stale_tasks(stale_after_minutes=30)` finds any task whose `status='in_progress'` and whose `heartbeat_at` is older than 30 minutes (or has no heartbeat at all). Those tasks get conditionally reverted to `status='pending'`. This is what catches workers that crashed mid-issue (process killed, network died, OOM, systemd reloaded).

The reclaim is safe under concurrent controllers because it's a *conditional* update on the exact heartbeat timestamp it just read — two reclaimers racing on the same task, only one wins.

### Step 1: Budget gate

`budget.has_budget()` reads today's spend from DDB and compares against `max_daily_cost_usd`. If we're at or over the cap, the cycle exits early. This is the "soft" gate; the "hard" gate is the per-API-call check inside `BudgetTracker.log_call()` which raises `BudgetExceeded` after every call once the cap is hit.

### Step 2: Fetch issues

`github.get_issues(label='dave')` calls `GET /repos/{repo}/issues?labels=dave&state=open`. PRs are filtered out (GitHub returns them as issues too). The result is a list of open issues currently tagged with the configured label.

### Step 3: Triage new ones

If the GitHub list contains any issues that aren't yet in our DDB pending queue, the controller passes them to `_triage()`. Triage is a single Haiku call that reads up to 10 issue titles+bodies and returns a JSON list with `priority` (1-5), `approach` (1-2 sentence plan), and `skip` (true if the issue is too vague / too large / not implementable).

For each non-skipped result, the controller calls `state.put_task()`. That call is *conditional* (`attribute_not_exists(PK)`) so two concurrent controllers can never insert the same task twice — the second one's call returns False and the controller logs "already tracked, skipping."

### Step 4: Pick top pending task

`state.get_pending_tasks()` does a filtered scan of DDB for `status='pending'` and sorts by `priority` ascending (1 = highest priority). The controller takes the top result, fetches the full issue from GitHub, and dispatches a `Worker`.

### Step 5: Worker runs

See [the worker pipeline](#5-the-worker-pipeline) below for the per-issue flow.

### Step 6: Or auto-propose if queue empty

If `state.get_pending_tasks()` returns empty, the controller marks the queue-empty timestamp (if it isn't already marked) and calls `_maybe_propose_issue()`. That function gates on three conditions, all of which must pass:

1. **Idle time** — queue must have been empty for at least `auto_propose_min_idle_minutes` (default 10).
2. **Open count** — fewer than `auto_propose_max_open` Dave-tagged issues currently open on the repo (default 3).
3. **Daily count** — fewer than `auto_propose_max_per_day` issues already proposed today (default 5).

If all three pass, the controller clones the repo, reads the README + file tree + recent lessons + recent proposals, calls Haiku via `prompts/propose_issue.md`, parses a structured proposal `{title, body, category, skip, skip_reason}`, and either files it via `github.create_issue()` or skips with logging.

### Step 7: Sleep

Loop sleeps for `poll_interval_seconds` (default 60), then repeats.

---

## 5. The worker pipeline

`worker.run(issue)` does an entire issue end-to-end:

### Atomic claim

`state.assign_task(issue_id, worker_id)` is a conditional update on `status='pending'`. If two workers race on the same task, only one wins; the loser logs "another worker grabbed it" and returns. This is the multi-instance safety boundary.

### Clone

`github.clone_repo(workdir)` does a shallow `git clone --depth=1 --single-branch`. The token is embedded in the URL via `https://x-access-token:{token}@github.com/...`. Done into a `tempfile.TemporaryDirectory()` so cleanup is automatic.

### Smart context loading (the secret sauce)

This is the difference between Dave producing good code and Dave producing garbage. Two passes:

**Pass 1 — file selection (Haiku, ~$0.005):**
- `git ls-files` lists everything tracked in the repo (respects .gitignore).
- The list (capped at 300 entries) plus the issue title + body is fed to Haiku via `prompts/select_files.md`.
- Haiku returns a JSON object: `{"files": ["path/one.py", "path/two.py"], "reasoning": "..."}` with the 1-10 files an agent would actually need.
- If Haiku's selection is empty or fails to parse, a `_keyword_fallback()` heuristic ranks files by simple keyword overlap with the issue text.

**Pass 2 — implementation (Sonnet, ~$0.05-$0.10):**
- The selected files are read into a dict (capped at 30KB total to keep the prompt manageable).
- The file tree, README, file contents, persona-relevant lessons, and the issue text are formatted into `prompts/implement.md`.
- The persona's `inject_into_prompt()` prepends a "Voice" instruction so the plan/summary/lessons come back in Dave's voice.
- Sonnet returns a JSON object: `{"plan": "...", "files": [...ops...], "summary": "...", "lessons": [...]}`.

### File ops

`_apply_changes()` walks the `files` list. Each op is `create | edit | delete`:
- **create** → mkdir parent + write content
- **edit** → read file, find `search` string (must appear once, warns if non-unique), replace with `replace`, write back
- **delete** → unlink

Path traversal is rejected (`..` in path or absolute paths). Search-not-found warns and skips that single op (does not abort the whole cycle).

### Commit + push

`github.commit_and_push()` does `git add -A`, checks for changes (returns False if there's nothing to commit), commits with `Author: Dave <dave@daves-garage.bot>`, pushes to `origin <branch>`.

### PR creation

`github.create_pr(branch, title, body)`. Title is `dave: <issue title>`, body is built by `_build_pr_body()` which assembles the persona-flavored plan + summary + file list with a `Closes #N` footer that auto-closes the issue on merge.

### Complete task + post to Slack

`state.complete_task(issue_id, pr_url, summary)` updates DDB and writes a separate `RESULT` row.

`slack.pr_created(issue_id, pr_url, title, repo, summary=summary_text)` posts the PR to Slack. **Critically**, the `summary` parameter is the Sonnet-generated implementation summary in Dave's voice — it's used verbatim instead of having Haiku regenerate something shorter. This is what makes Slack posts feel rich and in-character.

### Extract lessons

`_extract_lessons()` walks the `lessons` array from Sonnet's response. Each lesson is a dict with `category` (testing/migrations/style/gotcha/architecture/deps), `tags` (free-form list), and `lesson` (the text). Stored in DDB via `state.store_lesson()` for retrieval by future workers.

### Auto-merge (optional)

If `auto_merge: true` in config, `github.merge_pr(pr_number, method=auto_merge_method)` calls `PUT /pulls/{n}/merge` with `{merge_method: 'squash'}` (or 'merge' / 'rebase'). On success, posts `slack.pr_merged()`. On failure (conflicts, required reviews, failing checks), logs a warning and moves on — never raises, never blocks the loop.

### Heartbeats throughout

At every major step (clone done, files selected, Sonnet returned), the worker calls `state.heartbeat_task()` which updates `heartbeat_at` on the task row. This is what stale-task reclaim uses to distinguish a slow worker from a dead one.

---

## 6. DynamoDB schema

Single table, composite key. Default name `dave`.

| PK | SK | Attributes | Purpose |
|---|---|---|---|
| `TASK#{issue_id}` | `META` | `title, priority, approach, repo, status, assigned_to, assigned_at, heartbeat_at, created_at, completed_at, failed_at, error` | Per-issue task state |
| `TASK#{issue_id}` | `RESULT` | `pr_url, summary, completed_at` | PR result after completion |
| `BUDGET#{YYYY-MM-DD}` | `SPEND` | `total_usd, calls` | Atomic daily spend counter |
| `LESSON#{uuid8}` | `META` | `lesson, source_issue, repo, category, tags, created_at` | Structured lesson, retrieved by repo + tags |
| `PROPOSED#{repo}#{YYYY-MM-DD}` | `META` | `count, last_issue_id, last_title, last_at` | Auto-propose daily counter + recent titles for dedup |
| `QUEUE_STATE#{repo}` | `META` | `empty_since` | Idle-queue marker for auto-propose timing |
| `CONFIG` | `SETTINGS` | (free-form) | Hot-reload overrides (not currently used) |

**Why a single table:** simplicity, plus one table is enough — DDB scales to millions of items per table, and Dave's queries are all PK-prefix scans which are cheap on small tables.

**Status states for tasks:**
```
pending → in_progress → complete
                     ↓
                  failed
                     ↓
              (manual reset or stale-reclaim → pending)
```

**Atomicity:**
- `put_task` uses `ConditionExpression="attribute_not_exists(PK)"` — two concurrent triages can't both create the same task.
- `assign_task` uses `ConditionExpression="#status = pending"` — two concurrent workers can't both grab the same task.
- `log_spend` uses `UpdateExpression="ADD total_usd :amt"` — atomic increment, returns the post-increment total in one round trip.
- `reclaim_stale_tasks` uses `ConditionExpression="#status = in_progress AND heartbeat_at = :hb"` — two reclaimers can't both reclaim the same task.

---

## 7. The persona system

Persona is what makes Dave *Dave*. It's wired into two places:

### 1. Slack message generation (`persona.generate()`)

Every Slack notification (except `pr_created` which now uses the Sonnet summary verbatim) goes through `persona.generate(event, context, default)`. That function:

1. Builds a JSON context dict with whatever the SlackNotifier method passed (issue_id, repo, pr_url, etc.).
2. Auto-injects `repo_url` if `repo` is present (so templates can reference `https://github.com/owner/repo`).
3. Constructs a Haiku prompt that explicitly instructs Claude:
   - "You are Dave, an autonomous AI coding agent. You JUST performed an action."
   - "You are the actor — use first person, never refer to GitHub usernames as collaborators."
   - "Stay in character. 1-3 sentence Slack message."
4. Calls Haiku, logs cost via `BudgetTracker.log_call()`, returns the text.

The "you are the actor" wording is critical — without it, Haiku sees `JackFurton/CarlsGarage` in the URL and infers "Jack" as the human who did the work, producing third-person narration like "Hey, looks like Jack just opened a PR." With it, Dave consistently produces first-person ("I just opened PR #8").

### 2. Worker prompt injection (`persona.inject_into_prompt()`)

When the worker calls Sonnet to implement an issue, the persona's `inject_into_prompt()` prepends a "Voice" block to the prompt. This makes the plan, summary, lessons, and PR body all come back in Dave's voice. **Important**: the prompt also says the voice applies *only* to prose fields — actual code stays clean and idiomatic. This is enforced via a clear instruction that says "Never let the persona leak into source files, function names, comments, or commit messages beyond what the issue requires."

### Configuration

The persona block in `dave.yaml` has 5 fields:
- `name`: how Dave refers to himself
- `style`: the multi-line voice description (ideally 5-15 lines, references-rich)
- `emojis`: per-event Slack emoji map (`startup`, `issue_picked`, `pr_created`, `pr_merged`, `error`, `shutdown`, `budget_warning`)
- `first_message`: the literal startup line, with `{repo}`, `{repo_url}`, `{name}` template variables

The whole persona block is optional. Comment it out and Dave reverts to plain default messages (loop still works, just boring).

---

## 8. Cost gate

Every Anthropic call goes through `BudgetTracker.log_call(model, in_tokens, out_tokens, purpose)`:

```
1. cost = calculate_cost(model, in_tokens, out_tokens)   # local math
2. daily = state.log_spend(cost, model, purpose)          # atomic DDB ADD, returns post-increment
3. if daily >= max_daily * 0.8 and not _warned_80:
       slack.budget_warning(daily, max_daily)
       _warned_80 = True
4. if daily >= max_daily:
       raise BudgetExceeded(...)                          # bubbles to main loop, exit code 2
```

The atomic increment in step 2 means two Daves running in parallel against the same DDB table share a counter — they can't both spend $4.99 unaware of each other. The "best-effort gate" race window is exactly one in-flight call's worth of overshoot (typically ~$0.10), which is fine for a $5 cap.

`BudgetExceeded` is caught in `dave.py`'s main loop, sets `shutdown_reason="budget exhausted"`, posts `slack.shutdown()`, and exits with code 2. The systemd unit has `RestartPreventExitStatus=2` so systemd does NOT auto-restart Dave until UTC midnight when the next day's `BUDGET#{date}` row resets the counter.

---

## 9. Multi-instance safety

You can run Dave against multiple repos at once, on multiple machines, sharing the same DDB table:

```bash
python dave.py --config dave-repo-a.yaml &
python dave.py --config dave-repo-b.yaml &
```

Or two systemd instances on the same box via `dave@.service` template units.

The safety properties this requires:
1. **Two controllers don't double-triage the same issue.** Enforced by `put_task` conditional on `attribute_not_exists(PK)`.
2. **Two workers don't double-pick the same task.** Enforced by `assign_task` conditional on `status='pending'`.
3. **Two stale-task reclaimers don't both revert the same task.** Enforced by `reclaim_stale_tasks` conditional on the exact `heartbeat_at` it just read.
4. **Daily budget is shared.** Atomic `ADD` + post-increment-read on `BUDGET#{date}`.

What's NOT protected:
- Two Daves filing the *same* auto-proposed issue title (rare; auto-propose includes recent titles in the prompt for dedup, but it's best-effort).
- Concurrent Slack posts (no ordering guarantee — they may arrive out of order).

---

## 10. File-by-file map

```
dave.py                  Entry point. Argparse, config loading, logging setup,
                         signal handlers, the main while-running loop, the
                         --doctor preflight check, --watch logfile tailer.

controller.py            Cycle orchestration: reclaim → budget gate → fetch →
                         triage → dispatch worker. Also _maybe_propose_issue()
                         and _generate_proposal() for auto-propose mode.

worker.py                Per-issue pipeline: assign → clone → smart context →
                         Sonnet → apply changes → commit/push → PR → complete →
                         lessons → maybe auto-merge. The biggest module.

state.py                 DynamoDB layer. All conditional updates, atomic
                         counters, heartbeats, stale-task reclaim, lessons
                         storage/retrieval, queue-empty markers, proposed-issue
                         tracking. Also _build_boto3_session() helper for
                         credential resolution.

cost.py                  MODEL_PRICING table + calculate_cost() + BudgetTracker
                         class. Tiny, pure logic, easy to test.

github_client.py         GitHub REST API + git subprocess wrapper. Issues,
                         clone, branch, commit, push, PR create, PR merge,
                         issue create, default branch detection. Has a
                         _with_retry() decorator on every API call.

config.py                @dataclass HiveConfig + load_config() YAML loader
                         with env-var ${VAR} substitution and warning on
                         unknown keys.

logger.py                Rich-flavored logging setup. setup_logging() once
                         per process, get_logger("component") everywhere
                         else. Falls back to plain stderr if rich missing.

persona.py               The Persona class — generate() for Slack lines via
                         Haiku, inject_into_prompt() for worker Sonnet calls,
                         emoji_for() for per-event emoji selection,
                         first_message() for the iconic startup line.

slack.py                 SlackNotifier class. Persona-aware. Each notification
                         method goes through persona.generate() > config-static
                         template > formatted default in that order. Auto-
                         injects {repo_url} into template context.

setup_table.py           One-shot script to create the DynamoDB table.

prompts/__init__.py      Tiny prompt template loader with @lru_cache.
prompts/triage.md        Prompt for the controller's _triage() step.
prompts/select_files.md  Prompt for the worker's _select_relevant_files() step.
prompts/implement.md     Prompt for the worker's _implement() step.
prompts/propose_issue.md Prompt for auto-propose mode.

dave.example.yaml        Config template with the Dave persona pre-filled.
                         What users `cp` from when they first set up.
dave.yaml                The user's actual config — gitignored. Holds
                         credentials. Treat like a password manager entry.

deploy/dave.service      systemd unit template. Customize paths for your VM.
deploy/README.md         Hetzner-flavored deployment recipe.

tests/test_cost.py       Pure-logic tests for pricing math + BudgetTracker.
tests/test_config.py     YAML loader, env-var substitution, defaults, persona block.
tests/test_worker.py     File ops, slugify, JSON stripping, keyword fallback,
                         path traversal refusal. No network calls.
tests/test_state.py      DDB tests against moto. Conditional updates,
                         heartbeats, reclaim, structured lessons, auto-propose
                         counters, queue-empty markers.
tests/conftest.py        Shared pytest fixtures (path setup, fake AWS creds).

ARCHITECTURE.md          You are here.
RUNBOOK.md               Operating Dave: deploy, monitor, debug, recover.
README.md                Front door for new readers.
```

---

## 11. The "infinite train" mode

When all three of these are true in `dave.yaml`:

```yaml
auto_merge: true            # Dave merges his own PRs
auto_propose: true          # Dave files his own next issues
poll_interval_seconds: 60   # cycle every minute
```

…the loop becomes self-fueling. Concretely:

```
Dave starts (systemd) → polls every 60s
  cycle: real issue exists
    → smart context → Sonnet → PR → AUTO-MERGE → log lessons → next cycle
  cycle: queue empty
    → mark empty, sleep
  cycle: queue still empty for >= 10min
    → AUTO-PROPOSE: clone repo, ask Haiku for new issue, file it
    → next cycle picks it up immediately
  cycle: hit auto_propose_max_per_day (5/UTC day)
    → no more proposals today, just polls
  cycle: hit max_daily_cost_usd ($5/UTC day)
    → BudgetExceeded, exit code 2, systemd does NOT restart
  next UTC midnight:
    → fresh BUDGET#{date} row, fresh PROPOSED#{repo}#{date} row
    → systemctl restart dave (manual one-time, or via a daily cron)
    → loop resumes
```

**This is the actual "Dave runs forever" experience.** The cap is *exactly* the cost, never more (modulo a small race window of ~$0.10 if multi-instance). The pace is *exactly* the rate limits (1 cycle/minute, 5 proposals/day, 3 open issues max).

---

## 12. Key invariants — things that should always be true

If you're modifying Dave, don't break these:

1. **No state mutation outside DDB.** All persistent state lives in DynamoDB. Workers and controllers do not maintain in-memory state between cycles.
2. **Every DDB write that could race is conditional.** Use `ConditionExpression`. If the condition fails, log and continue — don't crash.
3. **Every Anthropic call goes through `budget.log_call()`.** No direct `client.messages.create()` calls that bypass cost tracking.
4. **Persona is optional.** Every code path that uses persona must check `persona is not None and persona.enabled` first.
5. **Workers heartbeat at every major step.** Stale-task reclaim depends on this.
6. **Code generated by Sonnet stays clean — only prose is in persona voice.** Enforced by the prompt instruction in `persona.inject_into_prompt()`.
7. **`dave.yaml` is gitignored.** Never write a default value into the example yaml that looks like a real credential.
8. **Failures in narration must never break the loop.** `persona.generate()` catches everything and returns the default.

---

## 13. Where to look first when debugging

| Symptom | Look here |
| --- | --- |
| Dave silent in Slack but no error | `slack.webhook_url` set? `journalctl -u dave -n 50` for HTTP errors |
| "DynamoDB access failed" | IAM perms on the `dave` table; AWS keys in `dave.yaml`; region match |
| "Anthropic API failed" | API key valid? Spending caps on Anthropic side? rate limits? |
| "GitHub access failed" | Token expired? Token doesn't have `repo` scope? Repo renamed? |
| Dave picks the same issue twice | Almost certainly a bug in `assign_task` — that should be impossible |
| PRs in third person ("Jack opened a PR") | `persona.generate()` prompt regression — see [persona section](#7-the-persona-system) |
| Sonnet returns malformed JSON | `worker._strip_to_json()` should handle most cases; check Sonnet response in logs |
| Auto-merge never fires | Check `auto_merge: true` in dave.yaml; check journal for "Auto-merge skipped" + reason |
| Auto-propose never fires | Idle time + max_open + max_per_day all gating; check `state.get_queue_empty_since` |
| Budget exceeds the cap | Look at `BUDGET#{date}` row in DDB; check `cost.calculate_cost` against actual model |

---

## 14. Things still cooking

Things that work today but could be improved:

- **Iterative PR mode (next up tonight).** Currently Dave does one-shot PRs: read → implement → push → done. He cannot continue work across cycles on the same branch, address review comments on his own past PRs, or break a huge issue into multiple PRs ("Part 1 of 3"). For issues too big for a single Sonnet call (e.g. "migrate the entire test suite to pytest"), this is a real limitation. The fix: save `pr_branch` and `pr_number` on the TASK row, have Sonnet return `complete: bool`, and on the next cycle clone the PR branch instead of main and continue. Optionally poll PR comments and feed them to Sonnet as guidance. Hard cap on iterations per task to prevent infinite loops on a single issue.
- **PR review mode.** Beyond his own PRs, Dave should be able to evaluate any open PR on the repo (human-filed too) and decide whether to merge, comment, or leave alone. Different control flow from the issue queue — operates on existing PRs.
- **Repo health audit.** A periodic deep read of the entire codebase (README + file tree + dependency files + test coverage + recent commits) where Dave asks Sonnet to identify problems that AREN'T in the issue tracker yet. Then files them as `dave`-tagged issues. A stronger version of auto-propose that uses full repo context.
- **Self-review pass.** After Sonnet implements an issue, ask Haiku to score the diff (does it actually solve the issue? does it introduce obvious bugs?). Only auto-merge if score >= threshold. Doubles cost per PR for marginal gain — only worth it if Dave starts shipping bad PRs.
- **CI integration.** Wait for repo's CI checks to pass before auto-merging. CarlsGarage doesn't have CI yet, so this isn't unlocked.
- **Hot-reload of dave.yaml.** Currently you have to `systemctl restart dave` to pick up config changes. The `CONFIG#SETTINGS` row in DDB exists but isn't wired to override config in-memory.
- **A web dashboard.** `python dave.py --status` is fine but a tiny FastAPI app reading from DDB would be slick.
- **Personality packs.** Currently Dave is hardcoded as "Dave Plummer" in `dave.example.yaml`. Could ship a `personalities/` directory with `doomer.yaml`, `noir-detective.yaml`, `1940s-pulp.yaml`, `corporate-mike.yaml`, etc.
- **Lesson summarization.** Lessons accumulate forever. After N lessons, summarize the older ones into a compact "wisdom" doc the worker fetches.
- **Issue feedback loop.** Currently if you close a Dave-proposed issue or post a "this is wrong" comment, Dave doesn't notice. He could check comments on his own past PRs/issues and learn from corrections.

None of these are blocking the core loop. They're all "nice to have."

---

*See `RUNBOOK.md` for how to actually operate Dave day-to-day.*
