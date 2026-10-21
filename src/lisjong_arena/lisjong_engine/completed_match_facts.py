"""`CompletedMatch.history`から局境界のengine factを射影し、整合を検証する共有module。

#370のL0.3 engine source（`focal_outcome_source.engine_source`）と、#513の
engine版AABB局単位record（`heuristic_candidate_engine_aabb`）が同じ射影と
検査を使うために、#370の実装から抽出した（#513）。Learning依存を持たない
execution / observationの部品であり、どちらのschemaも所有しない。

```text
各CompletedRound
  points_after   scores_after_settlement
  point_deltas   settlement.point_deltas
  points_before  points_after - point_deltas
                 （MatchState.settle_active_round()の
                  scores_after = scores_before.add(point_deltas)の厳密な逆）
```

- 精算は再計算しない。保存則・連続性・最終調整はengine factどうしの照合である
- seatはengine `Seat`の順（EAST / SOUTH / WEST / NORTH = 0..3）のindexで表す
- 不整合は`CompletedMatchFactError`でfail closedする。consumerは自分のerror型へ
  変換してよいが、検査を弱めない
"""

from collections.abc import Sequence
from dataclasses import dataclass

from lisjong_engine.match_state import CompletedMatch, CompletedRound, RoundPosition
from lisjong_engine.points import SeatPoints
from lisjong_engine.seat import Seat as EngineSeat
from lisjong_engine.settlement import RoundSettlement

from lisjong_arena.lisjong_engine.domain_conversion import seat_from_engine_seat

RIICHI_STICK_POINTS = 1000
"""`RuleSet.default().riichi_stick_points`。保存則の検査に使う。"""

MATCH_END_REASONS = frozenset(
    {"bankruptcy", "dealer_tenpai", "dealer_win", "final_round", "target_reached"}
)
"""受理する`MatchEndReason.value`。未知の値はfail closedする。"""

Points = tuple[int, int, int, int]


class CompletedMatchFactError(Exception):
    """engine factの射影・整合検査が成り立たない場合。"""


def seat_index(seat: EngineSeat) -> int:
    """engine `Seat`を0..3のindexへ変換する（`domain_conversion`の対応表を使う）。"""
    return int(seat_from_engine_seat(seat))


def seat_points(value: object, context: str) -> Points:
    """engine `SeatPoints`をseat index順の4要素tupleへ射影する。"""
    if not isinstance(value, SeatPoints):
        raise CompletedMatchFactError(f"{context} must be engine SeatPoints")
    points = [0, 0, 0, 0]
    for engine_seat in EngineSeat:
        points[seat_index(engine_seat)] = value[engine_seat]
    return tuple(points)


def require_kyoku_balance(
    *,
    points_before: Sequence[int],
    point_deltas: Sequence[int],
    points_after: Sequence[int],
    riichi_sticks_before: int,
    riichi_sticks_after: int,
    context: str,
) -> None:
    """局の逆算（before + delta == after）と、点数・供託の保存則を検証する。"""
    if any(
        before + delta != after
        for before, delta, after in zip(
            points_before, point_deltas, points_after, strict=True
        )
    ):
        raise CompletedMatchFactError(
            f"{context} points_before_kyoku + point_deltas != points_after_kyoku"
        )
    pot_delta = riichi_sticks_after - riichi_sticks_before
    if sum(point_deltas) + RIICHI_STICK_POINTS * pot_delta != 0:
        raise CompletedMatchFactError(
            f"{context} score/riichi-stick conservation is violated"
        )


def require_continuity(
    *,
    previous_points_after: Sequence[int],
    previous_riichi_sticks_after: int,
    points_before: Sequence[int],
    riichi_sticks_before: int,
    context: str,
) -> None:
    """次局の開始点数・供託が前局の精算後の値から続くことを検証する。"""
    if (
        tuple(points_before) != tuple(previous_points_after)
        or riichi_sticks_before != previous_riichi_sticks_after
    ):
        raise CompletedMatchFactError(
            f"{context} points / riichi sticks do not continue from the previous kyoku"
        )


def require_final_adjustment(
    *,
    final_points_after: Sequence[int],
    final_riichi_sticks_after: int,
    final_raw_scores: Sequence[int],
    awards: Sequence[tuple[int, int]],
) -> None:
    """半荘の最終素点と最終局精算後の差を、残供託の最終配分だけで説明する。

    ``awards``は``(seat index, amount)``の列。
    """
    awarded = [0, 0, 0, 0]
    for seat, amount in awards:
        if type(amount) is not int or amount <= 0:
            raise CompletedMatchFactError("final riichi stick award must be positive")
        awarded[int(seat)] += amount
    if sum(awarded) != RIICHI_STICK_POINTS * final_riichi_sticks_after:
        raise CompletedMatchFactError(
            "final riichi stick awards do not distribute exactly the remaining sticks"
        )
    if list(final_raw_scores) != [
        after + award for after, award in zip(final_points_after, awarded, strict=True)
    ]:
        raise CompletedMatchFactError(
            "hanchan final raw scores differ from the final kyoku boundary by more "
            "than the final riichi stick awards"
        )


@dataclass(frozen=True, slots=True)
class RoundBoundary:
    """1局のengine値と、その局境界の点数（seat index順）。"""

    completed: CompletedRound
    points_before: Points
    point_deltas: Points
    points_after: Points


@dataclass(frozen=True, slots=True)
class MatchBoundaries:
    """1半荘の全局境界と、半荘最終調整のaudit fact。"""

    rounds: tuple[RoundBoundary, ...]
    final_raw_scores: Points
    final_riichi_stick_awards: tuple[tuple[int, int], ...]
    end_reason: str


def match_boundaries(match: object) -> MatchBoundaries:
    """``CompletedMatch``を局境界へ射影し、局内・局間・最終調整の整合を検証する。

    検査: engine型、最終局だけ``next_position is None``、``position_before``が
    前局の``next_position``と一致、逆算と保存則、局間の連続、
    ``(prevailing_wind, hand_number, honba)``の一意性、最終調整、終局理由の列挙。
    """
    if not isinstance(match, CompletedMatch):
        raise CompletedMatchFactError("match must be a lisjong-engine CompletedMatch")
    if not match.history:
        raise CompletedMatchFactError("CompletedMatch has no completed kyoku")
    rounds: list[RoundBoundary] = []
    for index, completed in enumerate(match.history):
        context = f"completed kyoku {index}"
        if not isinstance(completed, CompletedRound):
            raise CompletedMatchFactError(f"{context} is not a CompletedRound")
        position = completed.position_before
        settlement = completed.settlement
        if not isinstance(position, RoundPosition) or not isinstance(
            settlement, RoundSettlement
        ):
            raise CompletedMatchFactError(f"{context} has invalid engine values")
        is_final = index == len(match.history) - 1
        if (completed.next_position is None) is not is_final:
            raise CompletedMatchFactError(
                f"{context} next_position does not match the terminal kyoku"
            )
        if index and match.history[index - 1].next_position != position:
            raise CompletedMatchFactError(
                f"{context} position_before is not the previous next_position"
            )
        after = seat_points(
            completed.scores_after_settlement, f"{context} scores_after"
        )
        deltas = seat_points(settlement.point_deltas, f"{context} point_deltas")
        boundary = RoundBoundary(
            completed=completed,
            points_before=tuple(a - d for a, d in zip(after, deltas, strict=True)),
            point_deltas=deltas,
            points_after=after,
        )
        require_kyoku_balance(
            points_before=boundary.points_before,
            point_deltas=deltas,
            points_after=after,
            riichi_sticks_before=position.riichi_sticks,
            riichi_sticks_after=settlement.riichi_sticks_after,
            context=context,
        )
        if rounds:
            require_continuity(
                previous_points_after=rounds[-1].points_after,
                previous_riichi_sticks_after=(
                    rounds[-1].completed.settlement.riichi_sticks_after
                ),
                points_before=boundary.points_before,
                riichi_sticks_before=position.riichi_sticks,
                context=context,
            )
        rounds.append(boundary)
    identities = [
        (
            item.completed.position_before.prevailing_wind,
            item.completed.position_before.hand_number,
            item.completed.position_before.honba,
        )
        for item in rounds
    ]
    if len(set(identities)) != len(identities):
        raise CompletedMatchFactError("completed kyoku round identity is not unique")
    awards = tuple(
        (seat_index(award.recipient), award.amount)
        for award in match.final_riichi_stick_awards
    )
    final_raw = seat_points(match.final_raw_scores, "final_raw_scores")
    final = rounds[-1]
    require_final_adjustment(
        final_points_after=final.points_after,
        final_riichi_sticks_after=final.completed.settlement.riichi_sticks_after,
        final_raw_scores=final_raw,
        awards=awards,
    )
    end_reason = match.end_reason.value
    if end_reason not in MATCH_END_REASONS:
        raise CompletedMatchFactError(f"unsupported match end reason {end_reason!r}")
    return MatchBoundaries(
        rounds=tuple(rounds),
        final_raw_scores=final_raw,
        final_riichi_stick_awards=awards,
        end_reason=end_reason,
    )


__all__ = [
    "MATCH_END_REASONS",
    "RIICHI_STICK_POINTS",
    "CompletedMatchFactError",
    "MatchBoundaries",
    "Points",
    "RoundBoundary",
    "match_boundaries",
    "require_continuity",
    "require_final_adjustment",
    "require_kyoku_balance",
    "seat_index",
    "seat_points",
]
