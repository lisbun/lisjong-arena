"""実際のriichienvを使うRiichiEnv Adapterの結合test。

`tests/test_riichienv_adapter_materialized_state.py`と
`tests/test_riichienv_adapter_policy_input.py`はfake Observationで各規則を
個別に確認する。本fileは実際の`RiichiEnv`を使い、複数kyoku・複数game modeに
わたって`build_policy_input()`が例外なく`PolicyInput`を生成できることを
確認する結合testである。

CI実行時間を抑えるため、seed数とstep上限を絞る。より広い範囲の実測は
lisbun/lisjong#28の調査段階で個別に実施済みであり(lisbun/lisjong `docs/riichienv-investigation.md`)、
ここでは実装の回帰を検出できる最小限の再現に絞る。lisbun/lisjong#23の最終統合では、
1 decisionについてPolicyInput、Action mapping、DecisionContext、共通Policy実行境界、
MinimalPolicy、元のRiichiEnv Actionまでの完全往復も固定する。
"""

import unittest

from lisjong.policies import MinimalPolicy
from lisjong.policy_contract import execute_policy
from lisjong.policy_contract.action import PassAction
from lisjong.policy_contract.decision_context import DecisionContext
from lisjong.policy_contract.policy_input import PolicyInput
from lisjong.policy_contract.seat import Seat
from riichienv import RiichiEnv

from lisjong_arena.riichienv.adapter import (
    ActorMismatchError,
    AdapterSyncError,
    RiichiEnvActionMappingSession,
    RiichiEnvDecision,
    SeatMaterializedState,
    StaleActionMappingError,
    UnmappedActionError,
    build_decision,
    build_policy_input,
)

_PREFERRED_ACTION_TYPES = (
    "ActionType.RIICHI",
    "ActionType.DAIMINKAN",
    "ActionType.ANKAN",
    "ActionType.KAKAN",
    "ActionType.RON",
    "ActionType.TSUMO",
    "ActionType.CHI",
    "ActionType.PON",
)


def _choose_action(observation):
    """riichi/kan/call/和了を優先し、実測範囲を広く踏むaction選択方針。"""
    legal_actions = observation.legal_actions()
    preferred = [
        action
        for action in legal_actions
        if str(action.action_type) in _PREFERRED_ACTION_TYPES
    ]
    return preferred[0] if preferred else legal_actions[0]


def _module_is_leaked_from_riichienv(value: object, seen: set[int]) -> bool:
    """valueから再帰的に到達可能なobjectにriichienv由来のものがないか調べる。"""
    if id(value) in seen:
        return False
    seen.add(id(value))

    module_name = type(value).__module__
    if module_name.startswith("riichienv"):
        return True

    if isinstance(value, (str, bytes, int, float, bool)) or value is None:
        return False
    if isinstance(value, (tuple, list, set, frozenset)):
        return any(_module_is_leaked_from_riichienv(item, seen) for item in value)
    if isinstance(value, dict):
        return any(
            _module_is_leaked_from_riichienv(key, seen)
            or _module_is_leaked_from_riichienv(item, seen)
            for key, item in value.items()
        )
    if hasattr(value, "__dict__"):
        return any(
            _module_is_leaked_from_riichienv(item, seen)
            for item in vars(value).values()
        )
    if hasattr(value, "__slots__"):
        return any(
            _module_is_leaked_from_riichienv(getattr(value, slot), seen)
            for slot in value.__slots__
            if hasattr(value, slot)
        )
    return False


class _SeatVisibleObservation:
    """実Observationのallowlist側だけを公開する境界test用proxy。

    Adapterが完全hand集合、wall、完全logへ触れようとした場合は、属性が存在しない
    ためtestが失敗する。自席の``hand``と公開状態は元Observationへ委譲する。
    """

    _FORBIDDEN_NAMES = frozenset({"hands", "wall", "mjai_log"})

    def __init__(self, observation) -> None:
        self._observation = observation

    def __getattr__(self, name: str):
        if name in self._FORBIDDEN_NAMES:
            raise AssertionError(
                f"Adapter accessed forbidden observation field: {name}"
            )
        return getattr(self._observation, name)


def _initial_observation(seed: int = 1):
    env = RiichiEnv(seed=seed, game_mode="4p-red-single")
    player_id, observation = next(iter(env.reset().items()))
    return player_id, observation


class RiichiEnvAdapterIntegrationTest(unittest.TestCase):
    def test_builds_policy_input_across_multiple_kyoku_without_failure(self) -> None:
        for seed in range(1, 6):
            with self.subTest(seed=seed):
                env = RiichiEnv(seed=seed, game_mode="4p-red-east")
                observations = env.reset()
                trackers = {}
                steps = 0
                decisions = 0

                while not env.done() and steps < 500:
                    actions = {}
                    for player_id, observation in observations.items():
                        seat = Seat(player_id)
                        tracker = trackers.setdefault(seat, SeatMaterializedState(seat))

                        policy_input = build_policy_input(tracker, observation)
                        decisions += 1

                        self.assertIsInstance(policy_input, PolicyInput)
                        self.assertEqual(policy_input.self_seat, seat)
                        self.assertFalse(
                            _module_is_leaked_from_riichienv(policy_input, set())
                        )

                        actions[player_id] = _choose_action(observation)
                    observations = env.step(actions)
                    steps += 1

                self.assertGreater(decisions, 0)

    def test_own_hand_does_not_reveal_other_seats_hidden_tiles(self) -> None:
        env = RiichiEnv(seed=1, game_mode="4p-red-single")
        observations = env.reset()
        tracker = SeatMaterializedState(Seat.SEAT_0)
        observation = observations[0]

        policy_input = build_policy_input(tracker, observation)

        # 自席が握れるのは配牌13枚+ツモ1枚の範囲であり、他家の非公開牌14枚*3が
        # own_handへ混入していないことを枚数からも確認する。
        self.assertLessEqual(len(policy_input.own_hand.concealed_tiles), 14)


class RiichiEnvDecisionIntegrationTest(unittest.TestCase):
    def test_minimal_policy_round_trip_for_one_real_decision(self) -> None:
        player_id, raw_observation = _initial_observation()
        seat = Seat(player_id)
        observation = _SeatVisibleObservation(raw_observation)
        tracker = SeatMaterializedState(seat)
        mapping_session = RiichiEnvActionMappingSession(seat)

        decision = build_decision(tracker, observation, mapping_session)

        self.assertIsInstance(decision, RiichiEnvDecision)
        self.assertIsInstance(decision.context, DecisionContext)
        self.assertIsInstance(decision.context.input, PolicyInput)
        self.assertEqual(decision.context.input.self_seat, seat)
        self.assertEqual(decision.context.legal_actions, decision.mapping.candidates)
        self.assertGreater(len(decision.context.legal_actions), 0)
        self.assertEqual(
            len(decision.context.legal_actions),
            len(set(decision.context.legal_actions)),
        )

        # Policyへ渡すのはRiichiEnvDecision全体ではなく、raw外部objectへ到達
        # できないDecisionContextだけである。
        self.assertFalse(_module_is_leaked_from_riichienv(decision.context, set()))
        selected = execute_policy(MinimalPolicy(), decision.context)
        self.assertTrue(
            any(selected is action for action in decision.context.legal_actions)
        )

        resolved = decision.mapping.resolve(selected)

        # resolve()が返したobjectは、mapping生成時に1回だけ取得して保持した
        # legal setの実在objectである。legal_actions()の再呼び出し結果とは
        # object identityで比較しない。
        self.assertTrue(
            any(
                resolved is legal_action
                for legal_action in decision.mapping._external_legal_actions
            )
        )

    def test_rejects_state_observation_and_session_seat_mismatches(self) -> None:
        player_id, observation = _initial_observation()
        seat = Seat(player_id)
        other_seat = Seat((player_id + 1) % 4)

        with self.subTest(boundary="state and Observation"):
            with self.assertRaises(AdapterSyncError):
                build_decision(
                    SeatMaterializedState(other_seat),
                    observation,
                    RiichiEnvActionMappingSession(other_seat),
                )

        with self.subTest(boundary="state and mapping session"):
            with self.assertRaises(AdapterSyncError):
                build_decision(
                    SeatMaterializedState(seat),
                    observation,
                    RiichiEnvActionMappingSession(other_seat),
                )

    def test_rejects_context_from_different_mapping_candidates(self) -> None:
        player_id, observation = _initial_observation()
        seat = Seat(player_id)
        decision = build_decision(
            SeatMaterializedState(seat),
            observation,
            RiichiEnvActionMappingSession(seat),
        )
        inconsistent_context = DecisionContext(
            input=decision.context.input,
            legal_actions=(PassAction(actor=seat),),
        )
        self.assertNotEqual(
            inconsistent_context.legal_actions, decision.mapping.candidates
        )

        with self.assertRaises(AdapterSyncError):
            RiichiEnvDecision(
                context=inconsistent_context,
                mapping=decision.mapping,
            )

    def test_new_integrated_decision_invalidates_unresolved_old_mapping(self) -> None:
        player_id, first_observation = _initial_observation(seed=2)
        second_player_id, second_observation = _initial_observation(seed=2)
        self.assertEqual(second_player_id, player_id)
        seat = Seat(player_id)
        tracker = SeatMaterializedState(seat)
        mapping_session = RiichiEnvActionMappingSession(seat)

        first = build_decision(tracker, first_observation, mapping_session)
        second = build_decision(tracker, second_observation, mapping_session)

        with self.assertRaises(StaleActionMappingError):
            first.mapping.resolve(first.context.legal_actions[0])

        selected = execute_policy(MinimalPolicy(), second.context)
        resolved = second.mapping.resolve(selected)
        self.assertTrue(
            any(
                resolved is legal_action
                for legal_action in second.mapping._external_legal_actions
            )
        )

    def test_failed_new_decision_construction_invalidates_old_mapping(self) -> None:
        player_id, first_observation = _initial_observation(seed=4)
        second_player_id, raw_second_observation = _initial_observation(seed=4)
        self.assertEqual(second_player_id, player_id)
        seat = Seat(player_id)
        tracker = SeatMaterializedState(seat)
        mapping_session = RiichiEnvActionMappingSession(seat)
        first = build_decision(tracker, first_observation, mapping_session)

        # Action mappingは構築できる一方、PolicyInputとのkyoku identity照合は
        # 通らないObservationを作る。新decision構築が後段で失敗しても、
        # mapping generationはすでに進んでおり旧mappingを再利用できない。
        inconsistent_observation = _SeatVisibleObservation(raw_second_observation)
        inconsistent_observation.round_wind = 99
        with self.assertRaises(AdapterSyncError):
            build_decision(tracker, inconsistent_observation, mapping_session)

        with self.assertRaises(StaleActionMappingError):
            first.mapping.resolve(first.context.legal_actions[0])

    def test_mapping_rejects_cross_seat_and_unmapped_policy_results(self) -> None:
        player_id, observation = _initial_observation(seed=3)
        seat = Seat(player_id)
        decision = build_decision(
            SeatMaterializedState(seat),
            observation,
            RiichiEnvActionMappingSession(seat),
        )

        with self.assertRaises(ActorMismatchError):
            decision.mapping.resolve(PassAction(actor=Seat((player_id + 1) % 4)))

        unmapped = PassAction(actor=seat)
        self.assertNotIn(unmapped, decision.context.legal_actions)
        with self.assertRaises(UnmappedActionError):
            decision.mapping.resolve(unmapped)


if __name__ == "__main__":
    unittest.main()
