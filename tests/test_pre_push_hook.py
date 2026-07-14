from __future__ import annotations

import os
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
PRE_PUSH_HOOK = REPO_ROOT / ".githooks" / "pre-push"


def _clean_git_env() -> dict[str, str]:
    env = os.environ.copy()
    result = subprocess.run(
        ["git", "rev-parse", "--local-env-vars"],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    for name in result.stdout.splitlines():
        env.pop(name, None)
    return env


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=repo,
        env=_clean_git_env(),
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def test_pre_push_sanitizes_git_hook_environment_and_pins_worktree_source(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.name", "Hook Test")
    _git(repo, "config", "user.email", "hook@example.invalid")

    (repo / ".gitignore").write_text(".venv/\n", encoding="utf-8")
    (repo / "src").mkdir()
    (repo / "src" / ".keep").write_text("", encoding="utf-8")
    (repo / "scripts").mkdir()
    check_script = repo / "scripts" / "check.sh"
    check_script.write_text(
        """#!/usr/bin/env bash
set -euo pipefail
while IFS= read -r name; do
  if [[ -v "$name" ]]; then
    echo "repository-local variable leaked: $name" >&2
    exit 91
  fi
done < <(git rev-parse --local-env-vars)
[[ "$PYTHONPATH" == "$PWD/src:/wrong/editable/src" ]]
printf '%s\n' '[probe] sanitized'
""",
        encoding="utf-8",
    )
    check_script.chmod(0o755)
    _git(repo, "add", ".gitignore", "src/.keep", "scripts/check.sh")
    _git(repo, "-c", "commit.gpgsign=false", "commit", "-qm", "initial")

    fake_python = repo / ".venv" / "bin" / "python"
    fake_python.parent.mkdir(parents=True)
    fake_python.write_text("#!/usr/bin/env sh\nexit 0\n", encoding="utf-8")
    fake_python.chmod(0o755)

    head = _git(repo, "rev-parse", "HEAD")
    env = _clean_git_env()
    env.update(
        {
            "GIT_DIR": str(repo / ".git"),
            "GIT_WORK_TREE": str(repo),
            "GIT_INDEX_FILE": str(repo / ".git" / "index"),
            "ORCA_AUTO_VENV": str(repo / ".venv"),
            "PYTHONPATH": "/wrong/editable/src",
        }
    )
    result = subprocess.run(
        [str(PRE_PUSH_HOOK)],
        cwd=repo,
        env=env,
        input=f"refs/heads/test {head} refs/heads/test {'0' * 40}\n",
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert "[probe] sanitized" in result.stdout
    assert _git(repo, "rev-list", "--count", "HEAD") == "1"
    assert _git(repo, "status", "--short") == ""
