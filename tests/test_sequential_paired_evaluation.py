"""Issue #296 sequential paired-evaluation primitive tests."""

from __future__ import annotations

import copy
import random
import tempfile
import unittest
from pathlib import Path

from lisjong_arena._artifact_io import canonical_json_text
from lisjong_arena.paired_evaluation import PairedSeedDelta
from lisjong_arena.sequential_paired_evaluation import (
    DECISION_CONTINUE,
    DECISION_FINAL_INCONCLUSIVE,
    DECISION_NEGATIVE,
    DECISION_POSITIVE,
    FUTILITY_RULE_ID,
    METHOD_ID,
    MIN_LOOK_BLOCK_COUNT,
    SequentialPairedEvaluationError,
    SequentialPairedProtocol,
    build_sequential_result,
    evaluate_sequential_look,
    iter_sequential_look_results,
    load_sequential_result,
    parse_sequential_result,
    save_sequential_result,
)


def protocol(
    *,
    maximum: int = 80,
    looks: tuple[int, ...] = (20, 40, 60, 80),
    delta_min: float = 0.25,
    alpha: float = 0.05,
) -> SequentialPairedProtocol:
    return SequentialPairedProtocol(
        protocol_id="synthetic-paired-v1",
        estimand_id="mean(candidate-parent)",
        sign_convention="positive means candidate is better",
        paired_unit_id="one immutable paired seed-block summary",
        ordered_seeds=tuple(range(10_000, 10_000 + maximum)),
        look_block_counts=looks,
        familywise_alpha=alpha,
        delta_min=delta_min,
    )


def units(
    locked: SequentialPairedProtocol,
    values: tuple[float, ...],
) -> tuple[PairedSeedDelta, ...]:
    return tuple(
        PairedSeedDelta(
            seed=seed,
            candidate_mean=float(value),
            parent_mean=0.0,
            delta=float(value),
        )
        for seed, value in zip(locked.ordered_seeds, values, strict=True)
    )


class ProtocolLockTest(unittest.TestCase):
    def test_v1_method_is_one_predeclared_bonferroni_family(self) -> None:
        locked = protocol()
        document = locked.to_document()
        self.assertEqual(METHOD_ID, "bonferroni-normal-approx-alpha-spending-v1")
        self.assertEqual(document["method_id"], METHOD_ID)
        self.assertEqual(document["futility_rule_id"], FUTILITY_RULE_ID)
        self.assertEqual(FUTILITY_RULE_ID, "none-v1")
        self.assertEqual(locked.per_look_alpha, 0.05 / 4)
        self.assertGreater(locked.critical_value, 1.96)

    def test_delta_min_is_bound_into_protocol_identity(self) -> None:
        left = protocol(delta_min=0.25)
        right = protocol(delta_min=0.5)
        self.assertNotEqual(left.identity, right.identity)
        self.assertNotEqual(left.to_document(), right.to_document())

    def test_invalid_schedules_fail_closed(self) -> None:
        with self.assertRaisesRegex(SequentialPairedEvaluationError, "at least"):
            protocol(maximum=19, looks=(19,))
        with self.assertRaisesRegex(SequentialPairedEvaluationError, "strictly increasing"):
            protocol(maximum=40, looks=(20, 20, 40))
        with self.assertRaisesRegex(SequentialPairedEvaluationError, "final look"):
            protocol(maximum=80, looks=(20, 40, 60))
        with self.assertRaisesRegex(SequentialPairedEvaluationError, "non-empty tuple"):
            SequentialPairedProtocol(
                protocol_id="x",
                estimand_id="x",
                sign_convention="x",
                paired_unit_id="x",
                ordered_seeds=(),
                look_block_counts=(),
                familywise_alpha=0.05,
                delta_min=0.25,
            )

    def test_invalid_alpha_delta_and_seed_population_fail_closed(self) -> None:
        with self.assertRaisesRegex(SequentialPairedEvaluationError, "familywise_alpha"):
            protocol(alpha=0.0)
        with self.assertRaisesRegex(SequentialPairedEvaluationError, "delta_min"):
            protocol(delta_min=0.0)

        base = protocol()
        with self.assertRaisesRegex(SequentialPairedEvaluationError, "unique"):
            SequentialPairedProtocol(
                protocol_id=base.protocol_id,
                estimand_id=base.estimand_id,
                sign_convention=base.sign_convention,
                paired_unit_id=base.paired_unit_id,
                ordered_seeds=(1,) * 20,
                look_block_counts=(20,),
                familywise_alpha=0.05,
                delta_min=0.25,
            )

    def test_first_look_minimum_is_explicit(self) -> None:
        self.assertEqual(MIN_LOOK_BLOCK_COUNT, 20)


class DecisionSemanticsTest(unittest.TestCase):
    def test_positive_boundary_stops_at_first_allowed_look(self) -> None:
        locked = protocol()
        result = evaluate_sequential_look(locked, units(locked, (1.0,) * 20))
        self.assertEqual(result.decision, DECISION_POSITIVE)
        self.assertTrue(result.stopped)
        self.assertGreater(result.interval_lower, locked.delta_min)

    def test_negative_boundary_stops_at_first_allowed_look(self) -> None:
        locked = protocol()
        result = evaluate_sequential_look(locked, units(locked, (-1.0,) * 20))
        self.assertEqual(result.decision, DECISION_NEGATIVE)
        self.assertTrue(result.stopped)
        self.assertLess(result.interval_upper, -locked.delta_min)

    def test_continue_path_uses_only_an_allowed_look(self) -> None:
        locked = protocol()
        values = tuple(-1.0 if index % 2 else 1.0 for index in range(20))
        result = evaluate_sequential_look(locked, units(locked, values))
        self.assertEqual(result.decision, DECISION_CONTINUE)
        self.assertFalse(result.stopped)
        self.assertEqual(result.remaining_maximum_budget, 60)

    def test_final_inconclusive_is_terminal_only_at_nmax(self) -> None:
        locked = protocol()
        result = evaluate_sequential_look(locked, units(locked, (0.0,) * 80))
        self.assertEqual(result.decision, DECISION_FINAL_INCONCLUSIVE)
        self.assertTrue(result.stopped)
        self.assertEqual(result.remaining_maximum_budget, 0)

    def test_exact_boundary_equality_does_not_establish_an_effect(self) -> None:
        locked = protocol()
        positive_equal = evaluate_sequential_look(
            locked, units(locked, (locked.delta_min,) * 20)
        )
        negative_equal = evaluate_sequential_look(
            locked, units(locked, (-locked.delta_min,) * 20)
        )
        self.assertEqual(positive_equal.interval_lower, locked.delta_min)
        self.assertEqual(negative_equal.interval_upper, -locked.delta_min)
        self.assertEqual(positive_equal.decision, DECISION_CONTINUE)
        self.assertEqual(negative_equal.decision, DECISION_CONTINUE)

    def test_non_scheduled_look_is_rejected(self) -> None:
        locked = protocol()
        with self.assertRaisesRegex(SequentialPairedEvaluationError, "not an allowed"):
            evaluate_sequential_look(locked, units(locked, (0.0,) * 21))

    def test_paired_seed_block_order_is_preserved(self) -> None:
        locked = protocol(maximum=40, looks=(20, 40))
        valid = list(units(locked, (0.0,) * 20))
        valid[0], valid[1] = valid[1], valid[0]
        with self.assertRaisesRegex(SequentialPairedEvaluationError, "ordered seed"):
            evaluate_sequential_look(locked, tuple(valid))

    def test_inconsistent_paired_delta_is_rejected(self) -> None:
        locked = protocol(maximum=40, looks=(20, 40))
        valid = list(units(locked, (0.0,) * 20))
        first = valid[0]
        valid[0] = PairedSeedDelta(
            seed=first.seed,
            candidate_mean=1.0,
            parent_mean=0.0,
            delta=0.0,
        )
        with self.assertRaisesRegex(SequentialPairedEvaluationError, "re-derived"):
            evaluate_sequential_look(locked, tuple(valid))


class ArtifactReplayTest(unittest.TestCase):
    def test_deterministic_replay_is_byte_for_byte_stable(self) -> None:
        locked = protocol(maximum=40, looks=(20, 40))
        paired = units(
            locked, tuple(-1.0 if index % 2 else 1.0 for index in range(20))
        )
        first = build_sequential_result(locked, paired)
        second = build_sequential_result(locked, paired)
        self.assertEqual(first, second)
        self.assertEqual(canonical_json_text(first), canonical_json_text(second))
        self.assertEqual(parse_sequential_result(first), first)

    def test_post_stop_continuation_fails_closed(self) -> None:
        locked = protocol(maximum=40, looks=(20, 40))
        with self.assertRaisesRegex(
            SequentialPairedEvaluationError, "continued execution"
        ):
            build_sequential_result(locked, units(locked, (1.0,) * 40))

    def test_reordered_missing_and_duplicated_look_data_fail_closed(self) -> None:
        locked = protocol(maximum=40, looks=(20, 40))
        values = tuple(-1.0 if index % 2 else 1.0 for index in range(40))
        document = build_sequential_result(locked, units(locked, values))

        missing = copy.deepcopy(document)
        missing["looks"].pop(0)
        with self.assertRaises(SequentialPairedEvaluationError):
            parse_sequential_result(missing)

        duplicated = copy.deepcopy(document)
        duplicated["looks"].append(copy.deepcopy(duplicated["looks"][-1]))
        with self.assertRaises(SequentialPairedEvaluationError):
            parse_sequential_result(duplicated)

        reordered = copy.deepcopy(document)
        reordered["paired_units"][0], reordered["paired_units"][1] = (
            reordered["paired_units"][1],
            reordered["paired_units"][0],
        )
        with self.assertRaises(SequentialPairedEvaluationError):
            parse_sequential_result(reordered)

    def test_changed_boundary_parameter_fails_closed(self) -> None:
        locked = protocol(maximum=40, looks=(20, 40))
        document = build_sequential_result(
            locked,
            units(
                locked,
                tuple(-1.0 if index % 2 else 1.0 for index in range(20)),
            ),
        )
        changed = copy.deepcopy(document)
        changed["protocol"]["critical_value"] += 0.01
        with self.assertRaisesRegex(SequentialPairedEvaluationError, "critical"):
            parse_sequential_result(changed)

    def test_artifact_round_trip_is_strict(self) -> None:
        locked = protocol(maximum=40, looks=(20, 40))
        document = build_sequential_result(
            locked,
            units(
                locked,
                tuple(-1.0 if index % 2 else 1.0 for index in range(20)),
            ),
        )
        with tempfile.TemporaryDirectory() as text:
            path = Path(text) / "look-1.json"
            save_sequential_result(document, path)
            self.assertEqual(load_sequential_result(path), document)
            with self.assertRaises(FileExistsError):
                save_sequential_result(document, path)


class BoundedIntegrationTest(unittest.TestCase):
    def test_lazy_source_terminates_before_nmax_after_positive_stop(self) -> None:
        locked = protocol(maximum=80, looks=(20, 40, 60, 80))
        consumed: list[int] = []

        def source():
            for seed in locked.ordered_seeds:
                consumed.append(seed)
                yield PairedSeedDelta(
                    seed=seed,
                    candidate_mean=1.0,
                    parent_mean=0.0,
                    delta=1.0,
                )

        sequence = iter_sequential_look_results(locked, source())
        first = next(sequence)
        self.assertEqual(first["terminal_decision"], DECISION_POSITIVE)
        self.assertEqual(len(consumed), 20)
        with self.assertRaises(StopIteration):
            next(sequence)
        self.assertEqual(len(consumed), 20)
        self.assertLess(len(consumed), locked.maximum_block_count)


class StatisticalValidationTest(unittest.TestCase):
    def test_fixed_rng_null_simulation_stays_within_predeclared_tolerance(self) -> None:
        """Validate repeated-look Type-I behavior for the locked v1 approximation.

        Under Gaussian paired deltas, the studentized normal-approximation
        boundaries are expected to be close to their nominal marginal levels.
        The Bonferroni spending then bounds repeated-look false effect
        establishment.  The RNG seed, trial count, schedule, and tolerance are
        fixed test data rather than a post-hoc choice.
        """

        locked = protocol(
            maximum=80,
            looks=(20, 40, 60, 80),
            delta_min=1.0e-9,
            alpha=0.05,
        )
        rng = random.Random(296)
        trials = 2_500
        tolerance = 0.015
        false_stops = 0

        for _ in range(trials):
            values = tuple(rng.gauss(0.0, 1.0) for _ in range(80))
            paired = units(locked, values)
            for look_count in locked.look_block_counts:
                look = evaluate_sequential_look(locked, paired[:look_count])
                if look.decision in (DECISION_POSITIVE, DECISION_NEGATIVE):
                    false_stops += 1
                    break

        observed = false_stops / trials
        self.assertLessEqual(observed, locked.familywise_alpha + tolerance)


if __name__ == "__main__":
    unittest.main()
