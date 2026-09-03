"""Synthetic Stage 2 dataset fixtures.

実RiichiEnv hanchanは1局あたり分単位のcostがかかるため、unit testでは
teacher実行を再現せず、契約上有効な合成rowでartifact / coverage / training
boundaryを検証する。production側へgeneric backend abstractionは導入しない。
"""

from lisjong_arena.learned_policy_stage2.artifact import Stage2DatasetWriter
from lisjong_arena.learned_policy_stage2.model import Stage2DecisionRow
from lisjong_arena.learned_policy_stage2.protocol import (
    FEATURE_DIMENSION,
    ORDERED_SEEDS,
    TEACHER_SOURCE_REVISION,
    VOCABULARY_SIZE,
    action_family,
    split_for_seed,
)

FIXTURE_PROVENANCE = {
    "execution_environment": "riichienv",
    "lisjong_arena_version": "0.1.0",
    "lisjong_arena_revision": "0" * 40,
    "lisjong_version": "0.1.0",
    "lisjong_revision": TEACHER_SOURCE_REVISION,
    "lisjong_engine_version": "0.1.0",
    "lisjong_engine_revision": "2" * 40,
    "riichienv_version": "0.4.8",
    "python_version": "3.14.0",
}

DISCARD_INDICES = (0, 2, 4, 6, 8, 10, 12, 14)
PASS_INDEX = 800
RIICHI_INDEX = 74


def legal_mask(indices) -> tuple[bool, ...]:
    chosen = set(indices)
    if not chosen or any(not 0 <= index < VOCABULARY_SIZE for index in chosen):
        raise ValueError("indices must be a non-empty set of vocabulary indices")
    return tuple(index in chosen for index in range(VOCABULARY_SIZE))


def feature_values(seed: int, ordinal: int) -> tuple[float, ...]:
    """decisionごとに異なる、決定的で有限なfeature vector。"""
    values = [0.0] * FEATURE_DIMENSION
    values[0] = float(seed % 7) / 8.0
    values[1] = float(ordinal % 5) / 8.0
    values[(seed * 31 + ordinal * 17) % FEATURE_DIMENSION] = 1.0
    return tuple(values)


def decision_row(
    seed: int,
    ordinal: int,
    *,
    legal_indices=None,
    teacher_index: int | None = None,
    values=None,
) -> Stage2DecisionRow:
    if legal_indices is None:
        legal_indices = (
            (PASS_INDEX,)
            if ordinal % 5 == 4
            else DISCARD_INDICES[: 2 + ordinal % 4] + (RIICHI_INDEX,)
        )
    mask = legal_mask(legal_indices)
    if teacher_index is None:
        ordered = tuple(sorted(set(legal_indices)))
        teacher_index = ordered[(seed + ordinal) % len(ordered)]
    return Stage2DecisionRow(
        seed=seed,
        split=split_for_seed(seed),
        step_ordinal=ordinal,
        decision_ordinal=ordinal,
        round_ordinal=ordinal // 4,
        round_wind="east",
        hand_number=1 + (ordinal // 4) % 4,
        honba=0,
        actor_seat=ordinal % 4,
        feature_values=feature_values(seed, ordinal) if values is None else values,
        legal_mask=mask,
        teacher_action_index=teacher_index,
        teacher_action_family=action_family(teacher_index),
    )


def write_synthetic_dataset(destination, *, rows_per_game: int = 12):
    """locked seed populationを満たす合成datasetを書き出し、readbackを返す。"""
    writer = Stage2DatasetWriter(destination, provenance=FIXTURE_PROVENANCE)
    try:
        for seed in ORDERED_SEEDS:
            writer.add_game(
                seed=seed,
                split=split_for_seed(seed),
                step_count=rows_per_game,
                scores=(25000, 25000, 25000, 25000),
                ranks=(1, 2, 3, 4),
                rows=(decision_row(seed, ordinal) for ordinal in range(rows_per_game)),
            )
        return writer.finalize()
    except BaseException:
        writer.discard()
        raise
