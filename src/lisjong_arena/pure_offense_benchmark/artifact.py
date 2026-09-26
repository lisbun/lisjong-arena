"""Issue #389 — 1 focal armのwrite-once artifact directory。

```text
<arm-dir>/
  strength.json   既存ABBB single-round artifact schema v1（無変更で保存）
  offense.json    benchmark-owned offense record v1
                  （strength.jsonのSHA-256、benchmark manifest、Seed Registry
                   binding、focal identity、per-kyoku recordを保持）
```

``strength.json``は既存readerでそのまま読める。``offense.json``は
``strength.json``のdigestへbindされ、読み戻し時には両者の整合（件数・順序・
SeatRoundStatsとの共通fact）を再検証する。directoryはstagingで両fileを
完成させてから公開し、既存destinationは上書きしない。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from lisjong.policy_contract import Seat

from lisjong_arena import seed_registry
from lisjong_arena._artifact_io import (
    ArtifactValidationError,
    canonical_json_text,
    expect_bool,
    expect_int,
    expect_list,
    expect_object,
    expect_str,
    read_json_document,
    sha256_bytes,
    staged_artifact_directory,
    write_new_artifact_file,
)
from lisjong_arena.single_round_artifact import (
    SingleRoundStrengthArtifact,
    load_single_round_artifact,
    save_single_round_artifact,
)

from .execution import BenchmarkArmResult
from .protocol import (
    BENCHMARK_IDENTITY,
    MAX_STEPS,
    OPPONENT_IDENTITY,
    OWNER_ISSUE,
    SEED_DOMAIN,
    protocol_manifest,
)
from .record import (
    OFFENSE_RECORD_VERSION,
    KyokuOffenseFacts,
    KyokuOffenseRecord,
    SeatOffenseFacts,
    validate_record_against_game_result,
)

STRENGTH_FILE = "strength.json"
OFFENSE_FILE = "offense.json"

_DOCUMENT_FIELDS = {
    "benchmark",
    "focal",
    "ordered_seeds",
    "record_version",
    "records",
    "seed_allocation",
    "strength_artifact",
}
_FOCAL_FIELDS = {"identity", "reference"}
_STRENGTH_FIELDS = {"file", "sha256"}
_ALLOCATION_FIELDS = {"binding", "population", "split"}
_RECORD_FIELDS = {
    "dealer_seat",
    "draw_reason",
    "focal_seat",
    "rotation",
    "seats",
    "seed",
    "termination",
}
_SEAT_FIELDS = {
    "dealt_in",
    "discard_count",
    "riichi_accepted",
    "riichi_turn",
    "win_tsumo",
    "win_turn",
    "won",
}


class PureOffenseArtifactError(ArtifactValidationError):
    """benchmark arm artifactを生成・検証できない場合。"""


@dataclass(frozen=True, slots=True)
class SeedAllocation:
    """Seed Registryのactive allocationから解決した実行seed population。"""

    seeds: tuple[int, ...]
    binding: dict[str, object]
    population: str
    split: str | None

    def to_document(self) -> dict[str, object]:
        return {
            "binding": dict(self.binding),
            "population": self.population,
            "split": self.split,
        }


def resolve_seed_allocation(
    ledger_document: object, allocation_identity: str
) -> SeedAllocation:
    """live ledger snapshotから#389所有のsingle-round allocationを解決する。

    ownerは#389、seed domainは``riichienv-4p-red-single-v1``、stateは
    RESERVED / COMMITTEDでなければならない。
    """
    try:
        record = seed_registry.find_allocation(ledger_document, allocation_identity)
        seeds = seed_registry.seeds_from_membership(record["seed_membership"])
        binding = seed_registry.allocation_binding(ledger_document, allocation_identity)
        seed_registry.require_allocation_binding(
            ledger_document,
            binding,
            seeds=seeds,
            owner_issue=OWNER_ISSUE,
            seed_domain=SEED_DOMAIN,
        )
    except seed_registry.SeedRegistryError as exc:
        raise PureOffenseArtifactError(f"seed allocation is not usable: {exc}") from exc
    return SeedAllocation(
        seeds=seeds,
        binding=binding,
        population=record["population"],
        split=record["split"],
    )


def _seat_to_document(facts: SeatOffenseFacts) -> dict[str, object]:
    return {
        "dealt_in": facts.dealt_in,
        "discard_count": facts.discard_count,
        "riichi_accepted": facts.riichi_accepted,
        "riichi_turn": facts.riichi_turn,
        "win_tsumo": facts.win_tsumo,
        "win_turn": facts.win_turn,
        "won": facts.won,
    }


def _record_to_document(record: KyokuOffenseRecord) -> dict[str, object]:
    return {
        "dealer_seat": int(record.facts.dealer_seat),
        "draw_reason": record.facts.draw_reason,
        "focal_seat": int(record.focal_seat),
        "rotation": record.rotation,
        "seats": [_seat_to_document(item) for item in record.facts.seats],
        "seed": record.seed,
        "termination": record.facts.termination,
    }


def _offense_document(
    arm: BenchmarkArmResult,
    *,
    focal_reference: str,
    allocation: SeedAllocation,
    strength_sha256: str,
) -> dict[str, object]:
    plan = arm.evaluation.plan
    return {
        "benchmark": protocol_manifest(),
        "focal": {"identity": plan.candidate.identity, "reference": focal_reference},
        "ordered_seeds": list(plan.seeds),
        "record_version": OFFENSE_RECORD_VERSION,
        "records": [_record_to_document(item) for item in arm.offense_records],
        "seed_allocation": allocation.to_document(),
        "strength_artifact": {"file": STRENGTH_FILE, "sha256": strength_sha256},
    }


def save_benchmark_arm(
    arm: BenchmarkArmResult,
    destination: str | Path,
    *,
    focal_reference: str,
    allocation: SeedAllocation,
) -> None:
    """成功したarmを新しいdirectoryへ保存する。既存pathは上書きしない。"""
    if not isinstance(arm, BenchmarkArmResult):
        raise TypeError("arm must be a BenchmarkArmResult")
    if type(focal_reference) is not str or not focal_reference:
        raise ValueError("focal_reference must be a non-empty str")
    if arm.evaluation.plan.seeds != allocation.seeds:
        raise PureOffenseArtifactError("arm seeds differ from the seed allocation")
    destination = Path(destination)
    if destination.exists():
        raise FileExistsError(
            f"benchmark arm destination already exists: {destination}"
        )
    if not destination.parent.is_dir():
        raise PureOffenseArtifactError("benchmark arm parent directory does not exist")
    with staged_artifact_directory(destination) as staging:
        save_single_round_artifact(arm.evaluation, staging / STRENGTH_FILE)
        digest = sha256_bytes((staging / STRENGTH_FILE).read_bytes())
        document = _offense_document(
            arm,
            focal_reference=focal_reference,
            allocation=allocation,
            strength_sha256=digest,
        )
        write_new_artifact_file(staging / OFFENSE_FILE, canonical_json_text(document))
        # 保存したものを同じstrict readerで読み戻してから公開する。
        _load_from_directory(staging)


@dataclass(frozen=True, slots=True)
class LoadedBenchmarkArm:
    """strict readbackした1 arm。"""

    focal_identity: str
    focal_reference: str
    strength: SingleRoundStrengthArtifact
    strength_sha256: str
    seed_allocation: dict[str, object]
    records: tuple[KyokuOffenseRecord, ...]

    @property
    def seeds(self) -> tuple[int, ...]:
        return self.strength.plan.seeds


def _parse_seat(value: object, context: str) -> SeatOffenseFacts:
    raw = expect_object(value, _SEAT_FIELDS, context)
    win_turn = raw["win_turn"]
    riichi_turn = raw["riichi_turn"]
    win_tsumo = raw["win_tsumo"]
    return SeatOffenseFacts(
        discard_count=expect_int(raw["discard_count"], f"{context}.discard_count"),
        won=expect_bool(raw["won"], f"{context}.won"),
        win_turn=None if win_turn is None else expect_int(win_turn, context),
        win_tsumo=None if win_tsumo is None else expect_bool(win_tsumo, context),
        dealt_in=expect_bool(raw["dealt_in"], f"{context}.dealt_in"),
        riichi_turn=None if riichi_turn is None else expect_int(riichi_turn, context),
        riichi_accepted=expect_bool(
            raw["riichi_accepted"], f"{context}.riichi_accepted"
        ),
    )


def _parse_record(value: object, index: int) -> KyokuOffenseRecord:
    context = f"records[{index}]"
    raw = expect_object(value, _RECORD_FIELDS, context)
    seats = expect_list(raw["seats"], f"{context}.seats")
    draw_reason = raw["draw_reason"]
    try:
        return KyokuOffenseRecord(
            seed=expect_int(raw["seed"], f"{context}.seed"),
            rotation=expect_int(raw["rotation"], f"{context}.rotation"),
            focal_seat=Seat(expect_int(raw["focal_seat"], f"{context}.focal_seat")),
            facts=KyokuOffenseFacts(
                dealer_seat=Seat(
                    expect_int(raw["dealer_seat"], f"{context}.dealer_seat")
                ),
                termination=expect_str(raw["termination"], f"{context}.termination"),
                draw_reason=(
                    None
                    if draw_reason is None
                    else expect_str(draw_reason, f"{context}.draw_reason")
                ),
                seats=tuple(
                    _parse_seat(item, f"{context}.seats[{seat}]")
                    for seat, item in enumerate(seats)
                ),
            ),
        )
    except ArtifactValidationError:
        raise
    except (TypeError, ValueError) as exc:
        raise PureOffenseArtifactError(f"{context} is invalid: {exc}") from exc


def _load_from_directory(directory: Path) -> LoadedBenchmarkArm:
    strength_path = directory / STRENGTH_FILE
    offense_path = directory / OFFENSE_FILE
    if not strength_path.is_file() or not offense_path.is_file():
        raise PureOffenseArtifactError(
            f"benchmark arm must contain {STRENGTH_FILE} and {OFFENSE_FILE}"
        )
    strength = load_single_round_artifact(strength_path)
    strength_sha256 = sha256_bytes(strength_path.read_bytes())
    try:
        document = read_json_document(offense_path)
    except json.JSONDecodeError as exc:
        raise PureOffenseArtifactError(f"{OFFENSE_FILE} is not valid JSON") from exc
    raw = expect_object(document, _DOCUMENT_FIELDS, OFFENSE_FILE)

    if expect_int(raw["record_version"], "record_version") != OFFENSE_RECORD_VERSION:
        raise PureOffenseArtifactError("unsupported offense record version")
    if raw["benchmark"] != protocol_manifest():
        raise PureOffenseArtifactError(
            f"benchmark manifest differs from {BENCHMARK_IDENTITY!r} v1"
        )
    bound = expect_object(raw["strength_artifact"], _STRENGTH_FIELDS, "strength")
    if bound["file"] != STRENGTH_FILE or bound["sha256"] != strength_sha256:
        raise PureOffenseArtifactError(
            f"{OFFENSE_FILE} is not bound to this {STRENGTH_FILE}"
        )
    focal = expect_object(raw["focal"], _FOCAL_FIELDS, "focal")
    focal_identity = expect_str(focal["identity"], "focal.identity")
    focal_reference = expect_str(focal["reference"], "focal.reference")
    plan = strength.plan
    if focal_identity != plan.candidate_identity:
        raise PureOffenseArtifactError("focal identity differs from strength artifact")
    if plan.baseline_identity != OPPONENT_IDENTITY:
        raise PureOffenseArtifactError("strength artifact opponent is not passive v1")
    if plan.max_steps != MAX_STEPS:
        raise PureOffenseArtifactError(
            "strength artifact max_steps is not the v1 value"
        )
    ordered_seeds = expect_list(raw["ordered_seeds"], "ordered_seeds")
    if tuple(ordered_seeds) != plan.seeds:
        raise PureOffenseArtifactError("ordered_seeds differ from strength artifact")

    allocation = expect_object(raw["seed_allocation"], _ALLOCATION_FIELDS, "allocation")
    try:
        binding = seed_registry.validate_binding_shape(
            allocation["binding"], seeds=plan.seeds
        )
    except seed_registry.SeedRegistryError as exc:
        raise PureOffenseArtifactError(f"seed allocation binding: {exc}") from exc
    if binding["seed_domain"] != SEED_DOMAIN:
        raise PureOffenseArtifactError("seed allocation is not single-round domain")

    records = tuple(
        _parse_record(item, index)
        for index, item in enumerate(expect_list(raw["records"], "records"))
    )
    if len(records) != len(strength.game_results):
        raise PureOffenseArtifactError("records must cover every game exactly once")
    for record, game_result in zip(records, strength.game_results, strict=True):
        try:
            validate_record_against_game_result(record, game_result)
        except ValueError as exc:
            raise PureOffenseArtifactError(str(exc)) from exc

    return LoadedBenchmarkArm(
        focal_identity=focal_identity,
        focal_reference=focal_reference,
        strength=strength,
        strength_sha256=strength_sha256,
        seed_allocation=allocation,
        records=records,
    )


def load_benchmark_arm(directory: str | Path) -> LoadedBenchmarkArm:
    """保存済みarm directoryをstrict readbackする。"""
    return _load_from_directory(Path(directory))


__all__ = [
    "LoadedBenchmarkArm",
    "OFFENSE_FILE",
    "PureOffenseArtifactError",
    "STRENGTH_FILE",
    "SeedAllocation",
    "load_benchmark_arm",
    "resolve_seed_allocation",
    "save_benchmark_arm",
]
