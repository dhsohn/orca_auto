from __future__ import annotations

from argparse import Namespace
from pathlib import Path
from types import SimpleNamespace

import pytest

from orca_auto.smoke import cli as smoke_cli
from orca_auto.smoke.cli import _resolve_roots, _source_checkout_root, build_parser


def test_fake_explicit_runs_root_does_not_load_unrelated_discovered_config(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    (repo / "config").mkdir(parents=True)
    (repo / "config" / "orca_auto.yaml").write_text("[invalid", encoding="utf-8")
    runs_root = tmp_path / "runs"

    resolved, admission = _resolve_roots(
        repo_root=repo,
        runs_root_argument=str(runs_root),
        config_argument="",
        profile="fake",
    )

    assert resolved == runs_root.resolve()
    assert admission is None


def test_fake_default_uses_discovered_config_runs_root(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    config_dir = repo / "config"
    config_dir.mkdir(parents=True)
    runs_root = tmp_path / "runs"
    (config_dir / "orca_auto.yaml").write_text(
        f"runs_root: {runs_root}\n",
        encoding="utf-8",
    )

    resolved, admission = _resolve_roots(
        repo_root=repo,
        runs_root_argument="",
        config_argument="",
        profile="fake",
    )

    assert resolved == runs_root.resolve()
    assert admission is None


def test_real_profile_uses_matching_production_root_and_admission_limits(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    runs_root = tmp_path / "runs"
    admission_root = tmp_path / "admission"
    config = tmp_path / "orca_auto.yaml"
    config.write_text(
        "\n".join(
            [
                f"runs_root: {runs_root}",
                "scheduler:",
                "  max_active_simulations: 3",
                "  max_active_xtb_md: 1",
                f"  admission_root: {admission_root}",
                "",
            ]
        ),
        encoding="utf-8",
    )

    resolved, admission = _resolve_roots(
        repo_root=repo,
        runs_root_argument=str(runs_root),
        config_argument=str(config),
        profile="real-xtb",
    )

    assert resolved == runs_root.resolve()
    assert admission is not None
    assert admission.root == admission_root.resolve()
    assert admission.global_limit == 3
    assert admission.xtb_md_limit == 1


def test_real_profile_uses_production_global_default_when_scheduler_key_is_missing(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    runs_root = tmp_path / "runs"
    config = tmp_path / "orca_auto.yaml"
    config.write_text(
        f"runs_root: {runs_root}\nscheduler:\n  max_active_xtb_md: 1\n",
        encoding="utf-8",
    )

    _resolved, admission = _resolve_roots(
        repo_root=repo,
        runs_root_argument=str(runs_root),
        config_argument=str(config),
        profile="real-xtb",
    )

    assert admission is not None
    assert admission.global_limit == 4
    assert admission.xtb_md_limit == 1


def test_real_profile_rejects_results_root_that_bypasses_configured_admission(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    configured_root = tmp_path / "configured-runs"
    config = tmp_path / "orca_auto.yaml"
    config.write_text(
        f"runs_root: {configured_root}\nscheduler:\n  max_active_simulations: 1\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="must match"):
        _resolve_roots(
            repo_root=repo,
            runs_root_argument=str(tmp_path / "different-runs"),
            config_argument=str(config),
            profile="real-xtb",
        )


def test_real_orca_profile_uses_global_admission_without_xtb_subcap(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    runs_root = tmp_path / "runs"
    admission_root = tmp_path / "admission"
    config = tmp_path / "orca_auto.yaml"
    config.write_text(
        "\n".join(
            [
                f"runs_root: {runs_root}",
                "scheduler:",
                "  max_active_simulations: 2",
                "  max_active_xtb_md: 1",
                f"  admission_root: {admission_root}",
                "",
            ]
        ),
        encoding="utf-8",
    )

    resolved, admission = _resolve_roots(
        repo_root=repo,
        runs_root_argument=str(runs_root),
        config_argument=str(config),
        profile="real-orca",
    )

    assert resolved == runs_root.resolve()
    assert admission is not None
    assert admission.root == admission_root.resolve()
    assert admission.global_limit == 2
    assert admission.xtb_md_limit is None


def test_parser_accepts_real_orca_profile() -> None:
    args = build_parser().parse_args(["--profile", "real-orca", "--runs-root", "/tmp/runs"])

    assert args.profile == "real-orca"


def test_source_checkout_root_rejects_a_different_checkout(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="must match the source checkout"):
        _source_checkout_root(str(tmp_path))


def test_source_checkout_root_rejects_wheel_without_tests(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = tmp_path / "site-packages" / "orca_auto" / "smoke" / "cli.py"
    module.parent.mkdir(parents=True)
    module.write_text("", encoding="utf-8")
    monkeypatch.setattr(smoke_cli, "__file__", str(module))

    with pytest.raises(ValueError, match="source checkout"):
        _source_checkout_root("")


def test_cmd_smoke_prints_retained_packet_paths_and_returns_batch_exit_code(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo = tmp_path / "repo"
    runs_root = tmp_path / "runs"
    batch = runs_root / ".orca_auto_smoke" / "batches" / "batch-1"
    review = SimpleNamespace(
        index_path=batch / "review" / "index.html",
        summary_path=batch / "summary.md",
    )
    monkeypatch.setattr(smoke_cli, "_source_checkout_root", lambda _argument: repo)
    monkeypatch.setattr(
        smoke_cli,
        "_resolve_roots",
        lambda **_kwargs: (runs_root, None),
    )
    monkeypatch.setattr(
        smoke_cli,
        "run_smoke_suite",
        lambda **_kwargs: SimpleNamespace(batch_dir=batch, review=review, exit_code=7),
    )

    result = smoke_cli.cmd_smoke(
        Namespace(
            repo_root="",
            runs_root="",
            config="",
            profile="fake",
            scenario=[],
        )
    )

    assert result == 7
    rendered = capsys.readouterr().out
    assert f"smoke batch: {batch}" in rendered
    assert f"review index: {review.index_path}" in rendered
    assert f"review summary: {review.summary_path}" in rendered
