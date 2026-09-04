"""Versioned Offline Q macro-transition dataset artifact: write, digest, readback.

1 dataset = 1 immutable directoryとし、既存pathを上書きしない。

```text
<dataset>/
    manifest.json        canonical JSON identity / protocol / provenance / digests
    rows.jsonl            1行 = 1 macro-transitionのplayer-safe metadata
    features.f32          N x 8204 little-endian float32 (row-major, current state)
    legal_mask.u8         N x 802 uint8 (0 / 1) (row-major, current state)
    next_features.f32     N x 8204 little-endian float32 (nonterminalのみ有効。
                           terminal rowはall-zero placeholderで埋める)
    next_legal_mask.u8    N x 802 uint8 (nonterminalのみ有効。同上)
```

`terminal`フィールドが`next_*`の有効性を決める。terminal rowのplaceholderは
学習側が`terminal`を見ずに誤読しないよう、all-zeroかつ`legal_mask`としては
不正な値（legal actionが0件）にしておく。

Stage 2の`Stage2DatasetWriter`（`arena-learned-policy-stage2-dataset-v1`）は
変更せず、このartifactは独立したversioned schema
（`arena-learned-policy-offlineq-dataset-v1`）を持つ。
"""

import hashlib
import json
from array import array
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from math import isfinite
from pathlib import Path
from shutil import rmtree
from tempfile import mkdtemp

from lisjong_arena._artifact_io import canonical_json_text
from lisjong_arena.single_round_artifact import collect_execution_provenance

from .errors import OfflineQArtifactError
from .model import MacroTransitionRow
from .protocol import (
    DATASET_HANCHAN_COUNT,
    DATASET_ORDERED_SEEDS,
    DATASET_SPLIT_SEEDS,
    FEATURE_DIMENSION,
    GAME_MODE,
    LOCKED_FEATURE_SCHEMA_FINGERPRINT,
    LOCKED_FEATURE_SEMANTICS_ID,
    LOCKED_TENSOR_DTYPE,
    LOCKED_TENSOR_SCHEMA_VERSION,
    LOCKED_VOCABULARY_FINGERPRINT,
    LOCKED_VOCABULARY_VERSION,
    PROTOCOL_ID,
    TEACHER_IDENTITY,
    TEACHER_POLICY_CLASS,
    TEACHER_POPULATION,
    TEACHER_SOURCE_REVISION,
    VOCABULARY_SIZE,
    Split,
    action_family,
    split_for_seed,
    verify_contract_identity,
)

DATASET_SCHEMA_VERSION = "arena-learned-policy-offlineq-dataset-v1"
MANIFEST_FILENAME = "manifest.json"
ROWS_FILENAME = "rows.jsonl"
FEATURES_FILENAME = "features.f32"
LEGAL_MASK_FILENAME = "legal_mask.u8"
NEXT_FEATURES_FILENAME = "next_features.f32"
NEXT_LEGAL_MASK_FILENAME = "next_legal_mask.u8"

_FEATURE_ROW_BYTES = FEATURE_DIMENSION * 4
_MASK_ROW_BYTES = VOCABULARY_SIZE

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
_MANIFEST_FIELDS = {
    "dataset_schema_version",
    "dataset_identity",
    "protocol",
    "feature",
    "vocabulary",
    "provenance",
    "games",
    "totals",
    "files",
}
_PROTOCOL_FIELDS = {
    "protocol_id",
    "teacher_identity",
    "teacher_policy_class",
    "teacher_population",
    "teacher_source_revision",
    "game_mode",
    "ordered_seeds",
    "split_unit",
    "train_seeds",
    "validation_seeds",
    "test_seeds",
}
_FEATURE_FIELDS = {
    "semantics_id",
    "tensor_schema_version",
    "dtype",
    "dimension",
    "schema_fingerprint",
}
_VOCABULARY_FIELDS = {"version", "size", "fingerprint"}
_GAME_FIELDS = {"seed", "split", "row_count", "scores", "ranks"}
_TOTALS_FIELDS = {"game_count", "row_count", "terminal_row_count"}
_FILE_FIELDS = {"bytes", "sha256"}
_FILE_NAMES = {"rows", "features", "legal_mask", "next_features", "next_legal_mask"}
_ARTIFACT_FILENAMES = {
    MANIFEST_FILENAME,
    ROWS_FILENAME,
    FEATURES_FILENAME,
    LEGAL_MASK_FILENAME,
    NEXT_FEATURES_FILENAME,
    NEXT_LEGAL_MASK_FILENAME,
}
_PROVENANCE_FIELDS = {
    "execution_environment",
    "lisjong_arena_version",
    "lisjong_arena_revision",
    "lisjong_version",
    "lisjong_revision",
    "lisjong_engine_version",
    "lisjong_engine_revision",
    "riichienv_version",
    "python_version",
}

_ZERO_FEATURE_ROW = bytes(_FEATURE_ROW_BYTES)
_ZERO_MASK_ROW = bytes(_MASK_ROW_BYTES)

if array("f").itemsize != 4:
    raise RuntimeError("float32 array itemsize must be 4 bytes")


def _error(message: str) -> OfflineQArtifactError:
    return OfflineQArtifactError(message)


def _expect(value: object, expected: type, context: str):
    if type(value) is not expected:
        raise _error(f"{context} must be a {expected.__name__}")
    return value


def _expect_object(value: object, fields: set[str], context: str) -> dict:
    if type(value) is not dict:
        raise _error(f"{context} must be an object")
    if set(value) != fields:
        raise _error(f"{context} fields are invalid")
    return value


def _expect_int_list(value: object, context: str) -> tuple[int, ...]:
    if type(value) is not list or any(type(item) is not int for item in value):
        raise _error(f"{context} must be an array of integers")
    return tuple(value)


def _digest(value: object, context: str) -> str:
    text = _expect(value, str, context)
    if len(text) != 64 or any(
        character not in "0123456789abcdef" for character in text
    ):
        raise _error(f"{context} must be a lowercase sha256 digest")
    return text


def feature_block() -> dict[str, object]:
    return {
        "semantics_id": LOCKED_FEATURE_SEMANTICS_ID,
        "tensor_schema_version": LOCKED_TENSOR_SCHEMA_VERSION,
        "dtype": LOCKED_TENSOR_DTYPE,
        "dimension": FEATURE_DIMENSION,
        "schema_fingerprint": LOCKED_FEATURE_SCHEMA_FINGERPRINT,
    }


def vocabulary_block() -> dict[str, object]:
    return {
        "version": LOCKED_VOCABULARY_VERSION,
        "size": VOCABULARY_SIZE,
        "fingerprint": LOCKED_VOCABULARY_FINGERPRINT,
    }


def _protocol_block() -> dict[str, object]:
    return {
        "protocol_id": PROTOCOL_ID,
        "teacher_identity": TEACHER_IDENTITY,
        "teacher_policy_class": TEACHER_POLICY_CLASS,
        "teacher_population": TEACHER_POPULATION,
        "teacher_source_revision": TEACHER_SOURCE_REVISION,
        "game_mode": GAME_MODE,
        "ordered_seeds": list(DATASET_ORDERED_SEEDS),
        "split_unit": "whole_hanchan",
        "train_seeds": list(DATASET_SPLIT_SEEDS[Split.TRAIN]),
        "validation_seeds": list(DATASET_SPLIT_SEEDS[Split.VALIDATION]),
        "test_seeds": list(DATASET_SPLIT_SEEDS[Split.TEST]),
    }


def dataset_identity(manifest: dict[str, object]) -> str:
    """`dataset_identity`自身を除いたcanonical manifestのsha256を返す。"""
    logical = {
        name: value for name, value in manifest.items() if name != "dataset_identity"
    }
    return hashlib.sha256(canonical_json_text(logical).encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class GameManifestEntry:
    """1 hanchan分のdeterministic manifest record。measurementは含まない。"""

    seed: int
    split: Split
    row_count: int
    scores: tuple[int, int, int, int]
    ranks: tuple[int, int, int, int]

    def to_document(self) -> dict[str, object]:
        return {
            "seed": self.seed,
            "split": self.split.value,
            "row_count": self.row_count,
            "scores": list(self.scores),
            "ranks": list(self.ranks),
        }


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


def _require_locked_teacher_revision(provenance: dict) -> None:
    actual = provenance["lisjong_revision"]
    if actual != TEACHER_SOURCE_REVISION:
        raise _error(
            "dataset lisjong provenance revision does not match the locked "
            f"Offline Q teacher source revision: {actual!r} != "
            f"{TEACHER_SOURCE_REVISION!r}"
        )


class _HashingWriter:
    """staging fileへ書きながらbyte countとsha256を同時に確定する。"""

    __slots__ = ("_stream", "_hash", "_bytes")

    def __init__(self, path: Path) -> None:
        self._stream = path.open("xb")
        self._hash = hashlib.sha256()
        self._bytes = 0

    def write(self, payload: bytes) -> None:
        self._stream.write(payload)
        self._hash.update(payload)
        self._bytes += len(payload)

    def close(self) -> dict[str, object]:
        if not self._stream.closed:
            self._stream.close()
        return {"bytes": self._bytes, "sha256": self._hash.hexdigest()}


def _row_document(row: MacroTransitionRow) -> dict[str, object]:
    return {
        "seed": row.seed,
        "split": row.split.value,
        "round_ordinal": row.round_ordinal,
        "round_wind": row.round_wind,
        "hand_number": row.hand_number,
        "honba": row.honba,
        "actor_seat": row.actor_seat,
        "step_ordinal": row.step_ordinal,
        "decision_ordinal": row.decision_ordinal,
        "legal_action_count": row.legal_action_count,
        "behavior_action_index": row.behavior_action_index,
        "behavior_action_family": row.behavior_action_family,
        "reward": row.reward,
        "terminal": row.terminal,
        "next_step_ordinal": row.next_step_ordinal,
        "next_decision_ordinal": row.next_decision_ordinal,
    }


class OfflineQDatasetWriter:
    """streaming writer。全rowを同時にmemoryへ載せない。

    `finalize()`まではstaging directoryにしか書かず、成功時にrenameで公開する。
    """

    __slots__ = (
        "_destination",
        "_provenance",
        "_staging",
        "_rows",
        "_features",
        "_masks",
        "_next_features",
        "_next_masks",
        "_games",
        "_row_count",
        "_terminal_row_count",
        "_finalized",
    )

    def __init__(
        self,
        destination: str | Path,
        *,
        provenance: dict[str, str] | None = None,
    ) -> None:
        verify_contract_identity()
        destination = Path(destination)
        if destination.exists():
            raise FileExistsError("dataset destination already exists")
        destination.parent.mkdir(parents=True, exist_ok=True)
        self._destination = destination
        self._provenance = (
            _provenance_document() if provenance is None else dict(provenance)
        )
        if set(self._provenance) != _PROVENANCE_FIELDS:
            raise OfflineQArtifactError("provenance fields are invalid")
        if any(
            type(value) is not str or not value for value in self._provenance.values()
        ):
            raise OfflineQArtifactError("provenance values must be non-empty strings")
        _require_locked_teacher_revision(self._provenance)
        self._staging = Path(
            mkdtemp(prefix=f".{destination.name}-staging-", dir=destination.parent)
        )
        self._rows = _HashingWriter(self._staging / ROWS_FILENAME)
        self._features = _HashingWriter(self._staging / FEATURES_FILENAME)
        self._masks = _HashingWriter(self._staging / LEGAL_MASK_FILENAME)
        self._next_features = _HashingWriter(self._staging / NEXT_FEATURES_FILENAME)
        self._next_masks = _HashingWriter(self._staging / NEXT_LEGAL_MASK_FILENAME)
        self._games: list[GameManifestEntry] = []
        self._row_count = 0
        self._terminal_row_count = 0
        self._finalized = False

    def discard(self) -> None:
        """公開前のstaging状態を破棄する。finalize済みのdatasetへは触れない。"""
        if self._finalized:
            return
        self._finalized = True
        for writer in (
            self._rows,
            self._features,
            self._masks,
            self._next_features,
            self._next_masks,
        ):
            writer.close()
        rmtree(self._staging, ignore_errors=True)

    def add_game(
        self,
        *,
        seed: int,
        split: Split,
        scores: tuple[int, int, int, int],
        ranks: tuple[int, int, int, int],
        rows: Iterable[MacroTransitionRow],
    ) -> GameManifestEntry:
        """1 hanchan分のmacro-transition rowを生成順のまま追記する。

        rowはstreamのまま消費し、1 hanchan分をまとめてmemoryへ載せない。
        """
        if self._finalized:
            raise OfflineQArtifactError("writer has already been finalized")
        if type(seed) is not int:
            raise TypeError("seed must be an int")
        if split is not split_for_seed(seed):
            raise OfflineQArtifactError("game split does not match the locked protocol")
        if self._games and seed <= self._games[-1].seed:
            raise OfflineQArtifactError(
                "games must be written in ascending seed order without duplicates"
            )

        written = 0
        for row in rows:
            if not isinstance(row, MacroTransitionRow):
                raise TypeError("rows must contain only MacroTransitionRow values")
            if row.seed != seed or row.split is not split:
                raise OfflineQArtifactError("row identity does not match its game")
            self._rows.write(
                (
                    json.dumps(
                        _row_document(row),
                        ensure_ascii=False,
                        allow_nan=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    )
                    + "\n"
                ).encode("utf-8")
            )
            features = array("f", row.feature_values)
            if array("f").itemsize * len(features) != _FEATURE_ROW_BYTES:
                raise OfflineQArtifactError("feature row byte width drifted")
            self._features.write(features.tobytes())
            self._masks.write(bytes(row.legal_mask))
            if row.terminal:
                self._next_features.write(_ZERO_FEATURE_ROW)
                self._next_masks.write(_ZERO_MASK_ROW)
                self._terminal_row_count += 1
            else:
                next_features = array("f", row.next_feature_values)
                if array("f").itemsize * len(next_features) != _FEATURE_ROW_BYTES:
                    raise OfflineQArtifactError("next feature row byte width drifted")
                self._next_features.write(next_features.tobytes())
                self._next_masks.write(bytes(row.next_legal_mask))
            written += 1
            self._row_count += 1

        if written == 0:
            raise OfflineQArtifactError(f"seed {seed} produced no macro-transitions")
        entry = GameManifestEntry(
            seed=seed, split=split, row_count=written, scores=scores, ranks=ranks
        )
        self._games.append(entry)
        return entry

    def finalize(self) -> "LoadedOfflineQDataset":
        """manifestを確定して公開し、その場でstrict readbackを返す。"""
        if self._finalized:
            raise OfflineQArtifactError("writer has already been finalized")
        published = False
        try:
            files = {
                "rows": self._rows.close(),
                "features": self._features.close(),
                "legal_mask": self._masks.close(),
                "next_features": self._next_features.close(),
                "next_legal_mask": self._next_masks.close(),
            }
            if tuple(entry.seed for entry in self._games) != DATASET_ORDERED_SEEDS:
                raise OfflineQArtifactError(
                    "dataset must contain exactly the locked seed population"
                )
            games = self._games
            manifest: dict[str, object] = {
                "dataset_schema_version": DATASET_SCHEMA_VERSION,
                "protocol": _protocol_block(),
                "feature": feature_block(),
                "vocabulary": vocabulary_block(),
                "provenance": dict(self._provenance),
                "games": [entry.to_document() for entry in games],
                "totals": {
                    "game_count": len(games),
                    "row_count": self._row_count,
                    "terminal_row_count": self._terminal_row_count,
                },
                "files": files,
            }
            manifest["dataset_identity"] = dataset_identity(manifest)
            (self._staging / MANIFEST_FILENAME).write_text(
                canonical_json_text(manifest), encoding="utf-8", newline="\n"
            )
            self._staging.rename(self._destination)
            published = True
        finally:
            self._finalized = True
            if not published:
                for writer in (
                    self._rows,
                    self._features,
                    self._masks,
                    self._next_features,
                    self._next_masks,
                ):
                    writer.close()
                rmtree(self._staging, ignore_errors=True)
        return load_dataset(self._destination)


@dataclass(frozen=True, slots=True)
class OfflineQRowRecord:
    """readbackした1 macro-transitionのmetadata。dense featureはfile側に保持する。"""

    seed: int
    split: Split
    round_ordinal: int
    round_wind: str
    hand_number: int
    honba: int
    actor_seat: int
    step_ordinal: int
    decision_ordinal: int
    legal_action_count: int
    behavior_action_index: int
    behavior_action_family: str
    reward: float
    terminal: bool
    next_step_ordinal: int | None
    next_decision_ordinal: int | None

    @property
    def is_choice_row(self) -> bool:
        return self.legal_action_count >= 2


@dataclass(frozen=True, slots=True)
class LoadedOfflineQDataset:
    """検証済みOffline Q macro-transition dataset。"""

    path: Path
    manifest: dict
    rows: tuple[OfflineQRowRecord, ...]

    @property
    def identity(self) -> str:
        return self.manifest["dataset_identity"]

    @property
    def row_count(self) -> int:
        return len(self.rows)

    def split_indices(self, split: Split) -> tuple[int, ...]:
        """指定splitのrow index（dataset順）を返す。"""
        if not isinstance(split, Split):
            raise TypeError("split must be a Split")
        return tuple(index for index, row in enumerate(self.rows) if row.split is split)

    def feature_row(self, index: int) -> array:
        return self._read_row(FEATURES_FILENAME, _FEATURE_ROW_BYTES, index, "f")

    def legal_mask_row(self, index: int) -> tuple[bool, ...]:
        payload = self._read_row_bytes(LEGAL_MASK_FILENAME, _MASK_ROW_BYTES, index)
        return tuple(value == 1 for value in payload)

    def next_feature_row(self, index: int) -> array:
        """terminal rowではall-zero placeholderを返す（呼び出し側が`terminal`を見て使う）。"""
        return self._read_row(NEXT_FEATURES_FILENAME, _FEATURE_ROW_BYTES, index, "f")

    def next_legal_mask_row(self, index: int) -> tuple[bool, ...]:
        payload = self._read_row_bytes(NEXT_LEGAL_MASK_FILENAME, _MASK_ROW_BYTES, index)
        return tuple(value == 1 for value in payload)

    def _read_row_bytes(self, filename: str, row_bytes: int, index: int) -> bytes:
        if type(index) is not int or not 0 <= index < self.row_count:
            raise IndexError("row index is outside the dataset")
        offset = index * row_bytes
        with (self.path / filename).open("rb") as stream:
            stream.seek(offset)
            payload = stream.read(row_bytes)
        if len(payload) != row_bytes:
            raise _error(f"{filename} is shorter than the manifest row count")
        return payload

    def _read_row(self, filename: str, row_bytes: int, index: int, typecode: str):
        payload = self._read_row_bytes(filename, row_bytes, index)
        values = array(typecode)
        values.frombytes(payload)
        return values

    def count_non_finite_features(self) -> int:
        """全featureのnon-finite数を数える（0であることをhard invariantとする）。"""
        values = array("f")
        values.frombytes((self.path / FEATURES_FILENAME).read_bytes())
        if len(values) != self.row_count * FEATURE_DIMENSION:
            raise _error("feature element count does not match the manifest")
        return sum(1 for value in values if not isfinite(value))


def _iter_row_documents(payload: bytes) -> Iterator[dict]:
    text = payload.decode("utf-8")
    if text and not text.endswith("\n"):
        raise _error("rows.jsonl must end with a newline")
    for line in text.splitlines():
        try:
            document = json.loads(line)
        except json.JSONDecodeError as error:
            raise _error("rows.jsonl contains malformed JSON") from error
        yield _expect_object(document, _ROW_FIELDS, "row")


def _validate_manifest(manifest: object) -> dict:
    document = _expect_object(manifest, _MANIFEST_FIELDS, "manifest")
    if document["dataset_schema_version"] != DATASET_SCHEMA_VERSION:
        raise _error(
            f"unsupported dataset schema: {document['dataset_schema_version']!r}"
        )

    protocol = _expect_object(document["protocol"], _PROTOCOL_FIELDS, "protocol")
    if protocol != _protocol_block():
        raise _error("dataset protocol does not match the locked Offline Q protocol")

    feature = _expect_object(document["feature"], _FEATURE_FIELDS, "feature")
    if feature != feature_block():
        raise _error("dataset feature schema identity is not supported")

    vocabulary = _expect_object(
        document["vocabulary"], _VOCABULARY_FIELDS, "vocabulary"
    )
    if vocabulary != vocabulary_block():
        raise _error("dataset action vocabulary identity is not supported")

    provenance = _expect_object(
        document["provenance"], _PROVENANCE_FIELDS, "provenance"
    )
    for name, value in provenance.items():
        if type(value) is not str or not value:
            raise _error(f"provenance.{name} must be a non-empty string")
    _require_locked_teacher_revision(provenance)

    games = document["games"]
    if type(games) is not list or len(games) != DATASET_HANCHAN_COUNT:
        raise _error(f"games must contain exactly {DATASET_HANCHAN_COUNT} entries")
    seen_seeds: list[int] = []
    total_rows = 0
    for entry in games:
        game = _expect_object(entry, _GAME_FIELDS, "game")
        seed = _expect(game["seed"], int, "game.seed")
        split = split_for_seed(seed)
        if game["split"] != split.value:
            raise _error(f"game {seed} split does not match the locked protocol")
        row_count = _expect(game["row_count"], int, "game.row_count")
        if row_count <= 0:
            raise _error("game.row_count must be positive")
        for name in ("scores", "ranks"):
            values = _expect_int_list(game[name], f"game.{name}")
            if len(values) != 4:
                raise _error(f"game.{name} must contain exactly four values")
        if sorted(_expect_int_list(game["ranks"], "game.ranks")) != [1, 2, 3, 4]:
            raise _error("game.ranks must be a permutation of 1..4")
        seen_seeds.append(seed)
        total_rows += row_count
    if seen_seeds != list(DATASET_ORDERED_SEEDS):
        raise _error("games must be the locked seed population in ascending order")

    totals = _expect_object(document["totals"], _TOTALS_FIELDS, "totals")
    if totals["game_count"] != DATASET_HANCHAN_COUNT:
        raise _error("totals.game_count does not match the locked hanchan count")
    if _expect(totals["row_count"], int, "totals.row_count") != total_rows:
        raise _error("totals.row_count does not match the per-game row counts")
    terminal_row_count = _expect(
        totals["terminal_row_count"], int, "totals.terminal_row_count"
    )
    if not 0 <= terminal_row_count <= total_rows:
        raise _error("totals.terminal_row_count is out of range")

    files = _expect_object(document["files"], _FILE_NAMES, "files")
    for name, value in files.items():
        entry = _expect_object(value, _FILE_FIELDS, f"files.{name}")
        if _expect(entry["bytes"], int, f"files.{name}.bytes") < 0:
            raise _error(f"files.{name}.bytes must not be negative")
        _digest(entry["sha256"], f"files.{name}.sha256")

    identity = _digest(document["dataset_identity"], "dataset_identity")
    if identity != dataset_identity(document):
        raise _error("dataset_identity does not match the manifest content")
    return document


def load_dataset(path: str | Path) -> LoadedOfflineQDataset:
    """dataset artifactを読み、identity / digest / row整合をfail closedで検証する。"""
    verify_contract_identity()
    path = Path(path)
    if not path.is_dir():
        raise _error("dataset path is not a directory")
    if {item.name for item in path.iterdir()} != _ARTIFACT_FILENAMES:
        raise _error("dataset contains missing or extra files")

    manifest_text = (path / MANIFEST_FILENAME).read_text(encoding="utf-8")
    try:
        manifest = json.loads(manifest_text)
    except json.JSONDecodeError as error:
        raise _error("manifest is not valid JSON") from error
    document = _validate_manifest(manifest)
    if canonical_json_text(document) != manifest_text:
        raise _error("manifest bytes are not canonical JSON")

    payloads = {
        "rows": (path / ROWS_FILENAME).read_bytes(),
        "features": (path / FEATURES_FILENAME).read_bytes(),
        "legal_mask": (path / LEGAL_MASK_FILENAME).read_bytes(),
        "next_features": (path / NEXT_FEATURES_FILENAME).read_bytes(),
        "next_legal_mask": (path / NEXT_LEGAL_MASK_FILENAME).read_bytes(),
    }
    for name, payload in payloads.items():
        expected = document["files"][name]
        if len(payload) != expected["bytes"]:
            raise _error(f"{name} byte count differs from the manifest")
        if hashlib.sha256(payload).hexdigest() != expected["sha256"]:
            raise _error(f"{name} sha256 differs from the manifest")

    row_count = document["totals"]["row_count"]
    if len(payloads["features"]) != row_count * _FEATURE_ROW_BYTES:
        raise _error("features file size does not match row count x 8204 float32")
    if len(payloads["legal_mask"]) != row_count * _MASK_ROW_BYTES:
        raise _error("legal mask file size does not match row count x 802 uint8")
    if len(payloads["next_features"]) != row_count * _FEATURE_ROW_BYTES:
        raise _error("next_features file size does not match row count x 8204 float32")
    if len(payloads["next_legal_mask"]) != row_count * _MASK_ROW_BYTES:
        raise _error("next_legal_mask file size does not match row count x 802 uint8")

    expected_rows = {entry["seed"]: entry["row_count"] for entry in document["games"]}
    records: list[OfflineQRowRecord] = []
    per_game_counts: dict[int, int] = {seed: 0 for seed in expected_rows}
    previous_seed: int | None = None
    terminal_row_count = 0
    for index, row in enumerate(_iter_row_documents(payloads["rows"])):
        seed = _expect(row["seed"], int, "row.seed")
        split = split_for_seed(seed)
        if row["split"] != split.value:
            raise _error(f"row {index} split does not match the locked protocol")
        if previous_seed is not None and seed < previous_seed:
            raise _error("rows must be grouped by ascending seed")
        previous_seed = seed
        per_game_counts[seed] += 1

        for name in (
            "round_ordinal",
            "hand_number",
            "honba",
            "actor_seat",
            "step_ordinal",
            "decision_ordinal",
            "legal_action_count",
            "behavior_action_index",
        ):
            _expect(row[name], int, f"row.{name}")
        _expect(row["round_wind"], str, "row.round_wind")
        _expect(row["behavior_action_family"], str, "row.behavior_action_family")
        _expect(row["terminal"], bool, "row.terminal")
        reward = row["reward"]
        if type(reward) not in (int, float) or not isfinite(float(reward)):
            raise _error(f"row {index} reward must be a finite number")

        behavior_index = row["behavior_action_index"]
        if not 0 <= behavior_index < VOCABULARY_SIZE:
            raise _error(f"row {index} behavior action index is outside the vocabulary")
        if row["behavior_action_family"] != action_family(behavior_index):
            raise _error(f"row {index} behavior action family is inconsistent")

        mask_start = index * _MASK_ROW_BYTES
        mask = payloads["legal_mask"][mask_start : mask_start + _MASK_ROW_BYTES]
        if any(value not in (0, 1) for value in mask):
            raise _error(f"row {index} legal mask is not a 0/1 mask")
        legal_count = mask.count(1)
        if legal_count < 2:
            raise _error(f"row {index} legal mask must have at least two actions")
        if legal_count != row["legal_action_count"]:
            raise _error(f"row {index} legal_action_count differs from its mask")
        if mask[behavior_index] != 1:
            raise _error(f"row {index} behavior action is not legal in its own mask")

        terminal = row["terminal"]
        next_step = row["next_step_ordinal"]
        next_decision = row["next_decision_ordinal"]
        if terminal:
            if next_step is not None or next_decision is not None:
                raise _error(
                    f"row {index} is terminal but carries a next decision identity"
                )
            terminal_row_count += 1
        else:
            if type(next_step) is not int or type(next_decision) is not int:
                raise _error(
                    f"row {index} is nonterminal but is missing its next decision identity"
                )
            if next_decision <= row["decision_ordinal"]:
                raise _error(f"row {index} next_decision_ordinal is not strictly later")
            next_mask = payloads["next_legal_mask"][
                mask_start : mask_start + _MASK_ROW_BYTES
            ]
            if any(value not in (0, 1) for value in next_mask):
                raise _error(f"row {index} next legal mask is not a 0/1 mask")
            if next_mask.count(1) < 2:
                raise _error(
                    f"row {index} next legal mask must have at least two actions"
                )

        records.append(
            OfflineQRowRecord(
                seed=seed,
                split=split,
                round_ordinal=row["round_ordinal"],
                round_wind=row["round_wind"],
                hand_number=row["hand_number"],
                honba=row["honba"],
                actor_seat=row["actor_seat"],
                step_ordinal=row["step_ordinal"],
                decision_ordinal=row["decision_ordinal"],
                legal_action_count=legal_count,
                behavior_action_index=behavior_index,
                behavior_action_family=row["behavior_action_family"],
                reward=float(reward),
                terminal=terminal,
                next_step_ordinal=next_step,
                next_decision_ordinal=next_decision,
            )
        )

    if len(records) != row_count:
        raise _error("rows.jsonl row count differs from the manifest")
    if per_game_counts != expected_rows:
        raise _error("per-game row counts differ from the manifest")
    if terminal_row_count != document["totals"]["terminal_row_count"]:
        raise _error("totals.terminal_row_count differs from the actual rows")

    return LoadedOfflineQDataset(path=path, manifest=document, rows=tuple(records))


__all__ = [
    "DATASET_SCHEMA_VERSION",
    "FEATURES_FILENAME",
    "LEGAL_MASK_FILENAME",
    "MANIFEST_FILENAME",
    "NEXT_FEATURES_FILENAME",
    "NEXT_LEGAL_MASK_FILENAME",
    "ROWS_FILENAME",
    "GameManifestEntry",
    "LoadedOfflineQDataset",
    "OfflineQDatasetWriter",
    "OfflineQRowRecord",
    "dataset_identity",
    "feature_block",
    "load_dataset",
    "vocabulary_block",
]
