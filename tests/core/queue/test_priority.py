from __future__ import annotations

import pytest

from orca_auto.core.queue.priority import normalize_queue_priority


@pytest.mark.parametrize(
    ("raw", "expected"),
    [(None, 10), ("", 10), (0, 0), (-3, -3), ("0", 0), ("-3", -3)],
)
def test_normalize_queue_priority_preserves_all_integer_values(
    raw: object,
    expected: int,
) -> None:
    assert normalize_queue_priority(raw) == expected


@pytest.mark.parametrize("raw", [False, True, 1.0, 1.5, "1.5", object()])
def test_normalize_queue_priority_rejects_non_integer_values(raw: object) -> None:
    with pytest.raises(ValueError, match="priority must be an integer"):
        normalize_queue_priority(raw)
