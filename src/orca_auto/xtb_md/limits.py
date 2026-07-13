from __future__ import annotations

from typing import Any

from orca_auto.core.geometry_limits import MAX_ADMISSION_ATOMS

from .manifest import XtbMdManifestLimits

# xTB 6.7.1 renders this value with an i6 field.  One million becomes
# asterisks, which would prevent the terminal validator from binding the log
# to the requested propagation count.
MAX_XTB_MD_STEPS = 999_999
MAX_XTB_MD_ATOM_STEPS = 100_000_000
MAX_XTB_MD_FRAMES = 100_000
MAX_XTB_MD_WALLTIME_SECONDS = 86_400
MAX_XTB_MD_OUTPUT_BYTES = 1_073_741_824
MAX_XTB_MD_OUTPUT_FILES = 10_000


def manifest_limits_for_config(cfg: Any) -> XtbMdManifestLimits:
    return XtbMdManifestLimits(
        max_atoms=MAX_ADMISSION_ATOMS,
        max_steps=MAX_XTB_MD_STEPS,
        max_atom_steps=MAX_XTB_MD_ATOM_STEPS,
        max_frames=MAX_XTB_MD_FRAMES,
        max_walltime_seconds=MAX_XTB_MD_WALLTIME_SECONDS,
        max_cores=int(cfg.resources.max_cores_per_task),
        max_memory_gb=int(cfg.resources.max_memory_gb_per_task),
    )


__all__ = [
    "MAX_XTB_MD_ATOM_STEPS",
    "MAX_XTB_MD_FRAMES",
    "MAX_XTB_MD_OUTPUT_BYTES",
    "MAX_XTB_MD_OUTPUT_FILES",
    "MAX_XTB_MD_STEPS",
    "MAX_XTB_MD_WALLTIME_SECONDS",
    "manifest_limits_for_config",
]
