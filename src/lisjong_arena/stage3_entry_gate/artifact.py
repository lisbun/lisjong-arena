"""Stage 3 Entry Gate model and result artifacts.

Phase 8の`phase8_sequential.artifact`はPhase 8 formal population、locked
dataset identity、`SNAPSHOT_VALIDATION_MAE`にbindされたvalidatorである。Stage 3
のためにそれらのconstantsやvalidatorを書き換えないため、Stage 3は別schema /
別identityのartifactを持つ（`lisjong-project#36`のintegration decisionに従う）。

artifactはstate_dictだけを保存し、factory / callable / 任意codeを保存・復元
しない。既存destinationを上書きせず、内部矛盾はload時にfail closedする。
weightsとgenerated resultはGit repositoryへcommitしない。
"""

import hashlib
import json
import platform
import sys
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory

from lisjong_arena.phase4_raw_corpus.codec import canonical_json_bytes
from lisjong_arena.phase6_snapshot.feature import FEATURE_SEMANTICS_ID
from lisjong_arena.phase6_snapshot.tensor import FEATURE_DIM
from lisjong_arena.phase8_sequential.model import (
    S2_PARAMETER_COUNT,
    model_config,
)
from lisjong_arena.phase8_sequential.protocol import (
    SEQUENCE_SEMANTICS_ID,
    checkpoint_improves,
)
from lisjong_arena.stage3_entry_gate.experiment import CANDIDATE, REFERENCE_ARM_ID
from lisjong_arena.stage3_entry_gate.population import PILOT_ROLE

MODEL_ARTIFACT_SCHEMA_VERSION = "stage3-entry-gate-sequential-model-v1"
RESULT_SCHEMA_VERSION = "stage3-entry-gate-result-v1"
MANIFEST_FILENAME = "manifest.json"
WEIGHTS_FILENAME = "weights.pt"
CHECKPOINT_SELECTION_RULE = "lowest pooled self-rollout VALIDATION per-tile MAE"
POPULATION_IDS = ("A", "B", "C")
_SHA256_LENGTH = 64


class Stage3ArtifactError(ValueError):
    """Stage 3 artifact contract / persistence violation。"""


@dataclass(frozen=True, slots=True)
class LoadedStage3Model:
    manifest: dict[str, object]
    state_dict: object
    weights_bytes: int


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _digest(value: object, name: str) -> str:
    if (
        type(value) is not str
        or len(value) != _SHA256_LENGTH
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise Stage3ArtifactError(f"{name} must be a lowercase SHA-256")
    return value


def execution_runtime_value() -> dict[str, object]:
    """training / inference runtimeのprovenance。値を捏造しない。

    Phase 8 formal runtimeとの差（CPython patch level、torch wheel build）は
    ここへそのまま記録し、`2.13.0+cpu`であるかのように書き換えない。
    """
    import torch
    from lisjong_engine.rules import RuleSet

    from lisjong_arena.phase2_training_anchor.pipeline_provenance import (
        collect_pipeline_provenance,
    )

    revisions = collect_pipeline_provenance(RuleSet.default()).source_revisions
    return {
        "python": platform.python_version(),
        "python_version_info": list(sys.version_info[:3]),
        "torch": torch.__version__,
        "device": "cpu",
        "cuda_available": bool(torch.cuda.is_available()),
        "torch_thread_count": torch.get_num_threads(),
        "platform": platform.platform(),
        # corpus生成時のrevisionとは別のprovenanceとして記録する。
        "execution_source_revisions": {
            "lisjong": revisions.lisjong,
            "lisjong_engine": revisions.lisjong_engine,
            "lisjong_arena": revisions.lisjong_arena,
        },
        "execution_source_revisions_fully_resolved": revisions.fully_resolved,
    }


def _locked_training_config_value() -> dict[str, object]:
    """Phase 8 `FORMAL_TRAINING_CONFIG`のcanonical value表現。"""
    from lisjong_arena.phase8_sequential.training import FORMAL_TRAINING_CONFIG

    config = FORMAL_TRAINING_CONFIG
    return {
        "seed": config.seed,
        "dataloader_seed": config.dataloader_seed,
        "learning_rate": config.learning_rate,
        "weight_decay": config.weight_decay,
        "max_epochs": config.max_epochs,
        "patience": config.patience,
        "workers": config.workers,
        "deterministic_algorithms": config.deterministic_algorithms,
        "torch_threads": config.torch_threads,
    }


def _selected_epoch_from_history(history: list) -> int:
    """Phase 8 `checkpoint_improves()`と同じ規則でselected epochを再導出する。

    単純な`min()`ではPhase 8の1e-12 tie toleranceを再現できない。historical
    Phase 8 contractと同じ規則を使い、tieでは先行epochを保持する。
    """
    best = float("inf")
    best_epoch = 0
    for row in history:
        if checkpoint_improves(float(row["validation_mae"]), best):
            best = float(row["validation_mae"])
            best_epoch = int(row["epoch"])
    return best_epoch


def _validate_within_population_validation(value: object) -> None:
    if type(value) is not dict:
        raise Stage3ArtifactError("within-population VALIDATION record is missing")
    expected = {
        "sequential_validation_mae",
        "conditional_uniform_validation_mae",
        "delta_mae_vs_conditional_uniform",
        "positive_game_count",
        "validation_game_count",
        "physical_consistency",
    }
    if set(value) != expected:
        raise Stage3ArtifactError("within-population VALIDATION fields are not exact")
    sequential = value["sequential_validation_mae"]
    baseline = value["conditional_uniform_validation_mae"]
    delta = value["delta_mae_vs_conditional_uniform"]
    if any(type(item) not in (int, float) for item in (sequential, baseline, delta)):
        raise Stage3ArtifactError("within-population VALIDATION metrics are invalid")
    if abs((baseline - sequential) - delta) > 1e-12:
        raise Stage3ArtifactError(
            "Delta MAE is not the conditional-uniform minus sequential difference"
        )
    games = value["validation_game_count"]
    positive = value["positive_game_count"]
    if (
        type(games) is not int
        or type(positive) is not int
        or not 0 <= positive <= games
        or games <= 0
    ):
        raise Stage3ArtifactError(
            "within-population VALIDATION game counts are invalid"
        )
    physical = value["physical_consistency"]
    if type(physical) is not dict or physical.get("blocking_gate_passed") is not True:
        raise Stage3ArtifactError(
            "Stage 3 artifacts require a passing physical-validity gate"
        )


def model_manifest_without_weights(
    *,
    population_id: str,
    population_identity: str,
    raw_corpus_identity: str,
    dataset_identity: str,
    inventory: dict[str, object],
    result,
    runtime: dict[str, object],
) -> dict[str, object]:
    """weights digestを除くStage 3 model manifestを構成する。"""
    config = result.config
    validation = result.validation
    return {
        "artifact_schema_version": MODEL_ARTIFACT_SCHEMA_VERSION,
        "pilot_role": PILOT_ROLE,
        "candidate": CANDIDATE.value,
        "reference_arm_id": REFERENCE_ARM_ID,
        "feature_semantics_id": FEATURE_SEMANTICS_ID,
        "feature_dimension": FEATURE_DIM,
        "sequence_semantics_id": SEQUENCE_SEMANTICS_ID,
        "model_config": model_config(CANDIDATE),
        "parameter_count": result.parameter_count,
        "training_population_id": population_id,
        "training_population_identity": population_identity,
        "raw_corpus_identity": raw_corpus_identity,
        "dataset_identity": dataset_identity,
        "inventory_identity": inventory["inventory_identity"],
        "bptt_policy": inventory["bptt_policy"],
        "training_config": {
            "seed": config.seed,
            "dataloader_seed": config.dataloader_seed,
            "learning_rate": config.learning_rate,
            "weight_decay": config.weight_decay,
            "max_epochs": config.max_epochs,
            "patience": config.patience,
            "workers": config.workers,
            "deterministic_algorithms": config.deterministic_algorithms,
            "torch_threads": config.torch_threads,
        },
        "checkpoint_selection_rule": CHECKPOINT_SELECTION_RULE,
        "selected_epoch": result.selected_epoch,
        "loss_history": [
            {
                "epoch": value.epoch,
                "train_mse": value.train_mse,
                "validation_mae": value.validation_mae,
            }
            for value in result.history
        ],
        "training_wall_clock_seconds": result.training_wall_clock_seconds,
        "peak_process_ram_bytes": result.peak_process_ram_bytes,
        "inference_samples_per_second": (
            result.inference_throughput.samples_per_second
        ),
        "self_rollout_failure_count": 0,
        "within_population_validation": {
            "sequential_validation_mae": validation.metrics.per_tile_mae,
            "conditional_uniform_validation_mae": (
                validation.snapshot_metrics.per_tile_mae
            ),
            "delta_mae_vs_conditional_uniform": validation.delta_mae,
            "positive_game_count": validation.positive_game_count,
            "validation_game_count": len(validation.per_game),
            "physical_consistency": dict(validation.physical_consistency),
        },
        "runtime": runtime,
        "test_partition_evaluated": False,
    }


def validate_model_manifest(value: object) -> dict[str, object]:
    if type(value) is not dict:
        raise Stage3ArtifactError("manifest must be an object")
    if value.get("artifact_schema_version") != MODEL_ARTIFACT_SCHEMA_VERSION:
        raise Stage3ArtifactError("artifact schema version differs")
    if value.get("pilot_role") != PILOT_ROLE:
        raise Stage3ArtifactError("Stage 3 artifacts are development-only")
    if value.get("candidate") != CANDIDATE.value:
        raise Stage3ArtifactError("Stage 3 locks the S2 candidate family")
    if value.get("feature_semantics_id") != FEATURE_SEMANTICS_ID:
        raise Stage3ArtifactError("feature semantics identity differs")
    if value.get("sequence_semantics_id") != SEQUENCE_SEMANTICS_ID:
        raise Stage3ArtifactError("sequence semantics identity differs")
    if value.get("parameter_count") != S2_PARAMETER_COUNT:
        raise Stage3ArtifactError("locked S2 parameter count differs")
    if value.get("model_config") != model_config(CANDIDATE):
        raise Stage3ArtifactError("locked S2 model config differs")
    if value.get("test_partition_evaluated") is not False:
        raise Stage3ArtifactError("Stage 3 must not evaluate a TEST partition")
    if value.get("reference_arm_id") != REFERENCE_ARM_ID:
        raise Stage3ArtifactError("Stage 3 reference arm identity differs")
    if value.get("feature_dimension") != FEATURE_DIM:
        raise Stage3ArtifactError("locked feature dimension differs")
    if value.get("checkpoint_selection_rule") != CHECKPOINT_SELECTION_RULE:
        raise Stage3ArtifactError("checkpoint selection rule differs")
    if value.get("self_rollout_failure_count") != 0:
        raise Stage3ArtifactError(
            "Stage 3 artifacts require zero self-rollout failures"
        )
    if value.get("training_config") != _locked_training_config_value():
        raise Stage3ArtifactError(
            "training config differs from the locked Phase 8 FORMAL_TRAINING_CONFIG"
        )
    runtime = value.get("runtime")
    if type(runtime) is not dict:
        raise Stage3ArtifactError("execution runtime record is missing")
    if runtime.get("device") != "cpu" or runtime.get("cuda_available") is not False:
        raise Stage3ArtifactError("Stage 3 pilot execution is CPU-only")
    if runtime.get("torch_thread_count") != 1:
        raise Stage3ArtifactError("Stage 3 pilot execution is single-threaded")
    revisions = runtime.get("execution_source_revisions")
    if type(revisions) is not dict or set(revisions) != {
        "lisjong",
        "lisjong_engine",
        "lisjong_arena",
    }:
        raise Stage3ArtifactError("execution source revisions are incomplete")
    for name, revision in sorted(revisions.items()):
        if revision is None:
            continue
        if (
            type(revision) is not str
            or len(revision) != 40
            or any(character not in "0123456789abcdef" for character in revision)
        ):
            raise Stage3ArtifactError(f"{name} execution revision is not a commit SHA")
    # `fully_resolved`はcorpus側provenanceのenforcement対象であり、Phase 4
    # persistenceと`validate_population_manifest()`がそちらをfail closedにする。
    # execution runtimeはeditable installでも記録できる必要があるため、ここでは
    # 解決済みであることを要求せず、**flagが記録済みrevisionと矛盾しないこと**
    # だけを固定する。解決していないのにresolvedと主張するmanifestは拒否する。
    resolved = all(revision is not None for revision in revisions.values())
    if runtime.get("execution_source_revisions_fully_resolved") is not resolved:
        raise Stage3ArtifactError(
            "execution source revision resolution flag contradicts its revisions"
        )
    for name in ("raw_corpus_identity", "dataset_identity", "inventory_identity"):
        _digest(value.get(name), name)
    _digest(value.get("training_population_identity"), "training_population_identity")
    bptt = value.get("bptt_policy")
    if type(bptt) is not dict or set(bptt) != {"mode", "truncation_length"}:
        raise Stage3ArtifactError("bound BPTT policy fields are not exact")
    history = value.get("loss_history")
    if type(history) is not list or not history:
        raise Stage3ArtifactError("loss_history must be non-empty")
    selected = value.get("selected_epoch")
    if type(selected) is not int or selected <= 0:
        raise Stage3ArtifactError("selected_epoch must be positive")
    if any(
        type(row) is not dict
        or set(row) != {"epoch", "train_mse", "validation_mae"}
        or type(row["validation_mae"]) not in (int, float)
        for row in history
    ):
        raise Stage3ArtifactError("loss_history rows are not exact")
    epochs = [row["epoch"] for row in history]
    if epochs != list(range(1, len(history) + 1)):
        raise Stage3ArtifactError("loss_history epochs must be contiguous from one")
    if selected != _selected_epoch_from_history(history):
        raise Stage3ArtifactError(
            "selected checkpoint differs from the locked Phase 8 checkpoint rule"
        )
    _validate_within_population_validation(value.get("within_population_validation"))
    return value


def save_model_artifact(
    destination: str | Path, model, manifest_without_weight_fields: dict[str, object]
) -> LoadedStage3Model:
    """state_dictとmanifestをatomicに一度だけpublishする。"""
    import torch

    destination = Path(destination)
    if destination.exists():
        raise FileExistsError(f"destination already exists: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(
        prefix=f".{destination.name}-staging-", dir=destination.parent
    ) as staging_name:
        staging = Path(staging_name)
        weights_path = staging / WEIGHTS_FILENAME
        torch.save(model.state_dict(), weights_path)
        weights = weights_path.read_bytes()
        manifest = dict(manifest_without_weight_fields)
        manifest["weights_bytes"] = len(weights)
        manifest["weights_sha256"] = _sha256(weights)
        validate_model_manifest(manifest)
        (staging / MANIFEST_FILENAME).write_bytes(canonical_json_bytes(manifest))
        state_dict = torch.load(weights_path, weights_only=True, map_location="cpu")
        if set(state_dict) != set(model.state_dict()) or any(
            not torch.equal(state_dict[name], model.state_dict()[name])
            for name in state_dict
        ):
            raise Stage3ArtifactError("staged state_dict readback differs")
        Path(staging).rename(destination)
    return load_model_artifact(destination)


def load_model_artifact(destination: str | Path) -> LoadedStage3Model:
    import torch

    destination = Path(destination)
    if {path.name for path in destination.iterdir()} != {
        MANIFEST_FILENAME,
        WEIGHTS_FILENAME,
    }:
        raise Stage3ArtifactError("artifact contains missing or extra files")
    manifest_bytes = (destination / MANIFEST_FILENAME).read_bytes()
    manifest = validate_model_manifest(json.loads(manifest_bytes))
    if canonical_json_bytes(manifest) != manifest_bytes:
        raise Stage3ArtifactError("manifest bytes are not canonical JSON")
    weights = (destination / WEIGHTS_FILENAME).read_bytes()
    if len(weights) != manifest["weights_bytes"]:
        raise Stage3ArtifactError("weights byte count differs")
    if _sha256(weights) != _digest(manifest["weights_sha256"], "weights_sha256"):
        raise Stage3ArtifactError("weights digest differs")
    state_dict = torch.load(
        destination / WEIGHTS_FILENAME, weights_only=True, map_location="cpu"
    )
    return LoadedStage3Model(manifest, state_dict, len(weights))


def load_model(destination: str | Path):
    """artifactからlocked S2 modelをrestoreする。任意codeは復元しない。"""
    from lisjong_arena.phase8_sequential.model import create_model, parameter_count

    loaded = load_model_artifact(destination)
    model = create_model(CANDIDATE)
    model.load_state_dict(loaded.state_dict, strict=True)
    if parameter_count(model) != loaded.manifest["parameter_count"]:
        raise Stage3ArtifactError("restored model parameter count differs")
    return model, loaded.manifest


def validate_result_value(value: object) -> dict[str, object]:
    """Stage 3 result artifactのprotocol semanticsをfail closedで検証する。

    schema versionとself-consistent digestだけでは、3 x 3 matrixが空のartifact
    すらvalidになってしまう。Issue #131がresultへ要求する
    development-only / TESTなし / fixed candidate / conditional-uniform
    reference arm / 完全な3 x 3 matrixを、artifact contractとして固定する。
    """
    if type(value) is not dict:
        raise Stage3ArtifactError("result must be an object")
    if value.get("result_schema_version") != RESULT_SCHEMA_VERSION:
        raise Stage3ArtifactError("result schema version differs")
    if value.get("pilot_role") != PILOT_ROLE:
        raise Stage3ArtifactError("Stage 3 results are development-only")
    if value.get("candidate") != CANDIDATE.value:
        raise Stage3ArtifactError("Stage 3 locks the S2 candidate family")
    if value.get("reference_arm_id") != REFERENCE_ARM_ID:
        raise Stage3ArtifactError("Stage 3 reference arm identity differs")
    if value.get("test_partition_evaluated") is not False:
        raise Stage3ArtifactError("Stage 3 must not evaluate a TEST partition")
    if value.get("accumulated_with_stage2_formal_holdout") is not False:
        raise Stage3ArtifactError(
            "Stage 3 pilot evidence must not be accumulated with Stage 2 formal holdout"
        )
    populations = value.get("populations")
    if type(populations) is not dict or tuple(sorted(populations)) != POPULATION_IDS:
        raise Stage3ArtifactError("result must describe exactly populations A, B and C")
    identities = {}
    for population_id in POPULATION_IDS:
        entry = populations[population_id]
        if type(entry) is not dict:
            raise Stage3ArtifactError(f"population {population_id} entry is invalid")
        for name in (
            "population_identity",
            "raw_corpus_identity",
            "dataset_identity",
        ):
            _digest(entry.get(name), f"{population_id}.{name}")
        identities[population_id] = entry
    dataset_identities = {entry["dataset_identity"] for entry in identities.values()}
    if len(dataset_identities) != len(POPULATION_IDS):
        raise Stage3ArtifactError("population dataset identities must be distinct")
    cells = value.get("cross_population_matrix")
    if type(cells) is not list or len(cells) != len(POPULATION_IDS) ** 2:
        raise Stage3ArtifactError("result must contain a complete 3 x 3 matrix")
    seen = set()
    for cell in cells:
        if type(cell) is not dict:
            raise Stage3ArtifactError("matrix cell is invalid")
        training = cell.get("training_population_id")
        validation = cell.get("validation_population_id")
        if training not in POPULATION_IDS or validation not in POPULATION_IDS:
            raise Stage3ArtifactError("matrix cell names an unknown population")
        if (training, validation) in seen:
            raise Stage3ArtifactError(
                "matrix contains a duplicated train/validation pair"
            )
        seen.add((training, validation))
        if (
            cell.get("training_population_identity")
            != identities[training]["population_identity"]
            or cell.get("validation_population_identity")
            != identities[validation]["population_identity"]
            or cell.get("validation_dataset_identity")
            != identities[validation]["dataset_identity"]
        ):
            raise Stage3ArtifactError(
                "matrix cell identities differ from the declared populations"
            )
        sequential = cell.get("sequential_validation_mae")
        baseline = cell.get("conditional_uniform_validation_mae")
        delta = cell.get("delta_mae_vs_conditional_uniform")
        if any(
            type(item) not in (int, float) for item in (sequential, baseline, delta)
        ):
            raise Stage3ArtifactError("matrix cell metrics are invalid")
        if abs((baseline - sequential) - delta) > 1e-12:
            raise Stage3ArtifactError(
                "matrix cell Delta is not the conditional-uniform minus sequential "
                "difference"
            )
        physical = cell.get("physical_consistency")
        if type(physical) is not dict or "blocking_gate_passed" not in physical:
            raise Stage3ArtifactError("matrix cell lacks a physical-validity record")
    if len(seen) != len(POPULATION_IDS) ** 2:
        raise Stage3ArtifactError("matrix does not cover every train/validation pair")
    return value


def save_result(destination: str | Path, value: dict[str, object]) -> Path:
    """Stage 3 pilot resultをcanonical JSONとして一度だけ書く。"""
    destination = Path(destination)
    if destination.exists():
        raise FileExistsError(f"destination already exists: {destination}")
    validate_result_value(value)
    destination.parent.mkdir(parents=True, exist_ok=True)
    payload = dict(value)
    payload["result_identity"] = _sha256(canonical_json_bytes(payload))
    destination.write_bytes(canonical_json_bytes(payload))
    return destination


def load_result(destination: str | Path) -> dict[str, object]:
    data = Path(destination).read_bytes()
    value = json.loads(data)
    if canonical_json_bytes(value) != data:
        raise Stage3ArtifactError("result bytes are not canonical JSON")
    identity = value.pop("result_identity", None)
    expected = _sha256(canonical_json_bytes(value))
    value["result_identity"] = identity
    if identity != expected:
        raise Stage3ArtifactError("result logical identity differs")
    without_identity = {
        name: item for name, item in value.items() if name != "result_identity"
    }
    validate_result_value(without_identity)
    return value


__all__ = [
    "CHECKPOINT_SELECTION_RULE",
    "MANIFEST_FILENAME",
    "MODEL_ARTIFACT_SCHEMA_VERSION",
    "RESULT_SCHEMA_VERSION",
    "WEIGHTS_FILENAME",
    "LoadedStage3Model",
    "Stage3ArtifactError",
    "execution_runtime_value",
    "load_model",
    "load_model_artifact",
    "load_result",
    "model_manifest_without_weights",
    "save_model_artifact",
    "save_result",
    "validate_model_manifest",
    "validate_result_value",
]
