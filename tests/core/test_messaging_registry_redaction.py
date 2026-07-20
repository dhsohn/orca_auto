from __future__ import annotations

import pytest

from orca_auto.core.config import MessengerConfig
from orca_auto.core.messaging import build_channel


def test_build_channel_does_not_expose_programmatic_provider_value() -> None:
    provider = "private-provider-credential"

    with pytest.raises(ValueError) as raised:
        build_channel(MessengerConfig(provider=provider))

    assert provider not in str(raised.value)
    assert raised.value.__context__ is None
    assert raised.value.__cause__ is None
