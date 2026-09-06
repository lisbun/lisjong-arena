"""P1 Q-v2 — the 8241-input Offline Q arm of Issue #158.

`#140` / current merged implementationのsimple Offline Q formulationを意図的に
そのまま再利用する。目的はQ formulationを改善することではなく、**representation
1 familyだけの差**を見ることである。

```text
retained transition rows (unchanged)
    -> P1 derived features (8241 = locked 8204 + keep-shanten 37)
    -> Linear(8241, 128) -> ReLU -> Linear(128, 802)
    -> #140と同一のsupport-restricted fitted-Q training semantics
    -> fixed_final_iteration checkpoint selection
```

現行`q_training.create_model()` pathは8204入力へ固定されているため、existing
v1 behaviorを変えずに、このmoduleが#158 experiment-localな8241入力modelを
構築する。training loop自体は`q_training.train_from_split_tensors()`を
`model_factory`経由で再利用し、実装を複製しない。したがって次はすべて#140と
literalに同一である。

```text
loss                   selected-action Huber (delta 1.0)
optimizer              Adam(lr=1e-3, weight_decay=0.0)
batch size             256
gamma                  1.0
target sync cadence    epoch-level hard sync
support restriction    TRAIN-supported next legal actions only
maximum epochs         MAXIMUM_EPOCHS
training seed          TRAINING_SEED
dataloader seed        DATALOADER_SEED
deterministic settings DETERMINISTIC_ALGORITHMS
checkpoint selection   fixed_final_iteration
hidden width           128
activation             relu
output dimension       802
```

**hidden widthでparameter countを合わせない。** input dimensionの増加に伴う
first-layer parameter増加は、feature追加の不可避な帰結として受け入れる。

```text
8204-input model   1,153,698
8241-input model   1,158,434
delta                 +4,736   = 37 x 128
```
"""

import hashlib
from array import array

from lisjong_arena.learned_policy_stage2.network import parameter_count

from .errors import OfflineQProtocolError
from .p1_features import P1_FEATURE_DIMENSION
from .protocol import (
    BATCH_SIZE,
    DATALOADER_SEED,
    DATALOADER_WORKERS,
    DETERMINISTIC_ALGORITHMS,
    EXPECTED_PARAMETER_COUNT,
    GAMMA,
    HIDDEN_WIDTH,
    HUBER_LOSS_DELTA,
    LEARNING_RATE,
    MAXIMUM_EPOCHS,
    TARGET_SYNC_CADENCE,
    TORCH_THREADS,
    TRAINING_SEED,
    VOCABULARY_SIZE,
    WEIGHT_DECAY,
    Split,
)
from .q_training import train_from_split_tensors

P1_Q_MODEL_ID = "arena-learned-policy-offlineq-p1-q-mlp-v1"

P1_EXPECTED_PARAMETER_COUNT = 1_158_434
"""8241*128 + 128 + 128*802 + 802。Issue #158のpreflightでlockした値。"""

BASELINE_PARAMETER_COUNT = EXPECTED_PARAMETER_COUNT
PARAMETER_COUNT_DELTA = P1_EXPECTED_PARAMETER_COUNT - BASELINE_PARAMETER_COUNT

if (
    P1_EXPECTED_PARAMETER_COUNT
    != P1_FEATURE_DIMENSION * HIDDEN_WIDTH
    + HIDDEN_WIDTH
    + HIDDEN_WIDTH * VOCABULARY_SIZE
    + VOCABULARY_SIZE
    or PARAMETER_COUNT_DELTA != (P1_FEATURE_DIMENSION - 8204) * HIDDEN_WIDTH
):
    raise RuntimeError("the locked P1 parameter count does not follow from the shapes")


def create_p1_model():
    """locked P1 1x128 MLPを構築する。parameter countが合わない場合はfail closed。

    hidden width、activation、output dimensionはStage 2 / #140と同一であり、
    input dimensionだけが8204から8241へ変わる。
    """
    import torch

    class P1QModel(torch.nn.Module):
        def __init__(self) -> None:
            super().__init__()
            self.network = torch.nn.Sequential(
                torch.nn.Linear(P1_FEATURE_DIMENSION, HIDDEN_WIDTH),
                torch.nn.ReLU(),
                torch.nn.Linear(HIDDEN_WIDTH, VOCABULARY_SIZE),
            )

        def forward(self, features):
            return self.network(features)

    model = P1QModel()
    count = parameter_count(model)
    if count != P1_EXPECTED_PARAMETER_COUNT:
        raise OfflineQProtocolError(
            f"the locked P1 model parameter count is {P1_EXPECTED_PARAMETER_COUNT}; "
            f"got {count}"
        )
    return model


def train_p1_q_model(tensors: dict):
    """P1 derived split tensorsを、#140と同一のtraining semanticsで学習する。"""
    return train_from_split_tensors(tensors, model_factory=create_p1_model)


def model_weights_digest(model) -> str:
    """state_dictのtensor bytesからdeterministicなdigestを作る。

    `torch.save()`のcontainer bytesではなくtensor payloadそのものをhashする
    ため、同一weightsに対して常に同一digestになる。
    """
    import torch

    digest = hashlib.sha256()
    state = model.state_dict()
    for name in sorted(state):
        value = state[name]
        digest.update(name.encode("utf-8"))
        digest.update(str(value.dtype).encode("utf-8"))
        digest.update(repr(tuple(value.shape)).encode("utf-8"))
        values = value.detach().to(torch.float32).contiguous().flatten().tolist()
        digest.update(array("f", values).tobytes())
    return digest.hexdigest()


def p1_model_block() -> dict[str, object]:
    """Q-v2 modelのlocked shape block。input dimension以外は#140と同一である。"""
    return {
        "model_id": P1_Q_MODEL_ID,
        "input_dimension": P1_FEATURE_DIMENSION,
        "hidden_layers": 1,
        "hidden_width": HIDDEN_WIDTH,
        "activation": "relu",
        "output_dimension": VOCABULARY_SIZE,
        "dropout": None,
        "normalization_layer": None,
        "parameter_count": P1_EXPECTED_PARAMETER_COUNT,
        "baseline_input_dimension": 8204,
        "baseline_parameter_count": BASELINE_PARAMETER_COUNT,
        "parameter_count_delta": PARAMETER_COUNT_DELTA,
    }


def p1_training_block() -> dict[str, object]:
    """Q-v2 trainingのlocked semantics block（#140とliteralに同一）。"""
    return {
        "loss": "huber_selected_action_td_target",
        "gamma": GAMMA,
        "target_sync_cadence": TARGET_SYNC_CADENCE,
        "huber_loss_delta": HUBER_LOSS_DELTA,
        "optimizer": "adam",
        "learning_rate": LEARNING_RATE,
        "weight_decay": WEIGHT_DECAY,
        "batch_size": BATCH_SIZE,
        "maximum_epochs": MAXIMUM_EPOCHS,
        "training_seed": TRAINING_SEED,
        "dataloader_seed": DATALOADER_SEED,
        "dataloader_workers": DATALOADER_WORKERS,
        "torch_threads": TORCH_THREADS,
        "deterministic_algorithms": DETERMINISTIC_ALGORITHMS,
        "checkpoint_selection": "fixed_final_iteration",
    }


def verify_locked_q_protocol_delta() -> None:
    """Q protocolの差分がinput dimensionだけであることをfail closedで確認する。

    `q_training.locked_model_block()` / `locked_training_block()`をsource of
    truthとして、P1 blockとの差が`input_dimension`（と、その帰結として記録する
    parameter count情報）だけであることを確かめる。
    """
    from .q_training import locked_model_block, locked_training_block

    if p1_training_block() != locked_training_block():
        raise OfflineQProtocolError(
            "the P1 training block differs from the locked #140 training semantics"
        )
    baseline = locked_model_block()
    candidate = p1_model_block()
    derived_only = {
        "model_id",
        "input_dimension",
        "parameter_count",
        "baseline_input_dimension",
        "baseline_parameter_count",
        "parameter_count_delta",
    }
    if set(candidate) - set(baseline) != derived_only - set(baseline):
        raise OfflineQProtocolError("the P1 model block carries unexpected fields")
    for name, value in baseline.items():
        if name in derived_only:
            continue
        if candidate[name] != value:
            raise OfflineQProtocolError(
                f"the P1 model block changed {name!r}; the only permitted difference "
                "is the input dimension"
            )
    if candidate["baseline_input_dimension"] != baseline["input_dimension"]:
        raise OfflineQProtocolError(
            "the P1 model block does not record the locked baseline input dimension"
        )


def require_p1_split_tensors(tensors: dict) -> dict:
    """TRAIN / VALIDATIONがP1 schemaで揃っていることをfail closedで確認する。"""
    missing = [
        split for split in (Split.TRAIN, Split.VALIDATION) if split not in tensors
    ]
    if missing:
        raise OfflineQProtocolError(
            f"P1 training requires {[split.value for split in missing]} tensors"
        )
    for split, entry in tensors.items():
        if int(entry.features.shape[1]) != P1_FEATURE_DIMENSION:
            raise OfflineQProtocolError(
                f"{split.value} features are not the P1 derived schema"
            )
        if int(entry.next_features.shape[1]) != P1_FEATURE_DIMENSION:
            raise OfflineQProtocolError(
                f"{split.value} next features are not the P1 derived schema"
            )
    return tensors


__all__ = [
    "BASELINE_PARAMETER_COUNT",
    "PARAMETER_COUNT_DELTA",
    "P1_EXPECTED_PARAMETER_COUNT",
    "P1_Q_MODEL_ID",
    "create_p1_model",
    "model_weights_digest",
    "p1_model_block",
    "p1_training_block",
    "require_p1_split_tensors",
    "train_p1_q_model",
    "verify_locked_q_protocol_delta",
]
