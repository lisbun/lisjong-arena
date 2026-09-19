"""Arena-owned RiichiLab self-history acquisition for operator-owned bots (#269).

このpackageはoperator-owned self BotのRiichiLab全対局metadataとserver-side MJAI
logをlocal canonical raw sourceとして維持する。Issue #170 third-party corpusの
fixed strong-bot contractとはschema identityを共有せず、bot discoveryや
third-party bulk acquisitionも提供しない。
"""

from lisjong_arena.riichilab_self_history.errors import SelfHistoryError
from lisjong_arena.riichilab_self_history.mjai import (
    LogFailure,
    MjaiAcquisitionResult,
    acquire_missing_logs,
)
from lisjong_arena.riichilab_self_history.models import (
    REQUEST_LIMIT,
    SelfHistory,
    SelfHistoryGame,
    build_history,
    history_from_value,
    history_identity,
    resolve_self_log_url,
    self_history_api_url,
)
from lisjong_arena.riichilab_self_history.pagination import (
    MaxGamesExceeded,
    SelfHistoryPage,
    fetch_self_history_pages,
    parse_self_history_page,
    self_history_page_url,
)
from lisjong_arena.riichilab_self_history.persistence import (
    history_csv_bytes,
    load_published_history,
)
from lisjong_arena.riichilab_self_history.sync import sync_self_history

__all__ = [
    "LogFailure",
    "MaxGamesExceeded",
    "MjaiAcquisitionResult",
    "REQUEST_LIMIT",
    "SelfHistory",
    "SelfHistoryError",
    "SelfHistoryGame",
    "SelfHistoryPage",
    "acquire_missing_logs",
    "build_history",
    "fetch_self_history_pages",
    "history_csv_bytes",
    "history_from_value",
    "history_identity",
    "load_published_history",
    "parse_self_history_page",
    "resolve_self_log_url",
    "self_history_api_url",
    "self_history_page_url",
    "sync_self_history",
]
