"""Mortal診断artifact testのためのin-memory fixture。

real Mortal model / Docker runtime / RiichiEnvを起動せず、successfulな
``MortalDecisionEvaluationResult``だけをsyntheticに組み立てる。execution
provenanceもinstall metadataやGit HEADへ依存させず固定値を注入する。
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from unittest import mock

from _round_stats_fixtures import neutral_seat_round_stats_tuple
from lisjong.policy_contract import (
    ChiAction,
    DecisionTrace,
    Discard,
    DiscardAction,
    MeldKind,
    OwnHandState,
    PassAction,
    PlayerPublicState,
    PolicyInput,
    PublicMeld,
    RiichiAction,
    RiichiState,
    RoundState,
    Seat,
    Tile,
    TileCategory,
    TileType,
    Wind,
)

from lisjong_arena.model import (
    SINGLE_ROUND_GAME_MODE,
    PolicySpec,
    SingleRoundGameResult,
)
from lisjong_arena.mortal_decision_comparison import (
    MortalDecisionComparisonRecord,
    MortalDecisionComparisonSummary,
    NormalizedRiichiEnvAction,
    RiichiEnvActionKind,
)
from lisjong_arena.mortal_decision_evaluation import (
    MortalDecisionEvaluationPlan,
    MortalDecisionEvaluationResult,
    MortalDecisionGameResult,
)
from lisjong_arena.mortal_runtime import MortalDockerConfig
from lisjong_arena.single_round_artifact import SingleRoundExecutionProvenance

ARENA_REVISION = "a" * 40
LISJONG_REVISION = "b" * 40
LISJONG_ENGINE_REVISION = "c" * 40

_PROVENANCE_MODULE = (
    "lisjong_arena.mortal_decision_analysis_artifact.collect_execution_provenance"
)


def provenance(**overrides: str) -> SingleRoundExecutionProvenance:
    values = {
        "execution_environment": "riichienv",
        "lisjong_arena_version": "0.1.0",
        "lisjong_arena_revision": ARENA_REVISION,
        "lisjong_version": "0.1.0",
        "lisjong_revision": LISJONG_REVISION,
        "lisjong_engine_version": "0.1.0",
        "lisjong_engine_revision": LISJONG_ENGINE_REVISION,
        "riichienv_version": "0.4.8",
        "python_version": "3.14.0",
    }
    values.update(overrides)
    return SingleRoundExecutionProvenance(**values)


def patched_provenance(value: SingleRoundExecutionProvenance | None = None):
    """install metadata / Git HEADへ依存せずartifactを保存するためのpatch。"""
    return mock.patch(
        _PROVENANCE_MODULE,
        return_value=provenance() if value is None else value,
    )


def tile(category: TileCategory, rank: int, *, is_red: bool = False) -> Tile:
    return Tile(tile_type=TileType(category, rank), is_red=is_red)


_MAN = TileCategory.MANZU
_PIN = TileCategory.PINZU
_SOU = TileCategory.SOUZU


def mortal_config(directory: Path) -> MortalDockerConfig:
    model = directory / "mortal.pth"
    model.write_bytes(b"model")
    return MortalDockerConfig(
        image="mortal@sha256:0123456789abcdef",
        implementation_revision="mortal-revision",
        model_path=model,
    )


def temporary_mortal_config(cleanups) -> MortalDockerConfig:
    directory = tempfile.TemporaryDirectory()
    cleanups(directory.cleanup)
    return mortal_config(Path(directory.name))


def _player_state(seat: Seat) -> PlayerPublicState:
    """seatごとに異なるpublic stateを持たせ、projectionの取り違えを検出する。"""
    return PlayerPublicState(
        score=25000 + 1000 * int(seat),
        discards=(
            Discard(
                tile=tile(_PIN, 1 + int(seat)),
                tsumogiri=bool(int(seat) % 2),
                order=int(seat),
                called_by=None if seat != Seat.SEAT_1 else Seat.SEAT_2,
            ),
        ),
        melds=(
            (
                PublicMeld(
                    kind=MeldKind.PON,
                    tiles=(tile(_SOU, 3), tile(_SOU, 3), tile(_SOU, 3)),
                    from_seat=Seat.SEAT_3,
                    called_tile=tile(_SOU, 3),
                ),
            )
            if seat == Seat.SEAT_2
            else ()
        ),
        riichi=RiichiState.ACCEPTED if seat == Seat.SEAT_1 else RiichiState.NONE,
    )


def policy_input(seat: Seat) -> PolicyInput:
    """redドラ、副露、riichi、drawn tileを含むplayer-safe snapshot。"""
    return PolicyInput(
        self_seat=seat,
        round=RoundState(
            round_wind=Wind.EAST,
            hand_number=1 + int(seat) % 4,
            dealer_seat=Seat.SEAT_0,
            honba=int(seat),
            riichi_sticks=1,
            dora_indicators=(tile(_MAN, 5, is_red=True), tile(_PIN, 9)),
            live_wall_tiles_remaining=70 - int(seat),
        ),
        players=tuple(_player_state(Seat(index)) for index in range(4)),
        own_hand=OwnHandState(
            concealed_tiles=(
                tile(_MAN, 1),
                tile(_MAN, 2),
                tile(_MAN, 3),
                tile(_SOU, 5, is_red=True),
            ),
            drawn_tile=tile(_SOU, 5, is_red=True),
        ),
    )


def discard_normalized(seat: Seat, rank: int, *, tsumogiri: bool):
    return NormalizedRiichiEnvAction(
        kind=RiichiEnvActionKind.DISCARD,
        actor=seat,
        tile=tile(_MAN, rank),
        consume_tiles=(),
        tsumogiri=tsumogiri,
    )


def chi_normalized(seat: Seat):
    return NormalizedRiichiEnvAction(
        kind=RiichiEnvActionKind.CHI,
        actor=seat,
        tile=tile(_PIN, 3),
        consume_tiles=(tile(_PIN, 1), tile(_PIN, 2)),
        tsumogiri=None,
    )


def pass_normalized(seat: Seat):
    return NormalizedRiichiEnvAction(
        kind=RiichiEnvActionKind.PASS,
        actor=seat,
        tile=None,
        consume_tiles=(),
        tsumogiri=None,
    )


def riichi_normalized(seat: Seat):
    return NormalizedRiichiEnvAction(
        kind=RiichiEnvActionKind.RIICHI,
        actor=seat,
        tile=None,
        consume_tiles=(),
        tsumogiri=None,
    )


def decision_trace(seat: Seat, kind: str) -> DecisionTrace:
    """複数variantのInternalActionを含むlegal actions / selected action。"""
    discard = DiscardAction(actor=seat, tile=tile(_MAN, 1), tsumogiri=False)
    riichi = RiichiAction(actor=seat)
    chi = ChiAction(
        actor=seat,
        target=Seat((int(seat) - 1) % 4),
        called_tile=tile(_PIN, 3),
        consumed_tiles=(tile(_PIN, 1), tile(_PIN, 2)),
    )
    passing = PassAction(actor=seat)
    legal = (discard, riichi, chi, passing)
    selected = {
        "discard": discard,
        "riichi": riichi,
        "chi": chi,
        "pass": passing,
    }[kind]
    return DecisionTrace(legal_actions=legal, selected_action=selected)


def comparison_records(
    seed: int, rotation: int, *, identity: str = "combined"
) -> tuple[MortalDecisionComparisonRecord, ...]:
    """1 gameあたり2件のpaired decision(agreement 1件 / disagreement 1件)。"""
    seat = Seat(rotation)
    agreed = discard_normalized(seat, 1, tsumogiri=False)
    driver = chi_normalized(seat)
    shadow = pass_normalized(seat)
    return (
        MortalDecisionComparisonRecord(
            seed=seed,
            rotation=rotation,
            mortal_seat=seat,
            decision_ordinal=0,
            shadow_policy_identity=identity,
            policy_input=policy_input(seat),
            decision_trace=decision_trace(seat, "discard"),
            driver_mortal_action=agreed,
            shadow_policy_action=agreed,
            agreement=True,
        ),
        MortalDecisionComparisonRecord(
            seed=seed,
            rotation=rotation,
            mortal_seat=seat,
            decision_ordinal=1,
            shadow_policy_identity=identity,
            policy_input=policy_input(seat),
            decision_trace=decision_trace(seat, "chi"),
            driver_mortal_action=driver,
            shadow_policy_action=shadow,
            agreement=False,
        ),
    )


def objective_result(seed: int, rotation: int) -> SingleRoundGameResult:
    seat = Seat(rotation)
    scores = tuple(40000 if index == int(seat) else 20000 for index in range(4))
    return SingleRoundGameResult(
        seed=seed,
        rotation=rotation,
        game_mode=SINGLE_ROUND_GAME_MODE,
        candidate_seat=seat,
        scores=scores,
        seat_round_stats=neutral_seat_round_stats_tuple(scores),
    )


def evaluation_result(
    config: MortalDockerConfig,
    *,
    seeds: tuple[int, ...] = (0,),
    identity: str = "combined",
) -> MortalDecisionEvaluationResult:
    plan = MortalDecisionEvaluationPlan(
        policy=PolicySpec(identity=identity, factory=lambda: object()),
        seeds=seeds,
        mortal_config=config,
    )
    games = tuple(
        MortalDecisionGameResult(
            objective_result=objective_result(seed, rotation),
            comparisons=comparison_records(seed, rotation, identity=identity),
        )
        for seed in plan.seeds
        for rotation in range(4)
    )
    return MortalDecisionEvaluationResult(
        plan=plan,
        game_results=games,
        summary=MortalDecisionComparisonSummary.from_records(
            comparison for game in games for comparison in game.comparisons
        ),
    )
