"""TEST-blind loader for the exact retained Issue #140 dataset.

The historical loader validates every payload byte and materializes all rows,
including TEST.  Issue #190 explicitly forbids even artifact-verification reads of
TEST rows, so this module validates the canonical manifest and then reads only the
contiguous TRAIN+VALIDATION prefix.  TEST population is checked from metadata only.
"""

import hashlib
import json
from array import array
from dataclasses import dataclass
from math import isfinite
from pathlib import Path

from lisjong_arena._artifact_io import canonical_json_text
from lisjong_arena.learned_policy_offline_q import artifact as source_artifact
from lisjong_arena.learned_policy_offline_q.errors import OfflineQArtifactError
from lisjong_arena.learned_policy_offline_q.protocol import (
    FEATURE_DIMENSION,
    VOCABULARY_SIZE,
    Split,
    action_family,
    split_for_seed,
)

from .errors import DataSufficiencyError, DataSufficiencyEvidenceBlocked
from .protocol import (
    SCALE_SEEDS,
    SOURCE_DATASET_IDENTITY,
    SOURCE_PREFIX_BINDING,
    TEST_SEEDS_METADATA_ONLY,
    TRAIN_SEEDS,
    VALIDATION_SEEDS,
)

_ROW_FIELDS = {
    "seed",
    "split",
    "round_ordinal",
    "round_wind",
    "hand_number",
    "honba",
    "actor_seat",
    "step_ordinal",
    "decision_ordinal",
    "legal_action_count",
    "behavior_action_index",
    "behavior_action_family",
    "reward",
    "terminal",
    "next_step_ordinal",
    "next_decision_ordinal",
}
_ARTIFACT_FILENAMES = {
    source_artifact.MANIFEST_FILENAME,
    source_artifact.ROWS_FILENAME,
    source_artifact.FEATURES_FILENAME,
    source_artifact.LEGAL_MASK_FILENAME,
    source_artifact.NEXT_FEATURES_FILENAME,
    source_artifact.NEXT_LEGAL_MASK_FILENAME,
}
_FEATURE_ROW_BYTES = FEATURE_DIMENSION * 4
_MASK_ROW_BYTES = VOCABULARY_SIZE


@dataclass(frozen=True, slots=True)
class BcSourceRow:
    """Only metadata required by the BC preflight; reward/next-state are unused."""

    source_row_index: int
    seed: int
    split: Split
    round_ordinal: int
    actor_seat: int
    decision_ordinal: int
    behavior_action_index: int


@dataclass(frozen=True, slots=True)
class LoadedDataSufficiencySource:
    """Strict metadata binding plus validated TRAIN/VALIDATION payload prefix."""

    path: Path
    manifest: dict[str, object]
    rows: tuple[BcSourceRow, ...]

    @property
    def identity(self) -> str:
        return self.manifest["dataset_identity"]

    @property
    def provenance(self) -> dict[str, object]:
        return self.manifest["provenance"]

    def indices_for_seeds(self, seeds: tuple[int, ...]) -> tuple[int, ...]:
        allowed = frozenset(seeds)
        return tuple(
            index for index, row in enumerate(self.rows) if row.seed in allowed
        )


@dataclass(frozen=True, slots=True)
class BcSubsetTensors:
    """Minimum tensor surface consumed by the exact #140 BC training path."""

    split: Split
    features: object
    legal_mask: object
    behavior_action_index: object
    row_indices: tuple[int, ...]
    source_rows: tuple[BcSourceRow, ...]

    @property
    def row_count(self) -> int:
        return len(self.row_indices)


def _blocked(message: str, error: BaseException | None = None):
    exception = DataSufficiencyEvidenceBlocked(message)
    if error is None:
        raise exception
    raise exception from error


def _read_exact_prefix(path: Path, size: int) -> bytes:
    """Read exactly ``size`` bytes without buffering beyond the permitted prefix."""
    try:
        with path.open("rb", buffering=0) as stream:
            payload = stream.read(size)
    except OSError as error:
        _blocked(f"cannot read retained source payload {path.name}", error)
    if len(payload) != size:
        _blocked(f"retained source payload {path.name} is shorter than metadata")
    return payload


def _read_row_prefix(path: Path, row_count: int) -> bytes:
    """Read exactly the declared TRAIN+VALIDATION JSONL lines, never the next line."""
    lines: list[bytes] = []
    try:
        with path.open("rb", buffering=0) as stream:
            for _ in range(row_count):
                line = stream.readline()
                if not line:
                    _blocked("rows.jsonl ends before the VALIDATION population")
                lines.append(line)
    except OSError as error:
        _blocked("cannot read retained source rows", error)
    return b"".join(lines)


def _validate_file_metadata(path: Path, manifest: dict[str, object]) -> None:
    """Validate names and sizes without hashing/reading the TEST-bearing suffix."""
    try:
        names = {item.name for item in path.iterdir()}
    except OSError as error:
        _blocked("cannot enumerate retained source dataset", error)
    if names != _ARTIFACT_FILENAMES:
        _blocked("retained source dataset contains missing or extra files")
    for logical_name, filename in (
        ("rows", source_artifact.ROWS_FILENAME),
        ("features", source_artifact.FEATURES_FILENAME),
        ("legal_mask", source_artifact.LEGAL_MASK_FILENAME),
        ("next_features", source_artifact.NEXT_FEATURES_FILENAME),
        ("next_legal_mask", source_artifact.NEXT_LEGAL_MASK_FILENAME),
    ):
        try:
            actual = (path / filename).stat().st_size
        except OSError as error:
            _blocked(f"cannot stat retained source payload {filename}", error)
        if actual != manifest["files"][logical_name]["bytes"]:
            _blocked(f"retained source payload {filename} size differs from metadata")


def _validate_rows(
    payload: bytes, legal_mask_payload: bytes, expected_count: int
) -> tuple[BcSourceRow, ...]:
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError as error:
        _blocked("TRAIN/VALIDATION rows are not UTF-8", error)
    if text and not text.endswith("\n"):
        _blocked("TRAIN/VALIDATION row prefix does not end with a newline")
    records: list[BcSourceRow] = []
    previous_seed: int | None = None
    for index, line in enumerate(text.splitlines()):
        try:
            row = json.loads(line)
        except json.JSONDecodeError as error:
            _blocked("TRAIN/VALIDATION rows contain malformed JSON", error)
        if type(row) is not dict or set(row) != _ROW_FIELDS:
            _blocked("TRAIN/VALIDATION row fields differ from the #140 schema")
        seed = row["seed"]
        if type(seed) is not int or seed not in (*TRAIN_SEEDS, *VALIDATION_SEEDS):
            _blocked("TRAIN/VALIDATION row has an unexpected seed")
        split = split_for_seed(seed)
        if split not in (Split.TRAIN, Split.VALIDATION) or row["split"] != split.value:
            _blocked("TRAIN/VALIDATION row split differs from the locked population")
        if previous_seed is not None and seed < previous_seed:
            _blocked("TRAIN/VALIDATION rows are not grouped by ascending seed")
        previous_seed = seed
        for field in (
            "round_ordinal",
            "actor_seat",
            "decision_ordinal",
            "legal_action_count",
            "behavior_action_index",
        ):
            if type(row[field]) is not int:
                _blocked(f"TRAIN/VALIDATION row {field} is not an integer")
        behavior = row["behavior_action_index"]
        if not 0 <= behavior < VOCABULARY_SIZE:
            _blocked("TRAIN/VALIDATION behavior action is outside the vocabulary")
        if row["behavior_action_family"] != action_family(behavior):
            _blocked("TRAIN/VALIDATION behavior action family is inconsistent")
        start = index * _MASK_ROW_BYTES
        mask = legal_mask_payload[start : start + _MASK_ROW_BYTES]
        if len(mask) != _MASK_ROW_BYTES or any(value not in (0, 1) for value in mask):
            _blocked("TRAIN/VALIDATION legal mask is malformed")
        legal_count = mask.count(1)
        if legal_count < 2 or row["legal_action_count"] != legal_count:
            _blocked("TRAIN/VALIDATION legal action count is inconsistent")
        if mask[behavior] != 1:
            _blocked("TRAIN/VALIDATION behavior action is not legal")
        records.append(
            BcSourceRow(
                source_row_index=index,
                seed=seed,
                split=split,
                round_ordinal=row["round_ordinal"],
                actor_seat=row["actor_seat"],
                decision_ordinal=row["decision_ordinal"],
                behavior_action_index=behavior,
            )
        )
    if len(records) != expected_count:
        _blocked("TRAIN/VALIDATION row count differs from source metadata")
    return tuple(records)


def load_source_dataset(path: str | Path) -> LoadedDataSufficiencySource:
    """Bind exact #140 metadata and load only TRAIN+VALIDATION row prefixes."""
    path = Path(path)
    if not path.is_dir():
        _blocked("exact retained #140 dataset path is unavailable")
    try:
        manifest_text = (path / source_artifact.MANIFEST_FILENAME).read_text(
            encoding="utf-8"
        )
        raw_manifest = json.loads(manifest_text)
        manifest = source_artifact._validate_manifest(raw_manifest)
    except (OSError, json.JSONDecodeError, OfflineQArtifactError, ValueError) as error:
        _blocked("retained #140 dataset metadata cannot be strict-read", error)
    if canonical_json_text(manifest) != manifest_text:
        _blocked("retained #140 dataset manifest is not canonical JSON")
    if manifest["dataset_identity"] != SOURCE_DATASET_IDENTITY:
        _blocked("retained dataset identity is not the exact locked #140 identity")
    protocol = manifest["protocol"]
    if tuple(protocol["train_seeds"]) != TRAIN_SEEDS:
        _blocked("retained source TRAIN population is not exact")
    if tuple(protocol["validation_seeds"]) != VALIDATION_SEEDS:
        _blocked("retained source VALIDATION population is not exact")
    if tuple(protocol["test_seeds"]) != TEST_SEEDS_METADATA_ONLY:
        _blocked("retained source TEST metadata population is not exact")
    _validate_file_metadata(path, manifest)

    relevant_games = [
        game
        for game in manifest["games"]
        if game["seed"] in (*TRAIN_SEEDS, *VALIDATION_SEEDS)
    ]
    if tuple(game["seed"] for game in relevant_games) != (
        *TRAIN_SEEDS,
        *VALIDATION_SEEDS,
    ):
        _blocked("retained source TRAIN/VALIDATION game ordering is not exact")
    relevant_row_count = sum(game["row_count"] for game in relevant_games)
    if relevant_row_count != SOURCE_PREFIX_BINDING["row_count"]:
        _blocked("TRAIN/VALIDATION row count differs from the exact retained prefix")
    rows_payload = _read_row_prefix(
        path / source_artifact.ROWS_FILENAME, relevant_row_count
    )
    features = _read_exact_prefix(
        path / source_artifact.FEATURES_FILENAME,
        relevant_row_count * _FEATURE_ROW_BYTES,
    )
    masks = _read_exact_prefix(
        path / source_artifact.LEGAL_MASK_FILENAME,
        relevant_row_count * _MASK_ROW_BYTES,
    )
    for name, payload in (
        ("rows", rows_payload),
        ("features", features),
        ("legal_mask", masks),
    ):
        if (
            hashlib.sha256(payload).hexdigest()
            != SOURCE_PREFIX_BINDING[f"{name}_sha256"]
        ):
            _blocked(f"TRAIN/VALIDATION {name} prefix digest differs from #140")
    feature_values = array("f")
    feature_values.frombytes(features)
    if len(feature_values) != relevant_row_count * FEATURE_DIMENSION or any(
        not isfinite(value) for value in feature_values
    ):
        _blocked("TRAIN/VALIDATION features are malformed or non-finite")
    rows = _validate_rows(rows_payload, masks, relevant_row_count)
    expected_counts = {game["seed"]: game["row_count"] for game in relevant_games}
    actual_counts = {seed: 0 for seed in expected_counts}
    for row in rows:
        actual_counts[row.seed] += 1
    if actual_counts != expected_counts:
        _blocked("TRAIN/VALIDATION per-hanchan row counts differ from metadata")
    return LoadedDataSufficiencySource(
        path=path,
        manifest=manifest,
        rows=rows,
    )


def _subset_tensors(
    source: LoadedDataSufficiencySource,
    split: Split,
    seeds: tuple[int, ...],
    features,
    legal_mask,
    behavior,
) -> BcSubsetTensors:
    import torch

    indices = source.indices_for_seeds(seeds)
    if not indices:
        raise DataSufficiencyError(f"{split.value} subset is empty")
    if any(source.rows[index].split is not split for index in indices):
        raise DataSufficiencyError(f"{split.value} subset crosses a split boundary")
    selector = torch.tensor(indices, dtype=torch.long)
    selected_rows = tuple(source.rows[index] for index in indices)
    return BcSubsetTensors(
        split=split,
        features=features.index_select(0, selector).contiguous(),
        legal_mask=legal_mask.index_select(0, selector).contiguous(),
        behavior_action_index=behavior.index_select(0, selector).contiguous(),
        row_indices=tuple(source.rows[index].source_row_index for index in indices),
        source_rows=selected_rows,
    )


def scale_tensors(
    source: LoadedDataSufficiencySource, scale: str
) -> dict[Split, BcSubsetTensors]:
    """Build a seed-only nested TRAIN prefix and the unchanged VALIDATION tensors."""
    if scale not in SCALE_SEEDS:
        raise DataSufficiencyError(f"unknown data-sufficiency scale: {scale!r}")
    import torch

    total_rows = len(source.rows)
    # Map exactly the permitted prefix length.  The TEST suffix is outside both
    # mappings and is never materialized.
    features = torch.from_file(
        str(source.path / source_artifact.FEATURES_FILENAME),
        shared=False,
        size=total_rows * FEATURE_DIMENSION,
        dtype=torch.float32,
    ).reshape(total_rows, FEATURE_DIMENSION)
    legal_mask = (
        torch.from_file(
            str(source.path / source_artifact.LEGAL_MASK_FILENAME),
            shared=False,
            size=total_rows * VOCABULARY_SIZE,
            dtype=torch.uint8,
        )
        .reshape(total_rows, VOCABULARY_SIZE)
        .bool()
    )
    behavior = torch.tensor(
        [row.behavior_action_index for row in source.rows], dtype=torch.long
    )
    return {
        Split.TRAIN: _subset_tensors(
            source,
            Split.TRAIN,
            SCALE_SEEDS[scale],
            features,
            legal_mask,
            behavior,
        ),
        Split.VALIDATION: _subset_tensors(
            source,
            Split.VALIDATION,
            VALIDATION_SEEDS,
            features,
            legal_mask,
            behavior,
        ),
    }


__all__ = [
    "BcSourceRow",
    "BcSubsetTensors",
    "LoadedDataSufficiencySource",
    "load_source_dataset",
    "scale_tensors",
]
