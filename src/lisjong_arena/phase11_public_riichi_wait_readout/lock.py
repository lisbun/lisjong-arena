"""Pre-result execution lock for the one-shot Arena #172 experiment."""

import importlib.metadata
import platform
import sysconfig

from lisjong_engine.rules import RuleSet

from lisjong_arena._execution_safety import require_clean_arena_head
from lisjong_arena.phase4_raw_corpus.extraction import phase4_provenance
from lisjong_arena.stage3_mix_pilot.generation import _provenance_value
from lisjong_arena.stage3_optimization_saturation.protocol import RULES

from .protocol import (
    ENGINE_REVISION,
    FORMAL_TRAINING_CONFIG,
    LISJONG_REVISION,
    RIICHIENV_VERSION,
    ROLE,
    SCHEMA,
    TORCH_VERSION,
    Phase11Error,
    digest,
    evaluation_value,
    exact,
    identity,
    readout_value,
    representation_value,
    retained_value,
    training_value,
)
from .retained import load_retained, retained_readback_value

LOCK_FIELDS = (
    "schema",
    "role",
    "retained",
    "retained_runtime",
    "provenance",
    "runtime",
    "representation",
    "readout",
    "training",
    "evaluation",
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


def validate_runtime(value: object, name: str) -> dict[str, object]:
    if type(value) is not dict or set(value) != set(RUNTIME_FIELDS):
        raise Phase11Error(f"{name} fields are not exact")
    if type(value["python"]) is not str or not value["python"].startswith("3.14."):
        raise Phase11Error(f"{name} must use ordinary CPython 3.14")
    exact(value["torch"], TORCH_VERSION, f"{name} PyTorch")
    exact(value["riichienv"], RIICHIENV_VERSION, f"{name} RiichiEnv")
    if type(value["platform"]) is not str or not value["platform"].strip():
        raise Phase11Error(f"{name} platform is invalid")
    exact(value["device"], "cpu", f"{name} device")
    exact(value["torch_threads"], 1, f"{name} torch threads")
    exact(value["deterministic_algorithms"], True, f"{name} determinism")
    exact(value["free_threaded"], False, f"{name} free-threaded status")
    return value


def validate_lock(value: object) -> dict[str, object]:
    if type(value) is not dict or set(value) != set(LOCK_FIELDS):
        raise Phase11Error("execution lock fields are not exact")
    exact(value["schema"], SCHEMA + "/execution-lock", "lock schema")
    exact(value["role"], ROLE, "lock role")
    exact(value["retained"], retained_value(), "retained identities")
    validate_runtime(value["retained_runtime"], "retained #167 runtime")
    validate_runtime(value["runtime"], "execution runtime")
    provenance = value["provenance"]
    if (
        type(provenance) is not dict
        or type(provenance.get("source_revisions")) is not dict
    ):
        raise Phase11Error("lock provenance is missing source revisions")
    exact(
        provenance,
        {
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
        },
        "execution provenance",
    )
    exact(value["representation"], representation_value(), "representation lock")
    exact(value["readout"], readout_value(), "readout lock")
    exact(value["training"], training_value(), "training lock")
    exact(value["evaluation"], evaluation_value(), "evaluation lock")
    audit = value["artifact_audit"]
    if type(audit) is not str or not audit.strip():
        raise Phase11Error("execution lock requires a dated retained-artifact audit")
    if type(value["result_exposed"]) is not bool:
        raise Phase11Error("result_exposed must be a JSON boolean")
    exact(value["result_exposed"], False, "pre-exposure lock")
    exact(
        value["execution_decision"],
        "ONE-SHOT LOCAL EXECUTION AFTER MERGE",
        "execution decision",
    )
    return value


def _runtime() -> dict[str, object]:
    import torch

    return {
        "python": platform.python_version(),
        "torch": torch.__version__,
        "riichienv": importlib.metadata.version("riichienv"),
        "platform": platform.platform(),
        "device": "cpu",
        "torch_threads": torch.get_num_threads(),
        "deterministic_algorithms": torch.are_deterministic_algorithms_enabled(),
        "free_threaded": bool(sysconfig.get_config_var("Py_GIL_DISABLED")),
    }


def current_receipt(
    *,
    arena_revision: str,
    corpus_root: str,
    phase157_root: str,
    phase167_root: str,
    artifact_audit: str,
) -> dict[str, object]:
    import torch

    torch.use_deterministic_algorithms(FORMAL_TRAINING_CONFIG.deterministic_algorithms)
    torch.set_num_threads(FORMAL_TRAINING_CONFIG.torch_threads)
    if torch.cuda.is_available():
        raise Phase11Error("Arena #172 requires the locked CPU-only runtime")
    clean_head = require_clean_arena_head()
    evidence = load_retained(corpus_root, phase157_root, phase167_root)
    provenance = _provenance_value(phase4_provenance(RuleSet.default()))
    exact(
        clean_head,
        provenance["source_revisions"]["lisjong_arena"],
        "clean Arena HEAD against execution provenance",
    )
    exact(
        clean_head,
        arena_revision,
        "clean Arena HEAD against execution target",
    )
    exact(
        provenance["source_revisions"]["lisjong_arena"],
        arena_revision,
        "installed execution Arena revision",
    )
    receipt = {
        "schema": SCHEMA + "/execution-lock",
        "role": ROLE,
        "retained": retained_readback_value(evidence),
        "retained_runtime": evidence.phase167_lock["runtime"],
        "provenance": provenance,
        "runtime": _runtime(),
        "representation": representation_value(),
        "readout": readout_value(),
        "training": training_value(),
        "evaluation": evaluation_value(),
        "artifact_audit": artifact_audit,
        "result_exposed": False,
        "execution_decision": "ONE-SHOT LOCAL EXECUTION AFTER MERGE",
    }
    return validate_lock(receipt)


def require_current_lock(
    lock: dict,
    *,
    corpus_root: str,
    phase157_root: str,
    phase167_root: str,
) -> str:
    validate_lock(lock)
    actual = current_receipt(
        arena_revision=lock["provenance"]["source_revisions"]["lisjong_arena"],
        corpus_root=corpus_root,
        phase157_root=phase157_root,
        phase167_root=phase167_root,
        artifact_audit=lock["artifact_audit"],
    )
    exact(actual, lock, "live execution lock")
    return identity(lock)


__all__ = [
    "LOCK_FIELDS",
    "RUNTIME_FIELDS",
    "current_receipt",
    "require_current_lock",
    "validate_lock",
    "validate_runtime",
]
