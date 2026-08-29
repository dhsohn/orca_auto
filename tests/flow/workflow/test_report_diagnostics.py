from __future__ import annotations

import os
from pathlib import Path

import pytest

from orca_auto.core.artifacts import RUN_REPORT_HTML_FILE, RUN_REPORT_JSON_FILE
from orca_auto.flow.workflow import report_collection, report_diagnostics
from orca_auto.flow.workflow.report_diagnostics import report_html_href


def test_report_collection_does_not_forward_diagnostic_owner() -> None:
    for name in report_diagnostics.__all__:
        assert not hasattr(report_collection, name), name


def test_report_html_href_accepts_only_direct_single_link_file(tmp_path: Path) -> None:
    generation = tmp_path / "generation"
    generation.mkdir()
    report_path = generation / RUN_REPORT_JSON_FILE
    html_path = generation / RUN_REPORT_HTML_FILE
    html_path.write_text("<html>report</html>\n", encoding="utf-8")

    assert report_html_href(report_path, tmp_path) == f"generation/{RUN_REPORT_HTML_FILE}"


@pytest.mark.parametrize("link_kind", ("symlink", "hardlink"))
def test_report_html_href_rejects_linked_file(
    tmp_path: Path,
    link_kind: str,
) -> None:
    generation = tmp_path / "generation"
    generation.mkdir()
    report_path = generation / RUN_REPORT_JSON_FILE
    target = generation / "report-target.html"
    target.write_text("<html>report</html>\n", encoding="utf-8")
    html_path = generation / RUN_REPORT_HTML_FILE
    if link_kind == "symlink":
        html_path.symlink_to(target.name)
    else:
        os.link(target, html_path)

    assert report_html_href(report_path, tmp_path) is None
