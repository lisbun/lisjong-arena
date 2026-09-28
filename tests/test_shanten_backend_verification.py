"""#400 shanten backend verification: wheel identity, fail-closed backend checks,
worker propagation and per-seed comparison.

The native extension is not installed in Arena CI; the rust-backend branches are
checked with an injected stand-in module (identity / wiring failures) and the
real extension is exercised on the target environment by the #400 bootstrap.
"""

import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import types
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from lisjong_arena.decision_capture import seat_digests, semantic_digest
from lisjong_arena.shanten_backend_verification import backend, measure
from lisjong_arena.shanten_backend_verification.__main__ import main

_ROOT = Path(__file__).resolve().parents[1]
_FAST_POLICY = (
    "lisjong_arena.learned_policy_offline_q.p1_gate_b_comparator:PassiveTsumogiriPolicy"
)
_ENV = backend.BACKEND_ENVIRONMENT_VARIABLE


def _environment(value: str | None) -> dict[str, str]:
    environment = dict(os.environ)
    environment.pop(_ENV, None)
    if value is not None:
        environment[_ENV] = value
    return environment


class WheelIdentityTest(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = Path(tempfile.mkdtemp())

    def _wheel(self, name: str, payload: bytes) -> Path:
        path = self.directory / name
        path.write_bytes(payload)
        return path

    def test_frozen_identity_is_the_current_lisjong_pin(self) -> None:
        project = (_ROOT / "pyproject.toml").read_text(encoding="utf-8")
        self.assertIn(f"lisjong.git@{backend.EXPECTED_LISJONG_REVISION}", project)
        self.assertRegex(backend.EXPECTED_WHEEL_SHA256, r"^[0-9a-f]{64}$")
        self.assertTrue(
            backend.EXPECTED_WHEEL_FILENAME.endswith(
                "-cp314-cp314-manylinux_2_28_x86_64.whl"
            )
        )

    def test_missing_wheel_fails_closed(self) -> None:
        with self.assertRaisesRegex(backend.ShantenBackendVerificationError, "missing"):
            backend.verify_wheel_file(self.directory / backend.EXPECTED_WHEEL_FILENAME)

    def test_unexpected_file_name_fails_closed(self) -> None:
        wheel = self._wheel(
            "lisjong_native-0.1.0-cp314-cp314-win_amd64.whl", b"payload"
        )
        with self.assertRaisesRegex(
            backend.ShantenBackendVerificationError, "file name"
        ):
            backend.verify_wheel_file(wheel)

    def test_digest_mismatch_fails_closed(self) -> None:
        wheel = self._wheel(backend.EXPECTED_WHEEL_FILENAME, b"not the wheel")
        with self.assertRaisesRegex(backend.ShantenBackendVerificationError, "SHA-256"):
            backend.verify_wheel_file(wheel)

    def test_matching_wheel_is_accepted(self) -> None:
        wheel = self._wheel(backend.EXPECTED_WHEEL_FILENAME, b"frozen")
        digest = backend.file_sha256(wheel)
        with mock.patch.object(backend, "EXPECTED_WHEEL_SHA256", digest):
            record = backend.verify_wheel_file(wheel)
        self.assertEqual(record["sha256"], digest)
        self.assertEqual(record["file"], backend.EXPECTED_WHEEL_FILENAME)


class BackendSelectionTest(unittest.TestCase):
    def test_unset_backend_is_refused(self) -> None:
        with mock.patch.dict(os.environ, _environment(None), clear=True):
            with self.assertRaisesRegex(
                backend.ShantenBackendVerificationError, "explicit"
            ):
                backend.require_shanten_backend("python")

    def test_backend_other_than_the_requested_one_is_refused(self) -> None:
        with mock.patch.dict(os.environ, _environment("python"), clear=True):
            with self.assertRaisesRegex(
                backend.ShantenBackendVerificationError, "explicit 'rust'"
            ):
                backend.require_shanten_backend("rust")

    def test_unknown_expected_backend_is_refused(self) -> None:
        with self.assertRaises(backend.ShantenBackendVerificationError):
            backend.require_shanten_backend("cython")

    def test_installed_lisjong_must_be_the_expected_revision(self) -> None:
        with mock.patch.dict(os.environ, _environment("python"), clear=True):
            with self.assertRaisesRegex(
                backend.ShantenBackendVerificationError, "not the pinned"
            ):
                backend.require_shanten_backend("python", expected_revision="0" * 40)

    def test_python_backend_record_does_not_load_the_extension(self) -> None:
        process = subprocess.run(
            [sys.executable, "-m", "lisjong_arena.shanten_backend_verification"]
            + ["probe", "--backend", "python"],
            capture_output=True,
            text=True,
            env=_environment("python"),
            timeout=120,
        )
        self.assertEqual(process.returncode, 0, process.stderr)
        record = json.loads(process.stdout)
        self.assertEqual(record["backend"], "python")
        self.assertEqual(record["lisjong_revision"], backend.EXPECTED_LISJONG_REVISION)
        self.assertIsNone(record["native"])

    def test_rust_without_the_extension_fails_before_any_game(self) -> None:
        # Checking availability must not import Rust into this Python process:
        # later backend checks deliberately reject an already loaded extension.
        if importlib.util.find_spec(backend.NATIVE_MODULE) is not None:
            self.skipTest("the native extension is installed here")
        process = subprocess.run(
            [sys.executable, "-m", "lisjong_arena.shanten_backend_verification"]
            + ["probe", "--backend", "rust"],
            capture_output=True,
            text=True,
            env=_environment("rust"),
            timeout=120,
        )
        # lisjong itself fails closed while the Arena package imports it, before
        # the probe runs; either way the process stops without a fallback.
        self.assertNotEqual(process.returncode, 0)
        self.assertIn("refusing to fall back", process.stderr)
        self.assertNotIn('"backend"', process.stdout)


class StartupProbeTest(unittest.TestCase):
    def test_child_times_the_lisjong_import_itself(self) -> None:
        # Importing lisjong_arena in the child would import lisjong before the
        # timer starts and hide the import cost being measured.
        self.assertNotIn("lisjong_arena", measure._STARTUP_CHILD)
        with mock.patch.dict(os.environ, _environment(None), clear=True):
            sample = measure._startup_once("python")
        self.assertEqual(sample["value"], 0)
        self.assertGreater(sample["import_first_call_ms"], 0.0)


_NO_ATTRIBUTE = object()


class _FakeNative(types.ModuleType):
    def __init__(
        self,
        revision: object,
        *,
        api_version: object = backend.EXPECTED_NATIVE_API_VERSION,
        counting: bool = False,
    ) -> None:
        super().__init__(backend.NATIVE_MODULE)
        self.SOURCE_REVISION = revision
        if api_version is not _NO_ATTRIBUTE:
            self.API_VERSION = api_version
        self.__file__ = "fake"
        self._calls = 7
        self._counting = counting

    def standard_shanten_call_count(self) -> int:
        # With ``counting`` every read moves, standing in for the probe call
        # that the python-imported lisjong core does not route here.
        if self._counting:
            self._calls += 1
        return self._calls


class RustIdentityTest(unittest.TestCase):
    """Rust-branch failures, with lisjong already imported under python."""

    def _require_rust(self, native: types.ModuleType | None) -> None:
        modules = {backend.NATIVE_MODULE: native} if native is not None else {}
        with mock.patch.dict(os.environ, _environment("rust"), clear=True):
            with mock.patch.dict(sys.modules, modules):
                if native is None:
                    sys.modules.pop(backend.NATIVE_MODULE, None)
                backend.require_shanten_backend("rust")

    def test_rust_selected_without_a_loaded_extension_fails(self) -> None:
        with self.assertRaisesRegex(
            backend.ShantenBackendVerificationError, "did not load"
        ):
            self._require_rust(None)

    def test_extension_built_from_another_revision_fails(self) -> None:
        for revision in ("unknown", "1" * 40, None):
            with self.subTest(revision=revision):
                with self.assertRaisesRegex(
                    backend.ShantenBackendVerificationError, "SOURCE_REVISION"
                ):
                    self._require_rust(_FakeNative(revision))

    def test_extension_that_is_not_called_fails(self) -> None:
        with self.assertRaisesRegex(
            backend.ShantenBackendVerificationError, "did not reach"
        ):
            self._require_rust(_FakeNative(backend.EXPECTED_LISJONG_REVISION))

    def test_extension_with_another_native_api_fails(self) -> None:
        # A pre-lisjong#224 wheel has no API_VERSION attribute (read as 1).
        for api_version in (_NO_ATTRIBUTE, 1, 3):
            with self.subTest(api_version=api_version):
                with self.assertRaisesRegex(
                    backend.ShantenBackendVerificationError, "API_VERSION"
                ):
                    self._require_rust(
                        _FakeNative(
                            backend.EXPECTED_LISJONG_REVISION,
                            api_version=api_version,
                            counting=True,
                        )
                    )

    def test_matching_extension_records_its_identity(self) -> None:
        native = _FakeNative(backend.EXPECTED_LISJONG_REVISION, counting=True)
        with mock.patch.dict(os.environ, _environment("rust"), clear=True):
            with mock.patch.dict(sys.modules, {backend.NATIVE_MODULE: native}):
                record = backend.require_shanten_backend("rust")
        self.assertEqual(
            record["native"]["source_revision"], backend.EXPECTED_LISJONG_REVISION
        )
        self.assertEqual(record["native"]["api_version"], 2)
        self.assertEqual(record["native"]["probe_native_calls"], 1)


class InstalledNativeTest(unittest.TestCase):
    """The loaded extension files must be the frozen wheel's files (#409)."""

    _FILES = {
        "_lisjong_native/__init__.py": b"from ._lisjong_native import *\n",
        "_lisjong_native/_lisjong_native.cpython-314-x86_64-linux-gnu.so": b"so",
    }

    def setUp(self) -> None:
        self.directory = Path(tempfile.mkdtemp())
        self.wheel = self.directory / backend.EXPECTED_WHEEL_FILENAME
        with zipfile.ZipFile(self.wheel, "w") as archive:
            for name, payload in self._FILES.items():
                archive.writestr(name, payload)
            archive.writestr("lisjong_native-0.1.0.dist-info/RECORD", b"")
        self.site = self.directory / "site" / backend.NATIVE_MODULE
        self.site.mkdir(parents=True)
        for name, payload in self._FILES.items():
            (self.site / name.partition("/")[2]).write_bytes(payload)
        native = types.ModuleType(backend.NATIVE_MODULE)
        native.__file__ = str(self.site / "__init__.py")
        digest = backend.file_sha256(self.wheel)
        for patcher in (
            mock.patch.object(backend, "EXPECTED_WHEEL_SHA256", digest),
            mock.patch.dict(sys.modules, {backend.NATIVE_MODULE: native}),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_installed_files_of_the_wheel_are_accepted(self) -> None:
        record = backend.verify_installed_native(self.wheel)
        self.assertEqual(record["files"], sorted(self._FILES))
        self.assertEqual(record["installed_directory"], str(self.site))

    def test_files_from_another_wheel_are_refused(self) -> None:
        # Same file name and package version, different build (#400 vs #409).
        (self.site / "_lisjong_native.cpython-314-x86_64-linux-gnu.so").write_bytes(
            b"old build"
        )
        with self.assertRaisesRegex(
            backend.ShantenBackendVerificationError, "force-reinstall"
        ):
            backend.verify_installed_native(self.wheel)

    def test_missing_installed_file_is_refused(self) -> None:
        (self.site / "__init__.py").unlink()
        with self.assertRaisesRegex(
            backend.ShantenBackendVerificationError, "not installed"
        ):
            backend.verify_installed_native(self.wheel)

    def test_unloaded_extension_is_refused(self) -> None:
        sys.modules.pop(backend.NATIVE_MODULE)
        with self.assertRaisesRegex(
            backend.ShantenBackendVerificationError, "not imported"
        ):
            backend.verify_installed_native(self.wheel)

    def test_unverified_wheel_is_refused_first(self) -> None:
        with mock.patch.object(backend, "EXPECTED_WHEEL_SHA256", "0" * 64):
            with self.assertRaisesRegex(
                backend.ShantenBackendVerificationError, "SHA-256"
            ):
                backend.verify_installed_native(self.wheel)

    def test_probe_wheel_option_requires_the_rust_backend(self) -> None:
        sys.modules.pop(backend.NATIVE_MODULE)
        with (
            mock.patch.dict(os.environ, _environment("python"), clear=True),
            mock.patch("sys.stderr", new_callable=io.StringIO) as stderr,
            mock.patch("sys.stdout", new_callable=io.StringIO),
        ):
            code = main(["probe", "--backend", "python", "--wheel", str(self.wheel)])
        self.assertEqual(code, 2)
        self.assertIn("--wheel is only valid", stderr.getvalue())


class DecisionDigestTest(unittest.TestCase):
    def test_digest_ignores_the_order_between_seats(self) -> None:
        def decision(seat: int, step: int):
            return types.SimpleNamespace(
                input=types.SimpleNamespace(self_seat=seat), step=step
            )

        a0, a1, b0 = decision(0, 1), decision(0, 2), decision(1, 1)
        first = [(a0, "x"), (b0, "y"), (a1, "z")]
        second = [(b0, "y"), (a0, "x"), (a1, "z")]
        self.assertEqual(semantic_digest(first), semantic_digest(second))
        self.assertEqual(seat_digests(first), seat_digests(second))
        self.assertEqual(seat_digests(first)[0]["decisions"], 2)
        swapped = [(a1, "z"), (b0, "y"), (a0, "x")]
        self.assertNotEqual(semantic_digest(first), semantic_digest(swapped))


class GamesAndCompareTest(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = Path(tempfile.mkdtemp())

    def _run(self, name: str, seeds: list[int], workers: int) -> Path:
        out = self.directory / name
        with mock.patch.dict(os.environ, _environment("python"), clear=True):
            measure.run_games(
                policy=_FAST_POLICY,
                seeds=seeds,
                game_mode="4p-red-half",
                backend="python",
                workers=workers,
                out_dir=out,
            )
        return out

    def test_every_worker_verifies_the_backend_and_runs_are_comparable(self) -> None:
        serial = self._run("serial", [0, 1], workers=1)
        parallel = self._run("parallel", [1, 0], workers=2)
        summary = json.loads((parallel / measure.SUMMARY_FILENAME).read_text("utf-8"))
        self.assertEqual(summary["games"], 2)
        self.assertEqual(summary["seeds"], [0, 1])
        self.assertEqual(summary["workers_requested"], 2)
        games = [
            json.loads(line)
            for line in (parallel / measure.GAMES_FILENAME)
            .read_text("utf-8")
            .splitlines()
        ]
        for game in games:
            self.assertEqual(game["worker"]["backend"], "python")
            self.assertIsNone(game["worker"]["native"])
            self.assertNotEqual(game["worker"]["pid"], summary["parent"]["pid"])
            self.assertIsNone(game["native_calls"])
            self.assertIsNone(game["native_discard_evaluations"])
        self.assertIsNone(summary["native_discard_evaluations"])
        self.assertEqual(
            sum(entry["games"] for entry in summary["per_worker"].values()), 2
        )
        for entry in summary["per_worker"].values():
            self.assertIsNone(entry["native_calls"])
            self.assertIsNone(entry["native_discard_evaluations"])
        self.assertTrue(measure.compare_games(serial, parallel)["ok"])

        expected = {0: games[0]["semantic_sha256"]}
        self.assertTrue(
            measure.compare_games(serial, parallel, expected_semantic=expected)["ok"]
        )
        wrong = measure.compare_games(serial, parallel, expected_semantic={0: "0" * 64})
        self.assertFalse(wrong["ok"])

        tampered = self.directory / "tampered"
        tampered.mkdir()
        lines = (parallel / measure.GAMES_FILENAME).read_text("utf-8").splitlines()
        game = json.loads(lines[0])
        game["ranks"] = list(reversed(game["ranks"]))
        lines[0] = json.dumps(game)
        (tampered / measure.GAMES_FILENAME).write_text(
            "\n".join(lines[:1]) + "\n", encoding="utf-8"
        )
        result = measure.compare_games(serial, tampered)
        self.assertFalse(result["ok"])
        kinds = {mismatch["kind"] for mismatch in result["mismatches"]}
        self.assertEqual(kinds, {"seed-set", "ranks"})

    def test_backend_mismatch_fails_before_workers_start(self) -> None:
        out = self.directory / "refused"
        with mock.patch.dict(os.environ, _environment("python"), clear=True):
            with self.assertRaises(backend.ShantenBackendVerificationError):
                measure.run_games(
                    policy=_FAST_POLICY,
                    seeds=[0],
                    game_mode="4p-red-half",
                    backend="rust",
                    workers=1,
                    out_dir=out,
                )
        self.assertFalse(out.exists())

    def test_compare_cli_exit_code_reports_mismatch(self) -> None:
        left = self._run("left", [0], workers=1)
        self.assertEqual(main(["compare", str(left), str(left)]), 0)
        self.assertEqual(
            main(
                [
                    "compare",
                    str(left),
                    str(left),
                    "--expected-semantic",
                    f"0:{'0' * 64}",
                ]
            ),
            1,
        )


if __name__ == "__main__":
    unittest.main()
