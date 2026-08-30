"""Explicit Phase 7 preflight and one-shot formal evaluation CLI."""

import argparse
import json
import sys
from pathlib import Path

from .evaluation import evaluate_and_save, preflight_value, prepare_preflight


def _revision(value: str) -> str:
    if len(value) != 40 or any(
        character not in "0123456789abcdef" for character in value
    ):
        raise argparse.ArgumentTypeError("revision must be a full lowercase commit SHA")
    return value


def _common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--raw", required=True)
    parser.add_argument("--dataset", required=True)
    parser.add_argument("--model-artifact", required=True)
    parser.add_argument(
        "--phase5-report",
        required=True,
        help="Exact machine-readable Phase 5 pipeline report; rounded prose is invalid.",
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run the locked Phase 7 frozen snapshot TEST gate."
    )
    commands = parser.add_subparsers(dest="command", required=True)
    preflight = commands.add_parser(
        "preflight",
        help="Verify compatibility without materializing learned TEST features.",
    )
    _common(preflight)
    evaluate = commands.add_parser(
        "evaluate",
        help="Explicitly perform the formal learned TEST evaluation once.",
    )
    _common(evaluate)
    evaluate.add_argument("--result", required=True)
    evaluate.add_argument("--arena-revision", required=True, type=_revision)
    return parser


def _verify_runtime() -> None:
    import torch

    if sys.version_info[:2] != (3, 14):
        raise RuntimeError("formal Phase 7 requires CPython 3.14")
    if torch.__version__ != "2.13.0+cpu":
        raise RuntimeError("formal Phase 7 requires PyTorch 2.13.0 CPU")
    if torch.cuda.is_available():
        raise RuntimeError("formal Phase 7 is CPU-only")


def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    _verify_runtime()
    if arguments.command == "evaluate" and Path(arguments.result).exists():
        raise FileExistsError("Phase 7 result destination already exists")
    preflight = prepare_preflight(
        raw_path=arguments.raw,
        dataset_path=arguments.dataset,
        artifact_path=arguments.model_artifact,
        phase5_report_path=arguments.phase5_report,
    )
    if arguments.command == "preflight":
        value = preflight_value(preflight)
    else:
        value = evaluate_and_save(
            preflight,
            result_destination=arguments.result,
            creation_software_revision=arguments.arena_revision,
        )
    print(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
