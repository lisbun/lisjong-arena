"""Teacher replay verification of a policy source record.

Reading a stored action back only proves the file round-trips. This check
re-executes the bound teacher on every stored player-safe input and canonical
legal action set and requires the result to equal the stored selected action
for **every** decision. The legal actions are given in their stored canonical
order, not the runner's order, so an order-dependent teacher tie-break is
reported as a mismatch.
"""

from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

from lisjong.policy_contract import DecisionContext, execute_policy

from . import binding, record
from .errors import PolicySourceRecordError


def require_replay_runtime(manifest, current_teacher, current_runtime) -> None:
    """Fail closed unless the current teacher and runtime equal the bound ones."""
    contract = manifest["source_contract"]
    if current_teacher != contract["teacher"]:
        raise PolicySourceRecordError(
            "installed teacher differs from the source record"
        )
    for field in binding.REPLAY_RUNTIME_FIELDS:
        if current_runtime.get(field) != contract["runtime"].get(field):
            raise PolicySourceRecordError(
                f"replay runtime {field} differs from the source record"
            )


def replay_game(path, game, teacher: str):
    """Return ``(decisions, mismatches)`` for one game; fresh teacher per seat."""
    spec = binding.resolve_teacher(teacher)
    policies = {}
    decisions, mismatches = 0, []
    for row, policy_input, legal_actions, selected in record.iter_rows(
        Path(path), game
    ):
        seat = row["actor_seat"]
        policy = policies.setdefault(seat, spec.factory())
        replayed = execute_policy(policy, DecisionContext(policy_input, legal_actions))
        if replayed != selected:
            mismatches.append(
                {
                    "game_ordinal": row["game_ordinal"],
                    "decision_ordinal": row["decision_ordinal"],
                }
            )
        decisions += 1
    return decisions, mismatches


def replay_verify(path, *, project="pyproject.toml", workers: int = 1):
    """Strict-read, bind the runtime, replay all decisions; 0 mismatches required."""
    path = Path(path)
    manifest = record.read_source_record(path)
    teacher = manifest["source_contract"]["teacher"]["catalog_identity"]
    require_replay_runtime(
        manifest,
        binding.teacher_identity(teacher),
        binding.runtime_binding(project),
    )
    jobs = [
        (path / f"game-{i:03d}", game, teacher)
        for i, game in enumerate(manifest["games"])
    ]
    if workers == 1:
        results = [replay_game(*job) for job in jobs]
    else:
        with ProcessPoolExecutor(max_workers=workers) as executor:
            results = list(executor.map(replay_game, *zip(*jobs, strict=True)))
    decisions = sum(count for count, _ in results)
    mismatches = [item for _, found in results for item in found]
    if decisions != sum(game["decision_count"] for game in manifest["games"]):
        raise PolicySourceRecordError("replay decision accounting mismatch")
    summary = {
        "source_identity": manifest["identity"],
        "teacher": teacher,
        "decisions": decisions,
        "mismatches": len(mismatches),
        "first_mismatches": mismatches[:20],
    }
    if mismatches:
        raise PolicySourceRecordError(
            f"teacher replay mismatch: {len(mismatches)} of {decisions} decisions"
        )
    return summary


__all__ = ["replay_game", "replay_verify", "require_replay_runtime"]
