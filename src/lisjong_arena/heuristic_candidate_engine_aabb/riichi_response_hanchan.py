"""リーチ下1向聴の押し引き候補（RR）の半荘開発比較（#591）。

lisjong #379の東1局対比較でSUPPORTED-DEVELOPMENTになった固定候補を、engine AABB
（1 seed × 6配置、主指標はengine ``final_points``）で現Heuristic Championと比べる。
**開発比較であり、正式評価・Champion変更の根拠ではない。** 正式lockのFORMAL-EVAL検査を
無効にして流用せず、このmodule専用のplanを使う。

```text
participants  A = placement-aware-speed-call-riichi-response-v1.push-dora-1.push-wall-24.fold-dora-1
              B = placement-aware-speed-call（現Heuristic Champion）
measurement   K = 500 block（3,000半荘）。seedはSeed Registryのallocation
              （split DEVELOPMENT-CONFIRM）からbindする
rehearsal     既使用の開発用seed 472005, 472006（registry外）の12半荘。時間と記録経路の
              確認だけに使い、区間・判定は出さない
stage         ordered seedsを ``stage_blocks`` 個ずつに分けた実行単位。stageごとに
              write-onceで保存する。各半荘は(seed, 配置)だけで決まるので、複数stageを
              別processで同時に実行しても結果は直列実行と同じになる
出力          <root>/stage-NNN/raw/        局単位record（第1層・第2層）
              <root>/stage-NNN/timings.json 半荘ごとのwall時間・worker peak RSS
              <root>/stage-NNN/stage.json   完了marker（rawのmanifest SHA-256をbind）
              <root>/summary.json           全stageがそろってから1回だけ作る集計
```

判定（measurementだけ、結果を見る前に固定）:

```text
SUPPORTED-DEVELOPMENT       平均D > 0 かつ 95%区間（mean ± 1.96 × SD(K-1) / sqrt(K)）の下限 > 0
INCONCLUSIVE-DEVELOPMENT    平均D > 0 かつ区間が0を含む
NOT-SUPPORTED-DEVELOPMENT   平均D <= 0
```

実行環境はAI作業環境で、lisjong main CIのwheelを取得できない。そのため、pinと同じ
lisjong revisionのsourceからbuildしたwheelをSHA-256でplanへbindし、installedな
``_lisjong_native`` がそのwheelとbyte一致することを親と全workerで確かめる
（``local_environment_document``）。凍結したCI wheelとの一致は主張しない。
"""

import os
import platform
from collections.abc import Callable
from pathlib import Path

from lisjong_arena import seed_registry
from lisjong_arena._artifact_io import (
    ArtifactValidationError,
    canonical_json_text,
    read_json_document,
    write_new_artifact_file,
)
from lisjong_arena._execution_safety import (
    ExecutionSafetyError,
    require_clean_arena_head,
)
from lisjong_arena.overall_champion_aabb.lock import document_identity
from lisjong_arena.single_round_artifact import (
    collect_execution_provenance,
    execution_provenance_to_dict,
)

from .development import DEVELOPMENT_SEEDS, catalog_participant
from .executor import (
    HanchanTask,
    PlayedHanchan,
    current_peak_rss_kib,
    play_task,
    run_schedule,
)
from .kyoku_record import PROTOCOL_ID, ROLE_CANDIDATE, ROLE_INCUMBENT, verify_schedule
from .lock import EngineAabbLockError, require_allocated_seeds
from .protocol import (
    POPULATION_PREFIX,
    PROTOCOL_DIGEST,
    RULES_DIGEST,
    EngineAabbProtocolError,
    Participant,
    expected_schedule,
    parse_participant,
    placement_schedule,
    require_hex,
    require_pair,
    require_seeds,
)
from .record_store import (
    EVENT_FACTS_VALID,
    EventFactStatus,
    RecordBindings,
    VerifiedKyokuRecords,
    read_kyoku_records,
    require_bindings,
    write_kyoku_records,
)
from .secondary_metrics import summarize_secondary_metrics
from .statistics import block_totals, summarize_blocks

PLAN_SCHEMA = "arena-riichi-response-hanchan-development-plan-v1"
STAGE_SCHEMA = "arena-riichi-response-hanchan-development-stage-v1"
SUMMARY_SCHEMA = "arena-riichi-response-hanchan-development-summary-v1"
EVALUATION_KIND = "DEVELOPMENT"
OWNER_ISSUE = "lisbun/lisjong-arena#591"
PURPOSE = (
    "development comparison of the fixed riichi-response candidate against the "
    "current Heuristic Champion in full hanchans; not a formal result and not "
    "promotion evidence"
)
CANDIDATE_IDENTITY = (
    "placement-aware-speed-call-riichi-response-v1.push-dora-1.push-wall-24.fold-dora-1"
)
INCUMBENT_IDENTITY = "placement-aware-speed-call"
MODE_MEASUREMENT = "measurement"
MODE_REHEARSAL = "rehearsal"
MEASUREMENT_K = 500
MEASUREMENT_STAGE_BLOCKS = 10
REHEARSAL_STAGE_BLOCKS = 1
SPLIT = "DEVELOPMENT-CONFIRM"
POPULATION = f"{POPULATION_PREFIX}riichi-response-591"
SUPPORTED = "SUPPORTED-DEVELOPMENT"
INCONCLUSIVE = "INCONCLUSIVE-DEVELOPMENT"
NOT_SUPPORTED = "NOT-SUPPORTED-DEVELOPMENT"
DECISION_RULE = {
    "interval": "mean D +/- 1.96 * SD(K-1) / sqrt(K); the unit is the seed block",
    INCONCLUSIVE: "mean D > 0 and the interval contains 0",
    NOT_SUPPORTED: "mean D <= 0",
    SUPPORTED: "mean D > 0 and the interval lower bound > 0",
}
REHEARSAL_STATUS = "REHEARSAL COMPLETE; NOT A STRENGTH RESULT"
WHEEL_IDENTITY = "built from the pinned lisjong source; not the frozen CI wheel"
PLAN_FILENAME = "plan.json"
RAW_DIRNAME = "raw"
STAGE_FILENAME = "stage.json"
SUMMARY_FILENAME = "summary.json"
TIMINGS_FILENAME = "timings.json"

_PLAN_FIELDS = frozenset(
    {
        "allocation_binding",
        "arena_revision",
        "decision_rule",
        "environment",
        "environment_digest",
        "evaluation_kind",
        "execution",
        "mode",
        "ordered_seeds",
        "owner_issue",
        "participants",
        "plan_digest",
        "protocol_digest",
        "purpose",
        "rules_digest",
        "schedule_digest",
        "schema",
    }
)
_EXECUTION_FIELDS = frozenset(
    {"backend", "stage_blocks", "timeout_seconds", "wheel_path"}
)
_STAGE_FIELDS = frozenset(
    {"plan_digest", "raw_manifest_sha256", "schema", "seeds", "stage"}
)


class RiichiResponseHanchanError(EngineAabbProtocolError):
    """#591の開発比較のplan・実行・集計が要求を満たさない。"""


# ---------------------------------------------------------------------------
# environment
# ---------------------------------------------------------------------------


def local_environment_document(wheel_path: str) -> dict[str, object]:
    """このprocessの実行環境。planに記録し、各workerの値と完全一致を要求する。

    ``executor.environment_document`` との違いは、wheelを凍結したCI wheelのSHA-256と
    照合しない点だけである。wheelのSHA-256はplanが束縛し、installedなnativeとの
    byte一致、native ``SOURCE_REVISION`` とlisjong pinの一致はここでも要求する。
    """
    from lisjong_arena.shanten_backend_verification.backend import (
        ShantenBackendVerificationError,
        compare_installed_native,
        file_sha256,
        require_shanten_backend,
    )

    backend = require_shanten_backend("rust")
    wheel = Path(wheel_path)
    if not wheel.is_file():
        raise ShantenBackendVerificationError(f"wheel is missing: {wheel}")
    compare_installed_native(wheel)
    native = backend["native"]
    if native["source_revision"] != backend["lisjong_revision"]:
        raise ShantenBackendVerificationError(
            "native SOURCE_REVISION differs from the installed lisjong revision"
        )
    return {
        "native": {
            "api_version": native["api_version"],
            "source_revision": native["source_revision"],
            "wheel_bytes": wheel.stat().st_size,
            "wheel_file": wheel.name,
            "wheel_identity": WHEEL_IDENTITY,
            "wheel_sha256": file_sha256(wheel),
        },
        "provenance": execution_provenance_to_dict(collect_execution_provenance()),
        "python": {
            "implementation": platform.python_implementation(),
            "version": platform.python_version(),
        },
        "shanten_backend": {
            "backend": backend["backend"],
            "lisjong_revision": backend["lisjong_revision"],
        },
    }


def play_hanchan_source_wheel(task: HanchanTask) -> PlayedHanchan:
    """worker内で1半荘を実行する（実engine・実Policy、source build wheelの環境照合）。"""
    environment = local_environment_document(task.wheel_path)
    capture = play_task(task)
    return PlayedHanchan(capture, environment, os.getpid(), current_peak_rss_kib())


# ---------------------------------------------------------------------------
# plan
# ---------------------------------------------------------------------------


def _mode_seeds(mode: object, values: object) -> tuple[int, ...]:
    if mode == MODE_REHEARSAL:
        if list(values) != list(DEVELOPMENT_SEEDS):
            raise RiichiResponseHanchanError(
                "the rehearsal uses exactly the known development seeds"
            )
        return tuple(DEVELOPMENT_SEEDS)
    if mode != MODE_MEASUREMENT:
        raise RiichiResponseHanchanError("mode must be rehearsal or measurement")
    seeds = require_seeds(values)
    if len(seeds) != MEASUREMENT_K:
        raise RiichiResponseHanchanError(
            f"the measurement uses exactly {MEASUREMENT_K} seed blocks"
        )
    return seeds


def _stage_blocks(mode: str) -> int:
    return (
        REHEARSAL_STAGE_BLOCKS if mode == MODE_REHEARSAL else MEASUREMENT_STAGE_BLOCKS
    )


def _schedule(seeds: tuple[int, ...]) -> dict:
    return {"hanchans": placement_schedule(seeds), "protocol_id": PROTOCOL_ID}


def _participants_for(lisjong_revision: str) -> tuple[Participant, Participant]:
    candidate = catalog_participant(CANDIDATE_IDENTITY, lisjong_revision)
    incumbent = catalog_participant(INCUMBENT_IDENTITY, lisjong_revision)
    require_pair(candidate, incumbent)
    return candidate, incumbent


def require_measurement_seeds(ledger: object, binding: object, seeds) -> dict:
    """#591のactive allocation（split DEVELOPMENT-CONFIRM）で、他と重ならないこと。"""
    try:
        return require_allocated_seeds(
            ledger,
            binding,
            tuple(seeds),
            owner_issue=OWNER_ISSUE,
            population=POPULATION,
            split=SPLIT,
        )
    except EngineAabbLockError as error:
        raise RiichiResponseHanchanError(str(error)) from error


def assemble_plan(
    *,
    mode: str,
    ordered_seeds: object,
    allocation_binding: object,
    timeout_seconds: int,
    wheel_path: str,
    environment: dict,
    arena_revision: str,
    ledger: object = None,
) -> dict[str, object]:
    """値を受け取ってplanを組み立てる（live値の収集は ``build_plan``）。"""
    require_hex(arena_revision, 40, "arena_revision")
    seeds = _mode_seeds(mode, ordered_seeds)
    if mode == MODE_MEASUREMENT:
        require_measurement_seeds(ledger, allocation_binding, seeds)
    elif allocation_binding is not None:
        raise RiichiResponseHanchanError("the rehearsal has no seed allocation")
    candidate, incumbent = _participants_for(
        environment["provenance"]["lisjong_revision"]
    )
    payload = {
        "allocation_binding": allocation_binding,
        "arena_revision": arena_revision,
        "decision_rule": DECISION_RULE,
        "environment": environment,
        "environment_digest": document_identity(environment),
        "evaluation_kind": EVALUATION_KIND,
        "execution": {
            "backend": "rust",
            "stage_blocks": _stage_blocks(mode),
            "timeout_seconds": timeout_seconds,
            "wheel_path": wheel_path,
        },
        "mode": mode,
        "ordered_seeds": list(seeds),
        "owner_issue": OWNER_ISSUE,
        "participants": {
            ROLE_CANDIDATE: candidate.to_document(),
            ROLE_INCUMBENT: incumbent.to_document(),
        },
        "protocol_digest": PROTOCOL_DIGEST,
        "purpose": PURPOSE,
        "rules_digest": RULES_DIGEST,
        "schedule_digest": document_identity(_schedule(seeds)),
        "schema": PLAN_SCHEMA,
    }
    return parse_plan({**payload, "plan_digest": document_identity(payload)})


def parse_plan(value: object) -> dict[str, object]:
    if type(value) is not dict or value.get("schema") != PLAN_SCHEMA:
        raise RiichiResponseHanchanError("unsupported #591 plan schema")
    if set(value) != _PLAN_FIELDS:
        raise RiichiResponseHanchanError("#591 plan fields do not match the schema")
    mode = value["mode"]
    seeds = _mode_seeds(mode, value["ordered_seeds"])
    execution = value["execution"]
    if type(execution) is not dict or set(execution) != _EXECUTION_FIELDS:
        raise RiichiResponseHanchanError("#591 plan execution fields are invalid")
    timeout = execution["timeout_seconds"]
    if (
        execution["backend"] != "rust"
        or execution["stage_blocks"] != _stage_blocks(mode)
        or type(timeout) is not int
        or timeout <= 0
        or type(execution["wheel_path"]) is not str
        or not os.path.isabs(execution["wheel_path"])
    ):
        raise RiichiResponseHanchanError("#591 plan execution values are invalid")
    if (
        value["evaluation_kind"] != EVALUATION_KIND
        or value["purpose"] != PURPOSE
        or value["owner_issue"] != OWNER_ISSUE
        or value["decision_rule"] != DECISION_RULE
        or value["protocol_digest"] != PROTOCOL_DIGEST
        or value["rules_digest"] != RULES_DIGEST
        or value["schedule_digest"] != document_identity(_schedule(seeds))
    ):
        raise RiichiResponseHanchanError("#591 plan identity drifted")
    require_hex(value["arena_revision"], 40, "arena_revision")
    if (mode == MODE_REHEARSAL) != (value["allocation_binding"] is None):
        raise RiichiResponseHanchanError("only the measurement binds a seed allocation")
    participants = value["participants"]
    if type(participants) is not dict or set(participants) != {
        ROLE_CANDIDATE,
        ROLE_INCUMBENT,
    }:
        raise RiichiResponseHanchanError("#591 plan participants must be A and B")
    candidate = parse_participant(participants[ROLE_CANDIDATE], "plan.participants.A")
    incumbent = parse_participant(participants[ROLE_INCUMBENT], "plan.participants.B")
    require_pair(candidate, incumbent)
    environment = value["environment"]
    if (
        (candidate.policy_identity, incumbent.policy_identity)
        != (CANDIDATE_IDENTITY, INCUMBENT_IDENTITY)
        or type(environment) is not dict
        or candidate.lisjong_revision
        != environment.get("provenance", {}).get("lisjong_revision")
    ):
        raise RiichiResponseHanchanError(
            "#591 plan must bind the fixed pair at the environment's lisjong revision"
        )
    if value["environment_digest"] != document_identity(environment):
        raise RiichiResponseHanchanError("#591 plan environment digest mismatch")
    payload = {key: item for key, item in value.items() if key != "plan_digest"}
    if value["plan_digest"] != document_identity(payload):
        raise RiichiResponseHanchanError("#591 plan digest mismatch")
    return value


def _live_head() -> str:
    try:
        return require_clean_arena_head()
    except ExecutionSafetyError as error:
        raise RiichiResponseHanchanError(str(error)) from error


def build_plan(
    *,
    mode: str,
    timeout_seconds: int,
    wheel_path: str,
    ordered_seeds: object = None,
    allocation_identity: str | None = None,
    ledger_path: str | Path = seed_registry.DEFAULT_LEDGER_PATH,
) -> dict[str, object]:
    """live値（ledger、clean HEAD、実行環境）を集めてplanを作る（保存はしない）。"""
    ledger = binding = None
    if mode == MODE_MEASUREMENT:
        try:
            ledger = seed_registry.load_ledger(ledger_path)
            binding = seed_registry.allocation_binding(ledger, allocation_identity)
        except seed_registry.SeedRegistryError as error:
            raise RiichiResponseHanchanError(
                f"seed registry cannot be used: {error}"
            ) from error
    else:
        ordered_seeds = DEVELOPMENT_SEEDS
    return assemble_plan(
        mode=mode,
        ordered_seeds=ordered_seeds,
        allocation_binding=binding,
        timeout_seconds=timeout_seconds,
        wheel_path=wheel_path,
        environment=local_environment_document(wheel_path),
        arena_revision=_live_head(),
        ledger=ledger,
    )


def save_plan(document: dict, path: str | Path) -> Path:
    destination = Path(path)
    write_new_artifact_file(destination, canonical_json_text(parse_plan(document)))
    return destination


def load_plan(path: str | Path) -> dict[str, object]:
    try:
        return parse_plan(read_json_document(Path(path)))
    except (ArtifactValidationError, OSError, ValueError) as error:
        raise RiichiResponseHanchanError(
            f"cannot read the #591 plan: {error}"
        ) from error


def require_live_target(
    plan: dict,
    *,
    ledger: object = None,
    environment: dict | None = None,
    head: str | None = None,
) -> None:
    """実行の直前に、live HEAD・実行環境・allocationがplanと一致することを要求する。

    ``environment`` / ``head`` はtestのseamで、実行では渡さない。
    """
    parsed = parse_plan(plan)
    if (_live_head() if head is None else head) != parsed["arena_revision"]:
        raise RiichiResponseHanchanError("live Arena HEAD differs from the plan")
    live = (
        local_environment_document(parsed["execution"]["wheel_path"])
        if environment is None
        else environment
    )
    if live != parsed["environment"]:
        raise RiichiResponseHanchanError("live environment differs from the plan")
    if parsed["mode"] == MODE_MEASUREMENT:
        require_measurement_seeds(
            ledger, parsed["allocation_binding"], parsed["ordered_seeds"]
        )


# ---------------------------------------------------------------------------
# stages
# ---------------------------------------------------------------------------


def stage_count(plan: dict) -> int:
    blocks = plan["execution"]["stage_blocks"]
    return -(-len(plan["ordered_seeds"]) // blocks)


def stage_seeds(plan: dict, stage: int) -> tuple[int, ...]:
    if type(stage) is not int or not 0 <= stage < stage_count(plan):
        raise RiichiResponseHanchanError(f"stage {stage!r} is outside the plan")
    blocks = plan["execution"]["stage_blocks"]
    return tuple(plan["ordered_seeds"][stage * blocks : (stage + 1) * blocks])


def stage_directory(output_root: str | Path, stage: int) -> Path:
    return Path(output_root) / f"stage-{stage:03d}"


def _participants(plan: dict) -> tuple[Participant, Participant]:
    return (
        parse_participant(plan["participants"][ROLE_CANDIDATE], "A"),
        parse_participant(plan["participants"][ROLE_INCUMBENT], "B"),
    )


def _bindings(plan: dict) -> RecordBindings:
    return RecordBindings(
        lock_digest=plan["plan_digest"], environment_digest=plan["environment_digest"]
    )


def _stage_marker(plan: dict, stage: int, records: VerifiedKyokuRecords) -> dict:
    return {
        "plan_digest": plan["plan_digest"],
        "raw_manifest_sha256": records.manifest_sha256,
        "schema": STAGE_SCHEMA,
        "seeds": list(stage_seeds(plan, stage)),
        "stage": stage,
    }


def run_stage(
    plan_path: str | Path,
    stage: int,
    output_root: str | Path,
    *,
    ledger_path: str | Path = seed_registry.DEFAULT_LEDGER_PATH,
    play: Callable[[HanchanTask], PlayedHanchan] = play_hanchan_source_wheel,
    environment: dict | None = None,
    head: str | None = None,
    on_hanchan: Callable[[int, int], None] | None = None,
) -> dict[str, object]:
    """1 stageを実行し、``stage-NNN`` へraw・timings・完了markerをwrite-onceで書く。

    例外・timeout・環境不一致が1件でもあれば何も書かずに止まる。
    """
    plan = load_plan(plan_path)
    seeds = stage_seeds(plan, stage)
    destination = stage_directory(output_root, stage)
    if destination.exists():
        raise FileExistsError(f"refusing to overwrite: {destination}")
    if not Path(output_root).is_dir():
        raise FileNotFoundError(f"output root does not exist: {output_root}")
    ledger = None
    if plan["mode"] == MODE_MEASUREMENT:
        try:
            ledger = seed_registry.load_ledger(ledger_path)
        except seed_registry.SeedRegistryError as error:
            raise RiichiResponseHanchanError(
                f"seed registry cannot be used: {error}"
            ) from error
    require_live_target(plan, ledger=ledger, environment=environment, head=head)
    candidate, incumbent = _participants(plan)
    executed = run_schedule(
        hanchans=_schedule(seeds)["hanchans"],
        candidate=candidate,
        incumbent=incumbent,
        wheel_path=plan["execution"]["wheel_path"],
        timeout_seconds=plan["execution"]["timeout_seconds"],
        expected_environment=plan["environment"],
        play=play,
        on_hanchan=on_hanchan,
    )
    destination.mkdir()
    records = write_kyoku_records(
        destination / RAW_DIRNAME,
        [item.capture for item in executed],
        producer_revision=plan["arena_revision"],
        event_facts_required=True,
        bindings=_bindings(plan),
    )
    timings = {
        "hanchans": [
            {
                "kyoku_count": len(item.capture.match.history),
                "peak_rss_kib": item.peak_rss_kib,
                "placement": item.capture.placement,
                "seed": item.capture.seed,
                "wall_seconds": item.wall_seconds,
            }
            for item in executed
        ],
        "plan_digest": plan["plan_digest"],
        "stage": stage,
    }
    write_new_artifact_file(
        destination / TIMINGS_FILENAME, canonical_json_text(timings)
    )
    marker = _stage_marker(plan, stage, records)
    write_new_artifact_file(destination / STAGE_FILENAME, canonical_json_text(marker))
    return marker


def _read_stage(plan: dict, stage: int, output_root: str | Path):
    directory = stage_directory(output_root, stage)
    try:
        marker = read_json_document(directory / STAGE_FILENAME)
    except (ArtifactValidationError, OSError, ValueError) as error:
        raise RiichiResponseHanchanError(
            f"stage {stage} is missing or incomplete: {error}"
        ) from error
    records = read_kyoku_records(directory / RAW_DIRNAME, event_facts_required=True)
    if type(marker) is not dict or set(marker) != _STAGE_FIELDS:
        raise RiichiResponseHanchanError(f"stage {stage} marker fields are invalid")
    if marker != _stage_marker(plan, stage, records):
        raise RiichiResponseHanchanError(f"stage {stage} marker does not match its raw")
    require_bindings(records, _bindings(plan))
    if not records.event_fact_status.valid or records.event_facts is None:
        raise RiichiResponseHanchanError(f"stage {stage} has no valid event facts")
    candidate, incumbent = _participants(plan)
    verify_schedule(
        records.hanchans,
        expected_schedule(
            _schedule(stage_seeds(plan, stage))["hanchans"], candidate, incumbent
        ),
    )
    return marker, records


# ---------------------------------------------------------------------------
# summary
# ---------------------------------------------------------------------------


def verdict_of(statistics: dict) -> str:
    """事前に固定した判定規則（module docstring）。"""
    if statistics["mean_d"] <= 0:
        return NOT_SUPPORTED
    return SUPPORTED if statistics["lower"] > 0 else INCONCLUSIVE


def build_summary(plan: dict, output_root: str | Path) -> dict[str, object]:
    """全stageの保存物から集計を作る。1 stageでも欠けていれば失敗する。"""
    plan = parse_plan(plan)
    markers = []
    hanchans: list[dict] = []
    events: list[dict] = []
    for stage in range(stage_count(plan)):
        marker, records = _read_stage(plan, stage, output_root)
        markers.append(marker)
        hanchans.extend(records.hanchans)
        events.extend(records.event_facts)
    stage_digest = document_identity({"stages": markers})
    merged = VerifiedKyokuRecords(
        hanchans=tuple(hanchans),
        event_facts=tuple(events),
        event_fact_status=EventFactStatus(EVENT_FACTS_VALID),
        bindings=_bindings(plan),
        producer_revision=plan["arena_revision"],
        manifest_sha256=stage_digest,
    )
    blocks = block_totals(merged.hanchans)
    if [block["seed"] for block in blocks] != plan["ordered_seeds"]:
        raise RiichiResponseHanchanError("stage seeds do not follow the plan order")
    payload = {
        "aggregation_revision": plan["arena_revision"],
        "blocks": blocks,
        "evaluation_kind": EVALUATION_KIND,
        "hanchan_count": len(hanchans),
        "kyoku_count": sum(len(row["kyokus"]) for row in hanchans),
        "mode": plan["mode"],
        "not_a_formal_result": True,
        "plan_digest": plan["plan_digest"],
        "schema": SUMMARY_SCHEMA,
        "secondary_metrics": summarize_secondary_metrics(merged),
        "stages": markers,
        "stages_digest": stage_digest,
    }
    if plan["mode"] == MODE_MEASUREMENT:
        statistics = summarize_blocks(
            [block["difference_internal"] for block in blocks]
        )
        # 正式protocolのclassification labelは開発比較の判定に使わない
        statistics = {
            key: item
            for key, item in statistics.items()
            if key not in ("classification", "classification_rule_id")
        }
        payload["statistics"] = statistics
        payload["status"] = verdict_of(statistics)
    else:
        payload["statistics"] = None
        payload["status"] = REHEARSAL_STATUS
    return {**payload, "summary_digest": document_identity(payload)}


def write_summary(plan_path: str | Path, output_root: str | Path) -> dict[str, object]:
    summary = build_summary(load_plan(plan_path), output_root)
    write_new_artifact_file(
        Path(output_root) / SUMMARY_FILENAME, canonical_json_text(summary)
    )
    return summary


def verify_summary(plan_path: str | Path, output_root: str | Path) -> dict[str, object]:
    """保存物だけからsummaryを再導出し、記録値との完全一致を確かめる。"""
    try:
        recorded = read_json_document(Path(output_root) / SUMMARY_FILENAME)
    except (ArtifactValidationError, OSError, ValueError) as error:
        raise RiichiResponseHanchanError(
            f"cannot read the #591 summary: {error}"
        ) from error
    rederived = build_summary(load_plan(plan_path), output_root)
    if recorded != rederived:
        raise RiichiResponseHanchanError(
            "the #591 summary differs from the re-derivation"
        )
    return rederived


__all__ = [
    "CANDIDATE_IDENTITY",
    "DECISION_RULE",
    "INCONCLUSIVE",
    "INCUMBENT_IDENTITY",
    "MEASUREMENT_K",
    "MODE_MEASUREMENT",
    "MODE_REHEARSAL",
    "NOT_SUPPORTED",
    "OWNER_ISSUE",
    "PLAN_SCHEMA",
    "POPULATION",
    "RiichiResponseHanchanError",
    "SPLIT",
    "SUMMARY_SCHEMA",
    "SUPPORTED",
    "assemble_plan",
    "build_plan",
    "build_summary",
    "load_plan",
    "local_environment_document",
    "parse_plan",
    "play_hanchan_source_wheel",
    "require_live_target",
    "require_measurement_seeds",
    "run_stage",
    "save_plan",
    "stage_count",
    "stage_directory",
    "stage_seeds",
    "verdict_of",
    "verify_summary",
    "write_summary",
]
