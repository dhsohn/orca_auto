from __future__ import annotations

import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from orca_auto.core.paths.workflow import validate_workflow_id_path_segment
from orca_auto.core.terminal import emit_error
from orca_auto.flow.templates import (
    CONFORMER_SCREENING_SHORTCUT,
    CONFORMER_SCREENING_TEMPLATE_ID,
    DEFAULT_CONFORMER_ORCA_ROUTE_LINE,
    STANDARD_CONFORMER_INPUT_FILENAME,
    WORKFLOW_TEMPLATE_IDS,
    workflow_template_shortcut,
)

_CREST_MODE_ALIASES = {
    "std": "standard",
    "standard": "standard",
    "nci": "nci",
}


@dataclass(frozen=True)
class ScaffoldTarget:
    path: Path
    content: str
    label: str


@dataclass(frozen=True)
class ScaffoldWriteSummary:
    root: Path
    workflow_type: str
    crest_mode: str
    created: list[str]
    skipped: list[str]


def _write_if_missing(path: Path, content: str) -> bool:
    if path.exists():
        return False
    path.write_text(content, encoding="utf-8")
    return True


def _xyz(content_label: str) -> str:
    return "\n".join(
        [
            "3",
            content_label,
            "O 0.000000 0.000000 0.000000",
            "H 0.000000 0.000000 0.970000",
            "H 0.000000 0.750000 -0.240000",
            "",
        ]
    )


def _normalize_crest_mode(value: object) -> str:
    text = str(value or "").strip().lower()
    return _CREST_MODE_ALIASES.get(text, "")


def _shortcut_name(workflow_type: str) -> str:
    return workflow_template_shortcut(workflow_type)


def _manifest(workflow_type: str, crest_mode: str) -> str:
    if workflow_type != CONFORMER_SCREENING_TEMPLATE_ID:
        raise ValueError(f"Unsupported workflow scaffold type: {workflow_type}")
    return "\n".join(
        [
            "# orca_auto workflow scaffold manifest",
            f"workflow_type: {CONFORMER_SCREENING_TEMPLATE_ID}",
            "# Change to `nci` when you want NCI-mode CREST stages.",
            f"crest_mode: {crest_mode}",
            "# Optional CREST job overrides; uncomment when GFN2 pre-opt changes topology.",
            "# crest:",
            "#   gfn: ff",
            "#   no_preopt: true",
            "#   noreftopo: true",
            "#   notopo: true",
            "#   nocbonds: true",
            "# Optional CREST sampling knobs (native-safe; a bad value fails the job):",
            "#   mdlen: 1.0     # MD length in ps (real)",
            "#   wscal: 1.0     # ellipsoid wall-potential scaling (real)",
            "#   tstep: 5.0     # MD time step in fs (positive real; 0.001..2500)",
            "#   mddump: 100    # trajectory dump step (positive 32-bit integer)",
            "#   shake: 2       # SHAKE mode: 0, 1, or 2",
            "#   norotmd: true  # exact boolean key; skip regular MDs after MTD",
            "#   nocross: true  # disable default GC crossing (cross: true keeps default)",
            "priority: 10",
            "# Up to 20 retained CREST conformers are handed off to ORCA by default.",
            "max_orca_stages: 20",
            "# SI populations require a complete terminal ensemble of converged minima",
            "# with complete 3N spectra, Nimag=0, and finite E/G/T: add `Freq` below.",
            "# The optional finite, positive temperature pin is stored at admission",
            "# and must match every parsed Freq temperature within 0.01 K.",
            "# boltzmann_temperature_k: 298.15",
            "# Optional post-DFT RMSD re-dedup of the optimized minima (all atoms by default).",
            "# Merging needs convergence, no known imaginary mode, exact provenance,",
            "# low proper RMSD/max displacement, and a small energy gap. Detected global",
            "# reflections stay distinct, but this is still a heuristic: inspect",
            "# merged groups before treating them as chemically identical.",
            "# rmsd_dedup:",
            "#   enabled: true",
            "#   rmsd_threshold_angstrom: 0.25",
            "#   energy_window_kcal: 0.1",
            "#   heavy_atoms_only: false",
            "# Optional interaction energy dE_int = E(complex) - sum E(fragment).",
            "# Two to eight fragments must partition every atom (0-based indices),",
            "# conserve charge, have electron-count-compatible multiplicities, and",
            "# spin-couple to the complex multiplicity. The complex",
            "# and each fragment run a fresh pure single point at sp_route_line on the",
            "# optimized geometry. No separate ghost-atom counterpoise is performed;",
            "# method-inherent corrections such as gCP may remain.",
            "# interaction_energy:",
            "#   enabled: true",
            '#   sp_route_line: "! r2scan-3c TightSCF"',
            "#   max_fragments: 2",
            "#   fragments:",
            "#     - label: host",
            "#       atom_indices: [0, 1, 2, 3]",
            "#       charge: 0",
            "#       multiplicity: 1",
            "#     - label: guest",
            "#       atom_indices: [4, 5, 6]",
            "#       charge: 0",
            "#       multiplicity: 1",
            "resources:",
            "  max_cores: 8",
            "  max_memory_gb: 32",
            "orca:",
            f'  route_line: "{DEFAULT_CONFORMER_ORCA_ROUTE_LINE}"',
            "  charge: 0",
            "  multiplicity: 1",
            "",
        ]
    )


def _readme(root: Path, workflow_type: str) -> str:
    if workflow_type != CONFORMER_SCREENING_TEMPLATE_ID:
        raise ValueError(f"Unsupported workflow scaffold type: {workflow_type}")
    lines = [
        f"- Replace `{STANDARD_CONFORMER_INPUT_FILENAME}` with the molecule you want to screen.",
        "- Adjust `flow.yaml` before materializing the workflow.",
        "- Change `crest_mode: standard` to `crest_mode: nci` when you want NCI-mode CREST stages.",
        "- Put CREST overrides under `crest:` in `flow.yaml`, for example "
        "`gfn: ff`, `noreftopo: true`, `notopo: true`, or `nocbonds: true` "
        "when topology filtering is too strict. Sampling knobs `mdlen`, "
        "`wscal`, `tstep`, `mddump`, `shake`, `norotmd`, and `cross`/`nocross` "
        "are also accepted with finite/native-safe values and exact boolean keys "
        "(a bad value fails the job).",
        f"- {CONFORMER_SCREENING_SHORTCUT} hands off up to 20 retained CREST conformers to ORCA child jobs by default.",
        "- Add `Freq` to the ORCA `route_line` for SI Boltzmann populations. The complete "
        "terminal ensemble must contain only converged minima with complete 3N spectra, "
        "Nimag=0, and finite E/G/T; the durable `boltzmann_temperature_k` pin must be finite, "
        "positive, and within 0.01 K of every parsed temperature.",
        "- Enable `rmsd_dedup:` to collapse DFT-degenerate minima to one representative "
        "(duplicate count noted in `workflow_si.md`); merging needs convergence, no known "
        "imaginary mode, exact provenance, low proper RMSD/max displacement, and a small "
        "energy gap. Detected global reflections stay distinct, but this is still a "
        "heuristic; heavy-atom mode increases the risk of an over-merge.",
        "- Enable `interaction_energy:` for dE_int = E(complex) - sum E(fragment). Fragments "
        "must number 2-8, partition every atom, conserve charge, have electron-count-compatible "
        "multiplicities, and spin-couple to the "
        "complex. The complex and fragments are fresh pure single points at `sp_route_line`; "
        "only RMSD representatives are computed. No separate ghost-atom counterpoise is run "
        "(method-inherent corrections such as gCP may remain). "
        "Results land in the SI interaction-energy section of `workflow_si.md`.",
    ]

    shortcut_name = _shortcut_name(workflow_type)
    root_text = shlex.quote(str(root))
    return "\n".join(
        [
            "# orca_auto workflow scaffold",
            "",
            f"This directory was created for `orca_auto scaffold {shortcut_name} {root_text}`.",
            "",
            *lines,
            "- Then materialize it with `orca_auto run-dir <path>`. Each run creates a "
            "timestamped generation directory in this folder (like standalone ORCA jobs) "
            "holding that run's workflow state and results.",
            "",
        ]
    )


def _scaffold_targets(root: Path, workflow_type: str, crest_mode: str) -> list[ScaffoldTarget]:
    common = [
        ScaffoldTarget(root / "flow.yaml", _manifest(workflow_type, crest_mode), "flow.yaml"),
        ScaffoldTarget(root / "README.md", _readme(root, workflow_type), "README.md"),
    ]
    return [
        ScaffoldTarget(
            root / STANDARD_CONFORMER_INPUT_FILENAME,
            _xyz("orca_auto workflow scaffold input"),
            STANDARD_CONFORMER_INPUT_FILENAME,
        ),
        *common,
    ]


def _write_scaffold_targets(
    *,
    root: Path,
    workflow_type: str,
    crest_mode: str,
) -> ScaffoldWriteSummary:
    created: list[str] = []
    skipped: list[str] = []
    for target in _scaffold_targets(root, workflow_type, crest_mode):
        if _write_if_missing(target.path, target.content):
            created.append(target.label)
        else:
            skipped.append(target.label)
    return ScaffoldWriteSummary(
        root=root,
        workflow_type=workflow_type,
        crest_mode=crest_mode,
        created=created,
        skipped=skipped,
    )


def _emit_scaffold_summary(summary: ScaffoldWriteSummary) -> None:
    print(f"workflow_dir: {summary.root}")
    print(f"workflow_type: {summary.workflow_type}")
    print(f"crest_mode: {summary.crest_mode}")
    print(f"created: {len(summary.created)}")
    print(f"skipped: {len(summary.skipped)}")
    for name in summary.created:
        print(f"created_file: {name}")
    for name in summary.skipped:
        print(f"skipped_file: {name}")


def cmd_scaffold(args: Any) -> int:
    raw_root = str(getattr(args, "root", "")).strip()
    if not raw_root:
        emit_error("scaffold requires --root")
        return 1

    workflow_type = str(getattr(args, "workflow_type", "")).strip().lower()
    if workflow_type not in WORKFLOW_TEMPLATE_IDS:
        emit_error(f"unsupported workflow scaffold type: {workflow_type}")
        return 1

    crest_mode = _normalize_crest_mode(getattr(args, "crest_mode", "standard"))
    if not crest_mode:
        emit_error(f"unsupported crest_mode: {getattr(args, 'crest_mode', '')}")
        return 1

    root = Path(raw_root).expanduser().resolve()
    try:
        validate_workflow_id_path_segment(root.name)
    except ValueError as exc:
        emit_error(exc)
        return 1
    if root.exists() and not root.is_dir():
        emit_error(f"scaffold root is not a directory: {root}")
        return 1
    root.mkdir(parents=True, exist_ok=True)

    _emit_scaffold_summary(
        _write_scaffold_targets(
            root=root,
            workflow_type=workflow_type,
            crest_mode=crest_mode,
        )
    )
    return 0
