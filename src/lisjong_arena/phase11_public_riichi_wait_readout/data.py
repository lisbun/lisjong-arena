"""Player-safe eligibility, exact target mapping, and next-latent extraction."""

from dataclasses import dataclass

from lisjong.belief import wind_for_seat
from lisjong_engine.public_state import PublicMeldType, PublicRiichiStatus

from lisjong_arena.lisjong_engine.domain_conversion import seat_from_engine_seat
from lisjong_arena.phase5_belief_dataset.model import DatasetPartition
from lisjong_arena.phase6_snapshot.feature import build_phase6_snapshot_feature
from lisjong_arena.phase8_sequential.model import S2_LATENT_DIM
from lisjong_arena.phase8_sequential.rollout import (
    _initial_tensor_rows,
    _remap_tensor_rows,
    _step_tensors,
)
from lisjong_arena.stage3_optimization_saturation.experiment import (
    saturation_population_data,
)

from .model import assert_frozen_state_unchanged, freeze_e160, frozen_state_snapshot
from .protocol import TILE_KIND_COUNT, Phase11Error


@dataclass(frozen=True, slots=True)
class OpponentTarget:
    wind: str
    seat: int
    established: bool
    mask: tuple[int, ...] | None
    unavailable_reason: str | None
    riichi_junme: int | None
    open_closed: str

    @property
    def eligible(self) -> bool:
        return self.established and self.mask is not None


@dataclass(frozen=True, slots=True)
class LatentExample:
    partition: DatasetPartition
    source_class: str
    game_seed: int
    round_index: int
    anchor_identity: str
    depth: int
    opponent_winds: tuple[str, str, str]
    latent: object
    targets: tuple[OpponentTarget, OpponentTarget, OpponentTarget]


def _riichi_junme(anchor, seat_value: int, declaration_order: int) -> int:
    seat_discards = next(
        row
        for row in anchor.observation.discards
        if seat_from_engine_seat(row.seat).value == seat_value
    )
    junme = sum(
        discard.order <= declaration_order for discard in seat_discards.discards
    )
    if junme <= 0 or not any(
        discard.is_riichi_declaration and discard.order == declaration_order
        for discard in seat_discards.discards
    ):
        raise Phase11Error("established riichi lacks its public declaration discard")
    return junme


def _targets_for_step(step) -> tuple[OpponentTarget, OpponentTarget, OpponentTarget]:
    """Map public status and training target by explicit logical Wind identity."""
    feature = build_phase6_snapshot_feature(step.sample.anchor)
    if (
        len(feature.opponents) != 3
        or len(step.sample.labels.structural_waits) != 3
        or len(step.sample.labels.expected_counts) != 3
    ):
        raise Phase11Error(
            "public and label opponent axes must each contain three rows"
        )
    public_by_wind = {row.wind.value: row for row in feature.opponents}
    label_by_wind = {
        row.identity.wind.value: row for row in step.sample.labels.structural_waits
    }
    expected_by_wind = {
        row.identity.wind.value: row for row in step.sample.labels.expected_counts
    }
    winds = tuple(wind.value for wind in step.opponent_winds)
    if (
        len(set(winds)) != 3
        or set(public_by_wind) != set(winds)
        or set(label_by_wind) != set(winds)
        or set(expected_by_wind) != set(winds)
    ):
        raise Phase11Error("public, expected-count, and wait opponent Winds differ")
    rows = []
    meld_types = tuple(PublicMeldType)
    ankan_index = meld_types.index(PublicMeldType.ANKAN)
    for wind in winds:
        public = public_by_wind[wind]
        wait = label_by_wind[wind]
        expected = expected_by_wind[wind]
        if wait.identity != expected.identity:
            raise Phase11Error(
                "wait target row identity differs from expected-count row"
            )
        dealer = seat_from_engine_seat(step.sample.anchor.observation.dealer_seat)
        if wind_for_seat(wait.identity.seat, dealer) is not wait.identity.wind:
            raise Phase11Error("opponent seat and Wind identity are inconsistent")
        established = public.riichi_status is PublicRiichiStatus.ESTABLISHED
        declaration_order = public.riichi_declaration_discard_order
        junme = (
            _riichi_junme(
                step.sample.anchor, wait.identity.seat.value, declaration_order
            )
            if established
            else None
        )
        open_meld_count = (
            sum(public.meld_kind_counts) - public.meld_kind_counts[ankan_index]
        )
        mask = None if wait.mask is None else tuple(wait.mask)
        if mask is None:
            if wait.unavailable_reason is None:
                raise Phase11Error("unavailable wait mask lacks its explicit reason")
        elif (
            wait.unavailable_reason is not None
            or len(mask) != TILE_KIND_COUNT
            or any(type(value) is not int or value not in (0, 1) for value in mask)
        ):
            raise Phase11Error(
                "available structural wait must be an exact 34-kind binary mask"
            )
        rows.append(
            OpponentTarget(
                wind=wind,
                seat=wait.identity.seat.value,
                established=established,
                mask=mask,
                unavailable_reason=(
                    None
                    if wait.unavailable_reason is None
                    else wait.unavailable_reason.value
                ),
                riichi_junme=junme,
                open_closed="open" if open_meld_count else "closed",
            )
        )
    return tuple(rows)


def extract_frozen_latents(model, sequences: tuple) -> tuple[LatentExample, ...]:
    """Run Phase 8 self-rollout and bind each target to that step's next_latent."""
    if not sequences:
        raise Phase11Error("latent extraction requires sequences")
    import torch

    before = frozen_state_snapshot(model)
    if any(parameter.requires_grad for parameter in model.parameters()):
        raise Phase11Error("E160 must be frozen before latent extraction")
    model.to("cpu")
    model.eval()
    records = []
    with torch.no_grad():
        for sequence in sequences:
            rows_by_wind = None
            latent = None
            for depth, step in enumerate(sequence.steps, start=1):
                if rows_by_wind is None:
                    _initial, rows_by_wind = _initial_tensor_rows(step)
                    latent = torch.zeros((1, S2_LATENT_DIM), dtype=torch.float32)
                previous = _remap_tensor_rows(rows_by_wind, step.opponent_winds)
                features, row_marginals, column_marginals = _step_tensors(step)
                constrained, next_latent = model(
                    features,
                    previous,
                    latent,
                    row_marginals,
                    column_marginals,
                )
                # Target alignment is intentionally after the recurrent update.
                records.append(
                    LatentExample(
                        partition=sequence.partition,
                        source_class=sequence.key.game.source_class,
                        game_seed=sequence.key.game.game_seed,
                        round_index=sequence.key.round_index,
                        anchor_identity=step.example.identity,
                        depth=depth,
                        opponent_winds=tuple(
                            wind.value for wind in step.opponent_winds
                        ),
                        latent=next_latent[0].detach().clone(),
                        targets=_targets_for_step(step),
                    )
                )
                rows_by_wind = {
                    wind: constrained.allocation[0, index, :]
                    for index, wind in enumerate(step.opponent_winds)
                }
                latent = next_latent
    assert_frozen_state_unchanged(before, model)
    return tuple(records)


def partition_records(
    records: tuple[LatentExample, ...], partition: DatasetPartition
) -> tuple[LatentExample, ...]:
    selected = tuple(record for record in records if record.partition is partition)
    if not selected:
        raise Phase11Error(f"{partition.value} has no latent records")
    return selected


def frozen_latent_records(evidence):
    """Freeze the retained E160 and roll every retained sequence out exactly once.

    The frozen byte guard and the latent records are produced together so that
    every caller — the one-shot training path and the #209 result-only
    continuation alike — observes the same rollout order and the same snapshot.
    """
    full = saturation_population_data(evidence.raw, evidence.dataset)
    snapshot = freeze_e160(evidence.e160_model)
    records = extract_frozen_latents(
        evidence.e160_model, full.train_sequences + full.validation_sequences
    )
    return records, snapshot


def eligible_rows(records: tuple[LatentExample, ...]):
    return tuple(
        (record, index, target)
        for record in records
        for index, target in enumerate(record.targets)
        if target.eligible
    )


__all__ = [
    "LatentExample",
    "OpponentTarget",
    "eligible_rows",
    "extract_frozen_latents",
    "frozen_latent_records",
    "partition_records",
]
