# Dave 🔧

> *Hey, I'm Dave. Welcome to my shop. Today we're gonna take a look at this GitHub repo of yours, and I'll walk you through how I work on it. Now back in my day at Microsoft, we would've called this "doing your job," but the kids these days call it "agentic coding." Either way — let's pop the hood and see what we're working with.*

Dave is a portable autonomous coding loop. You point him at any GitHub repository, tag some issues with the `dave` label, and he gets to work — clone, read, implement, open a PR, post to Slack, learn from what he did, repeat. Forever, if you let him. The only thing that ever stops him is the daily $ cap you set.

He has a personality. The Slack posts and PR descriptions sound like a retired Microsoft engineer narrating a YouTube workshop video, because that's exactly what he is.

---

## What he does

```
┌─────────────────────────────────────────────────────────────┐
│  Controller (every poll_interval)                            │
│                                                              │
│  0. Reclaim any stale in-progress tasks (crashed workers)    │
│  1. Fetch open issues with the `dave` label                  │
│  2. Triage new ones with Haiku (priority + approach)         │
│  3. Pick the top pending task and dispatch a worker          │
│  4. Sleep until next cycle                                   │
└──────────────────┬──────────────────────────────────────────┘
                   │
                   ▼
┌─────────────────────────────────────────────────────────────┐
│  Worker (per issue)                                          │
│                                                              │
│  1. Clone repo (shallow), create branch                      │
│  2. Ask Haiku which files are relevant to the issue          │
│  3. Read those files, fetch lessons from prior runs          │
│  4. Call Sonnet (or Opus on P1s) with the full context       │
│  5. Apply file changes — create / edit / delete              │
│  6. Commit, push, open a PR — in Dave's voice                │
│  7. Heartbeat throughout so crashes get reclaimed            │
│  8. Extract structured lessons → DynamoDB for next time      │
└─────────────────────────────────────────────────────────────┘
```

The key feature: **smart file selection.** Instead of dumping the entire repo into the prompt, Dave does a two-pass context load. Haiku picks the 5-10 files that matter for the issue, then those files (and only those) get fed to Sonnet. Cheap, fast, and a lot smarter than blasting the file tree at the model.

The other key feature: **the persona is wired everywhere.** Dave's voice shows up in Slack, in PR descriptions, in issue comments, even in the lessons he saves to DynamoDB. The code itself stays clean — only the prose is in character.

---

## Quick start

```bash
git clone https://github.com/JackFurton/daves-garage.git dave
cd dave
pip install -r requirements.txt
cp dave.example.yaml dave.yaml
# Edit dave.yaml — set your repo, GitHub token, Anthropic key, Slack webhook
python setup_table.py    # creates the DynamoDB table
python dave.py           # let him cook
```

Now tag a GitHub issue with the `dave` label and watch him work.

---

## Configuration

Everything lives in `dave.yaml`. The most important knobs:

```yaml
repo: your-username/your-repo      # what Dave will work on
github_token: ${GITHUB_TOKEN}
anthropic_api_key: ${ANTHROPIC_API_KEY}

worker_model: claude-sonnet-4-6
max_daily_cost_usd: 10.00          # the only thing that ever stops Dave

issue_label: dave                  # label Dave watches for
poll_interval_seconds: 60
stale_task_minutes: 30             # how long until crashed workers get reclaimed
```

See `dave.example.yaml` for the full set of options including the persona block.

---

## Running it

```bash
python dave.py                # default — runs the loop forever
python dave.py --once         # one cycle, then exit (great for cron / smoke tests)
python dave.py --status       # dump current state and exit
python dave.py --watch        # tail dave.log with rich coloring (no loop)
python dave.py --dry-run      # full loop, but no commits / no PRs / no DDB writes
python dave.py --doctor       # preflight: validates GitHub, Anthropic, DDB, Slack
```

**First-run sequence** (do this in order — each step is a safety net):

```bash
python dave.py --doctor              # 1. validate every credential & connection
python dave.py --once --dry-run      # 2. full Sonnet pass, no side effects (~$0.10)
python dave.py --once                # 3. one real cycle: file → PR → Slack
python dave.py                       # 4. let it loop
```

### Multiple repos in parallel

You can run Dave against several repos at once. Each instance has its own config, its own Slack webhook, its own daily budget:

```bash
python dave.py --config dave-project-a.yaml &
python dave.py --config dave-project-b.yaml &
```

The DynamoDB state is safe under concurrent runs — task assignment uses conditional updates so two Daves can never grab the same issue.

### Running it forever on a server

Dave is designed to be deployed on a VPS as a long-lived systemd service. Exit codes are systemd-friendly:

| code | meaning | systemd should... |
| --- | --- | --- |
| `0` | clean shutdown (signal received) | not restart (it was on purpose) |
| `1` | uncaught error | restart |
| `2` | daily budget exhausted | NOT restart until tomorrow — set `RestartPreventExitStatus=2` |

A sample `dave.service` unit file is in the works — see the `Phase 5` task list.

---

## How smart is the file selection?

The worker does a two-pass context load:

1. **Pass 1 (Haiku, ~1k tokens):** "Given this issue and this list of all tracked files, return the 5-10 files an agent would need to read to implement this." Cheap, fast, surprisingly accurate.
2. **Pass 2 (Sonnet, ~3-8k tokens):** Gets the issue + the file tree + the README + the actual *contents* of the files Haiku picked + relevant past lessons. Then implements.

If Haiku's selection fails for any reason, it falls back to a keyword-overlap heuristic: rank files by how many issue keywords appear in their path, take the top 5.

---

## Cost control

Dave tracks every API call atomically in DynamoDB. The flow:

- Each call's input/output tokens are converted to USD using the current price table in `cost.py`
- The amount is `ADD`ed to today's total in DDB (atomic, race-safe)
- The post-increment total is checked against `max_daily_cost_usd`
- At 80% of the cap, Dave posts a Slack warning
- At 100%, the loop raises `BudgetExceeded` and exits with code `2`

Multiple Dave instances share the same daily counter, so two instances pointed at the same DynamoDB table can share a budget across repos.

---

## DynamoDB schema

Single table, composite key:

| PK | SK | Purpose |
|---|---|---|
| `TASK#{issue_id}` | `META` | Status, priority, approach, worker assignment, heartbeat |
| `TASK#{issue_id}` | `RESULT` | PR URL, summary (after completion) |
| `BUDGET#{date}` | `SPEND` | Atomic daily spend counter + call count |
| `LESSON#{id}` | `META` | Structured lesson with category, tags, source issue |
| `CONFIG` | `SETTINGS` | Hot-reloadable overrides |

The `setup_table.py` script creates it as `PAY_PER_REQUEST`, so you only pay for the few writes per cycle.

---

## The Dave persona

Dave's voice is wired in two places:

**1. Slack posts** — every notification goes through `persona.generate(event, context)`, which calls Haiku with the persona's `style` block and asks for a single line in character. So every Slack post is improvised, fresh, and stays consistent without being templated.

**2. Worker prompts** — when the worker calls Sonnet to implement an issue, the persona's `style` is prepended as a "Voice" section. Sonnet writes the plan, summary, lessons, and PR description in Dave's voice, but the actual code stays clean and idiomatic. The voice applies to prose only.

You can disable Dave by commenting out the `persona` block in `dave.yaml`. The loop still works fine, just with boring default messages.

You can also replace him. Want a doomer Linux greybeard instead? A chipper junior dev? A 1940s noir detective narrating bug fixes? Just edit the persona block — name, style, emojis, first message. The whole thing is config-driven.

```yaml
persona:
  name: Dave
  style: |
    Retired Microsoft engineer running a YouTube workshop channel.
    Warm, educational, friendly. References your Windows 95/NT/XP days.
    Workshop and garage metaphors. Signs off like the end of a video.
  emojis:
    startup:      ":hey-im-dave:"
    pr_created:   ":dave-oven-mits:"
    pr_merged:    ":dave-chad:"
    error:        ":dave-ponder:"
  first_message: "Hey I'm Dave, welcome to my shop. Today we will be taking a look at {repo}"
```

---

## Project layout

```
dave/
├── dave.py              # entry point — the loop
├── controller.py        # fetch issues, triage, dispatch
├── worker.py            # clone, smart context, implement, PR
├── persona.py           # Dave's voice — Slack generation + prompt injection
├── slack.py             # webhook notifications, persona-aware
├── state.py             # DynamoDB — tasks, budget, lessons, reclaim
├── cost.py              # token tracking + atomic budget gate
├── github_client.py     # GitHub API + git operations
├── config.py            # YAML config loader with env var fallback
├── logger.py            # rich-flavored logging setup
├── setup_table.py       # one-shot DynamoDB table creator
├── prompts/
│   ├── triage.md        # Haiku triage prompt
│   ├── select_files.md  # Haiku file-selection prompt
│   └── implement.md     # Sonnet implementation prompt
├── dave.example.yaml    # config template with the Dave persona pre-filled
├── requirements.txt
└── README.md            # you are here
```

---

## Roadmap

Things that work today:
- The full loop: triage → smart context → implement → PR → lessons
- Multi-instance safe DynamoDB state with conditional updates, heartbeats, stale-task reclaim
- Persona-driven Slack + PR narration
- Smart two-pass file selection (Haiku picks → Sonnet implements)
- Structured lessons with category/tag retrieval
- Atomic budget gate with 80% warning + overshoot guard
- Operational surface: `--once`, `--status`, `--watch`, `--dry-run`, `--doctor`
- GitHub API retry decorator (connection errors + 5xx with exponential backoff)
- Systemd-friendly exit codes + sample unit file in `deploy/`
- **Auto-propose mode** — when the issue queue is empty for `auto_propose_min_idle_minutes`, Dave reads the repo and proposes ONE issue. Capped at `auto_propose_max_per_day` and `auto_propose_max_open` so it can't run away.
- Test suite: 64 pytest tests covering cost, config, worker file ops, and state via moto

Things you might still want (open follow-ups):
- A way to give Dave feedback when his PRs are bad (RLHF-lite — comment markers he reads on his next cycle)
- Hot-reload of `dave.yaml` without restarting (currently you have to `systemctl restart dave`)
- A web dashboard for `dave --status` (DDB → static HTML or a tiny FastAPI app)
- More personality packs in `dave.example.yaml` so users can swap "doomer Linux greybeard" / "noir detective" / "1940s pulp narrator" out of the box

---

## License

MIT — do whatever you want with it. Just don't blame me when Dave's brother starts opening PRs in your repo at 3am.

---

*Hey thanks for making it this far. If you build something cool with Dave, let me know. And remember: smash that like button — wait, wrong platform.*
