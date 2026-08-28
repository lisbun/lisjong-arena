"""Phase 2 domain values -> Phase 3 source-specific JSON value mapping。"""

from typing import Any

from lisjong_engine.observation import SeatObservation
from lisjong_engine.public_state import PublicDiscard, PublicMeld, PublicTile
from lisjong_engine.round_evidence import (
    DiscardEvidence,
    DoraIndicatorRevealedEvidence,
    DrawEvidence,
    KanConfirmedEvidence,
    KanDeclaredEvidence,
    MeldCalledEvidence,
    ResponseEpochClosedEvidence,
    ResponseEpochOpenedEvidence,
    RiichiDeclaredEvidence,
    RiichiEstablishedEvidence,
    RiichiFailedEvidence,
    RoundEndedEvidence,
    RoundEvidence,
    RoundStartedEvidence,
)

from lisjong_arena.phase2_training_anchor.pipeline_provenance import (
    TrainingPipelineProvenance,
)
from lisjong_arena.phase2_training_anchor.rule_provenance import EffectiveRuleProvenance
from lisjong_arena.phase2_training_anchor.training_labels import OpponentIdentity
from lisjong_arena.phase2_training_anchor.training_sample import TrainingSample
from lisjong_arena.phase3_bootstrap_corpus.model import Phase3BootstrapArtifactError


def rule_provenance_to_dict(value: EffectiveRuleProvenance) -> dict[str, Any]:
    return {
        "fingerprint": value.fingerprint,
        "name": value.name,
        "version": value.version,
    }


def provenance_to_dict(value: TrainingPipelineProvenance) -> dict[str, Any]:
    revisions = value.source_revisions
    return {
        "anchor_semantics_id": value.anchor_semantics_id,
        "effective_rules": rule_provenance_to_dict(value.effective_rules),
        "evidence_cutoff_semantics_id": value.evidence_cutoff_semantics_id,
        "label_semantics_id": value.label_semantics_id,
        "source_revisions": {
            "lisjong": revisions.lisjong,
            "lisjong_arena": revisions.lisjong_arena,
            "lisjong_engine": revisions.lisjong_engine,
        },
    }


def tile_to_dict(tile: PublicTile) -> dict[str, Any]:
    return {"is_red": tile.is_red, "tile_type_id": tile.tile_type.id}


def discard_to_dict(discard: PublicDiscard) -> dict[str, Any]:
    return {
        "called_by": None if discard.called_by is None else discard.called_by.value,
        "is_riichi_declaration": discard.is_riichi_declaration,
        "is_tsumogiri": discard.is_tsumogiri,
        "order": discard.order,
        "tile": tile_to_dict(discard.tile),
    }


def meld_to_dict(meld: PublicMeld) -> dict[str, Any]:
    return {
        "called_tile": None if meld.called_tile is None else tile_to_dict(meld.called_tile),
        "from_seat": None if meld.from_seat is None else meld.from_seat.value,
        "meld_type": meld.meld_type.value,
        "tiles": [tile_to_dict(tile) for tile in meld.tiles],
    }


def observation_to_dict(observation: SeatObservation) -> dict[str, Any]:
    return {
        "dealer_seat": observation.dealer_seat.value,
        "decision_kind": observation.decision_kind.value,
        "discards": [
            {
                "discards": [discard_to_dict(item) for item in row.discards],
                "seat": row.seat.value,
            }
            for row in observation.discards
        ],
        "dora_indicators": [tile_to_dict(tile) for tile in observation.dora_indicators],
        "drawn_tile": (
            None
            if observation.drawn_tile is None
            else tile_to_dict(observation.drawn_tile)
        ),
        "hand_number": observation.hand_number,
        "hand_tiles": [tile_to_dict(tile) for tile in observation.hand_tiles],
        "honba": observation.honba,
        "melds": [
            {"melds": [meld_to_dict(item) for item in row.melds], "seat": row.seat.value}
            for row in observation.melds
        ],
        "prevailing_wind": observation.prevailing_wind.value,
        "remaining_live_wall_count": observation.remaining_live_wall_count,
        "riichi_states": [
            {"seat": row.seat.value, "status": row.status.value}
            for row in observation.riichi_states
        ],
        "riichi_sticks": observation.riichi_sticks,
        "scores": [
            {"points": row.points, "seat": row.seat.value} for row in observation.scores
        ],
        "viewer_seat": observation.viewer_seat.value,
    }


def evidence_to_dict(evidence: RoundEvidence) -> dict[str, Any]:
    if isinstance(evidence, RoundStartedEvidence):
        return {
            "dealer_seat": evidence.dealer_seat.value,
            "prevailing_wind": evidence.prevailing_wind.value,
            "type": "round_started",
        }
    if isinstance(evidence, DrawEvidence):
        return {
            "seat": evidence.seat.value,
            "source": evidence.source.value,
            "tile": None if evidence.tile is None else tile_to_dict(evidence.tile),
            "type": "draw",
        }
    if isinstance(evidence, DiscardEvidence):
        return {
            "is_riichi_declaration": evidence.is_riichi_declaration,
            "is_tsumogiri": evidence.is_tsumogiri,
            "order": evidence.order,
            "seat": evidence.seat.value,
            "tile": tile_to_dict(evidence.tile),
            "type": "discard",
        }
    if isinstance(evidence, ResponseEpochOpenedEvidence):
        return {
            "responder_seats": [seat.value for seat in evidence.responder_seats],
            "source_seat": evidence.source_seat.value,
            "trigger": evidence.trigger.value,
            "type": "response_epoch_opened",
        }
    if isinstance(evidence, ResponseEpochClosedEvidence):
        return {
            "outcome": evidence.outcome.value,
            "source_seat": evidence.source_seat.value,
            "trigger": evidence.trigger.value,
            "type": "response_epoch_closed",
        }
    if isinstance(evidence, MeldCalledEvidence):
        return {
            "called_discard_order": evidence.called_discard_order,
            "meld": meld_to_dict(evidence.meld),
            "seat": evidence.seat.value,
            "type": "meld_called",
        }
    if isinstance(evidence, KanDeclaredEvidence):
        return {
            "meld": meld_to_dict(evidence.meld),
            "seat": evidence.seat.value,
            "type": "kan_declared",
        }
    if isinstance(evidence, KanConfirmedEvidence):
        return {
            "meld": meld_to_dict(evidence.meld),
            "seat": evidence.seat.value,
            "type": "kan_confirmed",
        }
    if isinstance(evidence, RiichiDeclaredEvidence):
        return {
            "declaration_discard_order": evidence.declaration_discard_order,
            "seat": evidence.seat.value,
            "tile": tile_to_dict(evidence.tile),
            "type": "riichi_declared",
        }
    if isinstance(evidence, RiichiEstablishedEvidence):
        return {"seat": evidence.seat.value, "type": "riichi_established"}
    if isinstance(evidence, RiichiFailedEvidence):
        return {"seat": evidence.seat.value, "type": "riichi_failed"}
    if isinstance(evidence, DoraIndicatorRevealedEvidence):
        return {
            "indicator": tile_to_dict(evidence.indicator),
            "seat": evidence.seat.value,
            "type": "dora_indicator_revealed",
        }
    if isinstance(evidence, RoundEndedEvidence):
        return {
            "abortive_reason": (
                None
                if evidence.abortive_reason is None
                else evidence.abortive_reason.value
            ),
            "kind": evidence.kind.value,
            "source_seat": (
                None if evidence.source_seat is None else evidence.source_seat.value
            ),
            "type": "round_ended",
            "win_method": (
                None if evidence.win_method is None else evidence.win_method.value
            ),
            "winner_seats": [seat.value for seat in evidence.winner_seats],
        }
    raise Phase3BootstrapArtifactError(
        f"unsupported RoundEvidence type: {type(evidence).__name__}"
    )


def opponent_identity_to_dict(identity: OpponentIdentity) -> dict[str, Any]:
    return {
        "seat": int(identity.seat),
        "viewer_relative_offset": identity.viewer_relative_offset,
        "wind": identity.wind.value,
    }


def sample_to_dict(sample: TrainingSample) -> dict[str, Any]:
    anchor = sample.anchor
    labels = sample.labels
    return {
        "anchor": {
            "anchor_index": anchor.anchor_index,
            "anchor_kind": anchor.anchor_kind.value,
            "evidence": [evidence_to_dict(item) for item in anchor.evidence],
            "hand_number": anchor.hand_number,
            "honba": anchor.honba,
            "observation": observation_to_dict(anchor.observation),
            "round_revision": anchor.round_revision,
            "rule_provenance": rule_provenance_to_dict(anchor.rule_provenance),
            "source": {
                "game_seed": anchor.source.game_seed,
                "source_class": anchor.source.source_class,
            },
            "viewer_seat": anchor.viewer_seat.value,
        },
        "labels": {
            "anchor_identity": {
                "dealer_seat": int(labels.anchor_identity.dealer_seat),
                "game_seed": labels.anchor_identity.game_seed,
                "hand_number": labels.anchor_identity.hand_number,
                "honba": labels.anchor_identity.honba,
                "prevailing_wind": labels.anchor_identity.prevailing_wind.value,
                "round_revision": labels.anchor_identity.round_revision,
                "viewer_seat": int(labels.anchor_identity.viewer_seat),
            },
            "expected_counts": [
                {
                    "concealed_size": row.concealed_size,
                    "counts": list(row.counts),
                    "identity": opponent_identity_to_dict(row.identity),
                    "red_five_present": list(row.red_five_present),
                }
                for row in labels.expected_counts
            ],
            "structural_waits": [
                {
                    "identity": opponent_identity_to_dict(row.identity),
                    "mask": None if row.mask is None else list(row.mask),
                    "unavailable_reason": (
                        None
                        if row.unavailable_reason is None
                        else row.unavailable_reason.value
                    ),
                }
                for row in labels.structural_waits
            ],
        },
    }
