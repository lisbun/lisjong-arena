"""Path B development-only Stage 3 serving fixture.

exact Stage 2 checkpoint bytesが失われている場合でもserving boundaryの検証を
止めないため、Stage 2 locked protocolのまま**新しいStage 3 identity**を持つ
fixture checkpointを構築する。

```text
Stage 2 recording seam (unchanged)
    -> seeds 200..212 のみ  (TRAIN 200..209 / VALIDATION 210..212)
    -> Stage 2 locked model / training config
    -> lowest VALIDATION choice-row masked CE
    -> Stage 3 fixture checkpoint (別schema version / 別identity)
```

固定する境界:

- Stage 2 locked architecture / training configを変更しない
- Stage 2 TEST hanchan `213..215`をrecord / load / selection / validationの
  いずれでも使わない（`require_fixture_seed()`がcodeとして固定する）
- Stage 2 checkpoint identityを名乗らない。digestが一致しないことをmanifestへ
  明示し、silent aliasしない
- **このfixtureからprediction quality / agreement / strength claimを作らない**
"""

import hashlib
import json
import struct
import time
from dataclasses import dataclass
from pathlib import Path
from shutil import rmtree
from tempfile import mkdtemp

from lisjong_arena._artifact_io import canonical_json_text
from lisjong_arena.learned_policy_stage2.artifact import (
    feature_block,
    vocabulary_block,
)
from lisjong_arena.learned_policy_stage2.errors import Stage2ProtocolError
from lisjong_arena.learned_policy_stage2.network import parameter_count
from lisjong_arena.learned_policy_stage2.protocol import (
    FEATURE_DIMENSION,
    TEACHER_IDENTITY,
    TEACHER_SOURCE_REVISION,
    VOCABULARY_SIZE,
    Split,
    verify_contract_identity,
)
from lisjong_arena.learned_policy_stage2.recording import (
    build_decision_rows,
    record_teacher_game,
)
from lisjong_arena.learned_policy_stage2.training import (
    MANIFEST_FILENAME,
    WEIGHTS_FILENAME,
    SplitTensors,
    checkpoint_identity,
    locked_model_block,
    locked_training_block,
    peak_process_ram_bytes,
    train_from_split_tensors,
)
from lisjong_arena.single_round_artifact import collect_execution_provenance

from .artifact import (
    FIXTURE_CHECKPOINT_SCHEMA_VERSION,
    ServingCheckpoint,
    load_serving_checkpoint,
)
from .errors import Stage3ProtocolError
from .protocol import (
    EXCLUDED_STAGE2_TEST_SEEDS,
    FIXTURE_TRAIN_SEEDS,
    FIXTURE_VALIDATION_SEEDS,
    PROTOCOL_ID,
    require_fixture_seed,
)

FIXTURE_ORIGIN = "stage3-development-only-serving-fixture"
FIXTURE_NOTE = (
    "Stage 3 serving-integration fixture. This is NOT the Stage 2 checkpoint and "
    "carries no prediction-quality or game-strength claim."
)

_SPLIT_SEEDS = {
    Split.TRAIN: FIXTURE_TRAIN_SEEDS,
    Split.VALIDATION: FIXTURE_VALIDATION_SEEDS,
}


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _provenance_document() -> dict[str, object]:
    provenance = collect_execution_provenance()
    return {
        "execution_environment": provenance.execution_environment,
        "lisjong_arena_version": provenance.lisjong_arena_version,
        "lisjong_arena_revision": provenance.lisjong_arena_revision,
        "lisjong_version": provenance.lisjong_version,
        "lisjong_revision": provenance.lisjong_revision,
        "lisjong_engine_version": provenance.lisjong_engine_version,
        "lisjong_engine_revision": provenance.lisjong_engine_revision,
        "riichienv_version": provenance.riichienv_version,
        "python_version": provenance.python_version,
    }


@dataclass(frozen=True, slots=True)
class FixtureRecording:
    """fixture populationのrecordingを、split単位のrow列として保持する。"""

    rows_by_split: dict
    dataset_identity: str
    row_count: int
    recording_wall_clock_seconds: float
    provenance: dict

    def split_row_count(self, split: Split) -> int:
        return len(self.rows_by_split[split])


def _row_digest_payload(rows) -> bytes:
    """rowのfeature / mask / labelを、dataset artifactと同じ並びでdigest化する。"""
    hasher = hashlib.sha256()
    for row in rows:
        hasher.update(struct.pack(f"<{FEATURE_DIMENSION}f", *row.feature_values))
        hasher.update(bytes(1 if legal else 0 for legal in row.legal_mask))
        hasher.update(struct.pack("<i", row.teacher_action_index))
    return hasher.digest()


def record_fixture_population(*, provenance: dict | None = None) -> FixtureRecording:
    """seeds 200..212だけをteacher x4で再実行し、fixture rowを構築する。

    Stage 2 TEST hanchanはこの経路から到達できない。`provenance`の明示指定は
    fixture / testのためだけの入口である。
    """
    verify_contract_identity()
    document = _provenance_document() if provenance is None else dict(provenance)
    if document["lisjong_revision"] != TEACHER_SOURCE_REVISION:
        raise Stage3ProtocolError(
            "fixture lisjong provenance revision does not match the locked teacher "
            f"source revision: {document['lisjong_revision']!r} != "
            f"{TEACHER_SOURCE_REVISION!r}"
        )

    started = time.perf_counter()
    rows_by_split: dict = {split: [] for split in _SPLIT_SEEDS}
    for split, seeds in _SPLIT_SEEDS.items():
        for seed in seeds:
            require_fixture_seed(seed)
            recording = record_teacher_game(seed)
            if recording.split is not split:
                raise Stage3ProtocolError(
                    f"seed {seed} does not belong to the {split.value} split"
                )
            rows_by_split[split].extend(build_decision_rows(recording))
    wall_clock = time.perf_counter() - started

    row_count = sum(len(rows) for rows in rows_by_split.values())
    if row_count == 0:
        raise Stage3ProtocolError("fixture recording produced no rows")

    descriptor = {
        "protocol_id": PROTOCOL_ID,
        "origin": FIXTURE_ORIGIN,
        "train_seeds": list(FIXTURE_TRAIN_SEEDS),
        "validation_seeds": list(FIXTURE_VALIDATION_SEEDS),
        "excluded_stage2_test_seeds": list(EXCLUDED_STAGE2_TEST_SEEDS),
        "feature": feature_block(),
        "vocabulary": vocabulary_block(),
        "teacher_identity": TEACHER_IDENTITY,
        "teacher_source_revision": TEACHER_SOURCE_REVISION,
        "provenance": document,
        "row_count": row_count,
        "split_digests": {
            split.value: _sha256(_row_digest_payload(rows_by_split[split]))
            for split in sorted(rows_by_split, key=lambda item: item.value)
        },
    }
    return FixtureRecording(
        rows_by_split=rows_by_split,
        dataset_identity=_sha256(canonical_json_text(descriptor).encode("utf-8")),
        row_count=row_count,
        recording_wall_clock_seconds=wall_clock,
        provenance=document,
    )


def build_split_tensors(recording: FixtureRecording) -> dict:
    """fixture rowをTRAIN / VALIDATION tensorへ変換する。"""
    import torch

    tensors: dict = {}
    for split, rows in recording.rows_by_split.items():
        if not rows:
            raise Stage3ProtocolError(f"{split.value} split is empty")
        features = torch.tensor(
            [row.feature_values for row in rows], dtype=torch.float32
        )
        if not bool(torch.isfinite(features).all()):
            raise Stage2ProtocolError("fixture features contain non-finite values")
        legal_mask = torch.tensor([row.legal_mask for row in rows], dtype=torch.bool)
        targets = torch.tensor(
            [row.teacher_action_index for row in rows], dtype=torch.long
        )
        if features.shape[1] != FEATURE_DIMENSION:
            raise Stage2ProtocolError("fixture feature dimension is not the locked one")
        if legal_mask.shape[1] != VOCABULARY_SIZE:
            raise Stage2ProtocolError("fixture mask dimension is not the locked one")
        if not bool(legal_mask.gather(1, targets.unsqueeze(1)).all()):
            raise Stage2ProtocolError("a teacher label is not legal in its own mask")
        tensors[split] = SplitTensors(
            split=split,
            features=features.contiguous(),
            legal_mask=legal_mask.contiguous(),
            targets=targets.contiguous(),
            row_indices=tuple(range(len(rows))),
        )
    return tensors


def save_fixture_checkpoint(
    destination: str | Path,
    recording: FixtureRecording,
    run,
) -> ServingCheckpoint:
    """Stage 3 fixture checkpointをstagingで検証してから公開する。"""
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
            "checkpoint_schema_version": FIXTURE_CHECKPOINT_SCHEMA_VERSION,
            "dataset_identity": recording.dataset_identity,
            "feature": feature_block(),
            "vocabulary": vocabulary_block(),
            "model": locked_model_block(),
            "training": locked_training_block(),
            "parameter_count": parameter_count(run.model),
            "selected_epoch": run.selected_epoch,
            "selected_validation_choice_masked_ce": (
                run.selected_validation_choice_masked_ce
            ),
            "epoch_history": [record.to_document() for record in run.history],
            "weights_bytes": len(weights),
            "weights_sha256": _sha256(weights),
            "fixture": {
                "origin": FIXTURE_ORIGIN,
                "protocol_id": PROTOCOL_ID,
                "train_seeds": list(FIXTURE_TRAIN_SEEDS),
                "validation_seeds": list(FIXTURE_VALIDATION_SEEDS),
                "excluded_stage2_test_seeds": list(EXCLUDED_STAGE2_TEST_SEEDS),
                "row_count": recording.row_count,
                "teacher_identity": TEACHER_IDENTITY,
                "teacher_source_revision": TEACHER_SOURCE_REVISION,
                "stage2_checkpoint_identity": None,
                "note": FIXTURE_NOTE,
            },
            "provenance": recording.provenance,
            "runtime": {
                **run.runtime,
                "training_wall_clock_seconds": run.wall_clock_seconds,
                "recording_wall_clock_seconds": (
                    recording.recording_wall_clock_seconds
                ),
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
            raise Stage3ProtocolError("staged state_dict readback differs")
        staging.rename(destination)
        published = True
    finally:
        if not published:
            rmtree(staging, ignore_errors=True)
    return load_serving_checkpoint(destination)


def build_fixture_checkpoint(
    destination: str | Path,
    *,
    provenance: dict | None = None,
) -> tuple[ServingCheckpoint, dict]:
    """record -> train -> saveをまとめてPath B fixtureを1本作る。"""
    recording = record_fixture_population(provenance=provenance)
    run = train_from_split_tensors(build_split_tensors(recording))
    checkpoint = save_fixture_checkpoint(destination, recording, run)
    report = {
        "protocol_id": PROTOCOL_ID,
        "artifact_handoff_path": "B",
        "origin": FIXTURE_ORIGIN,
        "train_seeds": list(FIXTURE_TRAIN_SEEDS),
        "validation_seeds": list(FIXTURE_VALIDATION_SEEDS),
        "excluded_stage2_test_seeds": list(EXCLUDED_STAGE2_TEST_SEEDS),
        "row_count": recording.row_count,
        "train_row_count": recording.split_row_count(Split.TRAIN),
        "validation_row_count": recording.split_row_count(Split.VALIDATION),
        "recording_wall_clock_seconds": recording.recording_wall_clock_seconds,
        "training_wall_clock_seconds": run.wall_clock_seconds,
        "selected_epoch": run.selected_epoch,
        "peak_process_ram_bytes": peak_process_ram_bytes(),
        "identity": checkpoint.identity_document(),
        "note": FIXTURE_NOTE,
    }
    return checkpoint, report


def write_report(path: str | Path, report: dict) -> None:
    """fixture生成reportをcanonical JSONで書き出す（既存pathは上書きしない）。"""
    path = Path(path)
    if path.exists():
        raise FileExistsError("report destination already exists")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(canonical_json_text(report), encoding="utf-8", newline="\n")


def load_report(path: str | Path) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


__all__ = [
    "FIXTURE_NOTE",
    "FIXTURE_ORIGIN",
    "FixtureRecording",
    "build_fixture_checkpoint",
    "build_split_tensors",
    "load_report",
    "record_fixture_population",
    "save_fixture_checkpoint",
    "write_report",
]
