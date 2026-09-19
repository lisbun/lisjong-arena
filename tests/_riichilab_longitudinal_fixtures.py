"""Synthetic fixtures for Issue #253; no live RiichiLab input."""

from __future__ import annotations

import gzip
import json
from pathlib import Path

from lisjong_arena.riichilab_corpus.models import canonical_json_bytes
from lisjong_arena.riichilab_longitudinal.models import (
    GameAnalysis,
    OpponentParticipation,
    PolicyProvenance,
    RoundMetrics,
)
from lisjong_arena.riichilab_self_history.models import (
    SelfHistoryGame,
    build_history,
)


def history_game(
    game_id: str = "game-1",
    played_at: str = "2026-09-14T10:00:00",
    *,
    seat: int = 0,
    rank: int = 1,
    score: int = 35000,
    rating_before: float = 1500.0,
    rating_delta: float = 12.0,
    disconnected: bool = False,
) -> SelfHistoryGame:
    return SelfHistoryGame(
        game_id=game_id,
        game_type="ranked",
        player_count=4,
        played_at=played_at,
        seat=seat,
        rank=rank,
        score=score,
        rating_before=rating_before,
        rating_delta=rating_delta,
        mu_before=25.0,
        is_disconnected=disconnected,
        is_penalized=False,
    )


def write_history(root: Path, games: tuple[SelfHistoryGame, ...]) -> None:
    history = build_history(
        bot_id=313,
        retrieved_at="2026-09-19T00:00:00Z",
        declared_total=len(games),
        games=games,
    )
    root.mkdir(parents=True, exist_ok=True)
    (root / "history.json").write_bytes(canonical_json_bytes(history.to_value()))


def mjai_bytes(events: list[dict[str, object]]) -> bytes:
    full = [
        {"type": "start_game", "names": ["self", "a", "b", "c"]},
        *events,
        {"type": "end_game"},
    ]
    text = "".join(json.dumps(event, separators=(",", ":")) + "\n" for event in full)
    return gzip.compress(text.encode("utf-8"), mtime=0)


def round_events(
    terminal: list[dict[str, object]],
    *,
    oya: int = 0,
    actions: list[dict[str, object]] | None = None,
) -> list[dict[str, object]]:
    return [
        {"type": "start_kyoku", "oya": oya},
        *(actions or []),
        *terminal,
        {"type": "end_kyoku"},
    ]


def opponent(
    game_id: str,
    bot_id: int,
    seat: int,
    rating: float | None,
) -> OpponentParticipation:
    return OpponentParticipation(
        game_id=game_id,
        bot_id=bot_id,
        bot_name=f"bot-{bot_id}",
        seat=seat,
        rank=seat + 1,
        score=25000,
        rating_before=rating,
    )


def analysis_game(
    game_id: str,
    *,
    played_at: str = "2026-09-14T10:00:00",
    rank: int = 1,
    rating: float = 1500.0,
    policy: str = "PolicyA",
    metrics: RoundMetrics | None = None,
    opponents: tuple[OpponentParticipation, ...] = (),
    disconnected: bool = False,
) -> GameAnalysis:
    return GameAnalysis(
        game_id=game_id,
        played_at=played_at,
        seat=0,
        rank=rank,
        final_score=35000,
        self_rating_before=rating,
        rating_delta=10.0,
        is_disconnected=disconnected,
        provenance=PolicyProvenance(
            policy_identity=policy,
            source="legacy_epoch",
        ),
        mjai_status="available" if metrics else "missing",
        metrics=metrics,
        opponents=opponents,
    )


__all__ = [
    "analysis_game",
    "history_game",
    "mjai_bytes",
    "opponent",
    "round_events",
    "write_history",
]
