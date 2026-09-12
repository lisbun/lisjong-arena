"""Measurement-only, one-epoch S2 CPU throughput profile for Arena #166."""

import copy
import hashlib
import importlib.metadata
import math
import platform
import statistics
import subprocess
import sysconfig
import time
from contextlib import contextmanager
from pathlib import Path

from lisjong_arena._artifact_io import (
    ArtifactValidationError,
    canonical_json_text,
    expect_bool,
    expect_float,
    expect_int,
    expect_list,
    expect_object,
    expect_str,
    read_json_document,
    write_new_artifact_file,
)
from lisjong_arena.phase8_sequential.evaluation import remap_predictions_by_reference
from lisjong_arena.phase8_sequential.model import create_model
from lisjong_arena.phase8_sequential.protocol import Candidate, checkpoint_improves
from lisjong_arena.phase8_sequential.rollout import self_rollout
from lisjong_arena.phase8_sequential.training import (
    FORMAL_TRAINING_CONFIG,
    _chunk_ranges,
    _metrics,
    _train_pooled_epoch,
)
from lisjong_arena.stage3_optimization_saturation.retained import retained_value

SCHEMA = "handbelief-throughput-profile-v1"
COMPONENTS = (
    "train_tensor",
    "train_forward_constraint",
    "train_backward",
    "train_optimizer",
    "train_other",
    "validation_tensor",
    "validation_forward_constraint",
    "validation_remap",
    "validation_metrics",
    "validation_other",
    "epoch_other",
)
METHOD = "independent-seed0-initialization; one-unprofiled-warmup; three-reference-profiled-pairs"


class ThroughputError(ArtifactValidationError):
    """A measurement or artifact violates the #166 contract."""


class EpochTimer:
    """Accumulate only disjoint leaf intervals; stage and epoch totals are separate."""

    def __init__(self):
        self.values = {name: [0.0, 0.0, 0] for name in COMPONENTS}

    def finish(self, name, started):
        if name not in self.values or name.endswith("_other"):
            raise ThroughputError("unknown measured component")
        wall = time.perf_counter() - started[0]
        cpu = time.process_time() - started[1]
        row = self.values[name]
        row[0] += wall
        row[1] += cpu
        row[2] += 1

    @contextmanager
    def measure(self, name):
        started = (time.perf_counter(), time.process_time())
        try:
            yield
        finally:
            self.finish(name, started)

    def complete(self, train, validation, epoch):
        for prefix, total in (("train_", train), ("validation_", validation)):
            children = [
                v
                for k, v in self.values.items()
                if k.startswith(prefix) and not k.endswith("_other")
            ]
            other = self.values[prefix + "other"]
            other[0] = total[0] - sum(v[0] for v in children)
            other[1] = total[1] - sum(v[1] for v in children)
            other[2] = 1
        other = self.values["epoch_other"]
        other[0] = epoch[0] - train[0] - validation[0]
        other[1] = epoch[1] - train[1] - validation[1]
        other[2] = 1
        if any(value < -1e-9 for row in self.values.values() for value in row[:2]):
            raise ThroughputError("exclusive component exceeds its enclosing interval")
        return {
            name: {
                "wall_seconds": max(0.0, values[0]),
                "cpu_seconds": max(0.0, values[1]),
                "calls": values[2],
                "share_of_epoch_wall_time": max(0.0, values[0]) / epoch[0],
            }
            for name, values in self.values.items()
        }


def _elapsed(started):
    return (time.perf_counter() - started[0], time.process_time() - started[1])


def _one_epoch(data, *, profiled):
    """Recreate epoch 1 from the locked Phase 8 S2 seed/config, without continuation."""
    import torch

    config = FORMAL_TRAINING_CONFIG
    torch.manual_seed(config.seed)
    torch.use_deterministic_algorithms(config.deterministic_algorithms)
    torch.set_num_threads(config.torch_threads)
    model = create_model(Candidate.S2)
    optimizer = torch.optim.Adam(
        model.parameters(), lr=config.learning_rate, weight_decay=config.weight_decay
    )
    generator = torch.Generator().manual_seed(config.dataloader_seed)
    timer = EpochTimer() if profiled else None
    epoch_started = (time.perf_counter(), time.process_time())
    model.train()
    order = torch.randperm(len(data.train_sequences), generator=generator).tolist()
    train_started = (time.perf_counter(), time.process_time())
    error, cells, train_residual = _train_pooled_epoch(
        model,
        Candidate.S2,
        data.train_sequences,
        data.inventory.bptt_policy,
        optimizer,
        order,
        timer=timer,
    )
    train = _elapsed(train_started)
    validation_started = (time.perf_counter(), time.process_time())
    rollout = self_rollout(model, Candidate.S2, data.validation_sequences, timer=timer)
    references = tuple(value.example for value in data.canonical_validation.examples)
    if timer is None:
        predictions = remap_predictions_by_reference(references, rollout.predictions)
        metrics = _metrics(
            data.dataset_identity, data.canonical_validation.examples, predictions
        )
    else:
        with timer.measure("validation_remap"):
            predictions = remap_predictions_by_reference(
                references, rollout.predictions
            )
        with timer.measure("validation_metrics"):
            metrics = _metrics(
                data.dataset_identity, data.canonical_validation.examples, predictions
            )
    validation = _elapsed(validation_started)
    mae = metrics.per_tile_mae
    # The epoch-1 checkpoint branch is part of the current training orchestration.
    if checkpoint_improves(mae, float("inf")):
        copy.deepcopy(model.state_dict())
    epoch = _elapsed(epoch_started)
    if epoch[0] <= 0 or cells <= 0:
        raise ThroughputError("empty epoch")
    result = {
        "epoch_wall_seconds": epoch[0],
        "epoch_cpu_seconds": epoch[1],
        "training_wall_seconds": train[0],
        "training_cpu_seconds": train[1],
        "validation_wall_seconds": validation[0],
        "validation_cpu_seconds": validation[1],
    }
    if timer is not None:
        result["components"] = timer.complete(train, validation, epoch)
    return (
        result,
        error / cells,
        mae,
        {name: value.detach().clone() for name, value in model.state_dict().items()},
        (train_residual, rollout.maximum_residual, predictions),
    )


def _workload(data):
    def counts(sequences):
        return (
            len({sequence.key.game.game_seed for sequence in sequences}),
            len(sequences),
            sum(len(sequence.steps) for sequence in sequences),
        )

    train = counts(data.train_sequences)
    validation = counts(data.validation_sequences)
    workload = dict(
        zip(
            (
                "train_hanchan",
                "train_sequences",
                "train_steps",
                "validation_hanchan",
                "validation_sequences",
                "validation_steps",
            ),
            train + validation,
            strict=True,
        )
    )
    workload["train_backward_calls"] = sum(
        len(_chunk_ranges(len(sequence.steps), data.inventory.bptt_policy))
        for sequence in data.train_sequences
    )
    workload["forward_calls"] = workload["train_steps"] + workload["validation_steps"]
    workload["constraint_calls"] = workload["forward_calls"]
    return workload


def profile(data):
    """Run independent equivalent warm-up and three paired reference/profiled epochs."""
    import torch

    workload = _workload(data)
    _one_epoch(data, profiled=False)
    samples = []
    for _ in range(3):
        reference, reference_loss, reference_mae, reference_state, reference_output = (
            _one_epoch(data, profiled=False)
        )
        measured, measured_loss, measured_mae, measured_state, measured_output = (
            _one_epoch(data, profiled=True)
        )
        if (
            (reference_loss, reference_mae) != (measured_loss, measured_mae)
            or reference_output != measured_output
            or any(
                not torch.equal(reference_state[name], measured_state[name])
                for name in reference_state
            )
        ):
            raise ThroughputError(
                "instrumentation changed training or validation values"
            )
        samples.append(
            {
                "reference": reference,
                "profiled": measured,
                "overhead_ratio": measured["epoch_wall_seconds"]
                / reference["epoch_wall_seconds"],
            }
        )
    return workload, samples


def _revision(distribution_name):
    import json

    direct_url = importlib.metadata.distribution(distribution_name).read_text(
        "direct_url.json"
    )
    if direct_url is None:
        raise ThroughputError(f"{distribution_name} lacks exact VCS provenance")
    try:
        revision = json.loads(direct_url)["vcs_info"]["commit_id"]
    except (KeyError, TypeError, ValueError) as error:
        raise ThroughputError(
            f"{distribution_name} lacks exact VCS provenance"
        ) from error
    _hex(revision, 40, distribution_name)
    return revision


def provenance():
    import torch

    from lisjong_arena.stage3_optimization_saturation.protocol import RIICHIENV_VERSION

    revision = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
    status = subprocess.check_output(["git", "status", "--porcelain"], text=True)
    if status:
        raise ThroughputError("measurement requires a clean committed Arena revision")
    _hex(revision, 40, "Arena revision")
    if (
        importlib.metadata.version("riichienv") != RIICHIENV_VERSION
        or torch.__version__ != "2.13.0+cpu"
        or torch.cuda.is_available()
    ):
        raise ThroughputError("measurement runtime differs from the locked CPU path")
    if bool(sysconfig.get_config_var("Py_GIL_DISABLED")):
        raise ThroughputError("free-threaded Python is outside the locked path")
    if torch.get_num_threads() != 1 or not torch.are_deterministic_algorithms_enabled():
        raise ThroughputError("Torch runtime differs from the locked training config")
    return {
        "revisions": {
            "arena": revision,
            "lisjong": _revision("lisjong"),
            "lisjong_engine": _revision("lisjong-engine"),
        },
        "runtime": {
            "python": platform.python_version(),
            "torch": str(torch.__version__),
            "riichienv": importlib.metadata.version("riichienv"),
            "os": platform.platform(),
            "cpu": platform.processor() or platform.machine(),
            "torch_threads": torch.get_num_threads(),
            "deterministic_algorithms": torch.are_deterministic_algorithms_enabled(),
            "free_threaded": False,
        },
        "retained": retained_value(),
    }


def _hex(value, length, name):
    if (
        type(value) is not str
        or len(value) != length
        or any(ch not in "0123456789abcdef" for ch in value)
    ):
        raise ThroughputError(f"{name} is not a lowercase hexadecimal identity")


def _seconds(value, name):
    result = expect_float(value, name)
    if not math.isfinite(result) or result < 0:
        raise ThroughputError(f"{name} must be finite and non-negative")
    return result


def _summary(samples):
    medians = {
        name: statistics.median(
            sample["profiled"]["components"][name]["wall_seconds"] for sample in samples
        )
        for name in COMPONENTS
    }
    ranked = sorted(COMPONENTS, key=lambda name: (-medians[name], name))
    return {
        "median_reference_wall_seconds": statistics.median(
            s["reference"]["epoch_wall_seconds"] for s in samples
        ),
        "median_profiled_wall_seconds": statistics.median(
            s["profiled"]["epoch_wall_seconds"] for s in samples
        ),
        "median_overhead_ratio": statistics.median(
            s["overhead_ratio"] for s in samples
        ),
        "min_overhead_ratio": min(s["overhead_ratio"] for s in samples),
        "max_overhead_ratio": max(s["overhead_ratio"] for s in samples),
        "largest_component": ranked[0],
        "second_largest_component": ranked[1],
        "top_two_combined_share": statistics.median(
            (
                s["profiled"]["components"][ranked[0]]["wall_seconds"]
                + s["profiled"]["components"][ranked[1]]["wall_seconds"]
            )
            / s["profiled"]["epoch_wall_seconds"]
            for s in samples
        ),
    }


def make_result(workload, samples, source):
    result = {
        "schema": SCHEMA,
        "method": METHOD,
        "provenance": source,
        "workload": workload,
        "samples": samples,
        "summary": _summary(samples),
    }
    validate_result(result)
    return result


def validate_result(value):
    try:
        value = expect_object(
            value,
            {"schema", "method", "provenance", "workload", "samples", "summary"},
            "result",
        )
        if value["schema"] != SCHEMA or value["method"] != METHOD:
            raise ThroughputError("schema or method differs")
        source = expect_object(
            value["provenance"], {"revisions", "runtime", "retained"}, "provenance"
        )
        revisions = expect_object(
            source["revisions"], {"arena", "lisjong", "lisjong_engine"}, "revisions"
        )
        for name, revision in revisions.items():
            _hex(revision, 40, name)
        runtime = expect_object(
            source["runtime"],
            {
                "python",
                "torch",
                "riichienv",
                "os",
                "cpu",
                "torch_threads",
                "deterministic_algorithms",
                "free_threaded",
            },
            "runtime",
        )
        for name in ("python", "torch", "riichienv", "os", "cpu"):
            if not expect_str(runtime[name], name):
                raise ThroughputError(f"{name} is empty")
        if (
            not runtime["python"].startswith("3.14.")
            or runtime["torch"] != "2.13.0+cpu"
            or runtime["riichienv"] != "0.4.8"
            or expect_int(runtime["torch_threads"], "torch_threads") != 1
            or expect_bool(runtime["deterministic_algorithms"], "determinism")
            is not True
            or expect_bool(runtime["free_threaded"], "free_threaded") is not False
        ):
            raise ThroughputError("runtime differs from the locked CPU path")
        if canonical_json_text(source["retained"]) != canonical_json_text(
            retained_value()
        ):
            raise ThroughputError("retained identity differs")
        workload = expect_object(
            value["workload"],
            {
                "train_hanchan",
                "train_sequences",
                "train_steps",
                "validation_hanchan",
                "validation_sequences",
                "validation_steps",
                "train_backward_calls",
                "forward_calls",
                "constraint_calls",
            },
            "workload",
        )
        for name, count in workload.items():
            if expect_int(count, name) <= 0:
                raise ThroughputError("workload counts must be positive")
        if (
            workload["train_hanchan"] > workload["train_sequences"]
            or workload["train_sequences"] > workload["train_steps"]
            or workload["validation_hanchan"] > workload["validation_sequences"]
            or workload["validation_sequences"] > workload["validation_steps"]
        ):
            raise ThroughputError("workload count order differs")
        if (
            not workload["train_sequences"]
            <= workload["train_backward_calls"]
            <= workload["train_steps"]
            or workload["forward_calls"]
            != workload["train_steps"] + workload["validation_steps"]
            or workload["constraint_calls"] != workload["forward_calls"]
        ):
            raise ThroughputError("forward, constraint or backward count differs")
        samples = expect_list(value["samples"], "samples")
        if len(samples) != 3:
            raise ThroughputError("three measured pairs are required")
        for sample in samples:
            sample = expect_object(
                sample, {"reference", "profiled", "overhead_ratio"}, "sample"
            )
            for name in ("reference", "profiled"):
                expected = {
                    "epoch_wall_seconds",
                    "epoch_cpu_seconds",
                    "training_wall_seconds",
                    "training_cpu_seconds",
                    "validation_wall_seconds",
                    "validation_cpu_seconds",
                }
                if name == "profiled":
                    expected.add("components")
                row = expect_object(sample[name], expected, name)
                for field in expected - {"components"}:
                    _seconds(row[field], field)
                if (
                    row["epoch_wall_seconds"] <= 0
                    or row["training_wall_seconds"] + row["validation_wall_seconds"]
                    > row["epoch_wall_seconds"] + 1e-9
                    or row["training_cpu_seconds"] + row["validation_cpu_seconds"]
                    > row["epoch_cpu_seconds"] + 1e-7
                ):
                    raise ThroughputError("stage wall times exceed epoch")
            row = sample["profiled"]
            components = expect_object(row["components"], set(COMPONENTS), "components")
            for name, component in components.items():
                component = expect_object(
                    component,
                    {
                        "wall_seconds",
                        "cpu_seconds",
                        "calls",
                        "share_of_epoch_wall_time",
                    },
                    name,
                )
                for field in (
                    "wall_seconds",
                    "cpu_seconds",
                    "share_of_epoch_wall_time",
                ):
                    _seconds(component[field], f"{name}.{field}")
                if expect_int(component["calls"], f"{name}.calls") <= 0:
                    raise ThroughputError("component calls must be positive")
                if (
                    component["share_of_epoch_wall_time"]
                    != component["wall_seconds"] / row["epoch_wall_seconds"]
                ):
                    raise ThroughputError("component share differs")
            for prefix, stage in (
                ("train_", "training_wall_seconds"),
                ("validation_", "validation_wall_seconds"),
            ):
                if not math.isclose(
                    sum(
                        v["wall_seconds"]
                        for k, v in components.items()
                        if k.startswith(prefix)
                    ),
                    row[stage],
                    abs_tol=1e-8,
                ):
                    raise ThroughputError("component stage wall time differs")
            for prefix, stage_cpu in (
                ("train_", "training_cpu_seconds"),
                ("validation_", "validation_cpu_seconds"),
            ):
                if not math.isclose(
                    sum(
                        v["cpu_seconds"]
                        for k, v in components.items()
                        if k.startswith(prefix)
                    ),
                    row[stage_cpu],
                    rel_tol=0,
                    abs_tol=1e-7,
                ):
                    raise ThroughputError("component stage CPU time differs")
            if not math.isclose(
                sum(v["wall_seconds"] for v in components.values()),
                row["epoch_wall_seconds"],
                abs_tol=1e-8,
            ):
                raise ThroughputError("exclusive component wall sum differs")
            if not math.isclose(
                sum(v["cpu_seconds"] for v in components.values()),
                row["epoch_cpu_seconds"],
                rel_tol=0,
                abs_tol=1e-7,
            ):
                raise ThroughputError("exclusive component CPU sum differs")
            for name, count in (
                ("train_tensor", 2 * workload["train_steps"]),
                ("train_forward_constraint", workload["train_steps"]),
                ("train_optimizer", 1),
                ("validation_tensor", workload["validation_steps"]),
                ("validation_forward_constraint", workload["validation_steps"]),
                ("validation_remap", workload["validation_steps"] + 1),
                ("validation_metrics", 1),
                ("train_other", 1),
                ("validation_other", 1),
                ("epoch_other", 1),
            ):
                if components[name]["calls"] != count:
                    raise ThroughputError(f"{name} call count differs")
            if (
                components["train_backward"]["calls"]
                != workload["train_backward_calls"]
            ):
                raise ThroughputError("backward call count differs")
            if (
                _seconds(sample["overhead_ratio"], "overhead_ratio")
                != row["epoch_wall_seconds"] / sample["reference"]["epoch_wall_seconds"]
            ):
                raise ThroughputError("overhead ratio differs")
        summary = expect_object(value["summary"], set(_summary(samples)), "summary")
        for name in (
            "median_reference_wall_seconds",
            "median_profiled_wall_seconds",
            "median_overhead_ratio",
            "min_overhead_ratio",
            "max_overhead_ratio",
            "top_two_combined_share",
        ):
            _seconds(summary[name], name)
        for name in ("largest_component", "second_largest_component"):
            expect_str(summary[name], name)
        if canonical_json_text(summary) != canonical_json_text(_summary(samples)):
            raise ThroughputError("derived summary differs")
        return value
    except ArtifactValidationError as error:
        raise ThroughputError(str(error)) from error


def save_result(path, value):
    validate_result(value)
    payload = dict(value)
    payload["result_identity"] = hashlib.sha256(
        canonical_json_text(value).encode()
    ).hexdigest()
    write_new_artifact_file(Path(path), canonical_json_text(payload))


def load_result(path):
    payload = expect_object(
        read_json_document(Path(path)),
        {
            "schema",
            "method",
            "provenance",
            "workload",
            "samples",
            "summary",
            "result_identity",
        },
        "result artifact",
    )
    identity = payload["result_identity"]
    _hex(identity, 64, "result identity")
    value = {name: item for name, item in payload.items() if name != "result_identity"}
    if identity != hashlib.sha256(canonical_json_text(value).encode()).hexdigest():
        raise ThroughputError("result identity differs")
    if Path(path).read_text(encoding="utf-8") != canonical_json_text(payload):
        raise ThroughputError("result bytes are not canonical")
    validate_result(value)
    return payload
