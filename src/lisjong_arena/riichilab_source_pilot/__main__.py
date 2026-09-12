"""Operator CLI for the Issue #211 RiichiLab source pilot。

すべてoffline / localである。network access、新規acquisition、実corpusの
uploadは行わない。`materialize`と`run`は既にlocalへ保存済みの#170 snapshot /
cacheだけを読み、generated dataset / weights / resultはretention root
（Git worktree外）へ書く。
"""

import argparse
import json
from pathlib import Path

from lisjong_arena.learned_policy_stage4a.candidate import resolve_retention_target
from lisjong_arena.learned_policy_stage4a.errors import Stage4aRetentionError
from lisjong_arena.riichilab_corpus.models import CorpusError, snapshot_from_value
from lisjong_arena.riichilab_corpus.persistence import read_json

from .artifact import load_checkpoint, load_result, load_seed_plan
from .dataset import build_row_budget, materialize_local_corpus
from .errors import SourcePilotError
from .experiment import run_source_pilot
from .protocol import SourcePilotOutcome, plan_document
from .training import load_retained_arm_y_source


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Issue #211 RiichiLab source pilot (offline operator tooling)"
    )
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("plan", help="print the locked Issue #211 protocol")

    gate0 = commands.add_parser(
        "materialize",
        help="run Gate 0 over the exact local #170 corpus and print the report",
    )
    gate0.add_argument("--snapshot", required=True)
    gate0.add_argument("--output-dir", required=True)

    run = commands.add_parser(
        "run",
        help="run the full two-arm pilot once and retain one write-once artifact",
    )
    run.add_argument("--snapshot", required=True)
    run.add_argument("--output-dir", required=True)
    run.add_argument("--arm-y-dataset", required=True)
    run.add_argument("--retention-backend", required=True)
    run.add_argument("--retention-root", required=True)
    run.add_argument("--retention-key", required=True)

    verify = commands.add_parser(
        "verify", help="strict-read a retained artifact bundle"
    )
    verify.add_argument("--bundle", required=True)
    return parser


def _load_snapshot(path: str):
    return snapshot_from_value(read_json(Path(path), "recent-games snapshot"))


def _emit(value: object) -> None:
    print(json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True))


def _verify(bundle: Path) -> dict[str, object]:
    result = load_result(bundle / "source-pilot-result.json")
    document: dict[str, object] = {
        "outcome": result["outcome"],
        "result_identity": result["result_identity"],
    }
    seed_plan_path = bundle / "seed-plan.json"
    if seed_plan_path.exists():
        document["seed_plan_identity"] = load_seed_plan(seed_plan_path)[
            "seed_plan_identity"
        ]
    checkpoints = bundle / "checkpoints"
    if checkpoints.is_dir():
        document["checkpoints"] = {
            path.name: load_checkpoint(path).identity
            for path in sorted(checkpoints.iterdir())
        }
    return document


def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    if arguments.command == "plan":
        _emit(plan_document())
        return 0
    if arguments.command == "verify":
        try:
            _emit(_verify(Path(arguments.bundle)))
        except (SourcePilotError, OSError) as error:
            _emit(
                {
                    "outcome": SourcePilotOutcome.STOP_INVALID.value,
                    "reason": str(error),
                }
            )
            return 2
        return 0
    if arguments.command == "materialize":
        try:
            snapshot = _load_snapshot(arguments.snapshot)
            source = materialize_local_corpus(snapshot, arguments.output_dir)
        except (SourcePilotError, CorpusError, OSError) as error:
            _emit(
                {
                    "outcome": SourcePilotOutcome.STOP_INVALID.value,
                    "reason": str(error),
                }
            )
            return 2
        document: dict[str, object] = {"gate0": source.report.to_document()}
        if not source.report.gate_passed:
            document["outcome"] = (
                SourcePilotOutcome.SOURCE_MATERIALIZATION_BLOCKED.value
            )
            _emit(document)
            return 2
        try:
            budget = build_row_budget(source)
        except SourcePilotError as error:
            document["outcome"] = SourcePilotOutcome.DATA_BUDGET_NOT_MATCHABLE.value
            document["reason"] = str(error)
            _emit(document)
            return 2
        document["budget"] = {
            "train_rows": len(budget.train_rows),
            "validation_rows": len(budget.validation_rows),
            "train_game_count": len(budget.train_game_ids),
            "validation_game_count": len(budget.validation_game_ids),
            "distribution": budget.distribution_document(),
        }
        _emit(document)
        return 0

    try:
        target = resolve_retention_target(
            backend=arguments.retention_backend,
            root=arguments.retention_root,
            key=arguments.retention_key,
        )
        snapshot = _load_snapshot(arguments.snapshot)
        arm_r_source = materialize_local_corpus(snapshot, arguments.output_dir)
        arm_y_source = load_retained_arm_y_source(arguments.arm_y_dataset)
        run = run_source_pilot(
            arm_y_source=arm_y_source,
            arm_r_source=arm_r_source,
            destination=target.bundle_path,
            backend=target.backend,
            key=target.key,
        )
    except (
        SourcePilotError,
        CorpusError,
        Stage4aRetentionError,
        OSError,
        RuntimeError,
    ) as error:
        _emit({"outcome": SourcePilotOutcome.STOP_INVALID.value, "reason": str(error)})
        return 2
    _emit(
        {
            "outcome": run.result["outcome"],
            "result_identity": run.result["result_identity"],
            "artifact": str(run.path),
        }
    )
    return 0 if run.outcome is not SourcePilotOutcome.STOP_INVALID else 2


if __name__ == "__main__":
    raise SystemExit(main())
