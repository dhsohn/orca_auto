"""Write a generation's HTML report, SI block and ``machine.json``.

One publication collects its evidence once (:class:`PublicationEvidence`: the
job state in both forms, the verified generation and the route facts of the
selected input). The HTML and SI renderers read that evidence and return text;
this module writes every file, removes a stale optional report and turns each
optional report into an :class:`~..machine_observation.ArtifactResult`
(produced, not applicable or failed). ``machine.json`` is built last from the
evidence and those explicit results
(``machine_observation.build_machine_observation``), written into the
verified generation once, and a terminal observation or the artifacts it pins
are never changed.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
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
from ..evidence import output_publication_scope
from ..machine_observation import (
    ArtifactResult,
    build_machine_observation,
    machine_json_bytes,
    machine_lifecycle,
    report_json_path,
)
from ..route_rules import RouteFacts, route_facts
from ..state import normalized_payload_from_state, retired_generation, write_generation_bytes
from .composer import compose_job_report_html
from .si import render_si_block, si_block_path

logger = logging.getLogger(__name__)


GenerationTarget = tuple[Path, tuple[int, int]]


@dataclass(frozen=True)
class PublicationEvidence:
    """What one publication reads, collected once by the publisher.

    ``state`` is the flat run state the HTML and SI renderers read and
    ``report_payload`` its normalized envelope that ``machine.json`` reads.
    ``route`` is the route facts of ``state["selected_inp"]``, read once for
    both renderers; ``None`` when there is no selected input or they could not
    be read here (each renderer then reads them itself, inside its own failure
    boundary). ``machine.json`` derives its scientific verdict from the
    receipted input bytes it digests, not from ``route``.
    """

    reaction_dir: Path
    state: Mapping[str, Any]
    report_payload: dict[str, Any]
    generation_target: GenerationTarget
    route: RouteFacts | None


def _selected_route(state: Mapping[str, Any]) -> RouteFacts | None:
    selected_raw = str(state.get("selected_inp") or "").strip()
    if not selected_raw:
        return None
    try:
        return route_facts(Path(selected_raw))
    except Exception:  # noqa: BLE001 -- each renderer re-reads and records its own failure
        return None


def _publish_optional_report(
    reaction_dir: Path,
    generation_target: GenerationTarget,
    *,
    artifact_id: str,
    path: Path,
    render: Callable[[], str | None],
) -> ArtifactResult:
    """Render one optional report and write it; the result says which of the three happened.

    A report the job type does not have removes a stale file of that name, so
    links cannot surface an obsolete report. A failure deliberately does not
    remove it: a transient error must not destroy the last valid report, and
    the observation receipts only what this publication produced.
    """
    try:
        rendered = render()
        if rendered is None:
            path.unlink(missing_ok=True)
            return ArtifactResult.not_applicable(artifact_id)
        atomic_write_confined_bytes(
            generation_target[0],
            path,
            rendered.encode("utf-8"),
            label="ORCA generation artifact",
            mode=0o600,
            expected_parent_identity=generation_target[1],
        )
        return ArtifactResult.produced(artifact_id, path)
    except Exception:  # noqa: BLE001
        logger.warning("%s generation failed for %s", artifact_id, reaction_dir, exc_info=True)
        return ArtifactResult.failed(artifact_id)


def write_job_html_report(
    reaction_dir: Path,
    state: Mapping[str, Any],
    *,
    generation_target: GenerationTarget,
    route: RouteFacts | None = None,
) -> ArtifactResult:
    """Write ``job_report.html`` into the verified generation (see :func:`_publish_optional_report`)."""
    return _publish_optional_report(
        reaction_dir,
        generation_target,
        artifact_id="human-report",
        path=generation_target[0] / RUN_REPORT_HTML_FILE,
        render=lambda: compose_job_report_html(reaction_dir, state, route),
    )


def write_si_block(
    reaction_dir: Path,
    state: Mapping[str, Any],
    *,
    generation_target: GenerationTarget,
    route: RouteFacts | None = None,
) -> ArtifactResult:
    """Write ``si_block.md`` into the verified generation (see :func:`_publish_optional_report`)."""
    return _publish_optional_report(
        reaction_dir,
        generation_target,
        artifact_id="supporting-information",
        path=si_block_path(generation_target[0]),
        render=lambda: render_si_block(reaction_dir, state, route),
    )


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
    generation_target: GenerationTarget | None = None,
    reports: Sequence[ArtifactResult] = (),
    before_write: Callable[[], None] | None = None,
) -> Path | None:
    """Write ``machine.json`` (and ``execution_provenance.json``) into the verified generation.

    ``reports`` are this publication's optional report results. A terminal
    observation is written once; writing different bytes over it raises.
    ``before_write`` runs after the observation is built and before it is
    written; when it raises, nothing is written.
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
    observation = build_machine_observation(generation_target[0], payload, reports=reports)
    if before_write is not None:
        before_write()
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
    evidence = PublicationEvidence(
        reaction_dir=reaction_dir,
        state=state,
        report_payload=report_payload,
        generation_target=generation_target,
        route=_selected_route(state),
    )
    # HTML, SI and machine.json must describe one version of each output: an
    # output that changes during the publication abandons it before
    # machine.json is written, and the next publication starts over.
    with output_publication_scope() as verify_outputs_unchanged:
        optional_reports = (
            write_job_html_report(
                evidence.reaction_dir,
                evidence.state,
                generation_target=evidence.generation_target,
                route=evidence.route,
            ),
            write_si_block(
                evidence.reaction_dir,
                evidence.state,
                generation_target=evidence.generation_target,
                route=evidence.route,
            ),
        )
        json_path = write_report_json(
            evidence.reaction_dir,
            evidence.report_payload,
            generation_target=evidence.generation_target,
            reports=optional_reports,
            before_write=verify_outputs_unchanged,
        )
    # List the reports as the observation just published receipts them, so
    # this result and any re-entry name the same files.
    published = _published_observation(generation_target[0])
    reports = _receipted_report_paths(
        generation_target[0], published[1] if published is not None else {}
    )
    reports["report_json"] = str(json_path)
    return reports
