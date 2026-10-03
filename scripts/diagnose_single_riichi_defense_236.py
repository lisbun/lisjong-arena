"""Development diagnostic for lisbun/lisjong#236 (R1800 H1, stage 1).

Plays Champion self-play hanchan on lisjong-engine and breaks down the
Champion's discard decisions made while exactly one opponent is in riichi by
the defense branch the Champion's own analysis reports.  The objective side
(who dealt in, how many points moved) comes from the engine's
``CompletedRound`` settlement; the branch comes from the Policy's
``TargetedHonorReleaseAnalysis``.  The two are joined only in this script's
aggregate output: no analysis value is written into an objective game record.

Development-only: the default seeds are the RETIRED engine-domain population of
lisjong-arena#385 (931000..931999), which can never be a scientific population
again.  The output is descriptive, not a strength claim.

Usage::

    LISJONG_SHANTEN_BACKEND=rust python scripts/diagnose_single_riichi_defense_236.py \
        --games 100 --workers 4 --output result.json
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from concurrent.futures import ProcessPoolExecutor

RETIRED_SEED_FIRST = 931000
RETIRED_SEED_LAST = 931999

# A seat's discard is classified by the Champion's own branch, refined by the
# shanten facts the branch already implies (PUSH under riichi = tenpai kept).
BRANCHES = (
    "PUSH_TENPAI",
    "FOLD_COMMON_GENBUTSU",
    "MECHANISM_DEFENSE_FILTERED",
    "FALLBACK_ALL_LEGAL_1_SHANTEN",
    "NO_ANALYSIS",
)


class _RecordingChampion:
    """Delegate to the Champion and keep (decision, PolicyDecision) pairs."""

    def __init__(self, inner, sink: list) -> None:
        self._inner = inner
        self._sink = sink

    def choose_action(self, decision):
        policy_decision = self._inner.choose_action_with_analysis(decision)
        self._sink.append((decision, policy_decision))
        return policy_decision.action


def _round_key_from_input(policy_input):
    round_state = policy_input.round
    return (round_state.round_wind.name, round_state.hand_number, round_state.honba)


def _round_key_from_position(position):
    return (position.prevailing_wind.name, position.hand_number, position.honba)


def _branch_name(analysis) -> str:
    from lisjong.policies.targeted_honor_release_terminal_progression import (
        TargetedHonorReleaseAnalysis,
        TargetedHonorReleaseBranch,
    )

    if not isinstance(analysis, TargetedHonorReleaseAnalysis):
        return "NO_ANALYSIS"
    branch = analysis.branch
    if branch is TargetedHonorReleaseBranch.PUSH:
        return "PUSH_TENPAI"
    if branch is TargetedHonorReleaseBranch.OTHER_CURRENT_FALLBACK:
        return "FALLBACK_ALL_LEGAL_1_SHANTEN"
    return branch.name


def _play(seed: int) -> dict:
    from lisjong.policies.placement_aware_speed_call import (
        PlacementAwareSpeedCallPolicy,
    )
    from lisjong.policy_contract import RiichiState
    from lisjong.policy_contract.action import DiscardAction
    from lisjong_engine.seat import Seat as EngineSeat
    from lisjong_engine.win_context import WinMethod, WinOrigin

    from lisjong_arena.lisjong_engine.domain_conversion import seat_from_engine_seat
    from lisjong_arena.lisjong_engine.hanchan import run_policy_hanchan

    sinks = {seat: [] for seat in EngineSeat}
    policies = {
        seat: _RecordingChampion(PlacementAwareSpeedCallPolicy(), sinks[seat])
        for seat in EngineSeat
    }
    match = run_policy_hanchan(policies, seed=seed)

    rounds = {}
    for completed in match.history:
        key = _round_key_from_position(completed.position_before)
        if key in rounds:
            raise RuntimeError(f"seed {seed}: duplicate round key {key}")
        rounds[key] = completed

    rows = []
    for engine_seat, records in sinks.items():
        seat = seat_from_engine_seat(engine_seat)
        seat_index = int(seat)
        last_discard_index_by_round: dict[tuple, int] = {}
        seat_rows = []
        for decision, policy_decision in records:
            if not isinstance(policy_decision.action, DiscardAction):
                continue
            key = _round_key_from_input(decision.input)
            if key not in rounds:
                raise RuntimeError(f"seed {seed}: decision round {key} not completed")
            last_discard_index_by_round[key] = len(seat_rows)
            players = decision.input.players
            own_riichi = players[seat_index].riichi is not RiichiState.NONE
            riichi_opponents = tuple(
                index
                for index, player in enumerate(players)
                if index != seat_index and player.riichi is not RiichiState.NONE
            )
            legal_discards = sum(
                isinstance(action, DiscardAction) for action in decision.legal_actions
            )
            seat_rows.append(
                {
                    "round": key,
                    "own_riichi": own_riichi,
                    "riichi_opponents": riichi_opponents,
                    "legal_discards": legal_discards,
                    "branch": _branch_name(policy_decision.analysis),
                    "dealt_in": False,
                    "deal_in_to_riichi": False,
                    "deal_in_points": 0,
                }
            )
        for key, index in last_discard_index_by_round.items():
            result = rounds[key].result
            if (
                getattr(result, "method", None) is WinMethod.RON
                and result.origin is WinOrigin.DISCARD
                and result.source_seat is engine_seat
            ):
                row = seat_rows[index]
                deltas = rounds[key].settlement.point_deltas
                row["dealt_in"] = True
                row["deal_in_points"] = -getattr(deltas, engine_seat.value)
                winners = {
                    int(seat_from_engine_seat(winner.seat)) for winner in result.winners
                }
                row["deal_in_to_riichi"] = bool(winners & set(row["riichi_opponents"]))
        rows.extend(seat_rows)
    return {"seed": seed, "rounds": len(rounds), "rows": rows}


def _aggregate(games: list[dict]) -> dict:
    branch_stats = defaultdict(Counter)
    for game in games:
        for row in game["rows"]:
            if row["own_riichi"] or len(row["riichi_opponents"]) != 1:
                continue
            if row["legal_discards"] < 2:
                continue
            stats = branch_stats[row["branch"]]
            stats["decisions"] += 1
            if row["dealt_in"]:
                stats["deal_ins"] += 1
                stats["deal_in_points"] += row["deal_in_points"]
                if row["deal_in_to_riichi"]:
                    stats["deal_ins_to_riichi"] += 1
                    stats["deal_in_points_to_riichi"] += row["deal_in_points"]
    total_decisions = sum(s["decisions"] for s in branch_stats.values())
    total_points = sum(s["deal_in_points"] for s in branch_stats.values())
    table = {}
    for name in BRANCHES:
        stats = branch_stats.get(name, Counter())
        decisions = stats["decisions"]
        table[name] = {
            "decisions": stats["decisions"],
            "decision_share": (
                stats["decisions"] / total_decisions if total_decisions else None
            ),
            "deal_ins": stats["deal_ins"],
            "deal_in_rate_per_decision": (
                stats["deal_ins"] / decisions if decisions else None
            ),
            "deal_in_points": stats["deal_in_points"],
            "deal_in_points_share": (
                stats["deal_in_points"] / total_points if total_points else None
            ),
            "mean_points_per_deal_in": (
                stats["deal_in_points"] / stats["deal_ins"]
                if stats["deal_ins"]
                else None
            ),
            "deal_ins_to_riichi": stats["deal_ins_to_riichi"],
            "deal_in_points_to_riichi": stats["deal_in_points_to_riichi"],
        }
    return {
        "scope": (
            "Champion discard decisions with exactly one opponent in riichi, "
            "self not in riichi, >= 2 legal discards"
        ),
        "games": len(games),
        "rounds": sum(game["rounds"] for game in games),
        "decisions": total_decisions,
        "deal_in_points_total": total_points,
        "by_branch": table,
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--games", type=int, default=100)
    parser.add_argument("--first-seed", type=int, default=RETIRED_SEED_FIRST)
    parser.add_argument("--workers", type=int, default=1)
    parser.add_argument("--output", required=True)
    arguments = parser.parse_args(argv)
    seeds = list(range(arguments.first_seed, arguments.first_seed + arguments.games))
    if seeds[0] < RETIRED_SEED_FIRST or seeds[-1] > RETIRED_SEED_LAST:
        parser.error("seeds must stay inside the RETIRED #385 range 931000..931999")
    if arguments.workers == 1:
        games = [_play(seed) for seed in seeds]
    else:
        with ProcessPoolExecutor(arguments.workers) as executor:
            games = list(executor.map(_play, seeds))
    summary = _aggregate(games)
    summary["seeds"] = [seeds[0], seeds[-1]]
    with open(arguments.output, "w", encoding="utf-8") as handle:
        json.dump(summary, handle, ensure_ascii=False, indent=2)
    json.dump(summary, sys.stdout, ensure_ascii=False, indent=2)
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
