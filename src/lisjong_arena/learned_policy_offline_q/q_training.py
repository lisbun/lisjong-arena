"""Arm B -- support-restricted Offline Q training path and frozen checkpoint.

```text
Offline Q dataset (eligible ordinary-discard source states only)
    -> split tensors (whole-hanchan membership from the manifest)
    -> fixed 1x128 MLP (same shape as Stage 2 / Arm A)
    -> Q(s, a) for every legal action
    -> TD target y = r                                            (terminal)
              y = r + gamma * max_a' Q_target(s', a')              (nonterminal,
                    a' restricted to next-state legal actions that are also
                    TRAIN-supported)
    -> Huber(Q(s, a_behavior), y), Adam(lr=1e-3, weight_decay=0),
       batch 256, fixed MAXIMUM_EPOCHS outer iterations, epoch-level hard target sync
    -> final iteration's model (no cross-iteration VALIDATION selection)
    -> frozen checkpoint artifact + sha256
```

Stage 2の1x128 MLP実装をQ-value modelとしてそのまま再利用する。target network
はepoch開始時にonline networkの重みをhard syncするだけで、1 epoch内は固定
した target に対して回帰する（moving targetへの直接依存を避ける）。

**checkpoint selectionはVALIDATION lossの epoch間比較で行わない。** fitted-Q
のouter iterationごとにbootstrap targetそのものが変わるため、
``y_k = r + gamma * max Q_{k-1}(s')`` に対する``loss(Q_k, y_k)``は
epoch間で同じ尺度を測っていない。value伝播が浅い初期epochほどtargetが
単純でlossが小さく見えるだけの場合があり、これを「良いcheckpoint」として
拾うと伝播が終わる前のmodelを誤選択する。したがって固定回数
``MAXIMUM_EPOCHS``のouter iterationを実行し、**最終iterationのmodelを
無条件に採用する**。VALIDATION Huber lossは全epochについて診断用historyへ
記録するが、selection criterionとしては使わない。TESTはこのmoduleへ渡さない。
VALIDATION metricsはstrength evidenceとして扱わない。
"""

import hashlib
import time
from dataclasses import dataclass
from pathlib import Path
from shutil import rmtree
from tempfile import mkdtemp

from lisjong_arena._artifact_io import canonical_json_text
from lisjong_arena.learned_policy_stage2.network import create_model, parameter_count

from .artifact import LoadedOfflineQDataset, feature_block, vocabulary_block
from .bc_training import configure_deterministic_runtime, peak_process_ram_bytes
from .errors import OfflineQArtifactError, OfflineQProtocolError
from .protocol import (
    BATCH_SIZE,
    DATALOADER_SEED,
    DATALOADER_WORKERS,
    DETERMINISTIC_ALGORITHMS,
    EXPECTED_PARAMETER_COUNT,
    FEATURE_DIMENSION,
    GAMMA,
    HIDDEN_WIDTH,
    HUBER_LOSS_DELTA,
    LEARNING_RATE,
    MAXIMUM_EPOCHS,
    Q_MODEL_ID,
    TARGET_SYNC_CADENCE,
    TORCH_THREADS,
    TRAINING_SEED,
    VOCABULARY_SIZE,
    WEIGHT_DECAY,
    Split,
    verify_contract_identity,
)
from .q_network import masked_max_q, q_value_at
from .split_tensors import OfflineQSplitTensors, load_split_tensors
from .support import support_set_identity

CHECKPOINT_SCHEMA_VERSION = "arena-learned-policy-offlineq-q-checkpoint-v1"
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
    "final_validation_huber_loss",
    "parameter_count",
    "weights_sha256",
    "supported_indices_digest",
)


def train_support_mask(train: OfflineQSplitTensors):
    """TRAIN上で1回以上selectされたexact discard vocabulary indexのbool mask。"""
    import torch

    mask = torch.zeros(VOCABULARY_SIZE, dtype=torch.bool)
    mask[train.behavior_action_index.unique()] = True
    return mask


def compute_td_targets(target_model, tensors: OfflineQSplitTensors, support_mask):
    """support-restricted fitted TD targetを、固定したtarget networkで計算する。"""
    import torch

    with torch.no_grad():
        targets = tensors.reward.clone()
        nonterminal = ~tensors.terminal
        if bool(nonterminal.any()):
            next_q = target_model(tensors.next_features[nonterminal])
            next_legal = tensors.next_legal_mask[nonterminal]
            unsupported_next_legal = next_legal & ~support_mask.unsqueeze(0)
            if bool(unsupported_next_legal.any()):
                raise OfflineQProtocolError(
                    "a nonterminal transition has a next legal discard action "
                    "that is not TRAIN-supported; the locked contract requires "
                    "every next-state legal discard index to be TRAIN-supported "
                    "before that transition may be used for the fitted-Q "
                    "bootstrap target (see the support gate report -- "
                    "OFFLINE Q DATA COVERAGE BLOCKED)"
                )
            restricted_mask = next_legal & support_mask.unsqueeze(0)
            max_next_q = masked_max_q(next_q, restricted_mask)
            targets[nonterminal] = (
                tensors.reward[nonterminal] + GAMMA * max_next_q.detach()
            )
    return targets


def evaluate_huber_loss(
    model, target_model, tensors: OfflineQSplitTensors, support_mask
) -> tuple[float, int]:
    """全rowのmean selected-action Huber lossと、その母数を返す。"""
    import torch

    count = tensors.row_count
    if count == 0:
        raise OfflineQProtocolError(f"{tensors.split.value} split contains no rows")
    targets = compute_td_targets(target_model, tensors, support_mask)
    model.eval()
    total = 0.0
    with torch.no_grad():
        for start in range(0, count, BATCH_SIZE):
            stop = min(start + BATCH_SIZE, count)
            q_values = model(tensors.features[start:stop])
            selected = q_value_at(q_values, tensors.behavior_action_index[start:stop])
            losses = torch.nn.functional.huber_loss(
                selected, targets[start:stop], delta=HUBER_LOSS_DELTA, reduction="none"
            )
            total += float(losses.sum())
    return total / count, count


@dataclass(frozen=True, slots=True)
class EpochRecord:
    epoch: int
    train_huber_loss: float
    validation_huber_loss: float

    def to_document(self) -> dict[str, object]:
        return {
            "epoch": self.epoch,
            "train_huber_loss": self.train_huber_loss,
            "validation_huber_loss": self.validation_huber_loss,
        }


@dataclass(frozen=True, slots=True)
class TrainingRun:
    """fixed MAXIMUM_EPOCHS outer iterationのfinal iteration結果。

    ``selected_epoch``は常に``MAXIMUM_EPOCHS``（=最終iteration）であり、
    VALIDATION lossを比較して選んだ値ではない。moving targetの下で
    epoch間のVALIDATION Huber lossを比較して checkpoint を選ぶと、
    value伝播が浅い初期epochを誤って「良い」と判定し得るため
    （bootstrap targetそのものがepochごとに変わる）、この比較は行わない。
    """

    model: object
    support_mask: object
    history: tuple[EpochRecord, ...]
    selected_epoch: int
    final_validation_huber_loss: float
    wall_clock_seconds: float
    peak_process_ram_bytes: int | None
    runtime: dict[str, object]


def train_q_model(dataset: LoadedOfflineQDataset) -> TrainingRun:
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
    support_mask = train_support_mask(train)

    model = create_model()
    target_model = create_model()
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
        num_workers=DATALOADER_WORKERS,
        generator=generator,
        drop_last=False,
    )

    history: list[EpochRecord] = []
    start = time.perf_counter()

    for epoch in range(1, MAXIMUM_EPOCHS + 1):
        target_model.load_state_dict(model.state_dict())
        epoch_targets = compute_td_targets(target_model, train, support_mask)

        model.train()
        total = 0.0
        seen = 0
        for (batch_indices,) in loader:
            optimizer.zero_grad(set_to_none=True)
            features = train.features[batch_indices]
            behavior = train.behavior_action_index[batch_indices]
            batch_targets = epoch_targets[batch_indices]
            q_values = model(features)
            selected = q_value_at(q_values, behavior)
            losses = torch.nn.functional.huber_loss(
                selected, batch_targets, delta=HUBER_LOSS_DELTA, reduction="none"
            )
            loss = losses.mean()
            loss.backward()
            optimizer.step()
            total += float(losses.detach().sum())
            seen += int(behavior.shape[0])
        if seen != train.row_count:
            raise OfflineQProtocolError("training epoch did not visit every TRAIN row")

        # VALIDATIONはdiagnostic historyとしてのみ記録する。moving target下で
        # epoch間のHuber lossを比較してcheckpointを選ぶと、value伝播が浅い
        # epochを誤って「良い」と判定し得るため、selectionには使わない。
        validation_metric, _ = evaluate_huber_loss(
            model, target_model, validation, support_mask
        )
        history.append(
            EpochRecord(
                epoch=epoch,
                train_huber_loss=total / seen,
                validation_huber_loss=validation_metric,
            )
        )

    wall_clock = time.perf_counter() - start
    if not history:
        raise OfflineQProtocolError("training produced no completed outer iteration")
    return TrainingRun(
        model=model,
        support_mask=support_mask,
        history=tuple(history),
        selected_epoch=history[-1].epoch,
        final_validation_huber_loss=history[-1].validation_huber_loss,
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
        "loss": "huber_selected_action_td_target",
        "gamma": GAMMA,
        "target_sync_cadence": TARGET_SYNC_CADENCE,
        "huber_loss_delta": HUBER_LOSS_DELTA,
        "optimizer": "adam",
        "learning_rate": LEARNING_RATE,
        "weight_decay": WEIGHT_DECAY,
        "batch_size": BATCH_SIZE,
        "maximum_epochs": MAXIMUM_EPOCHS,
        "training_seed": TRAINING_SEED,
        "dataloader_seed": DATALOADER_SEED,
        "dataloader_workers": DATALOADER_WORKERS,
        "torch_threads": TORCH_THREADS,
        "deterministic_algorithms": DETERMINISTIC_ALGORITHMS,
        "checkpoint_selection": "fixed_final_iteration",
    }


def _model_block() -> dict[str, object]:
    return {
        "model_id": Q_MODEL_ID,
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
) -> "LoadedQCheckpoint":
    """frozen checkpointをstagingで検証してから公開し、strict readbackを返す。

    support maskもcheckpointへ固定する: serving hybridはTRAIN-supportedかどうかを
    runtimeで再計算せず、ここで固定した値をそのまま使う。
    """
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
        supported_indices = sorted(
            int(index) for index in torch.nonzero(run.support_mask).flatten().tolist()
        )
        manifest: dict[str, object] = {
            "checkpoint_schema_version": CHECKPOINT_SCHEMA_VERSION,
            "dataset_identity": dataset.identity,
            "feature": dict(dataset.manifest["feature"]),
            "vocabulary": dict(dataset.manifest["vocabulary"]),
            "model": _model_block(),
            "training": _training_block(),
            "parameter_count": parameter_count(run.model),
            "supported_indices": supported_indices,
            "supported_indices_digest": support_set_identity(supported_indices),
            "selected_epoch": run.selected_epoch,
            "final_validation_huber_loss": run.final_validation_huber_loss,
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
class LoadedQCheckpoint:
    path: Path
    manifest: dict
    model: object
    supported_indices: frozenset[int]

    @property
    def identity(self) -> str:
        return self.manifest["checkpoint_identity"]

    @property
    def weights_sha256(self) -> str:
        return self.manifest["weights_sha256"]


def load_checkpoint(path: str | Path) -> LoadedQCheckpoint:
    """checkpointを読み、identity / dimension / digest / support setをfail closedで検証する。"""
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
    supported_indices = manifest.get("supported_indices")
    if (
        type(supported_indices) is not list
        or any(type(index) is not int for index in supported_indices)
        or any(not 0 <= index < VOCABULARY_SIZE for index in supported_indices)
        or sorted(supported_indices) != supported_indices
        or len(set(supported_indices)) != len(supported_indices)
    ):
        raise OfflineQArtifactError("checkpoint supported_indices is malformed")
    if manifest.get("supported_indices_digest") != support_set_identity(
        supported_indices
    ):
        # supported_indices_digest (not the raw list) is what checkpoint_identity()
        # hashes below, so a tampered raw list with a stale-but-matching digest
        # would otherwise slip past the checkpoint_identity check entirely.
        raise OfflineQArtifactError(
            "checkpoint supported_indices does not match its own digest"
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
    return LoadedQCheckpoint(
        path=path,
        manifest=manifest,
        model=model,
        supported_indices=frozenset(supported_indices),
    )


__all__ = [
    "CHECKPOINT_SCHEMA_VERSION",
    "MANIFEST_FILENAME",
    "WEIGHTS_FILENAME",
    "EpochRecord",
    "LoadedQCheckpoint",
    "TrainingRun",
    "checkpoint_identity",
    "compute_td_targets",
    "evaluate_huber_loss",
    "load_checkpoint",
    "locked_model_block",
    "locked_training_block",
    "save_checkpoint",
    "train_from_split_tensors",
    "train_q_model",
    "train_support_mask",
]
