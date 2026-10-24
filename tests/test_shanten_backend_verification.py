"""#400 / #531 shanten backend verification: wheel identity, fail-closed backend
checks, worker propagation and per-seed comparison.

Since #531 lisjong requires its native extension, and Arena CI installs a wheel
built from the pinned lisjong revision.  Identity / wiring failures are checked
with an injected stand-in module; ``python`` and unknown backends are refused
before lisjong is imported.
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
# A fast catalog Policy that calls the public shanten API (the per-game check
# requires at least one native call since #531).
_FAST_POLICY = "two-step"
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
        # #531: CI tests run against a wheel built from the same revision.
        workflow = (_ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
        self.assertIn(
            f'LISJONG_NATIVE_SOURCE_REVISION: "{backend.EXPECTED_LISJONG_REVISION}"',
            workflow,
        )
        self.assertEqual(workflow.count("LISJONG_NATIVE_SOURCE_REVISION: "), 1)
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


def _run_cli(arguments: list[str], value: str | None):
    return subprocess.run(
        [sys.executable, "-m", "lisjong_arena.shanten_backend_verification"]
        + arguments,
        capture_output=True,
        text=True,
        env=_environment(value),
        timeout=120,
    )


class BackendSelectionTest(unittest.TestCase):
    def test_new_execution_accepts_only_rust(self) -> None:
        self.assertEqual(backend.EXECUTION_BACKENDS, ("rust",))
        self.assertEqual(backend.RECORDED_BACKENDS, ("python", "rust"))
        self.assertEqual(backend.require_execution_backend("rust"), "rust")
        with self.assertRaisesRegex(
            backend.ShantenBackendVerificationError, "lisjong#346"
        ):
            backend.require_execution_backend("python")
        for name in ("cython", "Rust", "", None):
            with self.subTest(name=name):
                with self.assertRaisesRegex(
                    backend.ShantenBackendVerificationError, "must be 'rust'"
                ):
                    backend.require_execution_backend(name)

    def test_python_or_unknown_expected_backend_is_refused_first(self) -> None:
        # Refused before the installed revision or lisjong are looked at.
        with (
            mock.patch.object(backend, "_installed_lisjong_revision") as revision,
            mock.patch.dict(os.environ, _environment(None), clear=True),
        ):
            for name in ("python", "cython"):
                with self.subTest(name=name):
                    with self.assertRaises(backend.ShantenBackendVerificationError):
                        backend.require_shanten_backend(name)
        revision.assert_not_called()

    def test_environment_must_be_unset_or_rust(self) -> None:
        for value in (None, "rust"):
            with self.subTest(value=value):
                with mock.patch.dict(os.environ, _environment(value), clear=True):
                    self.assertEqual(backend.require_execution_environment(), value)
        for value, message in (
            ("python", "lisjong#346"),
            ("cython", "must be unset or 'rust'"),
            ("RUST", "must be unset or 'rust'"),
            ("", "must be unset or 'rust'"),
        ):
            with self.subTest(value=value):
                with (
                    mock.patch.dict(os.environ, _environment(value), clear=True),
                    mock.patch.object(backend, "_installed_lisjong_revision") as rev,
                ):
                    with self.assertRaisesRegex(
                        backend.ShantenBackendVerificationError, message
                    ):
                        backend.require_shanten_backend("rust")
                rev.assert_not_called()

    def test_installed_lisjong_must_be_the_expected_revision(self) -> None:
        with mock.patch.dict(os.environ, _environment(None), clear=True):
            with self.assertRaisesRegex(
                backend.ShantenBackendVerificationError, "not the pinned"
            ):
                backend.require_shanten_backend("rust", expected_revision="0" * 40)

    def test_unset_and_rust_probe_the_native_core(self) -> None:
        if importlib.util.find_spec(backend.NATIVE_MODULE) is None:
            self.skipTest("the native extension is not installed here")
        for value in (None, "rust"):
            with self.subTest(value=value):
                process = _run_cli(["probe", "--backend", "rust"], value)
                self.assertEqual(process.returncode, 0, process.stderr)
                record = json.loads(process.stdout)
                self.assertEqual(record["backend"], "rust")
                self.assertEqual(
                    record["lisjong_revision"], backend.EXPECTED_LISJONG_REVISION
                )
                native = record["native"]
                self.assertEqual(
                    native["source_revision"], backend.EXPECTED_LISJONG_REVISION
                )
                self.assertEqual(
                    native["api_version"], backend.EXPECTED_NATIVE_API_VERSION
                )
                self.assertGreaterEqual(native["probe_native_calls"], 1)

    def test_r5_probe_reaches_the_native_core(self) -> None:
        if importlib.util.find_spec(backend.NATIVE_MODULE) is None:
            self.skipTest("the native extension is not installed here")
        with mock.patch.dict(os.environ, _environment(None), clear=True):
            backend.require_shanten_backend("rust")
            self.assertGreaterEqual(backend.probe_r5_native(), 1)

    def test_python_backend_option_is_refused_with_guidance(self) -> None:
        for command in ("probe", "games"):
            with self.subTest(command=command):
                arguments = [command, "--backend", "python"]
                if command == "games":
                    arguments += ["--policy", "x", "--seeds", "0", "--workers", "1"]
                    arguments += ["--out", str(Path(tempfile.mkdtemp()) / "out")]
                process = _run_cli(arguments, None)
                self.assertEqual(process.returncode, 2)
                self.assertIn("lisjong#346", process.stderr)
                self.assertIn("recorded Arena / lisjong revision", process.stderr)
                self.assertEqual(process.stdout, "")

    def test_python_environment_is_refused_before_lisjong_is_imported(self) -> None:
        # lisjong raises ShantenBackendError at import time; Arena refuses first,
        # while its own package is imported, so the lisjong error never appears.
        for value in ("python", "cython"):
            with self.subTest(value=value):
                process = _run_cli(["probe", "--backend", "rust"], value)
                self.assertNotEqual(process.returncode, 0)
                self.assertIn("ShantenBackendVerificationError", process.stderr)
                self.assertNotIn("ShantenBackendError:", process.stderr)
                self.assertNotIn("lisjong/hand_evaluation", process.stderr)
                self.assertEqual(process.stdout, "")

    def test_startup_comparison_is_refused(self) -> None:
        out = Path(tempfile.mkdtemp()) / "startup.json"
        with (
            mock.patch("sys.stderr", new_callable=io.StringIO) as stderr,
            mock.patch("sys.stdout", new_callable=io.StringIO),
        ):
            code = main(["startup", "--repeat", "1", "--out", str(out)])
        self.assertEqual(code, 2)
        self.assertIn("lisjong#346", stderr.getvalue())
        self.assertFalse(out.exists())

    def test_missing_extension_fails_before_any_game(self) -> None:
        # lisjong fails at import time without the extension; whichever layer
        # reports it, the process stops without a fallback.
        if importlib.util.find_spec(backend.NATIVE_MODULE) is not None:
            self.skipTest("the native extension is installed here")
        process = _run_cli(["probe", "--backend", "rust"], None)
        self.assertNotEqual(process.returncode, 0)
        self.assertIn("_lisjong_native", process.stderr)
        self.assertNotIn('"backend"', process.stdout)


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
        # that the real lisjong core routes to the real extension.
        if self._counting:
            self._calls += 1
        return self._calls


class RustIdentityTest(unittest.TestCase):
    """Identity / wiring failures with a stand-in for the loaded extension."""

    def _require_rust(self, native: types.ModuleType | None) -> None:
        modules = {backend.NATIVE_MODULE: native} if native is not None else {}
        with mock.patch.dict(os.environ, _environment("rust"), clear=True):
            with mock.patch.dict(sys.modules, modules):
                if native is None:
                    sys.modules.pop(backend.NATIVE_MODULE, None)
                backend.require_shanten_backend("rust")

    def test_rust_selected_without_a_loaded_extension_fails(self) -> None:
        if importlib.util.find_spec(backend.NATIVE_MODULE) is None:
            self.skipTest("lisjong cannot be imported without the extension")
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
        for api_version in (_NO_ATTRIBUTE, 1, 2, 4):
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
        self.assertEqual(record["native"]["api_version"], 3)
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
        if importlib.util.find_spec(backend.NATIVE_MODULE) is None:
            self.skipTest("the native extension is not installed here")

    def _run(self, name: str, seeds: list[int], workers: int) -> Path:
        out = self.directory / name
        with mock.patch.dict(os.environ, _environment(None), clear=True):
            measure.run_games(
                policy=_FAST_POLICY,
                seeds=seeds,
                game_mode="4p-red-half",
                backend="rust",
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
            self.assertEqual(game["worker"]["backend"], "rust")
            self.assertEqual(
                game["worker"]["native"]["source_revision"],
                backend.EXPECTED_LISJONG_REVISION,
            )
            self.assertNotEqual(game["worker"]["pid"], summary["parent"]["pid"])
            self.assertGreaterEqual(game["native_calls"], 1)
            self.assertGreaterEqual(game["native_discard_evaluations"], 0)
        self.assertGreaterEqual(summary["native_calls"], 2)
        self.assertEqual(
            sum(entry["games"] for entry in summary["per_worker"].values()), 2
        )
        for entry in summary["per_worker"].values():
            self.assertGreaterEqual(entry["native_calls"], 1)
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

    def test_python_backend_fails_before_workers_start(self) -> None:
        out = self.directory / "refused"
        with mock.patch.dict(os.environ, _environment(None), clear=True):
            with self.assertRaisesRegex(
                backend.ShantenBackendVerificationError, "lisjong#346"
            ):
                measure.run_games(
                    policy=_FAST_POLICY,
                    seeds=[0],
                    game_mode="4p-red-half",
                    backend="python",
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
