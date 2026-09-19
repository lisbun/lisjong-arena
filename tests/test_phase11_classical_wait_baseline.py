"""Contract tests for Arena #222's classical wait baseline."""

import ast
import inspect
import unittest
from unittest.mock import patch
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory

from lisjong_engine.public_state import PublicRiichiStatus
from lisjong_engine.wind import Wind

import lisjong_arena.phase11_classical_wait_baseline.data as classical_data
import lisjong_arena.phase11_classical_wait_baseline.lock as classical_lock
from lisjong_arena.phase5_belief_dataset.model import DatasetPartition
from lisjong_arena.phase6_snapshot.feature import (
    OpponentSnapshotFeature,
    Phase6SnapshotFeature,
)
from lisjong_arena.phase11_classical_wait_baseline.__main__ import _parser
from lisjong_arena.phase11_classical_wait_baseline.artifact import _read_json
from lisjong_arena.phase11_classical_wait_baseline.data import (
    ClassicalExample,
    coverage_value,
    feature_vector,
)
from lisjong_arena.phase11_classical_wait_baseline.evaluation import classify_interval
from lisjong_arena.phase11_classical_wait_baseline.model import predict_probability
from lisjong_arena.phase11_classical_wait_baseline.protocol import (
    CLASSICAL_INCONCLUSIVE,
    CLASSICAL_REGRESSION,
    CLASSICAL_SIGNAL,
    EXPECTED_BASELINE_LOG_LOSS,
    FEATURE_DIM,
    FEATURE_NAMES,
    SOLVER,
    ClassicalWaitError,
    evaluation_value,
    feature_value,
    retained_value,
)
from lisjong_arena.phase11_public_riichi_wait_readout.data import OpponentTarget


def _opponent(wind: Wind, discards: tuple[int, ...] | None = None):
    zero34 = (0,) * 34
    return OpponentSnapshotFeature(
        wind=wind,
        concealed_slot_count=13,
        riichi_status=PublicRiichiStatus.ESTABLISHED,
        public_meld_tile_counts=zero34,
        meld_kind_counts=(0,) * 5,
        discard_counts=zero34 if discards is None else discards,
        tedashi_counts=zero34,
        tsumogiri_counts=zero34,
        last_discard_orders=zero34,
        last_discard_present=zero34,
        public_draw_source_counts=(0,) * 3,
        riichi_declaration_present=1,
        riichi_declaration_discard_order=0,
        last_call_evidence_position=0,
        last_call_present=0,
        last_kan_evidence_position=0,
        last_kan_present=0,
        discard_no_public_response_counts=zero34,
        kakan_no_public_response_count=0,
        ankan_no_public_response_count=0,
    )


def _snapshot(
    *,
    remaining: tuple[int, ...] | None = None,
    target_wind: Wind = Wind.EAST,
    target_discards: tuple[int, ...] | None = None,
):
    winds = (Wind.EAST, Wind.SOUTH, Wind.WEST)
    opponents = tuple(
        _opponent(
            wind,
            target_discards if wind is target_wind else None,
        )
        for wind in winds
    )
    return Phase6SnapshotFeature(
        viewer_wind=Wind.NORTH,
        prevailing_wind=Wind.EAST,
        dealer_relation=0,
        hand_number=1,
        honba=0,
        riichi_sticks=0,
        remaining_live_wall_count=30,
        global_public_discard_count=0,
        evidence_prefix_length=1,
        scores_by_wind=(25000, 25000, 25000, 25000),
        own_base_tile_counts=(0,) * 34,
        remaining_tile_counts=(4,) * 34 if remaining is None else remaining,
        visible_dora_indicator_counts=(0,) * 34,
        opponents=opponents,
        response_history_counts=(0,) * 9,
    )


def _target(
    *,
    wind: Wind = Wind.EAST,
    mask: tuple[int, ...] | None = None,
    junme: int = 6,
    established: bool = True,
):
    return OpponentTarget(
        wind=wind.value,
        seat=1,
        established=established,
        mask=(0,) * 34 if mask is None else mask,
        unavailable_reason=None,
        riichi_junme=junme if established else None,
        open_closed="closed",
    )


def _other_target(wind: Wind):
    return OpponentTarget(
        wind=wind.value,
        seat=2,
        established=False,
        mask=(0,) * 34,
        unavailable_reason=None,
        riichi_junme=None,
        open_closed="closed",
    )


def _record(mask: tuple[int, ...] | None = None):
    return ClassicalExample(
        partition=DatasetPartition.VALIDATION,
        source_class="fixture",
        game_seed=424,
        round_index=0,
        anchor_identity="a" * 64,
        depth=1,
        snapshot=_snapshot(),
        targets=(
            _target(mask=mask),
            _other_target(Wind.SOUTH),
            _other_target(Wind.WEST),
        ),
    )


class LockedProtocolTest(unittest.TestCase):
    def test_primary_feature_set_is_exact_and_has_no_static_tile_class_terms(self):
        self.assertEqual(FEATURE_DIM, 13)
        self.assertEqual(
            FEATURE_NAMES,
            (
                "candidate_unseen_fraction",
                "penchan_possible",
                "kanchan_possible",
                "ryanmen_low_possible",
                "ryanmen_high_possible",
                "penchan_support",
                "kanchan_support",
                "ryanmen_low_support",
                "ryanmen_high_support",
                "candidate_seen_in_opponent_river",
                "ryanmen_low_counterpart_seen_in_opponent_river",
                "ryanmen_high_counterpart_seen_in_opponent_river",
                "riichi_declaration_turn_normalized",
            ),
        )
        feature_contract = feature_value()
        self.assertFalse(feature_contract["static_tile_class_terms_in_primary_model"])
        self.assertEqual(len(feature_contract["fingerprint"]), 64)
        self.assertIn("red and normal five", feature_contract["base_tile_axis"])
        self.assertEqual(
            feature_value()["local_support_formula"],
            "min(unseen(required_a), unseen(required_b)) / 4",
        )

    def test_solver_is_fully_predeclared_and_has_no_validation_selection(self):
        self.assertEqual(SOLVER["initialization"], "all-zero correction weights")
        self.assertFalse(SOLVER["free_intercept"])
        self.assertEqual(SOLVER["history_size"], 100)
        self.assertEqual(SOLVER["l2_regularization"], 0.0)
        self.assertIsNone(SOLVER["training_seed"])
        self.assertIn("unweighted binary log loss", SOLVER["objective"])

    def test_retained_membership_and_selection_exposure_are_fixed(self):
        retained = retained_value()
        self.assertEqual(retained["train_seeds"], list(range(360, 424)))
        self.assertEqual(retained["validation_seeds"], list(range(424, 440)))
        self.assertFalse(retained["formal_test"])
        evaluation = evaluation_value()
        self.assertEqual(evaluation["selection_exposure_before"], 4)
        self.assertEqual(evaluation["selection_exposure_after"], 5)
        self.assertEqual(
            evaluation["baseline_reference_log_loss"],
            EXPECTED_BASELINE_LOG_LOSS,
        )

    def test_cli_has_no_feature_solver_seed_or_rescue_overrides(self):
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
                "--feature",
                "--solver",
                "--seed",
                "--regularization",
                "--learning-rate",
                "--resume",
                "--rescue",
                "--hpo",
            ):
                self.assertNotIn(forbidden, options)


class ExecutionProvenanceTest(unittest.TestCase):
    def test_editable_arena_revision_is_bound_to_clean_source_head(self):
        provenance = {
            "source_revisions": {
                "lisjong": "1" * 40,
                "lisjong_engine": "2" * 40,
                "lisjong_arena": None,
            },
            "fully_resolved": False,
        }
        with (
            patch.object(classical_lock, "phase4_provenance", return_value=object()),
            patch.object(
                classical_lock,
                "_provenance_value",
                return_value=provenance,
            ),
        ):
            actual = classical_lock._current_provenance("3" * 40)
        self.assertEqual(actual["source_revisions"]["lisjong_arena"], "3" * 40)
        self.assertTrue(actual["fully_resolved"])

    def test_resolved_arena_metadata_must_match_clean_source_head(self):
        provenance = {
            "source_revisions": {
                "lisjong": "1" * 40,
                "lisjong_engine": "2" * 40,
                "lisjong_arena": "4" * 40,
            },
            "fully_resolved": True,
        }
        with (
            patch.object(classical_lock, "phase4_provenance", return_value=object()),
            patch.object(
                classical_lock,
                "_provenance_value",
                return_value=provenance,
            ),
        ):
            with self.assertRaisesRegex(
                ClassicalWaitError,
                "installed Arena revision against clean source HEAD",
            ):
                classical_lock._current_provenance("3" * 40)


class ArtifactStrictReadbackTest(unittest.TestCase):
    def test_noncanonical_json_bytes_are_rejected(self):
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "artifact.json"
            path.write_text('{"a": 1}', encoding="utf-8")
            with self.assertRaisesRegex(ClassicalWaitError, "canonical JSON"):
                _read_json(path, "fixture")


class FeatureSemanticsTest(unittest.TestCase):
    def test_candidate_unseen_count_supports_full_zero_to_four_range(self):
        for count in range(5):
            remaining = list((4,) * 34)
            remaining[27] = count
            values = feature_vector(
                _snapshot(remaining=tuple(remaining)),
                _target(),
                27,
            )
            self.assertEqual(values[0], count / 4)
            self.assertEqual(values[1:9], (0.0,) * 8)

    def test_red_and_normal_five_share_the_single_canonical_base_kind(self):
        remaining = list((4,) * 34)
        remaining[4] = 2
        values = feature_vector(
            _snapshot(remaining=tuple(remaining)),
            _target(),
            4,
        )
        self.assertEqual(values[0], 0.5)
        self.assertEqual(len(_snapshot().remaining_tile_counts), 34)

    def test_penchan_kanchan_and_both_ryanmen_geometries_are_fixed(self):
        # 3m: penchan requires 1m/2m, kanchan 2m/4m, low ryanmen 4m/5m.
        values = feature_vector(_snapshot(), _target(), 2)
        self.assertEqual(values[1:5], (1.0, 1.0, 1.0, 0.0))
        self.assertEqual(values[5:9], (1.0, 1.0, 1.0, 0.0))

        # 5m: kanchan 4m/6m plus both ryanmen directions.
        values = feature_vector(_snapshot(), _target(), 4)
        self.assertEqual(values[1:5], (0.0, 1.0, 1.0, 1.0))
        self.assertEqual(values[5:9], (0.0, 1.0, 1.0, 1.0))

    def test_exhausted_required_tile_disables_only_relevant_mechanisms(self):
        remaining = list((4,) * 34)
        remaining[0] = 0  # 1m exhausted: disables 3m penchan, not its other shapes.
        values = feature_vector(
            _snapshot(remaining=tuple(remaining)),
            _target(),
            2,
        )
        self.assertEqual(values[1], 0.0)
        self.assertEqual(values[5], 0.0)
        self.assertEqual(values[2], 1.0)
        self.assertEqual(values[3], 1.0)

    def test_river_indicators_are_features_not_probability_hard_zeroes(self):
        discards = list((0,) * 34)
        discards[4] = 1  # candidate 5m already in opponent river.
        discards[7] = 1  # low-side counterpart 8m.
        discards[1] = 1  # high-side counterpart 2m.
        values = feature_vector(
            _snapshot(target_discards=tuple(discards)),
            _target(),
            4,
        )
        self.assertEqual(values[9:12], (1.0, 1.0, 1.0))
        probability = predict_probability(0.2, [0.0] * FEATURE_DIM, values)
        self.assertEqual(probability, 0.2)

    def test_support_uses_minimum_unseen_count_divided_by_four(self):
        remaining = list((4,) * 34)
        remaining[3] = 2  # 4m
        remaining[5] = 1  # 6m
        values = feature_vector(
            _snapshot(remaining=tuple(remaining)),
            _target(),
            4,  # 5m kanchan requires 4m/6m
        )
        self.assertEqual(values[2], 1.0)
        self.assertEqual(values[6], 0.25)

    def test_riichi_timing_is_the_only_timing_feature_and_is_normalized_by_18(self):
        values = feature_vector(_snapshot(), _target(junme=9), 27)
        self.assertEqual(values[-1], 0.5)
        timing_names = [name for name in FEATURE_NAMES if "turn" in name]
        self.assertEqual(timing_names, ["riichi_declaration_turn_normalized"])

    def test_structural_wait_truth_does_not_enter_feature_construction(self):
        target_zero = _target(mask=(0,) * 34)
        target_one = replace(target_zero, mask=(1,) * 34)
        self.assertEqual(
            feature_vector(_snapshot(), target_zero, 4),
            feature_vector(_snapshot(), target_one, 4),
        )

    def test_invalid_tile_index_fails_closed_instead_of_negative_indexing(self):
        with self.assertRaises(ClassicalWaitError):
            feature_vector(_snapshot(), _target(), -1)
        with self.assertRaises(ClassicalWaitError):
            feature_vector(_snapshot(), _target(), 34)

    def test_duplicate_public_wind_mapping_fails_closed(self):
        snapshot = _snapshot()
        duplicate = replace(
            snapshot,
            opponents=(
                snapshot.opponents[0],
                snapshot.opponents[0],
                snapshot.opponents[2],
            ),
        )
        with self.assertRaisesRegex(ClassicalWaitError, "ambiguous"):
            feature_vector(duplicate, _target(), 4)

    def test_no_mechanism_defense_danger_implementation_is_imported(self):
        tree = ast.parse(inspect.getsource(classical_data))
        imported = {
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        } | {
            node.module or ""
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom)
        }
        self.assertFalse(any("mechanism_riichi_defense" in name for name in imported))


class EvaluationBoundaryTest(unittest.TestCase):
    def test_zero_correction_exactly_reproduces_prevalence_probability(self):
        features = (0.37,) * FEATURE_DIM
        for probability in (0.001, 0.2, 0.5, 0.999):
            self.assertEqual(
                predict_probability(
                    probability,
                    [0.0] * FEATURE_DIM,
                    features,
                ),
                probability,
            )

    def test_classification_boundaries_are_exhaustive(self):
        self.assertEqual(classify_interval(0.01, 0.02), CLASSICAL_SIGNAL)
        self.assertEqual(classify_interval(-0.02, -0.01), CLASSICAL_REGRESSION)
        self.assertEqual(classify_interval(-0.01, 0.01), CLASSICAL_INCONCLUSIVE)
        self.assertEqual(classify_interval(0.0, 0.01), CLASSICAL_INCONCLUSIVE)
        self.assertEqual(classify_interval(-0.01, 0.0), CLASSICAL_INCONCLUSIVE)

    def test_coverage_counts_only_established_available_rows(self):
        mask = tuple([1] + [0] * 33)
        coverage = coverage_value((_record(mask),))
        self.assertEqual(coverage["eligible_hanchan"], 1)
        self.assertEqual(coverage["eligible_rows"], 1)
        self.assertEqual(coverage["eligible_cells"], 34)
        self.assertEqual(coverage["per_tile_positives"][0], 1)


if __name__ == "__main__":
    unittest.main()
