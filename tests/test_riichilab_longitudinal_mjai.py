from __future__ import annotations

import gzip
import json
import unittest

from _riichilab_longitudinal_fixtures import mjai_bytes, round_events

from lisjong_arena.riichilab_longitudinal import (
    LongitudinalAnalysisError,
    analyze_mjai,
)


class MjaiMetricTest(unittest.TestCase):
    def test_double_ron_is_one_deal_in_round_with_accumulated_loss(self) -> None:
        events = round_events(
            [
                {
                    "type": "hora",
                    "actor": 1,
                    "target": 0,
                    "deltas": [-8000, 8000, 0, 0],
                },
                {
                    "type": "hora",
                    "actor": 2,
                    "target": 0,
                    "deltas": [-12000, 0, 12000, 0],
                },
            ],
            actions=[{"type": "reach_accepted", "actor": 0}],
        )
        result = analyze_mjai(mjai_bytes(events), self_seat=0)
        self.assertEqual(result.rounds, 1)
        self.assertEqual(result.deal_in_rounds, 1)
        self.assertEqual(result.deal_in_loss, 20000)
        self.assertEqual(result.terminal_delta, -20000)
        self.assertEqual(result.riichi_rounds, 1)
        self.assertEqual(result.riichi_deal_ins, 1)

    def test_self_tsumo_is_distinct_from_ron(self) -> None:
        result = analyze_mjai(
            mjai_bytes(
                round_events(
                    [
                        {
                            "type": "hora",
                            "actor": 0,
                            "target": 0,
                            "deltas": [6000, -2000, -2000, -2000],
                        }
                    ],
                    oya=1,
                )
            ),
            self_seat=0,
        )
        self.assertEqual(result.wins, 1)
        self.assertEqual(result.tsumo_wins, 1)
        self.assertEqual(result.ron_wins, 0)

    def test_win_call_draw_tsumo_and_dealer_metrics(self) -> None:
        events = []
        events += round_events(
            [
                {
                    "type": "hora",
                    "actor": 0,
                    "target": 3,
                    "deltas": [8000, 0, 0, -8000],
                }
            ],
            oya=0,
            actions=[
                {"type": "pon", "actor": 0, "consumed": ["1m", "1m"]},
                {
                    "type": "daiminkan",
                    "actor": 0,
                    "consumed": ["3m", "3m", "3m"],
                },
                {"type": "kakan", "actor": 0, "pai": "1m"},
                {"type": "ankan", "actor": 0, "consumed": ["2m"] * 4},
            ],
        )
        events += round_events(
            [
                {
                    "type": "hora",
                    "actor": 2,
                    "target": 2,
                    "deltas": [-2000, -2000, 6000, -2000],
                }
            ],
            oya=1,
        )
        events += round_events(
            [{"type": "ryukyoku", "deltas": [1000, -1000, 0, 0]}],
            oya=2,
            actions=[{"type": "reach_accepted", "actor": 0}],
        )
        result = analyze_mjai(mjai_bytes(events), self_seat=0)
        self.assertEqual(result.rounds, 3)
        self.assertEqual(result.dealer_rounds, 1)
        self.assertEqual(result.wins, 1)
        self.assertEqual(result.ron_wins, 1)
        self.assertEqual(result.open_call_rounds, 1)
        self.assertEqual(result.open_call_count, 3)
        self.assertEqual(result.pon_count, 1)
        self.assertEqual(result.kan_count, 3)
        self.assertEqual(result.open_wins, 1)
        self.assertEqual(result.opponent_tsumo_rounds, 1)
        self.assertEqual(result.opponent_tsumo_loss, 2000)
        self.assertEqual(result.draw_rounds, 1)
        self.assertEqual(result.draw_delta, 1000)
        self.assertEqual(result.dealer_wins, 1)
        self.assertEqual(result.riichi_rounds, 1)
        self.assertEqual(result.terminal_delta, 7000)

    def test_riichi_and_open_state_reset_each_round(self) -> None:
        first = round_events(
            [{"type": "ryukyoku", "deltas": [0, 0, 0, 0]}],
            actions=[
                {"type": "reach_accepted", "actor": 0},
                {"type": "chi", "actor": 0, "consumed": ["1m", "2m"]},
            ],
        )
        second = round_events(
            [
                {
                    "type": "hora",
                    "actor": 1,
                    "target": 0,
                    "deltas": [-1000, 1000, 0, 0],
                }
            ]
        )
        result = analyze_mjai(mjai_bytes(first + second), self_seat=0)
        self.assertEqual(result.riichi_rounds, 1)
        self.assertEqual(result.open_call_rounds, 1)
        self.assertEqual(result.riichi_deal_ins, 0)
        self.assertEqual(result.open_deal_ins, 0)
        self.assertEqual(result.deal_in_rounds, 1)

    def test_malformed_lifecycle_fails_closed_through_shared_parser(self) -> None:
        events = [
            {"type": "start_game", "names": ["a", "b", "c", "d"]},
            {"type": "start_kyoku", "oya": 0},
            {"type": "end_game"},
        ]
        payload = gzip.compress(
            "".join(json.dumps(event) + "\n" for event in events).encode(), mtime=0
        )
        with self.assertRaisesRegex(LongitudinalAnalysisError, "strict validation"):
            analyze_mjai(payload, self_seat=0)

    def test_malformed_terminal_delta_fails_closed(self) -> None:
        payload = mjai_bytes(
            round_events(
                [
                    {
                        "type": "hora",
                        "actor": 1,
                        "target": 0,
                        "deltas": [-1000, "bad", 1000, 0],
                    }
                ]
            )
        )
        with self.assertRaisesRegex(LongitudinalAnalysisError, "four-item integer"):
            analyze_mjai(payload, self_seat=0)


if __name__ == "__main__":
    unittest.main()
