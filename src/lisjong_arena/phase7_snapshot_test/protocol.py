"""Pure deterministic contracts for the locked Phase 7 TEST gate."""

import random
from dataclasses import dataclass
from enum import Enum
from math import isfinite
from statistics import median

from lisjong_arena.phase5_belief_dataset.model import GameIdentity

PROTOCOL_ID = "phase7-snapshot-test-gate-v1"
MATERIALITY_EPSILON = 0.0025
BOOTSTRAP_SEED = 0
BOOTSTRAP_REPLICATES = 20_000
BOOTSTRAP_CLUSTERS_PER_REPLICATE = 10
BOOTSTRAP_LOWER_INDEX = 499
BOOTSTRAP_UPPER_INDEX = 19_499
PHYSICAL_RESIDUAL_TOLERANCE = 1e-6
LOCKED_TEST_ANCHOR_COUNT = 4_726
LOCKED_TEST_GAME_COUNT = 10
LOCKED_TEST_SEEDS = tuple(range(150, 160))
LOCKED_RAW_CORPUS_IDENTITY = (
    "779599517d787a9515c69a9bd8bd610a491520d24799b0532cd05e2d0136c79e"
)
LOCKED_DATASET_IDENTITY = (
    "3167b61277a088f7c2b5c0e9e01aefb8af1b8c9c1609eb8de3f9a6bf7eff73e1"
)
LOCKED_PHASE6_WEIGHTS_SHA256 = (
    "8fabd711aad299d64dc665d0a989efe84876f8e956a36317907b5cf32eeea351"
)
LOCKED_PHASE6_ARTIFACT_IDENTITY = (
    "54513de7885b1d08f3df315ae255b89dd2dd0bad0eebfeaba554572a408d9089"
)
LOCKED_PHASE6_MANIFEST_SHA256 = (
    "99b32845f8fc875a01c13d5dd607c1558e9a1ec4dea4a903f0bf10be459e7d09"
)


class GateClassification(Enum):
    CONTINUE = "CONTINUE"
    REFORMULATE = "REFORMULATE"
    STOP_REWORK = "STOP / REWORK"


@dataclass(frozen=True, slots=True)
class PairedGameCluster:
    game: GameIdentity
    anchor_count: int
    cell_count: int
    baseline_absolute_error_sum: float
    learned_absolute_error_sum: float

    def __post_init__(self) -> None:
        if not isinstance(self.game, GameIdentity):
            raise TypeError("game must be a GameIdentity")
        if type(self.anchor_count) is not int or self.anchor_count <= 0:
            raise ValueError("anchor_count must be a positive int")
        if self.cell_count != self.anchor_count * 102:
            raise ValueError("each anchor must contribute exactly 102 cells")
        for name in ("baseline_absolute_error_sum", "learned_absolute_error_sum"):
            value = getattr(self, name)
            if not isinstance(value, (int, float)) or not isfinite(value) or value < 0:
                raise ValueError(f"{name} must be finite and non-negative")

    @property
    def baseline_mae(self) -> float:
        return self.baseline_absolute_error_sum / self.cell_count

    @property
    def learned_mae(self) -> float:
        return self.learned_absolute_error_sum / self.cell_count

    @property
    def delta_mae(self) -> float:
        return self.baseline_mae - self.learned_mae


@dataclass(frozen=True, slots=True)
class BootstrapInterval:
    lower: float
    upper: float


@dataclass(frozen=True, slots=True)
class RobustnessDiagnostics:
    per_game_deltas: tuple[float, ...]
    game_macro_mean: float
    median_per_game: float
    positive_game_count: int
    leave_one_game_out_deltas: tuple[float, ...]


def pooled_delta(clusters: tuple[PairedGameCluster, ...]) -> float:
    if not clusters:
        raise ValueError("pooled delta requires at least one cluster")
    cells = sum(value.cell_count for value in clusters)
    baseline = sum(value.baseline_absolute_error_sum for value in clusters) / cells
    learned = sum(value.learned_absolute_error_sum for value in clusters) / cells
    return baseline - learned


def _bootstrap_delta_values(
    clusters: tuple[PairedGameCluster, ...],
    *,
    seed: int,
    replicates: int,
    clusters_per_replicate: int,
) -> tuple[float, ...]:
    if not clusters:
        raise ValueError("bootstrap requires at least one cluster")
    if type(seed) is not int:
        raise TypeError("bootstrap seed must be an int")
    if type(replicates) is not int or replicates <= 0:
        raise ValueError("replicates must be a positive int")
    if type(clusters_per_replicate) is not int or clusters_per_replicate <= 0:
        raise ValueError("clusters_per_replicate must be a positive int")
    rng = random.Random(seed)
    values = []
    for _ in range(replicates):
        selected = tuple(
            clusters[rng.randrange(len(clusters))]
            for _ in range(clusters_per_replicate)
        )
        values.append(pooled_delta(selected))
    return tuple(values)


def locked_percentile_interval(values: tuple[float, ...]) -> BootstrapInterval:
    if len(values) != BOOTSTRAP_REPLICATES:
        raise ValueError("formal interval requires exactly 20,000 values")
    ordered = sorted(values)
    return BootstrapInterval(
        ordered[BOOTSTRAP_LOWER_INDEX], ordered[BOOTSTRAP_UPPER_INDEX]
    )


def paired_hanchan_bootstrap(
    clusters: tuple[PairedGameCluster, ...],
) -> BootstrapInterval:
    if len(clusters) != BOOTSTRAP_CLUSTERS_PER_REPLICATE:
        raise ValueError("formal bootstrap requires exactly 10 canonical clusters")
    if len({value.game for value in clusters}) != len(clusters):
        raise ValueError("formal bootstrap clusters must have unique identities")
    return locked_percentile_interval(
        _bootstrap_delta_values(
            clusters,
            seed=BOOTSTRAP_SEED,
            replicates=BOOTSTRAP_REPLICATES,
            clusters_per_replicate=BOOTSTRAP_CLUSTERS_PER_REPLICATE,
        )
    )


def robustness_diagnostics(
    clusters: tuple[PairedGameCluster, ...],
) -> RobustnessDiagnostics:
    if len(clusters) != 10:
        raise ValueError("formal robustness diagnostics require exactly 10 games")
    deltas = tuple(value.delta_mae for value in clusters)
    return RobustnessDiagnostics(
        per_game_deltas=deltas,
        game_macro_mean=sum(deltas) / len(deltas),
        median_per_game=median(deltas),
        positive_game_count=sum(value > 0 for value in deltas),
        leave_one_game_out_deltas=tuple(
            pooled_delta(clusters[:index] + clusters[index + 1 :])
            for index in range(len(clusters))
        ),
    )


def classify_gate(
    *,
    validity_ok: bool,
    delta_mae: float,
    ci_lower: float,
    ci_upper: float,
) -> GateClassification:
    """Apply the exhaustive locked classification, with validity precedence."""
    if type(validity_ok) is not bool:
        raise TypeError("validity_ok must be a bool")
    for name, value in (
        ("delta_mae", delta_mae),
        ("ci_lower", ci_lower),
        ("ci_upper", ci_upper),
    ):
        if not isinstance(value, (int, float)) or not isfinite(value):
            raise ValueError(f"{name} must be finite")
    if ci_lower > ci_upper:
        raise ValueError("CI lower must not exceed CI upper")
    if not validity_ok:
        return GateClassification.STOP_REWORK
    if delta_mae >= MATERIALITY_EPSILON and ci_lower > 0:
        return GateClassification.CONTINUE
    if delta_mae <= -MATERIALITY_EPSILON and ci_upper < 0:
        return GateClassification.STOP_REWORK
    return GateClassification.REFORMULATE


def physical_gate_passes(
    *,
    constraint_non_convergence_count: int,
    maximum_residual: float,
    concealed_size_inconsistency_max: float,
    conservation_violation_sample_rate: float,
) -> bool:
    """Use tolerance-based semantic violations; raw total excess is report-only."""
    if type(constraint_non_convergence_count) is not int:
        raise TypeError("constraint_non_convergence_count must be an int")
    values = (
        maximum_residual,
        concealed_size_inconsistency_max,
        conservation_violation_sample_rate,
    )
    if any(
        not isinstance(value, (int, float)) or not isfinite(value) for value in values
    ):
        raise ValueError("physical metrics must be finite")
    return (
        constraint_non_convergence_count == 0
        and maximum_residual <= PHYSICAL_RESIDUAL_TOLERANCE
        and concealed_size_inconsistency_max <= PHYSICAL_RESIDUAL_TOLERANCE
        and conservation_violation_sample_rate == 0
    )


__all__ = [
    "BOOTSTRAP_CLUSTERS_PER_REPLICATE",
    "BOOTSTRAP_LOWER_INDEX",
    "BOOTSTRAP_REPLICATES",
    "BOOTSTRAP_SEED",
    "BOOTSTRAP_UPPER_INDEX",
    "MATERIALITY_EPSILON",
    "LOCKED_TEST_ANCHOR_COUNT",
    "LOCKED_TEST_GAME_COUNT",
    "LOCKED_TEST_SEEDS",
    "LOCKED_RAW_CORPUS_IDENTITY",
    "LOCKED_DATASET_IDENTITY",
    "LOCKED_PHASE6_WEIGHTS_SHA256",
    "LOCKED_PHASE6_ARTIFACT_IDENTITY",
    "LOCKED_PHASE6_MANIFEST_SHA256",
    "PHYSICAL_RESIDUAL_TOLERANCE",
    "PROTOCOL_ID",
    "BootstrapInterval",
    "GateClassification",
    "PairedGameCluster",
    "RobustnessDiagnostics",
    "classify_gate",
    "locked_percentile_interval",
    "paired_hanchan_bootstrap",
    "physical_gate_passes",
    "pooled_delta",
    "robustness_diagnostics",
]
