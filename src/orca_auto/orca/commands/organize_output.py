from __future__ import annotations

from typing import Any

from ..result_organizer.models import OrganizePlan, SkipReason


def emit_organize(payload: dict[str, Any]) -> None:
    for key in [
        "action",
        "to_organize",
        "skipped",
        "organized",
        "failed",
        "records_count",
        "job_locations_count",
    ]:
        if key in payload:
            print(f"{key}: {payload[key]}")
    for p in payload.get("plans", []):
        print(f"  {p['source_dir']} -> {p['target_rel_path']}")
    for s in payload.get("skip_reasons", []):
        print(f"  SKIP {s['reaction_dir']}: {s['reason']}")
    for f in payload.get("failures", []):
        print(f"  FAIL {f['run_id']}: {f['reason']}")
    for key in ["run_id", "job_type", "molecule_key", "organized_path", "count"]:
        if key in payload:
            print(f"{key}: {payload[key]}")
    for r in payload.get("results", []):
        print(f"  {r.get('run_id', '?')}: {r.get('organized_path', '?')}")


def plan_to_dict(plan: OrganizePlan) -> dict[str, Any]:
    return {
        "run_id": plan.run_id,
        "source_dir": str(plan.source_dir),
        "target_rel_path": plan.target_rel_path,
        "target_abs_path": str(plan.target_abs_path),
        "job_type": plan.job_type,
        "molecule_key": plan.molecule_key,
    }


def build_dry_run_summary(
    plans: list[OrganizePlan],
    skips_list: list[SkipReason],
) -> dict[str, Any]:
    return {
        "action": "dry_run",
        "to_organize": len(plans),
        "skipped": len(skips_list),
        "plans": [plan_to_dict(p) for p in plans],
        "skip_reasons": [{"reaction_dir": s.reaction_dir, "reason": s.reason} for s in skips_list],
    }
