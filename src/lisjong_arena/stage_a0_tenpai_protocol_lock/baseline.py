"""TRAIN-only Baseline 0 / Baseline 1 parameters for the #259 lock."""

from __future__ import annotations

import hashlib
import math
from collections import defaultdict

from lisjong_arena._artifact_io import canonical_json_text
from lisjong_arena.learned_policy_input import MAX_LIVE_WALL_TILES, MAX_MELDS_PER_PLAYER
from lisjong_arena.learned_policy_offline_q.protocol import Split
from lisjong_arena.stage_a0_tenpai_feasibility.labels import TargetAvailability
from lisjong_arena.stage_a0_tenpai_feasibility.sidecar import LoadedSidecar

from .materialize import LoadedPublicKeys
from .protocol import (
    BASELINE0_IDENTITY,
    BASELINE1_IDENTITY,
    JEFFREYS_ALPHA,
    JEFFREYS_BETA,
    MINIMUM_EXACT_KEY_SUPPORT,
    PROBABILITY_CLIP_EPSILON,
    RETAINED_DATASET_IDENTITY,
    TRAIN_SEEDS,
    StageA0ProtocolLockError,
)


def _jeffreys_probability(positive: int, support: int) -> float:
    if type(positive) is not int or type(support) is not int:
        raise TypeError("positive/support must be integers")
    if support <= 0 or not 0 <= positive <= support:
        raise StageA0ProtocolLockError("invalid Bernoulli support counts")
    return (positive + JEFFREYS_ALPHA) / (
        support + JEFFREYS_ALPHA + JEFFREYS_BETA
    )


def _cell_key(cell) -> tuple[int, int, int, int, int]:
    identity = cell.row_identity
    return (
        identity.seed,
        identity.step_ordinal,
        identity.decision_ordinal,
        identity.actor_seat,
        cell.identity.viewer_relative_offset,
    )


def _parameter_fingerprint(document: dict[str, object]) -> str:
    logical = {name: value for name, value in document.items() if name != "fingerprint"}
    return hashlib.sha256(canonical_json_text(logical).encode("utf-8")).hexdigest()


def build_train_baseline_parameters(
    sidecar: LoadedSidecar,
    public_keys: LoadedPublicKeys,
) -> dict[str, object]:
    """Build Baseline 0/1 from TRAIN eligible cells only.

    VALIDATION records are not read from the sidecar for target summaries here.
    Public key records may be validated structurally by their own strict reader,
    but only TRAIN keys enter the fitted tables.
    """
    if not isinstance(sidecar, LoadedSidecar):
        raise TypeError("sidecar must be a LoadedSidecar")
    if not isinstance(public_keys, LoadedPublicKeys):
        raise TypeError("public_keys must be a LoadedPublicKeys")
    if sidecar.manifest["protocol"]["source_identity"] != RETAINED_DATASET_IDENTITY:
        raise StageA0ProtocolLockError("sidecar source is not the retained dataset")
    if public_keys.manifest["source_identity"] != RETAINED_DATASET_IDENTITY:
        raise StageA0ProtocolLockError("public key source is not the retained dataset")
    if public_keys.manifest["sidecar_identity"] != sidecar.identity:
        raise StageA0ProtocolLockError("public keys are not bound to this sidecar")

    key_records = {
        record.cell_key: record
        for record in public_keys.records
        if record.split is Split.TRAIN
    }
    expected_train_seeds = set(TRAIN_SEEDS)
    if {record.seed for record in key_records.values()} != expected_train_seeds:
        raise StageA0ProtocolLockError("public-key TRAIN seed population is incomplete")

    exact_counts: dict[tuple[int, int], list[int]] = defaultdict(lambda: [0, 0])
    live_counts: dict[int, list[int]] = defaultdict(lambda: [0, 0])
    support = 0
    positive = 0
    seen_train_keys = set()

    for cell in sidecar.cells:
        if cell.row_identity.seed not in expected_train_seeds:
            continue
        key = _cell_key(cell)
        record = key_records.get(key)
        if record is None:
            raise StageA0ProtocolLockError(
                "TRAIN privileged cell has no matching player-safe public key"
            )
        seen_train_keys.add(key)
        if cell.availability is not TargetAvailability.AVAILABLE:
            continue
        target = cell.target.tenpai
        if target not in (0, 1):
            raise StageA0ProtocolLockError("eligible TRAIN cell has no binary Tenpai target")
        support += 1
        positive += target
        exact = (record.live_wall_tiles_remaining, record.public_meld_count)
        exact_counts[exact][0] += target
        exact_counts[exact][1] += 1
        live_counts[record.live_wall_tiles_remaining][0] += target
        live_counts[record.live_wall_tiles_remaining][1] += 1

    if seen_train_keys != set(key_records):
        raise StageA0ProtocolLockError(
            "TRAIN player-safe keys and privileged cells are not one-to-one"
        )
    if support == 0:
        raise StageA0ProtocolLockError("TRAIN contains zero eligible Tenpai cells")

    global_probability = _jeffreys_probability(positive, support)
    exact_table = []
    for (live, meld_count), (pos, n) in sorted(exact_counts.items()):
        if n < MINIMUM_EXACT_KEY_SUPPORT:
            continue
        exact_table.append(
            {
                "live_wall_tiles_remaining": live,
                "public_meld_count": meld_count,
                "positive": pos,
                "support": n,
                "probability": _jeffreys_probability(pos, n),
            }
        )
    live_table = []
    for live, (pos, n) in sorted(live_counts.items()):
        live_table.append(
            {
                "live_wall_tiles_remaining": live,
                "positive": pos,
                "support": n,
                "probability": _jeffreys_probability(pos, n),
            }
        )

    document: dict[str, object] = {
        "baseline0": {
            "identity": BASELINE0_IDENTITY,
            "positive": positive,
            "support": support,
            "raw_prevalence": positive / support,
            "jeffreys_probability": global_probability,
            "role": "descriptive-only",
        },
        "baseline1": {
            "identity": BASELINE1_IDENTITY,
            "global": {
                "positive": positive,
                "support": support,
                "probability": global_probability,
            },
            "exact_table": exact_table,
            "live_wall_backoff_table": live_table,
            "minimum_exact_key_support": MINIMUM_EXACT_KEY_SUPPORT,
            "out_of_domain_behavior": "fail-closed",
        },
        "train_seeds": list(TRAIN_SEEDS),
        "eligible_cell_count": support,
    }
    document["fingerprint"] = _parameter_fingerprint(document)
    return document


def validate_train_baseline_parameters(document: object) -> dict[str, object]:
    if type(document) is not dict:
        raise StageA0ProtocolLockError("baseline parameters must be an object")
    expected = {
        "baseline0",
        "baseline1",
        "train_seeds",
        "eligible_cell_count",
        "fingerprint",
    }
    if set(document) != expected:
        raise StageA0ProtocolLockError("baseline parameter fields are invalid")
    if document["train_seeds"] != list(TRAIN_SEEDS):
        raise StageA0ProtocolLockError("baseline TRAIN seeds drifted")
    if _parameter_fingerprint(document) != document["fingerprint"]:
        raise StageA0ProtocolLockError("baseline fingerprint does not match content")
    b0 = document["baseline0"]
    b1 = document["baseline1"]
    if type(b0) is not dict or b0.get("identity") != BASELINE0_IDENTITY:
        raise StageA0ProtocolLockError("Baseline 0 identity is invalid")
    if type(b1) is not dict or b1.get("identity") != BASELINE1_IDENTITY:
        raise StageA0ProtocolLockError("Baseline 1 identity is invalid")
    if b0.get("role") != "descriptive-only":
        raise StageA0ProtocolLockError("Baseline 0 must remain descriptive-only")
    support = document["eligible_cell_count"]
    if type(support) is not int or support <= 0 or support != b0.get("support"):
        raise StageA0ProtocolLockError("baseline eligible support is invalid")
    if b1.get("global", {}).get("support") != support:
        raise StageA0ProtocolLockError("Baseline 1 global support differs from Baseline 0")
    return document


def baseline1_probability(
    parameters: dict[str, object],
    *,
    live_wall_tiles_remaining: int,
    public_meld_count: int,
) -> float:
    """Apply the locked exact-key -> live-wall -> global deterministic backoff."""
    validate_train_baseline_parameters(parameters)
    if type(live_wall_tiles_remaining) is not int or not (
        0 <= live_wall_tiles_remaining <= MAX_LIVE_WALL_TILES
    ):
        raise StageA0ProtocolLockError("live-wall input is outside the locked range")
    if type(public_meld_count) is not int or not (
        0 <= public_meld_count <= MAX_MELDS_PER_PLAYER
    ):
        raise StageA0ProtocolLockError("meld-count input is outside the locked range")

    b1 = parameters["baseline1"]
    for record in b1["exact_table"]:
        if (
            record["live_wall_tiles_remaining"] == live_wall_tiles_remaining
            and record["public_meld_count"] == public_meld_count
        ):
            return float(record["probability"])
    for record in b1["live_wall_backoff_table"]:
        if record["live_wall_tiles_remaining"] == live_wall_tiles_remaining:
            return float(record["probability"])
    return float(b1["global"]["probability"])


def binary_log_loss(target: int, probability: float) -> float:
    """Locked finite Bernoulli NLL used by Baseline 1 and T-model evaluation."""
    if target not in (0, 1):
        raise StageA0ProtocolLockError("binary log-loss target must be 0 or 1")
    if type(probability) not in (int, float) or not math.isfinite(float(probability)):
        raise StageA0ProtocolLockError("binary log-loss probability must be finite")
    probability = float(probability)
    if not 0.0 <= probability <= 1.0:
        raise StageA0ProtocolLockError("binary log-loss probability is outside [0,1]")
    clipped = min(
        max(probability, PROBABILITY_CLIP_EPSILON),
        1.0 - PROBABILITY_CLIP_EPSILON,
    )
    return -(target * math.log(clipped) + (1 - target) * math.log(1.0 - clipped))


__all__ = [
    "baseline1_probability",
    "binary_log_loss",
    "build_train_baseline_parameters",
    "validate_train_baseline_parameters",
]
