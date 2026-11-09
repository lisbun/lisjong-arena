"""#588 DEVELOPMENT-only Overall entry with a reduced seed-block count.

  python dev_entry.py --seed-blocks N <lock|run|verify> ...

Runs the real ``lisjong_arena.overall_champion_aabb`` CLI (lock v2, run with kyoku
records, result, strict verify) with the frozen seed-block count 100 replaced by N
(1..8) in this process only. It exists so the #588 supervisor can be exercised end to
end on already-used development seeds (e.g. 472005/472006 -> 8 games) without
touching unused formal seeds or running 400 games.

Everything it writes describes a different, reduced protocol (``seed_block_count`` N
in the lock and result) and is never a formal Overall result. The supervisor refuses
this entry for a FORMAL plan. The production protocol module is not changed.
"""

import sys

MAX_DEVELOPMENT_SEED_BLOCKS = 8


def _reduce(seed_blocks: int) -> None:
    from lisjong_arena.overall_champion_aabb import (
        __main__ as cli,
    )
    from lisjong_arena.overall_champion_aabb import (
        experiment,
        protocol,
        statistics,
    )

    counts = {
        "SEED_BLOCK_COUNT": seed_blocks,
        "HANCHAN_COUNT": seed_blocks * protocol.ROTATION_COUNT,
        "SEAT_RESULT_COUNT": seed_blocks * protocol.SEAT_RESULTS_PER_BLOCK,
    }
    for module in (protocol, experiment, statistics, cli):
        for name, value in counts.items():
            if hasattr(module, name):
                setattr(module, name, value)


def main(argv: list[str]) -> int:
    if len(argv) < 3 or argv[0] != "--seed-blocks" or not argv[1].isdecimal():
        print("usage: dev_entry.py --seed-blocks N <command> ...", file=sys.stderr)
        return 2
    seed_blocks = int(argv[1])
    if not 1 <= seed_blocks <= MAX_DEVELOPMENT_SEED_BLOCKS:
        print(
            f"--seed-blocks must be 1..{MAX_DEVELOPMENT_SEED_BLOCKS}", file=sys.stderr
        )
        return 2
    _reduce(seed_blocks)
    print(
        f"DEVELOPMENT reduced Overall entry: seed_blocks={seed_blocks} "
        "(not the formal protocol)",
        file=sys.stderr,
    )
    from lisjong_arena.overall_champion_aabb.__main__ import main as overall_main

    return overall_main(argv[2:])


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
