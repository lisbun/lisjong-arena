"""#562 engine bridge上のArm Y / L2aのaction mapping fixture。

engineの ``SeatObservation`` と ``ActionDescriptor`` から、正式実行と同じbridge
（``build_decision``）で ``DecisionContext`` を作り、両Policyの入力・選択経路を通す。
実対局・実artifactは使わない。

```text
Arm Y   feature（8204）・action vocabulary・legal mask・resolverが、和了・リーチ・
        打牌・Pass・Chi / Pon・各種槓・九種九牌のどの合法行動もindexとして表し、
        そのindexが同じdecisionのcanonical objectへ戻り、engine descriptorへmapできる
        （torchがあるときは、実際のserving Policyで各合法行動を選ばせてmapする）
L2a     分類・guard・scorer候補（槓・九種九牌を除く）が同じbridgeの値で決まり、
        選んだ行動がcanonical合法objectとしてengine descriptorへmapできる
```
"""

import importlib.util
import math
import unittest
from types import SimpleNamespace

from _lisjong_engine_fixtures import (
    discard,
    honor,
    manzu,
    observation,
    pinzu,
    pon_meld,
    seat_discards,
    seat_melds,
    souzu,
)
from lisjong.action_vocabulary import (
    build_legal_action_mask,
    encode_legal_actions,
    resolve_legal_action,
)
from lisjong.learning.l2a_decision import (
    L2aDecisionKind,
    classify_l2a_decision,
    guard_action,
    scorer_candidates,
)
from lisjong.policy_contract import (
    AnkanAction,
    ChiAction,
    DaiminkanAction,
    DiscardAction,
    KakanAction,
    KyuushuKyuuhaiAction,
    PassAction,
    PonAction,
    RiichiAction,
    RonAction,
    TsumoAction,
)
from lisjong_engine.action_descriptor import (
    AnkanActionDescriptor,
    ChiActionDescriptor,
    DaiminkanActionDescriptor,
    DiscardActionDescriptor,
    KakanActionDescriptor,
    NineTerminalsActionDescriptor,
    PassActionDescriptor,
    PonActionDescriptor,
    RiichiActionDescriptor,
    RonActionDescriptor,
    TsumoActionDescriptor,
)
from lisjong_engine.observation import ObservationDecisionKind
from lisjong_engine.seat import Seat as EngineSeat

from lisjong_arena.learned_policy_input import build_policy_input_feature, tensor_values
from lisjong_arena.lisjong_engine.decision import build_decision
from lisjong_arena.riichilab_source_pilot.protocol import (
    FEATURE_DIMENSION,
    VOCABULARY_SIZE,
)

TORCH = importlib.util.find_spec("torch") is not None


def _turn_win():
    """自摸和了・リーチ・暗槓・打牌が同時に合法な手番。"""
    hand = (
        manzu(1), manzu(1), manzu(1), pinzu(2), pinzu(3), pinzu(4),
        souzu(5), souzu(6), souzu(7), honor(1), honor(1), manzu(9), souzu(1),
    )  # fmt: skip
    drawn = manzu(1)
    options = (
        TsumoActionDescriptor(drawn),
        RiichiActionDescriptor(),
        AnkanActionDescriptor((manzu(1),) * 4),
        DiscardActionDescriptor(manzu(9), False),
        DiscardActionDescriptor(souzu(1), False),
        DiscardActionDescriptor(drawn, True),
    )
    return observation(hand_tiles=(*hand, drawn), drawn_tile=drawn), options


def _turn_riichi_kakan():
    """リーチ・加槓・打牌（和了なし）。"""
    hand = (
        manzu(2), manzu(3), manzu(4), pinzu(6), pinzu(7), pinzu(8),
        souzu(2), souzu(2), honor(5), honor(6),
    )  # fmt: skip
    drawn = manzu(5)
    melds = seat_melds(east=(pon_meld(manzu(5), EngineSeat.WEST),))
    options = (
        RiichiActionDescriptor(),
        KakanActionDescriptor(drawn),
        DiscardActionDescriptor(honor(5), False),
        DiscardActionDescriptor(honor(6), False),
        DiscardActionDescriptor(drawn, True),
    )
    return observation(
        hand_tiles=(*hand, drawn), drawn_tile=drawn, melds=melds
    ), options


def _turn_nine_terminals():
    """九種九牌と打牌。"""
    hand = (
        manzu(1), manzu(9), pinzu(1), pinzu(9), souzu(1), souzu(9), honor(1),
        honor(2), honor(3), manzu(5), pinzu(5), souzu(5), souzu(6),
    )  # fmt: skip
    drawn = honor(4)
    options = (
        NineTerminalsActionDescriptor(),
        DiscardActionDescriptor(manzu(5), False),
        DiscardActionDescriptor(drawn, True),
    )
    return observation(hand_tiles=(*hand, drawn), drawn_tile=drawn), options


def _reaction_calls():
    """東の打牌2pへのロン・チー・ポン・大明槓・Pass（南の判断）。"""
    hand = (
        pinzu(2), pinzu(2), pinzu(2), pinzu(3), pinzu(4), manzu(1), manzu(2),
        manzu(3), souzu(7), souzu(8), souzu(9), honor(7), honor(7),
    )  # fmt: skip
    tile = pinzu(2)
    source = observation(
        viewer_seat=EngineSeat.SOUTH,
        decision_kind=ObservationDecisionKind.DISCARD_REACTION,
        hand_tiles=hand,
        drawn_tile=None,
        discards=seat_discards(east=(discard(tile, 0),)),
    )
    options = (
        RonActionDescriptor(tile, EngineSeat.EAST),
        ChiActionDescriptor(tile, (pinzu(3), pinzu(4)), EngineSeat.EAST),
        PonActionDescriptor(tile, (tile, tile), EngineSeat.EAST),
        DaiminkanActionDescriptor(tile, (tile, tile, tile), EngineSeat.EAST),
        PassActionDescriptor(tile, EngineSeat.EAST),
    )
    return source, options


def _reaction_pon_pass():
    """ポンとPassだけ（L2aのCALL）。"""
    hand = (
        souzu(4), souzu(4), manzu(1), manzu(2), manzu(3), pinzu(5), pinzu(6),
        pinzu(7), honor(1), honor(2), honor(3), manzu(9), pinzu(9),
    )  # fmt: skip
    tile = souzu(4)
    source = observation(
        viewer_seat=EngineSeat.NORTH,
        decision_kind=ObservationDecisionKind.DISCARD_REACTION,
        hand_tiles=hand,
        drawn_tile=None,
        discards=seat_discards(south=(discard(tile, 0),)),
    )
    options = (
        PonActionDescriptor(tile, (tile, tile), EngineSeat.SOUTH),
        PassActionDescriptor(tile, EngineSeat.SOUTH),
    )
    return source, options


def _reaction_daiminkan_pass():
    """大明槓とPassだけ（L2aではPASS_RESPONSE）。"""
    hand = (
        honor(5), honor(5), honor(5), manzu(1), manzu(2), manzu(3), pinzu(5),
        pinzu(6), pinzu(7), souzu(1), souzu(2), souzu(3), manzu(9),
    )  # fmt: skip
    tile = honor(5)
    source = observation(
        viewer_seat=EngineSeat.WEST,
        decision_kind=ObservationDecisionKind.DISCARD_REACTION,
        hand_tiles=hand,
        drawn_tile=None,
        discards=seat_discards(east=(discard(tile, 0),)),
    )
    options = (
        DaiminkanActionDescriptor(tile, (tile, tile, tile), EngineSeat.EAST),
        PassActionDescriptor(tile, EngineSeat.EAST),
    )
    return source, options


DECISIONS = {
    "turn win": (_turn_win, L2aDecisionKind.WIN, TsumoAction),
    "turn riichi kakan": (_turn_riichi_kakan, L2aDecisionKind.RIICHI, RiichiAction),
    "turn nine terminals": (_turn_nine_terminals, L2aDecisionKind.DISCARD, None),
    "reaction calls": (_reaction_calls, L2aDecisionKind.WIN, RonAction),
    "reaction pon": (_reaction_pon_pass, L2aDecisionKind.CALL, None),
    "reaction daiminkan": (
        _reaction_daiminkan_pass,
        L2aDecisionKind.PASS_RESPONSE,
        PassAction,
    ),
}
ACTION_TYPES = {
    AnkanAction,
    ChiAction,
    DaiminkanAction,
    DiscardAction,
    KakanAction,
    KyuushuKyuuhaiAction,
    PassAction,
    PonAction,
    RiichiAction,
    RonAction,
    TsumoAction,
}


def _decision(name):
    source, options = DECISIONS[name][0]()
    return build_decision(source, options), options


def _maps_back(test, decision, options, action):
    """選んだ行動がcanonical合法objectで、提示されたengine descriptorへ戻ること。"""
    index = next(
        i for i, legal in enumerate(decision.context.legal_actions) if legal is action
    )
    test.assertEqual(decision.mapping.resolve(action), options[index])


class FixtureCoverageTest(unittest.TestCase):
    def test_fixtures_cover_every_action_family(self):
        seen = set()
        for name in DECISIONS:
            decision, _ = _decision(name)
            seen.update(type(action) for action in decision.context.legal_actions)
        self.assertEqual(seen, ACTION_TYPES)


class ArmYMappingTest(unittest.TestCase):
    def test_vocabulary_mask_and_resolver_cover_every_legal_action(self):
        for name in DECISIONS:
            with self.subTest(name):
                decision, options = _decision(name)
                context = decision.context
                encoded = encode_legal_actions(context)
                mask = build_legal_action_mask(context)
                self.assertEqual(len(mask), VOCABULARY_SIZE)
                self.assertEqual(
                    {index for index, legal in enumerate(mask) if legal}, set(encoded)
                )
                self.assertEqual(len(encoded), len(context.legal_actions))
                for index, action in encoded.items():
                    resolved = resolve_legal_action(index, context)
                    self.assertIs(resolved, action)
                    _maps_back(self, decision, options, resolved)

    def test_feature_is_built_from_the_engine_policy_input(self):
        for name in DECISIONS:
            with self.subTest(name):
                decision, _ = _decision(name)
                values = tensor_values(
                    build_policy_input_feature(decision.context.input)
                )
                self.assertEqual(len(values), FEATURE_DIMENSION)
                self.assertTrue(all(math.isfinite(value) for value in values))

    @unittest.skipUnless(TORCH, "requires the locked CPU torch")
    def test_serving_policy_selects_and_maps_every_legal_action(self):
        """実際のArm Y serving Policyに、各合法行動を選ばせてengineへmapする。"""
        import torch

        from lisjong_arena.riichilab_source_pilot.protocol import ARM_Y
        from lisjong_arena.riichilab_source_pilot.serving import create_serving_runtime

        model = torch.nn.Linear(FEATURE_DIMENSION, VOCABULARY_SIZE)
        model.weight.requires_grad_(False)
        model.bias.requires_grad_(False)
        model.weight.zero_()
        model.eval()
        runtime = create_serving_runtime(ARM_Y, SimpleNamespace(model=model))
        for name in DECISIONS:
            decision, options = _decision(name)
            for index, action in encode_legal_actions(decision.context).items():
                with self.subTest(name=name, action=type(action).__name__):
                    model.bias.fill_(0.0)
                    model.bias[index] = 10.0
                    selected = runtime.create_policy().choose_action(decision.context)
                    self.assertIs(selected, action)
                    _maps_back(self, decision, options, selected)


class _ZeroScorer:
    """M1 / R1の実encodingで候補を作り、scoreは全候補0（canonical順で最初を選ぶ）。"""

    def __init__(self, encoding):
        self.encoding = encoding
        self.calls = []

    def score(self, context, vectors):
        self.calls.append((len(context), len(vectors)))
        return tuple(0.0 for _ in vectors)


class L2aMappingTest(unittest.TestCase):
    def _policy(self):
        from lisjong.learning.l2a_encoding import ENCODING_M1, ENCODING_R1
        from lisjong.learning.l2a_policy import L2aPolicy

        discard_scorer = _ZeroScorer(ENCODING_M1)
        reaction_scorer = _ZeroScorer(ENCODING_R1)
        scorers = {
            L2aDecisionKind.DISCARD: discard_scorer,
            L2aDecisionKind.CALL: reaction_scorer,
        }
        runtime = SimpleNamespace(scorer_for=lambda kind: scorers[kind])
        return L2aPolicy(runtime), scorers

    def test_classification_guard_and_scorer_candidates(self):
        for name, (_, kind, guarded_type) in DECISIONS.items():
            with self.subTest(name):
                decision, options = _decision(name)
                context = decision.context
                self.assertIs(classify_l2a_decision(context), kind)
                guarded = guard_action(context, kind)
                if guarded_type is None:
                    self.assertIsNone(guarded)
                    candidates = scorer_candidates(context, kind)
                    self.assertTrue(candidates)
                    for action in candidates:
                        self.assertNotIsInstance(
                            action,
                            (
                                AnkanAction,
                                KakanAction,
                                DaiminkanAction,
                                KyuushuKyuuhaiAction,
                            ),
                        )
                        _maps_back(self, decision, options, action)
                else:
                    self.assertIsInstance(guarded, guarded_type)
                    _maps_back(self, decision, options, guarded)

    def test_composite_policy_decides_through_the_bridge(self):
        policy, scorers = self._policy()
        for name, (_, kind, _) in DECISIONS.items():
            with self.subTest(name):
                decision, options = _decision(name)
                result = policy.decide(decision.context)
                self.assertIs(result.kind, kind)
                _maps_back(self, decision, options, result.action)
                if kind in scorers:
                    # 候補の並びはencodingが決める。集合はscorer候補と同じobject
                    self.assertEqual(
                        {id(action) for action in result.candidates},
                        {id(a) for a in scorer_candidates(decision.context, kind)},
                    )
                    self.assertIs(result.action, result.candidates[0])
        self.assertTrue(scorers[L2aDecisionKind.DISCARD].calls)
        self.assertTrue(scorers[L2aDecisionKind.CALL].calls)

    def test_single_legal_action_is_forced(self):
        source, options = _reaction_pon_pass()
        decision = build_decision(source, options[1:])
        self.assertIs(classify_l2a_decision(decision.context), L2aDecisionKind.FORCED)
        policy, _ = self._policy()
        result = policy.decide(decision.context)
        self.assertIs(result.action, decision.context.legal_actions[0])
        _maps_back(self, decision, options[1:], result.action)


if __name__ == "__main__":
    unittest.main()
