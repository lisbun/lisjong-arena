"""``lisjong_arena.game_trace``のcontract test。

Issue #43でlisjong ``tests/test_game_trace.py``(exact pin
``3505321b62e7a2be204cc555924b485a898c8f31``)からbehavior-preservingに
re-homeした。カバレッジは欠落なく移した。
"""

import json
import unittest
from dataclasses import FrozenInstanceError

from lisjong_arena.game_trace import (
    GameTrace,
    GameTraceEvent,
    GameTraceLifecycleError,
    GameTraceRecorder,
)


def _event(sequence: int, event_type: str = "start_game") -> GameTraceEvent:
    return GameTraceEvent(
        sequence=sequence,
        event=json.dumps({"type": event_type}, separators=(",", ":")),
    )


class GameTraceEventTest(unittest.TestCase):
    def test_accepts_detached_json_object_text(self) -> None:
        event = _event(0)

        self.assertEqual(event.sequence, 0)
        self.assertEqual(json.loads(event.event), {"type": "start_game"})

    def test_rejects_invalid_sequence_or_payload(self) -> None:
        invalid_values = (
            {"sequence": True, "event": "{}"},
            {"sequence": -1, "event": "{}"},
            {"sequence": 0, "event": {}},
            {"sequence": 0, "event": ""},
            {"sequence": 0, "event": "not-json"},
            {"sequence": 0, "event": "[]"},
        )

        for values in invalid_values:
            with (
                self.subTest(values=values),
                self.assertRaises((TypeError, ValueError)),
            ):
                GameTraceEvent(**values)

    def test_is_frozen_and_does_not_alias_decoded_consumer_data(self) -> None:
        event = GameTraceEvent(
            sequence=0,
            event='{"type":"hora","scores":[25000,25000,25000,25000]}',
        )
        decoded = json.loads(event.event)
        decoded["scores"][0] = 99999

        self.assertEqual(json.loads(event.event)["scores"][0], 25000)
        with self.assertRaises(FrozenInstanceError):
            event.sequence = 1


class GameTraceTest(unittest.TestCase):
    def test_normalizes_events_to_tuple(self) -> None:
        source_events = [_event(0)]
        trace = GameTrace(
            seed=7,
            game_mode="4p-red-half",
            events=source_events,
        )
        source_events.append(_event(1))

        self.assertIsInstance(trace.events, tuple)
        self.assertEqual(trace.events, (_event(0),))

    def test_rejects_invalid_metadata_events_or_sequences(self) -> None:
        invalid_values = (
            {"seed": True, "game_mode": "4p-red-half", "events": [_event(0)]},
            {"seed": 7, "game_mode": 1, "events": [_event(0)]},
            {"seed": 7, "game_mode": "", "events": [_event(0)]},
            {"seed": 7, "game_mode": "4p-red-half", "events": []},
            {"seed": 7, "game_mode": "4p-red-half", "events": [object()]},
            {"seed": 7, "game_mode": "4p-red-half", "events": [_event(1)]},
            {
                "seed": 7,
                "game_mode": "4p-red-half",
                "events": [_event(0), _event(2)],
            },
        )

        for values in invalid_values:
            with (
                self.subTest(values=values),
                self.assertRaises((TypeError, ValueError)),
            ):
                GameTrace(**values)


class GameTraceRecorderTest(unittest.TestCase):
    def test_records_one_completed_trace(self) -> None:
        recorder = GameTraceRecorder()
        recorder.on_start(seed=7, game_mode="4p-red-half")
        recorder.on_event(_event(0))
        recorder.on_event(_event(1, "end_game"))
        recorder.on_complete()

        trace = recorder.snapshot()
        self.assertEqual(trace.seed, 7)
        self.assertEqual(trace.game_mode, "4p-red-half")
        self.assertEqual(trace.events, (_event(0), _event(1, "end_game")))
        self.assertIs(trace, recorder.snapshot())

    def test_rejects_event_before_start(self) -> None:
        recorder = GameTraceRecorder()

        with self.assertRaisesRegex(GameTraceLifecycleError, "before start"):
            recorder.on_event(_event(0))

    def test_rejects_duplicate_start(self) -> None:
        recorder = GameTraceRecorder()
        recorder.on_start(seed=7, game_mode="4p-red-half")

        with self.assertRaisesRegex(GameTraceLifecycleError, "already been started"):
            recorder.on_start(seed=7, game_mode="4p-red-half")

    def test_rejects_complete_before_start(self) -> None:
        recorder = GameTraceRecorder()

        with self.assertRaisesRegex(GameTraceLifecycleError, "before start"):
            recorder.on_complete()

    def test_rejects_duplicate_complete(self) -> None:
        recorder = GameTraceRecorder()
        recorder.on_start(seed=7, game_mode="4p-red-half")
        recorder.on_event(_event(0))
        recorder.on_complete()

        with self.assertRaisesRegex(GameTraceLifecycleError, "already been completed"):
            recorder.on_complete()

    def test_rejects_event_after_complete(self) -> None:
        recorder = GameTraceRecorder()
        recorder.on_start(seed=7, game_mode="4p-red-half")
        recorder.on_event(_event(0))
        recorder.on_complete()

        with self.assertRaisesRegex(GameTraceLifecycleError, "after completion"):
            recorder.on_event(_event(1))

    def test_rejects_snapshot_before_complete(self) -> None:
        recorder = GameTraceRecorder()
        with self.assertRaisesRegex(GameTraceLifecycleError, "only after"):
            recorder.snapshot()

        recorder.on_start(seed=7, game_mode="4p-red-half")
        recorder.on_event(_event(0))
        with self.assertRaisesRegex(GameTraceLifecycleError, "only after"):
            recorder.snapshot()

    def test_rejects_non_contiguous_event_sequence(self) -> None:
        recorder = GameTraceRecorder()
        recorder.on_start(seed=7, game_mode="4p-red-half")

        with self.assertRaisesRegex(GameTraceLifecycleError, "contiguous"):
            recorder.on_event(_event(1))

    def test_does_not_complete_an_empty_trace(self) -> None:
        recorder = GameTraceRecorder()
        recorder.on_start(seed=7, game_mode="4p-red-half")

        with self.assertRaisesRegex(ValueError, "must not be empty"):
            recorder.on_complete()
        with self.assertRaises(GameTraceLifecycleError):
            recorder.snapshot()


if __name__ == "__main__":
    unittest.main()
