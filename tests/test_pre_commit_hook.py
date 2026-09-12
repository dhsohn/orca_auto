from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

HOOK = Path(__file__).resolve().parents[1] / ".githooks" / "pre-commit"


def _git(repo: Path, env: dict[str, str], *args: str) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        env=env,
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    ).stdout


def _executable(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")
    path.chmod(0o755)


def _fixture(tmp_path: Path) -> tuple[Path, Path, dict[str, str]]:
    repo = tmp_path / "repo"
    repo.mkdir()
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.pop("PYTHONPATH", None)
    env.update(
        GIT_CONFIG_NOSYSTEM="1",
        GIT_CONFIG_GLOBAL=os.devnull,
        GIT_TERMINAL_PROMPT="0",
        PYTHONDONTWRITEBYTECODE="1",
    )
    _git(repo, env, "init", "-q")
    hook = repo / ".githooks" / "pre-commit"
    hook.parent.mkdir()
    shutil.copy2(HOOK, hook)
    tools = tmp_path / "shared-venv" / "bin"
    tools.mkdir(parents=True)
    _executable(tools / "python", "#!/bin/sh\nexit 0\n")
    _executable(tools / "lint-imports", "#!/bin/sh\nexit 0\n")
    env["ORCA_AUTO_VENV"] = str(tools.parent)
    return repo, hook, env


def _run_hook(repo: Path, hook: Path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(hook)],
        cwd=repo,
        env=env,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )


@pytest.mark.parametrize("analyzer_exit", [0, 17])
def test_pre_commit_restores_untracked_links_and_unstaged_content(
    tmp_path: Path,
    analyzer_exit: int,
) -> None:
    repo, hook, env = _fixture(tmp_path)
    readme = repo / "README"
    readme.write_text("staged content\n", encoding="utf-8")
    (repo / "staged-link").symlink_to("missing")
    _git(repo, env, "add", ".")
    readme.write_text("unstaged content\n", encoding="utf-8")
    links = {
        "relative-link": "README",
        "dangling-link": "missing",
        "absolute-link": str(readme),
        "nested/link with spaces": "../README",
    }
    for name, target in links.items():
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.symlink_to(target)
    note = repo / "notes.txt"
    note.write_bytes(b"untracked content\x00\n")
    python = Path(env["ORCA_AUTO_VENV"]) / "bin" / "python"
    _executable(python, f"#!/bin/sh\nexit {analyzer_exit}\n")
    before_status = _git(repo, env, "status", "--porcelain=v1", "-z")
    before_index = _git(repo, env, "write-tree")

    result = _run_hook(repo, hook, env)

    assert result.returncode == analyzer_exit, result.stdout + result.stderr
    for name, target in {**links, "staged-link": "missing"}.items():
        assert (repo / name).is_symlink(), name
        assert os.readlink(repo / name) == target
    assert readme.read_text(encoding="utf-8") == "unstaged content\n"
    assert note.read_bytes() == b"untracked content\x00\n"
    assert _git(repo, env, "status", "--porcelain=v1", "-z") == before_status
    assert _git(repo, env, "write-tree") == before_index
    assert not list((repo / ".git").glob("pre-commit-*"))


@pytest.mark.parametrize("checked_tree_broken", [True, False])
def test_pre_commit_import_linter_checks_staged_tree_not_shared_venv(
    tmp_path: Path,
    checked_tree_broken: bool,
) -> None:
    repo, hook, env = _fixture(tmp_path)
    sibling = tmp_path / "sibling"
    for root, broken in [(repo, checked_tree_broken), (sibling, not checked_tree_broken)]:
        package = root / "src" / "orca_auto"
        package.mkdir(parents=True)
        (package / "__init__.py").write_text("", encoding="utf-8")
        extension = root / "extensions/workflows/src/orca_auto/flow"
        extension.mkdir(parents=True)
        (extension / "__init__.py").write_text("", encoding="utf-8")
        (extension / "higher.py").write_text("VALUE = 1\n", encoding="utf-8")
        (package / "lower.py").write_text(
            "from orca_auto.flow import higher\n" if broken else "VALUE = 2\n",
            encoding="utf-8",
        )
    (repo / "scripts").mkdir()
    shutil.copy2(HOOK.parents[1] / "scripts/check_imports.py", repo / "scripts/check_imports.py")
    (repo / "pyproject.toml").write_text(
        '[tool.importlinter]\nroot_packages = ["orca_auto"]\n'
        '[[tool.importlinter.contracts]]\nname = "lower cannot import higher"\n'
        'type = "forbidden"\nsource_modules = ["orca_auto.lower"]\n'
        'forbidden_modules = ["orca_auto.flow.higher"]\n',
        encoding="utf-8",
    )
    _git(repo, env, "add", ".")
    # Model sibling editable roots; the actual adapter must select both staged
    # roots, not silently pass by reading either sibling component instead.
    lint_code = (
        f"import sys, runpy; sys.path.insert(0, {str(sibling / 'src')!r}); "
        f"sys.path.insert(0, {str(sibling / 'extensions/workflows/src')!r}); "
        "sys.argv = ['scripts/check_imports.py', '--no-cache']; "
        "runpy.run_path('scripts/check_imports.py', run_name='__main__')"
    )
    python = Path(env["ORCA_AUTO_VENV"]) / "bin" / "python"
    _executable(
        python,
        '#!/bin/sh\nif [ "$1" != "scripts/check_imports.py" ]; then exit 0; fi\n'
        f"exec {shlex.quote(sys.executable)} -c {shlex.quote(lint_code)}\n",
    )

    result = _run_hook(repo, hook, env)

    assert "Analyzed 4 files" in result.stdout
    assert result.returncode == int(checked_tree_broken), result.stdout + result.stderr
    verdict = "BROKEN" if checked_tree_broken else "KEPT"
    assert f"lower cannot import higher {verdict}" in result.stdout
