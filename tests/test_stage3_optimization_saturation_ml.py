"""Arena #167のtorch依存test。

Issue #167はE80を再trainingせず、

```text
E160.loss_history[0:80]  ==  #157 retained E80.loss_history   (exact)
```

というdeterminism gateだけを根拠にする。その前提は`max_epochs`がtraining loop
の上限を決めるだけで、RNG stream (`torch.manual_seed(0)` /
`torch.Generator().manual_seed(0)`) にも各epochのupdateにも影響しないこと
である。ここではその前提を、同じsynthetic populationに対する2回の実trainingで
固定する。#157が同じ前提を`3 -> 6`で確認したのに対し、本childは`doubled
budget`の形（`3 -> 6`ではなく`4 -> 8`）でも成立することを見る。

あわせて、E160 artifactのon-disk contract（weights digest / strict S2 load /
byte count）をtampered artifactに対して確認する。

synthetic populationだけを使い、formalな80-hanchan corpusにもE160のlocked
budget (160) にも到達しない。large trainingはunit testへ入れない。
"""

import importlib.util
import unittest
from dataclasses import asdict, replace
from pathlib import Path
from tempfile import TemporaryDirectory

from _stage3_optimization_saturation_fixtures import (
    baseline_manifest_value,
    phase10_lock_value,
    predecessor_lock_value,
    saturation_lock_value,
    saturation_manifest_value,
    scale_manifest_value,
)
from _stage3_scale_learning_curve_fixtures import population_manifest

from lisjong_arena.phase4_raw_corpus.codec import canonical_json_bytes
from lisjong_arena.phase8_sequential.training import FORMAL_TRAINING_CONFIG
from lisjong_arena.stage3_optimization_saturation.artifact import (
    MANIFEST_FILENAME,
    WEIGHTS_FILENAME,
    load_model,
    load_model_artifact,
)
from lisjong_arena.stage3_optimization_saturation.experiment import (
    CANDIDATE,
    configure_torch_runtime,
    saturation_binding,
    saturation_population_data,
    saturation_train_view,
)
from lisjong_arena.stage3_optimization_saturation.protocol import (
    PATIENCE,
    PRIMARY_AXIS,
    SATURATION_MAX_EPOCHS,
    SaturationError,
    saturation_training_config,
)
from lisjong_arena.stage3_scale_learning_curve.experiment import (
    scale_data,
    training_binding,
)

TORCH_AVAILABLE = importlib.util.find_spec("torch") is not None

SHORT_EPOCHS = 4
LONG_EPOCHS = 8
"""実trainingの回数を抑えつつ、doubled budgetのprefix一致を実測できる最小の対。

Issue #167のlocked budget (80 / 160) をここで走らせない。確認しているのは
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
    """`max_epochs`を倍にしても先頭epochのhistoryが1 bitも変わらないこと。"""

    @classmethod
    def setUpClass(cls):
        configure_torch_runtime()
        cls._directory = TemporaryDirectory()
        root = Path(cls._directory.name)
        lock = phase10_lock_value()
        _population, raw, dataset = population_manifest(root, lock)
        cls.lock = lock
        cls.full = saturation_population_data(raw, dataset)
        view = saturation_train_view(cls.full)
        cls.short = _train(view, SHORT_EPOCHS)
        cls.long = _train(view, LONG_EPOCHS)

    @classmethod
    def tearDownClass(cls):
        cls._directory.cleanup()

    def test_the_doubled_budget_reproduces_the_shorter_history_exactly(self):
        self.assertEqual(_history(self.long)[:SHORT_EPOCHS], _history(self.short))

    def test_the_shared_prefix_matches_on_canonical_bytes(self):
        """gateはfloat比較ではなくcanonical bytes比較である。"""
        self.assertEqual(
            canonical_json_bytes(_history(self.long)[:SHORT_EPOCHS]),
            canonical_json_bytes(_history(self.short)),
        )

    def test_the_doubled_budget_actually_runs_more_epochs(self):
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
class SaturationArmWiringTest(unittest.TestCase):
    """E160 armが#150 S64 / #157 E80とexactに同じdata viewとbindingを使うこと。"""

    @classmethod
    def setUpClass(cls):
        configure_torch_runtime()
        cls._directory = TemporaryDirectory()
        root = Path(cls._directory.name)
        cls.lock = phase10_lock_value()
        _population, raw, dataset = population_manifest(root, cls.lock)
        cls.full = saturation_population_data(raw, dataset)

    @classmethod
    def tearDownClass(cls):
        cls._directory.cleanup()

    def test_the_saturation_view_is_the_phase10_s64_view(self):
        expected = scale_data(self.full, "S64")
        actual = saturation_train_view(self.full)
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

    def test_the_saturation_binding_is_the_phase10_s64_binding(self):
        provenance = self.lock["provenance"]
        self.assertEqual(
            saturation_binding(self.full, provenance),
            training_binding(self.full, "S64", provenance),
        )

    def test_the_locked_saturation_config_is_one_sixty_epochs_at_patience_six(self):
        config = asdict(saturation_training_config())
        self.assertEqual(config[PRIMARY_AXIS], SATURATION_MAX_EPOCHS)
        self.assertEqual(config[PRIMARY_AXIS], 160)
        self.assertEqual(config["patience"], PATIENCE)
        self.assertEqual(config["seed"], 0)
        self.assertEqual(config["dataloader_seed"], 0)
        self.assertEqual(config["workers"], 0)
        self.assertIs(config["deterministic_algorithms"], True)
        self.assertEqual(config["torch_threads"], 1)


@unittest.skipUnless(TORCH_AVAILABLE, "requires the ml extra")
class TamperedArtifactTest(unittest.TestCase):
    """on-diskのE160 artifactが記録どおりでなければloadを拒否すること。"""

    @classmethod
    def setUpClass(cls):
        import torch

        from lisjong_arena.phase8_sequential.model import create_model
        from lisjong_arena.phase8_sequential.protocol import Candidate

        configure_torch_runtime()
        cls._directory = TemporaryDirectory()
        cls.root = Path(cls._directory.name)
        cls.phase10_lock = phase10_lock_value()
        cls.population, _raw, _dataset = population_manifest(
            cls.root / "population", cls.phase10_lock
        )
        cls.scale = scale_manifest_value(cls.population, cls.phase10_lock)
        cls.predecessor_lock = predecessor_lock_value()
        cls.baseline = baseline_manifest_value(
            cls.population, cls.predecessor_lock, cls.scale
        )
        cls.lock = saturation_lock_value()
        cls.state_dict = create_model(Candidate.S2).state_dict()
        cls.torch = torch

    @classmethod
    def tearDownClass(cls):
        cls._directory.cleanup()

    def _publish(self, name: str, **manifest_overrides) -> Path:
        """digestまで整合した本物のartifact directoryを1つ作る。"""
        destination = self.root / name
        destination.mkdir()
        weights_path = destination / WEIGHTS_FILENAME
        self.torch.save(self.state_dict, weights_path)
        weights = weights_path.read_bytes()
        import hashlib

        manifest = saturation_manifest_value(
            self.population,
            self.lock,
            self.baseline,
            weights_bytes=len(weights),
            weights_sha256=hashlib.sha256(weights).hexdigest(),
            **manifest_overrides,
        )
        (destination / MANIFEST_FILENAME).write_bytes(canonical_json_bytes(manifest))
        return destination

    def _load(self, destination: Path):
        return load_model_artifact(
            destination, self.population, self.lock, self.baseline
        )

    def test_an_intact_artifact_loads_into_the_locked_s2_model(self):
        destination = self._publish("intact")
        loaded = self._load(destination)
        self.assertEqual(loaded.manifest["arm"], "E160")
        model, manifest = load_model(
            destination, self.population, self.lock, self.baseline
        )
        self.assertEqual(manifest["selected_epoch"], SATURATION_MAX_EPOCHS)
        self.assertEqual(
            sum(parameter.numel() for parameter in model.parameters()),
            manifest["training_lock"]["parameter_count"],
        )

    def test_rewritten_weights_bytes_are_rejected(self):
        destination = self._publish("rewritten-weights")
        (destination / WEIGHTS_FILENAME).write_bytes(b"not a checkpoint")
        with self.assertRaises(SaturationError):
            self._load(destination)

    def test_a_weights_digest_that_does_not_match_the_file_is_rejected(self):
        destination = self.root / "wrong-digest"
        destination.mkdir()
        weights_path = destination / WEIGHTS_FILENAME
        self.torch.save(self.state_dict, weights_path)
        weights = weights_path.read_bytes()
        manifest = saturation_manifest_value(
            self.population,
            self.lock,
            self.baseline,
            weights_bytes=len(weights),
            weights_sha256="a" * 64,
        )
        (destination / MANIFEST_FILENAME).write_bytes(canonical_json_bytes(manifest))
        with self.assertRaises(SaturationError):
            self._load(destination)

    def test_a_checkpoint_from_another_model_family_is_rejected(self):
        destination = self.root / "foreign-checkpoint"
        destination.mkdir()
        weights_path = destination / WEIGHTS_FILENAME
        self.torch.save({"not_s2": self.torch.zeros(3)}, weights_path)
        weights = weights_path.read_bytes()
        import hashlib

        manifest = saturation_manifest_value(
            self.population,
            self.lock,
            self.baseline,
            weights_bytes=len(weights),
            weights_sha256=hashlib.sha256(weights).hexdigest(),
        )
        (destination / MANIFEST_FILENAME).write_bytes(canonical_json_bytes(manifest))
        # byte countもSHA-256も整合しているが、strict loadは通らない。
        self._load(destination)
        with self.assertRaises(SaturationError):
            load_model(destination, self.population, self.lock, self.baseline)

    def test_a_non_canonical_manifest_is_rejected(self):
        destination = self._publish("non-canonical")
        manifest = destination / MANIFEST_FILENAME
        manifest.write_bytes(b" " + manifest.read_bytes())
        with self.assertRaises(SaturationError):
            self._load(destination)

    def test_missing_or_extra_files_are_rejected(self):
        destination = self._publish("extra-file")
        (destination / "notes.txt").write_bytes(b"extra")
        with self.assertRaises(SaturationError):
            self._load(destination)


if __name__ == "__main__":
    unittest.main()
