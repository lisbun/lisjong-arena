"""Automated Strength Evaluation v0 orchestration tests.

No test in this module runs a real RiichiEnv game or consumes a research seed
population.
"""

from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

from _single_round_artifact_fixtures import (
    canonical_summary,
    evaluation_result,
    provenance,
)

from lisjong_arena._artifact_io import canonical_json_text
from lisjong_arena._execution_safety import (
    ExecutionSafetyError,
    require_new_artifact_destinations,
)
from lisjong_arena.strength_evaluation import (
    CLASSIFICATION_RULE_TYPE,
    IntervalClassificationRule,
    PolicyReference,
    StrengthEvaluationExecutionError,
    StrengthEvaluationPreflightError,
    StrengthEvaluationResultError,
    StrengthEvaluationSpec,
    StrengthEvaluationSpecError,
    classify_strength_summary,
    format_compact_result_summary,
    load_strength_evaluation_result,
    main,
    parse_strength_evaluation_spec,
    prepare_strength_evaluation,
    run_strength_evaluation,
)

ARENA_REVISION = provenance().lisjong_arena_revision


def rule(threshold: float = 0.0) -> IntervalClassificationRule:
    return IntervalClassificationRule(
        threshold=threshold,
        positive_label="SIGNAL",
        negative_label="NEGATIVE",
        inconclusive_label="INCONCLUSIVE",
    )


def spec_document(
    root: Path,
    *,
    candidate: str = "yakuhai-call",
    baseline: str = "combined",
    seeds: list[int] | None = None,
    classification=True,
    workers: int = 1,
    constraints: dict[str, str] | None = None,
) -> dict:
    return {
        "artifact_output": str(root / "strength.json"),
        "baseline": {"identity": None, "reference": baseline},
        "candidate": {"identity": None, "reference": candidate},
        "classification_rule": (
            {
                "inconclusive_label": "INCONCLUSIVE",
                "negative_label": "NEGATIVE",
                "positive_label": "SIGNAL",
                "rule_type": CLASSIFICATION_RULE_TYPE,
                "threshold": 0,
            }
            if classification
            else None
        ),
        "execution_options": {"max_steps": 10_000, "workers": workers},
        "expected_provenance_constraints": constraints or {},
        "ordered_seeds": [20_200, 20_201] if seeds is None else seeds,
        "protocol": "abbb-single-round-v1",
        "result_output": str(root / "result.json"),
        "spec_version": 1,
    }


def parse_spec(root: Path, **overrides) -> StrengthEvaluationSpec:
    return parse_strength_evaluation_spec(spec_document(root, **overrides))


def preflight_patches():
    return (
        mock.patch(
            "lisjong_arena.strength_evaluation.collect_execution_provenance",
            return_value=provenance(),
        ),
        mock.patch(
            "lisjong_arena.strength_evaluation.require_clean_arena_head",
            return_value=ARENA_REVISION,
        ),
    )


class SpecTest(unittest.TestCase):
    def test_minimum_supported_spec_parses_and_normalizes_threshold(self):
        with tempfile.TemporaryDirectory() as directory:
            value = parse_spec(Path(directory))
        self.assertEqual(value.candidate, PolicyReference("yakuhai-call"))
        self.assertEqual(value.baseline, PolicyReference("combined"))
        self.assertEqual(value.classification_rule.threshold, 0.0)
        self.assertEqual(value.ordered_seeds, (20_200, 20_201))

    def test_missing_candidate_and_baseline_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            for name in ("candidate", "baseline"):
                with self.subTest(name=name):
                    document = spec_document(Path(directory))
                    del document[name]
                    with self.assertRaises(StrengthEvaluationSpecError):
                        parse_strength_evaluation_spec(document)

    def test_empty_duplicate_and_non_integer_seeds_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for seeds in ([], [1, 1], [1, "2"]):
                with self.subTest(seeds=seeds):
                    with self.assertRaises(StrengthEvaluationSpecError):
                        parse_spec(root, seeds=seeds)

    def test_unsupported_protocol_and_classification_rule_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            document = spec_document(root)
            document["protocol"] = "other"
            with self.assertRaisesRegex(
                StrengthEvaluationSpecError, "unsupported protocol"
            ):
                parse_strength_evaluation_spec(document)

            document = spec_document(root)
            document["classification_rule"]["rule_type"] = "other"
            with self.assertRaisesRegex(
                StrengthEvaluationSpecError, "unsupported classification rule"
            ):
                parse_strength_evaluation_spec(document)

    def test_explicit_reference_requires_explicit_identity_during_preflight(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            document = spec_document(root)
            document["candidate"] = {
                "identity": None,
                "reference": "lisjong.policies.some_policy:SomePolicy",
            }
            value = parse_strength_evaluation_spec(document)
            evaluator = mock.Mock()
            with mock.patch(
                "lisjong_arena.strength_evaluation.run_single_round_evaluation",
                evaluator,
            ):
                with self.assertRaises(StrengthEvaluationPreflightError):
                    run_strength_evaluation(value)
            evaluator.assert_not_called()

    def test_interval_classification_requires_two_seed_blocks(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(StrengthEvaluationSpecError, "at least two"):
                parse_spec(Path(directory), seeds=[1])


class DestinationPreflightTest(unittest.TestCase):
    def test_missing_parent_parent_file_existing_output_and_duplicates_fail(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            blocked_parent = root / "blocked"
            blocked_parent.write_text("file", encoding="utf-8")
            existing = root / "existing.json"
            existing.write_text("occupied", encoding="utf-8")
            cases = (
                {
                    "strength_artifact": root / "missing" / "strength.json",
                    "result": root / "result.json",
                },
                {
                    "strength_artifact": blocked_parent / "strength.json",
                    "result": root / "result.json",
                },
                {
                    "strength_artifact": existing,
                    "result": root / "result.json",
                },
                {
                    "strength_artifact": root / "same.json",
                    "result": root / "." / "same.json",
                },
            )
            for locations in cases:
                with self.subTest(locations=locations):
                    with self.assertRaises(ExecutionSafetyError):
                        require_new_artifact_destinations(
                            locations,
                            required_names=("strength_artifact", "result"),
                        )

    def test_non_writable_parent_fails_and_valid_preflight_creates_nothing(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            locations = {
                "strength_artifact": root / "strength.json",
                "result": root / "result.json",
            }
            with self.assertRaisesRegex(ExecutionSafetyError, "not writable"):
                require_new_artifact_destinations(
                    locations,
                    required_names=("strength_artifact", "result"),
                    writable_check=lambda _path, _mode: False,
                )
            require_new_artifact_destinations(
                locations,
                required_names=("strength_artifact", "result"),
            )
            self.assertFalse(locations["strength_artifact"].exists())
            self.assertFalse(locations["result"].exists())

    def test_missing_parent_rejects_before_evaluator(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            value = parse_spec(root)
            value = replace(value, artifact_output=root / "missing" / "strength.json")
            evaluator = mock.Mock()
            provenance_patch, head_patch = preflight_patches()
            with (
                provenance_patch,
                head_patch,
                mock.patch(
                    "lisjong_arena.strength_evaluation.run_single_round_evaluation",
                    evaluator,
                ),
            ):
                with self.assertRaisesRegex(
                    StrengthEvaluationPreflightError, "parent directory"
                ):
                    run_strength_evaluation(value)
            evaluator.assert_not_called()


class LockTest(unittest.TestCase):
    def prepare(self, value: StrengthEvaluationSpec):
        provenance_patch, head_patch = preflight_patches()
        with provenance_patch, head_patch:
            return prepare_strength_evaluation(value)

    def test_same_semantics_same_lock_and_local_paths_are_non_semantic(self):
        with (
            tempfile.TemporaryDirectory() as first,
            tempfile.TemporaryDirectory() as second,
        ):
            lock_a = self.prepare(parse_spec(Path(first)))
            lock_b = self.prepare(parse_spec(Path(second)))
        self.assertEqual(lock_a.spec.identity, lock_b.spec.identity)
        self.assertEqual(lock_a.identity, lock_b.identity)

    def test_semantic_changes_change_lock_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            base = self.prepare(parse_spec(root))
            variants = (
                parse_spec(root, candidate="extended-combined"),
                parse_spec(root, baseline="two-step"),
                parse_spec(root, seeds=[20_201, 20_200]),
                parse_spec(root, workers=2),
                replace(parse_spec(root), classification_rule=rule(1.0)),
                parse_spec(
                    root,
                    constraints={"lisjong_revision": provenance().lisjong_revision},
                ),
            )
            for variant in variants:
                with self.subTest(variant=variant.semantic_document()):
                    self.assertNotEqual(base.identity, self.prepare(variant).identity)

    def test_candidate_resolution_failure_precedes_execution(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            evaluator = mock.Mock()
            value = parse_spec(root, candidate="latest")
            with mock.patch(
                "lisjong_arena.strength_evaluation.run_single_round_evaluation",
                evaluator,
            ):
                with self.assertRaises(StrengthEvaluationPreflightError):
                    run_strength_evaluation(value)
            evaluator.assert_not_called()

    def test_declared_source_runtime_and_dependency_mismatch_precede_execution(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            evaluator = mock.Mock()
            for field in (
                "lisjong_arena_revision",
                "python_version",
                "lisjong_revision",
            ):
                with self.subTest(field=field):
                    value = parse_spec(root, constraints={field: "mismatch"})
                    provenance_patch, head_patch = preflight_patches()
                    with (
                        provenance_patch,
                        head_patch,
                        mock.patch(
                            "lisjong_arena.strength_evaluation.run_single_round_evaluation",
                            evaluator,
                        ),
                    ):
                        with self.assertRaises(StrengthEvaluationPreflightError):
                            run_strength_evaluation(value)
            evaluator.assert_not_called()


class ClassificationTest(unittest.TestCase):
    def summary_with_interval(self, lower, upper):
        value = canonical_summary(evaluation_result().game_results)
        statistics = replace(
            value.seed_block_statistics,
            normal_approx_95_interval_lower=lower,
            normal_approx_95_interval_upper=upper,
        )
        return replace(value, seed_block_statistics=statistics)

    def test_positive_negative_inconclusive_unclassified_and_boundaries(self):
        cases = (
            (self.summary_with_interval(1.0, 2.0), rule(), "POSITIVE"),
            (self.summary_with_interval(-2.0, -1.0), rule(), "NEGATIVE"),
            (self.summary_with_interval(-1.0, 1.0), rule(), "INCONCLUSIVE"),
            (self.summary_with_interval(0.0, 1.0), rule(), "INCONCLUSIVE"),
            (self.summary_with_interval(-1.0, 0.0), rule(), "INCONCLUSIVE"),
            (self.summary_with_interval(1.0, 2.0), None, "UNCLASSIFIED"),
        )
        for summary, classification_rule, expected in cases:
            with self.subTest(expected=expected):
                self.assertEqual(
                    classify_strength_summary(summary, classification_rule)["kind"],
                    expected,
                )

    def test_missing_interval_is_invalid_not_negative(self):
        summary = self.summary_with_interval(None, None)
        self.assertEqual(classify_strength_summary(summary, rule())["kind"], "INVALID")

    def test_non_finite_and_reversed_intervals_are_invalid(self):
        for lower, upper in ((float("nan"), 1.0), (2.0, 1.0)):
            with self.subTest(lower=lower, upper=upper):
                summary = self.summary_with_interval(lower, upper)
                self.assertEqual(
                    classify_strength_summary(summary, rule())["kind"], "INVALID"
                )


class ExecutionAndResultTest(unittest.TestCase):
    def run_fixture(self, root: Path, *, classification=True):
        value = parse_spec(root, classification=classification)
        fixed_result = evaluation_result()
        provenance_patch, head_patch = preflight_patches()
        with (
            provenance_patch,
            head_patch,
            mock.patch(
                "lisjong_arena.single_round_artifact.collect_execution_provenance",
                return_value=provenance(),
            ),
            mock.patch(
                "lisjong_arena.strength_evaluation.run_single_round_evaluation",
                return_value=fixed_result,
            ) as evaluator,
        ):
            result = run_strength_evaluation(value)
        return value, result, evaluator

    def test_runner_calls_existing_evaluator_with_exact_locked_plan(self):
        with tempfile.TemporaryDirectory() as directory:
            value, result, evaluator = self.run_fixture(Path(directory))
            plan = evaluator.call_args.args[0]
            self.assertEqual(plan.candidate.identity, "yakuhai-call")
            self.assertEqual(plan.baseline.identity, "combined")
            self.assertEqual(plan.seeds, value.ordered_seeds)
            self.assertEqual(plan.max_steps, value.max_steps)
            self.assertEqual(result["execution_status"], "COMPLETED")
            self.assertEqual(result["game_count"], 8)
            self.assertIn(
                "candidate=yakuhai-call", format_compact_result_summary(result)
            )

    def test_workers_greater_than_one_use_existing_parallel_evaluator(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            value = parse_spec(root, workers=2)
            fixed_result = evaluation_result()
            provenance_patch, head_patch = preflight_patches()
            with (
                provenance_patch,
                head_patch,
                mock.patch(
                    "lisjong_arena.single_round_artifact.collect_execution_provenance",
                    return_value=provenance(),
                ),
                mock.patch(
                    "lisjong_arena.strength_evaluation.run_single_round_evaluation"
                ) as serial,
                mock.patch(
                    "lisjong_arena.strength_evaluation."
                    "run_single_round_evaluation_parallel",
                    return_value=fixed_result,
                ) as parallel,
            ):
                run_strength_evaluation(value)
            serial.assert_not_called()
            parallel.assert_called_once()
            self.assertEqual(parallel.call_args.kwargs, {"max_workers": 2})

    def test_success_requires_artifact_strict_readback_and_unclassified_is_distinct(
        self,
    ):
        with tempfile.TemporaryDirectory() as directory:
            value, result, _ = self.run_fixture(Path(directory), classification=False)
            self.assertTrue(value.artifact_output.exists())
            self.assertTrue(value.result_output.exists())
            self.assertEqual(result["classification"]["kind"], "UNCLASSIFIED")
            loaded = load_strength_evaluation_result(
                value.result_output,
                strength_artifact_path=value.artifact_output,
            )
            self.assertEqual(loaded, result)

    def test_tampered_artifact_and_tampered_summary_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            value, result, _ = self.run_fixture(root)
            value.artifact_output.write_text("{}\n", encoding="utf-8")
            with self.assertRaisesRegex(
                StrengthEvaluationResultError, "digest does not match"
            ):
                load_strength_evaluation_result(
                    value.result_output,
                    strength_artifact_path=value.artifact_output,
                )

            second = root / "second"
            second.mkdir()
            value, result, _ = self.run_fixture(second)
            document = json.loads(value.result_output.read_text(encoding="utf-8"))
            document["canonical_summary"]["mean_candidate_game_delta"] += 1.0
            value.result_output.write_text(
                canonical_json_text(document), encoding="utf-8"
            )
            with self.assertRaisesRegex(
                StrengthEvaluationResultError, "canonical summary"
            ):
                load_strength_evaluation_result(
                    value.result_output,
                    strength_artifact_path=value.artifact_output,
                )

    def test_execution_and_artifact_failures_never_create_completed_result(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for failure_point in ("evaluator", "artifact"):
                with self.subTest(failure_point=failure_point):
                    case_root = root / failure_point
                    case_root.mkdir()
                    value = parse_spec(case_root)
                    provenance_patch, head_patch = preflight_patches()
                    evaluator_side_effect = (
                        RuntimeError("game failed")
                        if failure_point == "evaluator"
                        else None
                    )
                    save_side_effect = (
                        RuntimeError("save failed")
                        if failure_point == "artifact"
                        else None
                    )
                    with (
                        provenance_patch,
                        head_patch,
                        mock.patch(
                            "lisjong_arena.strength_evaluation.run_single_round_evaluation",
                            return_value=evaluation_result(),
                            side_effect=evaluator_side_effect,
                        ),
                        mock.patch(
                            "lisjong_arena.strength_evaluation.save_single_round_artifact",
                            side_effect=save_side_effect,
                        ),
                    ):
                        with self.assertRaises(StrengthEvaluationExecutionError):
                            run_strength_evaluation(value)
                    self.assertFalse(value.result_output.exists())

    def test_artifact_strict_readback_failure_never_creates_completed_result(self):
        with tempfile.TemporaryDirectory() as directory:
            value = parse_spec(Path(directory))
            provenance_patch, head_patch = preflight_patches()
            with (
                provenance_patch,
                head_patch,
                mock.patch(
                    "lisjong_arena.single_round_artifact.collect_execution_provenance",
                    return_value=provenance(),
                ),
                mock.patch(
                    "lisjong_arena.strength_evaluation.run_single_round_evaluation",
                    return_value=evaluation_result(),
                ),
                mock.patch(
                    "lisjong_arena.strength_evaluation.load_single_round_artifact",
                    side_effect=ValueError("strict summary validation failed"),
                ),
            ):
                with self.assertRaises(StrengthEvaluationExecutionError):
                    run_strength_evaluation(value)
            self.assertTrue(value.artifact_output.exists())
            self.assertFalse(value.result_output.exists())


class CliTest(unittest.TestCase):
    def test_success_is_json_on_stdout_and_compact_summary_on_stderr(self):
        with tempfile.TemporaryDirectory() as directory:
            value, result, _ = ExecutionAndResultTest().run_fixture(Path(directory))
            stdout = io.StringIO()
            stderr = io.StringIO()
            with (
                mock.patch(
                    "lisjong_arena.strength_evaluation.load_strength_evaluation_spec",
                    return_value=value,
                ),
                mock.patch(
                    "lisjong_arena.strength_evaluation.run_strength_evaluation",
                    return_value=result,
                ),
                contextlib.redirect_stdout(stdout),
                contextlib.redirect_stderr(stderr),
            ):
                status = main(["run", "spec.json"])
            self.assertEqual(status, 0)
            self.assertEqual(json.loads(stdout.getvalue()), result)
            self.assertIn("COMPLETED candidate=yakuhai-call", stderr.getvalue())

    def test_failure_is_nonzero_machine_readable_and_has_no_stdout(self):
        stdout = io.StringIO()
        stderr = io.StringIO()
        with (
            mock.patch(
                "lisjong_arena.strength_evaluation.load_strength_evaluation_spec",
                side_effect=StrengthEvaluationSpecError("bad spec"),
            ),
            contextlib.redirect_stdout(stdout),
            contextlib.redirect_stderr(stderr),
        ):
            status = main(["run", "spec.json"])
        self.assertEqual(status, 1)
        self.assertEqual(stdout.getvalue(), "")
        self.assertEqual(json.loads(stderr.getvalue())["status"], "FAILED")


if __name__ == "__main__":
    unittest.main()
