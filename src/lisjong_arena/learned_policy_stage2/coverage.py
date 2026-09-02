"""Stage 2 dataset coverage report.

TRAIN / VALIDATION / TEST / totalについて、hanchan数、decision row数、
legal-action count分布、forced / choice row、teacher selected action family、
unique selected index、selected-index frequency concentrationを集計する。

action familyは`lisjong.action_vocabulary`のactual blockから導出し、存在しない
familyを作らない。rare familyが0件でもここではcoverage limitationとして記録
するだけで、seed追加によるrescueは行わない。
"""

from dataclasses import dataclass

from .artifact import LoadedStage2Dataset, Stage2RowRecord
from .protocol import ACTION_FAMILY_NAMES, HANCHAN_COUNT, SPLIT_SEEDS, Split

CONCENTRATION_TOP_K = (1, 5, 10, 25)


@dataclass(frozen=True, slots=True)
class SplitCoverage:
    """1 split（またはtotal）のcoverage集計。"""

    name: str
    hanchan_count: int
    total_rows: int
    forced_rows: int
    choice_rows: int
    legal_action_count_distribution: tuple[tuple[int, int], ...]
    family_row_counts: tuple[tuple[str, int], ...]
    family_choice_row_counts: tuple[tuple[str, int], ...]
    absent_families: tuple[str, ...]
    unique_selected_indices: int
    top_selected_indices: tuple[tuple[int, int], ...]
    concentration: tuple[tuple[int, float], ...]

    @property
    def rows_per_hanchan(self) -> float:
        return self.total_rows / self.hanchan_count

    @property
    def forced_row_share(self) -> float:
        return self.forced_rows / self.total_rows

    def to_document(self) -> dict[str, object]:
        return {
            "name": self.name,
            "hanchan_count": self.hanchan_count,
            "total_rows": self.total_rows,
            "rows_per_hanchan": self.rows_per_hanchan,
            "forced_rows": self.forced_rows,
            "forced_row_share": self.forced_row_share,
            "choice_rows": self.choice_rows,
            "legal_action_count_distribution": [
                [count, rows] for count, rows in self.legal_action_count_distribution
            ],
            "family_row_counts": [
                [name, count] for name, count in self.family_row_counts
            ],
            "family_choice_row_counts": [
                [name, count] for name, count in self.family_choice_row_counts
            ],
            "absent_families": list(self.absent_families),
            "unique_selected_indices": self.unique_selected_indices,
            "top_selected_indices": [
                [index, count] for index, count in self.top_selected_indices
            ],
            "concentration": [[k, share] for k, share in self.concentration],
        }


def _summarize(
    name: str,
    rows: tuple[Stage2RowRecord, ...],
    hanchan_count: int,
) -> SplitCoverage:
    if not rows:
        raise ValueError(f"{name} must contain at least one row")

    legal_distribution: dict[int, int] = {}
    family_counts = {family: 0 for family in ACTION_FAMILY_NAMES}
    family_choice_counts = {family: 0 for family in ACTION_FAMILY_NAMES}
    index_counts: dict[int, int] = {}
    forced = 0
    choice = 0

    for row in rows:
        legal_distribution[row.legal_action_count] = (
            legal_distribution.get(row.legal_action_count, 0) + 1
        )
        family_counts[row.teacher_action_family] += 1
        index_counts[row.teacher_action_index] = (
            index_counts.get(row.teacher_action_index, 0) + 1
        )
        if row.is_choice_row:
            choice += 1
            family_choice_counts[row.teacher_action_family] += 1
        else:
            forced += 1

    ordered_indices = sorted(index_counts.items(), key=lambda item: (-item[1], item[0]))
    total = len(rows)
    concentration = tuple(
        (k, sum(count for _, count in ordered_indices[:k]) / total)
        for k in CONCENTRATION_TOP_K
    )
    return SplitCoverage(
        name=name,
        hanchan_count=hanchan_count,
        total_rows=total,
        forced_rows=forced,
        choice_rows=choice,
        legal_action_count_distribution=tuple(sorted(legal_distribution.items())),
        family_row_counts=tuple(
            (family, family_counts[family]) for family in ACTION_FAMILY_NAMES
        ),
        family_choice_row_counts=tuple(
            (family, family_choice_counts[family]) for family in ACTION_FAMILY_NAMES
        ),
        absent_families=tuple(
            family for family in ACTION_FAMILY_NAMES if family_counts[family] == 0
        ),
        unique_selected_indices=len(index_counts),
        top_selected_indices=tuple(ordered_indices[:10]),
        concentration=concentration,
    )


@dataclass(frozen=True, slots=True)
class DatasetCoverage:
    """split別 + total のcoverage報告。"""

    dataset_identity: str
    splits: tuple[SplitCoverage, ...]
    total: SplitCoverage

    def to_document(self) -> dict[str, object]:
        return {
            "dataset_identity": self.dataset_identity,
            "splits": [split.to_document() for split in self.splits],
            "total": self.total.to_document(),
        }


def build_coverage(dataset: LoadedStage2Dataset) -> DatasetCoverage:
    """dataset全体のcoverage報告を構築する。"""
    if not isinstance(dataset, LoadedStage2Dataset):
        raise TypeError("dataset must be a LoadedStage2Dataset")
    splits = tuple(
        _summarize(
            split.value,
            tuple(row for row in dataset.rows if row.split is split),
            len(SPLIT_SEEDS[split]),
        )
        for split in (Split.TRAIN, Split.VALIDATION, Split.TEST)
    )
    total = _summarize("TOTAL", dataset.rows, HANCHAN_COUNT)
    if sum(split.total_rows for split in splits) != total.total_rows:
        raise ValueError("split row counts do not sum to the dataset row count")
    return DatasetCoverage(
        dataset_identity=dataset.identity,
        splits=splits,
        total=total,
    )


__all__ = [
    "CONCENTRATION_TOP_K",
    "DatasetCoverage",
    "SplitCoverage",
    "build_coverage",
]
