"""Arena #167のpre-exposure execution lock。

execution lockはretained artifact readback、E160 training、result assemblyの
**すべて** のloaderへ明示的に渡すreceiptである。artifactはlock identityだけを
持ち、lock本体を自分の中へ埋め込まない。resultが自分で実行revisionや比較
semanticsを選び直すことはできない。

lock identityはE160 trainingを開始する前にIssue #167へ記録する。execution lock
作成後にexperimental semanticsを変更しない。

## lockする内容

```text
source revision            #167 execution Arena revision / pinned lisjong / engine
#150 artifact identities   execution lock / population / raw corpus / dataset / S64 weights
#157 artifact identities   execution lock / result / E80 weights / E80 full loss history digest
population identity        e59410ed…
dataset identity           fbfa8ad7…
TRAIN / VALIDATION         360..423 (64) / 424..439 (16) / formal TEST none
model / training config    #157 E80 training lockのmax_epochsだけを160にしたもの
max_epochs                 80 -> 160
patience                   6
seed / dataloader seed     0 / 0
deterministic settings     CPU / torch threads 1 / deterministic algorithms
comparison semantics       whole-hanchan paired MAE delta / prefix-80 determinism gate
bootstrap constants        10,000 replicates / seed 148 / 2.5・97.5 percentile
classification rule        CLEAR BUDGET IMPROVEMENT / REGRESSION / INCONCLUSIVE
exhaustive outcomes        5つ / no-E320 rule
selection exposure         cumulative uses 3
```

## runtime binding

E160のdeterminism gateは、#157 E80 trainingと同じnumeric runtimeで走ることを
前提にする。したがってlockは#157 execution lockのruntimeを`predecessor_runtime`
として持ち、live runtimeがnumericに関係するfield（CPython / torch / riichienv /
device / thread数 / deterministic algorithms / free-threaded）でそれと一致する
ことを要求する。#157 lock自身が同じ要求で#150 runtimeへbindしているので、この
一致は#150まで推移する。`platform` stringだけは一致を必須にせず、両方を記録して
`platform_matches_predecessor`で明示する。determinism gateがこの前提のempirical
な確認であり、mismatchはtoleranceで緩めずに`STOP / INVALID`とする。

`lisjong_arena` revisionは#157実行時 (`ef20aa9b…`) と異なる。本childはPhase 8 /
Stage 3 Entry Gate / Phase 10 / #157のtraining pathを変更せず新しいpackageを足す
だけであり、その同一性はdeterminism gateが実測で確認する。
"""

from .protocol import (
    ARMS,
    BASELINE_ARENA_REVISION,
    BASELINE_MAX_EPOCHS,
    BOOTSTRAP,
    CLASSIFICATIONS,
    DECISION_RULE,
    DETERMINISM_PREFIX_EPOCHS,
    ENGINE_REVISION,
    EXECUTION_DECISION,
    HARD_CAP_EPOCHS,
    INTERPRETATION_BOUNDARY,
    LISJONG_REVISION,
    NO_EXTENSION_RULE,
    OUTCOMES,
    PATIENCE,
    PRIMARY_AXIS,
    RETAINED_VALIDATION_HANCHAN,
    RETRY_RULE,
    RIICHIENV_VERSION,
    RULES,
    SATURATION_MAX_EPOCHS,
    SCHEMA,
    SELECTION_EXPOSURE,
    TORCH_VERSIONS,
    SaturationError,
    assert_bootstrap_constants_are_locked,
    baseline_training_lock,
    digest,
    exact,
    identity,
    saturation_training_lock,
)
from .retained import load_predecessor_lock, retained_value

LOCK_FIELDS = (
    "schema",
    "baseline_arena_revision",
    "retained",
    "predecessor_runtime",
    "platform_matches_predecessor",
    "baseline_training_lock",
    "saturation_training_lock",
    "comparison",
    "provenance",
    "runtime",
    "artifact_audit",
    "result_exposed",
    "execution_decision",
)
RUNTIME_FIELDS = (
    "python",
    "torch",
    "riichienv",
    "platform",
    "device",
    "torch_threads",
    "deterministic_algorithms",
    "free_threaded",
)
NUMERIC_RUNTIME_FIELDS = (
    "python",
    "torch",
    "riichienv",
    "device",
    "torch_threads",
    "deterministic_algorithms",
    "free_threaded",
)
"""determinism gateの前提として#157 runtimeとの一致を要求するfield。

`platform` stringは一致を必須にせず、`platform_matches_predecessor`として記録
する。
"""

DETERMINISM_GATE_RULE = (
    "E160.loss_history[0:80] must equal the retained #157 E80 loss_history exactly, "
    "compared on canonical bytes rather than a numeric tolerance; a mismatch is "
    "STOP / INVALID and is treated as a training reproducibility defect instead of "
    "being rescued by retraining E80, by changing the seed or runtime, by repeating "
    "E160 or by loosening the comparison"
)
RESUME_RULE = (
    "E160 is trained from epoch 1 under the current deterministic training "
    "semantics; it does not resume from the retained E80 checkpoint because the "
    "training artifact contract does not lock optimizer state or RNG continuation "
    "state, and this child does not add a resume contract"
)
CLASSIFICATION_RULE = (
    "interval lower > 0 -> CLEAR BUDGET IMPROVEMENT; interval upper < 0 -> CLEAR "
    "BUDGET REGRESSION; otherwise INCONCLUSIVE"
)


def comparison_semantics() -> dict[str, object]:
    """result exposure前にlockする比較semantics一式。"""
    return {
        "arms": list(ARMS),
        "primary_axis": PRIMARY_AXIS,
        "baseline_max_epochs": BASELINE_MAX_EPOCHS,
        "saturation_max_epochs": SATURATION_MAX_EPOCHS,
        "hard_cap_epochs": HARD_CAP_EPOCHS,
        "patience": PATIENCE,
        "unit": "whole VALIDATION hanchan",
        "hanchan": RETAINED_VALIDATION_HANCHAN,
        "statistic": BOOTSTRAP["statistic"],
        "positive_direction": "a larger epoch budget is better",
        "bootstrap": assert_bootstrap_constants_are_locked(),
        "classifications": list(CLASSIFICATIONS),
        "classification_rule": CLASSIFICATION_RULE,
        "determinism_gate": DETERMINISM_GATE_RULE,
        "determinism_prefix_epochs": DETERMINISM_PREFIX_EPOCHS,
        "resume_rule": RESUME_RULE,
        "outcomes": list(OUTCOMES),
        "decision_rule": DECISION_RULE,
        "retry_rule": RETRY_RULE,
        "no_extension_rule": NO_EXTENSION_RULE,
        "interpretation_boundary": INTERPRETATION_BOUNDARY,
        "selection_exposure": dict(SELECTION_EXPOSURE),
        "budget": {
            "saturation_arm_trainings": 1,
            "baseline_arm_trainings": 0,
            "new_hanchan": 0,
            "new_seed": 0,
            "new_corpus_generation": 0,
            "formal_test_exposure": 0,
        },
    }


def validate_runtime(runtime: object, predecessor_runtime: object) -> None:
    """live runtimeをlocked pinsと#157 numeric runtimeへ固定する。"""
    for value, name in (
        (runtime, "runtime"),
        (predecessor_runtime, "predecessor runtime"),
    ):
        if type(value) is not dict or set(value) != set(RUNTIME_FIELDS):
            raise SaturationError(f"{name} fields are not exact")
        if (
            type(value["python"]) is not str
            or not value["python"].startswith("3.14.")
            or value["torch"] not in TORCH_VERSIONS
            or type(value["platform"]) is not str
            or not value["platform"].strip()
        ):
            raise SaturationError(f"unsupported locked {name}")
        for locked_name, locked in (
            ("riichienv", RIICHIENV_VERSION),
            ("device", "cpu"),
            ("torch_threads", 1),
            ("deterministic_algorithms", True),
            ("free_threaded", False),
        ):
            exact(value[locked_name], locked, f"{name} {locked_name}")
    for name in NUMERIC_RUNTIME_FIELDS:
        exact(
            runtime[name],
            predecessor_runtime[name],
            f"live {name} against the retained #157 runtime",
        )


def validate_lock(lock: object) -> dict[str, object]:
    """execution lockをlocked artifacts / single axis / comparison semanticsへ固定する。"""
    if type(lock) is not dict or set(lock) != set(LOCK_FIELDS):
        raise SaturationError("execution lock fields are not exact")
    exact(lock["schema"], SCHEMA + "/execution-lock", "lock schema")
    exact(lock["baseline_arena_revision"], BASELINE_ARENA_REVISION, "preflight main")
    exact(lock["retained"], retained_value(), "retained artifact lock")
    exact(
        lock["baseline_training_lock"],
        baseline_training_lock(),
        "retained E80 training lock",
    )
    exact(
        lock["saturation_training_lock"],
        saturation_training_lock(),
        "E160 training lock",
    )
    exact(lock["comparison"], comparison_semantics(), "comparison semantics")
    exact(lock["result_exposed"], False, "pre-exposure lock")
    exact(lock["execution_decision"], EXECUTION_DECISION, "execution decision")
    audit = lock["artifact_audit"]
    if type(audit) is not str or not audit.strip():
        raise SaturationError("lock needs the dated Issue artifact-audit reference")
    provenance = lock["provenance"]
    if (
        type(provenance) is not dict
        or type(provenance.get("source_revisions")) is not dict
    ):
        raise SaturationError("lock provenance is missing its source revisions")
    expected = {
        "source_revisions": {
            "lisjong": LISJONG_REVISION,
            "lisjong_engine": ENGINE_REVISION,
            "lisjong_arena": digest(
                provenance["source_revisions"].get("lisjong_arena"),
                "execution Arena revision",
                40,
            ),
        },
        "fully_resolved": True,
        "effective_rules": RULES,
        "anchor_semantics_id": "turn-pre-action-frozen-anchor-v1",
        "evidence_cutoff_semantics_id": "anchor-time-round-evidence-prefix-v1",
        "label_semantics_id": "exact-concealed-count-red-structural-wait-v1",
    }
    exact(provenance, expected, "source / rules lock")
    validate_runtime(lock["runtime"], lock["predecessor_runtime"])
    exact(
        lock["platform_matches_predecessor"],
        lock["runtime"]["platform"] == lock["predecessor_runtime"]["platform"],
        "recorded platform agreement",
    )
    return lock


def current_receipt(
    *, arena_revision: str, predecessor_root: str, artifact_audit: str
) -> dict[str, object]:
    """live processのexecution receiptを構成する。値を捏造しない。

    `arena_revision`はcallerが宣言する実行Arena revisionであり、installed
    provenanceが返すrevisionと一致しなければfail closedする。
    `predecessor_runtime`は#157 execution lockから実際に読み出す。
    """
    import importlib.metadata
    import platform
    import sysconfig

    import torch
    from lisjong_engine.rules import RuleSet

    from lisjong_arena.phase4_raw_corpus.extraction import phase4_provenance
    from lisjong_arena.stage3_entry_gate.experiment import configure_torch_runtime
    from lisjong_arena.stage3_mix_pilot.generation import _provenance_value

    configure_torch_runtime()
    provenance = _provenance_value(phase4_provenance(RuleSet.default()))
    exact(
        provenance["source_revisions"]["lisjong_arena"],
        arena_revision,
        "installed execution revision",
    )
    if torch.cuda.is_available():
        raise SaturationError("Arena #167 requires a CPU-only runtime")
    predecessor_runtime = load_predecessor_lock(predecessor_root)["runtime"]
    runtime = {
        "python": platform.python_version(),
        "torch": torch.__version__,
        "riichienv": importlib.metadata.version("riichienv"),
        "platform": platform.platform(),
        "device": "cpu",
        "torch_threads": torch.get_num_threads(),
        "deterministic_algorithms": torch.are_deterministic_algorithms_enabled(),
        "free_threaded": bool(sysconfig.get_config_var("Py_GIL_DISABLED")),
    }
    receipt = {
        "schema": SCHEMA + "/execution-lock",
        "baseline_arena_revision": BASELINE_ARENA_REVISION,
        "retained": retained_value(),
        "predecessor_runtime": predecessor_runtime,
        "platform_matches_predecessor": runtime["platform"]
        == predecessor_runtime["platform"],
        "baseline_training_lock": baseline_training_lock(),
        "saturation_training_lock": saturation_training_lock(),
        "comparison": comparison_semantics(),
        "provenance": provenance,
        "runtime": runtime,
        "artifact_audit": artifact_audit,
        "result_exposed": False,
        "execution_decision": EXECUTION_DECISION,
    }
    return validate_lock(receipt)


def require_current_lock(lock: dict[str, object], predecessor_root: str) -> str:
    """渡されたlockが、いま走っているruntimeのreceiptと一致することを要求する。

    E160 trainingとresult assemblyはこれを通ってからでないと開始しない。別環境で
    作ったlockを持ち込んで、別revisionのlisjongで実行することはできない。
    """
    validate_lock(lock)
    actual = current_receipt(
        arena_revision=lock["provenance"]["source_revisions"]["lisjong_arena"],
        predecessor_root=predecessor_root,
        artifact_audit=lock["artifact_audit"],
    )
    exact(actual, lock, "live execution lock")
    return identity(lock)


__all__ = [
    "CLASSIFICATION_RULE",
    "DETERMINISM_GATE_RULE",
    "LOCK_FIELDS",
    "NUMERIC_RUNTIME_FIELDS",
    "RESUME_RULE",
    "RUNTIME_FIELDS",
    "comparison_semantics",
    "current_receipt",
    "require_current_lock",
    "validate_lock",
    "validate_runtime",
]
