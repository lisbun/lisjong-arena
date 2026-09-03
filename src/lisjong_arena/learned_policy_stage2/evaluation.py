"""Stage 2 prediction metrics, conditional-uniform reference, and TEST discipline.

forced decision (`len(legal_actions) == 1`) はmodel qualityを人工的に高く見せる
ため、primary metricから分離する。

```text
primary    choice rows (len(legal_actions) >= 2)
             masked CE / exact agreement / top-3 / top-5
             conditional-uniform legal baseline
             per-hanchan metrics
secondary  all-row masked CE / exact agreement
             forced-row count / share
             action-family agreement
             frequent / rare selected-index diagnostics
             TRAIN / VALIDATION / TEST gap
```

teacher agreementはdecision qualityでもgame strengthでもない。ここで計算する
のはteacher action予測のagreementだけである。
"""

import math
from dataclasses import dataclass

from .artifact import LoadedStage2Dataset, Stage2RowRecord
from .errors import Stage2EvaluationError
from .network import masked_cross_entropy, masked_top_indices
from .protocol import ACTION_FAMILY_NAMES, BATCH_SIZE, Split
from .training import SplitTensors, choice_row_selector

TOP_K_VALUES = (3, 5)
FREQUENT_INDEX_COUNT = 10
RARE_INDEX_MAXIMUM_TRAIN_COUNT = 2


@dataclass(frozen=True, slots=True)
class AgreementBucket:
    """1つの部分集合のrow数とexact agreement。"""

    name: str
    rows: int
    exact_agreement: float | None

    def to_document(self) -> dict[str, object]:
        return {
            "name": self.name,
            "rows": self.rows,
            "exact_agreement": self.exact_agreement,
        }


@dataclass(frozen=True, slots=True)
class HanchanMetric:
    """1 hanchanのchoice-row metric。"""

    seed: int
    choice_rows: int
    masked_cross_entropy: float
    exact_agreement: float

    def to_document(self) -> dict[str, object]:
        return {
            "seed": self.seed,
            "choice_rows": self.choice_rows,
            "masked_cross_entropy": self.masked_cross_entropy,
            "exact_agreement": self.exact_agreement,
        }


@dataclass(frozen=True, slots=True)
class SplitMetrics:
    """1 splitのprimary / secondary metric。"""

    split: Split
    total_rows: int
    forced_rows: int
    choice_rows: int
    choice_masked_cross_entropy: float
    choice_exact_agreement: float
    choice_top_k_agreement: tuple[tuple[int, float], ...]
    uniform_choice_cross_entropy: float
    uniform_choice_exact_agreement: float
    all_row_masked_cross_entropy: float
    all_row_exact_agreement: float
    family_agreement: tuple[AgreementBucket, ...]
    index_diagnostics: tuple[AgreementBucket, ...]
    per_hanchan: tuple[HanchanMetric, ...]
    illegal_selection_count: int

    @property
    def forced_row_share(self) -> float:
        return self.forced_rows / self.total_rows

    @property
    def choice_cross_entropy_improvement(self) -> float:
        """conditional-uniform legal baselineに対するmasked CEの改善量。"""
        return self.uniform_choice_cross_entropy - self.choice_masked_cross_entropy

    def to_document(self) -> dict[str, object]:
        return {
            "split": self.split.value,
            "total_rows": self.total_rows,
            "forced_rows": self.forced_rows,
            "forced_row_share": self.forced_row_share,
            "choice_rows": self.choice_rows,
            "choice_masked_cross_entropy": self.choice_masked_cross_entropy,
            "choice_exact_agreement": self.choice_exact_agreement,
            "choice_top_k_agreement": [
                [k, value] for k, value in self.choice_top_k_agreement
            ],
            "uniform_choice_cross_entropy": self.uniform_choice_cross_entropy,
            "uniform_choice_exact_agreement": self.uniform_choice_exact_agreement,
            "choice_cross_entropy_improvement": (self.choice_cross_entropy_improvement),
            "all_row_masked_cross_entropy": self.all_row_masked_cross_entropy,
            "all_row_exact_agreement": self.all_row_exact_agreement,
            "family_agreement": [
                bucket.to_document() for bucket in self.family_agreement
            ],
            "index_diagnostics": [
                bucket.to_document() for bucket in self.index_diagnostics
            ],
            "per_hanchan": [metric.to_document() for metric in self.per_hanchan],
            "illegal_selection_count": self.illegal_selection_count,
        }


def _bucket(name: str, correct: int, rows: int) -> AgreementBucket:
    return AgreementBucket(
        name=name,
        rows=rows,
        exact_agreement=None if rows == 0 else correct / rows,
    )


def train_index_counts(dataset: LoadedStage2Dataset) -> dict[int, int]:
    """TRAIN splitのteacher selected index頻度。TESTを見て決めない。"""
    counts: dict[int, int] = {}
    for row in dataset.rows:
        if row.split is Split.TRAIN:
            counts[row.teacher_action_index] = (
                counts.get(row.teacher_action_index, 0) + 1
            )
    return counts


def _predict(model, tensors: SplitTensors):
    """全rowのmasked CE、argmax index、top-k membershipをbatchで求める。"""
    import torch

    model.eval()
    losses: list[object] = []
    selected: list[object] = []
    top_hits = {k: [] for k in TOP_K_VALUES}
    count = tensors.row_count
    with torch.no_grad():
        for start in range(0, count, BATCH_SIZE):
            stop = min(start + BATCH_SIZE, count)
            features = tensors.features[start:stop]
            legal_mask = tensors.legal_mask[start:stop]
            targets = tensors.targets[start:stop]
            logits = model(features)
            losses.append(masked_cross_entropy(logits, legal_mask, targets))
            selected.append(masked_top_indices(logits, legal_mask, 1).squeeze(-1))
            for k in TOP_K_VALUES:
                indices = masked_top_indices(logits, legal_mask, k)
                top_hits[k].append((indices == targets.unsqueeze(-1)).any(dim=-1))
    return (
        torch.cat(losses),
        torch.cat(selected),
        {k: torch.cat(values) for k, values in top_hits.items()},
    )


def evaluate_split(
    model,
    dataset: LoadedStage2Dataset,
    tensors: SplitTensors,
) -> SplitMetrics:
    """1 splitのprimary / secondary metricを計算する。"""
    import torch

    if not isinstance(tensors, SplitTensors):
        raise TypeError("tensors must be a SplitTensors")
    rows: tuple[Stage2RowRecord, ...] = tuple(
        dataset.rows[index] for index in tensors.row_indices
    )
    if len(rows) != tensors.row_count:
        raise Stage2EvaluationError("split row records do not match the tensors")

    losses, selected, top_hits = _predict(model, tensors)
    correct = selected == tensors.targets
    legal_selection = tensors.legal_mask.gather(1, selected.unsqueeze(1)).squeeze(1)
    illegal_selection_count = int((~legal_selection).sum())

    choice_selector = choice_row_selector(tensors.legal_mask)
    choice_count = int(choice_selector.sum())
    if choice_count == 0:
        raise Stage2EvaluationError(
            f"{tensors.split.value} split contains no choice rows"
        )
    legal_counts = tensors.legal_mask.sum(dim=1)
    choice_legal_counts = legal_counts[choice_selector].to(torch.float64)

    choice_losses = losses[choice_selector].to(torch.float64)
    choice_correct = correct[choice_selector]
    top_k_agreement = tuple(
        (k, float(top_hits[k][choice_selector].to(torch.float64).mean()))
        for k in TOP_K_VALUES
    )

    per_hanchan: list[HanchanMetric] = []
    seeds = sorted({row.seed for row in rows})
    for seed in seeds:
        selector = (
            torch.tensor([row.seed == seed for row in rows], dtype=torch.bool)
            & choice_selector
        )
        count = int(selector.sum())
        if count == 0:
            raise Stage2EvaluationError(f"seed {seed} contributed no choice rows")
        per_hanchan.append(
            HanchanMetric(
                seed=seed,
                choice_rows=count,
                masked_cross_entropy=float(losses[selector].to(torch.float64).mean()),
                exact_agreement=float(correct[selector].to(torch.float64).mean()),
            )
        )

    family_hits = {name: [0, 0] for name in ACTION_FAMILY_NAMES}
    counts = train_index_counts(dataset)
    frequent_indices = {
        index
        for index, _ in sorted(counts.items(), key=lambda item: (-item[1], item[0]))[
            :FREQUENT_INDEX_COUNT
        ]
    }
    frequent = [0, 0]
    rare = [0, 0]
    for position, row in enumerate(rows):
        if not bool(choice_selector[position]):
            continue
        hit = bool(correct[position])
        entry = family_hits[row.teacher_action_family]
        entry[0] += int(hit)
        entry[1] += 1
        if row.teacher_action_index in frequent_indices:
            frequent[0] += int(hit)
            frequent[1] += 1
        if counts.get(row.teacher_action_index, 0) <= RARE_INDEX_MAXIMUM_TRAIN_COUNT:
            rare[0] += int(hit)
            rare[1] += 1

    return SplitMetrics(
        split=tensors.split,
        total_rows=tensors.row_count,
        forced_rows=tensors.row_count - choice_count,
        choice_rows=choice_count,
        choice_masked_cross_entropy=float(choice_losses.mean()),
        choice_exact_agreement=float(choice_correct.to(torch.float64).mean()),
        choice_top_k_agreement=top_k_agreement,
        uniform_choice_cross_entropy=float(choice_legal_counts.log().mean()),
        uniform_choice_exact_agreement=float(choice_legal_counts.reciprocal().mean()),
        all_row_masked_cross_entropy=float(losses.to(torch.float64).mean()),
        all_row_exact_agreement=float(correct.to(torch.float64).mean()),
        family_agreement=tuple(
            _bucket(name, family_hits[name][0], family_hits[name][1])
            for name in ACTION_FAMILY_NAMES
        ),
        index_diagnostics=(
            _bucket(
                f"train_top_{FREQUENT_INDEX_COUNT}_selected_index",
                frequent[0],
                frequent[1],
            ),
            _bucket(
                f"train_count_le_{RARE_INDEX_MAXIMUM_TRAIN_COUNT}_selected_index",
                rare[0],
                rare[1],
            ),
        ),
        per_hanchan=tuple(per_hanchan),
        illegal_selection_count=illegal_selection_count,
    )


def conditional_uniform_reference(legal_counts: tuple[int, ...]) -> tuple[float, float]:
    """`mean(log n)` と `mean(1/n)` のconditional-uniform legal referenceを返す。"""
    if not legal_counts or any(count < 1 for count in legal_counts):
        raise ValueError("legal_counts must be positive and non-empty")
    total = len(legal_counts)
    return (
        sum(math.log(count) for count in legal_counts) / total,
        sum(1.0 / count for count in legal_counts) / total,
    )


__all__ = [
    "FREQUENT_INDEX_COUNT",
    "RARE_INDEX_MAXIMUM_TRAIN_COUNT",
    "TOP_K_VALUES",
    "AgreementBucket",
    "HanchanMetric",
    "SplitMetrics",
    "conditional_uniform_reference",
    "evaluate_split",
    "train_index_counts",
]
