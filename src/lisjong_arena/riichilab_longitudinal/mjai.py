"""Strict server-side MJAI projection into self-perspective round metrics."""

from __future__ import annotations

from dataclasses import fields

from lisjong_arena.riichilab_corpus.models import CorpusError
from lisjong_arena.riichilab_corpus.validation import (
    parse_jsonl_gzip,
    validate_mjai_log,
)
from lisjong_arena.riichilab_longitudinal.errors import LongitudinalAnalysisError
from lisjong_arena.riichilab_longitudinal.models import RoundMetrics

_CALL_TYPES = {"chi", "pon", "daiminkan", "ankan", "kakan"}
_OPEN_CALL_TYPES = {"chi", "pon", "daiminkan", "kakan"}
_KAN_TYPES = {"daiminkan", "ankan", "kakan"}


def _seat(value: object, context: str) -> int:
    if type(value) is not int or not 0 <= value <= 3:
        raise LongitudinalAnalysisError(
            f"{context} must be an integer from 0 through 3"
        )
    return value


def _deltas(event: dict[str, object], context: str) -> tuple[int, int, int, int]:
    value = event.get("deltas")
    if (
        type(value) is not list
        or len(value) != 4
        or any(type(item) is not int for item in value)
    ):
        raise LongitudinalAnalysisError(
            f"{context} deltas must be a four-item integer array"
        )
    return (value[0], value[1], value[2], value[3])


def analyze_mjai(payload: bytes, *, self_seat: int) -> RoundMetrics:
    """Validate one complete game, then derive deterministic self metrics.

    Lifecycle, gzip and strict JSONL ownership stay in ``riichilab_corpus``.
    This consumer adds only Issue #253's purpose-specific terminal/call facts.
    """
    _seat(self_seat, "self seat")
    try:
        validate_mjai_log(payload, seats=(self_seat,))
        events = parse_jsonl_gzip(payload)
    except CorpusError as exc:
        raise LongitudinalAnalysisError(
            f"MJAI strict validation failed: {exc}"
        ) from exc

    totals = {field.name: 0 for field in fields(RoundMetrics)}
    in_round = False
    dealer = -1
    self_riichi = False
    self_open = False
    round_win = False
    round_tsumo_win = False
    round_ron_win = False
    round_deal_in = False
    round_deal_in_loss = 0
    round_draw = False
    round_draw_delta = 0
    round_opponent_tsumo = False
    round_opponent_tsumo_loss = 0
    round_terminal_delta = 0

    for index, event in enumerate(events[1:-1], start=2):
        event_type = event["type"]
        context = f"MJAI line {index} ({event_type})"
        if event_type == "start_kyoku":
            if in_round:
                raise LongitudinalAnalysisError(
                    "MJAI start_kyoku overlaps an active round"
                )
            in_round = True
            dealer = _seat(event.get("oya"), f"{context} oya")
            self_riichi = False
            self_open = False
            round_win = False
            round_tsumo_win = False
            round_ron_win = False
            round_deal_in = False
            round_deal_in_loss = 0
            round_draw = False
            round_draw_delta = 0
            round_opponent_tsumo = False
            round_opponent_tsumo_loss = 0
            round_terminal_delta = 0
            continue
        if event_type == "end_kyoku":
            if not in_round:
                raise LongitudinalAnalysisError("MJAI end_kyoku has no active round")
            totals["rounds"] += 1
            totals["dealer_rounds"] += int(dealer == self_seat)
            totals["wins"] += int(round_win)
            totals["tsumo_wins"] += int(round_tsumo_win)
            totals["ron_wins"] += int(round_ron_win)
            totals["deal_in_rounds"] += int(round_deal_in)
            totals["deal_in_loss"] += round_deal_in_loss
            totals["riichi_rounds"] += int(self_riichi)
            totals["riichi_wins"] += int(self_riichi and round_win)
            totals["riichi_deal_ins"] += int(self_riichi and round_deal_in)
            totals["open_call_rounds"] += int(self_open)
            totals["open_wins"] += int(self_open and round_win)
            totals["open_deal_ins"] += int(self_open and round_deal_in)
            totals["draw_rounds"] += int(round_draw)
            totals["draw_delta"] += round_draw_delta
            totals["opponent_tsumo_rounds"] += int(round_opponent_tsumo)
            totals["opponent_tsumo_loss"] += round_opponent_tsumo_loss
            totals["dealer_wins"] += int(dealer == self_seat and round_win)
            totals["dealer_deal_ins"] += int(dealer == self_seat and round_deal_in)
            totals["terminal_delta"] += round_terminal_delta
            in_round = False
            continue
        if not in_round:
            continue

        if event_type == "reach_accepted":
            actor = _seat(event.get("actor"), f"{context} actor")
            if actor == self_seat:
                if self_riichi:
                    raise LongitudinalAnalysisError(
                        "self has riichi accepted more than once in one round"
                    )
                self_riichi = True
                totals["riichi_count"] += 1
        elif event_type in _CALL_TYPES:
            actor = _seat(event.get("actor"), f"{context} actor")
            if actor == self_seat:
                if event_type in _OPEN_CALL_TYPES:
                    self_open = True
                    totals["open_call_count"] += 1
                if event_type == "chi":
                    totals["chi_count"] += 1
                elif event_type == "pon":
                    totals["pon_count"] += 1
                elif event_type in _KAN_TYPES:
                    totals["kan_count"] += 1
        elif event_type == "hora":
            actor = _seat(event.get("actor"), f"{context} actor")
            target = _seat(event.get("target"), f"{context} target")
            delta = _deltas(event, context)
            round_terminal_delta += delta[self_seat]
            if actor == self_seat:
                if round_win:
                    raise LongitudinalAnalysisError(
                        "self has multiple hora events in one round"
                    )
                round_win = True
                if target == actor:
                    round_tsumo_win = True
                else:
                    round_ron_win = True
            elif target == self_seat:
                # Multiple hora events may share one discard. Count the round once,
                # but accumulate every applicable loss.
                round_deal_in = True
                if delta[self_seat] > 0:
                    raise LongitudinalAnalysisError(
                        "deal-in hora gives the self seat a positive delta"
                    )
                round_deal_in_loss += -delta[self_seat]
            elif target == actor:
                round_opponent_tsumo = True
                if delta[self_seat] > 0:
                    raise LongitudinalAnalysisError(
                        "opponent tsumo gives the self seat a positive delta"
                    )
                round_opponent_tsumo_loss += -delta[self_seat]
        elif event_type == "ryukyoku":
            delta = _deltas(event, context)
            round_draw = True
            round_draw_delta += delta[self_seat]
            round_terminal_delta += delta[self_seat]

    if in_round:  # defensive after shared lifecycle validation
        raise LongitudinalAnalysisError("MJAI game ends inside an active round")
    return RoundMetrics(**totals)


__all__ = ["analyze_mjai"]
