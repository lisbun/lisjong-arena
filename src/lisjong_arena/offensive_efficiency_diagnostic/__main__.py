"""Issue #256 post-merge operator CLI.

Example::

    python -m lisjong_arena.offensive_efficiency_diagnostic \
      --parent-artifact C:\\Dev\\lisjong-artifacts\\issue-252-progression-development\\phase-b-parent-C.json \
      --out C:\\Dev\\lisjong-artifacts\\issue-256-offensive-efficiency\\result.json \
      --workers 8

The command is diagnostic-only.  It consumes no fresh strength seeds and refuses
to run from a dirty or unmerged Arena revision.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from lisjong_arena._execution_safety import (
    require_clean_arena_head,
    require_merged_arena_revision,
    require_new_artifact_destinations,
)
from lisjong_arena.progression_development.paired import load_arm_artifact

from .analysis import (
    build_phase1_plan,
    require_trajectory_identity,
    run_phase1_parallel,
    run_phase2_parallel,
    select_phase2_samples,
)
from .artifact import build_artifact, load_artifact, save_artifact


def _progress(prefix: str):
    def report(completed: int, total: int) -> None:
        print(f"{prefix}: {completed}/{total}", flush=True)

    return report


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Replay the exact #252 parent population and collect #172 "
            "offensive-efficiency diagnostics."
        )
    )
    parser.add_argument("--parent-artifact", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--workers", required=True, type=int)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    head = require_clean_arena_head()
    require_merged_arena_revision(head, branch="main")
    require_new_artifact_destinations({"result": args.out}, required_names=("result",))

    # Strict-read before game 1.  The same source object is used by the
    # trajectory gate and Phase 2 sampled-game identity checks.
    historical_parent = load_arm_artifact(args.parent_artifact)
    plan = build_phase1_plan()

    phase1 = run_phase1_parallel(
        plan,
        max_workers=args.workers,
        progress_callback=_progress("phase1"),
    )
    require_trajectory_identity(phase1, historical_parent)

    samples = select_phase2_samples(phase1.records)
    phase2_records = run_phase2_parallel(
        plan,
        phase1_records=phase1.records,
        samples=samples,
        historical_parent=historical_parent,
        max_workers=args.workers,
        progress_callback=_progress("phase2"),
    )

    artifact = build_artifact(
        phase1=phase1,
        phase2_records=phase2_records,
        parent_artifact_path=args.parent_artifact,
    )
    save_artifact(artifact, args.out)
    verified = load_artifact(args.out, parent_artifact_path=args.parent_artifact)

    print(
        json.dumps(
            {
                "artifact": str(args.out),
                "choice_discard_decisions": (
                    verified.phase1_aggregate.choice_discard_decision_count
                ),
                "forced_discard_decisions": (
                    verified.phase1_aggregate.forced_discard_decision_count
                ),
                "games": verified.phase1_aggregate.game_count,
                "phase2_sample_count": verified.phase2_aggregate.sample_count,
                "result_identity": verified.result_identity,
                "trajectory_identity_passed": verified.trajectory_identity_passed,
            },
            ensure_ascii=False,
            sort_keys=True,
        ),
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
