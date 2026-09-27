"""Secret-safe timing evidence of one RiichiLab connection (Issue #416).

The first in-game disconnects are `websockets` keepalive timeouts: a PONG was
not *processed* by the event loop within `ping_timeout` of its PING.  The
Policy runs synchronously on the same loop, so this can come from a loop
stall, a late PONG, or both.  This module keeps bounded evidence that tells
those contributions apart in the next experiment:

- event-loop lag intervals, from a light probe task (`run_timing_probe()`)
  that sleeps `PROBE_INTERVAL_SECONDS` and measures how late it woke up
- the recent `request_action` timings (dequeue, decision, send attempt, the
  server time budget fields and the acks received for it)
- successful keepalive latency samples, read from the public
  `connection.latency` attribute whenever it changes
- when the connection was last seen `OPEN` and first seen not `OPEN`

All times are seconds relative to the connection's own monotonic origin
(`loop.time()` by default).

Limits, by construction:

- The PONG of the failing PING is never observed: when the timeout fires, the
  PONG may already be in the socket buffer, and `websockets` discards all
  later input once it fails the connection.
- A lag interval `[expected, actual]` says the loop could not run in that
  interval; when exactly a PONG reached the socket is not observable.
- The probe cannot run during a synchronous decision, so a lag is attributed
  to a decision by interval overlap, never by a flag sampled at wake-up.
- `recv_elapsed` is when Arena dequeued the frame, not when it arrived.
- Latency samples are change-detected once per probe tick: two equal
  consecutive latencies, or several PONGs within one tick, count once.
- Connection state is sampled once per probe tick and once more when the
  failure evidence is taken; a short `CLOSING` may be seen as `CLOSED`.

No field holds a token, a header, a payload or free text.
"""

from __future__ import annotations

import asyncio
import math
from collections import deque
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass

#: Probe period.  Small enough to bound the close window, cheap enough to run
#: for a whole hanchan.
PROBE_INTERVAL_SECONDS = 0.5
#: A lag at or above this is "material": it is kept in the recent ring and
#: counts for `material_lag_ended_in_close_window`.  Sub-second scheduling
#: noise and short decisions (which the decision ring already shows) stay out.
MATERIAL_LAG_SECONDS = 1.0

RECENT_LAG_CAPACITY = 16
LARGEST_LAG_CAPACITY = 8
DECISION_CAPACITY = 8
KEEPALIVE_CAPACITY = 8
#: Acks kept per decision entry (a request is normally acked once).
MAX_ACKS_PER_DECISION = 4

STATE_OPEN = "OPEN"
#: Connection state names kept as evidence; anything else becomes `other`.
STATE_NAMES = frozenset({"CONNECTING", STATE_OPEN, "CLOSING", "CLOSED"})
_STATE_OTHER = "other"

ACK_DEFAULTED = "defaulted"
ACK_STALE = "stale"
#: Ack statuses kept as evidence (`session.KNOWN_ACK_STATUSES`); anything else
#: never reaches here because the session fails closed on it first.
_ACK_STATUSES = frozenset({"accepted", "rejected", "unparseable", "stale", "defaulted"})
_TIME_BUDGET_FIELDS = ("grace_ms", "bank_ms", "deadline_ms")
_MAX_REQUEST_ID = 2**53


@dataclass(frozen=True, slots=True)
class LagInterval:
    """The loop could not run between `expected_elapsed` and `actual_elapsed`.

    `decision_overlap_seconds` is how much of the interval was covered by the
    recorded `request_action` decisions.  `pending` marks the interval still
    open when the evidence was taken (the probe had not woken up yet).
    """

    expected_elapsed: float
    actual_elapsed: float
    lag_seconds: float
    decision_overlap_seconds: float
    pending: bool = False

    @property
    def outside_decision_seconds(self) -> float:
        return max(0.0, self.lag_seconds - self.decision_overlap_seconds)


@dataclass(frozen=True, slots=True)
class DecisionTiming:
    """One `request_action`: dequeue -> decision -> send attempt."""

    ordinal: int
    request_id: int | None
    recv_elapsed: float
    decision_start_elapsed: float
    decision_end_elapsed: float
    send_attempt_elapsed: float | None
    grace_ms: float | None
    bank_ms: float | None
    deadline_ms: float | None
    ack_statuses: tuple[str, ...]

    @property
    def decision_seconds(self) -> float:
        return self.decision_end_elapsed - self.decision_start_elapsed


@dataclass(frozen=True, slots=True)
class KeepaliveSample:
    """A successful keepalive: `latency_seconds` seen changed at `seen_elapsed`.

    The PING went out at about `seen_elapsed - latency_seconds` (at most one
    probe period earlier than that).
    """

    seen_elapsed: float
    latency_seconds: float


@dataclass(frozen=True, slots=True)
class TransportTimingEvidence:
    """Bounded timing evidence of one connection, taken at a transport failure."""

    evidence_elapsed: float
    probe_interval_seconds: float
    probe_ticks: int
    probe_failed: bool
    max_event_loop_lag_seconds: float | None
    recent_lags: tuple[LagInterval, ...]
    largest_lags: tuple[LagInterval, ...]
    decisions: tuple[DecisionTiming, ...]
    keepalive_samples: tuple[KeepaliveSample, ...]
    keepalive_sample_count: int
    last_open_seen_elapsed: float | None
    first_not_open_seen_elapsed: float | None
    first_not_open_state: str | None
    defaulted_ack_count: int
    stale_ack_count: int

    def _lags(self) -> tuple[LagInterval, ...]:
        return self.recent_lags + self.largest_lags

    @property
    def max_lag_ending_in_close_window_seconds(self) -> float | None:
        """Largest kept (material) lag whose wake-up falls in the close window.

        The close window is `[last seen OPEN, first seen not OPEN]`; the local
        or remote close happened inside it.  `0.0` when no kept lag ends there,
        `None` when the window was not seen.

        The keepalive PING time and its deadline are not observed, so this
        does not show that a stall crossed the keepalive deadline.
        """
        end = self.first_not_open_seen_elapsed
        if end is None:
            return None
        start = self.last_open_seen_elapsed
        ending = [
            lag.lag_seconds
            for lag in self._lags()
            if lag.actual_elapsed <= end
            and (start is None or lag.actual_elapsed >= start)
        ]
        return max(ending, default=0.0)

    @property
    def material_lag_ended_in_close_window(self) -> bool | None:
        """Whether a material loop stall ended inside the close window.

        `None` when the close window was not observed.  For a keepalive
        timeout (see `local_close_reason_class`), `True` is consistent with a
        loop stall near the keepalive deadline, but does not prove that the
        stall crossed the deadline: neither the PING time nor the deadline is
        observed, and a stall that ended just before the close is counted
        too.
        """
        lag = self.max_lag_ending_in_close_window_seconds
        return None if lag is None else lag > 0.0

    @property
    def max_lag_overlapping_decision_seconds(self) -> float | None:
        lags = self._lags()
        if not lags:
            return None
        return max(lag.decision_overlap_seconds for lag in lags)

    @property
    def max_lag_outside_decision_seconds(self) -> float | None:
        lags = self._lags()
        if not lags:
            return None
        return max(lag.outside_decision_seconds for lag in lags)

    @property
    def max_recent_decision_seconds(self) -> float | None:
        if not self.decisions:
            return None
        return max(decision.decision_seconds for decision in self.decisions)

    @property
    def max_recent_keepalive_latency_seconds(self) -> float | None:
        if not self.keepalive_samples:
            return None
        return max(sample.latency_seconds for sample in self.keepalive_samples)

    def to_evidence_dict(self) -> dict[str, object]:
        """A JSON-ready, bounded representation (milliseconds precision)."""
        return {
            "evidence_elapsed": _ms(self.evidence_elapsed),
            "probe_interval_seconds": self.probe_interval_seconds,
            "probe_ticks": self.probe_ticks,
            "probe_failed": self.probe_failed,
            "max_event_loop_lag_seconds": _ms(self.max_event_loop_lag_seconds),
            "max_lag_ending_in_close_window_seconds": _ms(
                self.max_lag_ending_in_close_window_seconds
            ),
            "material_lag_ended_in_close_window": self.material_lag_ended_in_close_window,
            "max_lag_overlapping_decision_seconds": _ms(
                self.max_lag_overlapping_decision_seconds
            ),
            "max_lag_outside_decision_seconds": _ms(
                self.max_lag_outside_decision_seconds
            ),
            "max_recent_decision_seconds": _ms(self.max_recent_decision_seconds),
            "max_recent_keepalive_latency_seconds": _ms(
                self.max_recent_keepalive_latency_seconds
            ),
            "keepalive_sample_count": self.keepalive_sample_count,
            "last_open_seen_elapsed": _ms(self.last_open_seen_elapsed),
            "first_not_open_seen_elapsed": _ms(self.first_not_open_seen_elapsed),
            "first_not_open_state": self.first_not_open_state,
            "defaulted_ack_count": self.defaulted_ack_count,
            "stale_ack_count": self.stale_ack_count,
            "recent_lags": [_lag_dict(lag) for lag in self.recent_lags],
            "largest_lags": [_lag_dict(lag) for lag in self.largest_lags],
            "decisions": [_decision_dict(entry) for entry in self.decisions],
            "keepalive_samples": [
                {
                    "seen_elapsed": _ms(sample.seen_elapsed),
                    "latency_seconds": _ms(sample.latency_seconds),
                }
                for sample in self.keepalive_samples
            ],
        }


def _ms(value: float | None) -> float | None:
    return None if value is None else round(value, 3)


def _lag_dict(lag: LagInterval) -> dict[str, object]:
    return {
        "expected_elapsed": _ms(lag.expected_elapsed),
        "actual_elapsed": _ms(lag.actual_elapsed),
        "lag_seconds": _ms(lag.lag_seconds),
        "decision_overlap_seconds": _ms(lag.decision_overlap_seconds),
        "pending": lag.pending,
    }


def _decision_dict(entry: DecisionTiming) -> dict[str, object]:
    return {
        "ordinal": entry.ordinal,
        "request_id": entry.request_id,
        "recv_elapsed": _ms(entry.recv_elapsed),
        "decision_start_elapsed": _ms(entry.decision_start_elapsed),
        "decision_end_elapsed": _ms(entry.decision_end_elapsed),
        "send_attempt_elapsed": _ms(entry.send_attempt_elapsed),
        "decision_seconds": _ms(entry.decision_seconds),
        "grace_ms": entry.grace_ms,
        "bank_ms": entry.bank_ms,
        "deadline_ms": entry.deadline_ms,
        "ack_statuses": list(entry.ack_statuses),
    }


def _budget_value(time_value: object, field_name: str) -> float | None:
    if not isinstance(time_value, Mapping):
        return None
    value = time_value.get(field_name)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    value = float(value)
    return value if math.isfinite(value) else None


def _request_id(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if 0 <= value < _MAX_REQUEST_ID else None


def _state_name(state: object) -> str | None:
    if state is None:
        return None
    name = getattr(state, "name", state)
    return name if isinstance(name, str) and name in STATE_NAMES else _STATE_OTHER


class _Decision:
    """Mutable ring entry: the send attempt and acks arrive after the decision."""

    __slots__ = (
        "ordinal",
        "request_id",
        "recv_at",
        "start_at",
        "end_at",
        "send_at",
        "time_budget",
        "acks",
    )

    def __init__(self, ordinal, request_id, recv_at, start_at, end_at, time_budget):
        self.ordinal = ordinal
        self.request_id = request_id
        self.recv_at = recv_at
        self.start_at = start_at
        self.end_at = end_at
        self.send_at: float | None = None
        self.time_budget = time_budget
        self.acks: list[str] = []


class ConnectionTiming:
    """Bounded in-memory timing state of one connection.

    Written by the drive loop (decisions, acks) and the probe task (lags,
    keepalive, state); both run on the connection's event loop, so no lock is
    needed.  `evidence()` returns an immutable snapshot.
    """

    __slots__ = (
        "_clock",
        "_origin",
        "_state_source",
        "_probe_interval",
        "_recent_lags",
        "_largest_lags",
        "_max_lag",
        "_decisions",
        "_ordinal",
        "_keepalive",
        "_keepalive_count",
        "_last_latency",
        "_last_open_seen",
        "_first_not_open_seen",
        "_first_not_open_state",
        "_defaulted",
        "_stale",
        "_probe_ticks",
        "_probe_failed",
        "_next_expected",
    )

    def __init__(
        self,
        *,
        clock: Callable[[], float] | None = None,
        state_source: Callable[[], object] | None = None,
        probe_interval: float = PROBE_INTERVAL_SECONDS,
    ) -> None:
        if clock is None:
            clock = asyncio.get_running_loop().time
        self._clock = clock
        self._origin = clock()
        self._state_source = state_source
        self._probe_interval = probe_interval
        self._recent_lags: deque[LagInterval] = deque(maxlen=RECENT_LAG_CAPACITY)
        self._largest_lags: list[LagInterval] = []
        self._max_lag: float | None = None
        self._decisions: deque[_Decision] = deque(maxlen=DECISION_CAPACITY)
        self._ordinal = 0
        self._keepalive: deque[KeepaliveSample] = deque(maxlen=KEEPALIVE_CAPACITY)
        self._keepalive_count = 0
        self._last_latency: float | None = None
        self._last_open_seen: float | None = None
        self._first_not_open_seen: float | None = None
        self._first_not_open_state: str | None = None
        self._defaulted = 0
        self._stale = 0
        self._probe_ticks = 0
        self._probe_failed = False
        self._next_expected: float | None = None

    @property
    def probe_interval(self) -> float:
        return self._probe_interval

    def now(self) -> float:
        """Seconds since this connection's origin."""
        return self._clock() - self._origin

    # Drive loop side -------------------------------------------------------

    def record_decision(
        self,
        *,
        request_id: object,
        time_budget: object,
        recv_at: float,
        start_at: float,
        end_at: float,
    ) -> _Decision:
        self._ordinal += 1
        entry = _Decision(
            self._ordinal,
            _request_id(request_id),
            recv_at,
            start_at,
            end_at,
            tuple(_budget_value(time_budget, name) for name in _TIME_BUDGET_FIELDS),
        )
        self._decisions.append(entry)
        return entry

    def record_send_attempt(self, entry: _Decision, at: float) -> None:
        entry.send_at = at

    def record_ack(self, request_id: object, status: object) -> None:
        if not isinstance(status, str) or status not in _ACK_STATUSES:
            return
        if status == ACK_DEFAULTED:
            self._defaulted += 1
        elif status == ACK_STALE:
            self._stale += 1
        request_id = _request_id(request_id)
        for entry in reversed(self._decisions):
            if entry.request_id is not None and entry.request_id == request_id:
                if len(entry.acks) < MAX_ACKS_PER_DECISION:
                    entry.acks.append(status)
                return

    # Probe side ------------------------------------------------------------

    def note_probe_sleep(self, expected_at: float) -> None:
        """The probe expects to wake up at `expected_at` (connection time)."""
        self._next_expected = expected_at

    def record_probe_tick(
        self, *, expected_at: float, actual_at: float, latency: object, state: object
    ) -> None:
        self._probe_ticks += 1
        self._next_expected = None
        self._record_lag(expected_at, actual_at, pending=False)
        self._record_latency(latency, actual_at)
        self._record_state(state, actual_at)

    def mark_probe_failed(self) -> None:
        self._probe_failed = True

    def _record_lag(
        self, expected_at: float, actual_at: float, *, pending: bool
    ) -> LagInterval | None:
        """Track the maximum; keep a material lag (a pending one is returned only)."""
        lag_seconds = max(0.0, actual_at - expected_at)
        if self._max_lag is None or lag_seconds > self._max_lag:
            self._max_lag = lag_seconds
        if lag_seconds < MATERIAL_LAG_SECONDS:
            return None
        lag = LagInterval(
            expected_elapsed=expected_at,
            actual_elapsed=actual_at,
            lag_seconds=lag_seconds,
            decision_overlap_seconds=self._decision_overlap(expected_at, actual_at),
            pending=pending,
        )
        if pending:
            return lag
        self._recent_lags.append(lag)
        self._largest_lags.append(lag)
        self._largest_lags.sort(key=lambda item: item.lag_seconds, reverse=True)
        del self._largest_lags[LARGEST_LAG_CAPACITY:]
        return lag

    def _decision_overlap(self, start: float, end: float) -> float:
        overlap = 0.0
        for entry in self._decisions:
            overlap += max(0.0, min(end, entry.end_at) - max(start, entry.start_at))
        return min(overlap, end - start)

    def _record_latency(self, latency: object, at: float) -> None:
        if isinstance(latency, bool) or not isinstance(latency, (int, float)):
            return
        latency = float(latency)
        if not math.isfinite(latency) or latency <= 0.0:
            return
        if latency == self._last_latency:
            return
        self._last_latency = latency
        self._keepalive_count += 1
        self._keepalive.append(
            KeepaliveSample(seen_elapsed=at, latency_seconds=latency)
        )

    def _record_state(self, state: object, at: float) -> None:
        name = _state_name(state)
        if name is None:
            return
        if name == STATE_OPEN:
            if self._first_not_open_seen is None:
                self._last_open_seen = at
        elif self._first_not_open_seen is None and name != "CONNECTING":
            self._first_not_open_seen = at
            self._first_not_open_state = name

    # Snapshot --------------------------------------------------------------

    def evidence(self) -> TransportTimingEvidence:
        """Snapshot, with a final state sample and any still-open lag."""
        at = self.now()
        if self._state_source is not None:
            try:
                self._record_state(self._state_source(), at)
            except Exception:
                self._probe_failed = True
        recent = tuple(self._recent_lags)
        if self._next_expected is not None:
            pending = self._record_lag(self._next_expected, at, pending=True)
            if pending is not None:
                recent += (pending,)
        return TransportTimingEvidence(
            evidence_elapsed=at,
            probe_interval_seconds=self._probe_interval,
            probe_ticks=self._probe_ticks,
            probe_failed=self._probe_failed,
            max_event_loop_lag_seconds=self._max_lag,
            recent_lags=recent,
            largest_lags=tuple(self._largest_lags),
            decisions=tuple(
                DecisionTiming(
                    ordinal=entry.ordinal,
                    request_id=entry.request_id,
                    recv_elapsed=entry.recv_at,
                    decision_start_elapsed=entry.start_at,
                    decision_end_elapsed=entry.end_at,
                    send_attempt_elapsed=entry.send_at,
                    grace_ms=entry.time_budget[0],
                    bank_ms=entry.time_budget[1],
                    deadline_ms=entry.time_budget[2],
                    ack_statuses=tuple(entry.acks),
                )
                for entry in self._decisions
            ),
            keepalive_samples=tuple(self._keepalive),
            keepalive_sample_count=self._keepalive_count,
            last_open_seen_elapsed=self._last_open_seen,
            first_not_open_seen_elapsed=self._first_not_open_seen,
            first_not_open_state=self._first_not_open_state,
            defaulted_ack_count=self._defaulted,
            stale_ack_count=self._stale,
        )


async def run_timing_probe(
    timing: ConnectionTiming,
    *,
    latency_source: Callable[[], object],
    state_source: Callable[[], object],
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> None:
    """Sample loop lag, keepalive latency and connection state until cancelled.

    An ordinary exception stops the probe and marks the evidence
    (`probe_failed`); it never reaches the ranked run.  Cancellation is not
    caught.
    """
    interval = timing.probe_interval
    try:
        while True:
            expected = timing.now() + interval
            timing.note_probe_sleep(expected)
            await sleep(interval)
            actual = timing.now()
            timing.record_probe_tick(
                expected_at=expected,
                actual_at=actual,
                latency=latency_source(),
                state=state_source(),
            )
    except Exception:
        timing.mark_probe_failed()


__all__ = [
    "DECISION_CAPACITY",
    "KEEPALIVE_CAPACITY",
    "LARGEST_LAG_CAPACITY",
    "MATERIAL_LAG_SECONDS",
    "PROBE_INTERVAL_SECONDS",
    "RECENT_LAG_CAPACITY",
    "ConnectionTiming",
    "DecisionTiming",
    "KeepaliveSample",
    "LagInterval",
    "TransportTimingEvidence",
    "run_timing_probe",
]
