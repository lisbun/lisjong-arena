"""Fixed Phase 3 bootstrap corpus generation CLI。"""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict

from lisjong_arena.phase3_bootstrap_corpus.generation import (
    generate_phase3_bootstrap_corpus,
    generate_phase3_reproducibility_check,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Generate the fixed Phase 3 first-party bootstrap corpus "
            "(seeds 1000..1007, TwoStepUkeirePolicy x4)."
        )
    )
    parser.add_argument("output", help="new JSON artifact path")
    parser.add_argument(
        "--repeat-output",
        help="optional second new path; run the same fixed spec twice and require equal digest",
    )
    return parser


def main() -> int:
    args = _parser().parse_args()
    if args.repeat_output:
        report = generate_phase3_reproducibility_check(args.output, args.repeat_output)
    else:
        report = generate_phase3_bootstrap_corpus(args.output)
    print(json.dumps(asdict(report), ensure_ascii=False, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
