"""Arm A -- BC control training path and frozen checkpoint artifact.

```text
Offline Q dataset (eligible ordinary-discard source states only)
    -> split tensors (whole-hanchan membership from the manifest)
    -> fixed 1x128 MLP (same shape as Stage 2 / Arm B)
    -> masked cross-entropy over legal actions
    -> Adam(lr=1e-3, weight_decay=0), batch 256, <=20 epochs, patience 4
    -> lowest VALIDATION choice-row masked CE
    -> frozen checkpoint artifact + sha256
```

Stage 2の1x128 MLP実装（`lisjong_arena.learned_policy_stage2.network`）を
そのまま再利用する。datasetはeligible ordinary-discard sourceだけを含む本Issue
専用のものであり、agreementはdiagnosticでありstrength proxyとしては扱わない。
TESTはこのmoduleへ渡さない。
"""

import hashlib
import platform
import time
from dataclasses import dataclass
from pathlib import Path
from shutil import rmtree
from tempfile import mkdtemp

from lisjong_arena._artifact_io import canonical_json_text
from lisjong_arena.learned_policy_stage2.network import (
    create_model,
    masked_cross_entropy,
    parameter_count,
)

from .artifact import LoadedOfflineQDataset, feature_block, vocabulary_block
from .errors import OfflineQArtifactError, OfflineQProtocolError
from .protocol import (
    BATCH_SIZE,
    BC_MODEL_ID,
    DATALOADER_SEED,
    DATALOADER_WORKERS,
    DETERMINISTIC_ALGORITHMS,
    EARLY_STOP_PATIENCE,
    EXPECTED_PARAMETER_COUNT,
    FEATURE_DIMENSION,
    HIDDEN_WIDTH,
    LEARNING_RATE,
    MAXIMUM_EPOCHS,
    TORCH_THREADS,
    TRAINING_SEED,
    VOCABULARY_SIZE,
    WEIGHT_DECAY,
    Split,
    verify_contract_identity,
)
from .split_tensors import OfflineQSplitTensors, load_split_tensors

CHECKPOINT_SCHEMA_VERSION = "arena-learned-policy-offlineq-bc-checkpoint-v1"
MANIFEST_FILENAME = "manifest.json"
WEIGHTS_FILENAME = "weights.pt"

_LOGICAL_IDENTITY_FIELDS = (
    "checkpoint_schema_version",
    "dataset_identity",
    "feature",
    "vocabulary",
    "model",
    "training",
    "selected_epoch",
    "selected_validation_choice_masked_ce",
    "parameter_count",
    "weights_sha256",
)


def configure_deterministic_runtime() -> dict[str, object]:
    """locked deterministic CPU条件を設定し、実測した条件を返す。"""
    import torch

    torch.manual_seed(TRAINING_SEED)
    torch.use_deterministic_algorithms(DETERMINISTIC_ALGORITHMS)
    torch.set_num_threads(TORCH_THREADS)
    return {
        "torch_version": torch.__version__,
        "torch_threads": torch.get_num_threads(),
        "deterministic_algorithms": bool(torch.are_deterministic_algorithms_enabled()),
        "cuda_available": bool(torch.cuda.is_available()),
        "python_version": platform.python_version(),
    }


def choice_row_selector(legal_mask):
    return legal_mask.sum(dim=1) >= 2


def evaluate_masked_cross_entropy(
    model, tensors: OfflineQSplitTensors
) -> tuple[float, int]:
    """choice rowだけのmean masked CEと、その母数を返す。"""
    import torch

    selector = choice_row_selector(tensors.legal_mask)
    count = int(selector.sum())
    if count == 0:
        raise OfflineQProtocolError(
            f"{tensors.split.value} split contains no choice rows"
        )
    features = tensors.features[selector]
    legal_mask = tensors.legal_mask[selector]
    targets = tensors.behavior_action_index[selector]
    model.eval()
    total = 0.0
    with torch.no_grad():
        for start in range(0, count, BATCH_SIZE):
            stop = min(start + BATCH_SIZE, count)
            losses = masked_cross_entropy(
                model(features[start:stop]),
                legal_mask[start:stop],
                targets[start:stop],
            )
            total += float(losses.sum())
    return total / count, count


@dataclass(frozen=True, slots=True)
class EpochRecord:
    epoch: int
    train_masked_ce: float
    validation_choice_masked_ce: float

    def to_document(self) -> dict[str, object]:
        return {
            "epoch": self.epoch,
            "train_masked_ce": self.train_masked_ce,
            "validation_choice_masked_ce": self.validation_choice_masked_ce,
        }


@dataclass(frozen=True, slots=True)
class TrainingRun:
    model: object
    history: tuple[EpochRecord, ...]
    selected_epoch: int
    selected_validation_choice_masked_ce: float
    wall_clock_seconds: float
    peak_process_ram_bytes: int | None
    runtime: dict[str, object]


def peak_process_ram_bytes() -> int | None:
    """best-effortなpeak RSS。取得できないplatformでは`None`を返す。"""
    try:
        import resource
    except ImportError:
        return None
    try:
        usage = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    except OSError, ValueError:
        return None
    return int(usage) if platform.system() == "Darwin" else int(usage) * 1024


def train_bc_model(dataset: LoadedOfflineQDataset) -> TrainingRun:
    """locked configでTRAINを学習し、VALIDATIONでcheckpointを固定する。"""
    return train_from_split_tensors(load_split_tensors(dataset))


def train_from_split_tensors(
    tensors: dict[Split, OfflineQSplitTensors],
) -> TrainingRun:
    import torch

    runtime = configure_deterministic_runtime()
    missing = [
        split for split in (Split.TRAIN, Split.VALIDATION) if split not in tensors
    ]
    if missing:
        raise OfflineQProtocolError(
            f"training requires {[split.value for split in missing]} tensors"
        )
    train = tensors[Split.TRAIN]
    validation = tensors[Split.VALIDATION]

    model = create_model()
    optimizer = torch.optim.Adam(
        model.parameters(), lr=LEARNING_RATE, weight_decay=WEIGHT_DECAY
    )
    generator = torch.Generator()
    generator.manual_seed(DATALOADER_SEED)
    loader = torch.utils.data.DataLoader(
        torch.utils.data.TensorDataset(
            train.features, train.legal_mask, train.behavior_action_index
        ),
        batch_size=BATCH_SIZE,
        shuffle=True,
        num_workers=DATALOADER_WORKERS,
        generator=generator,
        drop_last=False,
    )

    history: list[EpochRecord] = []
    best_state: dict[str, object] | None = None
    best_epoch = 0
    best_metric = float("inf")
    epochs_without_improvement = 0
    start = time.perf_counter()

    for epoch in range(1, MAXIMUM_EPOCHS + 1):
        model.train()
        total = 0.0
        seen = 0
        for features, legal_mask, targets in loader:
            optimizer.zero_grad(set_to_none=True)
            losses = masked_cross_entropy(model(features), legal_mask, targets)
            loss = losses.mean()
            loss.backward()
            optimizer.step()
            total += float(losses.detach().sum())
            seen += int(targets.shape[0])
        if seen != train.row_count:
            raise OfflineQProtocolError("training epoch did not visit every TRAIN row")

        validation_metric, _ = evaluate_masked_cross_entropy(model, validation)
        history.append(
            EpochRecord(
                epoch=epoch,
                train_masked_ce=total / seen,
                validation_choice_masked_ce=validation_metric,
            )
        )
        if validation_metric < best_metric:
            best_metric = validation_metric
            best_epoch = epoch
            best_state = {
                name: tensor.detach().clone()
                for name, tensor in model.state_dict().items()
            }
            epochs_without_improvement = 0
        else:
            epochs_without_improvement += 1
            if epochs_without_improvement >= EARLY_STOP_PATIENCE:
                break

    wall_clock = time.perf_counter() - start
    if best_state is None:
        raise OfflineQProtocolError("training produced no validated checkpoint")
    model.load_state_dict(best_state, strict=True)
    return TrainingRun(
        model=model,
        history=tuple(history),
        selected_epoch=best_epoch,
        selected_validation_choice_masked_ce=best_metric,
        wall_clock_seconds=wall_clock,
        peak_process_ram_bytes=peak_process_ram_bytes(),
        runtime=runtime,
    )


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def checkpoint_identity(manifest: dict) -> str:
    missing = [name for name in _LOGICAL_IDENTITY_FIELDS if name not in manifest]
    if missing:
        raise OfflineQArtifactError(f"checkpoint manifest is missing {missing}")
    logical = {name: manifest[name] for name in _LOGICAL_IDENTITY_FIELDS}
    return _sha256(canonical_json_text(logical).encode("utf-8"))


def _training_block() -> dict[str, object]:
    return {
        "loss": "masked_cross_entropy_over_legal_actions",
        "optimizer": "adam",
        "learning_rate": LEARNING_RATE,
        "weight_decay": WEIGHT_DECAY,
        "batch_size": BATCH_SIZE,
        "maximum_epochs": MAXIMUM_EPOCHS,
        "early_stop_patience": EARLY_STOP_PATIENCE,
        "training_seed": TRAINING_SEED,
        "dataloader_seed": DATALOADER_SEED,
        "dataloader_workers": DATALOADER_WORKERS,
        "torch_threads": TORCH_THREADS,
        "deterministic_algorithms": DETERMINISTIC_ALGORITHMS,
        "checkpoint_selection": "lowest_validation_choice_row_masked_ce",
    }


def _model_block() -> dict[str, object]:
    return {
        "model_id": BC_MODEL_ID,
        "input_dimension": FEATURE_DIMENSION,
        "hidden_layers": 1,
        "hidden_width": HIDDEN_WIDTH,
        "activation": "relu",
        "output_dimension": VOCABULARY_SIZE,
        "dropout": None,
        "normalization_layer": None,
    }


def locked_model_block() -> dict[str, object]:
    return _model_block()


def locked_training_block() -> dict[str, object]:
    return _training_block()


def save_checkpoint(
    destination: str | Path,
    dataset: LoadedOfflineQDataset,
    run: TrainingRun,
) -> "LoadedBcCheckpoint":
    """frozen checkpointをstagingで検証してから公開し、strict readbackを返す。"""
    import torch

    destination = Path(destination)
    if destination.exists():
        raise FileExistsError("checkpoint destination already exists")
    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(
        mkdtemp(prefix=f".{destination.name}-staging-", dir=destination.parent)
    )
    published = False
    try:
        weights_path = staging / WEIGHTS_FILENAME
        torch.save(run.model.state_dict(), weights_path)
        weights = weights_path.read_bytes()
        manifest: dict[str, object] = {
            "checkpoint_schema_version": CHECKPOINT_SCHEMA_VERSION,
            "dataset_identity": dataset.identity,
            "feature": dict(dataset.manifest["feature"]),
            "vocabulary": dict(dataset.manifest["vocabulary"]),
            "model": _model_block(),
            "training": _training_block(),
            "parameter_count": parameter_count(run.model),
            "selected_epoch": run.selected_epoch,
            "selected_validation_choice_masked_ce": (
                run.selected_validation_choice_masked_ce
            ),
            "epoch_history": [record.to_document() for record in run.history],
            "weights_bytes": len(weights),
            "weights_sha256": _sha256(weights),
            "runtime": {
                **run.runtime,
                "training_wall_clock_seconds": run.wall_clock_seconds,
                "peak_process_ram_bytes": run.peak_process_ram_bytes,
            },
        }
        manifest["checkpoint_identity"] = checkpoint_identity(manifest)
        (staging / MANIFEST_FILENAME).write_text(
            canonical_json_text(manifest), encoding="utf-8", newline="\n"
        )
        readback = torch.load(weights_path, weights_only=True, map_location="cpu")
        expected = run.model.state_dict()
        if set(readback) != set(expected) or any(
            not torch.equal(readback[name], expected[name]) for name in expected
        ):
            raise OfflineQArtifactError("staged state_dict readback differs")
        staging.rename(destination)
        published = True
    finally:
        if not published:
            rmtree(staging, ignore_errors=True)
    return load_checkpoint(destination)


@dataclass(frozen=True, slots=True)
class LoadedBcCheckpoint:
    path: Path
    manifest: dict
    model: object

    @property
    def identity(self) -> str:
        return self.manifest["checkpoint_identity"]

    @property
    def weights_sha256(self) -> str:
        return self.manifest["weights_sha256"]


def load_checkpoint(path: str | Path) -> LoadedBcCheckpoint:
    """checkpointを読み、identity / dimension / digestをfail closedで検証する。"""
    import json

    import torch

    verify_contract_identity()
    path = Path(path)
    if not path.is_dir():
        raise OfflineQArtifactError("checkpoint path is not a directory")
    if {item.name for item in path.iterdir()} != {MANIFEST_FILENAME, WEIGHTS_FILENAME}:
        raise OfflineQArtifactError("checkpoint contains missing or extra files")

    manifest_text = (path / MANIFEST_FILENAME).read_text(encoding="utf-8")
    try:
        manifest = json.loads(manifest_text)
    except json.JSONDecodeError as error:
        raise OfflineQArtifactError("checkpoint manifest is not valid JSON") from error
    if type(manifest) is not dict:
        raise OfflineQArtifactError("checkpoint manifest must be an object")
    if manifest.get("checkpoint_schema_version") != CHECKPOINT_SCHEMA_VERSION:
        raise OfflineQArtifactError("unsupported checkpoint schema version")
    if canonical_json_text(manifest) != manifest_text:
        raise OfflineQArtifactError("checkpoint manifest bytes are not canonical JSON")
    if manifest.get("model") != _model_block():
        raise OfflineQArtifactError("checkpoint model config is not the locked one")
    if manifest.get("training") != _training_block():
        raise OfflineQArtifactError("checkpoint training config is not the locked one")
    if manifest.get("parameter_count") != EXPECTED_PARAMETER_COUNT:
        raise OfflineQArtifactError("checkpoint parameter count is not the locked one")
    if manifest.get("feature") != feature_block():
        raise OfflineQArtifactError(
            "checkpoint feature schema identity is not the locked one"
        )
    if manifest.get("vocabulary") != vocabulary_block():
        raise OfflineQArtifactError(
            "checkpoint action vocabulary identity is not the locked one"
        )
    if manifest.get("checkpoint_identity") != checkpoint_identity(manifest):
        raise OfflineQArtifactError(
            "checkpoint_identity does not match the manifest content"
        )

    weights = (path / WEIGHTS_FILENAME).read_bytes()
    if len(weights) != manifest.get("weights_bytes"):
        raise OfflineQArtifactError("checkpoint weights byte count differs")
    if _sha256(weights) != manifest.get("weights_sha256"):
        raise OfflineQArtifactError("checkpoint weights sha256 differs")

    state_dict = torch.load(
        path / WEIGHTS_FILENAME, weights_only=True, map_location="cpu"
    )
    model = create_model()
    try:
        model.load_state_dict(state_dict, strict=True)
    except RuntimeError as error:
        raise OfflineQArtifactError(
            "checkpoint state_dict does not match the locked model"
        ) from error
    model.eval()
    for parameter in model.parameters():
        parameter.requires_grad_(False)
    return LoadedBcCheckpoint(path=path, manifest=manifest, model=model)


__all__ = [
    "CHECKPOINT_SCHEMA_VERSION",
    "MANIFEST_FILENAME",
    "WEIGHTS_FILENAME",
    "EpochRecord",
    "LoadedBcCheckpoint",
    "TrainingRun",
    "checkpoint_identity",
    "choice_row_selector",
    "configure_deterministic_runtime",
    "evaluate_masked_cross_entropy",
    "load_checkpoint",
    "locked_model_block",
    "locked_training_block",
    "peak_process_ram_bytes",
    "save_checkpoint",
    "train_bc_model",
    "train_from_split_tensors",
]
