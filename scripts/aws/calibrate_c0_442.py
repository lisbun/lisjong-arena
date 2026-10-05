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

After replay-verify, the record-200 source is packed with its replay evidence
into ``<output>/source/record200`` (lisbun/lisjong-arena#449), so it can be
reused without regeneration. ``convert`` (Arena venv) restores such an archive
after matching the source identity, archive SHA-256 and replay evidence, and
runs only the two Learning materialize steps, measured the same way.

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
import signal
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


POLL_SECONDS = 1.0
REPLAY_MISMATCH_EXIT = 3


def _tail(path):
    return path.read_bytes()[-1500:].decode(errors="replace")


def _run_steps(specs, log_dir):
    """Run ``(name, argv, output)`` steps concurrently; stop all on the first failure.

    Each step runs in its own process group and is reaped with ``os.wait4`` so
    its wall, user / system CPU and max RSS (children included) are recorded.
    When one step exits non-zero, every step still running is killed with its
    whole process group, so a failed run does not keep paying for the rest.
    """
    running, rows = {}, []
    try:
        for name, argv, output in specs:
            logs = tuple(
                log_dir / f"{name.replace('/', '-')}.{stream}"
                for stream in ("stdout", "stderr")
            )
            with open(logs[0], "wb") as stdout, open(logs[1], "wb") as stderr:
                proc = subprocess.Popen(
                    argv, stdout=stdout, stderr=stderr, start_new_session=True
                )
            running[proc.pid] = (name, proc, time.perf_counter(), output, logs)
        failed = None
        while running:
            for pid in list(running):
                reaped, status, usage = os.wait4(pid, os.WNOHANG)
                if reaped == 0:
                    continue
                name, proc, started, output, logs = running.pop(pid)
                proc.returncode = status
                rows.append(
                    {
                        "step": name,
                        "wall": time.perf_counter() - started,
                        "user": usage.ru_utime,
                        "sys": usage.ru_stime,
                        "maxrss_kb": usage.ru_maxrss,
                        "exit_status": status,
                        "output_bytes": None
                        if output is None or status
                        else _bytes(output),
                        "stdout_tail": _tail(logs[0]),
                        "stderr_tail": _tail(logs[1]),
                    }
                )
                if status and failed is None:
                    failed = name
                    _kill_all(running)
            if running:
                time.sleep(POLL_SECONDS)
    except BaseException:
        _kill_all(running)
        raise
    finally:
        for pid in list(running):
            os.waitpid(pid, 0)
    if failed is not None:
        stopped = [
            row["step"] for row in rows if row["exit_status"] and row["step"] != failed
        ]
        raise RuntimeError(f"step {failed} failed; stopped {stopped}: {rows}")
    return rows


def _kill_all(running):
    for _, proc, *_ in running.values():
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def _learning(learning_python, *args):
    return [learning_python, "-m", "lisjong.learning", *args]


def replay_and_materialize(work, project, workers, learning_python):
    specs = [
        (
            "record200/replay-verify",
            [
                sys.executable,
                __file__,
                "replay",
                "--record",
                str(work / "record200"),
                "--project",
                project,
                "--workers",
                str(workers),
                "--summary",
                str(work / "replay-summary.json"),
            ],
            None,
        )
    ]
    for name in POPULATIONS:
        out = work / "learn" / name
        out.mkdir(parents=True)
        specs += materialize_specs(work / name, out, learning_python, prefix=f"{name}/")
    steps = _run_steps(specs, work / "logs")
    summary = json.loads((work / "replay-summary.json").read_text())
    return {"replay": {"summary": summary}, "steps": steps}


def retain_source(work, output):
    """Pack the replay-verified record200 with its replay evidence (#449)."""
    from lisjong_arena.policy_source_record import archive

    summary = json.loads((work / "replay-summary.json").read_text())
    destination = output / "source" / "record200"
    destination.parent.mkdir(parents=True, exist_ok=True)
    started = time.perf_counter()
    evidence = archive.archive_source_record(
        work / "record200", destination, replay=summary
    )
    return {
        "wall": time.perf_counter() - started,
        "source_identity": evidence["source"]["identity"],
        "archive": evidence["archive"],
        "evidence_identity": evidence["identity"],
    }


def materialize_specs(source, out, learning_python, *, prefix=""):
    """The candidate and BC materialize steps of one source record."""
    return [
        (
            f"{prefix}{command}",
            _learning(
                learning_python,
                command,
                "--source-record",
                str(source),
                "--output",
                str(out / dataset),
            ),
            out / dataset,
        )
        for command, dataset in (
            ("materialize-candidate-dataset", "cand-dataset"),
            ("materialize-dataset", "bc-dataset"),
        )
    ]


def convert_main(args):
    """Restore a retained source and run only the Learning materialize steps.

    Restoration matches the expected source identity, the archive SHA-256 and
    the replay evidence first; on any mismatch no conversion starts.
    """
    from lisjong_arena.policy_source_record import archive

    work, output = Path(args.work), Path(args.output)
    (work / "logs").mkdir(parents=True)
    output.mkdir(parents=True, exist_ok=True)
    started = time.perf_counter()
    manifest = archive.restore_source_record(
        args.archive, work / "source", expected_identity=args.expected_identity
    )
    restore_wall = time.perf_counter() - started
    out = work / "learn"
    out.mkdir()
    steps = _run_steps(
        materialize_specs(work / "source", out, args.learning_python), work / "logs"
    )
    report = {
        "source_identity": manifest["identity"],
        "games": len(manifest["games"]),
        "decisions": sum(game["decision_count"] for game in manifest["games"]),
        "restore_wall": restore_wall,
        "host": host_facts(),
        "steps": steps,
    }
    (output / "convert-report.json").write_text(
        json.dumps(report, indent=1, sort_keys=True)
    )
    return report


def replay_main(args):
    """Replay-verify one record; exit non-zero on any mismatch."""
    import multiprocessing

    from lisjong_arena.policy_source_record import replay

    # Fork the replay workers from this process so that their CPU time and RSS
    # are reaped here and reach the parent's wait4 record (a forkserver would
    # own them instead).
    multiprocessing.set_start_method("fork")
    summary = replay.replay_verify(
        Path(args.record), project=args.project, workers=args.workers
    )
    children = resource.getrusage(resource.RUSAGE_CHILDREN)
    summary["worker_cpu_seconds"] = children.ru_utime + children.ru_stime
    Path(args.summary).write_text(json.dumps(summary, indent=1, sort_keys=True))
    if summary["mismatches"]:
        sys.exit(REPLAY_MISMATCH_EXIT)


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
            steps += _run_steps(
                [(f"{name}/{step}", _learning(learning_python, *args), output)],
                work / "logs",
            )
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
        ("retain_source", lambda: retain_source(work, output)),
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
    replay = commands.add_parser("replay")
    replay.add_argument("--record", required=True)
    replay.add_argument("--project", required=True)
    replay.add_argument("--workers", type=int, required=True)
    replay.add_argument("--summary", required=True)
    convert = commands.add_parser("convert")
    convert.add_argument("--archive", required=True)
    convert.add_argument("--expected-identity", required=True)
    convert.add_argument("--learning-python", required=True)
    convert.add_argument("--work", required=True)
    convert.add_argument("--output", required=True)
    evaluation = commands.add_parser("eval")
    evaluation.add_argument("--artifact", required=True)
    evaluation.add_argument("--workers", type=int, required=True)
    evaluation.add_argument("--seeds", required=True)
    args = parser.parse_args(argv)
    {
        "run": run_main,
        "replay": replay_main,
        "convert": convert_main,
        "eval": eval_main,
    }[args.command](args)


if __name__ == "__main__":
    main()
