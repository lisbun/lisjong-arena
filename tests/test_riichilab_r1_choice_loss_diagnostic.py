"""Issue #483 R1 choice / loss diagnostic tests."""

import unittest
from dataclasses import replace
from unittest import mock

from _riichilab_source_pilot_fixtures import (
    END_EVENTS,
    NEUTRAL_1,
    NEUTRAL_2,
    NEUTRAL_3,
    RYUKYOKU,
    TENPAI_HAND,
    dahai,
    generated_game_log,
    hora,
    kakan_log,
    multi_ron_log,
    normal_discard_log,
    reach,
    reach_accepted,
    riichi_log,
    riichi_stick_log,
    start_game,
    start_kyoku,
    tsumo,
)
from lisjong.policy_contract import DecisionContext
from lisjong.policy_contract.action import DiscardAction
from lisjong.policy_contract.discard import Discard
from lisjong.policy_contract.seat import Seat

from lisjong_arena.riichienv.adapter.tile_conversion import tile_from_mjai
from lisjong_arena.riichilab_deal_in_risk_disagreement.analysis import (
    _RiichiDeclarationTracker,
)
from lisjong_arena.riichilab_r1_choice_loss_diagnostic import (
    champion_play as champion_module,
)
from lisjong_arena.riichilab_r1_choice_loss_diagnostic import (
    classification as classification_module,
)
from lisjong_arena.riichilab_r1_choice_loss_diagnostic.champion_play import (
    CHAMPION_GROUPS,
    ChampionPlayAnalyzer,
    DiagnosticInvalidError,
    _select_games,
    seat_event_summary,
)
from lisjong_arena.riichilab_r1_choice_loss_diagnostic.classification import (
    classify_r1_choice,
)
from lisjong_arena.riichilab_r1_choice_loss_diagnostic.strong_bots import (
    StrongBotR1Analyzer,
)
from lisjong_arena.riichilab_source_pilot.materialization import (
    GameMaterialization,
    GameUnsupportedReason,
    materialize_game,
)

GAME_MODE = "4p-red-single"
BOT_ID = 120
BOT_NAME = "Mortal-v4b"
TARGET_SEATS = {Seat(index): BOT_ID for index in range(4)}


def _captured(log):
    observations = []
    materialize_game(
        log,
        game_id="fixture",
        target_seats=TARGET_SEATS,
        game_mode=GAME_MODE,
        decision_observer=observations.append,
    )
    return observations


def _discard_choices(log, seat):
    return [
        item
        for item in _captured(log)
        if int(item.actor_seat) == seat
        and item.legal_action_count >= 2
        and isinstance(item.teacher_action, DiscardAction)
    ]


def _discard_of(observation, tile_name):
    for action in observation.decision.legal_actions:
        if isinstance(action, DiscardAction) and action.tile == tile_from_mjai(
            tile_name
        ):
            return action
    raise AssertionError(f"fixture has no legal discard of {tile_name}")


def _tracked(observation):
    tracker = _RiichiDeclarationTracker()
    tracker.update(observation.decision.input)
    return tracker


def _riichi_log(*events):
    """seat 0がリーチした後へeventを続けるlog。"""
    return [
        start_game(),
        start_kyoku([TENPAI_HAND, NEUTRAL_1, NEUTRAL_2, NEUTRAL_3]),
        tsumo(0, "9s"),
        reach(0),
        dahai(0, "9s", tsumogiri=True),
        reach_accepted(0),
        tsumo(1, "9m"),
        *events,
        *END_EVENTS,
    ]


def _passed_tile_log():
    """リーチ後にseat 1が切った1sを、seat 2がツモ切りするlog。"""
    return _riichi_log(
        dahai(1, "1s", tsumogiri=False),
        tsumo(2, "1s"),
        dahai(2, "1s", tsumogiri=True),
        RYUKYOKU,
    )


def _deal_in_log(*, loss=8000):
    """seat 0のリーチへseat 1が3pを放銃するlog。"""
    win = hora(0, 1, "3p")
    win["deltas"] = [loss, -loss, 0, 0]
    return _riichi_log(dahai(1, "3p", tsumogiri=False), win)


def _shanten_by_tile(values, default):
    """牌ごとの打牌後向聴数を返す、`_post_discard_shanten`の差し替え。"""
    table = {tile_from_mjai(name): value for name, value in values.items()}

    def fake(_observation, action):
        return table.get(action.tile, default)

    return mock.patch.object(
        classification_module, "_post_discard_shanten", side_effect=fake
    )


class _FixedPolicy:
    def __init__(self, selected):
        self._selected = selected

    def choose_action(self, decision):
        return self._selected


def _analyzer_for(selected):
    return StrongBotR1Analyzer(policy_factory=lambda: _FixedPolicy(selected))


class ClassificationTests(unittest.TestCase):
    def test_no_genbutsu_among_legal_discards_is_none(self):
        observation = _discard_choices(riichi_stick_log(), 1)[0]
        choice = classify_r1_choice(observation, _tracked(observation))
        self.assertEqual(choice.genbutsu_availability, "none")

    def test_availability_looks_at_candidates_not_at_the_selected_tile(self):
        # seat 2の実打牌は現物の1s。別の牌を選んでも「現物あり」は変わらない。
        observation = _discard_choices(_passed_tile_log(), 2)[0]
        choice = classify_r1_choice(observation, _tracked(observation))

        self.assertEqual(choice.genbutsu_availability, "available")
        self.assertEqual(
            choice.selected_genbutsu(observation.teacher_action), "genbutsu"
        )
        self.assertEqual(
            choice.selected_genbutsu(_discard_of(observation, "W")), "not_genbutsu"
        )

    def test_ambiguous_only_candidate_is_undetermined_not_none(self):
        observation = _discard_choices(_passed_tile_log(), 2)[0]
        policy_input = observation.decision.input
        riichi_tile = policy_input.players[0].discards[0].tile
        players = list(policy_input.players)
        # 宣言牌の位置が決まらず、1sがリーチ後に通ったかどうかが分からない状況。
        players[0] = replace(
            players[0],
            discards=(
                Discard(riichi_tile, False, 0, None),
                Discard(riichi_tile, True, 2, None),
            ),
        )
        ambiguous = replace(
            observation,
            decision=DecisionContext(
                input=replace(policy_input, players=tuple(players)),
                legal_actions=observation.decision.legal_actions,
            ),
        )
        tracker = _tracked(ambiguous)
        self.assertEqual(tracker.declaration_range, {0: (0, 1)})

        choice = classify_r1_choice(ambiguous, tracker)

        self.assertEqual(choice.genbutsu_availability, "undetermined")
        self.assertEqual(
            choice.selected_genbutsu(ambiguous.teacher_action), "unavailable"
        )

    def test_keep_is_relative_to_the_minimum_over_legal_discards(self):
        observation = _discard_choices(_passed_tile_log(), 2)[0]
        with _shanten_by_tile({"1s": 3, "W": 2}, default=4):
            choice = classify_r1_choice(observation, _tracked(observation))

        self.assertEqual(choice.minimum_post_discard_shanten, 2)
        self.assertEqual(choice.shanten_state(_discard_of(observation, "W")), "keep")
        self.assertEqual(choice.shanten_state(observation.teacher_action), "worse")
        self.assertIn(("minimum_post_discard_shanten", "2+"), choice.axes())
        self.assertIn(
            ("genbutsu_availability|minimum_post_discard_shanten", "available|2+"),
            choice.axes(),
        )

    def test_unavailable_candidate_shanten_makes_the_row_unavailable(self):
        observation = _discard_choices(_passed_tile_log(), 2)[0]
        with _shanten_by_tile({"W": None}, default=1):
            choice = classify_r1_choice(observation, _tracked(observation))

        self.assertIsNone(choice.minimum_post_discard_shanten)
        self.assertEqual(
            choice.shanten_state(observation.teacher_action), "unavailable"
        )
        self.assertIn(("minimum_post_discard_shanten", "unavailable"), choice.axes())

    def test_pre_discard_shanten_uses_the_whole_concealed_hand(self):
        observation = _discard_choices(_passed_tile_log(), 2)[0]
        concealed = list(observation.decision.input.own_hand.concealed_tiles)
        with mock.patch.object(
            classification_module, "calculate_shanten", return_value=1
        ) as shanten:
            choice = classify_r1_choice(observation, _tracked(observation))

        shanten.assert_called_once_with(concealed)
        self.assertEqual(len(concealed) % 3, 2)
        self.assertEqual(choice.pre_discard_shanten, 1)
        self.assertIn(("pre_discard_shanten", "1"), choice.axes())


class StrongBotAnalyzerTests(unittest.TestCase):
    def _cell(self, analyzer, axis="all", value="all"):
        document = analyzer.aggregate_document()
        self.assertEqual(
            document["per_bot"][BOT_NAME]["axes"], document["all_bots"]["axes"]
        )
        return document["all_bots"]["axes"][axis][value]

    def test_agreement_records_the_single_selected_state(self):
        observation = _discard_choices(_passed_tile_log(), 2)[0]
        analyzer = _analyzer_for(observation.teacher_action)
        with _shanten_by_tile({"1s": 3}, default=2):
            analyzer.observe(observation)

        cell = self._cell(analyzer, "genbutsu_availability", "available")
        self.assertEqual(cell["rows"], 1)
        self.assertEqual(cell["agreement"], {"total": 1, "worse": 1})
        self.assertEqual(analyzer.r1_counts(BOT_ID), (1, 1, 0, 0))

    def test_discard_disagreement_is_split_into_four_exclusive_pairs(self):
        observation = _discard_choices(_passed_tile_log(), 2)[0]
        cases = {
            "both_keep": {"1s": 2, "W": 2},
            "comparator_only_worse": {"1s": 2, "W": 3},
            "teacher_only_worse": {"1s": 3, "W": 2},
            "both_worse": {"1s": 3, "W": 3},
        }
        for label, values in cases.items():
            with self.subTest(label=label):
                analyzer = _analyzer_for(_discard_of(observation, "W"))
                with _shanten_by_tile(values, default=2):
                    analyzer.observe(observation)
                cell = self._cell(analyzer)
                self.assertEqual(cell["discard_disagreement"], {"total": 1, label: 1})
                self.assertEqual(analyzer.r1_counts(BOT_ID), (1, 0, 1, 0))

    def test_relative_comparison_and_selected_genbutsu_are_kept_separately(self):
        observation = _discard_choices(_passed_tile_log(), 2)[0]
        analyzer = _analyzer_for(_discard_of(observation, "W"))
        # 両者とも最小値より悪いが、相対比較では強豪botの方が悪い。
        with _shanten_by_tile({"1s": 4, "W": 3}, default=2):
            analyzer.observe(observation)

        bots = analyzer.aggregate_document()["all_bots"]
        self.assertEqual(
            bots["axes"]["all"]["all"]["discard_disagreement"],
            {"total": 1, "both_worse": 1},
        )
        self.assertEqual(
            bots["relative_post_discard_shanten"],
            {"available": {"teacher_shanten_worse": 1}},
        )
        self.assertEqual(
            bots["selected_genbutsu"]["available"]["discard_disagreement"],
            {"teacher=genbutsu|comparator=not_genbutsu": 1},
        )

    def test_comparator_non_discard_is_separated_from_discard_pairs(self):
        riichi_turn = next(
            item
            for item in _captured(riichi_log())
            if item.legal_action_count >= 2
            and not isinstance(item.teacher_action, DiscardAction)
        )
        observation = _discard_choices(_passed_tile_log(), 2)[0]
        analyzer = _analyzer_for(riichi_turn.teacher_action)
        with mock.patch(
            "lisjong_arena.riichilab_r1_choice_loss_diagnostic.strong_bots"
            ".execute_policy",
            return_value=riichi_turn.teacher_action,
        ):
            analyzer.observe(observation)

        cell = self._cell(analyzer)
        self.assertEqual(cell["comparator_non_discard"]["total"], 1)
        self.assertEqual(cell["discard_disagreement"], {})
        self.assertEqual(analyzer.r1_counts(BOT_ID), (1, 0, 0, 1))
        self.assertEqual(
            analyzer.aggregate_document()["all_bots"]["relative_post_discard_shanten"],
            {},
        )

    def test_rows_outside_r1_are_not_counted(self):
        observation = _discard_choices(normal_discard_log(), 0)[0]
        analyzer = _analyzer_for(observation.teacher_action)
        analyzer.observe(observation)

        document = analyzer.aggregate_document()
        self.assertEqual(document["coverage"]["choice_decisions"], 1)
        self.assertEqual(document["all_bots"]["axes"], {})
        self.assertEqual(analyzer.r1_counts(BOT_ID), (0, 0, 0, 0))

    def test_forced_decision_does_not_execute_the_comparator(self):
        forced = next(
            item for item in _captured(riichi_log()) if item.legal_action_count == 1
        )

        def fail_if_called():
            raise AssertionError("forced decisions must not execute the comparator")

        analyzer = StrongBotR1Analyzer(policy_factory=fail_if_called)
        analyzer.observe(forced)
        self.assertEqual(
            analyzer.aggregate_document()["coverage"]["forced_decisions"], 1
        )


class SeatEventSummaryTests(unittest.TestCase):
    def test_ronned_discard_carries_the_payment_and_the_r1_state(self):
        summary = seat_event_summary(_deal_in_log(loss=8000), game_id="g", seat=1)

        self.assertEqual((summary.rounds, summary.deal_in_rounds), (1, 1))
        self.assertEqual(list(summary.ronned), [("g", 1, "E", 1, 0, 1)])
        hit = summary.ronned[("g", 1, "E", 1, 0, 1)]
        self.assertEqual((hit.loss, hit.r1_by_events), (8000, True))

    def test_multi_ron_is_one_discard_with_summed_payments(self):
        log = multi_ron_log()
        for event, loss in zip(
            [item for item in log if item["type"] == "hora"], (2000, 3900)
        ):
            event["deltas"] = [-loss, 0, 0, 0]
        summary = seat_event_summary(log, game_id="g", seat=0)

        self.assertEqual(summary.deal_in_rounds, 1)
        hit = summary.ronned[("g", 0, "E", 1, 0, 1)]
        self.assertEqual((hit.loss, hit.r1_by_events), (5900, False))

    def test_chankan_is_a_deal_in_round_but_not_a_ronned_discard(self):
        summary = seat_event_summary(
            kakan_log(with_chankan_ron=True), game_id="g", seat=0
        )
        self.assertEqual(summary.deal_in_rounds, 1)
        self.assertEqual(summary.ronned, {})

    def test_other_seats_deal_in_is_not_counted(self):
        summary = seat_event_summary(_deal_in_log(), game_id="g", seat=2)
        self.assertEqual((summary.rounds, summary.deal_in_rounds), (1, 0))
        self.assertEqual(summary.ronned, {})


class ChampionPlayAnalyzerTests(unittest.TestCase):
    def test_actual_r1_discard_is_classified_and_joined_to_its_loss(self):
        analyzer = ChampionPlayAnalyzer()
        with mock.patch.object(champion_module, "SOURCE_GAME_MODE", GAME_MODE):
            analyzer.analyze_game(
                "dev",
                game_id="g",
                seat=1,
                bot_id=313,
                events=_deal_in_log(loss=8000),
            )

        document = analyzer.aggregate_document()
        dev = document["per_bot"]["dev"]
        cell = dev["axes"]["genbutsu_availability"]["none"]
        self.assertEqual(cell["discards"]["total"], 1)
        self.assertEqual(cell["ronned_discards"]["total"], 1)
        self.assertEqual(cell["deal_in_loss"]["total"], 8000)
        self.assertEqual(cell["deal_in_loss_share_of_r1"], 1.0)
        self.assertEqual(dev["r1_deal_in_loss_total"], 8000)
        self.assertIn(
            "none|not_genbutsu", dev["axes"]["genbutsu_availability|selected_genbutsu"]
        )
        counts = dev["counts_only"]
        self.assertEqual(counts["replayed_games"], 1)
        self.assertEqual(counts["r1_ronned_discards_by_events"], 1)
        self.assertEqual(counts["ronned_discard_loss"], 8000)
        self.assertEqual(document["per_bot"]["baseline"]["axes"], {})
        self.assertEqual(document["seat_total"]["r1_deal_in_loss_total"], 8000)

    def test_unreplayable_game_drops_partial_rows_and_is_counted_apart(self):
        log = _deal_in_log(loss=8000)
        observations = _captured(log)

        def unsupported(events, *, game_id, decision_observer, **_kwargs):
            for observation in observations:
                if int(observation.actor_seat) == 1:
                    decision_observer(observation)
            return GameMaterialization(
                game_id=game_id,
                supported=False,
                unsupported_reason=next(iter(GameUnsupportedReason)),
                rounds=0,
                decision_opportunities=0,
                rows=(),
                forced_rows=0,
                unresolved_reasons=(),
            )

        analyzer = ChampionPlayAnalyzer()
        with mock.patch.object(
            champion_module, "materialize_game", side_effect=unsupported
        ):
            analyzer.analyze_game("dev", game_id="g", seat=1, bot_id=313, events=log)

        dev = analyzer.aggregate_document()["per_bot"]["dev"]
        self.assertEqual(dev["axes"], {})
        self.assertEqual(dev["counts_only"]["unreplayable_games"], 1)
        self.assertEqual(
            dev["counts_only"]["unreplayable_r1_ronned_loss_by_events"], 8000
        )
        self.assertNotIn("replayed_games", dev["counts_only"])
        self.assertEqual(sum(dev["unreplayable_game_reasons"].values()), 1)

    def test_generated_game_counts_are_consistent_across_axes(self):
        events, _live = generated_game_log(7, game_mode=GAME_MODE)
        analyzer = ChampionPlayAnalyzer()
        with mock.patch.object(champion_module, "SOURCE_GAME_MODE", GAME_MODE):
            for seat in range(4):
                analyzer.analyze_game(
                    "dev",
                    game_id=f"generated-{seat}",
                    seat=seat,
                    bot_id=313,
                    events=events,
                )

        dev = analyzer.aggregate_document()["per_bot"]["dev"]
        total = dev["axes"]["all"]["all"]
        self.assertGreater(total["discards"]["total"], 0)
        for values in dev["axes"].values():
            for name in ("discards", "ronned_discards", "deal_in_loss"):
                self.assertEqual(
                    sum(cell[name]["total"] for cell in values.values()),
                    total[name]["total"],
                )
            for cell in values.values():
                for name in ("discards", "ronned_discards", "deal_in_loss"):
                    parts = dict(cell[name])
                    self.assertEqual(parts.pop("total"), sum(parts.values()))
        counts = dev["counts_only"]
        self.assertLessEqual(
            total["ronned_discards"]["total"], counts["r1_ronned_discards_by_events"]
        )
        self.assertEqual(dev["r1_deal_in_loss_total"], total["deal_in_loss"]["total"])


class LockedSelectionTests(unittest.TestCase):
    def test_other_history_identity_is_rejected(self):
        group = CHAMPION_GROUPS[0]
        history = mock.Mock(bot_id=group.bot_id, identity="0" * 64, games=())
        with mock.patch.object(
            champion_module, "load_published_history", return_value=history
        ):
            with self.assertRaises(DiagnosticInvalidError):
                _select_games(mock.Mock(), group)

    def test_other_game_selection_is_rejected(self):
        group = CHAMPION_GROUPS[0]
        history = mock.Mock(
            bot_id=group.bot_id, identity=group.history_identity, games=()
        )
        with mock.patch.object(
            champion_module, "load_published_history", return_value=history
        ):
            with self.assertRaises(DiagnosticInvalidError):
                _select_games(mock.Mock(), group)


if __name__ == "__main__":
    unittest.main()
