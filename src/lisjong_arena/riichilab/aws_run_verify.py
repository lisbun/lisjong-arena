"""Secret-safe verification for the bounded AWS RiichiLab run (Issue #313).

Issue #383 adds stop requests: a run may also stop with ``stop_requested`` when
the run's stop file exists, and a run may be started without a duration bound
(until stopped).  The stop file is shared by every bot of the run and records
who requested the stop first: ``operator`` (stop-riichilab.ps1) or
``bot-exited:<name>`` (a bot of the run exited, so the others stop as well).

This module intentionally does not provision AWS resources.  The AWS launcher owns
EC2/SSM lifecycle, while this module owns the testable post-run checks that depend
on Arena's durable ranked-record contract.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any

from lisjong_arena.riichilab.durable_ranked_game_record import (
    UNRESOLVED_PROVENANCE_VALUE,
    DurableRankedGameRecordError,
    load_ranked_game_record,
)

_STOP_REQUEST_SOURCE_RE = re.compile(r"operator|bot-exited:[A-Za-z0-9._-]+")
_AWS_ACCESS_KEY_RE = re.compile(rb"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")
_AWS_CREDENTIAL_NAME_RE = re.compile(
    rb"(?i)\b(?:AWS_ACCESS_KEY_ID|AWS_SECRET_ACCESS_KEY|AWS_SESSION_TOKEN)\b"
)


class AwsRunVerificationError(RuntimeError):
    """The AWS bounded-run evidence does not satisfy the Issue #313 contract."""


def _parse_utc(value: str, field_name: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise AwsRunVerificationError(
            f"{field_name} is not valid ISO-8601 UTC"
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise AwsRunVerificationError(f"{field_name} must include a timezone")
    return parsed


def _read_stop_request_source(stop_file: Path | None) -> str | None:
    """Return who requested the stop, or ``None`` when no stop was requested."""

    if stop_file is None or not os.path.lexists(stop_file):
        return None
    try:
        raw = stop_file.read_bytes()
    except OSError as exc:
        raise AwsRunVerificationError("stop file could not be read") from exc
    try:
        source = raw.decode("ascii").strip()
    except UnicodeDecodeError as exc:
        raise AwsRunVerificationError("stop file content is not recognized") from exc
    if _STOP_REQUEST_SOURCE_RE.fullmatch(source) is None:
        raise AwsRunVerificationError("stop file content is not recognized")
    return source


def _parse_runner_summary(path: Path) -> dict[str, str]:
    if not path.is_file():
        raise AwsRunVerificationError("runner log is missing")
    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if ":" not in raw_line:
            continue
        key, value = raw_line.split(":", 1)
        key = key.strip().lower()
        if key in {
            "profile",
            "requested completed games",
            "requested duration seconds",
            "completed games",
            "failed games",
            "consecutive failures",
            "last failure type",
            "records",
            "stopped reason",
        }:
            values[key] = value.strip()
    required = {
        "profile",
        "requested duration seconds",
        "completed games",
        "failed games",
        "consecutive failures",
        "last failure type",
        "records",
        "stopped reason",
    }
    missing = sorted(required - set(values))
    if missing:
        raise AwsRunVerificationError(
            "runner log is missing summary fields: " + ", ".join(missing)
        )
    return values


def _parse_nonnegative_int(value: str, field_name: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise AwsRunVerificationError(f"{field_name} is not an integer") from exc
    if parsed < 0:
        raise AwsRunVerificationError(f"{field_name} must be non-negative")
    return parsed


def _scan_paths(
    *,
    record_dir: Path,
    runner_log: Path,
    token: str,
    additional_tokens: tuple[str, ...] = (),
) -> dict[str, bool]:
    if not token or any(not extra for extra in additional_tokens):
        raise AwsRunVerificationError(
            "runtime token is unavailable for exact-byte scan"
        )
    all_token_bytes = [value.encode("utf-8") for value in (token, *additional_tokens)]
    paths = [runner_log]
    paths.extend(path for path in record_dir.rglob("*") if path.is_file())

    exact_token = False
    authorization = False
    aws_credential_name = False
    aws_access_key = False

    for path in paths:
        data = path.read_bytes()
        if any(token_bytes in data for token_bytes in all_token_bytes):
            exact_token = True
        lower = data.lower()
        if b"authorization" in lower:
            authorization = True
        if _AWS_CREDENTIAL_NAME_RE.search(data):
            aws_credential_name = True
        if _AWS_ACCESS_KEY_RE.search(data):
            aws_access_key = True

    if exact_token:
        raise AwsRunVerificationError("runtime token bytes were persisted")
    if authorization:
        raise AwsRunVerificationError("Authorization material was persisted")
    if aws_credential_name or aws_access_key:
        raise AwsRunVerificationError("AWS credential material was persisted")

    return {
        "exact_runtime_token_persisted": False,
        "authorization_material_persisted": False,
        "aws_credential_material_persisted": False,
    }


def verify_run(
    *,
    record_dir: Path,
    runner_log: Path,
    expected_arena_revision: str,
    expected_profile: str,
    expected_policy: str,
    expected_duration_seconds: int | None,
    token: str,
    start_utc: str,
    cutoff_utc: str | None,
    stop_utc: str,
    elapsed_seconds: float,
    stop_file: Path | None = None,
    additional_tokens: tuple[str, ...] = (),
) -> dict[str, Any]:
    """Strict-read all records and return a secret-safe completion document.

    ``expected_duration_seconds=None`` verifies an until-stopped run; it then
    requires ``stop_file`` and a ``stop_requested`` stop.  ``stop_requested`` is
    accepted only when ``stop_file`` is given and exists, so a run that stopped
    for any other reason fails closed.  An existing stop file must name a known
    source.  ``additional_tokens`` are the other bots' runtime tokens of the same
    run (Issue #386); their exact bytes must not be persisted either.
    """

    if expected_duration_seconds is not None and expected_duration_seconds <= 0:
        raise AwsRunVerificationError("expected duration must be positive")
    if expected_duration_seconds is None and stop_file is None:
        raise AwsRunVerificationError("an until-stopped run requires a stop file")
    if (expected_duration_seconds is None) != (cutoff_utc is None):
        raise AwsRunVerificationError(
            "cutoff UTC must be given exactly when a duration is expected"
        )
    if elapsed_seconds < 0:
        raise AwsRunVerificationError("elapsed seconds must be non-negative")
    if not record_dir.is_dir():
        raise AwsRunVerificationError("record directory is missing")

    runner = _parse_runner_summary(runner_log)
    if runner["profile"] != expected_profile:
        raise AwsRunVerificationError("runner profile does not match expected profile")
    if runner["records"] != "on":
        raise AwsRunVerificationError("durable records were not enabled")
    stopped_reason = runner["stopped reason"]
    stop_request_source = _read_stop_request_source(stop_file)
    if stopped_reason == "stop_requested":
        if stop_request_source is None:
            raise AwsRunVerificationError(
                "runner stopped on request but no stop file exists"
            )
    elif stopped_reason != "duration_reached" or expected_duration_seconds is None:
        raise AwsRunVerificationError(
            "runner did not stop because duration was reached or the operator "
            "requested a stop"
        )
    requested_duration = (
        "unbounded"
        if expected_duration_seconds is None
        else str(expected_duration_seconds)
    )
    if runner["requested duration seconds"] != requested_duration:
        raise AwsRunVerificationError(
            "runner duration does not match expected duration"
        )

    completed_games = _parse_nonnegative_int(
        runner["completed games"], "completed games"
    )
    failed_games = _parse_nonnegative_int(runner["failed games"], "failed games")
    consecutive_failures = _parse_nonnegative_int(
        runner["consecutive failures"], "consecutive failures"
    )

    record_paths = sorted(
        path
        for path in record_dir.iterdir()
        if path.is_dir() and not path.name.startswith(".")
    )
    # A stop may be requested before the first hanchan completes; any other
    # stop must have published at least one durable record.
    if not record_paths and stopped_reason != "stop_requested":
        raise AwsRunVerificationError("no published durable records were found")

    records = []
    for path in record_paths:
        try:
            records.append(load_ranked_game_record(path))
        except (DurableRankedGameRecordError, OSError) as exc:
            raise AwsRunVerificationError(
                f"durable record failed strict readback: {path.name}"
            ) from exc

    if completed_games != len(records):
        raise AwsRunVerificationError(
            "completed-game count does not match published durable-record count"
        )

    identities = [record.record_identity for record in records]
    if len(set(identities)) != len(identities):
        raise AwsRunVerificationError("durable record identities are not unique")

    first_provenance: dict[str, Any] | None = None
    if records:
        first_provenance = asdict(records[0].provenance)
        for record in records[1:]:
            if asdict(record.provenance) != first_provenance:
                raise AwsRunVerificationError(
                    "durable record provenance is inconsistent"
                )

        if first_provenance["profile_identity"] != expected_profile:
            raise AwsRunVerificationError("record profile provenance is unexpected")
        if first_provenance["policy_identity"] != expected_policy:
            raise AwsRunVerificationError("record Policy provenance is unexpected")
        if first_provenance["lisjong_arena_revision"] != expected_arena_revision:
            raise AwsRunVerificationError("record Arena revision is unexpected")
        unresolved = sorted(
            key
            for key, value in first_provenance.items()
            if value == UNRESOLVED_PROVENANCE_VALUE
        )
        if unresolved:
            raise AwsRunVerificationError(
                "record provenance contains unresolved fields: " + ", ".join(unresolved)
            )

    start = _parse_utc(start_utc, "start UTC")
    stop = _parse_utc(stop_utc, "stop UTC")
    if stop < start:
        raise AwsRunVerificationError("stop UTC precedes start UTC")
    if expected_duration_seconds is not None:
        assert cutoff_utc is not None
        cutoff = _parse_utc(cutoff_utc, "cutoff UTC")
        duration_delta = (cutoff - start).total_seconds()
        if abs(duration_delta - expected_duration_seconds) > 1:
            raise AwsRunVerificationError(
                "cutoff UTC does not match requested duration"
            )
        if stopped_reason == "duration_reached":
            if stop < cutoff:
                raise AwsRunVerificationError("stop UTC precedes the graceful cutoff")
            if elapsed_seconds + 1 < expected_duration_seconds:
                raise AwsRunVerificationError(
                    "elapsed runtime is shorter than requested duration"
                )

    credential_scan = _scan_paths(
        record_dir=record_dir,
        runner_log=runner_log,
        token=token,
        additional_tokens=tuple(additional_tokens),
    )

    return {
        "schema_id": "lisjong-arena-aws-riichilab-bounded-run-summary",
        "schema_version": 2,
        "status": "PASS",
        "start_utc": start_utc,
        "cutoff_utc": cutoff_utc,
        "stop_utc": stop_utc,
        "actual_elapsed_seconds": elapsed_seconds,
        "requested_duration_seconds": expected_duration_seconds,
        "completed_games": completed_games,
        "failed_games": failed_games,
        "retry_or_disconnect_occurred": failed_games > 0,
        "final_consecutive_failures": consecutive_failures,
        "last_failure_type": runner["last failure type"],
        "stopped_reason": stopped_reason,
        "stop_request_source": stop_request_source,
        "operator_stop_requested": stop_request_source == "operator",
        "record_count": len(records),
        "strict_readback_pass_count": len(records),
        "strict_readback_failure_count": 0,
        "unique_record_identity_count": len(set(identities)),
        "provenance": first_provenance,
        "credential_scan": credential_scan,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m lisjong_arena.riichilab.aws_run_verify",
        description="Verify Issue #313 AWS bounded-run evidence without exposing secrets.",
    )
    parser.add_argument("--record-dir", required=True, type=Path)
    parser.add_argument("--runner-log", required=True, type=Path)
    parser.add_argument("--expected-arena-revision", required=True)
    parser.add_argument("--expected-profile", default="lisjong-dev")
    parser.add_argument(
        "--expected-policy", default="MechanismRiichiDefenseYakuhaiCallPolicy"
    )
    duration = parser.add_mutually_exclusive_group(required=True)
    duration.add_argument("--expected-duration-seconds", type=int)
    duration.add_argument(
        "--until-stopped",
        action="store_true",
        help="the run had no duration bound and must stop on operator request",
    )
    parser.add_argument("--stop-file", type=Path, default=None)
    parser.add_argument("--token-env", default="LISJONG_DEV_BOT_TOKEN")
    parser.add_argument("--start-utc", required=True)
    parser.add_argument("--cutoff-utc", default=None)
    parser.add_argument("--stop-utc", required=True)
    parser.add_argument("--elapsed-seconds", required=True, type=float)
    return parser


def _run_cli(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    token = os.environ.get(args.token_env)
    if not token:
        print(
            f"{args.token_env} is not set for verification",
            file=sys.stderr,
        )
        return 2
    try:
        summary = verify_run(
            record_dir=args.record_dir,
            runner_log=args.runner_log,
            expected_arena_revision=args.expected_arena_revision,
            expected_profile=args.expected_profile,
            expected_policy=args.expected_policy,
            expected_duration_seconds=args.expected_duration_seconds,
            token=token,
            start_utc=args.start_utc,
            cutoff_utc=args.cutoff_utc,
            stop_utc=args.stop_utc,
            elapsed_seconds=args.elapsed_seconds,
            stop_file=args.stop_file,
        )
    except AwsRunVerificationError as exc:
        print(f"AWS run verification failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(summary, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(_run_cli())


__all__ = [
    "AwsRunVerificationError",
    "verify_run",
]
