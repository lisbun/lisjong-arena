"""Strict readback of the retained #150 corpus and #167 E160 evidence."""

import json
from dataclasses import dataclass
from pathlib import Path

from lisjong_arena.stage3_optimization_saturation.artifact import (
    load_model as load_e160_model,
)
from lisjong_arena.stage3_optimization_saturation.artifact import (
    load_result as load_phase167_result_file,
)
from lisjong_arena.stage3_optimization_saturation.lock import (
    validate_lock as validate_phase167_lock,
)
from lisjong_arena.stage3_optimization_saturation.retained import (
    load_predecessor_result,
)
from lisjong_arena.stage3_optimization_saturation.retained import (
    load_retained as load_phase150_and_157,
)

from .protocol import (
    E160_ARENA_REVISION,
    E160_ARM,
    E160_EPOCHS_RUN,
    E160_OUTCOME,
    E160_SELECTED_EPOCH,
    E160_WEIGHTS_SHA256,
    ENGINE_REVISION,
    LABEL_SEMANTICS_ID,
    LISJONG_REVISION,
    PHASE167_EXECUTION_LOCK_IDENTITY,
    PHASE167_RESULT_IDENTITY,
    RETAINED_DATASET_IDENTITY,
    RETAINED_POPULATION_IDENTITY,
    RETAINED_RAW_CORPUS_IDENTITY,
    Phase11Error,
    exact,
    identity,
    representation_value,
    retained_value,
)

LOCK_FILENAME = "execution-lock.json"
MODEL_DIRNAME = "E160"
RESULT_FILENAME = "optimization-saturation-result.json"


@dataclass(frozen=True, slots=True)
class RetainedPhase11Evidence:
    phase10_lock: dict[str, object]
    population: dict[str, object]
    raw: object
    dataset: object
    phase167_lock: dict[str, object]
    phase167_result: dict[str, object]
    e160_manifest: dict[str, object]
    e160_model: object


def load_phase167_lock(root: str | Path) -> dict[str, object]:
    path = Path(root) / LOCK_FILENAME
    if not path.is_file():
        raise Phase11Error(f"retained #167 execution lock is missing: {path}")
    try:
        lock = validate_phase167_lock(json.loads(path.read_bytes()))
    except (ValueError, TypeError, KeyError, json.JSONDecodeError) as error:
        raise Phase11Error(
            f"retained #167 execution lock is invalid: {error}"
        ) from error
    exact(identity(lock), PHASE167_EXECUTION_LOCK_IDENTITY, "#167 execution lock")
    exact(
        lock["provenance"]["source_revisions"],
        {
            "lisjong": LISJONG_REVISION,
            "lisjong_engine": ENGINE_REVISION,
            "lisjong_arena": E160_ARENA_REVISION,
        },
        "#167 source revisions",
    )
    exact(lock["provenance"]["label_semantics_id"], LABEL_SEMANTICS_ID, "labels")
    return lock


def load_retained(
    corpus_root: str | Path,
    phase157_root: str | Path,
    phase167_root: str | Path,
) -> RetainedPhase11Evidence:
    """Load every predecessor through its own validator; never regenerate it."""
    try:
        predecessor = load_phase150_and_157(corpus_root, phase157_root)
        load_predecessor_result(phase157_root, predecessor.predecessor_lock)
        phase167_lock = load_phase167_lock(phase167_root)
        model, manifest = load_e160_model(
            Path(phase167_root) / MODEL_DIRNAME,
            predecessor.population,
            phase167_lock,
            predecessor.baseline_manifest,
        )
        result = load_phase167_result_file(
            Path(phase167_root) / RESULT_FILENAME, phase167_lock
        )
    except Phase11Error:
        raise
    except (ValueError, TypeError, KeyError, OSError) as error:
        raise Phase11Error(f"retained artifact readback failed: {error}") from error
    exact(result["result_identity"], PHASE167_RESULT_IDENTITY, "#167 result identity")
    exact(result["outcome"], E160_OUTCOME, "#167 outcome")
    exact(
        result["arms"][E160_ARM],
        manifest,
        "#167 result E160 evidence against the loaded checkpoint manifest",
    )
    exact(manifest["arm"], E160_ARM, "E160 arm")
    exact(manifest["weights_sha256"], E160_WEIGHTS_SHA256, "E160 weights")
    exact(manifest["selected_epoch"], E160_SELECTED_EPOCH, "E160 selected epoch")
    exact(len(manifest["loss_history"]), E160_EPOCHS_RUN, "E160 epochs run")
    training_lock = manifest["training_lock"]
    representation = representation_value()
    exact(training_lock["candidate"], "S2", "E160 candidate")
    exact(
        training_lock["parameter_count"],
        representation["parameter_count"],
        "E160 parameter count",
    )
    exact(
        training_lock["model_config"]["family"],
        representation["family"],
        "E160 model family",
    )
    exact(
        training_lock["model_config"]["latent_dimension"],
        representation["latent_dimension"],
        "E160 latent dimension",
    )
    exact(
        training_lock["feature_semantics"],
        representation["feature_semantics"],
        "E160 feature semantics",
    )
    exact(
        training_lock["sequence_semantics"],
        representation["sequence_semantics"],
        "E160 sequence semantics",
    )
    exact(
        training_lock["evaluation"],
        "Phase 8 serving-realistic self_rollout; analytic t=0 prior",
        "E160 self-rollout semantics",
    )
    for name, expected in (
        ("population_identity", RETAINED_POPULATION_IDENTITY),
        ("raw_corpus_identity", RETAINED_RAW_CORPUS_IDENTITY),
        ("dataset_identity", RETAINED_DATASET_IDENTITY),
    ):
        exact(predecessor.population[name], expected, f"retained {name}")
    return RetainedPhase11Evidence(
        phase10_lock=predecessor.phase10_lock,
        population=predecessor.population,
        raw=predecessor.raw,
        dataset=predecessor.dataset,
        phase167_lock=phase167_lock,
        phase167_result=result,
        e160_manifest=manifest,
        e160_model=model,
    )


def retained_readback_value(evidence: RetainedPhase11Evidence) -> dict[str, object]:
    value = retained_value()
    exact(
        identity(evidence.phase167_lock),
        value["phase167_execution_lock_identity"],
        "#167 lock",
    )
    exact(
        evidence.phase167_result["result_identity"],
        value["phase167_result_identity"],
        "#167 result",
    )
    exact(
        evidence.e160_manifest["weights_sha256"],
        value["e160_weights_sha256"],
        "E160 weights",
    )
    return value


__all__ = [
    "LOCK_FILENAME",
    "MODEL_DIRNAME",
    "RESULT_FILENAME",
    "RetainedPhase11Evidence",
    "load_phase167_lock",
    "load_retained",
    "retained_readback_value",
]
