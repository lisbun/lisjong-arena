"""Durable per-seed execution checkpoint / completion receipt primitive.

lisbun/lisjong-arena#339: a long-run population executor (e.g. the Issue #326
96-hanchan wait-shape pilot) must not lose completed seeds to an interrupted
run.  This module publishes each seed's artifact and an immutable completion
receipt durably, as soon as that seed finishes, instead of waiting for the
whole population to return.

This module owns only the audit/recovery durability primitive.  It does not
decide when a checkpoint set authorizes resume, selective rerun, or partial
scientific aggregation -- see Issue #339's boundary summary.  It also never
serializes callables, factories, or arbitrary code: a checkpoint payload must
already be a plain JSON-safe document owned by the caller's artifact contract.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any, Mapping, Sequence

from lisjong_arena._artifact_io import (
    ArtifactValidationError,
    canonical_json_text,
    parse_json_text,
)

RECEIPT_SCHEMA_VERSION = "arena-seed-checkpoint-receipt-v1"

_RECEIPT_FIELDS = frozenset(
    {
        "schema_version",
        "run_id",
        "seed",
        "protocol_identity",
        "artifact_relative_path",
        "artifact_size_bytes",
        "artifact_sha256",
        "started_at",
        "completed_at",
    }
)


class DurableSeedCheckpointError(ArtifactValidationError):
    """A seed checkpoint could not be published or verified as durable."""


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise DurableSeedCheckpointError(message)


def _require_non_empty_str(value: object, name: str) -> str:
    _require(type(value) is str and bool(value), f"{name} must be a non-empty string")
    return value  # type: ignore[return-value]


def _require_timestamp(value: object, name: str) -> str:
    text = _require_non_empty_str(value, name)
    _require(text.endswith("Z"), f"{name} must be a canonical UTC timestamp")
    return text


def _artifact_filename(seed: int) -> str:
    return f"seed-{seed}.artifact.json"


def _receipt_filename(seed: int) -> str:
    return f"seed-{seed}.receipt.json"


def _write_new_durable(path: Path, payload: bytes) -> None:
    """Publish ``payload`` to ``path`` atomically, fsynced, never overwriting.

    ``os.link`` onto an existing destination fails atomically instead of
    silently overwriting it, unlike ``os.replace``; the temporary file lives
    in the same directory as the destination so the link is a same-filesystem
    rename-class operation.  The destination directory is fsynced afterwards
    so the durable-publish is not lost to a crash that drops the directory
    entry before it reaches disk.
    """

    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        raise DurableSeedCheckpointError(
            f"checkpoint destination already exists: {path.name}"
        )
    with NamedTemporaryFile(
        mode="wb", prefix=f".{path.name}.", dir=path.parent, delete=False
    ) as stream:
        temporary = Path(stream.name)
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())
    try:
        try:
            os.link(temporary, path)
        except FileExistsError as exc:
            raise DurableSeedCheckpointError(
                f"checkpoint destination already exists: {path.name}"
            ) from exc
    finally:
        temporary.unlink(missing_ok=True)
    directory_fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def publish_seed_checkpoint(
    checkpoint_dir: str | Path,
    *,
    run_id: str,
    seed: int,
    protocol_identity: str,
    payload: Mapping[str, Any],
    started_at: str,
    completed_at: str,
) -> dict[str, object]:
    """Durably publish one seed's artifact, then its immutable completion receipt.

    The artifact becomes durable (fsynced, atomically created, never
    overwritten) before the completion receipt is published, so a receipt on
    disk always means its artifact is already durable too.  Both writes fail
    closed on a duplicate seed or an existing destination -- this function
    never overwrites or silently skips.
    """

    directory = Path(checkpoint_dir)
    _require_non_empty_str(run_id, "run_id")
    _require(type(seed) is int, "seed must be an int")
    _require_non_empty_str(protocol_identity, "protocol_identity")
    _require(isinstance(payload, Mapping), "payload must be a JSON-safe mapping")
    _require_timestamp(started_at, "started_at")
    completed = _require_timestamp(completed_at, "completed_at")
    _require(started_at <= completed, "completed_at precedes started_at")

    artifact_path = directory / _artifact_filename(seed)
    artifact_bytes = canonical_json_text(dict(payload)).encode("utf-8")
    _write_new_durable(artifact_path, artifact_bytes)

    receipt: dict[str, object] = {
        "schema_version": RECEIPT_SCHEMA_VERSION,
        "run_id": run_id,
        "seed": seed,
        "protocol_identity": protocol_identity,
        "artifact_relative_path": artifact_path.name,
        "artifact_size_bytes": len(artifact_bytes),
        "artifact_sha256": _sha256_bytes(artifact_bytes),
        "started_at": started_at,
        "completed_at": completed,
    }
    receipt_path = directory / _receipt_filename(seed)
    _write_new_durable(receipt_path, canonical_json_text(receipt).encode("utf-8"))
    return receipt


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _validate_receipt(value: object, *, context: str) -> dict[str, object]:
    _require(type(value) is dict, f"{context} must be an object")
    document = value
    _require(set(document) == set(_RECEIPT_FIELDS), f"{context} fields are invalid")
    _require(
        document["schema_version"] == RECEIPT_SCHEMA_VERSION,
        f"{context} schema version is invalid",
    )
    _require_non_empty_str(document["run_id"], f"{context}.run_id")
    _require(type(document["seed"]) is int, f"{context}.seed must be an int")
    _require_non_empty_str(
        document["protocol_identity"], f"{context}.protocol_identity"
    )
    _require_non_empty_str(
        document["artifact_relative_path"], f"{context}.artifact_relative_path"
    )
    _require(
        type(document["artifact_size_bytes"]) is int
        and document["artifact_size_bytes"] >= 0,
        f"{context}.artifact_size_bytes must be a non-negative int",
    )
    digest = document["artifact_sha256"]
    _require(
        type(digest) is str
        and len(digest) == 64
        and all(character in "0123456789abcdef" for character in digest),
        f"{context}.artifact_sha256 must be a lowercase hex SHA-256",
    )
    _require_timestamp(document["started_at"], f"{context}.started_at")
    _require_timestamp(document["completed_at"], f"{context}.completed_at")
    return document


def read_seed_checkpoint(
    checkpoint_dir: str | Path,
    seed: int,
    *,
    run_id: str,
    protocol_identity: str,
) -> tuple[dict[str, object], object]:
    """Read back one durably published seed's receipt and payload, fail closed.

    The artifact bytes are re-hashed and compared against the receipt's bound
    hash and size; any mismatch, and any receipt binding a different run or
    protocol identity, fails closed rather than returning an unverified
    payload.
    """

    directory = Path(checkpoint_dir)
    receipt_path = directory / _receipt_filename(seed)
    if not receipt_path.is_file():
        raise DurableSeedCheckpointError(f"missing durable receipt for seed {seed}")
    receipt = _validate_receipt(
        parse_json_text(receipt_path.read_text(encoding="utf-8")),
        context=f"seed {seed} receipt",
    )
    _require(receipt["seed"] == seed, f"seed {seed} receipt seed mismatch")
    _require(receipt["run_id"] == run_id, f"seed {seed} receipt run_id mismatch")
    _require(
        receipt["protocol_identity"] == protocol_identity,
        f"seed {seed} receipt protocol identity mismatch",
    )
    artifact_path = directory / str(receipt["artifact_relative_path"])
    if not artifact_path.is_file():
        raise DurableSeedCheckpointError(f"missing durable artifact for seed {seed}")
    artifact_bytes = artifact_path.read_bytes()
    _require(
        len(artifact_bytes) == receipt["artifact_size_bytes"],
        f"seed {seed} artifact size mismatch",
    )
    _require(
        _sha256_bytes(artifact_bytes) == receipt["artifact_sha256"],
        f"seed {seed} artifact hash mismatch",
    )
    payload = parse_json_text(artifact_bytes.decode("utf-8"))
    return receipt, payload


def verify_seed_checkpoint_set(
    checkpoint_dir: str | Path,
    *,
    run_id: str,
    protocol_identity: str,
    expected_seeds: Sequence[int],
) -> dict[int, dict[str, object]]:
    """Fail closed unless exactly ``expected_seeds`` are durably checkpointed.

    Missing seeds, duplicate/extra receipts beyond ``expected_seeds``, wrong
    run/protocol identity, and artifact hash/size drift all fail closed.
    Success does not, by itself, authorize resume or partial aggregation --
    the caller still owns that scientific decision.
    """

    directory = Path(checkpoint_dir)
    expected = list(expected_seeds)
    _require(
        len(expected) == len(set(expected)), "expected seed set contains duplicates"
    )

    receipts: dict[int, dict[str, object]] = {}
    for seed in expected:
        receipt, _payload = read_seed_checkpoint(
            directory, seed, run_id=run_id, protocol_identity=protocol_identity
        )
        receipts[seed] = receipt

    if directory.is_dir():
        present_receipt_seeds = {path.name for path in directory.glob("*.receipt.json")}
    else:
        present_receipt_seeds = set()
    expected_receipt_names = {_receipt_filename(seed) for seed in expected}
    _require(
        present_receipt_seeds == expected_receipt_names,
        "checkpoint directory contains unexpected seed receipts",
    )
    return receipts


__all__ = [
    "DurableSeedCheckpointError",
    "RECEIPT_SCHEMA_VERSION",
    "publish_seed_checkpoint",
    "read_seed_checkpoint",
    "verify_seed_checkpoint_set",
]
