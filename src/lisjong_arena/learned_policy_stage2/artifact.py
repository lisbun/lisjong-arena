"""Versioned Stage 2 dataset artifact: write, digest, and strict readback.

1 dataset = 1 immutable directoryとし、既存pathを上書きしない。

```text
<dataset>/
    manifest.json      canonical JSON identity / protocol / provenance / digests
    rows.jsonl         1行 = 1 decisionのplayer-safe metadata
    features.f32       N x 8204 little-endian float32 (row-major)
    legal_mask.u8      N x 802 uint8 (0 / 1) の fixed-size legal mask
```

dense featureとfixed legal maskをJSONへ展開しないのは容量のためだけであり、
契約は変わらない。両fileはrow-major fixed strideで、`manifest.json`が
dimension、row count、byte count、sha256を保持する。readbackはこれらの
すべてを照合し、1つでも合わなければfail closedする。

このmoduleはgeneric dataset / artifact frameworkではない。Stage 2 experiment
1本のためのformatだけを持ち、任意schemaのregistry、database、code / factory
のserializeは提供しない。
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

from .errors import Stage2ArtifactError
from .model import Stage2DecisionRow
from .protocol import (
    FEATURE_DIMENSION,
    GAME_MODE,
    HANCHAN_COUNT,
    LOCKED_FEATURE_SCHEMA_FINGERPRINT,
    LOCKED_FEATURE_SEMANTICS_ID,
    LOCKED_TENSOR_DTYPE,
    LOCKED_TENSOR_SCHEMA_VERSION,
    LOCKED_VOCABULARY_FINGERPRINT,
    LOCKED_VOCABULARY_VERSION,
    ORDERED_SEEDS,
    PROTOCOL_ID,
    SPLIT_SEEDS,
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

DATASET_SCHEMA_VERSION = "arena-learned-policy-stage2-dataset-v1"
MANIFEST_FILENAME = "manifest.json"
ROWS_FILENAME = "rows.jsonl"
FEATURES_FILENAME = "features.f32"
LEGAL_MASK_FILENAME = "legal_mask.u8"

_FEATURE_ROW_BYTES = FEATURE_DIMENSION * 4
_MASK_ROW_BYTES = VOCABULARY_SIZE

_ROW_FIELDS = {
    "seed",
    "split",
    "step_ordinal",
    "decision_ordinal",
    "round_ordinal",
    "round_wind",
    "hand_number",
    "honba",
    "actor_seat",
    "legal_action_count",
    "teacher_action_index",
    "teacher_action_family",
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
_GAME_FIELDS = {
    "seed",
    "split",
    "row_count",
    "step_count",
    "round_count",
    "scores",
    "ranks",
}
_TOTALS_FIELDS = {"game_count", "row_count"}
_FILE_FIELDS = {"bytes", "sha256"}
_FILE_NAMES = {"rows", "features", "legal_mask"}
_ARTIFACT_FILENAMES = {
    MANIFEST_FILENAME,
    ROWS_FILENAME,
    FEATURES_FILENAME,
    LEGAL_MASK_FILENAME,
}

if array("f").itemsize != 4:
    raise RuntimeError("float32 array itemsize must be 4 bytes")


def _error(message: str) -> Stage2ArtifactError:
    return Stage2ArtifactError(message)


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
        "ordered_seeds": list(ORDERED_SEEDS),
        "split_unit": "whole_hanchan",
        "train_seeds": list(SPLIT_SEEDS[Split.TRAIN]),
        "validation_seeds": list(SPLIT_SEEDS[Split.VALIDATION]),
        "test_seeds": list(SPLIT_SEEDS[Split.TEST]),
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
    step_count: int
    round_count: int
    scores: tuple[int, int, int, int]
    ranks: tuple[int, int, int, int]

    def to_document(self) -> dict[str, object]:
        return {
            "seed": self.seed,
            "split": self.split.value,
            "row_count": self.row_count,
            "step_count": self.step_count,
            "round_count": self.round_count,
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


def _row_document(row: Stage2DecisionRow) -> dict[str, object]:
    return {
        "seed": row.seed,
        "split": row.split.value,
        "step_ordinal": row.step_ordinal,
        "decision_ordinal": row.decision_ordinal,
        "round_ordinal": row.round_ordinal,
        "round_wind": row.round_wind,
        "hand_number": row.hand_number,
        "honba": row.honba,
        "actor_seat": row.actor_seat,
        "legal_action_count": row.legal_action_count,
        "teacher_action_index": row.teacher_action_index,
        "teacher_action_family": row.teacher_action_family,
    }


class Stage2DatasetWriter:
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
        "_games",
        "_row_count",
        "_finalized",
    )

    def __init__(
        self,
        destination: str | Path,
        *,
        provenance: dict[str, str] | None = None,
    ) -> None:
        """`provenance`未指定時は実行中のArena execution provenanceを実測する。

        明示指定はfixtureとtestのためだけの入口であり、実runでは使わない
        (実測経路はsource treeがdirtyな場合にfail closedする)。
        """
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
            raise Stage2ArtifactError("provenance fields are invalid")
        if any(
            type(value) is not str or not value for value in self._provenance.values()
        ):
            raise Stage2ArtifactError("provenance values must be non-empty strings")
        _require_locked_teacher_revision(self._provenance)
        self._staging = Path(
            mkdtemp(prefix=f".{destination.name}-staging-", dir=destination.parent)
        )
        self._rows = _HashingWriter(self._staging / ROWS_FILENAME)
        self._features = _HashingWriter(self._staging / FEATURES_FILENAME)
        self._masks = _HashingWriter(self._staging / LEGAL_MASK_FILENAME)
        self._games: list[GameManifestEntry] = []
        self._row_count = 0
        self._finalized = False

    def discard(self) -> None:
        """公開前のstaging状態を破棄する。finalize済みのdatasetへは触れない。"""
        if self._finalized:
            return
        self._finalized = True
        for writer in (self._rows, self._features, self._masks):
            writer.close()
        rmtree(self._staging, ignore_errors=True)

    def add_game(
        self,
        *,
        seed: int,
        split: Split,
        step_count: int,
        scores: tuple[int, int, int, int],
        ranks: tuple[int, int, int, int],
        rows: Iterable[Stage2DecisionRow],
    ) -> GameManifestEntry:
        """1 hanchan分のrowを生成順のまま追記し、そのmanifest entryを返す。

        rowはstreamのまま消費し、1 hanchan分をまとめてmemoryへ載せない。
        """
        if self._finalized:
            raise Stage2ArtifactError("writer has already been finalized")
        if type(seed) is not int:
            raise TypeError("seed must be an int")
        if split is not split_for_seed(seed):
            raise Stage2ArtifactError("game split does not match the locked protocol")
        if self._games and seed <= self._games[-1].seed:
            raise Stage2ArtifactError(
                "games must be written in ascending seed order without duplicates"
            )

        written = 0
        highest_round = -1
        for row in rows:
            if not isinstance(row, Stage2DecisionRow):
                raise TypeError("rows must contain only Stage2DecisionRow values")
            if row.seed != seed or row.split is not split:
                raise Stage2ArtifactError("row identity does not match its game")
            if row.decision_ordinal != written:
                raise Stage2ArtifactError(
                    "decision ordinals must be zero-based and contiguous per game"
                )
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
                raise Stage2ArtifactError("feature row byte width drifted")
            self._features.write(features.tobytes())
            self._masks.write(bytes(row.legal_mask))
            highest_round = max(highest_round, row.round_ordinal)
            written += 1
            self._row_count += 1

        if written == 0:
            raise Stage2ArtifactError(f"seed {seed} produced no decision rows")
        entry = GameManifestEntry(
            seed=seed,
            split=split,
            row_count=written,
            step_count=step_count,
            round_count=highest_round + 1,
            scores=scores,
            ranks=ranks,
        )
        self._games.append(entry)
        return entry

    def finalize(self) -> "LoadedStage2Dataset":
        """manifestを確定して公開し、その場でstrict readbackを返す。"""
        if self._finalized:
            raise Stage2ArtifactError("writer has already been finalized")
        published = False
        try:
            files = {
                "rows": self._rows.close(),
                "features": self._features.close(),
                "legal_mask": self._masks.close(),
            }
            if tuple(entry.seed for entry in self._games) != ORDERED_SEEDS:
                raise Stage2ArtifactError(
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
                for writer in (self._rows, self._features, self._masks):
                    writer.close()
                rmtree(self._staging, ignore_errors=True)
        return load_dataset(self._destination)


@dataclass(frozen=True, slots=True)
class Stage2RowRecord:
    """readbackした1 rowのmetadata。dense featureはfile側に保持する。"""

    seed: int
    split: Split
    step_ordinal: int
    decision_ordinal: int
    round_ordinal: int
    round_wind: str
    hand_number: int
    honba: int
    actor_seat: int
    legal_action_count: int
    teacher_action_index: int
    teacher_action_family: str

    @property
    def is_choice_row(self) -> bool:
        return self.legal_action_count >= 2


@dataclass(frozen=True, slots=True)
class LoadedStage2Dataset:
    """検証済みStage 2 dataset。"""

    path: Path
    manifest: dict
    rows: tuple[Stage2RowRecord, ...]

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

    def feature_bytes(self) -> bytes:
        return (self.path / FEATURES_FILENAME).read_bytes()

    def legal_mask_bytes(self) -> bytes:
        return (self.path / LEGAL_MASK_FILENAME).read_bytes()

    def feature_row(self, index: int) -> array:
        """1 rowのfeatureをfloat32 arrayとして読み出す。"""
        if type(index) is not int or not 0 <= index < self.row_count:
            raise IndexError("row index is outside the dataset")
        offset = index * _FEATURE_ROW_BYTES
        with (self.path / FEATURES_FILENAME).open("rb") as stream:
            stream.seek(offset)
            payload = stream.read(_FEATURE_ROW_BYTES)
        if len(payload) != _FEATURE_ROW_BYTES:
            raise _error("feature file is shorter than the manifest row count")
        values = array("f")
        values.frombytes(payload)
        return values

    def legal_mask_row(self, index: int) -> tuple[bool, ...]:
        """1 rowのfixed 802 legal maskを読み出す。"""
        if type(index) is not int or not 0 <= index < self.row_count:
            raise IndexError("row index is outside the dataset")
        offset = index * _MASK_ROW_BYTES
        with (self.path / LEGAL_MASK_FILENAME).open("rb") as stream:
            stream.seek(offset)
            payload = stream.read(_MASK_ROW_BYTES)
        if len(payload) != _MASK_ROW_BYTES:
            raise _error("legal mask file is shorter than the manifest row count")
        return tuple(value == 1 for value in payload)

    def count_non_finite_features(self) -> int:
        """全featureのnon-finite数を数える（0であることをhard invariantとする）。"""
        values = array("f")
        values.frombytes(self.feature_bytes())
        if len(values) != self.row_count * FEATURE_DIMENSION:
            raise _error("feature element count does not match the manifest")
        return sum(1 for value in values if not isfinite(value))


def _require_locked_teacher_revision(provenance: dict) -> None:
    """actual lisjong revisionがlocked teacher source revisionと一致することを要求する。

    protocolは`TEACHER_SOURCE_REVISION`を名乗り、provenanceは実際にinstallされた
    revisionを記録する。両者を照合しないと、同じaction vocabularyのまま
    `yakuhai-call`の実装だけが変わった別revisionで生成したdatasetが、protocol上は
    旧revisionを名乗ったまま成立してしまう。
    """
    actual = provenance["lisjong_revision"]
    if actual != TEACHER_SOURCE_REVISION:
        raise _error(
            "dataset lisjong provenance revision does not match the locked "
            f"Stage 2 teacher source revision: {actual!r} != "
            f"{TEACHER_SOURCE_REVISION!r}"
        )


def _validate_manifest(manifest: object) -> dict:
    document = _expect_object(manifest, _MANIFEST_FIELDS, "manifest")
    if document["dataset_schema_version"] != DATASET_SCHEMA_VERSION:
        raise _error(
            f"unsupported dataset schema: {document['dataset_schema_version']!r}"
        )

    protocol = _expect_object(document["protocol"], _PROTOCOL_FIELDS, "protocol")
    expected_protocol = _protocol_block()
    if protocol != expected_protocol:
        raise _error("dataset protocol does not match the locked Stage 2 protocol")

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
    if type(games) is not list or len(games) != HANCHAN_COUNT:
        raise _error(f"games must contain exactly {HANCHAN_COUNT} entries")
    seen_seeds: list[int] = []
    total_rows = 0
    for entry in games:
        game = _expect_object(entry, _GAME_FIELDS, "game")
        seed = _expect(game["seed"], int, "game.seed")
        split = split_for_seed(seed)
        if game["split"] != split.value:
            raise _error(f"game {seed} split does not match the locked protocol")
        for name in ("row_count", "step_count", "round_count"):
            value = _expect(game[name], int, f"game.{name}")
            if value <= 0:
                raise _error(f"game.{name} must be positive")
        for name in ("scores", "ranks"):
            values = _expect_int_list(game[name], f"game.{name}")
            if len(values) != 4:
                raise _error(f"game.{name} must contain exactly four values")
        if sorted(_expect_int_list(game["ranks"], "game.ranks")) != [1, 2, 3, 4]:
            raise _error("game.ranks must be a permutation of 1..4")
        seen_seeds.append(seed)
        total_rows += game["row_count"]
    if seen_seeds != list(ORDERED_SEEDS):
        raise _error("games must be the locked seed population in ascending order")

    totals = _expect_object(document["totals"], _TOTALS_FIELDS, "totals")
    if totals["game_count"] != HANCHAN_COUNT:
        raise _error("totals.game_count does not match the locked hanchan count")
    if _expect(totals["row_count"], int, "totals.row_count") != total_rows:
        raise _error("totals.row_count does not match the per-game row counts")

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


def load_dataset(path: str | Path) -> LoadedStage2Dataset:
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

    expected_rows = {entry["seed"]: entry["row_count"] for entry in document["games"]}
    records: list[Stage2RowRecord] = []
    per_game_counts: dict[int, int] = {seed: 0 for seed in expected_rows}
    previous_seed: int | None = None
    for index, row in enumerate(_iter_row_documents(payloads["rows"])):
        seed = _expect(row["seed"], int, "row.seed")
        split = split_for_seed(seed)
        if row["split"] != split.value:
            raise _error(f"row {index} split does not match the locked protocol")
        if previous_seed is not None and seed < previous_seed:
            raise _error("rows must be grouped by ascending seed")
        previous_seed = seed
        if row["decision_ordinal"] != per_game_counts[seed]:
            raise _error(
                f"row {index} decision ordinal is not contiguous within its game"
            )
        per_game_counts[seed] += 1

        for name in (
            "step_ordinal",
            "decision_ordinal",
            "round_ordinal",
            "hand_number",
            "honba",
            "actor_seat",
            "legal_action_count",
            "teacher_action_index",
        ):
            _expect(row[name], int, f"row.{name}")
        _expect(row["round_wind"], str, "row.round_wind")
        _expect(row["teacher_action_family"], str, "row.teacher_action_family")

        teacher_index = row["teacher_action_index"]
        if not 0 <= teacher_index < VOCABULARY_SIZE:
            raise _error(f"row {index} teacher action index is outside the vocabulary")
        if row["teacher_action_family"] != action_family(teacher_index):
            raise _error(f"row {index} teacher action family is inconsistent")

        mask_start = index * _MASK_ROW_BYTES
        mask = payloads["legal_mask"][mask_start : mask_start + _MASK_ROW_BYTES]
        if any(value not in (0, 1) for value in mask):
            raise _error(f"row {index} legal mask is not a 0/1 mask")
        legal_count = mask.count(1)
        if legal_count == 0:
            raise _error(f"row {index} legal mask has no legal action")
        if legal_count != row["legal_action_count"]:
            raise _error(f"row {index} legal_action_count differs from its mask")
        if mask[teacher_index] != 1:
            raise _error(f"row {index} teacher action is not legal in its own mask")

        records.append(
            Stage2RowRecord(
                seed=seed,
                split=split,
                step_ordinal=row["step_ordinal"],
                decision_ordinal=row["decision_ordinal"],
                round_ordinal=row["round_ordinal"],
                round_wind=row["round_wind"],
                hand_number=row["hand_number"],
                honba=row["honba"],
                actor_seat=row["actor_seat"],
                legal_action_count=legal_count,
                teacher_action_index=teacher_index,
                teacher_action_family=row["teacher_action_family"],
            )
        )

    if len(records) != row_count:
        raise _error("rows.jsonl row count differs from the manifest")
    if per_game_counts != expected_rows:
        raise _error("per-game row counts differ from the manifest")

    return LoadedStage2Dataset(path=path, manifest=document, rows=tuple(records))


__all__ = [
    "DATASET_SCHEMA_VERSION",
    "FEATURES_FILENAME",
    "LEGAL_MASK_FILENAME",
    "MANIFEST_FILENAME",
    "ROWS_FILENAME",
    "GameManifestEntry",
    "LoadedStage2Dataset",
    "Stage2DatasetWriter",
    "Stage2RowRecord",
    "dataset_identity",
    "feature_block",
    "load_dataset",
    "vocabulary_block",
]
