from __future__ import annotations

from orca_auto.core.activity import ActivitySourceRequest, ResolvedActivitySources
from orca_auto.core.config import discovery
from orca_auto.core.utils import normalize_text


def resolve_activity_source_request(request: ActivitySourceRequest) -> ResolvedActivitySources:
    config = normalize_text(request.orca_config) or normalize_text(request.shared_config)
    return ResolvedActivitySources(orca_config=discovery.resolve_shared_config_path(config or None))
