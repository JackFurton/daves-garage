"""Pytest fixtures shared across the Dave test suite."""
import os
import sys

import pytest

# Make the project root importable so tests can `import dave, controller, ...`
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))


@pytest.fixture
def tmp_repo(tmp_path):
    """A throwaway directory pretending to be a cloned repo."""
    return tmp_path


@pytest.fixture
def fake_aws_credentials(monkeypatch):
    """Set fake AWS credentials so boto3 doesn't try to read from ~/.aws."""
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_SECURITY_TOKEN", "testing")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
