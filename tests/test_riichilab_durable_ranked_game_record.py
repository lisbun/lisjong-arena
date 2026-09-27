"""RiichiLab ranked durable raw record (Issue #168) のArena-owned contract test。

live RiichiLabへは接続しない。fake `Transport`とsynthetic ranked protocol
traceだけで、completed bundleのround-trip、raw preservation、request /
send / ack correlation、fail-closedな拒否、credential safety、CLI
compositionを固定する。

protocol logger自体(`JsonlProtocolTraceWriter`)とrequest_action parser
(`parse_request_action()`)のcorrectnessは既存testが所有するため、ここでは
再実装・再検証せず、durable recordがそれらをreuseしていることだけを確認する。
"""

import asyncio
import contextlib
import io
import json
import os
import unittest
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from lisjong.policies import MinimalPolicy
from lisjong.policy_contract.seat import Seat
from riichienv import RiichiEnv

from lisjong_arena._artifact_io import canonical_json_text
from lisjong_arena.durable_local_game_record import LOCAL_GAME_RECORD_SCHEMA_ID
from lisjong_arena.riichienv.adapter import tile_from_physical_id, tile_to_mjai
from lisjong_arena.riichilab.cli import (
    build_arg_parser,
    resolve_ranked_record_path,
)
from lisjong_arena.riichilab.durable_ranked_game_record import (
    MANIFEST_FILENAME,
    PROTOCOL_TRACE_FILENAME,
    RANKED_GAME_RECORD_COMPLETION_STATUS,
    RANKED_GAME_RECORD_EXECUTION_BACKEND,
    RANKED_GAME_RECORD_MODE,
    RANKED_GAME_RECORD_SCHEMA_ID,
    RANKED_GAME_RECORD_SCHEMA_VERSION,
    RESULT_FILENAME,
    UNRESOLVED_PROVENANCE_VALUE,
    DurableRankedGameRecord,
    DurableRankedGameRecordError,
    RankedRecordProvenance,
    acquire_ranked_game_record,
    collect_ranked_record_provenance,
    iter_ranked_decisions,
    load_ranked_game_record,
    save_ranked_game_record,
    summarize_ranked_game_record,
)
from lisjong_arena.riichilab.errors import (
    ProtocolError,
    UnexpectedDisconnectError,
)
from lisjong_arena.riichilab.ranked import RankedGameResult, _run_cli
from lisjong_arena.riichilab.trace import ProtocolTraceError
from lisjong_arena.riichilab.transport import TransportClosed

_TOKEN = "test-only-bot-token-value"
_TOKEN_VAR = "LISJONG_DEV_BOT_TOKEN"
_TRACE_PATH_VAR = "RIICHILAB_TRACE_PATH"
_ALL_TOKEN_VARS = (
    "LISJONG_DEV_BOT_TOKEN",
    "LISJONG_BASELINE_BOT_TOKEN",
    "LISJONG_BOT_TOKEN",
)


def _observation_base64(seed: int) -> str:
    """seat 0のreal RiichiEnv Observationのbase64 representation。"""
    env = RiichiEnv(seed=seed, game_mode="4p-red-east")
    return next(iter(env.reset().values())).serialize_to_base64()


def _provenance(**overrides: str) -> RankedRecordProvenance:
    values: dict[str, str] = {
        "execution_environment": "riichilab-ranked",
        "lisjong_arena_version": "0.1.0",
        "lisjong_arena_revision": UNRESOLVED_PROVENANCE_VALUE,
        "lisjong_version": "0.1.0",
        "lisjong_revision": "0" * 40,
        "lisjong_engine_version": "0.1.0",
        "lisjong_engine_revision": "1" * 40,
        "riichienv_version": "0.4.8",
        "python_version": "3.14.0",
        "python_implementation": "CPython",
        "profile_identity": "lisjong-dev",
        "policy_identity": "MinimalPolicy",
    }
    values.update(overrides)
    return RankedRecordProvenance(**values)


def _timestamp(offset: int) -> str:
    return datetime(2026, 9, 12, 0, 0, offset, tzinfo=timezone.utc).isoformat()


def _trace_text(entries: list[tuple[str, dict]]) -> str:
    lines = []
    for index, (direction, payload) in enumerate(entries):
        lines.append(
            json.dumps(
                {
                    "timestamp": _timestamp(index),
                    "direction": direction,
                    "event_type": payload.get("type"),
                    "payload": payload,
                }
            )
        )
    return "".join(line + "\n" for line in lines)


def _request_action(request_id: int, *, seed: int = 7, **extra: object) -> dict:
    payload: dict = {
        "type": "request_action",
        "request_id": request_id,
        "possible_actions": [{"type": "none", "actor": 0}],
        "observation": _observation_base64(seed),
    }
    payload.update(extra)
    return payload


def _sent_action(request_id: int) -> dict:
    return {
        "type": "dahai",
        "actor": 0,
        "pai": "1m",
        "tsumogiri": False,
        "request_id": request_id,
    }


def _completed_entries() -> list[tuple[str, dict]]:
    return [
        ("recv", {"type": "start_game", "id": 0}),
        ("recv", _request_action(1, seed=7, time={"grace_ms": 500})),
        ("send", _sent_action(1)),
        ("recv", {"type": "action_ack", "request_id": 1, "status": "accepted"}),
        ("recv", _request_action(2, seed=8, future_field={"nested": True})),
        ("send", _sent_action(2)),
        ("recv", {"type": "action_ack", "request_id": 2, "status": "stale"}),
        ("recv", {"type": "action_ack", "request_id": 2, "status": "defaulted"}),
        ("recv", {"type": "end_game", "scores": [32000, 24000, 23000, 21000]}),
    ]


def _completed_result(**overrides: object) -> RankedGameResult:
    values: dict[str, object] = {
        "end_game_received": True,
        "seat": Seat.SEAT_0,
        "requests_received": 2,
        "responses_sent": 2,
        "ack_history": {1: ("accepted",), 2: ("stale", "defaulted")},
        "scores": (32000, 24000, 23000, 21000),
    }
    values.update(overrides)
    return RankedGameResult(**values)


def _write_trace(directory: Path, entries: list[tuple[str, dict]]) -> Path:
    path = directory / "staged-protocol.jsonl"
    path.write_text(_trace_text(entries), encoding="utf-8", newline="\n")
    return path


def _save(
    directory: Path,
    *,
    entries: list[tuple[str, dict]] | None = None,
    result: RankedGameResult | None = None,
    name: str = "record",
) -> DurableRankedGameRecord:
    trace_path = _write_trace(
        directory, _completed_entries() if entries is None else entries
    )
    return save_ranked_game_record(
        _completed_result() if result is None else result,
        trace_path,
        directory / name,
        provenance=_provenance(),
    )


def _read_manifest(bundle: Path) -> dict:
    return json.loads((bundle / MANIFEST_FILENAME).read_text(encoding="utf-8"))


def _write_manifest(bundle: Path, manifest: dict) -> None:
    (bundle / MANIFEST_FILENAME).write_text(
        canonical_json_text(manifest), encoding="utf-8", newline="\n"
    )


def _repack(bundle: Path) -> None:
    """payload fileを差し替えた後、digestとrecord identityを整合させる。

    digest / identity checkとsemantic checkを独立にtestするためのhelper。
    """
    import hashlib

    manifest = _read_manifest(bundle)
    for name, filename in (
        ("protocol_trace", PROTOCOL_TRACE_FILENAME),
        ("result", RESULT_FILENAME),
    ):
        data = (bundle / filename).read_bytes()
        manifest["payloads"][name] = {
            "byte_count": len(data),
            "filename": filename,
            "sha256": hashlib.sha256(data).hexdigest(),
        }
    without_identity = {
        key: value for key, value in manifest.items() if key != "record_identity"
    }
    manifest["record_identity"] = hashlib.sha256(
        canonical_json_text(without_identity).encode("utf-8")
    ).hexdigest()
    _write_manifest(bundle, manifest)


class RecordRoundTripTest(unittest.TestCase):
    def test_completed_record_round_trips_through_the_strict_loader(self) -> None:
        with TemporaryDirectory() as raw:
            directory = Path(raw)
            saved = _save(directory)
            loaded = load_ranked_game_record(directory / "record")

        self.assertIsInstance(loaded, DurableRankedGameRecord)
        self.assertEqual(loaded.record_identity, saved.record_identity)
        self.assertEqual(loaded.bound_seat, Seat.SEAT_0)
        self.assertEqual(loaded.result, _completed_result())
        self.assertEqual(len(loaded.protocol_entries), len(_completed_entries()))
        self.assertEqual(set(loaded.payloads), {"protocol_trace", "result"})

    def test_bundle_contains_exactly_the_three_logical_files(self) -> None:
        with TemporaryDirectory() as raw:
            directory = Path(raw)
            _save(directory)
            names = {entry.name for entry in (directory / "record").iterdir()}
            leftovers = [
                entry.name
                for entry in directory.iterdir()
                if entry.is_dir() and entry.name != "record"
            ]

        self.assertEqual(
            names, {MANIFEST_FILENAME, PROTOCOL_TRACE_FILENAME, RESULT_FILENAME}
        )
        self.assertEqual(leftovers, [])

    def test_manifest_fixes_schema_mode_and_completion_status(self) -> None:
        with TemporaryDirectory() as raw:
            directory = Path(raw)
            _save(directory)
            manifest = _read_manifest(directory / "record")

        self.assertEqual(manifest["schema_id"], RANKED_GAME_RECORD_SCHEMA_ID)
        self.assertEqual(manifest["schema_version"], RANKED_GAME_RECORD_SCHEMA_VERSION)
        self.assertEqual(manifest["mode"], RANKED_GAME_RECORD_MODE)
        self.assertEqual(
            manifest["execution_backend"], RANKED_GAME_RECORD_EXECUTION_BACKEND
        )
        self.assertEqual(
            manifest["completion_status"], RANKED_GAME_RECORD_COMPLETION_STATUS
        )
        self.assertEqual(manifest["bound_seat"], 0)

    def test_ranked_schema_is_independent_of_the_riichienv_local_record(self) -> None:
        self.assertNotEqual(RANKED_GAME_RECORD_SCHEMA_ID, LOCAL_GAME_RECORD_SCHEMA_ID)

    def test_semantic_identity_does_not_depend_on_the_storage_path(self) -> None:
        with TemporaryDirectory() as raw:
            directory = Path(raw)
            first = _save(directory, name="here")
            nested = directory / "deeply" / "nested" / "elsewhere"
            trace_path = _write_trace(directory, _completed_entries())
            second = save_ranked_game_record(
                _completed_result(), trace_path, nested, provenance=_provenance()
            )
            reloaded = load_ranked_game_record(nested)

        self.assertEqual(first.record_identity, second.record_identity)
        self.assertEqual(reloaded.record_identity, first.record_identity)

    def test_different_protocol_payloads_produce_different_identities(self) -> None:
        other = _completed_entries()
        other[1] = ("recv", _request_action(1, seed=11))
        with TemporaryDirectory() as raw:
            directory = Path(raw)
            first = _save(directory, name="first")
            second = _save(directory, entries=other, name="second")

        self.assertNotEqual(first.record_identity, second.record_identity)


class ProvenanceTest(unittest.TestCase):
    def test_unresolvable_values_are_explicit_rather_than_guessed(self) -> None:
        provenance = collect_ranked_record_provenance()

        self.assertEqual(provenance.riichienv_version, "0.4.10")
        self.assertEqual(provenance.profile_identity, UNRESOLVED_PROVENANCE_VALUE)
        self.assertEqual(provenance.policy_identity, UNRESOLVED_PROVENANCE_VALUE)
        self.assertEqual(provenance.execution_environment, "riichilab-ranked")

    def test_declared_identities_are_recorded(self) -> None:
        provenance = collect_ranked_record_provenance(
            profile_identity="lisjong-dev", policy_identity="MinimalPolicy"
        )

        self.assertEqual(provenance.profile_identity, "lisjong-dev")
        self.assertEqual(provenance.policy_identity, "MinimalPolicy")

    def test_provenance_rejects_a_foreign_execution_environment(self) -> None:
        with self.assertRaises(ValueError):
            _provenance(execution_environment="riichienv")

    def test_manifest_carries_no_local_path_or_machine_user(self) -> None:
        with TemporaryDirectory() as raw:
            directory = Path(raw)
            _save(directory)
            manifest_text = (directory / "record" / MANIFEST_FILENAME).read_text(
                encoding="utf-8"
            )

        self.assertNotIn(str(directory), manifest_text)
        for candidate in (os.environ.get("USER"), os.environ.get("USERNAME")):
            if candidate:
                self.assertNotIn(candidate, manifest_text)


class RawPreservationTest(unittest.TestCase):
    def test_raw_request_action_payload_is_preserved_losslessly(self) -> None:
        with TemporaryDirectory() as raw:
            record = _save(Path(raw))

        requests = [
            entry.payload
            for entry in record.protocol_entries
            if entry.payload.get("type") == "request_action"
        ]
        self.assertEqual(requests[0], _completed_entries()[1][1])
        self.assertEqual(requests[1], _completed_entries()[4][1])

    def test_unknown_request_action_fields_survive_readback(self) -> None:
        with TemporaryDirectory() as raw:
            record = _save(Path(raw))

        decisions = iter_ranked_decisions(record)
        self.assertEqual(decisions[1].request_payload["future_field"], {"nested": True})
        self.assertEqual(decisions[0].time, {"grace_ms": 500})

    def test_observation_base64_is_kept_as_the_raw_source(self) -> None:
        with TemporaryDirectory() as raw:
            record = _save(Path(raw))

        decisions = iter_ranked_decisions(record)
        self.assertEqual(decisions[0].observation_base64, _observation_base64(7))
        self.assertEqual(decisions[1].observation_base64, _observation_base64(8))

    def test_sent_actions_and_possible_actions_are_preserved(self) -> None:
        with TemporaryDirectory() as raw:
            record = _save(Path(raw))

        decisions = iter_ranked_decisions(record)
        self.assertEqual(decisions[0].sent_action, _sent_action(1))
        self.assertEqual(decisions[1].sent_action, _sent_action(2))
        self.assertEqual(decisions[0].possible_actions, ({"type": "none", "actor": 0},))


class ConsumerReadbackTest(unittest.TestCase):
    def test_decisions_expose_request_seat_observation_action_and_acks(self) -> None:
        with TemporaryDirectory() as raw:
            record = _save(Path(raw))

        decisions = iter_ranked_decisions(record)
        self.assertEqual([decision.request_id for decision in decisions], [1, 2])
        self.assertEqual([decision.seat for decision in decisions], [Seat.SEAT_0] * 2)
        self.assertEqual([int(d.observation.player_id) for d in decisions], [0, 0])
        self.assertEqual(decisions[0].ack_statuses, ("accepted",))
        self.assertEqual(decisions[1].ack_statuses, ("stale", "defaulted"))

    def test_readback_smoke_is_deterministic(self) -> None:
        with TemporaryDirectory() as raw:
            record = _save(Path(raw))
            first = summarize_ranked_game_record(record)
            second = summarize_ranked_game_record(
                load_ranked_game_record(Path(raw) / "record")
            )

        self.assertEqual(first, second)
        self.assertEqual(first.requests, 2)
        self.assertEqual(first.responses, 2)
        self.assertEqual(first.acknowledged_requests, 2)
        self.assertEqual(first.deserialized_observations, 2)
        self.assertEqual(first.seat, 0)
        self.assertEqual(first.scores, (32000, 24000, 23000, 21000))

    def test_undeserializable_observation_is_rejected_before_publish(self) -> None:
        entries = _completed_entries()
        entries[1] = ("recv", _request_action(1, observation="not-an-observation"))
        with TemporaryDirectory() as raw:
            directory = Path(raw)

            with self.assertRaises(DurableRankedGameRecordError):
                _save(directory, entries=entries)

            self.assertFalse((directory / "record").exists())
            self.assertEqual(
                [entry.name for entry in directory.iterdir()],
                ["staged-protocol.jsonl"],
            )

    def test_observation_for_another_seat_is_rejected_before_publish(self) -> None:
        """observation.player_idはbound seatと一致していなければならない。

        execution時点で`RiichiLabSeatAdapter`が強制しているinvariantを、
        completed recordのstrict readbackでも維持する。
        """
        entries = _completed_entries()
        entries[0] = ("recv", {"type": "start_game", "id": 2})
        with TemporaryDirectory() as raw:
            directory = Path(raw)

            with self.assertRaises(DurableRankedGameRecordError) as caught:
                _save(
                    directory,
                    entries=entries,
                    result=_completed_result(seat=Seat.SEAT_2),
                )

            self.assertIn("bound seat", str(caught.exception))
            self.assertFalse((directory / "record").exists())


class AckLifecycleTest(unittest.TestCase):
    def test_records_without_any_acknowledgement_are_accepted(self) -> None:
        entries = [
            entry
            for entry in _completed_entries()
            if entry[1].get("type") != "action_ack"
        ]
        with TemporaryDirectory() as raw:
            record = _save(
                Path(raw), entries=entries, result=_completed_result(ack_history={})
            )

        self.assertEqual(dict(record.result.ack_history), {})

    def test_fatal_acknowledgement_is_never_a_completed_record(self) -> None:
        entries = _completed_entries()
        entries[3] = (
            "recv",
            {"type": "action_ack", "request_id": 1, "status": "rejected"},
        )
        with TemporaryDirectory() as raw:
            directory = Path(raw)
            with self.assertRaises(DurableRankedGameRecordError):
                _save(
                    directory,
                    entries=entries,
                    result=_completed_result(
                        ack_history={1: ("rejected",), 2: ("stale", "defaulted")}
                    ),
                )
            self.assertFalse((directory / "record").exists())

    def test_acknowledgement_for_an_unknown_request_is_rejected(self) -> None:
        entries = _completed_entries()
        entries[3] = (
            "recv",
            {"type": "action_ack", "request_id": 99, "status": "accepted"},
        )
        with TemporaryDirectory() as raw:
            with self.assertRaises(DurableRankedGameRecordError):
                _save(Path(raw), entries=entries)

    def test_acknowledgement_history_must_match_the_ranked_result(self) -> None:
        with TemporaryDirectory() as raw:
            with self.assertRaises(DurableRankedGameRecordError):
                _save(Path(raw), result=_completed_result(ack_history={1: ("stale",)}))


class CorrelationTest(unittest.TestCase):
    def test_sent_action_must_reference_a_known_request(self) -> None:
        entries = _completed_entries()
        entries[2] = ("send", _sent_action(41))
        with TemporaryDirectory() as raw:
            with self.assertRaises(DurableRankedGameRecordError):
                _save(Path(raw), entries=entries)

    def test_request_ids_must_increase_monotonically(self) -> None:
        entries = _completed_entries()
        entries[4] = ("recv", _request_action(1, seed=8))
        with TemporaryDirectory() as raw:
            with self.assertRaises(DurableRankedGameRecordError):
                _save(Path(raw), entries=entries)

    def test_a_request_without_a_recorded_response_is_rejected(self) -> None:
        entries = [entry for entry in _completed_entries() if entry[0] != "send"]
        with TemporaryDirectory() as raw:
            with self.assertRaises(DurableRankedGameRecordError):
                _save(
                    Path(raw),
                    entries=entries,
                    result=_completed_result(responses_sent=0),
                )

    def test_request_count_mismatch_is_rejected(self) -> None:
        with TemporaryDirectory() as raw:
            directory = Path(raw)
            with self.assertRaises(DurableRankedGameRecordError):
                _save(directory, result=_completed_result(requests_received=3))
            self.assertFalse((directory / "record").exists())

    def test_response_count_mismatch_is_rejected(self) -> None:
        with TemporaryDirectory() as raw:
            with self.assertRaises(DurableRankedGameRecordError):
                _save(Path(raw), result=_completed_result(responses_sent=1))

    def test_bound_seat_mismatch_is_rejected(self) -> None:
        with TemporaryDirectory() as raw:
            with self.assertRaises(DurableRankedGameRecordError):
                _save(Path(raw), result=_completed_result(seat=Seat.SEAT_2))

    def test_end_game_scores_must_match_the_ranked_result(self) -> None:
        with TemporaryDirectory() as raw:
            with self.assertRaises(DurableRankedGameRecordError):
                _save(Path(raw), result=_completed_result(scores=None))


class CompletionBoundaryTest(unittest.TestCase):
    def test_partial_result_without_end_game_is_not_finalized(self) -> None:
        entries = [
            entry
            for entry in _completed_entries()
            if entry[1].get("type") != "end_game"
        ]
        with TemporaryDirectory() as raw:
            directory = Path(raw)
            with self.assertRaises(DurableRankedGameRecordError):
                _save(
                    directory,
                    entries=entries,
                    result=_completed_result(end_game_received=False, scores=None),
                )
            self.assertFalse((directory / "record").exists())

    def test_missing_end_game_in_the_trace_is_rejected(self) -> None:
        entries = [
            entry
            for entry in _completed_entries()
            if entry[1].get("type") != "end_game"
        ]
        with TemporaryDirectory() as raw:
            with self.assertRaises(DurableRankedGameRecordError):
                _save(Path(raw), entries=entries, result=_completed_result(scores=None))

    def test_events_after_end_game_are_rejected(self) -> None:
        entries = _completed_entries()
        entries.append(("recv", {"type": "start_game", "id": 0}))
        with TemporaryDirectory() as raw:
            with self.assertRaises(DurableRankedGameRecordError):
                _save(Path(raw), entries=entries)

    def test_a_second_appended_hanchan_is_not_one_record(self) -> None:
        entries = _completed_entries() + _completed_entries()
        with TemporaryDirectory() as raw:
            with self.assertRaises(DurableRankedGameRecordError):
                _save(
                    Path(raw),
                    entries=entries,
                    result=_completed_result(requests_received=4, responses_sent=4),
                )

    def test_empty_protocol_trace_is_rejected(self) -> None:
        with TemporaryDirectory() as raw:
            directory = Path(raw)
            trace_path = directory / "empty.jsonl"
            trace_path.write_text("", encoding="utf-8")
            with self.assertRaises(DurableRankedGameRecordError):
                save_ranked_game_record(
                    _completed_result(),
                    trace_path,
                    directory / "record",
                    provenance=_provenance(),
                )


class TamperedBundleTest(unittest.TestCase):
    def _bundle(self, directory: Path) -> Path:
        _save(directory)
        return directory / "record"

    def test_unsupported_schema_id_is_rejected(self) -> None:
        with TemporaryDirectory() as raw:
            bundle = self._bundle(Path(raw))
            manifest = _read_manifest(bundle)
            manifest["schema_id"] = "some-other-record"
            _write_manifest(bundle, manifest)

            with self.assertRaises(DurableRankedGameRecordError) as caught:
                load_ranked_game_record(bundle)
        self.assertIn("schema id", str(caught.exception))

    def test_unsupported_schema_version_is_rejected(self) -> None:
        with TemporaryDirectory() as raw:
            bundle = self._bundle(Path(raw))
            manifest = _read_manifest(bundle)
            manifest["schema_version"] = RANKED_GAME_RECORD_SCHEMA_VERSION + 1
            _write_manifest(bundle, manifest)

            with self.assertRaises(DurableRankedGameRecordError) as caught:
                load_ranked_game_record(bundle)
        self.assertIn("schema version", str(caught.exception))

    def test_non_completed_status_is_rejected(self) -> None:
        with TemporaryDirectory() as raw:
            bundle = self._bundle(Path(raw))
            manifest = _read_manifest(bundle)
            manifest["completion_status"] = "partial"
            _write_manifest(bundle, manifest)

            with self.assertRaises(DurableRankedGameRecordError):
                load_ranked_game_record(bundle)

    def test_protocol_trace_digest_mismatch_is_rejected(self) -> None:
        with TemporaryDirectory() as raw:
            bundle = self._bundle(Path(raw))
            path = bundle / PROTOCOL_TRACE_FILENAME
            path.write_text(
                path.read_text(encoding="utf-8").replace("32000", "31000"),
                encoding="utf-8",
                newline="\n",
            )

            with self.assertRaises(DurableRankedGameRecordError) as caught:
                load_ranked_game_record(bundle)
        self.assertIn("digest mismatch", str(caught.exception))

    def test_result_digest_mismatch_is_rejected(self) -> None:
        with TemporaryDirectory() as raw:
            bundle = self._bundle(Path(raw))
            document = json.loads((bundle / RESULT_FILENAME).read_text("utf-8"))
            document["requests_received"] = 3
            (bundle / RESULT_FILENAME).write_text(
                canonical_json_text(document), encoding="utf-8", newline="\n"
            )

            with self.assertRaises(DurableRankedGameRecordError) as caught:
                load_ranked_game_record(bundle)
        self.assertIn("digest mismatch", str(caught.exception))

    def test_record_identity_mismatch_is_rejected(self) -> None:
        with TemporaryDirectory() as raw:
            bundle = self._bundle(Path(raw))
            manifest = _read_manifest(bundle)
            manifest["provenance"]["policy_identity"] = "SomeOtherPolicy"
            _write_manifest(bundle, manifest)

            with self.assertRaises(DurableRankedGameRecordError) as caught:
                load_ranked_game_record(bundle)
        self.assertIn("record identity mismatch", str(caught.exception))

    def test_truncated_protocol_trace_is_rejected(self) -> None:
        with TemporaryDirectory() as raw:
            bundle = self._bundle(Path(raw))
            path = bundle / PROTOCOL_TRACE_FILENAME
            path.write_text(
                path.read_text(encoding="utf-8")[:-40], encoding="utf-8", newline="\n"
            )
            _repack(bundle)

            with self.assertRaises(DurableRankedGameRecordError):
                load_ranked_game_record(bundle)

    def test_corrupt_protocol_trace_json_is_rejected(self) -> None:
        with TemporaryDirectory() as raw:
            bundle = self._bundle(Path(raw))
            path = bundle / PROTOCOL_TRACE_FILENAME
            lines = path.read_text(encoding="utf-8").splitlines()
            lines[1] = "{not valid json"
            path.write_text(
                "".join(line + "\n" for line in lines), encoding="utf-8", newline="\n"
            )
            _repack(bundle)

            with self.assertRaises(DurableRankedGameRecordError):
                load_ranked_game_record(bundle)

    def test_corrupt_result_json_is_rejected(self) -> None:
        with TemporaryDirectory() as raw:
            bundle = self._bundle(Path(raw))
            (bundle / RESULT_FILENAME).write_text(
                "{not valid json", encoding="utf-8", newline="\n"
            )
            _repack(bundle)

            with self.assertRaises(DurableRankedGameRecordError):
                load_ranked_game_record(bundle)

    def test_tampered_trace_entry_shape_is_rejected(self) -> None:
        with TemporaryDirectory() as raw:
            bundle = self._bundle(Path(raw))
            path = bundle / PROTOCOL_TRACE_FILENAME
            lines = path.read_text(encoding="utf-8").splitlines()
            entry = json.loads(lines[1])
            entry["event_type"] = "end_game"
            lines[1] = json.dumps(entry)
            path.write_text(
                "".join(line + "\n" for line in lines), encoding="utf-8", newline="\n"
            )
            _repack(bundle)

            with self.assertRaises(DurableRankedGameRecordError):
                load_ranked_game_record(bundle)

    def _retamper_observation(self, bundle: Path, observation: object) -> None:
        path = bundle / PROTOCOL_TRACE_FILENAME
        lines = path.read_text(encoding="utf-8").splitlines()
        entry = json.loads(lines[1])
        entry["payload"]["observation"] = observation
        lines[1] = json.dumps(entry)
        path.write_text(
            "".join(line + "\n" for line in lines), encoding="utf-8", newline="\n"
        )
        _repack(bundle)

    def test_tampered_observation_is_rejected_by_the_loader(self) -> None:
        with TemporaryDirectory() as raw:
            bundle = self._bundle(Path(raw))
            self._retamper_observation(bundle, "not-an-observation")

            with self.assertRaises(DurableRankedGameRecordError) as caught:
                load_ranked_game_record(bundle)
        self.assertIn("canonical parser", str(caught.exception))

    def test_incomplete_bundle_is_rejected(self) -> None:
        with TemporaryDirectory() as raw:
            bundle = self._bundle(Path(raw))
            (bundle / RESULT_FILENAME).unlink()

            with self.assertRaises(DurableRankedGameRecordError):
                load_ranked_game_record(bundle)

    def test_extra_bundle_entry_is_rejected(self) -> None:
        with TemporaryDirectory() as raw:
            bundle = self._bundle(Path(raw))
            (bundle / "notes.txt").write_text("extra", encoding="utf-8")

            with self.assertRaises(DurableRankedGameRecordError):
                load_ranked_game_record(bundle)

    def test_missing_bundle_directory_is_rejected(self) -> None:
        with TemporaryDirectory() as raw:
            with self.assertRaises(DurableRankedGameRecordError):
                load_ranked_game_record(Path(raw) / "absent")


class ExistingTargetTest(unittest.TestCase):
    def test_existing_record_is_not_overwritten_or_appended(self) -> None:
        with TemporaryDirectory() as raw:
            directory = Path(raw)
            _save(directory)
            bundle = directory / "record"
            before = (bundle / PROTOCOL_TRACE_FILENAME).read_bytes()

            with self.assertRaises(FileExistsError):
                _save(directory)

            self.assertEqual((bundle / PROTOCOL_TRACE_FILENAME).read_bytes(), before)
            self.assertEqual(
                {entry.name for entry in bundle.iterdir()},
                {MANIFEST_FILENAME, PROTOCOL_TRACE_FILENAME, RESULT_FILENAME},
            )


def _ack(request_id: int, status: str, **extra: object) -> dict:
    return {
        "type": "action_ack",
        "request_id": request_id,
        "status": status,
        **extra,
    }


def _unanswered_entries(
    *acks_for_2: dict, ack_2_after_request_3: bool = False
) -> list[tuple[str, dict]]:
    """request 2だけresponseを送らなかった完走game。

    `acks_for_2`はrequest 2へのack。既定ではrequest 2の直後、
    `ack_2_after_request_3`なら次のrequest 3を受信した後に置く(遅着)。
    """
    late = [("recv", ack) for ack in acks_for_2]
    entries = [
        ("recv", {"type": "start_game", "id": 0}),
        ("recv", _request_action(1, seed=7)),
        ("send", _sent_action(1)),
        ("recv", _ack(1, "accepted")),
        ("recv", _request_action(2, seed=8)),
    ]
    if not ack_2_after_request_3:
        entries += late
    entries.append(("recv", _request_action(3, seed=9)))
    if ack_2_after_request_3:
        entries += late
    entries += [
        ("send", _sent_action(3)),
        ("recv", _ack(3, "accepted")),
        ("recv", {"type": "end_game", "scores": [32000, 24000, 23000, 21000]}),
    ]
    return entries


def _unanswered_result(
    statuses_for_2: tuple[str, ...] = ("defaulted",), **overrides: object
) -> RankedGameResult:
    ack_history = {1: ("accepted",), 3: ("accepted",)}
    if statuses_for_2:
        ack_history[2] = statuses_for_2
    values: dict[str, object] = {
        "requests_received": 3,
        "responses_sent": 2,
        "ack_history": ack_history,
        "unanswered_requests": {2: "local_cutoff"},
    }
    values.update(overrides)
    return _completed_result(**values)


# serverがdefault時にackへ含めるaction(protocol docs)。Policy出力ではない。
_DEFAULTED_ACK = _ack(2, "defaulted", action={"type": "dahai", "pai": "9s"})


class UnansweredRequestContractTest(unittest.TestCase):
    """意図的な未送信とserver evidenceの境界(Issue #421)。"""

    def test_unanswered_request_with_a_defaulted_ack_is_a_completed_record(
        self,
    ) -> None:
        with TemporaryDirectory() as raw:
            directory = Path(raw)
            _save(
                directory,
                entries=_unanswered_entries(_DEFAULTED_ACK),
                result=_unanswered_result(),
            )
            loaded = load_ranked_game_record(directory / "record")
            decisions = iter_ranked_decisions(loaded)
            summary = summarize_ranked_game_record(loaded)

        self.assertEqual({2: "local_cutoff"}, loaded.result.unanswered_requests)
        self.assertEqual([1, 2, 3], [decision.request_id for decision in decisions])
        answered, unanswered, later = decisions
        # The server default action is not presented as a sent action.
        self.assertIsNone(unanswered.sent_action)
        self.assertEqual("local_cutoff", unanswered.unanswered_reason)
        self.assertEqual(("defaulted",), unanswered.ack_statuses)
        self.assertEqual(_sent_action(1), answered.sent_action)
        self.assertIsNone(answered.unanswered_reason)
        self.assertEqual(_sent_action(3), later.sent_action)
        self.assertEqual(
            (3, 2, 1),
            (summary.requests, summary.responses, summary.unanswered_requests),
        )

    def test_defaulted_ack_arriving_after_the_next_request_is_accepted(
        self,
    ) -> None:
        with TemporaryDirectory() as raw:
            directory = Path(raw)
            _save(
                directory,
                entries=_unanswered_entries(_DEFAULTED_ACK, ack_2_after_request_3=True),
                result=_unanswered_result(),
            )
            loaded = load_ranked_game_record(directory / "record")
        self.assertEqual({2: "local_cutoff"}, loaded.result.unanswered_requests)

    def test_late_ack_reason_with_a_defaulted_ack_is_accepted(self) -> None:
        with TemporaryDirectory() as raw:
            directory = Path(raw)
            _save(
                directory,
                entries=_unanswered_entries(_DEFAULTED_ACK),
                result=_unanswered_result(unanswered_requests={2: "late_ack"}),
            )
            decisions = iter_ranked_decisions(
                load_ranked_game_record(directory / "record")
            )
        self.assertEqual("late_ack", decisions[1].unanswered_reason)

    def test_server_outcome_must_be_confirmed(self) -> None:
        cases = {
            # Local cutoff or the next request alone is not a server default.
            "no ack, superseded by the next request": ((), ()),
            # stale answers a reply; it never confirms a default.
            "stale only": ((_ack(2, "stale"),), ("stale",)),
            # A reply that was never sent cannot be accepted or discarded.
            "accepted without a reply": ((_ack(2, "accepted"),), ("accepted",)),
            "defaulted and stale without a reply": (
                (_DEFAULTED_ACK, _ack(2, "stale")),
                ("defaulted", "stale"),
            ),
        }
        for name, (acks, statuses) in cases.items():
            with self.subTest(name), TemporaryDirectory() as raw:
                directory = Path(raw)
                with self.assertRaises(DurableRankedGameRecordError):
                    _save(
                        directory,
                        entries=_unanswered_entries(*acks),
                        result=_unanswered_result(statuses),
                    )
                self.assertFalse((directory / "record").exists())

    def test_a_defaulted_ack_after_end_game_does_not_count(self) -> None:
        entries = _unanswered_entries()
        entries.append(("recv", _DEFAULTED_ACK))
        with TemporaryDirectory() as raw:
            with self.assertRaises(DurableRankedGameRecordError):
                _save(Path(raw), entries=entries, result=_unanswered_result())

    def test_missing_send_is_not_mistaken_for_an_intentional_unanswered_request(
        self,
    ) -> None:
        # The trace lacks a send line but the session never recorded a reason:
        # this is a record gap, even with a defaulted ack.
        with TemporaryDirectory() as raw:
            with self.assertRaises(DurableRankedGameRecordError):
                _save(
                    Path(raw),
                    entries=_unanswered_entries(_DEFAULTED_ACK),
                    result=_unanswered_result(unanswered_requests={}),
                )

    def test_inconsistent_unanswered_bookkeeping_is_rejected(self) -> None:
        answered_entries = _unanswered_entries(_DEFAULTED_ACK)
        answered_entries.insert(6, ("send", _sent_action(2)))
        cases = {
            "marked unanswered but sent": (
                answered_entries,
                _unanswered_result(responses_sent=3),
            ),
            "unknown request_id": (
                _unanswered_entries(_DEFAULTED_ACK),
                _unanswered_result(
                    unanswered_requests={2: "local_cutoff", 9: "local_cutoff"}
                ),
            ),
            "answered request marked unanswered": (
                _unanswered_entries(_DEFAULTED_ACK),
                _unanswered_result(
                    unanswered_requests={1: "local_cutoff", 2: "local_cutoff"}
                ),
            ),
            "unknown reason": (
                _unanswered_entries(_DEFAULTED_ACK),
                _unanswered_result(unanswered_requests={2: "server_default"}),
            ),
        }
        for name, (entries, result) in cases.items():
            with self.subTest(name), TemporaryDirectory() as raw:
                with self.assertRaises(DurableRankedGameRecordError):
                    _save(Path(raw), entries=entries, result=result)

    def test_duplicate_response_is_still_rejected(self) -> None:
        entries = _completed_entries()
        entries.insert(3, ("send", _sent_action(1)))
        with TemporaryDirectory() as raw:
            with self.assertRaises(DurableRankedGameRecordError):
                _save(Path(raw), entries=entries, result=_completed_result())

    def test_an_answered_late_request_keeps_its_sent_action(self) -> None:
        # Request 2 of the default fixture was sent and then acknowledged
        # stale / defaulted: it stays a sent action, never an unanswered one.
        with TemporaryDirectory() as raw:
            directory = Path(raw)
            _save(directory)
            decisions = iter_ranked_decisions(
                load_ranked_game_record(directory / "record")
            )
        self.assertEqual(_sent_action(2), decisions[1].sent_action)
        self.assertIsNone(decisions[1].unanswered_reason)
        self.assertEqual(("stale", "defaulted"), decisions[1].ack_statuses)


def _downgrade_to_v1(bundle: Path) -> None:
    """保存済みbundleを、#421以前のwriterが書いたv1 bundleへ書き換える。"""
    result = json.loads((bundle / RESULT_FILENAME).read_text(encoding="utf-8"))
    result.pop("unanswered_requests")
    (bundle / RESULT_FILENAME).write_text(
        canonical_json_text(result), encoding="utf-8", newline="\n"
    )
    manifest = _read_manifest(bundle)
    manifest["schema_version"] = 1
    _write_manifest(bundle, manifest)
    _repack(bundle)


class SchemaV1CompatibilityTest(unittest.TestCase):
    """v1 bundleは従来のv1規則のまま読める(Issue #421)。"""

    def test_v1_bundle_still_loads_with_its_own_identity(self) -> None:
        with TemporaryDirectory() as raw:
            bundle = Path(raw) / "record"
            _save(Path(raw))
            _downgrade_to_v1(bundle)
            identity = _read_manifest(bundle)["record_identity"]
            loaded = load_ranked_game_record(bundle)
            decisions = iter_ranked_decisions(loaded)

        self.assertEqual(identity, loaded.record_identity)
        self.assertEqual({}, loaded.result.unanswered_requests)
        self.assertTrue(all(decision.sent_action for decision in decisions))

    def test_v1_rules_are_not_relaxed(self) -> None:
        # A v1 bundle with an unanswered request stays invalid, even with a
        # defaulted acknowledgement.
        with TemporaryDirectory() as raw:
            bundle = Path(raw) / "record"
            _save(
                Path(raw),
                entries=_unanswered_entries(_DEFAULTED_ACK),
                result=_unanswered_result(),
            )
            _downgrade_to_v1(bundle)
            with self.assertRaises(DurableRankedGameRecordError):
                load_ranked_game_record(bundle)

    def test_v1_result_must_not_carry_v2_fields(self) -> None:
        with TemporaryDirectory() as raw:
            bundle = Path(raw) / "record"
            _save(Path(raw))
            manifest = _read_manifest(bundle)
            manifest["schema_version"] = 1
            _write_manifest(bundle, manifest)
            _repack(bundle)
            with self.assertRaises(DurableRankedGameRecordError):
                load_ranked_game_record(bundle)


class _FakeTransport:
    """`Transport` protocolのtest double。tokenもAuthorization headerも持たない。"""

    def __init__(self, incoming: list[str]) -> None:
        self._incoming = list(incoming)
        self.sent: list[str] = []

    async def recv(self) -> str:
        if not self._incoming:
            raise TransportClosed("no more fake messages queued")
        return self._incoming.pop(0)

    async def send(self, message: str) -> None:
        self.sent.append(message)

    async def close(self) -> None:
        return None


def _fake_connect(transport: _FakeTransport):
    @asynccontextmanager
    async def _connect(url: str, token: str):
        yield transport

    return _connect


def _dahai_possible_actions(observation) -> list[dict]:
    return [
        {"type": "dahai", "pai": tile_to_mjai(tile_from_physical_id(tile))}
        for tile in sorted(set(observation.hand))
    ]


def _live_session_messages() -> list[str]:
    """real Observation / real possible_actionsを使うsynthetic ranked session。"""
    env = RiichiEnv(seed=7, game_mode="4p-red-east")
    observation = next(iter(env.reset().values()))
    return [
        json.dumps({"type": "start_game", "id": 0}),
        json.dumps(
            {
                "type": "request_action",
                "request_id": 1,
                "possible_actions": _dahai_possible_actions(observation),
                "observation": observation.serialize_to_base64(),
                # 実Policy(lisjong-dev profile)の初回decisionでもserver
                # deadline内に収まる予算。deadline切れのrequestにはresponseを
                # 送らない(Issue #418)。
                "time": {"grace_ms": 500, "bank_ms": 600000},
            }
        ),
        json.dumps({"type": "action_ack", "request_id": 1, "status": "accepted"}),
        json.dumps({"type": "end_game", "scores": [25000, 25000, 25000, 25000]}),
    ]


class AcquisitionIntegrationTest(unittest.TestCase):
    def _acquire(self, messages: list[str], destination: Path):
        transport = _FakeTransport(messages)
        with patch(
            "lisjong_arena.riichilab.ranked.connect_ranked_transport",
            _fake_connect(transport),
        ):
            return asyncio.run(
                acquire_ranked_game_record(
                    MinimalPolicy(),
                    _TOKEN,
                    destination=destination,
                    profile_identity="lisjong-dev",
                )
            )

    def test_synthetic_ranked_session_produces_a_loadable_record(self) -> None:
        with TemporaryDirectory() as raw:
            directory = Path(raw)
            acquired = self._acquire(_live_session_messages(), directory / "record")
            loaded = load_ranked_game_record(directory / "record")
            summary = summarize_ranked_game_record(loaded)

        self.assertEqual(acquired.record_identity, loaded.record_identity)
        self.assertTrue(loaded.result.end_game_received)
        self.assertEqual(loaded.result.seat, Seat.SEAT_0)
        self.assertEqual(summary.requests, 1)
        self.assertEqual(summary.responses, 1)
        self.assertEqual(summary.deserialized_observations, 1)
        self.assertEqual(loaded.provenance.profile_identity, "lisjong-dev")
        self.assertEqual(loaded.provenance.policy_identity, "MinimalPolicy")
        self.assertEqual(loaded.provenance.riichienv_version, "0.4.10")

    def test_acquisition_uses_a_fresh_trace_and_leaves_no_staging(self) -> None:
        with TemporaryDirectory() as raw:
            directory = Path(raw)
            self._acquire(_live_session_messages(), directory / "record")
            entries = sorted(entry.name for entry in directory.iterdir())

        self.assertEqual(entries, ["record"])

    def test_unexpected_disconnect_does_not_finalize_a_record(self) -> None:
        messages = _live_session_messages()[:-1]
        with TemporaryDirectory() as raw:
            directory = Path(raw)
            with self.assertRaises(UnexpectedDisconnectError):
                self._acquire(messages, directory / "record")

            self.assertEqual(list(directory.iterdir()), [])

    def test_protocol_failure_does_not_finalize_a_record(self) -> None:
        messages = _live_session_messages()
        messages[2] = json.dumps(
            {"type": "action_ack", "request_id": 1, "status": "rejected"}
        )
        with TemporaryDirectory() as raw:
            directory = Path(raw)
            with self.assertRaises(ProtocolError):
                self._acquire(messages, directory / "record")

            self.assertEqual(list(directory.iterdir()), [])

    def _assert_trace_failure_is_not_finalized(self, writer_class) -> None:
        with TemporaryDirectory() as raw:
            directory = Path(raw)
            with patch(
                "lisjong_arena.riichilab.ranked.JsonlProtocolTraceWriter",
                writer_class,
            ):
                with self.assertRaises(ProtocolTraceError):
                    self._acquire(_live_session_messages(), directory / "record")

            self.assertEqual(list(directory.iterdir()), [])

    def test_trace_close_failure_does_not_finalize_a_record(self) -> None:
        class _FailingCloseWriter:
            def __init__(self, path) -> None:
                self.path = path

            def record(self, direction, event_type, payload) -> None:
                return None

            def close(self) -> None:
                raise ProtocolTraceError("failed to close protocol trace file")

        self._assert_trace_failure_is_not_finalized(_FailingCloseWriter)

    def test_trace_write_failure_does_not_finalize_a_record(self) -> None:
        class _FailingWriteWriter:
            def __init__(self, path) -> None:
                self.path = path

            def record(self, direction, event_type, payload) -> None:
                raise ProtocolTraceError("failed to write protocol trace record")

            def close(self) -> None:
                return None

        self._assert_trace_failure_is_not_finalized(_FailingWriteWriter)

    def test_existing_destination_fails_before_connecting(self) -> None:
        with TemporaryDirectory() as raw:
            directory = Path(raw)
            (directory / "record").mkdir()
            transport = _FakeTransport(_live_session_messages())

            with patch(
                "lisjong_arena.riichilab.ranked.connect_ranked_transport",
                _fake_connect(transport),
            ):
                with self.assertRaises(FileExistsError):
                    asyncio.run(
                        acquire_ranked_game_record(
                            MinimalPolicy(), _TOKEN, destination=directory / "record"
                        )
                    )

            self.assertEqual(transport.sent, [])

    def test_no_credential_material_reaches_the_record(self) -> None:
        with TemporaryDirectory() as raw:
            directory = Path(raw)
            self._acquire(_live_session_messages(), directory / "record")
            blobs = [path.read_bytes() for path in (directory / "record").iterdir()]

        for blob in blobs:
            text = blob.decode("utf-8")
            self.assertNotIn(_TOKEN, text)
            self.assertNotIn("Authorization", text)
            self.assertNotIn("Bearer", text)
            self.assertNotIn("BOT_TOKEN", text)


class RankedRecordCliTest(unittest.TestCase):
    def setUp(self) -> None:
        self._saved = {name: os.environ.get(name) for name in _ALL_TOKEN_VARS}
        self._saved[_TRACE_PATH_VAR] = os.environ.get(_TRACE_PATH_VAR)
        for name in (*_ALL_TOKEN_VARS, _TRACE_PATH_VAR):
            os.environ.pop(name, None)

    def tearDown(self) -> None:
        for name, value in self._saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value

    def test_record_dir_is_ranked_only(self) -> None:
        ranked = build_arg_parser(prog="ranked", ranked_record=True)
        shared = build_arg_parser(prog="validation")

        self.assertIn("--record-dir", ranked.format_help())
        self.assertNotIn("--record-dir", shared.format_help())

    def test_resolve_ranked_record_path_is_optional_and_unique(self) -> None:
        self.assertIsNone(resolve_ranked_record_path(None))
        self.assertIsNone(resolve_ranked_record_path(""))
        first = resolve_ranked_record_path("/tmp/records")
        second = resolve_ranked_record_path("/tmp/records")

        self.assertEqual(first.parent, Path("/tmp/records"))
        self.assertNotEqual(first, second)

    def test_record_dir_and_diagnostic_trace_are_mutually_exclusive(self) -> None:
        os.environ[_TOKEN_VAR] = _TOKEN
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            exit_code = _run_cli(
                ["--profile", "lisjong-dev", "--record-dir", "/tmp/records", "--trace"]
            )

        self.assertEqual(exit_code, 2)
        self.assertIn("--record-dir", stderr.getvalue())

    def test_existing_trace_behavior_is_unchanged_without_record_dir(self) -> None:
        os.environ[_TOKEN_VAR] = _TOKEN
        captured: dict[str, object] = {}

        async def _fake_run_ranked_game(policy, token, **kwargs):
            captured.update(kwargs)
            return _completed_result()

        stdout = io.StringIO()
        with TemporaryDirectory() as raw:
            trace_path = str(Path(raw) / "diagnostic.jsonl")
            with patch(
                "lisjong_arena.riichilab.ranked.run_ranked_game", _fake_run_ranked_game
            ):
                with contextlib.redirect_stdout(stdout):
                    exit_code = _run_cli(
                        ["--profile", "lisjong-dev", "--trace-path", trace_path]
                    )

            self.assertEqual(exit_code, 0)
            self.assertEqual(captured["trace_path"], trace_path)
        self.assertIn("record: off", stdout.getvalue())

    def test_record_dir_run_reports_the_record_identity(self) -> None:
        os.environ[_TOKEN_VAR] = _TOKEN
        stdout = io.StringIO()
        with TemporaryDirectory() as raw:
            directory = Path(raw)
            transport = _FakeTransport(_live_session_messages())
            with patch(
                "lisjong_arena.riichilab.ranked.connect_ranked_transport",
                _fake_connect(transport),
            ):
                with contextlib.redirect_stdout(stdout):
                    exit_code = _run_cli(
                        ["--profile", "lisjong-dev", "--record-dir", str(directory)]
                    )

            bundles = [entry for entry in directory.iterdir() if entry.is_dir()]
            self.assertEqual(len(bundles), 1)
            record = load_ranked_game_record(bundles[0])

        output = stdout.getvalue()
        self.assertEqual(exit_code, 0)
        self.assertIn("record: on", output)
        self.assertIn(f"record identity: {record.record_identity}", output)
        self.assertNotIn(_TOKEN, output)

    def test_failed_record_acquisition_exits_non_zero(self) -> None:
        os.environ[_TOKEN_VAR] = _TOKEN
        stderr = io.StringIO()
        with TemporaryDirectory() as raw:
            directory = Path(raw)

            async def _failing(*args, **kwargs):
                raise DurableRankedGameRecordError("strict readback failed")

            with patch(
                "lisjong_arena.riichilab.durable_ranked_game_record"
                ".acquire_ranked_game_record",
                _failing,
            ):
                with contextlib.redirect_stdout(io.StringIO()):
                    with contextlib.redirect_stderr(stderr):
                        exit_code = _run_cli(
                            ["--profile", "lisjong-dev", "--record-dir", str(directory)]
                        )

        self.assertEqual(exit_code, 1)
        self.assertIn("was not finalized", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
