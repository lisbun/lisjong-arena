"""Tenpai PUSH/FOLD paired source, production population (lisjong-arena#475).

The population, its allocation and the CLI preconditions only: no game is
played and no live ledger is read.  The producer itself is covered by
``test_generate_tenpai_push_fold_source_476.py``.
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from lisjong_arena import seed_registry

_ROOT = Path(__file__).resolve().parents[1]
_SPEC = importlib.util.spec_from_file_location(
    "generate_tenpai_push_fold_source_475",
    _ROOT / "scripts" / "generate_tenpai_push_fold_source_475.py",
)
production = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = production
_SPEC.loader.exec_module(production)
producer = production.producer

ARENA = "a" * 40
BOOTSTRAP = "scripts/aws/bootstrap-tenpai-push-fold-source-475.sh"


def reserved_ledger(**overrides):
    parameters = {
        "owner_issue": production.OWNER_ISSUE,
        "protocol": production.PROTOCOL,
        "seed_domain": seed_registry.LISJONG_ENGINE_HANCHAN_SEED_DOMAIN,
        "purpose": "test",
        "population": production.POPULATION,
        "split": production.SPLIT,
        "seeds": production.SEEDS,
        "arena_revision": ARENA,
        "protocol_revision": production.PROTOCOL,
        "provenance_reference": "https://example.invalid/provenance",
        "allocation_timestamp": "2026-10-09T00:00:00.000000Z",
    }
    parameters.update(overrides)
    return seed_registry.reserve_allocation(seed_registry.load_ledger(), **parameters)


def _cli(*arguments: str) -> tuple[int, str, str]:
    stdout, stderr = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        code = production.main(list(arguments))
    return code, stdout.getvalue(), stderr.getvalue()


class PopulationTest(unittest.TestCase):
    def test_the_population_is_fixed(self) -> None:
        self.assertEqual(production.SEEDS, tuple(range(939000, 939400)))
        self.assertEqual(
            production.SPLITS,
            {
                "train": tuple(range(939000, 939200)),
                "valid": tuple(range(939200, 939400)),
            },
        )
        preset = production.allocation_preset()
        producer.guard.check_population(preset)
        self.assertEqual(dict(preset.splits), production.SPLITS)
        self.assertEqual(preset.owner_issue, "lisbun/lisjong-arena#475")

    def test_the_pilot_and_every_earlier_population_are_never_reused(self) -> None:
        for seeds in (
            producer.PILOT_SEEDS,
            range(931000, 932000),
            range(932000, 932100),
            range(937000, 937200),
        ):
            self.assertTrue(
                all(any(s in used for used in production.USED_RANGES) for s in seeds)
            )
        with mock.patch.object(production, "USED_RANGES", (range(939399, 939500),)):
            with self.assertRaises(producer.guard.AllocationGuardError):
                producer.guard.check_population(production.allocation_preset())


class AllocationTest(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.ledger_path = Path(directory.name) / "ledger.json"
        self.output = Path(directory.name) / "output"

    def _write(self, ledger) -> None:
        self.ledger_path.write_text(json.dumps(ledger), encoding="utf-8")

    def _check(self, identity: str, revision: str = ARENA) -> tuple[int, str, str]:
        return _cli(
            "check-allocation",
            "--seed-ledger",
            str(self.ledger_path),
            "--allocation-identity",
            identity,
            "--arena-revision",
            revision,
        )

    def test_the_fresh_allocation_of_the_revision_is_authorized(self) -> None:
        ledger, record = reserved_ledger()
        self._write(ledger)
        code, stdout, _ = self._check(record["allocation_identity"])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(stdout)["allocation"], record)

    def test_another_revision_population_or_the_pilot_is_rejected(self) -> None:
        ledger, record = reserved_ledger()
        self._write(ledger)
        code, _, stderr = self._check(record["allocation_identity"], "d" * 40)
        self.assertEqual(code, 1)
        self.assertIn("STOP / INVALID", stderr)
        for overrides in (
            {"seeds": production.SEEDS[:-1]},
            {"split": "TRAIN300-VALID100"},
            {
                "owner_issue": producer.OWNER_ISSUE,
                "protocol": producer.PROTOCOL,
                "population": producer.POPULATION,
                "split": producer.SPLIT,
                "seeds": producer.PILOT_SEEDS,
            },
        ):
            with self.subTest(overrides=sorted(overrides)):
                ledger, record = reserved_ledger(**overrides)
                self._write(ledger)
                self.assertEqual(self._check(record["allocation_identity"])[0], 1)

    def test_run_requires_32_workers_before_anything_else(self) -> None:
        with mock.patch.object(producer, "run_reserved") as run_reserved:
            code, _, stderr = _cli(
                "run",
                "--seed-ledger",
                str(self.ledger_path),
                "--allocation-identity",
                "0" * 64,
                "--selection",
                str(self.ledger_path),
                "--workers",
                "8",
                "--output",
                str(self.output),
            )
        self.assertEqual(code, 1)
        self.assertIn("workers must be 32", stderr)
        run_reserved.assert_not_called()

    def test_run_generates_this_population_through_the_shared_producer(self) -> None:
        document = {"files": {}, "total": {}, "wall_seconds": 0.0}
        with mock.patch.object(
            producer, "run_reserved", return_value=document
        ) as run_reserved:
            code, stdout, _ = _cli(
                "run",
                "--seed-ledger",
                str(self.ledger_path),
                "--allocation-identity",
                "0" * 64,
                "--selection",
                str(self.ledger_path),
                "--workers",
                "32",
                "--output",
                str(self.output),
            )
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(stdout), document)
        (arguments, preset), _ = run_reserved.call_args
        self.assertEqual(preset, production.allocation_preset())
        self.assertEqual(arguments.output, self.output)


class RunReservedTest(unittest.TestCase):
    """The shared producer generates the preset it is given, not the pilot."""

    def test_the_presets_splits_and_identity_reach_the_generation(self) -> None:
        preset = production.allocation_preset()
        arguments = mock.Mock(workers=32)
        runtime = {"arena_revision": ARENA, "dependencies": {}}
        with (
            mock.patch.object(producer, "native_runtime", return_value={}),
            mock.patch.object(
                producer, "current_producer", return_value={"arena_revision": ARENA}
            ),
            mock.patch.object(
                producer, "check_allocation", return_value={}
            ) as check_allocation,
            mock.patch(
                "lisjong_arena.policy_source_record.binding.runtime_binding",
                return_value=runtime,
            ),
            mock.patch.object(producer, "generate") as generate,
        ):
            producer.run_reserved(arguments, preset)
        self.assertIs(check_allocation.call_args.args[3], preset)
        keywords = generate.call_args.kwargs
        self.assertEqual(keywords["splits"], production.SPLITS)
        self.assertEqual(keywords["evidence"]["owner_issue"], production.OWNER_ISSUE)
        self.assertEqual(keywords["evidence"]["population"], production.POPULATION)

    def test_a_checkout_that_is_not_the_producer_revision_stops(self) -> None:
        with (
            mock.patch.object(producer, "native_runtime", return_value={}),
            mock.patch.object(
                producer, "current_producer", return_value={"arena_revision": ARENA}
            ),
            mock.patch.object(producer, "check_allocation", return_value={}),
            mock.patch(
                "lisjong_arena.policy_source_record.binding.runtime_binding",
                return_value={"arena_revision": "d" * 40, "dependencies": {}},
            ),
            mock.patch.object(producer, "generate") as generate,
        ):
            with self.assertRaises(producer.TenpaiPushFoldProducerError):
                producer.run_reserved(mock.Mock(), production.allocation_preset())
        generate.assert_not_called()


@unittest.skipUnless(
    sys.platform != "win32" and shutil.which("bash"),
    "needs a POSIX bash (on Windows, PATH may resolve bash to the WSL launcher)",
)
class BootstrapSyntaxTest(unittest.TestCase):
    """Shell syntax only; the bootstrap has not been run on AWS."""

    def test_the_bootstrap_parses(self) -> None:
        checked = subprocess.run(
            ["bash", "-n", BOOTSTRAP], cwd=_ROOT, capture_output=True, text=True
        )
        self.assertEqual(checked.returncode, 0, checked.stderr)

    def test_the_bootstrap_stops_without_the_prologue_input(self) -> None:
        checked = subprocess.run(
            ["bash", BOOTSTRAP],
            cwd=_ROOT,
            capture_output=True,
            text=True,
            env={"PATH": "/usr/bin:/bin"},
        )
        self.assertEqual(checked.returncode, 2, checked.stderr)
        self.assertIn("measurement-source-prologue.sh", checked.stderr)


class BootstrapPinTest(unittest.TestCase):
    def test_the_bootstrap_fixes_the_wait_model_and_the_workers(self) -> None:
        # The revisions and the wheel are this run's frozen record and do not
        # follow later pins.
        text = (_ROOT / BOOTSTRAP).read_text(encoding="utf-8")
        self.assertIn(
            f'SELECTION_SHA256="{producer.wire.SELECTED_WAIT_MODEL_SHA256}"', text
        )
        self.assertIn(f'GENERATOR="scripts/{Path(_SPEC.origin).name}"', text)
        self.assertIn(f"REQUIRED_WORKERS={production.REQUIRED_WORKERS}", text)


if __name__ == "__main__":
    unittest.main()
