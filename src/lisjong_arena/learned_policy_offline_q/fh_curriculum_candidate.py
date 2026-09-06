"""Per-arm P1 candidate training / support binding / serving checkpoint (Issue #165).

```text
arm dataset (write-once, strict readback)
    -> load_split_tensors()              #140 contract
    -> derive_all_split_tensors()        #158 P1 8241 derivation
    -> train_p1_q_model()                #158 / #140 training semantics, unchanged
    -> own TRAIN support set
    -> write-once serving checkpoint + logical candidate identity
```

## training semanticsを複製しない

このmoduleは新しいtraining loopを持たない。`p1_q_training.train_p1_q_model()`を
そのまま呼び、その中で`q_training.train_from_split_tensors()`が`#140` locked
semanticsを実行する。loss、gamma、optimizer、learning rate、weight decay、
batch size、target sync cadence、maximum epochs、training seed、dataloader
seed、worker count、torch threads、deterministic algorithms、checkpoint
selectionはすべて両armで同一であり、`verify_locked_training_contract()`が
`#158` locked blockとの一致をfail closedで確認する。

**epoch budgetをこのIssueで変更しない。** `maximum_epochs`は
`learned_policy_stage2.protocol.MAXIMUM_EPOCHS`が唯一のsource of truthであり、
`#157`のHandBelief epoch-budget study（40 / 80 epoch）とは無関係である。
resultを見てepochを足すpath、learning rateを変えるpath、seedを追加するpathは
このmoduleに存在しない。

## support semantics

support setはarmごとに**自身のTRAIN dataset**からだけ導出する。

```text
Arm Y support = support(Arm Y TRAIN)
Arm F support = support(Arm F TRAIN)
```

historical `#158` support、cross-arm shared support、他armのsupportをここへ
持ち込むpathは無い。`build_arm_candidate()`はsupported indicesを引数で受け
取らず、渡されたdatasetから再導出する。checkpoint readbackも同じ再導出で
照合するため、cross-arm substitutionはfail closedする。

## candidate identity

weights digest単独をidentityにしない。

```text
candidate binding document
    arm / teacher identity
    source dataset identity
    canonical model weights digest
    P1 feature fingerprint
    action vocabulary fingerprint
    support digest
    training block / model block
    hybrid activation block / fallback Policy binding
    source revisions
        -> canonical serialization -> sha256
        -> learned-offlineq-fh-curriculum:<binding digest>
```

checkpoint bytesが変わればweights digestが変わり、candidate identityも変わる。

生成したweightsはGitへcommitしない。retention先の解決は
`learned_policy_stage4a.candidate.resolve_retention_target()`をそのまま再利用
する。
"""

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from shutil import rmtree
from tempfile import mkdtemp

from lisjong_arena._artifact_io import canonical_json_text
from lisjong_arena.learned_policy_stage4a.candidate import resolve_retention_target
from lisjong_arena.learned_policy_stage4a.errors import Stage4aRetentionError

from .artifact import PROVENANCE_FIELDS, provenance_document, vocabulary_block
from .errors import OfflineQArtifactError
from .fh_curriculum import (
    CurriculumArm,
    arm_block,
    candidate_retention_block,
    require_arm,
    teacher_block,
)
from .fh_curriculum_dataset import (
    LoadedFiniteHorizonCurriculumDataset,
    experiment_block,
)
from .p1_candidate import MANIFEST_FILENAME, WEIGHTS_FILENAME
from .p1_features import derive_all_split_tensors, p1_feature_block
from .p1_q_training import (
    P1_EXPECTED_PARAMETER_COUNT,
    create_p1_model,
    model_weights_digest,
    p1_model_block,
    p1_training_block,
    require_p1_split_tensors,
    train_p1_q_model,
    verify_locked_q_protocol_delta,
)
from .p1_serving import (
    fallback_policy_block,
    hybrid_activation_block,
    verify_p1_serving_contract,
)
from .protocol import (
    MAXIMUM_EPOCHS,
    PROTOCOL_ID,
    TORCH_THREADS,
    VOCABULARY_SIZE,
    Split,
    verify_contract_identity,
)
from .split_tensors import load_split_tensors
from .support import build_support_gate_report, support_set_identity

CANDIDATE_SCHEMA_VERSION = "arena-learned-policy-finite-horizon-curriculum-candidate-v1"
CANDIDATE_BINDING_SCHEMA_VERSION = (
    "arena-learned-policy-finite-horizon-curriculum-candidate-binding-v1"
)
CANDIDATE_IDENTITY_PREFIX = "learned-offlineq-fh-curriculum:"

SELECTED_EPOCH = MAXIMUM_EPOCHS
"""`fixed_final_iteration`なので常に最終epochである。#165で変更しない。"""

CHECKPOINT_SELECTION = "fixed_final_iteration"

_MANIFEST_FIELDS = {
    "checkpoint_schema_version",
    "experiment",
    "protocol_id",
    "arm",
    "teacher",
    "candidate_identity",
    "candidate_binding",
    "canonical_model_weights_digest",
    "weights_sha256",
    "weights_bytes",
    "source_dataset_identity",
    "source_dataset_arm",
    "p1_feature",
    "action_vocabulary",
    "supported_indices",
    "supported_indices_digest",
    "support_size",
    "support_coverage",
    "model",
    "training",
    "parameter_count",
    "selected_epoch",
    "checkpoint_selection",
    "hybrid_activation",
    "fallback_policy",
    "source_revisions",
    "runtime_provenance",
    "source_revisions_match_dataset",
    "runtime",
    "retention",
    "strength_claim",
}
_SUPPORT_COVERAGE_FIELDS = {
    "train_row_count",
    "validation_row_count",
    "train_support_complete_rate",
    "validation_support_complete_rate",
    "combined_support_complete_rate",
    "unsupported_index_count",
}


class CurriculumCandidateError(OfflineQArtifactError):
    """`#165` candidate materialization / checkpoint contract違反。"""


def _error(message: str) -> CurriculumCandidateError:
    return CurriculumCandidateError(message)


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def verify_locked_training_contract() -> None:
    """両armが使うlocked contractのdriftをfail closedで確認する。

    `#158` / `#140`のtraining blockとの一致、P1 serving contract、
    `fixed_final_iteration` epoch selectionを確認する。`#165`はここに独自の
    epoch budget、learning rate、patience、training seedを持たない。
    """
    verify_contract_identity()
    verify_locked_q_protocol_delta()
    verify_p1_serving_contract()
    training = p1_training_block()
    if training["maximum_epochs"] != MAXIMUM_EPOCHS:
        raise _error(
            "the training block maximum epoch budget is not the locked Offline Q "
            "one; Issue #165 never changes the epoch budget"
        )
    if training["checkpoint_selection"] != CHECKPOINT_SELECTION:
        raise _error(
            f"the locked checkpoint selection is {CHECKPOINT_SELECTION}, not "
            f"{training['checkpoint_selection']!r}"
        )
    if SELECTED_EPOCH != MAXIMUM_EPOCHS:
        raise _error(
            "fixed_final_iteration requires the selected epoch to be the maximum "
            "epoch budget"
        )


# --- Support ---------------------------------------------------------------


def derive_train_support(
    dataset: LoadedFiniteHorizonCurriculumDataset,
) -> tuple[int, ...]:
    """armのTRAIN rowだけからsupported action indicesを導出する。

    TRAIN split以外のrowは読まない。呼び出し側がindicesを差し込めないため、
    cross-arm / historical supportの流用は構造的に起こらない。
    """
    if not isinstance(dataset, LoadedFiniteHorizonCurriculumDataset):
        raise TypeError("dataset must be a LoadedFiniteHorizonCurriculumDataset")
    train_indices = dataset.split_indices(Split.TRAIN)
    if not train_indices:
        raise _error("the dataset contains no TRAIN row")
    supported = sorted(
        {dataset.rows[index].behavior_action_index for index in train_indices}
    )
    if any(not 0 <= index < VOCABULARY_SIZE for index in supported):
        raise _error("a TRAIN behavior action index is outside the vocabulary")
    return tuple(supported)


def require_own_train_support(
    dataset: LoadedFiniteHorizonCurriculumDataset, supported_indices
) -> tuple[int, ...]:
    """渡されたsupport setが、そのdatasetのTRAINから導出されることを確認する。

    別armのsupport、historical `#158` support、手で書いたsupportはここで
    fail closedする。
    """
    derived = derive_train_support(dataset)
    if tuple(sorted(int(index) for index in supported_indices)) != derived:
        raise _error(
            "the support set is not the one this arm's own TRAIN dataset derives; "
            "cross-arm and historical support substitution is never accepted"
        )
    return derived


def support_coverage_block(
    dataset: LoadedFiniteHorizonCurriculumDataset,
) -> dict[str, object]:
    """`#140`のTRAIN behavior-support gate reportをそのままcoverageへ使う。"""
    report = build_support_gate_report(dataset)
    if tuple(report.supported_indices) != derive_train_support(dataset):
        raise _error(
            "the support gate report and the TRAIN support derivation disagree"
        )
    return {
        "train_row_count": report.train_row_count,
        "validation_row_count": report.validation_row_count,
        "train_support_complete_rate": report.train_support_complete_rate,
        "validation_support_complete_rate": report.validation_support_complete_rate,
        "combined_support_complete_rate": report.combined_support_complete_rate,
        "unsupported_index_count": len(report.unsupported_indices),
    }


def support_block(
    dataset: LoadedFiniteHorizonCurriculumDataset,
) -> dict[str, object]:
    """persistするsupport identity一式。"""
    indices = derive_train_support(dataset)
    return {
        "supported_indices": list(indices),
        "supported_indices_digest": support_set_identity(indices),
        "support_size": len(indices),
        "support_coverage": support_coverage_block(dataset),
    }


# --- Candidate logical identity -------------------------------------------


def candidate_binding_document(
    *,
    arm: CurriculumArm,
    source_dataset_identity: str,
    canonical_model_weights_digest: str,
    support_set_digest: str,
    source_revisions: dict,
) -> dict[str, object]:
    """candidate logical identityの正本となるbinding document。"""
    require_arm(arm)
    for name, value in (
        ("source_dataset_identity", source_dataset_identity),
        ("canonical_model_weights_digest", canonical_model_weights_digest),
        ("support_set_digest", support_set_digest),
    ):
        if type(value) is not str or len(value) != 64:
            raise _error(f"{name} must be a 64 character sha256 digest")
    if type(source_revisions) is not dict or set(source_revisions) != PROVENANCE_FIELDS:
        raise _error("source_revisions fields are invalid")
    return {
        "binding_schema_version": CANDIDATE_BINDING_SCHEMA_VERSION,
        "arm": arm_block(arm),
        "teacher": teacher_block(arm),
        "source_dataset_identity": source_dataset_identity,
        "canonical_model_weights_digest": canonical_model_weights_digest,
        "p1_feature": p1_feature_block(),
        "action_vocabulary": vocabulary_block(),
        "support_set_digest": support_set_digest,
        "model": p1_model_block(),
        "training": p1_training_block(),
        "hybrid_activation": hybrid_activation_block(),
        "fallback_policy": fallback_policy_block(),
        "source_revisions": dict(source_revisions),
    }


def candidate_logical_identity(binding: dict[str, object]) -> str:
    """binding documentのcanonical serializationからlogical identityを導出する。"""
    if type(binding) is not dict:
        raise _error("candidate binding must be an object")
    digest = _sha256(canonical_json_text(binding).encode("utf-8"))
    return f"{CANDIDATE_IDENTITY_PREFIX}{digest}"


def require_candidate_identity(identity: object, binding: dict[str, object]) -> str:
    """identityがbinding documentからderiveされたものであることを確認する。"""
    if type(identity) is not str:
        raise _error("candidate identity must be a str")
    if not identity.startswith(CANDIDATE_IDENTITY_PREFIX):
        raise _error(
            "a free-form candidate alias is not a candidate identity; the identity "
            f"must be derived as {CANDIDATE_IDENTITY_PREFIX}<binding digest>"
        )
    expected = candidate_logical_identity(binding)
    if identity != expected:
        raise _error(
            "the candidate identity is not derivable from its binding document"
        )
    return identity


# --- Training --------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ArmCandidateTraining:
    """1 armのtraining結果。training semanticsそのものは#158が所有する。"""

    arm: CurriculumArm
    source_dataset_identity: str
    model: object
    canonical_model_weights_digest: str
    supported_indices: tuple[int, ...]
    support_set_digest: str
    support_coverage: dict
    selected_epoch: int
    history: tuple
    derived_coverage: dict
    source_revisions: dict
    wall_clock_seconds: float

    def __post_init__(self) -> None:
        if self.selected_epoch != SELECTED_EPOCH:
            raise _error(
                "the training run did not stop at the locked fixed_final_iteration "
                f"epoch {SELECTED_EPOCH}"
            )


def _finalize_model(model):
    model.eval()
    for parameter in model.parameters():
        parameter.requires_grad_(False)
    return model


def train_arm_candidate(
    dataset: LoadedFiniteHorizonCurriculumDataset,
) -> ArmCandidateTraining:
    """1 armのP1 Q candidateを、`#158`と完全に同一のsemanticsで学習する。

    epoch数、seed、learning rate、checkpoint selectionを上書きする引数を
    受け取らない。resultを見て学習条件を変えるpathは存在しない。
    """
    verify_locked_training_contract()
    if not isinstance(dataset, LoadedFiniteHorizonCurriculumDataset):
        raise TypeError("dataset must be a LoadedFiniteHorizonCurriculumDataset")

    tensors = load_split_tensors(dataset)
    derived, coverage = derive_all_split_tensors(tensors)
    require_p1_split_tensors(derived)

    indices = derive_train_support(dataset)
    run = train_p1_q_model(derived)
    trained_support = tuple(
        int(index) for index in run.support_mask.nonzero().flatten().tolist()
    )
    if trained_support != indices:
        raise _error(
            "the TRAIN support mask used by training is not the support set this "
            "arm's own TRAIN dataset derives"
        )
    model = _finalize_model(run.model)
    return ArmCandidateTraining(
        arm=dataset.arm,
        source_dataset_identity=dataset.identity,
        model=model,
        canonical_model_weights_digest=model_weights_digest(model),
        supported_indices=indices,
        support_set_digest=support_set_identity(indices),
        support_coverage=support_coverage_block(dataset),
        selected_epoch=run.selected_epoch,
        history=run.history,
        derived_coverage={
            split.value: entry.to_document() for split, entry in coverage.items()
        },
        source_revisions=dict(dataset.manifest["provenance"]),
        wall_clock_seconds=run.wall_clock_seconds,
    )


# --- Serving checkpoint ----------------------------------------------------


def _runtime_block() -> dict[str, object]:
    import torch

    return {
        "device": "cpu",
        "torch_version": str(torch.__version__),
        "torch_threads": TORCH_THREADS,
    }


def build_candidate_manifest(
    training: ArmCandidateTraining,
    *,
    weights_bytes: int,
    weights_sha256: str,
    runtime_provenance: dict | None = None,
) -> dict[str, object]:
    """serving checkpoint manifestを組み立てる。"""
    if not isinstance(training, ArmCandidateTraining):
        raise TypeError("training must be an ArmCandidateTraining")
    arm = require_arm(training.arm)
    live = (
        provenance_document()
        if runtime_provenance is None
        else dict(runtime_provenance)
    )
    if set(live) != PROVENANCE_FIELDS:
        raise _error("runtime provenance fields are invalid")
    binding = candidate_binding_document(
        arm=arm,
        source_dataset_identity=training.source_dataset_identity,
        canonical_model_weights_digest=training.canonical_model_weights_digest,
        support_set_digest=training.support_set_digest,
        source_revisions=training.source_revisions,
    )
    return {
        "checkpoint_schema_version": CANDIDATE_SCHEMA_VERSION,
        "experiment": experiment_block(),
        "protocol_id": PROTOCOL_ID,
        "arm": arm_block(arm),
        "teacher": teacher_block(arm),
        "candidate_identity": candidate_logical_identity(binding),
        "candidate_binding": binding,
        "canonical_model_weights_digest": training.canonical_model_weights_digest,
        "weights_sha256": weights_sha256,
        "weights_bytes": weights_bytes,
        "source_dataset_identity": training.source_dataset_identity,
        "source_dataset_arm": arm.value,
        "p1_feature": p1_feature_block(),
        "action_vocabulary": vocabulary_block(),
        "supported_indices": list(training.supported_indices),
        "supported_indices_digest": training.support_set_digest,
        "support_size": len(training.supported_indices),
        "support_coverage": dict(training.support_coverage),
        "model": p1_model_block(),
        "training": p1_training_block(),
        "parameter_count": P1_EXPECTED_PARAMETER_COUNT,
        "selected_epoch": training.selected_epoch,
        "checkpoint_selection": CHECKPOINT_SELECTION,
        "hybrid_activation": hybrid_activation_block(),
        "fallback_policy": fallback_policy_block(),
        "source_revisions": dict(training.source_revisions),
        "runtime_provenance": live,
        "source_revisions_match_dataset": live == dict(training.source_revisions),
        "runtime": _runtime_block(),
        "retention": candidate_retention_block(arm),
        "strength_claim": None,
    }


def save_arm_candidate(
    destination: str | Path,
    training: ArmCandidateTraining,
    *,
    runtime_provenance: dict | None = None,
) -> "LoadedArmCandidate":
    """serving checkpointをstagingで組み立ててからwrite-onceで公開する。"""
    import torch

    destination = Path(destination)
    if destination.exists():
        raise FileExistsError("candidate checkpoint destination already exists")
    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(
        mkdtemp(prefix=f".{destination.name}-staging-", dir=destination.parent)
    )
    published = False
    try:
        weights_path = staging / WEIGHTS_FILENAME
        torch.save(training.model.state_dict(), weights_path)
        weights = weights_path.read_bytes()
        manifest = build_candidate_manifest(
            training,
            weights_bytes=len(weights),
            weights_sha256=_sha256(weights),
            runtime_provenance=runtime_provenance,
        )
        (staging / MANIFEST_FILENAME).write_text(
            canonical_json_text(manifest), encoding="utf-8", newline="\n"
        )
        staging.rename(destination)
        published = True
    finally:
        if not published:
            rmtree(staging, ignore_errors=True)
    return load_arm_candidate(destination, arm=training.arm)


def materialize_arm_candidate(
    training: ArmCandidateTraining,
    *,
    backend: str,
    root: str | Path,
    key: str,
) -> tuple[str, "LoadedArmCandidate"]:
    """non-ephemeral retention先へcheckpointをwrite-onceで置く。

    retention先の判定は`resolve_retention_target()`をそのまま再利用する。
    宣言できるnon-ephemeral rootが無い場合は`Stage4aRetentionError`のまま
    caller（`CURRICULUM EVIDENCE BLOCKED`判定）へ返す。
    """
    target = resolve_retention_target(backend=backend, root=root, key=key)
    return target.key, save_arm_candidate(target.bundle_path, training)


@dataclass(frozen=True, slots=True)
class LoadedArmCandidate:
    """strict readback済みの1 arm serving checkpoint。"""

    path: Path
    manifest: dict
    model: object
    arm: CurriculumArm
    supported_indices: frozenset[int]

    @property
    def candidate_identity(self) -> str:
        return self.manifest["candidate_identity"]

    @property
    def canonical_model_weights_digest(self) -> str:
        return self.manifest["canonical_model_weights_digest"]

    @property
    def support_set_digest(self) -> str:
        return self.manifest["supported_indices_digest"]

    @property
    def source_dataset_identity(self) -> str:
        return self.manifest["source_dataset_identity"]


def _arm_from_manifest(manifest: dict) -> CurriculumArm:
    block = manifest.get("arm")
    if type(block) is not dict or type(block.get("arm")) is not str:
        raise _error("candidate manifest arm block is invalid")
    for arm in CurriculumArm:
        if arm.value == block["arm"]:
            return arm
    raise _error(f"unknown curriculum arm: {block['arm']!r}")


def _require_supported_indices(supported_indices) -> list[int]:
    if type(supported_indices) is not list:
        raise _error("supported_indices must be an array of ints")
    if any(type(index) is not int for index in supported_indices):
        raise _error("supported_indices must contain only exact ints")
    if not supported_indices:
        raise _error("the TRAIN support set must not be empty")
    if supported_indices != sorted(set(supported_indices)):
        raise _error("supported_indices must be sorted and free of duplicates")
    if any(not 0 <= index < VOCABULARY_SIZE for index in supported_indices):
        raise _error("supported_indices carries an out-of-range index")
    return supported_indices


def load_arm_candidate(
    path: str | Path,
    *,
    arm: CurriculumArm | None = None,
    source_dataset_identity: str | None = None,
) -> LoadedArmCandidate:
    """serving checkpointを読み、identity / digest / semanticsを再導出して検証する。

    documentが自己申告する値をauthorityにしない。candidate identityはbinding
    documentから、weights digestは実際にloadしたweightsから再導出して照合する。
    `arm`を渡した場合、別armのcheckpointはfail closedで拒否する。
    """
    verify_locked_training_contract()
    path = Path(path)
    if not path.is_dir():
        raise _error("candidate checkpoint path is not a directory")
    if {item.name for item in path.iterdir()} != {
        MANIFEST_FILENAME,
        WEIGHTS_FILENAME,
    }:
        raise _error("candidate checkpoint contains missing or extra files")

    manifest_text = (path / MANIFEST_FILENAME).read_text(encoding="utf-8")
    try:
        manifest = json.loads(manifest_text)
    except json.JSONDecodeError as error:
        raise _error("candidate manifest is not valid JSON") from error
    if type(manifest) is not dict or set(manifest) != _MANIFEST_FIELDS:
        raise _error("candidate manifest fields are invalid")
    if manifest["checkpoint_schema_version"] != CANDIDATE_SCHEMA_VERSION:
        raise _error("unsupported candidate checkpoint schema version")
    if canonical_json_text(manifest) != manifest_text:
        raise _error("candidate manifest bytes are not canonical JSON")

    recorded_arm = _arm_from_manifest(manifest)
    if arm is not None and recorded_arm is not require_arm(arm):
        raise _error(
            "the candidate arm is not the arm this readback requires; an Arm Y "
            "candidate is never accepted as the Arm F candidate and vice versa"
        )
    if manifest["experiment"] != experiment_block():
        raise _error("candidate experiment identity is not the locked Issue #165 one")
    if manifest["protocol_id"] != PROTOCOL_ID:
        raise _error("candidate protocol id is not the locked Offline Q one")
    if manifest["arm"] != arm_block(recorded_arm):
        raise _error("candidate arm block is not the locked one")
    if manifest["teacher"] != teacher_block(recorded_arm):
        raise _error("the candidate teacher is not the curated teacher of its arm")
    if manifest["source_dataset_arm"] != recorded_arm.value:
        raise _error("the candidate source dataset arm is not its own arm")
    for name, expected in (
        ("p1_feature", p1_feature_block()),
        ("action_vocabulary", vocabulary_block()),
        ("model", p1_model_block()),
        ("training", p1_training_block()),
        ("hybrid_activation", hybrid_activation_block()),
        ("fallback_policy", fallback_policy_block()),
        ("retention", candidate_retention_block(recorded_arm)),
    ):
        if manifest[name] != expected:
            raise _error(f"candidate manifest {name} is not the locked block")
    if manifest["parameter_count"] != P1_EXPECTED_PARAMETER_COUNT:
        raise _error("candidate manifest parameter count is not the locked one")
    if manifest["selected_epoch"] != SELECTED_EPOCH:
        raise _error(
            "the candidate is not the locked fixed_final_iteration epoch "
            f"{SELECTED_EPOCH} checkpoint"
        )
    if manifest["checkpoint_selection"] != CHECKPOINT_SELECTION:
        raise _error("candidate checkpoint selection is not the locked one")
    if manifest["strength_claim"] is not None:
        raise _error("a serving checkpoint must not carry a strength claim")
    if (
        source_dataset_identity is not None
        and manifest["source_dataset_identity"] != source_dataset_identity
    ):
        raise _error(
            "the candidate was not trained from the dataset this readback requires"
        )
    for name in ("source_revisions", "runtime_provenance"):
        block = manifest[name]
        if type(block) is not dict or set(block) != PROVENANCE_FIELDS:
            raise _error(f"candidate manifest {name} fields are invalid")
    if type(manifest["source_revisions_match_dataset"]) is not bool:
        raise _error("source_revisions_match_dataset must be an exact bool")
    if manifest["source_revisions_match_dataset"] is not (
        manifest["runtime_provenance"] == manifest["source_revisions"]
    ):
        raise _error(
            "source_revisions_match_dataset does not follow from the recorded "
            "provenance; it is derived by exact comparison, never self-declared"
        )
    coverage = manifest["support_coverage"]
    if type(coverage) is not dict or set(coverage) != _SUPPORT_COVERAGE_FIELDS:
        raise _error("candidate support coverage fields are invalid")
    for name in ("train_row_count", "validation_row_count", "unsupported_index_count"):
        if type(coverage[name]) is not int or coverage[name] < 0:
            raise _error(f"candidate support_coverage.{name} must be a count")
    for name in (
        "train_support_complete_rate",
        "validation_support_complete_rate",
        "combined_support_complete_rate",
    ):
        if type(coverage[name]) not in (int, float) or not 0.0 <= coverage[name] <= 1.0:
            raise _error(f"candidate support_coverage.{name} must be a unit rate")
    runtime = manifest["runtime"]
    if type(runtime) is not dict or set(runtime) != {
        "device",
        "torch_version",
        "torch_threads",
    }:
        raise _error("candidate manifest runtime fields are invalid")
    if runtime["device"] != "cpu":
        raise _error("Issue #165 candidates are trained and served on the CPU")
    if type(runtime["torch_version"]) is not str or not runtime["torch_version"]:
        raise _error("candidate manifest runtime torch_version must be recorded")
    if type(runtime["torch_threads"]) is not int or runtime["torch_threads"] < 1:
        raise _error("candidate manifest runtime torch_threads must be positive")

    indices = _require_supported_indices(manifest["supported_indices"])
    if manifest["support_size"] != len(indices):
        raise _error("candidate support size does not match its supported indices")
    if manifest["supported_indices_digest"] != support_set_identity(indices):
        raise _error(
            "candidate supported_indices does not match its own support digest"
        )

    weights = (path / WEIGHTS_FILENAME).read_bytes()
    if len(weights) != manifest["weights_bytes"]:
        raise _error("candidate checkpoint weights byte count differs")
    if _sha256(weights) != manifest["weights_sha256"]:
        raise _error("candidate checkpoint weights sha256 differs")

    import torch

    state_dict = torch.load(
        path / WEIGHTS_FILENAME, weights_only=True, map_location="cpu"
    )
    model = create_p1_model()
    try:
        model.load_state_dict(state_dict, strict=True)
    except RuntimeError as error:
        raise _error(
            "candidate checkpoint state_dict does not match the locked P1 model"
        ) from error
    _finalize_model(model)
    digest = model_weights_digest(model)
    if digest != manifest["canonical_model_weights_digest"]:
        raise _error(
            "the canonical weights digest recomputed from the loaded model differs "
            "from the recorded one"
        )

    binding = candidate_binding_document(
        arm=recorded_arm,
        source_dataset_identity=manifest["source_dataset_identity"],
        canonical_model_weights_digest=digest,
        support_set_digest=manifest["supported_indices_digest"],
        source_revisions=manifest["source_revisions"],
    )
    if manifest["candidate_binding"] != binding:
        raise _error(
            "the recorded candidate binding document is not the one this "
            "checkpoint's arm, teacher, dataset, weights, feature, vocabulary, "
            "support set, training, activation semantics and fallback Policy derive"
        )
    require_candidate_identity(manifest["candidate_identity"], binding)
    return LoadedArmCandidate(
        path=path,
        manifest=manifest,
        model=model,
        arm=recorded_arm,
        supported_indices=frozenset(indices),
    )


def require_candidate_pair(
    control: LoadedArmCandidate, curriculum: LoadedArmCandidate
) -> None:
    """2 armのcandidateが「teacher由来の差」だけを持つpairであることを確認する。

    representation、model、training、activation、fallback、source revisions、
    runtime provenanceが一致し、arm / teacher / dataset / support / weightsが
    異なることを要求する。
    """
    for candidate in (control, curriculum):
        if not isinstance(candidate, LoadedArmCandidate):
            raise TypeError("both candidates must be LoadedArmCandidate values")
    if control.arm is not CurriculumArm.CONTROL:
        raise _error("the control candidate is not the Arm Y candidate")
    if curriculum.arm is not CurriculumArm.CURRICULUM:
        raise _error("the curriculum candidate is not the Arm F candidate")
    for name in (
        "experiment",
        "p1_feature",
        "action_vocabulary",
        "model",
        "training",
        "hybrid_activation",
        "fallback_policy",
        "parameter_count",
        "selected_epoch",
        "checkpoint_selection",
        "source_revisions",
        "runtime_provenance",
        "runtime",
    ):
        if control.manifest[name] != curriculum.manifest[name]:
            raise _error(
                f"the two candidates do not share the same {name}; the teacher "
                "must be the only changed axis"
            )
    for candidate in (control, curriculum):
        if candidate.manifest["source_revisions_match_dataset"] is not True:
            raise _error(
                "a candidate was trained at different source revisions than its "
                "own dataset was generated at"
            )
    if control.manifest["teacher"] == curriculum.manifest["teacher"]:
        raise _error("the two candidates must be trained from different teachers")
    if control.source_dataset_identity == curriculum.source_dataset_identity:
        raise _error("the two candidates must come from different arm datasets")
    if control.candidate_identity == curriculum.candidate_identity:
        raise _error("the two candidates must have different candidate identities")


__all__ = [
    "CANDIDATE_BINDING_SCHEMA_VERSION",
    "CANDIDATE_IDENTITY_PREFIX",
    "CANDIDATE_SCHEMA_VERSION",
    "CHECKPOINT_SELECTION",
    "SELECTED_EPOCH",
    "ArmCandidateTraining",
    "CurriculumCandidateError",
    "LoadedArmCandidate",
    "Stage4aRetentionError",
    "build_candidate_manifest",
    "candidate_binding_document",
    "candidate_logical_identity",
    "derive_train_support",
    "load_arm_candidate",
    "materialize_arm_candidate",
    "require_candidate_identity",
    "require_candidate_pair",
    "require_own_train_support",
    "save_arm_candidate",
    "support_block",
    "support_coverage_block",
    "train_arm_candidate",
    "verify_locked_training_contract",
]
