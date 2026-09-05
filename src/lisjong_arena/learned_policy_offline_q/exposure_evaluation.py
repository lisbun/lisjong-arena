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


def evaluate_bc_test(model, tensors) -> BcTestDiagnostics:
    """choice rowのmasked CEとexact agreementを報告する。

    ``tensors``はfeature / legal mask / behavior actionを持てば良く、
    original dataset split（``OfflineQSplitTensors``）でもreplacement TEST
    tensorsでも同じ定義で計算する。
    """
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


def evaluate_q_with_support_mask(model, tensors, support_mask) -> QTestDiagnostics:
    """既にfixedなmodelとsupport maskに対して、Q diagnosticsを計算する。

    `tensors`はcurrent / next feature、legal mask、behavior action、reward、
    terminalを持つobjectであれば良く、original dataset splitである必要はない。
    `support_mask`はcallerが正本から与える。この関数はsupport setを推測せず、
    TRAIN rowsも要求しない。

    modelはevaluation時点で固定なので、自分自身をtarget networkとして使っても
    moving targetの懸念はない。
    """
    import torch

    from .protocol import HUBER_LOSS_DELTA

    targets = compute_td_targets(model, tensors, support_mask)
    model.eval()
    with torch.no_grad():
        q_values = model(tensors.features)
        selected = q_value_at(q_values, tensors.behavior_action_index)
        losses = torch.nn.functional.huber_loss(
            selected, targets, delta=HUBER_LOSS_DELTA, reduction="none"
        )
        finite = torch.isfinite(q_values)
    return QTestDiagnostics(
        row_count=tensors.row_count,
        selected_action_huber_loss=float(losses.mean()),
        finite_q_rate=float(finite.to(torch.float64).mean()),
        predicted_q_mean=float(selected.mean()),
        predicted_q_std=float(selected.std(unbiased=False)),
        target_mean=float(targets.mean()),
        target_std=float(targets.std(unbiased=False)),
    )


def evaluate_q_test(
    model, train_tensors: OfflineQSplitTensors, test_tensors: OfflineQSplitTensors
) -> QTestDiagnostics:
    """original datasetのTEST splitに対するQ diagnostics。

    support maskをoriginal TRAIN tensorsから再構成するoriginal one-shot TEST
    path専用である。replacement TEST pathはcheckpoint identity-boundな
    support setを使うため、この関数を経由しない
    （`replacement_test`モジュールを参照）。
    """
    return evaluate_q_with_support_mask(
        model, test_tensors, train_support_mask(train_tensors)
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
    "evaluate_q_with_support_mask",
    "require_test_split",
]
