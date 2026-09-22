"""Issue #331 O0 learner: TRAIN/SELECT-only materialization and frozen checkpoint.

This module deliberately has no OFFLINE-EVAL entry point.  It consumes only the
prelocked TRAIN and SELECT scientific payloads, maps SELECT onto the established
Stage-2 VALIDATION slot, and reuses the existing flat-BC trainer unchanged.

The purpose-specific boundary is:

    retained #331 scientific corpus
        -> strict TRAIN/SELECT payload read
        -> established 8204 -> 128 ReLU -> 802 flat BC
        -> SELECT-only checkpoint selection
        -> write-once frozen checkpoint + strict readback

OFFLINE-EVAL remains unopened until a later, separately reviewed evaluator path.
"""

from __future__ import annotations

import hashlib
import json
import math
import sys
from array import array
from dataclasses import dataclass
from pathlib import Path
from shutil import rmtree
from tempfile import mkdtemp

from lisjong_arena._artifact_io import canonical_json_text, parse_json_text
from lisjong_arena.learned_policy_stage2.artifact import feature_block, vocabulary_block
from lisjong_arena.learned_policy_stage2.network import create_model, parameter_count
from lisjong_arena.learned_policy_stage2.protocol import (
    EXPECTED_PARAMETER_COUNT,
    FEATURE_DIMENSION,
    LOCKED_FEATURE_SCHEMA_FINGERPRINT,
    LOCKED_VOCABULARY_FINGERPRINT,
    VOCABULARY_SIZE,
    Split,
    verify_contract_identity,
)
from lisjong_arena.learned_policy_stage2.training import (
    SplitTensors,
    evaluate_masked_cross_entropy,
    locked_model_block,
    locked_training_block,
    train_from_split_tensors,
)
from lisjong_arena.single_round_artifact import (
    collect_execution_provenance,
    execution_provenance_to_dict,
)

from . import source_record
from .corpus import _file_info, _read_row
from .protocol import GAME_MODE, ZERO_FAILURES, ordered_games, validate_lock
from .qualification import SCHEMA, read_document, unseal
from .semantics import OffenseError

CHECKPOINT_SCHEMA = "arena-offense-o0-bc-checkpoint-v1"
MANIFEST_FILENAME = "manifest.json"
WEIGHTS_FILENAME = "weights.pt"
TRAINING_SPLITS = ("TRAIN", "SELECT")
FORBIDDEN_PRECHECKPOINT_SPLIT = "OFFLINE-EVAL"

_FEATURE_ROW_BYTES = FEATURE_DIMENSION * 4
_MASK_ROW_BYTES = VOCABULARY_SIZE


@dataclass(frozen=True, slots=True)
class CorpusTrainingView:
    """Metadata safe for the pre-checkpoint learner path."""

    corpus_path: Path
    corpus_identity: str
    lock_identity: str
    source_record_identity: str
    train_seeds: tuple[int, ...]
    select_seeds: tuple[int, ...]
    game_entries: tuple[tuple[int, str, int, dict], ...]


@dataclass(frozen=True, slots=True)
class LoadedOffenseCheckpoint:
    path: Path
    manifest: dict
    model: object

    @property
    def identity(self) -> str:
        return self.manifest["checkpoint_identity"]

    @property
    def weights_sha256(self) -> str:
        return self.manifest["weights_sha256"]


def _require_current_student_contract(lock: dict) -> None:
    verify_contract_identity()
    binding = lock["qualification"]["binding"]
    if (
        binding.get("feature_dimension") != FEATURE_DIMENSION
        or binding.get("vocabulary_size") != VOCABULARY_SIZE
        or binding.get("feature_fingerprint") != LOCKED_FEATURE_SCHEMA_FINGERPRINT
        or binding.get("vocabulary_fingerprint") != LOCKED_VOCABULARY_FINGERPRINT
    ):
        raise OffenseError("STUDENT CONTRACT STALE")


def _validate_scientific_manifest_metadata(corpus_path: Path) -> dict:
    """Validate the sealed scientific manifest without opening game payloads.

    The OFFLINE-EVAL game summaries are not aggregated, reported, or used for any
    branch in this pre-checkpoint path.  Only membership/identity structure is
    retained in the returned training view.
    """

    manifest = read_document(corpus_path / MANIFEST_FILENAME)
    body = unseal(manifest)
    if (
        set(body)
        != {
            "schema",
            "kind",
            "lock",
            "p2_evidence",
            "games",
            "support",
            "failures",
            "p2_outcome",
        }
        or manifest["schema"] != SCHEMA
        or manifest["kind"] != "corpus"
    ):
        raise OffenseError("invalid scientific corpus manifest")
    lock = manifest["lock"]
    if lock["request"]["phase"] != "SCIENTIFIC":
        raise OffenseError("learner requires the SCIENTIFIC corpus")
    validate_lock(lock, manifest["p2_evidence"])
    _require_current_student_contract(lock)
    if manifest["p2_outcome"] is not None:
        raise OffenseError("scientific corpus must not carry a P2 outcome")
    if manifest["failures"] != dict.fromkeys(ZERO_FAILURES, 0):
        raise OffenseError("scientific corpus contains generation failures")

    games = ordered_games(lock)
    if len(manifest["games"]) != len(games):
        raise OffenseError("scientific corpus game membership is incomplete")
    expected_names = {"manifest.json", *(f"game-{i:03d}" for i in range(len(games)))}
    if {path.name for path in corpus_path.iterdir()} != expected_names:
        raise OffenseError("scientific corpus contains missing/unexpected paths")

    for ordinal, ((split, seed), game) in enumerate(
        zip(games, manifest["games"], strict=True)
    ):
        game_body = unseal(game)
        if set(game_body) != {
            "seed",
            "split",
            "lock_identity",
            "game_mode",
            "decision_count",
            "steps",
            "choice_rows",
            "forced_rows",
            "support",
            "files",
        }:
            raise OffenseError("invalid scientific game summary")
        if (
            game["seed"],
            game["split"],
            game["lock_identity"],
            game["game_mode"],
        ) != (seed, split, lock["identity"], GAME_MODE):
            raise OffenseError("scientific game membership/provenance mismatch")
        if set(game["files"]) != {"rows.jsonl", "features.f32", "legal-mask.u8"}:
            raise OffenseError("scientific game file contract mismatch")
        if split in TRAINING_SPLITS and (
            type(game["choice_rows"]) is not int or game["choice_rows"] <= 0
        ):
            raise OffenseError("TRAIN/SELECT scientific game has no choice rows")
        # Deliberately do not inspect/aggregate OFFLINE-EVAL support or counts.
        # Payload and support exposure belongs to the post-checkpoint evaluator.

    return manifest


def _validate_source_manifest_only(
    source_path: Path, *, lock: dict, corpus_identity: str
) -> dict:
    manifest = read_document(source_path / MANIFEST_FILENAME)
    body = unseal(manifest)
    if manifest["schema"] != source_record.SOURCE_SCHEMA_V2:
        raise OffenseError("learner requires current source-record schema v2")
    expected_fields = {
        "schema",
        "kind",
        "lock_identity",
        "scientific_corpus_identity",
        "game_mode",
        "source_contract",
        "allocation_bindings",
        "games",
    }
    if set(body) != expected_fields:
        raise OffenseError("invalid source-record manifest fields")
    if (
        manifest["kind"] != source_record.SOURCE_KIND
        or manifest["lock_identity"] != lock["identity"]
        or manifest["scientific_corpus_identity"] != corpus_identity
        or manifest["game_mode"] != GAME_MODE
        or manifest["source_contract"] != lock["qualification"]["binding"]
        or manifest["allocation_bindings"] != lock["request"]["allocation_bindings"]
    ):
        raise OffenseError("source-record provenance differs from scientific corpus")
    if len(manifest["games"]) != len(ordered_games(lock)):
        raise OffenseError("source-record game membership is incomplete")
    return manifest


def open_training_view(
    corpus_path: str | Path,
    source_record_path: str | Path,
    *,
    expected_corpus_identity: str | None = None,
) -> CorpusTrainingView:
    """Open only metadata required to locate TRAIN/SELECT payloads."""

    corpus_path = Path(corpus_path)
    source_record_path = Path(source_record_path)
    manifest = _validate_scientific_manifest_metadata(corpus_path)
    if (
        expected_corpus_identity is not None
        and manifest["identity"] != expected_corpus_identity
    ):
        raise OffenseError("scientific corpus identity differs from expected handoff")
    lock = manifest["lock"]
    source_manifest = _validate_source_manifest_only(
        source_record_path, lock=lock, corpus_identity=manifest["identity"]
    )

    selected: list[tuple[int, str, int, dict]] = []
    for ordinal, ((split, seed), game) in enumerate(
        zip(ordered_games(lock), manifest["games"], strict=True)
    ):
        if split in TRAINING_SPLITS:
            selected.append((ordinal, split, seed, game))

    populations = lock["request"]["populations"]
    expected_membership = tuple(
        [("TRAIN", seed) for seed in populations["TRAIN"]]
        + [("SELECT", seed) for seed in populations["SELECT"]]
    )
    actual_membership = tuple((split, seed) for _, split, seed, _ in selected)
    if actual_membership != expected_membership:
        raise OffenseError("TRAIN/SELECT membership ordering differs from protocol lock")
    return CorpusTrainingView(
        corpus_path=corpus_path,
        corpus_identity=manifest["identity"],
        lock_identity=lock["identity"],
        source_record_identity=source_manifest["identity"],
        train_seeds=tuple(populations["TRAIN"]),
        select_seeds=tuple(populations["SELECT"]),
        game_entries=tuple(selected),
    )


def _tensor_split_for_name(split: str) -> Split:
    if split == "TRAIN":
        return Split.TRAIN
    if split == "SELECT":
        return Split.VALIDATION
    raise OffenseError(f"pre-checkpoint learner must not open split {split}")


def _load_choice_split(view: CorpusTrainingView, split: str) -> SplitTensors:
    """Strict-read one allowed split and materialize choice rows only."""

    if split not in TRAINING_SPLITS:
        raise OffenseError(f"pre-checkpoint learner must not open split {split}")
    import torch

    feature_bytes = bytearray()
    mask_bytes = bytearray()
    targets: list[int] = []
    global_indices: list[int] = []

    for ordinal, game_split, seed, game in view.game_entries:
        if game_split != split:
            continue
        game_path = view.corpus_path / f"game-{ordinal:03d}"
        filenames = {"rows.jsonl", "features.f32", "legal-mask.u8"}
        if {path.name for path in game_path.iterdir()} != filenames:
            raise OffenseError("missing/unexpected scientific game payload")
        for name in filenames:
            if _file_info(game_path / name) != game["files"][name]:
                raise OffenseError("scientific game payload checksum/size mismatch")

        choices = 0
        total = 0
        with (
            (game_path / "rows.jsonl").open(encoding="utf-8") as rows,
            (game_path / "features.f32").open("rb") as features,
            (game_path / "legal-mask.u8").open("rb") as masks,
        ):
            for line in rows:
                row = parse_json_text(line)
                trace, _ = _read_row(row)
                if row["decision_ordinal"] != total:
                    raise OffenseError("scientific decision ordering mismatch")
                if len(trace.legal_actions) >= 2:
                    payload = features.read(_FEATURE_ROW_BYTES)
                    mask = masks.read(_MASK_ROW_BYTES)
                    if (
                        len(payload) != _FEATURE_ROW_BYTES
                        or len(mask) != _MASK_ROW_BYTES
                    ):
                        raise OffenseError("scientific tensor payload truncated")
                    expected_mask = bytes(
                        int(index in row["legal_indices"])
                        for index in range(VOCABULARY_SIZE)
                    )
                    if mask != expected_mask:
                        raise OffenseError("legal-mask payload differs from row")
                    feature_bytes.extend(payload)
                    mask_bytes.extend(mask)
                    targets.append(row["teacher_action_index"])
                    global_indices.append(len(global_indices))
                    choices += 1
                total += 1
            if features.read(1) or masks.read(1):
                raise OffenseError("unexpected trailing scientific tensor payload")

        if total != game["decision_count"] or choices != game["choice_rows"]:
            raise OffenseError("scientific game choice-row accounting mismatch")

    if not targets:
        raise OffenseError(f"{split} contains no choice rows")
    if sys.byteorder != "little":
        # Corpus payload is explicitly little-endian float32.
        values = array("f")
        values.frombytes(feature_bytes)
        values.byteswap()
        feature_bytes = bytearray(values.tobytes())

    features_tensor = torch.frombuffer(feature_bytes, dtype=torch.float32).reshape(
        len(targets), FEATURE_DIMENSION
    )
    if not bool(torch.isfinite(features_tensor).all()):
        raise OffenseError(f"{split} features contain non-finite values")
    legal_mask = (
        torch.frombuffer(mask_bytes, dtype=torch.uint8)
        .reshape(len(targets), VOCABULARY_SIZE)
        .bool()
    )
    target_tensor = torch.tensor(targets, dtype=torch.long)
    if not bool(legal_mask.gather(1, target_tensor.unsqueeze(1)).all()):
        raise OffenseError(f"{split} teacher label lies outside legal mask")

    return SplitTensors(
        split=_tensor_split_for_name(split),
        features=features_tensor,
        legal_mask=legal_mask,
        targets=target_tensor,
        row_indices=tuple(global_indices),
    )


def load_training_tensors(view: CorpusTrainingView) -> dict[Split, SplitTensors]:
    """Materialize exactly TRAIN and SELECT; there is no OFFLINE-EVAL argument."""

    return {
        Split.TRAIN: _load_choice_split(view, "TRAIN"),
        Split.VALIDATION: _load_choice_split(view, "SELECT"),
    }


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def checkpoint_identity(manifest: dict) -> str:
    fields = (
        "checkpoint_schema",
        "scientific_corpus_identity",
        "source_record_identity",
        "protocol_lock_identity",
        "train_seeds",
        "select_seeds",
        "feature",
        "vocabulary",
        "model",
        "training",
        "consumer_provenance",
        "selected_epoch",
        "selected_select_choice_masked_ce",
        "train_choice_masked_ce",
        "select_choice_masked_ce",
        "parameter_count",
        "weights_sha256",
    )
    missing = [field for field in fields if field not in manifest]
    if missing:
        raise OffenseError(f"checkpoint manifest missing identity fields: {missing}")
    logical = {field: manifest[field] for field in fields}
    return _sha256(canonical_json_text(logical).encode("utf-8"))


def save_checkpoint(
    destination: str | Path,
    *,
    view: CorpusTrainingView,
    run,
    train_choice_masked_ce: float,
    select_choice_masked_ce: float,
) -> LoadedOffenseCheckpoint:
    """Publish a write-once checkpoint and immediately strict-read it."""

    import torch

    destination = Path(destination)
    if destination.exists():
        raise FileExistsError(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(
        mkdtemp(prefix=f".{destination.name}-staging-", dir=destination.parent)
    )
    published = False
    try:
        weights_path = staging / WEIGHTS_FILENAME
        torch.save(run.model.state_dict(), weights_path)
        weights = weights_path.read_bytes()
        consumer_provenance = execution_provenance_to_dict(
            collect_execution_provenance()
        )
        manifest = {
            "checkpoint_schema": CHECKPOINT_SCHEMA,
            "scientific_corpus_identity": view.corpus_identity,
            "source_record_identity": view.source_record_identity,
            "protocol_lock_identity": view.lock_identity,
            "train_seeds": list(view.train_seeds),
            "select_seeds": list(view.select_seeds),
            "feature": feature_block(),
            "vocabulary": vocabulary_block(),
            "model": locked_model_block(),
            "training": locked_training_block(),
            "consumer_provenance": consumer_provenance,
            "selected_epoch": run.selected_epoch,
            "selected_select_choice_masked_ce": run.selected_validation_choice_masked_ce,
            "train_choice_masked_ce": train_choice_masked_ce,
            "select_choice_masked_ce": select_choice_masked_ce,
            "parameter_count": parameter_count(run.model),
            "weights_bytes": len(weights),
            "weights_sha256": _sha256(weights),
            "epoch_history": [record.to_document() for record in run.history],
            "runtime": dict(run.runtime)
            | {
                "training_wall_clock_seconds": run.wall_clock_seconds,
                "peak_process_ram_bytes": run.peak_process_ram_bytes,
            },
        }
        manifest["checkpoint_identity"] = checkpoint_identity(manifest)
        (staging / MANIFEST_FILENAME).write_text(
            canonical_json_text(manifest), encoding="utf-8", newline="\n"
        )
        staging.rename(destination)
        published = True
    finally:
        if not published:
            rmtree(staging, ignore_errors=True)
    return load_checkpoint(destination)


def load_checkpoint(
    path: str | Path,
    *,
    expected_corpus_identity: str | None = None,
) -> LoadedOffenseCheckpoint:
    """Strict-load and freeze the #331 O0 checkpoint."""

    import torch

    verify_contract_identity()
    path = Path(path)
    if not path.is_dir():
        raise OffenseError("checkpoint path is not a directory")
    if {child.name for child in path.iterdir()} != {
        MANIFEST_FILENAME,
        WEIGHTS_FILENAME,
    }:
        raise OffenseError("checkpoint contains missing/unexpected files")

    text = (path / MANIFEST_FILENAME).read_text(encoding="utf-8")
    try:
        manifest = json.loads(text)
    except json.JSONDecodeError as error:
        raise OffenseError("checkpoint manifest is not valid JSON") from error
    if canonical_json_text(manifest) != text:
        raise OffenseError("checkpoint manifest is not canonical JSON")
    if manifest.get("checkpoint_schema") != CHECKPOINT_SCHEMA:
        raise OffenseError("unsupported offense checkpoint schema")
    if manifest.get("feature") != feature_block():
        raise OffenseError("checkpoint feature contract is stale")
    if manifest.get("vocabulary") != vocabulary_block():
        raise OffenseError("checkpoint vocabulary contract is stale")
    if manifest.get("model") != locked_model_block():
        raise OffenseError("checkpoint model config is not the locked flat BC")
    if manifest.get("training") != locked_training_block():
        raise OffenseError("checkpoint training config is not the locked flat BC")
    if manifest.get("parameter_count") != EXPECTED_PARAMETER_COUNT:
        raise OffenseError("checkpoint parameter count differs from locked model")
    numeric_metrics = (
        manifest.get("selected_select_choice_masked_ce"),
        manifest.get("train_choice_masked_ce"),
        manifest.get("select_choice_masked_ce"),
    )
    if (
        type(manifest.get("selected_epoch")) is not int
        or manifest["selected_epoch"] <= 0
        or any(
            type(value) not in (int, float) or not math.isfinite(value)
            for value in numeric_metrics
        )
        or not math.isclose(
            manifest["selected_select_choice_masked_ce"],
            manifest["select_choice_masked_ce"],
            rel_tol=0.0,
            abs_tol=1e-9,
        )
    ):
        raise OffenseError("checkpoint training metrics are invalid")
    if type(manifest.get("consumer_provenance")) is not dict:
        raise OffenseError("checkpoint consumer provenance is invalid")
    if manifest.get("checkpoint_identity") != checkpoint_identity(manifest):
        raise OffenseError("checkpoint identity mismatch")
    if (
        expected_corpus_identity is not None
        and manifest.get("scientific_corpus_identity") != expected_corpus_identity
    ):
        raise OffenseError("checkpoint scientific corpus identity mismatch")

    weights = (path / WEIGHTS_FILENAME).read_bytes()
    if len(weights) != manifest.get("weights_bytes") or _sha256(
        weights
    ) != manifest.get("weights_sha256"):
        raise OffenseError("checkpoint weights digest/size mismatch")
    state_dict = torch.load(
        path / WEIGHTS_FILENAME, weights_only=True, map_location="cpu"
    )
    model = create_model()
    try:
        model.load_state_dict(state_dict, strict=True)
    except RuntimeError as error:
        raise OffenseError(
            "checkpoint state_dict does not match locked model"
        ) from error
    model.eval()
    for parameter in model.parameters():
        parameter.requires_grad_(False)
    return LoadedOffenseCheckpoint(path=path, manifest=manifest, model=model)


def train_once(
    corpus_path: str | Path,
    source_record_path: str | Path,
    checkpoint_path: str | Path,
    *,
    expected_corpus_identity: str | None = None,
) -> LoadedOffenseCheckpoint:
    """Run the predeclared BC training exactly once from TRAIN + SELECT."""

    view = open_training_view(
        corpus_path,
        source_record_path,
        expected_corpus_identity=expected_corpus_identity,
    )
    tensors = load_training_tensors(view)
    run = train_from_split_tensors(tensors)
    train_ce, _ = evaluate_masked_cross_entropy(run.model, tensors[Split.TRAIN])
    select_ce, _ = evaluate_masked_cross_entropy(run.model, tensors[Split.VALIDATION])
    if not math.isclose(
        select_ce,
        run.selected_validation_choice_masked_ce,
        rel_tol=0.0,
        abs_tol=1e-9,
    ):
        raise OffenseError(
            "frozen checkpoint does not reproduce SELECT selection metric"
        )
    return save_checkpoint(
        checkpoint_path,
        view=view,
        run=run,
        train_choice_masked_ce=train_ce,
        select_choice_masked_ce=select_ce,
    )


__all__ = [
    "CHECKPOINT_SCHEMA",
    "CorpusTrainingView",
    "LoadedOffenseCheckpoint",
    "checkpoint_identity",
    "load_checkpoint",
    "load_training_tensors",
    "open_training_view",
    "save_checkpoint",
    "train_once",
]
