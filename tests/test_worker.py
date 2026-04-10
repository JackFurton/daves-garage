"""Tests for Worker — file ops, file selection fallback, JSON parsing."""
import os
from unittest.mock import MagicMock

import pytest

from config import HiveConfig
from worker import Worker


def _build_worker(tmp_path):
    """Construct a Worker without making any network calls."""
    cfg = HiveConfig(
        repo="owner/repo",
        anthropic_api_key="sk-fake",  # anthropic.Anthropic stores but doesn't validate
    )
    return Worker(
        config=cfg,
        state=MagicMock(),
        budget=MagicMock(),
        github=MagicMock(),
        slack=MagicMock(),
        persona=None,
    )


# ── _slugify ──

def test_slugify_basic():
    assert Worker._slugify("Add login bug fix") == "add-login-bug-fix"


def test_slugify_strips_special_chars():
    assert Worker._slugify("Fix: parser crashes (issue #42)!") == "fix-parser-crashes-issue-42"


def test_slugify_truncates_long_titles():
    title = "a" * 100
    assert len(Worker._slugify(title)) <= 40


# ── _strip_to_json ──

def test_strip_to_json_plain():
    assert Worker._strip_to_json('{"a": 1}') == '{"a": 1}'


def test_strip_to_json_with_fences():
    text = '```json\n{"a": 1}\n```'
    assert Worker._strip_to_json(text) == '{"a": 1}'


def test_strip_to_json_with_prose_preamble():
    text = 'Sure, here\'s the JSON:\n{"a": 1}'
    result = Worker._strip_to_json(text)
    assert result == '{"a": 1}'


def test_strip_to_json_with_trailing_chatter():
    text = '{"a": 1}\n\nLet me know if you need anything else!'
    result = Worker._strip_to_json(text)
    assert result == '{"a": 1}'


def test_strip_to_json_array():
    assert Worker._strip_to_json('[1, 2, 3]') == '[1, 2, 3]'


# ── _keyword_fallback ──

def test_keyword_fallback_ranks_by_overlap():
    files = [
        "src/auth/login.py",
        "src/utils/helpers.py",
        "src/auth/middleware.py",
        "tests/test_login.py",
        "README.md",
    ]
    picks = Worker._keyword_fallback("fix the auth login bug", files, limit=3)
    assert "src/auth/login.py" in picks
    # Helpers and README have no overlap and should be excluded
    assert "src/utils/helpers.py" not in picks
    assert "README.md" not in picks


def test_keyword_fallback_returns_empty_on_no_overlap():
    files = ["src/foo.py", "src/bar.py"]
    picks = Worker._keyword_fallback("xyzzy plugh", files)
    assert picks == []


# ── _read_files ──

def test_read_files_reads_existing(tmp_path):
    (tmp_path / "a.py").write_text("hello")
    (tmp_path / "b.py").write_text("world")
    result = Worker._read_files(str(tmp_path), ["a.py", "b.py"])
    assert result == {"a.py": "hello", "b.py": "world"}


def test_read_files_skips_missing(tmp_path):
    (tmp_path / "a.py").write_text("hello")
    result = Worker._read_files(str(tmp_path), ["a.py", "missing.py"])
    assert "a.py" in result
    assert "missing.py" not in result


def test_read_files_caps_total_bytes(tmp_path):
    (tmp_path / "a.py").write_text("a" * 100)
    (tmp_path / "b.py").write_text("b" * 100)
    (tmp_path / "c.py").write_text("c" * 100)
    result = Worker._read_files(str(tmp_path), ["a.py", "b.py", "c.py"], max_total_bytes=150)
    total = sum(len(v) for v in result.values())
    assert total <= 150


# ── _apply_changes ──

def test_apply_create(tmp_path):
    worker = _build_worker(tmp_path)
    impl = {
        "files": [
            {"path": "new_file.py", "action": "create", "content": "print('hi')"},
        ]
    }
    worker._apply_changes(impl, str(tmp_path))
    assert (tmp_path / "new_file.py").read_text() == "print('hi')"


def test_apply_create_in_subdir(tmp_path):
    worker = _build_worker(tmp_path)
    impl = {
        "files": [
            {"path": "src/sub/new_file.py", "action": "create", "content": "x = 1"},
        ]
    }
    worker._apply_changes(impl, str(tmp_path))
    assert (tmp_path / "src" / "sub" / "new_file.py").read_text() == "x = 1"


def test_apply_edit_search_replace(tmp_path):
    worker = _build_worker(tmp_path)
    (tmp_path / "f.py").write_text("def foo():\n    return 1\n")
    impl = {
        "files": [
            {
                "path": "f.py",
                "action": "edit",
                "search": "return 1",
                "replace": "return 2",
            },
        ]
    }
    worker._apply_changes(impl, str(tmp_path))
    assert "return 2" in (tmp_path / "f.py").read_text()
    assert "return 1" not in (tmp_path / "f.py").read_text()


def test_apply_edit_missing_file_does_not_crash(tmp_path):
    worker = _build_worker(tmp_path)
    impl = {
        "files": [
            {"path": "missing.py", "action": "edit", "search": "x", "replace": "y"},
        ]
    }
    # Should warn and continue, not raise
    worker._apply_changes(impl, str(tmp_path))


def test_apply_edit_search_not_found_does_not_crash(tmp_path):
    worker = _build_worker(tmp_path)
    (tmp_path / "f.py").write_text("nothing to find here")
    impl = {
        "files": [
            {"path": "f.py", "action": "edit", "search": "absent", "replace": "present"},
        ]
    }
    worker._apply_changes(impl, str(tmp_path))
    assert (tmp_path / "f.py").read_text() == "nothing to find here"


def test_apply_delete(tmp_path):
    worker = _build_worker(tmp_path)
    (tmp_path / "f.py").write_text("doomed")
    impl = {
        "files": [
            {"path": "f.py", "action": "delete"},
        ]
    }
    worker._apply_changes(impl, str(tmp_path))
    assert not (tmp_path / "f.py").exists()


def test_apply_refuses_path_traversal(tmp_path):
    worker = _build_worker(tmp_path)
    # Create something outside the repo dir to make sure we don't touch it
    outside = tmp_path.parent / "outside.txt"
    outside.write_text("untouched")
    impl = {
        "files": [
            {"path": "../outside.txt", "action": "create", "content": "owned"},
        ]
    }
    worker._apply_changes(impl, str(tmp_path))
    assert outside.read_text() == "untouched"


def test_apply_refuses_absolute_path(tmp_path):
    worker = _build_worker(tmp_path)
    impl = {
        "files": [
            {"path": "/etc/passwd", "action": "create", "content": "x"},
        ]
    }
    # Should warn and skip — must not raise
    worker._apply_changes(impl, str(tmp_path))


def test_apply_unknown_action(tmp_path):
    worker = _build_worker(tmp_path)
    impl = {"files": [{"path": "f.py", "action": "frobnicate"}]}
    worker._apply_changes(impl, str(tmp_path))  # warn + skip, no crash
