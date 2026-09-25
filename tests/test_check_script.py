from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import sys
import venv
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
CHECK_SCRIPT = REPO_ROOT / "scripts" / "check.sh"

# Not covered here: the ``-O`` ownership guards in ``repo_default_venv_is_repairable``
# need a foreign-owned fixture (root or chown), which these tests cannot create.


def _copy_check_script(tmp_path: Path) -> tuple[Path, Path]:
    repo = tmp_path / "repo"
    scripts = repo / "scripts"
    scripts.mkdir(parents=True)
    script = scripts / "check.sh"
    shutil.copy2(CHECK_SCRIPT, script)
    # This suite tests venv lifecycle, not import graph analysis (covered by the
    # staged-hook tests). Keep the analyzer stub inside the copied checkout.
    (scripts / "check_imports.py").write_text("raise SystemExit(0)\n", encoding="utf-8")
    # Likewise the docs parity gate: it has its own suite (test_check_docs_parity).
    (scripts / "check_docs_parity.py").write_text("raise SystemExit(0)\n", encoding="utf-8")
    # The provenance guard requires the gate to import this checkout's package.
    package = repo / "src" / "orca_auto"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    return repo, script


def _expected_package(repo: Path) -> str:
    return str((repo / "src" / "orca_auto" / "__init__.py").resolve())


def _write_bootstrap_python(path: Path) -> None:
    path.write_text(
        """#!/usr/bin/env bash
set -euo pipefail

if [[ "${1:-}" == "-m" && "${2:-}" == "venv" && -n "${3:-}" ]]; then
  target="$3"
  mkdir -p "$target/bin"
  cp "$0" "$target/bin/python"
  printf 'home = /fake\ninclude-system-site-packages = false\n' > "$target/pyvenv.cfg"
  printf '#!/usr/bin/env bash\nexit 0\n' > "$target/bin/lint-imports"
  touch "$target/.created-by-bootstrap"
  chmod +x "$target/bin/python" "$target/bin/lint-imports"
  exit 0
fi

if [[ "$(basename -- "$0")" == "python" ]]; then
  if [[ "${1:-}" == "-c" && "${2:-}" == *orca_auto* ]]; then
    printf '%s\n' "${FAKE_ORCA_AUTO_FILE:-}"
  elif [[ "${1:-}" == "-c" ]]; then
    printf '%s\n' "$0"
  fi
  exit 0
fi

exit 91
""",
        encoding="utf-8",
    )
    path.chmod(0o755)


def _write_discoverable_python(path: Path, *, suitable: bool) -> None:
    """A bootstrap fake that also answers the interpreter-suitability probe."""
    _write_bootstrap_python(path)
    probe_rc = 0 if suitable else 1
    text = path.read_text(encoding="utf-8").replace(
        "set -euo pipefail\n",
        f'set -euo pipefail\n\nif [[ "${{1:-}}" == "-" ]]; then\n  exit {probe_rc}\nfi\n',
        1,
    )
    path.write_text(text, encoding="utf-8")


def _minimal_tool_path(tmp_path: Path) -> Path:
    """A PATH directory holding only the tools check.sh needs, and no python."""
    tools = tmp_path / "tools"
    tools.mkdir()
    for tool in ("bash", "realpath", "dirname", "basename", "mkdir", "cp", "rm", "touch", "chmod"):
        found = shutil.which(tool)
        assert found is not None, tool
        (tools / tool).symlink_to(found)
    return tools


def _run_discovery_check(
    repo: Path, script: Path, *, home: Path, path: Path
) -> subprocess.CompletedProcess[str]:
    env = {
        "HOME": str(home),
        "PATH": str(path),
        "ORCA_AUTO_CHECK_SKIP_INSTALL": "1",
        "FAKE_ORCA_AUTO_FILE": _expected_package(repo),
    }
    return subprocess.run(
        [shutil.which("bash") or "bash", str(script)],
        cwd=repo,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )


_SYSTEM_PYTHON_DIRS = (Path("/usr/local/bin"), Path("/opt/homebrew/bin"), Path("/opt/conda/bin"))


def _system_dirs_have_python() -> bool:
    return any(
        (directory / name).exists()
        for directory in _SYSTEM_PYTHON_DIRS
        for name in ("python3.13", "python3.12", "python3.11", "python3")
    )


def _run_check(
    repo: Path,
    script: Path,
    bootstrap_python: Path,
    *,
    venv: Path | None = None,
    pythonpath: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env.pop("ORCA_AUTO_VENV", None)
    env.pop("PYTHONPATH", None)
    env.update(
        {
            "ORCA_AUTO_CHECK_SKIP_INSTALL": "1",
            "PYTHON_BIN": str(bootstrap_python),
            "FAKE_ORCA_AUTO_FILE": _expected_package(repo),
        }
    )
    if pythonpath is not None:
        env["PYTHONPATH"] = str(pythonpath)
    if venv is not None:
        env["ORCA_AUTO_VENV"] = str(venv)
    return subprocess.run(
        ["bash", str(script)],
        cwd=repo,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )


def _create_usable_test_venv(path: Path, *, source_root: Path | None = None) -> None:
    venv.EnvBuilder(with_pip=False).create(path)
    python = path / "bin" / "python"
    purelib = Path(
        subprocess.run(
            [
                str(python),
                "-c",
                "import sysconfig; print(sysconfig.get_path('purelib'))",
            ],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
    )
    if source_root is not None:
        # Stand-in for the editable install: put the checkout's src on sys.path.
        (purelib / "_orca_auto_src.pth").write_text(f"{source_root}\n", encoding="utf-8")
    for module in ("ruff", "mypy", "pytest"):
        package = purelib / module
        package.mkdir()
        (package / "__init__.py").write_text("", encoding="utf-8")
        (package / "__main__.py").write_text("raise SystemExit(0)\n", encoding="utf-8")
    lint_imports = path / "bin" / "lint-imports"
    lint_imports.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
    lint_imports.chmod(0o755)


def test_repairs_owned_marked_default_venv(tmp_path: Path) -> None:
    repo, script = _copy_check_script(tmp_path)
    bootstrap_python = tmp_path / "bootstrap-python"
    _write_bootstrap_python(bootstrap_python)
    venv = repo / ".venv"
    venv.mkdir()
    (venv / "pyvenv.cfg").write_text("home = /broken\n", encoding="utf-8")
    sentinel = venv / "stale-package"
    sentinel.write_text("stale", encoding="utf-8")

    result = _run_check(repo, script, bootstrap_python)

    assert result.returncode == 0, result.stderr
    assert "Recreating unusable virtual environment" in result.stdout
    assert not sentinel.exists()
    assert (venv / ".created-by-bootstrap").is_file()


def test_preserves_existing_explicit_external_venv(tmp_path: Path) -> None:
    repo, script = _copy_check_script(tmp_path)
    bootstrap_python = tmp_path / "bootstrap-python"
    _write_bootstrap_python(bootstrap_python)
    external = tmp_path / "shared-venv"
    external.mkdir()
    (external / "pyvenv.cfg").write_text("home = /broken\n", encoding="utf-8")
    sentinel = external / "sentinel"
    sentinel.write_text("keep", encoding="utf-8")

    result = _run_check(repo, script, bootstrap_python, venv=external)

    assert result.returncode == 1
    assert "Refusing to replace unsafe virtual environment target" in result.stderr
    assert sentinel.read_text(encoding="utf-8") == "keep"
    assert not (external / ".created-by-bootstrap").exists()


def test_rejects_marked_external_target_bound_to_base_python(tmp_path: Path) -> None:
    repo, script = _copy_check_script(tmp_path)
    bootstrap_python = tmp_path / "bootstrap-python"
    _write_bootstrap_python(bootstrap_python)
    external = tmp_path / "base-python-target"
    (external / "bin").mkdir(parents=True)
    python = external / "bin" / "python"
    python.write_text(
        f'#!/usr/bin/env bash\nexec {shlex.quote(sys.executable)} "$@"\n',
        encoding="utf-8",
    )
    python.chmod(0o755)
    (external / "pyvenv.cfg").write_text("home = /fake\n", encoding="utf-8")
    sentinel = external / "sentinel"
    sentinel.write_text("keep", encoding="utf-8")

    result = _run_check(repo, script, bootstrap_python, venv=external)

    assert result.returncode == 1
    assert "Refusing to replace unsafe virtual environment target" in result.stderr
    assert sentinel.read_text(encoding="utf-8") == "keep"
    assert python.is_file()


def test_preserves_unmarked_default_target(tmp_path: Path) -> None:
    repo, script = _copy_check_script(tmp_path)
    bootstrap_python = tmp_path / "bootstrap-python"
    _write_bootstrap_python(bootstrap_python)
    venv = repo / ".venv"
    (venv / "bin").mkdir(parents=True)
    (venv / "bin" / "python").symlink_to(sys.executable)
    sentinel = venv / "sentinel"
    sentinel.write_text("keep", encoding="utf-8")

    result = _run_check(repo, script, bootstrap_python)

    assert result.returncode == 1
    assert "Refusing to replace unsafe virtual environment target" in result.stderr
    assert sentinel.read_text(encoding="utf-8") == "keep"


def test_preserves_symlinked_default_target(tmp_path: Path) -> None:
    repo, script = _copy_check_script(tmp_path)
    bootstrap_python = tmp_path / "bootstrap-python"
    _write_bootstrap_python(bootstrap_python)
    shared = tmp_path / "shared-default"
    _create_usable_test_venv(shared)
    sentinel = shared / "sentinel"
    sentinel.write_text("keep", encoding="utf-8")
    venv = repo / ".venv"
    venv.symlink_to(shared, target_is_directory=True)

    result = _run_check(repo, script, bootstrap_python)

    assert result.returncode == 1
    assert "Refusing symlinked repository virtual environment" in result.stderr
    assert venv.is_symlink()
    assert sentinel.read_text(encoding="utf-8") == "keep"


def test_creates_absent_explicit_venv_via_bootstrap_python(tmp_path: Path) -> None:
    repo, script = _copy_check_script(tmp_path)
    bootstrap_python = tmp_path / "bootstrap-python"
    _write_bootstrap_python(bootstrap_python)
    explicit = tmp_path / "new-shared-venv"

    result = _run_check(repo, script, bootstrap_python, venv=explicit)

    assert result.returncode == 0, result.stderr
    assert "Creating virtual environment" in result.stdout
    assert (explicit / "pyvenv.cfg").is_file()
    assert (explicit / ".created-by-bootstrap").is_file()


def test_accepts_usable_explicit_external_venv(tmp_path: Path) -> None:
    repo, script = _copy_check_script(tmp_path)
    bootstrap_python = tmp_path / "bootstrap-python"
    _write_bootstrap_python(bootstrap_python)
    external = tmp_path / "usable-shared-venv"
    _create_usable_test_venv(external, source_root=repo / "src")
    sentinel = external / "sentinel"
    sentinel.write_text("keep", encoding="utf-8")

    result = _run_check(repo, script, bootstrap_python, venv=external)

    assert result.returncode == 0, result.stderr
    assert "Using Python" in result.stdout
    assert "Creating virtual environment" not in result.stdout
    assert "Recreating unusable virtual environment" not in result.stdout
    assert sentinel.read_text(encoding="utf-8") == "keep"


def test_rejects_absent_path_that_normalizes_to_existing_repo(tmp_path: Path) -> None:
    repo, script = _copy_check_script(tmp_path)
    bootstrap_python = tmp_path / "bootstrap-python"
    _write_bootstrap_python(bootstrap_python)
    sentinel = repo / "sentinel"
    sentinel.write_text("keep", encoding="utf-8")
    deceptive = repo / "missing-component" / ".."

    result = _run_check(repo, script, bootstrap_python, venv=deceptive)

    assert result.returncode == 1
    assert "Refusing to replace unsafe virtual environment target" in result.stderr
    assert sentinel.read_text(encoding="utf-8") == "keep"
    assert not (repo / "pyvenv.cfg").exists()
    assert not (repo / "bin").exists()


def test_discovers_suitable_interpreter_outside_minimal_path(tmp_path: Path) -> None:
    repo, script = _copy_check_script(tmp_path)
    home = tmp_path / "home"
    local_bin = home / ".local" / "bin"
    local_bin.mkdir(parents=True)
    too_old = local_bin / "python3.13"
    _write_discoverable_python(too_old, suitable=False)
    suitable = local_bin / "python3.12"
    _write_discoverable_python(suitable, suitable=True)

    result = _run_discovery_check(repo, script, home=home, path=_minimal_tool_path(tmp_path))

    assert result.returncode == 0, result.stderr
    assert f"Bootstrap interpreter: {suitable}" in result.stdout
    assert "Creating virtual environment" in result.stdout
    assert (repo / ".venv" / ".created-by-bootstrap").is_file()


def test_reports_rejected_interpreters_when_none_is_suitable(tmp_path: Path) -> None:
    if _system_dirs_have_python():
        pytest.skip("a system python location is populated; discovery could succeed")
    repo, script = _copy_check_script(tmp_path)
    home = tmp_path / "home"
    local_bin = home / ".local" / "bin"
    local_bin.mkdir(parents=True)
    too_old = local_bin / "python3"
    _write_discoverable_python(too_old, suitable=False)

    result = _run_discovery_check(repo, script, home=home, path=_minimal_tool_path(tmp_path))

    assert result.returncode == 1
    assert "Python 3.11 or newer (with the venv module) is required" in result.stderr
    assert str(too_old) in result.stderr
    assert "Set PYTHON_BIN=" in result.stderr
    assert not (repo / ".venv").exists()


def test_refuses_package_imported_from_another_tree(tmp_path: Path) -> None:
    repo, script = _copy_check_script(tmp_path)
    bootstrap_python = tmp_path / "bootstrap-python"
    _write_bootstrap_python(bootstrap_python)
    external = tmp_path / "usable-shared-venv"
    _create_usable_test_venv(external, source_root=repo / "src")
    other_src = tmp_path / "other-checkout" / "src"
    (other_src / "orca_auto").mkdir(parents=True)
    other_init = other_src / "orca_auto" / "__init__.py"
    other_init.write_text("", encoding="utf-8")

    result = _run_check(repo, script, bootstrap_python, venv=external, pythonpath=other_src)

    assert result.returncode == 1
    assert "orca_auto is not imported from this checkout" in result.stderr
    assert f"Imported: {other_init.resolve()}" in result.stderr
    assert f"Expected: {_expected_package(repo)}" in result.stderr
    assert "Unset PYTHONPATH" in result.stderr
    assert "[check] Ruff" not in result.stdout
    assert "[check] pytest" not in result.stdout


def test_refuses_venv_without_checkout_package(tmp_path: Path) -> None:
    repo, script = _copy_check_script(tmp_path)
    bootstrap_python = tmp_path / "bootstrap-python"
    _write_bootstrap_python(bootstrap_python)
    external = tmp_path / "usable-shared-venv"
    _create_usable_test_venv(external)

    result = _run_check(repo, script, bootstrap_python, venv=external)

    assert result.returncode == 1
    assert "Imported: (import failed)" in result.stderr
    assert "[check] pytest" not in result.stdout
