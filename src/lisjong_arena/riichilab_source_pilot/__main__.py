"""Operator CLI for the Issue #211 RiichiLab source pilot。

すべてoffline / localである。network access、新規acquisition、実corpusの
uploadは行わない。`materialize`と`run`は既にlocalへ保存済みの#170 snapshot /
cacheだけを読み、generated dataset / weights / resultはretention root
（Git worktree外）へ書く。

```text
plan         locked protocolの印字（read-only）
materialize  Gate 0 diagnosticsのみ。NON-AUTHORITATIVE。
run          authoritative completion command。terminal outcomeを必ず残す。
verify       retained bundleのbundle-level strict readback
```

**`run`がIssue #211のauthoritative completion commandである。** `materialize`は
operatorがGate 0の成否を先に観測するためのdiagnosticであり、その出力だけでは
Issue #211のoutcomeは成立しない（artifactを書かず、reviewerが後から検証できる
evidenceを残さない）。`run`は実行開始後のterminal outcomeをすべてwrite-once
result artifactとして残し、同じretention keyでのrerunを拒否する。
"""

import argparse
import json
from pathlib import Path

from lisjong_arena.learned_policy_stage4a.candidate import resolve_retention_target
from lisjong_arena.learned_policy_stage4a.errors import Stage4aRetentionError
from lisjong_arena.riichilab_corpus.models import CorpusError, snapshot_from_value
from lisjong_arena.riichilab_corpus.persistence import read_json

from .bundle import verify_bundle
from .dataset import build_row_budget, materialize_local_corpus
from .errors import SourcePilotError
from .experiment import persist_stop_invalid, run_source_pilot
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
        help=(
            "NON-AUTHORITATIVE diagnostic: run Gate 0 over the exact local #170 "
            "corpus and print the report without retaining any artifact"
        ),
    )
    gate0.add_argument("--snapshot", required=True)
    gate0.add_argument("--output-dir", required=True)

    run = commands.add_parser(
        "run",
        help=(
            "authoritative: run the full two-arm pilot once and retain exactly one "
            "write-once result artifact for whichever terminal outcome occurs"
        ),
    )
    run.add_argument("--snapshot", required=True)
    run.add_argument("--output-dir", required=True)
    run.add_argument("--arm-y-dataset", required=True)
    run.add_argument("--retention-backend", required=True)
    run.add_argument("--retention-root", required=True)
    run.add_argument("--retention-key", required=True)

    verify = commands.add_parser(
        "verify",
        help="strict-read a retained artifact bundle and check its cross-bindings",
    )
    verify.add_argument("--bundle", required=True)
    return parser


def _load_snapshot(path: str):
    return snapshot_from_value(read_json(Path(path), "recent-games snapshot"))


def _emit(value: object) -> None:
    print(json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True))


#: `materialize`のstdout出力だけでは、Issue #211のoutcomeは成立しない。
#: observed stateとauthoritative completionを混同させないため、diagnostic
#: documentには`outcome`keyを置かず、この宣言を必ず同梱する。
DIAGNOSTIC_NOTICE = (
    "NON-AUTHORITATIVE Gate 0 diagnostic. This output retains no artifact and "
    "does not by itself constitute an Issue #211 outcome. Only the write-once "
    "result artifact published by the run command completes Issue #211."
)


def _diagnostic(document: dict[str, object]) -> dict[str, object]:
    return {**document, "authoritative": False, "notice": DIAGNOSTIC_NOTICE}


def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    if arguments.command == "plan":
        _emit(plan_document())
        return 0
    if arguments.command == "verify":
        try:
            _emit(verify_bundle(Path(arguments.bundle)))
        except (SourcePilotError, OSError) as error:
            # bundleがevidenceとして成立しないこと自体がdecision order 1番目の
            # STOP / INVALIDである。verifiedをfalseにして、bundleへ記録済みの
            # outcomeを読み取れた場合と混同させない。
            _emit(
                {
                    "outcome": SourcePilotOutcome.STOP_INVALID.value,
                    "reason": str(error),
                    "verified": False,
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
                _diagnostic(
                    {
                        "observed_outcome": SourcePilotOutcome.STOP_INVALID.value,
                        "reason": str(error),
                    }
                )
            )
            return 2
        document: dict[str, object] = {"gate0": source.report.to_document()}
        if not source.report.gate_passed:
            document["observed_outcome"] = (
                SourcePilotOutcome.SOURCE_MATERIALIZATION_BLOCKED.value
            )
            _emit(_diagnostic(document))
            return 2
        try:
            budget = build_row_budget(source)
        except SourcePilotError as error:
            document["observed_outcome"] = (
                SourcePilotOutcome.DATA_BUDGET_NOT_MATCHABLE.value
            )
            document["reason"] = str(error)
            _emit(_diagnostic(document))
            return 2
        document["budget"] = {
            "train_rows": len(budget.train_rows),
            "validation_rows": len(budget.validation_rows),
            "train_game_count": len(budget.train_game_ids),
            "validation_game_count": len(budget.validation_game_ids),
            "distribution": budget.distribution_document(),
        }
        _emit(_diagnostic(document))
        return 0

    # retention targetの解決までは実行開始前である。ここで失敗した場合は
    # 書き込む先が確定していないため、artifactを残さずSTOP / INVALIDを報告する。
    try:
        target = resolve_retention_target(
            backend=arguments.retention_backend,
            root=arguments.retention_root,
            key=arguments.retention_key,
        )
    except (Stage4aRetentionError, OSError) as error:
        _emit(
            {
                "outcome": SourcePilotOutcome.STOP_INVALID.value,
                "reason": str(error),
                "artifact": None,
                "durable": False,
            }
        )
        return 2

    # ここから先はauthoritative executionである。どのterminal outcomeも
    # write-once result artifactとして残す。
    try:
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
        _emit(_stopped(target, error))
        return 2
    _emit(
        {
            "outcome": run.result["outcome"],
            "result_identity": run.result["result_identity"],
            "artifact": str(run.path),
        }
    )
    return 0 if run.outcome is not SourcePilotOutcome.STOP_INVALID else 2


def _stopped(target, error: Exception) -> dict[str, object]:
    """実行開始後の失敗をdurableなSTOP / INVALIDとして残し、報告する。"""
    document: dict[str, object] = {
        "outcome": SourcePilotOutcome.STOP_INVALID.value,
        "reason": str(error),
        "artifact": str(target.bundle_path),
    }
    try:
        result = persist_stop_invalid(
            target.bundle_path,
            backend=target.backend,
            key=target.key,
            stop_reason=f"{type(error).__name__}: {error}",
        )
    except (SourcePilotError, OSError) as secondary:
        document["durable"] = False
        document["retention_failure"] = str(secondary)
        return document
    document["durable"] = True
    document["outcome"] = result["outcome"]
    document["result_identity"] = result["result_identity"]
    return document


if __name__ == "__main__":
    raise SystemExit(main())
