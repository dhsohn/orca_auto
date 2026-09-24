"""Persisted ORCA application identity derived from the engine catalog."""

from __future__ import annotations

from .engine_catalog import get_engine_catalog_entry

ORCA_AUTO_ORCA_APP_NAME = get_engine_catalog_entry("orca").app_id

ORCA_AUTO_ORCA_SOURCE = get_engine_catalog_entry("orca").source_id

__all__ = ["ORCA_AUTO_ORCA_APP_NAME", "ORCA_AUTO_ORCA_SOURCE"]
