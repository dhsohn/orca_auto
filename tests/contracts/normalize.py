"""Normalize run-dependent values for golden comparison, and compare against goldens.

``Normalizer`` keeps key order and list order. It replaces run-dependent values
with typed placeholders: known path prefixes, timestamps, process identities,
file identities, minted tokens and sha256 digests. Minted tokens and digests
keep their identity: the same value always maps to the same numbered
placeholder, so equal values stay visibly equal across files. Timestamp
placeholders keep the shape (separator, fraction digits, zone suffix) and
key placeholders keep the JSON type, so format and type changes still show.

``assert_golden`` compares a normalized value with ``golden/<name>`` and
``assert_pin`` with ``pins/<name>``, the rule characterization tables.
``ORCA_AUTO_REGEN_GOLDENS=1`` rewrites the file instead; it is off by default.
"""

from __future__ import annotations

import difflib
import json
import os
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

GOLDEN_DIR = Path(__file__).resolve().with_name("golden")
PINS_DIR = Path(__file__).resolve().with_name("pins")
REGEN_ENV_VAR = "ORCA_AUTO_REGEN_GOLDENS"

_TIMESTAMP_RE = re.compile(
    r"\d{4}-\d{2}-\d{2}(?P<sep>[T ])\d{2}:\d{2}:\d{2}(?P<fraction>[.,]\d+)?"
    r"(?P<zone>Z|[+-]\d{2}:?\d{2})?"
)
# UTC ``isoformat()`` omits a zero microsecond (one value in 10**6), so there an
# omitted fraction pins as ``.ffffff``. One timestamp value is copied into at most 8
# places per scenario; more omissions mean a producer stopped writing microseconds.
_MAX_ELIDED_MICROSECONDS = 8
# ``timestamped_token`` output: ``<prefix>_YYYYmmdd_HHMMSS_<hex>``.
_TOKEN_RE = re.compile(r"\b([a-z][a-z0-9_]*?)_\d{8}_\d{6}_[0-9a-f]{8,64}\b")
# ``new_visible_generation_name`` output: ``YYYYmmdd-HHMMSS-<8 hex>``.
_GENERATION_RE = re.compile(r"\b\d{8}-\d{6}-[0-9a-f]{8}\b")
_SHA256_RE = re.compile(r"\b[0-9a-f]{64}\b")
_WORKSPACE_RE = re.compile(r"\battempt-\d+-[0-9a-f]{16}\b")
# Host capacity numbers quoted inside scratch refusal messages.
_CAPACITY_RE = re.compile(r"\b(free|available_memory|scratch_free|minimum_free)=\d+")

_KEY_PLACEHOLDERS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"(?:^|_)pgid$"), "pgid"),
    (re.compile(r"(?:^|_)pid$"), "pid"),
    (re.compile(r"start_ticks$"), "start_ticks"),
    (re.compile(r"boot_id$"), "boot_id"),
    (re.compile(r"^(?:device|st_dev|dev)$"), "device"),
    (re.compile(r"^(?:inode|st_ino|ino)$"), "inode"),
    (re.compile(r"mtime(?:_ns)?$"), "mtime"),
    (re.compile(r"ctime(?:_ns)?$"), "ctime"),
)


class Normalizer:
    """Normalize one scenario's values with a shared placeholder numbering."""

    def __init__(self, paths: Mapping[str | Path, str]) -> None:
        prefixes: dict[str, str] = {}
        for path, placeholder in paths.items():
            prefixes[str(path)] = placeholder
            prefixes[os.path.realpath(path)] = placeholder
        self._prefixes = sorted(prefixes.items(), key=lambda item: -len(item[0]))
        self._numbered: dict[tuple[str, str], str] = {}
        self._counts: dict[str, int] = {}
        self._elided_microseconds: list[str] = []

    def _number(self, kind: str, value: str) -> str:
        key = (kind, value)
        if key not in self._numbered:
            self._counts[kind] = self._counts.get(kind, 0) + 1
            self._numbered[key] = f"<{kind}:{self._counts[kind]}>"
        return self._numbered[key]

    def _timestamp(self, match: re.Match[str]) -> str:
        """``<timestamp:YYYY-MM-DDThh:mm:ss.ffffff+00:00>``: the value's shape without its digits."""
        sep, fraction, zone = match["sep"], match["fraction"] or "", match["zone"] or ""
        if not fraction and zone == "+00:00":
            self._elided_microseconds.append(match.group(0))
            if len(self._elided_microseconds) > _MAX_ELIDED_MICROSECONDS:
                raise AssertionError(
                    f"timestamps without microseconds: {self._elided_microseconds}"
                )
            fraction = ".000000"
        fraction = fraction[:1] + "f" * (len(fraction) - 1)
        return f"<timestamp:YYYY-MM-DD{sep}hh:mm:ss{fraction}{zone}>"

    def text(self, value: str) -> str:
        for prefix, placeholder in self._prefixes:
            value = value.replace(prefix, placeholder)
        value = _SHA256_RE.sub(lambda match: self._number("sha256", match.group(0)), value)
        value = _TOKEN_RE.sub(lambda match: self._number(match.group(1), match.group(0)), value)
        value = _GENERATION_RE.sub(lambda match: self._number("generation", match.group(0)), value)
        value = _WORKSPACE_RE.sub(lambda match: self._number("workspace", match.group(0)), value)
        value = _TIMESTAMP_RE.sub(self._timestamp, value)
        return _CAPACITY_RE.sub(lambda match: f"{match.group(1)}=<bytes>", value)

    def __call__(self, value: Any, key: str = "") -> Any:
        if isinstance(value, Mapping):
            return {self.text(str(k)): self(v, str(k)) for k, v in value.items()}
        if isinstance(value, list | tuple):
            return [self(item, key) for item in value]
        if value is not None and not isinstance(value, bool):
            for pattern, name in _KEY_PLACEHOLDERS:
                if pattern.search(key):
                    return f"<{name}:{type(value).__name__}>"
        if isinstance(value, str):
            return self.text(value)
        return value


def key_tree(value: Any) -> Any:
    """The ordered key structure of a JSON document with leaf values reduced to types."""
    if isinstance(value, Mapping):
        return {str(k): key_tree(v) for k, v in value.items()}
    if isinstance(value, list):
        return [key_tree(item) for item in value]
    return type(value).__name__


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _golden_text(value: Any) -> str:
    if isinstance(value, str):
        return value if value.endswith("\n") else value + "\n"
    return json.dumps(value, indent=2, ensure_ascii=False) + "\n"


def assert_golden(name: str, value: Any) -> None:
    """Compare ``value`` (already normalized) with ``golden/<name>``."""
    _assert_file(GOLDEN_DIR, name, value)


def assert_pin(name: str, value: Any) -> None:
    """Compare a rule table with ``pins/<name>``."""
    _assert_file(PINS_DIR, name, value)


def _assert_file(directory: Path, name: str, value: Any) -> None:
    path = directory / name
    label = f"{directory.name}/{name}"
    actual = _golden_text(value)
    if os.environ.get(REGEN_ENV_VAR) == "1":
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(actual, encoding="utf-8")
        return
    if not path.exists():
        raise AssertionError(f"missing {path}; regenerate with {REGEN_ENV_VAR}=1")
    expected = path.read_text(encoding="utf-8")
    if actual != expected:
        diff = "".join(
            difflib.unified_diff(
                expected.splitlines(keepends=True),
                actual.splitlines(keepends=True),
                fromfile=label,
                tofile="actual",
            )
        )
        raise AssertionError(f"mismatch for {label}:\n{diff}")
