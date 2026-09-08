"""Operator CLI for the Issue #190 preflight machinery."""

import argparse
import json

from lisjong_arena.learned_policy_offline_q.errors import (
    OfflineQArtifactError,
    OfflineQProtocolError,
)
from lisjong_arena.learned_policy_stage4a.candidate import resolve_retention_target
from lisjong_arena.learned_policy_stage4a.errors import Stage4aRetentionError

from .artifact import load_artifact
from .errors import DataSufficiencyError, DataSufficiencyEvidenceBlocked
from .experiment import run_experiment
from .metrics import outcome_for_failure
from .protocol import plan_document
from .source import load_source_dataset


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Issue #190 retained-corpus data-sufficiency preflight"
    )
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("plan", help="print the locked measurement-only protocol")
    run = commands.add_parser(
        "run", help="train all four locked scales and retain one write-once artifact"
    )
    run.add_argument("--dataset", required=True)
    run.add_argument("--retention-backend", required=True)
    run.add_argument("--retention-root", required=True)
    run.add_argument("--retention-key", required=True)
    verify = commands.add_parser(
        "verify", help="strict-read an existing result artifact"
    )
    verify.add_argument("--artifact", required=True)
    return parser


def _emit(value: object) -> None:
    print(json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True))


def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    if arguments.command == "plan":
        _emit(plan_document())
        return 0
    if arguments.command == "verify":
        try:
            artifact = load_artifact(arguments.artifact)
        except DataSufficiencyError as error:
            _emit({"outcome": outcome_for_failure(error).value, "reason": str(error)})
            return 2
        _emit(
            {
                "classification": artifact.result["classification"],
                "result_identity": artifact.result["result_identity"],
            }
        )
        return 0
    try:
        target = resolve_retention_target(
            backend=arguments.retention_backend,
            root=arguments.retention_root,
            key=arguments.retention_key,
        )
        source = load_source_dataset(arguments.dataset)
        artifact = run_experiment(
            source=source,
            destination=target.bundle_path,
            backend=target.backend,
            key=target.key,
        )
    except DataSufficiencyEvidenceBlocked as error:
        _emit({"outcome": outcome_for_failure(error).value, "reason": str(error)})
        return 2
    except (
        DataSufficiencyError,
        OfflineQArtifactError,
        OfflineQProtocolError,
        Stage4aRetentionError,
        RuntimeError,
    ) as error:
        _emit({"outcome": outcome_for_failure(error).value, "reason": str(error)})
        return 2
    _emit(
        {
            "classification": artifact.result["classification"],
            "result_identity": artifact.result["result_identity"],
            "artifact": str(artifact.path),
        }
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
