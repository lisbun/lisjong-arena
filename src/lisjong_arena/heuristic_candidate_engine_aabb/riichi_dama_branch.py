"""#540 riichi-dama-branch-v1（#539）の実行・照合・summary・判定・strict verify。

```text
run       live gate -> R（seed順、Champion×4、配置の入替なし）
          -> D arm（Rの分岐点ごとに先頭から再実行。rehearsalの観測ありplanは全D armを
             別processでもう1回）-> live gate
          -> <output>/{hanchans.json, branches.jsonl, features.jsonl, summary.json,
                       timings.json, SHA256SUMS}（観測なしrehearsalはhanchansだけ）
verify    別processで保存物だけを読み、hash・file集合・schedule・形を検査し、保存した
          contextと宣言牌からlisjongの特徴を再導出し、保存した精算からΔ・部分集合・
          bootstrap・判定を再導出する。allocatedは固定2 seed（539000・539023）の
          R区間と全D armを再実行し、記録との完全一致を要求する
compare   rehearsalの観測あり / なしのRでAction列digest・客観outcomeの一致と、
          D armの2回実行の一致を要求する
estimate  照合済みrehearsalの実測から§7の時間見積もり P を計算する（P > 3時間なら
          NOT-STARTED-BUDGET。allocated planはWITHIN-BUDGETの見積もりを必須にする）
```

- ``hanchans.json``: Rの客観値（#513の局記録 ``kyoku_record``、終局・最終点・
  ``CompletedMatch`` digest・呼び出し数・席ごとのAction列digest・リーチが合法なのに
  選ばなかった判断の席ごとの件数）
- ``branches.jsonl``: 1分岐1行の客観記録（versioned source record）。public context
  （PolicyInput v3 lossless・合法手の元の順序）、Rの行動（リーチと宣言牌）、Dの行動
  （同じ宣言牌をリーチせずに打つ）、分岐直前のdigest、armごとの分岐後のdigest・精算・
  局の結果、Dの差し替え回数。encode済みの特徴やtraining用のlabelは持たない
- ``features.jsonl``: lisjongの解析（1分岐1行、branch idで結ぶ）。客観記録とは別file
- 判定は実行前に固定した順に1つだけ出す（allocatedのみ）:
  ``STOP-INVALID`` > ``STOP-INCOMPLETE``（実行が完了しないとsummaryを作らない）>
  ``STOP-LOW-ACTIVATION`` > ``STOP-NO-SIGNAL`` > ``PROCEED-TO-CANDIDATE-DESIGN``
- Δ・平均・分位は ``fractions.Fraction`` で厳密に比べ、floatで判定しない
- Arenaは点数・待ち・役を再計算しない。部分集合はlisjongの値の比較だけで決める
"""

import json
import math
import random
from collections.abc import Callable
from fractions import Fraction
from pathlib import Path

from lisjong_arena import seed_registry
from lisjong_arena._artifact_io import (
    ArtifactValidationError,
    canonical_json_text,
    parse_json_text,
    sha256_bytes,
    staged_artifact_directory,
    write_new_artifact_file,
)
from lisjong_arena.overall_champion_aabb.lock import document_identity

from .call_value_shadow_worker import decode_action
from .errors import KyokuRecordError
from .executor import (
    ExecutedHanchan,
    HanchanTask,
    RunStopped,
    current_peak_rss_kib,
    run_tasks,
)
from .kyoku_record import (
    PROTOCOL_ID,
    ROLE_INCUMBENT,
    SEAT_COUNT,
    RecordLayout,
    project_row,
    validate_hanchan_row,
)
from .lock import EngineAabbLockError, _live_environment
from .paijia_tie_activation import _hanchan_document
from .paijia_tie_plan import (
    ALLOCATED,
    REHEARSAL,
    SUMMARY_FILENAME,
    TIMINGS_FILENAME,
    clock_document,
    default_clocks,
    distribution,
    document_sha256,
    memory_summary,
    output_files,
    read_document,
    utc_now,
    write_stop,
)
from .protocol import EngineAabbProtocolError
from .riichi_dama_branch_plan import (
    CRITERIA,
    ESTIMATE_SCHEMA,
    EVALUATION_KIND,
    OBSERVED,
    PROCEED,
    STOP_INVALID,
    STOP_LOW_ACTIVATION,
    STOP_NO_SIGNAL,
    STOP_SCHEMA,
    SUBSETS,
    UNOBSERVED,
    RiichiDamaBranchError,
    estimate_values,
    load_plan,
    parse_time_estimate,
    plan_champion,
    plan_schedule,
    require_live_target,
)
from .riichi_dama_branch_worker import (
    FEATURES_VERSION,
    PROTOCOL_VERSION,
    BranchTask,
    analysis_api,
    branch_features,
    play_d_arm,
    play_r_observed,
    play_r_unobserved,
)

HANCHANS_FILENAME = "hanchans.json"
BRANCHES_FILENAME = "branches.jsonl"
FEATURES_FILENAME = "features.jsonl"
SUMS_FILENAME = "SHA256SUMS"
HANCHANS_SCHEMA = "arena-riichi-dama-branch-v1-hanchans-v1"
BRANCHES_SCHEMA = "arena-riichi-dama-branch-v1-branches-v1"
FEATURES_SCHEMA = "arena-riichi-dama-branch-v1-features-v1"
SUMMARY_SCHEMA = "arena-riichi-dama-branch-v1-summary-v1"
TIMINGS_SCHEMA = "arena-riichi-dama-branch-v1-timings-v1"
VERIFY_SCHEMA = "arena-riichi-dama-branch-v1-verify-v1"
COMPARE_SCHEMA = "arena-riichi-dama-branch-v1-rehearsal-compare-v1"
KYOKU_RECORD_SCHEMA = "arena-riichi-dama-branch-v1-kyoku-record-v1"

COMPLETE_STATUS = (
    "RIICHI-DAMA BRANCH COMPLETE; DEVELOPMENT DIAGNOSTIC, NOT A STRENGTH RESULT"
)
REHEARSAL_STATUS = "REHEARSAL COMPLETE; NO GATE DECISION"

ARM_R = "R"
ARM_D = "D"
ARM_D_REPEAT = "D-repeat"
ARMS = (ARM_R, ARM_D, ARM_D_REPEAT)
BOOTSTRAP_SEED = 539
BOOTSTRAP_REPLICATES = 10_000
QUANTILES = {"P10": Fraction(1, 10), "P50": Fraction(1, 2), "P90": Fraction(9, 10)}
_MANGAN_OR_ABOVE = ("MANGAN", "HANEMAN", "BAIMAN", "SANBAIMAN", "YAKUMAN")
_SEAT_OUTCOMES = ("win", "deal_in", "draw", "other")


def _fail(message: str):
    raise RiichiDamaBranchError(f"riichi-dama-branch-v1: {message}")


def _ratio(value: Fraction) -> dict[str, int]:
    return {"denominator": value.denominator, "numerator": value.numerator}


def _nonnegative_int(value: object) -> bool:
    return type(value) is int and value >= 0


# ---------------------------------------------------------------------------
# Rの局記録（#513の第1層と同じ検査。半荘は (seed, placement 0)、4席Champion）
# ---------------------------------------------------------------------------


def _validate_keys(row: dict) -> None:
    if row["placement"] != 0:
        raise KyokuRecordError("riichi-dama-branch-v1 uses placement 0 only")


def _validate_seats(row: dict) -> None:
    seats = row["seats"]
    if type(seats) is not list or len(seats) != SEAT_COUNT:
        raise KyokuRecordError("seats must have 4 entries")
    identities = set()
    for index, item in enumerate(seats):
        if type(item) is not dict or set(item) != {"policy_identity", "role"}:
            raise KyokuRecordError(f"seats[{index}] fields do not match the schema")
        if item["role"] != ROLE_INCUMBENT:
            raise KyokuRecordError("every seat of R is the Champion (role B)")
        identity = item["policy_identity"]
        if type(identity) is not str or not identity:
            raise KyokuRecordError(f"seats[{index}].policy_identity is invalid")
        identities.add(identity)
    if len(identities) != 1:
        raise KyokuRecordError("all four seats of R are the same Champion")


LAYOUT = RecordLayout(
    protocol_id=PROTOCOL_ID,
    kyoku_record_schema=KYOKU_RECORD_SCHEMA,
    event_fact_schema="arena-riichi-dama-branch-v1-event-fact-v1",
    manifest_schema="arena-riichi-dama-branch-v1-record-manifest-v1",
    key_fields=("placement",),
    validate_keys=_validate_keys,
    validate_seats=_validate_seats,
)

_HANCHAN_FIELDS = frozenset(
    {
        "action_sequence_sha256",
        "completed_match_sha256",
        "decisions",
        "end_reason",
        "final_points",
        "kyoku_count",
        "kyoku_record",
        "placement",
        "policy_call_counts",
        "riichi_declined",
        "seed",
        "selector_call_counts",
    }
)


def hanchan_row(item: ExecutedHanchan, *, observed: bool) -> dict:
    capture = item.capture
    analysis = item.policy_analysis
    record = project_row(
        capture.match,
        seed=capture.seed,
        keys={"placement": capture.placement},
        seats=capture.seats,
        selector_call_counts=capture.selector_call_counts,
        policy_call_counts=capture.policy_call_counts,
        layout=LAYOUT,
    )
    return {
        **_hanchan_document(item),
        "action_sequence_sha256": analysis["action_sequence_sha256"],
        "decisions": analysis["decisions"],
        "kyoku_record": record,
        "riichi_declined": analysis["riichi_declined"] if observed else None,
    }


def hanchans_document(
    plan: dict, executed: list[ExecutedHanchan], *, observed: bool
) -> dict:
    return {
        "hanchans": [hanchan_row(item, observed=observed) for item in executed],
        "observation": OBSERVED if observed else UNOBSERVED,
        "plan_digest": plan["plan_digest"],
        "schema": HANCHANS_SCHEMA,
    }


def require_hanchans(plan: dict, hanchans: object) -> None:
    """Rの客観recordが計画したscheduleと完全一致すること（不足・重複・順序の入替を拒否）。"""
    expected = [(item["seed"], item["placement"]) for item in plan_schedule(plan)]
    if (
        type(hanchans) is not dict
        or set(hanchans) != {"hanchans", "observation", "plan_digest", "schema"}
        or hanchans["schema"] != HANCHANS_SCHEMA
        or hanchans["plan_digest"] != plan["plan_digest"]
        or hanchans["observation"] != plan["observation"]
        or type(hanchans["hanchans"]) is not list
        or any(type(row) is not dict for row in hanchans["hanchans"])
        or [(row.get("seed"), row.get("placement")) for row in hanchans["hanchans"]]
        != expected
    ):
        _fail("hanchans do not cover the planned schedule")
    observed = plan["observation"] == OBSERVED
    for row in hanchans["hanchans"]:
        context = f"seed {row['seed']}"
        if set(row) != _HANCHAN_FIELDS:
            _fail(f"{context}: hanchan fields do not match")
        counts = row["policy_call_counts"]
        if (
            type(counts) is not list
            or len(counts) != 4
            or not all(_nonnegative_int(count) for count in counts)
            or row["selector_call_counts"] != counts
            or row["decisions"] != sum(counts)
        ):
            _fail(f"{context}: call counts are invalid")
        digests = row["action_sequence_sha256"]
        if (
            type(digests) is not list
            or len(digests) != 4
            or any(type(item) is not str or len(item) != 64 for item in digests)
        ):
            _fail(f"{context}: action sequence digests are invalid")
        declined = row["riichi_declined"]
        if observed:
            if (
                type(declined) is not list
                or len(declined) != 4
                or not all(_nonnegative_int(item) for item in declined)
            ):
                _fail(f"{context}: riichi_declined is invalid")
        elif declined is not None:
            _fail(f"{context}: an unobserved R has no riichi counts")
        try:
            record = validate_hanchan_row(row["kyoku_record"], LAYOUT)
        except KyokuRecordError as error:
            raise RiichiDamaBranchError(f"{context}: kyoku record: {error}") from error
        if (
            record["seed"] != row["seed"]
            or record["placement"] != row["placement"]
            or record["policy_call_counts"] != counts
            or len(record["kyokus"]) != row["kyoku_count"]
        ):
            _fail(f"{context}: kyoku record differs from the hanchan row")


# ---------------------------------------------------------------------------
# JSON Lines
# ---------------------------------------------------------------------------


def canonical_line(value: object) -> str:
    """JSON Linesの1行（key順・区切りを固定、非有限数を拒否）。"""
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    )


def jsonl_text(lines: list[dict]) -> str:
    return "".join(canonical_line(line) for line in lines)


def read_jsonl(path: Path, context: str) -> list[dict]:
    """canonical JSONの行だけを受け付ける（空行・非canonical・改行なしを拒否）。"""
    try:
        text = path.read_bytes().decode("utf-8")
    except (OSError, UnicodeDecodeError) as error:
        raise RiichiDamaBranchError(f"cannot read {context}: {error}") from error
    if not text.endswith("\n"):
        _fail(f"{context} must end with a newline")
    lines = []
    for raw in text[:-1].split("\n"):
        try:
            value = parse_json_text(raw)
        except (ArtifactValidationError, ValueError) as error:
            raise RiichiDamaBranchError(
                f"{context} line is invalid: {error}"
            ) from error
        if canonical_line(value) != raw + "\n":
            _fail(f"{context} line is not canonical JSON")
        lines.append(value)
    return lines


def _header(plan: dict, schema: str) -> dict:
    return {
        "features_version": FEATURES_VERSION,
        "kind": "header",
        "plan_digest": plan["plan_digest"],
        "protocol_version": PROTOCOL_VERSION,
        "schema": schema,
    }


# ---------------------------------------------------------------------------
# 分岐の記録（Rの分岐点 → D armのtask → 1分岐1行）
# ---------------------------------------------------------------------------


def branch_id(seed: int, round_ordinal: int, seat: int) -> str:
    return f"{seed}-{round_ordinal}-{seat}"


def r_branches(executed_r: list[ExecutedHanchan]) -> list[tuple[int, dict]]:
    """Rの分岐記録を (seed, record) でseed順・decision index順に並べる。"""
    ordered = []
    for item in executed_r:
        records = sorted(
            item.policy_analysis["branches"],
            key=lambda record: record["decision_index"],
        )
        ordered.extend((item.capture.seed, record) for record in records)
    return ordered


def task_branch(record: dict) -> dict:
    """D armの実行に渡す値（Rの結果は含めない）。"""
    return {
        "declaration": record["declaration"],
        "round": record["round"],
        "round_ordinal": record["round_ordinal"],
        "seat": record["seat"],
    }


def branch_tasks(plan: dict, branches: list[tuple[int, dict]]) -> list[BranchTask]:
    """宣言牌が記録された分岐だけD armを実行する（欠落はINVALIDとして残る）。"""
    champion = plan_champion(plan).to_document()
    return [
        BranchTask(
            seed=seed,
            branch_ordinal=ordinal,
            branch=task_branch(record),
            incumbent=champion,
            wheel_path=plan["execution"]["wheel_path"],
        )
        for ordinal, (seed, record) in enumerate(branches)
        if record["declaration"] is not None
    ]


def r_settlement(kyoku_record: dict, round_ordinal: int) -> dict:
    """Rの対象局の精算（局記録の値をそのまま取る）。"""
    kyokus = kyoku_record["kyokus"]
    if not 0 <= round_ordinal < len(kyokus):
        _fail("a branch round is outside the hanchan")
    kyoku = kyokus[round_ordinal]
    result = kyoku["result"]
    win = result.get("win") if result["kind"] == "win" else None
    final = None
    if round_ordinal == len(kyokus) - 1:
        final = [0, 0, 0, 0]
        for item in kyoku_record["final_riichi_stick_awards"]:
            final[item["seat"]] += item["amount"]
    return {
        "final_riichi_stick_awards": final,
        "outcome": result["kind"],
        "point_deltas": kyoku["point_deltas"],
        "points_after": kyoku["points_after"],
        "points_before": kyoku["points_before"],
        "round": {
            "dealer_seat": kyoku["dealer_seat"],
            "hand_number": kyoku["hand_number"],
            "honba": kyoku["honba"],
            "prevailing_wind": kyoku["prevailing_wind"],
        },
        "source_seat": None if win is None else win["source_seat"],
        "winner_seats": [] if win is None else win["winner_seats"],
    }


def d_settlement(record: dict) -> dict | None:
    """D armのcompletion factを、Rの精算と同じ形へ並べる。"""
    settlement = record["settlement"]
    if settlement is None:
        return None
    after, deltas = settlement["points_after"], settlement["point_deltas"]
    final = None
    if not settlement["has_next_round"]:
        raw = settlement["final_raw_scores"]
        if raw is None:
            _fail("the final round of a D arm has no match completion")
        final = [raw[seat] - after[seat] for seat in range(4)]
    return {
        "final_riichi_stick_awards": final,
        "outcome": settlement["outcome"],
        "point_deltas": deltas,
        "points_after": after,
        "points_before": [after[seat] - deltas[seat] for seat in range(4)],
        "round": record["round"],
        "source_seat": settlement["source_seat"],
        "winner_seats": settlement["winner_seats"],
    }


def branch_lines(
    plan: dict,
    hanchans: dict,
    branches: list[tuple[int, dict]],
    d_records: dict[int, dict],
    repeat_records: dict[int, dict] | None,
) -> list[dict]:
    rows = {row["seed"]: row for row in hanchans["hanchans"]}
    lines = [_header(plan, BRANCHES_SCHEMA)]
    for ordinal, (seed, record) in enumerate(branches):
        lines.append(
            {
                "branch_id": branch_id(seed, record["round_ordinal"], record["seat"]),
                "branch_ordinal": ordinal,
                "context": record["context"],
                "context_sha256": record["context_sha256"],
                "d": d_records.get(ordinal),
                "d_repeat": None
                if repeat_records is None
                else repeat_records.get(ordinal),
                "decision_index": record["decision_index"],
                "declaration": record["declaration"],
                "declaration_decision_index": record["declaration_decision_index"],
                "kind": "branch",
                "prefix_sha256": record["prefix_sha256"],
                "r_invalid": record["invalid"],
                "r_post_sha256": record["post_sha256"],
                "r_settlement": r_settlement(
                    rows[seed]["kyoku_record"], record["round_ordinal"]
                ),
                "riichi": record["riichi"],
                "round": record["round"],
                "round_ordinal": record["round_ordinal"],
                "seat": record["seat"],
                "seed": seed,
            }
        )
    return lines


def feature_lines(plan: dict, branches: list[tuple[int, dict]]) -> list[dict]:
    lines = [_header(plan, FEATURES_SCHEMA)]
    for ordinal, (seed, record) in enumerate(branches):
        lines.append(
            {
                "branch_id": branch_id(seed, record["round_ordinal"], record["seat"]),
                "branch_ordinal": ordinal,
                "error": record["features"]["error"],
                "features": record["features"]["features"],
                "kind": "features",
            }
        )
    return lines


_BRANCH_FIELDS = frozenset(
    {
        "branch_id",
        "branch_ordinal",
        "context",
        "context_sha256",
        "d",
        "d_repeat",
        "decision_index",
        "declaration",
        "declaration_decision_index",
        "kind",
        "prefix_sha256",
        "r_invalid",
        "r_post_sha256",
        "r_settlement",
        "riichi",
        "round",
        "round_ordinal",
        "seat",
        "seed",
    }
)
_D_FIELDS = frozenset(
    {
        "branch_context_sha256",
        "branch_decision_index",
        "branch_ordinal",
        "decisions",
        "invalid",
        "post_sha256",
        "prefix_sha256",
        "replacements",
        "riichi_after_branch",
        "round",
        "round_ordinal",
        "seat",
        "seed",
        "settlement",
        "status",
    }
)
_FEATURE_LINE_FIELDS = frozenset(
    {"branch_id", "branch_ordinal", "error", "features", "kind"}
)


def require_branch_lines(plan: dict, lines: object, hanchans: dict) -> None:
    """分岐行の形・順序・一意性・Rとの一対一を検査する（INVALIDの判定は別）。"""
    require_hanchans(plan, hanchans)
    if type(lines) is not list or not lines:
        _fail("branches are empty")
    if lines[0] != _header(plan, BRANCHES_SCHEMA):
        _fail("branches header does not belong to the plan")
    rows = {row["seed"]: row for row in hanchans["hanchans"]}
    seeds = [item["seed"] for item in plan_schedule(plan)]
    keys = []
    for ordinal, line in enumerate(lines[1:]):
        context = f"branch {ordinal}"
        if type(line) is not dict or set(line) != _BRANCH_FIELDS:
            _fail(f"{context}: branch fields do not match")
        if line["kind"] != "branch" or line["branch_ordinal"] != ordinal:
            _fail(f"{context}: branch ordinal is not sequential")
        if line["seed"] not in rows:
            _fail(f"{context}: the seed is not in the schedule")
        if type(line["seat"]) is not int or not 0 <= line["seat"] < 4:
            _fail(f"{context}: seat is invalid")
        row = rows[line["seed"]]
        if (
            not _nonnegative_int(line["decision_index"])
            or line["decision_index"] >= row["decisions"]
            or not _nonnegative_int(line["round_ordinal"])
            or line["round_ordinal"] >= row["kyoku_count"]
        ):
            _fail(f"{context}: decision index or round is outside the hanchan")
        if line["branch_id"] != branch_id(
            line["seed"], line["round_ordinal"], line["seat"]
        ):
            _fail(f"{context}: branch id does not match")
        if type(line["r_invalid"]) is not list or any(
            type(item) is not str for item in line["r_invalid"]
        ):
            _fail(f"{context}: r_invalid must be a list of strings")
        for name in ("d", "d_repeat"):
            record = line[name]
            if record is None:
                continue
            if type(record) is not dict or set(record) != _D_FIELDS:
                _fail(f"{context}: {name} fields do not match")
            if (
                record["branch_ordinal"] != ordinal
                or record["seed"] != line["seed"]
                or record["seat"] != line["seat"]
            ):
                _fail(f"{context}: {name} belongs to another branch")
        if line["d"] is None and line["declaration"] is not None:
            _fail(f"{context}: a branch with its declaration has no D arm")
        if line["d"] is not None and line["declaration"] is None:
            _fail(f"{context}: a D arm without the declaration")
        repeat_expected = plan["run_kind"] == REHEARSAL
        if (line["d_repeat"] is not None) is not (
            repeat_expected and line["d"] is not None
        ):
            _fail(f"{context}: the D arm repeat does not match the run kind")
        keys.append((seeds.index(line["seed"]), line["decision_index"]))
    if keys != sorted(keys) or len(keys) != len(set(keys)):
        _fail("branches are not in schedule and decision order or are duplicated")
    per_round = [
        (line["seed"], line["round_ordinal"], line["seat"]) for line in lines[1:]
    ]
    if len(per_round) != len(set(per_round)):
        _fail("a seat has two branch points in one round")


def require_feature_lines(plan: dict, lines: object, branches: list[dict]) -> None:
    if type(lines) is not list or not lines:
        _fail("features are empty")
    if lines[0] != _header(plan, FEATURES_SCHEMA):
        _fail("features header does not belong to the plan")
    if len(lines) != len(branches):
        _fail("features and branches differ in count (one-to-one)")
    for line, branch in zip(lines[1:], branches[1:], strict=True):
        if type(line) is not dict or set(line) != _FEATURE_LINE_FIELDS:
            _fail("feature fields do not match")
        if (
            line["kind"] != "features"
            or line["branch_ordinal"] != branch["branch_ordinal"]
            or line["branch_id"] != branch["branch_id"]
        ):
            _fail("features and branches are not one-to-one")
        if (line["error"] is None) is (line["features"] is None):
            _fail("a feature line holds exactly one of error and features")


# ---------------------------------------------------------------------------
# 1分岐の照合・Δ・部分集合（保存物だけから決まる）
# ---------------------------------------------------------------------------


def _seat_outcome(settlement: dict, seat: int) -> str:
    if settlement["outcome"] != "win":
        return "draw"
    if seat in settlement["winner_seats"]:
        return "win"
    if settlement["source_seat"] == seat:
        return "deal_in"
    return "other"


def subsets_of(features) -> dict[str, bool]:
    """S1 / S3（#539 §5）をlisjongの値の比較だけで判定する。"""
    live = [wait for wait in features.waits if wait.unseen_count >= 1]
    s1 = (
        not features.furiten
        and bool(live)
        and all(
            wait.dama_ron.is_scored and wait.dama_ron.limit.name in _MANGAN_OR_ABOVE
            for wait in live
        )
    )
    s3 = features.opponent_riichi_count >= 1 and features.total_unseen_count <= 4
    return {"S1": s1, "S3": s3}


def evaluate_branch(line: dict, feature: dict, row: dict) -> dict:
    """1分岐のINVALID理由・Δ・部分集合・記述値を返す。"""
    seat = line["seat"]
    invalid = [f"R: {reason}" for reason in line["r_invalid"]]
    result = {
        "branch_id": line["branch_id"],
        "dama_choice_matches_declaration": None,
        "delta": None,
        "final_riichi_stick_awards": None,
        "invalid": invalid,
        "outcomes": None,
        "replacements": None,
        "seed": line["seed"],
        "subsets": None,
        "yaku": None,
    }
    expected_r = r_settlement(row["kyoku_record"], line["round_ordinal"])
    if line["r_settlement"] != expected_r:
        invalid.append("the R settlement differs from the kyoku record")
    if expected_r["round"] != line["round"]:
        invalid.append("the branch context belongs to another round than the record")
    if line["declaration"] is None or line["context"] is None:
        invalid.append("the declaration or the context is missing")
    if feature["error"] is not None:
        invalid.append(f"lisjong rejects the features: {feature['error']}")
    else:
        try:
            api = analysis_api()
            features = api.parse_riichi_decision_features(feature["features"])
            declaration = decode_action(line["declaration"])
            if int(features.seat) != seat or (
                features.declaration_tile != declaration.tile
            ):
                invalid.append("the features belong to another seat or tile")
            else:
                result["subsets"] = subsets_of(features)
                result["yaku"] = any(wait.dama_ron.is_scored for wait in features.waits)
                result["dama_choice_matches_declaration"] = (
                    features.dama_choice == declaration
                )
        except Exception as error:
            invalid.append(f"the features cannot be read: {type(error).__name__}")
    record = line["d"]
    if record is None:
        invalid.append("the branch has no D arm")
        return result
    invalid.extend(f"D: {reason}" for reason in record["invalid"])
    if record["status"] != "branched":
        invalid.append(f"the D arm wrapper status is {record['status']}")
    if record["branch_decision_index"] != line["decision_index"]:
        invalid.append("the D branch point is at another decision index")
    if record["prefix_sha256"] != line["prefix_sha256"]:
        invalid.append("the actions before the branch point differ from R")
    if record["branch_context_sha256"] != line["context_sha256"]:
        invalid.append("the D branch context differs from R")
    if record["riichi_after_branch"] != 0:
        invalid.append("a riichi of the branch seat was applied in the D arm")
    if (
        record["round"] != line["round"]
        or record["round_ordinal"] != (line["round_ordinal"])
    ):
        invalid.append("the D arm settled another round")
    if line["d_repeat"] is not None and line["d_repeat"] != record:
        invalid.append("the D arm repeat differs")
    result["replacements"] = record["replacements"]
    try:
        settled = d_settlement(record)
    except RiichiDamaBranchError as error:
        invalid.append(str(error))
        return result
    if settled is None:
        invalid.append("the D arm has no settlement")
        return result
    if settled["points_before"] != expected_r["points_before"]:
        invalid.append("the D round does not start from the R scores")
    difference = settled["point_deltas"][seat] - expected_r["point_deltas"][seat]
    if difference % 100:
        invalid.append("the delta is not a multiple of 100 points")
        return result
    result["delta"] = difference // 100
    result["outcomes"] = {
        ARM_R: _seat_outcome(expected_r, seat),
        ARM_D: _seat_outcome(settled, seat),
    }
    result["final_riichi_stick_awards"] = {
        ARM_R: None
        if expected_r["final_riichi_stick_awards"] is None
        else expected_r["final_riichi_stick_awards"][seat],
        ARM_D: None
        if settled["final_riichi_stick_awards"] is None
        else settled["final_riichi_stick_awards"][seat],
    }
    return result


# ---------------------------------------------------------------------------
# 統計（Fractionで厳密に）
# ---------------------------------------------------------------------------


def nearest_rank(ordered: list[Fraction], quantile: Fraction) -> Fraction:
    return ordered[max(1, math.ceil(quantile * len(ordered))) - 1]


def bootstrap(clusters: list[list[int]]) -> dict[str, Fraction]:
    """seed clusterの復元抽出（``random.Random(539)`` を呼ぶたびに新しく初期化する）。"""
    m = len(clusters)
    if m == 0:
        _fail("bootstrap needs at least one cluster")
    sums = [sum(deltas) for deltas in clusters]
    counts = [len(deltas) for deltas in clusters]
    rng = random.Random(BOOTSTRAP_SEED)
    statistics = []
    for _ in range(BOOTSTRAP_REPLICATES):
        total = count = 0
        for _ in range(m):
            index = rng.randrange(m)
            total += sums[index]
            count += counts[index]
        statistics.append(Fraction(total, count))
    statistics.sort()
    return {name: nearest_rank(statistics, q) for name, q in QUANTILES.items()}


def _median(values: list[int]) -> Fraction:
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return Fraction(ordered[middle])
    return Fraction(ordered[middle - 1] + ordered[middle], 2)


def delta_stats(rows: list[dict], *, with_bootstrap: bool) -> dict:
    """部分集合のΔ（100点単位）の件数・seed数・平均・中央値・分布・分位。"""
    by_seed: dict[int, list[int]] = {}
    for row in rows:
        by_seed.setdefault(row["seed"], []).append(row["delta"])
    deltas = [row["delta"] for row in rows]
    stats = {
        "count": len(deltas),
        "distribution": [[value, deltas.count(value)] for value in sorted(set(deltas))],
        "mean": _ratio(Fraction(sum(deltas), len(deltas))) if deltas else None,
        "median": _ratio(_median(deltas)) if deltas else None,
        "seeds": len(by_seed),
    }
    if with_bootstrap:
        clusters = [by_seed[seed] for seed in sorted(by_seed)]
        stats["bootstrap"] = (
            {name: _ratio(value) for name, value in bootstrap(clusters).items()}
            if clusters
            else None
        )
    return stats


def _fraction_of(value: dict | None) -> Fraction | None:
    return None if value is None else Fraction(value["numerator"], value["denominator"])


def gate_decision(criteria: dict, *, invalid: int, subsets: dict) -> tuple[str, dict]:
    """#539 §6の順に1つだけ判定する（STOP-INCOMPLETEはsummaryを作らない経路）。"""
    qualified = [
        name
        for name in SUBSETS
        if subsets[name]["count"] >= criteria["minimum_branches"]
        and subsets[name]["seeds"] >= criteria["minimum_seeds"]
    ]
    signal = [
        name
        for name in qualified
        if _fraction_of(subsets[name]["mean"]) > 0
        and _fraction_of(subsets[name]["bootstrap"]["P10"]) > 0
    ]
    checks = {
        "invalid": invalid,
        "minimum_branches": criteria["minimum_branches"],
        "minimum_seeds": criteria["minimum_seeds"],
        "qualified_subsets": qualified,
        "signal_subsets": signal,
    }
    if invalid > 0:
        return STOP_INVALID, checks
    if not qualified:
        return STOP_LOW_ACTIVATION, checks
    if not signal:
        return STOP_NO_SIGNAL, checks
    return PROCEED, checks


# ---------------------------------------------------------------------------
# timings
# ---------------------------------------------------------------------------

_TIMING_ROW_FIELDS = frozenset(
    {
        "arm",
        "branch_ordinal",
        "parent_peak_rss_kib",
        "seed",
        "wall_seconds",
        "worker_peak_rss_kib",
    }
)
_TIMING_FIELDS = frozenset(
    {
        "finished_at_utc",
        "monotonic_elapsed_seconds",
        "not_rederived",
        "plan_digest",
        "realtime_elapsed_seconds",
        "schedule_wall_seconds",
        "schema",
        "started_at_utc",
        "summary_seconds",
        "tasks",
        "write_seconds",
    }
)


def expected_tasks(plan: dict, branches: list[dict] | None) -> list[tuple]:
    """timingsの行が持つべき (arm, seed, branch_ordinal) の順序。"""
    tasks = [(ARM_R, item["seed"], None) for item in plan_schedule(plan)]
    if branches is None:
        return tasks
    arms = [line for line in branches[1:] if line["d"] is not None]
    tasks.extend((ARM_D, line["seed"], line["branch_ordinal"]) for line in arms)
    if plan["run_kind"] == REHEARSAL:
        tasks.extend(
            (ARM_D_REPEAT, line["seed"], line["branch_ordinal"]) for line in arms
        )
    return tasks


def _seconds(value: object, context: str) -> float:
    if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
        _fail(f"{context} must be a finite non-negative number")
    return float(value)


def _rss(value: object, context: str) -> int | None:
    if value is None:
        return None
    if type(value) is not int or value < 0:
        _fail(f"{context} must be a non-negative int or null")
    return value


def require_timings(
    plan: dict, timings: object, tasks: list[tuple], *, complete: bool = True
) -> dict:
    """timingsの形・task順と、実行時と同じ上限（RSS・wall）を再確認する。

    ``complete=False`` は書込中の検査で、summaryが使う値（task行とschedule wall）だけを見る。
    """
    limits = plan["resource_limits"]
    if type(timings) is not dict or timings.get("plan_digest") != plan["plan_digest"]:
        _fail("timings do not belong to the plan")
    rows = timings.get("tasks")
    if (
        type(rows) is not list
        or any(type(row) is not dict or set(row) != _TIMING_ROW_FIELDS for row in rows)
        or [(row["arm"], row["seed"], row["branch_ordinal"]) for row in rows] != tasks
    ):
        _fail("timings do not cover the executed tasks in order")
    for index, row in enumerate(rows):
        _seconds(row["wall_seconds"], f"tasks[{index}].wall_seconds")
        known = [
            value
            for value in (
                _rss(row["parent_peak_rss_kib"], f"tasks[{index}].parent"),
                _rss(row["worker_peak_rss_kib"], f"tasks[{index}].worker"),
            )
            if value is not None
        ]
        if sum(known) * 1024 > limits["process_tree_rss_bytes"]:
            _fail("parent + worker peak RSS exceeded the process tree limit")
    schedule = _seconds(timings.get("schedule_wall_seconds"), "schedule_wall_seconds")
    if schedule > limits["wall_seconds"]:
        _fail("the schedule exceeded the wall limit")
    if complete:
        if set(timings) != _TIMING_FIELDS or timings["schema"] != TIMINGS_SCHEMA:
            _fail("timings fields do not match")
        elapsed = max(
            _seconds(timings["monotonic_elapsed_seconds"], "monotonic"),
            _seconds(timings["realtime_elapsed_seconds"], "realtime"),
        )
        if elapsed > limits["wall_seconds"]:
            _fail("the run exceeded the wall limit")
        if timings["not_rederived"] is not True:
            _fail("timings are measurements and are not re-derived")
    return timings


# ---------------------------------------------------------------------------
# summary（保存物だけから再導出できる値）
# ---------------------------------------------------------------------------


def _timing_summary(timings: dict) -> dict:
    rows = timings["tasks"]
    return {
        "d_arm_wall_seconds": distribution(
            [row["wall_seconds"] for row in rows if row["arm"] == ARM_D]
        ),
        "d_repeat_wall_seconds": distribution(
            [row["wall_seconds"] for row in rows if row["arm"] == ARM_D_REPEAT]
        ),
        "r_hanchan_wall_seconds": distribution(
            [row["wall_seconds"] for row in rows if row["arm"] == ARM_R]
        ),
        "schedule_wall_seconds": timings["schedule_wall_seconds"],
    }


def build_summary(
    plan: dict,
    hanchans: dict,
    branches: list[dict],
    features: list[dict],
    timings: dict,
) -> dict:
    """summaryは保存物（plan・hanchans・branches・features・timingsのtask行）だけから決まる。"""
    require_branch_lines(plan, branches, hanchans)
    require_feature_lines(plan, features, branches)
    require_timings(plan, timings, expected_tasks(plan, branches), complete=False)
    rows_by_seed = {row["seed"]: row for row in hanchans["hanchans"]}
    evaluated = [
        evaluate_branch(line, feature, rows_by_seed[line["seed"]])
        for line, feature in zip(branches[1:], features[1:], strict=True)
    ]
    invalid = [row for row in evaluated if row["invalid"]]
    valid = [row for row in evaluated if not row["invalid"]]
    subsets = {
        name: delta_stats(
            [row for row in valid if row["subsets"][name]], with_bootstrap=True
        )
        for name in SUBSETS
    }
    outcome_counts = {
        arm: {name: 0 for name in _SEAT_OUTCOMES} for arm in (ARM_R, ARM_D)
    }
    for row in valid:
        for arm in (ARM_R, ARM_D):
            outcome_counts[arm][row["outcomes"][arm]] += 1
    matches = [row["dama_choice_matches_declaration"] for row in valid]
    declined = [row["riichi_declined"] for row in hanchans["hanchans"]]
    rows = [
        {
            "branch_id": row["branch_id"],
            "dama_choice_matches_declaration": row["dama_choice_matches_declaration"],
            "delta": row["delta"],
            "final_riichi_stick_awards": row["final_riichi_stick_awards"],
            "invalid": row["invalid"],
            "outcomes": row["outcomes"],
            "replacements": row["replacements"],
            "seed": row["seed"],
            "subsets": row["subsets"],
            "yaku": row["yaku"],
        }
        for row in evaluated
    ]
    gate = None
    status = REHEARSAL_STATUS
    if plan["run_kind"] == ALLOCATED:
        decision, checks = gate_decision(
            CRITERIA, invalid=len(invalid), subsets=subsets
        )
        gate = {"checks": checks, "decision": decision}
        status = COMPLETE_STATUS
    payload = {
        "arms": {
            "denominator": len(valid),
            "seat_outcomes": outcome_counts,
        },
        "branches": len(evaluated),
        "decisions": sum(row["decisions"] for row in hanchans["hanchans"]),
        "descriptive": {
            "all": delta_stats(valid, with_bootstrap=False),
            "no_yaku": delta_stats(
                [row for row in valid if not row["yaku"]], with_bootstrap=False
            ),
            "yaku": delta_stats(
                [row for row in valid if row["yaku"]], with_bootstrap=False
            ),
        },
        "dama_choice_matches_declaration": {
            "denominator": len(matches),
            "numerator": sum(1 for item in matches if item),
        },
        "evaluation_kind": EVALUATION_KIND,
        "gate": gate,
        "hanchan_count": len(hanchans["hanchans"]),
        "inputs": {
            "branches_sha256": sha256_bytes(jsonl_text(branches).encode("utf-8")),
            "features_sha256": sha256_bytes(jsonl_text(features).encode("utf-8")),
            "hanchans_sha256": document_sha256(hanchans),
        },
        "invalid": {
            "branches": [
                {"branch_id": row["branch_id"], "reasons": row["invalid"]}
                for row in invalid
            ],
            "count": len(invalid),
        },
        "kyoku_count": sum(row["kyoku_count"] for row in hanchans["hanchans"]),
        "memory": memory_summary(timings["tasks"]),
        "not_a_strength_result": True,
        "observation": plan["observation"],
        "overlap_s1_s3": sum(
            1 for row in valid if row["subsets"]["S1"] and row["subsets"]["S3"]
        ),
        "plan_digest": plan["plan_digest"],
        "protocol_version": PROTOCOL_VERSION,
        "replacements": {
            "branches_with_replacement": sum(1 for row in valid if row["replacements"]),
            "total": sum(row["replacements"] for row in valid),
        },
        "riichi_declined": sum(sum(item) for item in declined),
        "rows": rows,
        "run_kind": plan["run_kind"],
        "schema": SUMMARY_SCHEMA,
        "seat_hanchans": 4 * len(hanchans["hanchans"]),
        "status": status,
        "subsets": subsets,
        "timing": _timing_summary(timings),
    }
    return {**payload, "summary_digest": document_identity(payload)}


def build_unobserved_summary(plan: dict, hanchans: dict, timings: dict) -> dict:
    require_hanchans(plan, hanchans)
    require_timings(plan, timings, expected_tasks(plan, None), complete=False)
    payload = {
        "evaluation_kind": EVALUATION_KIND,
        "gate": None,
        "hanchan_count": len(hanchans["hanchans"]),
        "inputs": {"hanchans_sha256": document_sha256(hanchans)},
        "kyoku_count": sum(row["kyoku_count"] for row in hanchans["hanchans"]),
        "memory": memory_summary(timings["tasks"]),
        "not_a_strength_result": True,
        "observation": UNOBSERVED,
        "plan_digest": plan["plan_digest"],
        "protocol_version": PROTOCOL_VERSION,
        "run_kind": plan["run_kind"],
        "schema": SUMMARY_SCHEMA,
        "status": REHEARSAL_STATUS,
        "timing": _timing_summary(timings),
    }
    return {**payload, "summary_digest": document_identity(payload)}


# ---------------------------------------------------------------------------
# run
# ---------------------------------------------------------------------------


def _sums_text(directory: Path) -> str:
    entries = output_files(directory, exclude=(SUMS_FILENAME,))
    return "".join(f"{item['sha256']}  {item['path']}\n" for item in entries)


def _r_tasks(plan: dict) -> list[HanchanTask]:
    champion = plan_champion(plan).to_document()
    return [
        HanchanTask(
            seed=item["seed"],
            placement=item["placement"],
            candidate=champion,
            incumbent=champion,
            wheel_path=plan["execution"]["wheel_path"],
        )
        for item in plan_schedule(plan)
    ]


def _execute(
    plan: dict,
    tasks: list,
    *,
    key_fields: tuple[str, ...],
    play: Callable,
    on_executed: Callable[[ExecutedHanchan], None],
    on_task: Callable[[int, int], None] | None,
) -> list[ExecutedHanchan]:
    return run_tasks(
        tasks,
        key_fields=key_fields,
        timeout_seconds=plan["execution"]["timeout_seconds"],
        expected_environment=plan["environment"],
        play=play,
        on_hanchan=on_task,
        on_executed=on_executed,
    )


def execute_branches(
    plan: dict,
    *,
    play_r: Callable,
    play_d: Callable,
    measure: Callable[[str], Callable[[ExecutedHanchan], None]],
    on_task: Callable[[int, int], None] | None = None,
    repeat: bool = False,
) -> tuple[list[ExecutedHanchan], list, dict, dict | None]:
    """Rの全半荘 → 全D arm（``repeat`` なら新しいworkerでもう1回）を実行する。

    戻り値は ``(executed_r, branches, d_records, repeat_records)``。
    """
    executed_r = _execute(
        plan,
        _r_tasks(plan),
        key_fields=("placement",),
        play=play_r,
        on_executed=measure(ARM_R),
        on_task=on_task,
    )
    if any(item.policy_analysis is None for item in executed_r):
        raise RunStopped("an R hanchan returned no observation")
    branches = r_branches(executed_r)
    tasks = branch_tasks(plan, branches)
    d_records: dict[int, dict] = {}
    repeat_records: dict[int, dict] | None = {} if repeat else None
    if tasks:
        for arm, target in ((ARM_D, d_records), (ARM_D_REPEAT, repeat_records)):
            if target is None:
                continue
            executed = _execute(
                plan,
                tasks,
                key_fields=("branch_ordinal",),
                play=play_d,
                on_executed=measure(arm),
                on_task=on_task,
            )
            for task, item in zip(tasks, executed, strict=True):
                target[task.branch_ordinal] = item.policy_analysis
    return executed_r, branches, d_records, repeat_records


def run_plan(
    plan_path: str | Path,
    *,
    ledger_path: str | Path = seed_registry.DEFAULT_LEDGER_PATH,
    play_r: Callable | None = None,
    play_d: Callable = play_d_arm,
    live_gate: Callable[..., None] = require_live_target,
    on_task: Callable[[int, int], None] | None = None,
    clock: Callable[[], float] | None = None,
    realtime: Callable[[], float] | None = None,
) -> dict[str, object]:
    """実行して公開する（失敗はSTOP-INCOMPLETEで何も公開しない）。

    ``play_r`` / ``play_d`` / ``live_gate`` / clockはtestのseam。
    """
    plan = load_plan(plan_path)
    observed = plan["observation"] == OBSERVED
    if play_r is None:
        play_r = play_r_observed if observed else play_r_unobserved
    default_clock, default_realtime = default_clocks()
    clock = clock or default_clock
    realtime = realtime or default_realtime
    output = Path(plan["artifact_destinations"]["output"])
    diagnostics = Path(plan["artifact_destinations"]["diagnostics"])
    if output.exists():
        _fail("output already exists; outputs are write-once and never re-run")
    limits = plan["resource_limits"]
    rows: list[dict] = []
    started_at = utc_now()
    started = clock()
    started_real = realtime()

    def elapsed() -> float:
        return max(clock() - started, realtime() - started_real)

    def measure(arm: str):
        def record(item: ExecutedHanchan) -> None:
            parent_kib = current_peak_rss_kib()
            rows.append(
                {
                    "arm": arm,
                    "branch_ordinal": getattr(item.capture, "branch_ordinal", None),
                    "parent_peak_rss_kib": parent_kib,
                    "seed": item.capture.seed,
                    "wall_seconds": item.wall_seconds,
                    "worker_peak_rss_kib": item.peak_rss_kib,
                }
            )
            if elapsed() > limits["wall_seconds"]:
                raise RunStopped(
                    f"the run exceeded the wall limit of {limits['wall_seconds']} s "
                    f"after {len(rows)} tasks"
                )
            known = [
                value for value in (parent_kib, item.peak_rss_kib) if value is not None
            ]
            if sum(known) * 1024 > limits["process_tree_rss_bytes"]:
                raise RunStopped(
                    "parent + worker peak RSS exceeded the process tree limit of "
                    f"{limits['process_tree_rss_bytes']} bytes"
                )

        return record

    def ledger():
        if plan["run_kind"] != ALLOCATED:
            return None
        return seed_registry.load_ledger(ledger_path)

    try:
        live_gate(plan, ledger=ledger())
        if observed:
            executed_r, branches, d_records, repeat_records = execute_branches(
                plan,
                play_r=play_r,
                play_d=play_d,
                measure=measure,
                on_task=on_task,
                repeat=plan["run_kind"] == REHEARSAL,
            )
        else:
            executed_r = _execute(
                plan,
                _r_tasks(plan),
                key_fields=("placement",),
                play=play_r,
                on_executed=measure(ARM_R),
                on_task=on_task,
            )
        schedule_seconds = clock() - started
        live_gate(plan, ledger=ledger())
        with staged_artifact_directory(output) as staging:
            timings = {
                "not_rederived": True,
                "plan_digest": plan["plan_digest"],
                "schedule_wall_seconds": schedule_seconds,
                "schema": TIMINGS_SCHEMA,
                "started_at_utc": started_at,
                "tasks": rows,
            }
            write_started = clock()
            hanchans = hanchans_document(plan, executed_r, observed=observed)
            write_new_artifact_file(
                staging / HANCHANS_FILENAME, canonical_json_text(hanchans)
            )
            if observed:
                lines = branch_lines(
                    plan, hanchans, branches, d_records, repeat_records
                )
                features = feature_lines(plan, branches)
                write_new_artifact_file(staging / BRANCHES_FILENAME, jsonl_text(lines))
                write_new_artifact_file(
                    staging / FEATURES_FILENAME, jsonl_text(features)
                )
            timings["write_seconds"] = clock() - write_started
            summary_started = clock()
            if observed:
                summary = build_summary(plan, hanchans, lines, features, timings)
            else:
                summary = build_unobserved_summary(plan, hanchans, timings)
            write_new_artifact_file(
                staging / SUMMARY_FILENAME, canonical_json_text(summary)
            )
            timings["summary_seconds"] = clock() - summary_started
            timings["finished_at_utc"] = utc_now()
            timings["monotonic_elapsed_seconds"] = clock() - started
            timings["realtime_elapsed_seconds"] = realtime() - started_real
            write_new_artifact_file(
                staging / TIMINGS_FILENAME, canonical_json_text(timings)
            )
            write_new_artifact_file(staging / SUMS_FILENAME, _sums_text(staging))
            if elapsed() > limits["wall_seconds"]:
                raise RunStopped("the run exceeded the wall limit while saving")
    except BaseException as error:
        write_stop(diagnostics, plan, error, len(rows), schema=STOP_SCHEMA)
        if not isinstance(error, Exception) or isinstance(
            error, (RunStopped, EngineAabbProtocolError)
        ):
            raise
        raise RunStopped(f"STOP-INCOMPLETE: {type(error).__name__}: {error}") from error
    return summary


# ---------------------------------------------------------------------------
# verify
# ---------------------------------------------------------------------------

_ANALYSIS_ENVIRONMENT = ("native", "python", "shanten_backend")


def require_analysis_environment(plan: dict, environment: dict) -> None:
    """再導出・再実行に使うlisjong / native / Python / engineがplanと同じであること。"""
    expected = plan["environment"]
    for name in _ANALYSIS_ENVIRONMENT:
        if environment.get(name) != expected[name]:
            _fail(f"verify environment {name} differs from the plan")
    for name in ("lisjong_revision", "lisjong_engine_revision"):
        if environment["provenance"].get(name) != expected["provenance"].get(name):
            _fail(f"verify environment {name} differs from the plan")


def rederive_features(branches: list[dict], features: list[dict]) -> int:
    """保存したcontextと宣言牌からlisjongの特徴を再導出し、記録と完全一致を要求する。"""
    count = 0
    for line, feature in zip(branches[1:], features[1:], strict=True):
        derived = branch_features(line)
        if derived != {"error": feature["error"], "features": feature["features"]}:
            _fail(f"branch {line['branch_id']}: re-derived features differ")
        count += 1
    return count


_R_BRANCH_KEYS = (
    ("context", "context"),
    ("context_sha256", "context_sha256"),
    ("decision_index", "decision_index"),
    ("declaration", "declaration"),
    ("declaration_decision_index", "declaration_decision_index"),
    ("prefix_sha256", "prefix_sha256"),
    ("r_invalid", "invalid"),
    ("r_post_sha256", "post_sha256"),
    ("riichi", "riichi"),
    ("round", "round"),
    ("round_ordinal", "round_ordinal"),
    ("seat", "seat"),
)


def replay_seeds_of(
    plan: dict,
    hanchans: dict,
    branches: list[dict],
    features: list[dict],
    *,
    play_r: Callable,
    play_d: Callable,
) -> dict:
    """固定seedのR区間と全D armを再実行し、記録との完全一致を要求する。"""
    seeds = plan["verify_replay_seeds"]
    if not seeds:
        return {"arms": 0, "seeds": []}
    rows = {row["seed"]: row for row in hanchans["hanchans"]}
    features_by_ordinal = {line["branch_ordinal"]: line for line in features[1:]}
    stored = {}
    for line in branches[1:]:
        stored.setdefault(line["seed"], []).append(line)
    champion = plan_champion(plan).to_document()
    tasks = [
        HanchanTask(
            seed=seed,
            placement=0,
            candidate=champion,
            incumbent=champion,
            wheel_path=plan["execution"]["wheel_path"],
        )
        for seed in seeds
    ]
    executed = _execute(
        plan,
        tasks,
        key_fields=("placement",),
        play=play_r,
        on_executed=lambda item: None,
        on_task=None,
    )
    arms = 0
    for seed, item in zip(seeds, executed, strict=True):
        if hanchan_row(item, observed=True) != rows[seed]:
            _fail(f"seed {seed}: the replayed R differs from the record (STOP-INVALID)")
        records = sorted(
            item.policy_analysis["branches"],
            key=lambda record: record["decision_index"],
        )
        lines = stored.get(seed, [])
        if len(records) != len(lines):
            _fail(f"seed {seed}: the replayed R has another number of branches")
        d_tasks = []
        for record, line in zip(records, lines, strict=True):
            if any(line[name] != record[key] for name, key in _R_BRANCH_KEYS):
                _fail(f"branch {line['branch_id']}: the replayed R branch differs")
            feature = features_by_ordinal[line["branch_ordinal"]]
            if record["features"] != {
                "error": feature["error"],
                "features": feature["features"],
            }:
                _fail(f"branch {line['branch_id']}: the replayed features differ")
            if record["declaration"] is not None:
                d_tasks.append(
                    (
                        line,
                        BranchTask(
                            seed=seed,
                            branch_ordinal=line["branch_ordinal"],
                            branch=task_branch(record),
                            incumbent=champion,
                            wheel_path=plan["execution"]["wheel_path"],
                        ),
                    )
                )
        if d_tasks:
            replayed = _execute(
                plan,
                [task for _, task in d_tasks],
                key_fields=("branch_ordinal",),
                play=play_d,
                on_executed=lambda item: None,
                on_task=None,
            )
            for (line, _), arm in zip(d_tasks, replayed, strict=True):
                if arm.policy_analysis != line["d"]:
                    _fail(
                        f"branch {line['branch_id']}: the replayed D arm differs "
                        "(STOP-INVALID)"
                    )
                arms += 1
    return {"arms": arms, "seeds": list(seeds)}


def verified_plan(
    plan_path: str | Path,
    *,
    environment: dict | None = None,
    play_r: Callable = play_r_observed,
    play_d: Callable = play_d_arm,
) -> tuple[dict, dict, dict, list[dict], dict]:
    """保存物を検査・再導出し、``(plan, summary, timings, files, rederived)`` を返す。"""
    plan = load_plan(plan_path)
    if environment is None:
        try:
            environment = _live_environment(plan["execution"]["wheel_path"])
        except EngineAabbLockError as error:
            raise RiichiDamaBranchError(str(error)) from error
    output = Path(plan["artifact_destinations"]["output"])
    try:
        recorded_sums = (output / SUMS_FILENAME).read_bytes().decode("utf-8")
    except (OSError, UnicodeDecodeError) as error:
        raise RiichiDamaBranchError(f"cannot read {SUMS_FILENAME}: {error}") from error
    if recorded_sums != _sums_text(output):
        _fail("output files differ from SHA256SUMS (missing, extra or changed)")
    observed = plan["observation"] == OBSERVED
    expected_files = {
        SUMMARY_FILENAME,
        HANCHANS_FILENAME,
        TIMINGS_FILENAME,
        SUMS_FILENAME,
    }
    if observed:
        expected_files |= {BRANCHES_FILENAME, FEATURES_FILENAME}
    files = output_files(output)
    if {item["path"] for item in files} != expected_files:
        _fail("output files do not match the expected set")
    recorded = read_document(output / SUMMARY_FILENAME, "summary")
    hanchans = read_document(output / HANCHANS_FILENAME, "hanchans")
    timings = read_document(output / TIMINGS_FILENAME, "timings")
    rederived = {"features": 0, "replay": {"arms": 0, "seeds": []}}
    if observed:
        branches = read_jsonl(output / BRANCHES_FILENAME, "branches")
        features = read_jsonl(output / FEATURES_FILENAME, "features")
        summary = build_summary(plan, hanchans, branches, features, timings)
        require_analysis_environment(plan, environment)
        rederived["features"] = rederive_features(branches, features)
        tasks = expected_tasks(plan, branches)
    else:
        summary = build_unobserved_summary(plan, hanchans, timings)
        tasks = expected_tasks(plan, None)
    if recorded != summary:
        _fail("summary differs from the re-derivation")
    require_timings(plan, timings, tasks)
    if observed:
        rederived["replay"] = replay_seeds_of(
            plan, hanchans, branches, features, play_r=play_r, play_d=play_d
        )
    return plan, summary, timings, files, rederived


def _write_once(output: str | Path | None, document: dict) -> None:
    if output is None:
        return
    destination = Path(output)
    if destination.exists():
        raise FileExistsError(f"refusing to overwrite: {destination}")
    write_new_artifact_file(destination, canonical_json_text(document))


def verify_plan(
    plan_path: str | Path,
    *,
    output: str | Path | None = None,
    environment: dict | None = None,
    play_r: Callable = play_r_observed,
    play_d: Callable = play_d_arm,
) -> dict[str, object]:
    plan, summary, timings, files, rederived = verified_plan(
        plan_path, environment=environment, play_r=play_r, play_d=play_d
    )
    gate = summary["gate"]
    document = {
        "clocks": clock_document(timings),
        "decision": None if gate is None else gate["decision"],
        "files": files,
        "observation": plan["observation"],
        "output_bytes": sum(item["bytes"] for item in files),
        "plan_digest": plan["plan_digest"],
        "rederived_lisjong_features": rederived["features"],
        "replayed": rederived["replay"],
        "run_kind": plan["run_kind"],
        "schema": VERIFY_SCHEMA,
        "status": summary["status"],
        "summary_digest": summary["summary_digest"],
    }
    _write_once(output, document)
    return document


_COMPARED = (
    "action_sequence_sha256",
    "completed_match_sha256",
    "decisions",
    "end_reason",
    "final_points",
    "kyoku_count",
    "kyoku_record",
    "policy_call_counts",
)


def compare_rehearsals(
    observed_plan: str | Path,
    unobserved_plan: str | Path,
    *,
    output: str | Path | None = None,
    environment: dict | None = None,
) -> dict[str, object]:
    """観測あり / なしのRの一致と、観測ありplanのD armの2回実行の一致を要求する。"""
    observed, summary, _, _, _ = verified_plan(observed_plan, environment=environment)
    unobserved, _, _, _, _ = verified_plan(unobserved_plan, environment=environment)
    if (observed["run_kind"], observed["observation"]) != (REHEARSAL, OBSERVED) or (
        unobserved["run_kind"],
        unobserved["observation"],
    ) != (REHEARSAL, UNOBSERVED):
        _fail("compare takes an observed and an unobserved rehearsal plan")
    for name in ("environment_digest", "execution_target", "participant", "population"):
        if observed[name] != unobserved[name]:
            _fail(f"rehearsal plans differ in {name}")
    rows = []
    for plan in (observed, unobserved):
        path = Path(plan["artifact_destinations"]["output"]) / HANCHANS_FILENAME
        rows.append(read_document(path, "hanchans")["hanchans"])
    seeds = []
    for left, right in zip(*rows, strict=True):
        differences = [name for name in _COMPARED if left[name] != right[name]]
        seeds.append({"differences": differences, "seed": left["seed"]})
    invalid = summary["invalid"]["count"]
    match = all(not item["differences"] for item in seeds) and invalid == 0
    document = {
        "branches": summary["branches"],
        "compared_fields": list(_COMPARED),
        "invalid_branches": invalid,
        "match": match,
        "observed_plan_digest": observed["plan_digest"],
        "observed_summary_digest": summary["summary_digest"],
        "schema": COMPARE_SCHEMA,
        "seeds": seeds,
        "unobserved_plan_digest": unobserved["plan_digest"],
    }
    _write_once(output, document)
    if not match:
        _fail(
            "rehearsal R observed / unobserved, the D arm repeat or a branch check "
            "differ (STOP-INVALID)"
        )
    return document


def estimate_budget(
    observed_plan: str | Path,
    unobserved_plan: str | Path,
    *,
    output: str | Path | None = None,
    environment: dict | None = None,
) -> dict[str, object]:
    """照合済みrehearsalの実測から§7の時間見積もりを作る（判定は記録する）。"""
    compared = compare_rehearsals(
        observed_plan, unobserved_plan, environment=environment
    )
    plan, summary, timings, _, _ = verified_plan(observed_plan, environment=environment)
    rows = timings["tasks"]
    values = estimate_values(
        r_walls=[row["wall_seconds"] for row in rows if row["arm"] == ARM_R],
        d_walls=[row["wall_seconds"] for row in rows if row["arm"] == ARM_D],
        branches=summary["branches"],
        rehearsal_seat_hanchans=summary["seat_hanchans"],
    )
    payload = {
        **values,
        "environment_digest": plan["environment_digest"],
        "execution_target": plan["execution_target"],
        "observed_plan_digest": plan["plan_digest"],
        "observed_summary_digest": compared["observed_summary_digest"],
        "schema": ESTIMATE_SCHEMA,
        "unobserved_plan_digest": compared["unobserved_plan_digest"],
    }
    document = parse_time_estimate(
        {**payload, "estimate_digest": document_identity(payload)}
    )
    _write_once(output, document)
    return document


__all__ = [
    "ARMS",
    "BOOTSTRAP_REPLICATES",
    "BOOTSTRAP_SEED",
    "COMPLETE_STATUS",
    "LAYOUT",
    "REHEARSAL_STATUS",
    "bootstrap",
    "branch_lines",
    "branch_tasks",
    "build_summary",
    "build_unobserved_summary",
    "compare_rehearsals",
    "d_settlement",
    "delta_stats",
    "estimate_budget",
    "evaluate_branch",
    "execute_branches",
    "expected_tasks",
    "feature_lines",
    "gate_decision",
    "hanchan_row",
    "hanchans_document",
    "r_branches",
    "r_settlement",
    "read_jsonl",
    "rederive_features",
    "replay_seeds_of",
    "require_branch_lines",
    "require_hanchans",
    "require_timings",
    "run_plan",
    "subsets_of",
    "verify_plan",
]
