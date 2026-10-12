"""Issue #487 ron-winner and three-axis breakdown tests (Champion-play side)."""

import unittest
from unittest import mock

from _riichilab_source_pilot_fixtures import (
    EAST_RON_HAND,
    END_EVENTS,
    NEUTRAL_1,
    NEUTRAL_2,
    NEUTRAL_3,
    TENPAI_HAND,
    dahai,
    hora,
    multi_ron_log,
    reach,
    reach_accepted,
    start_game,
    start_kyoku,
    tsumo,
)

from lisjong_arena.riichilab_r1_choice_loss_diagnostic import (
    champion_play as champion_module,
)
from lisjong_arena.riichilab_r1_choice_loss_diagnostic.champion_play import (
    CHAMPION_GROUPS,
    ChampionPlayAnalyzer,
    RonnedDiscard,
    SeatEventSummary,
    seat_event_summary,
)

GAME_MODE = "4p-red-single"
TRIPLE_AXIS = "genbutsu_availability|selected_genbutsu|minimum_post_discard_shanten"


def _deal_in_to_riichi_log(*, loss=8000):
    """seat 0のリーチへ、seat 1が3pを放銃するlog。"""
    win = hora(0, 1, "3p")
    win["deltas"] = [loss, -loss, 0, 0]
    return [
        start_game(),
        start_kyoku([TENPAI_HAND, NEUTRAL_1, NEUTRAL_2, NEUTRAL_3]),
        tsumo(0, "9s"),
        reach(0),
        dahai(0, "9s", tsumogiri=True),
        reach_accepted(0),
        tsumo(1, "9m"),
        dahai(1, "3p", tsumogiri=False),
        win,
        *END_EVENTS,
    ]


def _deal_in_to_other_log(*, loss=3900):
    """seat 0がリーチした後、seat 1の9sをリーチしていないseat 3がロンするlog。"""
    win = hora(3, 1, "9s")
    win["deltas"] = [0, -loss, 0, loss]
    return [
        start_game(),
        start_kyoku([TENPAI_HAND, NEUTRAL_1, NEUTRAL_2, EAST_RON_HAND]),
        tsumo(0, "N"),
        reach(0),
        dahai(0, "N", tsumogiri=True),
        reach_accepted(0),
        tsumo(1, "9s"),
        dahai(1, "9s", tsumogiri=True),
        win,
        *END_EVENTS,
    ]


def _analyze(log):
    analyzer = ChampionPlayAnalyzer()
    with mock.patch.object(champion_module, "SOURCE_GAME_MODE", GAME_MODE):
        analyzer.analyze_game("dev", game_id="g", seat=1, bot_id=313, events=log)
    return analyzer


def _all_cell(analyzer):
    return analyzer.aggregate_document()["per_bot"]["dev"]["axes"]["all"]["all"]


class RonWinnerEventTests(unittest.TestCase):
    def test_each_hora_records_its_winner_and_payment(self):
        log = multi_ron_log()
        for event, loss in zip(
            [item for item in log if item["type"] == "hora"], (2000, 3900)
        ):
            event["deltas"] = [-loss, 0, 0, 0]

        hit = seat_event_summary(log, game_id="g", seat=0).ronned[
            ("g", 0, "E", 1, 0, 1)
        ]

        self.assertEqual(hit.loss, 5900)
        self.assertEqual(hit.loss_by_winner, ((1, 2000), (3, 3900)))


class RonWinnerBreakdownTests(unittest.TestCase):
    def test_ron_by_the_riichi_player(self):
        cell = _all_cell(_analyze(_deal_in_to_riichi_log(loss=8000)))

        self.assertEqual(
            cell["ronned_discards_by_winner"],
            {"riichi_player_only": 1, "other_only": 0, "both": 0},
        )
        self.assertEqual(
            cell["deal_in_loss_by_payee"], {"riichi_player": 8000, "other": 0}
        )

    def test_ron_by_a_seat_other_than_the_riichi_player(self):
        analyzer = _analyze(_deal_in_to_other_log(loss=3900))
        cell = _all_cell(analyzer)

        self.assertEqual(cell["discards"]["total"], 1)
        self.assertEqual(
            cell["ronned_discards_by_winner"],
            {"riichi_player_only": 0, "other_only": 1, "both": 0},
        )
        self.assertEqual(
            cell["deal_in_loss_by_payee"], {"riichi_player": 0, "other": 3900}
        )

    def test_double_ron_including_the_riichi_player_splits_the_payment(self):
        log = _deal_in_to_riichi_log()
        key = ("g", 1, "E", 1, 0, 1)
        summary = SeatEventSummary(
            rounds=1,
            deal_in_rounds=1,
            ronned={
                key: RonnedDiscard(
                    loss=8000,
                    r1_by_events=True,
                    loss_by_winner=((0, 5000), (3, 3000)),
                )
            },
        )
        with mock.patch.object(
            champion_module, "seat_event_summary", return_value=summary
        ):
            cell = _all_cell(_analyze(log))

        self.assertEqual(
            cell["ronned_discards_by_winner"],
            {"riichi_player_only": 0, "other_only": 0, "both": 1},
        )
        self.assertEqual(
            cell["deal_in_loss_by_payee"], {"riichi_player": 5000, "other": 3000}
        )
        self.assertEqual(cell["deal_in_loss"]["total"], 8000)

    def test_three_axis_cell_matches_the_existing_axes(self):
        analyzer = _analyze(_deal_in_to_riichi_log(loss=8000))
        axes = analyzer.aggregate_document()["per_bot"]["dev"]["axes"]

        (triple,) = axes[TRIPLE_AXIS]
        (pair,) = axes["genbutsu_availability|selected_genbutsu"]
        (minimum,) = axes["minimum_post_discard_shanten"]
        self.assertEqual(triple, f"{pair}|{minimum}")
        self.assertEqual(pair, "none|not_genbutsu")
        cell = axes[TRIPLE_AXIS][triple]
        self.assertEqual(cell["discards"]["total"], 1)
        self.assertEqual(cell["deal_in_loss"]["total"], 8000)
        self.assertEqual(cell["deal_in_loss_by_payee"]["riichi_player"], 8000)
        self.assertEqual(cell["deal_in_loss_share_of_r1"], 1.0)

    def test_winner_and_payee_splits_sum_to_the_cell_totals(self):
        analyzer = ChampionPlayAnalyzer()
        with mock.patch.object(champion_module, "SOURCE_GAME_MODE", GAME_MODE):
            for game_id, log in (
                ("a", _deal_in_to_riichi_log(loss=8000)),
                ("b", _deal_in_to_other_log(loss=3900)),
            ):
                analyzer.analyze_game(
                    "dev", game_id=game_id, seat=1, bot_id=313, events=log
                )

        self.assertEqual(analyzer.r1_totals("dev"), (2, 2, 11900))
        axes = analyzer.aggregate_document()["seat_total"]["axes"]
        for values in axes.values():
            for cell in values.values():
                self.assertEqual(
                    sum(cell["ronned_discards_by_winner"].values()),
                    cell["ronned_discards"]["total"],
                )
                self.assertEqual(
                    sum(cell["deal_in_loss_by_payee"].values()),
                    cell["deal_in_loss"]["total"],
                )
            self.assertEqual(
                sum(cell["discards"]["total"] for cell in values.values()), 2
            )


class LockedR1TotalsTests(unittest.TestCase):
    def test_groups_carry_the_recorded_issue_483_totals(self):
        recorded = {
            group.name: (
                group.r1_discards,
                group.r1_ronned_discards,
                group.r1_deal_in_loss,
            )
            for group in CHAMPION_GROUPS
        }
        self.assertEqual(
            recorded,
            {"dev": (3724, 82, 496200), "baseline": (4154, 63, 373100)},
        )


if __name__ == "__main__":
    unittest.main()
