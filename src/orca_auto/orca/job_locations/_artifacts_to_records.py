"""Project a directory's ORCA artifacts onto one job-location row.

``normalized_artifact_view`` flattens a schema-version-1 ``machine.json``
into the flat keys the index reads; a ``job_state.json`` reaches the index
only as :func:`run_state_index_source` of the decoded state. ``record_from_artifacts``
reads the identity, status, selected input, job type, molecule key, resources
and original run directory out of a ``job_state.json`` / ``report.json`` pair
(``report`` wins over ``state``), falling back to the existing row and finally
to what the selected input implies, and builds the row with
``_records.build_job_location_record``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from orca_auto.core.indexing import JobLocationRecord
from orca_auto.core.indexing.store import normalize_index_text
from orca_auto.core.utils import copy_dict_or_empty, normalize_text

from ..state_reading import DecodedRunState
from ._records import build_job_location_record, derive_input_metadata
from ._utils import normalize_path_text, resource_dict_from_any


class UnreadableReportError(ValueError):
    """A ``machine.json`` whose fields the index cannot read (a malformed ``schema_version``)."""


def _schema_version(source: dict[str, Any]) -> int:
    value = source.get("schema_version")
    if value is None:
        return 0
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    if isinstance(value, str) and value.strip().isdecimal():
        return int(value)
    raise UnreadableReportError(f"schema_version {value!r} is not an integer")


# The fields a job-location row reads, by kind. ``decode_index_source`` checks
# and normalizes exactly these, so nothing after it interprets a raw value.
_INDEX_TEXT_KEYS = ("job_id", "run_id", "status", "job_type", "molecule_key")
_INDEX_PATH_KEYS = ("selected_inp", "selected_input_xyz", "original_run_dir")
_INDEX_RESOURCE_KEYS = ("resource_request", "resource_actual")


def _index_text(key: str, value: Any) -> str:
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise UnreadableReportError(f"{key} is not text")
    return normalize_index_text(value)


def _index_path(key: str, value: Any) -> str:
    """``value`` as the index stores it, once it is known to resolve as a path."""
    if not isinstance(value, str):
        raise UnreadableReportError(f"{key} is not a path")
    try:
        normalized = normalize_path_text(value)
        # The row builder resolves the run directory; refuse here what it cannot.
        Path(normalized).expanduser().resolve()
    except (OSError, RuntimeError, ValueError) as exc:
        raise UnreadableReportError(f"{key} is not a usable path: {exc}") from exc
    # The run directory keeps its recorded text; input paths are stored resolved.
    return normalize_index_text(value) if key == "original_run_dir" else normalized


def _index_resources(key: str, value: Any) -> dict[str, int]:
    if not isinstance(value, dict):
        raise UnreadableReportError(f"{key} is not an object")
    resources: dict[str, int] = {}
    for name, raw in value.items():
        name_text = normalize_text(name)
        if not name_text:
            continue
        try:
            resources[name_text] = int(raw)
        except (OverflowError, TypeError, ValueError) as exc:
            raise UnreadableReportError(f"{key}.{name_text} is not an integer") from exc
    return resources


def decode_index_source(view: dict[str, Any]) -> dict[str, Any]:
    """The index fields of a flat artifact view, checked and normalized.

    Empty (falsy) values are left out, as the row's fallbacks expect. A value of the
    wrong kind, a path that cannot be resolved, or a resource that is not a
    finite integer raises ``UnreadableReportError``.
    """
    source: dict[str, Any] = {}
    for key in _INDEX_TEXT_KEYS:
        if view.get(key):
            source[key] = _index_text(key, view[key])
    for key in _INDEX_PATH_KEYS:
        if view.get(key):
            source[key] = _index_path(key, view[key])
    for key in _INDEX_RESOURCE_KEYS:
        if view.get(key) is not None:
            source[key] = _index_resources(key, view[key])
    return source


def decode_report(report: Any) -> dict[str, Any]:
    """The report's index fields (:func:`decode_index_source`); ``UnreadableReportError`` when unreadable.

    The rebuild decodes each directory's report here once, so a malformed
    report is set aside for that directory (its ``job_state.json`` still
    indexes it) instead of failing the walk, and the row reads only the
    decoded fields.
    """
    if not isinstance(report, dict):
        raise UnreadableReportError("not a JSON object")
    return decode_index_source(normalized_artifact_view(report))


def normalized_artifact_view(source: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(source, dict):
        return {}
    if _schema_version(source) != 1:
        return dict(source)
    job = copy_dict_or_empty(source.get("job"))
    status = copy_dict_or_empty(source.get("status"))
    input_payload = copy_dict_or_empty(source.get("input"))
    resources = copy_dict_or_empty(source.get("resources"))
    artifacts = copy_dict_or_empty(source.get("artifacts"))
    engine_payload = copy_dict_or_empty(source.get("engine_payload"))
    view = dict(engine_payload)
    view.setdefault("job_id", job.get("id"))
    view.setdefault("queue_id", job.get("queue_id"))
    view.setdefault("job_dir", job.get("dir"))
    view.setdefault("original_run_dir", job.get("dir"))
    view.setdefault("status", status.get("state"))
    view.setdefault("reason", status.get("reason"))
    view.setdefault("selected_input_xyz", input_payload.get("selected_xyz_path"))
    view.setdefault("selected_inp", input_payload.get("primary_path"))
    view.setdefault("manifest_path", artifacts.get("manifest_path"))
    view.setdefault("resource_request", resources.get("request"))
    view.setdefault("resource_actual", resources.get("actual"))
    return view


def run_state_index_source(decoded: DecodedRunState) -> dict[str, Any]:
    """The index keys of a decoded ``job_state.json``.

    The decoder (``state_reading.decode_run_state``) has already validated the
    envelope and refused conflicting identity copies, so the row reads only
    what it returned. ``original_run_dir`` is the state's own ``job.dir``, as
    the envelope view gave it.
    """
    state = decoded.state
    return {
        "job_id": state.get("job_id", ""),
        "run_id": state.get("run_id", ""),
        "queue_id": state.get("queue_id", ""),
        "status": state.get("status", ""),
        "selected_inp": state.get("selected_inp", ""),
        "selected_input_xyz": decoded.selected_xyz_path,
        "original_run_dir": state.get("reaction_dir", ""),
        "resource_request": dict(decoded.resource_request),
        "resource_actual": dict(decoded.resource_actual),
    }


def first_artifact_value(sources: tuple[dict[str, Any], ...], *keys: str) -> Any:
    for source in sources:
        view = normalized_artifact_view(source)
        for key in keys:
            value = view.get(key)
            if value:
                return value
    return None


def first_artifact_text(sources: tuple[dict[str, Any], ...], *keys: str) -> str:
    value = first_artifact_value(sources, *keys)
    return "" if value is None else normalize_index_text(value)


def _first_resources(
    sources: tuple[dict[str, Any], ...],
    key: str,
    existing: JobLocationRecord | None,
) -> dict[str, int]:
    for source in sources:
        mapped = resource_dict_from_any(normalized_artifact_view(source).get(key))
        if mapped:
            return mapped
    if existing is None:
        return {}
    return dict(getattr(existing, key))


def record_from_artifacts(
    *,
    job_dir: Path,
    state: dict[str, Any] | None,
    report: dict[str, Any] | None,
    existing: JobLocationRecord | None = None,
    fallback_job_id: str = "",
    default_job_type: str = "other",
) -> JobLocationRecord | None:
    sources = (report or {}, state or {})
    job_id = (
        first_artifact_text(sources, "job_id")
        or normalize_text(fallback_job_id)
        or normalize_text(existing.job_id if existing else "")
        or first_artifact_text(sources, "run_id")
    )
    status = first_artifact_text(sources, "status") or "unknown"
    selected_inp = normalize_path_text(first_artifact_value(sources, "selected_inp"))
    supplied_input_xyz = normalize_path_text(first_artifact_value(sources, "selected_input_xyz"))
    if not job_id:
        return None

    # Each field takes the artifacts' value first; only a missing one is
    # derived from the selected input, which is otherwise never opened.
    supplied_job_type = first_artifact_value(sources, "job_type")
    supplied_molecule_key = first_artifact_value(sources, "molecule_key") or (
        existing.molecule_key if existing else ""
    )
    original_run_dir = (
        first_artifact_text(sources, "original_run_dir")
        or normalize_text(existing.original_run_dir if existing else "")
        or str(job_dir)
    )
    # One derivation, at most one read of the input, for every field the
    # artifacts lack. The directory fallback is the job's own run directory,
    # as submission recorded it, wherever the job lives now.
    need_input_xyz = not supplied_input_xyz.lower().endswith(".xyz")
    derived = derive_input_metadata(
        selected_inp,
        Path(original_run_dir),
        need_selected_input_xyz=need_input_xyz,
        need_job_type=not supplied_job_type,
        need_molecule_key=not supplied_molecule_key,
    )
    selected_input_xyz = (
        (derived.selected_input_xyz if need_input_xyz else supplied_input_xyz)
        or selected_inp
        or (existing.selected_input_xyz if existing else "")
    )
    job_type = (
        normalize_text(supplied_job_type or derived.job_type or default_job_type)
        or default_job_type
    )
    molecule_key = normalize_text(supplied_molecule_key or derived.molecule_key)
    resource_request = _first_resources(sources, "resource_request", existing)
    resource_actual = _first_resources(sources, "resource_actual", existing)
    return build_job_location_record(
        existing=existing,
        job_id=job_id,
        status=status,
        job_dir=Path(original_run_dir),
        job_type=job_type,
        selected_input_xyz=selected_input_xyz,
        molecule_key=molecule_key,
        resource_request=resource_request,
        resource_actual=resource_actual,
    )
