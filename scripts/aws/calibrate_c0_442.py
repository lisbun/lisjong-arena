"""Issue #442 — AWS calibration driver for the C0 policy source pipeline.

Development measurement only (purpose DEVELOPMENT, no seed allocation, no
strength claim). Run by ``bootstrap-c0-calibration-442.sh`` on one EC2
instance; see ``docs/aws-c0-calibration-442.md``.

``run`` (Arena venv) executes, in order:

1. generation: 200 C0 self-play hanchan, one traced execution per seed. The
   same inspection is written into the 200-hanchan record and, for its subset
   seeds, into the 100- and 50-hanchan records (no rerun). Each record is
   published with the production ``write_manifest`` / ``read_source_record``.
2. replay-verify of the 200-hanchan record (0 mismatches required), run
   concurrently with the Learning materialize steps of all three records.
3. BC / candidate scorer training and artifact verification per record.
4. 8 evaluation hanchan: the record-200 BC model in one seat, C0 in three.

Every Learning step runs as ``python -m lisjong.learning`` in the Learning venv
and is measured with ``os.wait4`` (wall, user / system CPU, max RSS, output
bytes). System memory in use (MemTotal - MemAvailable) is sampled every two
seconds. ``eval`` is the per-hanchan evaluation entry point run by the
Learning venv; it does not import the producer.
"""

import argparse
import json
import os
import platform
import resource
import shutil
import subprocess
import sys
import threading
import time
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

TEACHER = "placement-aware-speed-call"
BASE = 944600100
# 2 : 1 : 1 splits; the smaller records take the leading seeds of each split.
POPULATIONS = {
    "record200": {
        "TRAIN": list(range(BASE, BASE + 100)),
        "SELECT": list(range(BASE + 100, BASE + 150)),
        "OFFLINE-EVAL": list(range(BASE + 150, BASE + 200)),
    },
    "record100": {
        "TRAIN": list(range(BASE, BASE + 50)),
        "SELECT": list(range(BASE + 100, BASE + 125)),
        "OFFLINE-EVAL": list(range(BASE + 150, BASE + 175)),
    },
    "record50": {
        "TRAIN": list(range(BASE, BASE + 26)),
        "SELECT": list(range(BASE + 100, BASE + 112)),
        "OFFLINE-EVAL": list(range(BASE + 150, BASE + 162)),
    },
}
EVAL_SEEDS = list(range(BASE + 200, BASE + 208))
BC_WIDTH = 512
CANDIDATE_WIDTH = 256
EPOCHS = 20
MEMORY_SAMPLE_SECONDS = 2.0


class MemorySampler:
    """Peak system memory in use per phase, from /proc/meminfo."""

    def __init__(self):
        self.phase = "setup"
        self.peaks = {}
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True)

    @staticmethod
    def used_kb():
        info = {}
        with open("/proc/meminfo") as handle:
            for line in handle:
                key, value = line.split(":", 1)
                info[key] = int(value.split()[0])
        return info["MemTotal"] - info["MemAvailable"], info["MemTotal"]

    def _loop(self):
        while not self._stop.is_set():
            used, _ = self.used_kb()
            self.peaks[self.phase] = max(self.peaks.get(self.phase, 0), used)
            self._stop.wait(MEMORY_SAMPLE_SECONDS)

    def start(self):
        self._thread.start()

    def stop(self):
        self._stop.set()
        self._thread.join()


def _bytes(path):
    path = Path(path)
    if path.is_file():
        return path.stat().st_size
    return sum(p.stat().st_size for p in path.rglob("*") if p.is_file())


def _ordinals(name):
    from lisjong_arena.policy_source_record import record

    games = record.ordered_games(
        record.population_document(record.DEVELOPMENT, POPULATIONS[name])
    )
    return {seed: (i, split) for i, (split, seed) in enumerate(games)}


def _generate_one(seed, targets):
    """One traced hanchan, written into every record whose population has it.

    ``targets`` maps record name to ``(stage, game_ordinal, split)`` and is
    computed by the parent, so workers do not re-derive the populations.
    """
    from lisjong_arena.policy_source_record import generation, record

    u0 = resource.getrusage(resource.RUSAGE_SELF)
    t0 = time.perf_counter()
    result, inspection = generation.run_recorded_game(TEACHER, seed)
    t1 = time.perf_counter()
    u1 = resource.getrusage(resource.RUSAGE_SELF)
    summaries = {
        name: record.write_game(
            Path(stage) / f"game-{i:03d}",
            game_ordinal=i,
            split=split,
            seed=seed,
            result=result,
            inspection=inspection,
        )
        for name, (stage, i, split) in targets.items()
    }
    t2 = time.perf_counter()
    return {
        "seed": seed,
        "game_wall": t1 - t0,
        "game_user": u1.ru_utime - u0.ru_utime,
        "game_sys": u1.ru_stime - u0.ru_stime,
        "write_wall": t2 - t1,
        "maxrss_kb": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
        "steps": result.steps,
        "decisions": result.decisions,
        "kyoku": len(inspection.round_results),
        "summaries": summaries,
    }


def generate(work, project, workers, progress):
    from lisjong_arena.policy_source_record import binding, record

    contract = binding.source_contract(
        TEACHER, game_mode=record.GAME_MODE, project=project
    )
    stages = {name: work / f".stage-{name}" for name in POPULATIONS}
    for stage in stages.values():
        stage.mkdir()
    ordinals = {name: _ordinals(name) for name in POPULATIONS}
    seeds = sorted(ordinals["record200"])
    targets = {
        seed: {
            name: (stages[name], *ordinals[name][seed])
            for name in POPULATIONS
            if seed in ordinals[name]
        }
        for seed in seeds
    }
    games = []
    t0 = time.perf_counter()
    pool = ProcessPoolExecutor(max_workers=workers, max_tasks_per_child=1)
    try:
        futures = [pool.submit(_generate_one, seed, targets[seed]) for seed in seeds]
        for done, future in enumerate(futures, 1):
            games.append(future.result())
            progress(f"generation {done}/{len(seeds)}")
    finally:
        # A failed hanchan stops the run: pending hanchan are not started.
        pool.shutdown(wait=True, cancel_futures=True)
    wall = time.perf_counter() - t0
    records = {}
    for name, stage in stages.items():
        population = record.population_document(record.DEVELOPMENT, POPULATIONS[name])
        summaries = [None] * len(ordinals[name])
        for game in games:
            if game["seed"] in ordinals[name]:
                summaries[ordinals[name][game["seed"]][0]] = game["summaries"][name]
        manifest = record.write_manifest(stage, population, contract, summaries)
        if record.read_source_record(stage, expected_population=population) != manifest:
            raise RuntimeError(f"{name} strict readback mismatch")
        stage.rename(work / name)
        records[name] = {
            "identity": manifest["identity"],
            "hanchan": len(summaries),
            "bytes": _bytes(work / name),
        }
    for game in games:
        game.pop("summaries")
    return {"contract": contract, "wall": wall, "games": games, "records": records}


def _tail(path):
    return path.read_bytes()[-1500:].decode(errors="replace")


def _wait_step(name, proc, started, output, logs, sink):
    _, status, usage = os.wait4(proc.pid, 0)
    proc.returncode = status
    sink.append(
        {
            "step": name,
            "wall": time.perf_counter() - started,
            "user": usage.ru_utime,
            "sys": usage.ru_stime,
            "maxrss_kb": usage.ru_maxrss,
            "exit_status": status,
            "output_bytes": None if output is None or status else _bytes(output),
            "stdout_tail": _tail(logs[0]),
            "stderr_tail": _tail(logs[1]),
        }
    )


def _start_step(learning_python, name, args, output, sink, log_dir):
    # Output goes to files: a full pipe would block the step while wait4 waits.
    logs = (
        log_dir / f"{name.replace('/', '-')}.stdout",
        log_dir / f"{name.replace('/', '-')}.stderr",
    )
    started = time.perf_counter()
    with open(logs[0], "wb") as stdout, open(logs[1], "wb") as stderr:
        proc = subprocess.Popen(
            [learning_python, "-m", "lisjong.learning", *args],
            stdout=stdout,
            stderr=stderr,
        )
    thread = threading.Thread(
        target=_wait_step, args=(name, proc, started, output, logs, sink)
    )
    thread.start()
    return thread, proc


def _check_steps(rows):
    failed = [row["step"] for row in rows if row["exit_status"]]
    if failed:
        raise RuntimeError(f"Learning steps failed: {failed}")


def replay_and_materialize(work, project, workers, learning_python):
    from lisjong_arena.policy_source_record import replay

    steps, threads = [], []
    for name in POPULATIONS:
        out = work / "learn" / name
        out.mkdir(parents=True)
        source = str(work / name)
        threads.append(
            _start_step(
                learning_python,
                f"{name}/materialize-candidate-dataset",
                [
                    "materialize-candidate-dataset",
                    "--source-record",
                    source,
                    "--output",
                    str(out / "cand-dataset"),
                ],
                out / "cand-dataset",
                steps,
                work / "logs",
            )
        )
        threads.append(
            _start_step(
                learning_python,
                f"{name}/materialize-dataset",
                [
                    "materialize-dataset",
                    "--source-record",
                    source,
                    "--output",
                    str(out / "bc-dataset"),
                ],
                out / "bc-dataset",
                steps,
                work / "logs",
            )
        )
    t0 = time.perf_counter()
    try:
        summary = replay.replay_verify(
            work / "record200", project=project, workers=workers
        )
    except BaseException:
        # Do not keep paying for materialize steps once the record is invalid.
        for _, proc in threads:
            proc.kill()
        raise
    finally:
        replay_wall = time.perf_counter() - t0
        for thread, _ in threads:
            thread.join()
    _check_steps(steps)
    if summary["mismatches"]:
        raise RuntimeError(f"replay mismatches: {summary['mismatches']}")
    return {"replay": {"wall": replay_wall, "summary": summary}, "steps": steps}


def train(work, learning_python):
    steps = []
    for name in POPULATIONS:
        out = work / "learn" / name
        plan = [
            (
                "train",
                [
                    "train",
                    "--dataset",
                    str(out / "bc-dataset"),
                    "--output",
                    str(out / "bc-artifact"),
                    "--train-split",
                    "TRAIN",
                    "--validation-split",
                    "SELECT",
                    "--epochs",
                    str(EPOCHS),
                    "--hidden-width",
                    str(BC_WIDTH),
                ],
                out / "bc-artifact",
            ),
            (
                "verify-artifact",
                ["verify-artifact", "--artifact", str(out / "bc-artifact")],
                None,
            ),
            (
                "train-candidate-scorer",
                [
                    "train-candidate-scorer",
                    "--dataset",
                    str(out / "cand-dataset"),
                    "--output",
                    str(out / "cand-artifact"),
                    "--train-split",
                    "TRAIN",
                    "--select-split",
                    "SELECT",
                    "--epochs",
                    str(EPOCHS),
                    "--hidden-width",
                    str(CANDIDATE_WIDTH),
                ],
                out / "cand-artifact",
            ),
            (
                "verify-candidate-artifact",
                ["verify-candidate-artifact", "--artifact", str(out / "cand-artifact")],
                None,
            ),
        ]
        for step, args, output in plan:
            _start_step(
                learning_python, f"{name}/{step}", args, output, steps, work / "logs"
            )[0].join()
            _check_steps(steps)
    return {"steps": steps}


def evaluate(work, learning_python, workers):
    artifact = work / "learn" / "record200" / "bc-artifact"
    t0 = time.perf_counter()
    proc = subprocess.run(
        [
            learning_python,
            __file__,
            "eval",
            "--artifact",
            str(artifact),
            "--workers",
            str(workers),
            "--seeds",
            ",".join(map(str, EVAL_SEEDS)),
        ],
        capture_output=True,
        text=True,
    )
    wall = time.perf_counter() - t0
    if proc.returncode:
        raise RuntimeError(f"evaluation failed: {proc.stderr[-2000:]}")
    games = json.loads(proc.stdout)
    failures = [game for game in games if game["failure"]]
    if failures:
        raise RuntimeError(f"evaluation hanchan failed: {failures}")
    return {"wall": wall, "games": games}


def _eval_game(artifact, seed, student_seat):
    from lisjong.learning.policy import load_learned_policy_factory
    from lisjong.policies.placement_aware_speed_call import (
        PlacementAwareSpeedCallPolicy,
    )
    from lisjong.policy_contract import Seat

    from lisjong_arena.riichienv.local_game_runner import LocalGameRunner

    totals = {"seconds": 0.0, "decisions": 0}

    class TimedStudent:
        def __init__(self, inner):
            self._inner = inner

        def choose_action(self, decision):
            started = time.perf_counter()
            try:
                return self._inner.choose_action(decision)
            finally:
                totals["seconds"] += time.perf_counter() - started
                totals["decisions"] += 1

    u0 = resource.getrusage(resource.RUSAGE_SELF)
    t0 = time.perf_counter()
    factory = load_learned_policy_factory(artifact)
    load_seconds = time.perf_counter() - t0
    policies = {
        seat: TimedStudent(factory())
        if int(seat) == student_seat
        else PlacementAwareSpeedCallPolicy()
        for seat in Seat
    }
    try:
        result = LocalGameRunner(policies, seed=seed, game_mode="4p-red-half").run()
        failure = None
    except Exception as error:  # recorded and reported as a failed run
        result, failure = None, repr(error)
    u1 = resource.getrusage(resource.RUSAGE_SELF)
    return {
        "seed": seed,
        "student_seat": student_seat,
        "wall": time.perf_counter() - t0,
        "user": u1.ru_utime - u0.ru_utime,
        "sys": u1.ru_stime - u0.ru_stime,
        "maxrss_kb": u1.ru_maxrss,
        "artifact_load_seconds": load_seconds,
        "student_decision_seconds": totals["seconds"],
        "student_decisions": totals["decisions"],
        "decisions": None if result is None else result.decisions,
        "failure": failure,
    }


def eval_main(args):
    seeds = [int(seed) for seed in args.seeds.split(",")]
    seats = [i % 4 for i in range(len(seeds))]
    with ProcessPoolExecutor(max_workers=args.workers, max_tasks_per_child=1) as pool:
        games = list(pool.map(_eval_game, [args.artifact] * len(seeds), seeds, seats))
    json.dump(games, sys.stdout)


def host_facts():
    _, total = MemorySampler.used_kb()
    model = ""
    with open("/proc/cpuinfo") as handle:
        for line in handle:
            if line.startswith("model name"):
                model = line.split(":", 1)[1].strip()
                break
    return {
        "nproc": os.cpu_count(),
        "cpu_model": model,
        "mem_total_kb": total,
        "python": platform.python_version(),
    }


def run_main(args):
    work, output = Path(args.work), Path(args.output)
    (work / "logs").mkdir(parents=True)
    output.mkdir(parents=True, exist_ok=True)
    sampler = MemorySampler()
    sampler.start()
    report = {
        "teacher": TEACHER,
        "populations": POPULATIONS,
        "eval_seeds": EVAL_SEEDS,
        "host": host_facts(),
        "workers": args.workers,
        "phases": {},
    }

    def progress(text):
        Path(args.progress).write_text(
            f"{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())} {text}\n"
        )

    def save():
        report["memory_peak_used_kb"] = dict(sampler.peaks)
        (output / "calibration-report.json").write_text(
            json.dumps(report, indent=1, sort_keys=True)
        )

    phases = [
        ("generation", lambda: generate(work, args.project, args.workers, progress)),
        (
            "replay_and_materialize",
            lambda: replay_and_materialize(
                work, args.project, args.workers, args.learning_python
            ),
        ),
        ("train", lambda: train(work, args.learning_python)),
        ("evaluation", lambda: evaluate(work, args.learning_python, args.workers)),
    ]
    try:
        for name, phase in phases:
            sampler.phase = name
            progress(name)
            started = time.perf_counter()
            report["phases"][name] = phase()
            report["phases"][name]["phase_wall"] = time.perf_counter() - started
            save()
    finally:
        sampler.stop()
        save()
    for name in POPULATIONS:
        (output / f"{name}-manifest.json").write_bytes(
            (work / name / "manifest.json").read_bytes()
        )
        for artifact in ("bc-artifact", "cand-artifact"):
            shutil.copytree(
                work / "learn" / name / artifact, output / "artifacts" / name / artifact
            )
    progress("done")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser("run")
    run.add_argument("--project", required=True)
    run.add_argument("--learning-python", required=True)
    run.add_argument("--workers", type=int, required=True)
    run.add_argument("--work", required=True)
    run.add_argument("--output", required=True)
    run.add_argument("--progress", required=True)
    evaluation = commands.add_parser("eval")
    evaluation.add_argument("--artifact", required=True)
    evaluation.add_argument("--workers", type=int, required=True)
    evaluation.add_argument("--seeds", required=True)
    args = parser.parse_args(argv)
    if args.command == "run":
        run_main(args)
    else:
        eval_main(args)


if __name__ == "__main__":
    main()
