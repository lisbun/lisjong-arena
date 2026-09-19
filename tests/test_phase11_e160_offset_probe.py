"""Contract tests for Arena #291's matched frozen-E160 diagnostic."""

import ast
import inspect
import unittest
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory

import lisjong_arena.phase11_e160_offset_probe.model as probe_model
from lisjong_arena.phase5_belief_dataset.model import DatasetPartition
from lisjong_arena.phase11_e160_offset_probe.__main__ import _parser
from lisjong_arena.phase11_e160_offset_probe.artifact import _read_json
from lisjong_arena.phase11_e160_offset_probe.data import (
    centered_latent,
    centering_receipt,
    coverage_value,
    latent_fingerprint,
)
from lisjong_arena.phase11_e160_offset_probe.evaluation import classify_interval
from lisjong_arena.phase11_e160_offset_probe.model import predict_probability
from lisjong_arena.phase11_e160_offset_probe.protocol import (
    CENTERED_MEAN_ABSOLUTE_TOLERANCE,
    CENTERING_SEMANTICS_ID,
    E160_OFFSET_INCONCLUSIVE,
    E160_OFFSET_REGRESSION,
    E160_OFFSET_SIGNAL,
    FROZEN_E160_STATE_DIGEST,
    LATENT_DIM,
    OUTPUT_DIM,
    PARAMETER_COUNT,
    SELECTION_EXPOSURE_AFTER,
    SELECTION_EXPOSURE_BEFORE,
    SOLVER,
    E160OffsetProbeError,
    centering_value,
    evaluation_value,
    probe_value,
    retained_value,
)
from lisjong_arena.phase11_public_riichi_wait_readout.data import (
    LatentExample,
    OpponentTarget,
)


def _target(
    row: int,
    *,
    eligible: bool = True,
    positive_tile: int | None = 0,
) -> OpponentTarget:
    mask = None
    if eligible:
        values = [0] * 34
        if positive_tile is not None:
            values[positive_tile] = 1
        mask = tuple(values)
    return OpponentTarget(
        wind=("south", "west", "north")[row],
        seat=row + 1,
        established=eligible,
        mask=mask,
        unavailable_reason=None if eligible else "fixture",
        riichi_junme=6 if eligible else None,
        open_closed="closed",
    )


def _record(
    partition: DatasetPartition,
    seed: int,
    latent_value: float,
    *,
    eligible_rows: tuple[int, ...] = (0, 1, 2),
) -> LatentExample:
    return LatentExample(
        partition=partition,
        source_class="fixture",
        game_seed=seed,
        round_index=0,
        anchor_identity=f"{seed:064x}",
        depth=1,
        opponent_winds=("south", "west", "north"),
        latent=tuple([latent_value] * LATENT_DIM),
        targets=tuple(_target(row, eligible=row in eligible_rows) for row in range(3)),
    )


class LockedProtocolTest(unittest.TestCase):
    def test_probe_architecture_and_solver_are_exact(self):
        probe = probe_value()
        self.assertEqual(probe["weight_shape"], [3, 34, 128])
        self.assertEqual(OUTPUT_DIM, 102)
        self.assertEqual(PARAMETER_COUNT, 13_056)
        self.assertFalse(probe["free_intercept"])
        self.assertEqual(probe["hidden_layers"], [])
        self.assertIsNone(probe["activation"])
        self.assertFalse(probe["trunk_update"])
        self.assertEqual(SOLVER["family"].split()[0], "torch.optim.LBFGS")
        self.assertEqual(SOLVER["l2_regularization"], 0.0)
        self.assertIsNone(SOLVER["training_seed"])

    def test_retained_and_governance_contract_are_fixed(self):
        retained = retained_value()
        self.assertEqual(retained["train_seeds"], list(range(360, 424)))
        self.assertEqual(retained["validation_seeds"], list(range(424, 440)))
        self.assertFalse(retained["formal_test"])
        self.assertEqual(
            retained["frozen_e160_state_digest"],
            FROZEN_E160_STATE_DIGEST,
        )
        evaluation = evaluation_value()
        self.assertEqual(SELECTION_EXPOSURE_BEFORE, 5)
        self.assertEqual(SELECTION_EXPOSURE_AFTER, 6)
        self.assertEqual(evaluation["selection_exposure_before"], 5)
        self.assertEqual(evaluation["selection_exposure_after"], 6)

    def test_centering_contract_is_train_only_and_unscaled(self):
        contract = centering_value()
        self.assertEqual(contract["semantics_id"], CENTERING_SEMANTICS_ID)
        self.assertEqual(contract["fit_partition"], "train")
        self.assertEqual(contract["vectors"], 3)
        self.assertEqual(contract["dimension"], 128)
        self.assertFalse(contract["validation_influence"])
        self.assertEqual(contract["scaling"], "none")
        self.assertFalse(contract["pca"])
        self.assertFalse(contract["whitening"])
        self.assertEqual(
            contract["centered_mean_absolute_tolerance"],
            CENTERED_MEAN_ABSOLUTE_TOLERANCE,
        )

    def test_cli_has_no_solver_centering_or_rescue_override(self):
        parser = _parser()
        command = next(action for action in parser._actions if action.dest == "command")
        self.assertEqual(
            set(command.choices),
            {"plan", "lock", "preflight", "train", "evaluate", "verify"},
        )
        for subparser in command.choices.values():
            options = {
                option
                for action in subparser._actions
                for option in action.option_strings
            }
            for forbidden in (
                "--solver",
                "--seed",
                "--regularization",
                "--learning-rate",
                "--centering",
                "--bias",
                "--resume",
                "--rescue",
                "--hpo",
            ):
                self.assertNotIn(forbidden, options)


class CenteringAndLatentTest(unittest.TestCase):
    def test_centering_is_output_row_specific_and_train_only(self):
        records = (
            _record(
                DatasetPartition.TRAIN,
                1,
                1.0,
                eligible_rows=(0,),
            ),
            _record(
                DatasetPartition.TRAIN,
                2,
                2.0,
                eligible_rows=(1,),
            ),
            _record(
                DatasetPartition.TRAIN,
                3,
                3.0,
                eligible_rows=(2,),
            ),
        )
        centering = centering_receipt(records)
        self.assertEqual(centering["eligible_rows_by_output_row"], [1, 1, 1])
        self.assertEqual(centering["means"][0], [1.0] * LATENT_DIM)
        self.assertEqual(centering["means"][1], [2.0] * LATENT_DIM)
        self.assertEqual(centering["means"][2], [3.0] * LATENT_DIM)
        self.assertLessEqual(
            centering["max_abs_centered_train_mean"],
            CENTERED_MEAN_ABSOLUTE_TOLERANCE,
        )
        for row_index, record in enumerate(records):
            self.assertEqual(
                centered_latent(record, row_index, centering),
                (0.0,) * LATENT_DIM,
            )

        validation = (_record(DatasetPartition.VALIDATION, 4, 99.0),)
        with self.assertRaisesRegex(E160OffsetProbeError, "TRAIN records only"):
            centering_receipt(validation)

    def test_latent_fingerprint_changes_when_latent_changes(self):
        first = (_record(DatasetPartition.TRAIN, 1, 1.0),)
        second = (
            replace(
                first[0],
                latent=tuple([1.0] * (LATENT_DIM - 1) + [1.5]),
            ),
        )
        self.assertNotEqual(latent_fingerprint(first), latent_fingerprint(second))

    def test_coverage_uses_only_established_available_rows(self):
        records = (
            _record(
                DatasetPartition.VALIDATION,
                424,
                0.0,
                eligible_rows=(0, 2),
            ),
        )
        coverage = coverage_value(records)
        self.assertEqual(coverage["eligible_hanchan"], 1)
        self.assertEqual(coverage["eligible_rows"], 2)
        self.assertEqual(coverage["eligible_cells"], 68)
        self.assertEqual(coverage["eligible_rows_by_output_row"], [1, 0, 1])


class OffsetAndEvaluationBoundaryTest(unittest.TestCase):
    def test_zero_correction_exactly_reproduces_prevalence(self):
        features = (0.25,) * LATENT_DIM
        for probability in (0.001, 0.2, 0.5, 0.999):
            self.assertEqual(
                predict_probability(
                    probability,
                    [0.0] * LATENT_DIM,
                    features,
                ),
                probability,
            )

    def test_classification_boundaries_are_exhaustive(self):
        self.assertEqual(classify_interval(0.01, 0.02), E160_OFFSET_SIGNAL)
        self.assertEqual(
            classify_interval(-0.02, -0.01),
            E160_OFFSET_REGRESSION,
        )
        self.assertEqual(
            classify_interval(-0.01, 0.01),
            E160_OFFSET_INCONCLUSIVE,
        )
        self.assertEqual(
            classify_interval(0.0, 0.01),
            E160_OFFSET_INCONCLUSIVE,
        )
        self.assertEqual(
            classify_interval(-0.01, 0.0),
            E160_OFFSET_INCONCLUSIVE,
        )

    def test_model_module_does_not_import_classical_feature_builder(self):
        tree = ast.parse(inspect.getsource(probe_model))
        imported = {
            node.module or ""
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom)
        }
        self.assertFalse(
            any("phase11_classical_wait_baseline.data" in name for name in imported)
        )


class ArtifactStrictReadbackTest(unittest.TestCase):
    def test_noncanonical_json_bytes_are_rejected(self):
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "artifact.json"
            path.write_text('{"a": 1}', encoding="utf-8")
            with self.assertRaisesRegex(E160OffsetProbeError, "canonical JSON"):
                _read_json(path, "fixture")


if __name__ == "__main__":
    unittest.main()
