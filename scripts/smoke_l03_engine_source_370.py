"""#370 diagnostic-only lisjong-engine focal outcome source smoke.

16 hanchan（diagnostic seed 910000..910015、focal seatごとに4 hanchan）を
``arena-offense-l0.3-lisjong-engine-focal-outcome-source-v1``として生成し、
strict readbackする。scientific producerではない。

- DIAGNOSTIC role: Seed Registry allocationなし。これらのseedはTRAIN / SELECT /
  strength evaluationに永久に使えない（#366 / #367と同じdiagnostic seed）
- target構築、training、strength比較は行わない

```text
python scripts/smoke_l03_engine_source_370.py \\
    --arena-checkout <clean checkout> --output-dir <new directory>
```

環境: pinned lisjong / lisjong-engine（``engine_source``の定数）をexact VCS
revisionでinstallし、``lisjong_arena``はclean ``--arena-checkout``からimportする。
"""

import argparse
import json
import sys
import time
from pathlib import Path

from lisjong_arena.focal_outcome_source import engine_source

SEED_BASE = 910000
GAME_COUNT = 16
RESULT_PASS = "ENGINE OUTCOME SOURCE SMOKE PASS"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--arena-checkout", required=True)
    parser.add_argument("--output-dir", required=True)
    args = parser.parse_args(argv)
    output = Path(args.output_dir)
    output.mkdir(parents=True, exist_ok=False)
    contract = engine_source.build_source_contract(args.arena_checkout)
    games = [(SEED_BASE + ordinal, "DIAGNOSTIC") for ordinal in range(GAME_COUNT)]
    started = time.perf_counter()

    def progress(ordinal, seed):
        elapsed = time.perf_counter() - started
        print(f"game {ordinal} seed {seed} done ({elapsed:.1f}s)", flush=True)

    source = engine_source.generate_engine_focal_outcome_source(
        output / "source",
        population_role=engine_source.DIAGNOSTIC_ROLE,
        games=games,
        allocation_bindings={},
        source_contract=contract,
        on_game=progress,
    )
    # 公開後にもう一度、独立にstrict readbackする。
    reread = engine_source.verify_engine_focal_outcome_source(output / "source")
    if reread != source:
        raise SystemExit("STOP / INVALID: readback differs from the generated source")
    summary = {
        "decisions": sum(game.decision_count for game in source.games),
        "games": [
            {
                "decision_count": game.decision_count,
                "focal_seat": int(game.focal_seat),
                "game_ordinal": game.game_ordinal,
                "kyoku_count": game.kyoku_count,
                "multi_survivor_decision_count": game.multi_survivor_decision_count,
                "seed": game.seed,
            }
            for game in source.games
        ],
        "kyokus": sum(game.kyoku_count for game in source.games),
        "multi_survivor_decisions": sum(
            game.multi_survivor_decision_count for game in source.games
        ),
        "result": RESULT_PASS,
        "schema": engine_source.ENGINE_OUTCOME_SOURCE_SCHEMA,
        "scientific": False,
        "source_contract": contract,
        "source_identity": source.identity,
        "wall_seconds": round(time.perf_counter() - started, 1),
    }
    (output / "summary.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(json.dumps({k: v for k, v in summary.items() if k != "games"}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
