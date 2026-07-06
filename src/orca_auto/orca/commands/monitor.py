"""monitor command — send discovery alerts from periodic filesystem scans."""

from __future__ import annotations

import logging
from typing import Any

from ..config import AppConfig, load_config
from ..dft.index import DFTIndex
from ..dft.monitor import DFTMonitor
from ..telegram_notifier import has_monitor_updates, notify_monitor_report
from ._helpers import _to_resolved_local

logger = logging.getLogger(__name__)

_STATE_FILE = ".dft_monitor_state.json"
_DFT_DB = "dft.db"


def _run_monitor(cfg: AppConfig) -> int:
    tg = cfg.telegram
    if not tg.enabled:
        logger.error("Telegram is not configured.")
        return 1

    allowed_root = _to_resolved_local(cfg.runtime.allowed_root)
    if not allowed_root.is_dir():
        logger.error("runs_root not found: %s", allowed_root)
        return 1

    state_file = str(allowed_root / _STATE_FILE)
    db_path = str(allowed_root / _DFT_DB)
    dft_index = DFTIndex()
    dft_index.initialize(db_path)
    monitor = DFTMonitor(
        dft_index=dft_index,
        kb_dirs=[str(allowed_root)],
        state_file=state_file,
    )
    report = monitor.scan()
    if not has_monitor_updates(report):
        logger.info("No new monitor discoveries to send.")
        return 0

    success = notify_monitor_report(tg, report)
    if not success:
        logger.error("Failed to send Telegram notification")
        return 1

    logger.info("Telegram notification sent successfully")
    return 0


def cmd_monitor(args: Any) -> int:
    cfg = load_config(args.config)
    return _run_monitor(cfg)
