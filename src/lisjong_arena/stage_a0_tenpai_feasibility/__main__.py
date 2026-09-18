"""Stage A0 feasibility CLI。

operatorがclean merged mainから実行するentry point。

```text
python -m lisjong_arena.stage_a0_tenpai_feasibility retained-qualify \
    --dataset <retained corpus> --sidecar <out dir> --report <out file>

python -m lisjong_arena.stage_a0_tenpai_feasibility fresh-smoke \
    --retained-report <retained report> --sidecar <out dir> --report <out file>

python -m lisjong_arena.stage_a0_tenpai_feasibility recompute --sidecar <dir>
```

raw sidecarはconcealed handを含むためGitへcommitしない。Gitへ置いてよいのは
schema / code / tests / aggregate report / digestsだけである。
"""

import argparse
import json
import sys
from pathlib import Path

from lisjong_arena.learned_policy_offline_q.artifact import provenance_document

from .errors import StageA0SidecarError
from .fresh import (
    FRESH_LIVE_LABEL_PATH_QUALIFIED,
    qualify_fresh_live_label,
)
from .protocol import SMOKE_POPULATION_IDENTITY, SMOKE_SEEDS
from .report import (
    RETAINED_AUGMENTATION_QUALIFIED,
    STOP_INVALID,
    TENPAI_LABEL_PATH_BLOCKED,
    FeasibilityReport,
    QualificationCheck,
    build_checks,
    load_feasibility_report,
    require_retained_not_qualified,
)
from .retained import qualify_retained_augmentation
from .sidecar import (
    ROUTE_FRESH,
    ROUTE_RETAINED,
    availability_counts,
    load_sidecar,
    verify_deterministic_recomputation,
    write_sidecar,
)


def _recomputation_check(sidecar) -> QualificationCheck:
    try:
        count = verify_deterministic_recomputation(sidecar)
    except StageA0SidecarError as error:
        # recomputation不一致だけがqualification failureであり、それ以外の
        # 例外はroute outcomeへ丸めず伝播させる。
        return QualificationCheck(
            name="deterministic_recomputation",
            qualified=False,
            detail=f"{type(error).__name__}: {error}",
        )
    return QualificationCheck(
        name="deterministic_recomputation",
        qualified=True,
        detail=(
            f"{count} cells recompute to their stored Stage A0 target from the "
            "same sidecar bytes under the bound canonical implementation"
        ),
    )


def _failed_checks(checks) -> tuple[str, ...]:
    """qualification checkのうち失敗したものの名前を返す。

    `QualificationCheck`はすべて「route が利用可能か」ではなく「evidence と
    internal invariant が壊れていないか」を表す。したがって1つでも失敗した
    場合、それは route unavailable ではなく protocol / evidence violation で
    あり、#258 の `STOP / INVALID` になる。fresh fallback の理由にも
    `TENPAI LABEL PATH BLOCKED` にも変換しない。
    """
    return tuple(check.name for check in checks if not check.qualified)


def _emit(report: FeasibilityReport, path: Path) -> int:
    report.write(path)
    json.dump(report.to_document()["outcome"], sys.stdout, sort_keys=True)
    sys.stdout.write("\n")
    return 0


def _run_retained(arguments) -> int:
    provenance = provenance_document()
    qualification = qualify_retained_augmentation(arguments.dataset)
    cells = qualification.cells
    sidecar = None
    recomputation = None
    if qualification.is_qualified:
        sidecar = write_sidecar(
            arguments.sidecar,
            cells,
            route=ROUTE_RETAINED,
            source_identity=qualification.dataset_identity,
            provenance=provenance,
        )
        recomputation = _recomputation_check(sidecar)

    checks = build_checks(cells, deterministic_recomputation=recomputation)
    failed = _failed_checks(checks)
    if failed:
        # precondition は満たされ exact replay も実行済みで、その後の
        # invariant / evidence validation が壊れた状態。
        hard_outcome = STOP_INVALID
        pending = None
        recommended = None
    elif qualification.is_qualified:
        hard_outcome = RETAINED_AUGMENTATION_QUALIFIED
        pending = None
        recommended = "retained-augmentation"
    elif qualification.authorizes_fresh_fallback:
        hard_outcome = None
        pending = (
            "retained augmentation is disqualified by exact row alignment "
            f"({qualification.rejection_reason}); run fresh-smoke with this "
            "report to qualify the fresh live-label path before selecting a "
            "Stage A0 corpus route"
        )
        recommended = None
    else:
        hard_outcome = None
        pending = (
            "the retained qualification preconditions are not met "
            f"({qualification.rejection_reason}); exact row alignment has not "
            "been attempted under the required conditions yet. Reproduce the "
            "historical execution environment recorded in the retained manifest "
            "and re-run retained-qualify. This report cannot authorize the "
            "fresh live-label fallback"
        )
        recommended = None

    report = FeasibilityReport(
        provenance=provenance,
        instrumentation_provenance=qualification.instrumentation_provenance,
        source_paths_examined=(str(arguments.dataset),),
        retained_corpus_identity=qualification.dataset_identity,
        retained_outcome=qualification.to_document(),
        retained_evidence=None,
        smoke_population_identity=None,
        fresh_outcome=None,
        sidecar_identity=None if sidecar is None else sidecar.identity,
        row_count=len({cell.row_identity.decision_key for cell in cells}),
        cell_count=len(cells),
        availability_counts=availability_counts(cells),
        checks=checks,
        hard_outcome=hard_outcome,
        pending_reason=pending,
        recommended_route=recommended,
    )
    return _emit(report, arguments.report)


def _run_fresh(arguments) -> int:
    provenance = provenance_document()
    retained_outcome = None
    retained_identity = None
    retained_evidence = None
    if arguments.retained_report is not None:
        # stale / malformed / unrelated / tampered な report が fresh fallback を
        # authorize しないよう、strict reader を通したうえで retained route が
        # 明示的に NOT QUALIFIED であることだけを受け入れる。
        retained_report = load_feasibility_report(arguments.retained_report)
        retained_evidence = require_retained_not_qualified(retained_report)
        retained_outcome = retained_report.document["routes"]["retained_augmentation"]
        retained_identity = retained_report.retained_corpus_identity

    qualification = qualify_fresh_live_label(seeds=SMOKE_SEEDS)
    cells = qualification.cells
    sidecar = None
    recomputation = None
    if qualification.is_qualified:
        sidecar = write_sidecar(
            arguments.sidecar,
            cells,
            route=ROUTE_FRESH,
            source_identity=SMOKE_POPULATION_IDENTITY,
            provenance=provenance,
        )
        recomputation = _recomputation_check(sidecar)

    checks = build_checks(cells, deterministic_recomputation=recomputation)
    failed = _failed_checks(checks)
    if failed:
        # co-emission は成立したが、その evidence / invariant が壊れた状態。
        # expected な technical path failure ではないので BLOCKED にしない。
        hard_outcome = STOP_INVALID
        pending = None
        recommended = None
    elif qualification.is_qualified and retained_evidence is not None:
        hard_outcome = FRESH_LIVE_LABEL_PATH_QUALIFIED
        pending = None
        recommended = "fresh-live-label"
    elif qualification.is_qualified:
        hard_outcome = None
        pending = (
            "the fresh live-label co-emission path is demonstrated, but the "
            "retained augmentation route has not been measured against an "
            "operator-local retained corpus; #258 cannot select a final route yet"
        )
        recommended = None
    elif retained_evidence is not None:
        hard_outcome = TENPAI_LABEL_PATH_BLOCKED
        pending = None
        recommended = None
    else:
        hard_outcome = None
        pending = (
            "the fresh live-label smoke did not qualify and the retained route "
            "has not been measured; re-run both routes before concluding"
        )
        recommended = None

    report = FeasibilityReport(
        provenance=provenance,
        instrumentation_provenance=None,
        source_paths_examined=("lisjong_arena.riichienv.local_game_runner",),
        retained_corpus_identity=retained_identity,
        retained_outcome=retained_outcome,
        retained_evidence=retained_evidence,
        smoke_population_identity=SMOKE_POPULATION_IDENTITY,
        fresh_outcome=qualification.to_document(),
        sidecar_identity=None if sidecar is None else sidecar.identity,
        row_count=qualification.row_count,
        cell_count=qualification.cell_count,
        availability_counts=availability_counts(cells),
        checks=checks,
        hard_outcome=hard_outcome,
        pending_reason=pending,
        recommended_route=recommended,
    )
    return _emit(report, arguments.report)


def _run_recompute(arguments) -> int:
    sidecar = load_sidecar(arguments.sidecar)
    count = verify_deterministic_recomputation(sidecar)
    json.dump(
        {
            "sidecar_identity": sidecar.identity,
            "route": sidecar.route,
            "recomputed_cell_count": count,
        },
        sys.stdout,
        sort_keys=True,
    )
    sys.stdout.write("\n")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m lisjong_arena.stage_a0_tenpai_feasibility",
        description="Stage A0 non-riichi Tenpai label path feasibility (#258)",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    retained = commands.add_parser(
        "retained-qualify",
        help="qualify exact augmentation of the retained flat-BC corpus",
    )
    retained.add_argument("--dataset", type=Path, required=True)
    retained.add_argument("--sidecar", type=Path, required=True)
    retained.add_argument("--report", type=Path, required=True)
    retained.set_defaults(handler=_run_retained)

    fresh = commands.add_parser(
        "fresh-smoke",
        help="qualify the fresh live-label path on the locked technical smoke",
    )
    fresh.add_argument("--retained-report", type=Path, default=None)
    fresh.add_argument("--sidecar", type=Path, required=True)
    fresh.add_argument("--report", type=Path, required=True)
    fresh.set_defaults(handler=_run_fresh)

    recompute = commands.add_parser(
        "recompute",
        help="recompute Stage A0 targets from a sidecar and verify determinism",
    )
    recompute.add_argument("--sidecar", type=Path, required=True)
    recompute.set_defaults(handler=_run_recompute)
    return parser


def main(argv=None) -> int:
    arguments = build_parser().parse_args(argv)
    return arguments.handler(arguments)


if __name__ == "__main__":
    raise SystemExit(main())
