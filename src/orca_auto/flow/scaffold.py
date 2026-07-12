from __future__ import annotations

import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from orca_auto.cli_errors import emit_error
from orca_auto.core.paths.workflow import validate_workflow_id_path_segment
from orca_auto.flow.templates import (
    CONFORMER_SCREENING_SHORTCUT,
    CONFORMER_SCREENING_TEMPLATE_ID,
    REACTION_TS_SEARCH_TEMPLATE_ID,
    SCAN_TS_SEARCH_TEMPLATE_ID,
    STANDARD_CONFORMER_INPUT_FILENAME,
    STANDARD_REACTION_PRODUCT_FILENAME,
    STANDARD_REACTION_REACTANT_FILENAME,
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


def _xyz(content_label: str, *, delta: float = 0.0) -> str:
    return "\n".join(
        [
            "3",
            content_label,
            "O 0.000000 0.000000 0.000000",
            f"H 0.000000 0.000000 {0.970000 + delta:.6f}",
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
    if workflow_type == REACTION_TS_SEARCH_TEMPLATE_ID:
        return "\n".join(
            [
                "# orca_auto workflow scaffold manifest",
                f"workflow_type: {REACTION_TS_SEARCH_TEMPLATE_ID}",
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
                "#   mdlen: 1.0     # MD length in ps (real; alias: len)",
                "#   wscal: 1.0     # ellipsoid wall-potential scaling (real)",
                "#   tstep: 5.0     # MD time step in fs (positive real; 0.001..2500)",
                "#   mddump: 100    # trajectory dump step (positive 32-bit integer)",
                "#   shake: 2       # SHAKE mode: 0, 1, or 2",
                "#   norotmd: true  # exact boolean key; skip regular MDs after MTD",
                "#   nocross: true  # disable default GC crossing (cross: true keeps default)",
                "priority: 10",
                "# Each selected reactant/product CREST conformer pair becomes an xTB path search.",
                "max_crest_candidates: 3",
                "# Optional: filter CREST endpoint pairs before xTB path search.",
                "# List moving reaction-center atoms to compare the remaining scaffold.",
                "# endpoint_pairing:",
                "#   enabled: true",
                "#   moving_atoms: [5, 8]",
                "#   max_distance_rmsd: 0.75",
                "#   max_pairs: 3",
                "resources:",
                "  max_cores: 8",
                "  max_memory_gb: 32",
                "orca:",
                '  route_line: "! r2scan-3c OptTS Freq TightSCF"',
                "  charge: 0",
                "  multiplicity: 1",
                "",
            ]
        )
    if workflow_type == CONFORMER_SCREENING_TEMPLATE_ID:
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
                "#   mdlen: 1.0     # MD length in ps (real; alias: len)",
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
                "resources:",
                "  max_cores: 8",
                "  max_memory_gb: 32",
                "orca:",
                '  route_line: "! r2scan-3c Opt TightSCF"',
                "  charge: 0",
                "  multiplicity: 1",
                "",
            ]
        )
    if workflow_type == SCAN_TS_SEARCH_TEMPLATE_ID:
        return "\n".join(
            [
                "# orca_auto workflow scaffold manifest",
                f"workflow_type: {SCAN_TS_SEARCH_TEMPLATE_ID}",
                "# Relaxed-scan coordinate (0-based atom indices): kind atoms = start, end, points",
                'scan_coordinate: "B 0 1 = 1.20, 3.00, 16"',
                "priority: 10",
                "# One OptTS+Freq child job per interior maximum of the scan profile,",
                "# ranked by prominence; endpoints never become candidates.",
                "max_orca_stages: 5",
                "# Interior maxima below this prominence are treated as noise.",
                "barrier_threshold_kcal: 0.5",
                "# When the profile is barrierless, extend the scan past its endpoint",
                "# this many times (max(6, 20% of range) extra points each) before failing.",
                "max_scan_extensions: 1",
                "resources:",
                "  max_cores: 8",
                "  max_memory_gb: 32",
                "orca:",
                '  route_line: "! Opt r2scan-3c TightSCF"',
                "  charge: 0",
                "  multiplicity: 1",
                "# Route for the chained OptTS candidates (Freq keeps TS verification).",
                'orca_optts_route_line: "! OptTS Freq r2scan-3c TightSCF"',
                "",
            ]
        )
    raise ValueError(f"Unsupported workflow scaffold type: {workflow_type}")


def _readme(root: Path, workflow_type: str) -> str:
    if workflow_type == REACTION_TS_SEARCH_TEMPLATE_ID:
        lines = [
            f"- Replace `{STANDARD_REACTION_REACTANT_FILENAME}` and `{STANDARD_REACTION_PRODUCT_FILENAME}` with your precomplex inputs.",
            "- Adjust `flow.yaml` before materializing the workflow.",
            "- Change `crest_mode: standard` to `crest_mode: nci` when you want NCI-mode CREST stages.",
            "- Put CREST overrides under `crest:` in `flow.yaml`, for example "
            "`gfn: ff`, `noreftopo: true`, `notopo: true`, or `nocbonds: true` "
            "when topology filtering is too strict. Sampling knobs `mdlen`/`len`, "
            "`wscal`, `tstep`, `mddump`, `shake`, `norotmd`, and `cross`/`nocross` "
            "are also accepted with finite/native-safe values and exact boolean keys "
            "(a bad value fails the job).",
            "- Use `endpoint_pairing:` when multiple CREST conformers create bad reactant/product pairings before xTB.",
            f"- {REACTION_TS_SEARCH_TEMPLATE_ID} expands all selected reactant x product CREST pairs into xTB path searches, waits for the xTB phase to finish, and then batches matching ORCA OptTS child jobs from retained ts_guess artifacts.",
        ]
    elif workflow_type == CONFORMER_SCREENING_TEMPLATE_ID:
        lines = [
            f"- Replace `{STANDARD_CONFORMER_INPUT_FILENAME}` with the molecule you want to screen.",
            "- Adjust `flow.yaml` before materializing the workflow.",
            "- Change `crest_mode: standard` to `crest_mode: nci` when you want NCI-mode CREST stages.",
            "- Put CREST overrides under `crest:` in `flow.yaml`, for example "
            "`gfn: ff`, `noreftopo: true`, `notopo: true`, or `nocbonds: true` "
            "when topology filtering is too strict. Sampling knobs `mdlen`/`len`, "
            "`wscal`, `tstep`, `mddump`, `shake`, `norotmd`, and `cross`/`nocross` "
            "are also accepted with finite/native-safe values and exact boolean keys "
            "(a bad value fails the job).",
            f"- {CONFORMER_SCREENING_SHORTCUT} hands off up to 20 retained CREST conformers to ORCA child jobs by default.",
            "- Add `Freq` to the ORCA `route_line` for SI Boltzmann populations. The complete "
            "terminal ensemble must contain only converged minima with complete 3N spectra, "
            "Nimag=0, and finite E/G/T; the durable `boltzmann_temperature_k` pin must be finite, "
            "positive, and within 0.01 K of every parsed temperature.",
        ]
    elif workflow_type == SCAN_TS_SEARCH_TEMPLATE_ID:
        lines = [
            f"- Replace `{STANDARD_CONFORMER_INPUT_FILENAME}` with your starting geometry.",
            "- Set `scan_coordinate` in `flow.yaml` (0-based atom indices, ORCA scan syntax).",
            f"- {SCAN_TS_SEARCH_TEMPLATE_ID} runs an ORCA relaxed scan, then chains one "
            "OptTS+Freq child job per interior maximum of the profile (ranked by "
            "prominence); the workflow report ranks the verified candidates.",
        ]
    else:
        raise ValueError(f"Unsupported workflow scaffold type: {workflow_type}")

    shortcut_name = _shortcut_name(workflow_type)
    root_text = shlex.quote(str(root))
    return "\n".join(
        [
            "# orca_auto workflow scaffold",
            "",
            f"This directory was created for `orca_auto scaffold {shortcut_name} {root_text}`.",
            "",
            *lines,
            "- Then materialize it with `orca_auto run-dir <path>`.",
            "",
        ]
    )


def _scaffold_targets(root: Path, workflow_type: str, crest_mode: str) -> list[ScaffoldTarget]:
    common = [
        ScaffoldTarget(root / "flow.yaml", _manifest(workflow_type, crest_mode), "flow.yaml"),
        ScaffoldTarget(root / "README.md", _readme(root, workflow_type), "README.md"),
    ]
    if workflow_type == REACTION_TS_SEARCH_TEMPLATE_ID:
        return [
            ScaffoldTarget(
                root / STANDARD_REACTION_REACTANT_FILENAME,
                _xyz("orca_auto workflow scaffold reactant"),
                STANDARD_REACTION_REACTANT_FILENAME,
            ),
            ScaffoldTarget(
                root / STANDARD_REACTION_PRODUCT_FILENAME,
                _xyz("orca_auto workflow scaffold product", delta=0.05),
                STANDARD_REACTION_PRODUCT_FILENAME,
            ),
            *common,
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
