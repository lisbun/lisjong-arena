"""#531 Rust-only gate（P2に代わる新version gate）のrun・compare・verify（合成値）。

実engine・実Policyの代わりに、spawn workerで#513のfixture半荘を返す``fake_play_gate*``を
使う。合成値であり、gateの実測結果ではない。
"""

import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import _engine_aabb_protocol_fixtures as fx

from lisjong_arena._artifact_io import canonical_json_text
from lisjong_arena.heuristic_candidate_engine_aabb import (
    backend_check,
    calibration,
    rust_only_gate,
)
from lisjong_arena.heuristic_candidate_engine_aabb.__main__ import main
from lisjong_arena.heuristic_candidate_engine_aabb.errors import EngineAabbRecordError
from lisjong_arena.heuristic_candidate_engine_aabb.executor import RunStopped
from lisjong_arena.overall_champion_aabb.lock import document_identity


def _rewrite(path: Path, document: dict, digest_field: str) -> None:
    payload = {key: value for key, value in document.items() if key != digest_field}
    document[digest_field] = document_identity(payload)
    path.write_text(canonical_json_text(document), encoding="utf-8", newline="\n")


class RustOnlyGateTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)

    def run_gate(self, label, play=fx.fake_play_gate, **overrides):
        arguments = {
            "run_label": label,
            "wheel_path": fx.WHEEL_PATH,
            "timeout_seconds": fx.TIMEOUT_SECONDS,
            "play": play,
            "environment": fx.ENVIRONMENT,
            "arena_revision": fx.ARENA_REVISION,
            "r5_probe_calls": 1,
        }
        arguments.update(overrides)
        return rust_only_gate.run_rust_gate(self.tmp / f"{label}.json", **arguments)

    def paths(self):
        return self.tmp / "first.json", self.tmp / "second.json"

    def test_new_version_identity_is_separate_from_p2_v1(self):
        self.assertEqual(
            rust_only_gate.GATE_SCHEMA,
            "arena-heuristic-candidate-engine-aabb-rust-only-gate-v1",
        )
        self.assertNotIn(
            rust_only_gate.GATE_SCHEMA,
            (backend_check.SCHEMA, backend_check.COMPARISON_SCHEMA),
        )
        # P2 v1 identities are unchanged (#528 D3)
        self.assertEqual(
            backend_check.SCHEMA,
            "arena-heuristic-candidate-engine-aabb-backend-check-v1",
        )
        self.assertEqual(
            backend_check.COMPARISON_SCHEMA,
            "arena-heuristic-candidate-engine-aabb-backend-comparison-v1",
        )
        self.assertEqual(rust_only_gate.GATE_SEEDS, backend_check.BACKEND_CHECK_SEEDS)

    def test_two_identical_runs_pass_and_re_derive(self):
        first = self.run_gate("first")
        self.run_gate("second")
        self.assertEqual(len(first["hanchans"]), 12)
        self.assertEqual(
            first["participants"]["A"]["policy_identity"],
            calibration.CALIBRATION_PAIR[0],
        )
        self.assertEqual(
            first["hanchans"][0]["native_counters"], dict(fx.GATE_COUNTERS)
        )
        gate = rust_only_gate.compare_rust_gate_runs(*self.paths())
        self.assertEqual(gate["status"], rust_only_gate.PASS)
        self.assertEqual(gate["mismatches"], [])
        self.assertEqual(gate["hanchan_count"], 12)
        self.assertEqual(gate["lisjong_revision"], fx.LISJONG_REVISION)
        self.assertEqual(gate["native"]["wheel_sha256"], "d" * 64)
        self.assertEqual(
            gate["native_counter_totals"]["numeric_shanten"],
            12 * fx.GATE_COUNTERS["numeric_shanten"],
        )
        self.assertEqual(gate["r5_probe_calls"], [1, 1])
        path = self.tmp / "gate.json"
        path.write_text(canonical_json_text(gate), encoding="utf-8", newline="\n")
        self.assertEqual(rust_only_gate.verify_rust_gate(*self.paths(), path), gate)

    def test_counter_or_action_drift_fails_the_gate(self):
        for play, field in (
            (fx.fake_play_gate_counter_drift, "native_counters"),
            (fx.fake_play_gate_action_drift, "selected_actions"),
        ):
            with self.subTest(field=field):
                for path in self.paths():
                    path.unlink(missing_ok=True)
                self.run_gate("first")
                self.run_gate("second", play=play)
                gate = rust_only_gate.compare_rust_gate_runs(*self.paths())
                self.assertEqual(gate["status"], rust_only_gate.FAIL)
                self.assertEqual([m["field"] for m in gate["mismatches"]], [field])

    def test_runs_without_native_counter_evidence_stop(self):
        for play, message in (
            (fx.fake_play_gate_no_native_call, "numeric shanten"),
            (fx.fake_play_gate_missing_counters, "malformed"),
            (fx.fake_play_rust_missing_actions, "selected actions"),
        ):
            with self.subTest(message=message):
                with self.assertRaisesRegex(RunStopped, message):
                    self.run_gate("first", play=play)
                self.assertFalse((self.tmp / "first.json").exists())
        with self.assertRaisesRegex(EngineAabbRecordError, "R5"):
            self.run_gate("first", r5_probe_calls=0)

    def test_python_or_inconsistent_environment_is_refused(self):
        native = dict(fx.ENVIRONMENT["native"])
        cases = (
            (fx.PYTHON_ENVIRONMENT, "native provenance"),
            (
                {**fx.ENVIRONMENT, "native": {**native, "source_revision": "e" * 40}},
                "SOURCE_REVISION",
            ),
            (
                {
                    **fx.ENVIRONMENT,
                    "shanten_backend": {
                        "backend": "python",
                        "lisjong_revision": fx.LISJONG_REVISION,
                    },
                },
                "not rust",
            ),
        )
        for environment, message in cases:
            with self.subTest(message=message):
                with self.assertRaisesRegex(EngineAabbRecordError, message):
                    self.run_gate("first", environment=environment)
        with self.assertRaisesRegex(EngineAabbRecordError, "run label"):
            self.run_gate("third")

    def test_saved_gate_is_read_without_executing(self):
        self.run_gate("first")
        self.run_gate("second")
        gate = rust_only_gate.compare_rust_gate_runs(*self.paths())
        path = self.tmp / "gate.json"
        path.write_text(canonical_json_text(gate), encoding="utf-8", newline="\n")
        with (
            mock.patch.object(
                rust_only_gate, "run_schedule", side_effect=AssertionError
            ),
            mock.patch.object(
                rust_only_gate, "environment_document", side_effect=AssertionError
            ),
            mock.patch(
                "lisjong_arena.shanten_backend_verification.backend.require_shanten_backend",
                side_effect=AssertionError,
            ),
        ):
            self.assertEqual(rust_only_gate.verify_rust_gate(*self.paths(), path), gate)

    def test_tampered_documents_are_refused(self):
        self.run_gate("first")
        self.run_gate("second")
        first, second = self.paths()
        gate = rust_only_gate.compare_rust_gate_runs(first, second)
        gate_path = self.tmp / "gate.json"

        # a gate that claims PASS for different runs does not re-derive
        forged = dict(gate)
        forged["kyoku_count"] += 1
        _rewrite(gate_path, forged, "gate_digest")
        with self.assertRaisesRegex(EngineAabbRecordError, "re-derive"):
            rust_only_gate.verify_rust_gate(first, second, gate_path)

        # unknown gate schema with a consistent digest
        unknown = dict(gate, schema=rust_only_gate.GATE_SCHEMA[:-2] + "v2")
        _rewrite(gate_path, unknown, "gate_digest")
        with self.assertRaisesRegex(EngineAabbRecordError, "unsupported rust gate"):
            rust_only_gate.verify_rust_gate(first, second, gate_path)

        # a run row edited without its digests
        document = json.loads(first.read_text("utf-8"))
        document["hanchans"][0]["final_points"][0] += 1
        first.write_text(canonical_json_text(document), "utf-8", newline="\n")
        with self.assertRaisesRegex(EngineAabbRecordError, "digest"):
            rust_only_gate.load_rust_gate_run(first)

        # the second run passed as the first one
        with self.assertRaisesRegex(EngineAabbRecordError, "first run"):
            rust_only_gate.compare_rust_gate_runs(second, second)

    def test_runs_from_different_environments_cannot_be_compared(self):
        # The executor already stops a worker whose environment differs from
        # the parent; this is the saved-document side of the same check.
        document = self.run_gate("first")
        first, second = self.paths()
        environment = {
            **document["environment"],
            "python": {"implementation": "CPython", "version": "3.14.9"},
        }
        forged = dict(
            document,
            environment=environment,
            environment_digest=document_identity(environment),
            run_label="second",
        )
        _rewrite(second, forged, "document_digest")
        rust_only_gate.load_rust_gate_run(second)
        with self.assertRaisesRegex(EngineAabbRecordError, "environment_digest"):
            rust_only_gate.compare_rust_gate_runs(*self.paths())

    def test_cli_exit_codes(self):
        self.run_gate("first")
        self.run_gate("second", play=fx.fake_play_gate_counter_drift)
        first, second = self.paths()
        output = self.tmp / "gate.json"
        with mock.patch("sys.stdout"):
            code = main(
                [
                    "rust-gate-compare",
                    "--first",
                    str(first),
                    "--second",
                    str(second),
                    "--output",
                    str(output),
                ]
            )
            self.assertEqual(code, 3)
            verified = main(
                [
                    "rust-gate-verify",
                    "--first",
                    str(first),
                    "--second",
                    str(second),
                    "--gate",
                    str(output),
                ]
            )
        self.assertEqual(verified, 3)
        self.assertEqual(json.loads(output.read_text("utf-8"))["status"], "FAIL")

    def test_p2_python_half_is_refused_at_the_cli(self):
        with (
            mock.patch("sys.stderr") as stderr,
            self.assertRaises(SystemExit) as raised,
        ):
            main(
                [
                    "backend-check",
                    "--backend",
                    "python",
                    "--output",
                    str(self.tmp / "python.json"),
                    "--timeout-seconds",
                    "60",
                ]
            )
        self.assertEqual(raised.exception.code, 2)
        written = "".join(call.args[0] for call in stderr.write.call_args_list)
        self.assertIn("lisjong#346", written)
        self.assertFalse((self.tmp / "python.json").exists())


if __name__ == "__main__":
    unittest.main()
