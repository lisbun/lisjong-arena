"""Small deterministic contract checks for the measurement-only S2 profiler."""

import copy
import importlib.util
import json
import math
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from _phase4_raw_corpus_fixtures import fixture_corpus

from lisjong_arena.handbelief_throughput import (
    COMPONENTS,
    ThroughputError,
    _one_epoch,
    load_result,
    make_result,
    profile,
    provenance,
    save_result,
    validate_result,
)
from lisjong_arena.phase4_raw_corpus.persistence import save_raw_corpus
from lisjong_arena.phase5_belief_dataset.builder import (
    build_phase5_belief_dataset,
    resolve_training_samples,
)
from lisjong_arena.phase5_belief_dataset.model import DatasetPartition
from lisjong_arena.phase5_belief_dataset.split import FirstPartySplitPolicy
from lisjong_arena.phase6_snapshot.training import materialize_snapshot_example
from lisjong_arena.phase8_sequential.protocol import (
    BpttMode,
    BpttPolicy,
    Phase8Sequence,
    SequenceKey,
)
from lisjong_arena.stage3_optimization_saturation.retained import retained_value


@unittest.skipUnless(importlib.util.find_spec("torch"), "requires PyTorch")
class ThroughputTest(unittest.TestCase):
    def test_live_runtime_provenance_uses_plain_json_strings(self):
        from lisjong_arena.stage3_entry_gate.experiment import configure_torch_runtime

        configure_torch_runtime()
        with (
            patch(
                "lisjong_arena.handbelief_throughput.subprocess.check_output",
                side_effect=["a" * 40, ""],
            ),
            patch(
                "lisjong_arena.handbelief_throughput.platform.platform",
                return_value="test OS",
            ),
            patch(
                "lisjong_arena.handbelief_throughput.platform.processor",
                return_value="test CPU",
            ),
            patch(
                "lisjong_arena.handbelief_throughput.importlib.metadata.version",
                return_value="0.4.8",
            ),
        ):
            source = provenance()
        self.assertIs(type(source["runtime"]["torch"]), str)

    def test_historical_measurement_rejects_current_backend(self):
        from lisjong_arena.stage3_entry_gate.experiment import configure_torch_runtime

        configure_torch_runtime()
        with (
            patch(
                "lisjong_arena.handbelief_throughput.subprocess.check_output",
                side_effect=["a" * 40, ""],
            ),
            self.assertRaisesRegex(
                ThroughputError, "measurement runtime differs from the locked CPU path"
            ),
        ):
            provenance()

    def _data(self, root):
        raw = save_raw_corpus(fixture_corpus(), root / "raw")
        dataset = build_phase5_belief_dataset(raw, FirstPartySplitPolicy.ACCEPTANCE)
        samples = resolve_training_samples(dataset, raw)
        train = (
            replace(dataset.examples[0], partition=DatasetPartition.TRAIN),
            samples[0],
        )
        validation = (
            replace(dataset.examples[1], partition=DatasetPartition.VALIDATION),
            samples[1],
        )

        def sequence(pair):
            ref, sample = pair
            step = materialize_snapshot_example(ref, sample)
            return Phase8Sequence(
                SequenceKey(ref.game, ref.round_index, ref.viewer_seat),
                ref.partition,
                (step,),
            )

        return SimpleNamespace(
            train_sequences=(sequence(train),),
            validation_sequences=(sequence(validation),),
            inventory=SimpleNamespace(
                bptt_policy=BpttPolicy(BpttMode.FULL_SEQUENCE, None)
            ),
            canonical_validation=SimpleNamespace(
                examples=(sequence(validation).steps[0],)
            ),
            dataset_identity=dataset.dataset_identity,
        )

    def _source(self):
        return {
            "revisions": {
                "arena": "a" * 40,
                "lisjong": "b" * 40,
                "lisjong_engine": "c" * 40,
            },
            "runtime": {
                "python": "3.14.6",
                "torch": "2.13.0+cpu",
                "riichienv": "0.4.8",
                "os": "test OS",
                "cpu": "test CPU",
                "torch_threads": 1,
                "deterministic_algorithms": True,
                "free_threaded": False,
            },
            "retained": retained_value(),
        }

    def test_profile_preserves_values_updates_and_exclusive_components(self):
        import torch

        with tempfile.TemporaryDirectory() as directory:
            data = self._data(Path(directory))
            plain, plain_loss, plain_mae, plain_state, plain_output = _one_epoch(
                data, profiled=False
            )
            measured, measured_loss, measured_mae, measured_state, measured_output = (
                _one_epoch(data, profiled=True)
            )
            self.assertEqual((plain_loss, plain_mae), (measured_loss, measured_mae))
            self.assertEqual(plain_output, measured_output)
            self.assertTrue(
                all(
                    torch.equal(plain_state[name], measured_state[name])
                    for name in plain_state
                )
            )
            self.assertEqual(set(measured["components"]), set(COMPONENTS))
            self.assertAlmostEqual(
                sum(row["wall_seconds"] for row in measured["components"].values()),
                measured["epoch_wall_seconds"],
            )
            workload, samples = profile(data)
            self.assertEqual(
                (workload["train_steps"], workload["validation_steps"]), (1, 1)
            )
            self.assertEqual(len(samples), 3)
            self.assertEqual(
                samples[0]["profiled"]["components"]["train_forward_constraint"][
                    "calls"
                ],
                1,
            )
            self.assertTrue(
                all(
                    math.isfinite(row["cpu_seconds"]) and row["cpu_seconds"] >= 0
                    for sample in samples
                    for row in sample["profiled"]["components"].values()
                )
            )

    def test_artifact_readback_and_corruption_rejection(self):
        with tempfile.TemporaryDirectory() as directory:
            data = self._data(Path(directory))
            workload, samples = profile(data)
            value = make_result(workload, samples, self._source())
            path = Path(directory) / "profile.json"
            save_result(path, value)
            self.assertEqual(load_result(path)["summary"], value["summary"])
            with self.assertRaises(FileExistsError):
                save_result(path, value)
            corrupted = json.loads(path.read_text(encoding="utf-8"))
            corrupted["samples"][0]["profiled"]["components"]["train_tensor"][
                "calls"
            ] = 99
            path.write_text(json.dumps(corrupted), encoding="utf-8")
            with self.assertRaises(ThroughputError):
                load_result(path)
            changed = copy.deepcopy(value)
            changed["samples"][0]["profiled"]["components"]["train_tensor"]["calls"] = (
                99
            )
            with self.assertRaises(ThroughputError):
                make_result(workload, changed["samples"], self._source())
            changed = copy.deepcopy(value)
            changed["samples"][0]["profiled"]["components"]["train_tensor"][
                "wall_seconds"
            ] = -1.0
            with self.assertRaises(ThroughputError):
                make_result(workload, changed["samples"], self._source())
            changed = copy.deepcopy(value)
            changed["samples"][0]["profiled"]["components"]["train_tensor"][
                "wall_seconds"
            ] = float("nan")
            with self.assertRaises(ThroughputError):
                make_result(workload, changed["samples"], self._source())
            changed = copy.deepcopy(value)
            changed["workload"]["forward_calls"] += 1
            with self.assertRaises(ThroughputError):
                make_result(changed["workload"], samples, self._source())
            changed = copy.deepcopy(value)
            changed["samples"][0]["profiled"]["components"]["train_tensor"][
                "cpu_seconds"
            ] += 1.0
            with self.assertRaises(ThroughputError):
                make_result(workload, changed["samples"], self._source())
            changed = copy.deepcopy(value)
            changed["summary"]["largest_component"] = "not_a_component"
            with self.assertRaises(ThroughputError):
                validate_result(changed)
            self.assertNotIn("optimization", value)
