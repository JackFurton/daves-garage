"""Logging — rich console + optional logfile.

Components pull a child logger via get_logger("component"), e.g.
get_logger("controller"). The root "hive" logger (name kept for backwards-compat
of the logger namespace) is configured once in setup_logging() before the loop starts.
"""
import logging
import sys
from pathlib import Path
from typing import Optional


_CONFIGURED = False


def setup_logging(level: str = "INFO", logfile: Optional[str] = None) -> logging.Logger:
    """Configure the 'hive' logger. Idempotent — safe to call from --status, --watch, etc.

    Console output uses rich for color when available; falls back to plain stderr.
    A logfile (if given) gets plain text so it tails and greps cleanly.
    """
    global _CONFIGURED
    logger = logging.getLogger("hive")
    logger.setLevel(getattr(logging, level.upper(), logging.INFO))
    logger.handlers.clear()
    logger.propagate = False

    # Console handler
    try:
        from rich.logging import RichHandler
        from rich.console import Console
        console = Console(stderr=False, force_terminal=None)
        console_handler = RichHandler(
            console=console,
            show_time=True,
            show_level=True,
            show_path=False,
            rich_tracebacks=True,
            markup=False,
            log_time_format="[%H:%M:%S]",
        )
        console_handler.setFormatter(logging.Formatter("%(name)s — %(message)s"))
    except ImportError:
        console_handler = logging.StreamHandler(sys.stdout)
        console_handler.setFormatter(
            logging.Formatter("%(asctime)s [%(levelname)s] %(name)s — %(message)s",
                              datefmt="%H:%M:%S")
        )
    logger.addHandler(console_handler)

    # File handler
    if logfile:
        Path(logfile).expanduser().parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(Path(logfile).expanduser())
        file_handler.setFormatter(
            logging.Formatter("%(asctime)s [%(levelname)s] %(name)s — %(message)s")
        )
        logger.addHandler(file_handler)

    _CONFIGURED = True
    return logger


def get_logger(component: str) -> logging.Logger:
    """Return a child logger like 'hive.controller'. Auto-configures with defaults
    if setup_logging() hasn't been called yet (useful in tests and one-off scripts)."""
    if not _CONFIGURED:
        setup_logging()
    return logging.getLogger(f"hive.{component}")
