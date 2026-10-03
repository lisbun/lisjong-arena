"""Development diagnostic for lisbun/lisjong#236 (R1800 H1, stage 1).

Plays Champion self-play hanchan on lisjong-engine and breaks down the
Champion's discard decisions made while exactly one opponent is in riichi by
the defense branch the Champion's own analysis reports.  The objective side
(who dealt in, who was paid how much, how the round ended) comes from the
engine's ``CompletedRound`` result and settlement; the branch comes from the
Policy's ``TargetedHonorReleaseAnalysis``.  The two are joined only in this
script's aggregate output: no analysis value is written into an objective game
record.

Two aggregates are produced:

- per decision: decisions, deal-ins and the points paid by the dealing-in
  seat, split into payments to the riichi player and to others (a double ron
  pays both);
- per seat-round ("episode": a seat-round with at least one in-scope decision):
  how the round ended for that seat and the seat's round point delta.  An
  episode is counted once under every branch that occurred in it.

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

# The Champion's own branch, refined by the facts it implies under riichi
# (PUSH = tenpai kept; the parent fallback = 1-shanten with no common genbutsu
# and no mechanism filter).  TOP_FOLD_PREFILTERED takes precedence: the
# Champion's all-last top fold already narrowed the candidates to the minimum
# classical danger before the parent branch was computed, so that decision is
# not an undefended fallback.
BRANCHES = (
    "PUSH_TENPAI",
    "FOLD_COMMON_GENBUTSU",
    "MECHANISM_DEFENSE_FILTERED",
    "TOP_FOLD_PREFILTERED",
    "FALLBACK_ALL_LEGAL_1_SHANTEN",
    "NO_ANALYSIS",
)

OUTCOMES = (
    "WIN",
    "DEAL_IN",
    "TSUMO_BY_OTHER",
    "OTHER_RON",
    "DRAW_TENPAI",
    "DRAW_NOTEN",
    "ABORTIVE_DRAW",
)

_DEAL_IN_REASONS = ("RON", "PAO_RON", "HONBA")


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


def _top_fold_prefiltered(policy_input, discard_actions) -> bool:
    """Whether the Champion's all-last top fold (``_top_fold_actions``) applies.

    Mirrors that filter's activation guards with the Champion's own helpers;
    activation is not inferred from a candidate-set size change (a danger tie
    can leave the set unchanged).
    """
    from lisjong.policies.genbutsu_defense_finite_horizon_hand_value_aware import (
        _decide_push_fold,
        _PushFoldDecision,
    )
    from lisjong.policies.genbutsu_defense_two_step_ukeire import (
        _common_genbutsu_tile_types,
        _opponent_riichi_players,
    )
    from lisjong.policies.placement_aware_speed_call import (
        GameSituationMode,
        situation_mode,
    )

    if situation_mode(policy_input) is not GameSituationMode.ALL_LAST_TOP_SPEED:
        return False
    riichi_players = _opponent_riichi_players(policy_input)
    if not riichi_players:
        return False
    if _decide_push_fold(policy_input, discard_actions) is _PushFoldDecision.PUSH:
        return False
    common_genbutsu = _common_genbutsu_tile_types(riichi_players)
    return not any(
        action.tile.tile_type in common_genbutsu for action in discard_actions
    )


def _branch_name(analysis, *, top_fold: bool) -> str:
    from lisjong.policies.targeted_honor_release_terminal_progression import (
        TargetedHonorReleaseAnalysis,
        TargetedHonorReleaseBranch,
    )

    if top_fold:
        return "TOP_FOLD_PREFILTERED"
    if not isinstance(analysis, TargetedHonorReleaseAnalysis):
        return "NO_ANALYSIS"
    branch = analysis.branch
    if branch is TargetedHonorReleaseBranch.PUSH:
        return "PUSH_TENPAI"
    if branch is TargetedHonorReleaseBranch.OTHER_CURRENT_FALLBACK:
        return "FALLBACK_ALL_LEGAL_1_SHANTEN"
    return branch.name


def _round_outcome(completed, engine_seat) -> str:
    from lisjong_engine.match_state import AbortiveDrawResult, ExhaustiveDrawResult
    from lisjong_engine.win_context import WinMethod

    result = completed.result
    if isinstance(result, ExhaustiveDrawResult):
        return "DRAW_TENPAI" if engine_seat in result.tenpai_seats else "DRAW_NOTEN"
    if isinstance(result, AbortiveDrawResult):
        return "ABORTIVE_DRAW"
    if any(winner.seat is engine_seat for winner in result.winners):
        return "WIN"
    if result.method is WinMethod.TSUMO:
        return "TSUMO_BY_OTHER"
    if result.source_seat is engine_seat:
        return "DEAL_IN"
    return "OTHER_RON"


def _deal_in_payments(completed, engine_seat) -> dict[str, int]:
    """Points the seat paid to each ron winner, from the settlement transfers."""
    paid: dict[str, int] = defaultdict(int)
    for transfer in completed.settlement.transfers:
        if transfer.payer is engine_seat and transfer.reason.name in _DEAL_IN_REASONS:
            paid[transfer.recipient.name] += transfer.amount
    return dict(paid)


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

    engine_seat_by_index = {int(seat_from_engine_seat(s)): s for s in EngineSeat}
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
    episodes = []
    for engine_seat, records in sinks.items():
        seat_index = int(seat_from_engine_seat(engine_seat))
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
            riichi_opponents = tuple(
                index
                for index, player in enumerate(players)
                if index != seat_index and player.riichi is not RiichiState.NONE
            )
            discard_actions = tuple(
                action
                for action in decision.legal_actions
                if isinstance(action, DiscardAction)
            )
            top_fold = _top_fold_prefiltered(decision.input, discard_actions)
            seat_rows.append(
                {
                    "round": key,
                    "own_riichi": players[seat_index].riichi is not RiichiState.NONE,
                    "riichi_opponents": riichi_opponents,
                    "legal_discards": len(discard_actions),
                    "branch": _branch_name(policy_decision.analysis, top_fold=top_fold),
                    "dealt_in": False,
                    "deal_in_points": 0,
                    "deal_in_points_to_riichi": 0,
                }
            )
        for key, index in last_discard_index_by_round.items():
            completed = rounds[key]
            result = completed.result
            if (
                getattr(result, "method", None) is WinMethod.RON
                and result.origin is WinOrigin.DISCARD
                and result.source_seat is engine_seat
            ):
                row = seat_rows[index]
                riichi_names = {
                    engine_seat_by_index[i].name for i in row["riichi_opponents"]
                }
                paid = _deal_in_payments(completed, engine_seat)
                row["dealt_in"] = True
                row["deal_in_points"] = sum(paid.values())
                row["deal_in_points_to_riichi"] = sum(
                    amount for name, amount in paid.items() if name in riichi_names
                )
        rows.extend(seat_rows)

        branches_by_round: dict[tuple, set] = defaultdict(set)
        for row in seat_rows:
            if _in_scope(row):
                branches_by_round[row["round"]].add(row["branch"])
        for key, branches in branches_by_round.items():
            completed = rounds[key]
            episodes.append(
                {
                    "branches": sorted(branches),
                    "outcome": _round_outcome(completed, engine_seat),
                    "round_delta": getattr(
                        completed.settlement.point_deltas, engine_seat.value
                    ),
                }
            )
    return {"seed": seed, "rounds": len(rounds), "rows": rows, "episodes": episodes}


def _in_scope(row) -> bool:
    return (
        not row["own_riichi"]
        and len(row["riichi_opponents"]) == 1
        and row["legal_discards"] >= 2
    )


def _share(part, whole):
    return part / whole if whole else None


def _aggregate(games: list[dict]) -> dict:
    branch_stats = defaultdict(Counter)
    for game in games:
        for row in game["rows"]:
            if not _in_scope(row):
                continue
            stats = branch_stats[row["branch"]]
            stats["decisions"] += 1
            if row["dealt_in"]:
                stats["deal_ins"] += 1
                stats["deal_in_points"] += row["deal_in_points"]
                stats["deal_in_points_to_riichi"] += row["deal_in_points_to_riichi"]
                if row["deal_in_points_to_riichi"]:
                    stats["deal_ins_to_riichi"] += 1
    total_decisions = sum(s["decisions"] for s in branch_stats.values())
    total_points = sum(s["deal_in_points"] for s in branch_stats.values())
    decision_table = {}
    for name in BRANCHES:
        stats = branch_stats.get(name, Counter())
        decision_table[name] = {
            "decisions": stats["decisions"],
            "decision_share": _share(stats["decisions"], total_decisions),
            "deal_ins": stats["deal_ins"],
            "deal_in_rate_per_decision": _share(stats["deal_ins"], stats["decisions"]),
            "deal_in_points": stats["deal_in_points"],
            "deal_in_points_share": _share(stats["deal_in_points"], total_points),
            "mean_points_per_deal_in": _share(
                stats["deal_in_points"], stats["deal_ins"]
            ),
            "deal_ins_to_riichi": stats["deal_ins_to_riichi"],
            "deal_in_points_to_riichi": stats["deal_in_points_to_riichi"],
        }

    episode_stats = defaultdict(Counter)
    all_episodes = Counter()
    for game in games:
        for episode in game["episodes"]:
            for target in [all_episodes] + [
                episode_stats[b] for b in episode["branches"]
            ]:
                target["episodes"] += 1
                target[episode["outcome"]] += 1
                target["round_delta_total"] += episode["round_delta"]

    def _episode_row(stats):
        episodes = stats["episodes"]
        row = {"episodes": episodes}
        for outcome in OUTCOMES:
            row[outcome] = stats[outcome]
        row["round_delta_total"] = stats["round_delta_total"]
        row["mean_round_delta"] = _share(stats["round_delta_total"], episodes)
        return row

    return {
        "scope": (
            "Champion discard decisions with exactly one opponent in riichi, "
            "self not in riichi, >= 2 legal discards"
        ),
        "games": len(games),
        "rounds": sum(game["rounds"] for game in games),
        "decisions": total_decisions,
        "deal_in_points_total": total_points,
        "by_branch": decision_table,
        "episode_scope": (
            "seat-rounds with at least one in-scope decision; an episode is "
            "counted once under every branch that occurred in it"
        ),
        "episodes_all": _episode_row(all_episodes),
        "episodes_by_branch": {
            name: _episode_row(episode_stats.get(name, Counter())) for name in BRANCHES
        },
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
