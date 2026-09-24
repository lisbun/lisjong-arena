"""#372 L0.3 lisjong-engine focal outcome source runner (C0 / C2 shared).

Seed Registryで予約済みのallocationから、既存producer
``generate_engine_focal_outcome_source()``でengine sourceを1回生成し（``generate``）、
pinned lisjong consumerでstrict readして#79 §7のfactsを出す（``readback``）。
producer（``focal_outcome_source/``）と``seed_registry.py``は変更しない。

```text
# producer環境（lisjong aed9c84 / lisjong-engine 96b9796、clean Arena checkout）
python scripts/l03_engine_source_372.py generate \\
    --arena-checkout <clean checkout> --output-dir <new directory> \\
    --population-role CALIBRATION --owner-issue lisbun/lisjong-arena#372 \\
    --seed-ledger <live ledger（seed-registry branch）> \\
    --allocation CALIBRATION <allocation_identity> <authorizing ledger>

# consumer環境（lisjong 8d2ada48）
python scripts/l03_engine_source_372.py readback \\
    --source <output-dir>/source --generation <output-dir>/generation.json \\
    --output <new report path>
```

- ``--population-role SCIENTIFIC``ではTRAIN / SELECTの``--allocation``を渡す（C2）。
  game順は常にsplit順（CALIBRATION / TRAIN -> SELECT）、split内はseed membership順
- 生成はsequential（worker 1）。並列実行基盤は持たない
- 全allocationの``arena_revision``は実行checkoutのHEADと一致しなければならない
  （C0 / C2で同じfrozen producer SHAを使うため）
- ``readback``はlisjong以外をimportしない（consumer環境にArenaは不要）
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections import Counter
from importlib import metadata
from pathlib import Path

SPLIT_ORDER = ("CALIBRATION", "TRAIN", "SELECT")
ROLE_SPLITS = {"CALIBRATION": ("CALIBRATION",), "SCIENTIFIC": ("TRAIN", "SELECT")}
EXCLUDED_DIAGNOSTIC_SEEDS = range(910000, 910400)
"""#366 / #367 / #370 diagnostic seed。engine domainのRegistryには未登録。"""

CONSUMER_LISJONG_REVISION = "8d2ada48b90454d3018f60d3a8741d97f755e450"
SUPPORT_MINIMUM = 0.20
"""canonical-first / non-canonical-first selected rateの下限（uniform explorationの実行確認）。"""

C1_TRAIN_KYOKU = 4000
C1_SELECT_KYOKU = 1000

RESULT_PUBLISHED = "SOURCE PUBLISHED"
RESULT_C0_COMPLETE = "ENGINE C0 CALIBRATION COMPLETE"
RESULT_SCIENTIFIC_READY = "ENGINE SCIENTIFIC SOURCE READBACK PASS"
RESULT_STOP = "STOP / INVALID"


class RunnerError(RuntimeError):
    """#372 runner precondition / readback failure（STOP / INVALID）。"""


def _read_json(path) -> object:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _write_new(path, document: object) -> None:
    path = Path(path)
    if path.exists():
        raise FileExistsError(f"refusing to overwrite: {path}")
    path.write_text(
        json.dumps(document, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )


# ---------------------------------------------------------------------------
# generate（producer環境）
# ---------------------------------------------------------------------------


def plan_population(
    *,
    population_role: str,
    allocations,
    live_ledger: object,
    owner_issue: str,
    arena_revision: str,
):
    """予約済みallocationからgames / bindingsを作り、live authorityでfail closedする。

    ``allocations``は``(split, allocation_identity, authorizing_ledger)``の列。
    bindingは各allocationのauthorizing ledger snapshotから作る。
    """
    from lisjong_arena import seed_registry

    if population_role not in ROLE_SPLITS:
        raise RunnerError(f"unsupported population role {population_role!r}")
    by_split = {}
    for split, identity, authorizing in allocations:
        if split in by_split:
            raise RunnerError(f"duplicate {split} allocation")
        by_split[split] = (identity, authorizing)
    if set(by_split) != set(ROLE_SPLITS[population_role]):
        raise RunnerError(
            f"{population_role} requires exactly the "
            f"{'/'.join(ROLE_SPLITS[population_role])} allocations"
        )
    games: list[tuple[int, str]] = []
    bindings: dict[str, object] = {}
    records: dict[str, dict[str, object]] = {}
    for split in (name for name in SPLIT_ORDER if name in by_split):
        identity, authorizing = by_split[split]
        try:
            binding = seed_registry.allocation_binding(authorizing, identity)
            record = seed_registry.find_allocation(live_ledger, identity)
            seeds = seed_registry.seeds_from_membership(record["seed_membership"])
            seed_registry.require_allocation_binding(
                live_ledger,
                binding,
                seeds=seeds,
                owner_issue=owner_issue,
                seed_domain=seed_registry.LISJONG_ENGINE_HANCHAN_SEED_DOMAIN,
                split=split,
            )
        except seed_registry.SeedRegistryError as error:
            raise RunnerError(
                f"{split} allocation is not authorized: {error}"
            ) from error
        if record["arena_revision"] != arena_revision:
            raise RunnerError(
                f"{split} allocation arena_revision {record['arena_revision']} "
                f"is not the executing checkout {arena_revision}"
            )
        if any(seed in EXCLUDED_DIAGNOSTIC_SEEDS for seed in seeds):
            raise RunnerError(f"{split} overlaps the excluded diagnostic seed range")
        bindings[split] = binding
        records[split] = record
        games.extend((seed, split) for seed in seeds)
    if population_role == "CALIBRATION" and len(games) != 16:
        raise RunnerError("CALIBRATION requires exactly 16 games")
    return games, bindings, records


def generate(
    output_dir,
    *,
    population_role: str,
    games,
    allocation_bindings,
    allocation_records,
    source_contract,
    log=print,
) -> dict[str, object]:
    """既存producerで1回だけ生成し、``generation.json``をsourceの隣へ書く。"""
    from lisjong_arena.focal_outcome_source import engine_source

    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=False)
    started = time.perf_counter()
    game_seconds: list[float] = []
    last = [started]

    def on_game(ordinal, seed):
        now = time.perf_counter()
        game_seconds.append(round(now - last[0], 3))
        last[0] = now
        log(f"game {ordinal} seed {seed} done ({now - started:.1f}s)")

    log(f"starting game_ordinal 0 of {len(games)} ({population_role})")
    source = engine_source.generate_engine_focal_outcome_source(
        output / "source",
        population_role=population_role,
        games=games,
        allocation_bindings=allocation_bindings,
        source_contract=source_contract,
        on_game=on_game,
    )
    wall_seconds = round(time.perf_counter() - started, 3)
    # 公開後にもう一度、独立にstrict readbackする。
    if engine_source.verify_engine_focal_outcome_source(output / "source") != source:
        raise RunnerError("readback differs from the generated source")
    record = {
        "allocations": {
            split: {
                "allocation_identity": allocation_bindings[split][
                    "allocation_identity"
                ],
                "arena_revision": allocation_records[split]["arena_revision"],
                "owner_issue": allocation_records[split]["owner_issue"],
                "population": allocation_records[split]["population"],
                "protocol": allocation_records[split]["protocol"],
                "seed_membership": allocation_records[split]["seed_membership"],
                "state": allocation_records[split]["state"],
            }
            for split in allocation_bindings
        },
        "games": [
            {
                "decision_count": game.decision_count,
                "focal_seat": int(game.focal_seat),
                "game_ordinal": game.game_ordinal,
                "kyoku_count": game.kyoku_count,
                "multi_survivor_decision_count": game.multi_survivor_decision_count,
                "seconds": seconds,
                "seed": game.seed,
                "split": game.split,
            }
            for game, seconds in zip(source.games, game_seconds, strict=True)
        ],
        "hanchan": len(source.games),
        "population_role": population_role,
        "result": RESULT_PUBLISHED,
        "schema": engine_source.ENGINE_OUTCOME_SOURCE_SCHEMA,
        "source_contract": source_contract,
        "source_identity": source.identity,
        "wall_seconds": wall_seconds,
        "workers": 1,
    }
    _write_new(output / "generation.json", record)
    return record


def _generate_command(args) -> int:
    from lisjong_arena import seed_registry
    from lisjong_arena.focal_outcome_source import engine_source

    contract = engine_source.build_source_contract(args.arena_checkout)
    games, bindings, records = plan_population(
        population_role=args.population_role,
        allocations=[
            (split, identity, _read_json(ledger))
            for split, identity, ledger in args.allocation
        ],
        live_ledger=seed_registry.validate_ledger(_read_json(args.seed_ledger)),
        owner_issue=args.owner_issue,
        arena_revision=contract["arena_revision"],
    )
    record = generate(
        args.output_dir,
        population_role=args.population_role,
        games=games,
        allocation_bindings=bindings,
        allocation_records=records,
        source_contract=contract,
        log=lambda line: print(line, flush=True),
    )
    print(json.dumps({k: v for k, v in record.items() if k != "games"}, indent=2))
    return 0


# ---------------------------------------------------------------------------
# readback（consumer環境、lisjongだけをimportする）
# ---------------------------------------------------------------------------


def c1_sizing(unique_eligible_kyoku_count: int, hanchan_count: int) -> dict:
    """#372 §4の固定式。``k = unique / hanchan``、``N = 4 * ceil(K / (4k))``。

    floatを経由せず整数で計算する: ``K / (4k) = K * hanchan / (4 * unique)``。
    """
    if unique_eligible_kyoku_count <= 0 or hanchan_count <= 0:
        raise RunnerError("k == 0")

    def size(kyoku: int) -> int:
        return 4 * -(-kyoku * hanchan_count // (4 * unique_eligible_kyoku_count))

    return {
        "k": unique_eligible_kyoku_count / hanchan_count,
        "N_SELECT": size(C1_SELECT_KYOKU),
        "N_TRAIN": size(C1_TRAIN_KYOKU),
    }


def support_stops(summary: dict[str, object]) -> list[str]:
    """uniform residual explorationの実行確認（性能判定ではない）。"""
    if not summary["eligible_row_count"]:
        return ["no eligible rows"]
    stops = []
    if summary["canonical_first_selected_rate"] < SUPPORT_MINIMUM:
        stops.append("canonical-first selected < 20%")
    if summary["non_canonical_first_selected_rate"] < SUPPORT_MINIMUM:
        stops.append("non-canonical-first selected < 20%")
    return stops


def split_facts(summary: dict[str, object]) -> dict[str, object]:
    """``summarize_outcome_targets()``に#79 §7の比率を加える（excludedはsource全体で1回）。"""
    facts = {key: value for key, value in summary.items() if key != "excluded"}
    rows = summary["eligible_row_count"]
    kyokus = summary["unique_eligible_kyoku_count"]
    facts["eligible_rows_per_hanchan"] = rows / summary["hanchan_count"]
    facts["eligible_rows_per_eligible_kyoku"] = rows / kyokus if kyokus else None
    return facts


def assess(population_role: str, splits: dict[str, dict]) -> dict[str, object]:
    """support sanity checkと（CALIBRATIONなら）C1 sizingを機械的に決める。"""
    checked = "CALIBRATION" if population_role == "CALIBRATION" else "TRAIN"
    stops = [f"{checked}: {stop}" for stop in support_stops(splits[checked])]
    report: dict[str, object] = {"support_check_split": checked}
    if population_role == "CALIBRATION":
        facts = splits["CALIBRATION"]
        try:
            report["c1"] = c1_sizing(
                facts["unique_eligible_kyoku_count"], facts["hanchan_count"]
            )
        except RunnerError as error:
            stops.append(str(error))
        success = RESULT_C0_COMPLETE
    else:
        success = RESULT_SCIENTIFIC_READY
    report["hard_stops"] = stops
    report["result"] = RESULT_STOP if stops else success
    return report


def _consumer_revision() -> str:
    text = metadata.distribution("lisjong").read_text("direct_url.json")
    revision = None if text is None else json.loads(text).get("vcs_info", {})
    revision = None if revision is None else revision.get("commit_id")
    if revision != CONSUMER_LISJONG_REVISION:
        raise RunnerError(
            f"consumer lisjong {revision} is not {CONSUMER_LISJONG_REVISION}"
        )
    return revision


def readback(path, *, expected_identity: str | None = None) -> dict[str, object]:
    revision = _consumer_revision()
    from lisjong.learning import (
        ENGINE_OUTCOME_SOURCE_SCHEMA,
        OutcomeTargets,
        build_outcome_targets,
        read_outcome_source,
        summarize_outcome_targets,
    )

    source = read_outcome_source(path)
    if source.schema != ENGINE_OUTCOME_SOURCE_SCHEMA:
        raise RunnerError(f"not an engine source: {source.schema}")
    if source.population_role not in ROLE_SPLITS:
        raise RunnerError(f"unsupported population role {source.population_role}")
    if expected_identity is not None and source.identity != expected_identity:
        raise RunnerError("consumer identity differs from the producer record")
    targets = build_outcome_targets(source)
    splits = {}
    for split in ROLE_SPLITS[source.population_role]:
        rows = tuple(row for row in targets.rows if row.split == split)
        hanchan = sum(1 for game in source.games if game.split == split)
        if not hanchan:
            raise RunnerError(f"source has no {split} hanchan")
        splits[split] = split_facts(
            summarize_outcome_targets(
                OutcomeTargets(
                    source_identity=targets.source_identity,
                    rows=rows,
                    excluded=dict(targets.excluded),
                    hanchan_count=hanchan,
                )
            )
        )
    return {
        "allocation_bindings": source.allocation_bindings,
        "consumer_lisjong_revision": revision,
        "excluded": dict(targets.excluded),
        "focal_seat_counts": dict(
            sorted(Counter(str(int(game.focal_seat)) for game in source.games).items())
        ),
        "hanchan": len(source.games),
        "population_role": source.population_role,
        "schema": source.schema,
        "source_contract_digest": source.source_contract_digest,
        "source_identity": source.identity,
        "splits": splits,
        "target_identity": targets.target_identity,
        **assess(source.population_role, splits),
    }


def _readback_command(args) -> int:
    expected = None
    if args.generation:
        expected = _read_json(args.generation)["source_identity"]
    report = readback(args.source, expected_identity=expected)
    if args.output:
        _write_new(args.output, report)
    sys.stdout.write(json.dumps(report, indent=2, sort_keys=True) + "\n")
    return 0 if not report["hard_stops"] else 3


# ---------------------------------------------------------------------------


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)
    generate_parser = commands.add_parser("generate")
    generate_parser.add_argument("--arena-checkout", required=True)
    generate_parser.add_argument("--output-dir", required=True)
    generate_parser.add_argument(
        "--population-role", required=True, choices=sorted(ROLE_SPLITS)
    )
    generate_parser.add_argument("--owner-issue", required=True)
    generate_parser.add_argument("--seed-ledger", required=True)
    generate_parser.add_argument(
        "--allocation",
        nargs=3,
        action="append",
        required=True,
        metavar=("SPLIT", "ALLOCATION_IDENTITY", "AUTHORIZING_LEDGER"),
    )
    readback_parser = commands.add_parser("readback")
    readback_parser.add_argument("--source", required=True)
    readback_parser.add_argument("--generation")
    readback_parser.add_argument("--output")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "generate":
            return _generate_command(args)
        return _readback_command(args)
    except (RunnerError, FileExistsError) as error:
        print(f"{RESULT_STOP}: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
