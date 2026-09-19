"""Issue #250 Overall AABB half-game用のsynthetic fixture。

実RiichiEnvを起動せず、raw seat rowsとartifact bundleだけを合成する。
CIでactual 400-hanchan formal evaluationは実行しない。
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

from lisjong.policy_contract import Seat

import lisjong_arena.artifact as artifact_module
import lisjong_arena.overall_champion_aabb.lock as lock_module
from lisjong_arena.artifact import (
    ARTIFACT_SCHEMA_VERSION,
    COMPARISON_PROTOCOL,
    ArtifactPlan,
    ComparisonArtifact,
    ExecutionProvenance,
    save_comparison_artifact,
)
from lisjong_arena.comparison import aggregate_policy_metrics
from lisjong_arena.model import ComparisonPlan, ComparisonResult, PolicySpec, SeatResult
from lisjong_arena.overall_champion_aabb.lock import build_lock_document
from lisjong_arena.overall_champion_aabb.protocol import (
    GAME_MODE,
    HEURISTIC_FAMILY,
    HEURISTIC_SLOT,
    LEARNING_FAMILY,
    MAX_STEPS,
    ROTATION_COUNT,
    ROTATION_PLAN,
    SEAT_COUNT,
    SEED_BLOCK_COUNT,
    ParticipantBinding,
)
from lisjong_arena.single_round_artifact import SingleRoundExecutionProvenance

HEURISTIC_IDENTITY = "heuristic-champion-fixture-v1"
LEARNING_IDENTITY = "learning-champion-fixture-v1"

ARENA_REVISION = "a" * 40
LISJONG_REVISION = "b" * 40
LISJONG_ENGINE_REVISION = "c" * 40
HEURISTIC_REVISION = "d" * 40
LEARNING_REVISION = "e" * 40
CHECKPOINT_DIGEST = "f" * 64

SCORE_BY_RANK = {1: 40_000, 2: 30_000, 3: 20_000, 4: 10_000}

HEURISTIC_ADVANTAGE = "heuristic"
LEARNING_ADVANTAGE = "learning"
TIE = "tie"

_RANKS_BY_PROFILE = {
    # (heuristic seat ranks, learning seat ranks) within one hanchan.
    HEURISTIC_ADVANTAGE: ((1, 2), (3, 4)),
    LEARNING_ADVANTAGE: ((3, 4), (1, 2)),
    TIE: ((1, 4), (2, 3)),
}


class StubPolicy:
    """Policy契約に形だけ適合するstub。unit testは1手も進行しない。"""

    def choose_action(self, decision: object) -> object:
        raise AssertionError("Overall fixtures must not execute policies")


def stub_heuristic_policy() -> StubPolicy:
    return StubPolicy()


def stub_learning_policy() -> StubPolicy:
    return StubPolicy()


def other_learning_policy() -> StubPolicy:
    return StubPolicy()


def heuristic_binding(**overrides: object) -> ParticipantBinding:
    values: dict[str, object] = {
        "family": HEURISTIC_FAMILY,
        "policy_identity": HEURISTIC_IDENTITY,
        "factory_binding": ("_overall_champion_aabb_fixtures:stub_heuristic_policy"),
        "implementation_revision": HEURISTIC_REVISION,
    }
    values.update(overrides)
    return ParticipantBinding(**values)  # type: ignore[arg-type]


def learning_binding(**overrides: object) -> ParticipantBinding:
    values: dict[str, object] = {
        "family": LEARNING_FAMILY,
        "policy_identity": LEARNING_IDENTITY,
        "factory_binding": "_overall_champion_aabb_fixtures:stub_learning_policy",
        "implementation_revision": LEARNING_REVISION,
        "checkpoint_identity": "learning-champion-checkpoint-v1",
        "checkpoint_digest": CHECKPOINT_DIGEST,
    }
    values.update(overrides)
    return ParticipantBinding(**values)  # type: ignore[arg-type]


def heuristic_spec() -> PolicySpec:
    return PolicySpec(identity=HEURISTIC_IDENTITY, factory=stub_heuristic_policy)


def learning_spec() -> PolicySpec:
    return PolicySpec(identity=LEARNING_IDENTITY, factory=stub_learning_policy)


def seeds(count: int = SEED_BLOCK_COUNT, *, start: int = 90_000) -> tuple[int, ...]:
    return tuple(range(start, start + count))


def lock_provenance() -> SingleRoundExecutionProvenance:
    """install metadataへ依存しないlock provenance。"""
    return SingleRoundExecutionProvenance(
        execution_environment="riichienv",
        lisjong_arena_version="0.1.0",
        lisjong_arena_revision=ARENA_REVISION,
        lisjong_version="0.1.0",
        lisjong_revision=LISJONG_REVISION,
        lisjong_engine_version="0.1.0",
        lisjong_engine_revision=LISJONG_ENGINE_REVISION,
        riichienv_version="0.4.10",
        python_version="3.14.6",
    )


def comparison_provenance(**overrides: str) -> ExecutionProvenance:
    """lock provenanceと共通fieldが一致するgeneric comparison provenance。"""
    values: dict[str, str] = {
        "execution_environment": "riichienv",
        "lisjong_arena_version": "0.1.0",
        "lisjong_version": "0.1.0",
        "lisjong_revision": LISJONG_REVISION,
        "riichienv_version": "0.4.10",
        "python_version": "3.14.6",
    }
    values.update(overrides)
    return ExecutionProvenance(**values)


def uniform_profile(profile: str) -> Callable[[int], str]:
    def choose(seed: int) -> str:
        return profile

    return choose


def alternating_profile(seed_population: tuple[int, ...]) -> Callable[[int], str]:
    """半分をHeuristic advantage、半分をLearning advantageにする。"""
    order = {seed: index for index, seed in enumerate(seed_population)}

    def choose(seed: int) -> str:
        return HEURISTIC_ADVANTAGE if order[seed] % 2 == 0 else LEARNING_ADVANTAGE

    return choose


def game_ranks(rotation: int, profile: str) -> tuple[int, int, int, int]:
    """1 hanchanのSeat 0..3順のrankをprofileとrotation planから決める。"""
    heuristic_ranks, learning_ranks = _RANKS_BY_PROFILE[profile]
    remaining = {HEURISTIC_SLOT: list(heuristic_ranks), "B": list(learning_ranks)}
    ranks = []
    for slot in ROTATION_PLAN[rotation]:
        ranks.append(remaining[slot].pop(0))
    return tuple(ranks)  # type: ignore[return-value]


def seat_results(
    seed_population: tuple[int, ...],
    profile_of: Callable[[int], str],
    *,
    heuristic_identity: str = HEURISTIC_IDENTITY,
    learning_identity: str = LEARNING_IDENTITY,
    game_mode: str = GAME_MODE,
) -> tuple[SeatResult, ...]:
    """canonical ``seed -> rotation -> seat``順のraw seat rowsを合成する。"""
    rows: list[SeatResult] = []
    for seed in seed_population:
        profile = profile_of(seed)
        for rotation in range(ROTATION_COUNT):
            ranks = game_ranks(rotation, profile)
            for seat in range(SEAT_COUNT):
                slot = ROTATION_PLAN[rotation][seat]
                rows.append(
                    SeatResult(
                        seed=seed,
                        rotation=rotation,
                        game_mode=game_mode,
                        seat=Seat(seat),
                        policy_identity=(
                            heuristic_identity
                            if slot == HEURISTIC_SLOT
                            else learning_identity
                        ),
                        score=SCORE_BY_RANK[ranks[seat]],
                        rank=ranks[seat],
                    )
                )
    return tuple(rows)


def comparison_artifact(
    seed_population: tuple[int, ...] | None = None,
    profile_of: Callable[[int], str] | None = None,
    *,
    heuristic_identity: str = HEURISTIC_IDENTITY,
    learning_identity: str = LEARNING_IDENTITY,
    game_mode: str = GAME_MODE,
    max_steps: int = MAX_STEPS,
    rows: tuple[SeatResult, ...] | None = None,
    provenance: ExecutionProvenance | None = None,
) -> ComparisonArtifact:
    """既存generic ``ComparisonArtifact``をsyntheticに組み立てる。"""
    population = seeds() if seed_population is None else seed_population
    if rows is None:
        rows = seat_results(
            population,
            profile_of or uniform_profile(HEURISTIC_ADVANTAGE),
            heuristic_identity=heuristic_identity,
            learning_identity=learning_identity,
            game_mode=game_mode,
        )
    return ComparisonArtifact(
        schema_version=ARTIFACT_SCHEMA_VERSION,
        comparison_protocol=COMPARISON_PROTOCOL,
        plan=ArtifactPlan(
            policy_a_identity=heuristic_identity,
            policy_b_identity=learning_identity,
            seeds=population,
            game_mode=game_mode,
            max_steps=max_steps,
        ),
        provenance=provenance or comparison_provenance(),
        seat_results=rows,
        metrics_a=aggregate_policy_metrics(heuristic_identity, rows),
        metrics_b=aggregate_policy_metrics(learning_identity, rows),
    )


def comparison_result(
    seed_population: tuple[int, ...],
    profile_of: Callable[[int], str],
    *,
    heuristic_identity: str = HEURISTIC_IDENTITY,
    learning_identity: str = LEARNING_IDENTITY,
) -> ComparisonResult:
    """``save_comparison_artifact()``へ渡せるsynthetic comparison result。"""
    rows = seat_results(
        seed_population,
        profile_of,
        heuristic_identity=heuristic_identity,
        learning_identity=learning_identity,
    )
    plan = ComparisonPlan(
        policy_a=PolicySpec(identity=heuristic_identity, factory=stub_heuristic_policy),
        policy_b=PolicySpec(identity=learning_identity, factory=stub_learning_policy),
        seeds=seed_population,
        game_mode=GAME_MODE,
        max_steps=MAX_STEPS,
    )
    return ComparisonResult(
        plan=plan,
        seat_results=rows,
        metrics_a=aggregate_policy_metrics(heuristic_identity, rows),
        metrics_b=aggregate_policy_metrics(learning_identity, rows),
    )


@contextmanager
def merged_main_execution(
    provenance: SingleRoundExecutionProvenance | None = None,
) -> Iterator[SingleRoundExecutionProvenance]:
    """merged-main execution disciplineの読み取り結果だけを差し替える。

    check自体(``_execution_safety``)はproductionのまま使い、testのために
    disciplineを緩めた別経路を作らない。
    """
    live = provenance or lock_provenance()
    with (
        mock.patch.object(lock_module, "_require_environment_consistent"),
        mock.patch.object(
            lock_module, "collect_execution_provenance", return_value=live
        ),
        mock.patch.object(
            lock_module,
            "require_clean_arena_head",
            return_value=live.lisjong_arena_revision,
        ),
        mock.patch.object(
            lock_module,
            "require_merged_arena_revision",
            return_value=live.lisjong_arena_revision,
        ),
    ):
        yield live


def destinations(directory: Path) -> dict[str, Path]:
    return {
        "comparison_artifact": directory / "comparison.json",
        "overall_result": directory / "overall-result.json",
    }


def lock_document(
    directory: Path,
    *,
    seed_population: tuple[int, ...] | None = None,
    heuristic: ParticipantBinding | None = None,
    learning: ParticipantBinding | None = None,
    max_workers: int = 1,
    ml_runtime_packages: tuple[str, ...] = (),
) -> dict[str, object]:
    population = seeds() if seed_population is None else seed_population
    with merged_main_execution():
        return build_lock_document(
            destinations=destinations(directory),
            heuristic=heuristic or heuristic_binding(),
            learning=learning or learning_binding(),
            seeds=population,
            max_workers=max_workers,
            ml_runtime_packages=ml_runtime_packages,
        )


def save_synthetic_comparison(
    result: ComparisonResult,
    path: Path,
    *,
    provenance: ExecutionProvenance | None = None,
) -> None:
    """install metadataへ依存せずcomparison.jsonを既存save pathで書く。"""
    with mock.patch.object(
        artifact_module,
        "_collect_execution_provenance",
        return_value=provenance or comparison_provenance(),
    ):
        save_comparison_artifact(result, path)


_ARTIFACT_FIELDS = (
    "schema_version",
    "comparison_protocol",
    "plan",
    "provenance",
    "seat_results",
    "metrics_a",
    "metrics_b",
)


def tampered_comparison_artifact(
    rows: tuple[SeatResult, ...],
    *,
    seed_population: tuple[int, ...] | None = None,
    heuristic_identity: str = HEURISTIC_IDENTITY,
    learning_identity: str = LEARNING_IDENTITY,
    game_mode: str = GAME_MODE,
    max_steps: int = MAX_STEPS,
    metrics_a: object | None = None,
    metrics_b: object | None = None,
) -> ComparisonArtifact:
    """generic validationを通らないraw rowsを持つartifactを合成する。

    generic ``ComparisonArtifact``自身が既にこれらを拒否するのは正しい挙動
    なので、purpose-specific boundaryが独立にfail closedすることを確認する
    ためだけにvalue validationを迂回する。productionでこの経路は使わない。
    """
    population = seeds() if seed_population is None else seed_population
    values = {
        "schema_version": ARTIFACT_SCHEMA_VERSION,
        "comparison_protocol": COMPARISON_PROTOCOL,
        "plan": ArtifactPlan(
            policy_a_identity=heuristic_identity,
            policy_b_identity=learning_identity,
            seeds=population,
            game_mode=game_mode,
            max_steps=max_steps,
        ),
        "provenance": comparison_provenance(),
        "seat_results": rows,
        "metrics_a": (
            metrics_a
            if metrics_a is not None
            else aggregate_policy_metrics(heuristic_identity, rows)
        ),
        "metrics_b": (
            metrics_b
            if metrics_b is not None
            else aggregate_policy_metrics(learning_identity, rows)
        ),
    }
    artifact = ComparisonArtifact.__new__(ComparisonArtifact)
    for name in _ARTIFACT_FIELDS:
        object.__setattr__(artifact, name, values[name])
    return artifact
