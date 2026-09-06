"""Arena #157のtorch依存test。

Issue #157はE40を再trainingせず、

```text
E80.loss_history[0:40]  ==  #150 retained S64.loss_history   (exact)
```

というdeterminism gateだけを根拠にする。その前提は`max_epochs`がtraining loop
の上限を決めるだけで、RNG stream (`torch.manual_seed(0)` /
`torch.Generator().manual_seed(0)`) にも各epochのupdateにも影響しないこと
である。ここではその前提を、同じsynthetic populationに対する2回の実trainingで
固定する。

synthetic populationだけを使い、formalな80-hanchan corpusにもE80のlocked
budgetにも到達しない。large trainingはunit testへ入れない。
"""

import importlib.util
import unittest
from dataclasses import asdict, replace
from pathlib import Path
from tempfile import TemporaryDirectory

from _stage3_epoch_budget_fixtures import retained_lock_value
from _stage3_scale_learning_curve_fixtures import population_manifest

from lisjong_arena.phase8_sequential.training import FORMAL_TRAINING_CONFIG
from lisjong_arena.stage3_epoch_budget.experiment import (
    CANDIDATE,
    budget_binding,
    budget_population_data,
    budget_train_view,
    configure_torch_runtime,
)
from lisjong_arena.stage3_epoch_budget.protocol import (
    BUDGET_MAX_EPOCHS,
    PATIENCE,
    PRIMARY_AXIS,
    budget_training_config,
)
from lisjong_arena.stage3_scale_learning_curve.experiment import (
    scale_data,
    training_binding,
)

TORCH_AVAILABLE = importlib.util.find_spec("torch") is not None

SHORT_EPOCHS = 3
LONG_EPOCHS = 6
"""実trainingの回数を抑えつつ、prefix一致を実測できる最小のbudget対。

Issue #157のlocked budget (40 / 80) をここで走らせない。確認しているのは
`max_epochs`がepoch上限以外へ影響しないというdeterminism gateの前提である。
"""


def _train(view, epochs: int):
    from lisjong_arena.phase8_sequential.training import train_candidate

    return train_candidate(
        CANDIDATE,
        view.sequences,
        dataset_identity=view.dataset_identity,
        bptt_policy=view.inventory.bptt_policy,
        canonical_validation=view.canonical_validation,
        config=replace(FORMAL_TRAINING_CONFIG, max_epochs=epochs),
    )


def _history(result) -> list[dict]:
    return [
        {
            "epoch": row.epoch,
            "train_mse": row.train_mse,
            "validation_mae": row.validation_mae,
        }
        for row in result.history
    ]


@unittest.skipUnless(TORCH_AVAILABLE, "requires the ml extra")
class DeterminismPremiseTest(unittest.TestCase):
    """`max_epochs`を広げても先頭epochのhistoryが1 bitも変わらないこと。"""

    @classmethod
    def setUpClass(cls):
        configure_torch_runtime()
        cls._directory = TemporaryDirectory()
        root = Path(cls._directory.name)
        lock = retained_lock_value()
        _population, raw, dataset = population_manifest(root, lock)
        cls.lock = lock
        cls.full = budget_population_data(raw, dataset)
        view = budget_train_view(cls.full)
        cls.short = _train(view, SHORT_EPOCHS)
        cls.long = _train(view, LONG_EPOCHS)

    @classmethod
    def tearDownClass(cls):
        cls._directory.cleanup()

    def test_the_longer_budget_reproduces_the_shorter_history_exactly(self):
        self.assertEqual(_history(self.long)[:SHORT_EPOCHS], _history(self.short))

    def test_the_longer_budget_actually_runs_more_epochs(self):
        self.assertEqual(len(self.short.history), SHORT_EPOCHS)
        self.assertEqual(len(self.long.history), LONG_EPOCHS)

    def test_the_two_runs_differ_only_in_the_epoch_budget(self):
        short_config = asdict(self.short.config)
        long_config = asdict(self.long.config)
        self.assertEqual(
            {name for name in short_config if short_config[name] != long_config[name]},
            {PRIMARY_AXIS},
        )
        self.assertEqual(short_config["patience"], PATIENCE)
        self.assertEqual(long_config["patience"], PATIENCE)

    def test_the_shared_prefix_selects_the_same_checkpoint_when_it_is_the_best(self):
        """先頭prefix内にbestがある限り、longer budgetは同じepochを選ぶ。"""
        prefix = _history(self.long)[:SHORT_EPOCHS]
        best = min(row["validation_mae"] for row in prefix)
        self.assertEqual(
            min(row["validation_mae"] for row in _history(self.short)), best
        )


@unittest.skipUnless(TORCH_AVAILABLE, "requires the ml extra")
class BudgetArmWiringTest(unittest.TestCase):
    """E80 armが#150 S64とexactに同じdata viewとbindingを使うこと。"""

    @classmethod
    def setUpClass(cls):
        configure_torch_runtime()
        cls._directory = TemporaryDirectory()
        root = Path(cls._directory.name)
        cls.lock = retained_lock_value()
        _population, raw, dataset = population_manifest(root, cls.lock)
        cls.full = budget_population_data(raw, dataset)

    @classmethod
    def tearDownClass(cls):
        cls._directory.cleanup()

    def test_the_budget_view_is_the_phase10_s64_view(self):
        expected = scale_data(self.full, "S64")
        actual = budget_train_view(self.full)
        self.assertEqual(actual.population_id, expected.population_id)
        self.assertEqual(
            [sequence.key for sequence in actual.train_sequences],
            [sequence.key for sequence in expected.train_sequences],
        )
        self.assertEqual(
            [sequence.key for sequence in actual.validation_sequences],
            [sequence.key for sequence in expected.validation_sequences],
        )
        self.assertIs(actual.inventory.bptt_policy, expected.inventory.bptt_policy)

    def test_the_budget_binding_is_the_phase10_s64_binding(self):
        provenance = self.lock["provenance"]
        self.assertEqual(
            budget_binding(self.full, provenance),
            training_binding(self.full, "S64", provenance),
        )

    def test_the_locked_budget_config_is_eighty_epochs_at_patience_six(self):
        config = asdict(budget_training_config())
        self.assertEqual(config[PRIMARY_AXIS], BUDGET_MAX_EPOCHS)
        self.assertEqual(config["patience"], PATIENCE)
        self.assertEqual(config["seed"], 0)
        self.assertEqual(config["dataloader_seed"], 0)
        self.assertEqual(config["workers"], 0)
        self.assertIs(config["deterministic_algorithms"], True)
        self.assertEqual(config["torch_threads"], 1)


if __name__ == "__main__":
    unittest.main()
