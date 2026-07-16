from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from orca_auto.smoke import review as smoke_review
from orca_auto.smoke.review import (
    ReviewPacketError,
    ReviewPacketResult,
    generate_review_packet,
)


def _case_manifest(**overrides: object) -> dict[str, object]:
    manifest: dict[str, object] = {
        "case_id": "orca-sp-success",
        "surface": "orca-standalone",
        "scenario": "sp-success",
        "expected_terminal": "completed",
        "observed_terminal": "completed",
        "verdict": "PASS",
        "case_dir": "cases/orca-sp-success",
        "runtime_dir": "runtime",
    }
    manifest.update(overrides)
    return manifest


def _runtime(batch_dir: Path) -> Path:
    runtime = batch_dir / "cases" / "orca-sp-success" / "runtime"
    runtime.mkdir(parents=True)
    return runtime


def _projection_records(result: ReviewPacketResult) -> list[dict[str, object]]:
    manifest_path = result.artifact_manifest_path
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert payload["schema_version"] == 3
    assert payload["runtime_source_of_truth"] is True
    assert payload["artifact_count"] == len(payload["artifacts"])
    return payload["artifacts"]


def _record_for(records: list[dict[str, object]], source_suffix: str) -> dict[str, object]:
    matches = [record for record in records if str(record["source_path"]).endswith(source_suffix)]
    assert len(matches) == 1
    return matches[0]


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_generate_review_packet_links_artifacts_and_escapes_all_previews(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    (runtime / "job_report.html").write_text(
        '<script>alert("review-xss")</script><h1>SP complete</h1>', encoding="utf-8"
    )
    (runtime / "si_block.md").write_text("# SI\nEnergy: -40.1", encoding="utf-8")
    (runtime / "job_report.json").write_text('{"status":"completed"}', encoding="utf-8")
    (runtime / "energies.csv").write_text("step,energy\n1,-40.1\n", encoding="utf-8")
    (runtime / "orca.out").write_text("ORCA TERMINATED NORMALLY\n", encoding="utf-8")
    (runtime / "<img onerror=file-xss>.log").write_text("safe filename test", encoding="utf-8")

    manifest = {
        "batch_id": "batch-001",
        "profile": "fake",
        "status": "completed",
        "source": {"git_short": "deadbee"},
        "cases": [
            _case_manifest(
                case_id='<img src=x onerror="case-xss">',
                expected_terminal={"status": "completed"},
                observed_terminal={"status": "completed", "reasons": ["normal marker"]},
            )
        ],
    }
    result = generate_review_packet(tmp_path, manifest)

    assert result.summary_path == tmp_path / "summary.md"
    assert result.index_path == tmp_path / "review" / "index.html"
    assert result.artifact_manifest_path.is_file()
    assert result.artifact_count == 6
    assert result.openable_count == 6

    index = result.index_path.read_text(encoding="utf-8")
    summary = result.summary_path.read_text(encoding="utf-8")
    records = _projection_records(result)
    assert "default-src 'none'" in index
    assert "frame-src 'none'" in index
    assert '<script>alert("review-xss")</script>' not in index
    assert "&lt;script&gt;alert(&quot;review-xss&quot;)&lt;/script&gt;" in index
    assert '<img src=x onerror="case-xss">' not in index
    assert "&lt;img src=x onerror=&quot;case-xss&quot;&gt;" in index
    assert "<img onerror=file-xss>" not in index
    assert "&lt;img onerror=file-xss&gt;.log" in index
    assert "img-onerror-file-xss.log" in index
    assert 'href="../cases/' not in index
    assert 'href="g-' in index
    assert "Open HTML report" in index
    assert "expected" in index.lower()
    assert "completed" in index
    assert "normal marker" in index
    assert "PASS" in index
    assert "](cases/" not in summary
    assert "C01\\-A0001" in summary
    assert "review/g-" in summary
    assert "Expected terminal" in summary
    assert "Observed terminal" in summary
    assert "Candidate status" in summary
    assert "Terminal authority: [batch.json](batch.json)" in summary
    assert "provisional candidate status" in index
    assert 'href="../batch.json"' in index
    assert "only terminal authority" in index
    assert '<img src=x onerror="case-xss">' not in summary
    for record in records:
        source = tmp_path / str(record["source_path"])
        opened = tmp_path / str(record["open_path"])
        assert source.is_file() and opened.is_file()
        assert not source.samefile(opened)
        assert source.stat().st_nlink == 1
        assert opened.stat().st_nlink == 1
        assert _sha256(source) == _sha256(opened)
        assert record["source_sha256"] == record["review_sha256"] == _sha256(source)
        assert len(str(record["open_path"])) <= smoke_review._MAX_OPEN_RELATIVE_CHARS


def test_workflow_report_short_copy_preserves_confined_relative_job_report_links(
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path)
    workflow = runtime / "workflow"
    job = workflow / "03_orca" / "01_ts_guess"
    job.mkdir(parents=True)
    report_source = workflow / "workflow_report.html"
    job_source = job / "job_report.html"
    report_source.write_text(
        '<!doctype html><a href="03_orca/01_ts_guess/job_report.html">TS report</a>',
        encoding="utf-8",
    )
    job_source.write_text("<!doctype html><h1>TS complete</h1>", encoding="utf-8")

    result = generate_review_packet(tmp_path, {"cases": [_case_manifest()]})

    records = _projection_records(result)
    report = _record_for(records, "/workflow/workflow_report.html")
    report_copy = tmp_path / str(report["open_path"])
    linked_copy = report_copy.parent / "03_orca" / "01_ts_guess" / "job_report.html"
    assert report_copy.read_bytes() == report_source.read_bytes()
    assert linked_copy.read_bytes() == job_source.read_bytes()
    assert report_source.stat().st_nlink == job_source.stat().st_nlink == 1
    assert not report_source.samefile(report_copy)
    dependencies = report["dependencies"]
    assert isinstance(dependencies, list) and len(dependencies) == 1
    assert dependencies[0]["open_path"] == str(linked_copy.relative_to(tmp_path))
    assert dependencies[0]["sha256"] == _sha256(job_source)
    assert len(str(report["open_path"])) <= smoke_review._MAX_OPEN_RELATIVE_CHARS
    assert all(
        smoke_review._windows_safe_component(part) for part in Path(str(report["open_path"])).parts
    )


def test_deep_runtime_features_use_normal_unc_safe_review_paths(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    deep = runtime.joinpath(*(f"deep-product-snapshot-component-{index:02d}" for index in range(8)))
    linked_job = deep / "03_orca" / "01_ts_guess"
    linked_job.mkdir(parents=True)
    (deep / "job_report.html").write_text("<h1>standalone</h1>", encoding="utf-8")
    (deep / "workflow_report.html").write_text(
        '<a href="03_orca/01_ts_guess/job_report.html">TS report</a>',
        encoding="utf-8",
    )
    (linked_job / "job_report.html").write_text("<h1>workflow job</h1>", encoding="utf-8")
    (deep / "si_block.md").write_text("# SI block", encoding="utf-8")
    (deep / "workflow_si.md").write_text("# Workflow SI", encoding="utf-8")
    (deep / "si_data.csv").write_text("name,value\nenergy,-40.1\n", encoding="utf-8")

    result = generate_review_packet(tmp_path, {"cases": [_case_manifest()]})

    records = _projection_records(result)
    required_names = {
        "job_report.html",
        "workflow_report.html",
        "si_block.md",
        "workflow_si.md",
        "si_data.csv",
    }
    for name in required_names:
        matches = [record for record in records if Path(str(record["source_path"])).name == name]
        assert matches and all(record["open_path"] is not None for record in matches)

    normal_unc_batch = (
        r"\\wsl.localhost\Ubuntu-20.04\home\daehyupsohn\orca_runs"
        r"\.orca_auto_smoke\batches\20260714-122413-f-abcdef"
    )
    source_unc_lengths = [
        len(normal_unc_batch + "\\" + str(record["source_path"]).replace("/", "\\"))
        for record in records
    ]
    open_unc_lengths = [
        len(normal_unc_batch + "\\" + str(record["open_path"]).replace("/", "\\"))
        for record in records
        if record["open_path"] is not None
    ]
    assert max(source_unc_lengths) >= 260
    assert max(open_unc_lengths) <= 240
    index = result.index_path.read_text(encoding="utf-8")
    assert 'href="../cases/' not in index


def test_unconfined_html_link_is_not_exposed_through_a_long_source_fallback(
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path)
    report = runtime / "workflow_report.html"
    report.write_text('<a href="../outside.html">unsafe</a>', encoding="utf-8")
    (tmp_path / "cases" / "orca-sp-success" / "outside.html").write_text(
        "outside",
        encoding="utf-8",
    )

    result = generate_review_packet(tmp_path, {"cases": [_case_manifest()]})

    record = _record_for(_projection_records(result), "/workflow_report.html")
    assert record["open_path"] is None
    assert "unconfined local reference" in str(record["issue"])
    index = result.index_path.read_text(encoding="utf-8")
    assert 'href="../cases/' not in index


def test_projection_budget_omission_is_visible_and_never_falls_back_to_runtime_link(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _runtime(tmp_path)
    (runtime / "large.out").write_text("1234", encoding="utf-8")
    monkeypatch.setattr(smoke_review, "_MAX_TOTAL_PROJECTION_BYTES", 1)

    result = generate_review_packet(tmp_path, {"cases": [_case_manifest()]})

    record = _projection_records(result)[0]
    assert result.openable_count == 0
    assert record["open_path"] is None
    assert "bounded projection limit" in str(record["issue"])
    assert 'href="../cases/' not in result.index_path.read_text(encoding="utf-8")


def test_partial_html_bundle_copy_is_removed_and_report_is_not_linked(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _runtime(tmp_path)
    workflow = runtime / "workflow"
    job = workflow / "03_orca" / "01_ts_guess"
    job.mkdir(parents=True)
    (workflow / "workflow_report.html").write_text(
        '<a href="03_orca/01_ts_guess/job_report.html">TS report</a>',
        encoding="utf-8",
    )
    (job / "job_report.html").write_text("<h1>TS complete</h1>", encoding="utf-8")

    copy_projection_file = smoke_review._copy_projection_file

    def fail_on_nested_dependency(
        root_fd: int,
        projection_root_fd: int,
        entry: smoke_review._ProjectionPlanEntry,
    ) -> str:
        if len(entry.destination_parts) > 1:
            raise smoke_review._ProjectionUnavailable("simulated dependency copy failure")
        return copy_projection_file(root_fd, projection_root_fd, entry)

    monkeypatch.setattr(smoke_review, "_copy_projection_file", fail_on_nested_dependency)
    result = generate_review_packet(tmp_path, {"cases": [_case_manifest()]})

    records = _projection_records(result)
    workflow_record = _record_for(records, "/workflow/workflow_report.html")
    assert workflow_record["open_path"] is None
    assert workflow_record["issue"] == "simulated dependency copy failure"
    artifact_number = str(workflow_record["artifact_id"]).split("-A", 1)[1]
    case_projection = result.artifact_manifest_path.parent / "open" / "c01-orca-sp-success"
    assert not (case_projection / f"a{artifact_number}").exists()
    assert not any(".tmp" in path.name for path in result.artifact_manifest_path.parent.rglob("*"))
    assert (workflow / "workflow_report.html").stat().st_nlink == 1
    assert (job / "job_report.html").stat().st_nlink == 1


def test_html_link_scan_digest_mismatch_blocks_report_copy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _runtime(tmp_path)
    report = runtime / "workflow_report.html"
    report.write_text('<a href="job_report.html">job</a>', encoding="utf-8")
    (runtime / "job_report.html").write_text("<h1>complete</h1>", encoding="utf-8")

    def mismatched_scan(
        root_fd: int,
        artifact: smoke_review._Artifact,
        *,
        limit: int,
    ) -> bytes:
        del root_fd, limit
        assert artifact.size_bytes is not None
        return b"x" * artifact.size_bytes

    monkeypatch.setattr(smoke_review, "_read_artifact_bytes", mismatched_scan)
    result = generate_review_packet(tmp_path, {"cases": [_case_manifest()]})

    record = _record_for(_projection_records(result), "/workflow_report.html")
    assert record["open_path"] is None
    assert record["issue"] == "HTML report changed while local links were scanned"


def test_source_mutation_after_earlier_copy_aborts_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _runtime(tmp_path)
    first = runtime / "a.log"
    first.write_text("AAAA", encoding="utf-8")
    (runtime / "b.log").write_text("BBBB", encoding="utf-8")
    copy_projection_file = smoke_review._copy_projection_file

    def mutate_earlier_source(
        root_fd: int,
        projection_root_fd: int,
        entry: smoke_review._ProjectionPlanEntry,
    ) -> str:
        digest = copy_projection_file(root_fd, projection_root_fd, entry)
        if entry.artifact.runtime_path == "b.log":
            first.write_text("ZZZZ", encoding="utf-8")
        return digest

    monkeypatch.setattr(smoke_review, "_copy_projection_file", mutate_earlier_source)
    with pytest.raises(ReviewPacketError, match="source .*changed before publication"):
        generate_review_packet(tmp_path, {"cases": [_case_manifest()]})

    assert not (tmp_path / "review" / "index.html").exists()


@pytest.mark.parametrize("target", ["artifact", "generation"])
def test_projection_directory_substitution_aborts_before_index_publish(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    target: str,
) -> None:
    runtime = _runtime(tmp_path)
    (runtime / "job_report.html").write_text("<h1>complete</h1>", encoding="utf-8")
    rename = smoke_review.os.rename
    swapped = False

    def substitute_before_rename(
        source: str,
        destination: str,
        *,
        src_dir_fd: int | None = None,
        dst_dir_fd: int | None = None,
    ) -> None:
        nonlocal swapped
        is_target = (
            target == "artifact" and source.startswith(".a") and destination.startswith("a")
        ) or (target == "generation" and source.startswith(".g-") and destination.startswith("g-"))
        if is_target and not swapped:
            held = source + ".held"
            rename(
                source,
                held,
                src_dir_fd=src_dir_fd,
                dst_dir_fd=src_dir_fd,
            )
            assert src_dir_fd is not None
            smoke_review.os.mkdir(source, mode=0o700, dir_fd=src_dir_fd)
            swapped = True
        rename(
            source,
            destination,
            src_dir_fd=src_dir_fd,
            dst_dir_fd=dst_dir_fd,
        )

    monkeypatch.setattr(smoke_review.os, "rename", substitute_before_rename)
    with pytest.raises(ReviewPacketError, match="directory identity changed"):
        generate_review_packet(tmp_path, {"cases": [_case_manifest()]})

    assert swapped is True
    assert not (tmp_path / "review" / "index.html").exists()


@pytest.mark.parametrize("target", ["summary.md", "index.html"])
def test_failed_regeneration_is_reported_and_recoverable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    target: str,
) -> None:
    # The packet is regenerable from the retained runtime: a failed
    # publication surfaces as an error (never a silent success), and the next
    # regeneration succeeds without any rollback machinery.
    runtime = _runtime(tmp_path)
    (runtime / "job_report.html").write_text("<h1>first</h1>", encoding="utf-8")
    first = generate_review_packet(tmp_path, {"cases": [_case_manifest()]})
    summary_before = first.summary_path.read_bytes()
    index_before = first.index_path.read_bytes()

    replace_file = smoke_review.os.replace

    def fail_surface_publication(
        source: str,
        destination: str,
        *,
        src_dir_fd: int | None = None,
        dst_dir_fd: int | None = None,
    ) -> None:
        if destination == target:
            raise OSError("injected surface publication failure")
        replace_file(
            source,
            destination,
            src_dir_fd=src_dir_fd,
            dst_dir_fd=dst_dir_fd,
        )

    with monkeypatch.context() as failure:
        failure.setattr(smoke_review.os, "replace", fail_surface_publication)
        with pytest.raises(OSError, match="injected surface publication failure"):
            generate_review_packet(tmp_path, {"cases": [_case_manifest()]})

    # No rollback: surfaces written before the failure keep their new content,
    # untouched surfaces keep the previous packet's content.
    if target == "summary.md":
        assert first.summary_path.read_bytes() == summary_before
        assert first.index_path.read_bytes() == index_before
    else:
        assert first.summary_path.read_bytes() != summary_before
        assert first.index_path.read_bytes() == index_before

    recovered = generate_review_packet(tmp_path, {"cases": [_case_manifest()]})
    generation = recovered.artifact_manifest_path.parent.name
    assert generation.encode() in recovered.index_path.read_bytes()


def test_regeneration_publishes_only_new_short_generation_in_index(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    source = runtime / "job_report.html"
    source.write_text("<h1>complete</h1>", encoding="utf-8")

    first = generate_review_packet(tmp_path, {"cases": [_case_manifest()]})
    first_generation = first.artifact_manifest_path.parent.name
    second = generate_review_packet(tmp_path, {"cases": [_case_manifest()]})
    second_generation = second.artifact_manifest_path.parent.name
    index = second.index_path.read_text(encoding="utf-8")

    assert first_generation != second_generation
    assert f'href="{second_generation}/open/' in index
    assert f'href="{first_generation}/open/' not in index
    assert source.stat().st_nlink == 1
    assert second.summary_path.stat().st_nlink == 1
    assert second.index_path.stat().st_nlink == 1
    assert not any(path.name.endswith(".bak") for path in tmp_path.rglob("*"))


def test_symlinks_are_listed_but_never_followed_or_linked(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    outside_file = tmp_path.parent / f"{tmp_path.name}-outside-secret.txt"
    outside_file.write_text("OUTSIDE-SYMLINK-SECRET", encoding="utf-8")
    outside_dir = tmp_path.parent / f"{tmp_path.name}-outside-dir"
    outside_dir.mkdir()
    (outside_dir / "nested.log").write_text("NESTED-OUTSIDE-SECRET", encoding="utf-8")
    (runtime / "leak.log").symlink_to(outside_file)
    (runtime / "leak-dir").symlink_to(outside_dir, target_is_directory=True)
    (runtime / "safe.log").write_text("safe output", encoding="utf-8")

    result = generate_review_packet(tmp_path, {"cases": [_case_manifest()]})

    index = result.index_path.read_text(encoding="utf-8")
    assert result.artifact_count == 3
    assert "OUTSIDE-SYMLINK-SECRET" not in index
    assert "NESTED-OUTSIDE-SECRET" not in index
    assert "symlink blocked (target not read)" in index
    assert 'href="../cases/orca-sp-success/runtime/leak.log"' not in index
    assert 'href="../cases/orca-sp-success/runtime/leak-dir"' not in index
    records = _projection_records(result)
    safe = _record_for(records, "/safe.log")
    assert safe["open_path"] is not None
    assert (tmp_path / str(safe["open_path"])).read_text(encoding="utf-8") == "safe output"
    assert all(
        record["open_path"] is None
        for record in records
        if str(record["source_path"]).endswith(("/leak.log", "/leak-dir"))
    )
    assert all(
        record["disposition"] == "blocked"
        for record in records
        if str(record["source_path"]).endswith(("/leak.log", "/leak-dir"))
    )


def test_pytest_current_alias_from_an_old_batch_is_a_blocked_symlink(
    tmp_path: Path,
) -> None:
    # The runner unlinks pytest convenience aliases after each scenario, so a
    # surviving alias only appears when reviewing a batch produced before the
    # unlink existed. It is shown as an ordinary blocked symlink; the numbered
    # target directory keeps its real artifacts.
    runtime = _runtime(tmp_path)
    pytest_root = runtime / "pytest"
    numbered = pytest_root / "test_retained_case0"
    numbered.mkdir(parents=True)
    (numbered / "job_report.html").write_text("<h1>complete</h1>", encoding="utf-8")
    (numbered / "job_state.json").write_text('{"status":"completed"}', encoding="utf-8")
    alias = pytest_root / "test_retained_casecurrent"
    alias.symlink_to(numbered, target_is_directory=True)

    result = generate_review_packet(tmp_path, {"cases": [_case_manifest()]})

    index = result.index_path.read_text(encoding="utf-8")
    payload = json.loads(result.artifact_manifest_path.read_text(encoding="utf-8"))
    records = payload["artifacts"]
    report_record = _record_for(records, "/pytest/test_retained_case0/job_report.html")
    state_record = _record_for(records, "/pytest/test_retained_case0/job_state.json")
    alias_record = _record_for(records, "/pytest/test_retained_casecurrent")

    assert result.artifact_count == 3
    assert result.openable_count == 2
    assert payload["blocked_count"] == 1
    assert "hidden_harness_alias_count" not in payload
    assert report_record["open_path"] is not None
    assert state_record["open_path"] is not None
    assert alias_record["disposition"] == "blocked"
    assert alias_record["open_path"] is None
    assert "alias_target_source_path" not in alias_record
    assert alias_record["issue"] == "symlink blocked (target not read)"
    assert "Blocked entries</span><strong>1</strong>" in index
    assert alias.name in index


def test_pytest_current_lookalike_with_external_target_remains_visible_blocked(
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path)
    pytest_root = runtime / "pytest"
    pytest_root.mkdir()
    (pytest_root / "test_retained_case0").mkdir()
    outside = tmp_path.parent / f"{tmp_path.name}-external-pytest" / "test_retained_case0"
    outside.mkdir(parents=True)
    alias = pytest_root / "test_retained_casecurrent"
    alias.symlink_to(outside, target_is_directory=True)

    result = generate_review_packet(tmp_path, {"cases": [_case_manifest()]})

    index = result.index_path.read_text(encoding="utf-8")
    alias_record = _record_for(
        _projection_records(result),
        "/pytest/test_retained_casecurrent",
    )
    assert alias_record["artifact_id"] == "C01-A0001"
    assert alias_record["disposition"] == "blocked"
    assert alias_record["open_path"] is None
    assert alias.name in index
    assert "symlink blocked (target not read)" in index


def test_relative_pytest_current_lookalike_remains_visible_blocked(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    pytest_root = runtime / "pytest"
    numbered = pytest_root / "test_retained_case0"
    numbered.mkdir(parents=True)
    alias = pytest_root / "test_retained_casecurrent"
    alias.symlink_to(numbered.name, target_is_directory=True)

    result = generate_review_packet(tmp_path, {"cases": [_case_manifest()]})

    index = result.index_path.read_text(encoding="utf-8")
    alias_record = _record_for(
        _projection_records(result),
        "/pytest/test_retained_casecurrent",
    )
    assert alias_record["disposition"] == "blocked"
    assert alias.name in index
    assert "symlink blocked (target not read)" in index


def test_hardlinks_are_listed_but_external_content_is_not_read_or_linked(
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path)
    outside_file = tmp_path.parent / f"{tmp_path.name}-private-research.txt"
    private_marker = "PRIVATE-RESEARCH-STRUCTURE-MARKER"
    outside_file.write_text(private_marker, encoding="utf-8")
    hardlink = runtime / "outside-hardlink.log"
    hardlink.hardlink_to(outside_file)

    result = generate_review_packet(tmp_path, {"cases": [_case_manifest()]})

    review_html = result.index_path.read_text(encoding="utf-8")
    assert result.artifact_count == 1
    assert result.openable_count == 0
    assert private_marker not in review_html
    assert "hard-linked file blocked (content not read)" in review_html
    assert 'href="../cases/orca-sp-success/runtime/outside-hardlink.log"' not in review_html
    assert _projection_records(result)[0]["open_path"] is None


def test_path_escape_is_reported_without_reading_outside_batch(tmp_path: Path) -> None:
    outside = tmp_path.parent / f"{tmp_path.name}-outside-runtime"
    outside.mkdir()
    (outside / "secret.log").write_text("PATH-ESCAPE-SECRET", encoding="utf-8")
    case = _case_manifest(case_dir=f"../{outside.name}", runtime_dir=".")

    result = generate_review_packet(tmp_path, {"cases": [case]})

    assert result.artifact_count == 0
    index = result.index_path.read_text(encoding="utf-8")
    assert "PATH-ESCAPE-SECRET" not in index
    assert "artifact discovery was blocked" in index
    assert "[invalid]" in index


def test_secret_preview_is_redacted_and_large_preview_is_bounded(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    secret = "obviously-fake-review-secret-value"
    (runtime / "config.yaml").write_text(f"bot_token: {secret}\n", encoding="utf-8")
    large = "PREVIEW-HEAD\n" + ("x" * (40 * 1024)) + "\nPREVIEW-TAIL"
    (runtime / "engine.out").write_text(large, encoding="utf-8")

    result = generate_review_packet(tmp_path, {"cases": [_case_manifest()]})

    index = result.index_path.read_text(encoding="utf-8")
    assert secret not in index
    assert "preview redacted: possible secret material" in index
    assert "PREVIEW-HEAD" in index
    assert "PREVIEW-TAIL" not in index
    assert "truncated to 32768 bytes" in index
    engine = _record_for(_projection_records(result), "/engine.out")
    assert engine["open_path"] is not None
    assert (tmp_path / str(engine["open_path"])).read_text(encoding="utf-8") == large
    assert len(index.encode("utf-8")) < 100_000


def test_unsafe_review_directory_is_rejected_and_summary_symlink_is_replaced(
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path)
    (runtime / "safe.log").write_text("safe", encoding="utf-8")
    outside_summary = tmp_path.parent / f"{tmp_path.name}-outside-summary.md"
    outside_summary.write_text("do not overwrite", encoding="utf-8")
    (tmp_path / "summary.md").symlink_to(outside_summary)

    result = generate_review_packet(tmp_path, {"cases": [_case_manifest()]})

    assert outside_summary.read_text(encoding="utf-8") == "do not overwrite"
    assert not result.summary_path.is_symlink()

    unsafe_batch = tmp_path / "unsafe-batch"
    unsafe_batch.mkdir()
    outside_review = tmp_path / "outside-review"
    outside_review.mkdir()
    (unsafe_batch / "review").symlink_to(outside_review, target_is_directory=True)
    with pytest.raises(ReviewPacketError, match="review output directory"):
        generate_review_packet(unsafe_batch, {"cases": []})
    assert not (outside_review / "index.html").exists()


def test_global_discovery_budget_counts_directories_and_reports_omissions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _runtime(tmp_path)
    for branch_index in range(5):
        leaf = runtime / f"branch-{branch_index}" / "nested"
        leaf.mkdir(parents=True)
        (leaf / "artifact.log").write_text(
            f"branch {branch_index}\n",
            encoding="utf-8",
        )
    monkeypatch.setattr(smoke_review, "_MAX_DISCOVERY_ENTRIES", 3)

    result = generate_review_packet(tmp_path, {"cases": [_case_manifest()]})

    review_html = result.index_path.read_text(encoding="utf-8")
    assert result.artifact_count < 5
    assert (
        "global discovery entry limit reached; remaining entries were not discovered" in review_html
    )
    assert "branch-4/nested/artifact.log" not in review_html


def test_batch_namespace_substitution_never_returns_forged_review_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _runtime(tmp_path)
    (runtime / "safe.log").write_text("safe", encoding="utf-8")
    held = tmp_path.parent / f"{tmp_path.name}-held"
    original_normalise = smoke_review._normalise_case_manifests

    def substitute_batch(
        batch_manifest: dict[str, object],
        case_manifests: object,
    ) -> list[dict[str, object]]:
        cases = original_normalise(batch_manifest, case_manifests)  # type: ignore[arg-type]
        tmp_path.rename(held)
        (tmp_path / "review").mkdir(parents=True)
        (tmp_path / "review" / "index.html").write_text("FORGED", encoding="utf-8")
        return [dict(case) for case in cases]

    monkeypatch.setattr(smoke_review, "_normalise_case_manifests", substitute_batch)

    with pytest.raises(ReviewPacketError, match="namespace identity changed"):
        generate_review_packet(tmp_path, {"cases": [_case_manifest()]})

    assert (tmp_path / "review" / "index.html").read_text(encoding="utf-8") == "FORGED"
    assert not (held / "review" / "index.html").exists()
