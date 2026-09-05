"""Arena #148 model and result artifacts。

Phase 8の`phase8_sequential.artifact`はPhase 8 formal population / locked
dataset identity / `SNAPSHOT_VALIDATION_MAE`へbindされたvalidatorであり、
`stage3_entry_gate.artifact`は#131のhistorical development populationへbind
されたvalidatorである。どちらのconstantsもvalidatorも本pilotのために書き換え
ないため、mix pilotは別schema / 別identityのartifactを持つ。

artifactはstate_dictだけを保存し、factory / callable / 任意codeを保存・復元
しない。既存destinationを上書きせず、内部矛盾はload時にfail closedする。
weightsとgenerated resultはGit repositoryへcommitしない。
"""

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory

from lisjong_arena.phase4_raw_corpus.codec import canonical_json_bytes
from lisjong_arena.phase6_snapshot.feature import FEATURE_SEMANTICS_ID
from lisjong_arena.phase6_snapshot.tensor import FEATURE_DIM
from lisjong_arena.phase8_sequential.model import S2_PARAMETER_COUNT, model_config
from lisjong_arena.phase8_sequential.protocol import (
    SEQUENCE_SEMANTICS_ID,
    checkpoint_improves,
)
from lisjong_arena.stage3_entry_gate.artifact import (
    CHECKPOINT_SELECTION_RULE,
    execution_runtime_value,
)
from lisjong_arena.stage3_mix_pilot.comparison import (
    MixComparisonError,
    compare_against_control,
)
from lisjong_arena.stage3_mix_pilot.experiment import CANDIDATE, REFERENCE_ARM_ID
from lisjong_arena.stage3_mix_pilot.protocol import (
    ARM_IDS,
    CLEAR_REGRESSION,
    CONTROL_ARM_ID,
    MODEL_ARTIFACT_SCHEMA_VERSION,
    NO_CLEAR_REGRESSION,
    OUTCOMES,
    PILOT_ROLE,
    RESULT_SCHEMA_VERSION,
    SELECTION_RULE,
)
from lisjong_arena.stage3_mix_pilot.result import (
    MixResultError,
    arm_manifest_view,
    classify,
    selected_recipe,
)

MANIFEST_FILENAME = "manifest.json"
WEIGHTS_FILENAME = "weights.pt"
_SHA256_LENGTH = 64


class MixArtifactError(ValueError):
    """mix pilot artifact contract / persistence violation。"""


@dataclass(frozen=True, slots=True)
class LoadedMixModel:
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
        raise MixArtifactError(f"{name} must be a lowercase SHA-256")
    return value


def _locked_training_config_value() -> dict[str, object]:
    """Phase 8 `FORMAL_TRAINING_CONFIG`のcanonical value表現。

    3 armはこのconfigをexactに共有する。armごとに結果を見てhyperparameterを
    変えないことがこのpilotのprotocol invariantである。
    """
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


def _validate_within_arm_validation(value: object) -> None:
    if type(value) is not dict:
        raise MixArtifactError("within-arm VALIDATION record is missing")
    expected = {
        "sequential_validation_mae",
        "conditional_uniform_validation_mae",
        "delta_mae_vs_conditional_uniform",
        "positive_game_count",
        "validation_game_count",
        "physical_consistency",
    }
    if set(value) != expected:
        raise MixArtifactError("within-arm VALIDATION fields are not exact")
    sequential = value["sequential_validation_mae"]
    baseline = value["conditional_uniform_validation_mae"]
    delta = value["delta_mae_vs_conditional_uniform"]
    if any(type(item) not in (int, float) for item in (sequential, baseline, delta)):
        raise MixArtifactError("within-arm VALIDATION metrics are invalid")
    if abs((baseline - sequential) - delta) > 1e-12:
        raise MixArtifactError(
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
        raise MixArtifactError("within-arm VALIDATION game counts are invalid")
    physical = value["physical_consistency"]
    if type(physical) is not dict or physical.get("blocking_gate_passed") is not True:
        raise MixArtifactError(
            "mix pilot artifacts require a passing physical-validity gate"
        )


def model_manifest_without_weights(
    *,
    arm_id: str,
    population_identity: str,
    raw_corpus_identity: str,
    dataset_identity: str,
    inventory: dict[str, object],
    result,
    runtime: dict[str, object],
) -> dict[str, object]:
    """weights digestを除くmix pilot model manifestを構成する。"""
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
        "training_arm_id": arm_id,
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
        "inference_samples_per_second": result.inference_throughput.samples_per_second,
        "self_rollout_failure_count": 0,
        "within_arm_validation": {
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
        raise MixArtifactError("manifest must be an object")
    if value.get("artifact_schema_version") != MODEL_ARTIFACT_SCHEMA_VERSION:
        raise MixArtifactError("artifact schema version differs")
    if value.get("pilot_role") != PILOT_ROLE:
        raise MixArtifactError("mix pilot artifacts are development-only")
    if value.get("candidate") != CANDIDATE.value:
        raise MixArtifactError("the mix pilot locks the S2 candidate family")
    if value.get("feature_semantics_id") != FEATURE_SEMANTICS_ID:
        raise MixArtifactError("feature semantics identity differs")
    if value.get("sequence_semantics_id") != SEQUENCE_SEMANTICS_ID:
        raise MixArtifactError("sequence semantics identity differs")
    if value.get("parameter_count") != S2_PARAMETER_COUNT:
        raise MixArtifactError("locked S2 parameter count differs")
    if value.get("model_config") != model_config(CANDIDATE):
        raise MixArtifactError("locked S2 model config differs")
    if value.get("test_partition_evaluated") is not False:
        raise MixArtifactError("the mix pilot must not evaluate a TEST partition")
    if value.get("reference_arm_id") != REFERENCE_ARM_ID:
        raise MixArtifactError("reference arm identity differs")
    if value.get("feature_dimension") != FEATURE_DIM:
        raise MixArtifactError("locked feature dimension differs")
    if value.get("checkpoint_selection_rule") != CHECKPOINT_SELECTION_RULE:
        raise MixArtifactError("checkpoint selection rule differs")
    if value.get("self_rollout_failure_count") != 0:
        raise MixArtifactError("mix pilot artifacts require zero self-rollout failures")
    if value.get("training_config") != _locked_training_config_value():
        raise MixArtifactError(
            "training config differs from the locked Phase 8 FORMAL_TRAINING_CONFIG"
        )
    if value.get("training_arm_id") not in ARM_IDS:
        raise MixArtifactError("the model does not name a locked mix pilot arm")
    runtime = value.get("runtime")
    if type(runtime) is not dict:
        raise MixArtifactError("execution runtime record is missing")
    if runtime.get("device") != "cpu" or runtime.get("cuda_available") is not False:
        raise MixArtifactError("mix pilot execution is CPU-only")
    if runtime.get("torch_thread_count") != 1:
        raise MixArtifactError("mix pilot execution is single-threaded")
    revisions = runtime.get("execution_source_revisions")
    if type(revisions) is not dict or set(revisions) != {
        "lisjong",
        "lisjong_engine",
        "lisjong_arena",
    }:
        raise MixArtifactError("execution source revisions are incomplete")
    for name, revision in sorted(revisions.items()):
        if revision is None:
            continue
        if (
            type(revision) is not str
            or len(revision) != 40
            or any(character not in "0123456789abcdef" for character in revision)
        ):
            raise MixArtifactError(f"{name} execution revision is not a commit SHA")
    # `fully_resolved`はcorpus側provenanceのenforcement対象であり、Phase 4
    # persistenceと`validate_population_manifest()`がそちらをfail closedにする。
    # execution runtimeはeditable installでも記録できる必要があるため、ここでは
    # 解決済みであることを要求せず、flagが記録済みrevisionと矛盾しないことだけ
    # を固定する。
    resolved = all(revision is not None for revision in revisions.values())
    if runtime.get("execution_source_revisions_fully_resolved") is not resolved:
        raise MixArtifactError(
            "execution source revision resolution flag contradicts its revisions"
        )
    for name in ("raw_corpus_identity", "dataset_identity", "inventory_identity"):
        _digest(value.get(name), name)
    _digest(value.get("training_population_identity"), "training_population_identity")
    bptt = value.get("bptt_policy")
    if type(bptt) is not dict or set(bptt) != {"mode", "truncation_length"}:
        raise MixArtifactError("bound BPTT policy fields are not exact")
    history = value.get("loss_history")
    if type(history) is not list or not history:
        raise MixArtifactError("loss_history must be non-empty")
    selected = value.get("selected_epoch")
    if type(selected) is not int or selected <= 0:
        raise MixArtifactError("selected_epoch must be positive")
    if any(
        type(row) is not dict
        or set(row) != {"epoch", "train_mse", "validation_mae"}
        or type(row["validation_mae"]) not in (int, float)
        for row in history
    ):
        raise MixArtifactError("loss_history rows are not exact")
    epochs = [row["epoch"] for row in history]
    if epochs != list(range(1, len(history) + 1)):
        raise MixArtifactError("loss_history epochs must be contiguous from one")
    if selected != _selected_epoch_from_history(history):
        raise MixArtifactError(
            "selected checkpoint differs from the locked Phase 8 checkpoint rule"
        )
    _validate_within_arm_validation(value.get("within_arm_validation"))
    return value


def save_model_artifact(
    destination: str | Path, model, manifest_without_weight_fields: dict[str, object]
) -> LoadedMixModel:
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
            raise MixArtifactError("staged state_dict readback differs")
        Path(staging).rename(destination)
    return load_model_artifact(destination)


def load_model_artifact(destination: str | Path) -> LoadedMixModel:
    import torch

    destination = Path(destination)
    if {path.name for path in destination.iterdir()} != {
        MANIFEST_FILENAME,
        WEIGHTS_FILENAME,
    }:
        raise MixArtifactError("artifact contains missing or extra files")
    manifest_bytes = (destination / MANIFEST_FILENAME).read_bytes()
    manifest = validate_model_manifest(json.loads(manifest_bytes))
    if canonical_json_bytes(manifest) != manifest_bytes:
        raise MixArtifactError("manifest bytes are not canonical JSON")
    weights = (destination / WEIGHTS_FILENAME).read_bytes()
    if len(weights) != manifest["weights_bytes"]:
        raise MixArtifactError("weights byte count differs")
    if _sha256(weights) != _digest(manifest["weights_sha256"], "weights_sha256"):
        raise MixArtifactError("weights digest differs")
    state_dict = torch.load(
        destination / WEIGHTS_FILENAME, weights_only=True, map_location="cpu"
    )
    return LoadedMixModel(manifest, state_dict, len(weights))


def load_model(destination: str | Path):
    """artifactからlocked S2 modelをrestoreする。任意codeは復元しない。"""
    from lisjong_arena.phase8_sequential.model import create_model, parameter_count

    loaded = load_model_artifact(destination)
    model = create_model(CANDIDATE)
    model.load_state_dict(loaded.state_dict, strict=True)
    if parameter_count(model) != loaded.manifest["parameter_count"]:
        raise MixArtifactError("restored model parameter count differs")
    return model, loaded.manifest


def _validate_matrix(value: dict, identities: dict[str, dict]) -> None:
    cells = value.get("cross_population_matrix")
    if type(cells) is not list or len(cells) != len(ARM_IDS) ** 2:
        raise MixArtifactError("result must contain a complete 3 x 3 matrix")
    seen = set()
    for cell in cells:
        if type(cell) is not dict:
            raise MixArtifactError("matrix cell is invalid")
        training = cell.get("training_population_id")
        validation = cell.get("validation_population_id")
        if training not in ARM_IDS or validation not in ARM_IDS:
            raise MixArtifactError("matrix cell names an unknown arm")
        if (training, validation) in seen:
            raise MixArtifactError("matrix contains a duplicated train/validation pair")
        seen.add((training, validation))
        if (
            cell.get("training_population_identity")
            != identities[training]["population_identity"]
            or cell.get("validation_population_identity")
            != identities[validation]["population_identity"]
            or cell.get("validation_dataset_identity")
            != identities[validation]["dataset_identity"]
        ):
            raise MixArtifactError(
                "matrix cell identities differ from the declared arms"
            )
        sequential = cell.get("sequential_validation_mae")
        baseline = cell.get("conditional_uniform_validation_mae")
        delta = cell.get("delta_mae_vs_conditional_uniform")
        if any(
            type(item) not in (int, float) for item in (sequential, baseline, delta)
        ):
            raise MixArtifactError("matrix cell metrics are invalid")
        if abs((baseline - sequential) - delta) > 1e-12:
            raise MixArtifactError(
                "matrix cell Delta is not the conditional-uniform minus sequential "
                "difference"
            )
        physical = cell.get("physical_consistency")
        if type(physical) is not dict or "blocking_gate_passed" not in physical:
            raise MixArtifactError("matrix cell lacks a physical-validity record")
        if type(cell.get("depth_diagnostics")) is not list:
            raise MixArtifactError("matrix cell lacks depth-stratified diagnostics")
        if type(cell.get("per_game")) is not list or not cell["per_game"]:
            raise MixArtifactError("matrix cell lacks per-hanchan measurements")
    if len(seen) != len(ARM_IDS) ** 2:
        raise MixArtifactError("matrix does not cover every train/validation pair")


def _validate_comparisons(value: dict) -> list:
    """paired comparisonを、matrixのper-hanchan measurementから再導出して照合する。

    符号の整合だけを見ると、self-consistentに捏造したcomparison（例えばpooled
    deltaとintervalだけを都合よく書き換えたrow）が通ってしまう。したがって
    recorded matrix cellの`per_game`から`compare_against_control()`を再実行し、
    pooled delta、per-hanchan delta、deterministic bootstrap interval、
    classificationまでexact一致を要求する。
    """
    comparisons = value.get("paired_comparisons")
    candidates = tuple(name for name in ARM_IDS if name != CONTROL_ARM_ID)
    if type(comparisons) is not list or len(comparisons) != len(candidates) * len(
        ARM_IDS
    ):
        raise MixArtifactError(
            "result must compare every candidate arm on every evaluation population"
        )
    cell_by_pair = {
        (cell["training_population_id"], cell["validation_population_id"]): cell
        for cell in value["cross_population_matrix"]
    }
    expected_rows = []
    for candidate in candidates:
        for validation in ARM_IDS:
            try:
                expected_rows.append(
                    compare_against_control(
                        candidate_arm_id=candidate,
                        validation_arm_id=validation,
                        control_cell=cell_by_pair[(CONTROL_ARM_ID, validation)],
                        candidate_cell=cell_by_pair[(candidate, validation)],
                    )
                )
            except MixComparisonError as exc:
                raise MixArtifactError(
                    f"paired comparison {candidate}/{validation} cannot be "
                    f"re-derived from the recorded matrix: {exc}"
                ) from exc
    seen = set()
    for row in comparisons:
        if type(row) is not dict:
            raise MixArtifactError("paired comparison row is invalid")
        candidate = row.get("candidate_arm_id")
        validation = row.get("validation_population_id")
        if candidate not in candidates or validation not in ARM_IDS:
            raise MixArtifactError("paired comparison names an unknown arm")
        if (candidate, validation) in seen:
            raise MixArtifactError("paired comparison pair is duplicated")
        seen.add((candidate, validation))
        if row.get("control_arm_id") != CONTROL_ARM_ID:
            raise MixArtifactError("paired comparisons must use the control arm")
        if row.get("classification") not in (CLEAR_REGRESSION, NO_CLEAR_REGRESSION):
            raise MixArtifactError("unknown paired comparison classification")
    by_pair = {
        (row["candidate_arm_id"], row["validation_population_id"]): row
        for row in comparisons
    }
    for expected in expected_rows:
        key = (expected["candidate_arm_id"], expected["validation_population_id"])
        recorded = by_pair.get(key)
        if recorded is None:
            raise MixArtifactError(
                f"paired comparison {key[0]}/{key[1]} is missing from the result"
            )
        if recorded != expected:
            raise MixArtifactError(
                f"paired comparison {key[0]}/{key[1]} differs from the comparison "
                "re-derived from the recorded per-hanchan measurements"
            )
    return expected_rows


def _validate_classification(value: dict, comparisons: list) -> None:
    """outcome / gates / selected_recipeを、recorded evidenceから再導出して照合する。

    outcomeをartifactのfreeなstringにしない。arm entryが持つprovenance /
    coverage / dataset retention / cost / population plan / source attribution と、
    recorded matrix、再導出済みpaired comparisonでlocked selection ruleをもう一度
    走らせ、outcome、その理由、gate detail、locked recipeがexactに一致することを
    要求する。

    これがないと、`MIX LOCKED`を名乗りながら`gates`が空で`selected_recipe`が
    `null`のartifactがwell-formedとして通ってしまう。
    """
    try:
        views = {
            arm_id: arm_manifest_view(arm_id, value["arms"][arm_id])
            for arm_id in ARM_IDS
        }
        outcome, reasons, gates = classify(
            views, value["cross_population_matrix"], comparisons
        )
    except MixResultError as exc:
        raise MixArtifactError(
            f"the recorded evidence cannot be classified: {exc}"
        ) from exc
    if value["outcome"] != outcome:
        raise MixArtifactError(
            f"recorded outcome {value['outcome']!r} differs from the outcome "
            f"re-derived from the recorded evidence ({outcome!r})"
        )
    if value["outcome_reasons"] != list(reasons):
        raise MixArtifactError(
            "recorded outcome reasons differ from the re-derived reasons"
        )
    if value.get("gates") != gates:
        raise MixArtifactError(
            "recorded gate detail differs from the gates re-derived from the "
            "recorded evidence"
        )
    if value.get("selected_recipe") != selected_recipe(outcome, views):
        raise MixArtifactError(
            "recorded selected recipe differs from the recipe the locked "
            "selection rule derives for this outcome"
        )


def validate_result_value(value: object) -> dict[str, object]:
    """mix pilot result artifactのprotocol semanticsをfail closedで検証する。

    schema versionとself-consistent digestだけでは、matrixが空のartifactすら
    validになってしまう。Issue #148がresultへ要求する development-only /
    TESTなし / fixed candidate / 完全な3 x 3 matrix / 全candidateのpaired
    comparison / exhaustive outcomeを、artifact contractとして固定する。
    """
    if type(value) is not dict:
        raise MixArtifactError("result must be an object")
    if value.get("result_schema_version") != RESULT_SCHEMA_VERSION:
        raise MixArtifactError("result schema version differs")
    if value.get("pilot_role") != PILOT_ROLE:
        raise MixArtifactError("mix pilot results are development-only")
    if value.get("candidate") != CANDIDATE.value:
        raise MixArtifactError("the mix pilot locks the S2 candidate family")
    if value.get("reference_arm_id") != REFERENCE_ARM_ID:
        raise MixArtifactError("reference arm identity differs")
    if value.get("test_partition_evaluated") is not False:
        raise MixArtifactError("the mix pilot must not evaluate a TEST partition")
    if value.get("accumulated_with_historical_evidence") is not False:
        raise MixArtifactError(
            "mix pilot evidence must not be accumulated with #131 / #146 / Stage 2 "
            "formal evidence"
        )
    if value.get("selection_rule") != SELECTION_RULE:
        raise MixArtifactError("result selection rule differs from the locked rule")
    if value.get("outcome") not in OUTCOMES:
        raise MixArtifactError("result outcome is not an exhaustive outcome")
    reasons = value.get("outcome_reasons")
    if type(reasons) is not list or not reasons:
        raise MixArtifactError("result outcome requires recorded reasons")
    arms = value.get("arms")
    if type(arms) is not dict or tuple(sorted(arms)) != ARM_IDS:
        raise MixArtifactError("result must describe exactly arms A, B and C")
    identities = {}
    for arm_id in ARM_IDS:
        entry = arms[arm_id]
        if type(entry) is not dict:
            raise MixArtifactError(f"arm {arm_id} entry is invalid")
        for name in ("population_identity", "raw_corpus_identity", "dataset_identity"):
            _digest(entry.get(name), f"{arm_id}.{name}")
        identities[arm_id] = entry
    dataset_identities = {entry["dataset_identity"] for entry in identities.values()}
    if len(dataset_identities) != len(ARM_IDS):
        raise MixArtifactError("arm dataset identities must be distinct")
    _validate_matrix(value, identities)
    comparisons = _validate_comparisons(value)
    _validate_classification(value, comparisons)
    return value


def save_result(destination: str | Path, value: dict[str, object]) -> Path:
    """mix pilot resultをcanonical JSONとして一度だけ書く。"""
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
        raise MixArtifactError("result bytes are not canonical JSON")
    identity = value.pop("result_identity", None)
    expected = _sha256(canonical_json_bytes(value))
    value["result_identity"] = identity
    if identity != expected:
        raise MixArtifactError("result logical identity differs")
    without_identity = {
        name: item for name, item in value.items() if name != "result_identity"
    }
    validate_result_value(without_identity)
    return value


__all__ = [
    "MANIFEST_FILENAME",
    "WEIGHTS_FILENAME",
    "LoadedMixModel",
    "MixArtifactError",
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
