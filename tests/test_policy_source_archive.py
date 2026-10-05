"""Retained policy source record: archive / restore contract tests (#449)."""

import contextlib
import importlib.util
import io
import json
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from test_policy_source_record import RUNTIME, development_population, fake_game

from lisjong_arena.offense_foundation.qualification import seal, write_document
from lisjong_arena.policy_source_record import (
    PolicySourceRecordError,
    archive,
    binding,
    generation,
    record,
    replay,
)
from lisjong_arena.policy_source_record.__main__ import main

C0 = "placement-aware-speed-call"
_DRIVER = (
    Path(__file__).resolve().parents[1] / "scripts" / "aws" / "calibrate_c0_442.py"
)


def _files(root: Path) -> dict[str, bytes]:
    return {
        str(path.relative_to(root)): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


class SourceArchiveTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        patcher = patch.object(binding, "runtime_binding", return_value=RUNTIME)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.manifest, self.source = self.generate("source")
        self.summary = replay.replay_verify(self.source)

    def generate(self, name, population=None):
        with patch.object(
            generation,
            "run_recorded_game",
            side_effect=lambda _teacher, seed: fake_game(C0, seed),
        ):
            manifest = generation.generate(
                population or development_population(), self.root / name, teacher=C0
            )
        return manifest, self.root / name

    def pack(self, name="archive", **overrides):
        return archive.archive_source_record(
            self.source, self.root / name, replay={**self.summary, **overrides}
        )

    def assert_nothing_published(self, destination: Path):
        self.assertFalse(destination.exists())
        self.assertEqual(
            [
                child.name
                for child in destination.parent.iterdir()
                if ".staging-" in child.name
            ],
            [],
        )

    # --- archive ----------------------------------------------------------

    def test_restored_record_matches_the_generated_record(self):
        evidence = self.pack()
        restored = archive.restore_source_record(
            self.root / "archive",
            self.root / "restored",
            expected_identity=self.manifest["identity"],
        )

        self.assertEqual(restored["identity"], self.manifest["identity"])
        self.assertEqual(restored, record.read_source_record(self.root / "restored"))
        self.assertEqual(_files(self.root / "restored"), _files(self.source))
        self.assertEqual(evidence["source"]["identity"], self.manifest["identity"])
        self.assertEqual(
            evidence["replay"],
            {
                "decisions": self.summary["decisions"],
                "mismatches": 0,
                "source_identity": self.manifest["identity"],
                "teacher": C0,
            },
        )
        self.assertEqual(
            sorted(child.name for child in (self.root / "archive").iterdir()),
            sorted([archive.ARCHIVE_FILENAME, archive.EVIDENCE_FILENAME]),
        )

    def test_archive_bytes_are_deterministic(self):
        first = self.pack("first")
        second = self.pack("second")
        self.assertEqual(first["archive"], second["archive"])
        self.assertEqual(first["identity"], second["identity"])

    def test_archive_requires_a_full_mismatch_free_replay_of_this_source(self):
        cases = {
            "different source": {"source_identity": "0" * 64},
            "different teacher": {"teacher": "two-step"},
            "every decision": {"decisions": self.summary["decisions"] - 1},
            "reports mismatches": {"mismatches": 1},
        }
        for message, overrides in cases.items():
            with self.subTest(message):
                destination = self.root / "archive"
                with self.assertRaisesRegex(PolicySourceRecordError, message):
                    self.pack(**overrides)
                self.assert_nothing_published(destination)
        with self.assertRaisesRegex(PolicySourceRecordError, "missing fields"):
            archive.archive_source_record(
                self.source, self.root / "archive", replay={"mismatches": 0}
            )

    def test_archive_refuses_a_record_changed_after_its_strict_read(self):
        read = record.read_source_record

        def read_then_change(path, **kwargs):
            manifest = read(path, **kwargs)
            payload = Path(path) / "game-001" / record.SOURCE_FILENAME
            payload.write_bytes(payload.read_bytes()[:-1])
            return manifest

        with patch.object(record, "read_source_record", side_effect=read_then_change):
            with self.assertRaisesRegex(PolicySourceRecordError, "differs"):
                self.pack()
        self.assert_nothing_published(self.root / "archive")

    def test_archive_refuses_an_existing_destination(self):
        self.pack()
        with self.assertRaisesRegex(PolicySourceRecordError, "overwrite"):
            self.pack()

    # --- restore ----------------------------------------------------------

    def restore(self, name="restored", identity=None):
        return archive.restore_source_record(
            self.root / "archive",
            self.root / name,
            expected_identity=identity or self.manifest["identity"],
        )

    def reseal_evidence(self, mutate):
        path = self.root / "archive" / archive.EVIDENCE_FILENAME
        body = json.loads(path.read_text(encoding="utf-8"))
        body.pop("identity")
        mutate(body)
        path.unlink()
        write_document(path, seal(body))

    def replace_archive(self, build):
        """Rewrite the tar.gz with ``build(tar)`` and reseal matching evidence."""
        path = self.root / "archive" / archive.ARCHIVE_FILENAME
        path.unlink()
        with tarfile.open(path, mode="w:gz") as tar:
            build(tar)
        info = archive._file_info(path)
        self.reseal_evidence(lambda body: body["archive"].update(info))

    def test_restore_rejects_an_unexpected_source_identity(self):
        self.pack()
        with self.assertRaisesRegex(PolicySourceRecordError, "expected source"):
            self.restore(identity="0" * 64)
        self.assert_nothing_published(self.root / "restored")

    def test_restore_rejects_an_archive_hash_mismatch(self):
        self.pack()
        payload = self.root / "archive" / archive.ARCHIVE_FILENAME
        data = bytearray(payload.read_bytes())
        data[-1] ^= 0xFF
        payload.write_bytes(bytes(data))
        with self.assertRaisesRegex(PolicySourceRecordError, "SHA-256"):
            self.restore()
        self.assert_nothing_published(self.root / "restored")

    def test_restore_rejects_missing_or_failed_replay_evidence(self):
        self.pack()
        evidence = (self.root / "archive" / archive.EVIDENCE_FILENAME).read_bytes()
        for message, mutate in (
            ("reports mismatches", lambda body: body["replay"].update(mismatches=2)),
            (
                "every decision",
                lambda body: body["replay"].update(decisions=1),
            ),
            (
                "different source",
                lambda body: body["replay"].update(source_identity="0" * 64),
            ),
            ("evidence.replay", lambda body: body["replay"].pop("mismatches")),
        ):
            with self.subTest(message):
                self.reseal_evidence(mutate)
                with self.assertRaisesRegex(PolicySourceRecordError, message):
                    self.restore()
                self.assert_nothing_published(self.root / "restored")
                path = self.root / "archive" / archive.EVIDENCE_FILENAME
                path.unlink()
                path.write_bytes(evidence)

    def test_restore_rejects_tampered_evidence(self):
        self.pack()
        path = self.root / "archive" / archive.EVIDENCE_FILENAME
        text = path.read_text(encoding="utf-8")
        self.assertIn('"mismatches": 0', text)
        path.write_text(text.replace('"mismatches": 0', '"mismatches": 1'))
        with self.assertRaisesRegex(PolicySourceRecordError, "evidence"):
            self.restore()
        self.assert_nothing_published(self.root / "restored")

    def test_restore_rejects_a_record_that_differs_from_the_evidence(self):
        # Another valid record packed under this record's evidence.
        _, other = self.generate(
            "other",
            record.population_document(
                record.DEVELOPMENT, {"TRAIN": [31, 32], "SELECT": [33]}
            ),
        )
        self.pack()

        def build(tar):
            for name in sorted(_files(other)):
                tar.add(other / name, arcname=name)

        self.replace_archive(build)
        with self.assertRaises(PolicySourceRecordError):
            self.restore()
        self.assert_nothing_published(self.root / "restored")

    def test_restore_rejects_unsafe_or_unexpected_members(self):
        self.pack()
        outside = self.root / "outside.txt"
        outside.write_text("x")
        for name in ("../escape.jsonl", "/abs/manifest.json", "notes.txt"):
            with self.subTest(name):

                def build(tar, name=name):
                    tar.add(self.source / "manifest.json", arcname="manifest.json")
                    tar.add(outside, arcname=name)

                self.replace_archive(build)
                with self.assertRaisesRegex(PolicySourceRecordError, "member"):
                    self.restore()
                self.assert_nothing_published(self.root / "restored")
                self.assertFalse((self.root / "escape.jsonl").exists())

    def test_restore_refuses_an_existing_destination(self):
        self.pack()
        (self.root / "restored").mkdir()
        with self.assertRaisesRegex(PolicySourceRecordError, "overwrite"):
            self.restore()

    # --- CLI --------------------------------------------------------------

    def test_cli_replay_archive_restore_roundtrip(self):
        summary = self.root / "replay-summary.json"
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(
                main(
                    [
                        "replay-verify",
                        "--source",
                        str(self.source),
                        "--summary",
                        str(summary),
                    ]
                ),
                0,
            )
            self.assertEqual(
                main(
                    [
                        "archive",
                        "--source",
                        str(self.source),
                        "--replay-summary",
                        str(summary),
                        "--output",
                        str(self.root / "archive"),
                    ]
                ),
                0,
            )
            restore = [
                "restore",
                "--archive",
                str(self.root / "archive"),
                "--output",
                str(self.root / "restored"),
                "--expected-identity",
            ]
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(main([*restore, "0" * 64]), 1)
            self.assertEqual(main([*restore, self.manifest["identity"]]), 0)
        self.assertEqual(_files(self.root / "restored"), _files(self.source))

    # --- AWS driver entry -------------------------------------------------

    def test_driver_convert_starts_no_conversion_on_a_mismatch(self):
        spec = importlib.util.spec_from_file_location("calibrate_c0_442", _DRIVER)
        driver = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(driver)
        self.pack()
        args = type(
            "Args",
            (),
            {
                "archive": str(self.root / "archive"),
                "expected_identity": "0" * 64,
                "learning_python": "python",
                "work": str(self.root / "work"),
                "output": str(self.root / "out"),
            },
        )
        with patch.object(driver, "_run_steps") as run_steps:
            with self.assertRaisesRegex(PolicySourceRecordError, "expected source"):
                driver.convert_main(args)
            run_steps.assert_not_called()
            self.assertFalse((self.root / "work" / "source").exists())

            args.expected_identity = self.manifest["identity"]
            args.work = str(self.root / "work2")
            run_steps.return_value = []
            report = driver.convert_main(args)
        specs = run_steps.call_args.args[0]
        self.assertEqual(
            [name for name, _, _ in specs],
            ["materialize-candidate-dataset", "materialize-dataset"],
        )
        for _, argv, _ in specs:
            self.assertEqual(
                argv[argv.index("--source-record") + 1],
                str(self.root / "work2" / "source"),
            )
        self.assertEqual(report["source_identity"], self.manifest["identity"])
        self.assertEqual(_files(self.root / "work2" / "source"), _files(self.source))


if __name__ == "__main__":
    unittest.main()
