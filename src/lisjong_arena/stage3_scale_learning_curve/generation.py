"""1つのimmutableなPhase 4 corpusとPhase 5 dataset、およびそのcost scope。

80 hanchanは一度だけ生成する。S16 / S32 / S64はこの同じcorpus / datasetの
nested TRAIN subsetであり、scaleごとに生成し直さない。

```text
Phase 4 raw corpus (80 hanchan)
    -> Phase 5 dataset (64 TRAIN / 16 VALIDATION / no TEST)
    -> strict readback
    -> evidence re-derivation
```

`load_population()`はmanifestの内部整合だけを見ない。persisted raw corpusと
persisted datasetを実際に読み直し、datasetをraw corpusから再導出し、coverage /
retention / inventory / anchor identityまでrecorded evidenceと突き合わせる。
JSON内部でfield同士が一致しているだけのartifactは通らない。

generated data、dataset、weightsはGit repositoryへcommitしない。
"""

import json
import time
from pathlib import Path
from tempfile import TemporaryDirectory

from lisjong_arena.phase4_raw_corpus.codec import canonical_json_bytes
from lisjong_arena.phase4_raw_corpus.generation import (
    generate_phase4_raw_corpus_for_seeds,
)
from lisjong_arena.phase4_raw_corpus.persistence import load_raw_corpus
from lisjong_arena.phase5_belief_dataset.builder import (
    build_phase5_belief_dataset,
    resolve_training_samples,
)
from lisjong_arena.phase5_belief_dataset.persistence import load_belief_dataset
from lisjong_arena.phase5_belief_dataset.pipeline import (
    pipeline_report_value,
    run_phase5_pipeline,
)
from lisjong_arena.phase8_sequential.data import materialize_development_examples
from lisjong_arena.phase8_sequential.protocol import (
    build_inventory,
    build_sequences,
    inventory_value,
)
from lisjong_arena.stage3_entry_gate.coverage import measure_population_coverage
from lisjong_arena.stage3_kan_coverage.generation import (
    dataset_retention_value,
    kan_event_inventory,
)
from lisjong_arena.stage3_mix_pilot.generation import _provenance_value

from .experiment import validate_dataset
from .lock import require_current_lock, validate_lock
from .population import (
    plan_value,
    population_identity,
    seat_policy_factories_by_seed,
)
from .protocol import (
    ORDERED_SEEDS,
    SCHEMA,
    SPLIT_POLICY,
    ScaleError,
    digest,
    exact,
    finite,
    identity,
)

POPULATION_FILENAME = "population.json"
COST_FIELDS = (
    "phase4_cpu_seconds",
    "phase4_wall_seconds",
    "phase5_cpu_seconds",
    "phase5_wall_seconds",
    "raw_compressed_bytes",
    "raw_uncompressed_bytes",
    "dataset_bytes",
    "anchor_count",
)
PHASE5_FIELDS = (
    "dataset_identity",
    "raw_corpus_identity",
    "dataset_artifact_bytes",
    "games",
    "turn_samples",
    "samples_per_game",
    "samples_per_partition",
    "source_revisions",
    "runtime_seconds",
    "baseline",
)


def evidence_value(raw, dataset) -> dict[str, object]:
    """persisted raw corpusとdatasetから、population evidenceを再導出する。

    対局は実行しない。strict-loadされたraw corpusからdatasetを組み直し、
    dataset identityが一致することまで確認したうえでcoverage / retention /
    inventory / anchor identityを数える。
    """
    validate_dataset(dataset)
    exact([game.seed for game in raw.corpus.games], list(ORDERED_SEEDS), "raw seeds")
    exact(dataset.raw_corpus_identity, raw.corpus_identity, "raw binding")
    exact(
        _provenance_value(dataset.provenance),
        _provenance_value(raw.corpus.provenance),
        "raw / dataset provenance",
    )
    rebuilt = build_phase5_belief_dataset(raw, SPLIT_POLICY)
    exact(rebuilt.dataset_identity, dataset.dataset_identity, "dataset derivation")
    samples = resolve_training_samples(dataset, raw)
    examples = materialize_development_examples(dataset.examples, samples)
    coverage = measure_population_coverage(raw.corpus, examples).coverage_value()
    retention = dataset_retention_value(dataset, kan_event_inventory(raw.corpus))
    inventory = inventory_value(
        build_inventory(
            build_sequences(examples),
            raw_corpus_identity=raw.corpus_identity,
            dataset_identity=dataset.dataset_identity,
        )
    )
    return {
        "coverage": coverage,
        "retention": retention,
        "inventory": inventory,
        "anchor_identities": [reference.identity for reference in dataset.examples],
        "anchors_by_seed": {
            str(seed): [
                reference.identity
                for reference in dataset.examples
                if reference.game.game_seed == seed
            ]
            for seed in ORDERED_SEEDS
        },
    }


def validate_manifest(value: object, lock: dict[str, object]) -> dict[str, object]:
    """population manifestをlocked plan / execution lock / evidenceへ固定する。"""
    validate_lock(lock)
    expected_keys = {
        "schema",
        "execution_lock_identity",
        "population_plan",
        "population_identity",
        "raw_corpus_identity",
        "dataset_identity",
        "provenance",
        "phase2_equality_verified",
        "failure_count",
        "evidence",
        "cost",
        "phase5",
    }
    if type(value) is not dict or set(value) != expected_keys:
        raise ScaleError("population manifest fields are not exact")
    exact(value["schema"], SCHEMA + "/population", "population schema")
    exact(value["execution_lock_identity"], identity(lock), "execution lock binding")
    exact(value["population_plan"], plan_value(), "population plan")
    exact(value["population_identity"], population_identity(), "population identity")
    exact(value["provenance"], lock["provenance"], "population provenance")
    for name in ("raw_corpus_identity", "dataset_identity"):
        digest(value[name], name)
    exact(value["phase2_equality_verified"], True, "Phase 2 equality")
    exact(value["failure_count"], 0, "generation failures")

    evidence = value["evidence"]
    if type(evidence) is not dict or set(evidence) != {
        "coverage",
        "retention",
        "inventory",
        "anchor_identities",
        "anchors_by_seed",
    }:
        raise ScaleError("population evidence fields are not exact")
    anchors = evidence["anchors_by_seed"]
    exact(
        sorted(anchors), [str(seed) for seed in ORDERED_SEEDS], "anchor game coverage"
    )
    flattened: list[str] = []
    for seed in ORDERED_SEEDS:
        rows = anchors[str(seed)]
        if type(rows) is not list or not rows:
            raise ScaleError("every hanchan must retain anchors")
        for row in rows:
            digest(row, "anchor identity")
        flattened.extend(rows)
    exact(evidence["anchor_identities"], flattened, "canonical anchor ordering")
    if len(set(flattened)) != len(flattened):
        raise ScaleError("duplicate anchor identities")
    retention = evidence["retention"]
    exact(retention["dataset_game_seeds"], list(ORDERED_SEEDS), "retained games")
    exact(retention["dataset_games"], len(ORDERED_SEEDS), "retained count")
    exact(retention["kan_containing_games_dropped"], 0, "kan retention")
    exact(
        evidence["coverage"]["events"]["hanchan"],
        len(ORDERED_SEEDS),
        "coverage hanchan",
    )
    exact(
        evidence["inventory"]["dataset_identity"],
        value["dataset_identity"],
        "inventory dataset",
    )
    exact(
        evidence["inventory"]["raw_corpus_identity"],
        value["raw_corpus_identity"],
        "inventory raw corpus",
    )

    cost = value["cost"]
    if type(cost) is not dict or set(cost) != set(COST_FIELDS) | {
        "peak_process_ram_bytes"
    }:
        raise ScaleError("generation cost fields are not exact")
    for name in COST_FIELDS:
        finite(cost[name], name)
    peak = cost["peak_process_ram_bytes"]
    if peak is not None and (type(peak) is not int or peak <= 0):
        raise ScaleError("peak process RAM must be a positive int or null")
    exact(cost["anchor_count"], len(flattened), "anchor cost count")

    phase5 = value["phase5"]
    if type(phase5) is not dict or set(phase5) != set(PHASE5_FIELDS):
        raise ScaleError("Phase 5 report fields are not exact")
    exact(phase5["raw_corpus_identity"], value["raw_corpus_identity"], "Phase 5 raw")
    exact(phase5["dataset_identity"], value["dataset_identity"], "Phase 5 dataset")
    exact(
        phase5["source_revisions"],
        lock["provenance"]["source_revisions"],
        "Phase 5 source",
    )
    exact(phase5["games"], len(ORDERED_SEEDS), "Phase 5 hanchan")
    exact(phase5["turn_samples"], len(flattened), "Phase 5 anchors")
    exact(phase5["dataset_artifact_bytes"], cost["dataset_bytes"], "Phase 5 bytes")
    for name, seconds in phase5["runtime_seconds"].items():
        finite(seconds, f"Phase 5 {name} seconds")
    return value


def generate_population(destination: str | Path, lock: dict[str, object]):
    """locked 80-hanchan corpusとdatasetを一度だけpublishする。

    stagingで組み立て、manifestを検証し、strict readbackが通ってからdestination
    へrenameする。既存destinationは上書きしない。
    """
    require_current_lock(lock)
    destination = Path(destination)
    if destination.exists():
        raise FileExistsError(f"destination already exists: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(
        prefix=f".{destination.name}-", dir=destination.parent
    ) as stage:
        root = Path(stage) / "population"
        root.mkdir()
        value = build_population(root, lock)
        (root / POPULATION_FILENAME).write_bytes(canonical_json_bytes(value))
        load_population(root, lock)
        root.rename(destination)
    return value


def build_population(root: Path, lock: dict[str, object]) -> dict[str, object]:
    """Phase 4 / Phase 5をscoped costつきで実行し、population manifestを作る。"""
    from lisjong_arena.phase6_snapshot.training import _peak_process_ram_bytes

    cpu, wall = time.process_time(), time.perf_counter()
    report = generate_phase4_raw_corpus_for_seeds(
        root / "raw",
        ORDERED_SEEDS,
        seat_policy_factories_by_seed=seat_policy_factories_by_seed(),
    )
    phase4_cpu, phase4_wall = time.process_time() - cpu, time.perf_counter() - wall
    cpu, wall = time.process_time(), time.perf_counter()
    pipeline = run_phase5_pipeline(report.persisted, root / "dataset", SPLIT_POLICY)
    phase5_cpu, phase5_wall = time.process_time() - cpu, time.perf_counter() - wall
    dataset = pipeline.persisted_dataset.dataset
    value = {
        "schema": SCHEMA + "/population",
        "execution_lock_identity": identity(lock),
        "population_plan": plan_value(),
        "population_identity": population_identity(),
        "raw_corpus_identity": report.persisted.corpus_identity,
        "dataset_identity": dataset.dataset_identity,
        "provenance": _provenance_value(dataset.provenance),
        "phase2_equality_verified": report.phase2_equality_verified,
        "failure_count": report.failure_count,
        "evidence": evidence_value(report.persisted, dataset),
        "cost": {
            "phase4_cpu_seconds": phase4_cpu,
            "phase4_wall_seconds": phase4_wall,
            "phase5_cpu_seconds": phase5_cpu,
            "phase5_wall_seconds": phase5_wall,
            "raw_compressed_bytes": report.measurements.compressed_bytes,
            "raw_uncompressed_bytes": report.measurements.uncompressed_bytes,
            "dataset_bytes": pipeline.persisted_dataset.byte_count,
            "anchor_count": dataset.sample_count,
            "peak_process_ram_bytes": _peak_process_ram_bytes(),
        },
        "phase5": pipeline_report_value(pipeline),
    }
    return validate_manifest(value, lock)


def load_population(destination: str | Path, lock: dict[str, object]):
    """persisted populationをstrict readbackし、evidenceを再導出して照合する。"""
    root = Path(destination)
    data = (root / POPULATION_FILENAME).read_bytes()
    value = validate_manifest(json.loads(data), lock)
    if data != canonical_json_bytes(value):
        raise ScaleError("population bytes are not canonical")
    raw = load_raw_corpus(root / "raw")
    dataset = load_belief_dataset(root / "dataset").dataset
    exact(raw.corpus_identity, value["raw_corpus_identity"], "persisted raw")
    exact(dataset.dataset_identity, value["dataset_identity"], "persisted dataset")
    exact(
        _provenance_value(dataset.provenance),
        lock["provenance"],
        "persisted provenance",
    )
    exact(evidence_value(raw, dataset), value["evidence"], "persisted evidence")
    return value, raw, dataset


__all__ = [
    "COST_FIELDS",
    "PHASE5_FIELDS",
    "POPULATION_FILENAME",
    "build_population",
    "evidence_value",
    "generate_population",
    "load_population",
    "validate_manifest",
]
