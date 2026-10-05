"""Retained policy source record: compressed archive plus verification evidence.

lisbun/lisjong-arena#449. A source record that passed full replay-verify is
packed into a deterministic ``tar`` + ``gzip`` archive and kept next to a
sealed evidence document. A later conversion restores it instead of
regenerating and replaying: the manifest identity, the archive SHA-256 and the
replay-verify result must all match, or nothing is restored.

```text
<archive>/
  evidence.json          sealed canonical JSON (schema below)
  source-record.tar.gz   manifest.json + game-NNN/source-record.jsonl
```

The evidence binds the source identity and population size, the archive bytes
and SHA-256, and the replay-verify result (decisions and zero mismatches) for
that same source identity. The archive is canonical source, not a derived
dataset: Learning datasets made from it stay lisjong-owned derived products.
"""

import gzip
import hashlib
import re
import shutil
import tarfile
from pathlib import Path
from tempfile import mkdtemp

from lisjong_arena._artifact_io import canonical_json_text
from lisjong_arena.offense_foundation.qualification import (
    read_document,
    seal,
    unseal,
    write_document,
)
from lisjong_arena.offense_foundation.semantics import OffenseError

from . import record
from .errors import PolicySourceRecordError

SCHEMA = "arena-policy-source-archive-v1"
KIND = "policy-source-archive"
EVIDENCE_FILENAME = "evidence.json"
ARCHIVE_FILENAME = "source-record.tar.gz"
ARCHIVE_FORMAT = "tar+gzip"

_MEMBER_PATTERN = re.compile(r"^(manifest\.json|game-[0-9]{3,}/source-record\.jsonl)$")
_CHUNK = 1024 * 1024
_EVIDENCE_FIELDS = {"schema", "kind", "source", "archive", "replay"}
_SOURCE_FIELDS = {"schema", "identity", "purpose", "teacher", "games", "decisions"}
_ARCHIVE_FIELDS = {"filename", "format", "members", "bytes", "sha256"}
_REPLAY_FIELDS = {"source_identity", "teacher", "decisions", "mismatches"}


def _file_info(path: Path) -> dict[str, object]:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(_CHUNK), b""):
            digest.update(chunk)
    return {"bytes": path.stat().st_size, "sha256": digest.hexdigest()}


def _source_facts(manifest) -> dict[str, object]:
    return {
        "schema": manifest["schema"],
        "identity": manifest["identity"],
        "purpose": manifest["purpose"],
        "teacher": manifest["source_contract"]["teacher"]["catalog_identity"],
        "games": len(manifest["games"]),
        "decisions": sum(game["decision_count"] for game in manifest["games"]),
    }


def _members(manifest) -> list[str]:
    return ["manifest.json"] + [
        f"game-{ordinal:03d}/{record.SOURCE_FILENAME}"
        for ordinal in range(len(manifest["games"]))
    ]


def _require_replay(replay, source) -> dict[str, object]:
    """Accept only a full, mismatch-free replay of exactly this source."""
    if type(replay) is not dict or not _REPLAY_FIELDS <= set(replay):
        raise PolicySourceRecordError("replay evidence is missing fields")
    facts = {field: replay[field] for field in sorted(_REPLAY_FIELDS)}
    if facts["source_identity"] != source["identity"]:
        raise PolicySourceRecordError("replay evidence is for a different source")
    if facts["teacher"] != source["teacher"]:
        raise PolicySourceRecordError("replay evidence is for a different teacher")
    if type(facts["decisions"]) is not int or facts["decisions"] != source["decisions"]:
        raise PolicySourceRecordError("replay evidence does not cover every decision")
    if type(facts["mismatches"]) is not int or facts["mismatches"] != 0:
        raise PolicySourceRecordError("replay evidence reports mismatches")
    return facts


def _write_archive(source: Path, members: list[str], path: Path) -> None:
    """Deterministic tar: fixed order, mtime 0, root ownership, mode 0644."""
    with path.open("xb") as raw:
        with gzip.GzipFile(
            filename="", mode="wb", fileobj=raw, mtime=0, compresslevel=6
        ) as compressed:
            with tarfile.open(
                fileobj=compressed, mode="w", format=tarfile.PAX_FORMAT
            ) as archive:
                for name in members:
                    member = source / name
                    info = tarfile.TarInfo(name)
                    info.size = member.stat().st_size
                    info.mtime = 0
                    info.mode = 0o644
                    info.uid = info.gid = 0
                    info.uname = info.gname = ""
                    with member.open("rb") as stream:
                        archive.addfile(info, stream)


def _iter_members(path: Path, expected: list[str] | None):
    """Yield ``(name, stream)`` for each regular member, in archive order.

    Only ``manifest.json`` and ``game-NNN/source-record.jsonl`` are accepted;
    links, directories, absolute or parent-relative names and duplicates fail
    closed. With ``expected``, the names and their order must match exactly.
    """
    seen: list[str] = []
    try:
        with tarfile.open(path, mode="r:gz") as archive:
            for info in archive:
                name = info.name
                if (
                    not info.isreg()
                    or not _MEMBER_PATTERN.fullmatch(name)
                    or name in seen
                ):
                    raise PolicySourceRecordError(
                        f"unexpected source archive member: {name!r}"
                    )
                seen.append(name)
                yield name, archive.extractfile(info)
    except (tarfile.TarError, OSError, EOFError) as error:
        raise PolicySourceRecordError(
            f"source archive is unreadable: {error}"
        ) from error
    if expected is not None and seen != expected:
        raise PolicySourceRecordError("source archive members do not match the record")


def archive_source_record(source, destination, *, replay) -> dict[str, object]:
    """Pack a replay-verified source record and its evidence into ``destination``.

    ``replay`` is the ``replay_verify`` summary of this record. The record is
    strict-read first, and the written archive is read back member by member
    against the manifest and game digests that the strict read accepted before
    the directory is published. Existing
    destinations are refused.
    """
    source, destination = Path(source), Path(destination)
    manifest = record.read_source_record(source)
    facts = _source_facts(manifest)
    replay_facts = _require_replay(replay, facts)
    members = _members(manifest)
    # Compare the archive with what the strict read accepted, not with the
    # files as they are now, so a record changed after the read is refused.
    manifest_bytes = canonical_json_text(manifest).encode("utf-8")
    expected = {
        "manifest.json": {
            "bytes": len(manifest_bytes),
            "sha256": hashlib.sha256(manifest_bytes).hexdigest(),
        },
        **{
            name: game["files"][record.SOURCE_FILENAME]
            for name, game in zip(members[1:], manifest["games"], strict=True)
        },
    }
    if destination.exists():
        raise PolicySourceRecordError(f"refusing to overwrite {destination}")
    staging = Path(
        mkdtemp(prefix=f".{destination.name}-staging-", dir=destination.parent)
    )
    try:
        archive = staging / ARCHIVE_FILENAME
        _write_archive(source, members, archive)
        for name, stream in _iter_members(archive, members):
            digest, size = hashlib.sha256(), 0
            for chunk in iter(lambda: stream.read(_CHUNK), b""):
                digest.update(chunk)
                size += len(chunk)
            if {"bytes": size, "sha256": digest.hexdigest()} != expected[name]:
                raise PolicySourceRecordError(
                    f"archived {name} differs from the record"
                )
        evidence = seal(
            {
                "schema": SCHEMA,
                "kind": KIND,
                "source": facts,
                "archive": {
                    "filename": ARCHIVE_FILENAME,
                    "format": ARCHIVE_FORMAT,
                    "members": len(members),
                    **_file_info(archive),
                },
                "replay": replay_facts,
            }
        )
        write_document(staging / EVIDENCE_FILENAME, evidence)
        if destination.exists():
            raise PolicySourceRecordError(f"refusing to overwrite {destination}")
        staging.rename(destination)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return evidence


def read_evidence(path) -> dict[str, object]:
    """Strict-read ``evidence.json`` of an archive directory (no archive check)."""
    path = Path(path)
    if not path.is_dir() or {child.name for child in path.iterdir()} != {
        EVIDENCE_FILENAME,
        ARCHIVE_FILENAME,
    }:
        raise PolicySourceRecordError("missing/unexpected source archive files")
    try:
        evidence = read_document(path / EVIDENCE_FILENAME)
        body = unseal(evidence)
    except (OffenseError, OSError, ValueError) as error:
        raise PolicySourceRecordError(
            f"invalid source archive evidence: {error}"
        ) from error
    if set(body) != _EVIDENCE_FIELDS or (body["schema"], body["kind"]) != (
        SCHEMA,
        KIND,
    ):
        raise PolicySourceRecordError("unsupported source archive evidence")
    for field, expected in (
        ("source", _SOURCE_FIELDS),
        ("archive", _ARCHIVE_FIELDS),
        ("replay", _REPLAY_FIELDS),
    ):
        if type(body[field]) is not dict or set(body[field]) != expected:
            raise PolicySourceRecordError(f"invalid source archive evidence.{field}")
    if (body["archive"]["filename"], body["archive"]["format"]) != (
        ARCHIVE_FILENAME,
        ARCHIVE_FORMAT,
    ):
        raise PolicySourceRecordError("unsupported source archive format")
    _require_replay(body["replay"], body["source"])
    return evidence


def restore_source_record(archive, destination, *, expected_identity) -> dict:
    """Restore a retained source record after matching identity, hash and replay.

    The evidence must name ``expected_identity``, the archive bytes and SHA-256
    must equal the evidence, the replay evidence must cover every decision of
    that source with zero mismatches, and the extracted record must strict-read
    to the same manifest identity and decision count. Any mismatch fails closed
    before ``destination`` exists. Returns the restored manifest.
    """
    archive, destination = Path(archive), Path(destination)
    evidence = read_evidence(archive)
    source = evidence["source"]
    if source["identity"] != expected_identity:
        raise PolicySourceRecordError(
            "source archive identity differs from the expected source identity"
        )
    payload = archive / ARCHIVE_FILENAME
    info = _file_info(payload)
    if (info["bytes"], info["sha256"]) != (
        evidence["archive"]["bytes"],
        evidence["archive"]["sha256"],
    ):
        raise PolicySourceRecordError("source archive size/SHA-256 mismatch")
    if destination.exists():
        raise PolicySourceRecordError(f"refusing to overwrite {destination}")
    staging = Path(
        mkdtemp(prefix=f".{destination.name}-staging-", dir=destination.parent)
    )
    try:
        count = 0
        for name, stream in _iter_members(payload, None):
            target = staging / name
            target.parent.mkdir(exist_ok=True)
            with target.open("xb") as output:
                shutil.copyfileobj(stream, output, _CHUNK)
            count += 1
        if count != evidence["archive"]["members"]:
            raise PolicySourceRecordError("source archive member count mismatch")
        manifest = record.read_source_record(staging)
        restored = _source_facts(manifest)
        if restored != source:
            raise PolicySourceRecordError(
                "restored source record differs from the archive evidence"
            )
        if destination.exists():
            raise PolicySourceRecordError(f"refusing to overwrite {destination}")
        staging.rename(destination)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return manifest


__all__ = [
    "ARCHIVE_FILENAME",
    "EVIDENCE_FILENAME",
    "KIND",
    "SCHEMA",
    "archive_source_record",
    "read_evidence",
    "restore_source_record",
]
