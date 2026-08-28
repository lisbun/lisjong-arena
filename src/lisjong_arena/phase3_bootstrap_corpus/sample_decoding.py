"""Strict Phase 3 sample JSON -> existing Phase 2 immutable values。"""

from lisjong.policy_contract import Seat as LisjongSeat
from lisjong.policy_contract import Wind as LisjongWind
from lisjong_engine.seat import Seat as EngineSeat

from lisjong_arena.phase2_training_anchor.pipeline_provenance import (
    TrainingPipelineProvenance,
)
from lisjong_arena.phase2_training_anchor.player_safe_anchor import (
    AnchorKind,
    AnchorSourceIdentity,
    FrozenPlayerSafeAnchor,
)
from lisjong_arena.phase2_training_anchor.training_labels import (
    ExactTrainingLabels,
    LabelAnchorIdentity,
    OpponentExpectedCounts,
    OpponentStructuralWait,
    StructuralWaitUnavailableReason,
)
from lisjong_arena.phase2_training_anchor.training_sample import TrainingSample
from lisjong_arena.phase3_bootstrap_corpus.decoding import (
    expect_bool,
    expect_int,
    expect_list,
    expect_nonempty_str,
    expect_object,
    parse_enum,
    parse_evidence,
    parse_nullable_enum,
    parse_observation,
    parse_opponent_identity,
    parse_rule_provenance,
)
from lisjong_arena.phase3_bootstrap_corpus.model import Phase3BootstrapArtifactError


def parse_labels(value: object, context: str) -> ExactTrainingLabels:
    raw = expect_object(
        value,
        {"anchor_identity", "expected_counts", "structural_waits"},
        context,
    )
    identity = expect_object(
        raw["anchor_identity"],
        {
            "dealer_seat",
            "game_seed",
            "hand_number",
            "honba",
            "prevailing_wind",
            "round_revision",
            "viewer_seat",
        },
        f"{context}.anchor_identity",
    )
    try:
        anchor_identity = LabelAnchorIdentity(
            game_seed=expect_int(
                identity["game_seed"], f"{context}.anchor_identity.game_seed"
            ),
            hand_number=expect_int(
                identity["hand_number"], f"{context}.anchor_identity.hand_number"
            ),
            honba=expect_int(identity["honba"], f"{context}.anchor_identity.honba"),
            round_revision=expect_int(
                identity["round_revision"],
                f"{context}.anchor_identity.round_revision",
            ),
            viewer_seat=LisjongSeat(
                expect_int(
                    identity["viewer_seat"],
                    f"{context}.anchor_identity.viewer_seat",
                )
            ),
            dealer_seat=LisjongSeat(
                expect_int(
                    identity["dealer_seat"],
                    f"{context}.anchor_identity.dealer_seat",
                )
            ),
            prevailing_wind=parse_enum(
                LisjongWind,
                identity["prevailing_wind"],
                f"{context}.anchor_identity.prevailing_wind",
            ),
        )
        expected_counts = tuple(
            _parse_expected_counts(row, f"{context}.expected_counts[{index}]")
            for index, row in enumerate(
                expect_list(raw["expected_counts"], f"{context}.expected_counts")
            )
        )
        waits = tuple(
            _parse_structural_wait(row, f"{context}.structural_waits[{index}]")
            for index, row in enumerate(
                expect_list(raw["structural_waits"], f"{context}.structural_waits")
            )
        )
        return ExactTrainingLabels(
            anchor_identity=anchor_identity,
            expected_counts=expected_counts,
            structural_waits=waits,
        )
    except (TypeError, ValueError) as exc:
        if isinstance(exc, Phase3BootstrapArtifactError):
            raise
        raise Phase3BootstrapArtifactError(f"{context} is invalid") from exc


def _parse_expected_counts(value: object, context: str) -> OpponentExpectedCounts:
    raw = expect_object(
        value,
        {"concealed_size", "counts", "identity", "red_five_present"},
        context,
    )
    return OpponentExpectedCounts(
        identity=parse_opponent_identity(raw["identity"], f"{context}.identity"),
        counts=tuple(
            expect_int(count, f"{context}.counts[{index}]")
            for index, count in enumerate(
                expect_list(raw["counts"], f"{context}.counts")
            )
        ),
        red_five_present=tuple(
            expect_bool(flag, f"{context}.red_five_present[{index}]")
            for index, flag in enumerate(
                expect_list(
                    raw["red_five_present"], f"{context}.red_five_present"
                )
            )
        ),
        concealed_size=expect_int(
            raw["concealed_size"], f"{context}.concealed_size"
        ),
    )


def _parse_structural_wait(value: object, context: str) -> OpponentStructuralWait:
    raw = expect_object(value, {"identity", "mask", "unavailable_reason"}, context)
    mask = raw["mask"]
    return OpponentStructuralWait(
        identity=parse_opponent_identity(raw["identity"], f"{context}.identity"),
        mask=(
            None
            if mask is None
            else tuple(
                expect_int(item, f"{context}.mask[{index}]")
                for index, item in enumerate(expect_list(mask, f"{context}.mask"))
            )
        ),
        unavailable_reason=parse_nullable_enum(
            StructuralWaitUnavailableReason,
            raw["unavailable_reason"],
            f"{context}.unavailable_reason",
        ),
    )


def parse_sample(
    value: object,
    context: str,
    provenance: TrainingPipelineProvenance,
) -> TrainingSample:
    raw = expect_object(value, {"anchor", "labels"}, context)
    anchor = expect_object(
        raw["anchor"],
        {
            "anchor_index",
            "anchor_kind",
            "evidence",
            "hand_number",
            "honba",
            "observation",
            "round_revision",
            "rule_provenance",
            "source",
            "viewer_seat",
        },
        f"{context}.anchor",
    )
    source = expect_object(
        anchor["source"], {"game_seed", "source_class"}, f"{context}.anchor.source"
    )
    try:
        frozen = FrozenPlayerSafeAnchor(
            source=AnchorSourceIdentity(
                source_class=expect_nonempty_str(
                    source["source_class"],
                    f"{context}.anchor.source.source_class",
                ),
                game_seed=expect_int(
                    source["game_seed"], f"{context}.anchor.source.game_seed"
                ),
            ),
            hand_number=expect_int(
                anchor["hand_number"], f"{context}.anchor.hand_number"
            ),
            honba=expect_int(anchor["honba"], f"{context}.anchor.honba"),
            round_revision=expect_int(
                anchor["round_revision"], f"{context}.anchor.round_revision"
            ),
            viewer_seat=parse_enum(
                EngineSeat, anchor["viewer_seat"], f"{context}.anchor.viewer_seat"
            ),
            anchor_kind=parse_enum(
                AnchorKind, anchor["anchor_kind"], f"{context}.anchor.anchor_kind"
            ),
            anchor_index=expect_int(
                anchor["anchor_index"], f"{context}.anchor.anchor_index"
            ),
            observation=parse_observation(
                anchor["observation"], f"{context}.anchor.observation"
            ),
            evidence=tuple(
                parse_evidence(item, f"{context}.anchor.evidence[{index}]")
                for index, item in enumerate(
                    expect_list(anchor["evidence"], f"{context}.anchor.evidence")
                )
            ),
            rule_provenance=parse_rule_provenance(
                anchor["rule_provenance"], f"{context}.anchor.rule_provenance"
            ),
        )
        if (
            frozen.hand_number != frozen.observation.hand_number
            or frozen.honba != frozen.observation.honba
        ):
            raise ValueError("anchor position does not match observation")
        return TrainingSample(
            anchor=frozen,
            labels=parse_labels(raw["labels"], f"{context}.labels"),
            provenance=provenance,
        )
    except (TypeError, ValueError) as exc:
        if isinstance(exc, Phase3BootstrapArtifactError):
            raise
        raise Phase3BootstrapArtifactError(f"{context} is invalid") from exc
