"""Single source of truth for status icons across every surface.

The unified queue table (``activity_rendering``), the messenger bot, ORCA run
snapshots (``orca_auto.orca.run_snapshot``), and ORCA lifecycle notifications
(``orca_auto.orca.notifications``) all route through
:func:`activity_status_icon` so a given status always looks the same everywhere.
"""

from __future__ import annotations

from orca_auto.core import statuses as _s

_ACTIVITY_STATUS_ICONS = {
    _s.STATUS_CREATED: "🆕",
    _s.STATUS_PLANNED: "⏳",
    _s.STATUS_PENDING: "⏳",
    _s.STATUS_QUEUED: "⏳",
    _s.STATUS_SUBMITTED: "📤",
    _s.STATUS_RUNNING: "▶",
    _s.STATUS_RETRYING: "🔄",
    _s.STATUS_CANCEL_REQUESTED: "⏹",
    _s.STATUS_COMPLETED: "✅",
    _s.STATUS_FAILED: "❌",
    _s.STATUS_CANCEL_FAILED: "❌",
    _s.STATUS_SUBMISSION_FAILED: "❌",
    _s.STATUS_CANCELLED: "⛔",
    # ORCA run results can report a bare "error" status (a failure variant).
    "error": "❌",
}

_FALLBACK_ICON = "•"


def activity_status_icon(status: object) -> str:
    """Return the canonical icon for a workflow/queue activity status."""

    normalized = str(status).strip().lower() if status is not None else ""
    return _ACTIVITY_STATUS_ICONS.get(normalized, _FALLBACK_ICON)


__all__ = ["activity_status_icon"]
