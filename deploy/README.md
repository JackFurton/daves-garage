# Deploying Dave to a VPS

Dave is built to run forever on a small Linux box. Here's the rough recipe for a fresh
Hetzner / DigitalOcean / etc. cloud VM. Adjust paths to taste.

## 1. Create a `dave` user

```bash
sudo useradd -r -m -d /opt/dave -s /bin/bash dave
sudo mkdir -p /etc/dave /var/log/dave
sudo chown dave:dave /opt/dave /var/log/dave
sudo chmod 750 /etc/dave
```

## 2. Clone Dave + install deps

```bash
sudo -u dave git clone https://github.com/JackFurton/daves-garage.git /opt/dave
cd /opt/dave
sudo -u dave python3 -m venv .venv
sudo -u dave .venv/bin/pip install -r requirements.txt
```

## 3. Drop in your config

```bash
sudo cp /opt/dave/dave.example.yaml /etc/dave/dave.yaml
sudo nano /etc/dave/dave.yaml   # set repo, slack webhook, etc.
sudo chown root:dave /etc/dave/dave.yaml
sudo chmod 640 /etc/dave/dave.yaml
```

## 4. Drop in your secrets

`/etc/dave/dave.env` holds the things you don't want in YAML — API tokens, AWS
credentials. The systemd unit loads it via `EnvironmentFile=`.

```bash
sudo tee /etc/dave/dave.env > /dev/null <<'EOF'
GITHUB_TOKEN=ghp_xxxxxxxxxxxxxxxx
ANTHROPIC_API_KEY=sk-ant-xxxxxxxxxx
AWS_ACCESS_KEY_ID=AKIAxxxxxxxxxxxxxxxx
AWS_SECRET_ACCESS_KEY=xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
AWS_DEFAULT_REGION=us-east-1
EOF
sudo chown root:dave /etc/dave/dave.env
sudo chmod 640 /etc/dave/dave.env
```

The IAM user pointed to by those AWS keys needs read/write on the DynamoDB table
named in your `dave.yaml`.

## 5. Create the DynamoDB table (one-time)

```bash
sudo -u dave .venv/bin/python /opt/dave/setup_table.py /etc/dave/dave.yaml
```

## 6. Install the systemd unit

```bash
sudo cp /opt/dave/deploy/dave.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable dave
sudo systemctl start dave
```

## 7. Watch Dave work

```bash
# Live tail via journalctl
sudo journalctl -u dave -f

# Or via Dave's own logfile (if configured in dave.yaml)
sudo -u dave /opt/dave/.venv/bin/python /opt/dave/dave.py --watch --config /etc/dave/dave.yaml

# Check what state DDB is in
sudo -u dave /opt/dave/.venv/bin/python /opt/dave/dave.py --status --config /etc/dave/dave.yaml
```

## Exit code semantics

The `Restart=on-failure` + `RestartPreventExitStatus=2` combo in `dave.service` means:

| code | what happened | systemd behavior |
| --- | --- | --- |
| `0` | clean shutdown (signal received) | does not restart |
| `1` | uncaught error | restarts after 30s |
| `2` | daily budget exhausted | does NOT restart until you re-enable |

When the budget is exhausted, Dave goes quiet until you bump the cap or until the
DDB daily counter rolls over (UTC midnight). You don't have to do anything — the
controller will start running cycles again as soon as the budget has room.

To force a restart after a budget exhaustion (e.g., after upping `max_daily_cost_usd`):

```bash
sudo systemctl restart dave
```

## Multiple Daves on one box

You can run several Dave instances pointed at different repos by templating the
unit file:

```bash
# /etc/systemd/system/dave@.service
# Same as dave.service, but with %i in the config path:
ExecStart=/opt/dave/.venv/bin/python /opt/dave/dave.py --config /etc/dave/dave-%i.yaml
```

Then:

```bash
sudo cp /etc/dave/dave.yaml /etc/dave/dave-carlsgarage.yaml  # tweak repo:
sudo cp /etc/dave/dave.yaml /etc/dave/dave-personal.yaml     # tweak repo:
sudo systemctl enable --now dave@carlsgarage dave@personal
```

The DynamoDB state is multi-instance safe (conditional updates on assign + put,
atomic budget counter), so you can point multiple Daves at the same DDB table or
give each its own.
