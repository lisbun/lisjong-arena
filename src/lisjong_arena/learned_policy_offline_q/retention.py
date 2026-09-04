"""Artifact retention gate for the Q-vs-BC candidate pair (Issue #140).

strength run開始前にBC / Q両checkpointのexact bytesをnon-ephemeral location
へretainしstrict readbackする。retention先の`resolve_retention_target()`は
`lisbun/lisjong-arena #138`（Stage 4a）が確立したfail-closedな判定を
そのまま再利用し、このmoduleでは再実装しない。宣言できるnon-ephemeral root
が無い場合は`ARTIFACT RETENTION BLOCKED`としてstrength runへ進まない。
"""

import shutil
from dataclasses import dataclass
from pathlib import Path

from lisjong_arena._artifact_io import (
    canonical_json_text,
    expect_object,
    expect_str,
    read_json_document,
    write_new_artifact_file,
)
from lisjong_arena.learned_policy_stage4a.candidate import (
    RetentionTarget,
    resolve_retention_target,
)
from lisjong_arena.learned_policy_stage4a.errors import Stage4aRetentionError

from . import bc_training, q_training
from .errors import OfflineQArtifactError
from .protocol import PROTOCOL_ID, TEACHER_SOURCE_REVISION

FREEZE_RECORD_FILENAME = "offlineq-candidate-freeze.json"
FREEZE_RECORD_SCHEMA_VERSION = "arena-learned-policy-offlineq-candidate-freeze-v1"
BC_CHECKPOINT_DIRNAME = "bc-checkpoint"
Q_CHECKPOINT_DIRNAME = "q-checkpoint"

_FREEZE_FIELDS = {
    "freeze_record_schema_version",
    "protocol_id",
    "dataset_identity",
    "teacher_source_revision",
    "bc_checkpoint_identity",
    "q_checkpoint_identity",
    "retention",
    "strength_claim",
}


@dataclass(frozen=True, slots=True)
class OfflineQFreeze:
    """BC / Q checkpointのfrozen identityとretention reference。"""

    dataset_identity: str
    bc_checkpoint_identity: str
    q_checkpoint_identity: str
    retention: RetentionTarget

    def to_document(self) -> dict[str, object]:
        return {
            "freeze_record_schema_version": FREEZE_RECORD_SCHEMA_VERSION,
            "protocol_id": PROTOCOL_ID,
            "dataset_identity": self.dataset_identity,
            "teacher_source_revision": TEACHER_SOURCE_REVISION,
            "bc_checkpoint_identity": self.bc_checkpoint_identity,
            "q_checkpoint_identity": self.q_checkpoint_identity,
            "retention": self.retention.to_document(),
            "strength_claim": None,
        }


def freeze_candidates(
    *,
    bc_checkpoint_path: str | Path,
    q_checkpoint_path: str | Path,
    backend: str,
    root: str | Path,
    key: str,
) -> tuple[OfflineQFreeze, "RetainedCandidates"]:
    """BC / Q checkpointを両方strict loadし、non-ephemeral rootへ複製・記録する。

    `ARTIFACT RETENTION BLOCKED`はcaller側が`Stage4aRetentionError`を
    捕捉して扱う。このfunction自身はfail closedするだけで、代替値へ
    silent fallbackしない。
    """
    target = resolve_retention_target(backend=backend, root=root, key=key)

    bc_checkpoint = bc_training.load_checkpoint(bc_checkpoint_path)
    q_checkpoint = q_training.load_checkpoint(q_checkpoint_path)
    if (
        bc_checkpoint.manifest["dataset_identity"]
        != q_checkpoint.manifest["dataset_identity"]
    ):
        raise OfflineQArtifactError(
            "BC and Q checkpoints were not trained on the same dataset identity"
        )

    bc_destination = target.bundle_path / BC_CHECKPOINT_DIRNAME
    q_destination = target.bundle_path / Q_CHECKPOINT_DIRNAME
    target.bundle_path.mkdir(parents=True)
    try:
        shutil.copytree(bc_checkpoint.path, bc_destination)
        shutil.copytree(q_checkpoint.path, q_destination)
        freeze = OfflineQFreeze(
            dataset_identity=bc_checkpoint.manifest["dataset_identity"],
            bc_checkpoint_identity=bc_checkpoint.identity,
            q_checkpoint_identity=q_checkpoint.identity,
            retention=target,
        )
        write_new_artifact_file(
            target.bundle_path / FREEZE_RECORD_FILENAME,
            canonical_json_text(freeze.to_document()),
        )
    except BaseException:
        shutil.rmtree(target.bundle_path, ignore_errors=True)
        raise

    retained = strict_readback(target.bundle_path)
    return freeze, retained


@dataclass(frozen=True, slots=True)
class RetainedCandidates:
    """retention先からstrict readbackしたBC / Q checkpoint。"""

    freeze: OfflineQFreeze
    bc_checkpoint: object
    q_checkpoint: object


def load_freeze_record(bundle_path: str | Path) -> OfflineQFreeze:
    path = Path(bundle_path) / FREEZE_RECORD_FILENAME
    document = expect_object(read_json_document(path), _FREEZE_FIELDS, "freeze record")
    if document["freeze_record_schema_version"] != FREEZE_RECORD_SCHEMA_VERSION:
        raise OfflineQArtifactError("unsupported freeze record schema version")
    if document["protocol_id"] != PROTOCOL_ID:
        raise OfflineQArtifactError("freeze record protocol_id is not the locked one")
    if document["strength_claim"] is not None:
        raise OfflineQArtifactError("freeze record must not carry a strength claim")
    retention = expect_object(
        document["retention"],
        {"backend", "key", "checkpoint_relative_path"},
        "freeze record retention",
    )
    target = RetentionTarget(
        backend=expect_str(retention["backend"], "retention.backend"),
        root=Path(bundle_path).parent.resolve(),
        key=expect_str(retention["key"], "retention.key"),
    )
    return OfflineQFreeze(
        dataset_identity=expect_str(document["dataset_identity"], "dataset_identity"),
        bc_checkpoint_identity=expect_str(
            document["bc_checkpoint_identity"], "bc_checkpoint_identity"
        ),
        q_checkpoint_identity=expect_str(
            document["q_checkpoint_identity"], "q_checkpoint_identity"
        ),
        retention=target,
    )


def strict_readback(bundle_path: str | Path) -> RetainedCandidates:
    """retained bundleを再readbackし、freeze recordとcheckpoint identityを照合する。"""
    bundle_path = Path(bundle_path)
    freeze = load_freeze_record(bundle_path)
    bc_checkpoint = bc_training.load_checkpoint(bundle_path / BC_CHECKPOINT_DIRNAME)
    q_checkpoint = q_training.load_checkpoint(bundle_path / Q_CHECKPOINT_DIRNAME)
    if bc_checkpoint.identity != freeze.bc_checkpoint_identity:
        raise OfflineQArtifactError(
            "retained BC checkpoint identity does not match the freeze record"
        )
    if q_checkpoint.identity != freeze.q_checkpoint_identity:
        raise OfflineQArtifactError(
            "retained Q checkpoint identity does not match the freeze record"
        )
    if bc_checkpoint.manifest["dataset_identity"] != freeze.dataset_identity:
        raise OfflineQArtifactError(
            "retained BC checkpoint dataset identity does not match the freeze record"
        )
    if q_checkpoint.manifest["dataset_identity"] != freeze.dataset_identity:
        raise OfflineQArtifactError(
            "retained Q checkpoint dataset identity does not match the freeze record"
        )
    return RetainedCandidates(
        freeze=freeze, bc_checkpoint=bc_checkpoint, q_checkpoint=q_checkpoint
    )


__all__ = [
    "BC_CHECKPOINT_DIRNAME",
    "FREEZE_RECORD_FILENAME",
    "FREEZE_RECORD_SCHEMA_VERSION",
    "Q_CHECKPOINT_DIRNAME",
    "OfflineQFreeze",
    "RetainedCandidates",
    "Stage4aRetentionError",
    "freeze_candidates",
    "load_freeze_record",
    "strict_readback",
]
