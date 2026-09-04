"""BC hybrid / Q hybrid serving smoke (Issue #140).

fresh seeds `277..280`（`4p-red-half`）でBC hybrid / Q hybridを実際に走らせ、
```text
deterministic repeat
illegal selection / resolve failure / policy validation failure / non-finite = 0
learned activation count / rate
scaffold fallback count / rate
support fallback count / rate
feature / forward / select latency (reuse HybridPolicy.samples)
hanchan runtime
```
を報告する。smokeをmodel tuningへは使わない。serving semanticsがvalidで
なければstrengthへ進まない。
"""

import time
from dataclasses import dataclass

from lisjong.policy_contract import Seat

from lisjong_arena.riichienv.local_game_runner import LocalGameResult, LocalGameRunner

from .errors import OfflineQError
from .protocol import GAME_MODE, require_smoke_seed
from .serving import HybridRuntime


class OfflineQSmokeError(OfflineQError):
    """serving smokeが不変条件を満たさなかった。"""


@dataclass(frozen=True, slots=True)
class SmokeGameMeasurement:
    """1 hanchanのsmoke実行結果。"""

    seed: int
    result: LocalGameResult
    activation_count: int
    scaffold_fallback_count: int
    support_fallback_count: int
    decision_count: int
    wall_clock_seconds: float
    cpu_seconds: float

    def to_document(self) -> dict[str, object]:
        return {
            "seed": self.seed,
            "scores": list(self.result.scores),
            "activation_count": self.activation_count,
            "scaffold_fallback_count": self.scaffold_fallback_count,
            "support_fallback_count": self.support_fallback_count,
            "decision_count": self.decision_count,
            "wall_clock_seconds": self.wall_clock_seconds,
            "cpu_seconds": self.cpu_seconds,
        }


def _run_once(runtime: HybridRuntime, seed: int) -> tuple[SmokeGameMeasurement, tuple]:
    policies = {seat: runtime.create_policy() for seat in Seat}
    if len({id(policy) for policy in policies.values()}) != len(policies):
        raise OfflineQSmokeError("each seat must use a distinct Policy instance")

    wall_start = time.perf_counter()
    cpu_start = time.process_time()
    runner = LocalGameRunner(policies, seed=seed, game_mode=GAME_MODE)
    result = runner.run()
    wall_clock = time.perf_counter() - wall_start
    cpu_seconds = time.process_time() - cpu_start

    samples = tuple(sample for policy in policies.values() for sample in policy.samples)
    measurement = SmokeGameMeasurement(
        seed=seed,
        result=result,
        activation_count=sum(policy.activation_count for policy in policies.values()),
        scaffold_fallback_count=sum(
            policy.scaffold_fallback_count for policy in policies.values()
        ),
        support_fallback_count=sum(
            policy.support_fallback_count for policy in policies.values()
        ),
        decision_count=len(samples),
        wall_clock_seconds=wall_clock,
        cpu_seconds=cpu_seconds,
    )
    return measurement, samples


def run_smoke_game(runtime: HybridRuntime, seed: int) -> SmokeGameMeasurement:
    """1 seedをdeterministic repeatで2回実行し、結果が一致することを確認する。"""
    require_smoke_seed(seed)
    first, _ = _run_once(runtime, seed)
    second, _ = _run_once(runtime, seed)
    if (
        first.result.scores != second.result.scores
        or first.result.ranks != second.result.ranks
        or first.result.decisions != second.result.decisions
        or first.activation_count != second.activation_count
        or first.scaffold_fallback_count != second.scaffold_fallback_count
        or first.support_fallback_count != second.support_fallback_count
    ):
        raise OfflineQSmokeError(
            f"seed {seed} is not deterministic across repeated runs"
        )
    return first


def run_smoke(runtime: HybridRuntime, seeds) -> tuple[SmokeGameMeasurement, ...]:
    """全smoke seedを実行し、hanchan単位のmeasurementを返す。"""
    return tuple(run_smoke_game(runtime, seed) for seed in seeds)


@dataclass(frozen=True, slots=True)
class SmokeSummary:
    """serving smoke全体のexhaustive summary。"""

    arm: str
    game_count: int
    total_decisions: int
    total_activations: int
    total_scaffold_fallbacks: int
    total_support_fallbacks: int
    total_wall_clock_seconds: float

    @property
    def activation_rate(self) -> float:
        return self.total_activations / self.total_decisions

    @property
    def scaffold_fallback_rate(self) -> float:
        return self.total_scaffold_fallbacks / self.total_decisions

    @property
    def support_fallback_rate(self) -> float:
        return self.total_support_fallbacks / self.total_decisions

    def to_document(self) -> dict[str, object]:
        return {
            "arm": self.arm,
            "game_count": self.game_count,
            "total_decisions": self.total_decisions,
            "total_activations": self.total_activations,
            "activation_rate": self.activation_rate,
            "total_scaffold_fallbacks": self.total_scaffold_fallbacks,
            "scaffold_fallback_rate": self.scaffold_fallback_rate,
            "total_support_fallbacks": self.total_support_fallbacks,
            "support_fallback_rate": self.support_fallback_rate,
            "total_wall_clock_seconds": self.total_wall_clock_seconds,
        }


def summarize_smoke(
    arm: str, measurements: tuple[SmokeGameMeasurement, ...]
) -> SmokeSummary:
    if not measurements:
        raise OfflineQSmokeError("smoke summary requires at least one game")
    total_decisions = sum(item.decision_count for item in measurements)
    if total_decisions == 0:
        raise OfflineQSmokeError("smoke measurements contain no decisions")
    return SmokeSummary(
        arm=arm,
        game_count=len(measurements),
        total_decisions=total_decisions,
        total_activations=sum(item.activation_count for item in measurements),
        total_scaffold_fallbacks=sum(
            item.scaffold_fallback_count for item in measurements
        ),
        total_support_fallbacks=sum(
            item.support_fallback_count for item in measurements
        ),
        total_wall_clock_seconds=sum(item.wall_clock_seconds for item in measurements),
    )


__all__ = [
    "OfflineQSmokeError",
    "SmokeGameMeasurement",
    "SmokeSummary",
    "run_smoke",
    "run_smoke_game",
    "summarize_smoke",
]
