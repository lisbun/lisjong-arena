"""Per-seat Policy decision recording and its semantic digest (#398 / #400).

Shared by ``scripts/capture_policy_decisions.py`` and the #400 shanten backend
measurement, so the digest that identifies a decision sequence has one
definition.  Development-only: this is not a durable game record.

``semantic_digest()`` hashes, seat by seat in decision order,
``repr((decision, action))``.  It ignores the recording order of several seats
inside one step, which can differ between runs even when every decision is
identical.
"""

from __future__ import annotations

import hashlib


class RecordingPolicy:
    """Delegate to the wrapped Policy and record its input and output."""

    def __init__(self, inner, sink: list) -> None:
        self._inner = inner
        self._sink = sink

    def choose_action(self, decision):
        action = self._inner.choose_action(decision)
        self._sink.append((decision, action))
        return action


def _lines_by_seat(records) -> dict[int, list[str]]:
    by_seat: dict[int, list[str]] = {}
    for decision, action in records:
        by_seat.setdefault(int(decision.input.self_seat), []).append(
            repr((decision, action))
        )
    return by_seat


def semantic_digest(records) -> str:
    """Seat-by-seat digest that ignores the within-step recording order."""
    by_seat = _lines_by_seat(records)
    digest = hashlib.sha256()
    for seat in sorted(by_seat):
        for line in by_seat[seat]:
            digest.update(f"{seat}\t{line}\n".encode("utf-8"))
    return digest.hexdigest()


def seat_digests(records) -> dict[int, dict[str, object]]:
    """Per-seat decision count and digest, for locating a mismatching seat."""
    result: dict[int, dict[str, object]] = {}
    for seat, lines in sorted(_lines_by_seat(records).items()):
        digest = hashlib.sha256()
        for line in lines:
            digest.update(f"{line}\n".encode("utf-8"))
        result[seat] = {"decisions": len(lines), "sha256": digest.hexdigest()}
    return result


__all__ = ["RecordingPolicy", "seat_digests", "semantic_digest"]
