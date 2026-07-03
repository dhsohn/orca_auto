"""Shared HTML/SVG rendering primitives for job reports.

Everything here is calculation-type agnostic: page skeleton, badges, metric
cards, and a dependency-free SVG line chart. Type-specific report modules
(``scants``, ``opt``) compose these into full pages.
"""

from __future__ import annotations

import html
from dataclasses import dataclass

KCAL_PER_HARTREE = 627.5094740631

REASON_NOTES = {
    "ts_criteria_met": "TS criteria met: exactly one imaginary mode at the converged structure.",
    "ts_criteria_failed": (
        "The optimization converged, but the structure does not satisfy the TS criteria "
        "(imaginary mode count is not exactly one)."
    ),
    "scan_profile_no_barrier": (
        "The assembled forward profile is monotonic: no interior maximum above the noise "
        "threshold exists along the scanned coordinate. Reconsider the scan coordinate or "
        "the mechanistic hypothesis."
    ),
    "scants_recipes_exhausted": (
        "Every ScanTS retry recipe (continuation, endpoint completion, reverse scan) "
        "was used without meeting the TS criteria."
    ),
    "geometry_zero_distance": (
        "ORCA aborted after constructing a geometry with two atoms at zero distance; "
        "this typically happens inside the post-scan TS refinement."
    ),
    "geometry_not_converged": (
        "The geometry optimization did not converge within the iteration limit."
    ),
}

_CSS = """
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,'Segoe UI',system-ui,Roboto,'Helvetica Neue',Arial,sans-serif;
 background:#eef0f3;color:#242a33;line-height:1.6;font-size:15px}
.container{max-width:920px;margin:0 auto;padding:36px 40px 56px;background:#fff;min-height:100vh}
h1{font-size:22px;font-weight:600;margin-bottom:2px}
h2{font-size:16px;font-weight:600;margin:28px 0 10px;padding-top:14px;border-top:1px solid #e4e7ec}
.meta{color:#5b6472;font-size:13px;margin-bottom:14px}
.badges{display:flex;gap:6px;flex-wrap:wrap;margin:8px 0 4px}
.badge{font-size:12px;padding:3px 10px;border-radius:5px;font-weight:600}
.badge-ok{background:#e5f3e8;color:#1e6b34}
.badge-bad{background:#fbe7e7;color:#9d2626}
.badge-warn{background:#fdf3dd;color:#8a5a10}
.badge-muted{background:#eceef2;color:#4c5560}
.verdict{background:#f6f7f9;border-left:3px solid #9aa3b0;padding:10px 14px;font-size:14px;
 margin:10px 0 4px;color:#3d4451}
.metrics{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:12px;
 margin:14px 0 6px}
.metric{background:#f6f7f9;border-radius:8px;padding:12px 14px}
.metric-label{font-size:12px;color:#69707c}
.metric-value{font-size:22px;font-weight:600}
.metric-value small{font-size:12px;font-weight:400;color:#69707c}
.metric-note{font-size:12px;color:#69707c}
table{width:100%;border-collapse:collapse;font-size:13.5px}
th{color:#69707c;text-align:left;font-weight:600;padding:6px 10px;border-bottom:1px solid #e4e7ec}
td{padding:7px 10px;border-bottom:1px solid #eef0f3;vertical-align:top}
td .sub,.mode .sub{font-size:12px;color:#69707c}
td.ok{color:#1e6b34}td.warn{color:#8a5a10}td.bad{color:#9d2626}
.mode{background:#f6f7f9;border-radius:8px;padding:12px 14px;margin-bottom:10px}
.muted{color:#69707c;font-size:14px}
footer{margin-top:30px;font-size:12px;color:#8a919c}
code{background:#f2f3f6;padding:1px 6px;border-radius:4px;font-size:13px}
"""


@dataclass(frozen=True)
class ChartSeries:
    label: str
    color: str
    dash: str
    points: tuple[tuple[float, float], ...]


@dataclass(frozen=True)
class ReportPage:
    title: str
    badges: tuple[tuple[str, str], ...]
    meta_html: str
    verdict_html: str
    metrics_html: str
    sections: tuple[tuple[str, str], ...]
    footer_html: str


def badge(text: str, kind: str) -> str:
    return f'<span class="badge badge-{kind}">{html.escape(text)}</span>'


def status_badge_kind(status: str) -> str:
    return {"completed": "ok", "failed": "bad"}.get(status, "muted")


def metric_card(label: str, value_html: str, note: str) -> str:
    note_html = f'<div class="metric-note">{html.escape(note)}</div>' if note else ""
    return (
        '<div class="metric">'
        f'<div class="metric-label">{html.escape(label)}</div>'
        f'<div class="metric-value">{value_html}</div>'
        f"{note_html}</div>"
    )


def verdict_note(reason: str, fallback_reason: str = "") -> str:
    note = REASON_NOTES.get(reason) or REASON_NOTES.get(fallback_reason)
    if note is None:
        return ""
    return f'<p class="verdict">{html.escape(note)}</p>'


def _chart_ticks(low: float, high: float, count: int) -> list[float]:
    if count < 2 or high <= low:
        return [low]
    step = (high - low) / (count - 1)
    return [low + step * idx for idx in range(count)]


def line_chart_svg(
    series: tuple[ChartSeries, ...],
    *,
    x_label: str,
    y_label: str,
    x_tick_fmt: str = ".2f",
    y_tick_fmt: str = ".1f",
) -> str:
    """Dependency-free SVG line chart; empty string when under two points."""
    all_points = [point for entry in series for point in entry.points]
    if len(all_points) < 2:
        return ""

    x_values = [x for x, _ in all_points]
    y_values = [y for _, y in all_points]
    x_low, x_high = min(x_values), max(x_values)
    x_pad = (x_high - x_low) * 0.03 or 0.05
    x_low, x_high = x_low - x_pad, x_high + x_pad
    y_low = min(0.0, min(y_values))
    y_high = max(max(y_values) * 1.08, y_low + 1e-6)

    width, height = 760, 320
    left, right, top, bottom = 62, 18, 16, 42
    plot_w, plot_h = width - left - right, height - top - bottom

    def sx(x: float) -> float:
        return left + (x - x_low) / (x_high - x_low) * plot_w

    def sy(y: float) -> float:
        return top + plot_h - (y - y_low) / (y_high - y_low) * plot_h

    parts = [
        f'<svg viewBox="0 0 {width} {height}" role="img" '
        'style="width:100%;max-width:820px;display:block">',
        f'<line x1="{left}" y1="{top + plot_h}" x2="{left + plot_w}" y2="{top + plot_h}" '
        'stroke="#c8ccd4" stroke-width="1"/>',
        f'<line x1="{left}" y1="{top}" x2="{left}" y2="{top + plot_h}" '
        'stroke="#c8ccd4" stroke-width="1"/>',
    ]
    for tick in _chart_ticks(x_low, x_high, 6):
        parts.append(
            f'<text x="{sx(tick):.1f}" y="{top + plot_h + 18}" text-anchor="middle" '
            f'font-size="11" fill="#69707c">{tick:{x_tick_fmt}}</text>'
        )
    for tick in _chart_ticks(y_low, y_high, 5):
        parts.append(
            f'<text x="{left - 8}" y="{sy(tick):.1f}" text-anchor="end" '
            f'dominant-baseline="middle" font-size="11" fill="#69707c">{tick:{y_tick_fmt}}</text>'
        )
    parts.append(
        f'<text x="{left + plot_w / 2:.1f}" y="{height - 6}" text-anchor="middle" '
        f'font-size="12" fill="#3d4451">{html.escape(x_label)}</text>'
    )
    parts.append(
        f'<text x="14" y="{top + plot_h / 2:.1f}" text-anchor="middle" font-size="12" '
        f'fill="#3d4451" transform="rotate(-90 14 {top + plot_h / 2:.1f})">'
        f"{html.escape(y_label)}</text>"
    )

    for entry in series:
        dash_attr = f' stroke-dasharray="{entry.dash}"' if entry.dash else ""
        polyline = " ".join(f"{sx(x):.1f},{sy(y):.1f}" for x, y in entry.points)
        parts.append(
            f'<polyline points="{polyline}" fill="none" stroke="{entry.color}" '
            f'stroke-width="2"{dash_attr}/>'
        )
        parts.extend(
            f'<circle cx="{sx(x):.1f}" cy="{sy(y):.1f}" r="2.4" fill="{entry.color}"/>'
            for x, y in entry.points
        )

    legend_y = top + 8
    for entry in series:
        if not entry.label:
            continue
        dash_attr = f' stroke-dasharray="{entry.dash}"' if entry.dash else ""
        parts.append(
            f'<line x1="{left + 14}" y1="{legend_y}" x2="{left + 42}" y2="{legend_y}" '
            f'stroke="{entry.color}" stroke-width="2"{dash_attr}/>'
        )
        parts.append(
            f'<text x="{left + 48}" y="{legend_y + 4}" font-size="11" fill="#3d4451">'
            f"{html.escape(entry.label)}</text>"
        )
        legend_y += 18
    parts.append("</svg>")
    return "".join(parts)


def render_page(page: ReportPage) -> str:
    badges_html = "".join(badge(text, kind) for text, kind in page.badges)
    sections_html = "".join(
        f"<h2>{html.escape(heading)}</h2>\n{body}\n" for heading, body in page.sections
    )
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(page.title)}</title>
<style>{_CSS}</style>
</head>
<body>
<div class="container">
<h1>{html.escape(page.title)}</h1>
<div class="badges">{badges_html}</div>
<div class="meta">{page.meta_html}</div>
{page.verdict_html}
<div class="metrics">{page.metrics_html}</div>
{sections_html}<footer>{page.footer_html}</footer>
</div>
</body>
</html>
"""
