from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from orca_auto.core.utils.coercion import (
    normalize_bool,
    normalize_text,
    safe_float,
    safe_int,
)


@dataclass(frozen=True)
class XtbCandidateArtifact:
    rank: int
    kind: str
    path: str
    selected: bool = False
    score: float | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_raw(cls, raw: dict[str, Any]) -> XtbCandidateArtifact:
        metadata = {
            str(key): value
            for key, value in raw.items()
            if str(key) not in {"rank", "kind", "path", "selected", "score"}
        }
        return cls(
            rank=max(0, safe_int(raw.get("rank"), default=0)),
            kind=normalize_text(raw.get("kind"), none="None") or "candidate",
            path=normalize_text(raw.get("path"), none="None"),
            selected=normalize_bool(raw.get("selected")),
            score=safe_float(raw.get("score")),
            metadata=metadata,
        )

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        if self.score is None:
            payload.pop("score", None)
        if not self.metadata:
            payload["metadata"] = {}
        return payload


@dataclass(frozen=True)
class XtbArtifactContract:
    job_id: str
    job_type: str
    status: str
    reason: str
    job_dir: str
    latest_known_path: str
    reaction_key: str = ""
    selected_input_xyz: str = ""
    selected_candidate_paths: tuple[str, ...] = ()
    candidate_details: tuple[XtbCandidateArtifact, ...] = ()
    analysis_summary: dict[str, Any] = field(default_factory=dict)
    resource_request: dict[str, int] = field(default_factory=dict)
    resource_actual: dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "job_type": self.job_type,
            "status": self.status,
            "reason": self.reason,
            "job_dir": self.job_dir,
            "latest_known_path": self.latest_known_path,
            "reaction_key": self.reaction_key,
            "selected_input_xyz": self.selected_input_xyz,
            "selected_candidate_paths": list(self.selected_candidate_paths),
            "candidate_details": [item.to_dict() for item in self.candidate_details],
            "analysis_summary": dict(self.analysis_summary),
            "resource_request": dict(self.resource_request),
            "resource_actual": dict(self.resource_actual),
        }


@dataclass(frozen=True)
class XtbDownstreamPolicy:
    preferred_kinds: tuple[str, ...] = (
        "ts_guess",
        "selected_path",
        "optimized_geometry",
        "single_point_result",
    )
    max_candidates: int = 3
    selected_only: bool = True
    allowed_kinds: tuple[str, ...] = ()

    @classmethod
    def build(
        cls,
        *,
        preferred_kinds: list[str] | tuple[str, ...] | None = None,
        max_candidates: int = 3,
        selected_only: bool = True,
        allowed_kinds: list[str] | tuple[str, ...] | None = None,
    ) -> XtbDownstreamPolicy:
        kinds = tuple(
            text
            for item in (preferred_kinds or cls().preferred_kinds)
            if (text := normalize_text(item, none="None"))
        )
        filtered_kinds = tuple(
            text for item in (allowed_kinds or ()) if (text := normalize_text(item, none="None"))
        )
        return cls(
            preferred_kinds=kinds or cls().preferred_kinds,
            max_candidates=max(1, int(max_candidates)),
            selected_only=bool(selected_only),
            allowed_kinds=filtered_kinds,
        )


@dataclass(frozen=True)
class WorkflowStageInput:
    source_job_id: str
    source_job_type: str
    reaction_key: str
    selected_input_xyz: str
    rank: int
    kind: str
    artifact_path: str
    selected: bool = False
    score: float | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def geometry_invalid(self) -> bool:
        """True when upstream geometry validation explicitly rejected this candidate."""
        return self.metadata.get("geometry_valid") is False

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        if self.score is None:
            payload.pop("score", None)
        if not self.metadata:
            payload["metadata"] = {}
        return payload


__all__ = [
    "WorkflowStageInput",
    "XtbArtifactContract",
    "XtbCandidateArtifact",
    "XtbDownstreamPolicy",
]
