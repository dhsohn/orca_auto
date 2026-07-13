from __future__ import annotations

from pathlib import Path

from .manifest import XtbMdManifest


def _absolute_path(value: str | Path, *, field: str) -> str:
    text = str(value)
    if not text or "\x00" in text:
        raise ValueError(f"xTB-MD {field} must be a non-empty path")
    path = Path(text).expanduser()
    if not path.is_absolute():
        raise ValueError(f"xTB-MD {field} must be an absolute path")
    return str(path)


def build_xtb_md_command(
    *,
    executable: str | Path,
    input_xyz: str | Path,
    md_input: str | Path,
    manifest: XtbMdManifest,
    max_cores: int,
) -> tuple[str, ...]:
    """Construct a shell-free fresh-run command; retry and MD restart are absent."""

    if isinstance(max_cores, bool) or not isinstance(max_cores, int) or max_cores < 1:
        raise ValueError("xTB-MD max_cores must be a positive integer")
    command = [
        _absolute_path(executable, field="executable"),
        _absolute_path(input_xyz, field="input_xyz"),
        "--input",
        _absolute_path(md_input, field="md_input"),
        "--md",
        "--gfn",
        str(manifest.gfn),
        "--chrg",
        str(manifest.charge),
        "--uhf",
        str(manifest.uhf),
        "--parallel",
        str(max_cores),
        "--norestart",
        "--strict",
    ]
    if manifest.solvent:
        command.extend([f"--{manifest.solvent_model}", manifest.solvent])
    return tuple(command)


__all__ = ["build_xtb_md_command"]
