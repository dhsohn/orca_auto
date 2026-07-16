from __future__ import annotations

import logging
import socket
import threading
from collections.abc import Callable
from contextlib import contextmanager
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

LOGGER = logging.getLogger(__name__)
_IPV4_RESOLUTION_LOCK = threading.RLock()


def _iter_exception_chain(exc: BaseException) -> list[BaseException]:
    seen: set[int] = set()
    stack: list[BaseException] = [exc]
    rows: list[BaseException] = []
    while stack:
        current = stack.pop()
        marker = id(current)
        if marker in seen:
            continue
        seen.add(marker)
        rows.append(current)
        reason = getattr(current, "reason", None)
        if isinstance(reason, BaseException):
            stack.append(reason)
        cause = getattr(current, "__cause__", None)
        if isinstance(cause, BaseException):
            stack.append(cause)
        context = getattr(current, "__context__", None)
        if isinstance(context, BaseException):
            stack.append(context)
    return rows


def _is_network_unreachable_error(exc: BaseException) -> bool:
    for current in _iter_exception_chain(exc):
        errno = getattr(current, "errno", None)
        if errno in {101, 113}:
            return True
        text = str(current).strip().lower()
        if "network is unreachable" in text or "no route to host" in text:
            return True
    return False


def _is_timeout_error(exc: BaseException) -> bool:
    for current in _iter_exception_chain(exc):
        if isinstance(current, (TimeoutError, socket.timeout)):
            return True
        text = str(current).strip().lower()
        if "timed out" in text:
            return True
    return False


def _is_temporary_dns_error(exc: BaseException) -> bool:
    for current in _iter_exception_chain(exc):
        if isinstance(current, socket.gaierror) and current.errno == getattr(
            socket, "EAI_AGAIN", None
        ):
            return True
        text = str(current).strip().lower()
        if "temporary failure in name resolution" in text or "temporary dns failure" in text:
            return True
    return False


def _should_retry_url_error(exc: BaseException) -> bool:
    return (
        _is_timeout_error(exc) or _is_network_unreachable_error(exc) or _is_temporary_dns_error(exc)
    )


def _is_retryable_http_status(status_code: int | None) -> bool:
    return status_code in {429, 500, 502, 503, 504}


@contextmanager
def _force_ipv4_resolution(hostname: str):
    target = str(hostname).strip().lower()
    with _IPV4_RESOLUTION_LOCK:
        original_getaddrinfo = socket.getaddrinfo

        def _ipv4_only_getaddrinfo(
            host: bytes | str | None,
            port: bytes | str | int | None,
            family: int = 0,
            type: int = 0,
            proto: int = 0,
            flags: int = 0,
        ):
            results = original_getaddrinfo(host, port, family, type, proto, flags)
            if str(host).strip().lower() != target:
                return results
            filtered = [item for item in results if item[0] == socket.AF_INET]
            return filtered or results

        socket.getaddrinfo = _ipv4_only_getaddrinfo
        try:
            yield
        finally:
            socket.getaddrinfo = original_getaddrinfo


def urlopen_with_ipv4_fallback(
    request: Request,
    *,
    timeout: float,
    urlopen_fn: Callable[..., object] = urlopen,
):
    """Retry one request with hostname-scoped IPv4 resolution after IPv6 routing failure.

    urllib does not expose per-request address-family selection, so the fallback
    uses a short-lived process-global resolver override guarded by a module lock.
    Only the request hostname is filtered while the override is active; lookups
    for other hostnames still delegate to the original resolver unchanged.
    """
    try:
        return urlopen_fn(request, timeout=timeout)
    except OSError as exc:
        if not _is_network_unreachable_error(exc):
            raise
        hostname = urlsplit(getattr(request, "full_url", "")).hostname or ""
        if not hostname:
            raise
        with _force_ipv4_resolution(hostname):
            return urlopen_fn(request, timeout=timeout)


def _read_http_error_body(exc: HTTPError) -> str:
    try:
        return exc.read().decode("utf-8", errors="replace")
    except OSError:
        LOGGER.debug("failed to read Telegram HTTP error body", exc_info=True)
        return ""


__all__ = [
    "_force_ipv4_resolution",
    "_is_network_unreachable_error",
    "_is_retryable_http_status",
    "_is_temporary_dns_error",
    "_is_timeout_error",
    "_iter_exception_chain",
    "_read_http_error_body",
    "_should_retry_url_error",
    "urlopen_with_ipv4_fallback",
]
