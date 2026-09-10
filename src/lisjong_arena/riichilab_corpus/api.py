"""RiichiLab recent-game parsing and sanitized snapshot construction."""

from __future__ import annotations

from collections.abc import Mapping

from lisjong_arena.riichilab_corpus.http import (
    HttpTransport,
    get_with_bounded_retry,
)
from lisjong_arena.riichilab_corpus.models import (
    API_BASE_URL,
    TARGET_BOTS,
    CorpusError,
    Participation,
    RecentGamesSnapshot,
    build_snapshot,
    strict_json_loads,
    utc_now_text,
)


def bot_api_url(bot_id: int) -> str:
    if type(bot_id) is not int or bot_id <= 0:
        raise ValueError("bot_id must be a positive integer")
    return f"{API_BASE_URL}/bots/{bot_id}"


def _recent_games(value: object) -> list[object]:
    if type(value) is not dict:
        raise CorpusError("bot API response must be an object")
    candidates = [value]
    for key in ("data", "bot"):
        nested = value.get(key)
        if type(nested) is dict:
            candidates.append(nested)
    for candidate in candidates:
        games = candidate.get("recent_games")
        if type(games) is list:
            return games
    raise CorpusError("bot API response has no recent_games array")


def _first(mapping: Mapping[str, object], keys: tuple[str, ...]) -> object | None:
    for key in keys:
        if key in mapping:
            return mapping[key]
    return None


def _entry_bot_id(value: object) -> int | None:
    if type(value) is int:
        return value
    if type(value) is not dict:
        return None
    direct = _first(value, ("bot_id", "id"))
    if type(direct) is int:
        return direct
    nested = value.get("bot")
    if type(nested) is dict and type(nested.get("id")) is int:
        return nested["id"]
    return None


def _optional_int(value: object, context: str) -> int | None:
    if value is None:
        return None
    if type(value) is not int:
        raise CorpusError(f"{context} must be null or an integer")
    return value


def _participation_from_game(game: object, expected_bot_id: int) -> Participation:
    if type(game) is not dict:
        raise CorpusError("recent game entry must be an object")
    game_id = _first(game, ("game_id", "id"))
    played_at = _first(
        game, ("played_at", "started_at", "created_at", "ended_at", "finished_at")
    )
    players = _first(game, ("participations", "players", "bots"))

    player: Mapping[str, object] | None = None
    seat: int | None = None
    if type(players) is list:
        for index, candidate in enumerate(players):
            if _entry_bot_id(candidate) == expected_bot_id:
                if type(candidate) is dict:
                    player = candidate
                else:
                    player = game
                seat = index
                break
        if seat is None:
            raise CorpusError(
                f"recent game {game_id!r} does not contain expected bot {expected_bot_id}"
            )
    else:
        direct_bot_id = _first(game, ("bot_id", "participant_bot_id"))
        if direct_bot_id is not None and direct_bot_id != expected_bot_id:
            raise CorpusError("recent game entry belongs to a different bot")
        player = game

    assert player is not None
    explicit_seat = player.get("seat")
    if explicit_seat is not None:
        if type(explicit_seat) is not int:
            raise CorpusError("participation seat must be an integer")
        if seat is not None and explicit_seat != seat:
            raise CorpusError("explicit seat conflicts with player array order")
        seat = explicit_seat
    if seat is None:
        raise CorpusError("recent game participation has no seat")

    rank = _first(player, ("rank", "placement", "position"))
    score = _first(player, ("score", "final_score"))
    if rank is None and type(game.get("ranks")) is list and len(game["ranks"]) == 4:
        rank = game["ranks"][seat]
    if score is None and type(game.get("scores")) is list and len(game["scores"]) == 4:
        score = game["scores"][seat]

    return Participation(
        game_id=game_id,
        bot_id=expected_bot_id,
        seat=seat,
        played_at=played_at,
        rank=_optional_int(rank, "rank"),
        score=_optional_int(score, "score"),
    )


def parse_bot_recent_games(
    data: bytes, expected_bot_id: int
) -> tuple[Participation, ...]:
    value = strict_json_loads(data, f"bot {expected_bot_id} API response")
    if type(value) is dict:
        described_bot = value.get("bot")
        if type(described_bot) is dict:
            described_id = _first(described_bot, ("bot_id", "id"))
            if described_id is not None and described_id != expected_bot_id:
                raise CorpusError("bot API response identity does not match its URL")
    result = tuple(
        _participation_from_game(game, expected_bot_id) for game in _recent_games(value)
    )
    keys = [(item.game_id, item.bot_id) for item in result]
    if len(set(keys)) != len(keys):
        raise CorpusError(f"bot {expected_bot_id} recent_games contains duplicates")
    return result


def snapshot_recent_games(
    transport: HttpTransport,
    *,
    retrieved_at: str | None = None,
    timeout: float = 15.0,
) -> RecentGamesSnapshot:
    """Fetch exactly one current recent-game document per configured target bot."""
    source_apis = tuple(bot_api_url(bot_id) for bot_id, _ in TARGET_BOTS)
    participations = []
    for (bot_id, _), url in zip(TARGET_BOTS, source_apis, strict=True):
        response = get_with_bounded_retry(transport, url, timeout=timeout)
        participations.extend(parse_bot_recent_games(response.body, bot_id))
    return build_snapshot(
        retrieved_at=retrieved_at or utc_now_text(),
        source_apis=source_apis,
        target_bots=TARGET_BOTS,
        participations=tuple(participations),
    )


__all__ = ["bot_api_url", "parse_bot_recent_games", "snapshot_recent_games"]
