"""Issue #406 — v1 summaryを補う、事前宣言したunconditional paired metric。

v1 summary（``summary.py``）は局収支・和了・turn別cumulative indicatorだけを
pairedにする。#406の比較表は聴牌到達・立直・放銃等もseed block単位の差で
報告するため、同じ保存済みarmから次の**全局で定義できる**per-kyoku量を
同じ``paired_evaluation``のmechanicsで要約する。v1のprotocol・manifest・
record・summaryは変更しない。

```text
formal_tenpai_reached   形式聴牌へ到達した（0/1）
riichi_declared         立直を宣言した（0/1）
deal_in                 放銃した（0/1）
deal_in_loss            放銃による失点（放銃しなければ0）
win_points              和了による得点（和了しなければ0）
abortive_draw           途中流局で終局した（0/1）
```

条件付き平均（聴牌局の初聴牌turn、和了局の和了turn / 和了点等）はpairedに
しない。v1 summaryのarm profileに記述値として現れる。判定labelは作らない。

```text
python -m lisjong_arena.pure_offense_benchmark.supplementary \
    ARM_DIR [ARM_DIR ...] [--out JSON]
```
"""

from __future__ import annotations

import argparse
from collections.abc import Callable, Sequence
from pathlib import Path

from lisjong_arena import paired_evaluation
from lisjong_arena._artifact_io import canonical_json_text, write_new_artifact_file

from .artifact import LoadedBenchmarkArm, load_benchmark_arm
from .record import TERMINATION_ABORTIVE_DRAW
from .summary import ROTATION_COUNT, PureOffenseSummaryError, _kyoku, _require_pairable

SUPPLEMENTARY_VERSION = 1

_Row = tuple  # (KyokuOffenseRecord, SeatRoundStats, SeatOffenseFacts)

SUPPLEMENTARY_METRICS: dict[str, tuple[str, Callable[[_Row], float]]] = {
    "formal_tenpai_reached": (
        "rate",
        lambda row: float(row[1].first_tenpai_turn is not None),
    ),
    "riichi_declared": ("rate", lambda row: float(row[2].riichi_turn is not None)),
    "deal_in": ("rate", lambda row: float(row[1].dealt_in)),
    "deal_in_loss": (
        "points_per_kyoku",
        lambda row: float(row[1].deal_in_loss if row[1].dealt_in else 0),
    ),
    "win_points": (
        "points_per_kyoku",
        lambda row: float(row[1].win_points if row[1].won else 0),
    ),
    "abortive_draw": (
        "rate",
        lambda row: float(row[0].facts.termination == TERMINATION_ABORTIVE_DRAW),
    ),
}


def _block_means(
    rows: Sequence[_Row], value: Callable[[_Row], float]
) -> tuple[tuple[int, float], ...]:
    blocks: list[tuple[int, float]] = []
    for offset in range(0, len(rows), ROTATION_COUNT):
        block = rows[offset : offset + ROTATION_COUNT]
        seed = block[0][0].seed
        if tuple((row[0].seed, row[0].rotation) for row in block) != tuple(
            (seed, rotation) for rotation in range(ROTATION_COUNT)
        ):
            raise PureOffenseSummaryError("seed blocks must be rotations 0..3 in order")
        blocks.append((seed, sum(value(row) for row in block) / ROTATION_COUNT))
    return tuple(blocks)


def build_supplementary(arms: Sequence[LoadedBenchmarkArm]) -> dict[str, object]:
    """arms（v1 summaryと同じlineage順）の補助paired比較。"""
    arms = tuple(arms)
    _require_pairable(arms)
    rows = [_kyoku(arm) for arm in arms]
    blocks = {
        (index, metric): _block_means(arm_rows, value)
        for index, arm_rows in enumerate(rows)
        for metric, (_, value) in SUPPLEMENTARY_METRICS.items()
    }
    profiles = [
        {
            "focal_identity": arm.focal_identity,
            "kyoku_count": len(arm_rows),
            "per_kyoku_mean": {
                metric: sum(value(row) for row in arm_rows) / len(arm_rows)
                for metric, (_, value) in SUPPLEMENTARY_METRICS.items()
            },
        }
        for arm, arm_rows in zip(arms, rows, strict=True)
    ]
    comparisons = []
    for i in range(len(arms)):
        for j in range(i + 1, len(arms)):
            metrics: dict[str, object] = {}
            for metric, (unit, _) in SUPPLEMENTARY_METRICS.items():
                try:
                    summary = paired_evaluation.summarize_paired_deltas(
                        paired_evaluation.paired_deltas_from_block_means(
                            blocks[(j, metric)], blocks[(i, metric)]
                        )
                    )
                except paired_evaluation.PairedEvaluationError as exc:
                    raise PureOffenseSummaryError(f"{metric}: {exc}") from exc
                metrics[metric] = {
                    "ci95_lower": summary.interval_lower,
                    "ci95_upper": summary.interval_upper,
                    "mean_difference": summary.mean_delta,
                    "n_seed_blocks": summary.block_count,
                    "paired_sd": summary.sample_standard_deviation,
                    "standard_error": summary.standard_error,
                    "unit": unit,
                }
            comparisons.append(
                {
                    "difference": (
                        f"{arms[j].focal_identity} - {arms[i].focal_identity}"
                    ),
                    "metrics": metrics,
                    "other": arms[j].focal_identity,
                    "reference": arms[i].focal_identity,
                }
            )
    return {
        "arms": profiles,
        "comparisons": comparisons,
        "seed_allocation": arms[0].seed_allocation,
        "seed_block_count": len(arms[0].seeds),
        "supplementary_version": SUPPLEMENTARY_VERSION,
        "terminal_classification": None,
    }


def format_supplementary(document: dict[str, object]) -> str:
    lines = [
        "Pure-offense supplementary paired metrics (Issue #406) — descriptive",
        f"seed blocks: {document['seed_block_count']} (x4 rotations)",
        "",
    ]
    for profile in document["arms"]:  # type: ignore[union-attr]
        means = profile["per_kyoku_mean"]
        lines.append(
            f"[{profile['focal_identity']}] kyoku={profile['kyoku_count']}  "
            + "  ".join(f"{name} {value:.4f}" for name, value in means.items())
        )
    for comparison in document["comparisons"]:  # type: ignore[union-attr]
        lines.append("")
        lines.append(f"paired: {comparison['difference']}")
        for metric, value in comparison["metrics"].items():
            scale, suffix = (100, "pp") if value["unit"] == "rate" else (1, "")
            lines.append(
                f"  {metric:<22} {scale * value['mean_difference']:+.3f}{suffix} "
                f"[{scale * value['ci95_lower']:+.3f}, "
                f"{scale * value['ci95_upper']:+.3f}]  "
                f"sd {scale * value['paired_sd']:.3f}  N={value['n_seed_blocks']}"
            )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m lisjong_arena.pure_offense_benchmark.supplementary"
    )
    parser.add_argument("arms", nargs="+", type=Path)
    parser.add_argument("--out", default=None, type=Path)
    arguments = parser.parse_args(argv)
    document = build_supplementary(
        [load_benchmark_arm(path) for path in arguments.arms]
    )
    if arguments.out is not None:
        write_new_artifact_file(arguments.out, canonical_json_text(document))
    print(format_supplementary(document))
    if arguments.out is not None:
        print(f"supplementary_written={arguments.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
