"""Arena-local RiichiLab lower-level runtime error hierarchy test(Issue #23)。

`lisjong_arena.riichilab.errors`のcanonical hierarchyと、`trace.py`が定義する
`ProtocolTraceError`がこのhierarchyへ合流していることを確認する。lisjong側
`RiichiLabSeatAdapter`が送出する例外をこれらへwrapしないことは、
`test_riichilab_session.py`のAdapter exception passthrough testが担当する。
"""

import unittest

from lisjong_arena.riichilab.errors import (
    ProtocolError,
    RiichiLabClientError,
    TransportError,
    UnexpectedDisconnectError,
)
from lisjong_arena.riichilab.trace import ProtocolTraceError


class ErrorHierarchyTest(unittest.TestCase):
    def test_protocol_error_is_a_riichilab_client_error(self) -> None:
        self.assertTrue(issubclass(ProtocolError, RiichiLabClientError))

    def test_transport_error_is_a_riichilab_client_error(self) -> None:
        self.assertTrue(issubclass(TransportError, RiichiLabClientError))

    def test_unexpected_disconnect_error_is_a_transport_error(self) -> None:
        self.assertTrue(issubclass(UnexpectedDisconnectError, TransportError))

    def test_unexpected_disconnect_error_is_not_a_direct_protocol_error(self) -> None:
        self.assertFalse(issubclass(UnexpectedDisconnectError, ProtocolError))

    def test_protocol_trace_error_is_a_riichilab_client_error(self) -> None:
        self.assertTrue(issubclass(ProtocolTraceError, RiichiLabClientError))

    def test_protocol_trace_error_is_not_a_protocol_or_transport_error(self) -> None:
        self.assertFalse(issubclass(ProtocolTraceError, ProtocolError))
        self.assertFalse(issubclass(ProtocolTraceError, TransportError))

    def test_all_client_errors_are_arena_local(self) -> None:
        for error_class in (
            RiichiLabClientError,
            ProtocolError,
            TransportError,
            UnexpectedDisconnectError,
            ProtocolTraceError,
        ):
            with self.subTest(error_class=error_class):
                self.assertTrue(
                    error_class.__module__.startswith("lisjong_arena.riichilab"),
                    f"{error_class!r} is not Arena-local: {error_class.__module__}",
                )


if __name__ == "__main__":
    unittest.main()
