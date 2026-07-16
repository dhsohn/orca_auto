from orca_auto import __version__

from .admission.store import AdmissionSlot, activate_reserved_slot
from .config.schema import CommonResourceConfig, CommonRuntimeConfig, TelegramConfig
from .indexing import (
    JOB_LOCATION_INDEX_FILE_NAME,
    JOB_LOCATION_INDEX_LOCK_NAME,
    JobLocationIndexError,
    JobLocationRecord,
    get_job_location,
    list_job_locations,
    resolve_job_location,
    upsert_job_location,
)
from .messaging import TelegramSendResult, TelegramTransport, build_telegram_transport
from .queue.types import QueueEntry, QueueStatus

__all__ = [
    "__version__",
    "AdmissionSlot",
    "activate_reserved_slot",
    "CommonResourceConfig",
    "CommonRuntimeConfig",
    "JOB_LOCATION_INDEX_FILE_NAME",
    "JOB_LOCATION_INDEX_LOCK_NAME",
    "JobLocationIndexError",
    "JobLocationRecord",
    "get_job_location",
    "list_job_locations",
    "QueueEntry",
    "QueueStatus",
    "TelegramConfig",
    "TelegramSendResult",
    "TelegramTransport",
    "build_telegram_transport",
    "resolve_job_location",
    "upsert_job_location",
]
