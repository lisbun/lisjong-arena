"""FiniteHorizon-teacher curriculum dataset artifact (Issue #165).

`#165`のteacher pairはexperiment-localなversioned dataset contractを持つ。
historical `arena-learned-policy-offlineq-dataset-v1`のsemanticsはteacher
parameter化せず、そのまま残す。

```text
arena-learned-policy-finite-horizon-curriculum-dataset-v1

<dataset>/
    manifest.json         canonical JSON identity / arm / teacher / provenance / digests
    rows.jsonl            1行 = 1 macro-transitionのplayer-safe metadata
    features.f32          N x 8204 little-endian float32
    legal_mask.u8         N x 802 uint8
    next_features.f32     N x 8204 (terminal rowはall-zero placeholder)
    next_legal_mask.u8    N x 802 (同上)
```

row payloadのbinary layoutは`#140` macro-transition contractと同一であり、
`MacroTransitionFileWriters` / `verify_row_payloads()` / `read_validated_rows()`
をそのまま再利用する。**manifest schemaとartifact identityだけが別物である。**

Arm Y / Arm Fはそれぞれ別pathのwrite-once artifactを持ち、manifestは

```text
arm identity / teacher identity / class / factory
source revisions
ordered seeds / split / game mode
feature identity / fingerprint（stored v1 rowとderived P1 row）
action vocabulary identity / fingerprint
transition semantics / reward semantics
game records / row count / terminal count
teacher action-family counts
file digests / dataset identity / runtime provenance
```

をbindする。strict readbackはこれらをlocked constantから再導出して照合する
ので、wrong arm、wrong teacher、corrupted payloadはいずれもfail closedする。

`LoadedFiniteHorizonCurriculumDataset`は`LoadedOfflineQDataset`を継承する。
readerとsplit membership accessorを複製せずに`load_split_tensors()`や
`build_support_gate_report()`をthin reuseするためであり、historical manifest
schemaの意味を変更するものではない（この artifact は独自のmanifest
schema / identityを持ち、`load_dataset()`では読めない）。
"""

import hashlib
import json
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from shutil import rmtree
from tempfile import mkdtemp

from lisjong_arena._artifact_io import canonical_json_text
from lisjong_arena.learned_policy_stage2.protocol import ACTION_FAMILY_NAMES

from .artifact import (
    FEATURES_FILENAME,
    LEGAL_MASK_FILENAME,
    MANIFEST_FILENAME,
    NEXT_FEATURES_FILENAME,
    NEXT_LEGAL_MASK_FILENAME,
    PROVENANCE_FIELDS,
    ROWS_FILENAME,
    LoadedOfflineQDataset,
    MacroTransitionFileWriters,
    feature_block,
    provenance_document,
    read_validated_rows,
    verify_row_payloads,
    vocabulary_block,
)
from .errors import OfflineQArtifactError
from .fh_curriculum import (
    DATASET_HANCHAN_COUNT,
    DATASET_ORDERED_SEEDS,
    FH_CURRICULUM_ID,
    PARENT_ISSUE,
    PREDECESSOR_ISSUES,
    SOURCE_ISSUE,
    CurriculumArm,
    arm_block,
    dataset_protocol_block,
    require_arm,
    require_dataset_seed,
    require_fresh_seed_plan,
    reward_semantics_block,
    split_for_seed,
    teacher_block,
    transition_semantics_block,
)
from .model import MacroTransitionRow
from .p1_features import p1_feature_block
from .protocol import Split, verify_contract_identity

CURRICULUM_DATASET_SCHEMA_VERSION = (
    "arena-learned-policy-finite-horizon-curriculum-dataset-v1"
)

DISCARD_ACTION_FAMILIES = ("discard",)
RIICHI_ACTION_FAMILIES = ("riichi",)
CALL_ACTION_FAMILIES = ("chi", "pon", "daiminkan", "ankan", "kakan")
WINNING_ACTION_FAMILIES = ("ron", "tsumo")

_DECLARED_FAMILY_GROUPS = (
    DISCARD_ACTION_FAMILIES,
    RIICHI_ACTION_FAMILIES,
    CALL_ACTION_FAMILIES,
    WINNING_ACTION_FAMILIES,
)
if any(
    name not in ACTION_FAMILY_NAMES
    for group in _DECLARED_FAMILY_GROUPS
    for name in group
):
    raise RuntimeError(
        "an Issue #165 teacher action-family group names a family that the "
        "locked action vocabulary does not define"
    )

_ARTIFACT_FILENAMES = {
    MANIFEST_FILENAME,
    ROWS_FILENAME,
    FEATURES_FILENAME,
    LEGAL_MASK_FILENAME,
    NEXT_FEATURES_FILENAME,
    NEXT_LEGAL_MASK_FILENAME,
}

_MANIFEST_FIELDS = {
    "dataset_schema_version",
    "dataset_identity",
    "experiment",
    "arm",
    "teacher",
    "protocol",
    "feature",
    "derived_feature",
    "vocabulary",
    "transition_semantics",
    "reward_semantics",
    "provenance",
    "games",
    "totals",
    "files",
}
_EXPERIMENT_FIELDS = {
    "experiment_id",
    "source_issue",
    "predecessor_issues",
    "parent_issue",
}
_GAME_FIELDS = {
    "seed",
    "split",
    "row_count",
    "terminal_row_count",
    "decision_count",
    "teacher_action_family_counts",
    "scores",
    "ranks",
}
_TOTALS_FIELDS = {
    "game_count",
    "row_count",
    "terminal_row_count",
    "nonterminal_row_count",
    "decision_count",
    "teacher_action_family_counts",
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


def experiment_block() -> dict[str, object]:
    return {
        "experiment_id": FH_CURRICULUM_ID,
        "source_issue": SOURCE_ISSUE,
        "predecessor_issues": list(PREDECESSOR_ISSUES),
        "parent_issue": PARENT_ISSUE,
    }


def require_family_counts(counts: object, context: str) -> dict[str, int]:
    """teacher action-family countsをlocked vocabulary familyへ固定する。"""
    if type(counts) is not dict:
        raise _error(f"{context} must be an object")
    for name, value in counts.items():
        if type(name) is not str or name not in ACTION_FAMILY_NAMES:
            raise _error(f"{context} names an unknown action family: {name!r}")
        if type(value) is not int or value < 0:
            raise _error(f"{context}.{name} must be a non-negative int")
    return {name: int(value) for name, value in counts.items()}


def _merge_family_counts(target: dict[str, int], counts: dict[str, int]) -> None:
    for name, value in counts.items():
        target[name] = target.get(name, 0) + value


def dataset_identity(manifest: dict[str, object]) -> str:
    """`dataset_identity`自身を除いたcanonical manifestのsha256を返す。"""
    logical = {
        name: value for name, value in manifest.items() if name != "dataset_identity"
    }
    return hashlib.sha256(canonical_json_text(logical).encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class CurriculumGameEntry:
    """1 hanchan分のdeterministic manifest record。measurementは含まない。"""

    seed: int
    split: Split
    row_count: int
    terminal_row_count: int
    decision_count: int
    teacher_action_family_counts: dict[str, int]
    scores: tuple[int, int, int, int]
    ranks: tuple[int, int, int, int]

    def to_document(self) -> dict[str, object]:
        return {
            "seed": self.seed,
            "split": self.split.value,
            "row_count": self.row_count,
            "terminal_row_count": self.terminal_row_count,
            "decision_count": self.decision_count,
            "teacher_action_family_counts": dict(
                sorted(self.teacher_action_family_counts.items())
            ),
            "scores": list(self.scores),
            "ranks": list(self.ranks),
        }


class FiniteHorizonCurriculumDatasetWriter:
    """1 armぶんのwrite-once curriculum dataset writer。

    `finalize()`まではstaging directoryにしか書かず、成功時にrenameで公開する。
    既存pathは上書きしない。
    """

    __slots__ = (
        "_arm",
        "_destination",
        "_provenance",
        "_staging",
        "_files",
        "_games",
        "_finalized",
    )

    def __init__(
        self,
        destination: str | Path,
        *,
        arm: CurriculumArm,
        provenance: dict[str, str] | None = None,
    ) -> None:
        verify_contract_identity()
        require_fresh_seed_plan()
        self._arm = require_arm(arm)
        destination = Path(destination)
        if destination.exists():
            raise FileExistsError("curriculum dataset destination already exists")
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
        self._staging = Path(
            mkdtemp(prefix=f".{destination.name}-staging-", dir=destination.parent)
        )
        self._files = MacroTransitionFileWriters(self._staging)
        self._games: list[CurriculumGameEntry] = []
        self._finalized = False

    @property
    def arm(self) -> CurriculumArm:
        return self._arm

    def discard(self) -> None:
        """公開前のstaging状態を破棄する。finalize済みのdatasetへは触れない。"""
        if self._finalized:
            return
        self._finalized = True
        self._files.close()
        rmtree(self._staging, ignore_errors=True)

    def add_game(
        self,
        *,
        seed: int,
        split: Split,
        scores: tuple[int, int, int, int],
        ranks: tuple[int, int, int, int],
        decision_count: int,
        teacher_action_family_counts: dict[str, int],
        rows: Iterable[MacroTransitionRow],
    ) -> CurriculumGameEntry:
        """1 hanchan分のmacro-transition rowを生成順のまま追記する。"""
        if self._finalized:
            raise OfflineQArtifactError("writer has already been finalized")
        require_dataset_seed(seed)
        if split is not split_for_seed(seed):
            raise OfflineQArtifactError("game split does not match the locked protocol")
        if self._games and seed <= self._games[-1].seed:
            raise OfflineQArtifactError(
                "games must be written in ascending seed order without duplicates"
            )
        if type(decision_count) is not int or decision_count <= 0:
            raise OfflineQArtifactError("decision_count must be a positive int")
        counts = require_family_counts(
            teacher_action_family_counts, "teacher_action_family_counts"
        )
        if sum(counts.values()) != decision_count:
            raise OfflineQArtifactError(
                "teacher action-family counts do not partition the executed "
                "teacher decisions"
            )

        before = self._files.row_count
        before_terminal = self._files.terminal_row_count
        for row in rows:
            if not isinstance(row, MacroTransitionRow):
                raise TypeError("rows must contain only MacroTransitionRow values")
            if row.seed != seed or row.split is not split:
                raise OfflineQArtifactError("row identity does not match its game")
            self._files.write_row(row)
        written = self._files.row_count - before
        if written == 0:
            raise OfflineQArtifactError(f"seed {seed} produced no macro-transitions")

        entry = CurriculumGameEntry(
            seed=seed,
            split=split,
            row_count=written,
            terminal_row_count=self._files.terminal_row_count - before_terminal,
            decision_count=decision_count,
            teacher_action_family_counts=counts,
            scores=scores,
            ranks=ranks,
        )
        self._games.append(entry)
        return entry

    def finalize(self) -> "LoadedFiniteHorizonCurriculumDataset":
        """manifestを確定して公開し、その場でstrict readbackを返す。"""
        if self._finalized:
            raise OfflineQArtifactError("writer has already been finalized")
        published = False
        try:
            files = self._files.close()
            if tuple(entry.seed for entry in self._games) != DATASET_ORDERED_SEEDS:
                raise OfflineQArtifactError(
                    "the dataset must contain exactly the locked seed population"
                )
            row_count = self._files.row_count
            terminal_row_count = self._files.terminal_row_count
            totals_counts: dict[str, int] = {}
            for entry in self._games:
                _merge_family_counts(totals_counts, entry.teacher_action_family_counts)
            manifest: dict[str, object] = {
                "dataset_schema_version": CURRICULUM_DATASET_SCHEMA_VERSION,
                "experiment": experiment_block(),
                "arm": arm_block(self._arm),
                "teacher": teacher_block(self._arm),
                "protocol": dataset_protocol_block(),
                "feature": feature_block(),
                "derived_feature": p1_feature_block(),
                "vocabulary": vocabulary_block(),
                "transition_semantics": transition_semantics_block(),
                "reward_semantics": reward_semantics_block(),
                "provenance": dict(self._provenance),
                "games": [entry.to_document() for entry in self._games],
                "totals": {
                    "game_count": len(self._games),
                    "row_count": row_count,
                    "terminal_row_count": terminal_row_count,
                    "nonterminal_row_count": row_count - terminal_row_count,
                    "decision_count": sum(
                        entry.decision_count for entry in self._games
                    ),
                    "teacher_action_family_counts": dict(sorted(totals_counts.items())),
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
                self._files.close()
                rmtree(self._staging, ignore_errors=True)
        return load_curriculum_dataset(self._destination, arm=self._arm)


@dataclass(frozen=True, slots=True)
class LoadedFiniteHorizonCurriculumDataset(LoadedOfflineQDataset):
    """strict readback済みの1 arm curriculum dataset。

    `arm`はdefaultを持たない。armはmanifestから再導出した値だけが入り、
    構築時に省略して黙ってArm Yになることはない。
    """

    arm: CurriculumArm

    @property
    def teacher_identity(self) -> str:
        return self.manifest["teacher"]["identity"]

    @property
    def hanchan_count(self) -> int:
        return self.manifest["totals"]["game_count"]

    @property
    def terminal_row_count(self) -> int:
        return self.manifest["totals"]["terminal_row_count"]

    @property
    def source_revisions(self) -> dict:
        return self.manifest["provenance"]

    def games(self) -> tuple[dict, ...]:
        return tuple(self.manifest["games"])


def _arm_from_document(document: dict) -> CurriculumArm:
    arm_value = document["arm"]
    if type(arm_value) is not dict or type(arm_value.get("arm")) is not str:
        raise _error("manifest arm block is invalid")
    for arm in CurriculumArm:
        if arm.value == arm_value["arm"]:
            return arm
    raise _error(f"unknown curriculum arm: {arm_value['arm']!r}")


def _validate_manifest(
    manifest: object, *, arm: CurriculumArm | None
) -> tuple[dict, CurriculumArm]:
    document = _expect_object(manifest, _MANIFEST_FIELDS, "manifest")
    if document["dataset_schema_version"] != CURRICULUM_DATASET_SCHEMA_VERSION:
        raise _error(
            f"unsupported dataset schema: {document['dataset_schema_version']!r}"
        )
    if (
        _expect_object(document["experiment"], _EXPERIMENT_FIELDS, "experiment")
        != experiment_block()
    ):
        raise _error("dataset experiment identity is not the locked Issue #165 one")

    recorded_arm = _arm_from_document(document)
    if arm is not None and recorded_arm is not require_arm(arm):
        raise _error(
            "the dataset arm is not the arm this readback requires; an Arm Y "
            "dataset is never accepted as Arm F evidence and vice versa"
        )
    if document["arm"] != arm_block(recorded_arm):
        raise _error("dataset arm block is not the locked one")
    if document["teacher"] != teacher_block(recorded_arm):
        raise _error(
            "the recorded teacher is not the curated teacher this arm is defined "
            "by; a teacher mismatch is STOP / INVALID"
        )
    for name, expected in (
        ("protocol", dataset_protocol_block()),
        ("feature", feature_block()),
        ("derived_feature", p1_feature_block()),
        ("vocabulary", vocabulary_block()),
        ("transition_semantics", transition_semantics_block()),
        ("reward_semantics", reward_semantics_block()),
    ):
        if document[name] != expected:
            raise _error(f"dataset {name} is not the locked Issue #165 block")

    provenance = _expect_object(document["provenance"], PROVENANCE_FIELDS, "provenance")
    for name, value in provenance.items():
        if type(value) is not str or not value:
            raise _error(f"provenance.{name} must be a non-empty string")

    games = document["games"]
    if type(games) is not list or len(games) != DATASET_HANCHAN_COUNT:
        raise _error(f"games must contain exactly {DATASET_HANCHAN_COUNT} entries")
    seen_seeds: list[int] = []
    total_rows = 0
    total_terminal = 0
    total_decisions = 0
    total_counts: dict[str, int] = {}
    for entry in games:
        game = _expect_object(entry, _GAME_FIELDS, "game")
        seed = _expect(game["seed"], int, "game.seed")
        split = split_for_seed(seed)
        if game["split"] != split.value:
            raise _error(f"game {seed} split does not match the locked protocol")
        row_count = _expect(game["row_count"], int, "game.row_count")
        if row_count <= 0:
            raise _error("game.row_count must be positive")
        terminal_row_count = _expect(
            game["terminal_row_count"], int, "game.terminal_row_count"
        )
        if not 0 <= terminal_row_count <= row_count:
            raise _error("game.terminal_row_count is out of range")
        decision_count = _expect(game["decision_count"], int, "game.decision_count")
        if decision_count <= 0:
            raise _error("game.decision_count must be positive")
        counts = require_family_counts(
            game["teacher_action_family_counts"],
            "game.teacher_action_family_counts",
        )
        if sum(counts.values()) != decision_count:
            raise _error(
                "game teacher action-family counts do not partition its teacher "
                "decisions"
            )
        for name in ("scores", "ranks"):
            values = _expect_int_list(game[name], f"game.{name}")
            if len(values) != 4:
                raise _error(f"game.{name} must contain exactly four values")
        if sorted(_expect_int_list(game["ranks"], "game.ranks")) != [1, 2, 3, 4]:
            raise _error("game.ranks must be a permutation of 1..4")
        seen_seeds.append(seed)
        total_rows += row_count
        total_terminal += terminal_row_count
        total_decisions += decision_count
        _merge_family_counts(total_counts, counts)
    if seen_seeds != list(DATASET_ORDERED_SEEDS):
        raise _error("games must be the locked seed population in ascending order")

    totals = _expect_object(document["totals"], _TOTALS_FIELDS, "totals")
    if totals["game_count"] != DATASET_HANCHAN_COUNT:
        raise _error("totals.game_count does not match the locked hanchan count")
    if _expect(totals["row_count"], int, "totals.row_count") != total_rows:
        raise _error("totals.row_count does not match the per-game row counts")
    if (
        _expect(totals["terminal_row_count"], int, "totals.terminal_row_count")
        != total_terminal
    ):
        raise _error("totals.terminal_row_count does not match the per-game counts")
    if (
        _expect(totals["nonterminal_row_count"], int, "totals.nonterminal_row_count")
        != total_rows - total_terminal
    ):
        raise _error("totals.nonterminal_row_count is not the complement")
    if _expect(totals["decision_count"], int, "totals.decision_count") != (
        total_decisions
    ):
        raise _error("totals.decision_count does not match the per-game counts")
    if require_family_counts(
        totals["teacher_action_family_counts"],
        "totals.teacher_action_family_counts",
    ) != dict(sorted(total_counts.items())):
        raise _error(
            "totals.teacher_action_family_counts does not match the per-game counts"
        )

    files = _expect_object(document["files"], _FILE_NAMES, "files")
    for name, value in files.items():
        file_entry = _expect_object(value, _FILE_FIELDS, f"files.{name}")
        if _expect(file_entry["bytes"], int, f"files.{name}.bytes") < 0:
            raise _error(f"files.{name}.bytes must not be negative")
        _digest(file_entry["sha256"], f"files.{name}.sha256")

    identity = _digest(document["dataset_identity"], "dataset_identity")
    if identity != dataset_identity(document):
        raise _error("dataset_identity does not match the manifest content")
    return document, recorded_arm


def load_curriculum_dataset(
    path: str | Path, *, arm: CurriculumArm | None = None
) -> LoadedFiniteHorizonCurriculumDataset:
    """curriculum dataset artifactをstrict readbackする。

    manifestが自己申告する値をauthorityにしない。arm / teacher / protocol /
    feature / vocabulary / transition / reward blockはlocked constantから
    再導出して照合し、`dataset_identity`はmanifest本体から再計算する。
    `arm`を渡した場合、別armのdatasetはfail closedで拒否する。
    """
    verify_contract_identity()
    path = Path(path)
    if not path.is_dir():
        raise _error("curriculum dataset path is not a directory")
    if {item.name for item in path.iterdir()} != _ARTIFACT_FILENAMES:
        raise _error("curriculum dataset contains missing or extra files")

    manifest_text = (path / MANIFEST_FILENAME).read_text(encoding="utf-8")
    try:
        manifest = json.loads(manifest_text)
    except json.JSONDecodeError as error:
        raise _error("manifest is not valid JSON") from error
    document, recorded_arm = _validate_manifest(manifest, arm=arm)
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

    expected_rows = {entry["seed"]: entry["row_count"] for entry in document["games"]}
    records = read_validated_rows(payloads, split_resolver=split_for_seed)

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

    return LoadedFiniteHorizonCurriculumDataset(
        path=path, manifest=document, rows=records, arm=recorded_arm
    )


def require_dataset_pair(
    control: LoadedFiniteHorizonCurriculumDataset,
    curriculum: LoadedFiniteHorizonCurriculumDataset,
) -> None:
    """2 armのdatasetが「teacherだけが違う」pairであることを確認する。

    seed membership、split、game mode、feature / vocabulary identity、
    transition / reward semantics、source revisionsが一致し、arm / teacher /
    dataset identityだけが異なることを要求する。
    """
    for dataset in (control, curriculum):
        if not isinstance(dataset, LoadedFiniteHorizonCurriculumDataset):
            raise TypeError(
                "both datasets must be LoadedFiniteHorizonCurriculumDataset values"
            )
    if control.arm is not CurriculumArm.CONTROL:
        raise _error("the control dataset is not the Arm Y dataset")
    if curriculum.arm is not CurriculumArm.CURRICULUM:
        raise _error("the curriculum dataset is not the Arm F dataset")
    for name in (
        "protocol",
        "feature",
        "derived_feature",
        "vocabulary",
        "transition_semantics",
        "reward_semantics",
        "experiment",
    ):
        if control.manifest[name] != curriculum.manifest[name]:
            raise _error(
                f"the two arms do not share the same {name}; only the teacher "
                "may differ between the arms"
            )
    if control.manifest["provenance"] != curriculum.manifest["provenance"]:
        raise _error(
            "the two arms were not generated from the same source revisions; the "
            "teacher must be the only changed axis"
        )
    if control.manifest["teacher"] == curriculum.manifest["teacher"]:
        raise _error("the two arms must use different teachers")
    if control.identity == curriculum.identity:
        raise _error("the two arm datasets must have different dataset identities")


__all__ = [
    "CALL_ACTION_FAMILIES",
    "CURRICULUM_DATASET_SCHEMA_VERSION",
    "DISCARD_ACTION_FAMILIES",
    "RIICHI_ACTION_FAMILIES",
    "WINNING_ACTION_FAMILIES",
    "CurriculumGameEntry",
    "FiniteHorizonCurriculumDatasetWriter",
    "LoadedFiniteHorizonCurriculumDataset",
    "dataset_identity",
    "experiment_block",
    "load_curriculum_dataset",
    "require_dataset_pair",
    "require_family_counts",
]
