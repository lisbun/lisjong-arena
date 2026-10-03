"""Generate a policy source record by teacher self-play on RiichiEnv.

Every seat runs a fresh instance of the same catalog teacher. Each hanchan is
executed once with traced execution; the rows are projected from that same
``LocalGameInspection``, so the teacher is never rerun for recording.
"""

from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path
from tempfile import TemporaryDirectory

from lisjong.policy_contract import Seat

from lisjong_arena.riichienv.local_game_runner import (
    LocalGameInspectionRecorder,
    LocalGameRunner,
)

from . import binding, record
from .errors import PolicySourceRecordError

MAX_PROCESS_WORKERS = 32


def run_recorded_game(teacher: str, seed: int):
    """Execute one teacher self-play hanchan with the inspection recorder."""
    spec = binding.resolve_teacher(teacher)
    recorder = LocalGameInspectionRecorder()
    result = LocalGameRunner(
        {seat: spec.factory() for seat in Seat},
        seed=seed,
        game_mode=record.GAME_MODE,
        inspection_recorder=recorder,
    ).run()
    return result, recorder.snapshot()


def run_unrecorded_game(teacher: str, seed: int):
    """Execute the same hanchan without recording (non-interference baseline)."""
    spec = binding.resolve_teacher(teacher)
    return LocalGameRunner(
        {seat: spec.factory() for seat in Seat},
        seed=seed,
        game_mode=record.GAME_MODE,
    ).run()


def _write_game(path, teacher, game_ordinal, split, seed):
    result, inspection = run_recorded_game(teacher, seed)
    return record.write_game(
        path,
        game_ordinal=game_ordinal,
        split=split,
        seed=seed,
        result=result,
        inspection=inspection,
    )


def generate(
    population,
    destination,
    *,
    teacher: str,
    project="pyproject.toml",
    ledger=None,
    workers: int = 1,
    progress=None,
):
    """Publish a complete, strict-read source record; existing paths are refused."""
    if type(workers) is not int or not 1 <= workers <= MAX_PROCESS_WORKERS:
        raise PolicySourceRecordError(
            f"workers must be an integer from 1 through {MAX_PROCESS_WORKERS}"
        )
    population = record.validate_population(population)
    if population["purpose"] == record.SCIENTIFIC:
        if ledger is None:
            raise PolicySourceRecordError("SCIENTIFIC generation requires the ledger")
        record.require_population_authority(population, ledger)
    destination = Path(destination)
    if destination.exists():
        raise FileExistsError(destination)
    contract = binding.source_contract(
        teacher, game_mode=record.GAME_MODE, project=project
    )
    games = record.ordered_games(population)
    if workers > len(games):
        raise PolicySourceRecordError("workers must not exceed the hanchan count")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(
        prefix=f".{destination.name}-staging-", dir=destination.parent
    ) as staging_root:
        staging = Path(staging_root) / "source-record"
        staging.mkdir()
        summaries = [None] * len(games)
        jobs = [
            (staging / f"game-{i:03d}", teacher, i, split, seed)
            for i, (split, seed) in enumerate(games)
        ]
        if workers == 1:
            for i, job in enumerate(jobs):
                summaries[i] = _write_game(*job)
                if progress is not None:
                    progress(i + 1, len(games))
        else:
            executor = ProcessPoolExecutor(max_workers=workers)
            futures = {
                executor.submit(_write_game, *job): i for i, job in enumerate(jobs)
            }
            try:
                for completed, future in enumerate(as_completed(futures), 1):
                    summaries[futures[future]] = future.result()
                    if progress is not None:
                        progress(completed, len(games))
            except BaseException:
                for future in futures:
                    future.cancel()
                raise
            finally:
                executor.shutdown(wait=True, cancel_futures=True)
        if any(summary is None for summary in summaries):
            raise PolicySourceRecordError("incomplete hanchan collection")
        manifest = record.write_manifest(staging, population, contract, summaries)
        if (
            record.read_source_record(staging, expected_population=population)
            != manifest
        ):
            raise PolicySourceRecordError("source record strict readback mismatch")
        staging.rename(destination)
    return manifest


__all__ = [
    "MAX_PROCESS_WORKERS",
    "generate",
    "run_recorded_game",
    "run_unrecorded_game",
]
