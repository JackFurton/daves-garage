"""GitHub API client — issues, branches, PRs, clones."""
import os
import subprocess
from typing import Optional

import requests


class GitHubClient:
    def __init__(self, token: str, repo: str):
        self.token = token
        self.repo = repo  # "owner/repo"
        self.api = "https://api.github.com"
        self.headers = {
            "Authorization": f"token {token}",
            "Accept": "application/vnd.github.v3+json",
        }
        self._default_branch: Optional[str] = None

    # ── Repo metadata ──

    def get_default_branch(self) -> str:
        """Fetch the repo's default branch (cached). Falls back to 'main' if the API call fails."""
        if self._default_branch:
            return self._default_branch
        try:
            resp = requests.get(
                f"{self.api}/repos/{self.repo}",
                headers=self.headers,
                timeout=30,
            )
            resp.raise_for_status()
            self._default_branch = resp.json().get("default_branch", "main")
        except Exception as e:
            print(f"[github] Could not fetch default branch, falling back to 'main': {e}")
            self._default_branch = "main"
        return self._default_branch

    # ── Issues ──

    def get_issues(self, label: str) -> list:
        """Fetch open issues with a given label."""
        resp = requests.get(
            f"{self.api}/repos/{self.repo}/issues",
            headers=self.headers,
            params={"labels": label, "state": "open", "per_page": 50},
            timeout=30,
        )
        resp.raise_for_status()
        # Filter out pull requests (GitHub API returns them as issues too)
        return [i for i in resp.json() if "pull_request" not in i]

    def get_issue(self, issue_id: int) -> dict:
        resp = requests.get(
            f"{self.api}/repos/{self.repo}/issues/{issue_id}",
            headers=self.headers,
            timeout=30,
        )
        resp.raise_for_status()
        return resp.json()

    def comment_on_issue(self, issue_id: int, body: str):
        requests.post(
            f"{self.api}/repos/{self.repo}/issues/{issue_id}/comments",
            headers=self.headers,
            json={"body": body},
            timeout=30,
        )

    # ── Branches + PRs ──

    def clone_repo(self, workdir: str) -> str:
        """Shallow-clone the repo's default branch into workdir, return path."""
        repo_dir = os.path.join(workdir, self.repo.split("/")[-1])
        clone_url = f"https://x-access-token:{self.token}@github.com/{self.repo}.git"
        subprocess.run(
            [
                "git", "clone",
                "--depth=1",
                "--single-branch",
                clone_url,
                repo_dir,
            ],
            check=True, capture_output=True,
        )
        return repo_dir

    def create_branch(self, repo_dir: str, branch_name: str):
        subprocess.run(
            ["git", "checkout", "-b", branch_name],
            cwd=repo_dir, check=True, capture_output=True,
        )

    def commit_and_push(self, repo_dir: str, branch_name: str, message: str):
        subprocess.run(["git", "add", "-A"], cwd=repo_dir, check=True, capture_output=True)

        # Check if there are changes to commit
        result = subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=repo_dir, capture_output=True)
        if result.returncode == 0:
            return False  # Nothing to commit

        subprocess.run(
            ["git", "commit", "-m", message],
            cwd=repo_dir, check=True, capture_output=True,
            env={**os.environ, "GIT_AUTHOR_NAME": "Dave", "GIT_AUTHOR_EMAIL": "dave@daves-garage.bot",
                 "GIT_COMMITTER_NAME": "Dave", "GIT_COMMITTER_EMAIL": "dave@daves-garage.bot"},
        )
        subprocess.run(
            ["git", "push", "origin", branch_name],
            cwd=repo_dir, check=True, capture_output=True,
        )
        return True

    def create_pr(self, branch_name: str, title: str, body: str, base: Optional[str] = None) -> dict:
        if base is None:
            base = self.get_default_branch()
        resp = requests.post(
            f"{self.api}/repos/{self.repo}/pulls",
            headers=self.headers,
            json={
                "title": title,
                "body": body,
                "head": branch_name,
                "base": base,
            },
            timeout=30,
        )
        resp.raise_for_status()
        return resp.json()

    # ── Repo info ──

    def get_file_tree(self, repo_dir: str, max_files: int = 500) -> str:
        """Return a newline-separated list of tracked files (respects .gitignore)."""
        result = subprocess.run(
            ["git", "ls-files"],
            cwd=repo_dir, capture_output=True, text=True, check=True,
        )
        files = result.stdout.splitlines()
        if len(files) > max_files:
            files = files[:max_files] + [f"... ({len(result.stdout.splitlines()) - max_files} more files truncated)"]
        return "\n".join(files)

    def list_tracked_files(self, repo_dir: str) -> list[str]:
        """Return all tracked files as a list (used by the smart context loader)."""
        result = subprocess.run(
            ["git", "ls-files"],
            cwd=repo_dir, capture_output=True, text=True, check=True,
        )
        return [f for f in result.stdout.splitlines() if f]

    def get_readme(self, repo_dir: str) -> str:
        """Read the README if it exists."""
        for name in ["README.md", "readme.md", "README.rst", "README"]:
            path = os.path.join(repo_dir, name)
            if os.path.exists(path):
                with open(path) as f:
                    return f.read()[:5000]
        return ""
