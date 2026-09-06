"""Exact P1 candidate materialization / serving checkpoint contract (Issue #162).

`lisbun/lisjong-arena #158`のGate Aは、P1 Q-v2 candidateのweights digestを
記録したが、そのcandidateをserveするためのcheckpoint contractは確立して
いなかった。このmoduleはGate B（`#162`）のGate 0として、

```text
exact #158 candidate
    -> experiment-local versioned serving checkpoint
    -> write-once retention
    -> strict readback
```

を所有する。

## Candidate logical identity — weights-onlyではない

Policy behaviorはweightsだけで決まらない。同じweightsでも、P1 feature
semantics、action vocabulary、TRAIN support set、hybrid activation semantics、
fallback Policyのいずれかが違えば別のPolicyである。したがってcandidate
identityは

```text
candidate binding document
    -> canonical serialization
    -> sha256
    -> learned-offlineq-p1-gateb:<binding digest>
```

としてderiveする。free-form aliasやweights digest単独からidentityを作らない。

## Materialization path

```text
A. exact retained #158 weightsが利用可能
   -> strict state_dict load
   -> canonical weights digest verify

B. exact retained sourceからdeterministic reconstruction
   -> retained dataset
   -> #158と同一のP1 derived representation
   -> #140と同一のtraining semantics / seed / epoch
   -> canonical weights digest verify
```

Path Bは新しいresearch trainingではなく、`#158` weightsのexact
reconstructionだけを目的とする。digestが一致しない場合はfail closedであり、

```text
seed変更 / epoch追加 / optimizer変更 / alternate config retry / tolerance
```

へ進むpathをこのmoduleは持たない。

## Real candidate vs fixture candidate

`ExpectedCandidateIdentities`をcallerが差し替えられるのは、synthetic fixture
でcontract自体を検証できるようにするためだけである。checkpoint manifestは
`real_candidate_materialization`を**自己申告ではなく**locked constantとの
exact比較から導出して記録し、Gate B resultはこのflagがtrueのcandidateでしか
exhaustive outcomeを記録できない。fixture candidateのGate B結果がreal Gate B
evidenceとして扱われることはない。

generated weightsはGitへcommitしない。retention先の解決は
`learned_policy_stage4a.candidate.resolve_retention_target()`をそのまま再利用
し、Git work tree内やtemporary directory配下をfail closedで拒否する。
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
from lisjong_arena.single_round_artifact import (
    collect_execution_provenance,
    execution_provenance_to_dict,
    parse_execution_provenance,
)

from .artifact import vocabulary_block
from .diagnosis import LOCKED_SOURCE_IDENTITIES
from .errors import OfflineQArtifactError, OfflineQProtocolError
from .p1_features import p1_feature_block
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
    verify_contract_identity,
)
from .support import support_set_identity

SERVING_CHECKPOINT_SCHEMA_VERSION = (
    "arena-learned-policy-offlineq-p1-serving-checkpoint-v1"
)
CANDIDATE_BINDING_SCHEMA_VERSION = (
    "arena-learned-policy-offlineq-p1-candidate-binding-v1"
)
CANDIDATE_IDENTITY_PREFIX = "learned-offlineq-p1-gateb:"

SOURCE_ISSUE = "lisbun/lisjong-arena#162"
PREDECESSOR_ISSUE = "lisbun/lisjong-arena#158"
PARENT_ISSUE = "lisbun/lisjong-project#45"

MANIFEST_FILENAME = "manifest.json"
WEIGHTS_FILENAME = "weights.pt"

LOCKED_SELECTED_EPOCH = 20
"""`#158`が選択したepoch。`fixed_final_iteration`なので常に最終epochである。"""

if LOCKED_SELECTED_EPOCH != MAXIMUM_EPOCHS:
    raise RuntimeError(
        "the locked P1 selected epoch is not the locked maximum epoch; "
        "fixed_final_iteration checkpoint selection would no longer hold"
    )


class P1CandidateError(OfflineQArtifactError):
    """P1 candidateのmaterialization / checkpoint contract違反。"""


@dataclass(frozen=True, slots=True)
class ExpectedCandidateIdentities:
    """materializeするcandidateのexact identity。"""

    canonical_model_weights_digest: str
    source_dataset_identity: str
    support_set_digest: str

    def __post_init__(self) -> None:
        for name in (
            "canonical_model_weights_digest",
            "source_dataset_identity",
            "support_set_digest",
        ):
            value = getattr(self, name)
            if type(value) is not str or len(value) != 64:
                raise P1CandidateError(f"{name} must be a 64 character sha256 digest")

    def to_document(self) -> dict[str, object]:
        return {
            "canonical_model_weights_digest": self.canonical_model_weights_digest,
            "source_dataset_identity": self.source_dataset_identity,
            "support_set_digest": self.support_set_digest,
        }


LOCKED_P1_CANDIDATE = ExpectedCandidateIdentities(
    canonical_model_weights_digest=(
        "f8108bf1e671007f22a8b36295ff545b3198479f74d83bb48da8a45df194461f"
    ),
    source_dataset_identity=LOCKED_SOURCE_IDENTITIES.dataset_identity,
    support_set_digest=LOCKED_SOURCE_IDENTITIES.supported_indices_digest,
)
"""Issue #158のGate A resultがlockしたexact P1 candidate identity。"""


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


# --- Candidate logical identity -------------------------------------------


def candidate_binding_document(
    *,
    canonical_model_weights_digest: str,
    support_set_digest: str,
) -> dict[str, object]:
    """candidate logical identityの正本となるbinding document。

    weights digestだけでなく、P1 feature fingerprint、action vocabulary
    fingerprint、TRAIN support digest、hybrid activation semantics、fallback
    Policy identity / revisionまでを含める。どれか1つでも変われば別candidate
    identityになる。
    """
    if type(canonical_model_weights_digest) is not str or (
        len(canonical_model_weights_digest) != 64
    ):
        raise P1CandidateError(
            "canonical_model_weights_digest must be a 64 character sha256 digest"
        )
    if type(support_set_digest) is not str or len(support_set_digest) != 64:
        raise P1CandidateError(
            "support_set_digest must be a 64 character sha256 digest"
        )
    return {
        "binding_schema_version": CANDIDATE_BINDING_SCHEMA_VERSION,
        "canonical_model_weights_digest": canonical_model_weights_digest,
        "p1_feature": p1_feature_block(),
        "action_vocabulary": vocabulary_block(),
        "support_set_digest": support_set_digest,
        "hybrid_activation": hybrid_activation_block(),
        "fallback_policy": fallback_policy_block(),
    }


def candidate_logical_identity(binding: dict[str, object]) -> str:
    """binding documentのcanonical serializationからlogical identityを導出する。"""
    if type(binding) is not dict:
        raise P1CandidateError("candidate binding must be an object")
    digest = _sha256(canonical_json_text(binding).encode("utf-8"))
    return f"{CANDIDATE_IDENTITY_PREFIX}{digest}"


def require_candidate_identity(identity: object, binding: dict[str, object]) -> str:
    """identityがbinding documentからderiveされたものであることを確認する。

    free-form aliasや、prefixだけ合っていて中身が違うidentityを拒否する。
    """
    if type(identity) is not str:
        raise P1CandidateError("candidate identity must be a str")
    if not identity.startswith(CANDIDATE_IDENTITY_PREFIX):
        raise P1CandidateError(
            "a free-form candidate alias is not a candidate identity; the identity "
            f"must be derived as {CANDIDATE_IDENTITY_PREFIX}<binding digest>"
        )
    expected = candidate_logical_identity(binding)
    if identity != expected:
        raise P1CandidateError(
            "the candidate identity is not derivable from its binding document"
        )
    return identity


# --- Materialization ------------------------------------------------------


MATERIALIZATION_RETAINED_WEIGHTS = "exact-retained-158-weights"
MATERIALIZATION_RECONSTRUCTION = "exact-158-deterministic-reconstruction"
MATERIALIZATION_SOURCES = frozenset(
    {MATERIALIZATION_RETAINED_WEIGHTS, MATERIALIZATION_RECONSTRUCTION}
)


@dataclass(frozen=True, slots=True)
class MaterializedP1Candidate:
    """digest verify済みのP1 candidate modelと、その由来。"""

    model: object
    canonical_model_weights_digest: str
    selected_epoch: int
    materialization_source: str
    epoch_history: tuple

    def __post_init__(self) -> None:
        if self.materialization_source not in MATERIALIZATION_SOURCES:
            raise P1CandidateError(
                f"unknown materialization source: {self.materialization_source!r}"
            )
        if self.selected_epoch != LOCKED_SELECTED_EPOCH:
            raise P1CandidateError(
                "the exact #158 candidate is the fixed_final_iteration checkpoint "
                f"of epoch {LOCKED_SELECTED_EPOCH}"
            )


def _finalize_model(model):
    model.eval()
    for parameter in model.parameters():
        parameter.requires_grad_(False)
    return model


def _require_exact_digest(digest: str, expected: ExpectedCandidateIdentities) -> None:
    if digest != expected.canonical_model_weights_digest:
        raise P1CandidateError(
            "the canonical model weights digest does not match the exact #158 "
            f"candidate ({expected.canonical_model_weights_digest}); Gate B stops "
            "here as STOP / INVALID -- there is no seed change, extra epoch, "
            "alternate config retry, or tolerance acceptance path"
        )


def load_retained_p1_candidate(
    weights_path: str | Path,
    *,
    expected: ExpectedCandidateIdentities = LOCKED_P1_CANDIDATE,
) -> MaterializedP1Candidate:
    """Path A: exact retained #158 weightsをstrict loadしてdigest verifyする。"""
    import torch

    verify_contract_identity()
    verify_locked_q_protocol_delta()
    verify_p1_serving_contract()
    path = Path(weights_path)
    if not path.is_file():
        raise P1CandidateError(
            "the exact retained #158 P1 weights file does not exist at the given "
            "path; without it this run is P1 GATE B EVIDENCE BLOCKED unless the "
            "exact deterministic reconstruction source is available"
        )
    state_dict = torch.load(path, weights_only=True, map_location="cpu")
    model = create_p1_model()
    try:
        model.load_state_dict(state_dict, strict=True)
    except RuntimeError as error:
        raise P1CandidateError(
            "the retained state_dict does not match the locked P1 model"
        ) from error
    _finalize_model(model)
    digest = model_weights_digest(model)
    _require_exact_digest(digest, expected)
    return MaterializedP1Candidate(
        model=model,
        canonical_model_weights_digest=digest,
        selected_epoch=LOCKED_SELECTED_EPOCH,
        materialization_source=MATERIALIZATION_RETAINED_WEIGHTS,
        epoch_history=(),
    )


def reconstruct_p1_candidate(
    split_tensors: dict,
    *,
    expected: ExpectedCandidateIdentities = LOCKED_P1_CANDIDATE,
) -> MaterializedP1Candidate:
    """Path B: `#158`と完全に同じprotocolでP1 candidateをreconstructする。

    training semantics、seed、epoch数、checkpoint selectionは`#140` / `#158`の
    値をそのまま使う（`train_p1_q_model()`）。reconstructedなdigestがexact
    一致しなければfail closedし、再試行しない。
    """
    verify_contract_identity()
    verify_locked_q_protocol_delta()
    verify_p1_serving_contract()
    require_p1_split_tensors(split_tensors)
    run = train_p1_q_model(split_tensors)
    model = _finalize_model(run.model)
    if run.selected_epoch != LOCKED_SELECTED_EPOCH:
        raise P1CandidateError(
            "the reconstruction did not stop at the locked fixed_final_iteration "
            f"epoch {LOCKED_SELECTED_EPOCH}"
        )
    digest = model_weights_digest(model)
    _require_exact_digest(digest, expected)
    return MaterializedP1Candidate(
        model=model,
        canonical_model_weights_digest=digest,
        selected_epoch=run.selected_epoch,
        materialization_source=MATERIALIZATION_RECONSTRUCTION,
        epoch_history=run.history,
    )


# --- Serving checkpoint ---------------------------------------------------


def _runtime_block() -> dict[str, object]:
    import torch

    return {
        "device": "cpu",
        "torch_version": torch.__version__,
        "torch_threads": TORCH_THREADS,
    }


def _require_supported_indices(supported_indices) -> list[int]:
    if type(supported_indices) not in (list, tuple, frozenset, set):
        raise P1CandidateError("supported_indices must be a collection of ints")
    if any(type(index) is not int for index in supported_indices):
        raise P1CandidateError("supported_indices must contain only exact ints")
    indices = sorted(int(index) for index in supported_indices)
    if not indices:
        raise P1CandidateError("the retained TRAIN support set must not be empty")
    if len(set(indices)) != len(indices):
        raise P1CandidateError("supported_indices must not repeat an index")
    if any(not 0 <= index < VOCABULARY_SIZE for index in indices):
        raise P1CandidateError("supported_indices carries an out-of-range index")
    return indices


def build_serving_manifest(
    candidate: MaterializedP1Candidate,
    *,
    supported_indices,
    weights_bytes: int,
    weights_sha256: str,
    expected: ExpectedCandidateIdentities = LOCKED_P1_CANDIDATE,
) -> dict[str, object]:
    """serving checkpoint manifestを組み立てる。

    `real_candidate_materialization`は自己申告ではなく、`expected`がlocked
    constantとexact一致するかどうかから導出する。
    """
    if not isinstance(candidate, MaterializedP1Candidate):
        raise TypeError("candidate must be a MaterializedP1Candidate")
    if not isinstance(expected, ExpectedCandidateIdentities):
        raise TypeError("expected must be an ExpectedCandidateIdentities")
    indices = _require_supported_indices(supported_indices)
    digest = support_set_identity(indices)
    if digest != expected.support_set_digest:
        raise P1CandidateError(
            "the serving support set does not match the retained TRAIN support "
            "digest; the support restriction semantics are locked"
        )
    binding = candidate_binding_document(
        canonical_model_weights_digest=candidate.canonical_model_weights_digest,
        support_set_digest=digest,
    )
    return {
        "checkpoint_schema_version": SERVING_CHECKPOINT_SCHEMA_VERSION,
        "source_issue": SOURCE_ISSUE,
        "predecessor_issue": PREDECESSOR_ISSUE,
        "parent_issue": PARENT_ISSUE,
        "protocol_id": PROTOCOL_ID,
        "candidate_identity": candidate_logical_identity(binding),
        "candidate_binding": binding,
        "canonical_model_weights_digest": (candidate.canonical_model_weights_digest),
        "weights_sha256": weights_sha256,
        "weights_bytes": weights_bytes,
        "source_dataset_identity": expected.source_dataset_identity,
        "p1_feature": p1_feature_block(),
        "action_vocabulary": vocabulary_block(),
        "supported_indices": indices,
        "supported_indices_digest": digest,
        "model": p1_model_block(),
        "training": p1_training_block(),
        "parameter_count": P1_EXPECTED_PARAMETER_COUNT,
        "selected_epoch": candidate.selected_epoch,
        "hybrid_activation": hybrid_activation_block(),
        "fallback_policy": fallback_policy_block(),
        "materialization_source": candidate.materialization_source,
        "expected_identities": expected.to_document(),
        "real_candidate_materialization": expected == LOCKED_P1_CANDIDATE,
        "source_revisions": execution_provenance_to_dict(
            collect_execution_provenance()
        ),
        "runtime": _runtime_block(),
        "strength_claim": None,
    }


def save_p1_serving_checkpoint(
    destination: str | Path,
    candidate: MaterializedP1Candidate,
    *,
    supported_indices,
    expected: ExpectedCandidateIdentities = LOCKED_P1_CANDIDATE,
) -> "LoadedP1ServingCheckpoint":
    """serving checkpointをstagingで組み立ててからwrite-onceで公開する。

    既存bundleを上書きせず、公開後は必ずstrict readbackした結果を返す。
    """
    import torch

    destination = Path(destination)
    if destination.exists():
        raise FileExistsError("serving checkpoint destination already exists")
    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(
        mkdtemp(prefix=f".{destination.name}-staging-", dir=destination.parent)
    )
    published = False
    try:
        weights_path = staging / WEIGHTS_FILENAME
        torch.save(candidate.model.state_dict(), weights_path)
        weights = weights_path.read_bytes()
        manifest = build_serving_manifest(
            candidate,
            supported_indices=supported_indices,
            weights_bytes=len(weights),
            weights_sha256=_sha256(weights),
            expected=expected,
        )
        (staging / MANIFEST_FILENAME).write_text(
            canonical_json_text(manifest), encoding="utf-8", newline="\n"
        )
        staging.rename(destination)
        published = True
    finally:
        if not published:
            rmtree(staging, ignore_errors=True)
    return load_p1_serving_checkpoint(destination, expected=expected)


def materialize_p1_serving_checkpoint(
    candidate: MaterializedP1Candidate,
    *,
    supported_indices,
    backend: str,
    root: str | Path,
    key: str,
    expected: ExpectedCandidateIdentities = LOCKED_P1_CANDIDATE,
) -> tuple[str, "LoadedP1ServingCheckpoint"]:
    """non-ephemeral retention先へserving checkpointをwrite-onceで置く。

    retention先の判定（absolute / 実在 / temporary配下でない / Git work tree
    内でない / 未使用）は`resolve_retention_target()`をそのまま再利用する。
    宣言できるnon-ephemeral rootが無い場合は`Stage4aRetentionError`のまま
    caller（`P1 GATE B EVIDENCE BLOCKED`判定）へ返す。
    """
    target = resolve_retention_target(backend=backend, root=root, key=key)
    checkpoint = save_p1_serving_checkpoint(
        target.bundle_path,
        candidate,
        supported_indices=supported_indices,
        expected=expected,
    )
    return target.key, checkpoint


@dataclass(frozen=True, slots=True)
class LoadedP1ServingCheckpoint:
    """strict readback済みのP1 serving checkpoint。"""

    path: Path
    manifest: dict
    model: object
    supported_indices: frozenset[int]

    @property
    def candidate_identity(self) -> str:
        return self.manifest["candidate_identity"]

    @property
    def canonical_model_weights_digest(self) -> str:
        return self.manifest["canonical_model_weights_digest"]

    @property
    def real_candidate_materialization(self) -> bool:
        return self.manifest["real_candidate_materialization"]


def load_p1_serving_checkpoint(
    path: str | Path,
    *,
    expected: ExpectedCandidateIdentities = LOCKED_P1_CANDIDATE,
) -> LoadedP1ServingCheckpoint:
    """serving checkpointを読み、identity / digest / semanticsを再導出して検証する。

    documentが自己申告する値をauthorityにしない。candidate identityはbinding
    documentから、weights digestは実際にloadしたweightsから、
    `real_candidate_materialization`はlocked constantとの比較から、それぞれ
    再導出して照合する。

    torchを必要とするのはweights bytesのstate_dict loadだけであり、bundle
    layoutとmanifestのstructural checkはその前に済ませる。したがってserving
    bundleが存在しない場合は、ML extraの有無に関わらず
    `P1CandidateError`でfail closedする。
    """
    verify_contract_identity()
    verify_locked_q_protocol_delta()
    verify_p1_serving_contract()
    if not isinstance(expected, ExpectedCandidateIdentities):
        raise TypeError("expected must be an ExpectedCandidateIdentities")
    path = Path(path)
    if not path.is_dir():
        raise P1CandidateError("serving checkpoint path is not a directory")
    if {item.name for item in path.iterdir()} != {
        MANIFEST_FILENAME,
        WEIGHTS_FILENAME,
    }:
        raise P1CandidateError("serving checkpoint contains missing or extra files")

    manifest_text = (path / MANIFEST_FILENAME).read_text(encoding="utf-8")
    try:
        manifest = json.loads(manifest_text)
    except json.JSONDecodeError as error:
        raise P1CandidateError("serving manifest is not valid JSON") from error
    if type(manifest) is not dict:
        raise P1CandidateError("serving manifest must be an object")
    if manifest.get("checkpoint_schema_version") != (SERVING_CHECKPOINT_SCHEMA_VERSION):
        raise P1CandidateError("unsupported serving checkpoint schema version")
    if canonical_json_text(manifest) != manifest_text:
        raise P1CandidateError("serving manifest bytes are not canonical JSON")

    for name, value in (
        ("source_issue", SOURCE_ISSUE),
        ("predecessor_issue", PREDECESSOR_ISSUE),
        ("parent_issue", PARENT_ISSUE),
        ("protocol_id", PROTOCOL_ID),
    ):
        if manifest.get(name) != value:
            raise P1CandidateError(f"serving manifest {name} is not the locked one")
    for name, block in (
        ("p1_feature", p1_feature_block()),
        ("action_vocabulary", vocabulary_block()),
        ("model", p1_model_block()),
        ("training", p1_training_block()),
        ("hybrid_activation", hybrid_activation_block()),
        ("fallback_policy", fallback_policy_block()),
    ):
        if manifest.get(name) != block:
            raise P1CandidateError(
                f"serving manifest {name} is not the locked P1 Gate B block"
            )
    if manifest.get("parameter_count") != P1_EXPECTED_PARAMETER_COUNT:
        raise P1CandidateError("serving manifest parameter count is not the locked one")
    if manifest.get("selected_epoch") != LOCKED_SELECTED_EPOCH:
        raise P1CandidateError(
            "the served candidate is not the locked fixed_final_iteration epoch "
            f"{LOCKED_SELECTED_EPOCH} checkpoint"
        )
    if manifest.get("materialization_source") not in MATERIALIZATION_SOURCES:
        raise P1CandidateError("unknown materialization source in serving manifest")
    if manifest.get("strength_claim") is not None:
        raise P1CandidateError("a serving checkpoint must not carry a strength claim")
    if manifest.get("source_dataset_identity") != expected.source_dataset_identity:
        raise P1CandidateError(
            "the serving checkpoint was not derived from the expected source "
            "dataset identity"
        )
    if manifest.get("expected_identities") != expected.to_document():
        raise P1CandidateError(
            "the serving checkpoint records different expected identities than "
            "the ones this readback requires"
        )
    declared = manifest.get("real_candidate_materialization")
    if type(declared) is not bool:
        raise P1CandidateError("real_candidate_materialization must be an exact bool")
    if declared is not (expected == LOCKED_P1_CANDIDATE):
        raise P1CandidateError(
            "real_candidate_materialization does not follow from the recorded "
            "expected identities; it is derived from an exact comparison against "
            "the locked Issue #158 candidate, never self-declared"
        )
    parse_execution_provenance(manifest.get("source_revisions"))

    indices = _require_supported_indices(manifest.get("supported_indices") or ())
    if manifest.get("supported_indices") != indices:
        raise P1CandidateError("serving manifest supported_indices is malformed")
    if manifest.get("supported_indices_digest") != support_set_identity(indices):
        raise P1CandidateError(
            "serving manifest supported_indices does not match its own digest"
        )
    if manifest["supported_indices_digest"] != expected.support_set_digest:
        raise P1CandidateError(
            "the serving support set is not the expected retained TRAIN support set"
        )

    weights = (path / WEIGHTS_FILENAME).read_bytes()
    if len(weights) != manifest.get("weights_bytes"):
        raise P1CandidateError("serving checkpoint weights byte count differs")
    if _sha256(weights) != manifest.get("weights_sha256"):
        raise P1CandidateError("serving checkpoint weights sha256 differs")

    import torch

    state_dict = torch.load(
        path / WEIGHTS_FILENAME, weights_only=True, map_location="cpu"
    )
    model = create_p1_model()
    try:
        model.load_state_dict(state_dict, strict=True)
    except RuntimeError as error:
        raise P1CandidateError(
            "serving checkpoint state_dict does not match the locked P1 model"
        ) from error
    _finalize_model(model)
    digest = model_weights_digest(model)
    if digest != manifest.get("canonical_model_weights_digest"):
        raise P1CandidateError(
            "the canonical weights digest recomputed from the loaded model differs "
            "from the recorded one"
        )
    _require_exact_digest(digest, expected)

    binding = candidate_binding_document(
        canonical_model_weights_digest=digest,
        support_set_digest=manifest["supported_indices_digest"],
    )
    if manifest.get("candidate_binding") != binding:
        raise P1CandidateError(
            "the recorded candidate binding document is not the one this "
            "checkpoint's weights, feature, vocabulary, support set, activation "
            "semantics and fallback Policy derive"
        )
    require_candidate_identity(manifest.get("candidate_identity"), binding)
    return LoadedP1ServingCheckpoint(
        path=path,
        manifest=manifest,
        model=model,
        supported_indices=frozenset(indices),
    )


def verify_locked_candidate_contract() -> None:
    """Gate B開始前に、locked candidate contractのdriftをfail closedで確認する。"""
    verify_contract_identity()
    verify_locked_q_protocol_delta()
    verify_p1_serving_contract()
    if LOCKED_P1_CANDIDATE.source_dataset_identity != (
        LOCKED_SOURCE_IDENTITIES.dataset_identity
    ):
        raise OfflineQProtocolError(
            "the locked P1 candidate source dataset drifted from the retained "
            "#140 / #152 dataset identity"
        )
    if LOCKED_P1_CANDIDATE.support_set_digest != (
        LOCKED_SOURCE_IDENTITIES.supported_indices_digest
    ):
        raise OfflineQProtocolError(
            "the locked P1 candidate support digest drifted from the retained "
            "TRAIN support set"
        )


__all__ = [
    "CANDIDATE_BINDING_SCHEMA_VERSION",
    "CANDIDATE_IDENTITY_PREFIX",
    "LOCKED_P1_CANDIDATE",
    "LOCKED_SELECTED_EPOCH",
    "MANIFEST_FILENAME",
    "MATERIALIZATION_RECONSTRUCTION",
    "MATERIALIZATION_RETAINED_WEIGHTS",
    "MATERIALIZATION_SOURCES",
    "PARENT_ISSUE",
    "PREDECESSOR_ISSUE",
    "SERVING_CHECKPOINT_SCHEMA_VERSION",
    "SOURCE_ISSUE",
    "WEIGHTS_FILENAME",
    "ExpectedCandidateIdentities",
    "LoadedP1ServingCheckpoint",
    "MaterializedP1Candidate",
    "P1CandidateError",
    "Stage4aRetentionError",
    "build_serving_manifest",
    "candidate_binding_document",
    "candidate_logical_identity",
    "load_p1_serving_checkpoint",
    "load_retained_p1_candidate",
    "materialize_p1_serving_checkpoint",
    "reconstruct_p1_candidate",
    "require_candidate_identity",
    "save_p1_serving_checkpoint",
    "verify_locked_candidate_contract",
]
