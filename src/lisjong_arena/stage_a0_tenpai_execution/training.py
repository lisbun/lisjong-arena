"""Locked A/T training and checkpoint artifacts for #262."""

from __future__ import annotations

import hashlib
import json
import math
import platform
import struct
import time
from dataclasses import dataclass
from pathlib import Path
from shutil import rmtree
from tempfile import mkdtemp

from lisjong_arena._artifact_io import canonical_json_text
from lisjong_arena.learned_policy_offline_q.artifact import (
    feature_block,
    vocabulary_block,
)
from lisjong_arena.learned_policy_offline_q.protocol import Split
from lisjong_arena.learned_policy_stage2.network import (
    create_model,
    masked_cross_entropy,
    parameter_count,
)
from lisjong_arena.single_round_artifact import (
    collect_execution_provenance,
    execution_provenance_to_dict,
)
from lisjong_arena.stage_a0_tenpai_protocol_lock import protocol as locked

from .data import ScientificData, ScientificSplitTensors
from .errors import StageA0CheckpointError, StageA0ExecutionError
from .protocol import (
    AUXILIARY_PARAMETER_COUNT,
    CHECKPOINT_SCHEMA_VERSION,
    EXECUTION_PROTOCOL_ID,
    EXPECTED_LOCK_B_IDENTITY,
    EXPECTED_PUBLIC_KEYS_IDENTITY,
    EXPECTED_SCIENTIFIC_SIDECAR_IDENTITY,
    POLICY_PARAMETER_COUNT,
    Arm,
    auxiliary_seed_namespace,
    require_arm,
    training_seed_namespace,
)

MANIFEST_FILENAME = "manifest.json"
WEIGHTS_FILENAME = "weights.pt"
AUXILIARY_WEIGHTS_FILENAME = "auxiliary_weights.pt"


@dataclass(frozen=True, slots=True)
class EpochRecord:
    epoch: int
    train_policy_ce: float
    train_auxiliary_bce: float | None
    validation_policy_ce: float

    def to_document(self) -> dict[str, object]:
        return {
            "epoch": self.epoch,
            "train_policy_ce": self.train_policy_ce,
            "train_auxiliary_bce": self.train_auxiliary_bce,
            "validation_policy_ce": self.validation_policy_ce,
        }


@dataclass(frozen=True, slots=True)
class TrainingResult:
    arm: Arm
    seed: int
    model: object
    auxiliary_head: object | None
    initial_policy_fingerprint: str
    history: tuple[EpochRecord, ...]
    selected_epoch: int
    selected_validation_policy_ce: float
    train_policy_ce: float
    train_auxiliary_bce: float | None
    wall_clock_seconds: float
    runtime: dict[str, object]


@dataclass(frozen=True, slots=True)
class LoadedCheckpoint:
    path: Path
    manifest: dict
    model: object
    auxiliary_head: object | None

    @property
    def arm(self) -> Arm:
        return Arm(self.manifest["arm"])

    @property
    def seed(self) -> int:
        return int(self.manifest["training_seed"])

    @property
    def identity(self) -> str:
        return self.manifest["checkpoint_identity"]

    @property
    def weights_sha256(self) -> str:
        return self.manifest["weights_sha256"]


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _hash_field(hasher, payload: bytes) -> None:
    hasher.update(len(payload).to_bytes(8, "big"))
    hasher.update(payload)


def _state_fingerprint(state: dict[str, object]) -> str:
    """Canonical semantic identity for a tensor state_dict.

    Do not use torch.save bytes here: its container serialization is not a
    canonical representation of equal tensor content.
    """
    import torch

    hasher = hashlib.sha256()
    for name in sorted(state):
        tensor = state[name]
        if not isinstance(tensor, torch.Tensor):
            raise StageA0ExecutionError("Policy state contains a non-tensor value")
        canonical = tensor.detach().cpu().contiguous()
        shape = struct.pack(">Q", canonical.dim()) + b"".join(
            struct.pack(">q", int(size)) for size in canonical.shape
        )
        raw = bytes(canonical.reshape(-1).view(torch.uint8))
        _hash_field(hasher, name.encode("utf-8"))
        _hash_field(hasher, str(canonical.dtype).encode("ascii"))
        _hash_field(hasher, shape)
        _hash_field(hasher, raw)
    return hasher.hexdigest()


def _manifest_identity(manifest: dict[str, object]) -> str:
    logical = {
        name: value for name, value in manifest.items() if name != "checkpoint_identity"
    }
    return _sha256(canonical_json_text(logical).encode("utf-8"))


def _configure_runtime(seed: int) -> dict[str, object]:
    import torch

    training_seed_namespace(seed)
    torch.manual_seed(seed)
    torch.use_deterministic_algorithms(locked.DETERMINISTIC_ALGORITHMS)
    torch.set_num_threads(locked.TORCH_THREADS)
    return {
        "torch_version": torch.__version__,
        "torch_threads": torch.get_num_threads(),
        "deterministic_algorithms": bool(torch.are_deterministic_algorithms_enabled()),
        "cuda_available": bool(torch.cuda.is_available()),
        "python_version": platform.python_version(),
    }


def _create_auxiliary_head(seed: int):
    import torch

    torch.manual_seed(auxiliary_seed_namespace(seed))
    head = torch.nn.Linear(locked.HIDDEN_WIDTH, 3)
    if (
        sum(parameter.numel() for parameter in head.parameters())
        != AUXILIARY_PARAMETER_COUNT
    ):
        raise StageA0ExecutionError("auxiliary head parameter count drifted")
    return head


def _shared_hidden(model, features):
    return model.network[1](model.network[0](features))


def _policy_ce(model, tensors: ScientificSplitTensors) -> float:
    """Locked choice-row masked Policy CE used for checkpoint selection."""
    import torch

    selector = tensors.legal_mask.sum(dim=1) >= 2
    count = int(selector.sum())
    if count <= 0:
        raise StageA0ExecutionError("Policy CE requires at least one choice row")
    features = tensors.features[selector]
    legal_mask = tensors.legal_mask[selector]
    behavior = tensors.behavior_action_index[selector]
    model.eval()
    total = 0.0
    with torch.no_grad():
        for start in range(0, count, locked.BATCH_SIZE):
            stop = min(start + locked.BATCH_SIZE, count)
            losses = masked_cross_entropy(
                model(features[start:stop]),
                legal_mask[start:stop],
                behavior[start:stop],
            )
            total += float(losses.sum())
    return total / count


def _auxiliary_bce(model, head, tensors: ScientificSplitTensors) -> float:
    import torch

    model.eval()
    head.eval()
    total = 0.0
    eligible_count = 0
    with torch.no_grad():
        for start in range(0, tensors.row_count, locked.BATCH_SIZE):
            stop = min(start + locked.BATCH_SIZE, tensors.row_count)
            eligible = tensors.tenpai_eligible[start:stop]
            count = int(eligible.sum())
            if count == 0:
                continue
            logits = head(_shared_hidden(model, tensors.features[start:stop]))
            losses = torch.nn.functional.binary_cross_entropy_with_logits(
                logits,
                tensors.tenpai_targets[start:stop],
                reduction="none",
            )
            total += float(losses[eligible].sum())
            eligible_count += count
    if eligible_count == 0:
        raise StageA0ExecutionError("auxiliary evaluation has zero eligible cells")
    return total / eligible_count


def _clone_state(state: dict[str, object]) -> dict[str, object]:
    return {name: tensor.detach().clone() for name, tensor in state.items()}


def _assert_states_equal(
    left: dict[str, object],
    right: dict[str, object],
    *,
    label: str,
) -> None:
    import torch

    if set(left) != set(right):
        raise StageA0ExecutionError(f"{label} keys differ")
    for name in sorted(left):
        left_tensor = left[name]
        right_tensor = right[name]
        if not isinstance(left_tensor, torch.Tensor) or not isinstance(
            right_tensor, torch.Tensor
        ):
            raise StageA0ExecutionError(f"{label} contains a non-tensor value")
        if left_tensor.dtype != right_tensor.dtype:
            raise StageA0ExecutionError(f"{label} dtype differs for {name}")
        if tuple(left_tensor.shape) != tuple(right_tensor.shape):
            raise StageA0ExecutionError(f"{label} shape differs for {name}")
        if not torch.equal(left_tensor, right_tensor):
            raise StageA0ExecutionError(f"{label} tensor value differs for {name}")


def _materialize_initial_policy(
    initial_state: dict[str, object],
    *,
    initial_fingerprint: str,
):
    model = create_model()
    model.load_state_dict(_clone_state(initial_state), strict=True)
    loaded_state = model.state_dict()
    _assert_states_equal(
        initial_state,
        loaded_state,
        label="loaded initial Policy state",
    )
    if _state_fingerprint(loaded_state) != initial_fingerprint:
        raise StageA0ExecutionError("initial Policy semantic fingerprint drifted")
    return model


def _train_arm_from_initial_model(
    arm: Arm,
    seed: int,
    model,
    tensors: dict[Split, ScientificSplitTensors],
    *,
    initial_fingerprint: str,
) -> TrainingResult:
    import torch

    arm = require_arm(arm)
    runtime = _configure_runtime(seed)
    train = tensors[Split.TRAIN]
    validation = tensors[Split.VALIDATION]
    if _state_fingerprint(model.state_dict()) != initial_fingerprint:
        raise StageA0ExecutionError("A/T initial Policy state fingerprint drifted")
    head = _create_auxiliary_head(seed) if arm is Arm.T else None

    parameters = list(model.parameters())
    if head is not None:
        parameters.extend(head.parameters())
    optimizer = torch.optim.Adam(
        parameters,
        lr=locked.LEARNING_RATE,
        weight_decay=locked.WEIGHT_DECAY,
    )
    generator = torch.Generator()
    generator.manual_seed(seed)
    # Shuffle row indices, not privileged payload tensors. This keeps A/T row
    # order identical while the A arm never reads Tenpai targets at all.
    dataset = torch.utils.data.TensorDataset(
        torch.arange(train.row_count, dtype=torch.long)
    )
    loader = torch.utils.data.DataLoader(
        dataset,
        batch_size=locked.BATCH_SIZE,
        shuffle=True,
        num_workers=locked.DATALOADER_WORKERS,
        generator=generator,
        drop_last=False,
    )

    history: list[EpochRecord] = []
    best_policy_state = None
    best_auxiliary_state = None
    best_epoch = 0
    best_metric = float("inf")
    epochs_without_improvement = 0
    started = time.perf_counter()

    for epoch in range(1, locked.MAXIMUM_EPOCHS + 1):
        model.train()
        if head is not None:
            head.train()
        policy_sum = 0.0
        policy_count = 0
        auxiliary_sum = 0.0
        auxiliary_count = 0

        for (row_indices,) in loader:
            optimizer.zero_grad(set_to_none=True)
            features = train.features.index_select(0, row_indices)
            legal_mask = train.legal_mask.index_select(0, row_indices)
            behavior = train.behavior_action_index.index_select(0, row_indices)
            hidden = _shared_hidden(model, features)
            logits = model.network[2](hidden)
            policy_losses = masked_cross_entropy(logits, legal_mask, behavior)
            policy_loss = policy_losses.mean()
            loss = policy_loss

            if head is not None:
                tenpai_targets = train.tenpai_targets.index_select(0, row_indices)
                tenpai_eligible = train.tenpai_eligible.index_select(0, row_indices)
                auxiliary_logits = head(hidden)
                auxiliary_losses = torch.nn.functional.binary_cross_entropy_with_logits(
                    auxiliary_logits,
                    tenpai_targets,
                    reduction="none",
                )
                eligible_count = int(tenpai_eligible.sum())
                if eligible_count:
                    auxiliary_loss = (
                        auxiliary_losses[tenpai_eligible].sum() / eligible_count
                    )
                    loss = loss + locked.LAMBDA_TENPAI * auxiliary_loss
                    auxiliary_sum += float(
                        auxiliary_losses[tenpai_eligible].detach().sum()
                    )
                    auxiliary_count += eligible_count

            if not bool(torch.isfinite(loss)):
                raise StageA0ExecutionError("training loss became non-finite")
            loss.backward()
            optimizer.step()
            policy_sum += float(policy_losses.detach().sum())
            policy_count += int(row_indices.shape[0])

        if policy_count != train.row_count:
            raise StageA0ExecutionError("training epoch did not visit every TRAIN row")
        if head is not None and auxiliary_count == 0:
            raise StageA0ExecutionError("T epoch has zero eligible TRAIN Tenpai cells")

        validation_metric = _policy_ce(model, validation)
        train_auxiliary = None if head is None else auxiliary_sum / auxiliary_count
        history.append(
            EpochRecord(
                epoch=epoch,
                train_policy_ce=policy_sum / policy_count,
                train_auxiliary_bce=train_auxiliary,
                validation_policy_ce=validation_metric,
            )
        )

        # Strictly-less preserves the earliest epoch on an exact tie.
        if validation_metric < best_metric:
            best_metric = validation_metric
            best_epoch = epoch
            best_policy_state = _clone_state(model.state_dict())
            best_auxiliary_state = (
                None if head is None else _clone_state(head.state_dict())
            )
            epochs_without_improvement = 0
        else:
            epochs_without_improvement += 1
            if epochs_without_improvement >= locked.EARLY_STOP_PATIENCE:
                break

    if best_policy_state is None:
        raise StageA0ExecutionError("training produced no validated checkpoint")
    model.load_state_dict(best_policy_state, strict=True)
    if head is not None:
        if best_auxiliary_state is None:
            raise StageA0ExecutionError("T checkpoint lost auxiliary state")
        head.load_state_dict(best_auxiliary_state, strict=True)

    train_policy_ce = _policy_ce(model, train)
    train_auxiliary_bce = None if head is None else _auxiliary_bce(model, head, train)
    reproduced_validation = _policy_ce(model, validation)
    if abs(reproduced_validation - best_metric) > 1e-9:
        raise StageA0ExecutionError(
            "selected checkpoint does not reproduce its VALIDATION Policy CE"
        )

    return TrainingResult(
        arm=arm,
        seed=seed,
        model=model,
        auxiliary_head=head,
        initial_policy_fingerprint=initial_fingerprint,
        history=tuple(history),
        selected_epoch=best_epoch,
        selected_validation_policy_ce=best_metric,
        train_policy_ce=train_policy_ce,
        train_auxiliary_bce=train_auxiliary_bce,
        wall_clock_seconds=time.perf_counter() - started,
        runtime=runtime,
    )


def train_seed_pair(
    seed: int,
    tensors: dict[Split, ScientificSplitTensors],
) -> tuple[TrainingResult, TrainingResult]:
    """Train the locked A/T pair from one content-identical Policy initialization."""
    training_seed_namespace(seed)
    _configure_runtime(seed)
    base_model = create_model()
    initial_state = _clone_state(base_model.state_dict())
    fingerprint = _state_fingerprint(initial_state)

    a_model = _materialize_initial_policy(
        initial_state,
        initial_fingerprint=fingerprint,
    )
    t_model = _materialize_initial_policy(
        initial_state,
        initial_fingerprint=fingerprint,
    )
    _assert_states_equal(
        a_model.state_dict(),
        t_model.state_dict(),
        label="A/T initial Policy state",
    )

    a = _train_arm_from_initial_model(
        Arm.A,
        seed,
        a_model,
        tensors,
        initial_fingerprint=fingerprint,
    )
    t = _train_arm_from_initial_model(
        Arm.T,
        seed,
        t_model,
        tensors,
        initial_fingerprint=fingerprint,
    )
    if a.initial_policy_fingerprint != t.initial_policy_fingerprint:
        raise StageA0ExecutionError("A/T initialization fingerprints differ")
    return a, t


def save_checkpoint(
    destination,
    scientific: ScientificData,
    result: TrainingResult,
) -> LoadedCheckpoint:
    """Publish one immutable A/T checkpoint and strict-read it back."""
    import torch

    arm = require_arm(result.arm)
    training_seed_namespace(result.seed)
    if scientific.lock_b["lock_identity"] != EXPECTED_LOCK_B_IDENTITY:
        raise StageA0CheckpointError("checkpoint source Lock B identity drifted")

    destination = Path(destination)
    if destination.exists():
        raise FileExistsError("checkpoint destination already exists")
    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(
        mkdtemp(prefix=f".{destination.name}-staging-", dir=destination.parent)
    )
    published = False
    try:
        policy_path = staging / WEIGHTS_FILENAME
        torch.save(result.model.state_dict(), policy_path)
        policy_payload = policy_path.read_bytes()

        auxiliary_payload = None
        if arm is Arm.T:
            if result.auxiliary_head is None:
                raise StageA0CheckpointError("T result has no auxiliary head")
            auxiliary_path = staging / AUXILIARY_WEIGHTS_FILENAME
            torch.save(result.auxiliary_head.state_dict(), auxiliary_path)
            auxiliary_payload = auxiliary_path.read_bytes()
        elif result.auxiliary_head is not None:
            raise StageA0CheckpointError("A result unexpectedly has an auxiliary head")

        provenance = execution_provenance_to_dict(collect_execution_provenance())
        manifest: dict[str, object] = {
            "checkpoint_schema_version": CHECKPOINT_SCHEMA_VERSION,
            "execution_protocol_id": EXECUTION_PROTOCOL_ID,
            "lock_b_identity": EXPECTED_LOCK_B_IDENTITY,
            "arm": arm.value,
            "training_seed": result.seed,
            "dataset_identity": scientific.dataset.identity,
            "scientific_sidecar_identity": scientific.sidecar.identity,
            "public_keys_identity": scientific.public_keys.identity,
            "initial_policy_fingerprint": result.initial_policy_fingerprint,
            "feature": dict(scientific.dataset.manifest["feature"]),
            "vocabulary": dict(scientific.dataset.manifest["vocabulary"]),
            "model": {
                "model_id": locked.MODEL_ID,
                "input_dimension": locked.FEATURE_DIMENSION,
                "hidden_width": locked.HIDDEN_WIDTH,
                "policy_output_dimension": locked.VOCABULARY_SIZE,
                "activation": "relu",
                "auxiliary_head_identity": (
                    locked.AUXILIARY_HEAD_IDENTITY if arm is Arm.T else None
                ),
            },
            "training": {
                "optimizer": "Adam",
                "learning_rate": locked.LEARNING_RATE,
                "weight_decay": locked.WEIGHT_DECAY,
                "batch_size": locked.BATCH_SIZE,
                "maximum_epochs": locked.MAXIMUM_EPOCHS,
                "early_stop_patience": locked.EARLY_STOP_PATIENCE,
                "training_seed": result.seed,
                "dataloader_seed": result.seed,
                "dataloader_workers": locked.DATALOADER_WORKERS,
                "torch_threads": locked.TORCH_THREADS,
                "deterministic_algorithms": locked.DETERMINISTIC_ALGORITHMS,
                "checkpoint_rule": locked.CHECKPOINT_RULE,
                "lambda_tenpai": locked.LAMBDA_TENPAI if arm is Arm.T else 0.0,
                "class_treatment": locked.CLASS_IMBALANCE_TREATMENT,
            },
            "parameter_count": parameter_count(result.model),
            "auxiliary_parameter_count": (
                AUXILIARY_PARAMETER_COUNT if arm is Arm.T else 0
            ),
            "selected_epoch": result.selected_epoch,
            "selected_validation_policy_ce": result.selected_validation_policy_ce,
            "train_policy_ce": result.train_policy_ce,
            "train_auxiliary_bce": result.train_auxiliary_bce,
            "epoch_history": [record.to_document() for record in result.history],
            "weights_bytes": len(policy_payload),
            "weights_sha256": _sha256(policy_payload),
            "auxiliary_weights_bytes": (
                None if auxiliary_payload is None else len(auxiliary_payload)
            ),
            "auxiliary_weights_sha256": (
                None if auxiliary_payload is None else _sha256(auxiliary_payload)
            ),
            "runtime": {
                **result.runtime,
                "training_wall_clock_seconds": result.wall_clock_seconds,
            },
            "provenance": provenance,
            "protected_test_evaluated": False,
        }
        if manifest["parameter_count"] != POLICY_PARAMETER_COUNT:
            raise StageA0CheckpointError("Policy parameter count drifted")
        manifest["checkpoint_identity"] = _manifest_identity(manifest)
        (staging / MANIFEST_FILENAME).write_text(
            canonical_json_text(manifest), encoding="utf-8", newline="\n"
        )
        staging.rename(destination)
        published = True
    finally:
        if not published:
            rmtree(staging, ignore_errors=True)
    return load_checkpoint(destination)


def load_checkpoint(path) -> LoadedCheckpoint:
    """Strict-load one #262 checkpoint; no latest-file discovery or fallback."""
    import torch

    path = Path(path)
    if not path.is_dir():
        raise StageA0CheckpointError("checkpoint path is not a directory")
    manifest_path = path / MANIFEST_FILENAME
    try:
        text = manifest_path.read_text(encoding="utf-8")
        manifest = json.loads(text)
    except (OSError, json.JSONDecodeError) as error:
        raise StageA0CheckpointError("checkpoint manifest cannot be read") from error
    if type(manifest) is not dict or canonical_json_text(manifest) != text:
        raise StageA0CheckpointError("checkpoint manifest is not canonical JSON")
    if manifest.get("checkpoint_schema_version") != CHECKPOINT_SCHEMA_VERSION:
        raise StageA0CheckpointError("unsupported Stage A0 checkpoint schema")
    if manifest.get("execution_protocol_id") != EXECUTION_PROTOCOL_ID:
        raise StageA0CheckpointError("checkpoint execution protocol drifted")
    if manifest.get("lock_b_identity") != EXPECTED_LOCK_B_IDENTITY:
        raise StageA0CheckpointError("checkpoint Lock B binding drifted")
    try:
        arm = require_arm(manifest.get("arm"))
        seed = training_seed_namespace(manifest.get("training_seed"))
    except (TypeError, ValueError) as error:
        raise StageA0CheckpointError(
            "checkpoint arm/training seed is invalid"
        ) from error
    if manifest.get("dataset_identity") != locked.RETAINED_DATASET_IDENTITY:
        raise StageA0CheckpointError("checkpoint retained dataset identity drifted")
    if (
        manifest.get("scientific_sidecar_identity")
        != EXPECTED_SCIENTIFIC_SIDECAR_IDENTITY
    ):
        raise StageA0CheckpointError("checkpoint scientific sidecar identity drifted")
    if manifest.get("public_keys_identity") != EXPECTED_PUBLIC_KEYS_IDENTITY:
        raise StageA0CheckpointError("checkpoint public-key identity drifted")
    if manifest.get("feature") != feature_block():
        raise StageA0CheckpointError("checkpoint feature schema identity drifted")
    if manifest.get("vocabulary") != vocabulary_block():
        raise StageA0CheckpointError("checkpoint action vocabulary identity drifted")
    if manifest.get("parameter_count") != POLICY_PARAMETER_COUNT:
        raise StageA0CheckpointError("checkpoint Policy parameter count drifted")
    if manifest.get("auxiliary_parameter_count") != (
        AUXILIARY_PARAMETER_COUNT if arm is Arm.T else 0
    ):
        raise StageA0CheckpointError("checkpoint auxiliary parameter count drifted")
    if manifest.get("protected_test_evaluated") is not False:
        raise StageA0CheckpointError("checkpoint claims protected TEST exposure")
    if manifest.get("checkpoint_identity") != _manifest_identity(manifest):
        raise StageA0CheckpointError("checkpoint identity does not match its content")

    expected_files = {MANIFEST_FILENAME, WEIGHTS_FILENAME}
    if arm is Arm.T:
        expected_files.add(AUXILIARY_WEIGHTS_FILENAME)
    if {item.name for item in path.iterdir()} != expected_files:
        raise StageA0CheckpointError("checkpoint contains missing or extra files")

    policy_payload = (path / WEIGHTS_FILENAME).read_bytes()
    if len(policy_payload) != manifest.get("weights_bytes"):
        raise StageA0CheckpointError("Policy weights byte count differs")
    if _sha256(policy_payload) != manifest.get("weights_sha256"):
        raise StageA0CheckpointError("Policy weights sha256 differs")
    model = create_model()
    try:
        model.load_state_dict(
            torch.load(path / WEIGHTS_FILENAME, weights_only=True, map_location="cpu"),
            strict=True,
        )
    except Exception as error:
        raise StageA0CheckpointError("Policy state_dict strict load failed") from error
    if parameter_count(model) != POLICY_PARAMETER_COUNT:
        raise StageA0CheckpointError("loaded Policy parameter count drifted")
    for parameter in model.parameters():
        if not bool(torch.isfinite(parameter).all()):
            raise StageA0CheckpointError("Policy checkpoint contains non-finite values")
        parameter.requires_grad_(False)
    model.eval()

    head = None
    if arm is Arm.T:
        auxiliary_payload = (path / AUXILIARY_WEIGHTS_FILENAME).read_bytes()
        if len(auxiliary_payload) != manifest.get("auxiliary_weights_bytes"):
            raise StageA0CheckpointError("auxiliary weights byte count differs")
        if _sha256(auxiliary_payload) != manifest.get("auxiliary_weights_sha256"):
            raise StageA0CheckpointError("auxiliary weights sha256 differs")
        head = torch.nn.Linear(locked.HIDDEN_WIDTH, 3)
        try:
            head.load_state_dict(
                torch.load(
                    path / AUXILIARY_WEIGHTS_FILENAME,
                    weights_only=True,
                    map_location="cpu",
                ),
                strict=True,
            )
        except Exception as error:
            raise StageA0CheckpointError(
                "auxiliary state_dict strict load failed"
            ) from error
        if (
            sum(parameter.numel() for parameter in head.parameters())
            != AUXILIARY_PARAMETER_COUNT
        ):
            raise StageA0CheckpointError("loaded auxiliary parameter count drifted")
        for parameter in head.parameters():
            if not bool(torch.isfinite(parameter).all()):
                raise StageA0CheckpointError(
                    "auxiliary checkpoint contains non-finite values"
                )
            parameter.requires_grad_(False)
        head.eval()
    elif manifest.get("auxiliary_weights_sha256") is not None:
        raise StageA0CheckpointError("A checkpoint carries auxiliary weight metadata")

    selected_metric = manifest.get("selected_validation_policy_ce")
    if type(selected_metric) not in (int, float) or not math.isfinite(
        float(selected_metric)
    ):
        raise StageA0CheckpointError("selected VALIDATION Policy CE is invalid")
    if seed != manifest["training"]["training_seed"]:
        raise StageA0CheckpointError("training seed metadata is inconsistent")

    return LoadedCheckpoint(
        path=path,
        manifest=manifest,
        model=model,
        auxiliary_head=head,
    )


__all__ = [
    "AUXILIARY_WEIGHTS_FILENAME",
    "LoadedCheckpoint",
    "MANIFEST_FILENAME",
    "TrainingResult",
    "WEIGHTS_FILENAME",
    "load_checkpoint",
    "save_checkpoint",
    "train_seed_pair",
]
