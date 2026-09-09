"""Wholly synthetic Issue #170 fixtures; no third-party log bytes."""

from __future__ import annotations

import gzip
import json


def synthetic_mjai(
    *,
    drawn_tile: object = "5m",
    include_unknown: bool = True,
    game_names: object = None,
) -> bytes:
    names = ["seat0", "seat1", "seat2", "seat3"] if game_names is None else game_names
    hands = [
        ["1m"] * 13,
        ["1p"] * 13,
        ["1s"] * 13,
        ["E"] * 13,
    ]
    events = [
        {"type": "start_game", "names": names, "future": {"accepted": True}},
        {"type": "start_kyoku", "tehais": hands},
    ]
    if include_unknown:
        events.append({"type": "riichilab_future_metadata", "anything": [1, 2]})
    events.extend(
        [
            {"type": "tsumo", "actor": 0, "pai": drawn_tile},
            *(
                [{"type": "dahai", "actor": 0, "pai": drawn_tile}]
                if drawn_tile == "5m"
                else []
            ),
            {"type": "ryukyoku"},
            {"type": "end_kyoku"},
            {"type": "end_game"},
        ]
    )
    text = "".join(json.dumps(event, separators=(",", ":")) + "\n" for event in events)
    return gzip.compress(text.encode("utf-8"), mtime=0)


def bot_response(bot_id: int, games: list[dict[str, object]]) -> bytes:
    return json.dumps(
        {"bot": {"id": bot_id}, "recent_games": games}, separators=(",", ":")
    ).encode()


def game(
    game_id: str,
    played_at: str,
    players: list[dict[str, object]],
) -> dict[str, object]:
    return {
        "game_id": game_id,
        "played_at": played_at,
        "players": players,
        "unrelated_profile": {"must_not_be_retained": True},
    }


def player(bot_id: int, seat: int, *, rank: int = 1, score: int = 30000):
    return {"bot_id": bot_id, "rank": rank, "score": score, "seat": seat}
