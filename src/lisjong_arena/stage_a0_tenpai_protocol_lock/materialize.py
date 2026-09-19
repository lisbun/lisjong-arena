"""Exact retained TRAIN+VALIDATION materialization for the #259 scientific lock.

This module replays only the locked retained prefix 245..270.  Protected TEST
271..276 is never read or executed.  Public rows are not regenerated into a new
corpus: every replayed decision must byte-align with the retained dataset row
before its privileged label cells are accepted.
"""

from __future__ import annotations

import hashlib
import json
from array import array
from dataclasses import dataclass
from pathlib import Path
from shutil import rmtree
from tempfile import mkdtemp

from lisjong_arena._artifact_io import canonical_json_text, sha256_bytes
from lisjong_arena.learned_policy_input import MAX_LIVE_WALL_TILES, MAX_MELDS_PER_PLAYER
from lisjong_arena.learned_policy_offline_q.artifact import (
    load_dataset_seed_prefix,
    provenance_document,
)
from lisjong_arena.learned_policy_offline_q.protocol import Split, split_for_seed
from lisjong_arena.learned_policy_stage2.recording import RoundOrdinals
from lisjong_arena.stage_a0_tenpai_feasibility.emission import emit_decision
from lisjong_arena.stage_a0_tenpai_feasibility.execution import (
    observed_decisions_for_seed,
)
from lisjong_arena.stage_a0_tenpai_feasibility.protocol import (
    exact_wait_implementation_identity,
)
from lisjong_arena.stage_a0_tenpai_feasibility.retained import (
    compare_source_semantic_provenance,
    instrumentation_provenance_delta,
)
from lisjong_arena.stage_a0_tenpai_feasibility.sidecar import (
    ROUTE_RETAINED,
    LoadedSidecar,
    load_sidecar,
    write_sidecar,
)

from .protocol import (
    CANONICAL_WAIT_IMPLEMENTATION_IDENTITY,
    PUBLIC_KEYS_SCHEMA_VERSION,
    PROTECTED_TEST_SEEDS,
    RETAINED_DATASET_IDENTITY,
    SCIENTIFIC_SEEDS,
    SOURCE_LISJONG_ENGINE_REVISION,
    SOURCE_LISJONG_REVISION,
    SOURCE_PYTHON_VERSION,
    SOURCE_RIICHIENV_VERSION,
    TRAIN_SEEDS,
    VALIDATION_SEEDS,
    StageA0ProtocolLockError,
)

PUBLIC_KEYS_MANIFEST = "manifest.json"
PUBLIC_KEYS_ROWS = "rows.jsonl"

_PUBLIC_KEY_FIELDS = {
    "source_identity",
    "seed",
    "split",
    "step_ordinal",
    "decision_ordinal",
    "actor_seat",
    "relative_offset",
    "live_wall_tiles_remaining",
    "public_meld_count",
}
_PUBLIC_MANIFEST_FIELDS = {
    "schema_version",
    "artifact_identity",
    "source_identity",
    "sidecar_identity",
    "train_seeds",
    "validation_seeds",
    "protected_test_seeds_unread",
    "row_count",
    "cell_count",
    "provenance",
    "files",
}


@dataclass(frozen=True, slots=True)
class PublicKeyRecord:
    source_identity: str
    seed: int
    split: Split
    step_ordinal: int
    decision_ordinal: int
    actor_seat: int
    relative_offset: int
    live_wall_tiles_remaining: int
    public_meld_count: int

    @property
    def cell_key(self) -> tuple[int, int, int, int, int]:
        return (
            self.seed,
            self.step_ordinal,
            self.decision_ordinal,
            self.actor_seat,
            self.relative_offset,
        )

    def to_document(self) -> dict[str, object]:
        return {
            "source_identity": self.source_identity,
            "seed": self.seed,
            "split": self.split.value,
            "step_ordinal": self.step_ordinal,
            "decision_ordinal": self.decision_ordinal,
            "actor_seat": self.actor_seat,
            "relative_offset": self.relative_offset,
            "live_wall_tiles_remaining": self.live_wall_tiles_remaining,
            "public_meld_count": self.public_meld_count,
        }


@dataclass(frozen=True, slots=True)
class LoadedPublicKeys:
    path: Path
    manifest: dict
    records: tuple[PublicKeyRecord, ...]

    @property
    def identity(self) -> str:
        return self.manifest["artifact_identity"]


def _expected_source_semantics() -> dict[str, str]:
    return {
        "lisjong_revision": SOURCE_LISJONG_REVISION,
        "lisjong_engine_revision": SOURCE_LISJONG_ENGINE_REVISION,
        "riichienv_version": SOURCE_RIICHIENV_VERSION,
        "python_version": SOURCE_PYTHON_VERSION,
    }


def _require_materialization_environment(
    dataset_provenance: dict, current_provenance: dict
) -> dict[str, object]:
    for name, expected in _expected_source_semantics().items():
        if dataset_provenance[name] != expected:
            raise StageA0ProtocolLockError(
                f"retained dataset provenance {name} is not the locked source value"
            )
    differing = compare_source_semantic_provenance(
        dataset_provenance, current_provenance
    )
    if differing:
        raise StageA0ProtocolLockError(
            "scientific materialization must reproduce #258 source semantics; "
            f"differing fields: {list(differing)!r}"
        )
    actual_wait = exact_wait_implementation_identity()
    if actual_wait != CANONICAL_WAIT_IMPLEMENTATION_IDENTITY:
        raise StageA0ProtocolLockError(
            "canonical exact-wait implementation differs from the #258 qualified "
            "identity"
        )
    return instrumentation_provenance_delta(dataset_provenance, current_provenance)


def _align_scientific_prefix(
    dataset,
    *,
    observed_decision_source,
) -> tuple[tuple, tuple[PublicKeyRecord, ...]]:
    grouped: dict[int, list[int]] = {seed: [] for seed in SCIENTIFIC_SEEDS}
    for index, row in enumerate(dataset.rows):
        if row.seed not in grouped:
            raise StageA0ProtocolLockError(
                "scientific prefix reader returned a row outside TRAIN/VALIDATION"
            )
        grouped[row.seed].append(index)

    cells = []
    public_keys: list[PublicKeyRecord] = []
    for seed in SCIENTIFIC_SEEDS:
        split = split_for_seed(seed)
        if split is Split.TEST:
            raise StageA0ProtocolLockError(
                "protected TEST entered scientific materialization"
            )
        indices = grouped[seed]
        if not indices:
            raise StageA0ProtocolLockError(f"seed {seed} has no retained decision rows")

        rounds = RoundOrdinals()
        emissions = {}
        round_ordinals = {}
        public_by_key = {}
        for decision in observed_decision_source(seed):
            emission = emit_decision(
                decision,
                source_identity=RETAINED_DATASET_IDENTITY,
                seed=seed,
                split=split,
            )
            key = (
                decision.step_ordinal,
                decision.decision_ordinal,
                int(decision.actor_seat),
            )
            if key in emissions:
                raise StageA0ProtocolLockError(
                    f"seed {seed} emitted duplicate decision identity {key!r}"
                )
            emissions[key] = emission
            state = decision.context.input
            round_ordinals[key] = rounds.resolve(
                (
                    state.round.round_wind.value,
                    state.round.hand_number,
                    state.round.honba,
                )
            )
            live_wall = state.round.live_wall_tiles_remaining
            if not 0 <= live_wall <= MAX_LIVE_WALL_TILES:
                raise StageA0ProtocolLockError(
                    "live-wall key is outside the public contract"
                )
            public_by_key[key] = (
                live_wall,
                {
                    int(seat): len(state.players[int(seat)].melds)
                    for seat in [cell.identity.seat for cell in emission.cells]
                },
            )

        for index in indices:
            row = dataset.rows[index]
            key = (row.step_ordinal, row.decision_ordinal, row.actor_seat)
            emission = emissions.get(key)
            if emission is None:
                raise StageA0ProtocolLockError(
                    f"seed {seed} retained row {index} has no replayed decision {key!r}"
                )
            for name, retained_value, replayed_value in (
                ("round_wind", row.round_wind, emission.row.round_wind),
                ("hand_number", row.hand_number, emission.row.hand_number),
                ("honba", row.honba, emission.row.honba),
                ("round_ordinal", row.round_ordinal, round_ordinals[key]),
            ):
                if retained_value != replayed_value:
                    raise StageA0ProtocolLockError(
                        f"seed {seed} row {index} {name} mismatch: "
                        f"{retained_value!r} != {replayed_value!r}"
                    )
            if (
                array("f", dataset.feature_row(index)).tobytes()
                != emission.row.feature_bytes()
            ):
                raise StageA0ProtocolLockError(
                    f"seed {seed} row {index} feature bytes do not exact-align"
                )
            if dataset.legal_mask_row(index) != emission.row.legal_mask:
                raise StageA0ProtocolLockError(
                    f"seed {seed} row {index} legal mask does not exact-align"
                )
            if row.legal_action_count != emission.row.legal_action_count:
                raise StageA0ProtocolLockError(
                    f"seed {seed} row {index} legal-action count does not exact-align"
                )
            if row.behavior_action_index != emission.row.teacher_action_index:
                raise StageA0ProtocolLockError(
                    f"seed {seed} row {index} teacher action does not exact-align"
                )

            live_wall, meld_counts = public_by_key[key]
            for cell in emission.cells:
                meld_count = meld_counts[int(cell.identity.seat)]
                if not 0 <= meld_count <= MAX_MELDS_PER_PLAYER:
                    raise StageA0ProtocolLockError(
                        "public meld-count key is outside the public contract"
                    )
                cells.append(cell)
                public_keys.append(
                    PublicKeyRecord(
                        source_identity=RETAINED_DATASET_IDENTITY,
                        seed=seed,
                        split=split,
                        step_ordinal=row.step_ordinal,
                        decision_ordinal=row.decision_ordinal,
                        actor_seat=row.actor_seat,
                        relative_offset=cell.identity.viewer_relative_offset,
                        live_wall_tiles_remaining=live_wall,
                        public_meld_count=meld_count,
                    )
                )

    expected_rows = len(dataset.rows)
    if len(cells) != expected_rows * 3 or len(public_keys) != expected_rows * 3:
        raise StageA0ProtocolLockError(
            "every retained decision row must produce exactly three opponent cells"
        )
    if len({record.cell_key for record in public_keys}) != len(public_keys):
        raise StageA0ProtocolLockError(
            "public key records contain duplicate cell identities"
        )
    return tuple(cells), tuple(public_keys)


def _public_artifact_identity(manifest: dict[str, object]) -> str:
    logical = {k: v for k, v in manifest.items() if k != "artifact_identity"}
    return hashlib.sha256(canonical_json_text(logical).encode("utf-8")).hexdigest()


def write_public_keys(
    destination,
    records: tuple[PublicKeyRecord, ...],
    *,
    sidecar_identity: str,
    provenance: dict[str, str],
) -> LoadedPublicKeys:
    destination = Path(destination)
    if destination.exists():
        raise FileExistsError("public-key destination already exists")
    lines = "".join(
        json.dumps(
            record.to_document(),
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
        for record in records
    ).encode("utf-8")
    row_keys = {
        (r.seed, r.step_ordinal, r.decision_ordinal, r.actor_seat) for r in records
    }
    manifest: dict[str, object] = {
        "schema_version": PUBLIC_KEYS_SCHEMA_VERSION,
        "source_identity": RETAINED_DATASET_IDENTITY,
        "sidecar_identity": sidecar_identity,
        "train_seeds": list(TRAIN_SEEDS),
        "validation_seeds": list(VALIDATION_SEEDS),
        "protected_test_seeds_unread": list(PROTECTED_TEST_SEEDS),
        "row_count": len(row_keys),
        "cell_count": len(records),
        "provenance": dict(provenance),
        "files": {
            "rows": {
                "bytes": len(lines),
                "sha256": sha256_bytes(lines),
            }
        },
    }
    manifest["artifact_identity"] = _public_artifact_identity(manifest)
    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(
        mkdtemp(prefix=f".{destination.name}-staging-", dir=destination.parent)
    )
    try:
        (staging / PUBLIC_KEYS_ROWS).write_bytes(lines)
        (staging / PUBLIC_KEYS_MANIFEST).write_text(
            canonical_json_text(manifest), encoding="utf-8", newline="\n"
        )
        staging.rename(destination)
    except BaseException:
        rmtree(staging, ignore_errors=True)
        raise
    return load_public_keys(destination)


def load_public_keys(path) -> LoadedPublicKeys:
    path = Path(path)
    if not path.is_dir():
        raise StageA0ProtocolLockError("public-key artifact path is not a directory")
    if {item.name for item in path.iterdir()} != {
        PUBLIC_KEYS_MANIFEST,
        PUBLIC_KEYS_ROWS,
    }:
        raise StageA0ProtocolLockError("public-key artifact has missing or extra files")
    text = (path / PUBLIC_KEYS_MANIFEST).read_text(encoding="utf-8")
    try:
        manifest = json.loads(text)
    except json.JSONDecodeError as exc:
        raise StageA0ProtocolLockError("public-key manifest is invalid JSON") from exc
    if type(manifest) is not dict or set(manifest) != _PUBLIC_MANIFEST_FIELDS:
        raise StageA0ProtocolLockError("public-key manifest fields are invalid")
    if manifest["schema_version"] != PUBLIC_KEYS_SCHEMA_VERSION:
        raise StageA0ProtocolLockError("unsupported public-key schema")
    if manifest["source_identity"] != RETAINED_DATASET_IDENTITY:
        raise StageA0ProtocolLockError(
            "public-key source identity is not the retained corpus"
        )
    if manifest["train_seeds"] != list(TRAIN_SEEDS):
        raise StageA0ProtocolLockError("public-key TRAIN population drifted")
    if manifest["validation_seeds"] != list(VALIDATION_SEEDS):
        raise StageA0ProtocolLockError("public-key VALIDATION population drifted")
    if manifest["protected_test_seeds_unread"] != list(PROTECTED_TEST_SEEDS):
        raise StageA0ProtocolLockError("protected TEST metadata drifted")
    if _public_artifact_identity(manifest) != manifest["artifact_identity"]:
        raise StageA0ProtocolLockError("public-key artifact identity does not match")
    if canonical_json_text(manifest) != text:
        raise StageA0ProtocolLockError("public-key manifest is not canonical JSON")

    payload = (path / PUBLIC_KEYS_ROWS).read_bytes()
    file_entry = manifest["files"]["rows"]
    if (
        len(payload) != file_entry["bytes"]
        or sha256_bytes(payload) != file_entry["sha256"]
    ):
        raise StageA0ProtocolLockError("public-key rows digest does not match manifest")
    decoded = payload.decode("utf-8")
    if decoded and not decoded.endswith("\n"):
        raise StageA0ProtocolLockError("public-key rows must end with newline")

    records = []
    for index, line in enumerate(decoded.splitlines()):
        try:
            item = json.loads(line)
        except json.JSONDecodeError as exc:
            raise StageA0ProtocolLockError(
                f"public-key row {index} is invalid JSON"
            ) from exc
        if type(item) is not dict or set(item) != _PUBLIC_KEY_FIELDS:
            raise StageA0ProtocolLockError(f"public-key row {index} fields are invalid")
        try:
            split = Split(item["split"])
        except ValueError as exc:
            raise StageA0ProtocolLockError(
                f"public-key row {index} split is invalid"
            ) from exc
        record = PublicKeyRecord(
            source_identity=item["source_identity"],
            seed=item["seed"],
            split=split,
            step_ordinal=item["step_ordinal"],
            decision_ordinal=item["decision_ordinal"],
            actor_seat=item["actor_seat"],
            relative_offset=item["relative_offset"],
            live_wall_tiles_remaining=item["live_wall_tiles_remaining"],
            public_meld_count=item["public_meld_count"],
        )
        if record.source_identity != RETAINED_DATASET_IDENTITY:
            raise StageA0ProtocolLockError("public-key record source identity drifted")
        if record.seed in PROTECTED_TEST_SEEDS or record.seed not in SCIENTIFIC_SEEDS:
            raise StageA0ProtocolLockError(
                "public-key record entered protected TEST/outside prefix"
            )
        if record.split is not split_for_seed(record.seed):
            raise StageA0ProtocolLockError(
                "public-key record split does not match seed"
            )
        if record.relative_offset not in (1, 2, 3):
            raise StageA0ProtocolLockError("public-key relative offset must be 1..3")
        if not 0 <= record.actor_seat <= 3:
            raise StageA0ProtocolLockError("public-key actor seat is outside 0..3")
        if not 0 <= record.live_wall_tiles_remaining <= MAX_LIVE_WALL_TILES:
            raise StageA0ProtocolLockError("public-key live-wall value is out of range")
        if not 0 <= record.public_meld_count <= MAX_MELDS_PER_PLAYER:
            raise StageA0ProtocolLockError("public-key meld count is out of range")
        records.append(record)

    records = tuple(records)
    if len(records) != manifest["cell_count"]:
        raise StageA0ProtocolLockError("public-key cell count differs from manifest")
    row_keys = {
        (r.seed, r.step_ordinal, r.decision_ordinal, r.actor_seat) for r in records
    }
    if len(row_keys) != manifest["row_count"]:
        raise StageA0ProtocolLockError("public-key row count differs from manifest")
    if len({r.cell_key for r in records}) != len(records):
        raise StageA0ProtocolLockError("public-key cell identities are not unique")
    return LoadedPublicKeys(path=path, manifest=manifest, records=records)


def materialize_retained_scientific_data(
    dataset_path,
    *,
    sidecar_destination,
    public_keys_destination,
    observed_decision_source=observed_decisions_for_seed,
) -> tuple[LoadedSidecar, LoadedPublicKeys, dict[str, object]]:
    """Materialize exact 245..270 labels/public keys without reading protected TEST."""
    dataset = load_dataset_seed_prefix(dataset_path, SCIENTIFIC_SEEDS)
    if dataset.identity != RETAINED_DATASET_IDENTITY:
        raise StageA0ProtocolLockError("retained dataset identity does not match #259")
    if any(row.seed in PROTECTED_TEST_SEEDS for row in dataset.rows):
        raise StageA0ProtocolLockError("protected TEST payload was read")

    current = provenance_document()
    instrumentation = _require_materialization_environment(
        dataset.manifest["provenance"], current
    )
    cells, public_keys = _align_scientific_prefix(
        dataset,
        observed_decision_source=observed_decision_source,
    )
    sidecar = write_sidecar(
        sidecar_destination,
        cells,
        route=ROUTE_RETAINED,
        source_identity=RETAINED_DATASET_IDENTITY,
        provenance=current,
    )
    public = write_public_keys(
        public_keys_destination,
        public_keys,
        sidecar_identity=sidecar.identity,
        provenance=current,
    )
    return (
        sidecar,
        public,
        {
            "dataset_identity": dataset.identity,
            "scientific_seeds": list(SCIENTIFIC_SEEDS),
            "row_count": len(dataset.rows),
            "cell_count": len(cells),
            "sidecar_identity": sidecar.identity,
            "public_keys_identity": public.identity,
            "instrumentation_provenance": instrumentation,
            "protected_test_unread": True,
        },
    )


__all__ = [
    "LoadedPublicKeys",
    "PublicKeyRecord",
    "load_public_keys",
    "materialize_retained_scientific_data",
    "write_public_keys",
]
