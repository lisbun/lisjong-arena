"""Issue #455 deal-in-risk discard disagreement diagnostic tests."""

import os
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
    ankan_log,
    dahai,
    generated_game_log,
    hora,
    kakan_log,
    multi_ron_log,
    normal_discard_log,
    pon_log,
    reach,
    reach_accepted,
    riichi_log,
    riichi_stick_log,
    ron_log,
    start_game,
    start_kyoku,
    tsumo,
    tsumo_win_log,
)
from lisjong.policy_contract import DecisionContext
from lisjong.policy_contract.action import DiscardAction
from lisjong.policy_contract.discard import Discard
from lisjong.policy_contract.riichi import RiichiState
from lisjong.policy_contract.seat import Seat

from lisjong_arena.riichienv.adapter.tile_conversion import tile_from_mjai
from lisjong_arena.riichilab_deal_in_risk_disagreement import (
    analysis as analysis_module,
)
from lisjong_arena.riichilab_deal_in_risk_disagreement.analysis import (
    DealInRiskDisagreementAnalyzer,
    DiagnosticBlockedError,
    DiagnosticInvalidError,
    _genbutsu_bounds,
    _is_genbutsu,
    _RiichiDeclarationTracker,
    classify_region,
    ronned_discard_keys,
    run_diagnostic,
)
from lisjong_arena.riichilab_source_pilot.materialization import materialize_game

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


def _other_discard(observation):
    for action in observation.decision.legal_actions:
        if isinstance(action, DiscardAction) and action != observation.teacher_action:
            return action
    raise AssertionError("fixture did not expose a discard-vs-discard choice")


def _with_players(observation, players):
    decision = observation.decision
    return replace(
        observation,
        decision=DecisionContext(
            input=replace(decision.input, players=tuple(players)),
            legal_actions=decision.legal_actions,
        ),
    )


class _FixedPolicy:
    def __init__(self, selected):
        self._selected = selected

    def choose_action(self, decision):
        return self._selected


class _TeacherEchoPolicy:
    """直前に設定された行動を返す。testが1 decisionごとに設定する。"""

    selected = None

    def choose_action(self, decision):
        return type(self).selected


def _analyzer_for(selected):
    return DealInRiskDisagreementAnalyzer(policy_factory=lambda: _FixedPolicy(selected))


def _riichi_passed_tile_log():
    """seat 0のリーチ後、seat 1が切った1sをseat 2がツモ切りするlog。"""
    return [
        start_game(),
        start_kyoku([TENPAI_HAND, NEUTRAL_1, NEUTRAL_2, NEUTRAL_3]),
        tsumo(0, "9s"),
        reach(0),
        dahai(0, "9s", tsumogiri=True),
        reach_accepted(0),
        tsumo(1, "9m"),
        dahai(1, "1s", tsumogiri=False),
        tsumo(2, "1s"),
        dahai(2, "1s", tsumogiri=True),
        RYUKYOKU,
        *END_EVENTS,
    ]


def _riichi_deal_in_log():
    """seat 0のリーチへseat 1が3pを放銃するlog。"""
    return [
        start_game(),
        start_kyoku([TENPAI_HAND, NEUTRAL_1, NEUTRAL_2, NEUTRAL_3]),
        tsumo(0, "9s"),
        reach(0),
        dahai(0, "9s", tsumogiri=True),
        reach_accepted(0),
        tsumo(1, "9m"),
        dahai(1, "3p", tsumogiri=False),
        hora(0, 1, "3p"),
        *END_EVENTS,
    ]


def _after(log, *events):
    """logの流局直前へeventを差し込む。"""
    index = log.index(RYUKYOKU)
    return [*log[:index], *events, *log[index:]]


class RegionClassificationTests(unittest.TestCase):
    def test_one_accepted_riichi_opponent_is_r1(self):
        observation = _discard_choices(riichi_stick_log(), 1)[0]
        self.assertEqual(classify_region(observation.decision.input), "R1")

    def test_two_accepted_riichi_opponents_are_r2(self):
        observation = _discard_choices(riichi_stick_log(), 2)[0]
        players = list(observation.decision.input.players)
        players[1] = replace(players[1], riichi=RiichiState.ACCEPTED)
        mutated = _with_players(observation, players)
        self.assertEqual(classify_region(mutated.decision.input), "R2")

    def test_declared_but_not_accepted_riichi_is_not_a_riichi_region(self):
        observation = _discard_choices(riichi_stick_log(), 1)[0]
        players = list(observation.decision.input.players)
        players[0] = replace(players[0], riichi=RiichiState.DECLARED)
        mutated = _with_players(observation, players)
        self.assertIsNone(classify_region(mutated.decision.input))

    def test_open_meld_opponent_without_riichi_is_f(self):
        log = _after(pon_log(), tsumo(3, "9m"), dahai(3, "9m", tsumogiri=True))
        observation = _discard_choices(log, 3)[0]
        self.assertEqual(classify_region(observation.decision.input), "F")

    def test_own_open_meld_alone_is_no_region(self):
        observation = _discard_choices(pon_log(), 2)[0]
        self.assertIsNone(classify_region(observation.decision.input))

    def test_ankan_only_opponent_is_no_region(self):
        log = _after(ankan_log(), tsumo(1, "9m"), dahai(1, "9m", tsumogiri=True))
        observation = _discard_choices(log, 1)[0]
        melds = observation.decision.input.players[0].melds
        self.assertTrue(melds)
        self.assertIsNone(classify_region(observation.decision.input))


class AnalyzerScopeTests(unittest.TestCase):
    def test_in_scope_agreement_is_attributed_to_bot_region_and_breakdowns(self):
        observation = _discard_choices(riichi_stick_log(), 1)[0]
        analyzer = _analyzer_for(observation.teacher_action)

        analyzer.observe(observation)
        document = analyzer.aggregate_document()

        for bots in (document["all_bots"], document["per_bot"][BOT_NAME]):
            region = bots["regions"]["R1"]
            self.assertEqual(region["agreement"]["total"], 1)
            self.assertEqual(region["agreement"]["agreements"], 1)
            self.assertEqual(region["turn_band"]["1-6"]["total"], 1)
            self.assertEqual(region["own_meld"]["closed"]["total"], 1)
            self.assertEqual(region["turn_band_by_own_meld"]["1-6|closed"]["total"], 1)
            self.assertEqual(bots["regions"]["R2"]["agreement"]["total"], 0)
            self.assertEqual(bots["regions"]["F"]["agreement"]["total"], 0)
            self.assertNotIn("genbutsu_disagreement", bots["regions"]["F"])
        self.assertEqual(
            document["per_bot"]["FuuroMaster"]["regions"]["R1"]["agreement"]["total"],
            0,
        )

    def test_discard_disagreement_compares_post_discard_shanten(self):
        observation = _discard_choices(riichi_stick_log(), 1)[0]
        analyzer = _analyzer_for(_other_discard(observation))

        with mock.patch(
            "lisjong_arena.riichilab_disagreement_diagnostic.analysis"
            ".calculate_shanten",
            side_effect=[1, 0],
        ):
            analyzer.observe(observation)

        region = analyzer.aggregate_document()["all_bots"]["regions"]["R1"]
        self.assertEqual(region["agreement"]["disagreements"], 1)
        shanten = region["discard_shanten_disagreement"]
        self.assertEqual(shanten["discard_vs_discard_disagreements"], 1)
        self.assertEqual(shanten["teacher_shanten_worse"], 1)
        self.assertEqual(shanten["comparator_shanten_worse"], 0)

    def test_non_discard_comparator_action_is_counted_separately(self):
        observations = _captured(riichi_log())
        riichi_turn = next(
            item
            for item in observations
            if item.legal_action_count >= 2
            and not isinstance(item.teacher_action, DiscardAction)
        )
        discard = next(
            action
            for action in riichi_turn.decision.legal_actions
            if isinstance(action, DiscardAction)
        )
        players = list(riichi_turn.decision.input.players)
        players[1] = replace(
            players[1],
            discards=(Discard(discard.tile, False, 0, None),),
            riichi=RiichiState.ACCEPTED,
        )
        mutated = replace(
            _with_players(riichi_turn, players),
            teacher_action=discard,
        )
        analyzer = _analyzer_for(riichi_turn.teacher_action)

        analyzer.observe(mutated)

        region = analyzer.aggregate_document()["all_bots"]["regions"]["R1"]
        self.assertEqual(region["agreement"]["disagreements"], 1)
        self.assertEqual(region["comparator_non_discard_disagreements"], 1)
        self.assertEqual(
            region["discard_shanten_disagreement"]["discard_vs_discard_disagreements"],
            0,
        )

    def test_forced_decision_does_not_execute_the_comparator(self):
        forced = next(
            item for item in _captured(riichi_log()) if item.legal_action_count == 1
        )

        def fail_if_called():
            raise AssertionError("forced decisions must not execute the comparator")

        analyzer = DealInRiskDisagreementAnalyzer(policy_factory=fail_if_called)
        analyzer.observe(forced)
        document = analyzer.aggregate_document()

        self.assertEqual(document["coverage"]["forced_decisions"], 1)
        self.assertEqual(document["coverage"]["choice_decisions"], 0)
        self.assertEqual(document["all_bots"]["counts_only"]["forced_decisions"], 1)

    def test_rows_outside_the_regions_are_counted_only(self):
        analyzer = DealInRiskDisagreementAnalyzer(policy_factory=_TeacherEchoPolicy)
        riichi_turn = next(
            item
            for item in _captured(riichi_log())
            if item.legal_action_count >= 2
            and not isinstance(item.teacher_action, DiscardAction)
        )
        no_region = _discard_choices(normal_discard_log(), 0)[0]
        rows = [riichi_turn, replace(no_region, game_id="no-region")]
        for state in (RiichiState.DECLARED, RiichiState.ACCEPTED):
            players = list(no_region.decision.input.players)
            players[0] = replace(players[0], riichi=state)
            rows.append(replace(_with_players(no_region, players), game_id=state.value))
        for observation in rows:
            _TeacherEchoPolicy.selected = observation.teacher_action
            analyzer.observe(observation)

        document = analyzer.aggregate_document()
        counts = document["all_bots"]["counts_only"]
        self.assertEqual(document["coverage"]["choice_decisions"], 4)
        self.assertEqual(counts["non_discard_choice_decisions"], 1)
        self.assertEqual(counts["discard_choice_rows"], 3)
        self.assertEqual(counts["no_region_discard_choice_rows"], 1)
        self.assertEqual(counts["own_riichi_declaration_discard_rows"], 1)
        self.assertEqual(counts["own_riichi_accepted_discard_choice_rows"], 1)
        regions = document["all_bots"]["regions"]
        self.assertEqual(sum(regions[key]["agreement"]["total"] for key in regions), 0)

    def test_illegal_comparator_result_fails_closed(self):
        observation = _discard_choices(riichi_stick_log(), 1)[0]
        analyzer = _analyzer_for(object())
        with self.assertRaises(DiagnosticBlockedError):
            analyzer.observe(observation)


class GenbutsuTests(unittest.TestCase):
    def _genbutsu(self, observation, comparator_tile):
        analyzer = _analyzer_for(_discard_of(observation, comparator_tile))
        analyzer.observe(observation)
        return analyzer.aggregate_document()["all_bots"]["regions"]

    def test_tile_passed_after_riichi_is_genbutsu(self):
        # seat 2の教師打牌1sは、リーチ後にseat 1が切って通った牌である。
        observation = _discard_choices(_riichi_passed_tile_log(), 2)[0]
        self.assertEqual(observation.teacher_action.tile, tile_from_mjai("1s"))

        genbutsu = self._genbutsu(observation, "W")["R1"]["genbutsu_disagreement"]

        self.assertEqual(genbutsu["discard_vs_discard_disagreements"], 1)
        self.assertEqual(genbutsu["teacher_genbutsu_comparator_not"], 1)

    def test_riichi_players_own_discard_is_genbutsu(self):
        log = _after(
            riichi_stick_log(),
            tsumo(3, "9s"),
            dahai(3, "C", tsumogiri=False),
        )
        observation = _discard_choices(log, 3)[0]

        genbutsu = self._genbutsu(observation, "9s")["R1"]["genbutsu_disagreement"]

        self.assertEqual(genbutsu["comparator_genbutsu_teacher_not"], 1)

    def test_r2_requires_genbutsu_against_every_riichi_player(self):
        # seat 0の現物は9sと1s、seat 1の現物は1sだけ。Wはどちらの現物でもない。
        observation = _discard_choices(riichi_stick_log(), 2)[0]
        players = list(observation.decision.input.players)
        players[1] = replace(players[1], riichi=RiichiState.ACCEPTED)
        mutated = _with_players(observation, players)

        bounds = tuple(
            _genbutsu_bounds(mutated.decision.input, seat, (0, 0)) for seat in (0, 1)
        )
        nine_sou = mutated.decision.input.players[0].discards[0].tile.tile_type
        one_sou = mutated.decision.input.players[1].discards[0].tile.tile_type
        self.assertIs(_is_genbutsu(one_sou, bounds), True)
        self.assertIs(_is_genbutsu(nine_sou, bounds), False)

        regions = self._genbutsu(mutated, "9m")
        self.assertEqual(regions["R2"]["genbutsu_disagreement"]["neither_genbutsu"], 1)
        self.assertEqual(regions["R1"]["agreement"]["total"], 0)

    def test_declaration_position_is_exact_when_one_discard_separates_states(self):
        observation = _discard_choices(riichi_stick_log(), 1)[0]
        tracker = _RiichiDeclarationTracker()
        tracker.update(observation.decision.input)
        self.assertEqual(tracker.declaration_range, {0: (0, 0)})

    def test_ambiguous_declaration_position_is_kept_as_a_range(self):
        observation = _discard_choices(riichi_stick_log(), 2)[0]
        policy_input = observation.decision.input
        riichi_tile = policy_input.players[0].discards[0].tile
        passed_tile = policy_input.players[1].discards[0].tile
        players = list(policy_input.players)
        # 自席の手番が飛び、リーチ者の打牌を2枚まとめて初めて観測した状況。
        players[0] = replace(
            players[0],
            discards=(
                Discard(riichi_tile, False, 0, None),
                Discard(riichi_tile, True, 2, None),
            ),
        )
        players[1] = replace(
            players[1], discards=(Discard(passed_tile, False, 1, None),)
        )
        ambiguous = replace(policy_input, players=tuple(players))

        tracker = _RiichiDeclarationTracker()
        tracker.update(ambiguous)
        self.assertEqual(tracker.declaration_range, {0: (0, 1)})

        bounds = (_genbutsu_bounds(ambiguous, 0, tracker.declaration_range[0]),)
        self.assertIs(_is_genbutsu(riichi_tile.tile_type, bounds), True)
        self.assertIsNone(_is_genbutsu(passed_tile.tile_type, bounds))

    def test_hand_discard_after_the_candidate_excludes_it(self):
        observation = _discard_choices(riichi_stick_log(), 2)[0]
        policy_input = observation.decision.input
        tile = policy_input.players[0].discards[0].tile
        players = list(policy_input.players)
        players[0] = replace(
            players[0],
            discards=(
                Discard(tile, True, 0, None),
                Discard(tile, False, 2, None),
            ),
        )
        tracker = _RiichiDeclarationTracker()
        tracker.update(replace(policy_input, players=tuple(players)))
        self.assertEqual(tracker.declaration_range, {0: (1, 1)})


class RonnedDiscardTests(unittest.TestCase):
    def test_single_and_multi_ron_mark_the_discard_once(self):
        expected = {("g", 0, "E", 1, 0, 1): BOT_ID}
        for log in (ron_log(), multi_ron_log()):
            self.assertEqual(
                ronned_discard_keys(log, game_id="g", target_seats={0: BOT_ID}),
                expected,
            )

    def test_non_target_seat_tsumo_and_chankan_are_not_ronned_discards(self):
        self.assertEqual(
            ronned_discard_keys(ron_log(), game_id="g", target_seats={1: BOT_ID}),
            {},
        )
        for log in (tsumo_win_log(), kakan_log(with_chankan_ron=True)):
            self.assertEqual(
                ronned_discard_keys(log, game_id="g", target_seats={0: BOT_ID}),
                {},
            )

    def test_ronned_teacher_discard_is_joined_to_its_region_row(self):
        log = _riichi_deal_in_log()
        observation = _discard_choices(log, 1)[0]
        analyzer = _analyzer_for(_other_discard(observation))
        analyzer.observe(observation)
        ronned = ronned_discard_keys(
            log, game_id="fixture", target_seats={1: BOT_ID, 2: 126}
        )

        document = analyzer.aggregate_document(ronned)

        region = document["per_bot"][BOT_NAME]["regions"]["R1"]
        self.assertEqual(
            region["teacher_discard_ronned"],
            {"total": 1, "in_agreement_rows": 0, "in_disagreement_rows": 1},
        )
        counts = document["per_bot"][BOT_NAME]["counts_only"]
        self.assertEqual(counts["teacher_discards_ronned_any_row"], 1)
        other = document["per_bot"]["FuuroMaster"]
        self.assertEqual(other["regions"]["R1"]["teacher_discard_ronned"]["total"], 0)


class GeneratedGameInvariantTests(unittest.TestCase):
    def test_counts_partition_and_declaration_matches_public_events(self):
        events, _live = generated_game_log(7, game_mode=GAME_MODE)

        # 公開eventから求めた、局・席ごとの宣言牌の位置（0始まり）。
        truth = {}
        kyoku = None
        discard_counts = [0, 0, 0, 0]
        pending_reach = None
        for event in events:
            if event["type"] == "start_kyoku":
                kyoku = (event["bakaze"], event["kyoku"], event["honba"])
                discard_counts = [0, 0, 0, 0]
            elif event["type"] == "reach":
                pending_reach = event["actor"]
            elif event["type"] == "dahai":
                if pending_reach == event["actor"]:
                    truth[(*kyoku, event["actor"])] = discard_counts[event["actor"]]
                    pending_reach = None
                discard_counts[event["actor"]] += 1

        analyzer = DealInRiskDisagreementAnalyzer(policy_factory=_TeacherEchoPolicy)
        checked = 0

        def observe(observation):
            nonlocal checked
            legal = observation.decision.legal_actions
            _TeacherEchoPolicy.selected = legal[len(legal) // 2]
            analyzer.observe(observation)
            tracker = analyzer._trackers[
                (observation.game_id, int(observation.actor_seat))
            ]
            round_state = observation.decision.input.round
            bakaze = analysis_module._BAKAZE_BY_WIND[round_state.round_wind]
            for seat, (earliest, latest) in tracker.declaration_range.items():
                position = truth[
                    (bakaze, round_state.hand_number, round_state.honba, seat)
                ]
                self.assertLessEqual(earliest, position)
                self.assertLessEqual(position, latest)
                checked += 1

        materialize_game(
            events,
            game_id="generated",
            target_seats=TARGET_SEATS,
            game_mode=GAME_MODE,
            decision_observer=observe,
        )
        ronned = ronned_discard_keys(
            events, game_id="generated", target_seats=dict.fromkeys(range(4), BOT_ID)
        )
        bots = analyzer.aggregate_document(ronned)["all_bots"]
        counts = bots["counts_only"]
        regions = bots["regions"]

        self.assertGreater(checked, 0)
        self.assertGreater(regions["R1"]["agreement"]["total"], 0)
        self.assertEqual(
            counts["discard_choice_rows"],
            counts["own_riichi_accepted_discard_choice_rows"]
            + counts["own_riichi_declaration_discard_rows"]
            + counts["no_region_discard_choice_rows"]
            + sum(regions[key]["agreement"]["total"] for key in regions),
        )
        for key, region in regions.items():
            total = region["agreement"]["total"]
            shanten = region["discard_shanten_disagreement"]
            pairs = shanten["discard_vs_discard_disagreements"]
            self.assertEqual(
                region["agreement"]["disagreements"],
                region["comparator_non_discard_disagreements"] + pairs,
            )
            self.assertEqual(
                pairs,
                shanten["teacher_shanten_worse"]
                + shanten["comparator_shanten_worse"]
                + shanten["same_shanten"]
                + shanten["unavailable"],
            )
            for breakdown in ("turn_band", "own_meld", "turn_band_by_own_meld"):
                self.assertEqual(
                    sum(cell["total"] for cell in region[breakdown].values()), total
                )
            if key != "F":
                genbutsu = dict(region["genbutsu_disagreement"])
                self.assertEqual(
                    genbutsu.pop("discard_vs_discard_disagreements"), pairs
                )
                self.assertEqual(sum(genbutsu.values()), pairs)
            self.assertLessEqual(region["teacher_discard_ronned"]["total"], total)


class LockedRuntimeTests(unittest.TestCase):
    def _run(self):
        with mock.patch.object(
            analysis_module, "materialize_local_corpus"
        ) as materialize:
            with self.assertRaises(DiagnosticInvalidError):
                run_diagnostic(object(), "unused", arena_revision="a" * 40)
        materialize.assert_not_called()

    def test_other_lisjong_revision_stops_before_reading_the_corpus(self):
        with (
            mock.patch.object(
                analysis_module, "_runtime_lisjong_revision", return_value="b" * 40
            ),
            mock.patch.dict(os.environ, {"LISJONG_SHANTEN_BACKEND": "rust"}),
        ):
            self._run()

    def test_other_shanten_backend_stops_before_reading_the_corpus(self):
        with (
            mock.patch.object(
                analysis_module,
                "_runtime_lisjong_revision",
                return_value=analysis_module.LISJONG_REVISION,
            ),
            mock.patch.dict(os.environ, {"LISJONG_SHANTEN_BACKEND": "python"}),
        ):
            self._run()

    def test_locked_runtime_is_accepted(self):
        with (
            mock.patch.object(
                analysis_module,
                "_runtime_lisjong_revision",
                return_value=analysis_module.LISJONG_REVISION,
            ),
            mock.patch.dict(os.environ, {"LISJONG_SHANTEN_BACKEND": "rust"}),
        ):
            self.assertEqual(analysis_module._require_locked_runtime(), "0.4.10")


if __name__ == "__main__":
    unittest.main()
