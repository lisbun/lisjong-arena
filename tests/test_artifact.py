import copy
import dataclasses
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from lisjong.policy_contract import Seat

from lisjong_arena import (
    ARTIFACT_SCHEMA_VERSION,
    COMPARISON_PROTOCOL,
    ArtifactPlan,
    ComparisonArtifact,
    ComparisonArtifactError,
    ComparisonPlan,
    ComparisonResult,
    ExecutionProvenance,
    PolicyMetrics,
    PolicySpec,
    SeatResult,
    load_comparison_artifact,
    save_comparison_artifact,
)
from lisjong_arena.artifact import _lisjong_revision
from lisjong_arena.comparison import ROTATION_COUNT, aggregate_policy_metrics

_REVISION = "b11841e287e8f11d55fe0fdaa5127ad16e00aa01"
_RANKS = (1, 2, 3, 4)
_SCORES = (40_000, 30_000, 20_000, 10_000)


class _StubPolicy:
    def choose_action(self, decision: object) -> object:
        raise AssertionError("artifact tests must not execute policies")


def _identity_assignment(rotation: int) -> tuple[str, str, str, str]:
    base = ("a", "a", "b", "b")
    return tuple(base[(seat - rotation) % ROTATION_COUNT] for seat in range(4))


def _result(seeds: tuple[int, ...] = (30, 10)) -> ComparisonResult:
    plan = ComparisonPlan(
        policy_a=PolicySpec(identity="a", factory=_StubPolicy),
        policy_b=PolicySpec(identity="b", factory=_StubPolicy),
        seeds=seeds,
        game_mode="4p-red-east",
        max_steps=321,
    )
    seat_results = tuple(
        SeatResult(
            seed=seed,
            rotation=rotation,
            game_mode=plan.game_mode,
            seat=seat,
            policy_identity=_identity_assignment(rotation)[seat],
            score=_SCORES[seat],
            rank=_RANKS[seat],
        )
        for seed in seeds
        for rotation in range(ROTATION_COUNT)
        for seat in Seat
    )
    return ComparisonResult(
        plan=plan,
        seat_results=seat_results,
        metrics_a=aggregate_policy_metrics("a", seat_results),
        metrics_b=aggregate_policy_metrics("b", seat_results),
    )


def _provenance() -> ExecutionProvenance:
    return ExecutionProvenance(
        execution_environment="riichienv",
        lisjong_arena_version="0.1.0",
        lisjong_version="0.1.0",
        lisjong_revision=_REVISION,
        riichienv_version="0.4.8",
        python_version="3.14.0",
    )


def _save(result: ComparisonResult, path: Path) -> None:
    with mock.patch(
        "lisjong_arena.artifact._collect_execution_provenance",
        return_value=_provenance(),
    ):
        save_comparison_artifact(result, path)


class RoundTripTest(unittest.TestCase):
    def test_round_trip_returns_factory_free_immutable_snapshot(self) -> None:
        result = _result()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "comparison.json"
            _save(result, path)

            artifact = load_comparison_artifact(path)

        self.assertIsInstance(artifact, ComparisonArtifact)
        self.assertEqual(artifact.schema_version, ARTIFACT_SCHEMA_VERSION)
        self.assertEqual(artifact.comparison_protocol, COMPARISON_PROTOCOL)
        self.assertIsInstance(artifact.plan, ArtifactPlan)
        self.assertFalse(hasattr(artifact.plan, "factory"))
        self.assertFalse(hasattr(artifact.plan, "policy_a"))
        self.assertEqual(artifact.plan.policy_a_identity, "a")
        self.assertEqual(artifact.plan.policy_b_identity, "b")
        self.assertEqual(artifact.plan.seeds, (30, 10))
        self.assertEqual(artifact.plan.game_mode, "4p-red-east")
        self.assertEqual(artifact.plan.max_steps, 321)
        self.assertEqual(artifact.seat_results, result.seat_results)
        self.assertEqual(artifact.metrics_a, result.metrics_a)
        self.assertEqual(artifact.metrics_b, result.metrics_b)
        self.assertEqual(artifact.provenance, _provenance())
        with self.assertRaises(dataclasses.FrozenInstanceError):
            artifact.schema_version = 2

    def test_json_does_not_contain_factory_or_machine_local_information(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "comparison.json"
            _save(_result(), path)
            serialized = path.read_text(encoding="utf-8")
            document = json.loads(serialized)

        self.assertNotIn("factory", serialized)
        self.assertNotIn("hostname", serialized)
        self.assertNotIn("username", serialized)
        self.assertNotIn("home", serialized)
        self.assertEqual(document["provenance"]["lisjong_revision"], _REVISION)


class SerializationTest(unittest.TestCase):
    def test_same_result_has_stable_utf8_json_serialization(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            first = Path(directory) / "first.json"
            second = Path(directory) / "second.json"
            result = _result()
            _save(result, first)
            _save(result, second)

            self.assertEqual(first.read_bytes(), second.read_bytes())
            serialized = first.read_text(encoding="utf-8")

        self.assertTrue(serialized.endswith("\n"))
        self.assertIn('  "comparison_protocol"', serialized)
        self.assertNotIn("NaN", serialized)
        self.assertNotIn("Infinity", serialized)

    def test_policy_metrics_reject_non_finite_values(self) -> None:
        fields = dataclasses.asdict(_result().metrics_a)
        for name, value in (
            ("average_rank", float("nan")),
            ("average_score", float("inf")),
        ):
            with self.subTest(name=name):
                changed = dict(fields)
                changed[name] = value
                with self.assertRaises(ValueError):
                    PolicyMetrics(**changed)


class FailClosedLoadTest(unittest.TestCase):
    def setUp(self) -> None:
        self._directory = tempfile.TemporaryDirectory()
        self.path = Path(self._directory.name) / "comparison.json"
        _save(_result(), self.path)
        self.document = json.loads(self.path.read_text(encoding="utf-8"))

    def tearDown(self) -> None:
        self._directory.cleanup()

    def _assert_rejected(self, document: object) -> None:
        self.path.write_text(
            json.dumps(document, ensure_ascii=False, allow_nan=False),
            encoding="utf-8",
        )
        with self.assertRaises(ComparisonArtifactError):
            load_comparison_artifact(self.path)

    def test_rejects_malformed_and_truncated_json(self) -> None:
        for serialized in ("not json", '{"schema_version": 1'):
            with self.subTest(serialized=serialized):
                self.path.write_text(serialized, encoding="utf-8")
                with self.assertRaises(ComparisonArtifactError):
                    load_comparison_artifact(self.path)

    def test_rejects_non_finite_json_number(self) -> None:
        serialized = self.path.read_text(encoding="utf-8").replace(
            '"average_rank": 2.5',
            '"average_rank": NaN',
            1,
        )
        self.path.write_text(serialized, encoding="utf-8")
        with self.assertRaises(ComparisonArtifactError):
            load_comparison_artifact(self.path)

    def test_rejects_duplicate_keys_at_top_level_and_nested_plan(self) -> None:
        valid = self.path.read_text(encoding="utf-8")
        duplicate_top_level = valid.replace(
            '"schema_version": 1,',
            '"schema_version": 999,\n  "schema_version": 1,',
            1,
        )
        duplicate_plan = valid.replace(
            '"max_steps": 321,',
            '"max_steps": 999,\n    "max_steps": 321,',
            1,
        )

        for serialized in (duplicate_top_level, duplicate_plan):
            with self.subTest(serialized=serialized):
                self.path.write_text(serialized, encoding="utf-8")
                with self.assertRaises(ComparisonArtifactError):
                    load_comparison_artifact(self.path)

    def test_rejects_unsupported_schema_and_protocol(self) -> None:
        for field, value in (
            ("schema_version", 2),
            ("comparison_protocol", "future-protocol-v2"),
        ):
            with self.subTest(field=field):
                changed = copy.deepcopy(self.document)
                changed[field] = value
                self._assert_rejected(changed)

    def test_rejects_missing_unknown_and_incorrectly_typed_fields(self) -> None:
        changed = copy.deepcopy(self.document)
        del changed["plan"]["max_steps"]
        self._assert_rejected(changed)

        changed = copy.deepcopy(self.document)
        changed["unexpected"] = True
        self._assert_rejected(changed)

        changed = copy.deepcopy(self.document)
        changed["plan"]["max_steps"] = True
        self._assert_rejected(changed)

    def test_rejects_invalid_seat_rank_and_rotation(self) -> None:
        for field, value in (("seat", 4), ("rank", 5), ("rotation", 4)):
            with self.subTest(field=field):
                changed = copy.deepcopy(self.document)
                changed["seat_results"][0][field] = value
                self._assert_rejected(changed)

    def test_rejects_invalid_or_duplicate_seed_plan(self) -> None:
        changed = copy.deepcopy(self.document)
        changed["plan"]["seeds"][0] = True
        self._assert_rejected(changed)

        changed = copy.deepcopy(self.document)
        changed["plan"]["seeds"] = [30, 30]
        self._assert_rejected(changed)

    def test_rejects_seed_game_mode_and_policy_assignment_mismatch(self) -> None:
        for field, value in (
            ("seed", 999),
            ("game_mode", "different-mode"),
            ("policy_identity", "b"),
        ):
            with self.subTest(field=field):
                changed = copy.deepcopy(self.document)
                changed["seat_results"][0][field] = value
                self._assert_rejected(changed)

    def test_rejects_seat_result_count_and_order_mismatch(self) -> None:
        changed = copy.deepcopy(self.document)
        changed["seat_results"].pop()
        self._assert_rejected(changed)

        changed = copy.deepcopy(self.document)
        changed["seat_results"][0], changed["seat_results"][1] = (
            changed["seat_results"][1],
            changed["seat_results"][0],
        )
        self._assert_rejected(changed)

    def test_rejects_non_permutation_ranks_per_game(self) -> None:
        changed = copy.deepcopy(self.document)
        changed["seat_results"][0]["rank"] = 2
        self._assert_rejected(changed)

    def test_rejects_metrics_mismatch(self) -> None:
        changed = copy.deepcopy(self.document)
        changed["metrics"]["policy_a"]["average_score"] += 1.0
        self._assert_rejected(changed)

    def test_rejects_malformed_provenance(self) -> None:
        for field, value in (
            ("execution_environment", "unknown"),
            ("lisjong_revision", "not-a-full-commit"),
            ("riichienv_version", ""),
        ):
            with self.subTest(field=field):
                changed = copy.deepcopy(self.document)
                changed["provenance"][field] = value
                self._assert_rejected(changed)


class FileHandlingTest(unittest.TestCase):
    def test_existing_destination_is_not_overwritten(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "comparison.json"
            path.write_text("existing", encoding="utf-8")

            with self.assertRaises(FileExistsError):
                _save(_result(), path)

            self.assertEqual(path.read_text(encoding="utf-8"), "existing")

    def test_invalid_result_is_rejected_before_file_creation(self) -> None:
        result = _result()
        invalid = ComparisonResult(
            plan=result.plan,
            seat_results=result.seat_results,
            metrics_a=result.metrics_b,
            metrics_b=result.metrics_a,
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "comparison.json"
            with self.assertRaises(ValueError):
                _save(invalid, path)
            self.assertFalse(path.exists())

    def test_partial_file_is_removed_after_write_failure(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "comparison.json"

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
                    _save(_result(), path)

            self.assertFalse(path.exists())

    def test_loader_rejects_a_partial_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "comparison.json"
            path.write_text('{"schema_version": 1', encoding="utf-8")
            with self.assertRaises(ComparisonArtifactError):
                load_comparison_artifact(path)


class ProvenanceTest(unittest.TestCase):
    def test_lisjong_revision_comes_from_vcs_install_metadata(self) -> None:
        distribution = mock.Mock()
        distribution.read_text.return_value = json.dumps(
            {
                "url": "https://github.com/lisbun/lisjong.git",
                "vcs_info": {
                    "commit_id": _REVISION,
                    "requested_revision": _REVISION,
                    "vcs": "git",
                },
            }
        )
        with mock.patch(
            "lisjong_arena.artifact.metadata.distribution",
            return_value=distribution,
        ):
            self.assertEqual(_lisjong_revision(), _REVISION)

    def test_unverifiable_revision_fails_closed(self) -> None:
        distribution = mock.Mock()
        distribution.read_text.return_value = None
        with mock.patch(
            "lisjong_arena.artifact.metadata.distribution",
            return_value=distribution,
        ):
            with self.assertRaises(ComparisonArtifactError):
                _lisjong_revision()


if __name__ == "__main__":
    unittest.main()
