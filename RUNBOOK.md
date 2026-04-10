# Dave — Runbook

> Operating Dave day-to-day. Setup, monitoring, debugging, recovery. Read `ARCHITECTURE.md` first if you don't already know the system.

---

## Quick reference card

```bash
# Local (Mac/laptop)
python dave.py --doctor              # validate everything before running
python dave.py --once --dry-run      # safe practice run
python dave.py --once                # one real cycle
python dave.py                       # full loop, foreground
python dave.py --status              # spend, queue, recent lessons
python dave.py --watch               # tail dave.log with rich coloring

# Graviton / Hetzner / any systemd-managed Linux box
sudo systemctl start dave            # start the train
sudo systemctl stop dave             # stop the train
sudo systemctl restart dave          # restart (e.g., after config change)
sudo systemctl status dave           # current state, last 10 log lines
sudo journalctl -u dave -f           # live tail (Ctrl+C to detach)
sudo journalctl -u dave --since "10 min ago"
sudo journalctl -u dave -n 200       # last 200 lines
~/dave/.venv/bin/python ~/dave/dave.py --status --config ~/dave/dave.yaml
```

---

## 1. First-time setup (local)

You only do this once per machine.

```bash
# Clone
git clone https://github.com/JackFurton/daves-garage.git dave
cd dave

# Python env
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# Config
cp dave.example.yaml dave.yaml
# edit dave.yaml — at minimum set: repo, github_token, anthropic_api_key,
# slack_webhook_url, aws_access_key_id, aws_secret_access_key
# (see "Credentials" section below)

# DynamoDB table (one time)
python setup_table.py

# Verify
python dave.py --doctor
```

If `--doctor` is all green, you're set up. Run `python dave.py --once` for a smoke test, then `python dave.py` for the real loop.

---

## 2. Credentials — where everything goes

Dave needs four things, all of which live in `dave.yaml` (gitignored, mode 0600):

| Credential | How to get it | Where it goes |
|---|---|---|
| **GitHub Personal Access Token** | https://github.com/settings/tokens → Generate new token (classic OR fine-grained). Needs `repo` scope (read issues, write PRs, push branches). For fine-grained: Contents read/write, Issues read/write, Pull requests read/write, Metadata read. | `dave.yaml` → `github_token:` |
| **Anthropic API key** | https://console.anthropic.com/settings/keys → Create Key | `dave.yaml` → `anthropic_api_key:` |
| **AWS access key + secret** | IAM user with DynamoDB read/write on the configured table. Minimal policy: `dynamodb:*` on `arn:aws:dynamodb:*:*:table/dave`. | `dave.yaml` → `aws_access_key_id:` and `aws_secret_access_key:` |
| **Slack webhook URL** | https://api.slack.com/apps → Create New App → Incoming Webhooks → Add to channel | `dave.yaml` → `slack_webhook_url:` |

**Never check `dave.yaml` into git.** It's already in `.gitignore`. If you accidentally commit it, **rotate every credential immediately** — assume the repo is public.

If you prefer environment variables instead of inline credentials, you can use `${ENV_VAR}` substitution in the yaml:

```yaml
github_token: ${GITHUB_TOKEN}
anthropic_api_key: ${ANTHROPIC_API_KEY}
```

…and set the env vars in your shell rc file. AWS credentials can also come from `~/.aws/credentials` if you set `aws_profile: my-profile-name` in the yaml instead of inline keys.

---

## 3. Deploying to a server (the "final form")

The best home for Dave is a small Linux VM running 24/7 as a systemd service. Hetzner, DigitalOcean, EC2, Lightsail, all work fine. ARM (Graviton, Raspberry Pi) is fully supported — every dep has aarch64 wheels.

### Recipe for a fresh Amazon Linux 2023 / Debian / Ubuntu box

```bash
# 1. SSH in
ssh -i ~/your-key.pem user@your-box.example.com

# 2. Install python + git (if not already there)
sudo dnf install -y python3 python3-pip git    # Amazon Linux / RHEL family
# OR
sudo apt update && sudo apt install -y python3 python3-pip python3-venv git   # Debian / Ubuntu

# 3. Clone
cd ~
git clone https://github.com/JackFurton/daves-garage.git dave
cd dave

# 4. Venv + deps
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt

# 5. Get your dave.yaml onto the box
# Easiest: scp from your local machine (in a SECOND terminal)
#   scp -i ~/your-key.pem ~/local/dave.yaml user@your-box:/home/user/dave/dave.yaml
# Or: paste it manually with `nano dave.yaml`
chmod 600 dave.yaml

# 6. Create the DynamoDB table (once per AWS account)
python setup_table.py

# 7. Preflight
python dave.py --doctor
# All checks should be green. Fix any reds before continuing.

# 8. Install the systemd unit. The shipped deploy/dave.service template assumes
#    a dedicated 'dave' user under /opt/dave. If you're running as a non-root user
#    in your home dir (the typical EC2 setup), use this customized version:

sudo tee /etc/systemd/system/dave.service > /dev/null << EOF
[Unit]
Description=Dave — autonomous coding loop
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$(whoami)
Group=$(whoami)
WorkingDirectory=$HOME/dave
ExecStart=$HOME/dave/.venv/bin/python $HOME/dave/dave.py --config $HOME/dave/dave.yaml
Restart=on-failure
RestartSec=30
RestartPreventExitStatus=2
StandardOutput=journal
StandardError=journal
SyslogIdentifier=dave
NoNewPrivileges=true
PrivateTmp=true

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable dave
sudo systemctl start dave

# 9. Verify it's running
sudo systemctl status dave
sudo journalctl -u dave -f
```

Within 30 seconds of `start` you should see Dave's `:hey-im-dave: Hey I'm Dave, welcome to my shop...` post in your Slack channel.

### What `RestartPreventExitStatus=2` means

Dave exits with one of three codes:

| Exit code | Meaning | systemd behavior |
|---|---|---|
| `0` | Clean shutdown (signal received) | Does nothing — shutdown was intentional |
| `1` | Uncaught error | Restart after `RestartSec=30` |
| `2` | Daily budget exhausted | **Does NOT restart until you intervene** |

Code 2 is critical. It's how you avoid systemd cheerfully restarting Dave the moment he hits the cost cap, defeating the entire safety mechanism. UTC midnight rolls the daily counter; you can `sudo systemctl restart dave` any time after to resume.

---

## 4. Daily ops

### Check what Dave is doing

```bash
sudo journalctl -u dave -f                    # live tail
sudo journalctl -u dave --since "1 hour ago"  # recent activity
sudo systemctl status dave                    # is he alive? memory? PID?
~/dave/.venv/bin/python ~/dave/dave.py --status --config ~/dave/dave.yaml
```

The `--status` command prints today's spend, current queue depth, in-progress count, and the 5 most recent lessons Dave has learned.

### Restart Dave (e.g., after editing dave.yaml)

```bash
sudo systemctl restart dave
sudo journalctl -u dave -n 20    # confirm clean restart
```

Dave's state is in DDB, so a restart loses nothing. In-progress tasks resume via stale-task reclaim within `stale_task_minutes` (default 30) of the restart.

### Stop Dave

```bash
sudo systemctl stop dave
```

Dave handles SIGTERM cleanly: he finishes whatever he's doing in the current cycle, posts a `:hey-im-dave: shutting down: received signal` to Slack, exits 0. systemd does not restart on a clean stop.

### Bump the daily budget

```bash
nano ~/dave/dave.yaml
# Change max_daily_cost_usd: 5.00 → 10.00 (or whatever)
sudo systemctl restart dave
```

If Dave hit the cap and exited with code 2, the restart resumes him with the new cap.

### Add a new repo (multi-instance)

You can run multiple Daves on the same box, each pointed at a different repo. Two approaches:

**Approach A — One systemd unit per repo (cleanest):**

```bash
cp ~/dave/dave.yaml ~/dave/dave-repo-a.yaml
nano ~/dave/dave-repo-a.yaml   # set repo: owner/repo-a, slack_webhook_url: ...
chmod 600 ~/dave/dave-repo-a.yaml

# Create a templated unit
sudo cp /etc/systemd/system/dave.service /etc/systemd/system/dave@.service
sudo sed -i 's|dave.yaml|dave-%i.yaml|' /etc/systemd/system/dave@.service
sudo systemctl daemon-reload
sudo systemctl enable --now dave@repo-a
```

**Approach B — Run multiple processes manually:**

```bash
python dave.py --config dave-repo-a.yaml &
python dave.py --config dave-repo-b.yaml &
```

Both approaches share the same DynamoDB table (one `dave` table is enough). The conditional updates make it multi-instance safe.

---

## 5. Debugging — common failures

### `--doctor` says "GitHub access failed"

**Causes:**
- Token expired or revoked
- Token doesn't have `repo` scope
- Repo renamed or you don't have access
- Rate-limited (rare, GitHub API is generous)

**Fixes:**
- Generate a new token at https://github.com/settings/tokens
- Update `dave.yaml`, restart Dave
- Check the repo name in `dave.yaml` matches the actual GitHub URL

### `--doctor` says "DynamoDB access failed"

**Causes:**
- AWS credentials wrong or expired
- IAM user lacks `dynamodb:*` permission on the table
- Wrong region (table in us-east-1, credentials targeting us-west-2)
- Table doesn't exist yet

**Fixes:**
```bash
# Test AWS auth directly
.venv/bin/python -c "import boto3; print(boto3.Session(aws_access_key_id='AKI...', aws_secret_access_key='...').client('sts').get_caller_identity())"

# If that works, test DDB specifically
.venv/bin/python -c "import boto3; t = boto3.Session(...).resource('dynamodb').Table('dave'); print(t.table_status)"

# If table doesn't exist
python setup_table.py
```

The IAM policy you need:
```json
{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Action": ["dynamodb:*"],
    "Resource": "arn:aws:dynamodb:us-east-1:*:table/dave"
  }]
}
```

### `--doctor` says "Anthropic API failed"

**Causes:**
- Key invalid or revoked
- Spending limit hit on Anthropic's side
- Model name wrong (check `triage_model` and `worker_model` in `dave.yaml`)

**Fixes:**
- Test the key directly: `curl https://api.anthropic.com/v1/messages -H "x-api-key: $KEY" -H "anthropic-version: 2023-06-01" -H "content-type: application/json" -d '{"model":"claude-haiku-4-5-20251001","max_tokens":10,"messages":[{"role":"user","content":"hi"}]}'`
- Check console.anthropic.com for usage / limits
- Confirm model names match what's in `cost.py` MODEL_PRICING

### Slack webhook returns 404

**Causes:**
- Webhook URL deleted in Slack admin
- Channel deleted or app uninstalled

**Fixes:**
- Generate a new webhook URL at https://api.slack.com/apps
- Update `dave.yaml`, restart Dave

### Dave is silent (running but no Slack posts)

```bash
sudo journalctl -u dave -n 100 | grep -i slack
```

Look for "Slack post failed" warnings. The most common cause is `slack_webhook_url:` is empty or has a typo.

### Dave keeps picking up the same issue and failing

This means the worker is catching exceptions and marking the task as `failed`, then a different code path is reverting it to `pending` somehow. **This shouldn't happen** — `failed` is a terminal state.

```bash
# Inspect the task
.venv/bin/python -c "
from state import HiveState
from config import load_config
c = load_config('dave.yaml')
s = HiveState(c.dynamodb_table, aws_access_key_id=c.aws_access_key_id, aws_secret_access_key=c.aws_secret_access_key, aws_region=c.aws_region)
print(s.get_task(NUMBER))  # replace NUMBER with the issue id
"
```

If the task shows status=failed, it should NOT be picked up again. If it's status=pending, something reset it — check the journal for "Reclaimed N stale tasks" lines around the time it was reset.

### Sonnet returns malformed JSON

```bash
sudo journalctl -u dave -n 200 | grep -A 5 "invalid JSON"
```

`worker._strip_to_json()` handles ```json fences, prose preamble, and trailing chatter. If it still fails:
- Check the model name in `dave.yaml` — make sure it's a real Claude model
- Bump `max_tokens` in `worker._implement` if Sonnet is getting truncated mid-JSON
- Look at the raw response in the journal (it's logged at DEBUG level)

### Auto-merge skipped on every PR

```bash
sudo journalctl -u dave | grep "Auto-merge skipped"
```

Common reasons (logged in the warning):
- **"Pull request is not mergeable"** — branch protection requires reviews, or there are merge conflicts, or required status checks are failing.
- **"Required status check ... is expected"** — the repo has CI required, configure `dave.service` to wait or disable branch protection for the bot.
- **HTTP 403** — token doesn't have permission to merge. Need a token with `repo` scope, not just `public_repo`.

### Auto-propose never fires

Check the gates:

```bash
.venv/bin/python -c "
from state import HiveState
from config import load_config
c = load_config('dave.yaml')
s = HiveState(c.dynamodb_table, aws_access_key_id=c.aws_access_key_id, aws_secret_access_key=c.aws_secret_access_key, aws_region=c.aws_region)
print('Queue empty since:', s.get_queue_empty_since(c.repo))
print('Proposed today:', s.get_proposed_count_today(c.repo))
"
```

All three gates must pass:
- `get_queue_empty_since` must be at least `auto_propose_min_idle_minutes` ago
- Open Dave-tagged issues on GitHub must be < `auto_propose_max_open`
- Proposed count today must be < `auto_propose_max_per_day`

Also check `auto_propose: true` is actually set in `dave.yaml`.

---

## 6. Disaster recovery

### "I need to wipe DynamoDB and start over"

```bash
# Stop dave first
sudo systemctl stop dave

# Delete and recreate the table
.venv/bin/python -c "
import boto3
from config import load_config
c = load_config('dave.yaml')
ddb = boto3.Session(aws_access_key_id=c.aws_access_key_id, aws_secret_access_key=c.aws_secret_access_key, region_name=c.aws_region).client('dynamodb')
ddb.delete_table(TableName=c.dynamodb_table)
print('deleted')
"
# Wait ~30s for the delete to complete, then:
python setup_table.py

# Restart dave
sudo systemctl start dave
```

You'll lose all task history, lessons, and the daily spend counter.

### "Dave is in a runaway loop / spending too fast / broken in a bad way"

```bash
sudo systemctl stop dave
```

That's it. Dave is dead. He'll resume from DDB state when you `start` him again. If you don't trust him to come back clean, also disable:

```bash
sudo systemctl disable dave
```

Then he won't auto-start on next reboot until you re-enable.

### "Dave hit the budget cap and I need him back NOW"

```bash
# Bump the cap in the config
sudo nano ~/dave/dave.yaml
# Change max_daily_cost_usd: 5.00 → 10.00

# Restart (the prevention only applies on the SAME exit code from systemd's perspective)
sudo systemctl restart dave
```

Dave reads the new cap and resumes. The DDB counter still shows the old spend, but the new cap is higher so there's headroom.

### "Dave merged a bad PR into a real repo"

```bash
# On your laptop (not the dave box)
cd /tmp
git clone https://github.com/owner/repo.git
cd repo
git revert <commit-sha-of-the-bad-merge>
git push origin main
```

Then in Dave's `dave.yaml`, consider:
- Setting `auto_merge: false` until you investigate
- Reducing `auto_propose_max_per_day`
- Filing an issue with the `dave` label that says "Don't do X again" so the lesson gets stored and influences future runs

---

## 7. Upgrading Dave

Dave is just a git repo. To pull the latest version:

```bash
ssh user@your-box
cd ~/dave
sudo systemctl stop dave

git pull origin main
.venv/bin/pip install -r requirements.txt   # in case deps changed
.venv/bin/python -m pytest tests/ -q          # confirm nothing broken
.venv/bin/python dave.py --doctor             # confirm config still valid

sudo systemctl start dave
sudo journalctl -u dave -f                    # watch the restart
```

If the upgrade introduces new config fields, the loader logs a warning for unknown keys (not an error). Add the new fields to your `dave.yaml` if you want to opt in.

---

## 8. Backups

The only thing worth backing up is `dave.yaml` (your credentials). DynamoDB state can be recreated from scratch — Dave will pick up wherever he is on GitHub the next time he runs.

```bash
# On your laptop
scp -i ~/your-key.pem user@your-box:~/dave/dave.yaml ~/dave-yaml-backup.$(date +%Y%m%d).yaml
chmod 600 ~/dave-yaml-backup.*.yaml
```

Or just write down the four credentials in a password manager: GitHub PAT, Anthropic key, AWS access key + secret, Slack webhook URL.

---

## 9. Things you should NOT do

- **Don't `git add -f dave.yaml`.** It's gitignored for a reason. If you force-add it and push, your credentials are public.
- **Don't run Dave with `auto_merge: true` against a repo you don't own.** Dave will merge his own PRs without human review.
- **Don't set `max_daily_cost_usd` higher than you're willing to lose.** The cap is a safety net, not a goal. $5-$20/day is the sane range.
- **Don't disable `RestartPreventExitStatus=2`** in the systemd unit. That's what stops systemd from cheerfully restarting Dave the moment he hits the cap.
- **Don't share your `.pem` SSH key** with anyone (humans, AI assistants, scripts you don't control). The exact same logic applies as the credentials above — leaked SSH key means leaked server access.

---

## 10. Quick observability cheat sheet

```bash
# How much has Dave spent today?
~/dave/.venv/bin/python ~/dave/dave.py --status --config ~/dave/dave.yaml

# What did Dave do in the last hour?
sudo journalctl -u dave --since "1 hour ago" | grep -E "(PR created|Auto-merged|Picking up|Proposed)"

# Is Dave alive?
sudo systemctl is-active dave   # prints "active" or "inactive" or "failed"

# How much memory + CPU?
sudo systemctl status dave | head -5

# Did the last cycle have errors?
sudo journalctl -u dave -n 100 | grep -i "error\|warning\|failed"

# What's Dave proposing for himself today?
sudo journalctl -u dave -n 500 | grep -A 2 "Proposed issue"
```

---

*See `ARCHITECTURE.md` for the system explanation. See `README.md` for the project front door.*
