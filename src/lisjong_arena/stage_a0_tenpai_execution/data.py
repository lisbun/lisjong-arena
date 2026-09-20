"""Protected-TEST-blind scientific data loader for #262."""

from __future__ import annotations

from dataclasses import dataclass

from lisjong_arena.learned_policy_offline_q.artifact import (
    FEATURES_FILENAME,
    LEGAL_MASK_FILENAME,
    LoadedOfflineQDataset,
    load_dataset_seed_prefix,
)
from lisjong_arena.learned_policy_offline_q.protocol import Split
from lisjong_arena.stage_a0_tenpai_feasibility.labels import TargetAvailability
from lisjong_arena.stage_a0_tenpai_feasibility.sidecar import (
    LoadedSidecar,
    load_sidecar,
)
from lisjong_arena.stage_a0_tenpai_protocol_lock import protocol as locked
from lisjong_arena.stage_a0_tenpai_protocol_lock.artifact import load_lock_b
from lisjong_arena.stage_a0_tenpai_protocol_lock.materialize import (
    LoadedPublicKeys,
    load_public_keys,
)

from .errors import StageA0PreflightError
from .protocol import EXPECTED_LOCK_B_IDENTITY

_FEATURE_ROW_BYTES = locked.FEATURE_DIMENSION * 4
_MASK_ROW_BYTES = locked.VOCABULARY_SIZE


@dataclass(frozen=True, slots=True)
class ScientificData:
    lock_b: dict
    dataset: LoadedOfflineQDataset
    sidecar: LoadedSidecar
    public_keys: LoadedPublicKeys


@dataclass(frozen=True, slots=True)
class ScientificSplitTensors:
    split: Split
    row_keys: tuple[tuple[int, int, int, int], ...]
    features: object
    legal_mask: object
    behavior_action_index: object
    tenpai_targets: object
    tenpai_eligible: object

    @property
    def row_count(self) -> int:
        return len(self.row_keys)


def _row_key(row) -> tuple[int, int, int, int]:
    return (row.seed, row.step_ordinal, row.decision_ordinal, row.actor_seat)


def _cell_key(cell) -> tuple[int, int, int, int, int]:
    identity = cell.row_identity
    return (
        identity.seed,
        identity.step_ordinal,
        identity.decision_ordinal,
        identity.actor_seat,
        cell.identity.viewer_relative_offset,
    )


def load_scientific_data(
    *,
    lock_b_path,
    dataset_path,
    sidecar_path,
    public_keys_path,
) -> ScientificData:
    """Strict-read all #262 inputs without reading protected TEST payload bytes."""
    lock_b = load_lock_b(lock_b_path)
    if lock_b["lock_identity"] != EXPECTED_LOCK_B_IDENTITY:
        raise StageA0PreflightError(
            "Lock B identity is not the final completed #259 identity"
        )

    dataset = load_dataset_seed_prefix(dataset_path, locked.SCIENTIFIC_SEEDS)
    if dataset.identity != lock_b["data"]["retained_dataset_identity"]:
        raise StageA0PreflightError("retained dataset identity differs from Lock B")
    if any(row.seed in locked.PROTECTED_TEST_SEEDS for row in dataset.rows):
        raise StageA0PreflightError("protected TEST row entered scientific prefix")

    sidecar = load_sidecar(sidecar_path)
    if sidecar.identity != lock_b["data"]["scientific_sidecar_identity"]:
        raise StageA0PreflightError("scientific sidecar identity differs from Lock B")
    if any(
        cell.row_identity.seed in locked.PROTECTED_TEST_SEEDS for cell in sidecar.cells
    ):
        raise StageA0PreflightError("scientific sidecar contains protected TEST")

    public_keys = load_public_keys(public_keys_path)
    if public_keys.identity != lock_b["data"]["public_keys_identity"]:
        raise StageA0PreflightError("public-key artifact identity differs from Lock B")
    if public_keys.manifest["sidecar_identity"] != sidecar.identity:
        raise StageA0PreflightError(
            "public keys are not bound to the scientific sidecar"
        )

    dataset_rows = {_row_key(row) for row in dataset.rows}
    sidecar_rows = {
        (
            cell.row_identity.seed,
            cell.row_identity.step_ordinal,
            cell.row_identity.decision_ordinal,
            cell.row_identity.actor_seat,
        )
        for cell in sidecar.cells
    }
    public_rows = {
        (record.seed, record.step_ordinal, record.decision_ordinal, record.actor_seat)
        for record in public_keys.records
    }
    if dataset_rows != sidecar_rows or dataset_rows != public_rows:
        raise StageA0PreflightError(
            "dataset, privileged sidecar and player-safe public keys are not row-aligned"
        )
    if len(sidecar.cells) != len(dataset.rows) * 3:
        raise StageA0PreflightError(
            "scientific sidecar must contain three cells per row"
        )
    if len(public_keys.records) != len(dataset.rows) * 3:
        raise StageA0PreflightError("public keys must contain three cells per row")

    return ScientificData(
        lock_b=lock_b,
        dataset=dataset,
        sidecar=sidecar,
        public_keys=public_keys,
    )


def _read_prefix(path, byte_count: int, label: str) -> bytes:
    with path.open("rb") as stream:
        payload = stream.read(byte_count)
    if len(payload) != byte_count:
        raise StageA0PreflightError(f"{label} is shorter than the scientific prefix")
    return payload


def build_scientific_split_tensors(
    scientific: ScientificData,
) -> dict[Split, ScientificSplitTensors]:
    """Build TRAIN/VALIDATION tensors from exactly 245..270.

    The retained artifact also contains protected TEST rows 271..276. This
    reader therefore requests exactly the byte prefix represented by the
    already prefix-limited dataset object.
    """
    import torch

    dataset = scientific.dataset
    row_count = dataset.row_count
    feature_payload = _read_prefix(
        dataset.path / FEATURES_FILENAME,
        row_count * _FEATURE_ROW_BYTES,
        "features.f32",
    )
    mask_payload = _read_prefix(
        dataset.path / LEGAL_MASK_FILENAME,
        row_count * _MASK_ROW_BYTES,
        "legal_mask.u8",
    )
    features = torch.frombuffer(
        bytearray(feature_payload), dtype=torch.float32
    ).reshape(row_count, locked.FEATURE_DIMENSION)
    if not bool(torch.isfinite(features).all()):
        raise StageA0PreflightError(
            "scientific public features contain non-finite values"
        )
    legal_mask = (
        torch.frombuffer(bytearray(mask_payload), dtype=torch.uint8)
        .reshape(row_count, locked.VOCABULARY_SIZE)
        .bool()
    )
    behavior = torch.tensor(
        [row.behavior_action_index for row in dataset.rows], dtype=torch.long
    )
    if not bool(legal_mask.gather(1, behavior.unsqueeze(1)).all()):
        raise StageA0PreflightError("a teacher action is illegal in its own mask")

    cells = {_cell_key(cell): cell for cell in scientific.sidecar.cells}
    if len(cells) != len(scientific.sidecar.cells):
        raise StageA0PreflightError("scientific sidecar contains duplicate cell keys")

    targets = torch.zeros((row_count, 3), dtype=torch.float32)
    eligible = torch.zeros((row_count, 3), dtype=torch.bool)
    for row_index, row in enumerate(dataset.rows):
        base = _row_key(row)
        for relative_offset in (1, 2, 3):
            cell = cells.get((*base, relative_offset))
            if cell is None:
                raise StageA0PreflightError(
                    "scientific row is missing an opponent cell"
                )
            target_index = relative_offset - 1
            if cell.availability is TargetAvailability.AVAILABLE:
                if cell.target.tenpai not in (0, 1):
                    raise StageA0PreflightError(
                        "eligible Tenpai cell has no binary target"
                    )
                targets[row_index, target_index] = float(cell.target.tenpai)
                eligible[row_index, target_index] = True

    result: dict[Split, ScientificSplitTensors] = {}
    for split in (Split.TRAIN, Split.VALIDATION):
        indices = tuple(
            index for index, row in enumerate(dataset.rows) if row.split is split
        )
        if not indices:
            raise StageA0PreflightError(f"{split.value} scientific split is empty")
        selector = torch.tensor(indices, dtype=torch.long)
        result[split] = ScientificSplitTensors(
            split=split,
            row_keys=tuple(_row_key(dataset.rows[index]) for index in indices),
            features=features.index_select(0, selector).contiguous(),
            legal_mask=legal_mask.index_select(0, selector).contiguous(),
            behavior_action_index=behavior.index_select(0, selector).contiguous(),
            tenpai_targets=targets.index_select(0, selector).contiguous(),
            tenpai_eligible=eligible.index_select(0, selector).contiguous(),
        )

    if int(result[Split.TRAIN].tenpai_eligible.sum()) <= 0:
        raise StageA0PreflightError("TRAIN contains no eligible Tenpai cells")
    for seed in locked.VALIDATION_SEEDS:
        indices = [
            index
            for index, key in enumerate(result[Split.VALIDATION].row_keys)
            if key[0] == seed
        ]
        if not indices:
            raise StageA0PreflightError(f"VALIDATION seed {seed} has no decision rows")
        selector = torch.tensor(indices, dtype=torch.long)
        if (
            int(
                result[Split.VALIDATION].tenpai_eligible.index_select(0, selector).sum()
            )
            <= 0
        ):
            raise StageA0PreflightError(
                f"VALIDATION seed {seed} has zero eligible Tenpai cells"
            )
    return result


__all__ = [
    "ScientificData",
    "ScientificSplitTensors",
    "build_scientific_split_tensors",
    "load_scientific_data",
]
