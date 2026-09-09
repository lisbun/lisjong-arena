"""Two-step bounded acquisition planning, execution, and completion reporting."""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from lisjong_arena.riichilab_corpus.http import (
    HttpTransport,
    get_with_bounded_retry,
)
from lisjong_arena.riichilab_corpus.models import (
    MAX_ACQUISITION_CEILING,
    PLAN_SCHEMA_ID,
    REPORT_SCHEMA_ID,
    SCHEMA_VERSION,
    CorpusError,
    RecentGamesSnapshot,
    canonical_json_bytes,
    normalize_timestamp,
    sha256_bytes,
    utc_now_text,
)
from lisjong_arena.riichilab_corpus.persistence import (
    atomic_replace,
    build_manifest,
    cache_state_identity,
    ensure_outside_git_worktree,
    group_participations,
    inspect_cache,
    persist_download,
    resolve_log_url,
    save_manifest,
    validate_manifest_file,
)

REPORT_FILENAME = "last-acquisition-report.json"
SERIAL_REQUEST_INTERVAL_SECONDS = 0.5


class AcquisitionFailed(CorpusError):
    """An acquisition stopped fail-closed; its report was persisted."""


@dataclass(frozen=True, slots=True)
class AcquisitionPlan:
    created_at: str
    snapshot_identity: str
    output_dir: str
    requested_ceiling: int
    unique_game_count: int
    cache_hit_count: int
    download_count: int
    game_ids: tuple[str, ...]
    cache_state_identity: str
    plan_identity: str

    def to_value(self) -> dict[str, object]:
        return {
            "cache_hit_count": self.cache_hit_count,
            "cache_state_identity": self.cache_state_identity,
            "created_at": self.created_at,
            "download_count": self.download_count,
            "game_ids": list(self.game_ids),
            "hard_ceiling": MAX_ACQUISITION_CEILING,
            "output_dir": self.output_dir,
            "plan_identity": self.plan_identity,
            "requested_ceiling": self.requested_ceiling,
            "schema": PLAN_SCHEMA_ID,
            "schema_version": SCHEMA_VERSION,
            "snapshot_identity": self.snapshot_identity,
            "unique_game_count": self.unique_game_count,
        }


def _plan_identity_input(
    *,
    snapshot_identity: str,
    output_dir: str,
    requested_ceiling: int,
    game_ids: tuple[str, ...],
    cache_identity: str,
) -> dict[str, object]:
    return {
        "cache_state_identity": cache_identity,
        "game_ids": list(game_ids),
        "output_dir": output_dir,
        "requested_ceiling": requested_ceiling,
        "snapshot_identity": snapshot_identity,
    }


def create_acquisition_plan(
    snapshot: RecentGamesSnapshot,
    output_dir: Path,
    *,
    requested_ceiling: int,
    created_at: str | None = None,
) -> AcquisitionPlan:
    if (
        type(requested_ceiling) is not int
        or not 1 <= requested_ceiling <= MAX_ACQUISITION_CEILING
    ):
        raise CorpusError(
            f"requested ceiling must be from 1 through {MAX_ACQUISITION_CEILING}"
        )
    if not snapshot.game_ids:
        raise CorpusError("current snapshot contains no unique games")
    if len(snapshot.game_ids) > requested_ceiling:
        raise CorpusError(
            f"current snapshot has {len(snapshot.game_ids)} unique games, above the "
            f"explicit ceiling {requested_ceiling}; stop and reassess"
        )
    output = ensure_outside_git_worktree(output_dir)
    entries, hits = inspect_cache(output, snapshot)
    cache_identity = cache_state_identity(entries, snapshot.game_ids)
    plan_created_at = normalize_timestamp(created_at or utc_now_text(), "created_at")
    identity = sha256_bytes(
        canonical_json_bytes(
            _plan_identity_input(
                snapshot_identity=snapshot.snapshot_identity,
                output_dir=str(output),
                requested_ceiling=requested_ceiling,
                game_ids=snapshot.game_ids,
                cache_identity=cache_identity,
            )
        )
    )
    return AcquisitionPlan(
        created_at=plan_created_at,
        snapshot_identity=snapshot.snapshot_identity,
        output_dir=str(output),
        requested_ceiling=requested_ceiling,
        unique_game_count=len(snapshot.game_ids),
        cache_hit_count=len(hits),
        download_count=len(snapshot.game_ids) - len(hits),
        game_ids=snapshot.game_ids,
        cache_state_identity=cache_identity,
        plan_identity=identity,
    )


def plan_from_value(value: object) -> AcquisitionPlan:
    expected = {
        "cache_hit_count",
        "cache_state_identity",
        "created_at",
        "download_count",
        "game_ids",
        "hard_ceiling",
        "output_dir",
        "plan_identity",
        "requested_ceiling",
        "schema",
        "schema_version",
        "snapshot_identity",
        "unique_game_count",
    }
    if type(value) is not dict or set(value) != expected:
        raise CorpusError("acquisition plan fields are invalid")
    if (
        type(value["schema"]) is not str
        or value["schema"] != PLAN_SCHEMA_ID
        or type(value["schema_version"]) is not int
        or value["schema_version"] != SCHEMA_VERSION
        or type(value["hard_ceiling"]) is not int
        or value["hard_ceiling"] != MAX_ACQUISITION_CEILING
    ):
        raise CorpusError("acquisition plan schema is unsupported")
    integer_fields = (
        "cache_hit_count",
        "download_count",
        "requested_ceiling",
        "unique_game_count",
    )
    if any(type(value[key]) is not int or value[key] < 0 for key in integer_fields):
        raise CorpusError("acquisition plan counts are invalid")
    if type(value["game_ids"]) is not list or not all(
        type(item) is str for item in value["game_ids"]
    ):
        raise CorpusError("acquisition plan game IDs are invalid")
    game_ids = tuple(value["game_ids"])
    if game_ids != tuple(sorted(set(game_ids))):
        raise CorpusError("acquisition plan game IDs must be unique and sorted")
    string_fields = (
        "cache_state_identity",
        "created_at",
        "output_dir",
        "plan_identity",
        "snapshot_identity",
    )
    if any(type(value[key]) is not str or not value[key] for key in string_fields):
        raise CorpusError("acquisition plan string fields are invalid")
    normalized_created_at = normalize_timestamp(value["created_at"], "plan created_at")
    if normalized_created_at != value["created_at"]:
        raise CorpusError("plan created_at must use canonical UTC form")
    plan = AcquisitionPlan(
        created_at=value["created_at"],
        snapshot_identity=value["snapshot_identity"],
        output_dir=value["output_dir"],
        requested_ceiling=value["requested_ceiling"],
        unique_game_count=value["unique_game_count"],
        cache_hit_count=value["cache_hit_count"],
        download_count=value["download_count"],
        game_ids=tuple(value["game_ids"]),
        cache_state_identity=value["cache_state_identity"],
        plan_identity=value["plan_identity"],
    )
    if plan.unique_game_count != len(plan.game_ids):
        raise CorpusError("acquisition plan unique game count mismatch")
    if plan.cache_hit_count + plan.download_count != plan.unique_game_count:
        raise CorpusError("acquisition plan accounting mismatch")
    if not 1 <= plan.requested_ceiling <= MAX_ACQUISITION_CEILING:
        raise CorpusError("acquisition plan ceiling is outside the hard bound")
    expected_identity = sha256_bytes(
        canonical_json_bytes(
            _plan_identity_input(
                snapshot_identity=plan.snapshot_identity,
                output_dir=plan.output_dir,
                requested_ceiling=plan.requested_ceiling,
                game_ids=plan.game_ids,
                cache_identity=plan.cache_state_identity,
            )
        )
    )
    if plan.plan_identity != expected_identity:
        raise CorpusError("acquisition plan identity mismatch")
    return plan


def _aggregate_report(
    *,
    snapshot: RecentGamesSnapshot,
    entries: dict[str, dict[str, object]],
    cache_hits: int,
    downloaded: int,
    failures: list[dict[str, str]],
    corpus_identity: str | None,
    manifest_sha256: str | None,
) -> dict[str, Any]:
    valid_ids = set(snapshot.game_ids) & set(entries)
    hidden_keys = (
        "round_count",
        "all_seat_initial_hand_rounds",
        "actual_tsumo_count",
        "masked_tsumo_count",
        "missing_tsumo_count",
        "reconstructable_rounds",
    )
    hidden = {key: 0 for key in hidden_keys}
    for game_id in valid_ids:
        values = entries[game_id]["validation"]["hidden_information"]
        for key in hidden_keys:
            hidden[key] += values[key]
    complete = not failures and len(valid_ids) == len(snapshot.game_ids)
    status = "COMPLETE" if complete else ("PARTIAL" if valid_ids else "INVALID")
    technical = "GO" if complete else ("PARTIAL" if valid_ids else "NO-GO")
    counts = snapshot.participation_counts()
    return {
        "bounded_acquisition": status,
        "cache_hit_count": cache_hits,
        "corpus_identity": corpus_identity,
        "downloaded_count": downloaded,
        "failed_count": len(failures),
        "failures": failures,
        "hidden_information": hidden,
        "join_consistency": "valid" if complete else "incomplete",
        "manifest_sha256": manifest_sha256,
        "personal_noncommercial_ml_use_basis": "GO",
        "requested_game_count": len(snapshot.game_ids),
        "redistribution": "NO-GO",
        "report_generated_at": utc_now_text(),
        "schema": REPORT_SCHEMA_ID,
        "schema_version": SCHEMA_VERSION,
        "snapshot_identity": snapshot.snapshot_identity,
        "target_bot_participation_counts": {
            str(key): counts[key] for key, _ in snapshot.target_bots
        },
        "technical_corpus_quality": technical,
        "unique_game_count": len(snapshot.game_ids),
        "usable_game_count": len(valid_ids),
        "validation_counts": {
            "gzip_valid": len(valid_ids),
            "http_200": len(valid_ids),
            "jsonl_valid": len(valid_ids),
            "mjai_lifecycle_valid": len(valid_ids),
        },
    }


def _write_report(output_dir: Path, report: dict[str, Any]) -> Path:
    path = output_dir / REPORT_FILENAME
    atomic_replace(path, canonical_json_bytes(report))
    return path


def acquire_from_plan(
    snapshot: RecentGamesSnapshot,
    plan: AcquisitionPlan,
    transport: HttpTransport,
    *,
    timeout: float = 15.0,
    inter_request_sleeper: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    if plan.snapshot_identity != snapshot.snapshot_identity:
        raise CorpusError("plan does not refer to this snapshot")
    current = create_acquisition_plan(
        snapshot,
        Path(plan.output_dir),
        requested_ceiling=plan.requested_ceiling,
        created_at=plan.created_at,
    )
    if current.plan_identity != plan.plan_identity or current != plan:
        raise CorpusError("snapshot/cache state changed after plan; create a new plan")

    output_dir = Path(plan.output_dir)
    entries, hits = inspect_cache(output_dir, snapshot)
    grouped = group_participations(snapshot)
    downloaded = 0
    request_count = 0
    failures: list[dict[str, str]] = []
    for game_id in plan.game_ids:
        if game_id in hits:
            continue
        url = resolve_log_url(grouped[game_id])
        try:
            if request_count:
                inter_request_sleeper(SERIAL_REQUEST_INTERVAL_SECONDS)
            request_count += 1
            response = get_with_bounded_retry(transport, url, timeout=timeout)
            raw_length = response.headers.get("content-length")
            if raw_length is None:
                content_length = None
            else:
                try:
                    content_length = int(raw_length)
                except ValueError as exc:
                    raise CorpusError(
                        f"invalid HTTP content-length for {game_id}"
                    ) from exc
                if content_length < 0:
                    raise CorpusError(f"negative HTTP content-length for {game_id}")
            entry = persist_download(
                output_dir,
                participations=grouped[game_id],
                payload=response.body,
                http_status=response.status,
                content_length=content_length,
                retrieved_at=utc_now_text(),
            )
            entries[game_id] = entry
            downloaded += 1
        except CorpusError as exc:
            failures.append({"game_id": game_id, "reason": str(exc)})
            report = _aggregate_report(
                snapshot=snapshot,
                entries=entries,
                cache_hits=len(hits),
                downloaded=downloaded,
                failures=failures,
                corpus_identity=None,
                manifest_sha256=None,
            )
            path = _write_report(output_dir, report)
            raise AcquisitionFailed(
                f"acquisition stopped at {game_id}; failure report: {path}"
            ) from exc

    manifest = build_manifest(snapshot, entries)
    save_manifest(output_dir, manifest)
    validate_manifest_file(output_dir, snapshot, entries)
    report = _aggregate_report(
        snapshot=snapshot,
        entries=entries,
        cache_hits=len(hits),
        downloaded=downloaded,
        failures=failures,
        corpus_identity=manifest["corpus_identity"],
        manifest_sha256=manifest["manifest_sha256"],
    )
    _write_report(output_dir, report)
    return report


def validate_cached_corpus(
    snapshot: RecentGamesSnapshot, output_dir: Path
) -> dict[str, Any]:
    output = ensure_outside_git_worktree(output_dir)
    entries, hits = inspect_cache(output, snapshot)
    complete = len(hits) == len(snapshot.game_ids)
    if complete:
        manifest = validate_manifest_file(output, snapshot, entries)
    else:
        manifest = None
    report = _aggregate_report(
        snapshot=snapshot,
        entries=entries,
        cache_hits=len(hits),
        downloaded=0,
        failures=[]
        if complete
        else [{"game_id": "*", "reason": "cache does not cover the snapshot"}],
        corpus_identity=None if manifest is None else manifest["corpus_identity"],
        manifest_sha256=None if manifest is None else manifest["manifest_sha256"],
    )
    _write_report(output, report)
    return report


__all__ = [
    "AcquisitionFailed",
    "AcquisitionPlan",
    "REPORT_FILENAME",
    "SERIAL_REQUEST_INTERVAL_SECONDS",
    "acquire_from_plan",
    "create_acquisition_plan",
    "plan_from_value",
    "validate_cached_corpus",
]
