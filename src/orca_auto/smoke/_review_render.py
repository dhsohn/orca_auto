"""Render the smoke review packet surfaces (summary.md and review/index.html).

Pure text generation over the case reviews that discovery produced: no
filesystem access, no error policy, no imports from the review module at
runtime. The batch metadata rows arrive pre-normalized as ``batch_fields``
so field sanitization (including secret redaction) stays with discovery.
"""

from __future__ import annotations

import html
from collections.abc import Sequence
from pathlib import PurePosixPath
from typing import TYPE_CHECKING
from urllib.parse import quote

if TYPE_CHECKING:
    from .review import _Artifact, _CaseReview


def render_summary(
    batch_fields: Sequence[tuple[str, str]],
    cases: list[_CaseReview],
    artifact_count: int,
    openable_count: int,
    blocked_count: int,
) -> str:
    artifact_manifest = _projection_manifest_batch_path(cases)
    lines = [
        "# Smoke review packet",
        "",
        "Open the offline HTML review surface: [review/index.html](review/index.html)",
        "Terminal authority: [batch.json](batch.json). The status shown in this review packet "
        "is provisional until that manifest records a terminal status.",
        f"Artifact provenance map: [{artifact_manifest}]({artifact_manifest})",
        "",
    ]
    for label, value in batch_fields:
        lines.append(f"- {_md_text(label)}: `{_md_code(value)}`")
    lines.extend(
        [
            f"- Cases: `{len(cases)}`",
            f"- Discovered entries: `{artifact_count}`",
            f"- Files to review: `{openable_count}`",
            f"- Blocked entries: `{blocked_count}`",
            "",
            "Expected simulation failures count as smoke PASS only when the declared verdict says so. "
            "Expected, observed, and verdict are shown separately.",
            "",
            "| Case | Surface | Scenario | Expected | Observed | Verdict | Review issues |",
            "| --- | --- | --- | --- | --- | --- | ---: |",
        ]
    )
    for case in cases:
        values = [
            case.fields["case_id"],
            case.fields["surface"],
            case.fields["scenario"],
            case.fields["expected_terminal"],
            case.fields["observed_terminal"],
            case.fields["verdict"],
            str(len(case.issues)),
        ]
        lines.append("| " + " | ".join(_md_table(value) for value in values) + " |")

    for case in cases:
        lines.extend(
            [
                "",
                f"## {_md_text(case.fields['case_id'])}",
                "",
                f"- Review files: `{_md_code(case.open_path)}`",
                f"- Runtime provenance: `{_md_code(artifact_manifest)}`",
                f"- Expected terminal: `{_md_code(case.fields['expected_terminal'])}`",
                f"- Observed terminal: `{_md_code(case.fields['observed_terminal'])}`",
                f"- Verdict: `{_md_code(case.fields['verdict'])}`",
            ]
        )
        for issue in case.issues:
            lines.append(f"- Review issue: {_md_text(issue)}")
        lines.extend(["", "### Artifacts", ""])
        if not case.artifacts:
            lines.append("No reviewable runtime artifacts were discovered.")
            continue
        for artifact in case.artifacts:
            details = [artifact.kind]
            if artifact.size_bytes is not None:
                details.append(_format_size(artifact.size_bytes))
            if artifact.sha256 is not None:
                details.append(f"sha256 `{artifact.sha256}`")
            if artifact.issue:
                details.append(artifact.issue)
            label = _md_table(_artifact_label(artifact))
            if artifact.open_path is not None:
                target = quote(artifact.open_path, safe="/")
                lines.append(f"- [{label}]({target}) — " + "; ".join(_md_text(x) for x in details))
            else:
                lines.append(
                    f"- `{_md_code(_artifact_label(artifact))}` — "
                    + "; ".join(_md_text(x) for x in details)
                )
    return "\n".join(lines) + "\n"


def render_index(
    batch_fields: Sequence[tuple[str, str]],
    cases: list[_CaseReview],
    artifact_count: int,
    openable_count: int,
    blocked_count: int,
) -> str:
    batch_metadata = "".join(
        '<div class="metric"><span>'
        + html.escape(label)
        + "</span><strong>"
        + html.escape(value)
        + "</strong></div>"
        for label, value in batch_fields
    )
    batch_metadata += (
        f'<div class="metric"><span>Cases</span><strong>{len(cases)}</strong></div>'
        f'<div class="metric"><span>Entries found</span><strong>{artifact_count}</strong></div>'
        f'<div class="metric"><span>Files to review</span><strong>{openable_count}</strong></div>'
        f'<div class="metric"><span>Blocked entries</span><strong>{blocked_count}</strong></div>'
    )
    case_sections = "\n".join(
        _render_case_html(case, index=index) for index, case in enumerate(cases, start=1)
    )
    if not case_sections:
        case_sections = '<section class="empty">No case manifests were supplied.</section>'

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; img-src 'none'; frame-src 'none'; object-src 'none'; base-uri 'none'; form-action 'none'">
<title>orca_auto smoke review packet</title>
<style>
:root {{ color-scheme: light dark; --bg:#f4f7fb; --panel:#fff; --ink:#162033; --muted:#607086; --line:#dce3ec; --accent:#135e96; --good:#167348; --bad:#b42318; --warn:#9a6700; --code:#eef3f8; }}
@media (prefers-color-scheme:dark) {{ :root {{ --bg:#10151d; --panel:#171e28; --ink:#eef4fb; --muted:#aab8c8; --line:#303b49; --accent:#72b7e8; --good:#56d39b; --bad:#ff8a82; --warn:#f6c85f; --code:#111822; }} }}
* {{ box-sizing:border-box; }}
body {{ margin:0; background:var(--bg); color:var(--ink); font:15px/1.55 system-ui,-apple-system,"Segoe UI",sans-serif; }}
main {{ width:min(1180px,calc(100% - 32px)); margin:32px auto 72px; }}
.hero,.case,.artifact,.empty {{ background:var(--panel); border:1px solid var(--line); border-radius:14px; box-shadow:0 8px 24px rgba(20,34,54,.06); }}
.hero {{ padding:28px; }} h1,h2,h3 {{ line-height:1.2; }} h1 {{ margin:0 0 8px; font-size:28px; }}
.lede,.muted {{ color:var(--muted); }} .metrics {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(150px,1fr)); gap:10px; margin-top:20px; }}
.metric {{ border:1px solid var(--line); border-radius:10px; padding:10px 12px; min-width:0; }} .metric span {{ display:block; color:var(--muted); font-size:12px; text-transform:uppercase; letter-spacing:.04em; }} .metric strong {{ display:block; overflow-wrap:anywhere; }}
.case {{ margin-top:22px; padding:24px; }} .case-head {{ display:flex; align-items:flex-start; justify-content:space-between; gap:16px; }} .case-head h2 {{ margin:0; overflow-wrap:anywhere; }}
.badge {{ display:inline-block; border:1px solid currentColor; border-radius:999px; padding:3px 10px; font-size:12px; font-weight:700; text-transform:uppercase; }} .pass {{ color:var(--good); }} .fail {{ color:var(--bad); }} .warn {{ color:var(--warn); }} .neutral {{ color:var(--muted); }}
.contract {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(190px,1fr)); gap:10px; margin:18px 0; }} .contract div {{ background:var(--code); border-radius:9px; padding:10px 12px; overflow-wrap:anywhere; }} .contract span {{ display:block; color:var(--muted); font-size:12px; }}
.issues {{ border-left:4px solid var(--warn); background:color-mix(in srgb,var(--warn) 8%,transparent); padding:10px 14px; }} .issues li {{ margin:4px 0; }}
.artifact {{ margin-top:12px; padding:15px; box-shadow:none; }} .artifact-head {{ display:flex; align-items:center; justify-content:space-between; gap:12px; }} .artifact-title {{ min-width:0; }} .artifact-title code {{ overflow-wrap:anywhere; }}
.meta {{ color:var(--muted); font-size:13px; margin-top:4px; overflow-wrap:anywhere; }} a.open {{ color:var(--accent); border:1px solid currentColor; border-radius:8px; padding:6px 10px; text-decoration:none; white-space:nowrap; }} a.open:hover {{ text-decoration:underline; }}
.provenance {{ margin-top:10px; }} .provenance div {{ margin-top:7px; color:var(--muted); font-size:12px; overflow-wrap:anywhere; }}
details {{ margin-top:10px; }} summary {{ color:var(--accent); cursor:pointer; }} pre {{ max-height:420px; overflow:auto; white-space:pre-wrap; overflow-wrap:anywhere; background:var(--code); border-radius:9px; padding:12px; font:12px/1.45 ui-monospace,SFMono-Regular,Consolas,monospace; }} code {{ font-family:ui-monospace,SFMono-Regular,Consolas,monospace; }}
.blocked {{ border-color:var(--warn); }} .empty {{ padding:24px; margin-top:22px; }}
@media (max-width:640px) {{ main {{ width:min(100% - 18px,1180px); margin-top:10px; }} .hero,.case {{ padding:17px; }} .case-head,.artifact-head {{ display:block; }} a.open {{ display:inline-block; margin-top:10px; }} }}
</style>
</head>
<body><main>
<section class="hero">
<h1>Smoke review packet</h1>
<p class="lede"><strong>This packet shows a provisional candidate status.</strong> The only terminal authority is <a class="open" href="../batch.json">batch.json</a>. Expected and observed terminal states are deliberately separate. Open links use bounded short-path copies; full runtime paths and source digests remain under Provenance. Pytest "current" shortcuts are summarized instead of shown as artifacts.</p>
<div class="metrics">{batch_metadata}</div>
</section>
{case_sections}
</main></body></html>
"""


def _render_case_html(case: _CaseReview, *, index: int) -> str:
    fields = case.fields
    badge_class = _verdict_class(fields["verdict"])
    issues = ""
    if case.issues:
        issue_items = "".join(f"<li>{html.escape(issue)}</li>" for issue in case.issues)
        issues = f'<div class="issues"><strong>Review issues</strong><ul>{issue_items}</ul></div>'
    artifacts = "".join(_render_artifact_html(artifact) for artifact in case.artifacts)
    if not artifacts:
        artifacts = '<p class="muted">No reviewable runtime artifacts were discovered.</p>'
    return f"""<section class="case" id="case-{index}">
<div class="case-head"><div><div class="muted">{html.escape(fields["surface"])} · {html.escape(fields["scenario"])}</div><h2>{html.escape(fields["case_id"])}</h2></div><span class="badge {badge_class}">{html.escape(fields["verdict"])}</span></div>
<div class="contract">
<div><span>Expected terminal</span>{html.escape(fields["expected_terminal"])}</div>
<div><span>Observed terminal</span>{html.escape(fields["observed_terminal"])}</div>
<div><span>Review files</span>{html.escape(case.open_path)}</div>
<div><span>Review status</span>{html.escape(fields["review_status"])}</div>
</div>
<details class="provenance"><summary>Case provenance</summary><div><strong>Runtime source:</strong> <code>{html.escape(case.runtime_path)}</code></div></details>
{issues}

<h3>Artifacts</h3>
{artifacts}
</section>"""


def _render_artifact_html(artifact: _Artifact) -> str:
    classes = "artifact blocked" if artifact.open_path is None else "artifact"
    link = ""
    if artifact.open_path is not None:
        href = _index_href(artifact.open_path)
        link_label = "Open HTML report" if artifact.kind == "HTML report" else "Open artifact"
        link = (
            f'<a class="open" href="{html.escape(href, quote=True)}" '
            f'target="_blank" rel="noopener noreferrer">{link_label}</a>'
        )
    metadata = [artifact.kind]
    if artifact.size_bytes is not None:
        metadata.append(_format_size(artifact.size_bytes))
    if artifact.issue is not None:
        metadata.append(artifact.issue)
    preview = ""
    if artifact.preview is not None:
        preview = (
            "<details><summary>Preview — "
            + html.escape(artifact.preview_note)
            + "</summary><pre>"
            + html.escape(artifact.preview)
            + "</pre></details>"
        )
    else:
        preview = f'<div class="meta">{html.escape(artifact.preview_note)}</div>'
    metadata_html = html.escape(" · ".join(metadata))
    source_digest = artifact.sha256 or "unavailable"
    review_digest = artifact.review_sha256 or "unavailable"
    provenance = f"""<details class="provenance"><summary>Provenance</summary>
<div><strong>Runtime source:</strong> <code>{html.escape(artifact.batch_path)}</code></div>
<div><strong>Source SHA-256:</strong> <code>{html.escape(source_digest)}</code></div>
<div><strong>Review SHA-256:</strong> <code>{html.escape(review_digest)}</code></div>
</details>"""
    return f"""<article class="{classes}">
<div class="artifact-head"><div class="artifact-title"><code>{html.escape(_artifact_label(artifact))}</code><div class="meta">{metadata_html}</div></div>{link}</div>
{provenance}
{preview}
</article>"""


def _projection_manifest_batch_path(cases: Sequence[_CaseReview]) -> str:
    for case in cases:
        parts = PurePosixPath(case.open_path).parts
        if len(parts) >= 2 and parts[0] == "review":
            return PurePosixPath(parts[0], parts[1], "artifacts.json").as_posix()
    return "review/artifacts.json"


def _artifact_label(artifact: _Artifact) -> str:
    name = PurePosixPath(artifact.runtime_path).name
    if len(name) > 80:
        name = name[:76] + "…"
    identifier = artifact.artifact_id or "artifact"
    return f"{identifier} · {name}"


def _index_href(batch_path: str) -> str:
    quoted = quote(batch_path, safe="/")
    prefix = "review/"
    return quoted[len(prefix) :] if quoted.startswith(prefix) else "../" + quoted


def _verdict_class(verdict: str) -> str:
    lowered = verdict.strip().lower()
    if lowered in {"pass", "passed", "success", "succeeded"}:
        return "pass"
    if lowered in {"fail", "failed", "failure", "error", "invalid"}:
        return "fail"
    if lowered in {"warn", "warning", "review"}:
        return "warn"
    return "neutral"


def _format_size(size_bytes: int) -> str:
    units = ("B", "KiB", "MiB", "GiB", "TiB")
    value = float(size_bytes)
    for unit in units:
        if value < 1024 or unit == units[-1]:
            if unit == "B":
                return f"{size_bytes} B"
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{size_bytes} B"


def _md_text(text: str) -> str:
    escaped = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    for char in ("\\", "`", "*", "_", "{", "}", "[", "]", "(", ")", "#", "+", "-", ".", "!", "|"):
        escaped = escaped.replace(char, "\\" + char)
    return escaped


def _md_table(text: str) -> str:
    return _md_text(text).replace("\n", " ").replace("\r", " ")


def _md_code(text: str) -> str:
    return text.replace("`", "ˋ").replace("\n", " ").replace("\r", " ").replace("\t", " ")
