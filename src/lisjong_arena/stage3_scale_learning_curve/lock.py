"""Phase 10のsource / runtime execution lock。

execution lockはpopulation生成・training・result assemblyの **すべて** のloaderへ
明示的に渡すreceiptである。resultが自分でreceiptを埋め込んで実行revisionを
選び直せないよう、artifactはlock identityだけを持ち、lock本体は外から与える。

lock identityはgeneration前にIssue #150へ記録する（pre-execution lock）。

## installed provenance mismatch

開発機の`.venv`が`pyproject.toml`のexact pinsと違うrevisionのlisjongを持って
いても、Phase 10はそのまま実行できてはならない。`current_receipt()`は
Phase 4 provenanceから **実際にinstallされているrevision** を読み、locked pins
と違えばfail closedする。silentに許容しない。
"""

from .population import plan_value
from .protocol import (
    BASELINE_ARENA_REVISION,
    ENGINE_REVISION,
    EXECUTION_DECISION,
    LISJONG_REVISION,
    RIICHIENV_VERSION,
    RULES,
    SCHEMA,
    TORCH_VERSIONS,
    ScaleError,
    digest,
    exact,
    identity,
    training_lock,
)

LOCK_FIELDS = (
    "schema",
    "baseline_arena_revision",
    "population_plan",
    "training_lock",
    "provenance",
    "runtime",
    "seed_audit",
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


def validate_lock(lock: object) -> dict[str, object]:
    """execution lockをlocked plan / pinned sources / CPU runtimeへ固定する。"""
    if type(lock) is not dict or set(lock) != set(LOCK_FIELDS):
        raise ScaleError("execution lock fields are not exact")
    exact(lock["schema"], SCHEMA + "/execution-lock", "lock schema")
    exact(lock["baseline_arena_revision"], BASELINE_ARENA_REVISION, "preflight main")
    exact(lock["population_plan"], plan_value(), "locked plan")
    exact(lock["training_lock"], training_lock(), "training lock")
    exact(lock["result_exposed"], False, "pre-exposure lock")
    exact(lock["execution_decision"], EXECUTION_DECISION, "execution decision")
    audit = lock["seed_audit"]
    if type(audit) is not str or not audit.strip():
        raise ScaleError("lock needs the dated Issue seed-audit reference")
    provenance = lock["provenance"]
    if (
        type(provenance) is not dict
        or type(provenance.get("source_revisions")) is not dict
    ):
        raise ScaleError("lock provenance is missing its source revisions")
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
    runtime = lock["runtime"]
    if type(runtime) is not dict or set(runtime) != set(RUNTIME_FIELDS):
        raise ScaleError("runtime fields are not exact")
    if (
        type(runtime["python"]) is not str
        or not runtime["python"].startswith("3.14.")
        or runtime["torch"] not in TORCH_VERSIONS
        or type(runtime["platform"]) is not str
        or not runtime["platform"].strip()
    ):
        raise ScaleError("unsupported locked runtime")
    locked_runtime = {
        "riichienv": RIICHIENV_VERSION,
        "device": "cpu",
        "torch_threads": 1,
        "deterministic_algorithms": True,
        "free_threaded": False,
    }
    for name, value in locked_runtime.items():
        exact(runtime[name], value, name)
    return lock


def current_receipt(*, arena_revision: str, seed_audit: str) -> dict[str, object]:
    """live processのexecution receiptを構成する。値を捏造しない。

    `arena_revision`はcallerが宣言する実行Arena revisionであり、installed
    provenanceが返すrevisionと一致しなければfail closedする。
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
        raise ScaleError("Phase 10 requires a CPU-only runtime")
    receipt = {
        "schema": SCHEMA + "/execution-lock",
        "baseline_arena_revision": BASELINE_ARENA_REVISION,
        "population_plan": plan_value(),
        "training_lock": training_lock(),
        "provenance": provenance,
        "runtime": {
            "python": platform.python_version(),
            "torch": torch.__version__,
            "riichienv": importlib.metadata.version("riichienv"),
            "platform": platform.platform(),
            "device": "cpu",
            "torch_threads": torch.get_num_threads(),
            "deterministic_algorithms": torch.are_deterministic_algorithms_enabled(),
            "free_threaded": bool(sysconfig.get_config_var("Py_GIL_DISABLED")),
        },
        "seed_audit": seed_audit,
        "result_exposed": False,
        "execution_decision": EXECUTION_DECISION,
    }
    return validate_lock(receipt)


def require_current_lock(lock: dict[str, object]) -> str:
    """渡されたlockが、いま走っているruntimeのreceiptと一致することを要求する。

    generation / trainingはこれを通ってからでないと開始しない。別環境で作った
    lockを持ち込んで、別revisionのlisjongで実行することはできない。
    """
    validate_lock(lock)
    actual = current_receipt(
        arena_revision=lock["provenance"]["source_revisions"]["lisjong_arena"],
        seed_audit=lock["seed_audit"],
    )
    exact(actual, lock, "live execution lock")
    return identity(lock)


__all__ = [
    "LOCK_FIELDS",
    "RUNTIME_FIELDS",
    "current_receipt",
    "require_current_lock",
    "validate_lock",
]
