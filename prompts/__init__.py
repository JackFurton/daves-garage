"""Prompt template loader. Templates live as .md files alongside this module."""
from functools import lru_cache
from pathlib import Path

_DIR = Path(__file__).parent


@lru_cache(maxsize=None)
def load(name: str) -> str:
    """Load a prompt template by name (without .md). Cached after first read."""
    path = _DIR / f"{name}.md"
    return path.read_text()


def render(name: str, **kwargs) -> str:
    """Load a template and substitute {placeholders} from kwargs."""
    return load(name).format(**kwargs)
