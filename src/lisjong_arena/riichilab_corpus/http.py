"""Small stdlib HTTP transport with deliberately bounded retry semantics."""

from __future__ import annotations

import socket
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Callable, Mapping, Protocol

from lisjong_arena.riichilab_corpus.models import CorpusError

DEFAULT_TIMEOUT_SECONDS = 15.0
DEFAULT_MAX_ATTEMPTS = 3
DEFAULT_BACKOFF_SECONDS = 0.5
MAX_RESPONSE_BYTES = 16 * 1024 * 1024


class HttpTransportError(CorpusError):
    """A public HTTP request failed after the permitted handling."""


@dataclass(frozen=True, slots=True)
class HttpResponse:
    status: int
    headers: Mapping[str, str]
    body: bytes


class HttpTransport(Protocol):
    def get(self, url: str, *, timeout: float) -> HttpResponse: ...


class StdlibHttpTransport:
    """Credential-free GET transport for the two public RiichiLab hosts."""

    def get(self, url: str, *, timeout: float) -> HttpResponse:
        request = urllib.request.Request(
            url,
            headers={
                "Accept": "application/json, application/gzip",
                "User-Agent": "lisjong-arena-bounded-research-corpus/0.1",
            },
            method="GET",
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                body = response.read(MAX_RESPONSE_BYTES + 1)
                if len(body) > MAX_RESPONSE_BYTES:
                    raise HttpTransportError("HTTP response exceeds the 16 MiB bound")
                return HttpResponse(
                    response.status,
                    {key.lower(): value for key, value in response.headers.items()},
                    body,
                )
        except urllib.error.HTTPError as exc:
            return HttpResponse(
                exc.code,
                {}
                if exc.headers is None
                else {key.lower(): value for key, value in exc.headers.items()},
                b"",
            )


def get_with_bounded_retry(
    transport: HttpTransport,
    url: str,
    *,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    backoff_seconds: float = DEFAULT_BACKOFF_SECONDS,
    sleeper: Callable[[float], None] = time.sleep,
) -> HttpResponse:
    if type(timeout) not in (int, float) or timeout <= 0:
        raise ValueError("timeout must be positive")
    if type(max_attempts) is not int or not 1 <= max_attempts <= 3:
        raise ValueError("max_attempts must be from 1 through 3")
    if type(backoff_seconds) not in (int, float) or not 0 <= backoff_seconds <= 5:
        raise ValueError("backoff_seconds must be from 0 through 5")

    last_failure: BaseException | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            response = transport.get(url, timeout=float(timeout))
        except (TimeoutError, socket.timeout, urllib.error.URLError, OSError) as exc:
            last_failure = exc
            retryable = True
        else:
            if response.status == 200:
                if not response.body:
                    raise HttpTransportError(f"empty HTTP 200 payload from {url}")
                if len(response.body) > MAX_RESPONSE_BYTES:
                    raise HttpTransportError(
                        f"HTTP 200 payload exceeds the 16 MiB bound: {url}"
                    )
                return response
            retryable = response.status == 429 or 500 <= response.status <= 599
            if not retryable:
                raise HttpTransportError(f"HTTP {response.status} from {url}")
            last_failure = HttpTransportError(f"HTTP {response.status} from {url}")

        if not retryable or attempt == max_attempts:
            break
        sleeper(float(backoff_seconds) * (2 ** (attempt - 1)))

    assert last_failure is not None
    raise HttpTransportError(
        f"request failed after {max_attempts} bounded attempts: {url} ({last_failure})"
    ) from last_failure


__all__ = [
    "HttpResponse",
    "HttpTransport",
    "HttpTransportError",
    "MAX_RESPONSE_BYTES",
    "StdlibHttpTransport",
    "get_with_bounded_retry",
]
