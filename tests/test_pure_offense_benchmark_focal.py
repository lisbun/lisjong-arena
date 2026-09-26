"""Issue #389 runtime focal（canonical-first / outcome-q）のunit test。

outcome-q artifactのload（torch）はfake runtimeへ差し替え、実RiichiEnvは起動しない。
"""

import contextlib
import hashlib
import io
import pickle
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from _pure_offense_benchmark_fixtures import OTHER_ARM, allocation_ledger, game_function
from _single_round_artifact_fixtures import provenance
from lisjong.learning import CONSTANT_RESIDUAL_RUNTIME_IDENTITY
from lisjong.learning.envelope_policy import SemanticEnvelopeOffensePolicy

from lisjong_arena import seed_registry
from lisjong_arena.pure_offense_benchmark import __main__ as cli
from lisjong_arena.pure_offense_benchmark import execution, focal
from lisjong_arena.pure_offense_benchmark.artifact import load_benchmark_arm
from lisjong_arena.pure_offense_benchmark.focal import (
    RuntimeFocalError,
    RuntimePolicyFactory,
    resolve_runtime_focal,
)

RUNTIME_IDENTITY = "a" * 64
ARTIFACT_IDENTITY = "b" * 64


class _FakeOutcomeQRuntime:
    identity = RUNTIME_IDENTITY
    artifact_identity = ARTIFACT_IDENTITY

    def create_policy(self) -> str:
        return "policy"


class _RuntimeCacheMixin:
    def setUp(self) -> None:
        super().setUp()  # type: ignore[misc]
        focal._RUNTIMES.clear()
        self.addCleanup(focal._RUNTIMES.clear)  # type: ignore[attr-defined]


class CanonicalFirstFocalTest(_RuntimeCacheMixin, unittest.TestCase):
    def test_resolves_identity_reference_and_fresh_policies(self) -> None:
        spec, reference = resolve_runtime_focal("canonical-first", None)
        self.assertEqual(
            spec.identity, f"canonical-first@{CONSTANT_RESIDUAL_RUNTIME_IDENTITY}"
        )
        self.assertEqual(
            reference,
            "lisjong.learning:ConstantResidualRuntime"
            f"?runtime_identity={CONSTANT_RESIDUAL_RUNTIME_IDENTITY}",
        )
        first, second = spec.factory(), spec.factory()
        self.assertIsInstance(first, SemanticEnvelopeOffensePolicy)
        self.assertIsNot(first, second)

    def test_factory_is_process_serializable(self) -> None:
        spec, _ = resolve_runtime_focal("canonical-first", None)
        restored = pickle.loads(pickle.dumps(spec.factory))
        self.assertEqual(restored, spec.factory)
        self.assertIsInstance(restored(), SemanticEnvelopeOffensePolicy)

    def test_rejects_an_artifact(self) -> None:
        with self.assertRaises(RuntimeFocalError):
            resolve_runtime_focal("canonical-first", "somewhere")

    def test_rejects_an_unknown_kind(self) -> None:
        with self.assertRaises(RuntimeFocalError):
            resolve_runtime_focal("two-step", None)


class OutcomeQFocalTest(_RuntimeCacheMixin, unittest.TestCase):
    def setUp(self) -> None:
        super().setUp()
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.artifact = Path(self._tmp.name) / "outcome-q-artifact"
        self.artifact.mkdir()
        (self.artifact / "manifest.json").write_bytes(b"{}")
        (self.artifact / "weights.f32").write_bytes(b"\x00" * 8)

    def _patched_loader(self) -> mock.Mock:
        loader = mock.Mock(return_value=_FakeOutcomeQRuntime())
        patcher = mock.patch("lisjong.learning.load_outcome_q_policy_factory", loader)
        torch = mock.patch("lisjong.learning.model.require_torch")
        patcher.start()
        torch.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(torch.stop)
        return loader

    def test_reference_records_runtime_artifact_and_file_digests(self) -> None:
        loader = self._patched_loader()
        spec, reference = resolve_runtime_focal("outcome-q", self.artifact)
        path = str(self.artifact.resolve())
        loader.assert_called_once_with(path)
        self.assertEqual(spec.identity, f"outcome-q@{RUNTIME_IDENTITY}")
        manifest = hashlib.sha256(b"{}").hexdigest()
        weights = hashlib.sha256(b"\x00" * 8).hexdigest()
        self.assertEqual(
            reference,
            "lisjong.learning:load_outcome_q_policy_factory"
            f"?artifact_identity={ARTIFACT_IDENTITY}"
            f"&manifest_sha256={manifest}"
            f"&runtime_identity={RUNTIME_IDENTITY}"
            f"&weights_sha256={weights}",
        )
        self.assertEqual(
            spec.factory,
            RuntimePolicyFactory(
                kind="outcome-q", runtime_identity=RUNTIME_IDENTITY, artifact_path=path
            ),
        )

    def test_runtime_is_loaded_once_per_process(self) -> None:
        loader = self._patched_loader()
        spec, _ = resolve_runtime_focal("outcome-q", self.artifact)
        self.assertEqual([spec.factory() for _ in range(3)], ["policy"] * 3)
        loader.assert_called_once()

    def test_factory_fails_closed_on_runtime_identity_mismatch(self) -> None:
        self._patched_loader()
        factory = RuntimePolicyFactory(
            kind="outcome-q",
            runtime_identity="c" * 64,
            artifact_path=str(self.artifact.resolve()),
        )
        with self.assertRaises(RuntimeFocalError):
            factory()

    def test_requires_an_existing_artifact_directory(self) -> None:
        loader = self._patched_loader()
        with self.assertRaises(RuntimeFocalError):
            resolve_runtime_focal("outcome-q", None)
        with self.assertRaises(RuntimeFocalError):
            resolve_runtime_focal("outcome-q", self.artifact / "missing")
        (self.artifact / "weights.f32").unlink()
        with self.assertRaises(RuntimeFocalError):
            resolve_runtime_focal("outcome-q", self.artifact)
        loader.assert_not_called()


class RuntimeFocalCliTest(_RuntimeCacheMixin, unittest.TestCase):
    def setUp(self) -> None:
        super().setUp()
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        ledger, record = allocation_ledger()
        self.ledger_path = self.root / "ledger.json"
        seed_registry.write_ledger(self.ledger_path, ledger)
        self.allocation = record["allocation_identity"]

    def _arguments(self, out: Path, *focal_arguments: str) -> list[str]:
        return [
            "run",
            *focal_arguments,
            "--ledger",
            str(self.ledger_path),
            "--allocation-identity",
            self.allocation,
            "--workers",
            "1",
            "--out",
            str(out),
        ]

    def test_run_writes_a_canonical_first_arm(self) -> None:
        out = self.root / "canonical-first"
        with (
            mock.patch.object(
                execution,
                "_run_benchmark_game",
                game_function(lambda seed, focal: OTHER_ARM[(seed, focal)]),
            ),
            mock.patch.object(cli, "collect_execution_provenance"),
            mock.patch(
                "lisjong_arena.single_round_artifact.collect_execution_provenance",
                return_value=provenance(),
            ),
            contextlib.redirect_stdout(io.StringIO()) as stdout,
            contextlib.redirect_stderr(io.StringIO()),
        ):
            code = cli.main(self._arguments(out, "--focal", "canonical-first"))
        self.assertEqual(code, 0)
        identity = f"canonical-first@{CONSTANT_RESIDUAL_RUNTIME_IDENTITY}"
        self.assertIn(f"focal_identity={identity}", stdout.getvalue())
        arm = load_benchmark_arm(out)
        self.assertEqual(arm.focal_identity, identity)
        self.assertEqual(
            arm.focal_reference,
            "lisjong.learning:ConstantResidualRuntime"
            f"?runtime_identity={CONSTANT_RESIDUAL_RUNTIME_IDENTITY}",
        )

    def test_rejects_mismatched_focal_options_before_any_game(self) -> None:
        game = mock.Mock(side_effect=AssertionError("no game must run"))
        invalid = [
            ("--focal", "canonical-first", "--focal-identity", "x"),
            ("--focal", "outcome-q"),
            (
                "--focal",
                "lisjong.policies.shanten:ShantenPolicy",
                "--focal-identity",
                "shanten",
                "--focal-artifact",
                str(self.root),
            ),
        ]
        with mock.patch.object(execution, "_run_benchmark_game", game):
            for index, focal_arguments in enumerate(invalid):
                with self.subTest(focal_arguments=focal_arguments):
                    with self.assertRaises(ValueError):
                        cli.main(
                            self._arguments(
                                self.root / f"arm-{index}", *focal_arguments
                            )
                        )
        game.assert_not_called()


if __name__ == "__main__":
    unittest.main()
