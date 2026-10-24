"""CLI for the #400 shanten backend verification.

```text
python -m lisjong_arena.shanten_backend_verification verify-wheel <wheel>
python -m lisjong_arena.shanten_backend_verification probe --backend rust [--wheel <wheel>]
python -m lisjong_arena.shanten_backend_verification startup --repeat 10 --out f.json
python -m lisjong_arena.shanten_backend_verification games \\
    --policy placement-aware-speed-call --seeds 0 1 --backend rust \\
    --workers 1 --out <new dir>
python -m lisjong_arena.shanten_backend_verification compare <dir> <dir> \\
    [--expected-semantic 0:<sha256>]
python -m lisjong_arena.shanten_backend_verification report <run output dir>     [--hourly-usd 0.93]
```

``probe`` / ``games`` run the Rust backend only (#531): ``--backend python``
is refused with a migration message before anything runs, and
``LISJONG_SHANTEN_BACKEND`` must be unset or ``rust``.  ``startup`` measured
Python against Rust (#400) and is refused for the same reason; the saved #400
records stay readable with ``compare`` / ``report``.  Every command exits
non-zero on a verification failure or a mismatch; ``report`` exits 3 when the
evidence is incomplete or does not match the frozen plan.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .backend import (
    RETIRED_PYTHON_BACKEND_MESSAGE,
    ShantenBackendVerificationError,
    execution_backend_argument,
    require_shanten_backend,
    verify_installed_native,
    verify_wheel_file,
)
from .measure import compare_games, run_games
from .report import evaluate


def _seed_digest(value: str) -> tuple[int, str]:
    seed, _, digest = value.partition(":")
    if len(digest) != 64:
        raise argparse.ArgumentTypeError("expected SEED:SHA256")
    return int(seed), digest


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m lisjong_arena.shanten_backend_verification",
        description="Rust shanten backend verification (#400, Rust only since #531).",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    wheel = commands.add_parser("verify-wheel")
    wheel.add_argument("wheel", type=Path)

    probe = commands.add_parser("probe")
    probe.add_argument("--backend", required=True, type=execution_backend_argument)
    probe.add_argument(
        "--wheel",
        type=Path,
        default=None,
        help="also check the loaded extension files against this wheel",
    )

    startup = commands.add_parser("startup")
    startup.add_argument("--repeat", required=True, type=int)
    startup.add_argument("--out", required=True, type=Path)

    games = commands.add_parser("games")
    games.add_argument("--policy", required=True)
    games.add_argument("--seeds", required=True, type=int, nargs="+")
    games.add_argument("--game-mode", default="4p-red-half")
    games.add_argument("--backend", required=True, type=execution_backend_argument)
    games.add_argument("--workers", required=True, type=int)
    games.add_argument("--out", required=True, type=Path)

    compare = commands.add_parser("compare")
    compare.add_argument("left", type=Path)
    compare.add_argument("right", type=Path)
    compare.add_argument(
        "--expected-semantic", type=_seed_digest, action="append", default=[]
    )
    compare.add_argument("--out", type=Path, default=None)

    report = commands.add_parser("report")
    report.add_argument("run_directory", type=Path)
    report.add_argument("--hourly-usd", type=float, default=None)
    report.add_argument("--out", type=Path, default=None)
    return parser


def _emit(payload: dict[str, object], out: Path | None = None) -> None:
    text = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    if out is not None:
        if out.exists():
            raise FileExistsError(f"refusing to overwrite {out}")
        out.write_text(text, encoding="utf-8")
    sys.stdout.write(text)


def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        if arguments.command == "verify-wheel":
            _emit(verify_wheel_file(arguments.wheel))
        elif arguments.command == "probe":
            record = require_shanten_backend(arguments.backend)
            if arguments.wheel is not None:
                record["installed_wheel"] = verify_installed_native(arguments.wheel)
            _emit(record)
        elif arguments.command == "startup":
            # #400 startup alternates Python and Rust processes.
            raise ShantenBackendVerificationError(
                "startup compares the Python and Rust backends (#400); "
                + RETIRED_PYTHON_BACKEND_MESSAGE
            )
        elif arguments.command == "games":
            summary = run_games(
                policy=arguments.policy,
                seeds=arguments.seeds,
                game_mode=arguments.game_mode,
                backend=arguments.backend,
                workers=arguments.workers,
                out_dir=arguments.out,
            )
            _emit({key: summary[key] for key in ("backend", "games", "wall_s")})
        elif arguments.command == "report":
            report = evaluate(arguments.run_directory, hourly_usd=arguments.hourly_usd)
            _emit(report, arguments.out)
            if report["decision"] == "incomplete-evidence":
                print(
                    "INCOMPLETE EVIDENCE: no adoption decision; see evidence.problems",
                    file=sys.stderr,
                )
                return 3
        else:
            result = compare_games(
                arguments.left,
                arguments.right,
                expected_semantic=dict(arguments.expected_semantic),
            )
            _emit(result, arguments.out)
            return 0 if result["ok"] else 1
    except ShantenBackendVerificationError as error:
        print(f"SHANTEN BACKEND VERIFICATION FAILED: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
