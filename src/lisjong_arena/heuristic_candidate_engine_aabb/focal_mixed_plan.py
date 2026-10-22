"""#522 凍結paijia候補の混成相手focal比較（DEVELOPMENT）のprotocol・plan・実行枠。

AABB（#514）・#520とは別のcontractである。同じ卓にA/Bの2席ではなく、focal 1席と
固定の相手3席を置き、focalだけをAとBで入れ替えた2半荘を同じseed・同じ相手席で比べる。

```text
seed s（seed順）
  rotation r = 0, 1, 2, 3
    condition A: seat r = A（focal）  seat r+1 = O1  seat r+2 = O2  seat r+3 = O3（mod 4）
    condition B: seat r = B（focal）  他の3席は条件Aと同じidentity・同じ席
-> 1 seed 8半荘、K = 32 で256半荘（1回だけ）

D_s = sum_r (F_A(s, r) - F_B(s, r)) / 40     [pt / focal seat-result]
      F_X(s, r) = 条件Xのfocal席のengine final_points（内部単位 1 = 0.1 pt）
```

- O1はBと同じidentityだが、roleで区別する。focalの抽出はseatのrole ``focal``
  （= seat r）だけで行い、identityでは行わない
- 局記録（第1層・第2層）の局・終局の検査とI/OはAABBと共通で、半荘identity
  （``rotation`` / ``condition``）・seat roleの検査とschema名だけをこのprotocolで持つ
- planはwrite-onceで、participants・seeds・timeout・上限・判定条件・環境・保存先を
  実行前に固定する（``plan_digest``）。defaultはない
- rehearsal（既知の開発用seed 472005、8半荘）は同じ経路の動作確認で、判定を出さない
"""

import datetime
import math
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from lisjong_arena import seed_registry
from lisjong_arena._artifact_io import (
    ArtifactValidationError,
    canonical_json_text,
    read_json_document,
    staged_artifact_directory,
    write_new_artifact_file,
)
from lisjong_arena._execution_safety import (
    ExecutionSafetyError,
    require_new_artifact_destinations,
)
from lisjong_arena.overall_champion_aabb.lock import document_identity

from .development import catalog_participant
from .errors import KyokuRecordError
from .executor import ExecutedHanchan, RunStopped, current_peak_rss_kib, run_tasks
from .kyoku_record import SEAT_COUNT, RecordLayout, SeatAssignment
from .lock import (
    EXECUTION_BRANCH,
    EngineAabbLockError,
    _live_environment,
    _live_head,
    _require_environment,
    require_allocated_seeds,
)
from .paijia_tie_plan import rss, seconds
from .protocol import (
    BACKEND,
    INTERNAL_UNITS_PER_POINT,
    RULES_DIGEST,
    SEED_DOMAIN,
    EngineAabbProtocolError,
    Participant,
    parse_participant,
    require_hex,
    require_pair,
    require_seeds,
)
from .protocol import (
    protocol_document as aabb_protocol_document,
)

PROTOCOL_ID = "arena-heuristic-candidate-engine-focal-mixed-v1"
PROTOCOL_VERSION = 1
PLAN_SCHEMA = "arena-heuristic-candidate-engine-focal-mixed-plan-v1"
STOP_SCHEMA = "arena-heuristic-candidate-engine-focal-mixed-stop-v1"
EVALUATION_KIND = "DEVELOPMENT"
SPLIT = "DEVELOPMENT"
OWNER_ISSUE = "lisbun/lisjong-arena#522"
POPULATION = "heuristic-candidate-engine-focal-mixed-522"
STOP_STATUS = "STOP-INCOMPLETE"

FROZEN_LISJONG_REVISION = "0fff8918ec349eeec129907f6a723e2d7314b95a"
FROZEN_WHEEL_SHA256 = "130929229b16b0508a61e77d04c0a219ab65a15f7e79a74d60c43725c0cb8fba"
"""Issue #522の凍結値。live環境から作るplanはこの2つと一致しなければならない。"""

CONDITION_A = "A"
CONDITION_B = "B"
CONDITIONS = (CONDITION_A, CONDITION_B)
ROLE_FOCAL = "focal"
OPPONENT_ROLES = ("O1", "O2", "O3")
SEAT_ROLES = (ROLE_FOCAL, *OPPONENT_ROLES)
"""seat (r + i) % 4 のrole（i = 0..3）。"""
PARTICIPANT_ROLES = (*CONDITIONS, *OPPONENT_ROLES)

IDENTITIES = {
    CONDITION_A: "placement-aware-speed-call-suited-paijia-tie-v1",
    CONDITION_B: "placement-aware-speed-call",
    "O1": "placement-aware-speed-call",
    "O2": "targeted-honor-release-terminal-progression",
    "O3": "placement-aware-speed-call-kobalab-0004-belief-paijia",
}
"""A（lisjong #330の候補）、B（同revisionの現Champion。歴史的固定C0ではない）、
固定の相手3者（Issue #522で選定理由とともに固定。結果を見て選び直さない）。"""

ROTATIONS = 4
K = 32
HANCHANS_PER_SEED = ROTATIONS * len(CONDITIONS)
COMPARE_HANCHANS = K * HANCHANS_PER_SEED
FOCAL_SEAT_RESULTS_PER_CONDITION = ROTATIONS
DIVISOR = FOCAL_SEAT_RESULTS_PER_CONDITION * INTERNAL_UNITS_PER_POINT
"""D_sの除数40（4 focal seat-results × 内部単位10）。AABBの120は使わない。"""
INTERVAL_Z = 1.96

ALLOCATED = "allocated"
REHEARSAL = "rehearsal"
RUN_KINDS = (ALLOCATED, REHEARSAL)
REHEARSAL_SEEDS = (472_005,)
REHEARSAL_SOURCE = (
    "registry-external #472 development seed (also used by #514 dev-check, #517 "
    "P2 and the #520 rehearsal); rehearsal of the mixed runner, no decision"
)
PURPOSES = {
    ALLOCATED: (
        "#522 one fixed development re-check of the frozen #330 candidate against "
        "the current Champion as focal seat with fixed mixed opponents, 32 new "
        "seeds x 4 rotations x 2 conditions once; DEVELOPMENT, never a formal "
        "classification and never combined with #520"
    ),
    REHEARSAL: (
        "#522 rehearsal: seed 472005 x 4 rotations x 2 conditions through the same "
        "run, save, verify and secondary-metric path; no decision"
    ),
}

CRITERIA = {
    "conditions": {
        "base_points_mean_difference_pt_at_least": 0.0,
        "deal_in_rate_mean_difference_at_most": 0.01,
        "integrity": (
            "complete 256/256, hash-checked, separate-process verify, layer 1 and "
            "layer 2 VALID, focal A analysis consistent, within approved limits"
        ),
        "leave_one_seed_out_minimum_mean_d_pt_above": 0.0,
        "mean_d_pt_above": 0.0,
    },
    "interval": (
        "descriptive only: mean D +/- 1.96 * SD(K-1) / sqrt(K); a crossing of 0 is "
        "reported separately; not a power claim and not a promotion rule"
    ),
    "labels": {
        "all_conditions": "RECONFIRMED-DEVELOPMENT",
        "otherwise": "NOT-RECONFIRMED-DEVELOPMENT",
    },
    "leave_one_seed_out": (
        "for every seed s, the mean of D over the other K-1 seeds; the minimum over "
        "all K must be > 0; a development budget condition fixed before the data, "
        "not significance, non-inferiority or tail robustness"
    ),
    "no_rescue": (
        "NOT-RECONFIRMED stops further v1 games; no relaxed conditions, extra "
        "seeds or swapped opponents; not a claim that no effect exists"
    ),
    "primary": (
        "D_s = sum_r (F_A(s,r) - F_B(s,r)) / 40 pt per focal seat-result, "
        "seed-equal mean; F = focal seat engine final_points; not combined with "
        "the #520 D (different endpoint)"
    ),
    "secondary": (
        "focal seats only, condition A vs B; rates pool the 4 focal seats of a seed, "
        "A-B per seed, seed-equal; denominator 0 is null and a null needed for a "
        "condition fails it; conditional values pool all seeds; never replaces "
        "the primary decision"
    ),
}
"""実行前に固定した進行条件。結果を見てから閾値・指標・相手を替えない。"""

PLAN_FILENAME = "plan.json"
TIMINGS_FILENAME = "timings.json"
SUMMARY_FILENAME = "summary.json"
DESTINATIONS = ("diagnostics", "output")

_PLAN_FIELDS = frozenset(
    {
        "artifact_destinations",
        "criteria",
        "environment",
        "environment_digest",
        "evaluation_kind",
        "execution",
        "execution_target",
        "not_a_formal_result",
        "owner_issue",
        "participants",
        "plan_digest",
        "population",
        "protocol_digest",
        "protocol_id",
        "purpose",
        "resource_limits",
        "rules_digest",
        "run_kind",
        "schema",
    }
)
_ALLOCATED_FIELDS = frozenset(
    {
        "allocation_binding",
        "k",
        "ordered_seeds",
        "population",
        "schedule_digest",
        "seed_domain",
        "split",
    }
)
_REHEARSAL_FIELDS = frozenset({"k", "ordered_seeds", "schedule_digest", "source"})
_EXECUTION_FIELDS = frozenset({"backend", "timeout_seconds", "wheel_path", "workers"})
_LIMIT_FIELDS = frozenset({"enforcement", "process_tree_rss_bytes", "wall_seconds"})


class FocalMixedError(EngineAabbProtocolError):
    """#522のplan・保存物が作れない・読めない、または再導出と一致しない場合。"""


# ---------------------------------------------------------------------------
# protocol
# ---------------------------------------------------------------------------


def seat_roles(rotation: int) -> tuple[str, str, str, str]:
    """rotation rで、seat 0..3のrole（focalはseat r、O1..O3はその下家から順）。"""
    if type(rotation) is not int or not 0 <= rotation < ROTATIONS:
        raise KyokuRecordError("rotation must be an int in 0..3")
    return tuple(SEAT_ROLES[(seat - rotation) % SEAT_COUNT] for seat in range(4))


def protocol_document() -> dict[str, object]:
    """protocol identityを構成する値（``protocol_digest`` の対象）。"""
    aabb = aabb_protocol_document()
    return {
        "endpoint": {
            "block_difference": "D(s) = sum_r (F_A(s,r) - F_B(s,r)) / 4 / 10",
            "focal_extraction": "seat whose recorded role is focal (= seat r)",
            "source": "CompletedMatch.final_score.for_seat(focal seat).final_points",
            "unit": "internal, 1 = 0.1 pt",
        },
        "engine_revision": aabb["engine_revision"],
        "game_mode": aabb["game_mode"],
        "interval_z": INTERVAL_Z,
        "match_end_conditions": aabb["match_end_conditions"],
        "participant_identities": dict(IDENTITIES),
        "protocol_id": PROTOCOL_ID,
        "protocol_version": PROTOCOL_VERSION,
        "rules_digest": aabb["rules_digest"],
        "schedule": {
            "conditions": list(CONDITIONS),
            "order": "seed -> rotation 0..3 -> condition A, B",
            "rotations": ROTATIONS,
            "seat_roles": "seat (r + i) % 4 has role [focal, O1, O2, O3][i]",
        },
    }


PROTOCOL_DIGEST = document_identity(protocol_document())


def schedule(seeds: Sequence[int]) -> list[dict]:
    """実行順: seed順 -> rotation 0..3 -> 条件A, B。"""
    return [
        {
            "condition": condition,
            "roles": list(seat_roles(rotation)),
            "rotation": rotation,
            "seed": seed,
        }
        for seed in seeds
        for rotation in range(ROTATIONS)
        for condition in CONDITIONS
    ]


def schedule_digest(seeds: Sequence[int]) -> str:
    return document_identity({"hanchans": schedule(seeds), "protocol_id": PROTOCOL_ID})


def participants(lisjong_revision: str) -> dict[str, Participant]:
    """A・B・O1..O3を ``policy_catalog`` の明示bindingから作る（設定 ``{}``）。"""
    values = {
        role: catalog_participant(IDENTITIES[role], lisjong_revision)
        for role in PARTICIPANT_ROLES
    }
    require_pair(values[CONDITION_A], values[CONDITION_B])
    return values


def expected_seats(entries: dict[str, Participant], item: dict) -> list[dict]:
    """scheduleの1半荘で、第1層rowの ``seats`` が持つべき値。"""
    return [
        SeatAssignment(
            role,
            entries[item["condition"] if role == ROLE_FOCAL else role].policy_identity,
        ).to_value()
        for role in item["roles"]
    ]


# ---------------------------------------------------------------------------
# 局記録のlayout（半荘identityとseat role）
# ---------------------------------------------------------------------------


def _validate_keys(row: dict) -> None:
    seat_roles(row["rotation"])
    if type(row["condition"]) is not str or row["condition"] not in CONDITIONS:
        raise KyokuRecordError("condition must be A or B")


def _validate_seats(row: dict) -> None:
    """record内部: roleがrotationの席順と一致し、各identityが空でない文字列。

    participantとの照合（focalのidentityが条件のA / Bであること等）はplanを
    知る ``verify_schedule`` が行う。
    """
    seats = row["seats"]
    if type(seats) is not list or len(seats) != SEAT_COUNT:
        raise KyokuRecordError("seats must have 4 entries")
    for index, item in enumerate(seats):
        if type(item) is not dict or set(item) != {"policy_identity", "role"}:
            raise KyokuRecordError(f"seats[{index}] fields do not match the schema")
        identity = item["policy_identity"]
        if type(identity) is not str or not identity:
            raise KyokuRecordError(f"seats[{index}].policy_identity is invalid")
    if [item["role"] for item in seats] != list(seat_roles(row["rotation"])):
        raise KyokuRecordError("seat roles do not match the rotation")


LAYOUT = RecordLayout(
    protocol_id=PROTOCOL_ID,
    kyoku_record_schema="arena-heuristic-candidate-engine-focal-mixed-kyoku-record-v1",
    event_fact_schema="arena-heuristic-candidate-engine-focal-mixed-event-fact-v1",
    manifest_schema="arena-heuristic-candidate-engine-focal-mixed-record-manifest-v1",
    key_fields=("rotation", "condition"),
    validate_keys=_validate_keys,
    validate_seats=_validate_seats,
)


@dataclass(frozen=True, slots=True)
class FocalTask:
    """workerへ渡す1半荘分の入力（picklableなdocumentだけを持つ）。"""

    seed: int
    rotation: int
    condition: str
    participants: dict
    wheel_path: str


@dataclass(frozen=True, slots=True)
class FocalHanchanCapture:
    """1半荘の実行から渡す値。``HanchanCapture`` の ``placement`` の代わりに
    ``rotation`` / ``condition`` を持つ（``LAYOUT.key_fields``）。"""

    match: object
    seed: int
    rotation: int
    condition: str
    seats: tuple[SeatAssignment, SeatAssignment, SeatAssignment, SeatAssignment]
    selector_call_counts: tuple[int, int, int, int]
    policy_call_counts: tuple[int, int, int, int]
    round_evidence: tuple[object, ...] | None


def verify_schedule(plan: dict, rows: Sequence[dict]) -> None:
    """第1層rowの並び・半荘identity・seat assignmentがplanのscheduleと一致する。"""
    entries = plan_participants(plan)
    items = plan_schedule(plan)
    keys = [(row["seed"], row["rotation"], row["condition"]) for row in rows]
    expected = [(item["seed"], item["rotation"], item["condition"]) for item in items]
    if keys != expected:
        raise FocalMixedError(
            "hanchans differ from the planned schedule (missing, extra, duplicated "
            "or out of order)"
        )
    for row, item in zip(rows, items, strict=True):
        if row["seats"] != expected_seats(entries, item):
            raise FocalMixedError(
                f"seed {item['seed']} rotation {item['rotation']} condition "
                f"{item['condition']}: seat roles / identities differ from the plan"
            )


# ---------------------------------------------------------------------------
# plan
# ---------------------------------------------------------------------------


def _positive_int(value: object, name: str) -> int:
    if type(value) is not int or value <= 0:
        raise FocalMixedError(f"{name} must be an explicit positive int")
    return value


def _require_limits(value: object) -> dict:
    if type(value) is not dict or set(value) != _LIMIT_FIELDS:
        raise FocalMixedError(
            f"resource_limits must have exactly {sorted(_LIMIT_FIELDS)}"
        )
    _positive_int(value["wall_seconds"], "wall_seconds")
    _positive_int(value["process_tree_rss_bytes"], "process_tree_rss_bytes")
    if type(value["enforcement"]) is not str or not value["enforcement"].strip():
        raise FocalMixedError(
            "resource_limits.enforcement must describe the OS-level enforcement"
        )
    return value


def _run_seeds(run_kind: str, values: object) -> tuple[int, ...]:
    if run_kind == REHEARSAL:
        if isinstance(values, (str, bytes)) or list(values) != list(REHEARSAL_SEEDS):
            raise FocalMixedError(
                f"a rehearsal uses exactly the development seed {REHEARSAL_SEEDS}"
            )
        return REHEARSAL_SEEDS
    try:
        seeds = require_seeds(values)
    except EngineAabbProtocolError as error:
        raise FocalMixedError(str(error)) from error
    if len(seeds) != K:
        raise FocalMixedError(f"the comparison uses exactly {K} seeds")
    return seeds


def require_run_seeds(ledger: object, binding: object, seeds) -> dict:
    """split ``DEVELOPMENT``・owner #522・専用populationのactive allocationで、
    全domainの他allocation（#520の8 + 32 seedsを含む）と1 seedも重ならないこと。"""
    try:
        return require_allocated_seeds(
            ledger,
            binding,
            tuple(seeds),
            owner_issue=OWNER_ISSUE,
            population=POPULATION,
            split=SPLIT,
            protocol=PROTOCOL_ID,
        )
    except EngineAabbLockError as error:
        raise FocalMixedError(str(error)) from error


def _participant_documents(lisjong_revision: object) -> dict[str, dict]:
    if type(lisjong_revision) is not str:
        raise FocalMixedError("environment provenance has no lisjong revision")
    return {
        role: participant.to_document()
        for role, participant in participants(lisjong_revision).items()
    }


def assemble_plan(
    *,
    run_kind: str,
    ordered_seeds: object,
    allocation_binding: object,
    timeout_seconds: int,
    resource_limits: object,
    wheel_path: str,
    destinations: dict,
    environment: dict,
    arena_revision: str,
    ledger: object,
) -> dict[str, object]:
    """入力を検査してplanを作る（live値・ledgerはcallerが渡す）。"""
    if run_kind not in RUN_KINDS:
        raise FocalMixedError(f"run_kind must be one of {RUN_KINDS}")
    seeds = _run_seeds(run_kind, ordered_seeds)
    _positive_int(timeout_seconds, "timeout_seconds")
    limits = dict(_require_limits(resource_limits))
    if type(wheel_path) is not str or not Path(wheel_path).is_absolute():
        raise FocalMixedError("wheel_path must be an absolute path")
    require_hex(arena_revision, 40, "arena_revision")
    if type(environment) is not dict or type(environment.get("provenance")) is not dict:
        raise FocalMixedError("environment must hold provenance")
    documents = _participant_documents(
        environment["provenance"].get("lisjong_revision")
    )
    try:
        _require_environment(
            environment,
            tuple(parse_participant(item, role) for role, item in documents.items()),
        )
    except EngineAabbLockError as error:
        raise FocalMixedError(str(error)) from error
    if environment["provenance"].get("lisjong_arena_revision") != arena_revision:
        raise FocalMixedError("Arena revision differs from the environment")
    if type(destinations) is not dict or set(destinations) != set(DESTINATIONS):
        raise FocalMixedError(f"destinations must be exactly {DESTINATIONS}")
    digest = schedule_digest(seeds)
    if run_kind == ALLOCATED:
        binding = require_run_seeds(ledger, allocation_binding, seeds)
        population = {
            "allocation_binding": dict(binding),
            "k": len(seeds),
            "ordered_seeds": list(seeds),
            "population": POPULATION,
            "schedule_digest": digest,
            "seed_domain": SEED_DOMAIN,
            "split": SPLIT,
        }
    else:
        if allocation_binding is not None:
            raise FocalMixedError("a rehearsal has no allocation")
        population = {
            "k": len(seeds),
            "ordered_seeds": list(seeds),
            "schedule_digest": digest,
            "source": REHEARSAL_SOURCE,
        }
    payload = {
        "artifact_destinations": {
            name: str(destinations[name]) for name in DESTINATIONS
        },
        "criteria": CRITERIA,
        "environment": environment,
        "environment_digest": document_identity(environment),
        "evaluation_kind": EVALUATION_KIND,
        "execution": {
            "backend": BACKEND,
            "timeout_seconds": timeout_seconds,
            "wheel_path": wheel_path,
            "workers": 1,
        },
        "execution_target": {"branch": EXECUTION_BRANCH, "revision": arena_revision},
        "not_a_formal_result": True,
        "owner_issue": OWNER_ISSUE,
        "participants": documents,
        "population": population,
        "protocol_digest": PROTOCOL_DIGEST,
        "protocol_id": PROTOCOL_ID,
        "purpose": PURPOSES[run_kind],
        "resource_limits": limits,
        "rules_digest": RULES_DIGEST,
        "run_kind": run_kind,
        "schema": PLAN_SCHEMA,
    }
    return parse_plan({**payload, "plan_digest": document_identity(payload)})


def parse_plan(value: object) -> dict[str, object]:
    """保存済みplanを厳密に検査する（live値・ledgerは見ない）。"""
    try:
        return _parse_plan(value)
    except EngineAabbProtocolError:
        raise
    except (ArtifactValidationError, TypeError, KeyError, ValueError) as error:
        raise FocalMixedError(
            f"invalid #522 plan: {type(error).__name__}: {error}"
        ) from error


def _parse_plan(value: object) -> dict[str, object]:
    if type(value) is not dict or value.get("schema") != PLAN_SCHEMA:
        raise FocalMixedError("unsupported #522 plan schema")
    if set(value) != _PLAN_FIELDS:
        raise FocalMixedError("#522 plan fields do not match")
    run_kind = value["run_kind"]
    if run_kind not in RUN_KINDS:
        raise FocalMixedError("#522 plan run_kind is invalid")
    if (
        value["evaluation_kind"] != EVALUATION_KIND
        or value["not_a_formal_result"] is not True
        or value["owner_issue"] != OWNER_ISSUE
        or value["purpose"] != PURPOSES[run_kind]
        or value["protocol_id"] != PROTOCOL_ID
        or value["protocol_digest"] != PROTOCOL_DIGEST
        or value["rules_digest"] != RULES_DIGEST
        or value["criteria"] != CRITERIA
    ):
        raise FocalMixedError("#522 plan identity or criteria drifted")
    entries = value["participants"]
    if type(entries) is not dict or set(entries) != set(PARTICIPANT_ROLES):
        raise FocalMixedError(f"#522 participants must be {PARTICIPANT_ROLES}")
    parsed = {
        role: parse_participant(entries[role], f"plan.participants.{role}")
        for role in PARTICIPANT_ROLES
    }
    if parsed != participants(parsed[CONDITION_A].lisjong_revision):
        raise FocalMixedError("#522 plan must use the fixed participants")
    population = value["population"]
    expected = _ALLOCATED_FIELDS if run_kind == ALLOCATED else _REHEARSAL_FIELDS
    if type(population) is not dict or set(population) != expected:
        raise FocalMixedError("#522 population fields do not match")
    seeds = _run_seeds(run_kind, population["ordered_seeds"])
    if population["k"] != len(seeds) or population[
        "schedule_digest"
    ] != schedule_digest(seeds):
        raise FocalMixedError("#522 population schedule drifted")
    if run_kind == ALLOCATED:
        if (
            population["population"] != POPULATION
            or population["split"] != SPLIT
            or population["seed_domain"] != SEED_DOMAIN
        ):
            raise FocalMixedError("#522 population identity drifted")
        try:
            seed_registry.validate_binding_shape(
                population["allocation_binding"], seeds=seeds
            )
        except seed_registry.SeedRegistryError as error:
            raise FocalMixedError(
                f"#522 allocation binding invalid: {error}"
            ) from error
    elif population["source"] != REHEARSAL_SOURCE:
        raise FocalMixedError("#522 rehearsal source drifted")
    execution = value["execution"]
    if type(execution) is not dict or set(execution) != _EXECUTION_FIELDS:
        raise FocalMixedError("#522 execution fields do not match")
    if execution["backend"] != BACKEND or execution["workers"] != 1:
        raise FocalMixedError("#522 runs one rust worker")
    _positive_int(execution["timeout_seconds"], "timeout_seconds")
    if type(execution["wheel_path"]) is not str or not execution["wheel_path"]:
        raise FocalMixedError("#522 wheel_path is invalid")
    _require_limits(value["resource_limits"])
    try:
        _require_environment(value["environment"], tuple(parsed.values()))
    except EngineAabbLockError as error:
        raise FocalMixedError(str(error)) from error
    if value["environment_digest"] != document_identity(value["environment"]):
        raise FocalMixedError("#522 environment digest mismatch")
    target = value["execution_target"]
    if type(target) is not dict or set(target) != {"branch", "revision"}:
        raise FocalMixedError("#522 execution_target is invalid")
    if target["branch"] != EXECUTION_BRANCH:
        raise FocalMixedError("#522 runs from main")
    require_hex(target["revision"], 40, "execution_target.revision")
    if target["revision"] != value["environment"]["provenance"].get(
        "lisjong_arena_revision"
    ):
        raise FocalMixedError("plan revision differs from the environment")
    destinations = value["artifact_destinations"]
    if type(destinations) is not dict or set(destinations) != set(DESTINATIONS):
        raise FocalMixedError("#522 destinations do not match")
    if any(type(item) is not str or not item for item in destinations.values()):
        raise FocalMixedError("#522 destinations must be strings")
    if len({Path(item).resolve(strict=False) for item in destinations.values()}) != 2:
        raise FocalMixedError("#522 destinations must differ")
    payload = {key: item for key, item in value.items() if key != "plan_digest"}
    if value["plan_digest"] != document_identity(payload):
        raise FocalMixedError("#522 plan digest mismatch")
    return value


def require_frozen_environment(environment: dict) -> None:
    """live環境がIssue #522の凍結値（lisjong revision・wheel SHA-256）であること。"""
    if (
        environment["provenance"].get("lisjong_revision") != FROZEN_LISJONG_REVISION
        or environment["native"].get("wheel_sha256") != FROZEN_WHEEL_SHA256
    ):
        raise FocalMixedError(
            "installed lisjong / native wheel is not the frozen #522 input "
            f"({FROZEN_LISJONG_REVISION[:7]}, wheel {FROZEN_WHEEL_SHA256[:12]}...)"
        )


def build_plan(
    *,
    run_kind: str,
    ordered_seeds: object,
    allocation_identity: str | None,
    timeout_seconds: int,
    resource_limits: object,
    wheel_path: str,
    destinations: dict,
    ledger_path: str | Path = seed_registry.DEFAULT_LEDGER_PATH,
) -> dict[str, object]:
    """live値（ledger、clean merged HEAD、実行環境）を集めてplanを作る（保存はしない）。"""
    ledger = binding = None
    if run_kind == ALLOCATED:
        try:
            ledger = seed_registry.load_ledger(ledger_path)
            binding = seed_registry.allocation_binding(ledger, allocation_identity)
        except seed_registry.SeedRegistryError as error:
            raise FocalMixedError(f"seed registry cannot be used: {error}") from error
    elif allocation_identity is not None:
        raise FocalMixedError("a rehearsal has no allocation")
    try:
        require_new_artifact_destinations(destinations, required_names=DESTINATIONS)
    except ExecutionSafetyError as error:
        raise FocalMixedError(str(error)) from error
    try:
        head = _live_head()
        environment = _live_environment(wheel_path)
    except EngineAabbLockError as error:
        raise FocalMixedError(str(error)) from error
    require_frozen_environment(environment)
    return assemble_plan(
        run_kind=run_kind,
        ordered_seeds=ordered_seeds,
        allocation_binding=binding,
        timeout_seconds=timeout_seconds,
        resource_limits=resource_limits,
        wheel_path=wheel_path,
        destinations=destinations,
        environment=environment,
        arena_revision=head,
        ledger=ledger,
    )


def save_plan(document: dict, path: str | Path) -> Path:
    parse_plan(document)
    destination = Path(path)
    if destination.exists():
        raise FileExistsError(f"refusing to overwrite #522 plan: {destination}")
    write_new_artifact_file(destination, canonical_json_text(document))
    return destination


def load_plan(path: str | Path) -> dict[str, object]:
    try:
        document = read_json_document(Path(path))
    except (ArtifactValidationError, OSError, ValueError) as error:
        raise FocalMixedError(f"cannot read #522 plan: {error}") from error
    return parse_plan(document)


def require_live_target(
    plan: dict,
    *,
    ledger: object,
    environment: dict | None = None,
    head: str | None = None,
) -> None:
    """実行の直前・直後に、live HEAD・実行環境・（allocatedなら）allocationが
    planと一致することを要求する。``environment`` / ``head`` はtestのseam。"""
    parsed = parse_plan(plan)
    try:
        live_head = _live_head() if head is None else head
        live_environment = (
            _live_environment(parsed["execution"]["wheel_path"])
            if environment is None
            else environment
        )
    except EngineAabbLockError as error:
        raise FocalMixedError(str(error)) from error
    if live_head != parsed["execution_target"]["revision"]:
        raise FocalMixedError("live Arena HEAD differs from the plan")
    if live_environment != parsed["environment"]:
        raise FocalMixedError("live environment differs from the plan")
    if parsed["run_kind"] == ALLOCATED:
        population = parsed["population"]
        require_run_seeds(
            ledger, population["allocation_binding"], population["ordered_seeds"]
        )


def plan_participants(plan: dict) -> dict[str, Participant]:
    entries = plan["participants"]
    return {
        role: parse_participant(entries[role], f"plan.participants.{role}")
        for role in PARTICIPANT_ROLES
    }


def plan_schedule(plan: dict) -> list[dict]:
    return schedule(plan["population"]["ordered_seeds"])


def plan_tasks(plan: dict) -> list[FocalTask]:
    documents = plan["participants"]
    return [
        FocalTask(
            seed=item["seed"],
            rotation=item["rotation"],
            condition=item["condition"],
            participants=documents,
            wheel_path=plan["execution"]["wheel_path"],
        )
        for item in plan_schedule(plan)
    ]


# ---------------------------------------------------------------------------
# 実行枠（上限・停止・公開）
# ---------------------------------------------------------------------------


def utc_now() -> str:
    return datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def write_stop(diagnostics: Path, plan: dict, error: BaseException, done: int):
    diagnostics.mkdir(parents=True, exist_ok=True)
    stamp = datetime.datetime.now(datetime.UTC).strftime("%Y%m%dT%H%M%S%fZ")
    document = {
        "completed_hanchans": done,
        "error": f"{type(error).__name__}: {error}",
        "not_a_result": True,
        "plan_digest": plan["plan_digest"],
        "run_kind": plan["run_kind"],
        "schema": STOP_SCHEMA,
        "status": STOP_STATUS,
    }
    write_new_artifact_file(
        diagnostics / f"stop-{stamp}.json", canonical_json_text(document)
    )


def execute_plan(
    plan: dict,
    *,
    ledger_path: str | Path | None,
    play: Callable,
    live_gate: Callable[..., None],
    publish: Callable[[Path, list[ExecutedHanchan], dict], dict],
    on_hanchan: Callable[[int, int], None] | None,
    clock: Callable[[], float],
    realtime: Callable[[], float],
) -> dict[str, object]:
    """planのscheduleを1 workerで実行し、``publish(staging, executed, timings)`` が
    書いた保存物を ``<output>`` へ公開する。失敗はSTOP-INCOMPLETEで何も公開しない。

    全体のwall上限はmonotonicとrealtimeの両方で確かめ、長い方で判定する
    （#517でmonotonicがrealtimeより約401 s短かった）。process tree全体の強制は
    OS側（plan ``resource_limits.enforcement``）で行う。
    """
    output = Path(plan["artifact_destinations"]["output"])
    diagnostics = Path(plan["artifact_destinations"]["diagnostics"])
    if output.exists():
        raise FocalMixedError(
            "output already exists; #522 outputs are write-once and a completed run "
            "is never re-run"
        )
    limits = plan["resource_limits"]
    rows: list[dict] = []
    started_at = utc_now()
    started = clock()
    started_real = realtime()

    def elapsed() -> float:
        return max(clock() - started, realtime() - started_real)

    def measure(item: ExecutedHanchan) -> None:
        parent_kib = current_peak_rss_kib()
        rows.append(
            {
                "condition": item.capture.condition,
                "kyoku_count": len(item.capture.match.history),
                "parent_peak_rss_kib": parent_kib,
                "rotation": item.capture.rotation,
                "seed": item.capture.seed,
                "wall_seconds": item.wall_seconds,
                "worker_peak_rss_kib": item.peak_rss_kib,
            }
        )
        if elapsed() > limits["wall_seconds"]:
            raise RunStopped(
                f"run exceeded the wall limit of {limits['wall_seconds']} s "
                f"after {len(rows)} hanchans"
            )
        known = [
            value for value in (parent_kib, item.peak_rss_kib) if value is not None
        ]
        if sum(known) * 1024 > limits["process_tree_rss_bytes"]:
            raise RunStopped(
                "parent + worker peak RSS exceeded the process tree limit of "
                f"{limits['process_tree_rss_bytes']} bytes"
            )

    def ledger():
        if plan["run_kind"] != ALLOCATED:
            return None
        return seed_registry.load_ledger(ledger_path)

    try:
        live_gate(plan, ledger=ledger())
        executed = run_tasks(
            plan_tasks(plan),
            key_fields=LAYOUT.key_fields,
            timeout_seconds=plan["execution"]["timeout_seconds"],
            expected_environment=plan["environment"],
            play=play,
            on_hanchan=on_hanchan,
            on_executed=measure,
        )
        schedule_seconds = clock() - started
        live_gate(plan, ledger=ledger())
        if any(item.policy_analysis is None for item in executed):
            raise RunStopped("a hanchan returned no Policy observation")
        with staged_artifact_directory(output) as staging:
            timings = {
                "hanchans": rows,
                "not_rederived": True,
                "plan_digest": plan["plan_digest"],
                "schedule_wall_seconds": schedule_seconds,
                "started_at_utc": started_at,
            }
            summary = publish(staging, executed, timings)
            timings["finished_at_utc"] = utc_now()
            timings["monotonic_elapsed_seconds"] = clock() - started
            timings["realtime_elapsed_seconds"] = realtime() - started_real
            write_new_artifact_file(
                staging / TIMINGS_FILENAME, canonical_json_text(timings)
            )
            if elapsed() > limits["wall_seconds"]:
                raise RunStopped("run exceeded the wall limit while saving")
    except BaseException as error:
        write_stop(diagnostics, plan, error, len(rows))
        if not isinstance(error, Exception) or isinstance(
            error, (RunStopped, EngineAabbProtocolError)
        ):
            raise
        raise RunStopped(f"{STOP_STATUS}: {type(error).__name__}: {error}") from error
    return summary


def default_clocks() -> tuple[Callable[[], float], Callable[[], float]]:
    return time.monotonic, time.time


# ---------------------------------------------------------------------------
# timings（verify / report共通）
# ---------------------------------------------------------------------------

_TIMING_ROW_FIELDS = frozenset(
    {
        "condition",
        "kyoku_count",
        "parent_peak_rss_kib",
        "rotation",
        "seed",
        "wall_seconds",
        "worker_peak_rss_kib",
    }
)
_TIMING_TOTAL_FIELDS = (
    "monotonic_elapsed_seconds",
    "realtime_elapsed_seconds",
    "schedule_wall_seconds",
    "write_seconds",
    "summary_seconds",
)


def require_timings(
    plan: dict, timings: object, kyoku_counts: Sequence[int], *, complete: bool = True
) -> dict:
    """timingsの形・値を検査し、半荘の並びと局数を保存済みの客観値と照合する。

    実行時と同じ上限もここで再確認する（半荘ごとの「親 + workerのpeak RSS」の既知値の和、
    schedule wall、``complete`` ならmax(monotonic経過, realtime経過)）。RSSの欠測（null）は
    0とみなさない。``complete=False`` は書込中の検査で、summaryが使う値だけを見る。
    """
    limits = plan["resource_limits"]
    if type(timings) is not dict or timings.get("plan_digest") != plan["plan_digest"]:
        raise FocalMixedError("timings do not belong to the plan")
    rows = timings.get("hanchans")
    expected = [
        (item["seed"], item["rotation"], item["condition"])
        for item in plan_schedule(plan)
    ]
    if (
        type(rows) is not list
        or any(type(row) is not dict or set(row) != _TIMING_ROW_FIELDS for row in rows)
        or [(row["seed"], row["rotation"], row["condition"]) for row in rows]
        != expected
    ):
        raise FocalMixedError("timings do not cover the planned schedule")
    if len(kyoku_counts) != len(rows):
        raise FocalMixedError("stored hanchans do not cover the schedule")
    for row, kyoku_count in zip(rows, kyoku_counts, strict=True):
        context = (
            f"timings seed {row['seed']} rotation {row['rotation']} "
            f"condition {row['condition']}"
        )
        try:
            seconds(row["wall_seconds"], f"{context} wall_seconds")
            known = [
                value
                for value in (
                    rss(row["worker_peak_rss_kib"], f"{context} worker_peak_rss_kib"),
                    rss(row["parent_peak_rss_kib"], f"{context} parent_peak_rss_kib"),
                )
                if value is not None
            ]
        except EngineAabbProtocolError as error:
            raise FocalMixedError(str(error)) from error
        kyoku = row["kyoku_count"]
        if type(kyoku) is not int or kyoku <= 0:
            raise FocalMixedError(f"{context} kyoku_count must be a positive int")
        if kyoku != kyoku_count:
            raise FocalMixedError(f"{context} kyoku_count differs from the record")
        if sum(known) * 1024 > limits["process_tree_rss_bytes"]:
            raise FocalMixedError(
                f"{context}: parent + worker peak RSS exceeds the plan limit"
            )
    names = _TIMING_TOTAL_FIELDS if complete else ("schedule_wall_seconds",)
    for name in names:
        value = timings.get(name)
        if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
            raise FocalMixedError(
                f"timings {name} must be a finite non-negative number"
            )
    elapsed = [timings["schedule_wall_seconds"]]
    if complete:
        elapsed += [
            timings["monotonic_elapsed_seconds"],
            timings["realtime_elapsed_seconds"],
        ]
    if max(elapsed) > limits["wall_seconds"]:
        raise FocalMixedError("timings exceed the run wall limit of the plan")
    return timings


__all__ = [
    "ALLOCATED",
    "COMPARE_HANCHANS",
    "CONDITIONS",
    "CONDITION_A",
    "CONDITION_B",
    "CRITERIA",
    "DIVISOR",
    "FROZEN_LISJONG_REVISION",
    "FROZEN_WHEEL_SHA256",
    "HANCHANS_PER_SEED",
    "IDENTITIES",
    "K",
    "LAYOUT",
    "OPPONENT_ROLES",
    "OWNER_ISSUE",
    "PLAN_SCHEMA",
    "POPULATION",
    "PROTOCOL_DIGEST",
    "PROTOCOL_ID",
    "REHEARSAL",
    "REHEARSAL_SEEDS",
    "ROLE_FOCAL",
    "ROTATIONS",
    "STOP_STATUS",
    "FocalHanchanCapture",
    "FocalMixedError",
    "FocalTask",
    "assemble_plan",
    "build_plan",
    "execute_plan",
    "load_plan",
    "parse_plan",
    "participants",
    "protocol_document",
    "require_live_target",
    "require_run_seeds",
    "save_plan",
    "schedule",
    "seat_roles",
    "verify_schedule",
]
