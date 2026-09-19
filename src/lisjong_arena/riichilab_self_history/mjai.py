"""Valid-cache reuse and missing-only server-side MJAI acquisition.

cache hit判定はIssue #170の`cache-index.json`に依存しない。`games/<game_id>.jsonl.gz`
が存在すれば、そのfile自体を毎回strictに再検証する。manual acquisitionで作られた
index無しのlocal fileもこれによりreuseできる。
"""

from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

from lisjong_arena.riichilab_corpus.http import HttpTransport, get_with_bounded_retry
from lisjong_arena.riichilab_corpus.validation import (
    ValidationResult,
    validate_mjai_log,
)
from lisjong_arena.riichilab_self_history.errors import (
    ACQUISITION_ERRORS,
    SelfHistoryError,
)
from lisjong_arena.riichilab_self_history.models import (
    SelfHistory,
    SelfHistoryGame,
    resolve_self_log_url,
)
from lisjong_arena.riichilab_self_history.pacing import RequestPacer
from lisjong_arena.riichilab_self_history.persistence import GAMES_DIRECTORY


@dataclass(frozen=True, slots=True)
class LogFailure:
    """One game whose MJAI log could not be reused or acquired."""

    game_id: str
    reason: str


def log_path(root: Path, game_id: str) -> Path:
    return root / GAMES_DIRECTORY / f"{game_id}.jsonl.gz"


def validate_local_log(path: Path, game: SelfHistoryGame) -> ValidationResult:
    """Strictly validate one existing local MJAI file before reusing it."""
    if path.is_symlink() or not path.is_file():
        raise SelfHistoryError(f"cached MJAI log is not a regular file: {path}")
    try:
        payload = path.read_bytes()
    except OSError as exc:
        raise SelfHistoryError(f"cannot read cached MJAI log: {path}") from exc
    return validate_mjai_log(payload, seats=(game.seat,))


def _publish_downloaded_log(destination: Path, payload: bytes) -> None:
    """Publish already-validated bytes without ever overwriting silently."""
    created = False
    try:
        with destination.open("xb") as stream:
            created = True
            stream.write(payload)
    except FileExistsError:
        # `existing == payload`はrace後の安全なreuse。異なるbytesはfail closedする。
        if destination.read_bytes() != payload:
            raise SelfHistoryError(
                f"existing MJAI bytes conflict with the downloaded bytes: {destination}"
            ) from None
    except BaseException:
        if created:
            destination.unlink(missing_ok=True)
        raise


def download_log(
    transport: HttpTransport,
    root: Path,
    game: SelfHistoryGame,
    *,
    pacer: RequestPacer,
    timeout: float = 15.0,
) -> ValidationResult:
    """Download one missing MJAI log through staging, validation, then publish."""
    destination = log_path(root, game.game_id)
    destination.parent.mkdir(parents=True, exist_ok=True)
    pacer.before_request()
    response = get_with_bounded_retry(
        transport, resolve_self_log_url(game), timeout=timeout
    )
    handle, staged_name = tempfile.mkstemp(
        dir=destination.parent, prefix=f".incoming-{game.game_id}.", suffix=".part"
    )
    staged = Path(staged_name)
    try:
        with os.fdopen(handle, "wb") as stream:
            stream.write(response.body)
        # Validate exactly the staged bytes that publication will copy.
        validation = validate_local_log(staged, game)
        _publish_downloaded_log(destination, staged.read_bytes())
    finally:
        staged.unlink(missing_ok=True)
    return validation


@dataclass(frozen=True, slots=True)
class MjaiAcquisitionResult:
    expected_games: int
    cache_hits: tuple[str, ...]
    downloaded: tuple[str, ...]
    valid_games: tuple[str, ...]
    failures: tuple[LogFailure, ...]

    @property
    def coverage_rate(self) -> float:
        if not self.expected_games:
            return 1.0
        return round(len(self.valid_games) / self.expected_games, 6)

    @property
    def status(self) -> str:
        if not self.failures and len(self.valid_games) == self.expected_games:
            return "COMPLETE"
        return "PARTIAL" if self.valid_games else "FAILED"


def acquire_missing_logs(
    transport: HttpTransport,
    root: Path,
    history: SelfHistory,
    *,
    pacer: RequestPacer,
    timeout: float = 15.0,
) -> MjaiAcquisitionResult:
    """Reuse every valid local log and download only the missing ones.

    corrupt / inconsistent localfileはsilent redownloadで隠さず、その`game_id`を
    failureとして記録して次のgameへ進む。既存bytesは決して上書きしない。
    """
    (root / GAMES_DIRECTORY).mkdir(parents=True, exist_ok=True)
    cache_hits: list[str] = []
    downloaded: list[str] = []
    valid: list[str] = []
    failures: list[LogFailure] = []
    for game in history.games:
        path = log_path(root, game.game_id)
        if path.exists() or path.is_symlink():
            try:
                validate_local_log(path, game)
            except ACQUISITION_ERRORS as exc:
                failures.append(
                    LogFailure(
                        game.game_id,
                        f"existing local MJAI log is invalid; refusing to "
                        f"redownload or overwrite it: {exc}",
                    )
                )
                continue
            cache_hits.append(game.game_id)
            valid.append(game.game_id)
            continue
        try:
            download_log(transport, root, game, pacer=pacer, timeout=timeout)
        except ACQUISITION_ERRORS as exc:
            failures.append(LogFailure(game.game_id, str(exc)))
            continue
        downloaded.append(game.game_id)
        valid.append(game.game_id)
    return MjaiAcquisitionResult(
        expected_games=len(history.games),
        cache_hits=tuple(cache_hits),
        downloaded=tuple(downloaded),
        valid_games=tuple(valid),
        failures=tuple(failures),
    )


__all__ = [
    "LogFailure",
    "MjaiAcquisitionResult",
    "acquire_missing_logs",
    "download_log",
    "log_path",
    "validate_local_log",
]
