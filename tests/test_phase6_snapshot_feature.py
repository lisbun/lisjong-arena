"""Phase 6 pure feature, leakage, ordering, and response-history contracts."""

import inspect
import subprocess
import sys
import unittest
from dataclasses import replace

from _phase4_raw_corpus_fixtures import direct_phase2_sample
from lisjong_engine.public_state import (
    PublicDiscard,
    PublicMeld,
    PublicMeldType,
    PublicRiichiStatus,
)
from lisjong_engine.reaction import reaction_seat_order
from lisjong_engine.round_evidence import (
    DiscardEvidence,
    KanConfirmedEvidence,
    KanDeclaredEvidence,
    MeldCalledEvidence,
    ResponseEpochClosedEvidence,
    ResponseEpochOpenedEvidence,
    ResponseOutcome,
    ResponseTrigger,
    RiichiDeclaredEvidence,
    RiichiEstablishedEvidence,
)
from lisjong_engine.tile import STANDARD_TILE_TYPES

from lisjong_arena.phase2_training_anchor.player_safe_anchor import (
    AnchorSourceIdentity,
)
from lisjong_arena.phase6_snapshot.feature import (
    FEATURE_SEMANTICS_ID,
    build_phase6_snapshot_feature,
)
from lisjong_arena.phase6_snapshot.tensor import FEATURE_DIM, tensor_values


def _replace_seat_value(values, seat, **changes):
    return tuple(
        replace(value, **changes) if value.seat is seat else value for value in values
    )


class Phase6SnapshotFeatureTest(unittest.TestCase):
    def setUp(self) -> None:
        self.sample = direct_phase2_sample()
        self.anchor = self.sample.anchor

    def test_formal_identity_axis_and_opponent_wind_order_are_fixed(self):
        feature = build_phase6_snapshot_feature(self.anchor)
        self.assertEqual(FEATURE_SEMANTICS_ID, "phase6-history-snapshot-v1")
        self.assertEqual(FEATURE_DIM, 919)
        self.assertEqual(len(tensor_values(feature)), FEATURE_DIM)
        self.assertEqual(
            tuple(value.wind.value for value in feature.opponents),
            tuple(
                wind.value
                for wind in type(feature.viewer_wind)
                if wind is not feature.viewer_wind
            ),
        )
        self.assertEqual(tensor_values(feature), tensor_values(feature))

    def test_base_import_is_torch_free_and_formal_cli_has_no_test_option(self):
        completed = subprocess.run(
            [
                sys.executable,
                "-c",
                "import sys; import lisjong_arena; assert 'torch' not in sys.modules",
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        from lisjong_arena.phase6_snapshot.__main__ import _parser

        destinations = {action.dest for action in _parser()._actions}
        self.assertNotIn("partition", destinations)
        self.assertNotIn("evaluate_test", destinations)

    def test_feature_builder_accepts_only_anchor_and_ignores_provenance_identity(self):
        self.assertEqual(
            tuple(inspect.signature(build_phase6_snapshot_feature).parameters),
            ("anchor",),
        )
        expected = build_phase6_snapshot_feature(self.anchor)
        changed = replace(
            self.anchor,
            source=AnchorSourceIdentity("different-public-source", 999_999),
            anchor_index=self.anchor.anchor_index + 100,
            round_revision=self.anchor.round_revision + 100,
        )
        self.assertEqual(build_phase6_snapshot_feature(changed), expected)

    def test_training_truth_and_future_values_cannot_change_frozen_feature(self):
        expected = build_phase6_snapshot_feature(self.anchor)
        changed_row = replace(
            self.sample.labels.expected_counts[0],
            counts=self.sample.labels.expected_counts[1].counts,
            concealed_size=self.sample.labels.expected_counts[1].concealed_size,
        )
        changed_labels = replace(
            self.sample.labels,
            expected_counts=(changed_row,) + self.sample.labels.expected_counts[1:],
        )
        changed_sample = object.__new__(type(self.sample))
        object.__setattr__(changed_sample, "anchor", self.anchor)
        object.__setattr__(changed_sample, "labels", changed_labels)
        object.__setattr__(changed_sample, "provenance", self.sample.provenance)
        future_evidence = self.anchor.evidence + self.anchor.evidence
        self.assertNotEqual(future_evidence, self.anchor.evidence)
        self.assertEqual(build_phase6_snapshot_feature(changed_sample.anchor), expected)
        self.assertEqual(build_phase6_snapshot_feature(self.anchor), expected)

    def _discard_epoch_anchor(
        self,
        *,
        outcome: ResponseOutcome,
        riichi: bool = False,
        caller=None,
    ):
        observation = self.anchor.observation
        source = next(
            seat
            for seat in type(observation.viewer_seat)
            if seat is not observation.viewer_seat
        )
        tile = observation.dora_indicators[0]
        discard = PublicDiscard(tile, False, 0, riichi, caller)
        discards = _replace_seat_value(
            observation.discards, source, discards=(discard,)
        )
        riichi_states = observation.riichi_states
        if riichi:
            riichi_states = _replace_seat_value(
                riichi_states,
                source,
                status=PublicRiichiStatus.ESTABLISHED,
            )
        evidence = self.anchor.evidence + (
            DiscardEvidence(source, tile, False, 0, riichi),
        )
        if riichi:
            evidence += (RiichiDeclaredEvidence(source, tile, 0),)
        evidence += (
            ResponseEpochOpenedEvidence(
                ResponseTrigger.DISCARD,
                source,
                reaction_seat_order(source),
            ),
            ResponseEpochClosedEvidence(ResponseTrigger.DISCARD, source, outcome),
        )
        if riichi:
            evidence += (RiichiEstablishedEvidence(source),)
        return replace(
            self.anchor,
            observation=replace(
                observation,
                discards=discards,
                riichi_states=riichi_states,
            ),
            evidence=evidence,
        ), source

    def test_no_public_response_is_structural_exposure_not_source_pass(self):
        anchor, source = self._discard_epoch_anchor(
            outcome=ResponseOutcome.NO_PUBLIC_RESPONSE
        )
        feature = build_phase6_snapshot_feature(anchor)
        source_row = next(
            value for value in feature.opponents if value.wind.value == "south"
        )
        self.assertEqual(sum(source_row.discard_no_public_response_counts), 0)
        exposed = sum(
            sum(value.discard_no_public_response_counts) for value in feature.opponents
        )
        self.assertEqual(exposed, 2)
        self.assertEqual(source.value, "south")

    def test_riichi_timing_comes_from_ordered_public_evidence(self):
        anchor, _source = self._discard_epoch_anchor(
            outcome=ResponseOutcome.NO_PUBLIC_RESPONSE,
            riichi=True,
        )
        feature = build_phase6_snapshot_feature(anchor)
        row = next(value for value in feature.opponents if value.wind.value == "south")
        self.assertEqual(row.riichi_status, PublicRiichiStatus.ESTABLISHED)
        self.assertEqual(row.riichi_declaration_present, 1)
        self.assertEqual(row.riichi_declaration_discard_order, 0)

    def test_call_and_kakan_history_use_public_evidence_positions(self):
        observation = self.anchor.observation
        seats = tuple(type(observation.viewer_seat))
        source, caller = seats[1], seats[2]
        base_feature = build_phase6_snapshot_feature(self.anchor)
        tile_index = next(
            index
            for index, count in enumerate(base_feature.remaining_tile_counts)
            if count == 4
        )
        tile = type(observation.dora_indicators[0])(STANDARD_TILE_TYPES[tile_index])
        discard = PublicDiscard(tile, False, 0, False, caller)
        discards = _replace_seat_value(
            observation.discards, source, discards=(discard,)
        )
        pon = PublicMeld(PublicMeldType.PON, (tile, tile, tile), source, tile)
        kakan = PublicMeld(PublicMeldType.KAKAN, (tile, tile, tile, tile), source, tile)
        melds = _replace_seat_value(observation.melds, caller, melds=(kakan,))
        evidence = self.anchor.evidence + (
            DiscardEvidence(source, tile, False, 0, False),
            ResponseEpochOpenedEvidence(
                ResponseTrigger.DISCARD, source, reaction_seat_order(source)
            ),
            ResponseEpochClosedEvidence(
                ResponseTrigger.DISCARD, source, ResponseOutcome.CALL
            ),
            MeldCalledEvidence(caller, pon, 0),
            KanDeclaredEvidence(caller, kakan),
            ResponseEpochOpenedEvidence(
                ResponseTrigger.KAKAN, caller, reaction_seat_order(caller)
            ),
            ResponseEpochClosedEvidence(
                ResponseTrigger.KAKAN,
                caller,
                ResponseOutcome.NO_PUBLIC_RESPONSE,
            ),
            KanConfirmedEvidence(caller, kakan),
        )
        feature = build_phase6_snapshot_feature(
            replace(
                self.anchor,
                observation=replace(observation, discards=discards, melds=melds),
                evidence=evidence,
            )
        )
        caller_row = next(
            value for value in feature.opponents if value.wind.value == "west"
        )
        self.assertEqual(caller_row.last_call_present, 1)
        self.assertEqual(caller_row.last_kan_present, 1)
        self.assertEqual(
            caller_row.meld_kind_counts[
                tuple(PublicMeldType).index(PublicMeldType.KAKAN)
            ],
            1,
        )
        self.assertEqual(
            sum(value.kakan_no_public_response_count for value in feature.opponents),
            2,
        )

    def test_broken_response_epoch_fails_closed(self):
        anchor, _source = self._discard_epoch_anchor(
            outcome=ResponseOutcome.NO_PUBLIC_RESPONSE
        )
        broken = replace(anchor, evidence=anchor.evidence[:-2] + anchor.evidence[-1:])
        with self.assertRaisesRegex(ValueError, "closing lacks an opening"):
            build_phase6_snapshot_feature(broken)


if __name__ == "__main__":
    unittest.main()
