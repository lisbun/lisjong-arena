"""One first-party O0 corpus path; no cloud-specific scientific semantics."""

import hashlib
import json
import math
import sys
from array import array
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path
from tempfile import TemporaryDirectory

from lisjong.action_vocabulary import (
    build_legal_action_mask,
    decode_action,
    encode_action,
)
from lisjong.policies import TwoStepUkeirePolicy
from lisjong.policies.two_step_ukeire import (
    TwoStepUkeireAnalysis,
    TwoStepUkeireCandidateEvaluation,
)
from lisjong.policy_contract import DecisionContext, DecisionTrace, Seat
from lisjong.policy_contract.action import DiscardAction, RiichiAction

from lisjong_arena._artifact_io import parse_json_text
from lisjong_arena.learned_policy_input import build_policy_input_feature, tensor_values
from lisjong_arena.learned_policy_stage2.recording import (
    RecordedDecision,
    encode_teacher_action,
)
from lisjong_arena.riichienv.local_game_runner import (
    LocalGameInspectionRecorder,
    LocalGameRunner,
)

from . import source_record
from .protocol import (
    GAME_MODE,
    SUPPORT,
    ZERO_FAILURES,
    ordered_games,
    support_outcome,
    validate_lock,
)
from .qualification import (
    SCHEMA,
    candidate_records,
    read_document,
    require_qualification,
    runtime_binding,
    seal,
    write_document,
)
from .semantics import CALL, WIN, OffenseError, audit_trace

MAX_PROCESS_WORKERS = 32


def _support(trace, stages):
    choice = len(trace.legal_actions) >= 2
    return {
        "choice_rows": int(choice),
        "winning_opportunities": int(
            any(isinstance(a, WIN) for a in trace.legal_actions)
        ),
        "riichi_opportunities": int(
            any(isinstance(a, RiichiAction) for a in trace.legal_actions)
        ),
        "voluntary_call_opportunities": int(
            choice and any(isinstance(a, CALL) for a in trace.legal_actions)
        ),
        "normal_discard_choice_rows": int(choice and stages is not None),
        "second_step_applicable_rows": int(
            choice and stages is not None and stages.second_step is not None
        ),
    }


def _feature_bytes(values):
    if len(values) != 8204 or any(not math.isfinite(v) for v in values):
        raise OffenseError("feature dimension/non-finite feature")
    floats = array("f", values)
    if floats.itemsize != 4 or any(not math.isfinite(v) for v in floats):
        raise OffenseError("non-finite float32 feature")
    if sys.byteorder != "little":
        floats.byteswap()
    return floats.tobytes()


def encode_observation(observation, step, ordinal):
    trace = observation.decision_trace
    context = DecisionContext(observation.policy_input, trace.legal_actions)
    stages = audit_trace(trace)
    decision = RecordedDecision(
        step, ordinal, int(observation.seat), context, trace.selected_action
    )
    teacher = encode_teacher_action(decision)
    mask = build_legal_action_mask(context)
    if len(mask) != 802 or sum(mask) != len(context.legal_actions) or not mask[teacher]:
        raise OffenseError("legal-mask failure")
    features = _feature_bytes(tensor_values(build_policy_input_feature(context.input)))
    row = {
        "step_ordinal": step,
        "decision_ordinal": ordinal,
        "actor_seat": int(observation.seat),
        "legal_indices": sorted(encode_action(a) for a in trace.legal_actions),
        "teacher_action_index": teacher,
        "candidates": candidate_records(stages),
    }
    return row, features, bytes(mask), _support(trace, stages)


def _file_info(path):
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(chunk)
    return {"bytes": path.stat().st_size, "sha256": h.hexdigest()}


def _record_game(seed):
    recorder = LocalGameInspectionRecorder()
    policies = {seat: TwoStepUkeirePolicy() for seat in Seat}
    result = LocalGameRunner(
        policies, seed=seed, game_mode=GAME_MODE, inspection_recorder=recorder
    ).run()
    return result, recorder.snapshot()


def _write_game(path, source_path, game_ordinal, split, seed, lock_identity):
    result, inspection = _record_game(seed)
    if result.seed != seed or result.game_mode != GAME_MODE:
        raise OffenseError("executed game identity mismatch")
    path.mkdir()
    ordinal, choices = 0, 0
    counts = dict.fromkeys(SUPPORT, 0)
    with (
        (path / "rows.jsonl").open("x", encoding="utf-8", newline="\n") as rows,
        (path / "features.f32").open("xb") as features,
        (path / "legal-mask.u8").open("xb") as masks,
    ):
        for expected_step, step in enumerate(inspection.step_observations):
            if step.step_ordinal != expected_step:
                raise OffenseError("noncontiguous execution steps")
            previous_seat = -1
            for observation in step.seat_decisions:
                if int(observation.seat) <= previous_seat:
                    raise OffenseError("noncanonical execution seat order")
                previous_seat = int(observation.seat)
                row, values, mask, support = encode_observation(
                    observation, step.step_ordinal, ordinal
                )
                rows.write(
                    json.dumps(
                        row, sort_keys=True, separators=(",", ":"), allow_nan=False
                    )
                    + "\n"
                )
                if len(row["legal_indices"]) >= 2:
                    features.write(values)
                    masks.write(mask)
                    choices += 1
                for key, value in support.items():
                    counts[key] += value
                ordinal += 1
    if (
        ordinal != result.decisions
        or len(inspection.step_observations) != result.steps
        or not choices
    ):
        raise OffenseError("incomplete game/decision accounting")
    scientific_summary = seal(
        {
            "seed": seed,
            "split": split,
            "lock_identity": lock_identity,
            "game_mode": GAME_MODE,
            "decision_count": ordinal,
            "steps": result.steps,
            "choice_rows": choices,
            "forced_rows": ordinal - choices,
            "support": counts,
            "files": {
                name: _file_info(path / name)
                for name in ("rows.jsonl", "features.f32", "legal-mask.u8")
            },
        }
    )
    source_summary = source_record.write_game(
        source_path,
        game_ordinal=game_ordinal,
        split=split,
        seed=seed,
        lock_identity=lock_identity,
        result=result,
        inspection=inspection,
    )
    return scientific_summary, source_summary


def _read_row(row):
    if type(row) is not dict or set(row) != {
        "step_ordinal",
        "decision_ordinal",
        "actor_seat",
        "legal_indices",
        "teacher_action_index",
        "candidates",
    }:
        raise OffenseError("invalid decision row fields")
    for field in (
        "step_ordinal",
        "decision_ordinal",
        "actor_seat",
        "teacher_action_index",
    ):
        if type(row[field]) is not int or row[field] < 0:
            raise OffenseError("invalid row ordinal/index")
    seat = Seat(row["actor_seat"])
    legal = row["legal_indices"]
    if (
        type(legal) is not list
        or not legal
        or any(type(i) is not int or not 0 <= i < 802 for i in legal)
        or legal != sorted(set(legal))
    ):
        raise OffenseError("invalid legal indices")
    if row["teacher_action_index"] not in legal:
        raise OffenseError("teacher label outside legal mask")
    actions = tuple(decode_action(i, actor=seat) for i in legal)
    selected = actions[legal.index(row["teacher_action_index"])]
    if type(row["candidates"]) is not list:
        raise OffenseError("invalid candidates")
    candidates = []
    for e in row["candidates"]:
        if type(e) is not dict or set(e) != {
            "action_index",
            "post_discard_shanten",
            "current_ukeire_count",
            "second_step_ukeire_score",
        }:
            raise OffenseError("invalid candidate fields")
        if type(e["action_index"]) is not int or e["action_index"] not in legal:
            raise OffenseError("candidate is not legal")
        action = actions[legal.index(e["action_index"])]
        if not isinstance(action, DiscardAction):
            raise OffenseError("candidate must be a discard")
        candidates.append(
            TwoStepUkeireCandidateEvaluation(
                action,
                e["post_discard_shanten"],
                e["current_ukeire_count"],
                e["second_step_ukeire_score"],
            )
        )
    trace = DecisionTrace(
        actions,
        selected,
        TwoStepUkeireAnalysis(tuple(candidates)) if candidates else None,
    )
    stages = audit_trace(trace)
    if candidate_records(stages) != row["candidates"]:
        raise OffenseError("candidate roundtrip mismatch")
    return trace, stages


def _read_game(path, game, split, seed, lock_identity):
    from .qualification import unseal

    body = unseal(game)
    for field in ("seed", "decision_count", "steps", "choice_rows", "forced_rows"):
        if type(game.get(field)) is not int or game[field] < 0:
            raise OffenseError("invalid game count/seed")
    support_outcome(game["support"])
    if set(body) != {
        "seed",
        "split",
        "lock_identity",
        "game_mode",
        "decision_count",
        "steps",
        "choice_rows",
        "forced_rows",
        "support",
        "files",
    }:
        raise OffenseError("invalid game fields")
    if (game["seed"], game["split"], game["lock_identity"], game["game_mode"]) != (
        seed,
        split,
        lock_identity,
        GAME_MODE,
    ):
        raise OffenseError("game population/provenance mismatch")
    filenames = {"rows.jsonl", "features.f32", "legal-mask.u8"}
    if set(game["files"]) != filenames or {p.name for p in path.iterdir()} != filenames:
        raise OffenseError("missing/unexpected game payload")
    for name in filenames:
        if _file_info(path / name) != game["files"][name]:
            raise OffenseError("payload checksum/size mismatch")
    counts = dict.fromkeys(SUPPORT, 0)
    total, choices, last_step, last_seat = 0, 0, -1, -1
    with (
        (path / "rows.jsonl").open(encoding="utf-8") as rows,
        (path / "features.f32").open("rb") as features,
        (path / "legal-mask.u8").open("rb") as masks,
    ):
        for line in rows:
            row = parse_json_text(line)
            trace, stages = _read_row(row)
            step, seat = row["step_ordinal"], row["actor_seat"]
            if (
                row["decision_ordinal"] != total
                or step not in (last_step, last_step + 1)
                or (step == last_step and seat <= last_seat)
            ):
                raise OffenseError("decision ordering/accounting mismatch")
            last_step, last_seat = step, seat
            if len(trace.legal_actions) >= 2:
                payload = features.read(8204 * 4)
                if len(payload) != 8204 * 4:
                    raise OffenseError("feature payload truncated")
                values = array("f")
                values.frombytes(payload)
                if sys.byteorder != "little":
                    values.byteswap()
                _feature_bytes(values)
                mask = masks.read(802)
                if mask != bytes(int(i in row["legal_indices"]) for i in range(802)):
                    raise OffenseError("legal-mask payload differs from legal actions")
                choices += 1
            for key, value in _support(trace, stages).items():
                counts[key] += value
            total += 1
        if features.read(1) or masks.read(1):
            raise OffenseError("unexpected trailing tensor rows")
    if not choices or (total, choices, total - choices, last_step + 1, counts) != (
        game["decision_count"],
        game["choice_rows"],
        game["forced_rows"],
        game["steps"],
        game["support"],
    ):
        raise OffenseError("game support/accounting mismatch")
    return counts


def read_corpus(path, *, expected_lock=None):
    """Strict streaming readback, including per-hanchan membership and support."""
    path = Path(path)
    manifest = read_document(path / "manifest.json")
    if (
        set(manifest)
        != {
            "schema",
            "kind",
            "lock",
            "p2_evidence",
            "games",
            "support",
            "failures",
            "p2_outcome",
            "identity",
        }
        or manifest["schema"] != SCHEMA
        or manifest["kind"] != "corpus"
    ):
        raise OffenseError("invalid corpus manifest")
    lock = manifest["lock"]
    validate_lock(lock, manifest["p2_evidence"])
    if expected_lock is not None and lock != expected_lock:
        raise OffenseError("corpus differs from expected protocol lock")
    games = ordered_games(lock)
    if len(manifest["games"]) != len(games):
        raise OffenseError("missing/extra hanchan")
    expected_names = {"manifest.json", *(f"game-{i:03d}" for i in range(len(games)))}
    if {p.name for p in path.iterdir()} != expected_names:
        raise OffenseError("missing/unexpected corpus files")
    counts = dict.fromkeys(SUPPORT, 0)
    for i, ((split, seed), game) in enumerate(
        zip(games, manifest["games"], strict=True)
    ):
        for key, value in _read_game(
            path / f"game-{i:03d}", game, split, seed, lock["identity"]
        ).items():
            counts[key] += value
    outcome = support_outcome(counts) if lock["request"]["phase"] == "P2" else None
    if (
        manifest["support"] != counts
        or manifest["p2_outcome"] != outcome
        or manifest["failures"] != dict.fromkeys(ZERO_FAILURES, 0)
    ):
        raise OffenseError("corpus summary inconsistent")
    return manifest


def generate(
    lock,
    destination,
    *,
    project="pyproject.toml",
    p2_path=None,
    progress=None,
    workers=1,
    source_record_destination=None,
):
    """Publish only a complete, strict-read corpus. Operational progress is separate."""
    if type(workers) is not int or not 1 <= workers <= MAX_PROCESS_WORKERS:
        raise OffenseError(
            f"workers must be an integer from 1 through {MAX_PROCESS_WORKERS}"
        )
    destination = Path(destination)
    source_destination = (
        Path(source_record_destination)
        if source_record_destination is not None
        else destination.with_name(destination.name + "-source-record")
    )
    if destination == source_destination:
        raise OffenseError("corpus and source-record destinations must differ")
    if destination.parent != source_destination.parent:
        raise OffenseError(
            "corpus and source-record destinations must share a parent for paired publish"
        )
    for target in (destination, source_destination):
        if target.exists():
            raise FileExistsError(target)
    binding = runtime_binding(project)
    require_qualification(lock["qualification"], binding)
    p2 = read_corpus(p2_path) if p2_path is not None else None
    validate_lock(lock, p2)
    destination.parent.mkdir(parents=True, exist_ok=True)
    games = ordered_games(lock)
    if workers > len(games):
        raise OffenseError("workers must not exceed the hanchan count")
    with TemporaryDirectory(
        prefix=f".{destination.name}-paired-staging-", dir=destination.parent
    ) as staging_root_name:
        staging_root = Path(staging_root_name)
        staging = staging_root / "corpus"
        source_staging = staging_root / "source-record"
        staging.mkdir()
        source_staging.mkdir()
        summaries = [None] * len(games)
        source_summaries = [None] * len(games)
        if workers == 1:
            for i, (split, seed) in enumerate(games):
                summaries[i], source_summaries[i] = _write_game(
                    staging / f"game-{i:03d}",
                    source_staging / f"game-{i:03d}",
                    i,
                    split,
                    seed,
                    lock["identity"],
                )
                if progress is not None:
                    progress(i + 1, len(games))
        else:
            executor = ProcessPoolExecutor(max_workers=workers)
            futures = {
                executor.submit(
                    _write_game,
                    staging / f"game-{i:03d}",
                    source_staging / f"game-{i:03d}",
                    i,
                    split,
                    seed,
                    lock["identity"],
                ): i
                for i, (split, seed) in enumerate(games)
            }
            completed = 0
            try:
                for future in as_completed(futures):
                    i = futures[future]
                    summaries[i], source_summaries[i] = future.result()
                    completed += 1
                    if progress is not None:
                        progress(completed, len(games))
            except BaseException:
                for future in futures:
                    future.cancel()
                raise
            finally:
                executor.shutdown(wait=True, cancel_futures=True)
        if any(summary is None for summary in summaries) or any(
            summary is None for summary in source_summaries
        ):
            raise OffenseError("incomplete parallel hanchan collection")
        counts = {key: sum(g["support"][key] for g in summaries) for key in SUPPORT}
        manifest = seal(
            {
                "schema": SCHEMA,
                "kind": "corpus",
                "lock": lock,
                "p2_evidence": p2,
                "games": summaries,
                "support": counts,
                "failures": dict.fromkeys(ZERO_FAILURES, 0),
                "p2_outcome": support_outcome(counts)
                if lock["request"]["phase"] == "P2"
                else None,
            }
        )
        write_document(staging / "manifest.json", manifest)
        source_manifest = source_record.write_manifest(
            source_staging, lock, manifest["identity"], source_summaries
        )
        read_corpus(staging, expected_lock=lock)
        if (
            source_record.read_source_record(
                source_staging, expected_lock=lock, corpus_path=staging
            )
            != source_manifest
        ):
            raise OffenseError("source-record strict readback mismatch")

        # Publish the reusable source first. A process interruption can leave an
        # orphan source record, but never a visible scientific corpus without it.
        source_staging.rename(source_destination)
        try:
            staging.rename(destination)
        except BaseException:
            try:
                source_destination.rename(source_staging)
            except OSError:
                pass
            raise
    return manifest
