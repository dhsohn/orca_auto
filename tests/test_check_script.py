from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import sys
import venv
from pathlib import Path

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
    return repo, script


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
  if [[ "${1:-}" == "-c" ]]; then
    printf '%s\n' "$0"
  fi
  exit 0
fi

exit 91
""",
        encoding="utf-8",
    )
    path.chmod(0o755)


def _run_check(
    repo: Path,
    script: Path,
    bootstrap_python: Path,
    *,
    venv: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env.pop("ORCA_AUTO_VENV", None)
    env.update(
        {
            "ORCA_AUTO_CHECK_SKIP_INSTALL": "1",
            "PYTHON_BIN": str(bootstrap_python),
        }
    )
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


def _create_usable_test_venv(path: Path) -> None:
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
    _create_usable_test_venv(external)
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
