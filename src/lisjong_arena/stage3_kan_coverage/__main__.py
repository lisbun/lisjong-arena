"""Narrow explicit Arena #146 coverage-source qualification CLI.

```text
plan      locked successor population planのexact identityを出力する
qualify   24 hanchanを生成し、diagnostics / accounting / classificationまで行う
classify  生成済みpopulation directoryからresult artifactを再構成する
```

seeds、split、population、roleはcaller optionにしない。結果を見てからseedを
追加・置換するoptionも持たない。
"""

import argparse
import json
import sys
from pathlib import Path

from lisjong_arena.stage3_kan_coverage.generation import (
    generate_kan_coverage_population,
    load_population,
    load_population_manifest,
)
from lisjong_arena.stage3_kan_coverage.population import kan_coverage_population_plan
from lisjong_arena.stage3_kan_coverage.result import (
    load_result,
    result_value,
    save_result,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Run the locked development-only kan coverage-source qualification "
            "pilot for Arena #146."
        )
    )
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("plan", help="Print the locked successor population plan.")
    qualify = commands.add_parser(
        "qualify", help="Generate the 24-hanchan population and classify it."
    )
    qualify.add_argument("--output", required=True)
    classify = commands.add_parser(
        "classify", help="Re-derive the result artifact from a generated population."
    )
    classify.add_argument("--population-dir", required=True)
    classify.add_argument("--result-dir", required=True)
    return parser


def _plan_command() -> dict[str, object]:
    plan = kan_coverage_population_plan()
    return {
        "population_identity": plan.population_identity,
        "population_plan": plan.plan_value(),
    }


def _qualify_command(arguments) -> dict[str, object]:
    destination = Path(arguments.output)
    report = generate_kan_coverage_population(destination)
    manifest, _persisted_raw, _persisted_dataset = load_population(destination)
    if manifest != report.manifest:
        raise SystemExit("the persisted manifest differs from the generated manifest")
    value = result_value(manifest)
    save_result(destination, value)
    return value


def _classify_command(arguments) -> dict[str, object]:
    manifest = load_population_manifest(Path(arguments.population_dir))
    result_directory = Path(arguments.result_dir)
    result_directory.mkdir(parents=True, exist_ok=True)
    save_result(result_directory, result_value(manifest))
    return load_result(result_directory)


def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    if arguments.command == "plan":
        value = _plan_command()
    elif arguments.command == "qualify":
        value = _qualify_command(arguments)
    else:
        value = _classify_command(arguments)
    json.dump(value, sys.stdout, ensure_ascii=False, indent=2, sort_keys=True)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
