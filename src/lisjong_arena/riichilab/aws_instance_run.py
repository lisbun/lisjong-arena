"""Several RiichiLab bots on one AWS instance (Issue #386).

The AWS bootstrap supervises one continuous runner process per configured bot.
This module owns the testable parts of that run that depend on Arena contracts:

- ``check-config``: validate the explicit bot list before any credential is
  fetched (known profile, expected Policy, no duplicate profile / secret, bot
  count, deterministic collision-free spectate ports)
- ``verify``: after every bot has exited, verify each bot's evidence
  independently with :func:`verify_run` and aggregate an instance-level,
  secret-safe summary.  Overall PASS requires every bot to pass.

It does not start processes or provision AWS resources.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from lisjong_arena.riichilab.aws_run_verify import (
    AwsRunVerificationError,
    _parse_utc,
    _read_stop_request_source,
    verify_run,
)
from lisjong_arena.riichilab.profile import ProfileError, resolve_profile

INSTANCE_SUMMARY_SCHEMA_ID = "lisjong-arena-aws-riichilab-instance-run-summary"
INSTANCE_SUMMARY_SCHEMA_VERSION = 1

#: Upper bound of bots per instance.  The instance summary returns through the
#: SSM stdout sentinel, whose output is truncated at 24,000 characters.
MAX_BOTS = 4

#: Explicit expected Policy per supported profile.  It is deliberately not
#: derived from the profile itself: the runtime check below compares the two.
EXPECTED_POLICY_BY_PROFILE: Mapping[str, str] = {
    "lisjong-dev": "MechanismRiichiDefenseYakuhaiCallPolicy",
    "lisjong-baseline": "MinimalPolicy",
    "lisjong": "MinimalPolicy",
}

_SECRET_ID_RE = re.compile(r"[A-Za-z0-9/_+=.@-]+")
_MIN_SPECTATE_PORT = 1024
_MAX_PORT = 65535


class AwsInstanceRunConfigError(ValueError):
    """The requested multi-bot configuration cannot be run safely."""


@dataclass(frozen=True, slots=True)
class BotConfig:
    """One configured bot.  Contains identifiers only, never a credential."""

    profile: str
    secret_id: str
    spectate_port: int | None

    @property
    def expected_policy(self) -> str:
        return EXPECTED_POLICY_BY_PROFILE[self.profile]

    @property
    def credential_env_var(self) -> str:
        return resolve_profile(self.profile).credential_env_var


def parse_bot_configs(
    specs: Sequence[str], *, spectate_base_port: int | None
) -> tuple[BotConfig, ...]:
    """Parse ``PROFILE=SECRET_ID`` specs in the configured (launch) order.

    Spectate ports are ``spectate_base_port + index`` in that order.
    """

    if not specs:
        raise AwsInstanceRunConfigError("at least one bot is required")
    if len(specs) > MAX_BOTS:
        raise AwsInstanceRunConfigError(f"at most {MAX_BOTS} bots are supported")
    if spectate_base_port is not None and not (
        _MIN_SPECTATE_PORT <= spectate_base_port
        and spectate_base_port + len(specs) - 1 <= _MAX_PORT
    ):
        raise AwsInstanceRunConfigError(
            f"spectate ports must stay within {_MIN_SPECTATE_PORT}-{_MAX_PORT}"
        )

    bots: list[BotConfig] = []
    for index, spec in enumerate(specs):
        profile, separator, secret_id = spec.partition("=")
        if not separator or not profile or not secret_id:
            raise AwsInstanceRunConfigError(
                f"bot #{index + 1} must be PROFILE=SECRET_ID"
            )
        if profile not in EXPECTED_POLICY_BY_PROFILE:
            raise AwsInstanceRunConfigError(
                f"bot #{index + 1} uses an unsupported profile: {profile!r}"
            )
        if _SECRET_ID_RE.fullmatch(secret_id) is None:
            raise AwsInstanceRunConfigError(
                f"bot #{index + 1} has an invalid secret id"
            )
        port = None if spectate_base_port is None else spectate_base_port + index
        bots.append(BotConfig(profile=profile, secret_id=secret_id, spectate_port=port))

    for field, label in (("profile", "profile"), ("secret_id", "secret id")):
        values = [getattr(bot, field) for bot in bots]
        if len(set(values)) != len(values):
            raise AwsInstanceRunConfigError(f"duplicate bot {label}")
    env_vars = [bot.credential_env_var for bot in bots]
    if len(set(env_vars)) != len(env_vars):
        raise AwsInstanceRunConfigError("bots share a credential variable")
    return tuple(bots)


def check_runtime_policies(bots: Sequence[BotConfig]) -> None:
    """Fail closed unless each profile builds exactly its expected Policy."""

    for bot in bots:
        policy = resolve_profile(bot.profile).policy_factory()
        if type(policy).__name__ != bot.expected_policy:
            raise AwsInstanceRunConfigError(
                f"profile {bot.profile} does not build {bot.expected_policy}"
            )


def bot_directory(work_root: Path, profile: str) -> Path:
    """Per-bot evidence directory; profiles are unique, so paths never collide."""

    return work_root / "bots" / profile


def _verify_bot(
    *,
    work_root: Path,
    bot: BotConfig,
    tokens_by_profile: Mapping[str, str],
    expected_arena_revision: str,
    expected_duration_seconds: int | None,
    start_utc: str,
    cutoff_utc: str | None,
    stop_file: Path,
) -> dict[str, Any]:
    directory = bot_directory(work_root, bot.profile)
    entry: dict[str, Any] = {
        "profile": bot.profile,
        "secret_id": bot.secret_id,
        "expected_policy": bot.expected_policy,
        "spectate_port": bot.spectate_port,
        "evidence_directory": str(directory),
        "exit_code": None,
        "stop_utc": None,
        "status": "FAIL",
        "failure_reason": None,
        "verification": None,
    }
    try:
        try:
            exit_code = int((directory / "exit_code").read_text("ascii").strip())
            stop_utc = (directory / "stop_utc").read_text("ascii").strip()
        except OSError, ValueError, UnicodeDecodeError:
            raise AwsRunVerificationError(
                "bot exit evidence is missing or unreadable"
            ) from None
        entry["exit_code"] = exit_code
        entry["stop_utc"] = stop_utc
        if exit_code != 0:
            raise AwsRunVerificationError(f"bot runner exited with code {exit_code}")
        elapsed = (
            _parse_utc(stop_utc, "bot stop UTC") - _parse_utc(start_utc, "start UTC")
        ).total_seconds()
        entry["verification"] = verify_run(
            record_dir=directory / "records",
            runner_log=directory / "continuous.log",
            expected_arena_revision=expected_arena_revision,
            expected_profile=bot.profile,
            expected_policy=bot.expected_policy,
            expected_duration_seconds=expected_duration_seconds,
            token=tokens_by_profile[bot.profile],
            additional_tokens=tuple(
                token
                for profile, token in tokens_by_profile.items()
                if profile != bot.profile
            ),
            start_utc=start_utc,
            cutoff_utc=cutoff_utc,
            stop_utc=stop_utc,
            elapsed_seconds=elapsed,
            stop_file=stop_file,
        )
    except AwsRunVerificationError as error:
        entry["failure_reason"] = str(error)
        return entry
    entry["status"] = "PASS"
    return entry


def verify_instance_run(
    *,
    work_root: Path,
    bots: Sequence[BotConfig],
    tokens_by_profile: Mapping[str, str],
    expected_arena_revision: str,
    expected_duration_seconds: int | None,
    start_utc: str,
    cutoff_utc: str | None,
    stop_file: Path,
) -> dict[str, Any]:
    """Verify every bot independently and aggregate a secret-safe summary.

    A failing bot never hides behind passing ones: overall PASS requires every
    configured bot to PASS.  ``tokens_by_profile`` must hold every bot's token;
    each bot's evidence is scanned for all of them.
    """

    if not bots:
        raise AwsInstanceRunConfigError("at least one bot is required")
    if set(tokens_by_profile) != {bot.profile for bot in bots} or not all(
        tokens_by_profile.values()
    ):
        raise AwsInstanceRunConfigError("a runtime token is missing for a bot")
    if len(set(tokens_by_profile.values())) != len(tokens_by_profile):
        raise AwsInstanceRunConfigError("bots must not share a runtime token")

    instance_failure_reason: str | None = None
    try:
        stop_request_source = _read_stop_request_source(stop_file)
    except AwsRunVerificationError as error:
        stop_request_source = None
        instance_failure_reason = str(error)

    entries = [
        _verify_bot(
            work_root=work_root,
            bot=bot,
            tokens_by_profile=tokens_by_profile,
            expected_arena_revision=expected_arena_revision,
            expected_duration_seconds=expected_duration_seconds,
            start_utc=start_utc,
            cutoff_utc=cutoff_utc,
            stop_file=stop_file,
        )
        for bot in bots
    ]
    passed = sum(1 for entry in entries if entry["status"] == "PASS")
    stop_times = []
    for entry in entries:
        try:
            stop_times.append(_parse_utc(entry["stop_utc"], "bot stop UTC"))
        except AwsRunVerificationError, AttributeError:
            continue
    last_stop = max(stop_times, default=None)
    status = (
        "PASS" if instance_failure_reason is None and passed == len(entries) else "FAIL"
    )
    return {
        "schema_id": INSTANCE_SUMMARY_SCHEMA_ID,
        "schema_version": INSTANCE_SUMMARY_SCHEMA_VERSION,
        "status": status,
        "instance_failure_reason": instance_failure_reason,
        "start_utc": start_utc,
        "cutoff_utc": cutoff_utc,
        "stop_utc": (
            None if last_stop is None else last_stop.strftime("%Y-%m-%dT%H:%M:%SZ")
        ),
        "requested_duration_seconds": expected_duration_seconds,
        "until_stopped": expected_duration_seconds is None,
        "configured_bots": [bot.profile for bot in bots],
        "bot_count": len(entries),
        "passed_bot_count": passed,
        "stop_request_source": stop_request_source,
        "bots": entries,
    }


def _add_bot_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--bot",
        action="append",
        default=[],
        metavar="PROFILE=SECRET_ID",
        help="configured bot, in launch order (repeatable)",
    )
    parser.add_argument("--spectate-base-port", type=int, default=None)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m lisjong_arena.riichilab.aws_instance_run",
        description="Issue #386 multi-bot AWS RiichiLab run checks (secret-safe).",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    check = commands.add_parser(
        "check-config",
        help="validate the bot list; print PROFILE, ENV, POLICY, PORT per line",
    )
    _add_bot_arguments(check)

    verify = commands.add_parser(
        "verify", help="verify every bot and print the instance summary JSON"
    )
    _add_bot_arguments(verify)
    verify.add_argument("--work-root", required=True, type=Path)
    verify.add_argument("--expected-arena-revision", required=True)
    duration = verify.add_mutually_exclusive_group(required=True)
    duration.add_argument("--expected-duration-seconds", type=int)
    duration.add_argument("--until-stopped", action="store_true")
    verify.add_argument("--start-utc", required=True)
    verify.add_argument("--cutoff-utc", default=None)
    verify.add_argument("--stop-file", required=True, type=Path)
    return parser


def _run_cli(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        bots = parse_bot_configs(args.bot, spectate_base_port=args.spectate_base_port)
        if args.command == "check-config":
            check_runtime_policies(bots)
            for bot in bots:
                port = "-" if bot.spectate_port is None else str(bot.spectate_port)
                print(
                    "\t".join(
                        (bot.profile, bot.credential_env_var, bot.expected_policy, port)
                    )
                )
            return 0
        tokens = {
            bot.profile: os.environ.get(bot.credential_env_var, "") for bot in bots
        }
        summary = verify_instance_run(
            work_root=args.work_root,
            bots=bots,
            tokens_by_profile=tokens,
            expected_arena_revision=args.expected_arena_revision,
            expected_duration_seconds=args.expected_duration_seconds,
            start_utc=args.start_utc,
            cutoff_utc=args.cutoff_utc,
            stop_file=args.stop_file,
        )
    except (AwsInstanceRunConfigError, ProfileError) as error:
        print(f"AWS instance run configuration rejected: {error}", file=sys.stderr)
        return 2
    print(json.dumps(summary, sort_keys=True, separators=(",", ":")))
    return 0 if summary["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(_run_cli())


__all__ = [
    "EXPECTED_POLICY_BY_PROFILE",
    "INSTANCE_SUMMARY_SCHEMA_ID",
    "INSTANCE_SUMMARY_SCHEMA_VERSION",
    "MAX_BOTS",
    "AwsInstanceRunConfigError",
    "BotConfig",
    "bot_directory",
    "check_runtime_policies",
    "parse_bot_configs",
    "verify_instance_run",
]
