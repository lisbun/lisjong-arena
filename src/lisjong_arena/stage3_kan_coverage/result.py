"""coverage-source qualificationのexhaustive classificationとstrict result artifact。

classification ruleは実行前にlockされたdeterministic functionである。結果を見て
から判定条件を変えない。

```text
KAN COVERAGE SOURCE QUALIFIED FOR MIX DESIGN
KAN COVERAGE SOURCE EMPIRICALLY INSUFFICIENT
KAN COVERAGE ACCOUNTING REFORMULATE
SEED PLAN REFORMULATE
STOP / INVALID
```

`SEED PLAN REFORMULATE`はresult exposure前のpreflightでしか選べない
outcomeであり、measurementからは導出しない。したがってこのmoduleは
残り4つのうち1つを返す。

## zero-count kindの解釈

```text
eligible no-win opportunity = 0 かつ selected = 0
    -> UNMEASURED / ABSENT IN PILOT

eligible no-win opportunity > 0 かつ selected = 0
    -> source contract violation
```

この2つを混同しない。3 kindすべてが観測されることはqualificationの必須条件
ではない。
"""

import json
from pathlib import Path

from lisjong_arena.phase4_raw_corpus.codec import canonical_json_bytes
from lisjong_arena.stage3_kan_coverage.protocol import (
    ACCOUNTING_REFORMULATE,
    CLASSIFICATION_RULE,
    INSUFFICIENT,
    KAN_KINDS,
    OUTCOMES,
    PILOT_HANCHAN,
    PILOT_ROLE,
    QUALIFIED,
    RESULT_SCHEMA_VERSION,
    RETRY_RULE,
    STOP_INVALID,
)

RESULT_FILENAME = "result.json"

UNMEASURED = "UNMEASURED / ABSENT IN PILOT"
OBSERVED = "OBSERVED"
CONTRACT_VIOLATION = "SOURCE CONTRACT VIOLATION"


class KanCoverageResultError(ValueError):
    """result artifactのcontract violation。"""


def kind_interpretation(diagnostic: dict, kind: str) -> str:
    """kan kindごとのzero-count解釈をexplicitに分類する。"""
    counts = diagnostic["by_kind"][kind]
    eligible = counts["eligible_no_win_opportunities"]
    selected = counts["selected"]
    if eligible == 0:
        return UNMEASURED
    if selected == 0:
        return CONTRACT_VIOLATION
    return OBSERVED


def classify(manifest: dict) -> tuple[str, tuple[str, ...]]:
    """locked classification ruleでoutcomeとその根拠を返す。"""
    diagnostic = manifest["kan_opportunity_diagnostic"]
    accounting = manifest["kan_accounting"]["totals"]
    coverage = manifest["coverage"]["events"]
    retention = manifest["dataset_retention"]
    provenance = manifest["provenance"]
    reasons: list[str] = []

    if provenance["fully_resolved"] is not True:
        reasons.append("source revisions are not fully resolved")
    if coverage["hanchan"] != PILOT_HANCHAN:
        reasons.append("the pilot did not record exactly the locked hanchan count")
    if retention["kan_containing_games_dropped"] != 0:
        reasons.append("dataset materialization dropped a kan-containing game")
    if reasons:
        return STOP_INVALID, tuple(reasons)

    if diagnostic["selection_contract_violations"] != 0:
        reasons.append(
            "an eligible no-win kan opportunity did not produce a kan selection"
        )
    if accounting["unaccounted"] != 0:
        reasons.append("a selected kan could not be bound to public evidence")
    if accounting["rinshan_missing"] != 0:
        reasons.append(
            "a confirmed kan with an expected continuation has no rinshan draw"
        )
    if reasons:
        return ACCOUNTING_REFORMULATE, tuple(reasons)

    eligible = sum(
        diagnostic["by_kind"][kind]["eligible_no_win_opportunities"]
        for kind in KAN_KINDS
    )
    if (
        eligible == 0
        or accounting["selected"] == 0
        or accounting["confirmed"] == 0
        or coverage["rinshan_draw"] == 0
    ):
        return INSUFFICIENT, (
            "the bounded development population produced no usable "
            "eligible no-win kan opportunity, confirmed kan or rinshan draw",
        )
    return QUALIFIED, (
        "every hard validity gate passed and the coverage source produced "
        "eligible no-win kan opportunities, deterministic kan selection, "
        "confirmed kan, rinshan continuation and retained dataset membership",
    )


def result_value(manifest: dict) -> dict[str, object]:
    """classificationとhandoff用measurementをstrict result valueへまとめる。"""
    outcome, reasons = classify(manifest)
    diagnostic = manifest["kan_opportunity_diagnostic"]
    return {
        "result_schema_version": RESULT_SCHEMA_VERSION,
        "pilot_role": PILOT_ROLE,
        "retry_rule": RETRY_RULE,
        "classification_rule": CLASSIFICATION_RULE,
        "outcome": outcome,
        "outcome_reasons": list(reasons),
        "population_identity": manifest["population_identity"],
        "population_plan": manifest["population_plan"],
        "raw_corpus_identity": manifest["raw_corpus_identity"],
        "dataset_identity": manifest["dataset_identity"],
        "provenance": manifest["provenance"],
        "generation_runtime": manifest["generation_runtime"],
        "coverage": manifest["coverage"],
        "kan_opportunity_summary": {
            key: value
            for key, value in diagnostic.items()
            if key != "kan_opportunity_records"
        },
        "kan_accounting_summary": {
            key: value
            for key, value in manifest["kan_accounting"].items()
            if key != "selected_kan_accounts"
        },
        "kind_interpretation": {
            kind: kind_interpretation(diagnostic, kind) for kind in KAN_KINDS
        },
        "dataset_retention": {
            key: value
            for key, value in manifest["dataset_retention"].items()
            if key != "kan_event_rows"
        },
        "observed_rates": manifest["observed_rates"],
        "conditional_uniform_baseline": manifest["conditional_uniform_baseline"],
        "cost": manifest["cost"],
        "next_step_boundary": (
            "a positive outcome only enables the next bounded population-mix "
            "design Issue; it is not a final training population lock, a strength "
            "claim, or Phase 10 activation"
        ),
    }


def validate_result_value(value: object) -> dict[str, object]:
    """result artifactをfail closedで検証する。unknown / malformedは拒否する。"""
    if type(value) is not dict:
        raise KanCoverageResultError("result must be an object")
    if value.get("result_schema_version") != RESULT_SCHEMA_VERSION:
        raise KanCoverageResultError("result schema version differs")
    if value.get("pilot_role") != PILOT_ROLE:
        raise KanCoverageResultError("result pilot role differs")
    outcome = value.get("outcome")
    if outcome not in OUTCOMES:
        raise KanCoverageResultError("result outcome is not an exhaustive outcome")
    reasons = value.get("outcome_reasons")
    if type(reasons) is not list or not reasons:
        raise KanCoverageResultError("result outcome requires recorded reasons")
    interpretation = value.get("kind_interpretation")
    if type(interpretation) is not dict or tuple(sorted(interpretation)) != tuple(
        sorted(KAN_KINDS)
    ):
        raise KanCoverageResultError("result must interpret every kan kind")
    if any(
        row not in (UNMEASURED, OBSERVED, CONTRACT_VIOLATION)
        for row in interpretation.values()
    ):
        raise KanCoverageResultError("unknown kan kind interpretation")
    if outcome == QUALIFIED and CONTRACT_VIOLATION in interpretation.values():
        raise KanCoverageResultError(
            "a qualified result cannot carry a source contract violation"
        )
    for name in (
        "population_identity",
        "raw_corpus_identity",
        "dataset_identity",
    ):
        digest = value.get(name)
        if (
            type(digest) is not str
            or len(digest) != 64
            or any(character not in "0123456789abcdef" for character in digest)
        ):
            raise KanCoverageResultError(f"{name} must be a lowercase SHA-256")
    for name in (
        "provenance",
        "coverage",
        "kan_opportunity_summary",
        "kan_accounting_summary",
        "dataset_retention",
        "observed_rates",
        "cost",
    ):
        if type(value.get(name)) is not dict:
            raise KanCoverageResultError(f"result lacks {name}")
    return value


def save_result(destination: str | Path, value: dict[str, object]) -> Path:
    """result artifactをcanonical JSONとして一度だけ書き出す。"""
    destination = Path(destination)
    path = destination / RESULT_FILENAME
    if path.exists():
        raise FileExistsError(f"result already exists: {path}")
    path.write_bytes(canonical_json_bytes(validate_result_value(value)))
    return path


def load_result(destination: str | Path) -> dict[str, object]:
    """result artifactをstrictに読み戻す。"""
    destination = Path(destination)
    data = (destination / RESULT_FILENAME).read_bytes()
    value = json.loads(data)
    if canonical_json_bytes(value) != data:
        raise KanCoverageResultError("result bytes are not canonical JSON")
    return validate_result_value(value)


__all__ = [
    "CONTRACT_VIOLATION",
    "OBSERVED",
    "RESULT_FILENAME",
    "UNMEASURED",
    "KanCoverageResultError",
    "classify",
    "kind_interpretation",
    "load_result",
    "result_value",
    "save_result",
    "validate_result_value",
]
