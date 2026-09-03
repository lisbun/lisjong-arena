"""Locked Stage 2 training path and frozen checkpoint artifact.

```text
dataset artifact
    -> split tensors (whole-hanchan membership from the manifest)
    -> fixed 1x128 MLP
    -> masked cross-entropy over legal actions
    -> Adam(lr=1e-3, weight_decay=0), batch 256, <=20 epochs, patience 4
    -> lowest VALIDATION choice-row masked CE
    -> frozen checkpoint artifact + sha256
```

TESTはこのmoduleへ渡さない。checkpoint selectionはVALIDATION choice-row masked
CEだけで行い、TEST partitionはtraining pathから参照しない。
"""

import hashlib
import json
import platform
import time
from dataclasses import dataclass
from pathlib import Path
from shutil import rmtree
from tempfile import mkdtemp

from lisjong_arena._artifact_io import canonical_json_text

from .artifact import LoadedStage2Dataset, feature_block, vocabulary_block
from .errors import Stage2ArtifactError, Stage2ProtocolError
from .network import create_model, masked_cross_entropy, parameter_count
from .protocol import (
    BATCH_SIZE,
    DATALOADER_SEED,
    DATALOADER_WORKERS,
    DETERMINISTIC_ALGORITHMS,
    EARLY_STOP_PATIENCE,
    EXPECTED_PARAMETER_COUNT,
    FEATURE_DIMENSION,
    HIDDEN_WIDTH,
    LEARNING_RATE,
    MAXIMUM_EPOCHS,
    MODEL_ID,
    TORCH_THREADS,
    TRAINING_SEED,
    VOCABULARY_SIZE,
    WEIGHT_DECAY,
    Split,
    verify_contract_identity,
)

CHECKPOINT_SCHEMA_VERSION = "arena-learned-policy-stage2-checkpoint-v1"
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


@dataclass(frozen=True, slots=True)
class SplitTensors:
    """1 splitのfeature / legal mask / teacher label tensor。"""

    split: Split
    features: object
    legal_mask: object
    targets: object
    row_indices: tuple[int, ...]

    @property
    def row_count(self) -> int:
        return len(self.row_indices)


def load_split_tensors(dataset: LoadedStage2Dataset) -> dict[Split, SplitTensors]:
    """dataset artifactを、split単位のCPU tensorへ読み出す。

    split membershipはmanifestのwhole-hanchan seed populationだけから決まり、
    row単位のre-splitは行わない。
    """
    import torch

    verify_contract_identity()
    if not isinstance(dataset, LoadedStage2Dataset):
        raise TypeError("dataset must be a LoadedStage2Dataset")

    row_count = dataset.row_count
    features = torch.frombuffer(
        bytearray(dataset.feature_bytes()), dtype=torch.float32
    ).reshape(row_count, FEATURE_DIMENSION)
    if not bool(torch.isfinite(features).all()):
        raise Stage2ProtocolError("dataset features contain non-finite values")
    legal_mask = (
        torch.frombuffer(bytearray(dataset.legal_mask_bytes()), dtype=torch.uint8)
        .reshape(row_count, VOCABULARY_SIZE)
        .bool()
    )
    targets = torch.tensor(
        [row.teacher_action_index for row in dataset.rows], dtype=torch.long
    )
    if not bool(legal_mask.gather(1, targets.unsqueeze(1)).all()):
        raise Stage2ProtocolError("a teacher label is not legal in its own mask")

    tensors: dict[Split, SplitTensors] = {}
    for split in Split:
        indices = dataset.split_indices(split)
        if not indices:
            raise Stage2ProtocolError(f"{split.value} split is empty")
        selector = torch.tensor(indices, dtype=torch.long)
        tensors[split] = SplitTensors(
            split=split,
            features=features.index_select(0, selector).contiguous(),
            legal_mask=legal_mask.index_select(0, selector).contiguous(),
            targets=targets.index_select(0, selector).contiguous(),
            row_indices=indices,
        )
    seen = {index for entry in tensors.values() for index in entry.row_indices}
    if len(seen) != row_count:
        raise Stage2ProtocolError("split membership does not partition the dataset")
    return tensors


def choice_row_selector(legal_mask):
    """`len(legal_actions) >= 2` のrowだけを選ぶbool selectorを返す。"""
    return legal_mask.sum(dim=1) >= 2


def evaluate_masked_cross_entropy(model, tensors: SplitTensors) -> tuple[float, int]:
    """choice rowだけのmean masked CEと、その母数を返す。"""
    import torch

    selector = choice_row_selector(tensors.legal_mask)
    count = int(selector.sum())
    if count == 0:
        raise Stage2ProtocolError(
            f"{tensors.split.value} split contains no choice rows"
        )
    features = tensors.features[selector]
    legal_mask = tensors.legal_mask[selector]
    targets = tensors.targets[selector]
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
    """1 epochのtraining lossとVALIDATION choice-row masked CE。"""

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
    """training結果。TEST exposure前のfrozen checkpointを含む。"""

    model: object
    history: tuple[EpochRecord, ...]
    selected_epoch: int
    selected_validation_choice_masked_ce: float
    wall_clock_seconds: float
    peak_process_ram_bytes: int | None
    runtime: dict[str, object]


def peak_process_ram_bytes() -> int | None:
    """best-effortなpeak RSS。取得できないplatformでは`None`を返す。

    `resource`はUnix系専用であり、このrepositoryはWindows / PowerShellでの利用も
    案内している。Issue #133もpeak RAMを「where practical」としているため、
    測定不能をStage 2全体の起動不能にしない。既存Phase 6と同じく、測定できない
    場合は`None`を記録する。
    """
    try:
        import resource
    except ImportError:
        return None
    try:
        usage = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    except OSError, ValueError:
        return None
    return int(usage) if platform.system() == "Darwin" else int(usage) * 1024


def train_stage2_model(dataset: LoadedStage2Dataset) -> TrainingRun:
    """locked configでTRAINを学習し、VALIDATIONでcheckpointを固定する。"""
    import torch

    runtime = configure_deterministic_runtime()
    tensors = load_split_tensors(dataset)
    train = tensors[Split.TRAIN]
    validation = tensors[Split.VALIDATION]

    model = create_model()
    optimizer = torch.optim.Adam(
        model.parameters(), lr=LEARNING_RATE, weight_decay=WEIGHT_DECAY
    )
    generator = torch.Generator()
    generator.manual_seed(DATALOADER_SEED)
    loader = torch.utils.data.DataLoader(
        torch.utils.data.TensorDataset(train.features, train.legal_mask, train.targets),
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
            raise Stage2ProtocolError("training epoch did not visit every TRAIN row")

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
        raise Stage2ProtocolError("training produced no validated checkpoint")
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
    """runtime measurementを除いたlogical checkpoint identityを返す。"""
    missing = [name for name in _LOGICAL_IDENTITY_FIELDS if name not in manifest]
    if missing:
        raise Stage2ArtifactError(f"checkpoint manifest is missing {missing}")
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
        "model_id": MODEL_ID,
        "input_dimension": FEATURE_DIMENSION,
        "hidden_layers": 1,
        "hidden_width": HIDDEN_WIDTH,
        "activation": "relu",
        "output_dimension": VOCABULARY_SIZE,
        "dropout": None,
        "normalization_layer": None,
    }


def save_checkpoint(
    destination: str | Path,
    dataset: LoadedStage2Dataset,
    run: TrainingRun,
) -> "LoadedCheckpoint":
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
            raise Stage2ArtifactError("staged state_dict readback differs")
        staging.rename(destination)
        published = True
    finally:
        if not published:
            rmtree(staging, ignore_errors=True)
    return load_checkpoint(destination)


@dataclass(frozen=True, slots=True)
class LoadedCheckpoint:
    """検証済みのfrozen checkpoint。"""

    path: Path
    manifest: dict
    model: object

    @property
    def identity(self) -> str:
        return self.manifest["checkpoint_identity"]

    @property
    def weights_sha256(self) -> str:
        return self.manifest["weights_sha256"]


def load_checkpoint(path: str | Path) -> LoadedCheckpoint:
    """checkpointを読み、identity / dimension / digestをfail closedで検証する。"""
    import torch

    verify_contract_identity()
    path = Path(path)
    if not path.is_dir():
        raise Stage2ArtifactError("checkpoint path is not a directory")
    if {item.name for item in path.iterdir()} != {
        MANIFEST_FILENAME,
        WEIGHTS_FILENAME,
    }:
        raise Stage2ArtifactError("checkpoint contains missing or extra files")

    manifest_text = (path / MANIFEST_FILENAME).read_text(encoding="utf-8")
    try:
        manifest = json.loads(manifest_text)
    except json.JSONDecodeError as error:
        raise Stage2ArtifactError("checkpoint manifest is not valid JSON") from error
    if type(manifest) is not dict:
        raise Stage2ArtifactError("checkpoint manifest must be an object")
    if manifest.get("checkpoint_schema_version") != CHECKPOINT_SCHEMA_VERSION:
        raise Stage2ArtifactError("unsupported checkpoint schema version")
    if canonical_json_text(manifest) != manifest_text:
        raise Stage2ArtifactError("checkpoint manifest bytes are not canonical JSON")
    if manifest.get("model") != _model_block():
        raise Stage2ArtifactError("checkpoint model config is not the locked one")
    if manifest.get("training") != _training_block():
        raise Stage2ArtifactError("checkpoint training config is not the locked one")
    if manifest.get("parameter_count") != EXPECTED_PARAMETER_COUNT:
        raise Stage2ArtifactError("checkpoint parameter count is not the locked one")
    if manifest.get("feature") != feature_block():
        raise Stage2ArtifactError(
            "checkpoint feature schema identity is not the locked Stage 2 one"
        )
    if manifest.get("vocabulary") != vocabulary_block():
        raise Stage2ArtifactError(
            "checkpoint action vocabulary identity is not the locked Stage 2 one"
        )
    if manifest.get("checkpoint_identity") != checkpoint_identity(manifest):
        raise Stage2ArtifactError(
            "checkpoint_identity does not match the manifest content"
        )

    weights = (path / WEIGHTS_FILENAME).read_bytes()
    if len(weights) != manifest.get("weights_bytes"):
        raise Stage2ArtifactError("checkpoint weights byte count differs")
    if _sha256(weights) != manifest.get("weights_sha256"):
        raise Stage2ArtifactError("checkpoint weights sha256 differs")

    state_dict = torch.load(
        path / WEIGHTS_FILENAME, weights_only=True, map_location="cpu"
    )
    model = create_model()
    try:
        model.load_state_dict(state_dict, strict=True)
    except RuntimeError as error:
        raise Stage2ArtifactError(
            "checkpoint state_dict does not match the locked model"
        ) from error
    model.eval()
    return LoadedCheckpoint(path=path, manifest=manifest, model=model)


__all__ = [
    "CHECKPOINT_SCHEMA_VERSION",
    "MANIFEST_FILENAME",
    "WEIGHTS_FILENAME",
    "EpochRecord",
    "LoadedCheckpoint",
    "SplitTensors",
    "TrainingRun",
    "checkpoint_identity",
    "choice_row_selector",
    "peak_process_ram_bytes",
    "configure_deterministic_runtime",
    "evaluate_masked_cross_entropy",
    "load_checkpoint",
    "load_split_tensors",
    "save_checkpoint",
    "train_stage2_model",
]
