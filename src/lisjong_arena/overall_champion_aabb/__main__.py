"""Issue #250 operator CLI.

real Overall formal executionは意図的にpost-merge operator作業であり、
CIやtestからは実行しない。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from lisjong_arena.model import PolicySpec

from .experiment import OverallEvaluationOutcome, run_overall_evaluation
from .lock import (
    OverallChampionLockError,
    build_lock_document,
    load_lock_document,
    locked_participants,
    save_lock_document,
)
from .protocol import (
    IMPLEMENTATION_SOURCES,
    STOP_INVALID_LABEL,
    OverallChampionProtocolError,
    ParticipantBinding,
    resolve_binding_callable,
)
from .result import OverallChampionResultError, verify_overall_bundle
from .statistics import OverallChampionStatisticsError

_ERRORS = (
    OverallChampionLockError,
    OverallChampionProtocolError,
    OverallChampionResultError,
    OverallChampionStatisticsError,
)


def _parse_seeds(raw: str) -> tuple[int, ...]:
    """``START:END``または``a,b,c``形式のordered seed populationを読む。

    件数がprotocol v1のseed-block countと一致するかはlock側が検証する。
    """
    values: list[int] = []
    for chunk in raw.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        if ":" in chunk:
            parts = chunk.split(":")
            if len(parts) != 2:
                raise argparse.ArgumentTypeError("seed range must be START:END")
            try:
                start, end = (int(part) for part in parts)
            except ValueError:
                raise argparse.ArgumentTypeError(
                    "seed range endpoints must be integers"
                ) from None
            if start < 0 or end < start:
                raise argparse.ArgumentTypeError(
                    "seed range must be non-negative and ordered"
                )
            values.extend(range(start, end + 1))
            continue
        try:
            value = int(chunk)
        except ValueError:
            raise argparse.ArgumentTypeError(
                "seeds must be comma-separated ints or START:END ranges"
            ) from None
        if value < 0:
            raise argparse.ArgumentTypeError("seeds must be non-negative")
        values.append(value)
    if not values:
        raise argparse.ArgumentTypeError("seeds must not be empty")
    return tuple(values)


def _spec_from_binding(binding: ParticipantBinding) -> PolicySpec:
    return PolicySpec(
        identity=binding.policy_identity,
        factory=resolve_binding_callable(binding.factory_binding),  # type: ignore[arg-type]
    )


def _binding(arguments: argparse.Namespace, family: str) -> ParticipantBinding:
    return ParticipantBinding(
        family=family,
        policy_identity=getattr(arguments, f"{family}_identity"),
        factory_binding=getattr(arguments, f"{family}_factory"),
        implementation_source=getattr(arguments, f"{family}_source"),
        implementation_revision=getattr(arguments, f"{family}_revision"),
        checkpoint_binding=getattr(arguments, f"{family}_checkpoint_binding"),
        checkpoint_identity=getattr(arguments, f"{family}_checkpoint"),
        checkpoint_digest=getattr(arguments, f"{family}_checkpoint_digest"),
    )


def _lock(arguments: argparse.Namespace) -> int:
    document = build_lock_document(
        destinations={
            "comparison_artifact": arguments.comparison_artifact,
            "overall_result": arguments.overall_result,
        },
        heuristic=_binding(arguments, "heuristic"),
        learning=_binding(arguments, "learning"),
        seeds=arguments.seeds,
        max_workers=arguments.workers,
        ml_runtime_packages=tuple(arguments.ml_runtime_package or ()),
    )
    path = save_lock_document(document, arguments.out)
    print(f"lock_identity={document['lock_identity']}")
    print(f"seed_block_count={len(arguments.seeds)}")
    print("result_exposed=false")
    print(f"lock_written={path}")
    return 0


def _run(arguments: argparse.Namespace) -> int:
    lock = load_lock_document(arguments.lock)
    heuristic, learning = locked_participants(lock)
    outcome: OverallEvaluationOutcome = run_overall_evaluation(
        lock_path=arguments.lock,
        heuristic_spec=_spec_from_binding(heuristic),
        learning_spec=_spec_from_binding(learning),
    )
    _report(outcome.overall_result)
    print(f"comparison_artifact={outcome.comparison_artifact_path}")
    print(f"overall_result={outcome.overall_result_path}")
    return 0


def _verify(arguments: argparse.Namespace) -> int:
    document = verify_overall_bundle(
        lock_path=arguments.lock,
        comparison_path=arguments.comparison,
        result_path=arguments.result,
    )
    _report(document)
    return 0


def _report(document: dict[str, object]) -> None:
    classification = document["classification"]
    primary = document["primary"]
    assert isinstance(classification, dict)
    assert isinstance(primary, dict)
    print(f"result_identity={document['result_identity']}")
    print(f"classification={classification['label']}")
    print(f"primary_summary={primary['summary']}")
    print(f"block_sign_counts={primary['block_sign_counts']}")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m lisjong_arena.overall_champion_aabb",
        description="Issue #250 Overall Champion AABB half-game formal protocol v1",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    lock = commands.add_parser("lock", help="build the pre-execution lock")
    lock.add_argument("--out", type=Path, required=True)
    lock.add_argument("--seeds", type=_parse_seeds, required=True)
    lock.add_argument("--workers", type=int, required=True)
    lock.add_argument("--comparison-artifact", type=Path, required=True)
    lock.add_argument("--overall-result", type=Path, required=True)
    lock.add_argument("--ml-runtime-package", action="append", default=None)
    for family in ("heuristic", "learning"):
        lock.add_argument(f"--{family}-identity", required=True)
        lock.add_argument(f"--{family}-factory", required=True)
        lock.add_argument(
            f"--{family}-source", required=True, choices=sorted(IMPLEMENTATION_SOURCES)
        )
        lock.add_argument(f"--{family}-revision", required=True)
        lock.add_argument(f"--{family}-checkpoint-binding", default=None)
        lock.add_argument(f"--{family}-checkpoint", default=None)
        lock.add_argument(f"--{family}-checkpoint-digest", default=None)
    lock.set_defaults(handler=_lock)

    run = commands.add_parser("run", help="run the one-shot locked Overall event")
    run.add_argument("--lock", type=Path, required=True)
    run.set_defaults(handler=_run)

    verify = commands.add_parser("verify", help="strictly verify a finished bundle")
    verify.add_argument("--lock", type=Path, required=True)
    verify.add_argument("--comparison", type=Path, required=True)
    verify.add_argument("--result", type=Path, required=True)
    verify.set_defaults(handler=_verify)
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        return int(arguments.handler(arguments))
    except _ERRORS as exc:
        print(f"classification={STOP_INVALID_LABEL}", file=sys.stderr)
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
