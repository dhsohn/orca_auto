from __future__ import annotations

from decimal import Decimal

from .manifest import XtbMdManifest


def _canonical_real(value: float) -> str:
    normalized = Decimal(str(value)).normalize()
    rendered = format(normalized, "f")
    if "." in rendered:
        rendered = rendered.rstrip("0").rstrip(".")
    return rendered or "0"


def build_md_input(manifest: XtbMdManifest) -> str:
    """Build the only supported xcontrol input for a fresh standalone MD run."""

    lines = [
        "$samerand",
        "$md",
        f"  temp={_canonical_real(manifest.temperature_k)}",
        f"  time={_canonical_real(manifest.time_ps)}",
        f"  dump={_canonical_real(manifest.dump_fs)}",
        f"  step={_canonical_real(manifest.step_fs)}",
        "  velo=false",
        f"  nvt={'true' if manifest.ensemble == 'nvt' else 'false'}",
        "  restart=false",
        f"  hmass={manifest.hydrogen_mass_amu}",
        f"  shake={manifest.shake}",
        f"  sccacc={_canonical_real(manifest.scc_accuracy)}",
        "  forcewrrestart=true",
        "$end",
    ]
    return "\n".join(lines) + "\n"


__all__ = ["build_md_input"]
