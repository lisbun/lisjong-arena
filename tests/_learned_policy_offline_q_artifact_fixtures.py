"""Synthetic Offline Q dataset artifact fixtures.

実RiichiEnv hanchanは1局あたり分単位のcostがかかるため、artifact writer /
reader boundaryはunit testで契約上有効な合成macro-transition rowで検証し、
teacher実行を再現しない。
"""

from lisjong_arena.learned_policy_offline_q.artifact import OfflineQDatasetWriter
from lisjong_arena.learned_policy_offline_q.model import MacroTransitionRow
from lisjong_arena.learned_policy_offline_q.protocol import (
    DATASET_ORDERED_SEEDS,
    FEATURE_DIMENSION,
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

_LEGAL_INDICES = (0, 1, 2, 3)


def legal_mask(indices=_LEGAL_INDICES) -> tuple[bool, ...]:
    chosen = set(indices)
    return tuple(index in chosen for index in range(VOCABULARY_SIZE))


def feature_values(seed: int, ordinal: int) -> tuple[float, ...]:
    """decisionごとに異なる、決定的で有限なfeature vector。"""
    values = [0.0] * FEATURE_DIMENSION
    values[0] = float(seed % 7) / 8.0
    values[1] = float(ordinal % 5) / 8.0
    values[(seed * 31 + ordinal * 17) % FEATURE_DIMENSION] = 1.0
    return tuple(values)


def transition_row(
    seed: int, ordinal: int, *, rows_per_game: int
) -> MacroTransitionRow:
    behavior_index = _LEGAL_INDICES[ordinal % len(_LEGAL_INDICES)]
    terminal = ordinal == rows_per_game - 1
    kwargs = dict(
        seed=seed,
        split=split_for_seed(seed),
        round_ordinal=ordinal // 4,
        round_wind="east",
        hand_number=1 + (ordinal // 4) % 4,
        honba=0,
        actor_seat=0,
        step_ordinal=ordinal,
        decision_ordinal=ordinal,
        feature_values=feature_values(seed, ordinal),
        legal_mask=legal_mask(),
        behavior_action_index=behavior_index,
        behavior_action_family=action_family(behavior_index),
        reward=float(ordinal - (rows_per_game // 2)) / 10000.0,
        terminal=terminal,
    )
    if terminal:
        kwargs.update(
            next_step_ordinal=None,
            next_decision_ordinal=None,
            next_feature_values=None,
            next_legal_mask=None,
        )
    else:
        kwargs.update(
            next_step_ordinal=ordinal + 1,
            next_decision_ordinal=ordinal + 1,
            next_feature_values=feature_values(seed, ordinal + 1),
            next_legal_mask=legal_mask(),
        )
    return MacroTransitionRow(**kwargs)


def write_synthetic_dataset(destination, *, rows_per_game: int = 6):
    """locked seed populationを満たす合成datasetを書き出し、readbackを返す。"""
    writer = OfflineQDatasetWriter(destination, provenance=FIXTURE_PROVENANCE)
    try:
        for seed in DATASET_ORDERED_SEEDS:
            writer.add_game(
                seed=seed,
                split=split_for_seed(seed),
                scores=(25000, 25000, 25000, 25000),
                ranks=(1, 2, 3, 4),
                rows=(
                    transition_row(seed, ordinal, rows_per_game=rows_per_game)
                    for ordinal in range(rows_per_game)
                ),
            )
        return writer.finalize()
    except BaseException:
        writer.discard()
        raise
