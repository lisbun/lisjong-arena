"""lisjong-project #22 Phase 0.5 extraction / orchestration contract tests。"""

import unittest
from unittest.mock import patch

from lisjong_engine.match_state import MatchState
from lisjong_engine.observation import ObservationDecisionKind
from lisjong_engine.observation_builder import build_seat_observation
from lisjong_engine.rules import RuleSet
from lisjong_engine.seat import Seat as EngineSeat

import lisjong_arena.phase05_belief_slice.experiment as experiment
from lisjong_arena.phase05_belief_slice.extraction import (
    ONLINE_POLICY_IDENTITY,
    Phase05GameExtraction,
    _Phase05Recorder,
    extract_phase05_game,
)
from lisjong_arena.phase05_belief_slice.sample import (
    EXPERIMENT_SEEDS,
    TEST_SEEDS,
    Phase05Partition,
)
from tests import _phase05_fixtures as fixtures

_SEED = 20260827


def _extraction(
    seed: int,
    *,
    samples: tuple = (),
    total_decisions: int = 10,
    turn_anchors: int = 5,
    exclusion_counts: tuple = (),
    wall_clock_seconds: float = 1.0,
) -> Phase05GameExtraction:
    return Phase05GameExtraction(
        seed=seed,
        total_decisions=total_decisions,
        turn_anchors=turn_anchors,
        samples=samples,
        exclusion_counts=exclusion_counts,
        wall_clock_seconds=wall_clock_seconds,
    )


class AnchorSelectionTest(unittest.TestCase):
    def test_only_turn_decisions_become_anchors(self) -> None:
        match_state = MatchState(seed=_SEED, rules=RuleSet.default())
        match_state.start_round()
        round_state = match_state.active_round
        self.assertIsNotNone(round_state)
        round_state.draw(EngineSeat.EAST)
        observation = build_seat_observation(match_state, EngineSeat.EAST)
        self.assertIs(observation.decision_kind, ObservationDecisionKind.TURN)
        recorder = _Phase05Recorder(match_state, 100)

        recorder.observe(observation)
        non_turn = object.__new__(type(observation))
        for name in type(observation).__dataclass_fields__:
            value = getattr(observation, name)
            if name == "decision_kind":
                value = ObservationDecisionKind.DISCARD_REACTION
            object.__setattr__(non_turn, name, value)
        recorder.observe(non_turn)

        self.assertEqual(recorder.total_decisions, 2)
        self.assertEqual(recorder.turn_anchors, 1)
        self.assertEqual(len(recorder.samples), 1)

    def test_anchor_index_is_sequential_within_a_game(self) -> None:
        match_state = MatchState(seed=_SEED, rules=RuleSet.default())
        match_state.start_round()
        round_state = match_state.active_round
        self.assertIsNotNone(round_state)
        round_state.draw(EngineSeat.EAST)
        observation = build_seat_observation(match_state, EngineSeat.EAST)
        recorder = _Phase05Recorder(match_state, 100)

        recorder.observe(observation)
        recorder.observe(observation)

        self.assertEqual(
            [sample.anchor_index for sample in recorder.samples],
            [0, 1],
        )

    def test_recorder_partitions_samples_by_seed(self) -> None:
        match_state = MatchState(seed=_SEED, rules=RuleSet.default())
        match_state.start_round()
        round_state = match_state.active_round
        self.assertIsNotNone(round_state)
        round_state.draw(EngineSeat.EAST)
        observation = build_seat_observation(match_state, EngineSeat.EAST)
        recorder = _Phase05Recorder(match_state, 150)

        recorder.observe(observation)

        self.assertIs(recorder.samples[0].partition, Phase05Partition.TEST)

    def test_extraction_rejects_non_integer_seed(self) -> None:
        with self.assertRaises(TypeError):
            extract_phase05_game("100")


class CoverageAggregationTest(unittest.TestCase):
    def test_coverage_sums_games_anchors_and_reason_coded_exclusions(self) -> None:
        extractions = (
            _extraction(
                100,
                samples=(fixtures.sample(seed=100),),
                total_decisions=20,
                turn_anchors=8,
                exclusion_counts=(("unstable_opponent_hand_size", 2),),
                wall_clock_seconds=2.0,
            ),
            _extraction(
                150,
                samples=(fixtures.sample(seed=150, partition=Phase05Partition.TEST),),
                total_decisions=10,
                turn_anchors=4,
                exclusion_counts=(("unstable_opponent_hand_size", 1),),
                wall_clock_seconds=2.0,
            ),
        )

        coverage = experiment._build_coverage(extractions)

        self.assertEqual(coverage.games_attempted, 60)
        self.assertEqual(coverage.games_completed, 2)
        self.assertEqual(coverage.total_decisions, 30)
        self.assertEqual(coverage.turn_anchors, 12)
        self.assertEqual(coverage.usable_samples, 2)
        self.assertEqual(
            coverage.exclusion_counts,
            (("unstable_opponent_hand_size", 3),),
        )
        self.assertEqual(coverage.excluded_anchors, 3)
        self.assertEqual(coverage.exclusion_rate, 0.25)
        self.assertEqual(
            coverage.samples_by_partition,
            (("test", 1), ("train", 1)),
        )
        self.assertEqual(coverage.seconds_per_hanchan, 2.0)
        self.assertEqual(coverage.samples_per_hanchan, 1.0)

    def test_partition_selection_keeps_same_game_samples_together(self) -> None:
        extractions = (
            _extraction(
                100,
                samples=(
                    fixtures.sample(seed=100, anchor_index=0),
                    fixtures.sample(seed=100, anchor_index=1),
                ),
            ),
            _extraction(
                150,
                samples=(
                    fixtures.sample(
                        seed=150,
                        partition=Phase05Partition.TEST,
                        anchor_index=0,
                    ),
                ),
            ),
        )

        train = experiment._partition_samples(extractions, Phase05Partition.TRAIN)
        test = experiment._partition_samples(extractions, Phase05Partition.TEST)

        self.assertEqual({sample.seed for sample in train}, {100})
        self.assertEqual({sample.seed for sample in test}, {150})
        self.assertEqual(len(train), 2)


class StorageMeasurementTest(unittest.TestCase):
    def test_compressed_measurement_leaves_no_temporary_file(self) -> None:
        samples = (fixtures.sample(anchor_index=0), fixtures.sample(anchor_index=1))
        seen: list[str] = []
        original_unlink = experiment.os.unlink

        def recording_unlink(path):
            seen.append(path)
            return original_unlink(path)

        with patch.object(experiment.os, "unlink", recording_unlink):
            measurement = experiment._measure_compressed_storage(samples, 2)

        self.assertEqual(len(seen), 1)
        self.assertFalse(experiment.os.path.exists(seen[0]))
        self.assertEqual(measurement.sample_count, 2)
        self.assertGreater(measurement.compressed_bytes, 0)
        self.assertEqual(
            measurement.compressed_bytes_per_hanchan,
            measurement.compressed_bytes / 2,
        )


class LockedProtocolTest(unittest.TestCase):
    def test_experiment_runs_exactly_the_locked_sixty_seeds_in_order(self) -> None:
        calls: list[int] = []

        def fake_extract(seed, *, rules=None):
            calls.append(seed)
            return _extraction(
                seed,
                samples=(
                    fixtures.sample(
                        seed=seed,
                        partition=experiment.Phase05Partition.TRAIN
                        if seed < 140
                        else (
                            experiment.Phase05Partition.VALIDATION
                            if seed < 150
                            else experiment.Phase05Partition.TEST
                        ),
                    ),
                ),
            )

        with (
            patch.object(experiment, "extract_phase05_game", fake_extract),
            patch.object(
                experiment,
                "run_phase05_decision_linked",
                return_value=object(),
            ) as decision_linked,
        ):
            result = experiment.run_phase05_experiment()

        self.assertEqual(tuple(calls), EXPERIMENT_SEEDS)
        self.assertEqual(len(result.partition_reports), 3)
        self.assertEqual(decision_linked.call_args.args[0], TEST_SEEDS)

    def test_provenance_records_the_locked_source_configuration(self) -> None:
        provenance = experiment._build_provenance(RuleSet.default())

        self.assertEqual(
            provenance.source_classification,
            experiment.SOURCE_CLASSIFICATION,
        )
        self.assertEqual(provenance.online_policy_identity, ONLINE_POLICY_IDENTITY)
        self.assertEqual(provenance.rule_set_name, "project-standard-v1")
        self.assertEqual(provenance.rule_set_version, 1)
        self.assertEqual(provenance.train_seeds, tuple(range(100, 140)))
        self.assertEqual(provenance.validation_seeds, tuple(range(140, 150)))
        self.assertEqual(provenance.test_seeds, tuple(range(150, 160)))

    def test_experiment_rejects_a_non_ruleset_argument(self) -> None:
        with self.assertRaises(TypeError):
            experiment.run_phase05_experiment(rules=object())


if __name__ == "__main__":
    unittest.main()
