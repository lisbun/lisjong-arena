"""Arena #146 kan coverage-source qualification fixtures。

24 hanchanの実対局を回さずに、opportunity diagnostic / evidence accounting /
dataset compatibilityの境界だけを固定する。実populationの実行はここでは
検証しない。
"""

from dataclasses import replace
from functools import cache

from _phase2_anchor_fixtures import halt_at_turn_anchor
from _phase3_bootstrap_fixtures import resolved_provenance
from lisjong.policy_contract import Seat as PolicySeat
from lisjong.policy_contract import Tile, TileCategory, TileType
from lisjong.policy_contract.action import (
    AnkanAction,
    DaiminkanAction,
    DiscardAction,
    KakanAction,
    PassAction,
    RonAction,
    TsumoAction,
)
from lisjong.policy_contract.decision_context import DecisionContext
from lisjong_engine.public_state import PublicMeld, PublicMeldType, public_tile
from lisjong_engine.round_event import DrawSource
from lisjong_engine.round_evidence import (
    DrawEvidence,
    KanConfirmedEvidence,
    KanDeclaredEvidence,
    MeldCalledEvidence,
    ResponseEpochClosedEvidence,
    ResponseEpochOpenedEvidence,
    ResponseOutcome,
    ResponseTrigger,
    RoundEndedEvidence,
    RoundEndKind,
)
from lisjong_engine.round_evidence_builder import build_round_evidence
from lisjong_engine.seat import Seat
from lisjong_engine.tile import Tile as EngineTile
from lisjong_engine.tile import TileCategory as EngineTileCategory
from lisjong_engine.tile import TileType as EngineTileType
from lisjong_engine.win_context import WinMethod

from lisjong_arena.lisjong_engine.policy_input import build_policy_input
from lisjong_arena.phase4_raw_corpus.model import (
    CheckpointTruth,
    DecisionCheckpoint,
    OpponentConcealedTruth,
    RawCorpus,
    RawGame,
    RawRound,
    ViewerEvidence,
)
from lisjong_arena.phase4_raw_corpus.persistence import save_raw_corpus
from lisjong_arena.phase5_belief_dataset.pipeline import run_phase5_pipeline
from lisjong_arena.stage3_kan_coverage.population import kan_coverage_population_plan
from lisjong_arena.stage3_kan_coverage.protocol import (
    CLASSIFICATION_RULE,
    DIAGNOSTIC_SCHEMA_VERSION,
    MANIFEST_SCHEMA_VERSION,
    ORDERED_SEEDS,
    PILOT_HANCHAN,
    PILOT_ROLE,
    RETRY_RULE,
    SPLIT_POLICY,
)

KAN_COVERAGE_BASE_SEED = 1000


def tile(rank: int, category: TileCategory = TileCategory.MANZU) -> Tile:
    """lisjong契約側のTile value。"""
    return Tile(TileType(category, rank))


def _engine_tile(rank: int, copy_index: int) -> EngineTile:
    return EngineTile(EngineTileType(EngineTileCategory.MANZU, rank), copy_index)


def public_meld_of(
    meld_type: PublicMeldType, rank: int, *, from_seat: Seat = Seat.SOUTH
) -> PublicMeld:
    """evidence fixture用のpublic meld。"""
    size = 3 if meld_type in (PublicMeldType.CHI, PublicMeldType.PON) else 4
    tiles = tuple(public_tile(_engine_tile(rank, index)) for index in range(size))
    if meld_type is PublicMeldType.ANKAN:
        return PublicMeld(
            meld_type=meld_type, tiles=tiles, from_seat=None, called_tile=None
        )
    return PublicMeld(
        meld_type=meld_type,
        tiles=tiles,
        from_seat=from_seat,
        called_tile=tiles[0],
    )


@cache
def _anchor(seed: int = KAN_COVERAGE_BASE_SEED):
    return halt_at_turn_anchor(seed)


def policy_input():
    """実対局のTURN anchorから作った、実在のPolicyInput。"""
    return build_policy_input(_anchor().observation)


def decision_with(*actions) -> DecisionContext:
    """与えたlegal actionsだけを持つDecisionContext。"""
    return DecisionContext(input=policy_input(), legal_actions=tuple(actions))


def self_seat() -> PolicySeat:
    return policy_input().self_seat


def other_seat() -> PolicySeat:
    seats = [value for value in PolicySeat if value is not self_seat()]
    return seats[0]


def discard_action(rank: int = 1) -> DiscardAction:
    return DiscardAction(actor=self_seat(), tile=tile(rank), tsumogiri=False)


def pass_action() -> PassAction:
    return PassAction(actor=self_seat())


def ron_action(rank: int = 2) -> RonAction:
    return RonAction(actor=self_seat(), target=other_seat(), winning_tile=tile(rank))


def tsumo_action(rank: int = 2) -> TsumoAction:
    return TsumoAction(actor=self_seat(), winning_tile=tile(rank))


def daiminkan_action(rank: int = 3) -> DaiminkanAction:
    return DaiminkanAction(
        actor=self_seat(),
        target=other_seat(),
        called_tile=tile(rank),
        consumed_tiles=(tile(rank), tile(rank), tile(rank)),
    )


def ankan_action(rank: int = 4) -> AnkanAction:
    return AnkanAction(
        actor=self_seat(), tiles=(tile(rank), tile(rank), tile(rank), tile(rank))
    )


def kakan_action(rank: int = 5) -> KakanAction:
    return KakanAction(
        actor=self_seat(),
        added_tile=tile(rank),
        from_seat=other_seat(),
        called_tile=tile(rank),
    )


KAN_FIXTURE_RANK = {
    PublicMeldType.DAIMINKAN: 3,
    PublicMeldType.ANKAN: 4,
    PublicMeldType.KAKAN: 5,
}
"""fixture actionとevidence meldでtile semanticsを一致させるためのkind別rank。

`daiminkan_action()` / `ankan_action()` / `kakan_action()`の既定rankと揃える。
accountingがselected actionとpublic meldをsemanticに照合するため、両者が
一致していないとconfirmedにならない。
"""


def declared_kan_stream(
    meld_type: PublicMeldType,
    *,
    actor: Seat = Seat.EAST,
    rank: int | None = None,
    from_seat: Seat = Seat.SOUTH,
    confirmed: bool = True,
    rinshan: bool = True,
    chankan_ron: bool = False,
    abortive_after_confirm: bool = False,
) -> tuple:
    """加槓・暗槓のdeclared -> confirmed -> rinshan evidence列。"""
    meld = public_meld_of(
        meld_type,
        KAN_FIXTURE_RANK[meld_type] if rank is None else rank,
        from_seat=from_seat,
    )
    trigger = (
        ResponseTrigger.KAKAN
        if meld_type is PublicMeldType.KAKAN
        else ResponseTrigger.ANKAN
    )
    stream = [
        KanDeclaredEvidence(actor, meld),
        ResponseEpochOpenedEvidence(
            trigger=trigger,
            source_seat=actor,
            responder_seats=_responders(actor),
        ),
    ]
    if chankan_ron:
        stream.append(
            ResponseEpochClosedEvidence(
                trigger=trigger, source_seat=actor, outcome=ResponseOutcome.RON
            )
        )
        stream.append(
            RoundEndedEvidence(
                kind=RoundEndKind.WIN,
                win_method=WinMethod.RON,
                winner_seats=(Seat.SOUTH,),
                source_seat=actor,
            )
        )
        return tuple(stream)
    stream.append(
        ResponseEpochClosedEvidence(
            trigger=trigger,
            source_seat=actor,
            outcome=ResponseOutcome.NO_PUBLIC_RESPONSE,
        )
    )
    if confirmed:
        stream.append(KanConfirmedEvidence(actor, meld))
    if abortive_after_confirm:
        from lisjong_engine.round_result import AbortiveDrawReason

        stream.append(
            RoundEndedEvidence(
                kind=RoundEndKind.ABORTIVE_DRAW,
                abortive_reason=AbortiveDrawReason.FOUR_KANS,
            )
        )
        return tuple(stream)
    if rinshan:
        stream.append(DrawEvidence(actor, DrawSource.RINSHAN))
    return tuple(stream)


def daiminkan_stream(
    *,
    actor: Seat = Seat.EAST,
    target: Seat = Seat.SOUTH,
    called_by: Seat | None = None,
    ron: bool = False,
    rinshan: bool = True,
    rank: int | None = None,
    meld_from_seat: Seat | None = None,
) -> tuple:
    """大明槓のresponse epoch解決evidence列。"""
    stream = [
        ResponseEpochOpenedEvidence(
            trigger=ResponseTrigger.DISCARD,
            source_seat=target,
            responder_seats=_responders(target),
        )
    ]
    if ron:
        stream.append(
            ResponseEpochClosedEvidence(
                trigger=ResponseTrigger.DISCARD,
                source_seat=target,
                outcome=ResponseOutcome.RON,
            )
        )
        stream.append(
            RoundEndedEvidence(
                kind=RoundEndKind.WIN,
                win_method=WinMethod.RON,
                winner_seats=(Seat.WEST,),
                source_seat=target,
            )
        )
        return tuple(stream)
    caller = actor if called_by is None else called_by
    meld_type = PublicMeldType.DAIMINKAN if caller is actor else PublicMeldType.PON
    stream.append(
        ResponseEpochClosedEvidence(
            trigger=ResponseTrigger.DISCARD,
            source_seat=target,
            outcome=ResponseOutcome.CALL,
        )
    )
    stream.append(
        MeldCalledEvidence(
            caller,
            public_meld_of(
                meld_type,
                KAN_FIXTURE_RANK[PublicMeldType.DAIMINKAN] if rank is None else rank,
                from_seat=target if meld_from_seat is None else meld_from_seat,
            ),
            0,
        )
    )
    if caller is actor and rinshan:
        stream.append(DrawEvidence(actor, DrawSource.RINSHAN))
    return tuple(stream)


def _responders(source: Seat) -> tuple[Seat, ...]:
    from lisjong_engine.reaction import reaction_seat_order

    return reaction_seat_order(source)


@cache
def _base_raw_game(base_seed: int, kan: bool) -> RawGame:
    """kan evidenceの有無だけが違うsynthetic raw game。"""
    halted = halt_at_turn_anchor(base_seed)
    observation = halted.observation
    round_state = halted.round_state
    checkpoint_evidence = build_round_evidence(round_state, observation.viewer_seat)
    terminal = RoundEndedEvidence(kind=RoundEndKind.EXHAUSTIVE_DRAW)
    extra = (
        declared_kan_stream(PublicMeldType.ANKAN, actor=observation.viewer_seat)
        if kan
        else ()
    )
    streams = tuple(
        ViewerEvidence(
            viewer,
            build_round_evidence(round_state, viewer) + extra + (terminal,),
        )
        for viewer in Seat
    )
    checkpoint = DecisionCheckpoint(
        checkpoint_index=0,
        round_revision=round_state.revision,
        observation=observation,
        evidence_cutoff=len(checkpoint_evidence),
    )
    truth = CheckpointTruth(
        checkpoint_index=0,
        viewer_seat=observation.viewer_seat,
        opponents=tuple(
            OpponentConcealedTruth(
                seat,
                tuple(
                    sorted(
                        (public_tile(value) for value in round_state.hand_tiles(seat)),
                        key=lambda value: (value.tile_type.id, value.is_red),
                    )
                ),
            )
            for seat in Seat
            if seat is not observation.viewer_seat
        ),
    )
    raw_round = RawRound(
        round_index=0,
        prevailing_wind=observation.prevailing_wind,
        hand_number=observation.hand_number,
        dealer_seat=observation.dealer_seat,
        honba=observation.honba,
        viewer_evidence=streams,
        checkpoints=(checkpoint,),
        training_truth=(truth,),
    )
    return RawGame(base_seed, (raw_round,))


def kan_coverage_corpus(*, kan: bool = True) -> RawCorpus:
    """locked successor seed populationのsynthetic raw corpus。"""
    base = _base_raw_game(KAN_COVERAGE_BASE_SEED, kan)
    return RawCorpus(
        resolved_provenance(),
        tuple(replace(base, seed=seed) for seed in ORDERED_SEEDS),
    )


def kan_coverage_artifacts(root, *, kan: bool = True):
    """1 populationのpersisted raw corpusとPhase 5 datasetを作る。"""
    persisted_raw = save_raw_corpus(kan_coverage_corpus(kan=kan), root / "raw")
    report = run_phase5_pipeline(persisted_raw, root / "dataset", SPLIT_POLICY)
    return persisted_raw, report.persisted_dataset.dataset


def population_manifest_value(
    *,
    raw_corpus_identity: str = "a" * 64,
    dataset_identity: str = "b" * 64,
    fully_resolved: bool = True,
    hanchan: int = PILOT_HANCHAN,
) -> dict:
    """schema上well-formedなsuccessor population manifest。

    24 hanchanを実行せずにmanifest validatorとclassification ruleの境界だけを
    固定するためのfixtureである。
    """
    plan = kan_coverage_population_plan()
    return {
        "manifest_schema_version": MANIFEST_SCHEMA_VERSION,
        "pilot_role": PILOT_ROLE,
        "retry_rule": RETRY_RULE,
        "classification_rule": CLASSIFICATION_RULE,
        "population_identity": plan.population_identity,
        "population_plan": plan.plan_value(),
        "raw_corpus_identity": raw_corpus_identity,
        "dataset_identity": dataset_identity,
        "split_policy_id": SPLIT_POLICY.value,
        "provenance": {
            "source_revisions": {
                "lisjong": "1" * 40,
                "lisjong_engine": "2" * 40,
                "lisjong_arena": "3" * 40,
            },
            "fully_resolved": fully_resolved,
            "anchor_semantics_id": "turn-pre-action-frozen-anchor-v1",
            "evidence_cutoff_semantics_id": "anchor-time-round-evidence-prefix-v1",
            "label_semantics_id": "exact-concealed-count-red-structural-wait-v1",
            "effective_rules": {
                "name": "project-standard-v1",
                "version": 1,
                "fingerprint": "f" * 64,
            },
        },
        "generation_runtime": {"python": "3.14.0", "platform": "test", "cpu": 4},
        "coverage": {
            "events": {
                "hanchan": hanchan,
                "rounds": 240,
                "daiminkan": 4,
                "ankan": 5,
                "kakan": 0,
                "rinshan_draw": 9,
                "stable_turn_anchors": 10_000,
            }
        },
        "cost": {"hanchan": hanchan},
        "conditional_uniform_baseline": {},
        "kan_opportunity_diagnostic": kan_opportunity_diagnostic_value(),
        "kan_accounting": {"totals": kan_accounting_totals()},
        "dataset_retention": {
            "kan_containing_game_seeds": [306],
            "kan_containing_games_retained": 1,
            "kan_containing_games_dropped": 0,
        },
        "observed_rates": {"hanchan": hanchan},
        "test_partition_present": False,
    }


def kan_opportunity_diagnostic_value(
    *,
    daiminkan: tuple[int, int] = (6, 4),
    ankan: tuple[int, int] = (5, 5),
    kakan: tuple[int, int] = (0, 0),
    violations: int = 0,
    unconverted: dict[str, int] | None = None,
) -> dict:
    """`(eligible no-win opportunities, selected)`だけを動かすdiagnostic value。

    `unconverted`は、そのkindを含むeligible decisionのうちkanを一切選ばなかった
    decision数（decision-level contract violationとの交差）である。
    """
    pairs = {"daiminkan": daiminkan, "ankan": ankan, "kakan": kakan}
    unconverted = unconverted or {}
    return {
        "diagnostic_schema_version": DIAGNOSTIC_SCHEMA_VERSION,
        "total_decisions": 10_000,
        "selection_contract_violations": violations,
        "by_kind": {
            kind: {
                "legal_opportunities": eligible,
                "legal_candidate_actions": eligible,
                "legal_opportunities_with_winning_action": 0,
                "eligible_no_win_opportunities": eligible,
                "eligible_no_win_opportunities_without_kan_selection": (
                    unconverted.get(kind, 0)
                ),
                "selected": selected,
            }
            for kind, (eligible, selected) in pairs.items()
        },
    }


def kan_accounting_totals(
    *,
    selected: int = 9,
    confirmed: int = 9,
    explicit_non_confirm: int = 0,
    unaccounted: int = 0,
    rinshan_missing: int = 0,
) -> dict:
    return {
        "selected": selected,
        "confirmed": confirmed,
        "explicit_non_confirm": explicit_non_confirm,
        "unaccounted": unaccounted,
        "confirmed_with_expected_rinshan_continuation": confirmed,
        "confirmed_without_expected_continuation": 0,
        "rinshan_observed": confirmed - rinshan_missing,
        "rinshan_missing": rinshan_missing,
    }
