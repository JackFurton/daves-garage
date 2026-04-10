#!/usr/bin/env bash
# deploy.sh — one-command deploy of Dave to the Graviton.
#
#   ./deploy.sh           # full deploy: tests → push → ssh pull → systemctl restart
#   ./deploy.sh --skip-tests   # skip the local pytest gate (use sparingly)
#   ./deploy.sh --no-restart   # pull on the Graviton but don't restart the service
#
# Configure these for your box. Override via environment variables if you want.
DAVE_HOST="${DAVE_HOST:-ec2-user@3.237.43.78}"
DAVE_KEY="${DAVE_KEY:-$HOME/my-ec2-key.pem}"
DAVE_REMOTE_DIR="${DAVE_REMOTE_DIR:-/home/ec2-user/dave}"

set -euo pipefail

# Parse flags
SKIP_TESTS=0
NO_RESTART=0
for arg in "$@"; do
    case $arg in
        --skip-tests) SKIP_TESTS=1 ;;
        --no-restart) NO_RESTART=1 ;;
        -h|--help)
            sed -n '2,12p' "$0" | sed 's/^# \?//'
            exit 0
            ;;
        *)
            echo "Unknown flag: $arg" >&2
            exit 1
            ;;
    esac
done

# Helpers
say() { printf "\n\033[1;36m▶ %s\033[0m\n" "$*"; }
ok() { printf "  \033[32m✓\033[0m %s\n" "$*"; }
warn() { printf "  \033[33m!\033[0m %s\n" "$*"; }

# Sanity: are we in the dave repo root?
if [ ! -f "dave.py" ] || [ ! -f "dave.yaml" ]; then
    echo "Error: run this from the dave repo root (where dave.py and dave.yaml live)." >&2
    exit 1
fi

# 1. Local tests (unless skipped)
if [ "$SKIP_TESTS" -eq 0 ]; then
    say "Running local test suite"
    if [ -d ".venv" ]; then
        .venv/bin/python -m pytest tests/ -q
    else
        warn "No .venv found, falling back to system python"
        python3 -m pytest tests/ -q
    fi
    ok "All tests passing"
else
    warn "Skipping tests (--skip-tests)"
fi

# 2. Make sure local commits are on github
say "Pushing local commits"
if git diff --quiet HEAD origin/main 2>/dev/null; then
    ok "Local main is in sync with origin/main, no push needed"
else
    git push origin main
    ok "Pushed to origin/main"
fi

# 3. Pull on the Graviton
say "Pulling latest on $DAVE_HOST"
ssh -i "$DAVE_KEY" "$DAVE_HOST" "cd $DAVE_REMOTE_DIR && git pull origin main"
ok "Code updated on Graviton"

# 4. Restart the systemd service
if [ "$NO_RESTART" -eq 0 ]; then
    say "Restarting dave.service"
    ssh -i "$DAVE_KEY" "$DAVE_HOST" "sudo systemctl restart dave && sleep 2 && sudo systemctl is-active dave"
    ok "dave.service restarted"

    say "Last 15 journal lines"
    ssh -i "$DAVE_KEY" "$DAVE_HOST" "sudo journalctl -u dave -n 15 --no-pager"
else
    warn "Skipping restart (--no-restart) — Dave is still running the OLD code until you manually restart"
fi

echo
say "Deploy complete"
echo
echo "Useful follow-ups:"
echo "  ssh -i $DAVE_KEY $DAVE_HOST 'sudo journalctl -u dave -f'    # live tail"
echo "  ./deploy.sh --no-restart                                    # next time, if you don't want to restart"
