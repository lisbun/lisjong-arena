"""Issue #155 durable local game recordのthin operator CLI。"""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from pathlib import Path

from lisjong.policy_contract import Seat

from lisjong_arena._artifact_io import canonical_json_text
from lisjong_arena.durable_local_game_record import (
    DurableLocalGameRecordError,
    load_local_game_record,
    run_and_save_local_game_record,
    summarize_local_game_record,
)
from lisjong_arena.model import PolicySpec
from lisjong_arena.policy_reference import resolve_policy_reference


def _seat_mapping(values: Sequence[str], option: str) -> dict[Seat, str]:
    result = {}
    for value in values:
        try:
            seat_text, item = value.split("=", 1)
            seat = Seat(int(seat_text))
        except ValueError, TypeError:
            raise DurableLocalGameRecordError(
                f"{option} expects SEAT=VALUE with SEAT 0..3"
            ) from None
        if not item or seat in result:
            raise DurableLocalGameRecordError(
                f"{option} has an empty or duplicate seat assignment"
            )
        result[seat] = item
    return result


def _resolve_cli_policies(
    references: Sequence[str], identities: Sequence[str]
) -> dict[Seat, PolicySpec]:
    reference_by_seat = _seat_mapping(references, "--policy")
    identity_by_seat = _seat_mapping(identities, "--policy-id")
    if set(reference_by_seat) != set(Seat):
        raise DurableLocalGameRecordError(
            "--policy must assign each seat 0..3 exactly once"
        )
    result = {}
    for seat in Seat:
        reference = reference_by_seat[seat]
        explicit_identity = identity_by_seat.get(seat)
        if (":" in reference) != (explicit_identity is not None):
            raise DurableLocalGameRecordError(
                "explicit package.module:attribute policies require a matching "
                "--policy-id; catalog aliases must not use one"
            )
        result[seat] = resolve_policy_reference(
            reference, explicit_identity=explicit_identity
        )
    if set(identity_by_seat) - set(reference_by_seat):
        raise DurableLocalGameRecordError("--policy-id contains an unassigned seat")
    return result


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Write or inspect a strict Arena durable local game record"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    record = subparsers.add_parser(
        "record", help="run one standard RiichiEnv local game and persist it"
    )
    record.add_argument("--seed", type=int, required=True)
    record.add_argument("--game-mode", default="4p-red-half")
    record.add_argument("--max-steps", type=int)
    record.add_argument(
        "--policy",
        action="append",
        default=[],
        metavar="SEAT=REFERENCE",
        help="repeat once for seats 0..3",
    )
    record.add_argument(
        "--policy-id",
        action="append",
        default=[],
        metavar="SEAT=IDENTITY",
        help="required only for explicit package.module:attribute references",
    )
    record.add_argument("--output", type=Path, required=True)
    summary = subparsers.add_parser(
        "summary",
        help="strictly load a completed record and print a deterministic summary",
    )
    summary.add_argument("path", type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    if arguments.command == "record":
        record = run_and_save_local_game_record(
            _resolve_cli_policies(arguments.policy, arguments.policy_id),
            seed=arguments.seed,
            path=arguments.output,
            game_mode=arguments.game_mode,
            max_steps=arguments.max_steps,
        )
        summary = summarize_local_game_record(record)
    else:
        summary = summarize_local_game_record(load_local_game_record(arguments.path))
    print(
        canonical_json_text(
            {
                "decisions": summary.decisions,
                "decisions_with_analysis": summary.decisions_with_analysis,
                "game_mode": summary.game_mode,
                "record_identity": summary.record_identity,
                "seed": summary.seed,
                "steps": summary.steps,
            }
        ),
        end="",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
