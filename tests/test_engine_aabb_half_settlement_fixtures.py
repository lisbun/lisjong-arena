"""#472: engine版正式評価が前提にする局精算・終局・最終得点のfixture検証。

対象は `docs/heuristic-candidate-engine-aabb-half.md` の§6.3 U5（本場transfer）、
§7.1 C1〜C10、§7.2 F1〜F15。lisjong-engine `91af75e` の `RuleSet.default()` を
そのまま使う。

期待値はengine `docs/rules.md` と手計算から先に書いた値であり、実行結果に
合わせて作っていない。和了の手は七対子のみ（1m4m7m2p6p8p3sの対子、ドラなし）で、
ロンは2翻25符（子1600 / 親2400）、ツモは門前清自摸和を加えた3翻25符
（子800/1600、親1600オール）である。

局面はengine自身のtest（`tests/test_match_state.py`）と同じ方法で作る:
`MatchState._position` を書き換えて任意の局から開始し、開始済み局の
`RoundState` を終局済みfactへ直接進めてから `settle_active_round()` を呼ぶ。
private fieldへの依存はこのtestだけに閉じ、production側へposition注入APIを
追加しない。engine revisionを変える場合は、本fixtureも新revisionで再検証する。
"""

import unittest
from collections import Counter

from lisjong_engine.dora import DoraIndicators
from lisjong_engine.match_state import (
    CompletedMatch,
    CompletedRound,
    MatchEndReason,
    MatchPhase,
    MatchState,
    RoundPosition,
)
from lisjong_engine.round_phase import RoundPhase
from lisjong_engine.round_result import (
    AbortiveDrawReason,
    AbortiveDrawResult,
    ExhaustiveDrawResult,
    WinningPlayerResult,
    WinResult,
)
from lisjong_engine.rules import RuleSet
from lisjong_engine.seat import Seat
from lisjong_engine.settlement import TransferReason
from lisjong_engine.tile import STANDARD_TILES
from lisjong_engine.win_context import WinMethod, WinningContext, WinOrigin
from lisjong_engine.wind import Wind
from lisjong_engine.winning_score import evaluate_winning_scores

E, S, W, N = Seat.EAST, Seat.SOUTH, Seat.WEST, Seat.NORTH
_SEAT_ORDER = (E, S, W, N)
_RULES = RuleSet.default()
_FIXTURE_SEED = 472
_DEALER_BY_HAND = {1: E, 2: S, 3: W, 4: N}
_CHIITOITSU_TILE_TYPES = (0, 3, 6, 10, 14, 16, 20)

RON = TransferReason.RON
TSUMO = TransferReason.TSUMO
HONBA = TransferReason.HONBA
NOTEN = TransferReason.NOTEN_PENALTY


def _position(wind: Wind, hand: int, *, honba: int = 0, sticks: int = 0):
    return RoundPosition(
        prevailing_wind=wind,
        hand_number=hand,
        dealer_seat=_DEALER_BY_HAND[hand],
        honba=honba,
        riichi_sticks=sticks,
    )


def _scores(east: int, south: int, west: int, north: int) -> dict[Seat, int]:
    return {E: east, S: south, W: west, N: north}


def _seat_wind(seat: Seat, dealer: Seat) -> Wind:
    distance = (_SEAT_ORDER.index(seat) - _SEAT_ORDER.index(dealer)) % 4
    return tuple(Wind)[distance]


def _chiitoitsu_winner(
    seat: Seat, *, method: WinMethod, dealer: Seat, prevailing: Wind
) -> WinningPlayerResult:
    tiles = tuple(
        STANDARD_TILES[tile_type * 4 + copy]
        for tile_type in _CHIITOITSU_TILE_TYPES
        for copy in range(2)
    )
    context = WinningContext(
        concealed_tiles=tiles,
        winning_tile=tiles[-1],
        method=method,
        origin=WinOrigin.DISCARD if method is WinMethod.RON else WinOrigin.LIVE_WALL,
        seat_wind=_seat_wind(seat, dealer),
        prevailing_wind=prevailing,
    )
    return WinningPlayerResult(
        seat=seat,
        context=context,
        score_selection=evaluate_winning_scores(
            context, dora_indicators=DoraIndicators(), rules=_RULES
        ),
    )


def _win(position: RoundPosition, winners, *, source: Seat | None = None):
    method = WinMethod.TSUMO if source is None else WinMethod.RON
    players = tuple(
        _chiitoitsu_winner(
            seat,
            method=method,
            dealer=position.dealer_seat,
            prevailing=position.prevailing_wind,
        )
        for seat in winners
    )
    return WinResult(
        method=method,
        origin=players[0].context.origin,
        winning_tile=players[0].context.winning_tile,
        winners=players,
        dora_indicators=DoraIndicators(),
        source_seat=source,
    )


def _settle(position: RoundPosition, scores: dict[Seat, int], result):
    """`position` から1局だけ進め、`result` で終局させて精算する。"""
    match = MatchState(seed=_FIXTURE_SEED, rules=_RULES, starting_scores=scores)
    match._position = position
    round_state = match.start_round()
    round_state._phase = RoundPhase.FINISHED
    round_state._result = result
    completed = match.settle_active_round()
    return match, completed


def _transfers(completed: CompletedRound) -> Counter:
    return Counter(
        (item.payer, item.recipient, item.amount, item.reason)
        for item in completed.settlement.transfers
    )


def _deltas(completed: CompletedRound) -> dict[Seat, int]:
    return {seat: completed.settlement.point_deltas[seat] for seat in _SEAT_ORDER}


def _awards(awards) -> tuple[tuple[Seat, int], ...]:
    return tuple((award.recipient, award.amount) for award in awards)


def _final_table(match: CompletedMatch) -> dict[Seat, tuple[int, ...]]:
    return {
        player.seat: (
            player.rank,
            player.base_points,
            player.uma_points,
            player.oka_points,
            player.bankruptcy_points,
            player.final_points,
        )
        for player in match.final_score.players
    }


def _trunc_thousands(value: int) -> int:
    magnitude = abs(value) // 1_000
    return magnitude if value >= 0 else -magnitude


class _FixtureAssertions(unittest.TestCase):
    def assert_round_consistency(
        self,
        completed: CompletedRound,
        points_before: dict[Seat, int],
        position: RoundPosition,
    ) -> None:
        """§10.4 K4 / K5 / K8 相当: 局精算の内部整合。"""
        settlement = completed.settlement
        for seat in _SEAT_ORDER:
            self.assertEqual(
                points_before[seat] + settlement.point_deltas[seat],
                completed.scores_after_settlement[seat],
            )
        sticks_before = position.riichi_sticks
        self.assertEqual(
            settlement.point_deltas.total
            + _RULES.riichi_stick_points
            * (settlement.riichi_sticks_after - sticks_before),
            0,
        )
        for seat in _SEAT_ORDER:
            received = sum(
                item.amount for item in settlement.transfers if item.recipient is seat
            )
            paid = sum(
                item.amount for item in settlement.transfers if item.payer is seat
            )
            contributed = _RULES.riichi_stick_points * sum(
                1 for item in settlement.riichi_contributions if item.seat is seat
            )
            awarded = sum(
                award.amount
                for award in settlement.riichi_stick_awards
                if award.recipient is seat
            )
            self.assertEqual(
                settlement.point_deltas[seat],
                received - paid - contributed + awarded,
            )

    def assert_final_consistency(self, match: CompletedMatch) -> None:
        """§7.1 C1〜C10。lockしたRuleSet値を入力とする算術の照合だけを行う。"""
        players = {player.seat: player for player in match.final_score.players}
        last = match.history[-1]
        # C1
        self.assertEqual(sum(player.final_points for player in players.values()), 0)
        for player in players.values():
            # C2 / C10
            for value in (
                player.final_points,
                player.base_points,
                player.uma_points,
                player.oka_points,
                player.bankruptcy_points,
            ):
                self.assertIs(type(value), int)
            self.assertEqual(
                player.final_points,
                player.base_points
                + player.uma_points
                + player.oka_points
                + player.bankruptcy_points,
            )
        # C3
        awarded = {seat: 0 for seat in _SEAT_ORDER}
        for award in match.final_riichi_stick_awards:
            awarded[award.recipient] += award.amount
        for seat in _SEAT_ORDER:
            self.assertEqual(
                match.final_raw_scores[seat],
                last.scores_after_settlement[seat] + awarded[seat],
            )
        self.assertEqual(
            sum(awarded.values()),
            _RULES.riichi_stick_points * last.settlement.riichi_sticks_after,
        )
        # C4
        self.assertEqual(match.final_raw_scores.total, 4 * _RULES.starting_points)
        # C5
        for seat in _SEAT_ORDER:
            self.assertEqual(players[seat].score, match.final_raw_scores[seat])
        # C6
        ordered = sorted(
            _SEAT_ORDER,
            key=lambda seat: (-match.final_raw_scores[seat], _SEAT_ORDER.index(seat)),
        )
        self.assertEqual([players[seat].rank for seat in ordered], [1, 2, 3, 4])
        # C7
        oka_rank_points = (_RULES.return_points - _RULES.starting_points) * 4 // 1_000
        for player in players.values():
            self.assertEqual(player.uma_points, 10 * _RULES.uma[player.rank - 1])
            self.assertEqual(
                player.oka_points, 10 * oka_rank_points if player.rank == 1 else 0
            )
        # C8
        non_top_sum = 0
        for seat in ordered[1:]:
            expected = _trunc_thousands(players[seat].score - _RULES.return_points)
            self.assertEqual(players[seat].base_points, 10 * expected)
            non_top_sum += expected
        self.assertEqual(
            players[ordered[0]].base_points, 10 * (-oka_rank_points - non_top_sum)
        )
        # C9
        self.assertEqual(
            sum(player.bankruptcy_points for player in players.values()), 0
        )
        if match.end_reason is not MatchEndReason.BANKRUPTCY:
            for player in players.values():
                self.assertEqual(player.bankruptcy_points, 0)
        for player in players.values():
            if player.bankruptcy_points < 0:
                self.assertLess(
                    last.scores_after_settlement[player.seat],
                    _RULES.bankruptcy_threshold,
                )

    def run_terminal(
        self,
        position: RoundPosition,
        scores: dict[Seat, int],
        result,
        *,
        end_reason: MatchEndReason,
        final_raw: dict[Seat, int],
        final_awards: tuple[tuple[Seat, int], ...],
        table: dict[Seat, tuple[int, ...]],
    ) -> tuple[CompletedMatch, CompletedRound]:
        match_state, completed = _settle(position, scores, result)
        self.assertIs(match_state.phase, MatchPhase.FINISHED)
        match = match_state.completed_match
        self.assertIsNotNone(match)
        self.assertIsNone(completed.next_position)
        self.assertIs(match.end_reason, end_reason)
        self.assertEqual(_awards(match.final_riichi_stick_awards), final_awards)
        self.assertEqual(match.final_raw_scores.as_dict(), final_raw)
        self.assertEqual(_final_table(match), table)
        self.assert_round_consistency(completed, scores, position)
        self.assert_final_consistency(match)
        return match, completed

    def run_continuing(
        self,
        position: RoundPosition,
        scores: dict[Seat, int],
        result,
        *,
        next_position: RoundPosition,
    ) -> CompletedRound:
        match_state, completed = _settle(position, scores, result)
        self.assertIs(match_state.phase, MatchPhase.AWAITING_ROUND)
        self.assertIsNone(match_state.completed_match)
        self.assertEqual(completed.next_position, next_position)
        self.assert_round_consistency(completed, scores, position)
        return completed


class HonbaAndRiichiStickFixtureTest(_FixtureAssertions):
    """U5: 本場は手の点数と別の `HONBA` transferになる。供託の宛先と持ち越し。"""

    def test_ron_with_honba_and_carried_stick(self) -> None:
        position = _position(Wind.EAST, 1, honba=2, sticks=1)
        scores = _scores(25_000, 25_000, 25_000, 24_000)
        completed = self.run_continuing(
            position,
            scores,
            _win(position, (S,), source=W),
            next_position=_position(Wind.EAST, 2),
        )
        self.assertEqual(
            _transfers(completed),
            Counter({(W, S, 1_600, RON): 1, (W, S, 600, HONBA): 1}),
        )
        self.assertEqual(
            _awards(completed.settlement.riichi_stick_awards), ((S, 1_000),)
        )
        self.assertEqual(_deltas(completed), _scores(0, 3_200, -2_200, 0))
        self.assertEqual(completed.settlement.riichi_sticks_after, 0)

    def test_child_tsumo_with_honba(self) -> None:
        position = _position(Wind.EAST, 1, honba=1)
        completed = self.run_continuing(
            position,
            _scores(25_000, 25_000, 25_000, 25_000),
            _win(position, (S,)),
            next_position=_position(Wind.EAST, 2),
        )
        self.assertEqual(
            _transfers(completed),
            Counter(
                {
                    (E, S, 1_600, TSUMO): 1,
                    (E, S, 100, HONBA): 1,
                    (W, S, 800, TSUMO): 1,
                    (W, S, 100, HONBA): 1,
                    (N, S, 800, TSUMO): 1,
                    (N, S, 100, HONBA): 1,
                }
            ),
        )
        self.assertEqual(_deltas(completed), _scores(-1_700, 3_500, -900, -900))

    def test_dealer_tsumo_with_honba_continues(self) -> None:
        position = _position(Wind.EAST, 1, honba=3)
        completed = self.run_continuing(
            position,
            _scores(25_000, 25_000, 25_000, 25_000),
            _win(position, (E,)),
            next_position=_position(Wind.EAST, 1, honba=4),
        )
        self.assertEqual(
            _transfers(completed),
            Counter(
                {
                    (seat, E, amount, reason): 1
                    for seat in (S, W, N)
                    for amount, reason in ((1_600, TSUMO), (300, HONBA))
                }
            ),
        )
        self.assertEqual(_deltas(completed), _scores(5_700, -1_900, -1_900, -1_900))

    def test_exhaustive_draw_carries_sticks_and_adds_honba(self) -> None:
        position = _position(Wind.EAST, 2, sticks=2)
        completed = self.run_continuing(
            position,
            _scores(25_000, 24_000, 25_000, 24_000),
            ExhaustiveDrawResult(tenpai_seats=(S, W)),
            next_position=_position(Wind.EAST, 2, honba=1, sticks=2),
        )
        self.assertEqual(
            _transfers(completed),
            Counter(
                {
                    (E, S, 750, NOTEN): 1,
                    (E, W, 750, NOTEN): 1,
                    (N, S, 750, NOTEN): 1,
                    (N, W, 750, NOTEN): 1,
                }
            ),
        )
        self.assertEqual(completed.settlement.riichi_stick_awards, ())
        self.assertEqual(completed.settlement.riichi_sticks_after, 2)
        self.assertEqual(_deltas(completed), _scores(-1_500, 1_500, 1_500, -1_500))

    def test_double_ron_honba_and_sticks_go_to_nearest_winner(self) -> None:
        # 放銃者SOUTHの下家から WEST, NORTH, EAST の順。和了者はEASTとNORTHで、
        # 最も近いNORTHが本場と供託を受け取る（seat順のEASTではない）。
        position = _position(Wind.EAST, 1, honba=1, sticks=1)
        completed = self.run_continuing(
            position,
            _scores(25_000, 25_000, 25_000, 24_000),
            _win(position, (E, N), source=S),
            next_position=_position(Wind.EAST, 1, honba=2),
        )
        self.assertEqual(
            _transfers(completed),
            Counter(
                {
                    (S, E, 2_400, RON): 1,
                    (S, N, 1_600, RON): 1,
                    (S, N, 300, HONBA): 1,
                }
            ),
        )
        self.assertEqual(
            _awards(completed.settlement.riichi_stick_awards), ((N, 1_000),)
        )
        self.assertEqual(_deltas(completed), _scores(2_400, -4_300, 0, 2_900))


class FinalScoreFixtureTest(_FixtureAssertions):
    """F1〜F5、F14: 端数処理、残供託、同点順位。西4局の途中流局で終局させる。"""

    def _west_four(self, scores, *, sticks=0, final_raw, final_awards=(), table):
        position = _position(Wind.WEST, 4, sticks=sticks)
        return self.run_terminal(
            position,
            scores,
            AbortiveDrawResult(AbortiveDrawReason.FOUR_WINDS),
            end_reason=MatchEndReason.FINAL_ROUND,
            final_raw=final_raw,
            final_awards=final_awards,
            table=table,
        )

    def test_f1_thousand_point_multiples(self) -> None:
        scores = _scores(45_000, 25_000, 20_000, 10_000)
        self._west_four(
            scores,
            final_raw=scores,
            table={
                E: (1, 150, 300, 200, 0, 650),
                S: (2, -50, 100, 0, 0, 50),
                W: (3, -100, -100, 0, 0, -200),
                N: (4, -200, -300, 0, 0, -500),
            },
        )

    def test_f2_fractions_round_toward_zero_on_both_sides(self) -> None:
        scores = _scores(34_500, 31_700, 18_200, 15_600)
        self._west_four(
            scores,
            final_raw=scores,
            table={
                E: (1, 40, 300, 200, 0, 540),
                S: (2, 10, 100, 0, 0, 110),
                W: (3, -110, -100, 0, 0, -210),
                N: (4, -140, -300, 0, 0, -440),
            },
        )

    def test_f3_remainder_moves_top_away_from_raw_formula(self) -> None:
        scores = _scores(32_300, 28_700, 21_400, 17_600)
        match, _ = self._west_four(
            scores,
            final_raw=scores,
            table={
                E: (1, 10, 300, 200, 0, 510),
                S: (2, -10, 100, 0, 0, 90),
                W: (3, -80, -100, 0, 0, -180),
                N: (4, -120, -300, 0, 0, -420),
            },
        )
        # 旧v1の素点式 (32300 - 30000) / 1000 = 2.3 pt（内部23）に対し、engineは
        # 非首位の丸め残差 -1.3 pt を首位へ寄せて 1.0 pt（内部10）になる。
        old_v1_top_base = (32_300 - 30_000) // 100
        self.assertEqual(old_v1_top_base, 23)
        self.assertEqual(
            match.final_score.for_seat(E).base_points - old_v1_top_base, -13
        )

    def test_f4_leftover_sticks_go_to_single_top_once(self) -> None:
        _, completed = self._west_four(
            _scores(30_000, 26_000, 23_000, 19_000),
            sticks=2,
            final_raw=_scores(32_000, 26_000, 23_000, 19_000),
            final_awards=((E, 2_000),),
            table={
                E: (1, 20, 300, 200, 0, 520),
                S: (2, -40, 100, 0, 0, 60),
                W: (3, -70, -100, 0, 0, -170),
                N: (4, -110, -300, 0, 0, -410),
            },
        )
        # 局精算では配らず、終局時の配分だけで1回計上する。
        self.assertEqual(completed.settlement.riichi_stick_awards, ())
        self.assertEqual(completed.settlement.riichi_sticks_after, 2)
        self.assertEqual(_deltas(completed), _scores(0, 0, 0, 0))

    def test_f5_leftover_sticks_with_tied_top_go_to_earlier_seat(self) -> None:
        self._west_four(
            _scores(22_000, 28_000, 21_000, 28_000),
            sticks=1,
            final_raw=_scores(22_000, 29_000, 21_000, 28_000),
            final_awards=((S, 1_000),),
            table={
                S: (1, -10, 300, 200, 0, 490),
                N: (2, -20, 100, 0, 0, 80),
                E: (3, -80, -100, 0, 0, -180),
                W: (4, -90, -300, 0, 0, -390),
            },
        )

    def test_f14_two_way_ties(self) -> None:
        scores = _scores(20_000, 30_000, 20_000, 30_000)
        self._west_four(
            scores,
            final_raw=scores,
            table={
                S: (1, 0, 300, 200, 0, 500),
                N: (2, 0, 100, 0, 0, 100),
                E: (3, -100, -100, 0, 0, -200),
                W: (4, -100, -300, 0, 0, -400),
            },
        )

    def test_f14_three_way_tie(self) -> None:
        scores = _scores(22_000, 26_000, 26_000, 26_000)
        self._west_four(
            scores,
            final_raw=scores,
            table={
                S: (1, -40, 300, 200, 0, 460),
                W: (2, -40, 100, 0, 0, 60),
                N: (3, -40, -100, 0, 0, -140),
                E: (4, -80, -300, 0, 0, -380),
            },
        )

    def test_f14_four_way_tie(self) -> None:
        scores = _scores(25_000, 25_000, 25_000, 25_000)
        self._west_four(
            scores,
            final_raw=scores,
            table={
                E: (1, -50, 300, 200, 0, 450),
                S: (2, -50, 100, 0, 0, 50),
                W: (3, -50, -100, 0, 0, -150),
                N: (4, -50, -300, 0, 0, -350),
            },
        )


class BankruptcyFixtureTest(_FixtureAssertions):
    """F6〜F9とN1: 0点は続行、0点未満で終局。飛び賞罰の受取人と配分。"""

    def test_f6_exactly_zero_points_continues(self) -> None:
        position = _position(Wind.EAST, 1)
        completed = self.run_continuing(
            position,
            _scores(34_000, 32_000, 32_400, 1_600),
            _win(position, (S,), source=N),
            next_position=_position(Wind.EAST, 2),
        )
        self.assertEqual(completed.scores_after_settlement[N], 0)

    def test_f6_f7_ron_below_zero_rewards_the_winner(self) -> None:
        position = _position(Wind.EAST, 1)
        self.run_terminal(
            position,
            _scores(34_000, 32_000, 32_500, 1_500),
            _win(position, (S,), source=N),
            end_reason=MatchEndReason.BANKRUPTCY,
            final_raw=_scores(34_000, 33_600, 32_500, -100),
            final_awards=(),
            table={
                E: (1, 50, 300, 200, 0, 550),
                S: (2, 30, 100, 0, 100, 230),
                W: (3, 20, -100, 0, 0, -80),
                N: (4, -300, -300, 0, -100, -700),
            },
        )

    def test_f8_tsumo_with_one_bankrupt_payer(self) -> None:
        position = _position(Wind.EAST, 1)
        self.run_terminal(
            position,
            _scores(33_000, 33_000, 700, 33_300),
            _win(position, (S,)),
            end_reason=MatchEndReason.BANKRUPTCY,
            final_raw=_scores(31_400, 36_200, -100, 32_500),
            final_awards=(),
            table={
                S: (1, 70, 300, 200, 100, 670),
                N: (2, 20, 100, 0, 0, 120),
                E: (3, 10, -100, 0, 0, -90),
                W: (4, -300, -300, 0, -100, -700),
            },
        )

    def test_f8_tsumo_with_two_bankrupt_payers(self) -> None:
        # 飛んだ2席はそれぞれ -10 pt、受取人（どちらも和了者EAST）は +10 pt × 2。
        position = _position(Wind.EAST, 1)
        self.run_terminal(
            position,
            _scores(48_000, 1_500, 1_000, 49_500),
            _win(position, (E,)),
            end_reason=MatchEndReason.BANKRUPTCY,
            final_raw=_scores(52_800, -100, -600, 47_900),
            final_awards=(),
            table={
                E: (1, 230, 300, 200, 200, 930),
                N: (2, 170, 100, 0, 0, 270),
                S: (3, -300, -100, 0, -100, -500),
                W: (4, -300, -300, 0, -100, -700),
            },
        )

    def test_f9_double_ron_splits_bonus_between_winners(self) -> None:
        position = _position(Wind.EAST, 1)
        self.run_terminal(
            position,
            _scores(33_000, 32_500, 32_500, 2_000),
            _win(position, (S, W), source=N),
            end_reason=MatchEndReason.BANKRUPTCY,
            final_raw=_scores(33_000, 34_100, 34_100, -1_200),
            final_awards=(),
            table={
                S: (1, 40, 300, 200, 50, 590),
                W: (2, 40, 100, 0, 50, 190),
                E: (3, 30, -100, 0, 0, -70),
                N: (4, -310, -300, 0, -100, -710),
            },
        )

    def test_noten_penalty_bankruptcy_splits_remainder_from_bankrupt_downstream(
        self,
    ) -> None:
        # WESTが3席へノーテン罰符を払って飛ぶ。10 pt / 3 = 3 余り1 は、飛んだ席の
        # 下家から数えて最初の受取人NORTHへ（seat順のEASTではない）。
        position = _position(Wind.EAST, 1)
        self.run_terminal(
            position,
            _scores(32_500, 32_500, 2_500, 32_500),
            ExhaustiveDrawResult(tenpai_seats=(E, S, N)),
            end_reason=MatchEndReason.BANKRUPTCY,
            final_raw=_scores(33_500, 33_500, -500, 33_500),
            final_awards=(),
            table={
                E: (1, 40, 300, 200, 30, 570),
                S: (2, 30, 100, 0, 30, 160),
                N: (3, 30, -100, 0, 40, -30),
                W: (4, -300, -300, 0, -100, -700),
            },
        )


class MatchEndFixtureTest(_FixtureAssertions):
    """F10〜F13、F15: 南4局以降の終局条件と西4打ち切り。"""

    def test_f10_south_four_dealer_win_top_and_target(self) -> None:
        position = _position(Wind.SOUTH, 4)
        self.run_terminal(
            position,
            _scores(22_000, 22_000, 22_000, 34_000),
            _win(position, (N,)),
            end_reason=MatchEndReason.DEALER_WIN,
            final_raw=_scores(20_400, 20_400, 20_400, 38_800),
            final_awards=(),
            table={
                N: (1, 70, 300, 200, 0, 570),
                E: (2, -90, 100, 0, 0, 10),
                S: (3, -90, -100, 0, 0, -190),
                W: (4, -90, -300, 0, 0, -390),
            },
        )

    def test_f11_south_four_dealer_tenpai_top_and_target(self) -> None:
        position = _position(Wind.SOUTH, 4)
        self.run_terminal(
            position,
            _scores(23_000, 23_000, 23_000, 31_000),
            ExhaustiveDrawResult(tenpai_seats=(N,)),
            end_reason=MatchEndReason.DEALER_TENPAI,
            final_raw=_scores(22_000, 22_000, 22_000, 34_000),
            final_awards=(),
            table={
                N: (1, 40, 300, 200, 0, 540),
                E: (2, -80, 100, 0, 0, 20),
                S: (3, -80, -100, 0, 0, -180),
                W: (4, -80, -300, 0, 0, -380),
            },
        )

    def test_south_four_dealer_tenpai_below_target_continues(self) -> None:
        position = _position(Wind.SOUTH, 4)
        self.run_continuing(
            position,
            _scores(25_000, 25_000, 25_000, 25_000),
            ExhaustiveDrawResult(tenpai_seats=(N,)),
            next_position=_position(Wind.SOUTH, 4, honba=1),
        )

    def test_f12_south_four_abortive_draw_with_dealer_top_continues(self) -> None:
        position = _position(Wind.SOUTH, 4)
        self.run_continuing(
            position,
            _scores(23_000, 23_000, 23_000, 31_000),
            AbortiveDrawResult(AbortiveDrawReason.FOUR_WINDS),
            next_position=_position(Wind.SOUTH, 4, honba=1),
        )

    def test_f12_south_four_dealer_win_tied_with_earlier_seat_continues(self) -> None:
        position = _position(Wind.SOUTH, 4)
        completed = self.run_continuing(
            position,
            _scores(34_800, 18_400, 18_400, 28_400),
            _win(position, (N,)),
            next_position=_position(Wind.SOUTH, 4, honba=1),
        )
        self.assertEqual(
            completed.scores_after_settlement.as_dict(),
            _scores(33_200, 16_800, 16_800, 33_200),
        )

    def test_f13_west_four_dealer_continuation_still_ends(self) -> None:
        position = _position(Wind.WEST, 4)
        self.run_terminal(
            position,
            _scores(40_000, 20_000, 20_000, 20_000),
            _win(position, (N,)),
            end_reason=MatchEndReason.FINAL_ROUND,
            final_raw=_scores(38_400, 18_400, 18_400, 24_800),
            final_awards=(),
            table={
                E: (1, 70, 300, 200, 0, 570),
                N: (2, -50, 100, 0, 0, 50),
                S: (3, -110, -100, 0, 0, -210),
                W: (4, -110, -300, 0, 0, -410),
            },
        )

    def test_f13_west_four_final_round_precedes_dealer_win(self) -> None:
        position = _position(Wind.WEST, 4)
        self.run_terminal(
            position,
            _scores(22_000, 22_000, 22_000, 34_000),
            _win(position, (N,)),
            end_reason=MatchEndReason.FINAL_ROUND,
            final_raw=_scores(20_400, 20_400, 20_400, 38_800),
            final_awards=(),
            table={
                N: (1, 70, 300, 200, 0, 570),
                E: (2, -90, 100, 0, 0, 10),
                S: (3, -90, -100, 0, 0, -190),
                W: (4, -90, -300, 0, 0, -390),
            },
        )

    def test_f15_south_four_dealer_flow_below_target_enters_west(self) -> None:
        position = _position(Wind.SOUTH, 4)
        self.run_continuing(
            position,
            _scores(27_000, 25_000, 24_000, 24_000),
            _win(position, (E,), source=S),
            next_position=_position(Wind.WEST, 1),
        )

    def test_south_four_dealer_flow_target_reached(self) -> None:
        position = _position(Wind.SOUTH, 4)
        self.run_terminal(
            position,
            _scores(28_500, 25_000, 23_500, 23_000),
            _win(position, (E,), source=S),
            end_reason=MatchEndReason.TARGET_REACHED,
            final_raw=_scores(30_100, 23_400, 23_500, 23_000),
            final_awards=(),
            table={
                E: (1, -10, 300, 200, 0, 490),
                W: (2, -60, 100, 0, 0, 40),
                S: (3, -60, -100, 0, 0, -160),
                N: (4, -70, -300, 0, 0, -370),
            },
        )

    def test_west_one_dealer_flow_target_reached(self) -> None:
        position = _position(Wind.WEST, 1)
        self.run_terminal(
            position,
            _scores(25_000, 29_000, 24_000, 22_000),
            _win(position, (S,), source=W),
            end_reason=MatchEndReason.TARGET_REACHED,
            final_raw=_scores(25_000, 30_600, 22_400, 22_000),
            final_awards=(),
            table={
                S: (1, 0, 300, 200, 0, 500),
                E: (2, -50, 100, 0, 0, 50),
                W: (3, -70, -100, 0, 0, -170),
                N: (4, -80, -300, 0, 0, -380),
            },
        )

    def test_west_one_dealer_flow_below_target_continues(self) -> None:
        position = _position(Wind.WEST, 1)
        self.run_continuing(
            position,
            _scores(25_000, 25_000, 25_000, 25_000),
            _win(position, (S,), source=W),
            next_position=_position(Wind.WEST, 2),
        )


if __name__ == "__main__":
    unittest.main()
