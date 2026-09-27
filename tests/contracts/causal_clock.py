"""Test-only wall clock that never runs backwards across a contract test and its worker children.

Queue listings order rows by the UTC stamps that ``core.utils.persistence``
writes in the test process and in each worker child. A host whose wall clock
steps back (a WSL2 VM has been seen stepping about 5 s forward and back every
few seconds) can give a later write an earlier stamp and reorder a golden.
``install`` replaces ``persistence.datetime`` with ``CausalDatetime``, whose
``now`` returns the later of the real time and one microsecond after the last
stamp any process issued. That last stamp lives in the file named by
``ENV_VAR`` and is read and advanced under an exclusive ``flock``, so stamp
order follows write order. Production code has no hook: only the module
attribute is replaced, and nothing changes while the variable is unset.
"""

from __future__ import annotations

import fcntl
import os
from collections.abc import Callable
from datetime import UTC, datetime, timedelta, tzinfo

ENV_VAR = "ORCA_AUTO_TEST_CAUSAL_CLOCK"
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
_MICROSECOND = timedelta(microseconds=1)


class CausalDatetime(datetime):
    @classmethod
    def now(cls, tz: tzinfo | None = None) -> datetime:  # type: ignore[override]
        real = (datetime.now(UTC) - _EPOCH) // _MICROSECOND
        fd = os.open(os.environ[ENV_VAR], os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            stamp = max(real, int(os.pread(fd, 32, 0) or 0) + 1)
            # Stamps only grow, so an in-place write never leaves stale digits.
            os.pwrite(fd, str(stamp).encode("ascii"), 0)
        finally:
            os.close(fd)
        value = _EPOCH + stamp * _MICROSECOND
        return value.astimezone(tz) if tz is not None else value.astimezone().replace(tzinfo=None)


def install(set_attr: Callable[[object, str, object], None] = setattr) -> None:
    """Stamp through ``CausalDatetime`` while ``ENV_VAR`` is set; ``set_attr`` may undo later."""
    if not os.environ.get(ENV_VAR):
        return
    from orca_auto.core.utils import persistence

    set_attr(persistence, "datetime", CausalDatetime)
