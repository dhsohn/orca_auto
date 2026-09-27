from types import MappingProxyType

import pytest

from orca_auto.core.utils import copy_dict_or_empty


@pytest.mark.parametrize(
    "value",
    [None, [], "not-a-dict", MappingProxyType({"key": "value"})],
)
def test_copy_dict_or_empty_rejects_non_dict_values(value: object) -> None:
    assert copy_dict_or_empty(value) == {}


def test_copy_dict_or_empty_returns_a_distinct_shallow_copy() -> None:
    nested: list[str] = []
    original = {"nested": nested}

    copied = copy_dict_or_empty(original)

    assert copied == original
    assert copied is not original
    assert copied["nested"] is nested
