"""ABBB strength artifactのschema / validation / composition contractのunit test。

実RiichiEnvを起動せず、既存``SingleRoundEvaluationResult``契約を満たす
fixture raw resultsだけを使う。artifactのderived statisticsは常にraw game
resultsからのcanonical再集計と照合されることを固定する。
"""

import copy
import dataclasses
import json
import re
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import _single_round_artifact_fixtures as fixtures
from lisjong.policy_contract import Seat

from lisjong_arena.artifact import ComparisonArtifactError, load_comparison_artifact
from lisjong_arena.model import SingleRoundGameResult
from lisjong_arena.riichienv.round_stats import SeatRoundStats
from lisjong_arena.single_round_artifact import (
    SINGLE_ROUND_ARTIFACT_SCHEMA_VERSION,
    SINGLE_ROUND_EVALUATION_PROTOCOL,
    SingleRoundArtifactError,
    SingleRoundArtifactPlan,
    SingleRoundExecutionProvenance,
    SingleRoundStrengthArtifact,
    _arena_source_revision,
    _collect_execution_provenance,
    _vcs_revision,
    load_single_round_artifact,
    merge_single_round_artifacts,
    save_single_round_artifact,
)
from lisjong_arena.single_round_evaluation import SingleRoundStrengthSummary


class RoundTripTest(unittest.TestCase):
    def test_round_trip_returns_a_factory_free_immutable_snapshot(self) -> None:
        result = fixtures.evaluation_result(seeds=(20_200, 20_201))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "run.json"
            fixtures.save(result, path)
            artifact = load_single_round_artifact(path)

        self.assertIsInstance(artifact, SingleRoundStrengthArtifact)
        self.assertEqual(artifact.schema_version, SINGLE_ROUND_ARTIFACT_SCHEMA_VERSION)
        self.assertEqual(artifact.evaluation_protocol, SINGLE_ROUND_EVALUATION_PROTOCOL)
        self.assertIsInstance(artifact.plan, SingleRoundArtifactPlan)
        self.assertFalse(hasattr(artifact.plan, "candidate"))
        self.assertFalse(hasattr(artifact.plan, "factory"))
        self.assertEqual(artifact.plan.candidate_identity, fixtures.CANDIDATE)
        self.assertEqual(artifact.plan.baseline_identity, fixtures.BASELINE)
        self.assertEqual(artifact.plan.seeds, (20_200, 20_201))
        self.assertEqual(artifact.plan.game_mode, "4p-red-single")
        self.assertEqual(artifact.plan.rotation_count, 4)
        self.assertEqual(artifact.plan.max_steps, 10_000)
        self.assertEqual(artifact.game_results, result.game_results)
        self.assertEqual(artifact.provenance, fixtures.provenance())
        with self.assertRaises(dataclasses.FrozenInstanceError):
            artifact.schema_version = 2

    def test_raw_game_results_keep_scores_and_seat_round_stats(self) -> None:
        result = fixtures.evaluation_result(seeds=(7,))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "run.json"
            fixtures.save(result, path)
            artifact = load_single_round_artifact(path)

        for original, restored in zip(result.game_results, artifact.game_results):
            self.assertEqual(restored.seed, original.seed)
            self.assertEqual(restored.rotation, original.rotation)
            self.assertEqual(restored.candidate_seat, original.candidate_seat)
            self.assertEqual(restored.scores, original.scores)
            self.assertEqual(restored.seat_round_stats, original.seat_round_stats)
            self.assertTrue(
                all(
                    isinstance(stats, SeatRoundStats)
                    for stats in restored.seat_round_stats
                )
            )

    def test_summary_matches_canonical_aggregation_of_raw_results(self) -> None:
        result = fixtures.evaluation_result(seeds=(1, 2, 3))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "run.json"
            fixtures.save(result, path)
            artifact = load_single_round_artifact(path)

        self.assertEqual(
            artifact.summary,
            fixtures.canonical_summary(result.game_results),
        )
        self.assertEqual(artifact.summary.candidate_metrics, result.candidate_metrics)

    def test_json_holds_no_factory_trace_or_machine_local_information(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "run.json"
            fixtures.save(fixtures.evaluation_result(), path)
            serialized = path.read_text(encoding="utf-8")
            document = json.loads(serialized)

        for forbidden in (
            "factory",
            "hostname",
            "username",
            "home",
            "game_trace",
            "decision",
            "timestamp",
        ):
            self.assertNotIn(forbidden, serialized)
        self.assertEqual(
            document["provenance"]["lisjong_arena_revision"], fixtures.ARENA_REVISION
        )
        self.assertEqual(
            document["provenance"]["lisjong_revision"], fixtures.LISJONG_REVISION
        )
        self.assertEqual(
            document["provenance"]["lisjong_engine_revision"],
            fixtures.LISJONG_ENGINE_REVISION,
        )


class SerializationTest(unittest.TestCase):
    def test_same_result_has_stable_utf8_json_serialization(self) -> None:
        result = fixtures.evaluation_result()
        with tempfile.TemporaryDirectory() as directory:
            first = Path(directory) / "first.json"
            second = Path(directory) / "second.json"
            fixtures.save(result, first)
            fixtures.save(result, second)

            self.assertEqual(first.read_bytes(), second.read_bytes())
            serialized = first.read_text(encoding="utf-8")

        self.assertTrue(serialized.endswith("\n"))
        self.assertIn('  "evaluation_protocol"', serialized)
        self.assertNotIn("NaN", serialized)
        self.assertNotIn("Infinity", serialized)


class FailClosedLoadTest(unittest.TestCase):
    def setUp(self) -> None:
        self._directory = tempfile.TemporaryDirectory()
        self.path = Path(self._directory.name) / "run.json"
        fixtures.save(fixtures.evaluation_result(seeds=(11, 12)), self.path)
        self.document = json.loads(self.path.read_text(encoding="utf-8"))

    def tearDown(self) -> None:
        self._directory.cleanup()

    def _assert_rejected(self, document: object) -> None:
        self.path.write_text(
            json.dumps(document, ensure_ascii=False, allow_nan=False),
            encoding="utf-8",
        )
        with self.assertRaises(SingleRoundArtifactError):
            load_single_round_artifact(self.path)

    def test_rejects_malformed_and_truncated_json(self) -> None:
        for serialized in ("not json", '{"schema_version": 1'):
            with self.subTest(serialized=serialized):
                self.path.write_text(serialized, encoding="utf-8")
                with self.assertRaises(SingleRoundArtifactError):
                    load_single_round_artifact(self.path)

    def test_rejects_non_finite_json_number(self) -> None:
        valid = self.path.read_text(encoding="utf-8")
        serialized, replaced = re.subn(
            r'"mean_baseline_score": -?[0-9.e+-]+',
            '"mean_baseline_score": NaN',
            valid,
            count=1,
        )
        self.assertEqual(replaced, 1)
        self.path.write_text(serialized, encoding="utf-8")
        with self.assertRaises(SingleRoundArtifactError):
            load_single_round_artifact(self.path)

    def test_rejects_duplicate_object_keys(self) -> None:
        valid = self.path.read_text(encoding="utf-8")
        duplicate_top_level = valid.replace(
            '"schema_version": 1',
            '"schema_version": 999,\n  "schema_version": 1',
            1,
        )
        duplicate_plan = valid.replace(
            '"max_steps": 10000,',
            '"max_steps": 999,\n    "max_steps": 10000,',
            1,
        )

        for serialized in (duplicate_top_level, duplicate_plan):
            with self.subTest(serialized=serialized):
                self.path.write_text(serialized, encoding="utf-8")
                with self.assertRaises(SingleRoundArtifactError):
                    load_single_round_artifact(self.path)

    def test_rejects_unsupported_schema_version_and_protocol(self) -> None:
        for field, value in (
            ("schema_version", 2),
            ("evaluation_protocol", "abbb-single-round-v2"),
            ("evaluation_protocol", "fixed-seed-seat-rotation-v1"),
        ):
            with self.subTest(field=field, value=value):
                changed = copy.deepcopy(self.document)
                changed[field] = value
                self._assert_rejected(changed)

    def test_rejects_missing_unknown_and_mistyped_fields(self) -> None:
        without_field = copy.deepcopy(self.document)
        del without_field["plan"]["max_steps"]
        unknown_field = copy.deepcopy(self.document)
        unknown_field["plan"]["workers"] = 8
        mistyped_seed = copy.deepcopy(self.document)
        mistyped_seed["plan"]["seeds"][0] = "11"
        mistyped_score = copy.deepcopy(self.document)
        mistyped_score["summary"]["mean_baseline_score"] = 0

        for document in (without_field, unknown_field, mistyped_seed, mistyped_score):
            with self.subTest(document=document["plan"]):
                self._assert_rejected(document)

    def test_rejects_plan_conditions_outside_the_protocol_invariant(self) -> None:
        for field, value in (
            ("game_mode", "4p-red-half"),
            ("rotation_count", 2),
            ("max_steps", 0),
        ):
            with self.subTest(field=field):
                changed = copy.deepcopy(self.document)
                changed["plan"][field] = value
                self._assert_rejected(changed)

    def test_rejects_candidate_and_baseline_identity_collision(self) -> None:
        changed = copy.deepcopy(self.document)
        changed["plan"]["baseline_identity"] = changed["plan"]["candidate_identity"]
        self._assert_rejected(changed)

    def test_rejects_game_count_and_seed_ordering_inconsistency(self) -> None:
        missing_game = copy.deepcopy(self.document)
        del missing_game["game_results"][-1]
        reordered_seeds = copy.deepcopy(self.document)
        reordered_seeds["plan"]["seeds"] = [12, 11]
        duplicate_seeds = copy.deepcopy(self.document)
        duplicate_seeds["plan"]["seeds"] = [11, 11]

        for document in (missing_game, reordered_seeds, duplicate_seeds):
            with self.subTest(seeds=document["plan"]["seeds"]):
                self._assert_rejected(document)

    def test_rejects_rotation_and_candidate_seat_inconsistency(self) -> None:
        swapped_rotations = copy.deepcopy(self.document)
        swapped_rotations["game_results"][0], swapped_rotations["game_results"][1] = (
            swapped_rotations["game_results"][1],
            swapped_rotations["game_results"][0],
        )
        wrong_candidate_seat = copy.deepcopy(self.document)
        wrong_candidate_seat["game_results"][0]["candidate_seat"] = 1
        unknown_seat = copy.deepcopy(self.document)
        unknown_seat["game_results"][0]["candidate_seat"] = 9
        wrong_game_mode = copy.deepcopy(self.document)
        wrong_game_mode["game_results"][0]["game_mode"] = "4p-red-half"

        for document in (
            swapped_rotations,
            wrong_candidate_seat,
            unknown_seat,
            wrong_game_mode,
        ):
            self._assert_rejected(document)

    def test_rejects_scores_inconsistent_with_seat_round_stats(self) -> None:
        changed = copy.deepcopy(self.document)
        changed["game_results"][0]["scores"][1] += 100
        self._assert_rejected(changed)

    def test_rejects_tampered_derived_metrics(self) -> None:
        mean_score = copy.deepcopy(self.document)
        mean_score["summary"]["candidate_metrics"]["mean_candidate_score"] += 1.0
        baseline_score = copy.deepcopy(self.document)
        baseline_score["summary"]["mean_baseline_score"] += 1.0
        mean_delta = copy.deepcopy(self.document)
        mean_delta["summary"]["mean_candidate_game_delta"] += 1.0
        seat_means = copy.deepcopy(self.document)
        seat_means["summary"]["candidate_metrics"]["seat_mean_scores"][0] += 1.0
        seed_blocks = copy.deepcopy(self.document)
        seed_blocks["summary"]["seed_block_statistics"]["standard_error"] = 1.0
        mahjong = copy.deepcopy(self.document)
        mahjong["summary"]["candidate_metrics"]["mahjong_metrics"][
            "mean_round_score_delta"
        ] += 1.0

        for document in (
            mean_score,
            baseline_score,
            mean_delta,
            seat_means,
            seed_blocks,
            mahjong,
        ):
            self._assert_rejected(document)

    def test_rejects_malformed_provenance(self) -> None:
        for field, value in (
            ("execution_environment", "unknown"),
            ("lisjong_arena_revision", "not-a-full-commit"),
            ("lisjong_arena_revision", fixtures.ARENA_REVISION.upper()),
            ("lisjong_revision", "not-a-full-commit"),
            ("lisjong_engine_revision", "not-a-full-commit"),
            ("riichienv_version", ""),
        ):
            with self.subTest(field=field):
                changed = copy.deepcopy(self.document)
                changed["provenance"][field] = value
                self._assert_rejected(changed)


class CrossContractTest(unittest.TestCase):
    """AABB artifactとABBB artifactは互いのcontractとして受理しない。"""

    def test_single_round_loader_rejects_a_comparison_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "comparison.json"
            path.write_text(
                json.dumps(
                    {
                        "comparison_protocol": "fixed-seed-seat-rotation-v1",
                        "metrics": {},
                        "plan": {},
                        "provenance": {},
                        "schema_version": 1,
                        "seat_results": [],
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaises(SingleRoundArtifactError):
                load_single_round_artifact(path)

    def test_comparison_loader_rejects_a_single_round_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "run.json"
            fixtures.save(fixtures.evaluation_result(), path)
            with self.assertRaises(ComparisonArtifactError):
                load_comparison_artifact(path)


class FileHandlingTest(unittest.TestCase):
    def test_existing_destination_is_not_overwritten(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "run.json"
            path.write_text("existing", encoding="utf-8")

            with self.assertRaises(FileExistsError):
                fixtures.save(fixtures.evaluation_result(), path)

            self.assertEqual(path.read_text(encoding="utf-8"), "existing")

    def test_mortal_style_result_is_rejected_before_file_creation(self) -> None:
        class _NotASingleRoundEvaluationResult:
            pass

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "run.json"
            with self.assertRaises(TypeError):
                save_single_round_artifact(_NotASingleRoundEvaluationResult(), path)
            self.assertFalse(path.exists())

    def test_result_metrics_inconsistent_with_raw_results_are_rejected(self) -> None:
        result = fixtures.evaluation_result(seeds=(1,))
        tampered = dataclasses.replace(
            result,
            candidate_metrics=dataclasses.replace(
                result.candidate_metrics,
                mean_candidate_score=result.candidate_metrics.mean_candidate_score
                + 1.0,
            ),
        )

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "run.json"
            with self.assertRaises(ValueError):
                fixtures.save(tampered, path)
            self.assertFalse(path.exists())

    def test_unavailable_provenance_fails_before_file_creation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "run.json"
            with mock.patch(
                "lisjong_arena.single_round_artifact._collect_execution_provenance",
                side_effect=SingleRoundArtifactError("metadata is unavailable"),
            ):
                with self.assertRaises(SingleRoundArtifactError):
                    save_single_round_artifact(fixtures.evaluation_result(), path)
            self.assertFalse(path.exists())

    def test_partial_file_is_removed_after_write_failure(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "run.json"

            class _FailingStream:
                def __enter__(self):
                    self.stream = open(path, "x", encoding="utf-8")
                    return self

                def __exit__(self, *args):
                    self.stream.close()

                def write(self, value: str) -> None:
                    self.stream.write(value[:10])
                    self.stream.flush()
                    raise OSError("disk full")

            with mock.patch.object(Path, "open", return_value=_FailingStream()):
                with self.assertRaises(OSError):
                    fixtures.save(fixtures.evaluation_result(), path)

            self.assertFalse(path.exists())


def _fake_git(
    *,
    tracked: bool = True,
    status: str = "",
    head: str = fixtures.ARENA_REVISION,
):
    """``git ls-files`` / ``status`` / ``rev-parse``の結果を差し替える。

    実際のArena working treeの状態にtestが依存しないよう、gitの実行境界
    だけをstubする。
    """

    def run(command, **kwargs):
        subcommand = command[3]
        if subcommand == "ls-files":
            return subprocess.CompletedProcess(
                command, 0 if tracked else 1, stdout="", stderr=""
            )
        if subcommand == "status":
            return subprocess.CompletedProcess(command, 0, stdout=status, stderr="")
        if subcommand == "rev-parse":
            return subprocess.CompletedProcess(
                command, 0, stdout=f"{head}\n", stderr=""
            )
        raise AssertionError(f"unexpected git subcommand: {subcommand!r}")

    return run


class ArenaSourceRevisionTest(unittest.TestCase):
    """Arena自身のexact revisionは推測せず、確定できなければfail closedする。"""

    def _run_with(self, runner) -> str:
        with mock.patch(
            "lisjong_arena.single_round_artifact.subprocess.run",
            side_effect=runner,
        ):
            return _arena_source_revision()

    def test_clean_source_tree_reports_the_full_head_commit_id(self) -> None:
        self.assertEqual(
            self._run_with(_fake_git()),
            fixtures.ARENA_REVISION,
        )

    def test_dirty_source_tree_fails_closed(self) -> None:
        for status in (
            " M single_round_artifact.py\n",
            "?? new_module.py\n",
            "A  staged_module.py\n",
        ):
            with self.subTest(status=status):
                with self.assertRaises(SingleRoundArtifactError):
                    self._run_with(_fake_git(status=status))

    def test_untracked_source_tree_fails_closed(self) -> None:
        with self.assertRaises(SingleRoundArtifactError):
            self._run_with(_fake_git(tracked=False))

    def test_unavailable_git_fails_closed(self) -> None:
        for error in (
            FileNotFoundError("git"),
            subprocess.TimeoutExpired(cmd="git", timeout=30),
        ):
            with self.subTest(error=type(error).__name__):
                with self.assertRaises(SingleRoundArtifactError):
                    self._run_with(mock.Mock(side_effect=error))

    def test_malformed_head_commit_id_fails_closed(self) -> None:
        for head in ("HEAD", "abc123", fixtures.ARENA_REVISION.upper()):
            with self.subTest(head=head):
                with self.assertRaises(SingleRoundArtifactError):
                    self._run_with(_fake_git(head=head))

    def test_collected_provenance_records_the_arena_revision(self) -> None:
        with (
            mock.patch(
                "lisjong_arena.single_round_artifact.subprocess.run",
                side_effect=_fake_git(),
            ),
            mock.patch(
                "lisjong_arena.single_round_artifact._package_version",
                return_value="0.1.0",
            ),
            mock.patch(
                "lisjong_arena.single_round_artifact._vcs_revision",
                return_value=fixtures.LISJONG_REVISION,
            ),
        ):
            provenance = _collect_execution_provenance()

        self.assertEqual(provenance.lisjong_arena_revision, fixtures.ARENA_REVISION)
        self.assertEqual(provenance.lisjong_arena_version, "0.1.0")

    def test_unavailable_arena_revision_prevents_artifact_creation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "run.json"
            with mock.patch(
                "lisjong_arena.single_round_artifact.subprocess.run",
                side_effect=_fake_git(tracked=False),
            ):
                with self.assertRaises(SingleRoundArtifactError):
                    save_single_round_artifact(fixtures.evaluation_result(), path)
            self.assertFalse(path.exists())


class ProvenanceCollectionTest(unittest.TestCase):
    def test_revision_comes_from_vcs_install_metadata(self) -> None:
        distribution = mock.Mock()
        distribution.read_text.return_value = json.dumps(
            {
                "url": "https://github.com/lisbun/lisjong.git",
                "vcs_info": {
                    "commit_id": fixtures.LISJONG_REVISION,
                    "requested_revision": fixtures.LISJONG_REVISION,
                    "vcs": "git",
                },
            }
        )
        with mock.patch(
            "lisjong_arena.single_round_artifact.metadata.distribution",
            return_value=distribution,
        ):
            self.assertEqual(_vcs_revision("lisjong"), fixtures.LISJONG_REVISION)

    def test_editable_install_without_vcs_metadata_fails_closed(self) -> None:
        distribution = mock.Mock()
        distribution.read_text.return_value = json.dumps(
            {
                "url": "file:///home/user/lisjong-arena",
                "dir_info": {"editable": True},
            }
        )
        with mock.patch(
            "lisjong_arena.single_round_artifact.metadata.distribution",
            return_value=distribution,
        ):
            with self.assertRaises(SingleRoundArtifactError):
                _vcs_revision("lisjong-arena")

    def test_missing_direct_url_metadata_fails_closed(self) -> None:
        distribution = mock.Mock()
        distribution.read_text.return_value = None
        with mock.patch(
            "lisjong_arena.single_round_artifact.metadata.distribution",
            return_value=distribution,
        ):
            with self.assertRaises(SingleRoundArtifactError):
                _vcs_revision("lisjong")


def _artifact(
    seeds: tuple[int, ...],
    *,
    candidate: str = fixtures.CANDIDATE,
    baseline: str = fixtures.BASELINE,
    max_steps: int = 10_000,
    execution_provenance: SingleRoundExecutionProvenance | None = None,
) -> SingleRoundStrengthArtifact:
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "run.json"
        fixtures.save(
            fixtures.evaluation_result(
                seeds=seeds,
                candidate=candidate,
                baseline=baseline,
                max_steps=max_steps,
            ),
            path,
            execution_provenance=execution_provenance,
        )
        return load_single_round_artifact(path)


class CompositionTest(unittest.TestCase):
    def test_single_artifact_summary_is_the_artifact_summary(self) -> None:
        artifact = _artifact((1, 2))

        cumulative = merge_single_round_artifacts([artifact])

        self.assertEqual(cumulative.artifact_count, 1)
        self.assertEqual(cumulative.plan, artifact.plan)
        self.assertEqual(cumulative.game_results, artifact.game_results)
        self.assertEqual(cumulative.summary, artifact.summary)

    def test_disjoint_artifacts_are_merged_in_input_order(self) -> None:
        first = _artifact((1, 2))
        second = _artifact((3,))

        cumulative = merge_single_round_artifacts([first, second])

        self.assertEqual(cumulative.artifact_count, 2)
        self.assertEqual(cumulative.plan.seeds, (1, 2, 3))
        self.assertEqual(
            cumulative.game_results, first.game_results + second.game_results
        )
        self.assertEqual(cumulative.provenance, first.provenance)

    def test_input_order_is_preserved_without_sorting_seeds(self) -> None:
        first = _artifact((1, 2))
        second = _artifact((3,))

        cumulative = merge_single_round_artifacts([second, first])

        self.assertEqual(cumulative.plan.seeds, (3, 1, 2))
        self.assertEqual(
            cumulative.game_results, second.game_results + first.game_results
        )

    def test_cumulative_aggregation_equals_one_shot_aggregation(self) -> None:
        first = _artifact((1, 2))
        second = _artifact((3,))
        one_shot = fixtures.evaluation_result(seeds=(1, 2, 3))

        cumulative = merge_single_round_artifacts([first, second])

        self.assertEqual(
            cumulative.summary,
            fixtures.canonical_summary(one_shot.game_results),
        )
        self.assertEqual(
            cumulative.summary.candidate_metrics.game_count,
            len(one_shot.game_results),
        )
        self.assertEqual(cumulative.summary.seed_block_statistics.seed_block_count, 3)

    def test_seed_block_statistics_are_recomputed_from_raw_results(self) -> None:
        """個々のartifactのaggregateを合成せず、連結rawから再集計する。"""
        first = _artifact((1, 2))
        second = _artifact((3,))
        one_shot = fixtures.canonical_summary(fixtures.game_results((1, 2, 3)))

        cumulative = merge_single_round_artifacts([first, second])

        self.assertEqual(
            cumulative.summary.seed_block_statistics,
            one_shot.seed_block_statistics,
        )
        self.assertIsNone(second.summary.seed_block_statistics.standard_error)
        self.assertNotEqual(
            first.summary.seed_block_statistics.standard_error,
            cumulative.summary.seed_block_statistics.standard_error,
        )

    def test_rejects_an_empty_artifact_sequence(self) -> None:
        with self.assertRaises(SingleRoundArtifactError):
            merge_single_round_artifacts([])

    def test_rejects_non_artifact_inputs(self) -> None:
        with self.assertRaises(TypeError):
            merge_single_round_artifacts([_artifact((1,)), object()])
        with self.assertRaises(TypeError):
            merge_single_round_artifacts(_artifact((1,)))

    def test_rejects_candidate_identity_mismatch(self) -> None:
        first = _artifact((1,))
        second = _artifact((2,), candidate="extended-combined")

        with self.assertRaises(SingleRoundArtifactError):
            merge_single_round_artifacts([first, second])

    def test_rejects_baseline_identity_mismatch(self) -> None:
        first = _artifact((1,))
        second = _artifact((2,), baseline="two-step")

        with self.assertRaises(SingleRoundArtifactError):
            merge_single_round_artifacts([first, second])

    def test_rejects_evaluation_parameter_mismatch(self) -> None:
        first = _artifact((1,))
        second = _artifact((2,), max_steps=9_000)

        with self.assertRaises(SingleRoundArtifactError):
            merge_single_round_artifacts([first, second])

    def test_rejects_overlapping_seeds_without_deduplicating(self) -> None:
        first = _artifact((1, 2))
        second = _artifact((2, 3))

        with self.assertRaises(SingleRoundArtifactError):
            merge_single_round_artifacts([first, second])

    def test_rejects_the_same_artifact_twice(self) -> None:
        artifact = _artifact((1, 2))

        with self.assertRaises(SingleRoundArtifactError):
            merge_single_round_artifacts([artifact, artifact])

    def test_rejects_incompatible_provenance_by_default(self) -> None:
        first = _artifact((1,))
        second = _artifact(
            (2,),
            execution_provenance=fixtures.provenance(
                lisjong_revision="0" * 40,
            ),
        )

        with self.assertRaises(SingleRoundArtifactError):
            merge_single_round_artifacts([first, second])

    def test_rejects_arena_revision_mismatch_with_the_same_version(self) -> None:
        """同じ``lisjong-arena`` versionでもArena revisionが違えばmergeしない。"""
        first = _artifact((1,))
        second = _artifact(
            (2,),
            execution_provenance=fixtures.provenance(lisjong_arena_revision="0" * 40),
        )

        self.assertEqual(
            first.provenance.lisjong_arena_version,
            second.provenance.lisjong_arena_version,
        )
        with self.assertRaises(SingleRoundArtifactError):
            merge_single_round_artifacts([first, second])

    def test_rejects_provenance_version_mismatch(self) -> None:
        first = _artifact((1,))
        second = _artifact(
            (2,), execution_provenance=fixtures.provenance(lisjong_version="0.2.0")
        )

        with self.assertRaises(SingleRoundArtifactError):
            merge_single_round_artifacts([first, second])


class ArtifactValueTest(unittest.TestCase):
    """artifact valueをdirect構築した場合のfail closed contract。"""

    def _plan(self, seeds: tuple[int, ...] = (1,)) -> SingleRoundArtifactPlan:
        return SingleRoundArtifactPlan(
            candidate_identity=fixtures.CANDIDATE,
            baseline_identity=fixtures.BASELINE,
            seeds=seeds,
            game_mode="4p-red-single",
            rotation_count=4,
            max_steps=10_000,
        )

    def test_rejects_a_summary_that_does_not_match_raw_results(self) -> None:
        game_results = fixtures.game_results((1,))
        summary = fixtures.canonical_summary(game_results)
        tampered = SingleRoundStrengthSummary(
            candidate_metrics=summary.candidate_metrics,
            mean_baseline_score=summary.mean_baseline_score + 1.0,
            mean_candidate_game_delta=summary.mean_candidate_game_delta,
            seed_block_statistics=summary.seed_block_statistics,
        )

        with self.assertRaises(ValueError):
            SingleRoundStrengthArtifact(
                schema_version=SINGLE_ROUND_ARTIFACT_SCHEMA_VERSION,
                evaluation_protocol=SINGLE_ROUND_EVALUATION_PROTOCOL,
                plan=self._plan(),
                provenance=fixtures.provenance(),
                game_results=game_results,
                summary=tampered,
            )

    def test_rejects_a_summary_aggregated_for_another_candidate(self) -> None:
        game_results = fixtures.game_results((1,))

        with self.assertRaises(ValueError):
            SingleRoundStrengthArtifact(
                schema_version=SINGLE_ROUND_ARTIFACT_SCHEMA_VERSION,
                evaluation_protocol=SINGLE_ROUND_EVALUATION_PROTOCOL,
                plan=self._plan(),
                provenance=fixtures.provenance(),
                game_results=game_results,
                summary=fixtures.canonical_summary(
                    game_results, candidate="someone-else"
                ),
            )

    def test_rejects_unsupported_schema_version_and_protocol(self) -> None:
        game_results = fixtures.game_results((1,))
        summary = fixtures.canonical_summary(game_results)
        for schema_version, protocol in (
            (2, SINGLE_ROUND_EVALUATION_PROTOCOL),
            (SINGLE_ROUND_ARTIFACT_SCHEMA_VERSION, "abbb-single-round-v2"),
        ):
            with self.subTest(schema_version=schema_version, protocol=protocol):
                with self.assertRaises(ValueError):
                    SingleRoundStrengthArtifact(
                        schema_version=schema_version,
                        evaluation_protocol=protocol,
                        plan=self._plan(),
                        provenance=fixtures.provenance(),
                        game_results=game_results,
                        summary=summary,
                    )

    def test_rejects_game_results_that_do_not_match_the_plan_seeds(self) -> None:
        game_results = fixtures.game_results((1,))
        summary = fixtures.canonical_summary(game_results)

        with self.assertRaises(ValueError):
            SingleRoundStrengthArtifact(
                schema_version=SINGLE_ROUND_ARTIFACT_SCHEMA_VERSION,
                evaluation_protocol=SINGLE_ROUND_EVALUATION_PROTOCOL,
                plan=self._plan(seeds=(2,)),
                provenance=fixtures.provenance(),
                game_results=game_results,
                summary=summary,
            )

    def test_plan_rejects_conditions_outside_the_protocol_invariant(self) -> None:
        for field, value in (
            ("game_mode", "4p-red-half"),
            ("rotation_count", 2),
            ("max_steps", -1),
            ("baseline_identity", fixtures.CANDIDATE),
        ):
            with self.subTest(field=field):
                fields = {
                    "candidate_identity": fixtures.CANDIDATE,
                    "baseline_identity": fixtures.BASELINE,
                    "seeds": (1,),
                    "game_mode": "4p-red-single",
                    "rotation_count": 4,
                    "max_steps": 10_000,
                    field: value,
                }
                with self.assertRaises(ValueError):
                    SingleRoundArtifactPlan(**fields)

    def test_game_result_rejects_scores_inconsistent_with_round_stats(self) -> None:
        scores = fixtures.game_scores(1, 0)
        with self.assertRaises(ValueError):
            SingleRoundGameResult(
                seed=1,
                rotation=0,
                game_mode="4p-red-single",
                candidate_seat=Seat(0),
                scores=(scores[0] + 100, *scores[1:]),
                seat_round_stats=fixtures.game_results((1,))[0].seat_round_stats,
            )


if __name__ == "__main__":
    unittest.main()
