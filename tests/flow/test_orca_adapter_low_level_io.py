from __future__ import annotations

import json
from pathlib import Path

import pytest

from orca_auto.core.utils.coercion import coerce_bool, normalize_bool, safe_int
from orca_auto.flow.adapters import _orca_local_lookup, _orca_path_helpers


def test_normalize_bool_and_safe_int_cover_string_and_default_paths() -> None:
    assert normalize_bool(True) is True
    assert normalize_bool(" yes ") is True
    assert normalize_bool("off") is False
    assert coerce_bool("off") is False
    assert coerce_bool(["truthy"]) is True
    assert coerce_bool([]) is False

    assert safe_int("12") == 12
    assert safe_int("bad", default=7) == 7
    assert safe_int(None, default=9) == 9


def test_load_json_list_handles_missing_invalid_and_type_filtered_payloads(
    tmp_path: Path,
) -> None:
    missing = tmp_path / "missing.json"
    assert _orca_local_lookup.load_json_list_impl(missing) == []

    invalid = tmp_path / "invalid.json"
    invalid.write_text("{not-json", encoding="utf-8")
    assert _orca_local_lookup.load_json_list_impl(invalid) == []

    wrong_type = tmp_path / "wrong-type.json"
    wrong_type.write_text(json.dumps(["x", {"ok": True}]), encoding="utf-8")
    assert _orca_local_lookup.load_json_list_impl(wrong_type) == [{"ok": True}]


def test_resolve_candidate_path_and_direct_dir_target_cover_existing_and_oserror_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    xyz_path = tmp_path / "candidate.xyz"
    xyz_path.write_text("2\ncomment\nH 0 0 0\nH 0 0 0.7\n", encoding="utf-8")
    assert _orca_path_helpers.resolve_candidate_path_impl(str(xyz_path)) == xyz_path.resolve()
    assert _orca_path_helpers.resolve_candidate_path_impl("   ") is None
    assert _orca_path_helpers.direct_dir_target_impl(str(xyz_path)) == xyz_path.parent.resolve()
    assert _orca_path_helpers.direct_dir_target_impl(str(tmp_path)) == tmp_path.resolve()
    assert _orca_path_helpers.direct_dir_target_impl(str(tmp_path / "missing")) is None

    class _ExpandUserBroken:
        def expanduser(self) -> _ExpandUserBroken:
            raise OSError("bad path")

    monkeypatch.setattr(_orca_path_helpers, "Path", lambda raw: _ExpandUserBroken())
    assert _orca_path_helpers.resolve_candidate_path_impl("/tmp/ignored") is None
    assert _orca_path_helpers.direct_dir_target_impl("/tmp/ignored") is None
