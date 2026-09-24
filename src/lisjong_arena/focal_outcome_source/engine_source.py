"""L0.3 lisjong-engine focal outcome source（#370、parent project #79）。

RiichiEnv source（``arena-offense-l0.3-focal-outcome-source-v1``）とは別の
backend / source lineageである。RiichiEnv event deltaからの再構成は一切行わず、
lisjong-engineのauthoritative round settlementだけからkyoku境界の事実値を作る。
Arenaは事実だけを記録し、``target_q``の算出、training、paired evaluationは行わない。

```text
kyoku boundary（CompletedMatch.historyの各CompletedRound）
  round identity        position_before（prevailing_wind / hand_number / honba）
  riichi_sticks_before  position_before.riichi_sticks
  riichi_sticks_after   settlement.riichi_sticks_after
  point_deltas          settlement.point_deltas
  points_after_kyoku    scores_after_settlement
  points_before_kyoku   scores_after_settlement - point_deltas
                        （MatchState.settle_active_round()の
                         scores_after = scores_before.add(point_deltas)の厳密な逆）
```

- 最終kyokuでも``CompletedMatch.final_raw_scores``を``points_after_kyoku``に
  使わない。残存供託の最終配分（``final_riichi_stick_awards``）とその後の
  ``final_raw_scores``はgame summaryのaudit factとして別に保存し、差分が
  配分だけで説明できることを検証する
- 精算そのものは再計算しない。保存則・連続性はengine factの整合検証である

対局構成（#79 A4、RiichiEnv sourceと同じ）

```text
focal seat      = game_ordinal % 4
                  既存FocalExplorationPolicy（select_residual_exploration + token）
other 3 seats   lisjong ConstantResidualRuntime（game / seatごとにfresh）
rules           lisjong-engine RuleSet.default()
execution       lisjong_arena.lisjong_engine.hanchan.run_policy_hanchan
```

focal decisionはadapterのcaptureを、PolicyInputのround identity
（round_wind / hand_number / honba）でengine completed kyokuへ一意にbindする。
engineにはRiichiEnvの``step_ordinal``に当たるauthoritativeな実行順序factが
ないため、decision rowはこのfieldを持たない。

wire layoutはRiichiEnv sourceと同じ（``manifest.json`` / ``game-NNN/kyokus.jsonl``
/ ``game-NNN/focal-decisions.jsonl``）。fieldの差分は``docs/engine-focal-outcome-source.md``
を正本とする。
"""

import json
import shutil
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from importlib import metadata
from pathlib import Path

from lisjong.learning import ConstantResidualRuntime, select_residual_exploration
from lisjong.policy_contract import DecisionContext, Seat, Wind
from lisjong_engine.match_state import CompletedMatch, CompletedRound, RoundPosition
from lisjong_engine.points import SeatPoints
from lisjong_engine.round_result import (
    AbortiveDrawResult,
    ExhaustiveDrawResult,
    WinResult,
)
from lisjong_engine.rules import RuleSet
from lisjong_engine.seat import Seat as EngineSeat
from lisjong_engine.settlement import RoundSettlement

import lisjong_arena
from lisjong_arena import seed_registry
from lisjong_arena._artifact_io import ArtifactValidationError
from lisjong_arena.durable_local_game_record import (
    DurableLocalGameRecordError,
    _action_to_value,
    _parse_action,
    _parse_policy_input,
    _policy_input_to_value,
)
from lisjong_arena.lisjong_engine.domain_conversion import (
    seat_from_engine_seat,
    wind_from_engine_wind,
)
from lisjong_arena.lisjong_engine.hanchan import run_policy_hanchan
from lisjong_arena.offense_foundation.qualification import (
    read_document,
    seal,
    unseal,
    write_document,
)
from lisjong_arena.offense_foundation.semantics import OffenseError

from .accounting import RIICHI_DEPOSIT, FocalOutcomeSourceError
from .adapter import FocalExplorationPolicy, FocalSelectionCapture
from .exploration_token import exploration_token
from .source import (
    BEHAVIOR,
    CALIBRATION_ROLE,
    DECISION_PAYLOAD_FILENAME,
    KYOKU_PAYLOAD_FILENAME,
    MANIFEST_FILENAME,
    OUTCOME_SOURCE_KIND,
    PINNED_LISJONG_REVISION,
    SCIENTIFIC_ROLE,
    _actions_to_values,
    _canonical_line,
    _file_info,
    _write_lines,
    focal_seat_for,
)

ENGINE_OUTCOME_SOURCE_SCHEMA = (
    "arena-offense-l0.3-lisjong-engine-focal-outcome-source-v1"
)
BACKEND_NAME = "lisjong-engine"
PINNED_LISJONG_ENGINE_REVISION = "96b9796c76ef5db8f3968f689a1ca6f3dfc9aa3b"
"""#366 / #367で400 / 400 PASSしたengine revision（repository pinとは別に固定）。"""

RULES = {"constructor": "RuleSet.default", "name": "project-standard-v1", "version": 1}
"""source populationを実行した``RuleSet``のidentity。"""

DIAGNOSTIC_ROLE = "DIAGNOSTIC"
ROLE_SPLITS = {
    DIAGNOSTIC_ROLE: frozenset({"DIAGNOSTIC"}),
    CALIBRATION_ROLE: frozenset({"CALIBRATION"}),
    SCIENTIFIC_ROLE: frozenset({"TRAIN", "SELECT"}),
}
"""DIAGNOSTICはSeed Registry allocationを持たない。scientific evidenceにならない。"""

EXHAUSTIVE_DRAW_KIND = "exhaustive"
"""engine ``ExhaustiveDrawResult``。途中流局は``AbortiveDrawReason.value``を使う。"""

_SOURCE_CONTRACT_FIELDS = frozenset(
    {"arena_revision", "backend", "dependencies", "python", "rules"}
)
_MANIFEST_FIELDS = frozenset(
    {
        "allocation_bindings",
        "behavior",
        "games",
        "identity",
        "kind",
        "population_role",
        "schema",
        "source_contract",
    }
)
_GAME_FIELDS = frozenset(
    {
        "decision_count",
        "files",
        "final_riichi_stick_awards",
        "focal_seat",
        "game_ordinal",
        "hanchan_final_raw_scores",
        "identity",
        "kyoku_count",
        "match_end_reason",
        "seed",
        "split",
    }
)
_PAYLOAD_FILES = frozenset({KYOKU_PAYLOAD_FILENAME, DECISION_PAYLOAD_FILENAME})
_KYOKU_FIELDS = frozenset(
    {
        "dealer_seat",
        "end",
        "game_ordinal",
        "hand_number",
        "honba",
        "is_final_kyoku",
        "kyoku_ordinal",
        "point_deltas",
        "points_after_kyoku",
        "points_before_kyoku",
        "riichi_sticks_after",
        "riichi_sticks_before",
        "round_wind",
    }
)
_DECISION_FIELDS = frozenset(
    {
        "actor_seat",
        "exploration_token",
        "focal_decision_ordinal",
        "game_ordinal",
        "kyoku_ordinal",
        "legal_actions",
        "policy_input",
        "producer_survivor_actions",
        "selected_action",
    }
)
_MATCH_END_REASONS = frozenset(
    {"bankruptcy", "dealer_tenpai", "dealer_win", "final_round", "target_reached"}
)
_ABORTIVE_DRAW_KINDS = frozenset(
    {"four_kans", "four_riichi", "four_winds", "nine_terminals", "triple_ron"}
)

Points = tuple[int, int, int, int]


def _member(value: object, allowed) -> bool:
    """wire値の列挙判定。unhashableな値もschema違反として扱う。"""
    return type(value) is str and value in allowed


# ---------------------------------------------------------------------------
# engine facts
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class EngineKyoku:
    """1 completed kyokuのengine-authoritativeな事実値（seat順はlisjong Seat）。"""

    round_wind: Wind
    hand_number: int
    honba: int
    dealer_seat: Seat
    riichi_sticks_before: int
    riichi_sticks_after: int
    points_before_kyoku: Points
    points_after_kyoku: Points
    point_deltas: Points
    end: dict[str, object]

    @property
    def round_identity(self) -> tuple[Wind, int, int]:
        return (self.round_wind, self.hand_number, self.honba)


@dataclass(frozen=True, slots=True)
class EngineGameFacts:
    """1 hanchanの全kyokuと、hanchan最終調整のaudit fact。"""

    kyokus: tuple[EngineKyoku, ...]
    hanchan_final_raw_scores: Points
    final_riichi_stick_awards: tuple[tuple[Seat, int], ...]
    match_end_reason: str


def _points(value: object, context: str) -> Points:
    if not isinstance(value, SeatPoints):
        raise FocalOutcomeSourceError(f"{context} must be engine SeatPoints")
    points = [0, 0, 0, 0]
    for engine_seat in EngineSeat:
        points[int(seat_from_engine_seat(engine_seat))] = value[engine_seat]
    return tuple(points)


def _end(result: object) -> dict[str, object]:
    if isinstance(result, WinResult):
        return {
            "kind": "win",
            "winner_seats": [
                int(seat_from_engine_seat(winner.seat)) for winner in result.winners
            ],
        }
    if isinstance(result, ExhaustiveDrawResult):
        return {"kind": "draw", "draw_kind": EXHAUSTIVE_DRAW_KIND}
    if isinstance(result, AbortiveDrawResult):
        return {"kind": "draw", "draw_kind": result.reason.value}
    raise FocalOutcomeSourceError(f"unsupported engine RoundResult: {result!r}")


def require_kyoku_facts(kyoku: EngineKyoku, context: str) -> None:
    """engine settlementの逆算と保存則を検証する（精算は再計算しない）。"""
    if any(
        before + delta != after
        for before, delta, after in zip(
            kyoku.points_before_kyoku,
            kyoku.point_deltas,
            kyoku.points_after_kyoku,
            strict=True,
        )
    ):
        raise FocalOutcomeSourceError(
            f"{context} points_before_kyoku + point_deltas != points_after_kyoku"
        )
    pot_delta = kyoku.riichi_sticks_after - kyoku.riichi_sticks_before
    if sum(kyoku.point_deltas) + RIICHI_DEPOSIT * pot_delta != 0:
        raise FocalOutcomeSourceError(
            f"{context} score/riichi-stick conservation is violated"
        )


def require_continuity(previous: EngineKyoku, kyoku: EngineKyoku, context: str) -> None:
    if (
        kyoku.points_before_kyoku != previous.points_after_kyoku
        or kyoku.riichi_sticks_before != previous.riichi_sticks_after
    ):
        raise FocalOutcomeSourceError(
            f"{context} points / riichi sticks do not continue from the previous kyoku"
        )


def require_final_adjustment(
    final: EngineKyoku,
    hanchan_final_raw_scores: Sequence[int],
    awards: Sequence[tuple[Seat, int]],
) -> None:
    """hanchan最終scoreとの差を、残存供託の最終配分だけで説明する。"""
    awarded = [0, 0, 0, 0]
    for seat, amount in awards:
        if type(amount) is not int or amount <= 0:
            raise FocalOutcomeSourceError("final riichi stick award must be positive")
        awarded[int(seat)] += amount
    if sum(awarded) != RIICHI_DEPOSIT * final.riichi_sticks_after:
        raise FocalOutcomeSourceError(
            "final riichi stick awards do not distribute exactly the remaining sticks"
        )
    if list(hanchan_final_raw_scores) != [
        after + award
        for after, award in zip(final.points_after_kyoku, awarded, strict=True)
    ]:
        raise FocalOutcomeSourceError(
            "hanchan final raw scores differ from the final kyoku boundary by more "
            "than the final riichi stick awards"
        )


def engine_game_facts(match: object) -> EngineGameFacts:
    """``CompletedMatch``からkyoku境界の事実値を作り、整合をfail closedで検証する。"""
    if not isinstance(match, CompletedMatch):
        raise FocalOutcomeSourceError("match must be a lisjong-engine CompletedMatch")
    if not match.history:
        raise FocalOutcomeSourceError("CompletedMatch has no completed kyoku")
    kyokus: list[EngineKyoku] = []
    for index, completed in enumerate(match.history):
        context = f"completed kyoku {index}"
        if not isinstance(completed, CompletedRound):
            raise FocalOutcomeSourceError(f"{context} is not a CompletedRound")
        position = completed.position_before
        settlement = completed.settlement
        if not isinstance(position, RoundPosition) or not isinstance(
            settlement, RoundSettlement
        ):
            raise FocalOutcomeSourceError(f"{context} has invalid engine values")
        is_final = index == len(match.history) - 1
        if (completed.next_position is None) is not is_final:
            raise FocalOutcomeSourceError(
                f"{context} next_position does not match the terminal kyoku"
            )
        if index and match.history[index - 1].next_position != position:
            raise FocalOutcomeSourceError(
                f"{context} position_before is not the previous next_position"
            )
        after = _points(completed.scores_after_settlement, f"{context} scores_after")
        deltas = _points(settlement.point_deltas, f"{context} point_deltas")
        kyoku = EngineKyoku(
            round_wind=wind_from_engine_wind(position.prevailing_wind),
            hand_number=position.hand_number,
            honba=position.honba,
            dealer_seat=seat_from_engine_seat(position.dealer_seat),
            riichi_sticks_before=position.riichi_sticks,
            riichi_sticks_after=settlement.riichi_sticks_after,
            points_before_kyoku=tuple(
                a - d for a, d in zip(after, deltas, strict=True)
            ),
            points_after_kyoku=after,
            point_deltas=deltas,
            end=_end(completed.result),
        )
        require_kyoku_facts(kyoku, context)
        if kyokus:
            require_continuity(kyokus[-1], kyoku, context)
        kyokus.append(kyoku)
    identities = [kyoku.round_identity for kyoku in kyokus]
    if len(set(identities)) != len(identities):
        raise FocalOutcomeSourceError("completed kyoku round identity is not unique")
    awards = tuple(
        (seat_from_engine_seat(award.recipient), award.amount)
        for award in match.final_riichi_stick_awards
    )
    final_raw = _points(match.final_raw_scores, "final_raw_scores")
    require_final_adjustment(kyokus[-1], final_raw, awards)
    end_reason = match.end_reason.value
    if end_reason not in _MATCH_END_REASONS:
        raise FocalOutcomeSourceError(f"unsupported match end reason {end_reason!r}")
    return EngineGameFacts(
        kyokus=tuple(kyokus),
        hanchan_final_raw_scores=final_raw,
        final_riichi_stick_awards=awards,
        match_end_reason=end_reason,
    )


# ---------------------------------------------------------------------------
# population / provenance
# ---------------------------------------------------------------------------


def validate_population(
    population_role: object,
    games: object,
    allocation_bindings: object,
) -> tuple[tuple[int, str], ...]:
    """source populationをexecution前にfail closedで検証する。

    DIAGNOSTICはallocationを持たず、``allocation_bindings``は空objectに限る。
    CALIBRATION / SCIENTIFICはsplitごとに、engine seed domainのper-split
    bindingを完全一致で要求する。
    """
    if not _member(population_role, ROLE_SPLITS):
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
        if not _member(split, ROLE_SPLITS[population_role]):
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
    if type(allocation_bindings) is not dict:
        raise FocalOutcomeSourceError("allocation_bindings must be an object")
    if population_role == DIAGNOSTIC_ROLE:
        if allocation_bindings:
            raise FocalOutcomeSourceError(
                "a DIAGNOSTIC source must not carry allocation bindings"
            )
        return tuple(normalized)
    if set(allocation_bindings) != set(seeds_by_split):
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
        if binding["seed_domain"] != seed_registry.LISJONG_ENGINE_HANCHAN_SEED_DOMAIN:
            raise FocalOutcomeSourceError(
                f"{split} allocation binding uses the wrong seed_domain"
            )
    return tuple(normalized)


def validate_source_contract(source_contract: object) -> dict[str, object]:
    """engine sourceのArena-owned ``source_contract``のshapeとpinを検証する。"""
    if type(source_contract) is not dict or set(source_contract) != (
        _SOURCE_CONTRACT_FIELDS
    ):
        raise FocalOutcomeSourceError("source_contract fields do not match the schema")
    arena_revision = source_contract["arena_revision"]
    if (
        type(arena_revision) is not str
        or len(arena_revision) != 40
        or any(char not in "0123456789abcdef" for char in arena_revision)
    ):
        raise FocalOutcomeSourceError("source_contract.arena_revision is invalid")
    if source_contract["backend"] != BACKEND_NAME:
        raise FocalOutcomeSourceError("source_contract.backend is not lisjong-engine")
    if source_contract["dependencies"] != {
        "lisjong": PINNED_LISJONG_REVISION,
        "lisjong-engine": PINNED_LISJONG_ENGINE_REVISION,
    }:
        raise FocalOutcomeSourceError(
            "source_contract must record the pinned lisjong / lisjong-engine revisions"
        )
    if source_contract["rules"] != RULES:
        raise FocalOutcomeSourceError("source_contract.rules mismatch")
    if type(source_contract["python"]) is not str or not source_contract["python"]:
        raise FocalOutcomeSourceError("source_contract.python is invalid")
    return source_contract


def _installed_revision(distribution: str) -> str:
    text = metadata.distribution(distribution).read_text("direct_url.json")
    revision = None if text is None else json.loads(text).get("vcs_info", {})
    revision = None if revision is None else revision.get("commit_id")
    if type(revision) is not str:
        raise FocalOutcomeSourceError(
            f"STOP / INVALID: {distribution} is not installed from an exact VCS "
            "revision"
        )
    return revision


def build_source_contract(arena_checkout: str | Path) -> dict[str, object]:
    """clean Arena checkoutと、pinned revisionをinstallした環境から作る。

    repository全体の``pyproject.toml`` pinではなく、このsourceのpinned
    lisjong / lisjong-engine revisionを直接照合する（#366 / #367と同じ）。
    """
    checkout = Path(arena_checkout).resolve()

    def git(*args: str) -> str:
        return subprocess.check_output(
            ["git", "-C", str(checkout), *args], text=True
        ).strip()

    if git("status", "--porcelain", "--untracked-files=no") or git(
        "status", "--porcelain", "--untracked-files=normal", "--", "src"
    ):
        raise FocalOutcomeSourceError(
            "STOP / INVALID: tracked source changes must be committed"
        )
    if not Path(lisjong_arena.__file__).resolve().is_relative_to(checkout / "src"):
        raise FocalOutcomeSourceError(
            "STOP / INVALID: lisjong_arena is not imported from the checkout"
        )
    rules = RuleSet.default()
    if (rules.name, rules.version) != (RULES["name"], RULES["version"]):
        raise FocalOutcomeSourceError("STOP / INVALID: RuleSet.default() drifted")
    return validate_source_contract(
        {
            "arena_revision": git("rev-parse", "HEAD"),
            "backend": BACKEND_NAME,
            "dependencies": {
                name: _installed_revision(name)
                for name in ("lisjong", "lisjong-engine")
            },
            "python": ".".join(map(str, sys.version_info[:3])),
            "rules": dict(RULES),
        }
    )


# ---------------------------------------------------------------------------
# execution
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class EngineFocalGameExecution:
    """1 hanchanの``CompletedMatch``と、同じ実行でadapterがcaptureした監査値。"""

    seed: int
    focal_seat: Seat
    match: CompletedMatch
    captures: tuple[FocalSelectionCapture, ...]


def run_engine_focal_game(*, seed: int, focal_seat: Seat) -> EngineFocalGameExecution:
    """既存engine Policy bridgeで1 hanchanを実行する（単一game実行境界）。"""
    adapter = FocalExplorationPolicy(game_seed=seed, focal_seat=focal_seat)
    policies = {
        engine_seat: adapter
        if seat_from_engine_seat(engine_seat) == focal_seat
        else ConstantResidualRuntime().create_policy()
        for engine_seat in EngineSeat
    }
    match = run_policy_hanchan(policies, seed=seed, rules=RuleSet.default())
    return EngineFocalGameExecution(
        seed=seed, focal_seat=focal_seat, match=match, captures=adapter.captures
    )


# ---------------------------------------------------------------------------
# rows
# ---------------------------------------------------------------------------


def kyoku_row(
    kyoku: EngineKyoku, *, game_ordinal: int, kyoku_ordinal: int, is_final: bool
) -> dict[str, object]:
    return {
        "dealer_seat": int(kyoku.dealer_seat),
        "end": kyoku.end,
        "game_ordinal": game_ordinal,
        "hand_number": kyoku.hand_number,
        "honba": kyoku.honba,
        "is_final_kyoku": is_final,
        "kyoku_ordinal": kyoku_ordinal,
        "point_deltas": list(kyoku.point_deltas),
        "points_after_kyoku": list(kyoku.points_after_kyoku),
        "points_before_kyoku": list(kyoku.points_before_kyoku),
        "riichi_sticks_after": kyoku.riichi_sticks_after,
        "riichi_sticks_before": kyoku.riichi_sticks_before,
        "round_wind": kyoku.round_wind.value,
    }


def bind_kyoku(
    decision: DecisionContext, kyokus: Sequence[EngineKyoku], context: str
) -> int:
    """PolicyInputのround identityに一致するcompleted kyokuをちょうど1つ選ぶ。"""
    state = decision.input.round
    identity = (state.round_wind, state.hand_number, state.honba)
    matches = [
        index for index, kyoku in enumerate(kyokus) if kyoku.round_identity == identity
    ]
    if len(matches) != 1:
        raise FocalOutcomeSourceError(
            f"{context} matches {len(matches)} completed kyoku by round identity"
        )
    (index,) = matches
    if state.dealer_seat != kyokus[index].dealer_seat:
        raise FocalOutcomeSourceError(f"{context} dealer seat contradicts its kyoku")
    return index


def focal_decision_rows(
    execution: EngineFocalGameExecution,
    kyokus: Sequence[EngineKyoku],
    *,
    game_ordinal: int,
) -> list[dict[str, object]]:
    """adapterの全captureをengine completed kyokuへbindしてrowにする。"""
    rows = []
    previous_kyoku = 0
    for ordinal, capture in enumerate(execution.captures):
        context = f"focal decision {ordinal}"
        decision = capture.decision
        if (
            capture.focal_decision_ordinal != ordinal
            or decision.input.self_seat != execution.focal_seat
            or capture.exploration_token
            != exploration_token(
                game_seed=execution.seed,
                focal_seat=int(execution.focal_seat),
                focal_decision_ordinal=ordinal,
            )
            or not any(capture.selection.action is a for a in decision.legal_actions)
        ):
            raise FocalOutcomeSourceError(
                f"{context} is not bound to its exploration capture"
            )
        kyoku_ordinal = bind_kyoku(decision, kyokus, context)
        if kyoku_ordinal < previous_kyoku:
            raise FocalOutcomeSourceError(f"{context} goes back to an earlier kyoku")
        previous_kyoku = kyoku_ordinal
        legal = _actions_to_values(decision.legal_actions, "legal_actions")
        legal.sort(key=_canonical_line)
        if len({_canonical_line(value) for value in legal}) != len(legal):
            raise FocalOutcomeSourceError("duplicate canonical legal action")
        survivors = capture.selection.survivor_actions
        rows.append(
            {
                "actor_seat": int(execution.focal_seat),
                "exploration_token": capture.exploration_token,
                "focal_decision_ordinal": ordinal,
                "game_ordinal": game_ordinal,
                "kyoku_ordinal": kyoku_ordinal,
                "legal_actions": legal,
                "policy_input": _policy_input_to_value(decision.input),
                "producer_survivor_actions": None
                if survivors is None
                else _actions_to_values(survivors, "producer_survivor_actions"),
                "selected_action": _action_to_value(
                    capture.selection.action, "selected_action"
                ),
            }
        )
    return rows


def write_game(
    path: Path,
    execution: EngineFocalGameExecution,
    *,
    game_ordinal: int,
    seed: int,
    split: str,
) -> dict[str, object]:
    """1 hanchanのpayloadを書き、sealed game summaryを返す。"""
    if execution.seed != seed or execution.focal_seat != focal_seat_for(game_ordinal):
        raise FocalOutcomeSourceError("executed game identity mismatch")
    facts = engine_game_facts(execution.match)
    kyokus = facts.kyokus
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
            "final_riichi_stick_awards": [
                {"amount": amount, "recipient_seat": int(seat)}
                for seat, amount in facts.final_riichi_stick_awards
            ],
            "focal_seat": int(execution.focal_seat),
            "game_ordinal": game_ordinal,
            "hanchan_final_raw_scores": list(facts.hanchan_final_raw_scores),
            "kyoku_count": len(kyokus),
            "match_end_reason": facts.match_end_reason,
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
            "schema": ENGINE_OUTCOME_SOURCE_SCHEMA,
            "source_contract": source_contract,
        }
    )


def generate_engine_focal_outcome_source(
    destination: str | Path,
    *,
    population_role: str,
    games: Sequence[tuple[int, str]],
    allocation_bindings: Mapping[str, object],
    source_contract: dict[str, object],
    run_game: Callable[..., EngineFocalGameExecution] = run_engine_focal_game,
    on_game: Callable[[int, int], None] | None = None,
):
    """populationを順に実行し、strict readback済みsourceを``destination``へ公開する。

    全体としてfail closedである。1 gameでも失敗した場合は部分sourceを公開しない。
    ``run_game``はtestで単一game実行境界を差し替えるためだけのseamである。
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
            execution = run_game(seed=seed, focal_seat=focal_seat_for(game_ordinal))
            summaries.append(
                write_game(
                    staging / f"game-{game_ordinal:03d}",
                    execution,
                    game_ordinal=game_ordinal,
                    seed=seed,
                    split=split,
                )
            )
            if on_game is not None:
                on_game(game_ordinal, seed)
        write_document(
            staging / MANIFEST_FILENAME,
            build_manifest(
                population_role=population_role,
                game_summaries=summaries,
                allocation_bindings=allocation_bindings,
                source_contract=source_contract,
            ),
        )
        source = verify_engine_focal_outcome_source(staging)
        staging.rename(destination)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return source


# ---------------------------------------------------------------------------
# strict readback
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class VerifiedEngineGame:
    game_ordinal: int
    seed: int
    split: str
    focal_seat: Seat
    kyoku_count: int
    decision_count: int
    multi_survivor_decision_count: int


@dataclass(frozen=True, slots=True)
class VerifiedEngineSource:
    """strict readbackに成功したengine sourceのidentityとgame一覧。"""

    identity: str
    population_role: str
    games: tuple[VerifiedEngineGame, ...]


def _fields(value: object, fields: frozenset[str], context: str) -> dict:
    if type(value) is not dict or set(value) != fields:
        raise FocalOutcomeSourceError(f"{context} fields do not match the schema")
    return value


def _nonneg(value: object, context: str) -> int:
    if type(value) is not int or value < 0:
        raise FocalOutcomeSourceError(f"{context} must be a non-negative int")
    return value


def _seat_value(value: object, context: str) -> Seat:
    if _nonneg(value, context) > 3:
        raise FocalOutcomeSourceError(f"{context} is not a valid seat")
    return Seat(value)


def _four(value: object, context: str) -> Points:
    if type(value) is not list or len(value) != 4:
        raise FocalOutcomeSourceError(f"{context} must be a list of 4 ints")
    if any(type(item) is not int for item in value):
        raise FocalOutcomeSourceError(f"{context} must be a list of 4 ints")
    return tuple(value)


def _read_end(value: object, context: str) -> dict[str, object]:
    if type(value) is not dict:
        raise FocalOutcomeSourceError(f"{context} must be an object")
    if value.get("kind") == "win":
        _fields(value, frozenset({"kind", "winner_seats"}), context)
        winners = value["winner_seats"]
        if type(winners) is not list or not winners:
            raise FocalOutcomeSourceError(f"{context}.winner_seats must be non-empty")
        seats = [_seat_value(seat, f"{context}.winner_seats") for seat in winners]
        if len(set(seats)) != len(seats):
            raise FocalOutcomeSourceError(f"{context}.winner_seats must be unique")
    elif value.get("kind") == "draw":
        _fields(value, frozenset({"draw_kind", "kind"}), context)
        if not _member(
            value["draw_kind"], _ABORTIVE_DRAW_KINDS | {EXHAUSTIVE_DRAW_KIND}
        ):
            raise FocalOutcomeSourceError(f"{context}.draw_kind is unsupported")
    else:
        raise FocalOutcomeSourceError(f"{context}.kind must be 'win' or 'draw'")
    return value


def _lines(path: Path, context: str) -> list[dict]:
    rows = []
    with path.open(encoding="utf-8", newline="\n") as stream:
        for index, line in enumerate(stream):
            try:
                row = json.loads(line)
            except json.JSONDecodeError as error:
                raise FocalOutcomeSourceError(
                    f"{context}[{index}] is not JSON"
                ) from error
            if line != _canonical_line(row):
                raise FocalOutcomeSourceError(f"{context}[{index}] is not canonical")
            rows.append(row)
    return rows


def _read_kyoku(row: dict, *, game_ordinal: int, index: int) -> EngineKyoku:
    context = f"game-{game_ordinal:03d}.kyokus[{index}]"
    _fields(row, _KYOKU_FIELDS, context)
    if row["game_ordinal"] != game_ordinal or row["kyoku_ordinal"] != index:
        raise FocalOutcomeSourceError(f"{context} ordinal mismatch")
    if type(row["is_final_kyoku"]) is not bool:
        raise FocalOutcomeSourceError(f"{context}.is_final_kyoku must be a bool")
    hand_number = _nonneg(row["hand_number"], f"{context}.hand_number")
    if not 1 <= hand_number <= 4:
        raise FocalOutcomeSourceError(f"{context}.hand_number must be 1..4")
    try:
        round_wind = Wind(row["round_wind"])
    except ValueError:
        raise FocalOutcomeSourceError(f"{context}.round_wind is invalid") from None
    kyoku = EngineKyoku(
        round_wind=round_wind,
        hand_number=hand_number,
        honba=_nonneg(row["honba"], f"{context}.honba"),
        dealer_seat=_seat_value(row["dealer_seat"], f"{context}.dealer_seat"),
        riichi_sticks_before=_nonneg(
            row["riichi_sticks_before"], f"{context}.riichi_sticks_before"
        ),
        riichi_sticks_after=_nonneg(
            row["riichi_sticks_after"], f"{context}.riichi_sticks_after"
        ),
        points_before_kyoku=_four(
            row["points_before_kyoku"], f"{context}.points_before_kyoku"
        ),
        points_after_kyoku=_four(
            row["points_after_kyoku"], f"{context}.points_after_kyoku"
        ),
        point_deltas=_four(row["point_deltas"], f"{context}.point_deltas"),
        end=_read_end(row["end"], f"{context}.end"),
    )
    require_kyoku_facts(kyoku, context)
    return kyoku


def _read_actions(values: object, context: str):
    if type(values) is not list:
        raise FocalOutcomeSourceError(f"{context} must be a list")
    actions = tuple(
        _parse_action(value, f"{context}[{index}]")
        for index, value in enumerate(values)
    )
    if _actions_to_values(actions, context) != values:
        raise FocalOutcomeSourceError(f"{context} does not round trip")
    return actions


def _read_decision(
    row: dict,
    *,
    game_ordinal: int,
    seed: int,
    focal_seat: Seat,
    index: int,
    kyokus: Sequence[EngineKyoku],
) -> tuple[int, bool]:
    """1 decision rowを検証し、``(kyoku_ordinal, multi_survivor)``を返す。

    tokenを再導出し、lisjongのselectorを再実行してsurvivor / selectionを照合する。
    """
    context = f"game-{game_ordinal:03d}.decisions[{index}]"
    _fields(row, _DECISION_FIELDS, context)
    if row["game_ordinal"] != game_ordinal or row["focal_decision_ordinal"] != index:
        raise FocalOutcomeSourceError(f"{context} ordinal mismatch")
    if _seat_value(row["actor_seat"], f"{context}.actor_seat") != focal_seat:
        raise FocalOutcomeSourceError(f"{context}.actor_seat is not the focal seat")
    kyoku_ordinal = _nonneg(row["kyoku_ordinal"], f"{context}.kyoku_ordinal")
    token = row["exploration_token"]
    if token != exploration_token(
        game_seed=seed, focal_seat=int(focal_seat), focal_decision_ordinal=index
    ):
        raise FocalOutcomeSourceError(f"{context} exploration token mismatch")
    try:
        policy_input = _parse_policy_input(row["policy_input"], "policy_input")
        if _policy_input_to_value(policy_input) != row["policy_input"]:
            raise FocalOutcomeSourceError(f"{context}.policy_input does not round trip")
        legal_actions = _read_actions(row["legal_actions"], f"{context}.legal_actions")
        (selected,) = _read_actions(
            [row["selected_action"]], f"{context}.selected_action"
        )
        survivors = row["producer_survivor_actions"]
        recorded_survivors = (
            None
            if survivors is None
            else _read_actions(survivors, f"{context}.producer_survivor_actions")
        )
        decision = DecisionContext(input=policy_input, legal_actions=legal_actions)
    except (
        ArtifactValidationError,
        DurableLocalGameRecordError,
        TypeError,
        ValueError,
    ) as error:
        raise FocalOutcomeSourceError(f"{context} typed value: {error}") from error
    if policy_input.self_seat != focal_seat:
        raise FocalOutcomeSourceError(f"{context} PolicyInput seat is not focal")
    canonical = [_canonical_line(value) for value in row["legal_actions"]]
    if not canonical or canonical != sorted(canonical):
        raise FocalOutcomeSourceError(f"{context}.legal_actions is not canonical")
    if len(set(canonical)) != len(canonical):
        raise FocalOutcomeSourceError(f"{context}.legal_actions has duplicates")
    if selected not in legal_actions:
        raise FocalOutcomeSourceError(f"{context}.selected_action is not legal")
    if bind_kyoku(decision, kyokus, context) != kyoku_ordinal:
        raise FocalOutcomeSourceError(
            f"{context}.kyoku_ordinal is not the kyoku bound by round identity"
        )
    selection = select_residual_exploration(decision, token)
    if recorded_survivors != selection.survivor_actions:
        raise FocalOutcomeSourceError(
            f"{context}.producer_survivor_actions does not match the recomputed "
            "semantic-envelope survivors"
        )
    if selected != selection.action:
        raise FocalOutcomeSourceError(
            f"{context}.selected_action does not match the exploration selection"
        )
    multi = selection.survivors is not None and len(selection.survivors) >= 2
    return kyoku_ordinal, multi


def _read_game(root: Path, summary: object, *, ordinal: int, role: str):
    context = f"manifest.games[{ordinal}]"
    try:
        body = unseal(summary)
    except OffenseError as error:
        raise FocalOutcomeSourceError(f"{context}: {error}") from error
    _fields(summary, _GAME_FIELDS, context)
    if body["game_ordinal"] != ordinal:
        raise FocalOutcomeSourceError(f"{context} is not the contiguous game ordinal")
    seed = _nonneg(body["seed"], f"{context}.seed")
    split = body["split"]
    if not _member(split, ROLE_SPLITS[role]):
        raise FocalOutcomeSourceError(f"{context}.split is not allowed for {role}")
    focal_seat = _seat_value(body["focal_seat"], f"{context}.focal_seat")
    if focal_seat != focal_seat_for(ordinal):
        raise FocalOutcomeSourceError(f"{context}.focal_seat != game_ordinal % 4")
    if not _member(body["match_end_reason"], _MATCH_END_REASONS):
        raise FocalOutcomeSourceError(f"{context}.match_end_reason is unsupported")
    final_raw = _four(
        body["hanchan_final_raw_scores"], f"{context}.hanchan_final_raw_scores"
    )
    awards_value = body["final_riichi_stick_awards"]
    if type(awards_value) is not list:
        raise FocalOutcomeSourceError(f"{context}.final_riichi_stick_awards")
    awards = []
    for award in awards_value:
        _fields(award, frozenset({"amount", "recipient_seat"}), f"{context}.award")
        awards.append((_seat_value(award["recipient_seat"], context), award["amount"]))
    files = _fields(body["files"], _PAYLOAD_FILES, f"{context}.files")

    path = root / f"game-{ordinal:03d}"
    if not path.is_dir() or {child.name for child in path.iterdir()} != (
        _PAYLOAD_FILES
    ):
        raise FocalOutcomeSourceError(f"missing/unexpected payload files: {path.name}")
    for name in sorted(_PAYLOAD_FILES):
        if _file_info(path / name) != files[name]:
            raise FocalOutcomeSourceError(f"{path.name}/{name} size/digest mismatch")

    kyokus: list[EngineKyoku] = []
    finals = []
    for index, row in enumerate(_lines(path / KYOKU_PAYLOAD_FILENAME, path.name)):
        kyoku = _read_kyoku(row, game_ordinal=ordinal, index=index)
        if kyokus:
            require_continuity(kyokus[-1], kyoku, f"{path.name}.kyokus[{index}]")
        kyokus.append(kyoku)
        finals.append(row["is_final_kyoku"])
    if not kyokus or len(kyokus) != body["kyoku_count"]:
        raise FocalOutcomeSourceError(f"{path.name} kyoku count mismatch")
    if finals != [False] * (len(kyokus) - 1) + [True]:
        raise FocalOutcomeSourceError(f"{path.name} is_final_kyoku must mark the last")
    identities = [kyoku.round_identity for kyoku in kyokus]
    if len(set(identities)) != len(identities):
        raise FocalOutcomeSourceError(f"{path.name} kyoku round identity repeats")
    require_final_adjustment(kyokus[-1], final_raw, awards)

    multi = previous_kyoku = 0
    rows = _lines(path / DECISION_PAYLOAD_FILENAME, path.name)
    for index, row in enumerate(rows):
        kyoku_ordinal, is_multi = _read_decision(
            row,
            game_ordinal=ordinal,
            seed=seed,
            focal_seat=focal_seat,
            index=index,
            kyokus=kyokus,
        )
        if kyoku_ordinal < previous_kyoku:
            raise FocalOutcomeSourceError(
                f"{path.name}.decisions[{index}] execution ordering mismatch"
            )
        previous_kyoku = kyoku_ordinal
        multi += is_multi
    if len(rows) != body["decision_count"]:
        raise FocalOutcomeSourceError(f"{path.name} decision count mismatch")
    return VerifiedEngineGame(
        game_ordinal=ordinal,
        seed=seed,
        split=split,
        focal_seat=focal_seat,
        kyoku_count=len(kyokus),
        decision_count=len(rows),
        multi_survivor_decision_count=multi,
    )


def verify_engine_focal_outcome_source(path: str | Path) -> VerifiedEngineSource:
    """engine sourceをstrict readし、全shape / order / provenance / 連続性を検証する。

    他schema（RiichiEnv v1を含む）はfail closedでrejectする。
    """
    root = Path(path)
    try:
        manifest = read_document(root / MANIFEST_FILENAME)
        body = unseal(manifest)
    except (OffenseError, OSError, ValueError, ArtifactValidationError) as error:
        raise FocalOutcomeSourceError(
            f"invalid engine focal outcome source: {error}"
        ) from error
    if body.get("schema") != ENGINE_OUTCOME_SOURCE_SCHEMA:
        raise FocalOutcomeSourceError(
            f"unsupported outcome source schema: {body.get('schema')!r}"
        )
    _fields(manifest, _MANIFEST_FIELDS, "manifest")
    if body["kind"] != OUTCOME_SOURCE_KIND:
        raise FocalOutcomeSourceError("outcome source kind mismatch")
    if body["behavior"] != BEHAVIOR:
        raise FocalOutcomeSourceError("source behavior identity mismatch")
    validate_source_contract(body["source_contract"])
    role = body["population_role"]
    if not _member(role, ROLE_SPLITS):
        raise FocalOutcomeSourceError("unsupported population_role")
    summaries = body["games"]
    if type(summaries) is not list or not summaries:
        raise FocalOutcomeSourceError("manifest.games must be a non-empty list")
    if {child.name for child in root.iterdir()} != {MANIFEST_FILENAME} | {
        f"game-{ordinal:03d}" for ordinal in range(len(summaries))
    }:
        raise FocalOutcomeSourceError("missing/unexpected outcome source files")
    games = tuple(
        _read_game(root, summary, ordinal=ordinal, role=role)
        for ordinal, summary in enumerate(summaries)
    )
    validate_population(
        role,
        [(game.seed, game.split) for game in games],
        body["allocation_bindings"],
    )
    return VerifiedEngineSource(
        identity=manifest["identity"], population_role=role, games=games
    )


__all__ = [
    "BACKEND_NAME",
    "DIAGNOSTIC_ROLE",
    "ENGINE_OUTCOME_SOURCE_SCHEMA",
    "EXHAUSTIVE_DRAW_KIND",
    "PINNED_LISJONG_ENGINE_REVISION",
    "ROLE_SPLITS",
    "RULES",
    "EngineFocalGameExecution",
    "EngineGameFacts",
    "EngineKyoku",
    "VerifiedEngineGame",
    "VerifiedEngineSource",
    "bind_kyoku",
    "build_manifest",
    "build_source_contract",
    "engine_game_facts",
    "focal_decision_rows",
    "generate_engine_focal_outcome_source",
    "kyoku_row",
    "require_continuity",
    "require_final_adjustment",
    "require_kyoku_facts",
    "run_engine_focal_game",
    "validate_population",
    "validate_source_contract",
    "verify_engine_focal_outcome_source",
    "write_game",
]
