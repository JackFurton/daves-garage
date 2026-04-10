"""Tests for config.load_config."""
import pytest

import config


def test_missing_config_file_raises_friendly_error(tmp_path):
    missing = tmp_path / "nope.yaml"
    with pytest.raises(FileNotFoundError) as exc:
        config.load_config(str(missing))
    assert "dave.example.yaml" in str(exc.value)


def test_load_config_basic_values(tmp_path):
    cfg_file = tmp_path / "dave.yaml"
    cfg_file.write_text(
        "repo: foo/bar\n"
        "github_token: ghp_test\n"
        "anthropic_api_key: sk-test\n"
        "max_daily_cost_usd: 5.0\n"
        "issue_label: dave\n"
    )
    cfg = config.load_config(str(cfg_file))
    assert cfg.repo == "foo/bar"
    assert cfg.github_token == "ghp_test"
    assert cfg.anthropic_api_key == "sk-test"
    assert cfg.max_daily_cost_usd == 5.0
    assert cfg.issue_label == "dave"


def test_env_var_substitution(tmp_path, monkeypatch):
    monkeypatch.setenv("MY_FAKE_TOKEN", "abc123")
    cfg_file = tmp_path / "dave.yaml"
    cfg_file.write_text(
        "repo: foo/bar\n"
        "github_token: ${MY_FAKE_TOKEN}\n"
        "anthropic_api_key: sk-test\n"
    )
    cfg = config.load_config(str(cfg_file))
    assert cfg.github_token == "abc123"


def test_env_var_fallback_when_yaml_missing_secret(tmp_path, monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "from_env_var")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "key_from_env")
    cfg_file = tmp_path / "dave.yaml"
    cfg_file.write_text("repo: foo/bar\n")
    cfg = config.load_config(str(cfg_file))
    assert cfg.github_token == "from_env_var"
    assert cfg.anthropic_api_key == "key_from_env"


def test_unknown_keys_are_ignored_with_warning(tmp_path, capsys):
    cfg_file = tmp_path / "dave.yaml"
    cfg_file.write_text(
        "repo: foo/bar\n"
        "anthropic_api_key: sk-test\n"
        "github_token: ghp_test\n"
        "totally_made_up_key: 42\n"
    )
    cfg = config.load_config(str(cfg_file))
    captured = capsys.readouterr()
    assert "totally_made_up_key" in captured.out
    assert cfg.repo == "foo/bar"


def test_persona_block_loads_as_dict(tmp_path):
    cfg_file = tmp_path / "dave.yaml"
    cfg_file.write_text(
        "repo: foo/bar\n"
        "anthropic_api_key: sk-test\n"
        "github_token: ghp_test\n"
        "persona:\n"
        "  name: Dave\n"
        "  style: A retired engineer.\n"
        "  emojis:\n"
        "    startup: ':hey-im-dave:'\n"
        "  first_message: 'Hey {repo_url}'\n"
    )
    cfg = config.load_config(str(cfg_file))
    assert cfg.persona is not None
    assert cfg.persona["name"] == "Dave"
    assert cfg.persona["emojis"]["startup"] == ":hey-im-dave:"


def test_default_values():
    cfg = config.HiveConfig()
    assert cfg.worker_model == "claude-sonnet-4-6"
    assert cfg.triage_model == "claude-haiku-4-5-20251001"
    assert cfg.dynamodb_table == "dave"
    assert cfg.issue_label == "dave"
    assert cfg.logfile == "dave.log"
    assert cfg.persona is None
    assert cfg.escalate_priority is None
