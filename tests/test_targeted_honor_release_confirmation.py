"""Issue #270 independent targeted honor-release confirmation contract tests.

No real 2,200-seed / 17,600-game confirmation is executed in CI.
"""

from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import mock

from _progression_development_fixtures import provenance

import lisjong_arena.targeted_honor_release_confirmation.lock as lock_module
import lisjong_arena.targeted_honor_release_confirmation.paired as paired_module
import lisjong_arena.targeted_honor_release_confirmation.protocol as protocol_module
from lisjong_arena.progression_development.paired import PairedSummary
from lisjong_arena.targeted_honor_release_confirmation.lock import (
    build_lock_document,
    load_lock_document,
    save_lock_document,
)
from lisjong_arena.targeted_honor_release_confirmation.paired import (
    TargetedHonorReleaseConfirmationPairedError,
    classify,
    derive_paired_deltas,
)
from lisjong_arena.targeted_honor_release_confirmation.protocol import (
    CANDIDATE_IDENTITY,
    COMPARATOR_IDENTITY,
    CONFIRMED_NEGATIVE_LABEL,
    CONFIRMED_POSITIVE_LABEL,
    GAMES_PER_ARM,
    INCONCLUSIVE_LABEL,
    ISSUE_263_DEVELOPMENT_SEEDS,
    LISJONG_REVISION,
    MAX_STEPS,
    SEED_BLOCK_COUNT,
    TargetedHonorReleaseConfirmationProtocolError,
    require_confirmation_population,
    seed_freshness_block,
)
from lisjong_arena.targeted_honor_release_confirmation.trace import (
    TargetedHonorReleaseConfirmationTraceError,
    build_trace_artifact,
)

ARENA_REVISION = "a" * 40
FRESH_SEEDS = tuple(range(50_000, 50_000 + SEED_BLOCK_COUNT))


def _live_provenance():
    base = provenance(lisjong_revision=LISJONG_REVISION)
    return base.__class__(
        execution_environment=base.execution_environment,
        lisjong_arena_version=base.lisjong_arena_version,
        lisjong_arena_revision=ARENA_REVISION,
        lisjong_version=base.lisjong_version,
        lisjong_revision=LISJONG_REVISION,
        lisjong_engine_version=base.lisjong_engine_version,
        lisjong_engine_revision=base.lisjong_engine_revision,
        riichienv_version=base.riichienv_version,
        python_version=base.python_version,
    )


class ProtocolTest(unittest.TestCase):
    def test_population_requires_exact_contiguous_2200_seed_shape(self) -> None:
        self.assertEqual(require_confirmation_population(FRESH_SEEDS), FRESH_SEEDS)
        with self.assertRaises(TargetedHonorReleaseConfirmationProtocolError):
            require_confirmation_population(FRESH_SEEDS[:-1])
        with self.assertRaises(TargetedHonorReleaseConfirmationProtocolError):
            require_confirmation_population(FRESH_SEEDS + (FRESH_SEEDS[-1] + 1,))
        malformed = FRESH_SEEDS[:100] + (FRESH_SEEDS[100] + 1,) + FRESH_SEEDS[101:]
        with self.assertRaises(TargetedHonorReleaseConfirmationProtocolError):
            require_confirmation_population(malformed)

    def test_external_issue_and_local_private_audit_is_mandatory(self) -> None:
        with self.assertRaisesRegex(
            TargetedHonorReleaseConfirmationProtocolError, "local/private"
        ):
            seed_freshness_block(
                FRESH_SEEDS,
                external_freshness_confirmed=False,
            )

    def test_external_collision_requires_seed_plan_reformulation(self) -> None:
        with mock.patch.object(
            protocol_module,
            "repository_declared_allocated_seeds",
            return_value=frozenset(),
        ):
            with self.assertRaisesRegex(
                TargetedHonorReleaseConfirmationProtocolError,
                "SEED PLAN REFORMULATE",
            ):
                seed_freshness_block(
                    FRESH_SEEDS,
                    external_freshness_confirmed=True,
                    additional_allocated_seeds=(FRESH_SEEDS[123],),
                )

    def test_issue_263_development_population_is_never_fresh_confirmation(self) -> None:
        overlapping = tuple(range(751, 751 + SEED_BLOCK_COUNT))
        self.assertTrue(set(overlapping).intersection(ISSUE_263_DEVELOPMENT_SEEDS))
        with self.assertRaisesRegex(
            TargetedHonorReleaseConfirmationProtocolError, "SEED PLAN REFORMULATE"
        ):
            seed_freshness_block(
                overlapping,
                external_freshness_confirmed=True,
            )


class LockTest(unittest.TestCase):
    def _destinations(self, directory: Path) -> dict[str, Path]:
        return {
            "candidate_artifact": directory / "candidate.json",
            "parent_artifact": directory / "parent.json",
            "candidate_trace": directory / "candidate-trace.json",
            "paired_result": directory / "paired.json",
            "classified_result": directory / "classified.json",
        }

    def test_lock_binds_population_provenance_and_write_once_destinations(self) -> None:
        with TemporaryDirectory() as directory_text:
            directory = Path(directory_text)
            destinations = self._destinations(directory)
            lock_path = directory / "lock.json"
            live = _live_provenance()
            freshness = {
                "additional_allocated_seeds": [],
                "external_audit_scope": (
                    "relevant open/closed Issues + local/private allocations"
                ),
                "external_collisions": [],
                "external_freshness_confirmed": True,
                "fresh": True,
                "ordered_seeds": list(FRESH_SEEDS),
                "repository_collisions": [],
            }
            with (
                mock.patch.object(
                    lock_module, "seed_freshness_block", return_value=freshness
                ),
                mock.patch.object(lock_module, "_require_environment_consistent"),
                mock.patch.object(
                    lock_module, "collect_execution_provenance", return_value=live
                ),
                mock.patch.object(
                    lock_module, "require_clean_arena_head", return_value=ARENA_REVISION
                ),
                mock.patch.object(
                    lock_module,
                    "require_merged_arena_revision",
                    return_value=ARENA_REVISION,
                ),
            ):
                document = build_lock_document(
                    destinations=destinations,
                    confirmation_seeds=FRESH_SEEDS,
                    max_workers=8,
                    external_freshness_confirmed=True,
                )
                save_lock_document(document, lock_path)
                loaded = load_lock_document(lock_path)
        self.assertEqual(loaded["confirmation"]["seed_block_count"], SEED_BLOCK_COUNT)
        self.assertEqual(loaded["confirmation"]["games_per_arm"], GAMES_PER_ARM)
        self.assertEqual(loaded["provenance"]["lisjong_revision"], LISJONG_REVISION)
        self.assertFalse(loaded["result_exposed"])


class ClassificationTest(unittest.TestCase):
    def _summary(self, lower: float, upper: float) -> PairedSummary:
        return PairedSummary(
            block_count=SEED_BLOCK_COUNT,
            mean_delta=(lower + upper) / 2.0,
            sample_standard_deviation=100.0,
            standard_error=2.0,
            interval_lower=lower,
            interval_upper=upper,
        )

    def test_three_way_rule_and_zero_boundaries_are_exact(self) -> None:
        self.assertEqual(
            classify(self._summary(0.01, 2.0))["label"], CONFIRMED_POSITIVE_LABEL
        )
        self.assertEqual(
            classify(self._summary(-2.0, -0.01))["label"], CONFIRMED_NEGATIVE_LABEL
        )
        self.assertEqual(classify(self._summary(0.0, 2.0))["label"], INCONCLUSIVE_LABEL)
        self.assertEqual(
            classify(self._summary(-2.0, 0.0))["label"], INCONCLUSIVE_LABEL
        )

    def test_classification_rejects_wrong_primary_unit_count(self) -> None:
        wrong = PairedSummary(
            block_count=SEED_BLOCK_COUNT - 1,
            mean_delta=1.0,
            sample_standard_deviation=1.0,
            standard_error=1.0,
            interval_lower=0.1,
            interval_upper=1.9,
        )
        with self.assertRaises(TargetedHonorReleaseConfirmationPairedError):
            classify(wrong)


class PairedDeltaTest(unittest.TestCase):
    def test_derives_exactly_2200_paired_seed_deltas(self) -> None:
        provenance_identity = object()
        candidate = SimpleNamespace(provenance=provenance_identity, game_results=())
        parent = SimpleNamespace(provenance=provenance_identity, game_results=())
        h_blocks = tuple((seed, 25_100.0) for seed in FRESH_SEEDS)
        c_blocks = tuple((seed, 25_000.0) for seed in FRESH_SEEDS)
        with (
            mock.patch.object(paired_module, "_require_arm"),
            mock.patch.object(
                paired_module,
                "focal_seed_block_means",
                side_effect=(h_blocks, c_blocks),
            ),
        ):
            deltas = derive_paired_deltas(candidate, parent, seeds=FRESH_SEEDS)
        self.assertEqual(len(deltas), SEED_BLOCK_COUNT)
        self.assertEqual(tuple(item.seed for item in deltas), FRESH_SEEDS)
        self.assertTrue(all(item.delta == 100.0 for item in deltas))

    def test_arm_validation_rejects_incomplete_8799_game_evidence(self) -> None:
        plan = SimpleNamespace(
            candidate_identity=CANDIDATE_IDENTITY,
            baseline_identity=COMPARATOR_IDENTITY,
            seeds=FRESH_SEEDS,
            max_steps=MAX_STEPS,
        )
        artifact = SimpleNamespace(
            plan=plan,
            game_results=(None,) * (GAMES_PER_ARM - 1),
            provenance=SimpleNamespace(lisjong_revision=LISJONG_REVISION),
        )
        with self.assertRaisesRegex(
            TargetedHonorReleaseConfirmationPairedError, str(GAMES_PER_ARM)
        ):
            paired_module._require_arm(
                artifact,
                expected_identity=CANDIDATE_IDENTITY,
                seeds=FRESH_SEEDS,
                arm="H",
            )


class TraceTest(unittest.TestCase):
    def test_trace_rejects_incomplete_game_alignment_before_serialization(self) -> None:
        candidate = SimpleNamespace(
            plan=SimpleNamespace(
                candidate_identity=CANDIDATE_IDENTITY,
                seeds=FRESH_SEEDS,
            ),
            provenance=SimpleNamespace(lisjong_revision=LISJONG_REVISION),
            game_results=(None,) * GAMES_PER_ARM,
        )
        with self.assertRaisesRegex(
            TargetedHonorReleaseConfirmationTraceError, str(GAMES_PER_ARM)
        ):
            build_trace_artifact(
                games=(None,) * (GAMES_PER_ARM - 1),
                aggregate=SimpleNamespace(replay_wall_clock_seconds=1.0),
                candidate_artifact=candidate,
                candidate_artifact_path="candidate.json",
                seeds=FRESH_SEEDS,
                worker_count=8,
            )


if __name__ == "__main__":
    unittest.main()
