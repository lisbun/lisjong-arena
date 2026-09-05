"""Replacement offline TEST artifact + checkpoint-bound evaluation (Issue #140).

PR #141 merge後のfollow-upで、historical dataset / BC / Q checkpointがいずれも
ephemeral実行環境と共に失われたことがartifact availability auditで確定した。
candidate pairはrebuildされ、rebuiltなBC / Q checkpointはhistorical
`271..276` TEST結果のsubjectではない。したがってこのmoduleは、rebuilt
candidate pairに対するfresh one-shot offline diagnostic populationを
purpose-specificなTEST-only artifactとして所有する。

```text
locked replacement TEST seeds 354..359   (yakuhai-call x4 / 4p-red-half)
        |
        v
ReplacementTestWriter
    arena-learned-policy-offlineq-replacement-test-v1
    original training datasetへappendしない / 別のartifact identityを持つ
        |
        v
strict-loaded BC / Q checkpoint
    + Q checkpointへidentity-boundされたsupported_indices
        |
        v
BC / Q one-shot diagnostics
```

**support setの正本**: replacement TESTのsupport setをTEST rowから計算し直さ
ない。またTRAIN `245..264`をregenしてsupportを再計算することもしない。
source of truthはQ checkpointへidentity-boundされた`supported_indices` /
`supported_indices_digest`である。これによりreplacement TESTは、実際に
servingされるQ hybridと同一のsupport boundaryを評価する。

`exposure_evaluation.evaluate_q_test()`はoriginal TRAIN tensorsから
support maskを再構成する設計なので、このpathでは使用しない。ここでは
checkpoint由来のsupport maskを直接使い、評価時にTRAIN rowsを要求しない。

このmoduleはgeneric arbitrary-seed evaluation frameworkではない。locked
population `354..359`だけをfail closedで受け付ける。
"""

import hashlib
import json
from collections.abc import Iterable
from dataclasses import dataclass
from math import isfinite
from pathlib import Path
from shutil import rmtree
from tempfile import mkdtemp

from lisjong_arena._artifact_io import canonical_json_text

from .artifact import (
    FEATURES_FILENAME,
    LEGAL_MASK_FILENAME,
    MANIFEST_FILENAME,
    NEXT_FEATURES_FILENAME,
    NEXT_LEGAL_MASK_FILENAME,
    PROVENANCE_FIELDS,
    ROWS_FILENAME,
    MacroTransitionFileWriters,
    OfflineQRowRecord,
    feature_block,
    provenance_document,
    read_validated_rows,
    require_locked_teacher_revision,
    verify_row_payloads,
    vocabulary_block,
)
from .errors import OfflineQArtifactError
from .model import MacroTransitionRow
from .protocol import (
    FEATURE_DIMENSION,
    GAME_MODE,
    PROTOCOL_ID,
    REPLACEMENT_TEST_HANCHAN_COUNT,
    REPLACEMENT_TEST_PURPOSE,
    REPLACEMENT_TEST_SEEDS,
    TEACHER_IDENTITY,
    TEACHER_POLICY_CLASS,
    TEACHER_POPULATION,
    TEACHER_SOURCE_REVISION,
    VOCABULARY_SIZE,
    Split,
    require_replacement_test_seed,
    verify_contract_identity,
)

REPLACEMENT_TEST_SCHEMA_VERSION = "arena-learned-policy-offlineq-replacement-test-v1"
TRANSITION_SCHEMA = "offlineq-macro-transition-v1"
"""row payload layoutはmacro-transition datasetと同一contractである。
manifest schemaとartifact identityだけが別物である。"""

_ARTIFACT_FILENAMES = {
    MANIFEST_FILENAME,
    ROWS_FILENAME,
    FEATURES_FILENAME,
    LEGAL_MASK_FILENAME,
    NEXT_FEATURES_FILENAME,
    NEXT_LEGAL_MASK_FILENAME,
}

_MANIFEST_FIELDS = {
    "replacement_test_schema_version",
    "artifact_identity",
    "purpose",
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
    "purpose",
    "teacher_identity",
    "teacher_policy_class",
    "teacher_population",
    "teacher_source_revision",
    "game_mode",
    "replacement_test_seeds",
    "transition_schema",
    "split_unit",
}
_GAME_FIELDS = {"seed", "row_count", "scores", "ranks"}
_TOTALS_FIELDS = {
    "game_count",
    "row_count",
    "terminal_row_count",
    "nonterminal_row_count",
}
_FILE_NAMES = {"rows", "features", "legal_mask", "next_features", "next_legal_mask"}
_FILE_FIELDS = {"bytes", "sha256"}


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


def _replacement_test_split(seed: int) -> Split:
    """replacement TEST populationはTEST-onlyであり、他のsplitを持たない。"""
    require_replacement_test_seed(seed)
    return Split.TEST


def _protocol_block() -> dict[str, object]:
    return {
        "protocol_id": PROTOCOL_ID,
        "purpose": REPLACEMENT_TEST_PURPOSE,
        "teacher_identity": TEACHER_IDENTITY,
        "teacher_policy_class": TEACHER_POLICY_CLASS,
        "teacher_population": TEACHER_POPULATION,
        "teacher_source_revision": TEACHER_SOURCE_REVISION,
        "game_mode": GAME_MODE,
        "replacement_test_seeds": list(REPLACEMENT_TEST_SEEDS),
        "transition_schema": TRANSITION_SCHEMA,
        "split_unit": "whole_hanchan",
    }


def artifact_identity(manifest: dict[str, object]) -> str:
    """`artifact_identity`自身を除いたcanonical manifestのsha256を返す。"""
    logical = {
        name: value for name, value in manifest.items() if name != "artifact_identity"
    }
    return hashlib.sha256(canonical_json_text(logical).encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class ReplacementTestGameEntry:
    """1 hanchan分のdeterministic manifest record。"""

    seed: int
    row_count: int
    scores: tuple[int, int, int, int]
    ranks: tuple[int, int, int, int]

    def to_document(self) -> dict[str, object]:
        return {
            "seed": self.seed,
            "row_count": self.row_count,
            "scores": list(self.scores),
            "ranks": list(self.ranks),
        }


class ReplacementTestWriter:
    """write-onceなreplacement TEST artifact writer。

    original training datasetへappendせず、独立したartifact identityを持つ。
    `finalize()`まではstaging directoryにしか書かず、成功時にrenameで公開する。
    """

    __slots__ = (
        "_destination",
        "_provenance",
        "_staging",
        "_files",
        "_games",
        "_finalized",
    )

    def __init__(
        self, destination: str | Path, *, provenance: dict[str, str] | None = None
    ) -> None:
        verify_contract_identity()
        destination = Path(destination)
        if destination.exists():
            raise FileExistsError("replacement TEST destination already exists")
        destination.parent.mkdir(parents=True, exist_ok=True)
        self._destination = destination
        self._provenance = (
            provenance_document() if provenance is None else dict(provenance)
        )
        if set(self._provenance) != PROVENANCE_FIELDS:
            raise OfflineQArtifactError("provenance fields are invalid")
        if any(
            type(value) is not str or not value for value in self._provenance.values()
        ):
            raise OfflineQArtifactError("provenance values must be non-empty strings")
        require_locked_teacher_revision(self._provenance)
        self._staging = Path(
            mkdtemp(prefix=f".{destination.name}-staging-", dir=destination.parent)
        )
        self._files = MacroTransitionFileWriters(self._staging)
        self._games: list[ReplacementTestGameEntry] = []
        self._finalized = False

    def discard(self) -> None:
        if self._finalized:
            return
        self._finalized = True
        self._files.close()
        rmtree(self._staging, ignore_errors=True)

    def add_game(
        self,
        *,
        seed: int,
        scores: tuple[int, int, int, int],
        ranks: tuple[int, int, int, int],
        rows: Iterable[MacroTransitionRow],
    ) -> ReplacementTestGameEntry:
        if self._finalized:
            raise OfflineQArtifactError("writer has already been finalized")
        require_replacement_test_seed(seed)
        if self._games and seed <= self._games[-1].seed:
            raise OfflineQArtifactError(
                "games must be written in ascending seed order without duplicates"
            )
        before = self._files.row_count
        for row in rows:
            if not isinstance(row, MacroTransitionRow):
                raise TypeError("rows must contain only MacroTransitionRow values")
            if row.seed != seed or row.split is not Split.TEST:
                raise OfflineQArtifactError("row identity does not match its game")
            self._files.write_row(row)
        written = self._files.row_count - before
        if written == 0:
            raise OfflineQArtifactError(f"seed {seed} produced no macro-transitions")
        entry = ReplacementTestGameEntry(
            seed=seed, row_count=written, scores=scores, ranks=ranks
        )
        self._games.append(entry)
        return entry

    def finalize(self) -> "LoadedReplacementTest":
        if self._finalized:
            raise OfflineQArtifactError("writer has already been finalized")
        published = False
        try:
            files = self._files.close()
            if tuple(entry.seed for entry in self._games) != REPLACEMENT_TEST_SEEDS:
                raise OfflineQArtifactError(
                    "replacement TEST must contain exactly the locked seed population"
                )
            row_count = self._files.row_count
            terminal_row_count = self._files.terminal_row_count
            manifest: dict[str, object] = {
                "replacement_test_schema_version": REPLACEMENT_TEST_SCHEMA_VERSION,
                "purpose": REPLACEMENT_TEST_PURPOSE,
                "protocol": _protocol_block(),
                "feature": feature_block(),
                "vocabulary": vocabulary_block(),
                "provenance": dict(self._provenance),
                "games": [entry.to_document() for entry in self._games],
                "totals": {
                    "game_count": len(self._games),
                    "row_count": row_count,
                    "terminal_row_count": terminal_row_count,
                    "nonterminal_row_count": row_count - terminal_row_count,
                },
                "files": files,
            }
            manifest["artifact_identity"] = artifact_identity(manifest)
            (self._staging / MANIFEST_FILENAME).write_text(
                canonical_json_text(manifest), encoding="utf-8", newline="\n"
            )
            self._staging.rename(self._destination)
            published = True
        finally:
            self._finalized = True
            if not published:
                self._files.close()
                rmtree(self._staging, ignore_errors=True)
        return load_replacement_test(self._destination)


@dataclass(frozen=True, slots=True)
class LoadedReplacementTest:
    """検証済みreplacement TEST artifact。"""

    path: Path
    manifest: dict
    rows: tuple[OfflineQRowRecord, ...]

    @property
    def identity(self) -> str:
        return self.manifest["artifact_identity"]

    @property
    def row_count(self) -> int:
        return len(self.rows)

    @property
    def terminal_row_count(self) -> int:
        return self.manifest["totals"]["terminal_row_count"]

    @property
    def nonterminal_row_count(self) -> int:
        return self.manifest["totals"]["nonterminal_row_count"]

    @property
    def hanchan_count(self) -> int:
        return self.manifest["totals"]["game_count"]

    def feature_bytes(self) -> bytes:
        return (self.path / FEATURES_FILENAME).read_bytes()

    def legal_mask_bytes(self) -> bytes:
        return (self.path / LEGAL_MASK_FILENAME).read_bytes()

    def next_feature_bytes(self) -> bytes:
        return (self.path / NEXT_FEATURES_FILENAME).read_bytes()

    def next_legal_mask_bytes(self) -> bytes:
        return (self.path / NEXT_LEGAL_MASK_FILENAME).read_bytes()

    def count_non_finite_features(self) -> int:
        from array import array

        values = array("f")
        values.frombytes(self.feature_bytes())
        if len(values) != self.row_count * FEATURE_DIMENSION:
            raise _error("feature element count does not match the manifest")
        return sum(1 for value in values if not isfinite(value))


def _validate_manifest(manifest: object) -> dict:
    document = _expect_object(manifest, _MANIFEST_FIELDS, "manifest")
    if document["replacement_test_schema_version"] != REPLACEMENT_TEST_SCHEMA_VERSION:
        raise _error("unsupported replacement TEST schema version")
    if document["purpose"] != REPLACEMENT_TEST_PURPOSE:
        raise _error("replacement TEST purpose is not the locked one")

    protocol = _expect_object(document["protocol"], _PROTOCOL_FIELDS, "protocol")
    if protocol != _protocol_block():
        raise _error(
            "replacement TEST protocol does not match the locked Offline Q protocol"
        )
    if _expect_object(document["feature"], set(feature_block()), "feature") != (
        feature_block()
    ):
        raise _error("replacement TEST feature schema identity is not supported")
    if (
        _expect_object(document["vocabulary"], set(vocabulary_block()), "vocabulary")
        != vocabulary_block()
    ):
        raise _error("replacement TEST action vocabulary identity is not supported")

    provenance = _expect_object(document["provenance"], PROVENANCE_FIELDS, "provenance")
    for name, value in provenance.items():
        if type(value) is not str or not value:
            raise _error(f"provenance.{name} must be a non-empty string")
    require_locked_teacher_revision(provenance)

    games = document["games"]
    if type(games) is not list or len(games) != REPLACEMENT_TEST_HANCHAN_COUNT:
        raise _error(
            f"games must contain exactly {REPLACEMENT_TEST_HANCHAN_COUNT} entries"
        )
    seen_seeds: list[int] = []
    total_rows = 0
    for entry in games:
        game = _expect_object(entry, _GAME_FIELDS, "game")
        seed = _expect(game["seed"], int, "game.seed")
        require_replacement_test_seed(seed)
        row_count = _expect(game["row_count"], int, "game.row_count")
        if row_count <= 0:
            raise _error("game.row_count must be positive")
        for name in ("scores", "ranks"):
            values = game[name]
            if type(values) is not list or any(
                type(item) is not int for item in values
            ):
                raise _error(f"game.{name} must be an array of integers")
            if len(values) != 4:
                raise _error(f"game.{name} must contain exactly four values")
        if sorted(game["ranks"]) != [1, 2, 3, 4]:
            raise _error("game.ranks must be a permutation of 1..4")
        seen_seeds.append(seed)
        total_rows += row_count
    if seen_seeds != list(REPLACEMENT_TEST_SEEDS):
        raise _error("games must be the locked seed population in ascending order")

    totals = _expect_object(document["totals"], _TOTALS_FIELDS, "totals")
    if totals["game_count"] != REPLACEMENT_TEST_HANCHAN_COUNT:
        raise _error("totals.game_count does not match the locked hanchan count")
    if _expect(totals["row_count"], int, "totals.row_count") != total_rows:
        raise _error("totals.row_count does not match the per-game row counts")
    terminal = _expect(totals["terminal_row_count"], int, "totals.terminal_row_count")
    nonterminal = _expect(
        totals["nonterminal_row_count"], int, "totals.nonterminal_row_count"
    )
    if not 0 <= terminal <= total_rows or terminal + nonterminal != total_rows:
        raise _error("totals terminal / nonterminal counts are inconsistent")

    files = _expect_object(document["files"], _FILE_NAMES, "files")
    for name, value in files.items():
        entry = _expect_object(value, _FILE_FIELDS, f"files.{name}")
        if _expect(entry["bytes"], int, f"files.{name}.bytes") < 0:
            raise _error(f"files.{name}.bytes must not be negative")
        digest = entry["sha256"]
        if type(digest) is not str or len(digest) != 64:
            raise _error(f"files.{name}.sha256 must be a sha256 digest")

    identity = document["artifact_identity"]
    if type(identity) is not str or identity != artifact_identity(document):
        raise _error("artifact_identity does not match the manifest content")
    return document


def load_replacement_test(path: str | Path) -> LoadedReplacementTest:
    """replacement TEST artifactを読み、identity / digest / row整合をfail closedで検証する。"""
    verify_contract_identity()
    path = Path(path)
    if not path.is_dir():
        raise _error("replacement TEST path is not a directory")
    if {item.name for item in path.iterdir()} != _ARTIFACT_FILENAMES:
        raise _error("replacement TEST contains missing or extra files")

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
    row_count = document["totals"]["row_count"]
    verify_row_payloads(payloads, document["files"], row_count)
    records = read_validated_rows(payloads, split_resolver=_replacement_test_split)

    expected_rows = {entry["seed"]: entry["row_count"] for entry in document["games"]}
    per_game_counts: dict[int, int] = {seed: 0 for seed in expected_rows}
    for record in records:
        per_game_counts[record.seed] += 1
    if len(records) != row_count:
        raise _error("rows.jsonl row count differs from the manifest")
    if per_game_counts != expected_rows:
        raise _error("per-game row counts differ from the manifest")
    if (
        sum(1 for record in records if record.terminal)
        != (document["totals"]["terminal_row_count"])
    ):
        raise _error("totals.terminal_row_count differs from the actual rows")

    return LoadedReplacementTest(path=path, manifest=document, rows=records)


# --- Checkpoint-bound evaluation -----------------------------------------


@dataclass(frozen=True, slots=True)
class ReplacementTestTensors:
    """replacement TEST artifactのCPU tensor。TRAIN rowsを一切必要としない。"""

    features: object
    legal_mask: object
    behavior_action_index: object
    reward: object
    terminal: object
    next_features: object
    next_legal_mask: object
    row_count: int


def load_replacement_test_tensors(
    artifact: LoadedReplacementTest,
) -> ReplacementTestTensors:
    """replacement TEST artifactをCPU tensorへ読み出す。"""
    import torch

    verify_contract_identity()
    if not isinstance(artifact, LoadedReplacementTest):
        raise TypeError("artifact must be a LoadedReplacementTest")

    row_count = artifact.row_count
    features = torch.frombuffer(
        bytearray(artifact.feature_bytes()), dtype=torch.float32
    ).reshape(row_count, FEATURE_DIMENSION)
    if not bool(torch.isfinite(features).all()):
        raise _error("replacement TEST features contain non-finite values")
    next_features = torch.frombuffer(
        bytearray(artifact.next_feature_bytes()), dtype=torch.float32
    ).reshape(row_count, FEATURE_DIMENSION)
    if not bool(torch.isfinite(next_features).all()):
        raise _error("replacement TEST next features contain non-finite values")
    legal_mask = (
        torch.frombuffer(bytearray(artifact.legal_mask_bytes()), dtype=torch.uint8)
        .reshape(row_count, VOCABULARY_SIZE)
        .bool()
    )
    next_legal_mask = (
        torch.frombuffer(bytearray(artifact.next_legal_mask_bytes()), dtype=torch.uint8)
        .reshape(row_count, VOCABULARY_SIZE)
        .bool()
    )
    behavior_action_index = torch.tensor(
        [row.behavior_action_index for row in artifact.rows], dtype=torch.long
    )
    reward = torch.tensor([row.reward for row in artifact.rows], dtype=torch.float32)
    terminal = torch.tensor([row.terminal for row in artifact.rows], dtype=torch.bool)

    if not bool(legal_mask.gather(1, behavior_action_index.unsqueeze(1)).all()):
        raise _error("a behavior action is not legal in its own mask")
    if not bool((next_legal_mask[~terminal].sum(dim=1) >= 2).all()):
        raise _error("a nonterminal row has fewer than 2 next actions")

    return ReplacementTestTensors(
        features=features.contiguous(),
        legal_mask=legal_mask.contiguous(),
        behavior_action_index=behavior_action_index,
        reward=reward,
        terminal=terminal,
        next_features=next_features.contiguous(),
        next_legal_mask=next_legal_mask.contiguous(),
        row_count=row_count,
    )


def support_mask_from_checkpoint(supported_indices):
    """checkpointへidentity-boundされた`supported_indices`をmaskへ変換する。

    TEST rowからsupport setを再計算せず、TRAIN tensorsも要求しない。実際に
    servingされるQ hybridと同一のsupport boundaryを評価するための正本である。
    """
    import torch

    indices = sorted(supported_indices)
    if not indices:
        raise _error("checkpoint supported_indices must not be empty")
    if any(
        type(index) is not int or not 0 <= index < VOCABULARY_SIZE for index in indices
    ):
        raise _error("checkpoint supported_indices contains an invalid index")
    mask = torch.zeros(VOCABULARY_SIZE, dtype=torch.bool)
    mask[torch.tensor(indices, dtype=torch.long)] = True
    return mask


def support_complete_flags(tensors: ReplacementTestTensors, support_mask):
    """各rowのcurrent legal actionsがすべてTRAIN-supportedかを返す。"""
    return ~(tensors.legal_mask & ~support_mask.unsqueeze(0)).any(dim=1)


def count_unsupported_bootstrap(tensors: ReplacementTestTensors, support_mask) -> int:
    """next legal actionにTRAIN-unsupportedを含むnonterminal rowを数える。

    `compute_td_targets()`はこの条件でfail closedするため、正常なreplacement
    TESTではこの数は必ず0になる。hard validity gateとして明示的に数える。
    """
    unsupported = (tensors.next_legal_mask & ~support_mask.unsqueeze(0)).any(dim=1)
    return int((unsupported & ~tensors.terminal).sum())


__all__ = [
    "REPLACEMENT_TEST_SCHEMA_VERSION",
    "TRANSITION_SCHEMA",
    "LoadedReplacementTest",
    "ReplacementTestGameEntry",
    "ReplacementTestTensors",
    "ReplacementTestWriter",
    "artifact_identity",
    "count_unsupported_bootstrap",
    "load_replacement_test",
    "load_replacement_test_tensors",
    "support_complete_flags",
    "support_mask_from_checkpoint",
]
