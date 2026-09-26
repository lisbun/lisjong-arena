"""Issue #389 benchmark-owned offense recordのMJAI event導出test。

実RiichiEnvを起動せず、``env.mjai_log``と同じ形のevent列から直接導出する。
turn 0 / tsumo / ron / 立直 / 流局種別の境界と、想定外event列のfail closedを
固定する。
"""

import json
import unittest

from _round_stats_fixtures import neutral_seat_round_stats
from lisjong.policy_contract import Seat

from lisjong_arena.game_trace import GameTraceEvent
from lisjong_arena.model import SingleRoundGameResult
from lisjong_arena.pure_offense_benchmark.record import (
    TERMINATION_ABORTIVE_DRAW,
    TERMINATION_EXHAUSTIVE_DRAW,
    TERMINATION_WIN,
    KyokuOffenseRecord,
    OffenseFactsCollector,
    OffenseRecordError,
    SeatOffenseFacts,
    derive_kyoku_offense_facts,
    validate_record_against_game_result,
)
from lisjong_arena.riichienv.round_stats import SeatRoundStats


def _start(oya: int = 0) -> list[dict]:
    return [
        {"type": "start_game"},
        {
            "type": "start_kyoku",
            "bakaze": "E",
            "kyoku": 1,
            "honba": 0,
            "kyotaku": 0,
            "oya": oya,
            "scores": [25000] * 4,
            "dora_marker": "1p",
        },
    ]


def _discards(*actors: int) -> list[dict]:
    return [{"type": "dahai", "actor": actor, "pai": "1m"} for actor in actors]


def _end() -> list[dict]:
    return [{"type": "end_kyoku"}, {"type": "end_game"}]


def _hora(actor: int, target: int, *, tsumo: bool) -> dict:
    return {
        "type": "hora",
        "actor": actor,
        "target": target,
        "tsumo": tsumo,
        "deltas": [0, 0, 0, 0],
    }


def _ryukyoku(reason: str) -> dict:
    return {"type": "ryukyoku", "reason": reason, "deltas": [0, 0, 0, 0]}


class DeriveKyokuOffenseFactsTest(unittest.TestCase):
    def test_tsumo_win_turn_is_current_own_discard_count(self) -> None:
        events = _start() + _discards(0, 1, 2, 3, 0, 1, 2, 3, 0)
        events += [_hora(1, 1, tsumo=True)] + _end()
        facts = derive_kyoku_offense_facts(events)
        self.assertEqual(facts.termination, TERMINATION_WIN)
        self.assertIsNone(facts.draw_reason)
        winner = facts.seats[1]
        self.assertTrue(winner.won)
        self.assertTrue(winner.win_tsumo)
        self.assertEqual(winner.win_turn, 2)
        self.assertEqual(winner.discard_count, 2)
        self.assertFalse(any(seat.dealt_in for seat in facts.seats))

    def test_ron_win_turn_and_deal_in_target(self) -> None:
        events = _start() + _discards(0, 1, 2, 3, 0, 1, 2)
        events += [_hora(0, 2, tsumo=False)] + _end()
        facts = derive_kyoku_offense_facts(events)
        self.assertEqual(facts.seats[0].win_turn, 2)
        self.assertFalse(facts.seats[0].win_tsumo)
        self.assertTrue(facts.seats[2].dealt_in)
        self.assertFalse(facts.seats[1].dealt_in)

    def test_win_before_any_own_discard_is_turn_zero(self) -> None:
        tenhou = derive_kyoku_offense_facts(
            _start(oya=0) + [_hora(0, 0, tsumo=True)] + _end()
        )
        self.assertEqual(tenhou.seats[0].win_turn, 0)
        self.assertEqual(tenhou.seats[0].discard_count, 0)

        ron_before_first_discard = derive_kyoku_offense_facts(
            _start(oya=0) + _discards(0) + [_hora(1, 0, tsumo=False)] + _end()
        )
        self.assertEqual(ron_before_first_discard.seats[1].win_turn, 0)
        self.assertTrue(ron_before_first_discard.seats[0].dealt_in)

    def test_double_ron_records_both_winners(self) -> None:
        events = _start() + _discards(0, 1, 2)
        events += [_hora(0, 2, tsumo=False), _hora(1, 2, tsumo=False)] + _end()
        facts = derive_kyoku_offense_facts(events)
        self.assertTrue(facts.seats[0].won and facts.seats[1].won)
        self.assertTrue(facts.seats[2].dealt_in)

    def test_riichi_turn_is_declaration_discard_and_accepted(self) -> None:
        events = _start() + _discards(0, 1, 2, 3)
        events += [{"type": "reach", "actor": 0}] + _discards(0)
        events += [{"type": "reach_accepted", "actor": 0}] + _discards(1, 2, 3)
        events += [_ryukyoku("exhaustive_draw")] + _end()
        facts = derive_kyoku_offense_facts(events)
        self.assertEqual(facts.seats[0].riichi_turn, 2)
        self.assertTrue(facts.seats[0].riichi_accepted)
        self.assertIsNone(facts.seats[1].riichi_turn)

    def test_ron_on_declaration_tile_is_not_accepted(self) -> None:
        events = _start() + _discards(0, 1, 2, 3)
        events += [{"type": "reach", "actor": 0}] + _discards(0)
        events += [_hora(1, 0, tsumo=False)] + _end()
        facts = derive_kyoku_offense_facts(events)
        self.assertEqual(facts.seats[0].riichi_turn, 2)
        self.assertFalse(facts.seats[0].riichi_accepted)
        self.assertTrue(facts.seats[0].dealt_in)

    def test_exhaustive_and_abortive_draws_are_distinguished(self) -> None:
        exhaustive = derive_kyoku_offense_facts(
            _start() + _discards(0, 1) + [_ryukyoku("exhaustive_draw")] + _end()
        )
        self.assertEqual(exhaustive.termination, TERMINATION_EXHAUSTIVE_DRAW)
        self.assertEqual(exhaustive.draw_reason, "exhaustive_draw")

        abortive = derive_kyoku_offense_facts(
            _start() + [_ryukyoku("kyushu_kyuhai")] + _end()
        )
        self.assertEqual(abortive.termination, TERMINATION_ABORTIVE_DRAW)
        self.assertEqual(abortive.draw_reason, "kyushu_kyuhai")

    def test_dealer_seat_is_taken_from_start_kyoku(self) -> None:
        facts = derive_kyoku_offense_facts(
            _start(oya=2) + [_ryukyoku("exhaustive_draw")] + _end()
        )
        self.assertEqual(facts.dealer_seat, Seat(2))

    def test_invalid_event_streams_fail_closed(self) -> None:
        invalid = {
            "no start": _discards(0) + [_ryukyoku("exhaustive_draw")],
            "two kyoku": _start() + [_ryukyoku("exhaustive_draw")] + _start()[1:],
            "no termination": _start() + _discards(0, 1) + _end(),
            "discard after win": _start() + [_hora(0, 0, tsumo=True)] + _discards(1),
            "hora after draw": _start()
            + [_ryukyoku("exhaustive_draw"), _hora(0, 0, tsumo=True)],
            "tsumo flag mismatch": _start() + [_hora(0, 1, tsumo=True)],
            "ron on self": _start() + [_hora(0, 0, tsumo=False)],
            "double win": _start()
            + _discards(1)
            + [_hora(0, 1, tsumo=False), _hora(0, 1, tsumo=False)],
            "riichi twice": _start()
            + [{"type": "reach", "actor": 0}]
            + _discards(0)
            + [{"type": "reach", "actor": 0}],
            "accepted without discard": _start()
            + [{"type": "reach", "actor": 0}, {"type": "reach_accepted", "actor": 0}],
            "pending riichi at end": _start()
            + [{"type": "reach", "actor": 0}, _ryukyoku("exhaustive_draw")],
            "bad actor": _start() + [{"type": "dahai", "actor": 4, "pai": "1m"}],
            "empty reason": _start() + [_ryukyoku("")],
            "non-bool tsumo": _start()
            + [{"type": "hora", "actor": 0, "target": 0, "tsumo": 1}],
        }
        for name, events in invalid.items():
            with self.subTest(name), self.assertRaises(OffenseRecordError):
                derive_kyoku_offense_facts(events)


class OffenseFactsCollectorTest(unittest.TestCase):
    def test_collects_detached_trace_events(self) -> None:
        events = _start() + _discards(0) + [_ryukyoku("exhaustive_draw")] + _end()
        collector = OffenseFactsCollector()
        collector.on_start(seed=1, game_mode="4p-red-single")
        for sequence, event in enumerate(events):
            collector.on_event(
                GameTraceEvent(sequence=sequence, event=json.dumps(event))
            )
        collector.on_complete()
        self.assertEqual(collector.facts(), derive_kyoku_offense_facts(events))

    def test_lifecycle_violations_fail_closed(self) -> None:
        collector = OffenseFactsCollector()
        with self.assertRaises(OffenseRecordError):
            collector.facts()
        with self.assertRaises(OffenseRecordError):
            collector.on_event(GameTraceEvent(sequence=0, event='{"type":"x"}'))
        collector.on_start(seed=1, game_mode="4p-red-single")
        with self.assertRaises(OffenseRecordError):
            collector.on_start(seed=1, game_mode="4p-red-single")


def _stats(**overrides: object) -> SeatRoundStats:
    base = neutral_seat_round_stats(start_score=25000, end_score=25000)
    values = {
        "start_score": base.start_score,
        "end_score": base.end_score,
        "won": base.won,
        "win_points": base.win_points,
        "dealt_in": base.dealt_in,
        "deal_in_loss": base.deal_in_loss,
        "exhaustive_draw": base.exhaustive_draw,
        "tenpai_at_exhaustive_draw": base.tenpai_at_exhaustive_draw,
        "first_tenpai_turn": base.first_tenpai_turn,
    }
    values.update(overrides)
    return SeatRoundStats(**values)


class ValidateAgainstGameResultTest(unittest.TestCase):
    def _game(self, stats: tuple[SeatRoundStats, ...]) -> SingleRoundGameResult:
        return SingleRoundGameResult(
            seed=7,
            rotation=1,
            game_mode="4p-red-single",
            candidate_seat=Seat(1),
            scores=tuple(item.end_score for item in stats),
            seat_round_stats=stats,
        )

    def _record(self, events: list[dict]) -> KyokuOffenseRecord:
        return KyokuOffenseRecord(
            seed=7,
            rotation=1,
            focal_seat=Seat(1),
            facts=derive_kyoku_offense_facts(events),
        )

    def _win_stats(self, *, tenpai_turn: int | None) -> tuple[SeatRoundStats, ...]:
        return (
            _stats(end_score=24000, dealt_in=True, deal_in_loss=1000),
            _stats(
                end_score=26000,
                won=True,
                win_points=1000,
                first_tenpai_turn=tenpai_turn,
            ),
            _stats(),
            _stats(),
        )

    def test_consistent_record_is_accepted(self) -> None:
        record = self._record(
            _start() + _discards(0, 1, 2, 3, 0) + [_hora(1, 0, tsumo=False)] + _end()
        )
        validate_record_against_game_result(
            record, self._game(self._win_stats(tenpai_turn=1))
        )

    def test_disagreements_fail_closed(self) -> None:
        ron = _start() + _discards(0, 1, 2, 3, 0) + [_hora(1, 0, tsumo=False)] + _end()
        cases = {
            "win before first tenpai": (ron, self._win_stats(tenpai_turn=None)),
            "tenpai after win turn": (ron, self._win_stats(tenpai_turn=2)),
            "won differs": (
                _start() + [_ryukyoku("exhaustive_draw")] + _end(),
                self._win_stats(tenpai_turn=0),
            ),
            "exhaustive differs": (
                _start() + [_ryukyoku("exhaustive_draw")] + _end(),
                (_stats(),) * 4,
            ),
        }
        for name, (events, stats) in cases.items():
            with self.subTest(name), self.assertRaises(OffenseRecordError):
                validate_record_against_game_result(
                    self._record(events), self._game(stats)
                )

    def test_mismatched_identity_fails_closed(self) -> None:
        record = KyokuOffenseRecord(
            seed=8,
            rotation=1,
            focal_seat=Seat(1),
            facts=derive_kyoku_offense_facts(
                _start() + [_ryukyoku("kyushu_kyuhai")] + _end()
            ),
        )
        with self.assertRaises(OffenseRecordError):
            validate_record_against_game_result(record, self._game((_stats(),) * 4))


class SeatOffenseFactsTest(unittest.TestCase):
    def test_invalid_combinations_are_rejected(self) -> None:
        valid = {
            "discard_count": 3,
            "won": False,
            "win_turn": None,
            "win_tsumo": None,
            "dealt_in": False,
            "riichi_turn": None,
            "riichi_accepted": False,
        }
        SeatOffenseFacts(**valid)
        for name, change in {
            "win turn without win": {"win_turn": 1},
            "win turn after discards": {"won": True, "win_turn": 4, "win_tsumo": True},
            "win and deal in": {
                "won": True,
                "win_turn": 1,
                "win_tsumo": False,
                "dealt_in": True,
            },
            "riichi turn zero": {"riichi_turn": 0},
            "accepted without riichi": {"riichi_accepted": True},
        }.items():
            with self.subTest(name), self.assertRaises((TypeError, ValueError)):
                SeatOffenseFacts(**{**valid, **change})


if __name__ == "__main__":
    unittest.main()
