"""Offline mechanism diagnostics for the two curriculum arms (Issue #165).

TRAIN / VALIDATION / TESTについて、teacher変更のdownstream consequenceを
記述するためのdiagnosticsだけを集める。

```text
hanchan count / transition rows / terminal rows
teacher action-family counts
discard / riichi / call / winning decisions
TRAIN support size / digest / coverage
reward distribution summary
TEST behavior vs Q-selected hand progression   (#152 / #158 thin reuse)
```

## primary classificationへ入らない

これらはmechanism diagnosticsであり、`#165`のprimary classificationは
fresh rollout intervalだけから導出される。特にArm Y / Arm FのTESTは
**state-pairedではない**。同じseedでもteacherが最初に異なるactionを選んだ
時点でtrajectoryは分岐しており、

```text
Arm F worsen-rate < Arm Y worsen-rate
```

はcausal primary evidenceではない。

## shanten semanticsを再実装しない

hand progressionは`#152`の`hand_progression`（`calculate_shanten()`を唯一の
正本とするplayer-safe derivation）と`#158` Gate Aのsummary関数をそのまま
呼ぶ。ambiguousに復元されるrowが1つでもあればroleを`UNAVAILABLE`にし、
推測で埋めない。
"""

from .diagnosis import (
    fixed_summary,
    hand_progression_arm_summary,
    rate,
    require_finite,
    select_eligible_rows,
)
from .errors import OfflineQAmbiguousStateError
from .fh_curriculum import ARM_IDENTITY, CurriculumArm, require_arm
from .fh_curriculum_candidate import (
    LoadedArmCandidate,
    derive_train_support,
    support_block,
)
from .fh_curriculum_dataset import (
    CALL_ACTION_FAMILIES,
    DISCARD_ACTION_FAMILIES,
    RIICHI_ACTION_FAMILIES,
    WINNING_ACTION_FAMILIES,
    LoadedFiniteHorizonCurriculumDataset,
)
from .hand_progression import MeasurementAvailability, hand_progression_for_row
from .p1_features import derive_split_tensors
from .protocol import VOCABULARY_SIZE, Split
from .split_tensors import load_split_tensors
from .support import support_set_identity

DIAGNOSTICS_SCHEMA_VERSION = (
    "arena-learned-policy-finite-horizon-curriculum-diagnostics-v1"
)

IMPROVE_SHANTEN_REASON = (
    "a discard never lowers the shanten count, so the improve bucket is "
    "structurally zero under the locked shanten contract; keep and worsen "
    "partition every eligible decision"
)

DIAGNOSTIC_ROLE = (
    "mechanism diagnostic only; the Arm Y and Arm F populations are seed-aligned "
    "but not state-paired, and no diagnostic may alter the primary rollout "
    "classification"
)


def _family_total(counts: dict, families: tuple[str, ...]) -> int:
    return sum(int(counts.get(name, 0)) for name in families)


def teacher_decision_block(counts: dict) -> dict[str, object]:
    """action-family countsを、Issueが要求するdecision groupへ畳む。"""
    return {
        "teacher_action_family_counts": dict(sorted(counts.items())),
        "discard_decisions": _family_total(counts, DISCARD_ACTION_FAMILIES),
        "riichi_decisions": _family_total(counts, RIICHI_ACTION_FAMILIES),
        "call_decisions": _family_total(counts, CALL_ACTION_FAMILIES),
        "winning_decisions": _family_total(counts, WINNING_ACTION_FAMILIES),
    }


def split_diagnostics(
    dataset: LoadedFiniteHorizonCurriculumDataset,
) -> dict[str, object]:
    """TRAIN / VALIDATION / TESTごとのrow / decision / reward diagnostics。"""
    if not isinstance(dataset, LoadedFiniteHorizonCurriculumDataset):
        raise TypeError("dataset must be a LoadedFiniteHorizonCurriculumDataset")
    games_by_seed = {entry["seed"]: entry for entry in dataset.manifest["games"]}
    document: dict[str, object] = {}
    for split in Split:
        indices = dataset.split_indices(split)
        rows = [dataset.rows[index] for index in indices]
        seeds = sorted({row.seed for row in rows})
        counts: dict[str, int] = {}
        decision_count = 0
        for seed in seeds:
            entry = games_by_seed[seed]
            decision_count += entry["decision_count"]
            for name, value in entry["teacher_action_family_counts"].items():
                counts[name] = counts.get(name, 0) + int(value)
        terminal_rows = sum(1 for row in rows if row.terminal)
        document[split.value] = {
            "hanchan_count": len(seeds),
            "seeds": seeds,
            "transition_rows": len(rows),
            "terminal_rows": terminal_rows,
            "nonterminal_rows": len(rows) - terminal_rows,
            "teacher_decision_count": decision_count,
            **teacher_decision_block(counts),
            "reward_distribution": fixed_summary([row.reward for row in rows]),
        }
    return document


def train_support_diagnostics(
    dataset: LoadedFiniteHorizonCurriculumDataset,
) -> dict[str, object]:
    """自armのTRAINだけから導出したsupport identityとcoverage。"""
    block = support_block(dataset)
    return {
        "support_size": block["support_size"],
        "support_digest": block["supported_indices_digest"],
        "support_coverage": block["support_coverage"],
        "derived_from": "own TRAIN split behavior actions only",
    }


def _support_mask(supported_indices):
    import torch

    mask = torch.zeros(VOCABULARY_SIZE, dtype=torch.bool)
    for index in supported_indices:
        mask[int(index)] = True
    return mask


def _unavailable(reason: str) -> dict[str, object]:
    return {
        "status": MeasurementAvailability.UNAVAILABLE.value,
        "unavailable_reason": reason,
        "eligible_row_count": None,
        "row_counts": None,
        "behavior": None,
        "q_selected": None,
        "improve_shanten_count": None,
        "improve_shanten_reason": IMPROVE_SHANTEN_REASON,
    }


def test_hand_progression(
    dataset: LoadedFiniteHorizonCurriculumDataset,
    *,
    model,
    supported_indices,
    batch_size: int = 256,
) -> dict[str, object]:
    """TEST splitのbehavior / Q-selected hand progressionを同一rowから導出する。

    eligibilityはserving activationと同じ条件（`select_eligible_rows()`）で
    決める。featureはbase v1 rowをhand progression derivationへ、P1 derived
    rowをmodel forwardへ渡す。
    """
    import torch

    from .q_network import masked_argmax_q

    if not isinstance(dataset, LoadedFiniteHorizonCurriculumDataset):
        raise TypeError("dataset must be a LoadedFiniteHorizonCurriculumDataset")
    tensors = load_split_tensors(dataset)[Split.TEST]
    derived, _ = derive_split_tensors(tensors)
    eligible, counts = select_eligible_rows(tensors, _support_mask(supported_indices))
    selector = torch.nonzero(eligible).flatten()
    row_count = int(selector.shape[0])
    if row_count == 0:
        return _unavailable(
            "the TEST split has no eligible ordinary-discard support-complete row; "
            "no rate is defined on an empty population"
        )

    features = tensors.features.index_select(0, selector)
    p1_features = derived.features.index_select(0, selector)
    legal_mask = tensors.legal_mask.index_select(0, selector)
    behavior_index = tensors.behavior_action_index.index_select(0, selector)

    outputs = []
    with torch.inference_mode():
        for start in range(0, row_count, batch_size):
            outputs.append(model(p1_features[start : start + batch_size]).clone())
    q_values = torch.cat(outputs, dim=0)
    require_finite(q_values, "curriculum candidate Q output")
    selections = {
        "behavior": behavior_index.tolist(),
        "q_selected": masked_argmax_q(q_values, legal_mask).tolist(),
    }

    progressions: dict[str, list] = {name: [] for name in selections}
    try:
        for position in range(row_count):
            derived_progressions = hand_progression_for_row(
                features[position],
                tuple(selections[name][position] for name in selections),
            )
            for name, entry in zip(selections, derived_progressions, strict=True):
                progressions[name].append(entry)
    except OfflineQAmbiguousStateError as error:
        return _unavailable(str(error))

    return {
        "status": MeasurementAvailability.AVAILABLE.value,
        "unavailable_reason": None,
        "eligible_row_count": row_count,
        "row_counts": counts.to_document(),
        "behavior": hand_progression_arm_summary(progressions["behavior"]),
        "q_selected": hand_progression_arm_summary(progressions["q_selected"]),
        "improve_shanten_count": 0,
        "improve_shanten_reason": IMPROVE_SHANTEN_REASON,
    }


def build_arm_diagnostics(
    dataset: LoadedFiniteHorizonCurriculumDataset,
    candidate: LoadedArmCandidate,
) -> dict[str, object]:
    """1 armのoffline diagnostics documentを組み立てる。"""
    if not isinstance(candidate, LoadedArmCandidate):
        raise TypeError("candidate must be a LoadedArmCandidate")
    arm = require_arm(dataset.arm)
    if candidate.arm is not arm:
        raise ValueError("the candidate and dataset describe different arms")
    if candidate.source_dataset_identity != dataset.identity:
        raise ValueError("the candidate was not trained from this dataset")
    supported = derive_train_support(dataset)
    if candidate.support_set_digest != support_set_identity(supported):
        raise ValueError(
            "the candidate support digest is not the one this arm's own TRAIN "
            "dataset derives"
        )
    return {
        "diagnostics_schema_version": DIAGNOSTICS_SCHEMA_VERSION,
        "arm": arm.value,
        "arm_identity": ARM_IDENTITY[arm],
        "teacher_identity": dataset.teacher_identity,
        "dataset_identity": dataset.identity,
        "candidate_identity": candidate.candidate_identity,
        "splits": split_diagnostics(dataset),
        "train_support": train_support_diagnostics(dataset),
        "test_hand_progression": test_hand_progression(
            dataset, model=candidate.model, supported_indices=supported
        ),
        "role": DIAGNOSTIC_ROLE,
    }


def compare_arm_diagnostics(control: dict, curriculum: dict) -> dict[str, object]:
    """2 armのdiagnosticsを並べるだけのview。classificationへは入らない。

    差分の符号だけを記録し、`signal` / `regression`のようなoutcome条件を
    導出しない。primary classificationはrollout intervalだけが決める。
    """
    for document, arm in (
        (control, CurriculumArm.CONTROL),
        (curriculum, CurriculumArm.CURRICULUM),
    ):
        if type(document) is not dict or document.get("arm") != arm.value:
            raise ValueError("diagnostics documents are not the expected arm pair")
    comparison: dict[str, object] = {
        "role": DIAGNOSTIC_ROLE,
        "state_paired": False,
        "state_pairing_note": (
            "same seeds align the environment RNG initial conditions only; the "
            "arms diverge at the first differing teacher action, so these "
            "populations are seed-aligned and never state-paired"
        ),
    }
    for split in Split:
        control_split = control["splits"][split.value]
        curriculum_split = curriculum["splits"][split.value]
        comparison[split.value] = {
            "control_transition_rows": control_split["transition_rows"],
            "curriculum_transition_rows": curriculum_split["transition_rows"],
            "control_teacher_decision_count": control_split["teacher_decision_count"],
            "curriculum_teacher_decision_count": (
                curriculum_split["teacher_decision_count"]
            ),
        }
    control_test = control["test_hand_progression"]
    curriculum_test = curriculum["test_hand_progression"]
    available = (
        control_test["status"] == MeasurementAvailability.AVAILABLE.value
        and curriculum_test["status"] == MeasurementAvailability.AVAILABLE.value
    )
    comparison["test_worsen_shanten_rates"] = (
        None
        if not available
        else {
            "control_behavior": control_test["behavior"]["worsen_shanten_rate"],
            "control_q_selected": control_test["q_selected"]["worsen_shanten_rate"],
            "curriculum_behavior": curriculum_test["behavior"]["worsen_shanten_rate"],
            "curriculum_q_selected": (
                curriculum_test["q_selected"]["worsen_shanten_rate"]
            ),
        }
    )
    comparison["control_support_size"] = control["train_support"]["support_size"]
    comparison["curriculum_support_size"] = curriculum["train_support"]["support_size"]
    comparison["support_size_ratio"] = rate(
        curriculum["train_support"]["support_size"],
        control["train_support"]["support_size"],
    )
    return comparison


__all__ = [
    "DIAGNOSTICS_SCHEMA_VERSION",
    "DIAGNOSTIC_ROLE",
    "IMPROVE_SHANTEN_REASON",
    "build_arm_diagnostics",
    "compare_arm_diagnostics",
    "split_diagnostics",
    "teacher_decision_block",
    "test_hand_progression",
    "train_support_diagnostics",
]
