"""No-network bounded HTTP status, timeout, connection and retry tests."""

from __future__ import annotations

import unittest
import urllib.error

from lisjong_arena.riichilab_corpus.http import (
    HttpResponse,
    HttpTransportError,
    get_with_bounded_retry,
)


class FakeTransport:
    def __init__(self, outcomes: list[object]):
        self.outcomes = list(outcomes)
        self.calls = []

    def get(self, url: str, *, timeout: float) -> HttpResponse:
        self.calls.append((url, timeout))
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


class BoundedHttpTest(unittest.TestCase):
    def test_success_and_empty_success(self) -> None:
        transport = FakeTransport([HttpResponse(200, {}, b"ok")])
        self.assertEqual(
            get_with_bounded_retry(transport, "https://example.test", timeout=2).body,
            b"ok",
        )
        with self.assertRaisesRegex(HttpTransportError, "empty"):
            get_with_bounded_retry(
                FakeTransport([HttpResponse(200, {}, b"")]), "https://example.test"
            )

    def test_401_403_404_are_not_retried(self) -> None:
        for status in (401, 403, 404):
            transport = FakeTransport([HttpResponse(status, {}, b"denied")])
            with self.subTest(status=status):
                with self.assertRaisesRegex(HttpTransportError, str(status)):
                    get_with_bounded_retry(transport, "https://example.test")
                self.assertEqual(len(transport.calls), 1)

    def test_429_and_5xx_have_bounded_exponential_backoff(self) -> None:
        transport = FakeTransport(
            [
                HttpResponse(429, {}, b"slow"),
                HttpResponse(503, {}, b"down"),
                HttpResponse(200, {}, b"ok"),
            ]
        )
        delays = []
        response = get_with_bounded_retry(
            transport,
            "https://example.test",
            backoff_seconds=0.25,
            sleeper=delays.append,
        )
        self.assertEqual(response.body, b"ok")
        self.assertEqual(delays, [0.25, 0.5])
        self.assertEqual(len(transport.calls), 3)

    def test_timeout_connection_failure_and_retry_exhaustion(self) -> None:
        for failure in (TimeoutError("late"), urllib.error.URLError("offline")):
            transport = FakeTransport([failure, failure, failure])
            delays = []
            with self.subTest(failure=type(failure).__name__):
                with self.assertRaisesRegex(HttpTransportError, "3 bounded attempts"):
                    get_with_bounded_retry(
                        transport,
                        "https://example.test",
                        backoff_seconds=0,
                        sleeper=delays.append,
                    )
                self.assertEqual(len(transport.calls), 3)
                self.assertEqual(delays, [0.0, 0.0])


if __name__ == "__main__":
    unittest.main()
