"""captureの行と半荘結果のjoin、rank contrast（D）と補正のcontrast（X）。

初回の対象はlearning engine AABB（``arena-learning-candidate-engine-aabb-v1``）の順位差だけ。

```text
半荘の結果   第1層の半荘row（局recordの`validate_hanchan_row`で検査、AABB layoutの鍵・seat）
join         captureの計画 == 半荘rowの (seed, placement) 列（順序つき、全6配置の完全なseed block）
             半荘ごとに局数・round_ordinal 1..n、各行の局の親・場風 == 局recordの値
D(s)         (R_B(s) − R_A(s)) / 12          learning engine AABBの`block_rank_totals`をそのまま使う
X(s)         (X_B(s) − X_A(s)) / 12          X_role = roleの12 seat-resultについて Σ_t c_t^{(seat)}
             c_t = lisjong #391の単位補正（κ = 1）。Dと同じ重み・同じ向き
```

部分的なblock、計画外の半荘、局・席の不一致はfail closedする（成功分だけを返さない）。
"""

from collections.abc import Sequence
from dataclasses import dataclass
from fractions import Fraction

from lisjong.draw_correction import unit_draw_corrections, v1_definition

from lisjong_arena.heuristic_candidate_engine_aabb.kyoku_record import (
    AABB_LAYOUT,
    PLACEMENT_CANDIDATE_SEATS,
    ROLE_CANDIDATE,
    ROLE_INCUMBENT,
    KyokuRecordError,
    validate_hanchan_row,
)
from lisjong_arena.learning_candidate_engine_aabb.protocol import (
    PROTOCOL_ID,
    SEAT_RESULTS_PER_ROLE_PER_BLOCK,
    LearningEngineAabbError,
)
from lisjong_arena.learning_candidate_engine_aabb.statistics import block_rank_totals
from lisjong_arena.lisjong_engine.live_draw_store import LiveDrawArtifact

from .projection import project_record

PLACEMENTS = len(PLACEMENT_CANDIDATE_SEATS)
SEAT_COUNT = 4


class DrawCorrectionJoinError(ValueError):
    """captureと半荘結果が同じ実行の完全なseed blockとして結べない場合。"""


@dataclass(frozen=True)
class BlockContrast:
    """1 seed blockのrank contrastと補正contrast（κ = 1）。"""

    seed: int
    d: Fraction
    x: Fraction
    rank_sum_a: int
    rank_sum_b: int
    draw_count: int
    drawer_corrections: tuple[Fraction, ...]
    """block内の全通常ツモのdrawer席の単位補正（観測順）。異常値の記述用。"""


@dataclass(frozen=True)
class JoinedBlocks:
    """join済みのseed block列と、そのcaptureのprovenance。"""

    protocol_id: str
    blocks: tuple[BlockContrast, ...]
    capture_payload_sha256: str
    capture_producer: dict
    value_definition: dict

    @property
    def seeds(self) -> tuple[int, ...]:
        return tuple(block.seed for block in self.blocks)


def _require_complete_blocks(plan: Sequence[tuple[int, int]]) -> list[int]:
    """計画が、seedごとにplacement 0..5を順に並べた完全なblockの列であることを要求する。"""
    if not plan or len(plan) % PLACEMENTS:
        raise DrawCorrectionJoinError(
            "the capture plan must hold 6 placements per seed"
        )
    seeds = []
    for start in range(0, len(plan), PLACEMENTS):
        block = plan[start : start + PLACEMENTS]
        seed = block[0][0]
        if [key for key in block] != [(seed, p) for p in range(PLACEMENTS)]:
            raise DrawCorrectionJoinError(
                f"seed block starting at plan[{start}] must hold placements 0..5 in order"
            )
        seeds.append(seed)
    if len(set(seeds)) != len(seeds):
        raise DrawCorrectionJoinError("a seed block appears more than once in the plan")
    return seeds


def _seat_corrections(
    records, kyokus, context: str
) -> tuple[list[Fraction], list[Fraction]]:
    """1半荘の行から、席ごとの Σ_t c_t と drawerの補正列を作る。局の親・場風を照合する。"""
    totals = [Fraction(0)] * SEAT_COUNT
    drawer_corrections = []
    for record in records:
        round_ordinal = record.identity["round_ordinal"]
        kyoku = kyokus[round_ordinal - 1]
        checkpoint = record.pre_draw["checkpoint"]
        if (
            checkpoint["dealer_seat"] != kyoku["dealer_seat"]
            or checkpoint["prevailing_wind"] != kyoku["prevailing_wind"]
        ):
            raise DrawCorrectionJoinError(
                f"{context} round {round_ordinal}: capture dealer / wind differ from the result"
            )
        projected = project_record(record)
        corrections = unit_draw_corrections(
            projected.probability, projected.value, projected.realized_category
        )
        for seat in range(SEAT_COUNT):
            totals[seat] += corrections[seat]
        drawer_corrections.append(corrections[projected.value.drawer_seat])
    return totals, drawer_corrections


def join_capture_with_hanchans(
    artifact: LiveDrawArtifact, hanchans: Sequence[dict]
) -> JoinedBlocks:
    """captureのartifactを、同じ実行の第1層の半荘row列へ結び、seed blockごとのD・Xを返す。"""
    if not isinstance(artifact, LiveDrawArtifact):
        raise DrawCorrectionJoinError("artifact must be a LiveDrawArtifact")
    manifest = artifact.manifest
    plan = [tuple(item) for item in manifest["plan"]]
    seeds = _require_complete_blocks(plan)

    rows = list(hanchans)
    try:
        rows = [validate_hanchan_row(row, AABB_LAYOUT) for row in rows]
    except KyokuRecordError as error:
        raise DrawCorrectionJoinError(f"hanchan row is invalid: {error}") from error
    keys = [(row["seed"], row["placement"]) for row in rows]
    if keys != plan:
        missing = [key for key in plan if key not in keys]
        extra = [key for key in keys if key not in plan]
        raise DrawCorrectionJoinError(
            "hanchan results differ from the capture plan "
            f"(missing={missing}, extra={extra}, or out of order)"
        )

    by_hanchan: dict[tuple[int, int], list] = {key: [] for key in plan}
    for record in artifact.records:
        key = (record.identity["seed"], record.identity["placement"])
        if key not in by_hanchan:
            raise DrawCorrectionJoinError(f"capture row {key} is outside the plan")
        by_hanchan[key].append(record)

    seat_totals: dict[tuple[int, int], list[Fraction]] = {}
    drawer_by_hanchan: dict[tuple[int, int], list[Fraction]] = {}
    for entry, row in zip(manifest["hanchans"], rows, strict=True):
        key = (row["seed"], row["placement"])
        context = f"seed {key[0]} placement {key[1]}"
        kyokus = row["kyokus"]
        ordinals = [kyoku["round_ordinal"] for kyoku in kyokus]
        if ordinals != list(range(1, len(entry["rounds"]) + 1)):
            raise DrawCorrectionJoinError(
                f"{context}: round ordinals differ between capture and result"
            )
        seat_totals[key], drawer_by_hanchan[key] = _seat_corrections(
            by_hanchan[key], kyokus, context
        )

    try:
        rank_blocks = block_rank_totals(rows)
    except LearningEngineAabbError as error:
        raise DrawCorrectionJoinError(f"rank contrast is invalid: {error}") from error

    blocks = []
    for seed, rank_block in zip(seeds, rank_blocks, strict=True):
        if rank_block["seed"] != seed:
            raise DrawCorrectionJoinError("rank blocks are out of plan order")
        role_totals = {ROLE_CANDIDATE: Fraction(0), ROLE_INCUMBENT: Fraction(0)}
        drawer_corrections = []
        for placement in range(PLACEMENTS):
            row = rows[seeds.index(seed) * PLACEMENTS + placement]
            totals = seat_totals[(seed, placement)]
            for seat, assignment in enumerate(row["seats"]):
                role_totals[assignment["role"]] += totals[seat]
            drawer_corrections.extend(drawer_by_hanchan[(seed, placement)])
        blocks.append(
            BlockContrast(
                seed=seed,
                d=Fraction(
                    rank_block["difference_ranks"], SEAT_RESULTS_PER_ROLE_PER_BLOCK
                ),
                x=(role_totals[ROLE_INCUMBENT] - role_totals[ROLE_CANDIDATE])
                / SEAT_RESULTS_PER_ROLE_PER_BLOCK,
                rank_sum_a=rank_block["rank_sum_a"],
                rank_sum_b=rank_block["rank_sum_b"],
                draw_count=len(drawer_corrections),
                drawer_corrections=tuple(drawer_corrections),
            )
        )
    return JoinedBlocks(
        protocol_id=PROTOCOL_ID,
        blocks=tuple(blocks),
        capture_payload_sha256=manifest["payload"]["sha256"],
        capture_producer=dict(manifest["producer"]),
        value_definition=v1_definition(),
    )


__all__ = [
    "BlockContrast",
    "DrawCorrectionJoinError",
    "JoinedBlocks",
    "join_capture_with_hanchans",
]
