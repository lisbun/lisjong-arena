"""Policy decisions run off the WebSocket event loop (Issue #418).

The adapter is replaced by a controllable fake whose decisions block on a
`threading.Event`, so every test decides exactly when a decision finishes.
Deadlines use real but scaled times (tens to hundreds of milliseconds) with
the local safety margin patched to zero.
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
import unittest
from unittest.mock import patch

import websockets
from lisjong.policies import MinimalPolicy

from lisjong_arena.riichilab.adapter import SendReadyResponse
from lisjong_arena.riichilab.decision_pipeline import (
    response_cutoff,
    response_window_seconds,
)
from lisjong_arena.riichilab.errors import (
    DecisionBacklogError,
    UnexpectedDisconnectError,
)
from lisjong_arena.riichilab.session import RankedSession
from lisjong_arena.riichilab.transport import (
    TransportClosed,
    WebSocketTransport,
    drive_ranked_session,
)

_ADAPTER_TARGET = "lisjong_arena.riichilab.session.RiichiLabSeatAdapter"
_MARGIN_TARGET = (
    "lisjong_arena.riichilab.decision_pipeline.RESPONSE_SAFETY_MARGIN_SECONDS"
)
_WAIT = 10.0


def _request_action(request_id: int, **extra) -> dict:
    return {"type": "request_action", "request_id": request_id, **extra}


def _ack(request_id: int, status: str) -> dict:
    return {"type": "action_ack", "request_id": request_id, "status": status}


class _ControlledAdapter:
    """Records every adapter call; decisions can be held and released."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, int]] = []
        self.threads: set[int] = set()
        self.entered: dict[int, threading.Event] = {}
        self.gates: dict[int, threading.Event] = {}
        self.failures: dict[int, Exception] = {}
        self.busy_seconds: dict[int, float] = {}
        self.max_active = 0
        self.finished = threading.Event()
        self._active = 0
        self._lock = threading.Lock()

    def hold(self, request_id: int) -> threading.Event:
        gate = threading.Event()
        self.gates[request_id] = gate
        return gate

    def factory(self, self_seat, policy):
        self.self_seat = self_seat
        return self

    def process_request_action(self, raw_request_action):
        request_id = raw_request_action["request_id"]
        self._enter("decide", request_id)
        try:
            self.entered.setdefault(request_id, threading.Event()).set()
            gate = self.gates.get(request_id)
            if gate is not None:
                gate.wait(_WAIT)
            busy = self.busy_seconds.get(request_id)
            if busy is not None:
                _burn_cpu(busy)
            if request_id in self.failures:
                raise self.failures[request_id]
            return SendReadyResponse(
                request_id=request_id,
                action={
                    "type": "dahai",
                    "actor": int(self.self_seat),
                    "pai": f"decided-{request_id}",
                },
            )
        finally:
            self._exit()

    def synchronize_request_action(self, raw_request_action):
        self._enter("sync", raw_request_action["request_id"])
        self._exit()

    def _enter(self, kind: str, request_id: int) -> None:
        with self._lock:
            self._active += 1
            self.max_active = max(self.max_active, self._active)
            self.calls.append((kind, request_id))
            self.threads.add(threading.get_ident())

    def _exit(self) -> None:
        with self._lock:
            self._active -= 1
        self.finished.set()


def _burn_cpu(seconds: float) -> None:
    """Pure-Python work that holds the GIL except at interpreter switches."""
    end = time.perf_counter() + seconds
    total = 0
    while time.perf_counter() < end:
        for value in range(1000):
            total += value * value


_CLOSED = object()


class _ServerTransport:
    """In-memory `Transport` whose frames the test pushes while it runs."""

    def __init__(self) -> None:
        self._incoming: asyncio.Queue = asyncio.Queue()
        self.sent: list[dict] = []
        self.received = 0

    def push(self, *events) -> None:
        for event in events:
            self._incoming.put_nowait(
                event if isinstance(event, (str, bytes)) else json.dumps(event)
            )

    def disconnect(self) -> None:
        self._incoming.put_nowait(_CLOSED)

    async def recv(self):
        item = await self._incoming.get()
        if item is _CLOSED:
            raise TransportClosed("closed by the fake server")
        self.received += 1
        return item

    async def send(self, message: str) -> None:
        self.sent.append(json.loads(message))

    async def close(self) -> None:
        pass


async def _wait_thread_event(event: threading.Event) -> None:
    if not await asyncio.to_thread(event.wait, _WAIT):
        raise AssertionError("timed out waiting for the adapter")


async def _until(predicate, timeout: float = _WAIT) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError("condition was not reached")
        await asyncio.sleep(0.005)


def _entered(adapter: _ControlledAdapter, request_id: int) -> threading.Event:
    return adapter.entered.setdefault(request_id, threading.Event())


class ResponseWindowTest(unittest.TestCase):
    def test_deadline_ms_is_preferred(self) -> None:
        window = response_window_seconds(
            {"grace_ms": 3000, "bank_ms": 15000, "deadline_ms": 12000}
        )
        self.assertEqual(12.0, window)

    def test_grace_plus_bank_without_deadline(self) -> None:
        self.assertEqual(
            3.5, response_window_seconds({"grace_ms": 3000, "bank_ms": 500})
        )

    def test_unknown_window(self) -> None:
        for time_value in (
            None,
            {},
            {"grace_ms": 500},
            {"deadline_ms": True},
            {"deadline_ms": float("nan")},
            {"deadline_ms": float("inf")},
            [3000],
        ):
            with self.subTest(time_value=time_value):
                self.assertIsNone(response_window_seconds(time_value))

    def test_cutoff_is_relative_to_local_recv_minus_margin(self) -> None:
        self.assertEqual(10.0 + 3.0 - 0.5, response_cutoff(10.0, {"deadline_ms": 3000}))
        self.assertIsNone(response_cutoff(10.0, None))


class SlowPolicyTest(unittest.TestCase):
    def _run(self, scenario, adapter: _ControlledAdapter):
        async def _main():
            transport = _ServerTransport()
            session = RankedSession(MinimalPolicy())
            drive = asyncio.create_task(drive_ranked_session(session, transport))
            try:
                await asyncio.wait_for(scenario(transport, drive), _WAIT)
            finally:
                for gate in adapter.gates.values():
                    gate.set()
                if not drive.done():
                    drive.cancel()
                await asyncio.gather(drive, return_exceptions=True)
            return transport, session

        with patch(_ADAPTER_TARGET, adapter.factory), patch(_MARGIN_TARGET, 0.0):
            return asyncio.run(_main())

    def test_loop_keeps_receiving_while_policy_is_blocked(self) -> None:
        adapter = _ControlledAdapter()
        gate = adapter.hold(1)
        ticks: list[float] = []

        async def scenario(transport, drive):
            transport.push({"type": "start_game", "id": 0}, _request_action(1))
            await _wait_thread_event(_entered(adapter, 1))

            async def heartbeat():
                for _ in range(10):
                    ticks.append(time.monotonic())
                    await asyncio.sleep(0.01)

            # More frames than the websockets receive queue holds (16).
            transport.push(*({"type": "server_chatter", "n": n} for n in range(40)))
            await heartbeat()
            await _until(lambda: transport.received == 42)
            self.assertEqual([], transport.sent)
            gate.set()
            transport.push(_ack(1, "accepted"), {"type": "end_game"})
            await drive

        transport, session = self._run(scenario, adapter)
        self.assertEqual(10, len(ticks))
        self.assertEqual([1], [sent["request_id"] for sent in transport.sent])
        self.assertEqual({1: ("accepted",)}, session.status().ack_history)

    def test_cpu_bound_policy_does_not_starve_the_event_loop(self) -> None:
        adapter = _ControlledAdapter()
        adapter.busy_seconds[1] = 1.0
        gaps: list[float] = []

        async def scenario(transport, drive):
            transport.push({"type": "start_game", "id": 0}, _request_action(1))
            await _wait_thread_event(_entered(adapter, 1))
            last = time.monotonic()
            while not adapter.finished.is_set():
                await asyncio.sleep(0.01)
                now = time.monotonic()
                gaps.append(now - last)
                last = now
            transport.push({"type": "end_game"})
            await drive

        transport, _ = self._run(scenario, adapter)
        self.assertGreater(len(gaps), 10)
        # A blocked loop would show one ~1 s gap; interpreter switching keeps
        # it far below that.  Generous bound: no wall-clock flakiness.
        self.assertLess(max(gaps), 0.5)
        self.assertEqual([1], [sent["request_id"] for sent in transport.sent])

    def test_result_after_local_cutoff_is_discarded(self) -> None:
        adapter = _ControlledAdapter()
        gate = adapter.hold(1)

        async def scenario(transport, drive):
            transport.push(
                {"type": "start_game", "id": 0},
                _request_action(1, time={"deadline_ms": 50}),
            )
            await _wait_thread_event(_entered(adapter, 1))
            await asyncio.sleep(0.15)
            # Past the cutoff the loop moves on and accepts the next request,
            # which waits behind the still-running decision for request 1.
            transport.push(_request_action(2, time={"deadline_ms": 60000}))
            await _until(lambda: transport.received == 3)
            await asyncio.sleep(0.05)
            gate.set()
            await _until(lambda: len(transport.sent) == 1)
            transport.push(_ack(2, "accepted"), {"type": "end_game"})
            await drive

        transport, session = self._run(scenario, adapter)

        self.assertEqual([("decide", 1), ("decide", 2)], adapter.calls)
        self.assertEqual(
            [{"type": "dahai", "actor": 0, "pai": "decided-2", "request_id": 2}],
            transport.sent,
        )
        self.assertEqual(1, session.status().responses_sent)

    def test_defaulted_request_is_not_sent_and_backlog_is_only_synchronized(
        self,
    ) -> None:
        adapter = _ControlledAdapter()
        gate = adapter.hold(1)

        async def scenario(transport, drive):
            transport.push({"type": "start_game", "id": 0}, _request_action(1))
            await _wait_thread_event(_entered(adapter, 1))
            transport.push(
                _ack(1, "defaulted"),
                _request_action(2),
                _ack(2, "defaulted"),
                _request_action(3),
            )
            await _until(lambda: transport.received == 6)
            await asyncio.sleep(0.05)
            gate.set()
            await _until(lambda: len(transport.sent) == 1)
            transport.push(_ack(3, "accepted"), {"type": "end_game"})
            await drive

        transport, session = self._run(scenario, adapter)
        # Only the newest answerable request reaches the Policy; the defaulted
        # one in between is still applied to the adapter state, in order.
        self.assertEqual([("decide", 1), ("sync", 2), ("decide", 3)], adapter.calls)
        self.assertEqual([3], [sent["request_id"] for sent in transport.sent])
        self.assertEqual(
            {1: ("defaulted",), 2: ("defaulted",), 3: ("accepted",)},
            session.status().ack_history,
        )

    def test_stale_ack_also_stops_the_late_result(self) -> None:
        adapter = _ControlledAdapter()
        gate = adapter.hold(1)

        async def scenario(transport, drive):
            transport.push({"type": "start_game", "id": 0}, _request_action(1))
            await _wait_thread_event(_entered(adapter, 1))
            transport.push(_ack(1, "stale"), {"type": "end_game"})
            await _until(lambda: transport.received == 4)
            await asyncio.sleep(0.05)
            gate.set()
            await drive

        transport, _ = self._run(scenario, adapter)
        self.assertEqual([], transport.sent)

    def test_adapter_calls_are_serial_on_one_worker_thread(self) -> None:
        adapter = _ControlledAdapter()
        gate = adapter.hold(1)

        async def scenario(transport, drive):
            transport.push({"type": "start_game", "id": 0}, _request_action(1))
            await _wait_thread_event(_entered(adapter, 1))
            for request_id in range(2, 8):
                transport.push(_ack(request_id - 1, "defaulted"))
                transport.push(_request_action(request_id))
            await _until(lambda: transport.received == 14)
            await asyncio.sleep(0.05)
            gate.set()
            await _until(lambda: len(transport.sent) == 1)
            transport.push({"type": "end_game"})
            await drive

        transport, _ = self._run(scenario, adapter)
        self.assertEqual(1, adapter.max_active)
        self.assertEqual(1, len(adapter.threads))
        self.assertNotIn(threading.get_ident(), adapter.threads)
        self.assertEqual(
            [("decide", 1)] + [("sync", n) for n in range(2, 7)] + [("decide", 7)],
            adapter.calls,
        )
        self.assertEqual([7], [sent["request_id"] for sent in transport.sent])

    def test_pending_request_backlog_is_bounded(self) -> None:
        adapter = _ControlledAdapter()
        gate = adapter.hold(1)
        outcome: list[bool] = []

        async def scenario(transport, drive):
            transport.push({"type": "start_game", "id": 0}, _request_action(1))
            await _wait_thread_event(_entered(adapter, 1))
            for request_id in range(2, 6):
                transport.push(_ack(request_id - 1, "defaulted"))
                transport.push(_request_action(request_id))
            await _until(lambda: transport.received == 10)
            await asyncio.sleep(0.05)
            # The overflow is detected, but the call waits for the worker.
            outcome.append(drive.done())
            gate.set()
            with self.assertRaises(DecisionBacklogError):
                await drive
            outcome.append(adapter.finished.is_set())

        with patch("lisjong_arena.riichilab.transport.MAX_PENDING_REQUESTS", 3):
            transport, _ = self._run(scenario, adapter)
        self.assertEqual([False, True], outcome)
        self.assertEqual([], transport.sent)
        self.assertEqual([("decide", 1)], adapter.calls)

    def test_received_frame_backlog_is_bounded(self) -> None:
        adapter = _ControlledAdapter()
        adapter.hold(1)

        async def scenario(transport, drive):
            transport.push({"type": "start_game", "id": 0}, _request_action(1))
            await _wait_thread_event(_entered(adapter, 1))
            transport.push(*({"type": "server_chatter"} for _ in range(10)))
            await _until(lambda: transport.received >= 7)
            adapter.gates[1].set()
            with self.assertRaises(DecisionBacklogError):
                await drive

        with patch("lisjong_arena.riichilab.decision_pipeline.MAX_BUFFERED_FRAMES", 5):
            transport, _ = self._run(scenario, adapter)
        self.assertEqual([], transport.sent)

    def test_policy_failure_after_discard_still_fails_closed(self) -> None:
        adapter = _ControlledAdapter()
        gate = adapter.hold(1)
        adapter.failures[1] = RuntimeError("policy exploded")

        async def scenario(transport, drive):
            transport.push(
                {"type": "start_game", "id": 0},
                _request_action(1, time={"deadline_ms": 20}),
            )
            await _wait_thread_event(_entered(adapter, 1))
            await asyncio.sleep(0.1)
            transport.push({"type": "end_game"})
            await asyncio.sleep(0.05)
            self.assertFalse(drive.done())
            gate.set()
            with self.assertRaisesRegex(RuntimeError, "policy exploded"):
                await drive

        transport, session = self._run(scenario, adapter)
        self.assertEqual([], transport.sent)
        self.assertTrue(session.status().end_game_received)

    def test_disconnect_during_slow_decision_is_reported_in_order(self) -> None:
        adapter = _ControlledAdapter()
        gate = adapter.hold(1)

        async def scenario(transport, drive):
            transport.push({"type": "start_game", "id": 0}, _request_action(1))
            await _wait_thread_event(_entered(adapter, 1))
            transport.disconnect()
            await asyncio.sleep(0.05)
            self.assertFalse(drive.done())
            gate.set()
            with self.assertRaises(UnexpectedDisconnectError) as caught:
                await drive
            raw = caught.exception._raw_transport_failure
            self.assertEqual(("in_game", "recv"), (raw.phase, raw.operation))
            self.assertEqual(1, raw.requests_received)

        self._run(scenario, adapter)


class RealKeepaliveTest(unittest.TestCase):
    """A real `websockets` connection with scaled keepalive timing.

    The client pings every 0.1 s and gives up after 0.3 s without a pong.  The
    Policy takes 1.5 s (5x the ping timeout, the scaled analogue of a >20 s
    decision against the default 20 s timeout) while the server floods more
    frames than the client's receive queue holds.
    """

    _PING_INTERVAL = 0.1
    _PING_TIMEOUT = 0.3
    _POLICY_SECONDS = 1.5

    def _serve(self, handler):
        return websockets.serve(handler, "127.0.0.1", 0, ping_interval=None)

    async def _flooding_game(self, websocket, responses: list) -> None:
        await websocket.send(json.dumps({"type": "start_game", "id": 0}))
        await websocket.send(
            json.dumps(_request_action(1, time={"deadline_ms": 60000}))
        )
        await asyncio.sleep(0.05)
        for n in range(40):
            await websocket.send(json.dumps({"type": "server_chatter", "n": n}))
        try:
            responses.append(json.loads(await websocket.recv()))
            await websocket.send(json.dumps(_ack(1, "accepted")))
            await websocket.send(json.dumps({"type": "end_game"}))
        except websockets.exceptions.ConnectionClosed:
            return
        await websocket.wait_closed()

    def test_slow_policy_does_not_cause_keepalive_timeout(self) -> None:
        adapter = _ControlledAdapter()
        adapter.busy_seconds[1] = self._POLICY_SECONDS
        responses: list = []

        async def _main():
            async with self._serve(
                lambda ws: self._flooding_game(ws, responses)
            ) as server:
                port = server.sockets[0].getsockname()[1]
                connection = await websockets.connect(
                    f"ws://127.0.0.1:{port}",
                    ping_interval=self._PING_INTERVAL,
                    ping_timeout=self._PING_TIMEOUT,
                )
                try:
                    session = RankedSession(MinimalPolicy())
                    await asyncio.wait_for(
                        drive_ranked_session(session, WebSocketTransport(connection)),
                        _WAIT,
                    )
                    self.assertIsNone(connection.close_code)
                finally:
                    await connection.close()
                return session

        with patch(_ADAPTER_TARGET, adapter.factory):
            session = asyncio.run(_main())
        self.assertEqual([1], [response["request_id"] for response in responses])
        self.assertTrue(session.status().end_game_received)

    def test_control_unread_queue_loses_the_connection(self) -> None:
        """Sanity check of the scenario: not draining recv trips keepalive."""

        async def _main():
            async with self._serve(lambda ws: self._flooding_game(ws, [])) as server:
                port = server.sockets[0].getsockname()[1]
                connection = await websockets.connect(
                    f"ws://127.0.0.1:{port}",
                    ping_interval=self._PING_INTERVAL,
                    ping_timeout=self._PING_TIMEOUT,
                    close_timeout=0.2,
                )
                try:
                    await asyncio.sleep(self._POLICY_SECONDS)
                    # Reading is paused, so the closing handshake cannot
                    # finish; the close frame this side sent shows why.
                    close_sent = connection.protocol.close_sent
                    return None if close_sent is None else close_sent.code
                finally:
                    await connection.close()

        self.assertEqual(1011, asyncio.run(_main()))


if __name__ == "__main__":
    unittest.main()
