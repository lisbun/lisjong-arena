"""Pre-execution lock for the FiniteHorizon-teacher curriculum (Issue #165).

real dataset generationを始める前に、実験条件をIssue #165へ一度だけ記録する
ためのlock documentを組み立てる。

```text
source revisions / runtime
teacher factories / classes / populations
game mode
dataset ordered seeds / TRAIN / VALIDATION / TEST split
feature identity / fingerprint（locked v1 8204 と derived P1 8241）
action vocabulary identity / fingerprint
model block / training block
support semantics
candidate identity algorithm
serving activation semantics / fallback semantics
rollout seeds / rotations / game count / workers
classification rules
dataset / candidate / result artifact locations
seed freshness preflight
```

lockはすべてlocked constantから機械的に組み立てられ、`result_exposed`は常に
`False`である。**resultを見てからここを変更するpathを持たない。**
`validate_pre_execution_lock()`は同じconstantから期待値を再導出して照合する
ので、値を書き換えたlockは通らない。

`build_pre_execution_lock()`はlive runtimeを読むためPyTorchを必要とする
（実行環境で作る）。`validate_pre_execution_lock()`と
`render_pre_execution_lock()`はdocumentだけを見るのでMLを必要としない。
"""

import hashlib
from dataclasses import dataclass

from lisjong_arena._artifact_io import canonical_json_text
from lisjong_arena.single_round_artifact import (
    collect_execution_provenance,
    execution_provenance_to_dict,
    parse_execution_provenance,
)

from .artifact import feature_block, vocabulary_block
from .errors import OfflineQProtocolError
from .fh_curriculum import (
    CLASSIFICATION_RULE,
    CURRICULUM_LIMITATIONS,
    INTERPRETATION_BOUNDARY,
    LOCKED_UNCHANGED_AXES,
    NEXT_ACTION_BOUNDARY,
    PARENT_ISSUE,
    PREDECESSOR_ISSUES,
    PRIMARY_CHANGED_AXIS,
    RETENTION_BACKEND,
    SOURCE_ISSUE,
    CurriculumArm,
    CurriculumOutcome,
    arm_block,
    candidate_retention_block,
    check_seed_freshness,
    dataset_protocol_block,
    dataset_retention_block,
    result_retention_block,
    reward_semantics_block,
    rollout_plan_block,
    teacher_block,
    transition_semantics_block,
)
from .fh_curriculum_candidate import (
    CANDIDATE_BINDING_SCHEMA_VERSION,
    CANDIDATE_IDENTITY_PREFIX,
    CANDIDATE_SCHEMA_VERSION,
    CHECKPOINT_SELECTION,
    SELECTED_EPOCH,
)
from .fh_curriculum_dataset import (
    CURRICULUM_DATASET_SCHEMA_VERSION,
    experiment_block,
)
from .fh_curriculum_rollout import ROLLOUT_SCHEMA_VERSION
from .p1_features import p1_feature_block
from .p1_q_training import (
    P1_EXPECTED_PARAMETER_COUNT,
    p1_model_block,
    p1_training_block,
)
from .p1_serving import fallback_policy_block, hybrid_activation_block

LOCK_SCHEMA_VERSION = "arena-learned-policy-finite-horizon-curriculum-execution-lock-v1"

LOCK_FIELDS = {
    "lock_schema_version",
    "experiment",
    "source_issue",
    "predecessor_issues",
    "parent_issue",
    "primary_changed_axis",
    "locked_unchanged_axes",
    "arms",
    "dataset_protocol",
    "transition_semantics",
    "reward_semantics",
    "feature",
    "derived_feature",
    "action_vocabulary",
    "model",
    "training",
    "support_semantics",
    "candidate_identity_algorithm",
    "serving_semantics",
    "rollout_plan",
    "classification_rule",
    "outcomes",
    "limitations",
    "interpretation_boundary",
    "next_action_boundary",
    "artifact_locations",
    "artifact_schemas",
    "seed_freshness",
    "provenance",
    "runtime",
    "result_exposed",
    "lock_identity",
}
_LOCATION_FIELDS = {
    "retention_backend",
    "dataset_control",
    "dataset_curriculum",
    "candidate_control",
    "candidate_curriculum",
    "result_artifact",
    "retention_keys",
}
RUNTIME_FIELDS = {
    "python_version",
    "torch_version",
    "riichienv_version",
    "platform",
    "device",
    "torch_threads",
    "deterministic_algorithms",
    "free_threaded",
}


class CurriculumLockError(OfflineQProtocolError):
    """pre-execution lockの契約違反。"""


def _error(message: str) -> CurriculumLockError:
    return CurriculumLockError(message)


@dataclass(frozen=True, slots=True)
class CurriculumArtifactLocations:
    """operatorが宣言するnon-ephemeral artifactのlocation。

    ここに書くのはlockする「どこへ置くか」であり、artifact本体ではない。
    生成物はGitへcommitしない。
    """

    dataset_control: str
    dataset_curriculum: str
    candidate_control: str
    candidate_curriculum: str
    result_artifact: str
    retention_backend: str = RETENTION_BACKEND

    def __post_init__(self) -> None:
        for name in (
            "dataset_control",
            "dataset_curriculum",
            "candidate_control",
            "candidate_curriculum",
            "result_artifact",
            "retention_backend",
        ):
            value = getattr(self, name)
            if type(value) is not str or not value.strip():
                raise CurriculumLockError(f"{name} must be a non-empty string")
        declared = (
            self.dataset_control,
            self.dataset_curriculum,
            self.candidate_control,
            self.candidate_curriculum,
            self.result_artifact,
        )
        if len(set(declared)) != len(declared):
            raise CurriculumLockError(
                "each locked artifact must have its own location; Arm Y and Arm F "
                "never share an artifact path"
            )

    def to_document(self) -> dict[str, object]:
        return {
            "retention_backend": self.retention_backend,
            "dataset_control": self.dataset_control,
            "dataset_curriculum": self.dataset_curriculum,
            "candidate_control": self.candidate_control,
            "candidate_curriculum": self.candidate_curriculum,
            "result_artifact": self.result_artifact,
            "retention_keys": {
                "dataset_control": dataset_retention_block(CurriculumArm.CONTROL),
                "dataset_curriculum": dataset_retention_block(CurriculumArm.CURRICULUM),
                "candidate_control": candidate_retention_block(CurriculumArm.CONTROL),
                "candidate_curriculum": candidate_retention_block(
                    CurriculumArm.CURRICULUM
                ),
                "result_artifact": result_retention_block(),
            },
        }


def arms_block() -> dict[str, object]:
    """両armのidentity / teacher factory / teacher classをlockする。"""
    return {
        arm.value: {**arm_block(arm), "teacher": teacher_block(arm)}
        for arm in CurriculumArm
    }


def support_semantics_block() -> dict[str, object]:
    """support setの導出規則と禁止事項をlockする。"""
    return {
        "rule": "TRAIN behavior action indices of the arm's own dataset",
        "scope": "per arm; derived from that arm's own TRAIN split only",
        "cross_arm_sharing": False,
        "historical_substitution": False,
        "serving_gate": "train-support-complete-legal-discard-indices",
        "persisted": [
            "supported_indices",
            "supported_indices_digest",
            "support_size",
            "support_coverage",
        ],
    }


def candidate_identity_algorithm_block() -> dict[str, object]:
    """candidate logical identityの導出algorithmをlockする。"""
    return {
        "identity_prefix": CANDIDATE_IDENTITY_PREFIX,
        "binding_schema_version": CANDIDATE_BINDING_SCHEMA_VERSION,
        "algorithm": (
            "sha256 of the canonical JSON serialization of the candidate binding "
            "document, prefixed by the identity prefix"
        ),
        "binding_fields": [
            "binding_schema_version",
            "arm",
            "teacher",
            "source_dataset_identity",
            "canonical_model_weights_digest",
            "p1_feature",
            "action_vocabulary",
            "support_set_digest",
            "model",
            "training",
            "hybrid_activation",
            "fallback_policy",
            "source_revisions",
        ],
        "weights_only_identity": False,
        "selected_epoch": SELECTED_EPOCH,
        "checkpoint_selection": CHECKPOINT_SELECTION,
        "parameter_count": P1_EXPECTED_PARAMETER_COUNT,
    }


def serving_semantics_block() -> dict[str, object]:
    """両armで同一のhybrid activation / fallback semanticsをlockする。"""
    return {
        "hybrid_activation": hybrid_activation_block(),
        "fallback_policy": fallback_policy_block(),
        "same_across_arms": True,
        "curriculum_arm_fallback_is_finite_horizon": False,
        "note": (
            "the Arm F training teacher is finite-horizon while its serving "
            "fallback stays yakuhai-call; changing the fallback too would move "
            "the serving axis as well and break the controlled comparison"
        ),
    }


def artifact_schemas_block() -> dict[str, object]:
    return {
        "dataset": CURRICULUM_DATASET_SCHEMA_VERSION,
        "candidate": CANDIDATE_SCHEMA_VERSION,
        "rollout_result": ROLLOUT_SCHEMA_VERSION,
        "historical_dataset_schema_changed": False,
    }


def runtime_block() -> dict[str, object]:
    """live runtimeを読む。値を捏造しない。"""
    import platform
    import sysconfig

    import torch

    from lisjong_arena.learned_policy_offline_q.bc_training import (
        configure_deterministic_runtime,
    )

    configure_deterministic_runtime()
    import importlib.metadata

    return {
        "python_version": platform.python_version(),
        "torch_version": str(torch.__version__),
        "riichienv_version": importlib.metadata.version("riichienv"),
        "platform": platform.platform(),
        "device": "cpu",
        "torch_threads": torch.get_num_threads(),
        "deterministic_algorithms": torch.are_deterministic_algorithms_enabled(),
        "free_threaded": bool(sysconfig.get_config_var("Py_GIL_DISABLED")),
    }


def lock_identity(document: dict) -> str:
    """`lock_identity`自身を除いたcanonical lock bytesのdigest。"""
    payload = {
        name: value for name, value in document.items() if name != "lock_identity"
    }
    return hashlib.sha256(canonical_json_text(payload).encode("utf-8")).hexdigest()


def build_pre_execution_lock(
    *,
    locations: CurriculumArtifactLocations,
    provenance: dict | None = None,
    runtime: dict | None = None,
) -> dict[str, object]:
    """real dataset generation前にlockするdocumentを組み立てる。

    `provenance` / `runtime`はtestがdeterministicな値を渡すためのseamであり、
    既定では実行環境から読む。
    """
    if not isinstance(locations, CurriculumArtifactLocations):
        raise TypeError("locations must be a CurriculumArtifactLocations")
    freshness = check_seed_freshness()
    if not freshness["fresh"]:
        raise _error(
            "the locked seed plan is not fresh; resolve it as SEED PLAN "
            "REFORMULATE before locking, never after result exposure"
        )
    document: dict[str, object] = {
        "lock_schema_version": LOCK_SCHEMA_VERSION,
        "experiment": experiment_block(),
        "source_issue": SOURCE_ISSUE,
        "predecessor_issues": list(PREDECESSOR_ISSUES),
        "parent_issue": PARENT_ISSUE,
        "primary_changed_axis": PRIMARY_CHANGED_AXIS,
        "locked_unchanged_axes": list(LOCKED_UNCHANGED_AXES),
        "arms": arms_block(),
        "dataset_protocol": dataset_protocol_block(),
        "transition_semantics": transition_semantics_block(),
        "reward_semantics": reward_semantics_block(),
        "feature": feature_block(),
        "derived_feature": p1_feature_block(),
        "action_vocabulary": vocabulary_block(),
        "model": p1_model_block(),
        "training": p1_training_block(),
        "support_semantics": support_semantics_block(),
        "candidate_identity_algorithm": candidate_identity_algorithm_block(),
        "serving_semantics": serving_semantics_block(),
        "rollout_plan": rollout_plan_block(),
        "classification_rule": dict(CLASSIFICATION_RULE),
        "outcomes": [outcome.value for outcome in CurriculumOutcome],
        "limitations": list(CURRICULUM_LIMITATIONS),
        "interpretation_boundary": {
            "positive_claim_limit": INTERPRETATION_BOUNDARY["positive_claim_limit"],
            "forbidden_claims": list(INTERPRETATION_BOUNDARY["forbidden_claims"]),
            "negative_claim_limit": INTERPRETATION_BOUNDARY["negative_claim_limit"],
        },
        "next_action_boundary": NEXT_ACTION_BOUNDARY,
        "artifact_locations": locations.to_document(),
        "artifact_schemas": artifact_schemas_block(),
        "seed_freshness": freshness,
        "provenance": (
            execution_provenance_to_dict(collect_execution_provenance())
            if provenance is None
            else dict(provenance)
        ),
        "runtime": runtime_block() if runtime is None else dict(runtime),
        "result_exposed": False,
        "lock_identity": None,
    }
    return validate_pre_execution_lock(
        {**document, "lock_identity": lock_identity(document)}
    )


def validate_pre_execution_lock(document: object) -> dict:
    """lock documentをlocked constantから再導出してfail closedに検証する。"""
    if type(document) is not dict:
        raise _error("the execution lock must be an object")
    if set(document) != LOCK_FIELDS:
        missing = sorted(LOCK_FIELDS - set(document))
        extra = sorted(set(document) - LOCK_FIELDS)
        raise _error(f"execution lock has missing {missing!r} or extra {extra!r}")
    for name, expected in (
        ("lock_schema_version", LOCK_SCHEMA_VERSION),
        ("experiment", experiment_block()),
        ("source_issue", SOURCE_ISSUE),
        ("predecessor_issues", list(PREDECESSOR_ISSUES)),
        ("parent_issue", PARENT_ISSUE),
        ("primary_changed_axis", PRIMARY_CHANGED_AXIS),
        ("locked_unchanged_axes", list(LOCKED_UNCHANGED_AXES)),
        ("arms", arms_block()),
        ("dataset_protocol", dataset_protocol_block()),
        ("transition_semantics", transition_semantics_block()),
        ("reward_semantics", reward_semantics_block()),
        ("feature", feature_block()),
        ("derived_feature", p1_feature_block()),
        ("action_vocabulary", vocabulary_block()),
        ("model", p1_model_block()),
        ("training", p1_training_block()),
        ("support_semantics", support_semantics_block()),
        ("candidate_identity_algorithm", candidate_identity_algorithm_block()),
        ("serving_semantics", serving_semantics_block()),
        ("rollout_plan", rollout_plan_block()),
        ("classification_rule", dict(CLASSIFICATION_RULE)),
        ("outcomes", [outcome.value for outcome in CurriculumOutcome]),
        ("limitations", list(CURRICULUM_LIMITATIONS)),
        (
            "interpretation_boundary",
            {
                "positive_claim_limit": INTERPRETATION_BOUNDARY["positive_claim_limit"],
                "forbidden_claims": list(INTERPRETATION_BOUNDARY["forbidden_claims"]),
                "negative_claim_limit": INTERPRETATION_BOUNDARY["negative_claim_limit"],
            },
        ),
        ("next_action_boundary", NEXT_ACTION_BOUNDARY),
        ("artifact_schemas", artifact_schemas_block()),
    ):
        if document[name] != expected:
            raise _error(f"execution lock {name} is not the locked one")
    if document["result_exposed"] is not False:
        raise _error(
            "a pre-execution lock is created before any result exists; it never "
            "records an exposed result"
        )
    locations = document["artifact_locations"]
    if type(locations) is not dict or set(locations) != _LOCATION_FIELDS:
        raise _error("execution lock artifact_locations fields are invalid")
    declared = [
        locations[name]
        for name in (
            "dataset_control",
            "dataset_curriculum",
            "candidate_control",
            "candidate_curriculum",
            "result_artifact",
        )
    ]
    if any(type(value) is not str or not value.strip() for value in declared):
        raise _error("each locked artifact location must be a non-empty string")
    if len(set(declared)) != len(declared):
        raise _error("Arm Y and Arm F never share an artifact location")
    if locations["retention_keys"] != {
        "dataset_control": dataset_retention_block(CurriculumArm.CONTROL),
        "dataset_curriculum": dataset_retention_block(CurriculumArm.CURRICULUM),
        "candidate_control": candidate_retention_block(CurriculumArm.CONTROL),
        "candidate_curriculum": candidate_retention_block(CurriculumArm.CURRICULUM),
        "result_artifact": result_retention_block(),
    }:
        raise _error("execution lock retention keys are not the locked ones")

    freshness = document["seed_freshness"]
    if type(freshness) is not dict or freshness.get("fresh") is not True:
        raise _error(
            "a pre-execution lock records a fresh seed plan; a collision is "
            "resolved as SEED PLAN REFORMULATE before locking"
        )
    if freshness.get("result_exposed") is not False:
        raise _error("the recorded seed freshness preflight is not a pre-exposure one")
    parse_execution_provenance(document["provenance"])
    runtime = document["runtime"]
    if type(runtime) is not dict or set(runtime) != RUNTIME_FIELDS:
        raise _error("execution lock runtime fields are invalid")
    for name in ("python_version", "torch_version", "riichienv_version", "platform"):
        if type(runtime[name]) is not str or not runtime[name].strip():
            raise _error(f"execution lock runtime {name} must be a non-empty string")
    if runtime["device"] != "cpu":
        raise _error("Issue #165 locks a CPU-only runtime")
    if type(runtime["torch_threads"]) is not int or runtime["torch_threads"] < 1:
        raise _error("execution lock runtime torch_threads must be a positive int")
    for name in ("deterministic_algorithms", "free_threaded"):
        if type(runtime[name]) is not bool:
            raise _error(f"execution lock runtime {name} must be an exact bool")
    if runtime["deterministic_algorithms"] is not True:
        raise _error("Issue #165 locks deterministic torch algorithms")
    if runtime["free_threaded"] is not False:
        raise _error(
            "the free-threaded build is out of scope until compatibility is "
            "verified separately"
        )
    if document["lock_identity"] != lock_identity(document):
        raise _error(
            "lock_identity is not the digest of this lock document; the lock was "
            "edited after the fact"
        )
    return document


def render_pre_execution_lock(document: dict) -> str:
    """lock documentを、Issue #165へそのまま貼れるMarkdownへ整形する。

    値はdocumentからのみ読む。ここで新しい条件を作らない。
    """
    lock = validate_pre_execution_lock(document)
    provenance = lock["provenance"]
    runtime = lock["runtime"]
    arms = lock["arms"]
    protocol = lock["dataset_protocol"]
    rollout = lock["rollout_plan"]
    training = lock["training"]
    locations = lock["artifact_locations"]
    lines = [
        "## Issue #165 pre-execution lock",
        "",
        "```text",
        f"lock schema            {lock['lock_schema_version']}",
        f"lock identity          {lock['lock_identity']}",
        f"result exposed         {lock['result_exposed']}",
        "",
        f"arena revision         {provenance['lisjong_arena_revision']}",
        f"lisjong revision       {provenance['lisjong_revision']}",
        f"engine revision        {provenance['lisjong_engine_revision']}",
        f"riichienv              {runtime['riichienv_version']}",
        f"python                 {runtime['python_version']}",
        f"pytorch                {runtime['torch_version']}",
        f"device / threads       {runtime['device']} / {runtime['torch_threads']}",
        f"deterministic          {runtime['deterministic_algorithms']}",
        "",
        f"primary changed axis   {lock['primary_changed_axis']}",
        "",
    ]
    for arm in CurriculumArm:
        entry = arms[arm.value]
        teacher = entry["teacher"]
        lines.extend(
            [
                f"arm {arm.value} ({entry['role']})",
                f"  identity             {entry['arm_identity']}",
                f"  teacher identity     {teacher['identity']}",
                f"  teacher class        {teacher['policy_class']}",
                f"  teacher factory      {teacher['factory']}",
                f"  teacher population   {teacher['population']}",
            ]
        )
    lines.extend(
        [
            "",
            f"game mode              {protocol['game_mode']}",
            f"dataset seeds          {protocol['ordered_seeds'][0]}"
            f"..{protocol['ordered_seeds'][-1]}"
            f" ({protocol['hanchan_count']} hanchan per arm)",
            f"TRAIN                  {protocol['train_seeds'][0]}"
            f"..{protocol['train_seeds'][-1]} ({len(protocol['train_seeds'])})",
            f"VALIDATION             {protocol['validation_seeds'][0]}"
            f"..{protocol['validation_seeds'][-1]}"
            f" ({len(protocol['validation_seeds'])})",
            f"TEST                   {protocol['test_seeds'][0]}"
            f"..{protocol['test_seeds'][-1]} ({len(protocol['test_seeds'])})",
            f"split unit             {protocol['split_unit']}",
            "",
            f"feature identity       {lock['feature']['semantics_id']}",
            f"feature fingerprint    {lock['feature']['schema_fingerprint']}",
            f"derived feature        {lock['derived_feature']['semantics_id']}",
            f"derived fingerprint    {lock['derived_feature']['schema_fingerprint']}",
            f"derived dimension      {lock['derived_feature']['dimension']}",
            f"vocabulary identity    {lock['action_vocabulary']['version']}",
            f"vocabulary fingerprint {lock['action_vocabulary']['fingerprint']}",
            "",
            f"model                  {lock['model']['input_dimension']} -> "
            f"{lock['model']['hidden_width']} {lock['model']['activation']} -> "
            f"{lock['model']['output_dimension']}",
            f"parameter count        {lock['model']['parameter_count']}",
            f"loss / gamma           {training['loss']} / {training['gamma']}",
            f"optimizer / lr         {training['optimizer']} / "
            f"{training['learning_rate']}",
            f"weight decay / batch   {training['weight_decay']} / "
            f"{training['batch_size']}",
            f"target sync            {training['target_sync_cadence']}",
            f"maximum epochs         {training['maximum_epochs']}",
            f"training seed          {training['training_seed']}",
            f"dataloader seed        {training['dataloader_seed']}",
            f"checkpoint selection   {training['checkpoint_selection']}",
            "",
            f"support rule           {lock['support_semantics']['rule']}",
            f"support scope          {lock['support_semantics']['scope']}",
            "cross-arm support      forbidden",
            "historical support     forbidden",
            "",
            f"candidate identity     {lock['candidate_identity_algorithm']['algorithm']}",
            f"identity prefix        "
            f"{lock['candidate_identity_algorithm']['identity_prefix']}",
            "",
            "serving activation     "
            f"{lock['serving_semantics']['hybrid_activation']['eligibility']}"
            " + train-support-complete",
            "serving fallback       "
            f"{lock['serving_semantics']['fallback_policy']['identity']}"
            " (both arms)",
            "",
            f"rollout seeds          {rollout['ordered_seeds'][0]}"
            f"..{rollout['ordered_seeds'][-1]}"
            f" ({rollout['seed_block_count']} seed blocks)",
            f"rotations / seed       {rollout['rotation_count']}",
            f"total games            {rollout['game_count']}",
            f"rollout game mode      {rollout['game_mode']}",
            f"workers                {rollout['max_workers']}",
            f"formal TEST            {rollout['formal_test']}",
            f"role                   {rollout['role']}",
            "",
            "classification",
            f"  positive             {lock['classification_rule']['positive']}",
            f"  negative             {lock['classification_rule']['negative']}",
            f"  inconclusive         {lock['classification_rule']['inconclusive']}",
            f"  blocked              {CurriculumOutcome.EVIDENCE_BLOCKED.value}",
            f"  invalid              {CurriculumOutcome.STOP_INVALID.value}",
            "  secondary metrics    never alter the classification",
            "",
            f"retention backend      {locations['retention_backend']}",
            f"dataset Arm Y          {locations['dataset_control']}",
            f"dataset Arm F          {locations['dataset_curriculum']}",
            f"candidate Arm Y        {locations['candidate_control']}",
            f"candidate Arm F        {locations['candidate_curriculum']}",
            f"result artifact        {locations['result_artifact']}",
            "",
            "seed freshness         "
            f"{'fresh' if lock['seed_freshness']['fresh'] else 'COLLISION'}",
            "```",
            "",
            "この lock 以降、result を見て seed / epoch / feature / support rule / "
            "reward / classification を変更しない。",
        ]
    )
    return "\n".join(lines) + "\n"


__all__ = [
    "LOCK_FIELDS",
    "LOCK_SCHEMA_VERSION",
    "RUNTIME_FIELDS",
    "CurriculumArtifactLocations",
    "CurriculumLockError",
    "arms_block",
    "artifact_schemas_block",
    "build_pre_execution_lock",
    "candidate_identity_algorithm_block",
    "lock_identity",
    "render_pre_execution_lock",
    "runtime_block",
    "serving_semantics_block",
    "support_semantics_block",
    "validate_pre_execution_lock",
]
