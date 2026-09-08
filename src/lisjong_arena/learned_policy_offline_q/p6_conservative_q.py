"""P6 fixed conservative Offline Q formulation for Issue #181.

This module intentionally leaves the historical #140/#158 training paths untouched.
It reuses the exact P1 model, TD target, support, optimizer, deterministic runtime,
and training budget, changing only one training term::

    mean Huber(Q(s, a_behavior), TD_target)
      + 0.1 * mean CQL_gap(s)

where ``CQL_gap`` is computed over the current legal ordinary-discard actions that
also belong to the exact TRAIN support set.  The fixed ``alpha=0.1`` and
``temperature=1.0`` are one bounded P6 formulation, not tuned hyperparameters.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from pathlib import Path
from shutil import rmtree
from tempfile import mkdtemp

from lisjong_arena._artifact_io import canonical_json_text
from lisjong_arena.learned_policy_stage2.network import parameter_count

from .artifact import LoadedOfflineQDataset, vocabulary_block
from .bc_training import configure_deterministic_runtime, peak_process_ram_bytes
from .errors import OfflineQArtifactError, OfflineQProtocolError
from .p1_features import p1_feature_block
from .p1_q_training import (
    P1_EXPECTED_PARAMETER_COUNT,
    create_p1_model,
    model_weights_digest,
    p1_model_block,
    p1_training_block,
    require_p1_split_tensors,
    verify_locked_q_protocol_delta,
)
from .protocol import (
    BATCH_SIZE,
    DATALOADER_SEED,
    DATALOADER_WORKERS,
    HUBER_LOSS_DELTA,
    LEARNING_RATE,
    MAXIMUM_EPOCHS,
    VOCABULARY_SIZE,
    WEIGHT_DECAY,
    Split,
    verify_contract_identity,
)
from .q_network import q_value_at
from .q_training import compute_td_targets, train_support_mask
from .support import support_set_identity

P6_CHECKPOINT_SCHEMA_VERSION = "arena-learned-policy-p6-conservative-q-checkpoint-v1"
P6_MODEL_ID = "arena-learned-policy-p6-conservative-q-p1-mlp-v1"
P6_OBJECTIVE_ID = "arena-learned-policy-p6-conservative-q-objective-v1"
P6_CANDIDATE_IDENTITY_PREFIX = "learned-p6-conservative-q:"

CQL_ALPHA = 0.1
CQL_TEMPERATURE = 1.0

MANIFEST_FILENAME = "manifest.json"
WEIGHTS_FILENAME = "weights.pt"


def conservative_regularization_block() -> dict[str, object]:
    """Return the single pre-registered P6 changed axis."""
    return {
        "objective_id": P6_OBJECTIVE_ID,
        "kind": "discrete-cql-style-current-supported-legal-gap",
        "alpha": CQL_ALPHA,
        "temperature": CQL_TEMPERATURE,
        "action_set": "current-legal-ordinary-discard-intersect-exact-train-support",
        "formula": "T*logsumexp(Q/T)-Q_behavior",
        "alpha_tuning": False,
        "temperature_tuning": False,
    }


def p6_training_block() -> dict[str, object]:
    """Bind the exact #158 base training contract plus the one P6 penalty."""
    return {
        "base_training": p1_training_block(),
        "conservative_regularization": conservative_regularization_block(),
    }


def p6_model_block() -> dict[str, object]:
    """Use the exact #158 P1 model shape; only the semantic model ID is new."""
    block = dict(p1_model_block())
    block["model_id"] = P6_MODEL_ID
    return block


def verify_p6_protocol_delta() -> None:
    """Fail closed unless P6 changes only the conservative regularization term."""
    verify_contract_identity()
    verify_locked_q_protocol_delta()
    if CQL_ALPHA != 0.1 or CQL_TEMPERATURE != 1.0:
        raise OfflineQProtocolError("the locked P6 alpha / temperature drifted")
    base = p1_model_block()
    candidate = p6_model_block()
    for name, value in base.items():
        if name == "model_id":
            continue
        if candidate[name] != value:
            raise OfflineQProtocolError(
                f"P6 changed the locked P1 model field {name!r}"
            )
    if candidate["model_id"] != P6_MODEL_ID:
        raise OfflineQProtocolError("the P6 model identity drifted")
    if p6_training_block()["base_training"] != p1_training_block():
        raise OfflineQProtocolError("the P6 base training block drifted from #158")


def _require_support_mask(support_mask):
    import torch

    if not isinstance(support_mask, torch.Tensor):
        raise TypeError("support_mask must be a torch.Tensor")
    if support_mask.dtype is not torch.bool or tuple(support_mask.shape) != (
        VOCABULARY_SIZE,
    ):
        raise OfflineQProtocolError(
            f"support_mask must be bool[{VOCABULARY_SIZE}]"
        )
    if not bool(support_mask.any()):
        raise OfflineQProtocolError("TRAIN support set must not be empty")
    return support_mask


def conservative_action_mask(legal_mask, behavior_action_index, support_mask):
    """Build A_cql(s) from current legal mask intersected with TRAIN support."""
    import torch

    _require_support_mask(support_mask)
    if not isinstance(legal_mask, torch.Tensor) or legal_mask.dtype is not torch.bool:
        raise OfflineQProtocolError("legal_mask must be a bool tensor")
    if legal_mask.dim() != 2 or int(legal_mask.shape[1]) != VOCABULARY_SIZE:
        raise OfflineQProtocolError(
            f"legal_mask must have shape (N, {VOCABULARY_SIZE})"
        )
    if (
        not isinstance(behavior_action_index, torch.Tensor)
        or behavior_action_index.dtype is not torch.long
        or behavior_action_index.dim() != 1
        or int(behavior_action_index.shape[0]) != int(legal_mask.shape[0])
    ):
        raise OfflineQProtocolError(
            "behavior_action_index must be a long vector aligned with legal_mask"
        )
    if bool(
        ((behavior_action_index < 0) | (behavior_action_index >= VOCABULARY_SIZE)).any()
    ):
        raise OfflineQProtocolError("behavior_action_index is out of range")

    action_mask = legal_mask & support_mask.unsqueeze(0)
    if bool((action_mask.sum(dim=1) == 0).any()):
        raise OfflineQProtocolError(
            "a TRAIN row has no current legal action inside the locked support set"
        )
    behavior_in_set = action_mask.gather(1, behavior_action_index.unsqueeze(1)).squeeze(1)
    if not bool(behavior_in_set.all()):
        raise OfflineQProtocolError(
            "a behavior action is outside current-legal intersect exact TRAIN support"
        )
    return action_mask


def cql_gap(q_values, legal_mask, behavior_action_index, support_mask):
    """Compute the per-row fixed discrete CQL-style gap."""
    import torch

    if not isinstance(q_values, torch.Tensor) or q_values.dim() != 2:
        raise OfflineQProtocolError("q_values must be a 2-D tensor")
    if tuple(q_values.shape) != tuple(legal_mask.shape):
        raise OfflineQProtocolError("q_values and legal_mask shapes differ")
    if not bool(torch.isfinite(q_values).all()):
        raise OfflineQProtocolError("P6 Q output contains a non-finite value")
    action_mask = conservative_action_mask(
        legal_mask, behavior_action_index, support_mask
    )
    scaled = q_values / CQL_TEMPERATURE
    masked = scaled.masked_fill(~action_mask, float("-inf"))
    logsumexp = torch.logsumexp(masked, dim=1) * CQL_TEMPERATURE
    behavior_q = q_value_at(q_values, behavior_action_index)
    gaps = logsumexp - behavior_q
    if not bool(torch.isfinite(gaps).all()):
        raise OfflineQProtocolError("P6 CQL gap contains a non-finite value")
    return gaps


@dataclass(frozen=True, slots=True)
class P6EpochRecord:
    epoch: int
    train_huber_loss: float
    train_cql_gap: float
    train_total_loss: float
    validation_huber_loss: float
    validation_cql_gap: float
    validation_total_loss: float

    def to_document(self) -> dict[str, object]:
        return {
            "epoch": self.epoch,
            "train_huber_loss": self.train_huber_loss,
            "train_cql_gap": self.train_cql_gap,
            "train_total_loss": self.train_total_loss,
            "validation_huber_loss": self.validation_huber_loss,
            "validation_cql_gap": self.validation_cql_gap,
            "validation_total_loss": self.validation_total_loss,
        }


@dataclass(frozen=True, slots=True)
class P6TrainingRun:
    model: object
    support_mask: object
    history: tuple[P6EpochRecord, ...]
    selected_epoch: int
    final_validation_huber_loss: float
    final_validation_cql_gap: float
    final_validation_total_loss: float
    wall_clock_seconds: float
    peak_process_ram_bytes: int | None
    runtime: dict[str, object]


def _objective_metrics(model, target_model, tensors, support_mask) -> tuple[float, float, float]:
    import torch

    if tensors.row_count <= 0:
        raise OfflineQProtocolError(f"{tensors.split.value} split contains no rows")
    targets = compute_td_targets(target_model, tensors, support_mask)
    model.eval()
    huber_total = 0.0
    gap_total = 0.0
    with torch.no_grad():
        for start in range(0, tensors.row_count, BATCH_SIZE):
            stop = min(start + BATCH_SIZE, tensors.row_count)
            q_values = model(tensors.features[start:stop])
            behavior = tensors.behavior_action_index[start:stop]
            selected = q_value_at(q_values, behavior)
            losses = torch.nn.functional.huber_loss(
                selected,
                targets[start:stop],
                delta=HUBER_LOSS_DELTA,
                reduction="none",
            )
            gaps = cql_gap(
                q_values,
                tensors.legal_mask[start:stop],
                behavior,
                support_mask,
            )
            huber_total += float(losses.sum())
            gap_total += float(gaps.sum())
    huber = huber_total / tensors.row_count
    gap = gap_total / tensors.row_count
    return huber, gap, huber + CQL_ALPHA * gap


def train_p6_conservative_q(tensors: dict) -> P6TrainingRun:
    """Train the one fixed P6 candidate without modifying historical training code."""
    import torch

    verify_p6_protocol_delta()
    require_p1_split_tensors(tensors)
    missing = [
        split for split in (Split.TRAIN, Split.VALIDATION) if split not in tensors
    ]
    if missing:
        raise OfflineQProtocolError(
            f"P6 training requires {[split.value for split in missing]} tensors"
        )
    train = tensors[Split.TRAIN]
    validation = tensors[Split.VALIDATION]
    support_mask = train_support_mask(train)
    _require_support_mask(support_mask)

    runtime = configure_deterministic_runtime()
    model = create_p1_model()
    target_model = create_p1_model()
    target_model.load_state_dict(model.state_dict())
    target_model.eval()
    for parameter in target_model.parameters():
        parameter.requires_grad_(False)

    optimizer = torch.optim.Adam(
        model.parameters(), lr=LEARNING_RATE, weight_decay=WEIGHT_DECAY
    )
    generator = torch.Generator()
    generator.manual_seed(DATALOADER_SEED)
    loader = torch.utils.data.DataLoader(
        torch.utils.data.TensorDataset(torch.arange(train.row_count, dtype=torch.long)),
        batch_size=BATCH_SIZE,
        shuffle=True,
        num_workers=0,
        generator=generator,
        drop_last=False,
    )

    history: list[P6EpochRecord] = []
    start_time = time.perf_counter()
    for epoch in range(1, MAXIMUM_EPOCHS + 1):
        target_model.load_state_dict(model.state_dict())
        epoch_targets = compute_td_targets(target_model, train, support_mask)
        model.train()
        huber_total = 0.0
        gap_total = 0.0
        seen = 0
        for (batch_indices,) in loader:
            optimizer.zero_grad(set_to_none=True)
            features = train.features[batch_indices]
            behavior = train.behavior_action_index[batch_indices]
            q_values = model(features)
            selected = q_value_at(q_values, behavior)
            huber_losses = torch.nn.functional.huber_loss(
                selected,
                epoch_targets[batch_indices],
                delta=HUBER_LOSS_DELTA,
                reduction="none",
            )
            gaps = cql_gap(
                q_values,
                train.legal_mask[batch_indices],
                behavior,
                support_mask,
            )
            loss = huber_losses.mean() + CQL_ALPHA * gaps.mean()
            if not bool(torch.isfinite(loss)):
                raise OfflineQProtocolError("P6 training loss is non-finite")
            loss.backward()
            optimizer.step()
            huber_total += float(huber_losses.detach().sum())
            gap_total += float(gaps.detach().sum())
            seen += int(behavior.shape[0])
        if seen != train.row_count:
            raise OfflineQProtocolError("P6 training epoch did not visit every TRAIN row")

        validation_huber, validation_gap, validation_total = _objective_metrics(
            model, target_model, validation, support_mask
        )
        train_huber = huber_total / seen
        train_gap = gap_total / seen
        history.append(
            P6EpochRecord(
                epoch=epoch,
                train_huber_loss=train_huber,
                train_cql_gap=train_gap,
                train_total_loss=train_huber + CQL_ALPHA * train_gap,
                validation_huber_loss=validation_huber,
                validation_cql_gap=validation_gap,
                validation_total_loss=validation_total,
            )
        )

    if not history:
        raise OfflineQProtocolError("P6 training produced no completed outer iteration")
    wall_clock = time.perf_counter() - start_time
    final = history[-1]
    return P6TrainingRun(
        model=model,
        support_mask=support_mask,
        history=tuple(history),
        selected_epoch=final.epoch,
        final_validation_huber_loss=final.validation_huber_loss,
        final_validation_cql_gap=final.validation_cql_gap,
        final_validation_total_loss=final.validation_total_loss,
        wall_clock_seconds=wall_clock,
        peak_process_ram_bytes=peak_process_ram_bytes(),
        runtime=runtime,
    )


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _supported_indices(support_mask) -> list[int]:
    import torch

    _require_support_mask(support_mask)
    return sorted(int(index) for index in torch.nonzero(support_mask).flatten().tolist())


def p6_candidate_binding(
    *, source_dataset_identity: str, supported_indices_digest: str, weights_digest: str
) -> dict[str, object]:
    for name, value in (
        ("source_dataset_identity", source_dataset_identity),
        ("supported_indices_digest", supported_indices_digest),
        ("canonical_model_weights_digest", weights_digest),
    ):
        if type(value) is not str or len(value) != 64:
            raise OfflineQArtifactError(f"{name} must be a 64 character sha256 digest")
    return {
        "checkpoint_schema_version": P6_CHECKPOINT_SCHEMA_VERSION,
        "source_dataset_identity": source_dataset_identity,
        "p1_feature": p1_feature_block(),
        "action_vocabulary": vocabulary_block(),
        "supported_indices_digest": supported_indices_digest,
        "model": p6_model_block(),
        "training": p6_training_block(),
        "canonical_model_weights_digest": weights_digest,
    }


def p6_candidate_identity(binding: dict[str, object]) -> str:
    digest = _sha256(canonical_json_text(binding).encode("utf-8"))
    return f"{P6_CANDIDATE_IDENTITY_PREFIX}{digest}"


@dataclass(frozen=True, slots=True)
class LoadedP6Checkpoint:
    path: Path
    manifest: dict[str, object]
    model: object
    supported_indices: frozenset[int]

    @property
    def candidate_identity(self) -> str:
        return str(self.manifest["candidate_identity"])


def _checkpoint_manifest(dataset: LoadedOfflineQDataset, run: P6TrainingRun, weights: bytes):
    indices = _supported_indices(run.support_mask)
    support_digest = support_set_identity(indices)
    canonical_weights_digest = model_weights_digest(run.model)
    binding = p6_candidate_binding(
        source_dataset_identity=dataset.identity,
        supported_indices_digest=support_digest,
        weights_digest=canonical_weights_digest,
    )
    return {
        "checkpoint_schema_version": P6_CHECKPOINT_SCHEMA_VERSION,
        "source_issue": "lisbun/lisjong-arena#181",
        "source_dataset_identity": dataset.identity,
        "p1_feature": p1_feature_block(),
        "action_vocabulary": vocabulary_block(),
        "model": p6_model_block(),
        "training": p6_training_block(),
        "parameter_count": parameter_count(run.model),
        "supported_indices": indices,
        "supported_indices_digest": support_digest,
        "selected_epoch": run.selected_epoch,
        "final_validation_huber_loss": run.final_validation_huber_loss,
        "final_validation_cql_gap": run.final_validation_cql_gap,
        "final_validation_total_loss": run.final_validation_total_loss,
        "epoch_history": [entry.to_document() for entry in run.history],
        "weights_bytes": len(weights),
        "weights_sha256": _sha256(weights),
        "canonical_model_weights_digest": canonical_weights_digest,
        "candidate_binding": binding,
        "candidate_identity": p6_candidate_identity(binding),
        "runtime": {
            **run.runtime,
            "training_wall_clock_seconds": run.wall_clock_seconds,
            "peak_process_ram_bytes": run.peak_process_ram_bytes,
        },
        "strength_claim": None,
    }


def save_p6_checkpoint(
    destination: str | Path,
    dataset: LoadedOfflineQDataset,
    run: P6TrainingRun,
) -> LoadedP6Checkpoint:
    """Publish one write-once P6 checkpoint directory and strict-read it back."""
    import torch

    verify_p6_protocol_delta()
    if not isinstance(dataset, LoadedOfflineQDataset):
        raise TypeError("dataset must be a LoadedOfflineQDataset")
    if not isinstance(run, P6TrainingRun):
        raise TypeError("run must be a P6TrainingRun")
    destination = Path(destination)
    if destination.exists():
        raise FileExistsError("P6 checkpoint destination already exists")
    if not destination.parent.is_dir():
        raise OfflineQArtifactError("P6 checkpoint parent directory must already exist")

    staging = Path(mkdtemp(prefix=f".{destination.name}-staging-", dir=destination.parent))
    published = False
    try:
        weights_path = staging / WEIGHTS_FILENAME
        torch.save(run.model.state_dict(), weights_path)
        weights = weights_path.read_bytes()
        manifest = _checkpoint_manifest(dataset, run, weights)
        (staging / MANIFEST_FILENAME).write_text(
            canonical_json_text(manifest), encoding="utf-8", newline="\n"
        )
        staging.rename(destination)
        published = True
    finally:
        if not published:
            rmtree(staging, ignore_errors=True)
    return load_p6_checkpoint(destination)


def load_p6_checkpoint(path: str | Path) -> LoadedP6Checkpoint:
    """Strictly read a P6 checkpoint and re-derive all identity-bearing values."""
    import torch

    verify_p6_protocol_delta()
    path = Path(path)
    if not path.is_dir():
        raise OfflineQArtifactError("P6 checkpoint path is not a directory")
    if {item.name for item in path.iterdir()} != {MANIFEST_FILENAME, WEIGHTS_FILENAME}:
        raise OfflineQArtifactError("P6 checkpoint contains missing or extra files")
    manifest_text = (path / MANIFEST_FILENAME).read_text(encoding="utf-8")
    try:
        manifest = json.loads(manifest_text)
    except json.JSONDecodeError as error:
        raise OfflineQArtifactError("P6 checkpoint manifest is not valid JSON") from error
    if type(manifest) is not dict or canonical_json_text(manifest) != manifest_text:
        raise OfflineQArtifactError("P6 checkpoint manifest is not canonical JSON")
    if manifest.get("checkpoint_schema_version") != P6_CHECKPOINT_SCHEMA_VERSION:
        raise OfflineQArtifactError("unsupported P6 checkpoint schema version")
    if manifest.get("source_issue") != "lisbun/lisjong-arena#181":
        raise OfflineQArtifactError("P6 checkpoint source issue is not #181")
    if manifest.get("p1_feature") != p1_feature_block():
        raise OfflineQArtifactError("P6 checkpoint P1 feature block drifted")
    if manifest.get("action_vocabulary") != vocabulary_block():
        raise OfflineQArtifactError("P6 checkpoint vocabulary block drifted")
    if manifest.get("model") != p6_model_block():
        raise OfflineQArtifactError("P6 checkpoint model block drifted")
    if manifest.get("training") != p6_training_block():
        raise OfflineQArtifactError("P6 checkpoint training block drifted")
    if manifest.get("parameter_count") != P1_EXPECTED_PARAMETER_COUNT:
        raise OfflineQArtifactError("P6 checkpoint parameter count drifted")
    if manifest.get("selected_epoch") != MAXIMUM_EPOCHS:
        raise OfflineQArtifactError("P6 checkpoint is not fixed_final_iteration")
    if manifest.get("strength_claim") is not None:
        raise OfflineQArtifactError("P6 Gate A checkpoint must not carry a strength claim")

    indices = manifest.get("supported_indices")
    if (
        type(indices) is not list
        or any(type(index) is not int for index in indices)
        or sorted(indices) != indices
        or len(set(indices)) != len(indices)
        or any(not 0 <= index < VOCABULARY_SIZE for index in indices)
    ):
        raise OfflineQArtifactError("P6 checkpoint supported_indices is malformed")
    support_digest = support_set_identity(indices)
    if manifest.get("supported_indices_digest") != support_digest:
        raise OfflineQArtifactError("P6 checkpoint support digest differs")

    weights = (path / WEIGHTS_FILENAME).read_bytes()
    if len(weights) != manifest.get("weights_bytes") or _sha256(weights) != manifest.get(
        "weights_sha256"
    ):
        raise OfflineQArtifactError("P6 checkpoint weights bytes/digest differ")
    state_dict = torch.load(path / WEIGHTS_FILENAME, weights_only=True, map_location="cpu")
    model = create_p1_model()
    try:
        model.load_state_dict(state_dict, strict=True)
    except RuntimeError as error:
        raise OfflineQArtifactError("P6 checkpoint state_dict does not match P1 model") from error
    model.eval()
    for parameter in model.parameters():
        parameter.requires_grad_(False)
    canonical_weights_digest = model_weights_digest(model)
    if manifest.get("canonical_model_weights_digest") != canonical_weights_digest:
        raise OfflineQArtifactError("P6 canonical model weights digest differs")
    binding = p6_candidate_binding(
        source_dataset_identity=str(manifest.get("source_dataset_identity")),
        supported_indices_digest=support_digest,
        weights_digest=canonical_weights_digest,
    )
    if manifest.get("candidate_binding") != binding:
        raise OfflineQArtifactError("P6 checkpoint candidate binding differs")
    if manifest.get("candidate_identity") != p6_candidate_identity(binding):
        raise OfflineQArtifactError("P6 checkpoint candidate identity differs")
    return LoadedP6Checkpoint(
        path=path,
        manifest=manifest,
        model=model,
        supported_indices=frozenset(indices),
    )


__all__ = [
    "CQL_ALPHA",
    "CQL_TEMPERATURE",
    "LoadedP6Checkpoint",
    "P6_CHECKPOINT_SCHEMA_VERSION",
    "P6_CANDIDATE_IDENTITY_PREFIX",
    "P6_MODEL_ID",
    "P6_OBJECTIVE_ID",
    "P6EpochRecord",
    "P6TrainingRun",
    "conservative_action_mask",
    "conservative_regularization_block",
    "cql_gap",
    "load_p6_checkpoint",
    "p6_candidate_binding",
    "p6_candidate_identity",
    "p6_model_block",
    "p6_training_block",
    "save_p6_checkpoint",
    "train_p6_conservative_q",
    "verify_p6_protocol_delta",
]
