"""#245 formal run contract tests (no AWS call, no hanchan execution)."""

import hashlib
import importlib.util
import io
import json
import lzma
import os
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import textwrap
import time
import types
import unittest
from argparse import Namespace
from pathlib import Path
from unittest import mock

from lisjong_arena.shanten_backend_verification import backend

_ROOT = Path(__file__).resolve().parents[1]
_AWS = _ROOT / "scripts" / "aws"
_BOOTSTRAP = _AWS / "bootstrap-riichi-wait-formal-245.sh"
_DRIVER = _AWS / "run_riichi_wait_formal_245.py"
_DOC = _ROOT / "docs" / "riichi-wait-formal-245.md"

sys.path.insert(0, str(_ROOT / "scripts"))
import generate_riichi_wait_formal_source_245 as generator  # noqa: E402


def _driver():
    spec = importlib.util.spec_from_file_location("run_riichi_wait_formal_245", _DRIVER)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _shell_value(text: str, name: str) -> str:
    match = re.search(rf'^{name}="?([^"\n]+)"?$', text, re.MULTILINE)
    assert match is not None, name
    return match.group(1)


class ProtocolConstantsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.driver = _driver()

    def test_driver_and_generator_agree_on_the_formal_population(self) -> None:
        self.assertEqual(
            tuple(range(self.driver.FORMAL_FIRST, self.driver.FORMAL_LAST + 1)),
            generator.FORMAL_SEEDS,
        )
        self.assertEqual(self.driver.FORMAL_PROTOCOL, generator.PROTOCOL)
        self.assertEqual(self.driver.GENERATOR.name, Path(generator.__file__).name)

    def test_s1_splits_are_the_s1_population_and_excluded_from_the_test(self) -> None:
        seeds = {s for part in self.driver.S1_SPLITS.values() for s in part}
        self.assertEqual(seeds, set(generator.S1_SEEDS))
        self.assertEqual(
            {name: len(part) for name, part in self.driver.S1_SPLITS.items()},
            {"train": 160, "valid": 20, "test": 20},
        )
        self.assertFalse(seeds & set(generator.FORMAL_SEEDS))

    def test_documented_s1_bundle_pin(self) -> None:
        self.assertRegex(self.driver.S1_BUNDLE_SHA256, r"^[0-9a-f]{64}$")
        doc = _DOC.read_text(encoding="utf-8")
        self.assertIn(self.driver.S1_BUNDLE_SHA256, doc)
        self.assertIn(f"{self.driver.S1_BUNDLE_BYTES:,}", doc)


class UnpackS1Test(unittest.TestCase):
    """The S1 bundle is checked against pinned values (replaced by synthetic ones)."""

    def setUp(self) -> None:
        self.driver = _driver()
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.decisions = b'{"a":1}\n{"a":2}\n'
        self.facts = b'{"b":1}\n{"b":2}\n'

    def manifest(self, **overrides) -> bytes:
        document = {
            "splits": self.driver.S1_SPLITS,
            "files": {
                "decisions": {
                    "bytes": len(self.decisions),
                    "sha256": hashlib.sha256(self.decisions).hexdigest(),
                    "rows": 2,
                },
                "label_facts": {
                    "bytes": len(self.facts),
                    "sha256": hashlib.sha256(self.facts).hexdigest(),
                    "rows": 2,
                },
            },
        }
        document.update(overrides)
        return (json.dumps(document, sort_keys=True) + "\n").encode()

    def bundle(self, *, compressed=True, manifest=None, drop=None, **kw) -> Path:
        manifest = self.manifest(**kw) if manifest is None else manifest
        members = {
            "manifest.json": manifest,
            "decisions.jsonl": lzma.compress(self.decisions)
            if compressed
            else self.decisions,
            "label_facts.jsonl": lzma.compress(self.facts)
            if compressed
            else self.facts,
        }
        suffix = ".xz" if compressed else ""
        path = self.root / "bundle.tar.xz"
        with tarfile.open(path, "w:xz") as archive:
            for name, data in members.items():
                if name == drop:
                    continue
                full = name + (suffix if name != "manifest.json" else "")
                info = tarfile.TarInfo(f"s1-riichi-deal-in-200-v1/source/{full}")
                info.size = len(data)
                archive.addfile(info, io.BytesIO(data))
            info = tarfile.TarInfo("s1-riichi-deal-in-200-v1/README.md")
            info.size = 1
            archive.addfile(info, io.BytesIO(b"x"))
        return path

    def pin(self, path: Path, manifest_sha: str | None = None) -> None:
        manifest = hashlib.sha256(self.manifest()).hexdigest()
        self.driver.S1_BUNDLE_SHA256 = hashlib.sha256(path.read_bytes()).hexdigest()
        self.driver.S1_BUNDLE_BYTES = path.stat().st_size
        self.driver.S1_MANIFEST_SHA256_PREFIX = (manifest_sha or manifest)[:8]
        self.driver.S1_MANIFEST_SHA256_SUFFIX = (manifest_sha or manifest)[-4:]

    def test_compressed_and_plain_members_are_written_decompressed(self) -> None:
        for compressed in (True, False):
            with self.subTest(compressed=compressed):
                path = self.bundle(compressed=compressed)
                self.pin(path)
                target = self.root / f"out-{compressed}"
                digests = self.driver.unpack_s1(path, target)
                self.assertEqual(
                    (target / "decisions.jsonl").read_bytes(), self.decisions
                )
                self.assertEqual(
                    (target / "label_facts.jsonl").read_bytes(), self.facts
                )
                self.assertEqual(
                    set(digests),
                    {"manifest.json", "decisions.jsonl", "label_facts.jsonl"},
                )

    def test_bundle_must_match_the_pinned_digest(self) -> None:
        path = self.bundle()
        self.pin(path)
        self.driver.S1_BUNDLE_SHA256 = "0" * 64
        with self.assertRaisesRegex(self.driver.RunError, "SHA-256"):
            self.driver.unpack_s1(path, self.root / "out")

    def test_bundle_must_match_the_pinned_size(self) -> None:
        path = self.bundle()
        self.pin(path)
        self.driver.S1_BUNDLE_BYTES += 1
        with self.assertRaisesRegex(self.driver.RunError, "size"):
            self.driver.unpack_s1(path, self.root / "out")

    def test_missing_source_file_is_rejected(self) -> None:
        path = self.bundle(drop="label_facts.jsonl")
        self.pin(path)
        with self.assertRaisesRegex(self.driver.RunError, "label_facts"):
            self.driver.unpack_s1(path, self.root / "out")

    def test_manifest_digest_is_checked(self) -> None:
        path = self.bundle()
        self.pin(path, manifest_sha="f" * 64)
        with self.assertRaisesRegex(self.driver.RunError, "manifest SHA-256"):
            self.driver.unpack_s1(path, self.root / "out")

    def test_file_digests_are_checked_against_the_manifest(self) -> None:
        bad = self.manifest()
        bad = bad.replace(
            hashlib.sha256(self.decisions).hexdigest().encode(), b"0" * 64
        )
        path = self.bundle(manifest=bad)
        self.driver.S1_BUNDLE_SHA256 = hashlib.sha256(path.read_bytes()).hexdigest()
        self.driver.S1_BUNDLE_BYTES = path.stat().st_size
        digest = hashlib.sha256(bad).hexdigest()
        self.driver.S1_MANIFEST_SHA256_PREFIX = digest[:8]
        self.driver.S1_MANIFEST_SHA256_SUFFIX = digest[-4:]
        with self.assertRaisesRegex(self.driver.RunError, "decisions.jsonl"):
            self.driver.unpack_s1(path, self.root / "out")

    def test_splits_must_be_the_s1_splits(self) -> None:
        manifest = self.manifest(splits={"train": [1], "valid": [2], "test": [3]})
        path = self.bundle(manifest=manifest)
        self.pin(path, manifest_sha=hashlib.sha256(manifest).hexdigest())
        with self.assertRaisesRegex(self.driver.RunError, "splits"):
            self.driver.unpack_s1(path, self.root / "out")


class RunStepsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.driver = _driver()
        self.driver.POLL_SECONDS = 0.05
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.logs = Path(temp.name)

    def python(self, code):
        return [sys.executable, "-c", code]

    def test_successful_steps_are_measured(self) -> None:
        rows = self.driver.run_steps(
            [("a", self.python("print('ok')"), None), ("b", self.python(""), None)],
            self.logs,
        )
        self.assertEqual(sorted(row["step"] for row in rows), ["a", "b"])
        self.assertTrue(all(row["exit_status"] == 0 for row in rows))
        self.assertTrue(all(row["maxrss_kb"] > 0 for row in rows))
        self.assertIn("ok", next(r for r in rows if r["step"] == "a")["stdout_tail"])

    def test_first_failure_kills_the_remaining_process_groups(self) -> None:
        marker = self.logs / "grandchild.pid"
        slow = self.python(
            "import subprocess, sys, time\n"
            "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
            f"open({str(marker)!r}, 'w').write(str(child.pid))\n"
            "time.sleep(60)\n"
        )
        fail = self.python("import time, sys; time.sleep(0.5); sys.exit(3)")
        started = time.perf_counter()
        with self.assertRaisesRegex(self.driver.RunError, "step fail failed") as raised:
            self.driver.run_steps(
                [("slow", slow, None), ("fail", fail, None)], self.logs
            )
        self.assertLess(time.perf_counter() - started, 20)
        self.assertEqual({r["step"] for r in raised.exception.rows}, {"slow", "fail"})
        grandchild = int(marker.read_text())
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            try:
                os.kill(grandchild, 0)
            except ProcessLookupError:
                break
            time.sleep(0.05)
        else:
            self.fail("grandchild of the killed step is still running")

    def test_the_compute_deadline_kills_every_step(self) -> None:
        sleeper = self.python("import time; time.sleep(60)")
        with mock.patch.object(
            self.driver, "uptime_seconds", side_effect=[1.0, 2.0] + [100.0] * 50
        ):
            started = time.perf_counter()
            with self.assertRaisesRegex(self.driver.RunError, "deadline"):
                self.driver.run_steps(
                    [("sleep", sleeper, None)], self.logs, deadline_uptime=50.0
                )
        self.assertLess(time.perf_counter() - started, 20)


FAKE_GENERATOR = textwrap.dedent(
    """\
    import json, sys
    from pathlib import Path

    args = sys.argv[1:]
    mode = args[0]
    out = Path(args[args.index("--output") + 1])
    if "FAIL_GENERATE" in __import__("os").environ:
        raise SystemExit(7)
    (out / "source").mkdir(parents=True)
    (out / "source" / "manifest.json").write_text("{}")
    (out / "source" / "decisions.jsonl").write_text('{"k":1}\\n' * 50)
    (out / "source" / "label_facts.jsonl").write_text('{"k":2}\\n' * 50)
    (out / "generation.json").write_text(json.dumps({"mode": mode}))
    """
)

FAKE_CONSUMER = textwrap.dedent(
    """\
    import json, os, sys

    args = sys.argv[1:]
    log = os.environ["FAKE_CALLS"]
    with open(log, "a") as handle:
        handle.write(" ".join(a for a in args if not a.startswith("/")) + "\\n")
    if "select" in args:
        if "FAIL_SELECT" in os.environ:
            raise SystemExit(5)
        out = args[-1]
        open(out, "w").write(json.dumps({"split": "valid"}))
    elif "test" in args:
        open(args[-1], "w").write(json.dumps({"comparison": {"passed": True}}))
    elif "verify-source" in args:
        if "FAIL_VERIFY" in os.environ:
            raise SystemExit(6)
        print(json.dumps({"hanchan": 100}))
    """
)


class RunTest(unittest.TestCase):
    def setUp(self) -> None:
        self.driver = _driver()
        self.driver.POLL_SECONDS = 0.05
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        (self.root / "generator.py").write_text(FAKE_GENERATOR)
        (self.root / "consumer.py").write_text(FAKE_CONSUMER)
        consumer = self.root / "consumer"
        consumer.write_text(
            f'#!/bin/sh\nexec "{sys.executable}" "{self.root}/consumer.py" "$@"\n'
        )
        consumer.chmod(consumer.stat().st_mode | stat.S_IEXEC)
        self.consumer = consumer
        self.calls = self.root / "calls.txt"
        self.s1 = self.root / "s1-source"
        self.s1.mkdir()
        for name in ("manifest.json", "decisions.jsonl", "label_facts.jsonl"):
            (self.s1 / name).write_text("{}\n")
        self.driver.GENERATOR = self.root / "generator.py"
        for patcher in (
            mock.patch.dict(os.environ, {"FAKE_CALLS": str(self.calls)}),
            mock.patch.object(self.driver, "uptime_seconds", return_value=10.0),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def arguments(self, **overrides):
        values = {
            "mode": "smoke",
            "workers": 2,
            "consumer_python": str(self.consumer),
            "work": self.root / "work",
            "output": self.root / "out",
            "s1_bundle": None,
            "s1_source": self.s1,
            "seed_ledger": None,
            "allocation_identity": None,
            "smoke_seeds": "931100..931103",
            "deadline_uptime_seconds": None,
        }
        values.update(overrides)
        return Namespace(**values)

    def report(self):
        return json.loads((self.root / "out" / "formal-run-report.json").read_text())

    def test_successful_run_writes_evidence(self) -> None:
        code = self.driver.run(self.arguments())
        self.assertEqual(code, 0)
        report = self.report()
        self.assertEqual(report["status"], "COMPLETE")
        self.assertEqual(report["mode"], "smoke")
        self.assertEqual(
            [row["step"] for row in report["steps"]],
            sorted(row["step"] for row in report["steps"][:2])
            + ["verify-source", "test"],
        )
        self.assertEqual(report["source_check"], {"hanchan": 100})
        out = self.root / "out"
        for name in (
            "selection.json",
            "test-result.json",
            "generation.json",
            "source/manifest.json",
            "source/decisions.jsonl.xz",
            "source/label_facts.jsonl.xz",
        ):
            self.assertTrue((out / name).is_file(), name)
        self.assertEqual(
            lzma.decompress((out / "source" / "decisions.jsonl.xz").read_bytes()),
            b'{"k":1}\n' * 50,
        )
        self.assertEqual(
            report["selection"]["sha256"],
            hashlib.sha256((out / "selection.json").read_bytes()).hexdigest(),
        )
        calls = self.calls.read_text().splitlines()
        self.assertEqual(sum("test" in c.split() for c in calls), 1)

    def test_a_failed_generation_never_runs_test(self) -> None:
        with mock.patch.dict(os.environ, {"FAIL_GENERATE": "1"}):
            code = self.driver.run(self.arguments())
        self.assertEqual(code, 1)
        report = self.report()
        self.assertEqual(report["status"], "INCOMPLETE")
        self.assertIn("generate", report["reason"])
        self.assertNotIn("test", {row["step"] for row in report["steps"]})
        self.assertFalse((self.root / "out" / "test-result.json").exists())
        self.assertNotIn("test", " ".join(self.calls.read_text().split()).split())

    def test_a_failed_select_stops_generation_and_never_runs_test(self) -> None:
        with mock.patch.dict(os.environ, {"FAIL_SELECT": "1"}):
            code = self.driver.run(self.arguments())
        self.assertEqual(code, 1)
        report = self.report()
        self.assertEqual(report["status"], "INCOMPLETE")
        self.assertFalse((self.root / "out" / "test-result.json").exists())

    def test_a_failed_completeness_check_never_runs_test(self) -> None:
        with mock.patch.dict(os.environ, {"FAIL_VERIFY": "1"}):
            code = self.driver.run(self.arguments())
        self.assertEqual(code, 1)
        report = self.report()
        self.assertEqual(report["status"], "INCOMPLETE")
        self.assertIn("verify-source", report["reason"])
        self.assertFalse((self.root / "out" / "test-result.json").exists())
        self.assertNotIn("test", " ".join(self.calls.read_text().split()).split())

    def test_the_compute_deadline_leaves_an_incomplete_report(self) -> None:
        slow = textwrap.dedent(
            """\
            import time
            time.sleep(60)
            """
        )
        (self.root / "generator.py").write_text(slow)
        with mock.patch.object(
            self.driver, "uptime_seconds", side_effect=[1.0] * 6 + [9999.0] * 200
        ):
            code = self.driver.run(self.arguments(deadline_uptime_seconds=100))
        self.assertEqual(code, 1)
        report = self.report()
        self.assertEqual(report["status"], "INCOMPLETE")
        self.assertIn("deadline", report["reason"])

    def test_formal_mode_requires_the_pinned_bundle(self) -> None:
        code = self.driver.run(self.arguments(mode="formal", smoke_seeds=None))
        self.assertEqual(code, 1)
        report = self.report()
        self.assertEqual(report["status"], "INCOMPLETE")
        self.assertIn("pinned S1 bundle", report["reason"])
        self.assertEqual(report["steps"], [])

    def test_existing_work_or_output_is_refused(self) -> None:
        (self.root / "out").mkdir()
        with self.assertRaisesRegex(self.driver.RunError, "overwrite"):
            self.driver.run(self.arguments())


class ArgumentChecksTest(unittest.TestCase):
    def setUp(self) -> None:
        self.driver = _driver()

    def main(self, *extra):
        with mock.patch.object(self.driver, "run", return_value=0) as run:
            code = self.driver.main(
                [
                    "run",
                    "--workers",
                    "2",
                    "--consumer-python",
                    "x",
                    "--work",
                    "w",
                    "--output",
                    "o",
                    *extra,
                ]
            )
        return code, run

    def test_valid_smoke_and_formal_arguments(self) -> None:
        code, run = self.main(
            "--mode", "smoke", "--smoke-seeds", "931100..931101", "--s1-source", "s"
        )
        self.assertEqual((code, run.called), (0, True))
        code, run = self.main(
            "--mode",
            "formal",
            "--s1-bundle",
            "b",
            "--seed-ledger",
            "l",
            "--allocation-identity",
            "a" * 64,
        )
        self.assertEqual((code, run.called), (0, True))

    def test_invalid_combinations_are_rejected(self) -> None:
        for extra in (
            ("--mode", "formal", "--s1-bundle", "b"),
            (
                "--mode",
                "formal",
                "--s1-source",
                "s",
                "--seed-ledger",
                "l",
                "--allocation-identity",
                "a",
            ),
            (
                "--mode",
                "formal",
                "--s1-bundle",
                "b",
                "--seed-ledger",
                "l",
                "--allocation-identity",
                "a",
                "--smoke-seeds",
                "931100",
            ),
            ("--mode", "smoke", "--s1-source", "s"),
            (
                "--mode",
                "smoke",
                "--smoke-seeds",
                "931100",
                "--s1-bundle",
                "b",
                "--s1-source",
                "s",
            ),
        ):
            with self.subTest(extra=extra):
                code, run = self.main(*extra)
                self.assertEqual((code, run.called), (1, False))


class VerifySourceTest(unittest.TestCase):
    """``verify-source`` against a stub of the lisjong strict reader."""

    def setUp(self) -> None:
        self.driver = _driver()
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.seeds = tuple(range(932000, 932100))
        self.document = None

    def write(self, *, mode="formal", decisions_seed=None, games=None, **document):
        source = self.root / "source"
        source.mkdir(exist_ok=True)
        files = {}
        for name in ("manifest.json", "decisions.jsonl", "label_facts.jsonl"):
            (source / name).write_text(name)
            data = (source / name).read_bytes()
            files[name] = {
                "bytes": len(data),
                "sha256": hashlib.sha256(data).hexdigest(),
            }
        games = games or [{"seed": s, "decisions": 2} for s in self.seeds]
        generation = {
            "mode": mode,
            "seeds": {
                "first": self.seeds[0],
                "last": self.seeds[-1],
                "count": len(self.seeds),
            },
            "games": games,
            "files": files,
            "allocation": {
                "allocation_identity": "a" * 64,
                "protocol": self.driver.FORMAL_PROTOCOL,
                "state": "RESERVED",
            },
            "runtime": {"shanten_backend": {"name": "rust"}},
        }
        generation.update(document)
        (self.root / "generation.json").write_text(json.dumps(generation))
        decisions = [
            types.SimpleNamespace(key=types.SimpleNamespace(seed=decisions_seed or s))
            for s in self.seeds
            for _ in range(2)
        ]
        manifest = types.SimpleNamespace(
            splits={"train": (), "valid": (), "test": self.seeds}, producer={}
        )
        module = types.ModuleType("lisjong.learning.riichi_deal_in_source")
        module.read_decisions = lambda directory: (manifest, decisions)
        return mock.patch.dict(sys.modules, {module.__name__: module})

    def verify(self, **kwargs):
        return self.driver.verify_source(
            self.root, mode="formal", allocation_identity="a" * 64, **kwargs
        )

    def test_a_complete_source_passes(self) -> None:
        with self.write():
            summary = self.verify()
        self.assertEqual((summary["hanchan"], summary["decisions"]), (100, 200))

    def test_failures(self) -> None:
        cases = {
            "seed outside": dict(decisions_seed=999),
            "missing game": dict(
                games=[{"seed": s, "decisions": 2} for s in self.seeds[:-1]]
            ),
            "wrong allocation": dict(
                allocation={
                    "allocation_identity": "b" * 64,
                    "protocol": self.driver.FORMAL_PROTOCOL,
                    "state": "RESERVED",
                }
            ),
            "retired allocation": dict(
                allocation={
                    "allocation_identity": "a" * 64,
                    "protocol": self.driver.FORMAL_PROTOCOL,
                    "state": "RETIRED",
                }
            ),
            "python backend": dict(runtime={"shanten_backend": {"name": "python"}}),
            "wrong mode": dict(mode="smoke"),
        }
        for name, kwargs in cases.items():
            with (
                self.subTest(name),
                self.write(**kwargs),
                self.assertRaises(self.driver.RunError),
            ):
                self.verify()

    def test_a_changed_file_is_detected(self) -> None:
        with self.write():
            (self.root / "source" / "decisions.jsonl").write_text("changed")
            with self.assertRaisesRegex(self.driver.RunError, "decisions.jsonl"):
                self.verify()


class BootstrapTest(unittest.TestCase):
    def setUp(self) -> None:
        self.text = _BOOTSTRAP.read_text(encoding="utf-8")
        self.doc = _DOC.read_text(encoding="utf-8")
        self.driver = _driver()

    def test_bash_syntax(self) -> None:
        bash = shutil.which("bash")
        if bash is None:
            self.skipTest("bash is unavailable")
        subprocess.run([bash, "-n", str(_BOOTSTRAP)], check=True)

    def test_pins_match_the_project(self) -> None:
        project = (_ROOT / "pyproject.toml").read_text(encoding="utf-8")
        riichienv = _shell_value(self.text, "FROZEN_RIICHIENV_VERSION")
        self.assertIn(f'"riichienv=={riichienv}"', project)
        self.assertEqual(
            _shell_value(self.text, "WHEEL_FILE"), backend.EXPECTED_WHEEL_FILENAME
        )

    def test_consumer_revision_is_the_documented_246_merge_commit(self) -> None:
        revision = _shell_value(self.text, "FROZEN_CONSUMER_REVISION")
        self.assertEqual(revision, "1e8a2d7547262510eb320d60ae297b662fbb6380")
        self.assertNotEqual(revision, backend.EXPECTED_LISJONG_REVISION)
        self.assertIn(revision, self.doc)

    def test_planned_configuration_matches_the_driver_and_doc(self) -> None:
        self.assertEqual(_shell_value(self.text, "REQUIRED_WORKERS"), "32")
        self.assertEqual(
            int(_shell_value(self.text, "DEADLINE_UPTIME_SECONDS")),
            self.driver.DEADLINE_UPTIME_SECONDS,
        )
        self.assertEqual(self.driver.DEADLINE_UPTIME_SECONDS, 45 * 60)
        self.assertIn("c7i.8xlarge", self.doc)

    def test_runs_the_driver_from_the_checked_out_revision(self) -> None:
        self.assertIn(
            'DRIVER="$ARENA_DIR/scripts/aws/run_riichi_wait_formal_245.py"', self.text
        )
        self.assertIn(
            'merge-base --is-ancestor "$ARENA_REVISION" origin/main', self.text
        )
        self.assertIn("--mode formal", self.text)
        self.assertNotIn("--smoke", self.text)

    def test_input_names_are_valid_runner_names(self) -> None:
        for name in ("WHEEL_FILE", "S1_BUNDLE", "LEDGER_FILE"):
            self.assertRegex(_shell_value(self.text, name), r"^[A-Za-z0-9._-]+$")

    def test_line_endings_are_lf(self) -> None:
        for path in (
            _BOOTSTRAP,
            _DRIVER,
            _ROOT / "scripts" / Path(generator.__file__).name,
        ):
            self.assertNotIn(b"\r", path.read_bytes())


if __name__ == "__main__":
    unittest.main()
