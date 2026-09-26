"""RiichiLab ranked live presentation seamのfocused test
(`lisbun/lisjong-play#41` cross-repo prerequisite)。

real RiichiLab networkへは接続せず、fake transportとsynthetic ranked event
streamだけを使う。確認する境界は次のとおり。

- presentation consumerを渡さない既存behaviorが完全に不変であること
- observer有無で同じevent streamに対するPolicy responseが同一であること
- presentation decisionがexact `request_id` / bound seatへbindされること
- presentationの`PolicyInput`がPolicyへ実際に渡したdecisionの正本そのもの
  であること
- token / credential / Authorization / raw transport payloadを
  presentation factが持たないこと
- bounded bufferがunbounded growthしないこと、slow / detached consumerが
  ranked execution pathをblockまたはabortしないこと
- terminal fact(completion / failure)が1 runにつき一意にdeliveryされ、
  silentにdropもされないこと
- durable acquisitionへpresentationを付けてもrecord readback semanticsが
  不変であること

Policy / Adapter / Session lifecycle自体のsemanticsはここで再検証せず、
既存testの責務のままとする。
"""

import asyncio
import json
import threading
import unittest
from contextlib import asynccontextmanager
from dataclasses import fields
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from lisjong.policies import MinimalPolicy
from lisjong.policy_contract import PolicyInput, Seat
from riichienv import RiichiEnv

from lisjong_arena.riichienv.adapter import tile_from_physical_id, tile_to_mjai
from lisjong_arena.riichilab.adapter import RiichiLabSeatAdapter
from lisjong_arena.riichilab.durable_ranked_game_record import (
    PROTOCOL_TRACE_FILENAME,
    acquire_ranked_game_record,
    load_ranked_game_record,
    summarize_ranked_game_record,
)
from lisjong_arena.riichilab.errors import UnexpectedDisconnectError
from lisjong_arena.riichilab.live_presentation import (
    DEFAULT_DECISION_BUFFER_CAPACITY,
    BoundedRankedPresentationBuffer,
    ContinuousRankedPresentationFeed,
    RankedCompletionPresentation,
    RankedDecisionPresentation,
    RankedFailurePresentation,
    RankedGamePresentation,
)
from lisjong_arena.riichilab.ranked import run_ranked_game
from lisjong_arena.riichilab.session import RankedSession, ValidationSession
from lisjong_arena.riichilab.trace import ProtocolTraceError
from lisjong_arena.riichilab.transport import TransportClosed

_TOKEN = "test-only-bot-token-value"
_FINAL_SCORES = [32000, 24000, 23000, 21000]


def _reset_observation(seed: int = 7):
    env = RiichiEnv(seed=seed, game_mode="4p-red-east")
    return next(iter(env.reset().values()))


def _dahai_request_action(observation, request_id: int) -> dict:
    """打牌局面専用の最小fixture helper(`test_riichilab_adapter.py`と同形)。"""
    return {
        "type": "request_action",
        "request_id": request_id,
        "possible_actions": [
            {"type": "dahai", "pai": tile_to_mjai(tile_from_physical_id(tile))}
            for tile in sorted(set(observation.hand))
        ],
        "observation": observation.serialize_to_base64(),
    }


def _ranked_messages(observation, request_ids=(1,), *, end_game: bool = True):
    """synthetic ranked event stream(text frame)。"""
    messages = [json.dumps({"type": "start_game", "id": 0})]
    for request_id in request_ids:
        messages.append(json.dumps(_dahai_request_action(observation, request_id)))
        messages.append(
            json.dumps(
                {"type": "action_ack", "request_id": request_id, "status": "accepted"}
            )
        )
    if end_game:
        messages.append(json.dumps({"type": "end_game", "scores": _FINAL_SCORES}))
    return messages


class _FakeTransport:
    """`Transport` protocolのtest double。tokenもAuthorization headerも持たない。"""

    def __init__(self, incoming) -> None:
        self._incoming = list(incoming)
        self.sent: list[str] = []

    async def recv(self) -> str:
        if not self._incoming:
            raise TransportClosed("no more fake messages queued")
        return self._incoming.pop(0)

    async def send(self, message: str) -> None:
        self.sent.append(message)

    async def close(self) -> None:
        return None


def _fake_connect(transport: _FakeTransport):
    @asynccontextmanager
    async def _connect(url: str, token: str):
        yield transport

    return _connect


def _run_ranked(messages, *, presentation=None, trace_path=None):
    """fake transport上で1 ranked runを実行するshared helper。"""
    transport = _FakeTransport(messages)
    with patch(
        "lisjong_arena.riichilab.ranked.connect_ranked_transport",
        _fake_connect(transport),
    ):
        result = asyncio.run(
            run_ranked_game(
                MinimalPolicy(),
                _TOKEN,
                presentation=presentation,
                trace_path=trace_path,
            )
        )
    return result, transport


class _FailingCloseTraceWriter:
    """openには成功し`close()`でだけ失敗するtrace writer double。"""

    def __init__(self, path) -> None:
        self.path = path

    def record(self, direction, event_type, payload) -> None:
        return None

    def close(self) -> None:
        raise ProtocolTraceError("failed to close protocol trace file")


class _FailingOpenTraceWriter:
    """constructionの時点で失敗するtrace writer double。"""

    def __init__(self, path) -> None:
        raise ProtocolTraceError("failed to open protocol trace file")


class _RecordingPolicy:
    """Policyへ実際に渡されたdecisionを記録するだけのtest double。"""

    def __init__(self) -> None:
        self.decisions = []
        self.selected = []

    def choose_action(self, decision):
        self.decisions.append(decision)
        action = MinimalPolicy().choose_action(decision)
        self.selected.append(action)
        return action


def _decision_fact(
    *, request_id: int = 1, seat: Seat = Seat.SEAT_0
) -> RankedDecisionPresentation:
    """実Adapterが生成するのと同じ経路でdecision factを1件作る。"""
    observation = _reset_observation()
    adapter = RiichiLabSeatAdapter(seat, MinimalPolicy())
    processed = adapter.process_request_action_with_decision_facts(
        _dahai_request_action(observation, request_id)
    )
    return RankedDecisionPresentation(
        request_id=processed.response.request_id,
        self_seat=seat,
        policy_input=processed.policy_input,
        selected_action=processed.selected_action,
    )


class PresentationFactContractTest(unittest.TestCase):
    def test_decision_fact_exposes_exactly_the_agreed_player_visible_fields(
        self,
    ) -> None:
        self.assertEqual(
            [field.name for field in fields(RankedDecisionPresentation)],
            ["request_id", "self_seat", "policy_input", "selected_action"],
        )

    def test_decision_fact_carries_no_credential_or_raw_transport_payload(self) -> None:
        observation = _reset_observation()
        raw_observation = observation.serialize_to_base64()
        fact = _decision_fact()

        names = {field.name.lower() for field in fields(RankedDecisionPresentation)}
        for forbidden in (
            "token",
            "authorization",
            "credential",
            "observation",
            "websocket",
            "transport",
            "raw",
        ):
            self.assertNotIn(forbidden, names)

        rendered = repr(fact)
        self.assertNotIn(_TOKEN, rendered)
        self.assertNotIn("Bearer", rendered)
        self.assertNotIn(raw_observation, rendered)
        self.assertIsInstance(fact.policy_input, PolicyInput)

    def test_decision_fact_rejects_a_policy_input_from_another_seat(self) -> None:
        fact = _decision_fact(seat=Seat.SEAT_0)
        with self.assertRaises(ValueError):
            RankedDecisionPresentation(
                request_id=fact.request_id,
                self_seat=Seat.SEAT_2,
                policy_input=fact.policy_input,
                selected_action=fact.selected_action,
            )

    def test_decision_fact_rejects_a_non_integer_request_id(self) -> None:
        fact = _decision_fact()
        for bad_request_id in (True, "1", 1.0):
            with self.assertRaises(TypeError):
                RankedDecisionPresentation(
                    request_id=bad_request_id,
                    self_seat=fact.self_seat,
                    policy_input=fact.policy_input,
                    selected_action=fact.selected_action,
                )

    def test_completion_fact_keeps_scores_optional_and_fail_closed(self) -> None:
        self.assertIsNone(
            RankedCompletionPresentation(self_seat=Seat.SEAT_0, scores=None).scores
        )
        with self.assertRaises(TypeError):
            RankedCompletionPresentation(self_seat=Seat.SEAT_0, scores=(1, 2, 3))
        with self.assertRaises(TypeError):
            RankedCompletionPresentation(
                self_seat=Seat.SEAT_0, scores=(1, 2, 3, "25000")
            )

    def test_failure_fact_carries_only_an_exception_type_name(self) -> None:
        self.assertEqual(
            [field.name for field in fields(RankedFailurePresentation)],
            ["failure_type"],
        )
        with self.assertRaises(TypeError):
            RankedFailurePresentation(failure_type="")


class BoundedBufferTest(unittest.TestCase):
    def test_capacity_is_explicit_finite_and_positive(self) -> None:
        self.assertEqual(
            BoundedRankedPresentationBuffer().capacity,
            DEFAULT_DECISION_BUFFER_CAPACITY,
        )
        with self.assertRaises(ValueError):
            BoundedRankedPresentationBuffer(capacity=0)
        with self.assertRaises(TypeError):
            BoundedRankedPresentationBuffer(capacity=True)
        with self.assertRaises(TypeError):
            BoundedRankedPresentationBuffer(capacity=None)

    def test_decision_snapshots_coalesce_instead_of_growing_unbounded(self) -> None:
        buffer = BoundedRankedPresentationBuffer(capacity=4)
        template = _decision_fact()
        published = 40
        for request_id in range(1, published + 1):
            buffer.publish_decision(
                RankedDecisionPresentation(
                    request_id=request_id,
                    self_seat=template.self_seat,
                    policy_input=template.policy_input,
                    selected_action=template.selected_action,
                )
            )
            self.assertLessEqual(buffer.pending_decisions, 4)

        self.assertEqual(buffer.pending_decisions, 4)
        self.assertEqual(buffer.total_coalesced_decisions, published - 4)

        batch = buffer.drain()
        # 最新のcumulative snapshotだけが残り、古いものからcoalesceされる。
        self.assertEqual(
            [decision.request_id for decision in batch.decisions], [37, 38, 39, 40]
        )
        self.assertEqual(batch.coalesced_decisions, published - 4)
        self.assertEqual(buffer.pending_decisions, 0)
        self.assertTrue(buffer.drain().is_empty)

    def test_terminal_facts_survive_a_flooded_decision_buffer(self) -> None:
        buffer = BoundedRankedPresentationBuffer(capacity=2)
        template = _decision_fact()
        buffer.publish_completion(
            RankedCompletionPresentation(
                self_seat=Seat.SEAT_0, scores=(32000, 24000, 23000, 21000)
            )
        )
        for request_id in range(1, 51):
            buffer.publish_decision(
                RankedDecisionPresentation(
                    request_id=request_id,
                    self_seat=template.self_seat,
                    policy_input=template.policy_input,
                    selected_action=template.selected_action,
                )
            )

        batch = buffer.drain()
        self.assertIsNotNone(batch.completion)
        self.assertEqual(batch.completion.scores, (32000, 24000, 23000, 21000))
        self.assertEqual(len(batch.decisions), 2)

    def test_detached_buffer_accepts_publishes_as_no_ops(self) -> None:
        buffer = BoundedRankedPresentationBuffer(capacity=2)
        buffer.detach()
        buffer.detach()

        self.assertFalse(buffer.is_attached)
        buffer.publish_decision(_decision_fact())
        buffer.publish_completion(
            RankedCompletionPresentation(self_seat=Seat.SEAT_0, scores=None)
        )
        buffer.publish_failure(RankedFailurePresentation(failure_type="ProtocolError"))

        self.assertTrue(buffer.drain().is_empty)

    def test_buffer_rejects_foreign_fact_types(self) -> None:
        buffer = BoundedRankedPresentationBuffer(capacity=2)
        with self.assertRaises(TypeError):
            buffer.publish_decision(object())
        with self.assertRaises(TypeError):
            buffer.publish_completion(object())
        with self.assertRaises(TypeError):
            buffer.publish_failure(object())


class ContinuousPresentationFeedTest(unittest.TestCase):
    """Issue #381: continuous runner用per-game buffer handoff。"""

    def _completion(self) -> RankedCompletionPresentation:
        return RankedCompletionPresentation(self_seat=Seat.SEAT_0, scores=None)

    def test_current_is_none_before_first_game(self) -> None:
        self.assertIsNone(ContinuousRankedPresentationFeed().current())

    def test_each_game_gets_a_fresh_buffer_with_increasing_ordinal(self) -> None:
        feed = ContinuousRankedPresentationFeed(decision_capacity=3)
        first = feed.open_game()
        current = feed.current()
        self.assertIsInstance(current, RankedGamePresentation)
        self.assertEqual(current.game_ordinal, 1)
        self.assertIs(current.buffer, first)
        self.assertEqual(first.capacity, 3)

        second = feed.open_game()
        self.assertIsNot(second, first)
        self.assertEqual(feed.current().game_ordinal, 2)
        self.assertIs(feed.current().buffer, second)

    def test_previous_game_terminal_survives_next_game_open(self) -> None:
        """consumerがordinal変化を見てから旧bufferを最終drainできること。"""
        feed = ContinuousRankedPresentationFeed()
        first = feed.open_game()
        consumer_handle = feed.current()
        first.publish_completion(self._completion())
        feed.open_game()

        self.assertEqual(feed.current().game_ordinal, 2)
        batch = consumer_handle.buffer.drain()
        self.assertEqual(batch.completion, self._completion())

    def test_second_game_terminal_is_not_blocked_by_undrained_first(self) -> None:
        feed = ContinuousRankedPresentationFeed()
        feed.open_game().publish_completion(self._completion())
        second = feed.open_game()
        second.publish_failure(RankedFailurePresentation("TransportError"))
        batch = feed.current().buffer.drain()
        self.assertIsNone(batch.completion)
        self.assertEqual(batch.failure.failure_type, "TransportError")

    def test_detach_detaches_current_and_future_buffers_without_raising(
        self,
    ) -> None:
        feed = ContinuousRankedPresentationFeed()
        first = feed.open_game()
        feed.detach()
        feed.detach()
        self.assertFalse(feed.is_attached)
        self.assertFalse(first.is_attached)

        later = feed.open_game()
        self.assertFalse(later.is_attached)
        later.publish_completion(self._completion())
        self.assertTrue(later.drain().is_empty)
        self.assertEqual(feed.current().game_ordinal, 2)

    def test_invalid_capacity_fails_closed(self) -> None:
        for invalid in (0, -1, True, 1.5):
            with self.subTest(invalid=invalid):
                with self.assertRaises((TypeError, ValueError)):
                    ContinuousRankedPresentationFeed(decision_capacity=invalid)

    def test_game_handle_validates_fields(self) -> None:
        buffer = BoundedRankedPresentationBuffer()
        for invalid in (0, True, "1"):
            with self.subTest(invalid=invalid):
                with self.assertRaises((TypeError, ValueError)):
                    RankedGamePresentation(game_ordinal=invalid, buffer=buffer)
        with self.assertRaises(TypeError):
            RankedGamePresentation(game_ordinal=1, buffer=object())


class RankedSessionPresentationSeamTest(unittest.TestCase):
    def _drive(self, session, observation, request_ids):
        outgoing = []
        session.handle_event({"type": "start_game", "id": 0})
        for request_id in request_ids:
            outgoing.append(
                session.handle_event(_dahai_request_action(observation, request_id))
            )
            session.handle_event(
                {"type": "action_ack", "request_id": request_id, "status": "accepted"}
            )
        session.handle_event({"type": "end_game", "scores": _FINAL_SCORES})
        return outgoing

    def test_session_without_presentation_keeps_the_existing_adapter_path(
        self,
    ) -> None:
        """observerなしでは既存の`process_request_action()`だけを通り、

        decision factのpublishが一度も起きないことを固定する。
        """
        observation = _reset_observation()
        original = RiichiLabSeatAdapter.process_request_action
        adapter_calls = []
        published = []

        def _counting(self, raw_request_action):
            adapter_calls.append(raw_request_action["request_id"])
            return original(self, raw_request_action)

        def _recording_publish(self, processed):
            published.append(processed)

        with (
            patch.object(RiichiLabSeatAdapter, "process_request_action", _counting),
            patch.object(
                RankedSession, "_publish_decision_presentation", _recording_publish
            ),
        ):
            session = RankedSession(MinimalPolicy())
            outgoing = self._drive(session, observation, (1, 2))

        self.assertEqual(adapter_calls, [1, 2])
        self.assertEqual(published, [])
        self.assertEqual(len(outgoing), 2)
        self.assertTrue(session.status().end_game_received)

    def test_presentation_does_not_change_policy_responses_or_status(self) -> None:
        observation = _reset_observation()
        request_ids = (1, 4, 9)

        plain_session = RankedSession(MinimalPolicy())
        plain_outgoing = self._drive(plain_session, observation, request_ids)
        plain_status = plain_session.status()

        buffer = BoundedRankedPresentationBuffer(capacity=8)
        observed_session = RankedSession(MinimalPolicy(), presentation=buffer)
        observed_outgoing = self._drive(observed_session, observation, request_ids)
        observed_status = observed_session.status()

        self.assertEqual(observed_outgoing, plain_outgoing)
        self.assertEqual(
            observed_status.requests_received, plain_status.requests_received
        )
        self.assertEqual(observed_status.responses_sent, plain_status.responses_sent)
        self.assertEqual(observed_status.ack_history, plain_status.ack_history)
        self.assertEqual(observed_status.scores, plain_status.scores)
        self.assertEqual(observed_status.seat, plain_status.seat)

    def test_decision_facts_bind_to_the_exact_request_id_and_bound_seat(self) -> None:
        observation = _reset_observation()
        buffer = BoundedRankedPresentationBuffer(capacity=8)
        session = RankedSession(MinimalPolicy(), presentation=buffer)
        self._drive(session, observation, (3, 6, 11))

        batch = buffer.drain()
        self.assertEqual(
            [decision.request_id for decision in batch.decisions], [3, 6, 11]
        )
        for decision in batch.decisions:
            self.assertEqual(decision.self_seat, Seat.SEAT_0)
            self.assertEqual(decision.policy_input.self_seat, Seat.SEAT_0)

    def test_presented_policy_input_is_the_same_decision_given_to_the_policy(
        self,
    ) -> None:
        observation = _reset_observation()
        policy = _RecordingPolicy()
        buffer = BoundedRankedPresentationBuffer(capacity=8)
        session = RankedSession(policy, presentation=buffer)
        self._drive(session, observation, (1, 2))

        batch = buffer.drain()
        self.assertEqual(len(batch.decisions), 2)
        self.assertEqual(len(policy.decisions), 2)
        for decision, given, chosen in zip(
            batch.decisions, policy.decisions, policy.selected, strict=True
        ):
            # presentationはprojectionを再計算せず、Policyへ渡した
            # `DecisionContext.input`そのものを公開する。
            self.assertIs(decision.policy_input, given.input)
            self.assertEqual(decision.selected_action, chosen)
            # `execute_policy()`が返すcanonical候補objectそのものであること。
            self.assertTrue(
                any(
                    decision.selected_action is candidate
                    for candidate in given.legal_actions
                )
            )

    def test_session_owns_decision_facts_but_not_terminal_facts(self) -> None:
        """`end_game`受信だけではcompletionをpublishしないことを固定する。

        terminal factはrun全体の成否が確定して初めて一意に決まるため、
        `run_ranked_game()`が所有する(`RunRankedGameTerminalLifecycleTest`)。
        """
        observation = _reset_observation()
        buffer = BoundedRankedPresentationBuffer(capacity=8)
        session = RankedSession(MinimalPolicy(), presentation=buffer)
        self._drive(session, observation, (1,))

        batch = buffer.drain()
        self.assertEqual(len(batch.decisions), 1)
        self.assertIsNone(batch.completion)
        self.assertIsNone(batch.failure)
        self.assertTrue(session.status().end_game_received)
        self.assertEqual(session.status().scores, tuple(_FINAL_SCORES))

    def test_detached_consumer_does_not_break_the_session(self) -> None:
        observation = _reset_observation()
        buffer = BoundedRankedPresentationBuffer(capacity=8)
        session = RankedSession(MinimalPolicy(), presentation=buffer)

        session.handle_event({"type": "start_game", "id": 0})
        first = session.handle_event(_dahai_request_action(observation, 1))
        buffer.detach()
        second = session.handle_event(_dahai_request_action(observation, 2))
        session.handle_event({"type": "end_game", "scores": _FINAL_SCORES})

        self.assertEqual(first["request_id"], 1)
        self.assertEqual(second["request_id"], 2)
        self.assertTrue(session.status().end_game_received)
        self.assertTrue(buffer.drain().is_empty)

    def test_validation_session_has_no_presentation_seam(self) -> None:
        self.assertFalse(hasattr(ValidationSession(MinimalPolicy()), "_presentation"))
        with self.assertRaises(TypeError):
            RankedSession(MinimalPolicy(), presentation=object())


class RunRankedGamePresentationTest(unittest.TestCase):
    def _run(self, messages, *, presentation=None, trace_path=None):
        return _run_ranked(messages, presentation=presentation, trace_path=trace_path)

    def test_run_without_presentation_keeps_existing_behavior(self) -> None:
        observation = _reset_observation()
        messages = _ranked_messages(observation, (1, 2))

        plain_result, plain_transport = self._run(messages)
        buffer = BoundedRankedPresentationBuffer(capacity=8)
        observed_result, observed_transport = self._run(messages, presentation=buffer)

        self.assertEqual(observed_transport.sent, plain_transport.sent)
        self.assertEqual(observed_result, plain_result)
        self.assertEqual(plain_result.requests_received, 2)
        self.assertEqual(plain_result.responses_sent, 2)
        self.assertEqual(plain_result.scores, tuple(_FINAL_SCORES))

    def test_slow_consumer_never_blocks_the_ranked_execution_path(self) -> None:
        observation = _reset_observation()
        buffer = BoundedRankedPresentationBuffer(capacity=1)
        release = threading.Event()
        consumer_started = threading.Event()
        drained = []

        def _slow_consumer() -> None:
            consumer_started.set()
            # ranked runが完了するまでdrainしないconsumerを再現する。publish側は
            # このconsumerを一切待たない。
            release.wait(timeout=30)
            drained.append(buffer.drain())

        consumer = threading.Thread(target=_slow_consumer)
        consumer.start()
        self.assertTrue(consumer_started.wait(timeout=30))

        result, transport = self._run(
            _ranked_messages(observation, tuple(range(1, 11))), presentation=buffer
        )
        release.set()
        consumer.join(timeout=30)

        self.assertFalse(consumer.is_alive())
        self.assertEqual(result.requests_received, 10)
        self.assertEqual(result.responses_sent, 10)
        self.assertEqual(len(transport.sent), 10)
        # 完走までdrainされなくてもbufferは有限のまま。
        self.assertEqual(len(drained), 1)
        self.assertEqual(len(drained[0].decisions), 1)
        self.assertEqual(drained[0].coalesced_decisions, 9)

    def test_terminal_completion_is_not_dropped_by_decision_overflow(self) -> None:
        observation = _reset_observation()
        buffer = BoundedRankedPresentationBuffer(capacity=1)
        result, _transport = _run_ranked(
            _ranked_messages(observation, tuple(range(1, 8))), presentation=buffer
        )

        batch = buffer.drain()
        self.assertTrue(result.end_game_received)
        self.assertEqual(len(batch.decisions), 1)
        self.assertIsNotNone(batch.completion)
        self.assertEqual(batch.completion.scores, tuple(_FINAL_SCORES))

    def test_detached_consumer_does_not_abort_the_ranked_run(self) -> None:
        observation = _reset_observation()
        messages = _ranked_messages(observation, (1, 2, 3))
        plain_result, plain_transport = self._run(messages)

        buffer = BoundedRankedPresentationBuffer(capacity=4)
        buffer.detach()
        detached_result, detached_transport = self._run(messages, presentation=buffer)

        self.assertEqual(detached_result, plain_result)
        self.assertEqual(detached_transport.sent, plain_transport.sent)
        self.assertTrue(buffer.drain().is_empty)

    def test_incomplete_run_publishes_a_failure_type_without_a_message(self) -> None:
        observation = _reset_observation()
        buffer = BoundedRankedPresentationBuffer(capacity=4)
        messages = _ranked_messages(observation, (1,), end_game=False)

        with self.assertRaises(UnexpectedDisconnectError):
            self._run(messages, presentation=buffer)

        batch = buffer.drain()
        self.assertIsNone(batch.completion)
        self.assertIsNotNone(batch.failure)
        self.assertEqual(batch.failure.failure_type, "UnexpectedDisconnectError")
        self.assertNotIn(_TOKEN, repr(batch.failure))
        self.assertEqual(len(batch.decisions), 1)

    def test_presentation_facts_never_carry_the_token(self) -> None:
        observation = _reset_observation()
        buffer = BoundedRankedPresentationBuffer(capacity=8)
        self._run(_ranked_messages(observation, (1, 2)), presentation=buffer)

        batch = buffer.drain()
        rendered = repr(batch)
        self.assertNotIn(_TOKEN, rendered)
        self.assertNotIn("Bearer", rendered)
        self.assertNotIn("Authorization", rendered)
        self.assertNotIn(observation.serialize_to_base64(), rendered)


class RunRankedGameTerminalLifecycleTest(unittest.TestCase):
    """1 runにつきterminal factがcompletion / failureのどちらか一方だけになる。

    `end_game`受信後にもtransport cleanup、trace writer close、
    `session.status()` / bound seat validationが残るため、`end_game`時点で
    completionをpublishすると、その後の失敗でcompletionとfailureの両方が
    deliveryされ得る。`run_ranked_game()`がそれらをすべて通過した場合だけ
    completionをpublishすることを固定する。
    """

    def test_normal_completion_publishes_exactly_one_completion(self) -> None:
        observation = _reset_observation()
        buffer = BoundedRankedPresentationBuffer(capacity=8)
        result, _transport = _run_ranked(
            _ranked_messages(observation, (1, 2)), presentation=buffer
        )

        batch = buffer.drain()
        self.assertIsNotNone(batch.completion)
        self.assertEqual(batch.completion.self_seat, Seat.SEAT_0)
        self.assertEqual(batch.completion.scores, tuple(_FINAL_SCORES))
        self.assertIsNone(batch.failure)
        self.assertEqual(result.scores, tuple(_FINAL_SCORES))
        self.assertTrue(result.end_game_received)
        # 2回目のdrainでterminal factが再配信されないこと。
        self.assertTrue(buffer.drain().is_empty)

    def test_trace_close_failure_after_end_game_publishes_only_a_failure(self) -> None:
        observation = _reset_observation()
        buffer = BoundedRankedPresentationBuffer(capacity=8)
        with patch(
            "lisjong_arena.riichilab.ranked.JsonlProtocolTraceWriter",
            _FailingCloseTraceWriter,
        ):
            with self.assertRaises(ProtocolTraceError):
                _run_ranked(
                    _ranked_messages(observation, (1,)),
                    presentation=buffer,
                    trace_path="unused-by-the-failing-writer",
                )

        batch = buffer.drain()
        # `end_game`自体は受信済みだが、runは完走していない。
        self.assertIsNone(batch.completion)
        self.assertIsNotNone(batch.failure)
        self.assertEqual(batch.failure.failure_type, "ProtocolTraceError")
        self.assertTrue(buffer.drain().is_empty)

    def test_trace_open_failure_publishes_only_a_failure(self) -> None:
        observation = _reset_observation()
        buffer = BoundedRankedPresentationBuffer(capacity=8)
        with patch(
            "lisjong_arena.riichilab.ranked.JsonlProtocolTraceWriter",
            _FailingOpenTraceWriter,
        ):
            with self.assertRaises(ProtocolTraceError):
                _run_ranked(
                    _ranked_messages(observation, (1,)),
                    presentation=buffer,
                    trace_path="unused-by-the-failing-writer",
                )

        batch = buffer.drain()
        self.assertEqual(batch.decisions, ())
        self.assertIsNone(batch.completion)
        self.assertIsNotNone(batch.failure)
        self.assertEqual(batch.failure.failure_type, "ProtocolTraceError")
        self.assertTrue(buffer.drain().is_empty)

    def test_trace_failure_keeps_the_no_presentation_behavior_unchanged(self) -> None:
        observation = _reset_observation()
        with patch(
            "lisjong_arena.riichilab.ranked.JsonlProtocolTraceWriter",
            _FailingCloseTraceWriter,
        ):
            with self.assertRaises(ProtocolTraceError):
                _run_ranked(
                    _ranked_messages(observation, (1,)),
                    trace_path="unused-by-the-failing-writer",
                )


class DurableRecordPresentationTest(unittest.TestCase):
    def _acquire(self, messages, destination: Path, *, presentation=None):
        transport = _FakeTransport(messages)
        with patch(
            "lisjong_arena.riichilab.ranked.connect_ranked_transport",
            _fake_connect(transport),
        ):
            return asyncio.run(
                acquire_ranked_game_record(
                    MinimalPolicy(),
                    _TOKEN,
                    destination=destination,
                    profile_identity="lisjong-dev",
                    presentation=presentation,
                )
            )

    @staticmethod
    def _semantics(bundle: Path) -> dict:
        loaded = load_ranked_game_record(bundle)
        summary = summarize_ranked_game_record(loaded)
        # timestampだけはacquisition時刻でありrun間で一致しないため、
        # semantic payloadの比較対象から外す。
        trace = [
            {
                key: value
                for key, value in json.loads(line).items()
                if key != "timestamp"
            }
            for line in (bundle / PROTOCOL_TRACE_FILENAME)
            .read_text(encoding="utf-8")
            .splitlines()
        ]
        return {
            "result": loaded.result,
            "requests": summary.requests,
            "responses": summary.responses,
            "deserialized_observations": summary.deserialized_observations,
            "provenance": loaded.provenance,
            "trace": trace,
        }

    def test_presentation_does_not_change_record_readback_semantics(self) -> None:
        observation = _reset_observation()
        messages = _ranked_messages(observation, (1, 2))

        with TemporaryDirectory() as raw:
            directory = Path(raw)
            self._acquire(messages, directory / "plain")
            buffer = BoundedRankedPresentationBuffer(capacity=8)
            self._acquire(messages, directory / "observed", presentation=buffer)

            plain = self._semantics(directory / "plain")
            observed = self._semantics(directory / "observed")

        self.assertEqual(observed, plain)

        batch = buffer.drain()
        self.assertEqual([decision.request_id for decision in batch.decisions], [1, 2])
        self.assertIsNotNone(batch.completion)


if __name__ == "__main__":
    unittest.main()
