"""Offline Q dataset -> split-unit CPU tensors (shared by both training arms).

BC (Arm A) とsupport-restricted Offline Q (Arm B) が同じsource statesを使う
ことを保証するため、split単位のtensor読み出しを1箇所へ集約する。split
membershipはdataset manifestのwhole-hanchan seed populationだけから決まり、
row単位のre-splitは行わない。
"""

from dataclasses import dataclass

from .artifact import LoadedOfflineQDataset
from .errors import OfflineQProtocolError
from .protocol import (
    FEATURE_DIMENSION,
    VOCABULARY_SIZE,
    Split,
    verify_contract_identity,
)


@dataclass(frozen=True, slots=True)
class OfflineQSplitTensors:
    """1 splitのcurrent / next feature・legal mask・behavior action・reward・terminal。"""

    split: Split
    features: object
    legal_mask: object
    behavior_action_index: object
    reward: object
    terminal: object
    next_features: object
    next_legal_mask: object
    row_indices: tuple[int, ...]

    @property
    def row_count(self) -> int:
        return len(self.row_indices)


def load_split_tensors(
    dataset: LoadedOfflineQDataset,
) -> dict[Split, OfflineQSplitTensors]:
    """dataset artifactを、split単位のCPU tensorへ読み出す。"""
    import torch

    verify_contract_identity()
    if not isinstance(dataset, LoadedOfflineQDataset):
        raise TypeError("dataset must be a LoadedOfflineQDataset")

    row_count = dataset.row_count
    features = torch.frombuffer(
        bytearray(dataset.feature_bytes()), dtype=torch.float32
    ).reshape(row_count, FEATURE_DIMENSION)
    if not bool(torch.isfinite(features).all()):
        raise OfflineQProtocolError("dataset features contain non-finite values")
    legal_mask = (
        torch.frombuffer(bytearray(dataset.legal_mask_bytes()), dtype=torch.uint8)
        .reshape(row_count, VOCABULARY_SIZE)
        .bool()
    )
    next_features = torch.frombuffer(
        bytearray(dataset.next_feature_bytes()), dtype=torch.float32
    ).reshape(row_count, FEATURE_DIMENSION)
    if not bool(torch.isfinite(next_features).all()):
        raise OfflineQProtocolError("dataset next features contain non-finite values")
    next_legal_mask = (
        torch.frombuffer(bytearray(dataset.next_legal_mask_bytes()), dtype=torch.uint8)
        .reshape(row_count, VOCABULARY_SIZE)
        .bool()
    )
    behavior_action_index = torch.tensor(
        [row.behavior_action_index for row in dataset.rows], dtype=torch.long
    )
    reward = torch.tensor([row.reward for row in dataset.rows], dtype=torch.float32)
    terminal = torch.tensor([row.terminal for row in dataset.rows], dtype=torch.bool)

    if not bool(legal_mask.gather(1, behavior_action_index.unsqueeze(1)).all()):
        raise OfflineQProtocolError("a behavior action is not legal in its own mask")
    if not bool((next_legal_mask[~terminal].sum(dim=1) >= 2).all()):
        raise OfflineQProtocolError("a nonterminal row has fewer than 2 next actions")

    tensors: dict[Split, OfflineQSplitTensors] = {}
    for split in Split:
        indices = dataset.split_indices(split)
        if not indices:
            raise OfflineQProtocolError(f"{split.value} split is empty")
        selector = torch.tensor(indices, dtype=torch.long)
        tensors[split] = OfflineQSplitTensors(
            split=split,
            features=features.index_select(0, selector).contiguous(),
            legal_mask=legal_mask.index_select(0, selector).contiguous(),
            behavior_action_index=behavior_action_index.index_select(
                0, selector
            ).contiguous(),
            reward=reward.index_select(0, selector).contiguous(),
            terminal=terminal.index_select(0, selector).contiguous(),
            next_features=next_features.index_select(0, selector).contiguous(),
            next_legal_mask=next_legal_mask.index_select(0, selector).contiguous(),
            row_indices=indices,
        )
    seen = {index for entry in tensors.values() for index in entry.row_indices}
    if len(seen) != row_count:
        raise OfflineQProtocolError("split membership does not partition the dataset")
    return tensors


__all__ = ["OfflineQSplitTensors", "load_split_tensors"]
