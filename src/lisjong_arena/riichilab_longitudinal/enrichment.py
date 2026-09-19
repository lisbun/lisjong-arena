"""Separately invoked, bounded opponent metadata enrichment."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from lisjong_arena.riichilab_corpus.api import (
    bot_api_url,
    parse_bot_recent_games,
)
from lisjong_arena.riichilab_corpus.http import (
    DEFAULT_TIMEOUT_SECONDS,
    HttpTransport,
    get_with_bounded_retry,
)
from lisjong_arena.riichilab_corpus.models import (
    CorpusError,
    canonical_json_bytes,
    sha256_bytes,
    strict_json_loads,
    utc_now_text,
)
from lisjong_arena.riichilab_corpus.persistence import (
    atomic_replace,
    ensure_outside_git_worktree,
    read_json,
)
from lisjong_arena.riichilab_longitudinal.errors import LongitudinalAnalysisError
from lisjong_arena.riichilab_longitudinal.models import (
    OpponentCache,
    OpponentParticipation,
    strict_number,
    strict_text,
)
from lisjong_arena.riichilab_self_history.models import SelfHistory
from lisjong_arena.riichilab_self_history.pacing import RequestPacer

CANDIDATE_SCHEMA_ID = "lisjong-arena-riichilab-opponent-candidates"


@dataclass(frozen=True, slots=True, order=True)
class OpponentCandidate:
    bot_id: int
    bot_name: str

    def __post_init__(self) -> None:
        if type(self.bot_id) is not int or self.bot_id <= 0:
            raise LongitudinalAnalysisError(
                "candidate bot_id must be a positive integer"
            )
        strict_text(self.bot_name, "candidate bot_name")

    def to_value(self) -> dict[str, object]:
        return {"bot_id": self.bot_id, "bot_name": self.bot_name}


@dataclass(frozen=True, slots=True)
class CandidateUniverse:
    source: str
    bots: tuple[OpponentCandidate, ...]
    identity: str

    def __post_init__(self) -> None:
        strict_text(self.source, "candidate universe source")
        if Path(self.source).is_absolute():
            raise LongitudinalAnalysisError(
                "candidate universe source must be a sanitized identity, not a local path"
            )
        if type(self.bots) is not tuple or tuple(sorted(self.bots)) != self.bots:
            raise LongitudinalAnalysisError(
                "candidate universe bots must use canonical order"
            )
        if not self.bots:
            raise LongitudinalAnalysisError("candidate universe must contain bots")
        if len({bot.bot_id for bot in self.bots}) != len(self.bots):
            raise LongitudinalAnalysisError("candidate universe repeats a bot_id")
        strict_text(self.identity, "candidate universe identity")


def load_candidate_universe(path: str | Path) -> CandidateUniverse:
    """Read a sanitized leaderboard-derived candidate universe.

    The accepted bot rows intentionally contain no current rating field. The
    snapshot chooses which Bot endpoints may be queried; it is never a source
    of historical ``rating_before``.
    """
    try:
        value = read_json(Path(path), "opponent candidate universe")
    except CorpusError as exc:
        raise LongitudinalAnalysisError(str(exc)) from exc
    expected = {"bots", "schema", "schema_version", "source"}
    if type(value) is not dict or set(value) != expected:
        raise LongitudinalAnalysisError("candidate universe fields are invalid")
    if value["schema"] != CANDIDATE_SCHEMA_ID or value["schema_version"] != 1:
        raise LongitudinalAnalysisError("candidate universe schema is unsupported")
    source = strict_text(value["source"], "candidate universe source")
    if Path(source).is_absolute():
        raise LongitudinalAnalysisError(
            "candidate universe source must be a sanitized identity, not a local path"
        )
    if type(value["bots"]) is not list:
        raise LongitudinalAnalysisError("candidate universe bots must be an array")
    bots = []
    for raw in value["bots"]:
        if type(raw) is not dict or set(raw) != {"bot_id", "bot_name"}:
            raise LongitudinalAnalysisError("candidate bot fields are invalid")
        bots.append(OpponentCandidate(**raw))
    ordered = tuple(sorted(bots))
    if len({bot.bot_id for bot in ordered}) != len(ordered):
        raise LongitudinalAnalysisError("candidate universe repeats a bot_id")
    logical = {"bots": [bot.to_value() for bot in ordered], "source": source}
    return CandidateUniverse(
        source=source,
        bots=ordered,
        identity=sha256_bytes(canonical_json_bytes(logical)),
    )


def _first(mapping: Mapping[str, object], keys: tuple[str, ...]) -> object | None:
    for key in keys:
        if key in mapping:
            return mapping[key]
    return None


def _recent_games(value: object) -> list[object]:
    if type(value) is not dict:
        raise LongitudinalAnalysisError("Bot API response must be an object")
    candidates = [value]
    for key in ("data", "bot"):
        nested = value.get(key)
        if type(nested) is dict:
            candidates.append(nested)
    for candidate in candidates:
        games = candidate.get("recent_games")
        if type(games) is list:
            return games
    raise LongitudinalAnalysisError("Bot API response has no recent_games array")


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


def _raw_player(game: dict[str, object], bot_id: int) -> Mapping[str, object]:
    players = _first(game, ("participations", "players", "bots"))
    if type(players) is list:
        matches = [item for item in players if _entry_bot_id(item) == bot_id]
        if len(matches) != 1 or type(matches[0]) is not dict:
            raise LongitudinalAnalysisError(
                "selected recent game does not have one candidate participation"
            )
        return matches[0]
    direct = _first(game, ("bot_id", "participant_bot_id"))
    if direct is not None and direct != bot_id:
        raise LongitudinalAnalysisError("selected recent game belongs to another bot")
    return game


def parse_opponent_recent_games(
    data: bytes,
    *,
    candidate: OpponentCandidate,
    selected_game_ids: frozenset[str],
) -> tuple[OpponentParticipation, ...]:
    """Project exact selected-game joins from one public Bot response."""
    try:
        base = parse_bot_recent_games(data, candidate.bot_id)
        document = strict_json_loads(data, f"bot {candidate.bot_id} API response")
    except CorpusError as exc:
        raise LongitudinalAnalysisError(str(exc)) from exc
    by_game = {item.game_id: item for item in base if item.game_id in selected_game_ids}
    raw_by_game: dict[str, dict[str, object]] = {}
    for raw in _recent_games(document):
        if type(raw) is not dict:
            raise LongitudinalAnalysisError("recent game entry must be an object")
        game_id = _first(raw, ("game_id", "id"))
        if game_id in selected_game_ids:
            if game_id in raw_by_game:
                raise LongitudinalAnalysisError(
                    "Bot API response repeats a selected game_id"
                )
            raw_by_game[game_id] = raw
    if set(raw_by_game) != set(by_game):
        raise LongitudinalAnalysisError(
            "typed and raw opponent participation joins disagree"
        )

    result = []
    for game_id in sorted(by_game):
        base_row = by_game[game_id]
        if base_row.rank is None or base_row.score is None:
            raise LongitudinalAnalysisError(
                f"opponent participation {game_id} lacks rank or score"
            )
        player = _raw_player(raw_by_game[game_id], candidate.bot_id)
        rating_raw = player.get("rating_before")
        rating = (
            None
            if rating_raw is None
            else strict_number(rating_raw, "opponent historical rating_before")
        )
        result.append(
            OpponentParticipation(
                game_id=game_id,
                bot_id=candidate.bot_id,
                bot_name=candidate.bot_name,
                seat=base_row.seat,
                rank=base_row.rank,
                score=base_row.score,
                rating_before=rating,
            )
        )
    return tuple(result)


def load_opponent_cache(path: str | Path | None) -> OpponentCache | None:
    if path is None:
        return None
    candidate = Path(path)
    if not candidate.is_file():
        raise LongitudinalAnalysisError(f"opponent cache is missing: {candidate}")
    try:
        return OpponentCache.from_value(read_json(candidate, "opponent cache"))
    except CorpusError as exc:
        raise LongitudinalAnalysisError(str(exc)) from exc


def _validate_cache_against_history(cache: OpponentCache, history: SelfHistory) -> None:
    games = {game.game_id: game for game in history.games}
    if cache.self_bot_id != history.bot_id:
        raise LongitudinalAnalysisError(
            "opponent cache self bot does not match self history"
        )
    if any(game_id not in games for game_id in cache.selected_game_ids):
        raise LongitudinalAnalysisError(
            "opponent cache contains a game outside self history"
        )
    for row in cache.opponents:
        if row.seat == games[row.game_id].seat:
            raise LongitudinalAnalysisError(
                f"opponent cache occupies the self seat in {row.game_id}"
            )


def enrich_opponents(
    history: SelfHistory,
    *,
    selected_game_ids: tuple[str, ...],
    candidate_universe: CandidateUniverse,
    max_games: int,
    max_bots: int,
    output_path: str | Path,
    transport: HttpTransport,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    pacer: RequestPacer | None = None,
    retrieved_at: str | None = None,
) -> OpponentCache:
    """Query at most one recent-games document per declared candidate Bot."""
    if type(max_games) is not int or max_games <= 0:
        raise LongitudinalAnalysisError("max_games must be a positive integer")
    if type(max_bots) is not int or max_bots <= 0:
        raise LongitudinalAnalysisError("max_bots must be a positive integer")
    timeout_value = strict_number(timeout, "timeout")
    if timeout_value <= 0:
        raise LongitudinalAnalysisError("timeout must be positive")
    if len(candidate_universe.bots) > max_bots:
        raise LongitudinalAnalysisError(
            "candidate universe exceeds max_bots; no request was issued"
        )
    selected = tuple(sorted(set(selected_game_ids)))
    if selected != selected_game_ids:
        raise LongitudinalAnalysisError(
            "selected_game_ids must be unique canonical order"
        )
    if len(selected) > max_games:
        raise LongitudinalAnalysisError(
            "selected game count exceeds max_games; no request was issued"
        )
    history_ids = set(history.game_ids)
    if any(game_id not in history_ids for game_id in selected):
        raise LongitudinalAnalysisError("selected game is absent from self history")
    try:
        output = ensure_outside_git_worktree(Path(output_path))
    except CorpusError as exc:
        raise LongitudinalAnalysisError(str(exc)) from exc
    if output.exists():
        cached = load_opponent_cache(output)
        assert cached is not None
        if (
            cached.self_bot_id != history.bot_id
            or cached.selected_game_ids != selected
            or cached.candidate_universe_identity != candidate_universe.identity
            or cached.candidate_universe_source != candidate_universe.source
        ):
            raise LongitudinalAnalysisError(
                "existing opponent cache belongs to different inputs"
            )
        _validate_cache_against_history(cached, history)
        return cached

    request_pacer = pacer or RequestPacer()
    rows: list[OpponentParticipation] = []
    queried: list[int] = []
    failures: list[dict[str, object]] = []
    selected_set = frozenset(selected)
    for candidate in candidate_universe.bots:
        if candidate.bot_id == history.bot_id:
            continue
        queried.append(candidate.bot_id)
        try:
            request_pacer.before_request()
            response = get_with_bounded_retry(
                transport,
                bot_api_url(candidate.bot_id),
                timeout=timeout_value,
            )
            rows.extend(
                parse_opponent_recent_games(
                    response.body,
                    candidate=candidate,
                    selected_game_ids=selected_set,
                )
            )
        except (CorpusError, LongitudinalAnalysisError) as exc:
            failures.append(
                {
                    "bot_id": candidate.bot_id,
                    "error_type": type(exc).__name__,
                    "message": str(exc),
                }
            )

    cache = OpponentCache(
        self_bot_id=history.bot_id,
        retrieved_at=retrieved_at or utc_now_text(),
        selected_game_ids=selected,
        candidate_universe_identity=candidate_universe.identity,
        candidate_universe_source=candidate_universe.source,
        candidate_bot_count=len(candidate_universe.bots),
        bots_queried=tuple(sorted(queried)),
        api_failures=tuple(sorted(failures, key=lambda item: item["bot_id"])),
        opponents=tuple(sorted(rows)),
    )
    _validate_cache_against_history(cache, history)
    output.parent.mkdir(parents=True, exist_ok=True)
    atomic_replace(output, canonical_json_bytes(cache.to_value()))
    readback = load_opponent_cache(output)
    if readback != cache:
        raise LongitudinalAnalysisError(
            "published opponent cache does not read back identically"
        )
    return cache


__all__ = [
    "CANDIDATE_SCHEMA_ID",
    "CandidateUniverse",
    "OpponentCandidate",
    "enrich_opponents",
    "load_candidate_universe",
    "load_opponent_cache",
    "parse_opponent_recent_games",
]
