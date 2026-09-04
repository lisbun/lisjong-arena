"""TRAIN behavior-support gate report (Issue #140).

Offline Qのriskは、datasetで選択されていないactionへのextrapolationである。
このmoduleはTRAIN上でbehavior actionとして1回以上観測されたexact discard
vocabulary index集合を`supported_indices`として固定し、TRAIN / VALIDATIONの
eligible stateについて「全legal discard indicesがTRAIN-supportedか」を
報告する。

TEST split rowはここでは一切読まない。data coverageが不十分な場合の
`OFFLINE Q DATA COVERAGE BLOCKED`判定はresult-drivenなseed追加を防ぐため、
この報告を見た人間 / bounded Issue判断に委ね、このmoduleでは自動確定しない。
"""

from dataclasses import dataclass

from .artifact import LoadedOfflineQDataset
from .protocol import Split

LEGAL_ACTION_COUNT_BUCKETS = (2, 3, 4, 5, 6, 7, 8)


def _legal_indices(dataset: LoadedOfflineQDataset, index: int) -> frozenset[int]:
    return frozenset(
        position
        for position, legal in enumerate(dataset.legal_mask_row(index))
        if legal
    )


def _bucket(legal_action_count: int) -> int:
    for bucket in LEGAL_ACTION_COUNT_BUCKETS:
        if legal_action_count <= bucket:
            return bucket
    return LEGAL_ACTION_COUNT_BUCKETS[-1] + 1


@dataclass(frozen=True, slots=True)
class SupportGateReport:
    """TRAIN / VALIDATIONだけを使ったbehavior-support gateの報告。"""

    dataset_identity: str
    train_row_count: int
    validation_row_count: int
    supported_indices: tuple[int, ...]
    unsupported_indices: tuple[int, ...]
    train_support_complete_rate: float
    validation_support_complete_rate: float
    combined_support_complete_rate: float
    support_complete_rate_by_legal_action_count: tuple[tuple[int, int, float], ...]
    fallback_risk_estimate: float

    def to_document(self) -> dict[str, object]:
        return {
            "dataset_identity": self.dataset_identity,
            "train_row_count": self.train_row_count,
            "validation_row_count": self.validation_row_count,
            "supported_indices": list(self.supported_indices),
            "supported_index_count": len(self.supported_indices),
            "unsupported_indices": list(self.unsupported_indices),
            "unsupported_index_count": len(self.unsupported_indices),
            "train_support_complete_rate": self.train_support_complete_rate,
            "validation_support_complete_rate": self.validation_support_complete_rate,
            "combined_support_complete_rate": self.combined_support_complete_rate,
            "support_complete_rate_by_legal_action_count": [
                [bucket, row_count, rate]
                for bucket, row_count, rate in (
                    self.support_complete_rate_by_legal_action_count
                )
            ],
            "fallback_risk_estimate": self.fallback_risk_estimate,
        }


def build_support_gate_report(dataset: LoadedOfflineQDataset) -> SupportGateReport:
    """TRAIN / VALIDATION rowだけを使ってsupport gate reportを構築する。

    TEST split rowはfeature / legal maskを含め一切参照しない。
    """
    if not isinstance(dataset, LoadedOfflineQDataset):
        raise TypeError("dataset must be a LoadedOfflineQDataset")

    train_indices = dataset.split_indices(Split.TRAIN)
    validation_indices = dataset.split_indices(Split.VALIDATION)
    if not train_indices:
        raise ValueError("dataset must contain at least one TRAIN row")
    if not validation_indices:
        raise ValueError("dataset must contain at least one VALIDATION row")

    supported = frozenset(
        dataset.rows[index].behavior_action_index for index in train_indices
    )

    combined_indices = train_indices + validation_indices
    all_legal_seen: set[int] = set()
    for index in combined_indices:
        all_legal_seen |= _legal_indices(dataset, index)
    unsupported = sorted(all_legal_seen - supported)

    def _complete(index: int) -> bool:
        return _legal_indices(dataset, index).issubset(supported)

    def _rate(indices: tuple[int, ...]) -> float:
        return sum(1 for index in indices if _complete(index)) / len(indices)

    bucket_totals: dict[int, int] = {}
    bucket_complete: dict[int, int] = {}
    for index in combined_indices:
        bucket = _bucket(dataset.rows[index].legal_action_count)
        bucket_totals[bucket] = bucket_totals.get(bucket, 0) + 1
        if _complete(index):
            bucket_complete[bucket] = bucket_complete.get(bucket, 0) + 1

    by_bucket = tuple(
        (
            bucket,
            bucket_totals[bucket],
            bucket_complete.get(bucket, 0) / bucket_totals[bucket],
        )
        for bucket in sorted(bucket_totals)
    )

    combined_rate = _rate(combined_indices)
    return SupportGateReport(
        dataset_identity=dataset.identity,
        train_row_count=len(train_indices),
        validation_row_count=len(validation_indices),
        supported_indices=tuple(sorted(supported)),
        unsupported_indices=tuple(unsupported),
        train_support_complete_rate=_rate(train_indices),
        validation_support_complete_rate=_rate(validation_indices),
        combined_support_complete_rate=combined_rate,
        support_complete_rate_by_legal_action_count=by_bucket,
        fallback_risk_estimate=1.0 - combined_rate,
    )


def is_support_complete(
    supported_indices: frozenset[int], legal_mask: tuple[bool, ...]
) -> bool:
    """runtime activation gate: 全legal discard indicesがTRAIN-supportedか。"""
    legal = frozenset(index for index, value in enumerate(legal_mask) if value)
    return legal.issubset(supported_indices)


__all__ = [
    "LEGAL_ACTION_COUNT_BUCKETS",
    "SupportGateReport",
    "build_support_gate_report",
    "is_support_complete",
]
