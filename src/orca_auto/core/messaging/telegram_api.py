from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from .telegram_config import DEFAULT_TELEGRAM_BASE_URL, DEFAULT_TIMEOUT_SECONDS
from .telegram_logging import safe_log_body
from .telegram_network import (
    _read_http_error_body,
)
from .telegram_network import (
    urlopen_with_ipv4_fallback as _network_urlopen_with_ipv4_fallback,
)

LOGGER = logging.getLogger(__name__)


def _open_telegram_request(request: Request, *, timeout: float):
    return _network_urlopen_with_ipv4_fallback(
        request,
        timeout=timeout,
        urlopen_fn=urlopen,
    )


@dataclass(frozen=True)
class TelegramApiClient:
    token: str
    timeout: float = DEFAULT_TIMEOUT_SECONDS
    base_url: str = DEFAULT_TELEGRAM_BASE_URL
    logger: logging.Logger = LOGGER

    def api_call(
        self,
        method: str,
        payload: dict[str, Any] | None = None,
        *,
        timeout: float | None = None,
    ) -> Any | None:
        token = str(self.token).strip()
        if not token:
            return None

        url = f"{self.base_url.rstrip('/')}/bot{token}/{method}"
        data = json.dumps(payload or {}).encode("utf-8")
        request = Request(
            url,
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with _open_telegram_request(
                request,
                timeout=float(timeout if timeout is not None else self.timeout),
            ) as response:
                result = json.loads(response.read().decode("utf-8"))
                if result.get("ok"):
                    return result.get("result")
                self.logger.warning(
                    "telegram_api_error: method=%s response=%s",
                    method,
                    safe_log_body(result),
                )
                return None
        except HTTPError as exc:
            body = _read_http_error_body(exc)
            self.logger.warning(
                "telegram_api_http_error: method=%s status=%d body=%s",
                method,
                exc.code,
                safe_log_body(body),
            )
            return None
        except Exception as exc:  # noqa: BLE001
            self.logger.warning("telegram_api_failed: method=%s error=%s", method, exc)
            return None


__all__ = ["TelegramApiClient"]
