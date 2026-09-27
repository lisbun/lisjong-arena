"""Issue #416: bounded connection timing evidence of a RiichiLab connection.

`ConnectionTiming` is driven with an injected clock so that lag / decision
overlap and the keepalive close window are deterministic.  The drive-loop and
`connect_transport()` tests use fake transports / connections; no real
WebSocket connection is made.
"""

import asyncio
import json
import time
import unittest
from unittest.mock import patch

from lisjong.policies import MinimalPolicy
from websockets.exceptions import ConnectionClosedError
from websockets.frames import Close

from lisjong_arena.riichilab import transport_timing
from lisjong_arena.riichilab.adapter import SendReadyResponse
from lisjong_arena.riichilab.errors import TransportError, UnexpectedDisconnectError
from lisjong_arena.riichilab.session import RankedSession
from lisjong_arena.riichilab.transport import (
    TransportClosed,
    connect_transport,
    drive_ranked_session,
)
from lisjong_arena.riichilab.transport_diagnostics import RawTransportFailure
from lisjong_arena.riichilab.transport_timing import (
    DECISION_CAPACITY,
    KEEPALIVE_CAPACITY,
    LARGEST_LAG_CAPACITY,
    RECENT_LAG_CAPACITY,
    ConnectionTiming,
    run_timing_probe,
)

_PATCH_TARGET = "lisjong_arena.riichilab.session.RiichiLabSeatAdapter"
_TOKEN = "unit-test-timing-token-0123456789abcdef"


class _Clock:
    def __init__(self, now: float = 100.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


def _timing(clock: _Clock, **kwargs) -> ConnectionTiming:
    return ConnectionTiming(clock=clock, **kwargs)


def _tick(timing: ConnectionTiming, expected, actual, *, latency=None, state="OPEN"):
    timing.record_probe_tick(
        expected_at=expected, actual_at=actual, latency=latency, state=state
    )


class LagEvidenceTest(unittest.TestCase):
    def test_small_lags_count_for_the_maximum_only(self) -> None:
        timing = _timing(_Clock())
        _tick(timing, 0.5, 0.52)
        _tick(timing, 1.0, 1.9)

        evidence = timing.evidence()

        self.assertAlmostEqual(0.9, evidence.max_event_loop_lag_seconds)
        self.assertEqual((), evidence.recent_lags)
        self.assertEqual((), evidence.largest_lags)
        self.assertEqual(2, evidence.probe_ticks)

    def test_recent_and_largest_rings_are_bounded_and_kept_apart(self) -> None:
        timing = _timing(_Clock())
        _tick(timing, 0.0, 30.0)  # the largest, soon evicted from "recent"
        for index in range(RECENT_LAG_CAPACITY + 5):
            start = 100.0 + index * 10
            _tick(timing, start, start + 1.0 + index * 0.01)

        evidence = timing.evidence()

        self.assertEqual(RECENT_LAG_CAPACITY, len(evidence.recent_lags))
        self.assertNotIn(30.0, [lag.lag_seconds for lag in evidence.recent_lags])
        self.assertEqual(LARGEST_LAG_CAPACITY, len(evidence.largest_lags))
        self.assertEqual(30.0, evidence.largest_lags[0].lag_seconds)
        self.assertEqual(30.0, evidence.max_event_loop_lag_seconds)
        # The most recent material lag is last in the recent ring.
        self.assertAlmostEqual(
            100.0 + (RECENT_LAG_CAPACITY + 4) * 10,
            evidence.recent_lags[-1].expected_elapsed,
        )

    def test_lag_is_attributed_to_decisions_by_interval_overlap(self) -> None:
        timing = _timing(_Clock())
        # A decision 10.0 -> 18.0; the probe expected 9.0 and woke at 20.0.
        timing.record_decision(
            request_id=7, time_budget=None, recv_at=9.9, start_at=10.0, end_at=18.0
        )
        _tick(timing, 9.0, 20.0)
        # A stall with no decision at all.
        _tick(timing, 30.0, 33.0)

        evidence = timing.evidence()

        inside, outside = evidence.recent_lags
        self.assertAlmostEqual(8.0, inside.decision_overlap_seconds)
        self.assertAlmostEqual(3.0, inside.outside_decision_seconds)
        self.assertEqual(0.0, outside.decision_overlap_seconds)
        self.assertAlmostEqual(8.0, evidence.max_lag_overlapping_decision_seconds)
        self.assertAlmostEqual(3.0, evidence.max_lag_outside_decision_seconds)

    def test_no_lag_means_no_overlap_facts(self) -> None:
        evidence = _timing(_Clock()).evidence()
        self.assertIsNone(evidence.max_event_loop_lag_seconds)
        self.assertIsNone(evidence.max_lag_overlapping_decision_seconds)
        self.assertIsNone(evidence.max_lag_outside_decision_seconds)


class CloseWindowTest(unittest.TestCase):
    """Only "a material lag ended inside the close window" is asserted.

    No keepalive PING time or deadline is observed, so no test claims that a
    stall crossed the keepalive deadline.
    """

    def test_stall_ending_inside_the_close_window_is_reported(self) -> None:
        timing = _timing(_Clock())
        _tick(timing, 0.5, 0.5)
        _tick(timing, 1.0, 1.0)  # last seen OPEN
        _tick(timing, 1.5, 23.0, state="CLOSING")  # stall ends, closing seen

        evidence = timing.evidence()

        self.assertEqual(1.0, evidence.last_open_seen_elapsed)
        self.assertEqual(23.0, evidence.first_not_open_seen_elapsed)
        self.assertEqual("CLOSING", evidence.first_not_open_state)
        self.assertAlmostEqual(21.5, evidence.max_lag_ending_in_close_window_seconds)
        self.assertIs(True, evidence.material_lag_ended_in_close_window)

    def test_stall_ending_while_still_seen_open_is_in_the_window(self) -> None:
        # The probe may wake up before the close is seen; the window runs from
        # that last OPEN sample, so the stall still ends inside it.
        timing = _timing(_Clock())
        _tick(timing, 0.5, 0.5)
        _tick(timing, 1.0, 22.0)  # stall ends, still OPEN
        _tick(timing, 22.5, 22.5, state="CLOSING")

        self.assertIs(True, timing.evidence().material_lag_ended_in_close_window)

    def test_an_earlier_stall_does_not_count_for_the_close(self) -> None:
        timing = _timing(_Clock())
        _tick(timing, 0.5, 5.0)  # an old stall, the connection stayed OPEN
        _tick(timing, 5.5, 5.5)
        _tick(timing, 30.0, 30.0)
        _tick(timing, 30.5, 30.5, state="CLOSING")

        evidence = timing.evidence()

        self.assertEqual(0.0, evidence.max_lag_ending_in_close_window_seconds)
        self.assertIs(False, evidence.material_lag_ended_in_close_window)
        self.assertAlmostEqual(4.5, evidence.max_event_loop_lag_seconds)

    def test_close_window_not_seen_is_unknown(self) -> None:
        timing = _timing(_Clock())
        _tick(timing, 0.5, 0.5)

        evidence = timing.evidence()

        self.assertIsNone(evidence.max_lag_ending_in_close_window_seconds)
        self.assertIsNone(evidence.material_lag_ended_in_close_window)

    def test_evidence_takes_a_final_state_sample_and_the_open_lag(self) -> None:
        clock = _Clock(100.0)
        state = {"value": "OPEN"}
        timing = _timing(clock, state_source=lambda: state["value"])
        _tick(timing, 0.5, 0.5)
        timing.note_probe_sleep(1.0)
        # The loop stalls; the connection is closed before the probe wakes.
        clock.now = 100.0 + 25.0
        state["value"] = "CLOSED"

        evidence = timing.evidence()

        self.assertEqual(25.0, evidence.first_not_open_seen_elapsed)
        self.assertEqual("CLOSED", evidence.first_not_open_state)
        pending = evidence.recent_lags[-1]
        self.assertTrue(pending.pending)
        self.assertAlmostEqual(24.0, pending.lag_seconds)
        self.assertIs(True, evidence.material_lag_ended_in_close_window)
        self.assertAlmostEqual(24.0, evidence.max_event_loop_lag_seconds)

    def test_unknown_state_names_are_not_kept_as_text(self) -> None:
        timing = _timing(_Clock())
        _tick(timing, 0.5, 0.5, state="free text <x>")
        self.assertEqual("other", timing.evidence().first_not_open_state)


class DecisionAndAckTest(unittest.TestCase):
    def test_decision_ring_is_bounded_and_keeps_budget_and_acks(self) -> None:
        timing = _timing(_Clock())
        for request_id in range(1, DECISION_CAPACITY + 3):
            entry = timing.record_decision(
                request_id=request_id,
                time_budget={"grace_ms": 1000, "bank_ms": 5000.5, "deadline_ms": 1},
                recv_at=request_id,
                start_at=request_id + 0.1,
                end_at=request_id + 0.6,
            )
            timing.record_send_attempt(entry, request_id + 0.7)
        timing.record_ack(DECISION_CAPACITY + 2, "defaulted")
        timing.record_ack(DECISION_CAPACITY + 2, "stale")
        timing.record_ack(1, "accepted")  # evicted request: counted nowhere

        evidence = timing.evidence()

        self.assertEqual(DECISION_CAPACITY, len(evidence.decisions))
        last = evidence.decisions[-1]
        self.assertEqual(DECISION_CAPACITY + 2, last.ordinal)
        self.assertEqual(DECISION_CAPACITY + 2, last.request_id)
        self.assertAlmostEqual(0.5, last.decision_seconds)
        self.assertEqual(
            (1000.0, 5000.5, 1.0), (last.grace_ms, last.bank_ms, last.deadline_ms)
        )
        self.assertEqual(("defaulted", "stale"), last.ack_statuses)
        self.assertEqual(1, evidence.defaulted_ack_count)
        self.assertEqual(1, evidence.stale_ack_count)
        self.assertAlmostEqual(0.5, evidence.max_recent_decision_seconds)

    def test_malformed_values_are_dropped_not_kept(self) -> None:
        timing = _timing(_Clock())
        timing.record_decision(
            request_id=True,
            time_budget={"grace_ms": True, "bank_ms": float("nan"), "deadline_ms": "1"},
            recv_at=0.0,
            start_at=0.0,
            end_at=0.1,
        )
        timing.record_decision(
            request_id=2**60, time_budget="x", recv_at=0.0, start_at=0.0, end_at=0.1
        )
        timing.record_ack(None, "not-a-status")

        first, second = timing.evidence().decisions

        self.assertIsNone(first.request_id)
        self.assertEqual(
            (None, None, None), (first.grace_ms, first.bank_ms, first.deadline_ms)
        )
        self.assertIsNone(second.request_id)
        self.assertIsNone(second.grace_ms)

    def test_acks_per_decision_are_bounded(self) -> None:
        timing = _timing(_Clock())
        timing.record_decision(
            request_id=1, time_budget=None, recv_at=0, start_at=0, end_at=1
        )
        for _ in range(10):
            timing.record_ack(1, "stale")
        evidence = timing.evidence()
        self.assertEqual(4, len(evidence.decisions[0].ack_statuses))
        self.assertEqual(10, evidence.stale_ack_count)


class KeepaliveSampleTest(unittest.TestCase):
    def test_latency_changes_are_sampled_and_bounded(self) -> None:
        timing = _timing(_Clock())
        _tick(timing, 0.5, 0.5, latency=0.0)  # before the first PONG
        _tick(timing, 1.0, 1.0, latency=0.12)
        _tick(timing, 1.5, 1.5, latency=0.12)  # unchanged: not a new sample
        for index in range(KEEPALIVE_CAPACITY + 2):
            _tick(timing, 2.0 + index, 2.0 + index, latency=0.2 + index * 0.1)
        for bad in (None, True, float("inf"), -1.0):
            _tick(timing, 50.0, 50.0, latency=bad)

        evidence = timing.evidence()

        self.assertEqual(KEEPALIVE_CAPACITY + 3, evidence.keepalive_sample_count)
        self.assertEqual(KEEPALIVE_CAPACITY, len(evidence.keepalive_samples))
        self.assertAlmostEqual(
            0.2 + (KEEPALIVE_CAPACITY + 1) * 0.1,
            evidence.max_recent_keepalive_latency_seconds,
        )


class EvidenceDictTest(unittest.TestCase):
    def test_evidence_dict_is_json_numbers_and_fixed_names_only(self) -> None:
        timing = _timing(_Clock())
        entry = timing.record_decision(
            request_id=3,
            time_budget={"grace_ms": 500},
            recv_at=1.0,
            start_at=1.0001,
            end_at=9.12345,
        )
        timing.record_send_attempt(entry, 9.2)
        timing.record_ack(3, "accepted")
        _tick(timing, 1.0, 21.0, latency=0.1234567, state="CLOSING")

        data = timing.evidence().to_evidence_dict()
        rendered = json.dumps(data)

        self.assertEqual(9.123, data["decisions"][0]["decision_end_elapsed"])
        self.assertEqual(["accepted"], data["decisions"][0]["ack_statuses"])
        self.assertTrue(data["material_lag_ended_in_close_window"])
        strings = set()

        def _collect(value):
            if isinstance(value, dict):
                for item in value.values():
                    _collect(item)
            elif isinstance(value, list):
                for item in value:
                    _collect(item)
            elif isinstance(value, str):
                strings.add(value)

        _collect(data)
        self.assertLessEqual(strings, {"CLOSING", "accepted"})
        self.assertLess(len(rendered), 4000)


class ProbeTest(unittest.TestCase):
    def test_probe_samples_until_cancelled_and_cancellation_propagates(
        self,
    ) -> None:
        clock = _Clock(0.0)
        latencies = iter([0.1, 0.1, 0.3])

        async def _sleep(seconds: float) -> None:
            clock.now += seconds + (2.0 if clock.now >= 1.0 else 0.0)
            await asyncio.sleep(0)

        async def _scenario():
            timing = ConnectionTiming(clock=clock)
            probe = asyncio.create_task(
                run_timing_probe(
                    timing,
                    latency_source=lambda: next(latencies, 0.3),
                    state_source=lambda: "OPEN",
                    sleep=_sleep,
                )
            )
            for _ in range(8):
                await asyncio.sleep(0)
            probe.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await probe
            return timing.evidence()

        evidence = asyncio.run(_scenario())

        self.assertGreaterEqual(evidence.probe_ticks, 3)
        self.assertFalse(evidence.probe_failed)
        self.assertEqual(2, evidence.keepalive_sample_count)
        self.assertAlmostEqual(2.0, evidence.max_event_loop_lag_seconds)

    def test_probe_source_error_stops_the_probe_without_raising(self) -> None:
        async def _no_sleep(seconds: float) -> None:
            await asyncio.sleep(0)

        def _broken():
            raise RuntimeError("state source broke")

        async def _scenario():
            timing = ConnectionTiming()
            await run_timing_probe(
                timing,
                latency_source=lambda: None,
                state_source=_broken,
                sleep=_no_sleep,
            )
            return timing.evidence()

        evidence = asyncio.run(_scenario())
        self.assertTrue(evidence.probe_failed)


class _FakeAdapter:
    def __init__(self, self_seat) -> None:
        self.self_seat = self_seat

    def process_request_action(self, raw_request_action):
        return SendReadyResponse(
            request_id=raw_request_action["request_id"],
            action={"type": "none"},
        )


def _fake_adapter_factory(self_seat, policy):
    return _FakeAdapter(self_seat)


def _frames(*events) -> list[str]:
    return [json.dumps(event) for event in events]


def _request_action(request_id: int, **extra) -> dict:
    return {"type": "request_action", "request_id": request_id, **extra}


class DriveSessionTimingTest(unittest.TestCase):
    def _failure(self, transport) -> RawTransportFailure:
        with patch(_PATCH_TARGET, _fake_adapter_factory):
            with self.assertRaises(TransportError) as caught:
                asyncio.run(
                    drive_ranked_session(RankedSession(MinimalPolicy()), transport)
                )
        raw = getattr(caught.exception, "_raw_transport_failure")
        self.assertIsInstance(raw, RawTransportFailure)
        return raw

    def test_decisions_acks_and_budget_reach_the_failure_evidence(self) -> None:
        clock = _Clock(0.0)
        pending = _frames(
            {"type": "start_game", "id": 1},
            _request_action(1, time={"grace_ms": 2000, "bank_ms": 9000}),
            {"type": "action_ack", "request_id": 1, "status": "accepted"},
            _request_action(2),
        )

        class _Transport:
            timing = ConnectionTiming(clock=clock)
            sent: list[str] = []

            async def recv(self):
                clock.now += 1.0
                if pending:
                    return pending.pop(0)
                raise TransportClosed("closed", code_sent=1011)

            async def send(self, message: str) -> None:
                clock.now += 0.25
                self.sent.append(message)

            async def close(self) -> None:
                pass

        raw = self._failure(_Transport())

        self.assertEqual(("in_game", "recv"), (raw.phase, raw.operation))
        timing = raw.timing
        self.assertEqual(2, len(timing.decisions))
        first, second = timing.decisions
        self.assertEqual((1, 1), (first.ordinal, first.request_id))
        self.assertEqual(2.0, first.recv_elapsed)
        # The reader keeps receiving during the decision (Issue #418): this
        # fake never suspends, so all five recvs happen before the send.
        self.assertEqual(5.0, first.send_attempt_elapsed)
        self.assertEqual(
            (2000.0, 9000.0, None), (first.grace_ms, first.bank_ms, first.deadline_ms)
        )
        self.assertEqual(("accepted",), first.ack_statuses)
        self.assertEqual(2, second.request_id)
        self.assertEqual(4.0, second.recv_elapsed)
        self.assertEqual((), second.ack_statuses)
        self.assertEqual(0, timing.defaulted_ack_count)

    def test_send_failure_keeps_the_send_attempt_of_the_failing_decision(
        self,
    ) -> None:
        clock = _Clock(0.0)
        pending = _frames({"type": "start_game", "id": 0}, _request_action(5))

        class _Transport:
            timing = ConnectionTiming(clock=clock)

            async def recv(self):
                return pending.pop(0)

            async def send(self, message: str) -> None:
                raise TransportClosed(
                    "closed", code_sent=1011, reason_sent="keepalive ping timeout"
                )

            async def close(self) -> None:
                pass

        raw = self._failure(_Transport())

        self.assertEqual("send", raw.operation)
        (decision,) = raw.timing.decisions
        self.assertEqual(5, decision.request_id)
        self.assertIsNotNone(decision.send_attempt_elapsed)

    def test_transport_without_timing_still_gets_evidence(self) -> None:
        pending = _frames({"type": "start_game", "id": 0}, _request_action(1))

        class _Transport:
            async def recv(self):
                if pending:
                    return pending.pop(0)
                raise TransportClosed("closed")

            async def send(self, message: str) -> None:
                pass

            async def close(self) -> None:
                pass

        raw = self._failure(_Transport())
        self.assertEqual(1, len(raw.timing.decisions))


class _Connection:
    """A fake `websockets` connection with the public `latency` / `state`."""

    def __init__(self, recv) -> None:
        self._recv = recv
        self.latency = 0.0
        self.state = "OPEN"
        self.closed = False

    async def recv(self):
        return await self._recv(self)

    async def send(self, message: str) -> None:
        pass

    async def close(self) -> None:
        self.closed = True


class ConnectTransportTimingTest(unittest.TestCase):
    def _run(self, recv):
        connection = _Connection(recv)

        async def _connect(url, additional_headers):
            return connection

        leftover: list = []

        async def _scenario():
            try:
                async with connect_transport("wss://example.invalid/ws", _TOKEN) as t:
                    await drive_ranked_session(RankedSession(MinimalPolicy()), t)
            finally:
                leftover.extend(
                    task
                    for task in asyncio.all_tasks()
                    if task is not asyncio.current_task()
                )

        with patch("websockets.connect", _connect):
            with self.assertRaises(TransportError) as caught:
                asyncio.run(_scenario())
        self.assertTrue(connection.closed)
        self.assertEqual([], leftover)
        return caught.exception

    def test_loop_stall_before_a_keepalive_close_ends_in_the_window(self) -> None:
        # A real (short) blocking stall on the event loop, as a synchronous
        # decision would cause, followed by the keepalive close.  The evidence
        # shows the stall ended in the close window, nothing about the PING.
        steps = iter(["start", "stall"])

        async def _recv(connection):
            step = next(steps, "close")
            if step == "start":
                connection.latency = 0.05
                await asyncio.sleep(0.6)  # the probe sees OPEN at ~0.5 s
                return json.dumps({"type": "start_game", "id": 2})
            if step == "stall":
                time.sleep(0.8)  # blocks the loop across a probe wake-up
                connection.state = "CLOSING"
                await asyncio.sleep(0.6)
            raise ConnectionClosedError(None, Close(1011, "keepalive ping timeout"))

        with patch.object(transport_timing, "MATERIAL_LAG_SECONDS", 0.2):
            error = self._run(_recv)

        self.assertIsInstance(error, UnexpectedDisconnectError)
        diagnostics = error.diagnostics
        self.assertEqual("keepalive_timeout", diagnostics.local_close_reason_class)
        timing = diagnostics.timing
        self.assertIsNotNone(timing)
        self.assertIs(True, timing.material_lag_ended_in_close_window)
        self.assertGreaterEqual(timing.max_lag_ending_in_close_window_seconds, 0.2)
        self.assertEqual(0.0, timing.max_lag_overlapping_decision_seconds)
        self.assertEqual("CLOSING", timing.first_not_open_state)
        self.assertEqual(1, timing.keepalive_sample_count)
        self.assertNotIn(_TOKEN, repr(diagnostics))

    def test_probe_is_stopped_and_outer_cancellation_propagates(self) -> None:
        connection = _Connection(None)

        async def _recv(conn):
            await asyncio.sleep(3600)

        connection._recv = _recv

        async def _connect(url, additional_headers):
            return connection

        async def _scenario():
            async def _drive():
                async with connect_transport("wss://example.invalid/ws", _TOKEN) as t:
                    await drive_ranked_session(RankedSession(MinimalPolicy()), t)

            task = asyncio.create_task(_drive())
            await asyncio.sleep(0.05)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            return [
                other
                for other in asyncio.all_tasks()
                if other is not asyncio.current_task()
            ]

        with patch("websockets.connect", _connect):
            leftover = asyncio.run(_scenario())
        self.assertEqual([], leftover)
        self.assertTrue(connection.closed)


if __name__ == "__main__":
    unittest.main()
