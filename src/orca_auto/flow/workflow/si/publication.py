"""Ownership-safe publication of the canonical workflow SI artifact set."""

from __future__ import annotations

import hashlib
import logging
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from orca_auto.core.artifacts import (
    INTERACTION_ENERGY_CSV_FILE,
    INTERACTION_ENERGY_CSV_OWNER_FILE,
    WORKFLOW_SI_CSV_FILE,
    WORKFLOW_SI_MD_FILE,
)
from orca_auto.core.utils.persistence import atomic_write_text

from ...manifest import (
    normalize_interaction_energy_block,
    normalize_rmsd_dedup_block,
    optional_positive_float,
    require_int,
    validate_conformer_postprocessing_template,
    validate_interaction_energy_state_balance,
)
from ..report import _text
from .collection import collect_workflow_si_data
from .evidence import _request_parameters
from .rendering import (
    render_interaction_energy_csv,
    render_workflow_si_csv,
    render_workflow_si_md,
)

logger = logging.getLogger(__name__)


def _boltzmann_temperature_override(
    payload: Mapping[str, Any],
) -> tuple[float | None, str]:
    """Read the admission-validated override from durable workflow state only."""
    parameters = _request_parameters(payload)
    if "boltzmann_temperature_k" not in parameters:
        return None, ""
    try:
        return optional_positive_float(parameters, "boltzmann_temperature_k"), ""
    except ValueError:
        logger.warning("Invalid durable boltzmann_temperature_k", exc_info=True)
        return None, "(populations omitted: durable boltzmann_temperature_k is invalid)"


def _remove_si_artifacts(*paths: Path, raise_on_error: bool = False) -> None:
    first_error: OSError | None = None
    for path in paths:
        try:
            path.unlink(missing_ok=True)
        except OSError as exc:
            first_error = first_error or exc
            logger.warning("Failed to remove inconsistent SI artifact %s", path, exc_info=True)
    if raise_on_error and first_error is not None:
        raise first_error


def _interaction_content_digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _interaction_owner_identity(workflow_id: str) -> str:
    return hashlib.sha256(workflow_id.encode("utf-8")).hexdigest()


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _interaction_owner_text(
    workflow_id: str,
    digest: str,
    *,
    pending_digest: str = "-",
) -> str:
    return (
        "orca_auto interaction_energy.csv owner v2\n"
        f"workflow-sha256:{_interaction_owner_identity(workflow_id)}\n"
        f"sha256:{digest}\npending-sha256:{pending_digest}\n"
    )


def _read_interaction_owner(owner_path: Path) -> tuple[str, str, str] | None:
    if not owner_path.is_file() or owner_path.is_symlink():
        return None
    try:
        lines = owner_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    if len(lines) != 4 or lines[0] != "orca_auto interaction_energy.csv owner v2":
        return None
    if not lines[2].startswith("sha256:") or not lines[3].startswith("pending-sha256:"):
        return None
    digest = lines[2][7:]
    pending_digest = lines[3][15:]
    if any(
        value != "-" and (len(value) != 64 or re.fullmatch(r"[0-9a-f]{64}", value) is None)
        for value in (digest, pending_digest)
    ):
        return None
    if not lines[1].startswith("workflow-sha256:"):
        return None
    identity = lines[1][16:]
    if len(identity) != 64 or re.fullmatch(r"[0-9a-f]{64}", identity) is None:
        return None
    return identity, digest, pending_digest


def _owned_interaction_artifact(
    interaction_path: Path,
    owner_path: Path,
    *,
    workflow_id: str,
) -> bool:
    if not interaction_path.is_file() or interaction_path.is_symlink():
        return False
    owner = _read_interaction_owner(owner_path)
    if owner is None or owner[0] != _interaction_owner_identity(workflow_id):
        return False
    try:
        digest = _file_sha256(interaction_path)
    except OSError:
        return False
    return digest in {owner[1], owner[2]} - {"-"}


def _write_owned_interaction_artifact(
    interaction_path: Path,
    owner_path: Path,
    *,
    workflow_id: str,
    text: str,
) -> None:
    desired_digest = _interaction_content_digest(text)
    current_digest = "-"
    if interaction_path.exists() or interaction_path.is_symlink():
        if not _owned_interaction_artifact(interaction_path, owner_path, workflow_id=workflow_id):
            raise FileExistsError(
                f"refusing to overwrite unowned or modified interaction-energy artifact: "
                f"{interaction_path}"
            )
        current_digest = _file_sha256(interaction_path)
    elif owner_path.exists() or owner_path.is_symlink():
        owner = _read_interaction_owner(owner_path)
        if owner is None or owner[0] != _interaction_owner_identity(workflow_id):
            raise FileExistsError(
                f"interaction-energy artifact owner marker is not owned by {workflow_id}"
            )
    atomic_write_text(
        owner_path,
        _interaction_owner_text(
            workflow_id,
            current_digest,
            pending_digest=desired_digest,
        ),
    )
    atomic_write_text(interaction_path, text)
    atomic_write_text(
        owner_path,
        _interaction_owner_text(workflow_id, desired_digest),
    )


def _preflight_interaction_artifact_write(
    interaction_path: Path,
    owner_path: Path,
    *,
    workflow_id: str,
) -> None:
    """Reject an unowned/conflicting target before base SI files are touched."""
    if interaction_path.exists() or interaction_path.is_symlink():
        if not _owned_interaction_artifact(interaction_path, owner_path, workflow_id=workflow_id):
            owner = _read_interaction_owner(owner_path)
            if owner is not None and owner[0] == _interaction_owner_identity(workflow_id):
                # The data was edited/replaced after publication. Preserve it,
                # but release this workflow's stale authority before blocking.
                owner_path.unlink(missing_ok=True)
            raise FileExistsError(
                f"refusing to overwrite unowned or modified interaction-energy artifact: "
                f"{interaction_path}"
            )
        return
    if owner_path.exists() or owner_path.is_symlink():
        owner = _read_interaction_owner(owner_path)
        if owner is None or owner[0] != _interaction_owner_identity(workflow_id):
            raise FileExistsError(
                f"interaction-energy artifact owner marker is not owned by {workflow_id}"
            )


def _remove_owned_interaction_artifact(
    interaction_path: Path,
    owner_path: Path,
    *,
    workflow_id: str,
) -> None:
    owner = _read_interaction_owner(owner_path)
    if owner is None or owner[0] != _interaction_owner_identity(workflow_id):
        return
    if not interaction_path.exists():
        owner_path.unlink(missing_ok=True)
        return
    if interaction_path.is_symlink() or not _owned_interaction_artifact(
        interaction_path, owner_path, workflow_id=workflow_id
    ):
        # Preserve a user-replaced or edited file, and release the stale marker
        # so it can never authorize an overwrite.
        owner_path.unlink(missing_ok=True)
        return
    interaction_path.unlink()
    owner_path.unlink(missing_ok=True)


def write_workflow_si(
    workspace_dir: Path,
    payload: Mapping[str, Any],
    *,
    raise_on_error: bool = False,
) -> Path | None:
    """Write ``workflow_si.md`` + ``si_data.csv`` (+ ``interaction_energy.csv``).

    A workflow without ORCA stages has no SI: stale files from an earlier
    template are removed so nothing obsolete can be pasted into a paper. The
    interaction-energy CSV is written only when ΔE_int data exists and is removed
    otherwise so a disabled feature never leaves a stale table behind.
    Errors are logged and suppressed by default; ``raise_on_error=True`` exposes
    them to the durable publication retry state machine.
    """
    md_path = workspace_dir / WORKFLOW_SI_MD_FILE
    csv_path = workspace_dir / WORKFLOW_SI_CSV_FILE
    interaction_path = workspace_dir / INTERACTION_ENERGY_CSV_FILE
    interaction_owner_path = workspace_dir / INTERACTION_ENERGY_CSV_OWNER_FILE
    workflow_id = _text(payload.get("workflow_id"))
    try:
        # Durable corruption is not equivalent to an explicit feature disable.
        # Validate before touching the last known-good publication.
        parameters = _request_parameters(payload)
        normalized_interaction = normalize_interaction_energy_block(
            parameters.get("interaction_energy")
        )
        # Evaluate the strict charge/multiplicity reads only when the feature is
        # configured: corrupt request parameters must fail the interaction
        # feature closed, not block the whole SI of an unrelated workflow.
        if normalized_interaction is not None:
            validate_interaction_energy_state_balance(
                normalized_interaction,
                complex_charge=require_int(parameters.get("charge", 0), field="charge"),
                complex_multiplicity=require_int(
                    parameters.get("multiplicity", 1), field="multiplicity", minimum=1
                ),
            )
        normalized_rmsd = normalize_rmsd_dedup_block(parameters.get("rmsd_dedup"))
        validate_conformer_postprocessing_template(
            payload.get("template_name"),
            interaction_energy=normalized_interaction,
            rmsd_dedup=normalized_rmsd,
        )
        override, population_blocker = _boltzmann_temperature_override(payload)
        data = collect_workflow_si_data(
            payload,
            boltzmann_temperature_k=override,
            population_blocker=population_blocker,
            raise_feature_errors=True,
        )
        if not data.has_orca_stages():
            _remove_si_artifacts(md_path, csv_path, raise_on_error=raise_on_error)
            _remove_owned_interaction_artifact(
                interaction_path,
                interaction_owner_path,
                workflow_id=workflow_id,
            )
            return None
        # Render every document before writing any, so a rendering failure leaves
        # the previous consistent set on disk instead of a fresh md next to a
        # stale csv.
        md_text = render_workflow_si_md(data)
        csv_text = render_workflow_si_csv(data)
        interaction_text = render_interaction_energy_csv(data)
        if interaction_text is not None:
            # A deterministic ownership conflict must not replace/delete the
            # already published base SI before the durable state is blocked.
            _preflight_interaction_artifact_write(
                interaction_path,
                interaction_owner_path,
                workflow_id=workflow_id,
            )
        try:
            atomic_write_text(md_path, md_text)
            atomic_write_text(csv_path, csv_text)
            if interaction_text is not None:
                _write_owned_interaction_artifact(
                    interaction_path,
                    interaction_owner_path,
                    workflow_id=workflow_id,
                    text=interaction_text,
                )
            else:
                _remove_owned_interaction_artifact(
                    interaction_path,
                    interaction_owner_path,
                    workflow_id=workflow_id,
                )
        except Exception:
            # A caught mid-write failure must never leave an inconsistent mix of
            # fresh and stale SI artifacts. All of them are reproducible.
            _remove_si_artifacts(md_path, csv_path)
            _remove_owned_interaction_artifact(
                interaction_path,
                interaction_owner_path,
                workflow_id=workflow_id,
            )
            raise
        return md_path
    except Exception:  # noqa: BLE001
        logger.warning("Workflow SI generation failed for %s", workspace_dir, exc_info=True)
        if raise_on_error:
            raise
        return None
