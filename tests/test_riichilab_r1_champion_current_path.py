"""Issue #489 genbutsu-source and Champion current-path tests."""

import unittest
from unittest import mock

from _riichilab_source_pilot_fixtures import (
    END_EVENTS,
    NEUTRAL_1,
    NEUTRAL_2,
    NEUTRAL_3,
    RYUKYOKU,
    TENPAI_HAND,
    dahai,
    reach,
    reach_accepted,
    start_game,
    start_kyoku,
    tsumo,
)
from lisjong.policy_contract.action import DiscardAction
from lisjong.policy_contract.seat import Seat

from lisjong_arena.policy_catalog import POLICY_CATALOG
from lisjong_arena.riichienv.adapter.tile_conversion import tile_from_mjai
from lisjong_arena.riichilab_r1_choice_loss_diagnostic import (
    champion_play as champion_module,
)
from lisjong_arena.riichilab_r1_choice_loss_diagnostic.champion_play import (
    CHAMPION_GROUPS,
    PATH_AXIS,
    SOURCE_AXIS,
    ChampionPlayAnalyzer,
)
from lisjong_arena.riichilab_r1_choice_loss_diagnostic.champion_view import (
    NO_ANALYSIS,
    observe_champion_view,
)
from lisjong_arena.riichilab_source_pilot.materialization import materialize_game

GAME_MODE = "4p-red-single"


def _log(seat_two_draw, seat_two_discard, *, tsumogiri):
    """seat 0がリーチし、seat 1が1sを切った後、seat 2が打牌するlog。"""
    return [
        start_game(),
        start_kyoku([TENPAI_HAND, NEUTRAL_1, NEUTRAL_2, NEUTRAL_3]),
        tsumo(0, "9s"),
        reach(0),
        dahai(0, "9s", tsumogiri=True),
        reach_accepted(0),
        tsumo(1, "9m"),
        dahai(1, "1s", tsumogiri=False),
        tsumo(2, seat_two_draw),
        dahai(2, seat_two_discard, tsumogiri=tsumogiri),
        RYUKYOKU,
        *END_EVENTS,
    ]


def _seat_two_row(log):
    observations = []
    materialize_game(
        log,
        game_id="g",
        target_seats={Seat(2): 313},
        game_mode=GAME_MODE,
        decision_observer=observations.append,
    )
    (row,) = [
        item
        for item in observations
        if item.legal_action_count >= 2
        and isinstance(item.teacher_action, DiscardAction)
    ]
    return row


def _analyze(log, policy_factory=None):
    analyzer = ChampionPlayAnalyzer(policy_factory=policy_factory)
    with mock.patch.object(champion_module, "SOURCE_GAME_MODE", GAME_MODE):
        analyzer.analyze_game("dev", game_id="g", seat=2, bot_id=313, events=log)
    return analyzer


def _axis(analyzer, axis):
    return analyzer.aggregate_document()["per_bot"]["dev"]["axes"][axis]


class _FixedPolicy:
    def __init__(self, selected):
        self._selected = selected

    def choose_action(self, decision):
        return self._selected


class GenbutsuSourceTests(unittest.TestCase):
    def test_only_a_tile_passed_after_riichi_is_passed_only(self):
        # seat 2の候補のうち現物は、リーチ後にseat 1が切った1sだけ。Wを切る。
        analyzer = _analyze(_log("1s", "W", tsumogiri=False))

        (value,) = _axis(analyzer, SOURCE_AXIS)
        self.assertTrue(value.startswith("available|not_genbutsu|"))
        self.assertTrue(value.endswith("|passed_only"))
        self.assertNotIn(
            PATH_AXIS, analyzer.aggregate_document()["per_bot"]["dev"]["axes"]
        )

    def test_a_tile_in_the_riichi_players_river_is_river(self):
        # 9sはリーチ者の河にある。1sは持っていないので河の現物だけがある。
        analyzer = _analyze(_log("9s", "W", tsumogiri=False))

        (value,) = _axis(analyzer, SOURCE_AXIS)
        self.assertTrue(value.startswith("available|not_genbutsu|"))
        self.assertTrue(value.endswith("|river"))

    def test_no_genbutsu_candidate_is_none(self):
        analyzer = _analyze(_log("9m", "W", tsumogiri=False))

        (value,) = _axis(analyzer, SOURCE_AXIS)
        self.assertTrue(value.startswith("none|not_genbutsu|"))
        self.assertTrue(value.endswith("|none"))

    def test_target_discards_count_only_available_non_genbutsu_non_tenpai(self):
        target = _analyze(_log("1s", "W", tsumogiri=False))
        genbutsu_selected = _analyze(_log("1s", "1s", tsumogiri=True))
        no_genbutsu = _analyze(_log("9m", "W", tsumogiri=False))

        self.assertEqual(target.target_discards("dev"), 1)
        self.assertEqual(genbutsu_selected.target_discards("dev"), 0)
        self.assertEqual(no_genbutsu.target_discards("dev"), 0)

    def test_groups_carry_the_recorded_issue_487_target_counts(self):
        self.assertEqual(
            {group.name: group.target_discards for group in CHAMPION_GROUPS},
            {"dev": 330, "baseline": 390},
        )


class ChampionViewTests(unittest.TestCase):
    def test_policy_without_analysis_is_reported_as_no_analysis(self):
        row = _seat_two_row(_log("1s", "W", tsumogiri=False))

        view = observe_champion_view(_FixedPolicy(row.teacher_action), row)

        self.assertEqual(view.branch, NO_ANALYSIS)
        self.assertEqual(view.riichi_players, "1")
        self.assertTrue(view.replay_matches)
        self.assertFalse(view.all_last_top)
        self.assertIn(view.shanten_stage, ("1", "2+"))

    def test_replayed_action_other_than_the_played_discard_is_a_mismatch(self):
        row = _seat_two_row(_log("1s", "W", tsumogiri=False))
        other = next(
            action
            for action in row.decision.legal_actions
            if isinstance(action, DiscardAction) and action.tile == tile_from_mjai("1s")
        )

        view = observe_champion_view(_FixedPolicy(other), row)

        self.assertFalse(view.replay_matches)
        self.assertIn("replay=mismatch", view.label())

    def test_champion_branch_comes_from_the_lisjong_analysis(self):
        champion = POLICY_CATALOG["placement-aware-speed-call"].factory

        # 河の現物（9s）が候補にあれば、Championは現物だけへ絞る。
        river = observe_champion_view(
            champion(), _seat_two_row(_log("9s", "W", tsumogiri=False))
        )
        # 候補の現物がリーチ後に通った1sだけなら、Championは現物として扱わない。
        passed = observe_champion_view(
            champion(), _seat_two_row(_log("1s", "W", tsumogiri=False))
        )

        self.assertEqual(river.branch, "FOLD_COMMON_GENBUTSU")
        self.assertNotEqual(passed.branch, "FOLD_COMMON_GENBUTSU")
        self.assertNotEqual(passed.branch, "PUSH")
        self.assertNotEqual(passed.branch, NO_ANALYSIS)

    def test_path_axis_joins_the_source_and_the_champion_view(self):
        log = _log("1s", "W", tsumogiri=False)
        played = _seat_two_row(log).teacher_action
        analyzer = _analyze(log, policy_factory=lambda: _FixedPolicy(played))

        (source,) = _axis(analyzer, SOURCE_AXIS)
        (path,) = _axis(analyzer, PATH_AXIS)
        self.assertTrue(path.startswith(f"{source}|branch={NO_ANALYSIS}|riichi=1|"))
        self.assertTrue(path.endswith("|top=no|replay=match"))
        cell = _axis(analyzer, PATH_AXIS)[path]
        self.assertEqual(cell["discards"]["total"], 1)


if __name__ == "__main__":
    unittest.main()
