"""#418/#421: real driver, adapter, record and continuous paths; fake wire only."""

from __future__ import annotations

import asyncio
import threading
import unittest
from contextlib import asynccontextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from lisjong.policies import MinimalPolicy
from test_riichilab_adapter import (
    _consecutive_seat_observations,
    _dahai_request_action,
)
from test_riichilab_decision_pipeline import (
    _ack,
    _ServerTransport,
    _until,
    _wait_thread_event,
)

from lisjong_arena.riichilab.continuous_ranked import run_continuous_ranked
from lisjong_arena.riichilab.durable_ranked_game_record import (
    DurableRankedGameRecordError,
    iter_ranked_decisions,
    load_ranked_game_record,
)
from lisjong_arena.riichilab.profile import RuntimeProfile
from lisjong_arena.riichilab.transport_timing import ConnectionTiming


class _HeldPolicy:
    def __init__(self, held: bool) -> None:
        self.entered = threading.Event()
        self.release = threading.Event()
        self.inputs = []
        if not held:
            self.release.set()

    def choose_action(self, decision):
        self.inputs.append(decision.input)
        self.entered.set()
        if not self.release.wait(10):
            raise AssertionError("test did not release Policy")
        return MinimalPolicy().choose_action(decision)


class PipelineRecordIntegrationTest(unittest.TestCase):
    def _run(self, root: Path, mode: str):
        policies = []
        transports = []
        requests = [
            _dahai_request_action(observation, index)
            for index, observation in enumerate(_consecutive_seat_observations(0, 3), 1)
        ]

        def factory():
            policy = _HeldPolicy(held=not policies)
            policies.append(policy)
            return policy

        profile = RuntimeProfile(
            name="pipeline-record-test",
            credential_env_var="TEST_TOKEN",
            policy_factory=factory,
            runtime_namespace="pipeline-record-test",
        )
        end_game = {"type": "end_game", "scores": [30000, 25000, 20000, 25000]}

        @asynccontextmanager
        async def connect(url, token):
            transport = _ServerTransport()
            now = [0.0]
            transport.timing = ConnectionTiming(clock=lambda: now[0])
            transports.append(transport)
            policy = policies[-1]

            async def server():
                first = dict(requests[0], time={"deadline_ms": 60000})
                transport.push({"type": "start_game", "id": 0}, first)
                await _wait_thread_event(policy.entered)
                if len(transports) == 1:
                    if mode == "late_ack":
                        transport.push(_ack(1, "defaulted"))
                    else:
                        # Advance the local clock beyond cutoff and wake the reader.
                        now[0] = 61.0
                        transport.push({"type": "server_chatter"})
                    # Request 2 waits behind request 1 and must be synchronized
                    # without calling Policy. Request 3 must see its state.
                    transport.push(requests[1], _ack(2, "defaulted"), requests[2])
                    await _until(lambda: transport.received >= 6)
                    if mode == "local_cutoff":
                        # Evidence arrives after the following requests.
                        transport.push(_ack(1, "defaulted"))
                    elif mode == "stale":
                        transport.push(_ack(1, "stale"))
                    policy.release.set()
                    await _until(lambda: len(transport.sent) == 1)
                    transport.push(_ack(3, "accepted"), end_game)
                else:
                    await _until(lambda: len(transport.sent) == 1)
                    transport.push(_ack(1, "accepted"), end_game)

            task = asyncio.create_task(server())
            try:
                yield transport
                await task
            finally:
                policy.release.set()
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

        with patch("lisjong_arena.riichilab.ranked.connect_ranked_transport", connect):
            summary = asyncio.run(
                run_continuous_ranked(
                    profile, "fixture-token", record_dir=root, max_completed_games=2
                )
            )
        records = [load_ranked_game_record(path) for path in root.iterdir()]
        return summary, records, policies, transports

    def test_defaulted_game_is_saved_and_next_game_runs(self):
        for reason in ("local_cutoff", "late_ack"):
            with self.subTest(reason=reason), TemporaryDirectory() as raw:
                summary, records, policies, transports = self._run(Path(raw), reason)
                self.assertEqual(2, summary.completed_games)
                self.assertEqual(0, summary.failed_games)
                self.assertEqual(2, len(records))
                record = next(r for r in records if r.result.unanswered_requests)
                self.assertEqual(
                    {1: reason, 2: "late_ack"}, record.result.unanswered_requests
                )
                decisions = iter_ranked_decisions(record)
                self.assertEqual([None, None], [d.sent_action for d in decisions[:2]])
                self.assertEqual(3, decisions[2].sent_action["request_id"])
                self.assertEqual([3], [s["request_id"] for s in transports[0].sent])
                self.assertEqual([2, 1], [len(p.inputs) for p in policies])

    def test_missing_or_stale_only_evidence_stops_without_publishing(self):
        for mode in ("missing", "stale"):
            with self.subTest(mode=mode), TemporaryDirectory() as raw:
                with self.assertRaisesRegex(
                    DurableRankedGameRecordError, "server outcome is unconfirmed"
                ):
                    self._run(Path(raw), mode)
                self.assertEqual([], list(Path(raw).iterdir()))
