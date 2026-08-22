"""`JsonlProtocolTraceWriter`のunit test(Arena-local canonical, Issue #23)。

JSONL record生成、timestamp/direction/event_type/payload、複数recordの
1行1JSON読み戻し、書き込み・open失敗をsilentに無視しないことを、実
WebSocket/RiichiLab接続なしに確認する。`ProtocolTraceError`の継承関係は
`test_riichilab_errors.py`が担当する。
"""

import json
import os
import tempfile
import unittest
from datetime import datetime

from lisjong_arena.riichilab.trace import JsonlProtocolTraceWriter, ProtocolTraceError


class JsonlProtocolTraceWriterTest(unittest.TestCase):
    def test_record_writes_a_single_valid_json_line(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            path = os.path.join(tmp_dir, "trace.jsonl")
            writer = JsonlProtocolTraceWriter(path)
            try:
                writer.record("recv", "start_game", {"type": "start_game", "id": 0})
            finally:
                writer.close()

            with open(path, encoding="utf-8") as trace_file:
                lines = trace_file.readlines()

            self.assertEqual(len(lines), 1)
            record = json.loads(lines[0])
            self.assertEqual(record["direction"], "recv")
            self.assertEqual(record["event_type"], "start_game")
            self.assertEqual(record["payload"], {"type": "start_game", "id": 0})

    def test_timestamp_is_a_timezone_aware_iso_8601_value(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            path = os.path.join(tmp_dir, "trace.jsonl")
            writer = JsonlProtocolTraceWriter(path)
            try:
                writer.record("recv", "start_game", {"type": "start_game", "id": 0})
            finally:
                writer.close()

            with open(path, encoding="utf-8") as trace_file:
                record = json.loads(trace_file.readline())

            parsed_timestamp = datetime.fromisoformat(record["timestamp"])
            self.assertIsNotNone(parsed_timestamp.tzinfo)
            self.assertEqual(parsed_timestamp.utcoffset().total_seconds(), 0)

    def test_multiple_records_read_back_as_one_json_object_per_line_in_order(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            path = os.path.join(tmp_dir, "trace.jsonl")
            writer = JsonlProtocolTraceWriter(path)
            try:
                writer.record("recv", "start_game", {"type": "start_game", "id": 0})
                writer.record(
                    "recv",
                    "request_action",
                    {"type": "request_action", "request_id": 1},
                )
                writer.record(
                    "send",
                    "dahai",
                    {"type": "dahai", "actor": 0, "pai": "1m", "request_id": 1},
                )
            finally:
                writer.close()

            with open(path, encoding="utf-8") as trace_file:
                lines = trace_file.readlines()

            self.assertEqual(len(lines), 3)
            records = [json.loads(line) for line in lines]
            self.assertEqual(
                [record["direction"] for record in records],
                ["recv", "recv", "send"],
            )
            self.assertEqual(
                [record["event_type"] for record in records],
                ["start_game", "request_action", "dahai"],
            )
            for line in lines:
                # 各行が独立したvalid JSONであることを明示的に確認する。
                json.loads(line)

    def test_creates_missing_parent_directories(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            path = os.path.join(tmp_dir, "nested", "trace-dir", "trace.jsonl")
            writer = JsonlProtocolTraceWriter(path)
            try:
                writer.record("recv", "start_game", {"type": "start_game", "id": 0})
            finally:
                writer.close()

            self.assertTrue(os.path.isfile(path))

    def test_open_failure_raises_protocol_trace_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            # 既存の通常fileをdirectoryとして扱おうとするため、openに失敗する。
            blocking_file = os.path.join(tmp_dir, "blocking")
            with open(blocking_file, "w", encoding="utf-8") as handle:
                handle.write("not a directory")
            path = os.path.join(blocking_file, "trace.jsonl")

            with self.assertRaises(ProtocolTraceError):
                JsonlProtocolTraceWriter(path)

    def test_write_failure_is_not_silently_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            path = os.path.join(tmp_dir, "trace.jsonl")
            writer = JsonlProtocolTraceWriter(path)

            def _raise_os_error(*_args, **_kwargs) -> None:
                raise OSError("disk full (simulated)")

            writer._file.write = _raise_os_error  # 実disk書き込み失敗を模擬する

            with self.assertRaises(ProtocolTraceError):
                writer.record("recv", "start_game", {"type": "start_game", "id": 0})

            writer._file.write = lambda *_args, **_kwargs: None
            writer.close()

    def test_constructor_accepts_only_a_path(self) -> None:
        # tokenやAuthorization headerを受け取れるAPIを追加しないことを、
        # constructorがpath以外の引数を受け付けないことで固定する。
        with tempfile.TemporaryDirectory() as tmp_dir:
            path = os.path.join(tmp_dir, "trace.jsonl")
            writer = JsonlProtocolTraceWriter(path)
            try:
                with self.assertRaises(TypeError):
                    JsonlProtocolTraceWriter(path, token="should-not-be-accepted")
            finally:
                writer.close()


if __name__ == "__main__":
    unittest.main()
