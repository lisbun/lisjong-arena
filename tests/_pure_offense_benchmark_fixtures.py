"""Issue #389 pure-offense benchmark testが共有するfixture。

単一game境界を差し替えるための、``SeatRoundStats``とMJAI event列が整合した
決定的な1局scenarioと、#389所有のtest用Seed Registry allocationを作る。
"""

from pathlib import Path
from unittest import mock

from _single_round_artifact_fixtures import provenance
from lisjong.policies import MinimalPolicy, ShantenPolicy

from lisjong_arena import seed_registry
from lisjong_arena.model import PolicySpec
from lisjong_arena.pure_offense_benchmark import execution
from lisjong_arena.pure_offense_benchmark.artifact import (
    resolve_seed_allocation,
    save_benchmark_arm,
)
from lisjong_arena.pure_offense_benchmark.protocol import OWNER_ISSUE, SEED_DOMAIN
from lisjong_arena.pure_offense_benchmark.record import derive_kyoku_offense_facts
from lisjong_arena.riichienv.local_game_runner import LocalGameResult
from lisjong_arena.riichienv.round_stats import SeatRoundStats

SEEDS = (1, 2)
REFERENCE = PolicySpec(identity="reference", factory=MinimalPolicy)
OTHER = PolicySpec(identity="other", factory=ShantenPolicy)
ARENA_REVISION = "2672cc24b90712e98d863727e3bb55785035c35b"


def _stats(
    end: int,
    *,
    won: bool = False,
    dealt_in: bool = False,
    exhaustive: bool = False,
    tenpai_turn: int | None = None,
) -> SeatRoundStats:
    return SeatRoundStats(
        start_score=25000,
        end_score=end,
        won=won,
        win_points=end - 25000 if won else None,
        dealt_in=dealt_in,
        deal_in_loss=25000 - end if dealt_in else None,
        exhaustive_draw=exhaustive,
        tenpai_at_exhaustive_draw=(tenpai_turn is not None) if exhaustive else None,
        first_tenpai_turn=tenpai_turn,
    )


def _dahai(actor: int, count: int) -> list[dict]:
    return [{"type": "dahai", "actor": actor, "pai": "1m"} for _ in range(count)]


def _scenario(kind: str, focal: int, *, turn: int | None = None):
    """(events, 4 seat stats) の整合した1局を作る。"""
    start = [{"type": "start_kyoku", "oya": 0}]
    if kind == "tsumo":
        events = start + _dahai(focal, turn)
        events.append({"type": "hora", "actor": focal, "target": focal, "tsumo": True})
        stats = [_stats(24000) for _ in range(4)]
        stats[focal] = _stats(28000, won=True, tenpai_turn=turn - 1)
    elif kind == "draw":
        events = start + _dahai(focal, 18)
        events.append({"type": "ryukyoku", "reason": "exhaustive_draw"})
        stats = [_stats(25000, exhaustive=True) for _ in range(4)]
        stats[focal] = _stats(25000, exhaustive=True, tenpai_turn=turn)
    elif kind == "deal_in":
        winner = (focal + 1) % 4
        events = start + _dahai(focal, 5)
        events.append(
            {"type": "hora", "actor": winner, "target": focal, "tsumo": False}
        )
        stats = [_stats(25000) for _ in range(4)]
        stats[focal] = _stats(23000, dealt_in=True)
        stats[winner] = _stats(27000, won=True, tenpai_turn=0)
    else:
        raise AssertionError(kind)
    return events, tuple(stats)


OTHER_ARM = {
    (1, 0): ("tsumo", 4),
    (1, 1): ("tsumo", 10),
    (1, 2): ("draw", 12),
    (1, 3): ("deal_in", None),
    (2, 0): ("tsumo", 4),
    (2, 1): ("draw", None),
    (2, 2): ("draw", 12),
    (2, 3): ("deal_in", None),
}


def game_function(scenarios):
    def run(policies, *, seed, max_steps):
        focal = next(
            seat
            for seat, policy in policies.items()
            if not type(policy).__name__.startswith("PassiveTsumogiri")
        )
        kind, turn = scenarios(seed, int(focal))
        events, stats = _scenario(kind, int(focal), turn=turn)
        result = LocalGameResult(
            seed=seed,
            game_mode="4p-red-single",
            scores=tuple(item.end_score for item in stats),
            ranks=(1, 2, 3, 4),
            steps=1,
            decisions=1,
            seat_round_stats=stats,
        )
        return result, derive_kyoku_offense_facts(events)

    return run


def allocation_ledger(*, owner_issue: str = OWNER_ISSUE, seeds=SEEDS):
    return seed_registry.reserve_allocation(
        seed_registry.new_ledger(),
        owner_issue=owner_issue,
        protocol="pure-offense-benchmark",
        seed_domain=SEED_DOMAIN,
        purpose="test",
        population="pure-offense-calibration",
        split="DEVELOPMENT",
        seeds=seeds,
        arena_revision=ARENA_REVISION,
        protocol_revision="v1",
        provenance_reference="test",
        allocation_timestamp="2026-09-26T00:00:00Z",
    )


def save_arm(directory: Path, name: str, focal: PolicySpec, scenarios, *, seeds=SEEDS):
    ledger, record = allocation_ledger(seeds=seeds)
    allocation = resolve_seed_allocation(ledger, record["allocation_identity"])
    with mock.patch.object(execution, "_run_benchmark_game", game_function(scenarios)):
        arm = execution.run_benchmark_arm(
            execution.benchmark_plan(focal, allocation.seeds), max_workers=1
        )
    with mock.patch(
        "lisjong_arena.single_round_artifact.collect_execution_provenance",
        return_value=provenance(),
    ):
        save_benchmark_arm(
            arm, directory / name, focal_reference=focal.identity, allocation=allocation
        )
    return directory / name
