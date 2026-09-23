"""L0.3 B focal outcome source producer（#359、#79 A1 / A3 / A4）。

Arenaが所有する新しいversioned source contract
``arena-offense-l0.3-focal-outcome-source-v1``を生成・検証する。Arenaは事実
だけを記録し、``target_q``の算出、training、paired evaluationは行わない。

```text
対局構成（A4）
  focal seat      = source_game_ordinal % 4
                    lisjong select_residual_exploration + Arena exploration token
  other 3 seats   lisjong ConstantResidualRuntime（game / seatごとにfresh）
  game_ordinal    source全体で0..N-1。SCIENTIFICのTRAIN / SELECT境界でもrestartしない

wire layout
  <root>/
    manifest.json               sealed canonical JSON
    game-NNN/kyokus.jsonl        全kyoku row（canonical JSON line）
    game-NNN/focal-decisions.jsonl  focal seatの全decision row
```

pinned lisjong ``PINNED_LISJONG_REVISION``の``outcome_source.py``がこのwire
formatのstrict consumerである。``verify_focal_outcome_source()``はそのconsumerで
strict readしたうえで、Arena-owned audit（exploration tokenの再導出、保存則、
hanchan最終調整の差分、source_contract / population provenance）を再検証する。

既存#342 player-safe source record（v1 / v2）、``RoundResult``、durable local
game recordは変更しない。
"""

import bisect
import hashlib
import json
import shutil
import subprocess
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from importlib import metadata
from pathlib import Path

from lisjong.learning import (
    CONSTANT_RESIDUAL_RUNTIME_IDENTITY,
    RESIDUAL_EXPLORATION_BEHAVIOR_IDENTITY,
    ConstantResidualRuntime,
    OutcomeSourceError,
    UnsupportedSourceSchemaError,
    read_outcome_source,
)
from lisjong.policy_contract import Seat

from lisjong_arena import seed_registry
from lisjong_arena._artifact_io import write_new_artifact_file
from lisjong_arena.durable_local_game_record import (
    _action_to_value,
    _policy_input_to_value,
)
from lisjong_arena.environment_identity import verify_environment
from lisjong_arena.offense_foundation.qualification import (
    read_document,
    seal,
    unseal,
    write_document,
)
from lisjong_arena.offense_foundation.semantics import OffenseError
from lisjong_arena.riichienv.local_game_runner import (
    LocalGameInspection,
    LocalGameInspectionRecorder,
    LocalGameRunner,
)

from .accounting import (
    FocalOutcomeSourceError,
    KyokuAccount,
    account_game,
    require_conservation,
    require_final_adjustment,
)
from .adapter import FocalExplorationPolicy, FocalSelectionCapture
from .exploration_token import EXPLORATION_TOKEN_IDENTITY, exploration_token

OUTCOME_SOURCE_SCHEMA = "arena-offense-l0.3-focal-outcome-source-v1"
OUTCOME_SOURCE_KIND = "focal-outcome-source-record"
FOCAL_ROTATION_RULE = "lisjong-arena-l0.3-focal-seat-game-ordinal-mod-4-v1"
PINNED_LISJONG_REVISION = "aed9c840bc120471e557fc0c8444965c0b81a9c3"
"""このproducerが準拠するlisjong consumer（#193 / PR #194のmerge revision）。"""

GAME_MODE = "4p-red-half"
BACKEND_NAME = "riichienv"

MANIFEST_FILENAME = "manifest.json"
KYOKU_PAYLOAD_FILENAME = "kyokus.jsonl"
DECISION_PAYLOAD_FILENAME = "focal-decisions.jsonl"

CALIBRATION_ROLE = "CALIBRATION"
SCIENTIFIC_ROLE = "SCIENTIFIC"
ROLE_SPLITS = {
    CALIBRATION_ROLE: frozenset({"CALIBRATION"}),
    SCIENTIFIC_ROLE: frozenset({"TRAIN", "SELECT"}),
}

BEHAVIOR = {
    "baseline_runtime_identity": CONSTANT_RESIDUAL_RUNTIME_IDENTITY,
    "exploration_behavior_identity": RESIDUAL_EXPLORATION_BEHAVIOR_IDENTITY,
    "exploration_token_identity": EXPLORATION_TOKEN_IDENTITY,
    "focal_rotation_rule": FOCAL_ROTATION_RULE,
}

_SOURCE_CONTRACT_FIELDS = frozenset(
    {"arena_revision", "backend", "dependencies", "game_mode", "python"}
)


def focal_seat_for(game_ordinal: int) -> Seat:
    """``FOCAL_ROTATION_RULE``: global game ordinalの4剰余。"""
    if type(game_ordinal) is not int or game_ordinal < 0:
        raise FocalOutcomeSourceError("game_ordinal must be a non-negative int")
    return Seat(game_ordinal % 4)


def _canonical_line(value: object) -> str:
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


def _file_info(path: Path) -> dict[str, object]:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return {"bytes": path.stat().st_size, "sha256": digest.hexdigest()}


# ---------------------------------------------------------------------------
# population / provenance
# ---------------------------------------------------------------------------


def validate_population(
    population_role: object,
    games: object,
    allocation_bindings: object,
) -> tuple[tuple[int, str], ...]:
    """source populationをexecution前にfail closedで検証する。

    ``games``は``game_ordinal``順の``(seed, split)``列である。ordinalは列の
    indexそのものであり、split境界でrestartしない。
    """
    if population_role not in ROLE_SPLITS:
        raise FocalOutcomeSourceError("unsupported population_role")
    if isinstance(games, (str, bytes)) or not isinstance(games, Sequence):
        raise FocalOutcomeSourceError("games must be a sequence of (seed, split)")
    normalized: list[tuple[int, str]] = []
    seeds_by_split: dict[str, list[int]] = {}
    for item in games:
        if not isinstance(item, (tuple, list)) or len(item) != 2:
            raise FocalOutcomeSourceError("each game must be a (seed, split) pair")
        seed, split = item
        if type(seed) is not int or not 0 <= seed < 2**32:
            raise FocalOutcomeSourceError("seed must be an unsigned 32-bit int")
        if split not in ROLE_SPLITS[population_role]:
            raise FocalOutcomeSourceError(
                f"split {split!r} is not allowed for population role "
                f"{population_role!r}"
            )
        normalized.append((seed, split))
        seeds_by_split.setdefault(split, []).append(seed)
    if not normalized:
        raise FocalOutcomeSourceError("source population must not be empty")
    if len({seed for seed, _ in normalized}) != len(normalized):
        raise FocalOutcomeSourceError("source population must not reuse a seed")
    if type(allocation_bindings) is not dict or set(allocation_bindings) != set(
        seeds_by_split
    ):
        raise FocalOutcomeSourceError(
            "allocation_bindings must exactly match the population splits"
        )
    for split, seeds in seeds_by_split.items():
        try:
            binding = seed_registry.validate_binding_shape(
                allocation_bindings[split], seeds=seeds
            )
        except seed_registry.SeedRegistryError as error:
            raise FocalOutcomeSourceError(
                f"invalid {split} allocation binding: {error}"
            ) from error
        if binding["seed_domain"] != seed_registry.RIICHIENV_HALF_HANCHAN_SEED_DOMAIN:
            raise FocalOutcomeSourceError(
                f"{split} allocation binding uses the wrong seed_domain"
            )
    return tuple(normalized)


def validate_source_contract(source_contract: object) -> dict[str, object]:
    """Arena-owned ``source_contract`` objectのshapeとpinを検証する。"""
    if type(source_contract) is not dict or set(source_contract) != (
        _SOURCE_CONTRACT_FIELDS
    ):
        raise FocalOutcomeSourceError("source_contract fields do not match v1")
    arena_revision = source_contract["arena_revision"]
    if type(arena_revision) is not str or not arena_revision:
        raise FocalOutcomeSourceError("source_contract.arena_revision is invalid")
    dependencies = source_contract["dependencies"]
    if (
        type(dependencies) is not dict
        or dependencies.get("lisjong") != PINNED_LISJONG_REVISION
    ):
        raise FocalOutcomeSourceError(
            "source_contract must record the pinned lisjong revision"
        )
    backend = source_contract["backend"]
    if (
        type(backend) is not dict
        or set(backend) != {"name", "version"}
        or backend["name"] != BACKEND_NAME
        or type(backend["version"]) is not str
        or not backend["version"]
    ):
        raise FocalOutcomeSourceError("source_contract.backend is invalid")
    if source_contract["game_mode"] != GAME_MODE:
        raise FocalOutcomeSourceError("source_contract.game_mode mismatch")
    if type(source_contract["python"]) is not str or not source_contract["python"]:
        raise FocalOutcomeSourceError("source_contract.python is invalid")
    return source_contract


def build_source_contract(project: str | Path = "pyproject.toml") -> dict[str, object]:
    """clean checkoutと検証済みinstalled環境からsource_contractを作る。"""
    project = Path(project).resolve()
    check = verify_environment(project)
    if check.errors:
        raise FocalOutcomeSourceError("STOP / INVALID: " + "; ".join(check.errors))

    def git(*args: str) -> str:
        return subprocess.check_output(
            ["git", "-C", str(project.parent), *args], text=True
        ).strip()

    if git("status", "--porcelain", "--untracked-files=no") or git(
        "status", "--porcelain", "--untracked-files=normal", "--", "src"
    ):
        raise FocalOutcomeSourceError(
            "STOP / INVALID: tracked source changes must be committed"
        )
    return validate_source_contract(
        {
            "arena_revision": git("rev-parse", "HEAD"),
            "backend": {
                "name": BACKEND_NAME,
                "version": metadata.version(BACKEND_NAME),
            },
            "dependencies": {
                identity.name: identity.revision for identity in check.identities
            },
            "game_mode": GAME_MODE,
            "python": ".".join(map(str, sys.version_info[:3])),
        }
    )


# ---------------------------------------------------------------------------
# execution
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class FocalGameExecution:
    """1 hanchanの実行結果と、focal adapterが同じ実行でcaptureした監査値。"""

    seed: int
    focal_seat: Seat
    inspection: LocalGameInspection
    captures: tuple[FocalSelectionCapture, ...]


def build_game_policies(
    *, seed: int, focal_seat: Seat
) -> tuple[dict[Seat, object], FocalExplorationPolicy]:
    """1 game分のfresh Policy割当を作る。探索するseatはfocal seatだけである。"""
    runtime = ConstantResidualRuntime()
    adapter = FocalExplorationPolicy(game_seed=seed, focal_seat=focal_seat)
    policies = {
        seat: adapter if seat == focal_seat else runtime.create_policy()
        for seat in Seat
    }
    return policies, adapter


def run_focal_game(
    *, seed: int, focal_seat: Seat, max_steps: int | None = None
) -> FocalGameExecution:
    """既存``LocalGameRunner``で1 hanchanを実行する（単一game実行境界）。"""
    policies, adapter = build_game_policies(seed=seed, focal_seat=focal_seat)
    recorder = LocalGameInspectionRecorder()
    LocalGameRunner(
        policies,
        seed=seed,
        game_mode=GAME_MODE,
        max_steps=max_steps,
        inspection_recorder=recorder,
    ).run()
    return FocalGameExecution(
        seed=seed,
        focal_seat=focal_seat,
        inspection=recorder.snapshot(),
        captures=adapter.captures,
    )


# ---------------------------------------------------------------------------
# rows
# ---------------------------------------------------------------------------


def kyoku_row(
    account: KyokuAccount, *, game_ordinal: int, kyoku_ordinal: int, is_final: bool
) -> dict[str, object]:
    if account.draw_kind is None:
        end = {
            "kind": "win",
            "winner_seats": [int(seat) for seat in account.winner_seats],
        }
    else:
        end = {"kind": "draw", "draw_kind": account.draw_kind}
    return {
        "dealer_seat": int(account.dealer_seat),
        "end": end,
        "game_ordinal": game_ordinal,
        "hand_number": account.hand_number,
        "honba": account.honba,
        "is_final_kyoku": is_final,
        "kyoku_ordinal": kyoku_ordinal,
        "points_after_kyoku": list(account.points_after_kyoku),
        "points_before_kyoku": list(account.points_before_kyoku),
        "riichi_sticks_after": account.riichi_sticks_after,
        "riichi_sticks_before": account.riichi_sticks_before,
        "round_wind": account.round_wind.value,
    }


def _actions_to_values(actions, context: str) -> list[dict[str, object]]:
    return [
        _action_to_value(action, f"{context}[{index}]")
        for index, action in enumerate(actions)
    ]


def focal_decision_rows(
    execution: FocalGameExecution,
    kyokus: Sequence[KyokuAccount],
    *,
    game_ordinal: int,
) -> list[dict[str, object]]:
    """focal seatの全decisionを、同じdecisionのcaptureへbindしてrowにする。"""
    focal_seat = execution.focal_seat
    observed = [
        (step, observation)
        for step in execution.inspection.step_observations
        for observation in step.seat_decisions
        if observation.seat == focal_seat
    ]
    if len(observed) != len(execution.captures):
        raise FocalOutcomeSourceError(
            "focal adapter captures do not match the focal decisions"
        )
    starts = [kyoku.start_event_sequence for kyoku in kyokus]
    rows = []
    for ordinal, ((step, observation), capture) in enumerate(
        zip(observed, execution.captures, strict=True)
    ):
        decision = capture.decision
        if (
            capture.focal_decision_ordinal != ordinal
            or observation.policy_input is not decision.input
            or observation.decision_trace.legal_actions != decision.legal_actions
            or observation.decision_trace.selected_action != capture.selection.action
        ):
            raise FocalOutcomeSourceError(
                f"focal decision {ordinal} is not bound to its selector capture"
            )
        kyoku_ordinal = bisect.bisect_left(starts, step.event_sequence_start) - 1
        if kyoku_ordinal < 0:
            raise FocalOutcomeSourceError("focal decision precedes the first kyoku")
        kyoku = kyokus[kyoku_ordinal]
        round_state = decision.input.round
        if (
            round_state.round_wind is not kyoku.round_wind
            or round_state.hand_number != kyoku.hand_number
            or round_state.honba != kyoku.honba
        ):
            raise FocalOutcomeSourceError(
                f"focal decision {ordinal} PolicyInput does not match its kyoku"
            )

        legal = _actions_to_values(decision.legal_actions, "legal_actions")
        legal.sort(key=_canonical_line)
        serialized = [_canonical_line(value) for value in legal]
        if len(serialized) != len(set(serialized)):
            raise FocalOutcomeSourceError("duplicate canonical legal action")
        selected = _action_to_value(capture.selection.action, "selected_action")
        survivors = capture.selection.survivor_actions
        rows.append(
            {
                "actor_seat": int(focal_seat),
                "exploration_token": capture.exploration_token,
                "focal_decision_ordinal": ordinal,
                "game_ordinal": game_ordinal,
                "kyoku_ordinal": kyoku_ordinal,
                "legal_actions": legal,
                "policy_input": _policy_input_to_value(decision.input),
                "producer_survivor_actions": None
                if survivors is None
                else _actions_to_values(survivors, "producer_survivor_actions"),
                "selected_action": selected,
                "step_ordinal": step.step_ordinal,
            }
        )
    return rows


def _write_lines(path: Path, rows: Sequence[dict[str, object]]) -> None:
    write_new_artifact_file(path, "".join(_canonical_line(row) for row in rows))


def write_game(
    path: Path,
    execution: FocalGameExecution,
    *,
    game_ordinal: int,
    seed: int,
    split: str,
) -> dict[str, object]:
    """1 hanchanのpayloadを書き、sealed game summaryを返す。"""
    inspection = execution.inspection
    if (
        execution.seed != seed
        or inspection.result.seed != seed
        or inspection.result.game_mode != GAME_MODE
        or execution.focal_seat != focal_seat_for(game_ordinal)
    ):
        raise FocalOutcomeSourceError("executed game identity mismatch")
    account = account_game(inspection)
    kyokus = account.kyokus
    decisions = focal_decision_rows(execution, kyokus, game_ordinal=game_ordinal)
    path.mkdir()
    _write_lines(
        path / KYOKU_PAYLOAD_FILENAME,
        [
            kyoku_row(
                kyoku,
                game_ordinal=game_ordinal,
                kyoku_ordinal=index,
                is_final=index == len(kyokus) - 1,
            )
            for index, kyoku in enumerate(kyokus)
        ],
    )
    _write_lines(path / DECISION_PAYLOAD_FILENAME, decisions)
    return seal(
        {
            "decision_count": len(decisions),
            "files": {
                name: _file_info(path / name)
                for name in (DECISION_PAYLOAD_FILENAME, KYOKU_PAYLOAD_FILENAME)
            },
            "focal_seat": int(execution.focal_seat),
            "game_ordinal": game_ordinal,
            "hanchan_final_riichi_sticks": account.hanchan_final_riichi_sticks,
            "hanchan_final_scores": list(account.hanchan_final_scores),
            "kyoku_count": len(kyokus),
            "seed": seed,
            "split": split,
        }
    )


def build_manifest(
    *,
    population_role: str,
    game_summaries: Sequence[dict[str, object]],
    allocation_bindings: Mapping[str, object],
    source_contract: dict[str, object],
) -> dict[str, object]:
    return seal(
        {
            "allocation_bindings": dict(allocation_bindings),
            "behavior": dict(BEHAVIOR),
            "games": list(game_summaries),
            "kind": OUTCOME_SOURCE_KIND,
            "population_role": population_role,
            "schema": OUTCOME_SOURCE_SCHEMA,
            "source_contract": source_contract,
        }
    )


def generate_focal_outcome_source(
    destination: str | Path,
    *,
    population_role: str,
    games: Sequence[tuple[int, str]],
    allocation_bindings: Mapping[str, object],
    source_contract: dict[str, object],
    max_steps: int | None = None,
):
    """populationを順に実行し、検証済みsourceを``destination``へ公開する。

    全体としてfail closedである。1 gameでも失敗した場合は部分sourceを公開しない。
    staging directoryへ書き、strict readback後にだけ``destination``へrenameする。
    """
    population = validate_population(population_role, games, allocation_bindings)
    validate_source_contract(source_contract)
    destination = Path(destination)
    staging = destination.with_name(f".{destination.name}.partial")
    if destination.exists() or staging.exists():
        raise FileExistsError(f"refusing to overwrite: {destination}")
    staging.mkdir(parents=False)
    try:
        summaries = []
        for game_ordinal, (seed, split) in enumerate(population):
            execution = run_focal_game(
                seed=seed, focal_seat=focal_seat_for(game_ordinal), max_steps=max_steps
            )
            summaries.append(
                write_game(
                    staging / f"game-{game_ordinal:03d}",
                    execution,
                    game_ordinal=game_ordinal,
                    seed=seed,
                    split=split,
                )
            )
        write_document(
            staging / MANIFEST_FILENAME,
            build_manifest(
                population_role=population_role,
                game_summaries=summaries,
                allocation_bindings=allocation_bindings,
                source_contract=source_contract,
            ),
        )
        source = verify_focal_outcome_source(staging)
        staging.rename(destination)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return source


# ---------------------------------------------------------------------------
# strict verification
# ---------------------------------------------------------------------------


def verify_focal_outcome_source(path: str | Path):
    """pinned lisjong consumerでstrict readし、Arena-owned auditを再検証する。

    返り値はlisjongの``FocalOutcomeSource``である。
    """
    path = Path(path)
    try:
        source = read_outcome_source(path)
        manifest = unseal(read_document(path / MANIFEST_FILENAME))
    except (
        OutcomeSourceError,
        UnsupportedSourceSchemaError,
        OffenseError,
        OSError,
        ValueError,
    ) as error:
        raise FocalOutcomeSourceError(
            f"invalid focal outcome source: {error}"
        ) from error
    if manifest["behavior"] != BEHAVIOR:
        raise FocalOutcomeSourceError("source behavior identity mismatch")
    validate_source_contract(manifest["source_contract"])
    validate_population(
        manifest["population_role"],
        [(game.seed, game.split) for game in source.games],
        manifest["allocation_bindings"],
    )
    for game in source.games:
        # tokenの導出はArena-ownedであり、lisjongは再計算しない。
        for decision in game.decisions:
            if decision.exploration_token != exploration_token(
                game_seed=game.seed,
                focal_seat=int(game.focal_seat),
                focal_decision_ordinal=decision.focal_decision_ordinal,
            ):
                raise FocalOutcomeSourceError(
                    f"game {game.game_ordinal} decision "
                    f"{decision.focal_decision_ordinal} exploration token mismatch"
                )
        for kyoku in game.kyokus:
            require_conservation(
                kyoku.points_before_kyoku,
                kyoku.riichi_sticks_before,
                kyoku.points_after_kyoku,
                kyoku.riichi_sticks_after,
            )
        final = game.kyokus[-1]
        require_final_adjustment(
            final.points_after_kyoku,
            final.riichi_sticks_after,
            game.hanchan_final_scores,
            game.hanchan_final_riichi_sticks,
        )
    return source


__all__ = [
    "BEHAVIOR",
    "CALIBRATION_ROLE",
    "DECISION_PAYLOAD_FILENAME",
    "FOCAL_ROTATION_RULE",
    "GAME_MODE",
    "KYOKU_PAYLOAD_FILENAME",
    "MANIFEST_FILENAME",
    "OUTCOME_SOURCE_KIND",
    "OUTCOME_SOURCE_SCHEMA",
    "PINNED_LISJONG_REVISION",
    "ROLE_SPLITS",
    "SCIENTIFIC_ROLE",
    "FocalGameExecution",
    "build_game_policies",
    "build_manifest",
    "build_source_contract",
    "focal_decision_rows",
    "focal_seat_for",
    "generate_focal_outcome_source",
    "kyoku_row",
    "run_focal_game",
    "validate_population",
    "validate_source_contract",
    "verify_focal_outcome_source",
    "write_game",
]
