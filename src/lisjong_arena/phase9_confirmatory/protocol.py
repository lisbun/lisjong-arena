"""Pure locked contracts for the Phase 9 one-shot family-selection gate."""

import random
from dataclasses import dataclass
from enum import Enum
from math import isfinite
from statistics import median

from lisjong_arena.phase2_training_anchor.extraction import FIRST_PARTY_SOURCE_CLASS
from lisjong_arena.phase5_belief_dataset.model import GameIdentity

PROTOCOL_ID = "phase9-confirmatory-family-selection-v1"
HOLDOUT_ROLE = "confirmatory-test-only"
HOLDOUT_SEEDS = tuple(range(160, 180))
HOLDOUT_GAME_COUNT = 20
HISTORICAL_FORBIDDEN_SEEDS = tuple(range(100, 160))

SNAPSHOT_ARTIFACT_IDENTITY = (
    "54513de7885b1d08f3df315ae255b89dd2dd0bad0eebfeaba554572a408d9089"
)
SNAPSHOT_WEIGHTS_SHA256 = (
    "8fabd711aad299d64dc665d0a989efe84876f8e956a36317907b5cf32eeea351"
)
SNAPSHOT_PARAMETER_COUNT = 134_856
S2_ARTIFACT_IDENTITY = (
    "90c4b7cc2d368b4db6b72f65b45b982afd5cac05ff86491c3da07c871d08c3b6"
)
S2_WEIGHTS_SHA256 = "463f9f49e6fefe02e8b2560cf1fe70b645e6f87625000cebd9d2fbfa83e28e3a"
S2_PARAMETER_COUNT = 459_080
S2_SELECTED_EPOCH = 40

HISTORICAL_REVISIONS = {
    "lisjong": "6db1ddc0c6fae312801104008bf18660975f687d",
    "lisjong_engine": "8735e89e1aea000ab59368d0368d476787827741",
    "lisjong_arena": "e667890f0124670a6858fba13bc41767cdc80350",
}
HISTORICAL_TREES = {
    "lisjong": "d249ad8653d749e5b7ec7aff79b69454cd8666ab",
    "lisjong_engine": "2ce97010c59b123705a74467a9672fd63b1654db",
    "lisjong_arena": "2686d4a279a79dcd0fc01e960e97ec5316667759",
}
HISTORICAL_ARENA_REF = "archive/handbelief-phase5-e667890"
HISTORICAL_RIICHIENV_VERSION = "0.4.8"
HISTORICAL_POLICY_POPULATION = "TwoStepUkeirePolicy x4"
LOCKED_RULE_FINGERPRINT = (
    "8e22eae8b8e97c081bccf5875b4201535969a9844164b30087e602078eb75135"
)

MATERIALITY_EPSILON = 0.0025
BOOTSTRAP_RNG = "python-stdlib-random.Random"
BOOTSTRAP_SEED = 0
BOOTSTRAP_REPLICATES = 20_000
BOOTSTRAP_CLUSTERS_PER_REPLICATE = 20
BOOTSTRAP_LOWER_INDEX = 499
BOOTSTRAP_UPPER_INDEX = 19_499
PHYSICAL_RESIDUAL_TOLERANCE = 1e-6
DEPTH_BUCKETS = ("depth 1", "depth 2..4", "depth 5..8", "depth 9+")


class FamilyClassification(Enum):
    STOP_REWORK = "STOP / REWORK"
    REFORMULATE = "REFORMULATE"
    SEQUENTIAL_FAMILY_LOCKED = "SEQUENTIAL FAMILY LOCKED"
    SNAPSHOT_FAMILY_LOCKED = "SNAPSHOT FAMILY LOCKED"


@dataclass(frozen=True, slots=True)
class PairedGameCluster:
    game: GameIdentity
    anchor_count: int
    cell_count: int
    snapshot_absolute_error_sum: float
    s2_absolute_error_sum: float

    def __post_init__(self) -> None:
        if not isinstance(self.game, GameIdentity):
            raise TypeError("game must be a GameIdentity")
        if type(self.anchor_count) is not int or self.anchor_count <= 0:
            raise ValueError("anchor_count must be a positive int")
        if self.cell_count != self.anchor_count * 102:
            raise ValueError("each anchor must contribute exactly 102 cells")
        for name in ("snapshot_absolute_error_sum", "s2_absolute_error_sum"):
            value = getattr(self, name)
            if not isinstance(value, (int, float)) or not isfinite(value) or value < 0:
                raise ValueError(f"{name} must be finite and non-negative")

    @property
    def snapshot_mae(self) -> float:
        return self.snapshot_absolute_error_sum / self.cell_count

    @property
    def s2_mae(self) -> float:
        return self.s2_absolute_error_sum / self.cell_count

    @property
    def delta_mae(self) -> float:
        return self.snapshot_mae - self.s2_mae


@dataclass(frozen=True, slots=True)
class BootstrapInterval:
    lower: float
    upper: float


@dataclass(frozen=True, slots=True)
class RobustnessDiagnostics:
    per_game_deltas: tuple[float, ...]
    positive_game_count: int
    zero_game_count: int
    negative_game_count: int
    game_macro_mean: float
    median_per_game: float
    leave_one_game_out_deltas: tuple[float, ...]


def validate_holdout_games(games: tuple[GameIdentity, ...]) -> None:
    expected = tuple(
        GameIdentity(FIRST_PARTY_SOURCE_CLASS, seed) for seed in HOLDOUT_SEEDS
    )
    if games != expected:
        raise ValueError("Phase 9 requires exactly the ordered seeds 160..179")
    if len(games) != HOLDOUT_GAME_COUNT:
        raise ValueError("Phase 9 requires exactly 20 hanchan")
    if any(game.game_seed in HISTORICAL_FORBIDDEN_SEEDS for game in games):
        raise ValueError("Phase 9 holdout contains a historical seed")


def pooled_arm_mae(clusters: tuple[PairedGameCluster, ...], arm: str) -> float:
    if not clusters:
        raise ValueError("pooled MAE requires clusters")
    if arm not in ("snapshot", "s2"):
        raise ValueError("arm must be snapshot or s2")
    cells = sum(cluster.cell_count for cluster in clusters)
    absolute = sum(
        getattr(cluster, f"{arm}_absolute_error_sum") for cluster in clusters
    )
    return absolute / cells


def pooled_delta(clusters: tuple[PairedGameCluster, ...]) -> float:
    return pooled_arm_mae(clusters, "snapshot") - pooled_arm_mae(clusters, "s2")


def paired_hanchan_bootstrap(
    clusters: tuple[PairedGameCluster, ...],
) -> BootstrapInterval:
    if len(clusters) != BOOTSTRAP_CLUSTERS_PER_REPLICATE:
        raise ValueError("formal Phase 9 bootstrap requires exactly 20 clusters")
    validate_holdout_games(tuple(cluster.game for cluster in clusters))
    rng = random.Random(BOOTSTRAP_SEED)
    deltas = []
    for _ in range(BOOTSTRAP_REPLICATES):
        selected = tuple(
            clusters[rng.randrange(len(clusters))]
            for _ in range(BOOTSTRAP_CLUSTERS_PER_REPLICATE)
        )
        deltas.append(pooled_delta(selected))
    ordered = sorted(deltas)
    return BootstrapInterval(
        ordered[BOOTSTRAP_LOWER_INDEX], ordered[BOOTSTRAP_UPPER_INDEX]
    )


def robustness_diagnostics(
    clusters: tuple[PairedGameCluster, ...],
) -> RobustnessDiagnostics:
    if len(clusters) != HOLDOUT_GAME_COUNT:
        raise ValueError("formal diagnostics require exactly 20 games")
    validate_holdout_games(tuple(cluster.game for cluster in clusters))
    deltas = tuple(cluster.delta_mae for cluster in clusters)
    return RobustnessDiagnostics(
        per_game_deltas=deltas,
        positive_game_count=sum(value > 0 for value in deltas),
        zero_game_count=sum(value == 0 for value in deltas),
        negative_game_count=sum(value < 0 for value in deltas),
        game_macro_mean=sum(deltas) / len(deltas),
        median_per_game=median(deltas),
        leave_one_game_out_deltas=tuple(
            pooled_delta(clusters[:index] + clusters[index + 1 :])
            for index in range(len(clusters))
        ),
    )


def physical_gate_passes(
    *,
    constraint_non_convergence_count: int,
    maximum_residual: float,
    concealed_size_inconsistency_max: float,
    conservation_violation_sample_rate: float,
) -> bool:
    values = (
        maximum_residual,
        concealed_size_inconsistency_max,
        conservation_violation_sample_rate,
    )
    if (
        type(constraint_non_convergence_count) is not int
        or constraint_non_convergence_count < 0
        or any(
            not isinstance(value, (int, float)) or not isfinite(value) or value < 0
            for value in values
        )
    ):
        raise ValueError("physical validity inputs are invalid")
    return (
        constraint_non_convergence_count == 0
        and maximum_residual <= PHYSICAL_RESIDUAL_TOLERANCE
        and concealed_size_inconsistency_max <= PHYSICAL_RESIDUAL_TOLERANCE
        and conservation_violation_sample_rate == 0
    )


def classify_family(
    *,
    source_semantics_ok: bool = True,
    validity_ok: bool,
    delta_mae: float,
    ci_lower: float,
    ci_upper: float,
) -> FamilyClassification:
    """Apply the exhaustive pre-registered rule with validity precedence."""
    if type(source_semantics_ok) is not bool:
        raise TypeError("source_semantics_ok must be bool")
    if type(validity_ok) is not bool:
        raise TypeError("validity_ok must be bool")
    if not source_semantics_ok:
        return FamilyClassification.REFORMULATE
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
        return FamilyClassification.STOP_REWORK
    if delta_mae >= MATERIALITY_EPSILON and ci_lower > 0:
        return FamilyClassification.SEQUENTIAL_FAMILY_LOCKED
    return FamilyClassification.SNAPSHOT_FAMILY_LOCKED


def depth_bucket(depth: int) -> str:
    if type(depth) is not int or depth <= 0:
        raise ValueError("depth must be a positive int")
    if depth == 1:
        return DEPTH_BUCKETS[0]
    if depth <= 4:
        return DEPTH_BUCKETS[1]
    if depth <= 8:
        return DEPTH_BUCKETS[2]
    return DEPTH_BUCKETS[3]


__all__ = [
    "BOOTSTRAP_CLUSTERS_PER_REPLICATE",
    "BOOTSTRAP_LOWER_INDEX",
    "BOOTSTRAP_REPLICATES",
    "BOOTSTRAP_RNG",
    "BOOTSTRAP_SEED",
    "BOOTSTRAP_UPPER_INDEX",
    "DEPTH_BUCKETS",
    "FamilyClassification",
    "HISTORICAL_ARENA_REF",
    "HISTORICAL_FORBIDDEN_SEEDS",
    "HISTORICAL_POLICY_POPULATION",
    "HISTORICAL_REVISIONS",
    "HISTORICAL_RIICHIENV_VERSION",
    "HISTORICAL_TREES",
    "HOLDOUT_GAME_COUNT",
    "HOLDOUT_ROLE",
    "HOLDOUT_SEEDS",
    "LOCKED_RULE_FINGERPRINT",
    "MATERIALITY_EPSILON",
    "PROTOCOL_ID",
    "PairedGameCluster",
    "SNAPSHOT_ARTIFACT_IDENTITY",
    "SNAPSHOT_PARAMETER_COUNT",
    "SNAPSHOT_WEIGHTS_SHA256",
    "S2_ARTIFACT_IDENTITY",
    "S2_PARAMETER_COUNT",
    "S2_SELECTED_EPOCH",
    "S2_WEIGHTS_SHA256",
    "classify_family",
    "depth_bucket",
    "paired_hanchan_bootstrap",
    "physical_gate_passes",
    "pooled_arm_mae",
    "pooled_delta",
    "robustness_diagnostics",
    "validate_holdout_games",
]
