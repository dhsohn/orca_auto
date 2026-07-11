"""Config-parsing safety for the upload policy."""

from __future__ import annotations

import pytest

from orca_auto.core.ingest import DEFAULT_ALLOWED_EXTENSIONS
from orca_auto.core.ingest.policy import upload_policy_from_mapping


def test_enabled_absent_is_disabled() -> None:
    assert upload_policy_from_mapping({}).enabled is False
    assert upload_policy_from_mapping(None).enabled is False


@pytest.mark.parametrize("value", [True, "true", "yes", "on", "1", 1])
def test_enabled_truthy(value: object) -> None:
    assert upload_policy_from_mapping({"enabled": value}).enabled is True


@pytest.mark.parametrize("value", [False, "false", "no", "off", "0", 0, "garbage"])
def test_enabled_falsy_and_stringy_fail_closed(value: object) -> None:
    # The key fix: a quoted/typo'd falsy value must NOT enable the feature.
    assert upload_policy_from_mapping({"enabled": value}).enabled is False


def test_allowed_extensions_absent_uses_default() -> None:
    assert upload_policy_from_mapping({}).allowed_extensions == DEFAULT_ALLOWED_EXTENSIONS


def test_allowed_extensions_empty_list_locks_down() -> None:
    # An explicit empty list means "allow nothing", not "revert to default".
    assert upload_policy_from_mapping({"allowed_extensions": []}).allowed_extensions == ()


def test_allowed_extensions_normalized() -> None:
    policy = upload_policy_from_mapping({"allowed_extensions": ["inp", ".XYZ", "yaml", "inp"]})
    assert policy.allowed_extensions == (".inp", ".xyz", ".yaml")


@pytest.mark.parametrize("field", ["max_archive_bytes", "max_entries", "max_file_bytes"])
def test_size_fields_reject_bool(field: str) -> None:
    with pytest.raises(ValueError, match="not a boolean"):
        upload_policy_from_mapping({field: True})


@pytest.mark.parametrize("value", [0, -1, "abc"])
def test_size_fields_reject_invalid(value: object) -> None:
    with pytest.raises(ValueError):
        upload_policy_from_mapping({"max_archive_bytes": value})


def test_allowed_extensions_rejects_scalar() -> None:
    with pytest.raises(ValueError, match="list of extensions"):
        upload_policy_from_mapping({"allowed_extensions": "inp"})
