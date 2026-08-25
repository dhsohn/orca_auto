from __future__ import annotations

from importlib import import_module

from orca_auto.core.engine_catalog import get_engine_catalog_entry

from .definitions import EngineDefinition


def get_engine_definition(engine: str) -> EngineDefinition:
    entry = get_engine_catalog_entry(engine)
    module = import_module(entry.definition_module)
    definition = module.ENGINE_DEFINITION
    if not isinstance(definition, EngineDefinition):
        raise TypeError(f"{entry.definition_module}.ENGINE_DEFINITION is not an EngineDefinition")
    return definition


__all__ = ["get_engine_definition"]
