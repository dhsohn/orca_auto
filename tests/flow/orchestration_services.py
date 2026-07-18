from __future__ import annotations

from collections.abc import Mapping
from dataclasses import fields, replace
from typing import Any

from orca_auto.flow.orchestration.services import (
    OrchestrationServices,
    default_orchestration_services,
)


def orchestration_services(overrides: Mapping[str, Any] | None = None) -> OrchestrationServices:
    base = default_orchestration_services()
    requested = dict(overrides or {})
    sections: dict[str, Any] = {
        "persistence": base.persistence,
        "engines": base.engines,
        "clock": base.clock,
        "events": base.events,
    }
    owners = {
        field.name: section_name
        for section_name, section in sections.items()
        for field in fields(section)
    }
    unknown = sorted(set(requested) - set(owners))
    if unknown:
        raise TypeError(f"unknown orchestration service override(s): {', '.join(unknown)}")

    replacements: dict[str, Any] = {}
    for section_name, section in sections.items():
        section_overrides = {
            name: value for name, value in requested.items() if owners[name] == section_name
        }
        replacements[section_name] = replace(section, **section_overrides)
    return replace(base, **replacements)


__all__ = ["orchestration_services"]
