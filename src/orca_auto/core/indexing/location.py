from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class JobLocationRecord:
    job_id: str
    app_name: str
    job_type: str
    status: str
    original_run_dir: str
    molecule_key: str = ""
    selected_input_xyz: str = ""
    latest_known_path: str = ""
    resource_request: dict[str, int] = field(default_factory=dict)
    resource_actual: dict[str, int] = field(default_factory=dict)
