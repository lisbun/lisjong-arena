"""Secret-safe diagnostics of one RiichiLab transport failure (Issue #411).

A transport failure (`TransportError`, including `UnexpectedDisconnectError`)
carries a `TransportDiagnostics` value so that operators can tell *where* the
connection failed and *what the server said*:

- phase: `connect` / `before_start_game` / `in_game`
- operation: `connect` / `recv` / `send`
- HTTP status of a rejected handshake, WebSocket close codes (received / sent)
- server-provided reason (an untyped `{"error": ...}` message, a rejected
  handshake body, or the received close reason) and the locally sent close
  reason

The canonical evidence of a reason is its normalized classification
(`REASON_CLASSES`).  A bounded, redacted excerpt is kept in addition, only when
redaction is explicitly confirmed: the exact runtime token and its fragments,
Authorization / Bearer material, and long token-like runs must all be absent.
The character allow-list is applied as well but is never relied upon alone.

The transport layer first records a raw `RawTransportFailure` on the exception
and `connect_transport()` — the only place that knows the token — converts it
with `sanitize_transport_failure()` before the exception leaves the connection.
Callers only ever read the sanitized `diagnostics`.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass

PHASE_CONNECT = "connect"
PHASE_BEFORE_START_GAME = "before_start_game"
PHASE_IN_GAME = "in_game"

OPERATION_CONNECT = "connect"
OPERATION_RECV = "recv"
OPERATION_SEND = "send"

REASON_NONE = "none"
REASON_SAME_BOT_ALREADY_ACTIVE = "same_bot_already_active"
REASON_TOKEN_OR_BOT_REJECTED = "token_or_bot_rejected"
REASON_KEEPALIVE_TIMEOUT = "keepalive_timeout"
REASON_OTHER = "other"

#: Classes in match order.  The keyword rules are a provisional heuristic for
#: evidence only and drive no control decision: RiichiLab does not document its
#: rejection texts, so `other` together with the excerpt is what reveals an
#: unknown one.  Phrases stay specific (no bare "already" / "in game") so that
#: unrelated texts are not labelled a same-bot rejection; the final rule is
#: fixed only after a reproduction shows the real server signal (#411 PR 2).
REASON_CLASSES = (
    REASON_NONE,
    REASON_SAME_BOT_ALREADY_ACTIVE,
    REASON_TOKEN_OR_BOT_REJECTED,
    REASON_KEEPALIVE_TIMEOUT,
    REASON_OTHER,
)
_REASON_KEYWORDS = (
    (
        REASON_SAME_BOT_ALREADY_ACTIVE,
        (
            "already connected",
            "already in a game",
            "already in game",
            "already in the queue",
            "already in queue",
            "already queued",
            "concurrent connection",
            "multiple concurrent",
            "multiple connections",
            "duplicate connection",
        ),
    ),
    (
        REASON_TOKEN_OR_BOT_REJECTED,
        ("token", "unauthori", "forbidden", "inactive", "not active"),
    ),
    (REASON_KEEPALIVE_TIMEOUT, ("keepalive", "ping timeout")),
)

#: Upper bound of a kept excerpt, in characters.
MAX_EXCERPT_LENGTH = 80
#: Raw text beyond this length is not examined at all.
_MAX_RAW_LENGTH = 1000
#: A secret fragment of this length anywhere in an excerpt drops the excerpt.
_SECRET_FRAGMENT_LENGTH = 12
_REDACTED = "[redacted]"
_AUTH_MATERIAL_RE = re.compile(r"(?i)\b(?:authorization|bearer)\b\s*[:=]?\s*\S*")
_TOKEN_LIKE_RE = re.compile(r"[A-Za-z0-9_+/=.-]{24,}")
_DISALLOWED_CHAR_RE = re.compile(r"[^A-Za-z0-9 .,:;_()'\[\]-]")
_FORBIDDEN_WORDS = ("authorization", "bearer")


@dataclass(frozen=True, slots=True)
class RawTransportFailure:
    """Unsanitized failure facts.  Never logged; converted before it leaves."""

    phase: str
    operation: str
    http_status: int | None = None
    close_code_received: int | None = None
    close_code_sent: int | None = None
    server_text: str | None = None
    local_close_reason: str | None = None
    requests_received: int = 0
    last_decision_elapsed_seconds: float | None = None


@dataclass(frozen=True, slots=True)
class TransportDiagnostics:
    """Secret-safe facts of one transport failure.

    `last_decision_elapsed_seconds` is how long the most recent
    `request_action` handling (Policy decision included) took before the
    failure.  It is not a decision in flight at the failure.
    """

    phase: str
    operation: str
    http_status: int | None
    close_code_received: int | None
    close_code_sent: int | None
    server_reason_class: str
    server_reason_excerpt: str | None
    local_close_reason_class: str
    requests_received: int
    last_decision_elapsed_seconds: float | None


def classify_reason(text: str | None) -> str:
    """Map a raw reason text to one of `REASON_CLASSES`."""
    if text is None or not text.strip():
        return REASON_NONE
    lowered = text[:_MAX_RAW_LENGTH].lower()
    for reason_class, keywords in _REASON_KEYWORDS:
        if any(keyword in lowered for keyword in keywords):
            return reason_class
    return REASON_OTHER


def _contains_secret_fragment(text: str, secrets: tuple[str, ...]) -> bool:
    for secret in secrets:
        if secret in text:
            return True
        if len(secret) >= _SECRET_FRAGMENT_LENGTH:
            for start in range(len(secret) - _SECRET_FRAGMENT_LENGTH + 1):
                if secret[start : start + _SECRET_FRAGMENT_LENGTH] in text:
                    return True
    return False


def is_confirmed_secret_free(text: str, secrets: Iterable[str] = ()) -> bool:
    """Explicit check that `text` holds no secret or Authorization material."""
    kept = tuple(secret for secret in secrets if secret)
    lowered = text.lower()
    if any(word in lowered for word in _FORBIDDEN_WORDS):
        return False
    if _TOKEN_LIKE_RE.search(text):
        return False
    return not _contains_secret_fragment(text, kept)


def redact_excerpt(text: str | None, secrets: Iterable[str] = ()) -> str | None:
    """Return a bounded, redacted excerpt, or `None` if it cannot be confirmed."""
    if text is None:
        return None
    kept = tuple(secret for secret in secrets if secret)
    excerpt = text[:_MAX_RAW_LENGTH]
    for secret in kept:
        excerpt = excerpt.replace(secret, _REDACTED)
    excerpt = _AUTH_MATERIAL_RE.sub(_REDACTED, excerpt)
    excerpt = _TOKEN_LIKE_RE.sub(_REDACTED, excerpt)
    excerpt = " ".join(excerpt.split())
    excerpt = _DISALLOWED_CHAR_RE.sub("?", excerpt)[:MAX_EXCERPT_LENGTH].strip()
    if not excerpt:
        return None
    # The redacted marker itself is safe; confirm everything around it.
    if not is_confirmed_secret_free(excerpt.replace(_REDACTED, " "), kept):
        return None
    return excerpt


def sanitize_transport_failure(
    raw: RawTransportFailure, secrets: Iterable[str] = ()
) -> TransportDiagnostics:
    """Convert raw failure facts into secret-safe `TransportDiagnostics`."""
    kept = tuple(secret for secret in secrets if secret)
    return TransportDiagnostics(
        phase=raw.phase,
        operation=raw.operation,
        http_status=raw.http_status,
        close_code_received=raw.close_code_received,
        close_code_sent=raw.close_code_sent,
        server_reason_class=classify_reason(raw.server_text),
        server_reason_excerpt=redact_excerpt(raw.server_text, kept),
        local_close_reason_class=classify_reason(raw.local_close_reason),
        requests_received=raw.requests_received,
        last_decision_elapsed_seconds=raw.last_decision_elapsed_seconds,
    )


__all__ = [
    "MAX_EXCERPT_LENGTH",
    "OPERATION_CONNECT",
    "OPERATION_RECV",
    "OPERATION_SEND",
    "PHASE_BEFORE_START_GAME",
    "PHASE_CONNECT",
    "PHASE_IN_GAME",
    "REASON_CLASSES",
    "RawTransportFailure",
    "TransportDiagnostics",
    "classify_reason",
    "is_confirmed_secret_free",
    "redact_excerpt",
    "sanitize_transport_failure",
]
