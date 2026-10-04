"""Write a generation's HTML report, SI block and ``machine.json``.

``machine_observation.build_machine_observation`` assembles ``machine.json``;
this module writes it into the verified generation once and refuses to change a
terminal observation or the artifacts it pins.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from orca_auto.core.artifacts import (
    EXECUTION_PROVENANCE_FILE,
    MAX_RUN_ARTIFACT_JSON_BYTES,
    RUN_REPORT_HTML_FILE,
    SI_BLOCK_MD_FILE,
)
from orca_auto.core.confined_io import atomic_write_confined_bytes, read_confined_text
from orca_auto.core.utils import copy_dict_or_empty as _dict

from .. import state_reading as _state_reading
from ..machine_observation import (
    REPORT_GENERATION_FAILED,
    REPORT_NOT_APPLICABLE,
    REPORT_PRODUCED,
    build_machine_observation,
    machine_json_bytes,
    machine_lifecycle,
    record_report_generation,
    report_json_path,
)
from ..state import normalized_payload_from_state, retired_generation, write_generation_bytes
from .composer import compose_job_report_html
from .si import write_si_block

logger = logging.getLogger(__name__)


def write_job_html_report(
    reaction_dir: Path,
    state: Mapping[str, Any],
    *,
    generation_target: tuple[Path, tuple[int, int]],
    report_generation: dict[str, str] | None = None,
) -> Path | None:
    """Write ``job_report.html``; ``None`` when the job type has no HTML report or it failed.

    The report lands inside the verified execution generation. When the current
    job type has no HTML report, a stale ``job_report.html`` in that generation
    is removed so links cannot surface an obsolete report. The exception path deliberately does NOT remove it: a
    transient parse error must not destroy the last valid report.
    ``report_generation``, when given, receives the report's fixed outcome
    (``machine_observation.record_report_generation``), never the error text.
    """
    path = generation_target[0] / RUN_REPORT_HTML_FILE
    try:
        rendered = compose_job_report_html(reaction_dir, state)
        if rendered is None:
            path.unlink(missing_ok=True)
            record_report_generation(report_generation, "human-report", REPORT_NOT_APPLICABLE)
            return None
        atomic_write_confined_bytes(
            generation_target[0],
            path,
            rendered.encode("utf-8"),
            label="ORCA generation artifact",
            mode=0o600,
            expected_parent_identity=generation_target[1],
        )
        record_report_generation(report_generation, "human-report", REPORT_PRODUCED)
        return path
    except Exception:  # noqa: BLE001
        logger.warning("Job HTML report generation failed for %s", reaction_dir, exc_info=True)
        record_report_generation(report_generation, "human-report", REPORT_GENERATION_FAILED)
        return None


def _published_terminal_observation(
    generation_dir: Path,
) -> tuple[str, dict[str, Any]] | None:
    """Existing finished observation text and parsed document, if one is published.

    A missing or nonterminal observation returns ``None``. Any existing path
    that cannot be read as bounded UTF-8 JSON fails closed before artifact
    writers can change files pinned by a terminal observation.
    """
    published = _published_observation(generation_dir)
    if published is None or _dict(published[1].get("lifecycle")).get("phase") != "finished":
        return None
    return published


def _published_observation(generation_dir: Path) -> tuple[str, dict[str, Any]] | None:
    """The published ``machine.json`` text and document of any phase; ``None`` when absent.

    An existing path that cannot be read as bounded UTF-8 JSON fails closed.
    """
    path = report_json_path(generation_dir)
    try:
        path.lstat()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise RuntimeError(f"existing machine observation is invalid: {path}") from exc
    try:
        existing_text = read_confined_text(
            generation_dir,
            path,
            label="ORCA generation machine observation",
            max_bytes=MAX_RUN_ARTIFACT_JSON_BYTES,
        )
        existing = json.loads(existing_text)
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        raise RuntimeError(f"existing machine observation is invalid: {path}") from exc
    if not isinstance(existing, dict):
        return None
    return existing_text, existing


def write_report_json(
    reaction_dir: Path,
    report_payload: dict[str, Any],
    *,
    generation_target: tuple[Path, tuple[int, int]] | None = None,
    html_path: Path | None = None,
    si_path: Path | None = None,
    report_generation: dict[str, str] | None = None,
) -> Path | None:
    """Write ``machine.json`` (and ``execution_provenance.json``) into the verified generation.

    ``html_path`` and ``si_path`` are the reports already published there and
    ``report_generation`` their outcomes in this publication. A terminal
    observation is written once; writing different bytes over it raises.
    """
    payload = report_payload
    if generation_target is None:
        generation_target = _state_reading.verified_generation_artifact_target(
            reaction_dir, payload
        )
    if generation_target is None:
        logger.warning(
            "report JSON not published: no verified execution generation for %s", reaction_dir
        )
        return None
    existing_terminal = _published_terminal_observation(generation_target[0])
    provenance = _dict(_dict(payload.get("engine_payload")).get("execution_provenance"))
    if provenance.get("source_inputs"):
        provenance_path = generation_target[0] / EXECUTION_PROVENANCE_FILE
        provenance_bytes = machine_json_bytes(provenance)
        if existing_terminal is None:
            write_generation_bytes(generation_target, provenance_path, provenance_bytes)
        elif (
            read_confined_text(
                generation_target[0],
                provenance_path,
                label="ORCA execution provenance",
                max_bytes=MAX_RUN_ARTIFACT_JSON_BYTES,
            ).encode("utf-8")
            != provenance_bytes
        ):
            raise RuntimeError(f"terminal execution provenance is immutable: {provenance_path}")
    observation = build_machine_observation(
        generation_target[0],
        payload,
        html_path=html_path,
        si_path=si_path,
        report_generation=report_generation,
    )
    path = report_json_path(generation_target[0])
    observation_bytes = machine_json_bytes(observation)
    if existing_terminal is not None:
        existing_text, _ = existing_terminal
        if existing_text.encode("utf-8") == observation_bytes:
            return path
        raise RuntimeError(f"terminal machine observation is immutable: {path}")
    write_generation_bytes(generation_target, path, observation_bytes)
    return path


def _existing_terminal_report_paths(
    generation_dir: Path,
) -> tuple[dict[str, str], str] | None:
    """Published report paths and recorded outcome of a terminal ``machine.json``.

    ``None`` means no terminal observation is published yet and reports may be
    written. An unreadable or corrupt existing observation fails closed.
    """
    published_terminal = _published_terminal_observation(generation_dir)
    if published_terminal is None:
        return None
    _, existing = published_terminal
    path = report_json_path(generation_dir)
    reports = {"report_json": str(path), **_receipted_report_paths(generation_dir, existing)}
    lifecycle = _dict(existing.get("lifecycle"))
    return reports, _state_reading.normalized_text(lifecycle.get("outcome"))


def _receipted_report_paths(generation_dir: Path, observation: Mapping[str, Any]) -> dict[str, str]:
    """HTML/SI paths the published ``observation`` receipts as available.

    A report file is listed only when the observation receipted it as
    available: a stale file left by a failed report, a file whose receipt is
    invalid, and a file an earlier observation never receipted are not
    published reports.
    """
    artifacts = _dict(observation.get("artifacts"))
    reports: dict[str, str] = {}
    for key, artifact_id, filename in (
        ("report_html", "human-report", RUN_REPORT_HTML_FILE),
        ("si_block", "supporting-information", SI_BLOCK_MD_FILE),
    ):
        report_path = generation_dir / filename
        receipted = _dict(artifacts.get(artifact_id)).get("status") == "available"
        if receipted and report_path.is_file():
            reports[key] = str(report_path)
    return reports


def write_report_files(reaction_dir: Path, state: Mapping[str, Any]) -> dict[str, str]:
    """Write the machine (JSON) and human (HTML, SI block) job reports.

    Reports are published only inside the verified execution generation. A run
    whose generation cannot be verified gets no report (fail closed, logged);
    its state and queue record still carry the outcome.
    """

    report_payload = normalized_payload_from_state(reaction_dir, state)
    generation_target = _state_reading.verified_generation_artifact_target(
        reaction_dir, report_payload
    )
    if generation_target is None:
        logger.warning(
            "job reports not published: no verified execution generation for %s", reaction_dir
        )
        return {}
    existing_terminal = _existing_terminal_report_paths(generation_target[0])
    if existing_terminal is not None:
        # The published terminal machine.json pins its artifacts' exact bytes
        # and is immutable; re-entry must not regenerate or remove any of them.
        existing_reports, existing_outcome = existing_terminal
        current_status = _state_reading.normalized_text(
            _dict(report_payload.get("status")).get("state")
        )
        if machine_lifecycle(current_status)[1] != existing_outcome:
            logger.warning(
                "terminal machine observation outcome %r no longer matches job state %r "
                "for %s; the immutable generation is preserved unchanged",
                existing_outcome,
                current_status,
                generation_target[0],
            )
        return existing_reports
    if retired_generation(generation_target[0]):
        return {}
    # This publication's optional report outcomes; never persisted elsewhere.
    report_generation: dict[str, str] = {}
    html_path = write_job_html_report(
        reaction_dir,
        state,
        generation_target=generation_target,
        report_generation=report_generation,
    )
    si_path = write_si_block(
        reaction_dir,
        state,
        generation_target=generation_target,
        report_generation=report_generation,
    )
    json_path = write_report_json(
        reaction_dir,
        report_payload,
        generation_target=generation_target,
        html_path=html_path,
        si_path=si_path,
        report_generation=report_generation,
    )
    # List the reports as the observation just published receipts them, so
    # this result and any re-entry name the same files.
    published = _published_observation(generation_target[0])
    reports = _receipted_report_paths(
        generation_target[0], published[1] if published is not None else {}
    )
    reports["report_json"] = str(json_path)
    return reports
