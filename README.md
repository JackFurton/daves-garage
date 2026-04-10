# Dave 🔧

> *Hey, I'm Dave. Welcome to my shop. Today we're gonna take a look at this GitHub repo of yours, and I'll walk you through how I work on it. Now back in my day at Microsoft, we would've called this "doing your job," but the kids these days call it "agentic coding." Either way — let's pop the hood and see what we're working with.*

Dave is a portable autonomous coding loop. You point him at any GitHub repository, tag some issues with the `dave` label, and he gets to work — clone, read, implement, open a PR, optionally **merge it himself**, post to Slack, learn from what he did, and when the queue empties he can **propose his own next issues**. Forever, if you let him. The only thing that ever stops him is the daily $ cap you set.

He has a personality. The Slack posts and PR descriptions sound like a retired Microsoft engineer narrating a YouTube workshop video, because that's exactly what he is — Dave is modeled on Dave Plummer, the real retired Microsoft engineer who runs the *Dave's Garage* YouTube channel. Workshop metaphors, "back in my day at Microsoft" asides, and "smash that like button — wait, wrong platform" outros included.

> 📖 **For the system explanation**, read [`ARCHITECTURE.md`](ARCHITECTURE.md). For day-to-day operation, read [`RUNBOOK.md`](RUNBOOK.md). This README is the front door.

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
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp dave.example.yaml dave.yaml
# Edit dave.yaml — fill in 4 things:
#   1. repo: owner/your-target-repo
#   2. github_token: ghp_xxx     (or fine-grained github_pat_xxx)
#   3. anthropic_api_key: sk-ant-xxx
#   4. slack_webhook_url: https://hooks.slack.com/services/...
#   5. aws_access_key_id + aws_secret_access_key  (DDB read/write on the 'dave' table)
python setup_table.py    # creates the DynamoDB table
python dave.py --doctor  # validates everything before running
python dave.py           # let him cook
```

Now tag a GitHub issue with the `dave` label and watch him work.

**First-run sequence** (each step is a safety net — total cost ~$0.30):

```bash
python dave.py --doctor              # 1. validate every credential & connection
python dave.py --once --dry-run      # 2. full Sonnet pass, no side effects (~$0.10)
python dave.py --once                # 3. one real cycle: file → PR → Slack
python dave.py                       # 4. let it loop
```

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
- The full loop: triage → smart context → implement → PR → **auto-merge** → lessons
- **Train mode** — `auto_merge: true` + `auto_propose: true` makes the loop self-fueling: Dave merges his own PRs and files his own next issues, capped only by daily $ and per-day proposal limits
- Multi-instance safe DynamoDB state with conditional updates, heartbeats, stale-task reclaim
- Persona-driven Slack + PR narration with Sonnet summary piped verbatim to Slack (rich, in-character messages instead of templated ones)
- Smart two-pass file selection (Haiku picks 5-10 relevant files → Sonnet implements)
- Structured lessons with category/tag retrieval (Dave gets smarter on each repo over time)
- Atomic budget gate with 80% warning + post-cap kill switch (exit code 2, systemd-aware)
- Operational surface: `--once`, `--status`, `--watch`, `--dry-run`, `--doctor`
- GitHub API retry decorator (connection errors + 5xx with exponential backoff)
- Systemd-friendly exit codes + ready-to-customize unit file in `deploy/`
- AWS credentials inline in `dave.yaml` — no `~/.aws/credentials` setup required
- Test suite: **64 pytest tests** covering cost math, config loading, worker file ops, and full state via moto

Things still cooking (the next stretch):
- **Iterative PR mode** — let Dave continue work across cycles on the same branch for issues too big for one Sonnet call (e.g. "migrate the test suite to pytest"). Currently one-shot only.
- **Self-review pass** — have Haiku score Dave's diff before auto-merge, only merge if quality threshold met
- **CI integration** — wait for the repo's CI checks to pass before auto-merging
- **Hot-reload of `dave.yaml`** — pick up config changes without `systemctl restart dave`
- **Personality packs** — ship a `personalities/` directory with `doomer-linux.yaml`, `noir-detective.yaml`, etc. for one-line persona swaps
- **Issue feedback loop** — Dave reads comments on his own past PRs/issues and adjusts behavior on the next cycle (RLHF-lite)
- **Web dashboard** — tiny FastAPI app reading from DDB so you don't need to ssh into the box for `--status`

---

## Dave in the wild

Once Dave is running on a server with `auto_merge: true` and `auto_propose: true`, here's what a typical sequence looks like in `journalctl -u dave`:

```
04:43:38  Started dave.service - Dave — autonomous coding loop.
04:43:39  Persona active: Dave
04:43:39  Dave online for JackFurton/CarlsGarage
04:43:39  Model: claude-sonnet-4-6 | Budget: $5.0/day | Label: 'dave' | Poll: 60s
04:43:39  Found 0 open issues with label 'dave'
04:43:39  No pending tasks
04:43:39  Queue idle for 0.0min, waiting for 10min before proposing
04:43:39  Sleeping 60s...
...
04:53:42  Auto-proposing issue (idle 10min, 0/5 today)
04:53:46  Proposed issue #9: Add a CONTRIBUTING.md with build and test instructions
04:54:46  Found 1 open issues with label 'dave'
04:54:46  Triaging 1 new issues
04:54:48    triaged #9 → priority 3
04:54:48  [worker-3a8e2b] Picking up #9: Add a CONTRIBUTING.md with build and test instructions
04:54:50  [worker-3a8e2b] Cloning JackFurton/CarlsGarage...
04:54:51  [worker-3a8e2b] Selecting relevant files...
04:54:53  [worker-3a8e2b] Loaded 4 files into context
04:54:53  [worker-3a8e2b] Calling claude-sonnet-4-6 to implement #9...
04:55:18    created CONTRIBUTING.md
04:55:22  [worker-3a8e2b] PR created: https://github.com/JackFurton/CarlsGarage/pull/10
04:55:22  [worker-3a8e2b] Attempting auto-merge (squash)...
04:55:24  [worker-3a8e2b] Auto-merged PR #10
```

Meanwhile in Slack, baremetal_bill is posting:

```
:hey-im-dave: Hey I'm Dave, welcome to my shop. Today we will be taking a look at https://github.com/JackFurton/CarlsGarage

:dave-ponder: [JackFurton/CarlsGarage] Alright folks, I just took a look at issue #9 — looks like we're missing a CONTRIBUTING.md, which is kind of like having a workshop with no manual on the wall. I'm gonna roll up my sleeves and put one together so the next person who walks in knows where everything is.

:dave-oven-mits: [JackFurton/CarlsGarage] Alright, so I dug into the repo and put together a fresh CONTRIBUTING.md that walks through the CMake build flow, the test runner, and how to add new logger destinations — kind of like writing up a wall chart for the workshop. Think of it as that laminated card you keep next to the lathe so you don't have to dig through the manual every time. Should make it a lot easier for anyone wanting to jump in and help out.
https://github.com/JackFurton/CarlsGarage/pull/10

:dave-chad: [JackFurton/CarlsGarage] Shipped #9!
```

That's the train. One issue every 2-3 minutes when Dave has work, polite 60-second polls when he doesn't, and a 10-minute idle timer before he proposes a new one. The whole thing fits inside the $5/day cap with room to spare.

## License

MIT — do whatever you want with it. Just don't blame me when Dave's brother starts opening PRs in your repo at 3am.

---

*Hey thanks for making it this far. If you build something cool with Dave, let me know. And remember: smash that like button — wait, wrong platform.*
