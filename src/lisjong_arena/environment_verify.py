from __future__ import annotations

import argparse
from pathlib import Path

from lisjong_arena.environment_identity import (
    EnvironmentIdentityError,
    verify_environment,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m lisjong_arena.environment_verify",
        description=(
            "Verify installed lisjong VCS dependencies against pyproject.toml."
        ),
    )
    parser.add_argument(
        "--project",
        type=Path,
        default=Path("pyproject.toml"),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        result = verify_environment(arguments.project)
    except EnvironmentIdentityError as exc:
        print("ENVIRONMENT MISMATCH")
        print(f"  {exc}")
        print("  repair: recreate the virtualenv and reinstall this checkout")
        return 2

    if result.ok:
        print("ENVIRONMENT CONSISTENT")
        for identity in result.identities:
            print(f"  {identity.name:<14} {identity.revision} ({identity.version})")
        print("  pip check: OK")
        return 0

    print("ENVIRONMENT MISMATCH")
    for error in result.errors:
        print(f"  {error}")
    print("  repair: recreate the virtualenv and reinstall this checkout")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
