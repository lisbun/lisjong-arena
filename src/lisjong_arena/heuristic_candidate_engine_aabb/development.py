"""§10.7の記録経路確認（development harness、#514）。

正式runnerと同じexecutor・record・集計codeを、既知の開発用入力だけで1回通す。
強さ比較・正式判定ではない。正式lockのFORMAL-EVAL / live ledger検査を無効にして
流用するのではなく、このprotocol専用の別documentを使う。

```text
seeds         472005, 472006（#472の開発用。registry外、正式populationから除外済み）
participants  A = placement-aware-speed-call（C0）
              B = one-shanten-defense-placement-aware-speed-call（#472と同じ組）
backend       rust
出力          <dir>/plan.json      evaluation_kind = DEVELOPMENT、plan_digest
              <dir>/raw/           局単位record（第1層・第2層、bindingsはplan_digest）
              <dir>/summary.json   block合計・副指標（classificationと区間は出さない）
              <dir>/timings.json   半荘ごとのwall時間・worker peak RSS（再導出対象外）
```

第1層・第2層ともそろうことを開始条件とする（``event_facts_required=True``）。
"""

from collections.abc import Callable
from pathlib import Path

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
from lisjong_arena.policy_catalog import POLICY_CATALOG

from .executor import (
    HanchanTask,
    PlayedHanchan,
    environment_document,
    play_hanchan,
    run_schedule,
)
from .kyoku_record import PROTOCOL_ID, ROLE_CANDIDATE, ROLE_INCUMBENT, verify_schedule
from .protocol import (
    FACTORY_MODULE,
    PROTOCOL_DIGEST,
    RULES_DIGEST,
    EngineAabbProtocolError,
    Participant,
    expected_schedule,
    parse_participant,
    placement_schedule,
    require_hex,
)
from .record_store import (
    RecordBindings,
    read_kyoku_records,
    require_bindings,
    write_kyoku_records,
)
from .secondary_metrics import summarize_secondary_metrics
from .statistics import block_totals

PLAN_SCHEMA = "arena-heuristic-candidate-engine-aabb-development-plan-v1"
SUMMARY_SCHEMA = "arena-heuristic-candidate-engine-aabb-development-summary-v1"
EVALUATION_KIND = "DEVELOPMENT"
OWNER_ISSUE = "lisbun/lisjong-arena#514"
PURPOSE = (
    "record path check (spec §10.7): capture -> store -> read -> verify -> aggregate; "
    "not a strength comparison and not a formal result"
)
DEVELOPMENT_SEEDS = (472_005, 472_006)
DEVELOPMENT_PAIR = (
    "placement-aware-speed-call",
    "one-shanten-defense-placement-aware-speed-call",
)
PLAN_FILENAME = "plan.json"
RAW_DIRNAME = "raw"
SUMMARY_FILENAME = "summary.json"
TIMINGS_FILENAME = "timings.json"

_PLAN_FIELDS = frozenset(
    {
        "arena_revision",
        "environment",
        "environment_digest",
        "evaluation_kind",
        "execution",
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


def catalog_participant(identity: str, lisjong_revision: str) -> Participant:
    """``policy_catalog``の設定なしfactoryからparticipantを作る。"""
    spec = POLICY_CATALOG.get(identity)
    if spec is None:
        raise EngineAabbProtocolError(f"unknown catalog policy {identity!r}")
    return Participant(
        policy_identity=identity,
        factory_binding=f"{FACTORY_MODULE}:{spec.factory.__name__}",
        lisjong_revision=lisjong_revision,
        behavior_configuration={},
    )


def development_participants(lisjong_revision: str) -> tuple[Participant, Participant]:
    return tuple(
        catalog_participant(identity, lisjong_revision) for identity in DEVELOPMENT_PAIR
    )


def build_plan(
    *, environment: dict, arena_revision: str, timeout_seconds: int, wheel_path: str
) -> dict[str, object]:
    require_hex(arena_revision, 40, "arena_revision")
    if type(timeout_seconds) is not int or timeout_seconds <= 0:
        raise EngineAabbProtocolError(
            "timeout_seconds must be an explicit positive int"
        )
    lisjong = environment["provenance"]["lisjong_revision"]
    candidate, incumbent = development_participants(lisjong)
    payload = {
        "arena_revision": arena_revision,
        "environment": environment,
        "environment_digest": document_identity(environment),
        "evaluation_kind": EVALUATION_KIND,
        "execution": {
            "backend": "rust",
            "timeout_seconds": timeout_seconds,
            "wheel_path": wheel_path,
            "workers": 1,
        },
        "ordered_seeds": list(DEVELOPMENT_SEEDS),
        "owner_issue": OWNER_ISSUE,
        "participants": {
            ROLE_CANDIDATE: candidate.to_document(),
            ROLE_INCUMBENT: incumbent.to_document(),
        },
        "protocol_digest": PROTOCOL_DIGEST,
        "purpose": PURPOSE,
        "rules_digest": RULES_DIGEST,
        "schedule_digest": document_identity(_schedule()),
        "schema": PLAN_SCHEMA,
    }
    return parse_plan({**payload, "plan_digest": document_identity(payload)})


def _schedule() -> dict:
    # 開発用seedはregistry外の既知seedなので、正式populationの検査（require_seeds）を通さない
    return {
        "hanchans": placement_schedule(DEVELOPMENT_SEEDS),
        "protocol_id": PROTOCOL_ID,
    }


def parse_plan(value: object) -> dict[str, object]:
    if type(value) is not dict or value.get("schema") != PLAN_SCHEMA:
        raise EngineAabbProtocolError("unsupported development plan schema")
    if set(value) != _PLAN_FIELDS:
        raise EngineAabbProtocolError("development plan fields do not match the schema")
    if (
        value["evaluation_kind"] != EVALUATION_KIND
        or value["purpose"] != PURPOSE
        or value["owner_issue"] != OWNER_ISSUE
        or value["ordered_seeds"] != list(DEVELOPMENT_SEEDS)
        or value["protocol_digest"] != PROTOCOL_DIGEST
        or value["rules_digest"] != RULES_DIGEST
        or value["schedule_digest"] != document_identity(_schedule())
    ):
        raise EngineAabbProtocolError("development plan identity drifted")
    participants = value["participants"]
    if type(participants) is not dict or set(participants) != {
        ROLE_CANDIDATE,
        ROLE_INCUMBENT,
    }:
        raise EngineAabbProtocolError("development plan participants must be A and B")
    candidate = parse_participant(participants[ROLE_CANDIDATE], "plan.participants.A")
    incumbent = parse_participant(participants[ROLE_INCUMBENT], "plan.participants.B")
    if (candidate.policy_identity, incumbent.policy_identity) != DEVELOPMENT_PAIR:
        raise EngineAabbProtocolError("development plan must use the #472 pair")
    if value["environment_digest"] != document_identity(value["environment"]):
        raise EngineAabbProtocolError("development plan environment digest mismatch")
    payload = {key: item for key, item in value.items() if key != "plan_digest"}
    if value["plan_digest"] != document_identity(payload):
        raise EngineAabbProtocolError("development plan digest mismatch")
    return value


def _participants(plan: dict) -> tuple[Participant, Participant]:
    return (
        parse_participant(plan["participants"][ROLE_CANDIDATE], "A"),
        parse_participant(plan["participants"][ROLE_INCUMBENT], "B"),
    )


def _bindings(plan: dict) -> RecordBindings:
    return RecordBindings(
        lock_digest=plan["plan_digest"], environment_digest=plan["environment_digest"]
    )


def build_summary(plan: dict, records) -> dict[str, object]:
    candidate, incumbent = _participants(plan)
    verify_schedule(
        records.hanchans,
        expected_schedule(_schedule()["hanchans"], candidate, incumbent),
    )
    require_bindings(records, _bindings(plan))
    payload = {
        "aggregation_revision": plan["arena_revision"],
        "blocks": block_totals(records.hanchans),
        "evaluation_kind": EVALUATION_KIND,
        "event_fact_status": records.event_fact_status.to_value(),
        "hanchan_count": len(records.hanchans),
        "kyoku_count": sum(len(row["kyokus"]) for row in records.hanchans),
        "not_a_formal_result": True,
        "plan_digest": plan["plan_digest"],
        "raw_manifest_sha256": records.manifest_sha256,
        "schema": SUMMARY_SCHEMA,
        "secondary_metrics": summarize_secondary_metrics(records),
    }
    return {**payload, "summary_digest": document_identity(payload)}


def run_development_check(
    output_dir: str | Path,
    *,
    wheel_path: str,
    timeout_seconds: int,
    play: Callable[[HanchanTask], PlayedHanchan] = play_hanchan,
    environment: dict | None = None,
    arena_revision: str | None = None,
    on_hanchan: Callable[[int, int], None] | None = None,
) -> dict[str, object]:
    """開発用12半荘を実行し、plan / raw / summary / timingsを新しいdirectoryへ書く。"""
    output = Path(output_dir)
    if output.exists():
        raise FileExistsError(f"refusing to overwrite: {output}")
    if arena_revision is None:
        try:
            arena_revision = require_clean_arena_head()
        except ExecutionSafetyError as error:
            raise EngineAabbProtocolError(str(error)) from error
    if environment is None:
        environment = environment_document(wheel_path)
    plan = build_plan(
        environment=environment,
        arena_revision=arena_revision,
        timeout_seconds=timeout_seconds,
        wheel_path=wheel_path,
    )
    candidate, incumbent = _participants(plan)
    executed = run_schedule(
        hanchans=_schedule()["hanchans"],
        candidate=candidate,
        incumbent=incumbent,
        wheel_path=wheel_path,
        timeout_seconds=timeout_seconds,
        expected_environment=environment,
        play=play,
        on_hanchan=on_hanchan,
    )
    output.mkdir()
    write_new_artifact_file(output / PLAN_FILENAME, canonical_json_text(plan))
    records = write_kyoku_records(
        output / RAW_DIRNAME,
        [item.capture for item in executed],
        producer_revision=arena_revision,
        event_facts_required=True,
        bindings=_bindings(plan),
    )
    summary = build_summary(plan, records)
    write_new_artifact_file(output / SUMMARY_FILENAME, canonical_json_text(summary))
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
    }
    write_new_artifact_file(output / TIMINGS_FILENAME, canonical_json_text(timings))
    return summary


def verify_development_check(output_dir: str | Path) -> dict[str, object]:
    """保存物だけからsummaryを再導出し、記録値との完全一致を確かめる。"""
    output = Path(output_dir)
    try:
        plan = parse_plan(read_json_document(output / PLAN_FILENAME))
        recorded = read_json_document(output / SUMMARY_FILENAME)
    except (ArtifactValidationError, OSError, ValueError) as error:
        raise EngineAabbProtocolError(
            f"cannot read development output: {error}"
        ) from error
    records = read_kyoku_records(output / RAW_DIRNAME, event_facts_required=True)
    rederived = build_summary(plan, records)
    if recorded != rederived:
        raise EngineAabbProtocolError(
            "development summary differs from the re-derivation"
        )
    return rederived


__all__ = [
    "DEVELOPMENT_PAIR",
    "DEVELOPMENT_SEEDS",
    "EVALUATION_KIND",
    "PLAN_SCHEMA",
    "SUMMARY_SCHEMA",
    "build_plan",
    "build_summary",
    "catalog_participant",
    "parse_plan",
    "run_development_check",
    "verify_development_check",
]
