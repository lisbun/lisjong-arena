"""Frozen-TEST serving-path safety check and inference microbenchmark.

TEST hanchanをlocked seedで再実行し、各decisionで

```text
actual DecisionContext
    -> PolicyInput feature (Stage 1 encoder)
    -> frozen model logits
    -> fixed 802 legal mask
    -> masked argmax index
    -> resolve_legal_action() -> canonical legal InternalAction
```

を通す。model出力はgameへ適用せず、teacher x4のexecutionをそのまま観測する
（学習modelがgameを駆動しない）。ここで確認するのは次である。

- teacher label legal-mask membership
- masked argmax illegal selection
- `resolve_legal_action()` failure
- 再実行featureとfrozen dataset rowのbit一致
- same tensor + same weights -> same logits / same selected index

Stage 3のserving-realistic Policy adapterはここでは作らない。
"""

import time
from array import array
from dataclasses import dataclass

from lisjong.action_vocabulary import (
    IllegalActionIndexError,
    build_legal_action_mask,
    resolve_legal_action,
)

from lisjong_arena.learned_policy_input import (
    build_policy_input_feature,
    tensor_values,
)

from .artifact import LoadedStage2Dataset
from .errors import Stage2EvaluationError
from .network import masked_argmax
from .protocol import Split, split_for_seed, verify_contract_identity
from .recording import (
    encode_teacher_action,
    iter_recorded_decisions,
    record_teacher_game,
)
from .training import LoadedCheckpoint


@dataclass(frozen=True, slots=True)
class InferenceLatency:
    """1 decisionあたりのmean latency（秒）。"""

    decisions: int
    feature_encode_seconds: float
    model_forward_seconds: float
    mask_select_resolve_seconds: float
    full_path_seconds: float

    def to_document(self) -> dict[str, object]:
        return {
            "decisions": self.decisions,
            "feature_encode_seconds": self.feature_encode_seconds,
            "model_forward_seconds": self.model_forward_seconds,
            "mask_select_resolve_seconds": self.mask_select_resolve_seconds,
            "full_path_seconds": self.full_path_seconds,
        }


@dataclass(frozen=True, slots=True)
class ServingPathReport:
    """frozen TEST上のsafety結果とlatency測定。"""

    seeds: tuple[int, ...]
    decisions: int
    teacher_label_legal: int
    illegal_selected_actions: int
    resolve_failures: int
    feature_mismatches: int
    legal_mask_mismatches: int
    teacher_index_mismatches: int
    nondeterministic_logits: int
    nondeterministic_selections: int
    latency: InferenceLatency

    @property
    def teacher_label_legal_share(self) -> float:
        return self.teacher_label_legal / self.decisions

    @property
    def passed(self) -> bool:
        return (
            self.decisions > 0
            and self.teacher_label_legal == self.decisions
            and self.illegal_selected_actions == 0
            and self.resolve_failures == 0
            and self.feature_mismatches == 0
            and self.legal_mask_mismatches == 0
            and self.teacher_index_mismatches == 0
            and self.nondeterministic_logits == 0
            and self.nondeterministic_selections == 0
        )

    def to_document(self) -> dict[str, object]:
        return {
            "seeds": list(self.seeds),
            "decisions": self.decisions,
            "teacher_label_legal": self.teacher_label_legal,
            "teacher_label_legal_share": self.teacher_label_legal_share,
            "illegal_selected_actions": self.illegal_selected_actions,
            "resolve_failures": self.resolve_failures,
            "feature_mismatches": self.feature_mismatches,
            "legal_mask_mismatches": self.legal_mask_mismatches,
            "teacher_index_mismatches": self.teacher_index_mismatches,
            "nondeterministic_logits": self.nondeterministic_logits,
            "nondeterministic_selections": self.nondeterministic_selections,
            "latency": self.latency.to_document(),
            "passed": self.passed,
        }


def run_serving_path_check(
    checkpoint: LoadedCheckpoint,
    dataset: LoadedStage2Dataset,
    *,
    split: Split = Split.TEST,
) -> ServingPathReport:
    """frozen checkpointを、再実行したTEST decision上でserving pathとして通す。"""
    import torch

    verify_contract_identity()
    if not isinstance(checkpoint, LoadedCheckpoint):
        raise TypeError("checkpoint must be a LoadedCheckpoint")
    if not isinstance(dataset, LoadedStage2Dataset):
        raise TypeError("dataset must be a LoadedStage2Dataset")
    if checkpoint.manifest["dataset_identity"] != dataset.identity:
        raise Stage2EvaluationError(
            "checkpoint was not trained on this dataset identity"
        )

    model = checkpoint.model
    model.eval()
    seeds = tuple(sorted({row.seed for row in dataset.rows if row.split is split}))
    if not seeds:
        raise Stage2EvaluationError(f"{split.value} split has no games")

    stored = {
        seed: tuple(index for index, row in enumerate(dataset.rows) if row.seed == seed)
        for seed in seeds
    }

    decisions = 0
    teacher_label_legal = 0
    illegal_selected = 0
    resolve_failures = 0
    feature_mismatches = 0
    mask_mismatches = 0
    teacher_mismatches = 0
    nondeterministic_logits = 0
    nondeterministic_selections = 0
    encode_total = 0.0
    forward_total = 0.0
    resolve_total = 0.0
    full_total = 0.0

    for seed in seeds:
        if split_for_seed(seed) is not split:
            raise Stage2EvaluationError(f"seed {seed} is not in {split.value}")
        recording = record_teacher_game(seed)
        row_indices = stored[seed]
        seed_decisions = 0
        for decision in iter_recorded_decisions(recording):
            if decision.decision_ordinal >= len(row_indices):
                raise Stage2EvaluationError(
                    f"seed {seed} produced more decisions than the frozen dataset"
                )
            record = dataset.rows[row_indices[decision.decision_ordinal]]

            full_start = time.perf_counter()
            encode_start = time.perf_counter()
            values = tensor_values(build_policy_input_feature(decision.context.input))
            encode_seconds = time.perf_counter() - encode_start

            features = torch.tensor(values, dtype=torch.float32).unsqueeze(0)
            forward_start = time.perf_counter()
            with torch.no_grad():
                logits = model(features)
            forward_seconds = time.perf_counter() - forward_start

            select_start = time.perf_counter()
            legal_mask = build_legal_action_mask(decision.context)
            mask_tensor = torch.tensor(legal_mask, dtype=torch.bool).unsqueeze(0)
            selected_index = int(masked_argmax(logits, mask_tensor)[0])
            try:
                resolved = resolve_legal_action(selected_index, decision.context)
            except IllegalActionIndexError:
                resolved = None
                resolve_failures += 1
            select_seconds = time.perf_counter() - select_start
            full_seconds = time.perf_counter() - full_start

            encode_total += encode_seconds
            forward_total += forward_seconds
            resolve_total += select_seconds
            full_total += full_seconds

            if not legal_mask[selected_index]:
                illegal_selected += 1
            if resolved is not None and not any(
                resolved is candidate for candidate in decision.context.legal_actions
            ):
                resolve_failures += 1

            teacher_index = encode_teacher_action(decision)
            if legal_mask[teacher_index]:
                teacher_label_legal += 1
            if teacher_index != record.teacher_action_index:
                teacher_mismatches += 1
            if legal_mask != dataset.legal_mask_row(
                row_indices[decision.decision_ordinal]
            ):
                mask_mismatches += 1
            stored_features = dataset.feature_row(
                row_indices[decision.decision_ordinal]
            )
            if stored_features.tobytes() != array("f", values).tobytes():
                feature_mismatches += 1

            with torch.no_grad():
                repeated = model(features)
            if not bool(torch.equal(repeated, logits)):
                nondeterministic_logits += 1
            if int(masked_argmax(repeated, mask_tensor)[0]) != selected_index:
                nondeterministic_selections += 1

            decisions += 1
            seed_decisions += 1

        if seed_decisions != len(row_indices):
            raise Stage2EvaluationError(
                f"seed {seed} produced fewer decisions than the frozen dataset"
            )

    return ServingPathReport(
        seeds=seeds,
        decisions=decisions,
        teacher_label_legal=teacher_label_legal,
        illegal_selected_actions=illegal_selected,
        resolve_failures=resolve_failures,
        feature_mismatches=feature_mismatches,
        legal_mask_mismatches=mask_mismatches,
        teacher_index_mismatches=teacher_mismatches,
        nondeterministic_logits=nondeterministic_logits,
        nondeterministic_selections=nondeterministic_selections,
        latency=InferenceLatency(
            decisions=decisions,
            feature_encode_seconds=encode_total / decisions,
            model_forward_seconds=forward_total / decisions,
            mask_select_resolve_seconds=resolve_total / decisions,
            full_path_seconds=full_total / decisions,
        ),
    )


__all__ = [
    "InferenceLatency",
    "ServingPathReport",
    "run_serving_path_check",
]
