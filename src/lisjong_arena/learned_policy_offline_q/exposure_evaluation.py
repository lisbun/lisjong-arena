"""One-shot TEST diagnostics for both arms (Issue #140).

TEST exposureは1回だけ行う。ここで計算するのはdiagnosticsだけであり、
strength claimはABBB screening（`strength.py`）だけが持つ。BC側の
agreementはteacher-imitation diagnosticであり、Q側のTD residualはBellman
consistency diagnosticである。いずれもgame strengthの代替として扱わない。
"""

from dataclasses import dataclass

from .bc_training import choice_row_selector
from .protocol import Split
from .q_network import q_value_at
from .q_training import compute_td_targets, train_support_mask
from .split_tensors import OfflineQSplitTensors


@dataclass(frozen=True, slots=True)
class BcTestDiagnostics:
    """BC hybridのTEST diagnostics。agreementはstrength proxyではない。"""

    choice_rows: int
    choice_masked_cross_entropy: float
    choice_exact_agreement: float

    def to_document(self) -> dict[str, object]:
        return {
            "choice_rows": self.choice_rows,
            "choice_masked_cross_entropy": self.choice_masked_cross_entropy,
            "choice_exact_agreement": self.choice_exact_agreement,
        }


def evaluate_bc_test(model, tensors: OfflineQSplitTensors) -> BcTestDiagnostics:
    import torch

    from lisjong_arena.learned_policy_stage2.network import (
        masked_argmax,
        masked_cross_entropy,
    )

    selector = choice_row_selector(tensors.legal_mask)
    count = int(selector.sum())
    features = tensors.features[selector]
    legal_mask = tensors.legal_mask[selector]
    targets = tensors.behavior_action_index[selector]
    model.eval()
    with torch.no_grad():
        logits = model(features)
        losses = masked_cross_entropy(logits, legal_mask, targets)
        predicted = masked_argmax(logits, legal_mask)
    return BcTestDiagnostics(
        choice_rows=count,
        choice_masked_cross_entropy=float(losses.mean()),
        choice_exact_agreement=float((predicted == targets).to(torch.float64).mean()),
    )


@dataclass(frozen=True, slots=True)
class QTestDiagnostics:
    """Q hybridのTEST diagnostics。TD residualはstrength proxyではない。"""

    row_count: int
    selected_action_huber_loss: float
    finite_q_rate: float
    predicted_q_mean: float
    predicted_q_std: float
    target_mean: float
    target_std: float

    def to_document(self) -> dict[str, object]:
        return {
            "row_count": self.row_count,
            "selected_action_huber_loss": self.selected_action_huber_loss,
            "finite_q_rate": self.finite_q_rate,
            "predicted_q_mean": self.predicted_q_mean,
            "predicted_q_std": self.predicted_q_std,
            "target_mean": self.target_mean,
            "target_std": self.target_std,
        }


def evaluate_q_test(
    model, train_tensors: OfflineQSplitTensors, test_tensors: OfflineQSplitTensors
) -> QTestDiagnostics:
    """trainedmodel自身をtarget networkとして、TEST上のBellman残差を報告する。

    checkpoint selection後の固定modelなので、moving targetの懸念はない。
    """
    import torch

    from .protocol import HUBER_LOSS_DELTA

    support_mask = train_support_mask(train_tensors)
    targets = compute_td_targets(model, test_tensors, support_mask)
    model.eval()
    with torch.no_grad():
        q_values = model(test_tensors.features)
        selected = q_value_at(q_values, test_tensors.behavior_action_index)
        losses = torch.nn.functional.huber_loss(
            selected, targets, delta=HUBER_LOSS_DELTA, reduction="none"
        )
        finite = torch.isfinite(q_values)
    return QTestDiagnostics(
        row_count=test_tensors.row_count,
        selected_action_huber_loss=float(losses.mean()),
        finite_q_rate=float(finite.to(torch.float64).mean()),
        predicted_q_mean=float(selected.mean()),
        predicted_q_std=float(selected.std(unbiased=False)),
        target_mean=float(targets.mean()),
        target_std=float(targets.std(unbiased=False)),
    )


def require_test_split(
    tensors: dict[Split, OfflineQSplitTensors],
) -> OfflineQSplitTensors:
    if Split.TEST not in tensors:
        raise ValueError("tensors must include the TEST split")
    return tensors[Split.TEST]


__all__ = [
    "BcTestDiagnostics",
    "QTestDiagnostics",
    "evaluate_bc_test",
    "evaluate_q_test",
    "require_test_split",
]
