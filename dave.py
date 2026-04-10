#!/usr/bin/env python3
"""Dave — autonomous coding loop with personality.

Hey, I'm Dave. Point me at any GitHub repo and I'll get to work on the issues
labeled `dave` (or whatever label you configure). I clone, I read the code, I
implement, I open PRs, and I narrate the whole thing in Slack like it's a
YouTube workshop video. Back in my day at Microsoft we just called this "doing
your job," but the kids these days call it "agentic coding."

Usage:
    python dave.py                          # Run with dave.yaml
    python dave.py --config my.yaml         # Run with custom config
    python dave.py --once                   # Run one cycle and exit
    python dave.py --status                 # Show current state and exit
    python dave.py --watch                  # Tail the dave logfile (no loop)
    python dave.py --dry-run                # Run the loop but don't push code or open PRs

Exit codes (designed for systemd Restart= semantics):
    0  clean shutdown (signal received, --once finished)
    1  uncaught error (systemd should restart)
    2  daily budget exhausted (systemd should NOT restart until tomorrow;
       set RestartPreventExitStatus=2 in your unit file)
"""
import argparse
import signal
import sys
import time
from pathlib import Path

import anthropic

from config import load_config
from controller import Controller
from cost import BudgetTracker, BudgetExceeded
from github_client import GitHubClient
from logger import setup_logging, get_logger
from persona import Persona
from slack import SlackNotifier
from state import HiveState

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_BUDGET_EXHAUSTED = 2


def main() -> int:
    parser = argparse.ArgumentParser(description="Dave — autonomous coding loop with personality")
    parser.add_argument("--config", default="dave.yaml", help="Path to config file")
    parser.add_argument("--once", action="store_true", help="Run one cycle and exit")
    parser.add_argument("--status", action="store_true", help="Show status and exit")
    parser.add_argument("--watch", action="store_true", help="Tail the hive logfile and exit")
    parser.add_argument("--dry-run", action="store_true",
                        help="Run the loop but skip git push, PR creation, and DDB task writes")
    args = parser.parse_args()

    # Load config
    try:
        config = load_config(args.config)
    except FileNotFoundError as e:
        print(str(e), file=sys.stderr)
        return EXIT_ERROR

    # --watch can run without secrets
    if args.watch:
        return _watch_logfile(config.logfile)

    if not config.github_token:
        print("Error: No GitHub token. Set GITHUB_TOKEN or add to config.", file=sys.stderr)
        return EXIT_ERROR
    if not config.anthropic_api_key:
        print("Error: No Anthropic API key. Set ANTHROPIC_API_KEY or add to config.", file=sys.stderr)
        return EXIT_ERROR

    # Configure logging
    setup_logging(level=config.log_level, logfile=config.logfile)
    log = get_logger("hive")

    # Initialize components
    state = HiveState(config.dynamodb_table, config.aws_profile, config.aws_region)
    budget = BudgetTracker(state, config.max_daily_cost_usd, slack=None)  # slack wired below

    # Persona — uses the cheap triage model so live narration stays affordable.
    anthropic_client = anthropic.Anthropic(api_key=config.anthropic_api_key)
    persona = Persona(
        config=config.persona,
        client=anthropic_client,
        model=config.triage_model,
        budget=budget,
    )
    if persona.enabled:
        log.info(f"Persona active: {persona.name}")

    slack = SlackNotifier(config.slack_webhook_url, config.slack_messages, persona=persona)
    budget.slack = slack  # late-bind so budget warnings get the persona-aware notifier

    github = GitHubClient(config.github_token, config.repo)
    controller = Controller(config, state, budget, github, slack, persona=persona)

    if args.dry_run:
        log.warning("DRY RUN — no commits, no pushes, no PRs, no DDB task writes")
        # Monkey-patch the worker class to be safe.
        # The cleanest place to apply this is via a flag the controller passes.
        controller.dry_run = True

    # Status mode
    if args.status:
        _print_status(state, budget, config)
        return EXIT_OK

    # Graceful shutdown — handler only flips a flag, no I/O.
    running = True
    shutdown_reason = None

    def handle_signal(sig, frame):
        nonlocal running, shutdown_reason
        if not running:
            return
        log.info("Shutdown signal received, finishing current work...")
        shutdown_reason = "received signal"
        running = False

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    summary = (
        f"Model: {config.worker_model} | "
        f"Budget: ${config.max_daily_cost_usd}/day | "
        f"Label: '{config.issue_label}' | "
        f"Poll: {config.poll_interval_seconds}s"
    )
    slack.startup(config.repo, summary)
    log.info(f"Dave online for {config.repo}")
    log.info(summary)

    exit_code = EXIT_OK
    try:
        while running:
            try:
                controller.run_cycle()
            except BudgetExceeded as e:
                log.error(str(e))
                shutdown_reason = "budget exceeded"
                exit_code = EXIT_BUDGET_EXHAUSTED
                running = False
                break
            except KeyboardInterrupt:
                running = False
                break
            except Exception as e:
                # Cycle errors are non-fatal — log and keep going.
                log.exception(f"Cycle error: {e}")

            if args.once:
                break

            log.info(f"Sleeping {config.poll_interval_seconds}s...")
            for _ in range(config.poll_interval_seconds):
                if not running:
                    break
                time.sleep(1)
    finally:
        if shutdown_reason:
            try:
                slack.shutdown(config.repo, shutdown_reason)
            except Exception as e:
                log.warning(f"Could not post shutdown message to Slack: {e}")
        log.info("Done.")

    return exit_code


def _print_status(state: HiveState, budget: BudgetTracker, config) -> None:
    """Print current Dave status to stdout."""
    pending = state.get_pending_tasks()
    in_progress = state.get_in_progress_count()
    daily_spend = state.get_daily_spend()
    lessons = state.get_lessons(config.repo, limit=5)

    print(f"\nDave's Garage — {config.repo}")
    print(f"   Budget: ${daily_spend:.2f} / ${config.max_daily_cost_usd:.2f}")
    print(f"   Pending tasks: {len(pending)}")
    print(f"   In progress: {in_progress}")
    print(f"   Lessons stored: {len(lessons)}")

    if pending:
        print(f"\n   Top pending:")
        for t in pending[:5]:
            issue_id = t["PK"].split("#")[1]
            print(f"     #{issue_id}: {t.get('title', '?')} (priority {t.get('priority', '?')})")

    if lessons:
        print(f"\n   Recent lessons:")
        for l in lessons[:3]:
            cat = l.get("category", "?")
            print(f"     [{cat}] {l['lesson'][:80]}")
    print()


def _watch_logfile(logfile_path: str) -> int:
    """Tail the Dave logfile with rich coloring. Used by `--watch`."""
    if not logfile_path:
        print("No logfile configured. Set 'logfile' in your config.", file=sys.stderr)
        return EXIT_ERROR
    path = Path(logfile_path).expanduser()
    if not path.exists():
        print(f"Logfile not found: {path}", file=sys.stderr)
        return EXIT_ERROR

    try:
        from rich.console import Console
        console = Console()
        styled = True
    except ImportError:
        console = None
        styled = False

    def _print_line(line: str):
        line = line.rstrip("\n")
        if not styled:
            print(line)
            return
        if "[ERROR]" in line:
            console.print(line, style="bold red")
        elif "[WARNING]" in line:
            console.print(line, style="yellow")
        elif "[INFO]" in line:
            console.print(line, style="cyan")
        elif "[DEBUG]" in line:
            console.print(line, style="dim")
        else:
            console.print(line)

    # Print existing tail (last 50 lines) then follow.
    with open(path) as f:
        lines = f.readlines()
        for line in lines[-50:]:
            _print_line(line)
        try:
            while True:
                where = f.tell()
                line = f.readline()
                if not line:
                    time.sleep(0.5)
                    f.seek(where)
                else:
                    _print_line(line)
        except KeyboardInterrupt:
            return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
