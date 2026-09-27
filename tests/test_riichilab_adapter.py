"""Arena-local `RiichiLabSeatAdapter` composition regression(Arena-owned bridge、Issue #27)。

protocol-facing bridge instanceのcomposition behavior(constructor validation、
request_id echo、seat照合、Policy入力からのtransport metadata隔離、tracker /
mapping sessionの継続保持、失敗時のfail closed propagation)を固定する。

Adapter / Policy semanticsのcorrectnessそのものはlisjongが所有するため、この
testはRiichiEnv `legal_actions()`からserver-style `possible_actions`を生成
する汎用helper、candidate独自dedupe/補完helper、full gameを回してkakan
candidateを探索するhelper、`_resolve_for_env()`相当をserver semantics
oracleとして利用しない(Arena Issue #27)。ここでは打牌局面という単一
protocol shapeだけを対象にした最小限のfixture helperだけを使う。

cross-boundary integrationの本線は
`test_riichilab_session_adapter_integration.py`が担当する。kakan等の
protocol semanticsはsmall explicit fixtureで固定する
`test_riichilab_possible_action_validation.py`が担当する。
"""

import unittest
from unittest.mock import patch

from lisjong.policies import MinimalPolicy
from lisjong.policy_contract import PassAction, PolicyActionValidationError, Seat
from riichienv import ActionType, RiichiEnv

from lisjong_arena.riichienv.adapter import (
    AdapterSyncError,
    tile_from_physical_id,
    tile_to_mjai,
)
from lisjong_arena.riichilab.adapter import RiichiLabSeatAdapter, SendReadyResponse
from lisjong_arena.riichilab.adapter_errors import (
    PossibleActionsValidationError,
    ProtocolConversionError,
    SeatMismatchError,
)


def _reset_observations(seed=1, game_mode="4p-red-east"):
    env = RiichiEnv(seed=seed, game_mode=game_mode)
    return env, env.reset()


def _dahai_request_action(observation, request_id):
    """打牌局面専用の最小fixture helper。

    server candidate生成の汎用oracleではなく、初期打牌のように legal
    actionsがすべてdahaiである局面専用に、observationの手牌から直接
    `possible_actions`を組み立てるだけの最小限のhelperである。dedupeや
    複数Action type補完は行わない。
    """
    possible_actions = [
        {"type": "dahai", "pai": tile_to_mjai(tile_from_physical_id(tile))}
        for tile in sorted(set(observation.hand))
    ]
    return {
        "type": "request_action",
        "request_id": request_id,
        "possible_actions": possible_actions,
        "observation": observation.serialize_to_base64(),
    }


class RiichiLabSeatAdapterConstructionTest(unittest.TestCase):
    def test_rejects_non_seat_self_seat(self) -> None:
        with self.assertRaises(TypeError):
            RiichiLabSeatAdapter(0, MinimalPolicy())


class RiichiLabSeatAdapterRoundTripTest(unittest.TestCase):
    def test_processes_a_single_request_action_end_to_end(self) -> None:
        _env, observations = _reset_observations()
        player_id, observation = next(iter(observations.items()))
        seat = Seat(player_id)
        adapter = RiichiLabSeatAdapter(seat, MinimalPolicy())

        request = _dahai_request_action(observation, request_id=1)
        response = adapter.process_request_action(request)

        self.assertIsInstance(response, SendReadyResponse)
        self.assertEqual(response.request_id, 1)
        self.assertIn("type", response.action)
        self.assertEqual(response.action["actor"], player_id)

    def test_current_request_id_is_echoed_without_being_generated(self) -> None:
        _env, observations = _reset_observations(seed=2)
        player_id, observation = next(iter(observations.items()))
        seat = Seat(player_id)
        adapter = RiichiLabSeatAdapter(seat, MinimalPolicy())

        request = _dahai_request_action(observation, request_id=9)
        response = adapter.process_request_action(request)

        self.assertEqual(response.request_id, 9)

    def test_request_id_and_transport_metadata_do_not_reach_the_policy(self) -> None:
        seen_decisions = []

        class _RecordingPolicy:
            def choose_action(self, decision):
                seen_decisions.append(decision)
                return MinimalPolicy().choose_action(decision)

        _env, observations = _reset_observations(seed=3)
        player_id, observation = next(iter(observations.items()))
        seat = Seat(player_id)
        adapter = RiichiLabSeatAdapter(seat, _RecordingPolicy())

        request = _dahai_request_action(observation, request_id=77)
        request["time"] = {"grace_ms": 500}
        adapter.process_request_action(request)

        self.assertEqual(len(seen_decisions), 1)
        decision = seen_decisions[0]
        # DecisionContextはinput(PolicyInput)とlegal_actionsだけを持つ。
        self.assertTrue(hasattr(decision, "input"))
        self.assertTrue(hasattr(decision, "legal_actions"))
        self.assertFalse(hasattr(decision, "request_id"))
        self.assertFalse(hasattr(decision, "time"))
        self.assertFalse(hasattr(decision, "possible_actions"))


class RiichiLabSeatAdapterStatefulRuntimeTest(unittest.TestCase):
    def test_reuses_the_same_tracker_and_mapping_session_across_requests(self) -> None:
        _env, observations = _reset_observations(seed=5)
        player_id, observation = next(iter(observations.items()))
        seat = Seat(player_id)
        adapter = RiichiLabSeatAdapter(seat, MinimalPolicy())
        tracker = adapter._tracker
        mapping_session = adapter._mapping_session

        # constructorが1回だけ生成した`_tracker` / `_mapping_session`が、
        # 複数回の`process_request_action()`呼び出しをまたいで同一instanceの
        # まま継続利用されること(requestごとに作り直さないこと)を確認する。
        # gameを実際に進行させたりkakan候補を探索したりはしない。
        adapter.process_request_action(_dahai_request_action(observation, request_id=1))
        adapter.process_request_action(_dahai_request_action(observation, request_id=2))

        self.assertIs(adapter._tracker, tracker)
        self.assertIs(adapter._mapping_session, mapping_session)

    def test_cross_seat_observation_is_rejected(self) -> None:
        _env, observations = _reset_observations(seed=4)
        player_id, observation = next(iter(observations.items()))
        other_seat = Seat((player_id + 1) % 4)
        adapter = RiichiLabSeatAdapter(other_seat, MinimalPolicy())

        request = _dahai_request_action(observation, request_id=1)

        with self.assertRaises(SeatMismatchError):
            adapter.process_request_action(request)


class RiichiLabSeatAdapterFailClosedTest(unittest.TestCase):
    def test_observation_seat_mismatch_produces_no_payload(self) -> None:
        _env, observations = _reset_observations(seed=5)
        player_id, observation = next(iter(observations.items()))
        adapter = RiichiLabSeatAdapter(Seat((player_id + 1) % 4), MinimalPolicy())
        request = _dahai_request_action(observation, request_id=1)

        with self.assertRaises(SeatMismatchError):
            adapter.process_request_action(request)

    def test_build_decision_failure_produces_no_payload(self) -> None:
        _env, observations = _reset_observations(seed=6)
        player_id, observation = next(iter(observations.items()))
        seat = Seat(player_id)
        adapter = RiichiLabSeatAdapter(seat, MinimalPolicy())
        request = _dahai_request_action(observation, request_id=1)

        with patch(
            "lisjong_arena.riichilab.adapter.build_decision",
            side_effect=AdapterSyncError("boom"),
        ):
            with self.assertRaises(AdapterSyncError):
                adapter.process_request_action(request)

    def test_policy_exception_propagates_without_fallback(self) -> None:
        class _RaisingPolicy:
            def choose_action(self, decision):
                raise RuntimeError("policy exploded")

        _env, observations = _reset_observations(seed=8)
        player_id, observation = next(iter(observations.items()))
        seat = Seat(player_id)
        adapter = RiichiLabSeatAdapter(seat, _RaisingPolicy())
        request = _dahai_request_action(observation, request_id=1)

        with self.assertRaises(RuntimeError):
            adapter.process_request_action(request)

    def test_execute_policy_validation_failure_produces_no_payload(self) -> None:
        class _IllegalPolicy:
            def choose_action(self, decision):
                return PassAction(actor=decision.input.self_seat)

        _env, observations = _reset_observations(seed=9)
        player_id, observation = next(iter(observations.items()))
        seat = Seat(player_id)
        adapter = RiichiLabSeatAdapter(seat, _IllegalPolicy())
        request = _dahai_request_action(observation, request_id=1)

        # 初期打牌局面ではPassActionは合法候補に含まれないため、
        # execute_policy()がPolicyActionValidationErrorで拒否するはずである。
        with self.assertRaises(PolicyActionValidationError):
            adapter.process_request_action(request)

    def test_mjai_conversion_failure_produces_no_payload(self) -> None:
        _env, observations = _reset_observations(seed=10)
        player_id, observation = next(iter(observations.items()))
        seat = Seat(player_id)
        adapter = RiichiLabSeatAdapter(seat, MinimalPolicy())
        request = _dahai_request_action(observation, request_id=1)

        with patch(
            "lisjong_arena.riichilab.adapter.build_mjai_response",
            side_effect=ProtocolConversionError("boom"),
        ):
            with self.assertRaises(ProtocolConversionError):
                adapter.process_request_action(request)

    def test_possible_actions_mismatch_produces_no_payload(self) -> None:
        _env, observations = _reset_observations(seed=11)
        player_id, observation = next(iter(observations.items()))
        seat = Seat(player_id)
        adapter = RiichiLabSeatAdapter(seat, MinimalPolicy())
        request = _dahai_request_action(observation, request_id=1)
        # possible_actionsを、選択され得ない候補だけへ差し替える。
        request["possible_actions"] = [{"type": "ryukyoku"}]

        with self.assertRaises(PossibleActionsValidationError):
            adapter.process_request_action(request)


def _consecutive_seat_observations(seat: int, count: int, seed: int = 1) -> list:
    """`seat`が続けて受け取るObservationを、間を飛ばさず`count`件集める。"""
    env, observations = _reset_observations(seed=seed)
    collected = []
    while len(collected) < count:
        chosen = {}
        for player_id, observation in observations.items():
            legal = observation.legal_actions()
            if not legal:
                continue
            if player_id == seat:
                collected.append(observation)
            chosen[player_id] = next(
                (
                    action
                    for action in legal
                    if action.action_type == ActionType.DISCARD
                ),
                legal[0],
            )
        observations = env.step(chosen)
    return collected[:count]


class _InputRecordingPolicy:
    def __init__(self) -> None:
        self.inputs = []

    def choose_action(self, decision):
        self.inputs.append(decision.input)
        return MinimalPolicy().choose_action(decision)


class RiichiLabSeatAdapterSynchronizeTest(unittest.TestCase):
    """Policyを呼ばないrequestもmaterialized stateへ順番どおり同期する(Issue #418)。"""

    def test_skipped_request_leaves_the_same_state_as_a_decided_one(self) -> None:
        first, skipped, third = _consecutive_seat_observations(seat=0, count=3)
        requests = [
            _dahai_request_action(observation, request_id=request_id)
            for request_id, observation in ((1, first), (2, skipped), (3, third))
        ]

        decided_all = _InputRecordingPolicy()
        adapter = RiichiLabSeatAdapter(Seat.SEAT_0, decided_all)
        for request in requests:
            adapter.process_request_action(request)

        with_skip = _InputRecordingPolicy()
        adapter = RiichiLabSeatAdapter(Seat.SEAT_0, with_skip)
        adapter.process_request_action(requests[0])
        adapter.synchronize_request_action(requests[1])
        response = adapter.process_request_action(requests[2])

        self.assertEqual(3, response.request_id)
        self.assertEqual(2, len(with_skip.inputs))
        self.assertEqual(decided_all.inputs[2], with_skip.inputs[1])

    def test_dropping_a_skipped_request_breaks_the_state(self) -> None:
        """Sanity check: the skipped Observation cannot simply be dropped."""
        first, _skipped, third = _consecutive_seat_observations(seat=0, count=3)
        adapter = RiichiLabSeatAdapter(Seat.SEAT_0, MinimalPolicy())
        adapter.process_request_action(_dahai_request_action(first, request_id=1))

        with self.assertRaises(AdapterSyncError):
            adapter.process_request_action(_dahai_request_action(third, request_id=3))

    def test_synchronize_does_not_call_the_policy_or_build_a_mapping(self) -> None:
        (observation,) = _consecutive_seat_observations(seat=0, count=1)
        policy = _InputRecordingPolicy()
        adapter = RiichiLabSeatAdapter(Seat.SEAT_0, policy)

        with patch("lisjong_arena.riichilab.adapter.build_decision") as build_decision:
            adapter.synchronize_request_action(
                _dahai_request_action(observation, request_id=1)
            )

        build_decision.assert_not_called()
        self.assertEqual([], policy.inputs)

    def test_synchronize_fails_closed_like_a_decision(self) -> None:
        (observation,) = _consecutive_seat_observations(seat=0, count=1)
        adapter = RiichiLabSeatAdapter(Seat.SEAT_1, MinimalPolicy())
        with self.assertRaises(SeatMismatchError):
            adapter.synchronize_request_action(
                _dahai_request_action(observation, request_id=1)
            )

        request = _dahai_request_action(observation, request_id=1)
        del request["observation"]
        with self.assertRaises(Exception):
            RiichiLabSeatAdapter(
                Seat.SEAT_0, MinimalPolicy()
            ).synchronize_request_action(request)


if __name__ == "__main__":
    unittest.main()
