"""Dotted-leader settings rows shared by the NEB and IRC report parsers.

ORCA prints module settings as ``label .... value`` rows with a run of three
or more leader dots. Labels may contain periods (``Max. no of cycles``) and
the leader may butt directly against the label (``...change.... 2.000 mEh``),
so the matcher anchors on the leader run instead of a label character class.
"""

from __future__ import annotations

import html
import re
from collections.abc import Sequence
from dataclasses import dataclass

_DOTTED_SETTING_RE = re.compile(r"^\s*([^\s.].*?)\s*\.{3,}\s+(\S.*?)\s*$")


@dataclass(frozen=True)
class ReportSetting:
    label: str
    value: str


def match_dotted_setting(line: str) -> ReportSetting | None:
    match = _DOTTED_SETTING_RE.match(line)
    if match is None:
        return None
    return ReportSetting(
        label=" ".join(match.group(1).split()),
        value=" ".join(match.group(2).split()),
    )


def settings_table_html(settings: Sequence[ReportSetting]) -> str:
    if not settings:
        return ""
    rows = [
        f"<tr><td>{html.escape(setting.label)}</td><td>{html.escape(setting.value)}</td></tr>"
        for setting in settings
    ]
    return (
        "<table><thead><tr><th>Setting</th><th>Value</th></tr></thead>"
        f"<tbody>{''.join(rows)}</tbody></table>"
    )


__all__ = [
    "ReportSetting",
    "match_dotted_setting",
    "settings_table_html",
]
