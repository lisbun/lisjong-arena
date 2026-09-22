"""Contract tests for the #342 player-safe Offense Foundation source record."""

import copy
import json
import tempfile
import unittest
from concurrent.futures import Future
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from lisjong.policies import TwoStepUkeirePolicy
from lisjong.policy_contract import DecisionTraceRecorder, execute_policy_with_trace

from lisjong_arena import seed_registry
from lisjong_arena.offense_foundation import corpus, source_record
from lisjong_arena.offense_foundation.__main__ import main
from lisjong_arena.offense_foundation.fixtures import probes
from lisjong_arena.offense_foundation.protocol import make_lock
from lisjong_arena.offense_foundation.qualification import (
    qualify,
    read_document,
    seal,
    write_document,
)
from lisjong_arena.offense_foundation.semantics import OffenseError
from lisjong_arena.riichienv.local_game_runner import SeatDecisionObservation


def _request():
    seeds = list(range(20))
    return {
        "allocation_bindings": {
            "QUALIFICATION": {
                "allocation_identity": "1" * 64,
                "ledger_revision": "2" * 64,
                "owner_repository": "lisbun/lisjong-arena",
                "seed_domain": seed_registry.RIICHIENV_HALF_HANCHAN_SEED_DOMAIN,
                "seed_membership_identity": seed_registry.seed_membership_identity(
                    seeds
                ),
            }
        },
        "phase": "P2",
        "populations": {"QUALIFICATION": seeds},
    }


def _observation(probe):
    recorder = DecisionTraceRecorder()
    execute_policy_with_trace(TwoStepUkeirePolicy(), probe.context, recorder)
    return SeatDecisionObservation(
        probe.context.input.self_seat, probe.context.input, recorder.snapshot()[0]
    )


class SourceRecordTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.report = qualify({})
        cls.observations = tuple(_observation(probe) for probe in probes())
        cls.inspection = SimpleNamespace(
            step_observations=tuple(
                SimpleNamespace(step_ordinal=index, seat_decisions=(observation,))
                for index, observation in enumerate(cls.observations)
            )
        )

    def fake_game(self, seed):
        return SimpleNamespace(
            seed=seed,
            game_mode="4p-red-half",
            decisions=len(self.observations),
            steps=len(self.observations),
        ), self.inspection

    def generate_fixture(self, root, *, workers=1, source_name="source"):
        lock = make_lock(_request(), self.report)
        corpus_path = root / "corpus"
        source_path = root / source_name
        with (
            patch.object(corpus, "runtime_binding", return_value={}),
            patch.object(corpus, "_record_game", side_effect=self.fake_game),
        ):
            result = corpus.generate(
                lock,
                corpus_path,
                workers=workers,
                source_record_destination=source_path,
            )
        return lock, result, corpus_path, source_path

    class ImmediateExecutor:
        def __init__(self, max_workers):
            self.max_workers = max_workers
            self.futures = []

        def submit(self, function, *args):
            future = Future()
            try:
                future.set_result(function(*args))
            except BaseException as error:
                future.set_exception(error)
            self.futures.append(future)
            return future

        def shutdown(self, *, wait, cancel_futures):
            pass

    def test_player_safe_roundtrip_and_locked_corpus_identity_are_independent(self):
        with (
            tempfile.TemporaryDirectory() as first_tmp,
            tempfile.TemporaryDirectory() as second_tmp,
        ):
            first_root, second_root = Path(first_tmp), Path(second_tmp)
            lock, first, corpus_path, source_path = self.generate_fixture(
                first_root, source_name="source-a"
            )
            _, second, corpus_path_2, source_path_2 = self.generate_fixture(
                second_root, source_name="a-different-sidecar-name"
            )

            self.assertEqual(first, second)
            self.assertEqual(
                source_record.read_source_record(
                    source_path, expected_lock=lock, corpus_path=corpus_path
                )["schema"],
                source_record.SOURCE_SCHEMA,
            )
            self.assertEqual(
                source_record.read_source_record(
                    source_path_2, expected_lock=lock, corpus_path=corpus_path_2
                )["source_contract"],
                source_record.read_source_record(
                    source_path, expected_lock=lock, corpus_path=corpus_path
                )["source_contract"],
            )

            # #331 remains byte/identity compatible: no source-record field or
            # payload is admitted into the locked scientific corpus.
            self.assertNotIn("source", first)
            for game_index, game in enumerate(first["games"]):
                self.assertEqual(
                    set(game["files"]),
                    {"rows.jsonl", "features.f32", "legal-mask.u8"},
                )
                self.assertEqual(
                    {
                        path.name
                        for path in (corpus_path / f"game-{game_index:03d}").iterdir()
                    },
                    {"rows.jsonl", "features.f32", "legal-mask.u8"},
                )
            for left in sorted(
                path for path in corpus_path.rglob("*") if path.is_file()
            ):
                relative = left.relative_to(corpus_path)
                self.assertEqual(
                    left.read_bytes(), (corpus_path_2 / relative).read_bytes()
                )

            row = json.loads(
                (source_path / "game-000" / source_record.SOURCE_FILENAME)
                .read_text(encoding="utf-8")
                .splitlines()[0]
            )
            self.assertEqual(
                set(row),
                {
                    "game_ordinal",
                    "seed",
                    "split",
                    "step_ordinal",
                    "decision_ordinal",
                    "actor_seat",
                    "policy_input",
                    "legal_actions",
                    "teacher_selected_action",
                },
            )
            self.assertEqual(
                set(row["policy_input"]), {"self_seat", "round", "players", "own_hand"}
            )
            self.assertFalse(
                {"features", "analysis", "candidates", "hidden_state", "future_events"}
                & set(row)
            )

    def test_worker_completion_order_does_not_change_source_record(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            lock, expected, corpus_serial, source_serial = self.generate_fixture(
                root / "serial"
            )
            parallel_root = root / "parallel"
            parallel_root.mkdir()
            with (
                patch.object(corpus, "runtime_binding", return_value={}),
                patch.object(corpus, "_record_game", side_effect=self.fake_game),
                patch.object(corpus, "ProcessPoolExecutor", self.ImmediateExecutor),
                patch.object(
                    corpus,
                    "as_completed",
                    side_effect=lambda futures: reversed(tuple(futures)),
                ),
            ):
                actual = corpus.generate(
                    lock,
                    parallel_root / "corpus",
                    workers=4,
                    source_record_destination=parallel_root / "source",
                )
            self.assertEqual(expected, actual)
            source_parallel = parallel_root / "source"
            for left in sorted(
                path for path in source_serial.rglob("*") if path.is_file()
            ):
                relative = left.relative_to(source_serial)
                self.assertEqual(
                    left.read_bytes(), (source_parallel / relative).read_bytes()
                )
            self.assertEqual(
                corpus.read_corpus(corpus_serial, expected_lock=lock),
                corpus.read_corpus(parallel_root / "corpus", expected_lock=lock),
            )

    def test_resealed_canonical_action_mismatch_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            lock, _, corpus_path, source_path = self.generate_fixture(Path(tmp))
            payload = source_path / "game-000" / source_record.SOURCE_FILENAME
            lines = payload.read_text(encoding="utf-8").splitlines()
            changed = False
            for index, line in enumerate(lines):
                row = json.loads(line)
                if len(row["legal_actions"]) >= 2:
                    replacement = next(
                        action
                        for action in row["legal_actions"]
                        if action != row["teacher_selected_action"]
                    )
                    row["teacher_selected_action"] = copy.deepcopy(replacement)
                    lines[index] = source_record._canonical_line(row).rstrip("\n")
                    changed = True
                    break
            self.assertTrue(changed)
            payload.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")

            manifest_path = source_path / "manifest.json"
            manifest = read_document(manifest_path)
            game = {
                key: value
                for key, value in manifest["games"][0].items()
                if key != "identity"
            }
            game["files"] = {
                source_record.SOURCE_FILENAME: source_record._file_info(payload)
            }
            manifest_body = {
                key: value for key, value in manifest.items() if key != "identity"
            }
            manifest_body["games"][0] = seal(game)
            manifest_path.unlink()
            write_document(manifest_path, seal(manifest_body))

            with self.assertRaisesRegex(
                OffenseError, "canonical action/scientific row mismatch"
            ):
                source_record.read_source_record(
                    source_path, expected_lock=lock, corpus_path=corpus_path
                )

    def _reseal_first_game_payload(self, source_path, payload):
        manifest_path = source_path / "manifest.json"
        manifest = read_document(manifest_path)
        game = {
            key: value
            for key, value in manifest["games"][0].items()
            if key != "identity"
        }
        game["files"] = {
            source_record.SOURCE_FILENAME: source_record._file_info(payload)
        }
        body = {key: value for key, value in manifest.items() if key != "identity"}
        body["games"][0] = seal(game)
        manifest_path.unlink()
        write_document(manifest_path, seal(body))

    def test_duplicate_missing_and_extra_decisions_are_rejected(self):
        mutations = {
            "missing": lambda lines: lines[1:],
            "extra": lambda lines: [*lines, lines[-1]],
            "duplicate": lambda lines: [lines[0], lines[0], *lines[1:]],
        }
        for name, mutate in mutations.items():
            with self.subTest(name=name), tempfile.TemporaryDirectory() as tmp:
                lock, _, corpus_path, source_path = self.generate_fixture(Path(tmp))
                payload = source_path / "game-000" / source_record.SOURCE_FILENAME
                lines = payload.read_text(encoding="utf-8").splitlines()
                mutated = mutate(lines)
                payload.write_text(
                    "\n".join(mutated) + "\n", encoding="utf-8", newline="\n"
                )
                self._reseal_first_game_payload(source_path, payload)
                with self.assertRaises(OffenseError):
                    source_record.read_source_record(
                        source_path, expected_lock=lock, corpus_path=corpus_path
                    )

    def test_resealed_provenance_identity_mismatch_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            lock, _, corpus_path, source_path = self.generate_fixture(Path(tmp))
            manifest_path = source_path / "manifest.json"
            manifest = read_document(manifest_path)
            body = {key: value for key, value in manifest.items() if key != "identity"}
            body["lock_identity"] = "0" * 64
            manifest_path.unlink()
            write_document(manifest_path, seal(body))
            with self.assertRaisesRegex(
                OffenseError, "schema/provenance identity mismatch"
            ):
                source_record.read_source_record(
                    source_path, expected_lock=lock, corpus_path=corpus_path
                )

    def test_schema_extra_file_and_cli_readback_fail_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            lock, _, corpus_path, source_path = self.generate_fixture(Path(tmp))
            lock_path = Path(tmp) / "lock.json"
            write_document(lock_path, lock)
            self.assertEqual(
                main(
                    [
                        "source-readback",
                        "--source-record",
                        str(source_path),
                        "--corpus",
                        str(corpus_path),
                        "--lock",
                        str(lock_path),
                    ]
                ),
                0,
            )

            extra = source_path / "unexpected"
            extra.write_text("bad", encoding="utf-8")
            with self.assertRaises(OffenseError):
                source_record.read_source_record(
                    source_path, expected_lock=lock, corpus_path=corpus_path
                )
            extra.unlink()

            manifest_path = source_path / "manifest.json"
            manifest = read_document(manifest_path)
            body = {key: value for key, value in manifest.items() if key != "identity"}
            body["schema"] = "future-unsupported-schema"
            manifest_path.unlink()
            write_document(manifest_path, seal(body))
            with self.assertRaisesRegex(OffenseError, "schema/provenance"):
                source_record.read_source_record(
                    source_path, expected_lock=lock, corpus_path=corpus_path
                )


if __name__ == "__main__":
    unittest.main()
