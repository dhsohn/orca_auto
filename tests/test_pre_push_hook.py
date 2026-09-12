from __future__ import annotations

import json
import os
import shutil
import subprocess
import venv
from pathlib import Path

import pytest

HOOK = Path(__file__).resolve().parents[1] / ".githooks" / "pre-push"


def _git(repo: Path, env: dict[str, str], *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "core.hooksPath=/dev/null", *args],
        cwd=repo,
        env=env,
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    ).stdout.strip()


def _git_env() -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(
        GIT_CONFIG_NOSYSTEM="1",
        GIT_CONFIG_GLOBAL=os.devnull,
        GIT_TERMINAL_PROMPT="0",
        GIT_AUTHOR_NAME="Test User",
        GIT_AUTHOR_EMAIL="test@example.invalid",
        GIT_COMMITTER_NAME="Test User",
        GIT_COMMITTER_EMAIL="test@example.invalid",
    )
    return env


@pytest.mark.parametrize("new_branch", [False, True])
@pytest.mark.parametrize(
    ("changed_file", "expected_skip_install"),
    [("extensions/workflows/pyproject.toml", "0"), ("README.md", "1")],
)
def test_pre_push_refreshes_dependencies_for_extension_metadata(
    tmp_path: Path,
    changed_file: str,
    expected_skip_install: str,
    new_branch: bool,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    env = _git_env()
    _git(repo, env, "init", "-q")
    hook = repo / ".githooks" / "pre-push"
    hook.parent.mkdir()
    shutil.copy2(HOOK, hook)
    scripts = repo / "scripts"
    scripts.mkdir()
    # Observe the hook's install-step request without changing a real venv or
    # running the full suite. check.sh owns the actual pip invocation.
    (scripts / "check.sh").write_text(
        '#!/bin/sh\nprintf "skip_install=%s\\n" "$ORCA_AUTO_CHECK_SKIP_INSTALL"\n',
        encoding="utf-8",
    )
    python = tmp_path / "venv" / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    python.chmod(0o755)
    env["ORCA_AUTO_VENV"] = str(python.parent.parent)
    metadata = repo / "extensions" / "workflows" / "pyproject.toml"
    metadata.parent.mkdir(parents=True)
    metadata.write_text('[project]\ndependencies = ["orca_auto==5.0.0"]\n', encoding="utf-8")
    (repo / "README.md").write_text("Initial documentation\n", encoding="utf-8")
    _git(repo, env, "add", ".")
    _git(repo, env, "commit", "-qm", "Initial fixture")
    base = _git(repo, env, "rev-parse", "HEAD")
    _git(repo, env, "update-ref", "refs/remotes/origin/main", base)

    changed = repo / changed_file
    changed.write_text(
        '[project]\ndependencies = ["orca_auto==5.0.1"]\n'
        if changed_file.endswith("pyproject.toml")
        else "Updated documentation\n",
        encoding="utf-8",
    )
    _git(repo, env, "add", changed_file)
    _git(repo, env, "commit", "-qm", "Update fixture")
    head = _git(repo, env, "rev-parse", "HEAD")
    remote_sha = "0" * 40 if new_branch else base
    assert _git(repo, env, "status", "--porcelain") == ""

    result = subprocess.run(
        ["bash", str(hook)],
        input=f"refs/heads/fixture {head} refs/heads/fixture {remote_sha}\n",
        cwd=repo,
        env=env,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert f"skip_install={expected_skip_install}\n" in result.stdout


def test_pre_push_imports_both_roots_from_pushed_checkout_with_sibling_venv(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    sibling = tmp_path / "sibling"
    source_package = HOOK.parents[1] / "src" / "orca_auto"
    for checkout in (repo, sibling):
        core = checkout / "src" / "orca_auto"
        core.mkdir(parents=True)
        # Use the real namespace initializer, which extends the core-owned
        # package with separately installed workflow roots.
        for name in ("__init__.py", "_version.py"):
            shutil.copy2(source_package / name, core / name)
        flow = checkout / "extensions/workflows/src/orca_auto/flow"
        flow.mkdir(parents=True)
        (flow / "__init__.py").write_text("", encoding="utf-8")
        (flow / "changed.py").write_text('VALUE = "old"\n', encoding="utf-8")

    env = _git_env()
    env.pop("PYTHONPATH", None)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    _git(repo, env, "init", "-q")
    hook = repo / ".githooks" / "pre-push"
    hook.parent.mkdir()
    shutil.copy2(HOOK, hook)
    scripts = repo / "scripts"
    scripts.mkdir()
    (scripts / "check.sh").write_text(
        '#!/bin/sh\nprintf "skip_install=%s\\n" "$ORCA_AUTO_CHECK_SKIP_INSTALL"\n'
        'exec "$PYTHON_BIN" scripts/probe.py\n',
        encoding="utf-8",
    )
    probe = scripts / "probe.py"
    probe.write_text(
        "import json\nimport orca_auto\nfrom orca_auto.flow import changed\n"
        'print(json.dumps({"core": orca_auto.__file__, "flow": changed.__file__, '
        '"value": changed.VALUE}))\n',
        encoding="utf-8",
    )
    shared_venv = tmp_path / "shared-venv"
    venv.EnvBuilder(with_pip=False).create(shared_venv)
    python = shared_venv / "bin" / "python"
    site_packages = Path(
        subprocess.run(
            [str(python), "-c", "import sysconfig; print(sysconfig.get_path('purelib'))"],
            env=env,
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        ).stdout.strip()
    )
    # Model the simple editable .pth files produced by both distributions.
    (site_packages / "sibling-editable.pth").write_text(
        f"{sibling / 'src'}\n{sibling / 'extensions/workflows/src'}\n",
        encoding="utf-8",
    )
    env["ORCA_AUTO_VENV"] = str(shared_venv)
    baseline = subprocess.run(
        [str(python), str(probe)],
        cwd=repo,
        env=env,
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert json.loads(baseline.stdout) == {
        "core": str(sibling / "src/orca_auto/__init__.py"),
        "flow": str(sibling / "extensions/workflows/src/orca_auto/flow/changed.py"),
        "value": "old",
    }
    _git(repo, env, "add", ".")
    _git(repo, env, "commit", "-qm", "Initial fixture")
    base = _git(repo, env, "rev-parse", "HEAD")
    changed_file = "extensions/workflows/src/orca_auto/flow/changed.py"
    (repo / changed_file).write_text('VALUE = "new"\n', encoding="utf-8")
    _git(repo, env, "add", changed_file)
    _git(repo, env, "commit", "-qm", "Update workflow source only")
    head = _git(repo, env, "rev-parse", "HEAD")
    assert _git(repo, env, "diff", "--name-only", base, head) == changed_file
    assert _git(repo, env, "status", "--porcelain") == ""

    result = subprocess.run(
        ["bash", str(hook)],
        input=f"refs/heads/fixture {head} refs/heads/fixture {base}\n",
        cwd=repo,
        env=env,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "skip_install=1\n" in result.stdout
    assert json.loads(result.stdout.splitlines()[-1]) == {
        "core": str(repo / "src/orca_auto/__init__.py"),
        "flow": str(repo / changed_file),
        "value": "new",
    }
