"""Strict transport/encoding/MJAI validation and hidden-information diagnostics."""

from __future__ import annotations

import gzip
from collections import Counter
from dataclasses import dataclass

from lisjong_arena.riichilab_corpus.models import (
    CorpusError,
    Participation,
    strict_json_loads,
)

_MASKED_TILES = {"?", "??", "_", "masked", "unknown"}
_ROUND_ACTIONS = {
    "tsumo",
    "dahai",
    "chi",
    "pon",
    "daiminkan",
    "ankan",
    "kakan",
    "reach",
    "reach_accepted",
    "dora",
    "hora",
    "ryukyoku",
}
_TERMINAL_EVENTS = {"hora", "ryukyoku"}


@dataclass(frozen=True, slots=True)
class HiddenInformationCoverage:
    round_count: int
    all_seat_initial_hand_rounds: int
    actual_tsumo_count: int
    masked_tsumo_count: int
    missing_tsumo_count: int
    reconstructable_rounds: int

    def to_value(self) -> dict[str, int]:
        return {
            "actual_tsumo_count": self.actual_tsumo_count,
            "all_seat_initial_hand_rounds": self.all_seat_initial_hand_rounds,
            "masked_tsumo_count": self.masked_tsumo_count,
            "missing_tsumo_count": self.missing_tsumo_count,
            "reconstructable_rounds": self.reconstructable_rounds,
            "round_count": self.round_count,
        }


@dataclass(frozen=True, slots=True)
class ValidationResult:
    event_count: int
    lifecycle_valid: bool
    hidden_information: HiddenInformationCoverage

    def to_value(self) -> dict[str, object]:
        return {
            "event_count": self.event_count,
            "hidden_information": self.hidden_information.to_value(),
            "lifecycle_valid": self.lifecycle_valid,
        }


def _actual_tile(value: object) -> bool:
    return type(value) is str and bool(value) and value.lower() not in _MASKED_TILES


def _actor(event: dict[str, object], context: str) -> int:
    value = event.get("actor")
    if type(value) is not int or not 0 <= value <= 3:
        raise CorpusError(f"{context} actor must be an integer from 0 through 3")
    return value


def _remove_tiles(hand: Counter[str] | None, values: object) -> bool:
    if (
        hand is None
        or type(values) is not list
        or not all(_actual_tile(x) for x in values)
    ):
        return False
    required = Counter(values)
    if any(hand[tile] < count for tile, count in required.items()):
        return False
    hand.subtract(required)
    return True


def parse_jsonl_gzip(payload: bytes) -> tuple[dict[str, object], ...]:
    if not payload:
        raise CorpusError("compressed payload is empty")
    try:
        decoded = gzip.decompress(payload)
    except (gzip.BadGzipFile, EOFError, OSError) as exc:
        raise CorpusError("payload is not an intact gzip stream") from exc
    if not decoded:
        raise CorpusError("gzip payload is empty")
    try:
        text = decoded.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise CorpusError("gzip content is not UTF-8") from exc
    lines = text.splitlines()
    if not lines or any(not line.strip() for line in lines):
        raise CorpusError("MJAI JSONL contains an empty line")
    events = []
    for index, line in enumerate(lines):
        value = strict_json_loads(line.encode("utf-8"), f"MJAI line {index + 1}")
        if type(value) is not dict:
            raise CorpusError(f"MJAI line {index + 1} is not an object")
        event_type = value.get("type")
        if type(event_type) is not str or not event_type:
            raise CorpusError(f"MJAI line {index + 1} has no event type")
        events.append(value)
    return tuple(events)


def validate_mjai_gzip(
    payload: bytes,
    *,
    participations: tuple[Participation, ...],
) -> ValidationResult:
    events = parse_jsonl_gzip(payload)
    if events[0]["type"] != "start_game" or events[-1]["type"] != "end_game":
        raise CorpusError("MJAI game must start with start_game and end with end_game")
    if sum(event["type"] == "start_game" for event in events) != 1:
        raise CorpusError("MJAI game must contain exactly one start_game")
    if sum(event["type"] == "end_game" for event in events) != 1:
        raise CorpusError("MJAI game must contain exactly one end_game")

    names = events[0].get("names")
    if names is not None and (type(names) is not list or len(names) != 4):
        raise CorpusError("start_game names must contain four seats when present")
    for item in participations:
        if not 0 <= item.seat <= 3 or (names is not None and item.seat >= len(names)):
            raise CorpusError("participation seat cannot join to start_game")

    in_round = False
    terminal_seen = False
    action_seen = False
    round_count = 0
    all_initial = 0
    reconstructable = 0
    actual_tsumo = 0
    masked_tsumo = 0
    missing_tsumo = 0
    hands: list[Counter[str] | None] = [None, None, None, None]
    round_reconstructable = False

    for index, event in enumerate(events[1:-1], start=2):
        event_type = event["type"]
        context = f"MJAI line {index} ({event_type})"
        if event_type == "start_game" or event_type == "end_game":
            raise CorpusError(f"{context} is out of lifecycle order")
        if event_type == "start_kyoku":
            if in_round:
                raise CorpusError("start_kyoku encountered before prior end_kyoku")
            in_round = True
            terminal_seen = False
            action_seen = False
            round_count += 1
            tehais = event.get("tehais")
            full = (
                type(tehais) is list
                and len(tehais) == 4
                and all(type(hand) is list for hand in tehais)
                and all(len(hand) == 13 for hand in tehais)
                and all(_actual_tile(tile) for hand in tehais for tile in hand)
            )
            if full:
                all_initial += 1
                hands = [Counter(hand) for hand in tehais]
            else:
                hands = [None, None, None, None]
            round_reconstructable = full
            continue
        if event_type == "end_kyoku":
            if not in_round or not action_seen or not terminal_seen:
                raise CorpusError("end_kyoku lacks an active, terminal round")
            if round_reconstructable:
                reconstructable += 1
            in_round = False
            continue
        if not in_round:
            # Unknown, non-critical server metadata is forward compatible outside rounds.
            if event_type in _ROUND_ACTIONS:
                raise CorpusError(f"{context} occurs outside a round")
            continue
        if event_type not in _ROUND_ACTIONS:
            continue
        action_seen = True
        if terminal_seen and event_type not in _TERMINAL_EVENTS:
            raise CorpusError(f"{context} occurs after the round terminal event")
        if event_type in _TERMINAL_EVENTS:
            if event_type == "hora":
                _actor(event, context)
            terminal_seen = True
            continue
        if event_type == "tsumo":
            actor = _actor(event, context)
            tile = event.get("pai")
            if tile is None:
                missing_tsumo += 1
                hands[actor] = None
                round_reconstructable = False
            elif not _actual_tile(tile):
                masked_tsumo += 1
                hands[actor] = None
                round_reconstructable = False
            else:
                actual_tsumo += 1
                if hands[actor] is not None:
                    hands[actor][tile] += 1
        elif event_type == "dahai":
            actor = _actor(event, context)
            tile = event.get("pai")
            if not _actual_tile(tile) or hands[actor] is None or hands[actor][tile] < 1:
                hands[actor] = None
                round_reconstructable = False
            else:
                hands[actor][tile] -= 1
        elif event_type in {"chi", "pon", "daiminkan", "ankan"}:
            actor = _actor(event, context)
            if not _remove_tiles(hands[actor], event.get("consumed")):
                hands[actor] = None
                round_reconstructable = False
        elif event_type == "kakan":
            actor = _actor(event, context)
            tile = event.get("pai")
            if not _actual_tile(tile) or hands[actor] is None or hands[actor][tile] < 1:
                hands[actor] = None
                round_reconstructable = False
            else:
                hands[actor][tile] -= 1
        elif event_type in {"reach", "reach_accepted"}:
            _actor(event, context)

    if in_round:
        raise CorpusError("end_game encountered before end_kyoku")
    if round_count == 0:
        raise CorpusError("MJAI game contains no rounds")
    return ValidationResult(
        event_count=len(events),
        lifecycle_valid=True,
        hidden_information=HiddenInformationCoverage(
            round_count=round_count,
            all_seat_initial_hand_rounds=all_initial,
            actual_tsumo_count=actual_tsumo,
            masked_tsumo_count=masked_tsumo,
            missing_tsumo_count=missing_tsumo,
            reconstructable_rounds=reconstructable,
        ),
    )


__all__ = [
    "HiddenInformationCoverage",
    "ValidationResult",
    "parse_jsonl_gzip",
    "validate_mjai_gzip",
]
