from __future__ import annotations

import pytest

from orca_auto import cli_systemd_units, systemd_plan


def test_service_units_for_user_reuses_canonical_name_owner() -> None:
    assert cli_systemd_units._service_units_for_user("alice") == (
        ("runtime", systemd_plan._runtime_unit_for_user("alice")),
        ("engines", systemd_plan._engine_workers_unit_for_user("alice")),
        ("worker", systemd_plan._worker_unit_for_user("alice")),
        ("workflow", systemd_plan._workflow_worker_unit_for_user("alice")),
    )


def test_service_units_for_user_rejects_blank_user() -> None:
    with pytest.raises(ValueError, match="service user is required"):
        cli_systemd_units._service_units_for_user("  ")


def test_default_service_user_resolves_the_account_behind_sudo(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Under sudo, getpass reports root and every unit becomes an @root instance.

    systemd calls those a success -- `reset-failed` exits 0 on a unit it says is
    not loaded -- so the restart silently misses the real workers.
    """

    monkeypatch.setattr(systemd_plan, "_is_root", lambda: True)
    monkeypatch.setattr(cli_systemd_units.getpass, "getuser", lambda: "root")
    monkeypatch.setenv("SUDO_USER", "alice")

    assert cli_systemd_units._default_service_user() == "alice"


def test_default_service_user_keeps_root_for_a_real_root_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(systemd_plan, "_is_root", lambda: True)
    monkeypatch.setattr(cli_systemd_units.getpass, "getuser", lambda: "root")
    monkeypatch.delenv("SUDO_USER", raising=False)

    assert cli_systemd_units._default_service_user() == "root"


def test_default_service_user_ignores_a_root_sudo_invoker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(systemd_plan, "_is_root", lambda: True)
    monkeypatch.setattr(cli_systemd_units.getpass, "getuser", lambda: "root")
    monkeypatch.setenv("SUDO_USER", "root")

    assert cli_systemd_units._default_service_user() == "root"


def test_default_service_user_ignores_sudo_user_without_root(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stale SUDO_USER in a normal shell must not redirect the units."""

    monkeypatch.setattr(systemd_plan, "_is_root", lambda: False)
    monkeypatch.setattr(cli_systemd_units.getpass, "getuser", lambda: "alice")
    monkeypatch.setenv("SUDO_USER", "bob")

    assert cli_systemd_units._default_service_user() == "alice"
