"""Issue #326 locked 96-hanchan wait-shape qualification pilot.

This module owns only the non-scientific F1/F2 qualification execution that was
prelocked by lisbun/lisjong-arena#322.  It deliberately cannot generate the
scientific TRAIN/SELECT/EVAL populations and does not train A/M models.

The durable pilot artifact contains only support observations needed to rederive
F1/F2.  Privileged concealed hands are consumed in-memory by the canonical
lisjong wait builder and are never persisted by this module.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import platform
import sys
from collections import Counter, defaultdict
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from lisjong.action_vocabulary import encode_action, resolve_legal_action
from lisjong.policy_contract import Seat
from lisjong.policy_contract.action import DiscardAction
from lisjong.policy_contract.riichi import RiichiState
from lisjong.policies.targeted_honor_release_terminal_progression import (
    TargetedHonorReleaseAnalysis,
    TargetedHonorReleaseBranch,
)

from lisjong_arena._artifact_io import (
    ArtifactValidationError,
    canonical_json_text,
    parse_json_text,
    read_json_document,
    staged_artifact_directory,
    write_new_artifact_file,
)
from lisjong_arena._execution_safety import (
    ExecutionSafetyError,
    require_clean_arena_head,
    require_merged_arena_revision,
    require_new_artifact_destinations,
)
from lisjong_arena.environment_identity import (
    EnvironmentIdentityError,
    verify_environment,
)
from lisjong_arena.learned_policy_offline_q.activation import (
    is_eligible_ordinary_discard_choice,
)
from lisjong_arena.learned_policy_stage2.recording import iter_inspection_decisions
from lisjong_arena.policy_catalog import (
    create_targeted_honor_release_terminal_progression,
)
from lisjong_arena.riichienv.local_game_runner import (
    LocalGameInspectionRecorder,
    LocalGameRunner,
)
from lisjong_arena.single_round_artifact import (
    collect_execution_provenance,
    execution_provenance_to_dict,
)
from lisjong_arena.stage_a0_tenpai_feasibility.hidden_state import (
    DecisionPointHiddenStateRecorder,
    require_same_decision_state,
)
from lisjong_arena.stage_a0_tenpai_feasibility.protocol import (
    exact_wait_implementation_identity,
)

from .labels import (
    WaitShapeAvailability,
    WaitShapeProjection,
    build_wait_shape_target,
)
from .protocol import (
    DEFENSE_DIAGNOSTIC_STATUS,
    DESCRIPTIVE_SHAPE,
    F1_MAX_ANCHOR_PREVALENCE,
    F1_MAX_SINGLE_EPISODE_ROW_CONCENTRATION,
    F1_MIN_ANCHOR_PREVALENCE,
    F1_MIN_LABELABLE_FRACTION,
    F1_MIN_NEGATIVE_ANCHOR_EPISODES,
    F1_MIN_NEGATIVE_SOURCE_HANCHAN,
    F1_MIN_POSITIVE_ANCHOR_EPISODES,
    F1_MIN_POSITIVE_SOURCE_HANCHAN,
    F2_MAX_SELECTED_DISCARD_INDEX_SHARE,
    F2_MIN_DISCARD_CHOICE_DECISIONS,
    F2_MIN_RIICHI_EPISODES,
    F2_MIN_SELECTED_DISCARD_INDICES,
    F2_MIN_SOURCE_HANCHAN,
    GAME_MODE,
    LISJONG_ENGINE_REVISION,
    PILOT_SEEDS,
    PRIMARY_SHAPES,
    RIICHIENV_VERSION,
    TEACHER_IDENTITY,
    TEACHER_LISJONG_REVISION,
    TEACHER_POLICY_CLASS,
    TEACHER_POPULATION,
    TEACHER_SOURCE_MODULE,
    protocol_lock_document,
    protocol_lock_identity,
)

EXPECTED_PROTOCOL_LOCK_IDENTITY = (
    "15b9b3ad22569491860593509647d753f2b7160680ff0fb3c8b953575530784d"
)
EXPECTED_CANONICAL_WAIT_IDENTITY = (
    "18ce0fbf9c8f986d9fe214f1056a2cf57ba11d0a5e5656e32ea2437894ca6afe"
)
F0_OUTCOME = "RETAINED WAIT-SHAPE LABEL PATH QUALIFIED"
F0_ARTIFACT_SHA256 = "0af9ad89be0cf69d939cfefbc576f99e28678556c1dcab7c58aaf5be8b7280a7"

LOCK_SCHEMA_VERSION = "arena-wait-shape-pilot-lock-v1"
RAW_SCHEMA_VERSION = "arena-wait-shape-pilot-raw-v1"
F1_SCHEMA_VERSION = "arena-wait-shape-f1-v1"
F2_SCHEMA_VERSION = "arena-wait-shape-f2-v1"
QUALIFICATION_SCHEMA_VERSION = "arena-wait-shape-qualification-result-v1"

F1_QUALIFIED = "WAIT-SHAPE SUPPORT QUALIFIED"
F1_NOT_QUALIFIED = "WAIT-SHAPE SUPPORT NOT QUALIFIED"
F2_QUALIFIED = "RIICHI-EXPOSED ACTION SUPPORT QUALIFIED"
F2_NOT_QUALIFIED = "RIICHI-EXPOSED ACTION SUPPORT NOT QUALIFIED"
STOP_INVALID = "STOP / INVALID"
FINAL_QUALIFIED = "WAIT-SHAPE QUALIFICATION PASSED — SCIENTIFIC PROTOCOL LOCKED"

OBSERVATIONS_FILENAME = "observations.jsonl"
RAW_MANIFEST_FILENAME = "manifest.json"
_PROJECT_PATH = Path(__file__).resolve().parents[3] / "pyproject.toml"

_NO_RESCUE_BOUNDARY = (
    "pilot seeds 2000..2095 are qualification-only and never reused for scientific TRAIN/SELECT/EVAL/probe",
    "pilot seeds are never extended, replaced, or selectively rerun after support-result exposure",
    "teacher remains targeted-honor-release-terminal-progression x4",
    "all five ordinary wait shapes remain primary F1 targets",
    "F1/F2 thresholds remain exactly the #322 protocol lock",
    "DEFENSE DIAGNOSTIC NOT APPLICABLE remains non-negotiable for F2",
    "no A/M model training occurs in Issue #326",
    "scientific seeds 2100..2259 are never generated by this module",
)


class WaitShapePilotError(ArtifactValidationError):
    """The #326 pilot cannot be executed or interpreted under the locked contract."""


@dataclass(frozen=True, slots=True)
class LoadedPilotRaw:
    path: Path
    manifest: dict[str, object]
    observations: tuple[dict[str, object], ...]

    @property
    def identity(self) -> str:
        return str(self.manifest["raw_identity"])


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise WaitShapePilotError(message)


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _is_full_commit_id(value: object) -> bool:
    return (
        type(value) is str
        and len(value) == 40
        and all(character in "0123456789abcdef" for character in value)
    )


def _document_identity(document: dict[str, object], field: str) -> str:
    logical = {key: value for key, value in document.items() if key != field}
    return _sha256_text(canonical_json_text(logical))


def _runtime_document() -> dict[str, object]:
    return {
        "python_implementation": platform.python_implementation(),
        "python_version": platform.python_version(),
        "sys_version": sys.version.split()[0],
    }


def _verify_locked_environment() -> dict[str, object]:
    if protocol_lock_identity() != EXPECTED_PROTOCOL_LOCK_IDENTITY:
        raise WaitShapePilotError("the #322 protocol lock identity drifted")
    if exact_wait_implementation_identity() != EXPECTED_CANONICAL_WAIT_IDENTITY:
        raise WaitShapePilotError("the canonical wait implementation identity drifted")

    try:
        environment = verify_environment(_PROJECT_PATH)
    except EnvironmentIdentityError as exc:
        raise WaitShapePilotError(str(exc)) from exc
    if not environment.ok:
        raise WaitShapePilotError(
            "internal VCS dependency environment is inconsistent: "
            + "; ".join(environment.errors)
        )

    provenance = collect_execution_provenance()
    if provenance.lisjong_revision != TEACHER_LISJONG_REVISION:
        raise WaitShapePilotError("installed lisjong revision differs from #322 lock")
    if provenance.lisjong_engine_revision != LISJONG_ENGINE_REVISION:
        raise WaitShapePilotError(
            "installed lisjong-engine revision differs from #322 lock"
        )
    if provenance.riichienv_version != RIICHIENV_VERSION:
        raise WaitShapePilotError("installed RiichiEnv version differs from #322 lock")

    try:
        head = require_clean_arena_head()
        if head != provenance.lisjong_arena_revision:
            raise WaitShapePilotError(
                "Arena HEAD differs from collected execution provenance"
            )
        require_merged_arena_revision(head, branch="main")
    except ExecutionSafetyError as exc:
        raise WaitShapePilotError(str(exc)) from exc

    return {
        "execution_target": {
            "branch": "main",
            "revision": head,
            "target_type": "reviewed-merged-main-v1",
        },
        "provenance": execution_provenance_to_dict(provenance),
        "runtime": _runtime_document(),
    }


def build_pilot_teacher_population() -> dict[Seat, object]:
    """Create exactly four fresh locked-teacher instances for one game."""

    policies = {
        seat: create_targeted_honor_release_terminal_progression() for seat in Seat
    }
    if len({id(policy) for policy in policies.values()}) != 4:
        raise WaitShapePilotError("each seat must receive a fresh Policy instance")
    for policy in policies.values():
        cls = type(policy)
        if cls.__name__ != TEACHER_POLICY_CLASS:
            raise WaitShapePilotError("teacher class differs from the #322 lock")
        if cls.__module__ != TEACHER_SOURCE_MODULE:
            raise WaitShapePilotError(
                "teacher source module differs from the #322 lock"
            )
    return policies


def _destinations(output_root: Path) -> dict[str, Path]:
    return {
        "lock": output_root / "execution-lock.json",
        "raw": output_root / "pilot-raw",
        "f1": output_root / "f1.json",
        "f2": output_root / "f2.json",
        "qualification": output_root / "qualification.json",
    }


def build_execution_lock(
    output_root: str | Path,
    *,
    max_workers: int,
    repository_collision_audit_pass: bool,
    private_collision_audit_pass: bool,
    no_prior_result_exposure_confirmed: bool,
) -> dict[str, object]:
    """Build a result-independent write-once lock before seed 2000 is generated."""

    if type(max_workers) is not int or not 1 <= max_workers <= 2:
        raise WaitShapePilotError("max_workers must be 1 or 2")
    for name, value in (
        ("repository_collision_audit_pass", repository_collision_audit_pass),
        ("private_collision_audit_pass", private_collision_audit_pass),
        ("no_prior_result_exposure_confirmed", no_prior_result_exposure_confirmed),
    ):
        if type(value) is not bool or not value:
            raise WaitShapePilotError(f"{name} must be explicitly true")

    root = Path(output_root)
    destinations = _destinations(root)
    try:
        require_new_artifact_destinations(
            destinations, required_names=tuple(destinations)
        )
    except ExecutionSafetyError as exc:
        raise WaitShapePilotError(str(exc)) from exc

    execution = _verify_locked_environment()
    payload: dict[str, object] = {
        "schema_version": LOCK_SCHEMA_VERSION,
        "issue": "lisbun/lisjong-arena#326",
        "parent_issue": "lisbun/lisjong-arena#322",
        "protocol_lock_identity": protocol_lock_identity(),
        "protocol": protocol_lock_document(),
        "f0_prerequisite": {
            "outcome": F0_OUTCOME,
            "artifact_sha256": F0_ARTIFACT_SHA256,
        },
        "freshness": {
            "repository_collision_audit_pass": True,
            "private_collision_audit_pass": True,
            "no_prior_result_exposure_confirmed": True,
        },
        "pilot": {
            "ordered_seeds": list(PILOT_SEEDS),
            "hanchan_count": len(PILOT_SEEDS),
            "game_mode": GAME_MODE,
            "teacher_identity": TEACHER_IDENTITY,
            "teacher_policy_class": TEACHER_POLICY_CLASS,
            "teacher_population": TEACHER_POPULATION,
            "fresh_policy_instance_per_seat_game": True,
            "max_workers": max_workers,
            "scientific_reuse_forbidden": True,
        },
        "f2_operationalization": {
            "normal_discard_choice_predicate": (
                "lisjong_arena.learned_policy_offline_q.activation."
                "is_eligible_ordinary_discard_choice"
            ),
            "semantics": (
                "all legal actions are DiscardAction and there are at least two"
            ),
        },
        "defense_diagnostic_status": DEFENSE_DIAGNOSTIC_STATUS,
        "no_rescue_boundary": list(_NO_RESCUE_BOUNDARY),
        "result_exposed": False,
        "artifact_destinations": {
            name: str(path) for name, path in destinations.items()
        },
        **execution,
    }
    document = dict(payload)
    document["lock_identity"] = _document_identity(document, "lock_identity")
    return document


def _write_lock(document: dict[str, object], path: Path) -> None:
    _validate_lock_document(document)
    path.parent.mkdir(parents=True, exist_ok=True)
    write_new_artifact_file(path, canonical_json_text(document))


def _validate_lock_document(document: object) -> dict[str, object]:
    _require(type(document) is dict, "lock must be an object")
    expected = {
        "schema_version",
        "issue",
        "parent_issue",
        "protocol_lock_identity",
        "protocol",
        "f0_prerequisite",
        "freshness",
        "pilot",
        "f2_operationalization",
        "defense_diagnostic_status",
        "no_rescue_boundary",
        "result_exposed",
        "artifact_destinations",
        "execution_target",
        "provenance",
        "runtime",
        "lock_identity",
    }
    _require(set(document) == expected, "lock fields are invalid")
    _require(document["schema_version"] == LOCK_SCHEMA_VERSION, "unsupported lock")
    _require(
        document["protocol_lock_identity"] == EXPECTED_PROTOCOL_LOCK_IDENTITY,
        "lock protocol identity drifted",
    )
    _require(
        canonical_json_text(document["protocol"])  # type: ignore[arg-type]
        == canonical_json_text(protocol_lock_document()),
        "lock protocol document drifted",
    )
    _require(document["result_exposed"] is False, "lock must precede result exposure")
    pilot = document["pilot"]
    _require(type(pilot) is dict, "lock pilot must be an object")
    _require(pilot.get("ordered_seeds") == list(PILOT_SEEDS), "pilot seeds drifted")
    _require(pilot.get("hanchan_count") == 96, "pilot hanchan count drifted")
    _require(pilot.get("game_mode") == GAME_MODE, "pilot game mode drifted")
    _require(
        pilot.get("teacher_identity") == TEACHER_IDENTITY,
        "pilot teacher identity drifted",
    )
    _require(
        pilot.get("teacher_policy_class") == TEACHER_POLICY_CLASS,
        "pilot teacher class drifted",
    )
    _require(
        pilot.get("teacher_population") == TEACHER_POPULATION,
        "pilot teacher population drifted",
    )
    _require(
        pilot.get("fresh_policy_instance_per_seat_game") is True,
        "fresh Policy requirement drifted",
    )
    _require(
        type(pilot.get("max_workers")) is int and 1 <= int(pilot["max_workers"]) <= 2,
        "pilot max_workers is invalid",
    )
    _require(
        pilot.get("scientific_reuse_forbidden") is True,
        "pilot scientific-reuse boundary drifted",
    )
    freshness = document["freshness"]
    _require(
        type(freshness) is dict
        and freshness
        == {
            "repository_collision_audit_pass": True,
            "private_collision_audit_pass": True,
            "no_prior_result_exposure_confirmed": True,
        },
        "freshness gate is not affirmative",
    )
    f0 = document["f0_prerequisite"]
    _require(
        f0 == {"outcome": F0_OUTCOME, "artifact_sha256": F0_ARTIFACT_SHA256},
        "F0 prerequisite binding drifted",
    )
    _require(
        document["defense_diagnostic_status"] == DEFENSE_DIAGNOSTIC_STATUS,
        "defense diagnostic lock drifted",
    )
    _require(
        document["no_rescue_boundary"] == list(_NO_RESCUE_BOUNDARY),
        "no-rescue boundary drifted",
    )

    execution_target = document["execution_target"]
    _require(
        type(execution_target) is dict
        and set(execution_target) == {"branch", "revision", "target_type"},
        "execution target fields are invalid",
    )
    _require(execution_target["branch"] == "main", "execution branch drifted")
    _require(
        execution_target["target_type"] == "reviewed-merged-main-v1",
        "execution target type drifted",
    )
    _require(
        _is_full_commit_id(execution_target["revision"]),
        "execution Arena revision is not a full commit id",
    )

    provenance = document["provenance"]
    provenance_fields = {
        "execution_environment",
        "lisjong_arena_version",
        "lisjong_arena_revision",
        "lisjong_version",
        "lisjong_revision",
        "lisjong_engine_version",
        "lisjong_engine_revision",
        "riichienv_version",
        "python_version",
    }
    _require(
        type(provenance) is dict and set(provenance) == provenance_fields,
        "execution provenance fields are invalid",
    )
    _require(
        provenance["execution_environment"] == "riichienv",
        "execution environment drifted",
    )
    _require(
        provenance["lisjong_arena_revision"] == execution_target["revision"],
        "Arena provenance differs from execution target",
    )
    _require(
        provenance["lisjong_revision"] == TEACHER_LISJONG_REVISION,
        "lisjong provenance differs from #322 lock",
    )
    _require(
        provenance["lisjong_engine_revision"] == LISJONG_ENGINE_REVISION,
        "lisjong-engine provenance differs from #322 lock",
    )
    _require(
        provenance["riichienv_version"] == RIICHIENV_VERSION,
        "RiichiEnv provenance differs from #322 lock",
    )

    runtime = document["runtime"]
    _require(
        type(runtime) is dict
        and set(runtime)
        == {"python_implementation", "python_version", "sys_version"}
        and all(type(value) is str and value for value in runtime.values()),
        "runtime identity is invalid",
    )

    destinations = document["artifact_destinations"]
    _require(
        type(destinations) is dict
        and set(destinations) == {"lock", "raw", "f1", "f2", "qualification"}
        and all(type(value) is str and value for value in destinations.values()),
        "artifact destination lock is invalid",
    )
    _require(
        document["lock_identity"] == _document_identity(document, "lock_identity"),
        "lock identity mismatch",
    )
    return document


def _load_lock(path: Path) -> dict[str, object]:
    try:
        document = _validate_lock_document(read_json_document(path))
    except (OSError, json.JSONDecodeError, ArtifactValidationError, TypeError) as exc:
        if isinstance(exc, WaitShapePilotError):
            raise
        raise WaitShapePilotError("execution lock is malformed") from exc
    _require(
        path.read_text(encoding="utf-8") == canonical_json_text(document),
        "execution lock is not canonical JSON",
    )
    return document


def _projection_document(projection: WaitShapeProjection) -> dict[str, int]:
    return {
        "TANKI": projection.tanki,
        "SHANPON": projection.shanpon,
        "KANCHAN": projection.kanchan,
        "PENCHAN": projection.penchan,
        "RYANMEN": projection.ryanmen,
        "KOKUSHI": projection.kokushi,
    }


def _run_seed(
    seed: int,
) -> tuple[dict[str, object], tuple[dict[str, object], ...]]:
    """Execute one exact pilot seed and return a game receipt plus support observations."""

    if seed not in PILOT_SEEDS:
        raise WaitShapePilotError("pilot executor refuses non-pilot seeds")

    inspection_recorder = LocalGameInspectionRecorder()
    hidden_recorder = DecisionPointHiddenStateRecorder()
    runner = LocalGameRunner(
        build_pilot_teacher_population(),
        seed=seed,
        game_mode=GAME_MODE,
        inspection_recorder=inspection_recorder,
        decision_point_observer=hidden_recorder,
    )
    result = runner.run()
    if result.seed != seed or result.game_mode != GAME_MODE:
        raise WaitShapePilotError("game result identity differs from the locked pilot")
    if hidden_recorder.observed_step_count != result.steps:
        raise WaitShapePilotError("hidden-state observer did not cover every game step")

    inspection = inspection_recorder.snapshot()
    analysis_by_decision: dict[tuple[int, int], object] = {}
    for step in inspection.step_observations:
        for seat_decision in step.seat_decisions:
            analysis_by_decision[(step.step_ordinal, int(seat_decision.seat))] = (
                seat_decision.decision_trace.analysis
            )

    observations: list[dict[str, object]] = []
    for decision in iter_inspection_decisions(
        inspection, expected_decision_count=result.decisions
    ):
        actor = Seat(decision.actor_seat)
        hidden = hidden_recorder.snapshot(decision.step_ordinal)
        require_same_decision_state(decision.context.input, hidden, actor)

        accepted: list[dict[str, object]] = []
        for opponent in Seat:
            if opponent is actor:
                continue
            public_state = decision.context.input.players[int(opponent)]
            if public_state.riichi is not RiichiState.ACCEPTED:
                continue
            privileged = hidden.seat_state(opponent)
            target = build_wait_shape_target(
                public_riichi=public_state.riichi,
                privileged_riichi_declared=privileged.riichi_declared,
                concealed_tiles=privileged.concealed_tiles,
                melds=privileged.melds,
            )
            accepted.append(
                {
                    "opponent_seat": int(opponent),
                    "availability": target.availability.value,
                    "projection": (
                        None
                        if target.projection is None
                        else _projection_document(target.projection)
                    ),
                }
            )

        if not accepted:
            continue

        selected_index = encode_action(decision.selected_action)
        if (
            resolve_legal_action(selected_index, decision.context)
            is not decision.selected_action
        ):
            raise WaitShapePilotError("teacher action encode/resolve round trip failed")
        discard_indices = [
            encode_action(action)
            for action in decision.context.legal_actions
            if isinstance(action, DiscardAction)
        ]
        if len(discard_indices) != len(set(discard_indices)):
            raise WaitShapePilotError("legal discard vocabulary indices are not unique")

        analysis = analysis_by_decision.get(
            (decision.step_ordinal, decision.actor_seat)
        )
        production_branch = (
            analysis.branch.name
            if isinstance(analysis, TargetedHonorReleaseAnalysis)
            else None
        )

        round_state = decision.context.input.round
        observations.append(
            {
                "seed": seed,
                "round_wind": round_state.round_wind.value,
                "hand_number": round_state.hand_number,
                "honba": round_state.honba,
                "step_ordinal": decision.step_ordinal,
                "decision_ordinal": decision.decision_ordinal,
                "actor_seat": int(actor),
                "teacher_action_index": selected_index,
                "ordinary_discard_choice": is_eligible_ordinary_discard_choice(
                    decision.context.legal_actions
                ),
                "production_defensive_branch": production_branch,
                "legal_discard_indices": discard_indices,
                "accepted_opponents": accepted,
            }
        )

    receipt: dict[str, object] = {
        "seed": seed,
        "game_mode": result.game_mode,
        "steps": result.steps,
        "decisions": result.decisions,
        "scores": list(result.scores),
        "ranks": list(result.ranks),
        "support_observation_count": len(observations),
    }
    return receipt, tuple(observations)


def _validate_game_receipt(record: object, context: str) -> dict[str, object]:
    _require(type(record) is dict, f"{context} must be an object")
    expected = {
        "seed",
        "game_mode",
        "steps",
        "decisions",
        "scores",
        "ranks",
        "support_observation_count",
    }
    _require(set(record) == expected, f"{context} fields are invalid")
    _require(type(record["seed"]) is int, f"{context}.seed must be an int")
    _require(record["seed"] in PILOT_SEEDS, f"{context}.seed is not a pilot seed")
    _require(record["game_mode"] == GAME_MODE, f"{context}.game_mode drifted")
    for name in ("steps", "decisions", "support_observation_count"):
        _require(
            type(record[name]) is int and int(record[name]) >= 0,
            f"{context}.{name} must be a nonnegative int",
        )
    _require(
        int(record["decisions"]) >= int(record["steps"]),
        f"{context}.decisions must be at least steps",
    )
    for name in ("scores", "ranks"):
        values = record[name]
        _require(
            type(values) is list
            and len(values) == 4
            and all(type(value) is int for value in values),
            f"{context}.{name} must contain exactly four ints",
        )
    _require(
        sorted(record["ranks"]) == [1, 2, 3, 4],
        f"{context}.ranks must be a permutation of 1..4",
    )
    return record


def _observation_line(record: dict[str, object]) -> str:
    return (
        json.dumps(
            record,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    )


def _validate_projection(value: object, context: str) -> dict[str, int]:
    _require(type(value) is dict, f"{context} must be an object")
    expected = {*PRIMARY_SHAPES, DESCRIPTIVE_SHAPE}
    _require(set(value) == expected, f"{context} fields are invalid")
    for shape in expected:
        _require(value[shape] in (0, 1), f"{context}.{shape} must be binary")
    _require(any(value[shape] for shape in expected), f"{context} must not be all-zero")
    return value  # type: ignore[return-value]


def _validate_observation(record: object, context: str) -> dict[str, object]:
    _require(type(record) is dict, f"{context} must be an object")
    expected = {
        "seed",
        "round_wind",
        "hand_number",
        "honba",
        "step_ordinal",
        "decision_ordinal",
        "actor_seat",
        "teacher_action_index",
        "ordinary_discard_choice",
        "production_defensive_branch",
        "legal_discard_indices",
        "accepted_opponents",
    }
    _require(set(record) == expected, f"{context} fields are invalid")
    for name in (
        "seed",
        "hand_number",
        "honba",
        "step_ordinal",
        "decision_ordinal",
        "actor_seat",
        "teacher_action_index",
    ):
        _require(type(record[name]) is int, f"{context}.{name} must be an int")
    _require(record["seed"] in PILOT_SEEDS, f"{context}.seed is not a pilot seed")
    _require(0 <= int(record["actor_seat"]) <= 3, f"{context}.actor_seat is invalid")
    _require(
        type(record["round_wind"]) is str and bool(record["round_wind"]),
        f"{context}.round_wind is invalid",
    )
    _require(
        type(record["ordinary_discard_choice"]) is bool,
        f"{context}.ordinary_discard_choice must be bool",
    )
    branch = record["production_defensive_branch"]
    _require(
        branch is None
        or (
            type(branch) is str
            and branch in {member.name for member in TargetedHonorReleaseBranch}
        ),
        f"{context}.production_defensive_branch is invalid",
    )
    discard_indices = record["legal_discard_indices"]
    _require(type(discard_indices) is list, f"{context}.legal_discard_indices invalid")
    _require(
        all(type(index) is int and index >= 0 for index in discard_indices),
        f"{context}.legal_discard_indices must contain nonnegative ints",
    )
    _require(
        len(discard_indices) == len(set(discard_indices)),
        f"{context}.legal_discard_indices contain duplicates",
    )
    if record["ordinary_discard_choice"]:
        _require(
            len(discard_indices) >= 2,
            f"{context} ordinary discard choice has fewer than two candidates",
        )
        _require(
            record["teacher_action_index"] in discard_indices,
            f"{context} ordinary discard choice selected a non-discard action",
        )

    opponents = record["accepted_opponents"]
    _require(
        type(opponents) is list and 1 <= len(opponents) <= 3,
        f"{context}.accepted_opponents must contain 1..3 cells",
    )
    seats: list[int] = []
    for index, cell in enumerate(opponents):
        cell_context = f"{context}.accepted_opponents[{index}]"
        _require(type(cell) is dict, f"{cell_context} must be an object")
        _require(
            set(cell) == {"opponent_seat", "availability", "projection"},
            f"{cell_context} fields are invalid",
        )
        seat = cell["opponent_seat"]
        _require(type(seat) is int and 0 <= seat <= 3, f"{cell_context}.seat invalid")
        _require(seat != record["actor_seat"], f"{cell_context} points to actor")
        seats.append(seat)
        try:
            availability = WaitShapeAvailability(cell["availability"])
        except (TypeError, ValueError) as exc:
            raise WaitShapePilotError(
                f"{cell_context}.availability is invalid"
            ) from exc
        _require(
            availability is not WaitShapeAvailability.NOT_ACCEPTED_RIICHI,
            f"{cell_context} cannot be NOT_ACCEPTED_RIICHI",
        )
        if availability is WaitShapeAvailability.AVAILABLE:
            _validate_projection(cell["projection"], f"{cell_context}.projection")
        else:
            _require(
                cell["projection"] is None,
                f"{cell_context} unavailable target carries a projection",
            )
    _require(len(seats) == len(set(seats)), f"{context} repeats opponent seats")
    return record


def _raw_identity(manifest: dict[str, object]) -> str:
    return _document_identity(manifest, "raw_identity")


def write_raw_artifact(
    destination: str | Path,
    *,
    game_receipts: Iterable[dict[str, object]],
    observations: Iterable[dict[str, object]],
    lock: dict[str, object],
) -> LoadedPilotRaw:
    destination = Path(destination)
    _validate_lock_document(lock)
    if destination.exists():
        raise FileExistsError("raw pilot destination already exists")

    receipts = tuple(game_receipts)
    _require(len(receipts) == len(PILOT_SEEDS), "raw game receipt count must be 96")
    for index, receipt in enumerate(receipts):
        _validate_game_receipt(receipt, f"game_receipt[{index}]")
    _require(
        tuple(int(receipt["seed"]) for receipt in receipts) == PILOT_SEEDS,
        "raw game receipts must exactly cover ordered pilot seeds",
    )

    records = tuple(observations)
    previous_key: tuple[int, int, int, int] | None = None
    for index, record in enumerate(records):
        _validate_observation(record, f"observation[{index}]")
        key = (
            int(record["seed"]),
            int(record["step_ordinal"]),
            int(record["decision_ordinal"]),
            int(record["actor_seat"]),
        )
        if previous_key is not None:
            _require(key > previous_key, "raw observations are not in canonical order")
        previous_key = key

    observed_by_seed = Counter(int(record["seed"]) for record in records)
    for receipt in receipts:
        seed = int(receipt["seed"])
        _require(
            int(receipt["support_observation_count"]) == observed_by_seed[seed],
            f"game receipt support count disagrees for seed {seed}",
        )

    payload = "".join(_observation_line(record) for record in records).encode("utf-8")
    provenance = lock["provenance"]
    manifest: dict[str, object] = {
        "schema_version": RAW_SCHEMA_VERSION,
        "protocol_lock_identity": EXPECTED_PROTOCOL_LOCK_IDENTITY,
        "lock_identity": lock["lock_identity"],
        "ordered_seeds": list(PILOT_SEEDS),
        "game_receipts": list(receipts),
        "teacher": {
            "identity": TEACHER_IDENTITY,
            "policy_class": TEACHER_POLICY_CLASS,
            "population": TEACHER_POPULATION,
            "lisjong_revision": TEACHER_LISJONG_REVISION,
        },
        "runtime": {
            "game_mode": GAME_MODE,
            "lisjong_engine_revision": LISJONG_ENGINE_REVISION,
            "riichienv_version": RIICHIENV_VERSION,
            "canonical_wait_implementation_identity": (
                exact_wait_implementation_identity()
            ),
        },
        "provenance": provenance,
        "observation_count": len(records),
        "file": {
            "name": OBSERVATIONS_FILENAME,
            "bytes": len(payload),
            "sha256": hashlib.sha256(payload).hexdigest(),
        },
    }
    manifest["raw_identity"] = _raw_identity(manifest)

    destination.parent.mkdir(parents=True, exist_ok=True)
    with staged_artifact_directory(destination) as staging:
        (staging / OBSERVATIONS_FILENAME).write_bytes(payload)
        (staging / RAW_MANIFEST_FILENAME).write_text(
            canonical_json_text(manifest), encoding="utf-8", newline="\n"
        )
    return load_raw_artifact(destination)


def load_raw_artifact(path: str | Path) -> LoadedPilotRaw:
    path = Path(path)
    _require(path.is_dir(), "raw pilot path is not a directory")
    _require(
        {item.name for item in path.iterdir()}
        == {RAW_MANIFEST_FILENAME, OBSERVATIONS_FILENAME},
        "raw pilot directory contains missing or extra files",
    )
    manifest_text = (path / RAW_MANIFEST_FILENAME).read_text(encoding="utf-8")
    try:
        manifest = parse_json_text(manifest_text)
    except json.JSONDecodeError as exc:
        raise WaitShapePilotError("raw manifest is malformed") from exc
    _require(type(manifest) is dict, "raw manifest must be an object")
    expected = {
        "schema_version",
        "protocol_lock_identity",
        "lock_identity",
        "ordered_seeds",
        "game_receipts",
        "teacher",
        "runtime",
        "provenance",
        "observation_count",
        "file",
        "raw_identity",
    }
    _require(set(manifest) == expected, "raw manifest fields are invalid")
    _require(manifest["schema_version"] == RAW_SCHEMA_VERSION, "unsupported raw schema")
    _require(
        manifest["protocol_lock_identity"] == EXPECTED_PROTOCOL_LOCK_IDENTITY,
        "raw protocol identity drifted",
    )
    _require(manifest["ordered_seeds"] == list(PILOT_SEEDS), "raw seed plan drifted")
    _require(
        manifest["teacher"]
        == {
            "identity": TEACHER_IDENTITY,
            "policy_class": TEACHER_POLICY_CLASS,
            "population": TEACHER_POPULATION,
            "lisjong_revision": TEACHER_LISJONG_REVISION,
        },
        "raw teacher binding drifted",
    )
    _require(
        manifest["runtime"]
        == {
            "game_mode": GAME_MODE,
            "lisjong_engine_revision": LISJONG_ENGINE_REVISION,
            "riichienv_version": RIICHIENV_VERSION,
            "canonical_wait_implementation_identity": EXPECTED_CANONICAL_WAIT_IDENTITY,
        },
        "raw runtime binding drifted",
    )
    _require(
        type(manifest["provenance"]) is dict,
        "raw provenance must be an object",
    )
    game_receipts = manifest["game_receipts"]
    _require(
        type(game_receipts) is list and len(game_receipts) == len(PILOT_SEEDS),
        "raw game receipts must contain exactly 96 entries",
    )
    for index, receipt in enumerate(game_receipts):
        _validate_game_receipt(receipt, f"manifest.game_receipts[{index}]")
    _require(
        tuple(int(receipt["seed"]) for receipt in game_receipts) == PILOT_SEEDS,
        "raw manifest game receipts do not cover exact ordered pilot seeds",
    )
    _require(
        manifest["raw_identity"] == _raw_identity(manifest),
        "raw identity mismatch",
    )
    _require(
        manifest_text == canonical_json_text(manifest), "raw manifest not canonical"
    )

    file_doc = manifest["file"]
    _require(
        type(file_doc) is dict
        and set(file_doc) == {"name", "bytes", "sha256"}
        and file_doc["name"] == OBSERVATIONS_FILENAME,
        "raw file metadata is invalid",
    )
    payload = (path / OBSERVATIONS_FILENAME).read_bytes()
    _require(file_doc["bytes"] == len(payload), "raw byte count mismatch")
    _require(
        file_doc["sha256"] == hashlib.sha256(payload).hexdigest(),
        "raw payload digest mismatch",
    )
    text = payload.decode("utf-8")
    _require(not text or text.endswith("\n"), "raw JSONL must end with newline")

    records: list[dict[str, object]] = []
    previous_key: tuple[int, int, int, int] | None = None
    for index, line in enumerate(text.splitlines()):
        try:
            record = parse_json_text(line)
        except json.JSONDecodeError as exc:
            raise WaitShapePilotError("raw JSONL contains malformed JSON") from exc
        validated = _validate_observation(record, f"observation[{index}]")
        key = (
            int(validated["seed"]),
            int(validated["step_ordinal"]),
            int(validated["decision_ordinal"]),
            int(validated["actor_seat"]),
        )
        _require(
            previous_key is None or key > previous_key,
            "raw observations are not in canonical order",
        )
        previous_key = key
        records.append(validated)
    _require(
        manifest["observation_count"] == len(records),
        "raw observation count mismatch",
    )
    observed_by_seed = Counter(int(record["seed"]) for record in records)
    for receipt in game_receipts:
        seed = int(receipt["seed"])
        _require(
            int(receipt["support_observation_count"]) == observed_by_seed[seed],
            f"raw manifest game receipt support count disagrees for seed {seed}",
        )
    return LoadedPilotRaw(path=path, manifest=manifest, observations=tuple(records))


def _episode_key(record: dict[str, object], opponent_seat: int) -> tuple[object, ...]:
    return (
        record["seed"],
        record["round_wind"],
        record["hand_number"],
        record["honba"],
        opponent_seat,
    )


def _histogram(values: Iterable[int]) -> dict[str, int]:
    counts = Counter(values)
    return {str(key): counts[key] for key in sorted(counts)}


def _safe_share(numerator: int, denominator: int) -> float:
    return 0.0 if denominator == 0 else numerator / denominator


def summarize_f1(raw: LoadedPilotRaw) -> dict[str, object]:
    """Derive the exact prelocked F1 support report from immutable raw observations."""

    accepted_cells = 0
    labelled_cells = 0
    unavailable = {
        availability.value: 0
        for availability in WaitShapeAvailability
        if availability is not WaitShapeAvailability.AVAILABLE
    }
    episode_rows: dict[tuple[object, ...], list[dict[str, int] | None]] = defaultdict(
        list
    )
    episode_seeds: dict[tuple[object, ...], int] = {}
    episode_first_availability: dict[tuple[object, ...], WaitShapeAvailability] = {}

    all_six_zero_rows = 0
    ordinary_zero_kokushi_rows = 0
    multi_positive_rows = 0
    kokushi_rows = 0

    for record in raw.observations:
        for cell in record["accepted_opponents"]:  # type: ignore[index]
            accepted_cells += 1
            availability = WaitShapeAvailability(cell["availability"])
            episode = _episode_key(record, int(cell["opponent_seat"]))
            episode_seeds[episode] = int(record["seed"])
            episode_first_availability.setdefault(episode, availability)
            if availability is WaitShapeAvailability.AVAILABLE:
                projection = cell["projection"]
                assert isinstance(projection, dict)
                labels = {
                    shape: int(projection[shape])
                    for shape in (*PRIMARY_SHAPES, DESCRIPTIVE_SHAPE)
                }
                labelled_cells += 1
                episode_rows[episode].append(labels)
                ordinary_positive = sum(labels[shape] for shape in PRIMARY_SHAPES)
                six_positive = ordinary_positive + labels[DESCRIPTIVE_SHAPE]
                multi_positive_rows += int(six_positive > 1)
                ordinary_zero_kokushi_rows += int(
                    ordinary_positive == 0 and labels[DESCRIPTIVE_SHAPE] == 1
                )
                kokushi_rows += labels[DESCRIPTIVE_SHAPE]
            else:
                unavailable[availability.value] += 1
                episode_rows[episode].append(None)
                all_six_zero_rows += int(
                    availability is WaitShapeAvailability.NO_STRUCTURAL_WAIT
                )

    # The live recorder always owns the technical hidden state; binding/hand/meld
    # absence are the only reasons a public ACCEPTED cell is not technically
    # labelable before canonical semantic validation.
    technically_unlabelable = sum(
        unavailable[name]
        for name in (
            WaitShapeAvailability.RIICHI_BINDING_MISMATCH.value,
            WaitShapeAvailability.HIDDEN_HAND_UNAVAILABLE.value,
            WaitShapeAvailability.MELD_STATE_UNAVAILABLE.value,
        )
    )
    technically_labelable = accepted_cells - technically_unlabelable
    labelable_fraction = _safe_share(labelled_cells, technically_labelable)

    # Support anchors are the first AVAILABLE labelled observation per episode.
    # The integrity zero-channel anchor is classified from the first observation's
    # exact availability reason, so unrelated fail-closed reasons are not
    # misreported as NO_STRUCTURAL_WAIT.
    support_anchors: dict[tuple[object, ...], dict[str, int]] = {}
    for episode, rows in episode_rows.items():
        for row in rows:
            if row is not None:
                support_anchors[episode] = row
                break

    all_six_zero_anchors = sum(
        availability is WaitShapeAvailability.NO_STRUCTURAL_WAIT
        for availability in episode_first_availability.values()
    )
    ordinary_zero_kokushi_anchors = sum(
        1
        for value in support_anchors.values()
        if sum(value[shape] for shape in PRIMARY_SHAPES) == 0
        and value[DESCRIPTIVE_SHAPE] == 1
    )
    kokushi_anchor_episodes = sum(
        value[DESCRIPTIVE_SHAPE] for value in support_anchors.values()
    )
    anchor_positive_shape_count_distribution = _histogram(
        sum(value[shape] for shape in PRIMARY_SHAPES)
        for value in support_anchors.values()
    )

    shapes: dict[str, object] = {}
    for shape in PRIMARY_SHAPES:
        positive_rows_by_episode: Counter[tuple[object, ...]] = Counter()
        negative_rows_by_episode: Counter[tuple[object, ...]] = Counter()
        positive_rows = 0
        negative_rows = 0
        for episode, rows in episode_rows.items():
            for labels in rows:
                if labels is None:
                    continue
                if labels[shape]:
                    positive_rows += 1
                    positive_rows_by_episode[episode] += 1
                else:
                    negative_rows += 1
                    negative_rows_by_episode[episode] += 1

        positive_anchor_episodes = {
            episode for episode, labels in support_anchors.items() if labels[shape] == 1
        }
        negative_anchor_episodes = {
            episode for episode, labels in support_anchors.items() if labels[shape] == 0
        }
        anchor_total = len(positive_anchor_episodes) + len(negative_anchor_episodes)
        anchor_prevalence = _safe_share(len(positive_anchor_episodes), anchor_total)
        positive_hanchan = {
            episode_seeds[episode] for episode in positive_anchor_episodes
        }
        negative_hanchan = {
            episode_seeds[episode] for episode in negative_anchor_episodes
        }
        max_positive_concentration = _safe_share(
            max(positive_rows_by_episode.values(), default=0), positive_rows
        )
        max_negative_concentration = _safe_share(
            max(negative_rows_by_episode.values(), default=0), negative_rows
        )
        passed = (
            len(positive_anchor_episodes) >= F1_MIN_POSITIVE_ANCHOR_EPISODES
            and len(negative_anchor_episodes) >= F1_MIN_NEGATIVE_ANCHOR_EPISODES
            and len(positive_hanchan) >= F1_MIN_POSITIVE_SOURCE_HANCHAN
            and len(negative_hanchan) >= F1_MIN_NEGATIVE_SOURCE_HANCHAN
            and F1_MIN_ANCHOR_PREVALENCE
            <= anchor_prevalence
            <= F1_MAX_ANCHOR_PREVALENCE
            and max_positive_concentration <= F1_MAX_SINGLE_EPISODE_ROW_CONCENTRATION
            and max_negative_concentration <= F1_MAX_SINGLE_EPISODE_ROW_CONCENTRATION
        )
        shapes[shape] = {
            "positive_rows": positive_rows,
            "negative_rows": negative_rows,
            "row_prevalence": _safe_share(positive_rows, positive_rows + negative_rows),
            "positive_anchor_episodes": len(positive_anchor_episodes),
            "negative_anchor_episodes": len(negative_anchor_episodes),
            "anchor_prevalence": anchor_prevalence,
            "positive_source_hanchan": len(positive_hanchan),
            "negative_source_hanchan": len(negative_hanchan),
            "maximum_positive_row_share_from_one_episode": max_positive_concentration,
            "maximum_negative_row_share_from_one_episode": max_negative_concentration,
            "qualified": passed,
        }

    unexpected_fail_closed = sum(unavailable.values())
    integrity_pass = (
        all_six_zero_anchors == 0
        and unexpected_fail_closed == 0
        and technically_labelable > 0
        and labelable_fraction >= F1_MIN_LABELABLE_FRACTION
    )
    all_shapes_pass = all(bool(shapes[shape]["qualified"]) for shape in PRIMARY_SHAPES)  # type: ignore[index]
    if not integrity_pass:
        outcome = STOP_INVALID
    elif all_shapes_pass:
        outcome = F1_QUALIFIED
    else:
        outcome = F1_NOT_QUALIFIED

    summary = {
        "source_hanchan_count": len(PILOT_SEEDS),
        "accepted_riichi_opponent_cells": accepted_cells,
        "unavailable_accepted_riichi_cells": accepted_cells - labelled_cells,
        "technically_labelable_accepted_riichi_cells": technically_labelable,
        "eligible_labelled_accepted_riichi_cells": labelled_cells,
        "labelable_fraction": labelable_fraction,
        "unavailable_reason_counts": unavailable,
        "unexpected_fail_closed_or_semantic_error_count": unexpected_fail_closed,
        "unique_riichi_episodes": len(episode_rows),
        "rows_per_riichi_episode_distribution": _histogram(
            len(rows) for rows in episode_rows.values()
        ),
        "label_repetitions_per_riichi_episode_distribution": _histogram(
            sum(row is not None for row in rows) for rows in episode_rows.values()
        ),
        "shapes": shapes,
        "multi_positive_rows": multi_positive_rows,
        "anchor_positive_shape_count_distribution": anchor_positive_shape_count_distribution,
        "all_six_channel_zero_rows": all_six_zero_rows,
        "all_six_channel_zero_anchors": all_six_zero_anchors,
        "ordinary_five_all_zero_kokushi_positive_rows": ordinary_zero_kokushi_rows,
        "ordinary_five_all_zero_kokushi_positive_anchors": ordinary_zero_kokushi_anchors,
        "kokushi_descriptive_positive_rows": kokushi_rows,
        "kokushi_descriptive_positive_anchor_episodes": kokushi_anchor_episodes,
        "all_five_primary_shapes_pass": all_shapes_pass,
        "integrity_pass": integrity_pass,
    }
    document: dict[str, object] = {
        "schema_version": F1_SCHEMA_VERSION,
        "protocol_lock_identity": EXPECTED_PROTOCOL_LOCK_IDENTITY,
        "raw_identity": raw.identity,
        "summary": summary,
        "outcome": outcome,
    }
    document["result_identity"] = _document_identity(document, "result_identity")
    return document


def summarize_f2(raw: LoadedPilotRaw) -> dict[str, object]:
    """Derive the exact prelocked F2 public action-support report."""

    rows = [
        record
        for record in raw.observations
        if bool(record["ordinary_discard_choice"])
        and len(record["accepted_opponents"]) >= 1  # type: ignore[arg-type]
    ]
    source_hanchan = {int(record["seed"]) for record in rows}
    episodes = {
        _episode_key(record, int(cell["opponent_seat"]))
        for record in rows
        for cell in record["accepted_opponents"]  # type: ignore[index]
    }
    selected = Counter(int(record["teacher_action_index"]) for record in rows)
    candidate_counts = Counter(
        len(record["legal_discard_indices"])
        for record in rows  # type: ignore[arg-type]
    )
    branch_counts = Counter(
        str(record["production_defensive_branch"])
        for record in rows
        if record["production_defensive_branch"] is not None
    )
    branch_unavailable_count = sum(
        record["production_defensive_branch"] is None for record in rows
    )
    multiple_riichi = sum(
        len(record["accepted_opponents"]) >= 2
        for record in rows  # type: ignore[arg-type]
    )
    decision_count = len(rows)
    largest_share = _safe_share(max(selected.values(), default=0), decision_count)

    passed = (
        decision_count >= F2_MIN_DISCARD_CHOICE_DECISIONS
        and len(source_hanchan) >= F2_MIN_SOURCE_HANCHAN
        and len(episodes) >= F2_MIN_RIICHI_EPISODES
        and len(selected) >= F2_MIN_SELECTED_DISCARD_INDICES
        and largest_share <= F2_MAX_SELECTED_DISCARD_INDEX_SHARE
    )
    outcome = F2_QUALIFIED if passed else F2_NOT_QUALIFIED
    summary = {
        "riichi_exposed_discard_choice_decisions": decision_count,
        "unique_source_hanchan": len(source_hanchan),
        "unique_accepted_riichi_episodes": len(episodes),
        "teacher_selected_discard_action_index_histogram": {
            str(index): selected[index] for index in sorted(selected)
        },
        "unique_selected_discard_vocabulary_indices": len(selected),
        "largest_selected_discard_index_share": largest_share,
        "legal_discard_candidate_count_distribution": {
            str(count): candidate_counts[count] for count in sorted(candidate_counts)
        },
        "multiple_riichi_opponent_count": multiple_riichi,
        "defense_diagnostic_status": DEFENSE_DIAGNOSTIC_STATUS,
        "production_defensive_branch_counts": {
            branch: branch_counts[branch] for branch in sorted(branch_counts)
        },
        "production_defensive_branch_unavailable_count": branch_unavailable_count,
        "qualified": passed,
    }
    document: dict[str, object] = {
        "schema_version": F2_SCHEMA_VERSION,
        "protocol_lock_identity": EXPECTED_PROTOCOL_LOCK_IDENTITY,
        "raw_identity": raw.identity,
        "summary": summary,
        "outcome": outcome,
    }
    document["result_identity"] = _document_identity(document, "result_identity")
    return document


def qualification_document(
    raw: LoadedPilotRaw,
    f1: dict[str, object],
    f2: dict[str, object],
    lock: dict[str, object],
) -> dict[str, object]:
    if f1["outcome"] == STOP_INVALID or f2["outcome"] == STOP_INVALID:
        outcome = STOP_INVALID
    elif f1["outcome"] != F1_QUALIFIED:
        outcome = F1_NOT_QUALIFIED
    elif f2["outcome"] != F2_QUALIFIED:
        outcome = F2_NOT_QUALIFIED
    else:
        outcome = FINAL_QUALIFIED

    document: dict[str, object] = {
        "schema_version": QUALIFICATION_SCHEMA_VERSION,
        "protocol_lock_identity": EXPECTED_PROTOCOL_LOCK_IDENTITY,
        "lock_identity": lock["lock_identity"],
        "raw_identity": raw.identity,
        "f0": {
            "outcome": F0_OUTCOME,
            "artifact_sha256": F0_ARTIFACT_SHA256,
        },
        "f1": {
            "outcome": f1["outcome"],
            "result_identity": f1["result_identity"],
        },
        "f2": {
            "outcome": f2["outcome"],
            "result_identity": f2["result_identity"],
        },
        "scientific_population_generated": False,
        "am_training_performed": False,
        "outcome": outcome,
    }
    document["result_identity"] = _document_identity(document, "result_identity")
    return document


def _write_result(path: Path, document: dict[str, object]) -> None:
    _require(
        document.get("result_identity")
        == _document_identity(document, "result_identity"),
        "result document identity mismatch before write",
    )
    write_new_artifact_file(path, canonical_json_text(document))


def _load_result(path: Path, schema: str) -> dict[str, object]:
    try:
        document = read_json_document(path)
    except (OSError, json.JSONDecodeError, ArtifactValidationError) as exc:
        raise WaitShapePilotError(f"{path.name} is malformed") from exc
    _require(type(document) is dict, f"{path.name} must be an object")
    _require(document.get("schema_version") == schema, f"{path.name} schema drifted")
    _require(
        document.get("protocol_lock_identity") == EXPECTED_PROTOCOL_LOCK_IDENTITY,
        f"{path.name} protocol identity drifted",
    )
    _require(
        document.get("result_identity")
        == _document_identity(document, "result_identity"),
        f"{path.name} result identity mismatch",
    )
    _require(
        path.read_text(encoding="utf-8") == canonical_json_text(document),
        f"{path.name} is not canonical JSON",
    )
    return document


def verify_output_root(output_root: str | Path) -> dict[str, object]:
    root = Path(output_root)
    destinations = _destinations(root)
    lock = _load_lock(destinations["lock"])
    raw = load_raw_artifact(destinations["raw"])
    _require(
        raw.manifest["lock_identity"] == lock["lock_identity"], "raw/lock mismatch"
    )
    _require(
        raw.manifest["provenance"] == lock["provenance"],
        "raw provenance differs from the execution lock",
    )
    recorded_f1 = _load_result(destinations["f1"], F1_SCHEMA_VERSION)
    recorded_f2 = _load_result(destinations["f2"], F2_SCHEMA_VERSION)
    recorded_qualification = _load_result(
        destinations["qualification"], QUALIFICATION_SCHEMA_VERSION
    )

    expected_f1 = summarize_f1(raw)
    expected_f2 = summarize_f2(raw)
    expected_qualification = qualification_document(raw, expected_f1, expected_f2, lock)
    _require(
        canonical_json_text(recorded_f1) == canonical_json_text(expected_f1),
        "F1 artifact does not rederive from raw evidence",
    )
    _require(
        canonical_json_text(recorded_f2) == canonical_json_text(expected_f2),
        "F2 artifact does not rederive from raw evidence",
    )
    _require(
        canonical_json_text(recorded_qualification)
        == canonical_json_text(expected_qualification),
        "qualification result does not rederive from F0/F1/F2 evidence",
    )
    return {
        "lock_identity": lock["lock_identity"],
        "raw_identity": raw.identity,
        "f1_result_identity": recorded_f1["result_identity"],
        "f1_outcome": recorded_f1["outcome"],
        "f2_result_identity": recorded_f2["result_identity"],
        "f2_outcome": recorded_f2["outcome"],
        "qualification_result_identity": recorded_qualification["result_identity"],
        "outcome": recorded_qualification["outcome"],
    }


def _collect_observations(
    max_workers: int,
) -> tuple[tuple[dict[str, object], ...], tuple[dict[str, object], ...]]:
    if max_workers == 1:
        per_seed = [_run_seed(seed) for seed in PILOT_SEEDS]
    else:
        with ProcessPoolExecutor(max_workers=max_workers) as executor:
            per_seed = list(executor.map(_run_seed, PILOT_SEEDS))
    receipts = tuple(receipt for receipt, _ in per_seed)
    observations = tuple(
        record for _, seed_records in per_seed for record in seed_records
    )
    return receipts, observations


def run_pilot(
    output_root: str | Path,
    *,
    max_workers: int,
    repository_collision_audit_pass: bool,
    private_collision_audit_pass: bool,
    no_prior_result_exposure_confirmed: bool,
) -> dict[str, object]:
    """Lock, generate exactly 96 pilot hanchan once, derive F1/F2, and verify."""

    root = Path(output_root)
    lock = build_execution_lock(
        root,
        max_workers=max_workers,
        repository_collision_audit_pass=repository_collision_audit_pass,
        private_collision_audit_pass=private_collision_audit_pass,
        no_prior_result_exposure_confirmed=no_prior_result_exposure_confirmed,
    )
    root.mkdir(parents=True, exist_ok=True)
    destinations = _destinations(root)
    _write_lock(lock, destinations["lock"])

    game_receipts, observations = _collect_observations(max_workers)
    raw = write_raw_artifact(
        destinations["raw"],
        game_receipts=game_receipts,
        observations=observations,
        lock=lock,
    )
    f1 = summarize_f1(raw)
    f2 = summarize_f2(raw)
    qualification = qualification_document(raw, f1, f2, lock)
    _write_result(destinations["f1"], f1)
    _write_result(destinations["f2"], f2)
    _write_result(destinations["qualification"], qualification)
    return verify_output_root(root)


def preflight(
    output_root: str | Path,
    *,
    max_workers: int,
    repository_collision_audit_pass: bool,
    private_collision_audit_pass: bool,
    no_prior_result_exposure_confirmed: bool,
) -> dict[str, object]:
    lock = build_execution_lock(
        output_root,
        max_workers=max_workers,
        repository_collision_audit_pass=repository_collision_audit_pass,
        private_collision_audit_pass=private_collision_audit_pass,
        no_prior_result_exposure_confirmed=no_prior_result_exposure_confirmed,
    )
    return {
        "status": "PASS",
        "protocol_lock_identity": lock["protocol_lock_identity"],
        "arena_revision": lock["execution_target"]["revision"],  # type: ignore[index]
        "ordered_seeds": lock["pilot"]["ordered_seeds"],  # type: ignore[index]
        "teacher_identity": TEACHER_IDENTITY,
        "max_workers": max_workers,
        "billable_resource_created": False,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("preflight", "run"):
        child = sub.add_parser(name)
        child.add_argument("--output-root", required=True)
        child.add_argument("--max-workers", type=int, default=2)
        child.add_argument("--repository-collision-audit-pass", action="store_true")
        child.add_argument("--private-collision-audit-pass", action="store_true")
        child.add_argument("--no-prior-result-exposure-confirmed", action="store_true")
    verify = sub.add_parser("verify")
    verify.add_argument("--output-root", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "verify":
        result = verify_output_root(args.output_root)
    elif args.command == "preflight":
        result = preflight(
            args.output_root,
            max_workers=args.max_workers,
            repository_collision_audit_pass=args.repository_collision_audit_pass,
            private_collision_audit_pass=args.private_collision_audit_pass,
            no_prior_result_exposure_confirmed=args.no_prior_result_exposure_confirmed,
        )
    else:
        result = run_pilot(
            args.output_root,
            max_workers=args.max_workers,
            repository_collision_audit_pass=args.repository_collision_audit_pass,
            private_collision_audit_pass=args.private_collision_audit_pass,
            no_prior_result_exposure_confirmed=args.no_prior_result_exposure_confirmed,
        )
    print(canonical_json_text(result), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
