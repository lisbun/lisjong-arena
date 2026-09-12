"""両armを同じflat-BC trainerで1回ずつ学習するtraining boundary。

```text
Arm Y   exact retained #140/#190 S20 TRAIN 9,116 / VALIDATION 2,555
Arm R   Gate 0 materialized TRAIN 9,116 / VALIDATION 2,555
            |
            v
same tensors contract (8204 float32 / 802 bool / int64 teacher index)
            |
            v
learned_policy_offline_q.bc_training.train_from_split_tensors()
    Linear(8204,128) + ReLU + Linear(128,802)
    masked cross entropy over exact legal actions
    Adam lr=1e-3 weight_decay=0 / batch 256 / <=20 epochs / patience 4
    training seed 0 / dataloader seed 0 / workers 0 / torch threads 1
    checkpoint selection = lowest own-source VALIDATION choice-row masked CE
```

trainerは新規に書かない。generic trainer framework、architecture変更、
class weighting、oversampling、label smoothing、HPO、複数training seed、
unmasked CE fallbackを導入しない。片armだけに適用するguardも持たない。

Arm YはTEST 271..276をmetadataでしか触らないTEST-blind loaderを使う
(`learned_policy_data_sufficiency.source`)。このmoduleはTEST rowを
読む入口を持たない。
"""

import hashlib
import platform
import time
from collections import Counter
from dataclasses import dataclass

from lisjong_arena._artifact_io import canonical_json_text
from lisjong_arena.learned_policy_data_sufficiency.source import (
    LoadedDataSufficiencySource,
    load_source_dataset,
    scale_tensors,
)
from lisjong_arena.learned_policy_offline_q.bc_training import (
    TrainingRun,
    evaluate_masked_cross_entropy,
    train_from_split_tensors,
)
from lisjong_arena.learned_policy_offline_q.protocol import Split, action_family
from lisjong_arena.learned_policy_stage2.protocol import TORCH_THREADS

from .dataset import RowBudget, dataset_identity_document, rows_identity
from .errors import SourcePilotProtocolError
from .materialization import MaterializedRow
from .protocol import (
    ARM_R,
    ARM_SOURCE_IDENTITY,
    ARM_Y,
    ARM_Y_DATASET_IDENTITY,
    ARM_Y_TEST_SEEDS_NEVER_READ,
    ARM_Y_TRAIN_SEEDS,
    ARM_Y_VALIDATION_SEEDS,
    FEATURE_DIMENSION,
    MODEL_BLOCK,
    TRAIN_ROW_BUDGET,
    TRAINING_BLOCK,
    VALIDATION_ROW_BUDGET,
    VOCABULARY_SIZE,
    Arm,
    feature_block,
    require_arm,
    vocabulary_block,
)

#: Arm Yのretained sourceからTRAIN budgetへ対応するscale。#190のS20が
#: exactに9,116 TRAIN rowsである。
ARM_Y_SCALE = "S20"


@dataclass(frozen=True, slots=True)
class ArmSplitTensors:
    """`train_from_split_tensors()`が読むtensor surfaceだけを持つvalue。"""

    split: Split
    features: object
    legal_mask: object
    behavior_action_index: object

    @property
    def row_count(self) -> int:
        return int(self.features.shape[0])


def _require_row_budget(rows: tuple[MaterializedRow, ...], expected: int) -> None:
    if len(rows) != expected:
        raise SourcePilotProtocolError(
            "training tensors must carry exactly the locked row budget"
        )


def materialized_tensors(budget: RowBudget) -> dict[Split, ArmSplitTensors]:
    """Gate 0 rowからArm RのTRAIN / VALIDATION tensorを作る。

    featureはGate 0が`arena-policy-input-feature-v1`から生成した値をそのまま
    使う。ここでre-encode、normalize、reorderしない。
    """
    import torch

    _require_row_budget(budget.train_rows, TRAIN_ROW_BUDGET)
    _require_row_budget(budget.validation_rows, VALIDATION_ROW_BUDGET)

    tensors: dict[Split, ArmSplitTensors] = {}
    for split, rows in (
        (Split.TRAIN, budget.train_rows),
        (Split.VALIDATION, budget.validation_rows),
    ):
        # rowはfixed-size float32 / uint8 payloadを保持しているので、
        # 値を1つずつPython objectへ戻さずbufferのまま読み込む。
        feature_buffer = bytearray().join(row.feature_payload for row in rows)
        features = (
            torch.frombuffer(feature_buffer, dtype=torch.float32)
            .reshape(len(rows), FEATURE_DIMENSION)
            .clone()
        )
        if not bool(torch.isfinite(features).all()):
            raise SourcePilotProtocolError("materialized features are not finite")
        mask_buffer = bytearray().join(row.legal_mask_payload for row in rows)
        legal_mask = (
            torch.frombuffer(mask_buffer, dtype=torch.uint8)
            .reshape(len(rows), VOCABULARY_SIZE)
            .bool()
            .clone()
        )
        behavior = torch.tensor(
            [row.teacher_action_index for row in rows], dtype=torch.long
        )
        if not bool(legal_mask.gather(1, behavior.unsqueeze(1)).all()):
            raise SourcePilotProtocolError("a teacher action is outside its legal mask")
        tensors[split] = ArmSplitTensors(
            split=split,
            features=features.contiguous(),
            legal_mask=legal_mask.contiguous(),
            behavior_action_index=behavior.contiguous(),
        )
    return tensors


def retained_tensors(
    source: LoadedDataSufficiencySource,
) -> dict[Split, ArmSplitTensors]:
    """Arm Yのexact retained S20 TRAIN / VALIDATION tensorをstrict-readする。"""
    if not isinstance(source, LoadedDataSufficiencySource):
        raise TypeError("source must be a LoadedDataSufficiencySource")
    if source.identity != ARM_Y_DATASET_IDENTITY:
        raise SourcePilotProtocolError(
            "the retained Arm Y dataset identity is not the exact locked identity"
        )
    subsets = scale_tensors(source, ARM_Y_SCALE)
    tensors: dict[Split, ArmSplitTensors] = {}
    expected = {Split.TRAIN: TRAIN_ROW_BUDGET, Split.VALIDATION: VALIDATION_ROW_BUDGET}
    for split, subset in subsets.items():
        if subset.row_count != expected[split]:
            raise SourcePilotProtocolError(
                f"retained Arm Y {split.value} rows are not the locked budget"
            )
        tensors[split] = ArmSplitTensors(
            split=split,
            features=subset.features,
            legal_mask=subset.legal_mask,
            behavior_action_index=subset.behavior_action_index,
        )
    return tensors


def load_retained_arm_y_source(path) -> LoadedDataSufficiencySource:
    """exact retained #140/#190 datasetをTEST-blindにstrict-readする。"""
    source = load_source_dataset(path)
    protocol = source.manifest["protocol"]
    if tuple(protocol["train_seeds"]) != ARM_Y_TRAIN_SEEDS:
        raise SourcePilotProtocolError("retained Arm Y TRAIN population is not exact")
    if tuple(protocol["validation_seeds"]) != ARM_Y_VALIDATION_SEEDS:
        raise SourcePilotProtocolError(
            "retained Arm Y VALIDATION population is not exact"
        )
    if tuple(protocol["test_seeds"]) != ARM_Y_TEST_SEEDS_NEVER_READ:
        raise SourcePilotProtocolError(
            "retained Arm Y TEST metadata population is not exact"
        )
    return source


@dataclass(frozen=True, slots=True)
class ArmTrainingResult:
    """1 armのtraining結果とdiagnostics。"""

    arm: Arm
    run: TrainingRun
    train_row_count: int
    validation_row_count: int
    train_choice_masked_ce: float
    validation_choice_masked_ce: float
    validation_choice_rows: int
    cpu_seconds: float
    source_document: dict[str, object]

    @property
    def model(self):
        return self.run.model

    def diagnostics_document(self) -> dict[str, object]:
        return {
            "selected_epoch": self.run.selected_epoch,
            "epochs_run": len(self.run.history),
            "train_choice_masked_ce": self.train_choice_masked_ce,
            "validation_choice_masked_ce": self.validation_choice_masked_ce,
            "validation_choice_rows": self.validation_choice_rows,
            "history": [record.to_document() for record in self.run.history],
            "training_wall_clock_seconds": self.run.wall_clock_seconds,
            "training_cpu_seconds": self.cpu_seconds,
            "peak_process_ram_bytes": self.run.peak_process_ram_bytes,
            "runtime": dict(self.run.runtime),
        }


def training_config_identity() -> str:
    """両armが共有するtraining config identity。"""
    document = {
        "model": dict(MODEL_BLOCK),
        "training": dict(TRAINING_BLOCK),
        "feature": feature_block(),
        "vocabulary": vocabulary_block(),
        "torch_threads": TORCH_THREADS,
        "python_implementation": platform.python_implementation(),
    }
    return hashlib.sha256(canonical_json_text(document).encode("utf-8")).hexdigest()


def train_arm(
    arm: Arm,
    tensors: dict[Split, ArmSplitTensors],
    *,
    source_document: dict[str, object],
) -> ArmTrainingResult:
    """1 armをlocked trainerで1回学習する。

    TRAIN / VALIDATIONはそのarm自身のsourceだけから来る。もう一方の
    sourceのVALIDATIONは読まない。
    """
    arm = require_arm(arm)
    missing = [
        split for split in (Split.TRAIN, Split.VALIDATION) if split not in tensors
    ]
    if missing:
        raise SourcePilotProtocolError(
            f"training requires {[split.value for split in missing]} tensors"
        )
    cpu_start = time.process_time()
    run = train_from_split_tensors(tensors)
    cpu_seconds = time.process_time() - cpu_start

    train_ce, _ = evaluate_masked_cross_entropy(run.model, tensors[Split.TRAIN])
    validation_ce, validation_rows = evaluate_masked_cross_entropy(
        run.model, tensors[Split.VALIDATION]
    )
    if abs(validation_ce - run.selected_validation_choice_masked_ce) > 1e-9:
        raise SourcePilotProtocolError(
            "the frozen model does not reproduce its selected VALIDATION metric"
        )
    return ArmTrainingResult(
        arm=arm,
        run=run,
        train_row_count=tensors[Split.TRAIN].row_count,
        validation_row_count=tensors[Split.VALIDATION].row_count,
        train_choice_masked_ce=train_ce,
        validation_choice_masked_ce=validation_ce,
        validation_choice_rows=validation_rows,
        cpu_seconds=cpu_seconds,
        source_document=source_document,
    )


def _retained_partition_distribution(
    source: LoadedDataSufficiencySource, seeds: tuple[int, ...]
) -> dict[str, object]:
    """Arm Rと同じ形でArm Yのrow分布をdescriptiveに記録する。

    reweightのための値ではない。両armのsource差を同じ粒度で読めるように
    するためだけの診断である。
    """
    rows = [source.rows[index] for index in source.indices_for_seeds(seeds)]
    return {
        "rows": len(rows),
        "partition_games": len(seeds),
        "represented_games": len({row.seed for row in rows}),
        "target_participations": len({(row.seed, row.actor_seat) for row in rows}),
        "action_family_counts": dict(
            sorted(
                Counter(
                    action_family(row.behavior_action_index) for row in rows
                ).items()
            )
        ),
        "rows_per_seat": dict(
            sorted(
                (str(seat), count)
                for seat, count in Counter(row.actor_seat for row in rows).items()
            )
        ),
    }


def arm_y_source_document(source: LoadedDataSufficiencySource) -> dict[str, object]:
    return {
        "arm": ARM_Y.value,
        "source_identity": ARM_SOURCE_IDENTITY[ARM_Y],
        "dataset_identity": source.identity,
        "scale": ARM_Y_SCALE,
        "train_seeds": list(ARM_Y_TRAIN_SEEDS),
        "validation_seeds": list(ARM_Y_VALIDATION_SEEDS),
        "test_seeds_never_read": list(ARM_Y_TEST_SEEDS_NEVER_READ),
        "train_rows_identity": _retained_rows_identity(source, ARM_Y_TRAIN_SEEDS),
        "validation_rows_identity": _retained_rows_identity(
            source, ARM_Y_VALIDATION_SEEDS
        ),
        "provenance": dict(source.provenance),
        "feature": feature_block(),
        "vocabulary": vocabulary_block(),
        "distribution": {
            "train": _retained_partition_distribution(source, ARM_Y_TRAIN_SEEDS),
            "validation": _retained_partition_distribution(
                source, ARM_Y_VALIDATION_SEEDS
            ),
        },
    }


def _retained_rows_identity(
    source: LoadedDataSufficiencySource, seeds: tuple[int, ...]
) -> str:
    digest = hashlib.sha256()
    digest.update(
        f"arena-riichilab-source-pilot-retained-rows|{source.identity}\n".encode()
    )
    for index in source.indices_for_seeds(seeds):
        row = source.rows[index]
        digest.update(
            f"{row.seed}|{row.round_ordinal}|{row.actor_seat}|"
            f"{row.decision_ordinal}|{row.behavior_action_index}\n".encode()
        )
    return digest.hexdigest()


def arm_r_source_document(source, budget: RowBudget) -> dict[str, object]:
    document = {"arm": ARM_R.value, "source_identity": ARM_SOURCE_IDENTITY[ARM_R]}
    document.update(dataset_identity_document(source, budget))
    document["dataset_identity"] = hashlib.sha256(
        canonical_json_text(document).encode("utf-8")
    ).hexdigest()
    return document


__all__ = [
    "ARM_Y_SCALE",
    "ArmSplitTensors",
    "ArmTrainingResult",
    "arm_r_source_document",
    "arm_y_source_document",
    "load_retained_arm_y_source",
    "materialized_tensors",
    "retained_tensors",
    "rows_identity",
    "train_arm",
    "training_config_identity",
]
