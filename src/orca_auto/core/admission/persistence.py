from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from ..utils.persistence import atomic_write_json, load_json_list_file, resolve_root_path
from .records import AdmissionSlot, slot_from_dict, slot_to_dict

ADMISSION_DIR_NAME = ".admission"
ADMISSION_FILE_NAME = "admission_slots.json"
ADMISSION_LOCK_NAME = "admission.lock"


class AdmissionStoreCorruptError(RuntimeError):
    """Raised when the admission slot file cannot be safely loaded."""


def admission_dir(runs_root: str | Path) -> Path:
    """The one admission store of an installation: ``<runs_root>/.admission``."""
    return resolve_root_path(runs_root) / ADMISSION_DIR_NAME


def admission_path(root: Path) -> Path:
    return root / ADMISSION_FILE_NAME


def admission_lock_path(root: Path) -> Path:
    return root / ADMISSION_LOCK_NAME


def load_slots(root: str | Path) -> list[AdmissionSlot]:
    resolved_root = resolve_root_path(root)
    raw = load_json_list_file(
        admission_path(resolved_root),
        corrupt_error=AdmissionStoreCorruptError,
        description="Admission slot file",
    )
    slots: list[AdmissionSlot] = []
    seen_tokens: set[str] = set()
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            raise AdmissionStoreCorruptError(
                f"Admission slot file item {index} must contain a JSON object: "
                f"{admission_path(resolved_root)}"
            )
        raw_token = item.get("token")
        if not isinstance(raw_token, str) or not raw_token.strip():
            raise AdmissionStoreCorruptError(
                "Admission slot file contains a blank or non-string token: "
                f"{admission_path(resolved_root)}"
            )
        slot = slot_from_dict(item)
        if slot.token in seen_tokens:
            raise AdmissionStoreCorruptError(
                f"Admission slot file contains duplicate token {slot.token!r}: "
                f"{admission_path(resolved_root)}"
            )
        seen_tokens.add(slot.token)
        slots.append(slot)
    return slots


def save_slots(root: str | Path, slots: Sequence[AdmissionSlot]) -> None:
    resolved_root = resolve_root_path(root)
    serialized: list[dict[str, object]] = []
    seen_tokens: set[str] = set()
    for index, slot in enumerate(slots):
        item = slot_to_dict(slot)
        raw_token = item.get("token")
        if not isinstance(raw_token, str) or not raw_token.strip():
            raise AdmissionStoreCorruptError(f"Admission slot at index {index} has a blank token")
        token = raw_token.strip()
        if token in seen_tokens:
            raise AdmissionStoreCorruptError(f"Admission slot list has duplicate token {token!r}")
        seen_tokens.add(token)
        serialized.append(item)
    atomic_write_json(
        admission_path(resolved_root),
        serialized,
        ensure_ascii=True,
        indent=2,
    )
