"""Frozen-arm and exact historical-generation preflight for Phase 9."""

import hashlib
import importlib
import json
import os
import platform
import subprocess
import sys
from importlib.metadata import PackageNotFoundError, distribution
from pathlib import Path

from lisjong_arena.phase6_snapshot.artifact import (
    artifact_logical_identity as snapshot_logical_identity,
)
from lisjong_arena.phase6_snapshot.artifact import (
    load_model_artifact as load_snapshot_artifact,
)
from lisjong_arena.phase6_snapshot.feature import FEATURE_SEMANTICS_ID
from lisjong_arena.phase8_sequential.artifact import (
    artifact_logical_identity as s2_logical_identity,
)
from lisjong_arena.phase8_sequential.artifact import (
    load_model_artifact as load_s2_artifact,
)
from lisjong_arena.phase8_sequential.protocol import SEQUENCE_SEMANTICS_ID, Candidate

from .protocol import (
    EVALUATION_REVISIONS,
    EVALUATION_RIICHIENV_VERSION,
    EVALUATION_TORCH_VERSION,
    HISTORICAL_ARENA_REF,
    HISTORICAL_POLICY_POPULATION,
    HISTORICAL_REVISIONS,
    HISTORICAL_RIICHIENV_VERSION,
    HISTORICAL_TREES,
    HOLDOUT_GAME_COUNT,
    HOLDOUT_ROLE,
    HOLDOUT_SEEDS,
    LOCKED_RULE_FINGERPRINT,
    PROTOCOL_ID,
    S2_ARTIFACT_IDENTITY,
    S2_PARAMETER_COUNT,
    S2_SELECTED_EPOCH,
    S2_WEIGHTS_SHA256,
    SNAPSHOT_ARTIFACT_IDENTITY,
    SNAPSHOT_PARAMETER_COUNT,
    SNAPSHOT_WEIGHTS_SHA256,
)

PREFLIGHT_SCHEMA_VERSION = "phase9-preflight-v1"
GENERATION_REPORT_SCHEMA_VERSION = "phase9-generation-report-v1"
FORMAL_EXECUTION_ENVIRONMENT = "LISJONG_ARENA_PHASE9_FORMAL_EXECUTION"
FORMAL_EXECUTION_VALUE = "approved-after-reviewed-merge"


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _full_revision(value: object, name: str) -> str:
    if (
        type(value) is not str
        or len(value) != 40
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ValueError(f"{name} must be a full lowercase commit SHA")
    return value


def artifact_file_state(path: str | Path) -> dict[str, object]:
    path = Path(path)
    files = (path / "manifest.json", path / "weights.pt")
    if any(not file.is_file() for file in files):
        raise ValueError("artifact is missing manifest.json or weights.pt")
    return {
        file.name: {"sha256": _sha256(file.read_bytes()), "bytes": file.stat().st_size}
        for file in files
    }


def verify_frozen_arms(
    snapshot_path: str | Path, s2_path: str | Path
) -> tuple[object, object, dict[str, object]]:
    """Use the current strict loaders and then apply the Phase 9 identity lock."""
    before = {
        "snapshot": artifact_file_state(snapshot_path),
        "s2": artifact_file_state(s2_path),
    }
    snapshot = load_snapshot_artifact(snapshot_path)
    s2 = load_s2_artifact(s2_path)
    snapshot_checks = {
        "logical_identity": snapshot_logical_identity(snapshot.manifest),
        "weights_sha256": snapshot.manifest["weights_sha256"],
        "parameter_count": snapshot.manifest["parameter_count"],
        "feature_semantics_id": snapshot.manifest["feature_semantics_id"],
        "test_partition_evaluated": snapshot.manifest["test_partition_evaluated"],
    }
    if snapshot_checks != {
        "logical_identity": SNAPSHOT_ARTIFACT_IDENTITY,
        "weights_sha256": SNAPSHOT_WEIGHTS_SHA256,
        "parameter_count": SNAPSHOT_PARAMETER_COUNT,
        "feature_semantics_id": FEATURE_SEMANTICS_ID,
        "test_partition_evaluated": False,
    }:
        raise RuntimeError("frozen snapshot identity/config differs")
    s2_checks = {
        "candidate": s2.manifest["candidate"],
        "logical_identity": s2_logical_identity(s2.manifest),
        "weights_sha256": s2.manifest["weights_sha256"],
        "parameter_count": s2.manifest["parameter_count"],
        "selected_epoch": s2.manifest["selected_epoch"],
        "feature_semantics_id": s2.manifest["feature_semantics_id"],
        "sequence_semantics_id": s2.manifest["sequence_semantics_id"],
        "test_partition_evaluated": s2.manifest["test_partition_evaluated"],
    }
    if s2_checks != {
        "candidate": Candidate.S2.value,
        "logical_identity": S2_ARTIFACT_IDENTITY,
        "weights_sha256": S2_WEIGHTS_SHA256,
        "parameter_count": S2_PARAMETER_COUNT,
        "selected_epoch": S2_SELECTED_EPOCH,
        "feature_semantics_id": FEATURE_SEMANTICS_ID,
        "sequence_semantics_id": SEQUENCE_SEMANTICS_ID,
        "test_partition_evaluated": False,
    }:
        raise RuntimeError("frozen S2 identity/config differs")
    if s2.manifest["previous_belief_semantics"] != {
        "axis": "Wind->expected_count[34]",
        "current_order": "explicit-opponent_winds-remap",
        "scale": 4.0,
        "source": "prior-self-prediction",
    }:
        raise RuntimeError("frozen S2 previous-belief semantics differ")
    if s2.manifest["initial_state_semantics"] != {
        "depth_1_previous_belief": "current-public-conditional-uniform-baseline",
        "s2_latent": "zeros",
    }:
        raise RuntimeError("frozen S2 initial-state semantics differ")
    if s2.manifest["self_rollout_semantics"] != ("prediction_t->previous_belief_t+1"):
        raise RuntimeError("frozen S2 self-rollout semantics differ")
    after = {
        "snapshot": artifact_file_state(snapshot_path),
        "s2": artifact_file_state(s2_path),
    }
    if before != after:
        raise RuntimeError("frozen artifact bytes changed during strict verification")
    return snapshot, s2, before


def verify_artifact_state(
    snapshot_path: str | Path, s2_path: str | Path, expected: object
) -> None:
    actual = {
        "snapshot": artifact_file_state(snapshot_path),
        "s2": artifact_file_state(s2_path),
    }
    if actual != expected:
        raise RuntimeError("frozen artifact bytes differ from preflight")


def _git(repo: Path, *arguments: str) -> str:
    result = subprocess.run(
        ("git", "-c", f"safe.directory={repo.as_posix()}", "-C", str(repo), *arguments),
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def verify_current_checkout_revision(revision: str, repo: str | Path = ".") -> str:
    """Bind declared creation provenance to the checkout running the command."""
    revision = _full_revision(revision, "creation_software_revision")
    repo = Path(repo).resolve()
    expected_source = (
        repo / "src" / "lisjong_arena" / "phase9_confirmatory" / "preflight.py"
    ).resolve()
    if Path(__file__).resolve() != expected_source:
        raise RuntimeError("executing Phase 9 code is not from the current checkout")
    actual = _git(repo, "rev-parse", "HEAD")
    if actual != revision:
        raise RuntimeError("creation revision differs from the current Arena checkout")
    if _git(repo, "status", "--porcelain=v1", "--untracked-files=no"):
        raise RuntimeError("current Arena checkout has tracked or staged changes")
    return actual


def _installed_vcs_revision(name: str) -> str:
    try:
        direct_url = distribution(name).read_text("direct_url.json")
    except PackageNotFoundError as error:
        raise RuntimeError(f"formal evaluation requires installed {name}") from error
    if direct_url is None:
        raise RuntimeError(f"formal evaluation requires VCS provenance for {name}")
    try:
        value = json.loads(direct_url)
    except json.JSONDecodeError as error:
        raise RuntimeError(f"{name} direct_url.json is malformed") from error
    if type(value) is not dict:
        raise RuntimeError(f"{name} direct_url.json is malformed")
    vcs = value.get("vcs_info")
    if (
        type(vcs) is not dict
        or vcs.get("vcs") != "git"
        or "dir_info" in value
        or type(vcs.get("commit_id")) is not str
    ):
        raise RuntimeError(
            f"formal evaluation rejects local/editable provenance for {name}"
        )
    return _full_revision(vcs["commit_id"], f"{name} installed revision")


def verify_formal_evaluation_runtime() -> dict[str, object]:
    """Fail before holdout inference unless the evaluation environment is exact."""
    if sys.version_info[:2] != (3, 14):
        raise RuntimeError("formal Phase 9 evaluation requires CPython 3.14")
    revisions = {
        "lisjong": _installed_vcs_revision("lisjong"),
        "lisjong_engine": _installed_vcs_revision("lisjong-engine"),
    }
    if revisions != EVALUATION_REVISIONS:
        raise RuntimeError("formal evaluation dependency revisions differ")
    try:
        riichienv = distribution("riichienv").version
    except PackageNotFoundError as error:
        raise RuntimeError("formal evaluation requires RiichiEnv") from error
    if riichienv != EVALUATION_RIICHIENV_VERSION:
        raise RuntimeError("formal evaluation RiichiEnv version differs")
    torch = importlib.import_module("torch")
    if torch.__version__ != EVALUATION_TORCH_VERSION or torch.cuda.is_available():
        raise RuntimeError("formal Phase 9 evaluation requires PyTorch 2.13.0 CPU")
    return {
        "python": platform.python_version(),
        "torch": torch.__version__,
        "riichienv": riichienv,
        "installed_revisions": revisions,
    }


def _repository_state(
    repo: str | Path, revision: str, expected_tree: str
) -> dict[str, str]:
    repo = Path(repo).resolve()
    resolved = _git(repo, "rev-parse", f"{revision}^{{commit}}")
    if resolved != revision:
        raise RuntimeError("historical repository revision resolves differently")
    tree = _git(repo, "rev-parse", f"{revision}^{{tree}}")
    if tree != expected_tree:
        raise RuntimeError("historical repository tree differs from the lock")
    checkout_revision = _git(repo, "rev-parse", "HEAD")
    checkout_tree = _git(repo, "rev-parse", "HEAD^{tree}")
    if (checkout_revision, checkout_tree) != (resolved, tree):
        raise RuntimeError("historical checkout HEAD/tree differs from the lock")
    origin = _git(repo, "remote", "get-url", "origin")
    return {
        "declared_revision": revision,
        "resolved_revision": resolved,
        "tree": tree,
        "checkout_revision": checkout_revision,
        "checkout_tree": checkout_tree,
        "origin": origin,
        "acquisition_method": "verified local git object from recorded origin",
    }


def _verify_historical_arena(repo: str | Path) -> dict[str, str]:
    repo = Path(repo).resolve()
    state = _repository_state(
        repo,
        HISTORICAL_REVISIONS["lisjong_arena"],
        HISTORICAL_TREES["lisjong_arena"],
    )
    candidates = (
        f"refs/remotes/origin/{HISTORICAL_ARENA_REF}",
        f"refs/heads/{HISTORICAL_ARENA_REF}",
    )
    matching = tuple(
        reference
        for reference in candidates
        if subprocess.run(
            (
                "git",
                "-c",
                f"safe.directory={repo.as_posix()}",
                "-C",
                str(repo),
                "show-ref",
                "--verify",
                "--quiet",
                reference,
            ),
            check=False,
        ).returncode
        == 0
        and _git(repo, "rev-parse", reference) == HISTORICAL_REVISIONS["lisjong_arena"]
    )
    if not matching:
        raise RuntimeError("preserved historical Arena ref is unavailable or differs")
    pyproject = _git(
        repo,
        "show",
        f"{HISTORICAL_REVISIONS['lisjong_arena']}:pyproject.toml",
    )
    required = (
        HISTORICAL_REVISIONS["lisjong"],
        HISTORICAL_REVISIONS["lisjong_engine"],
        f"riichienv=={HISTORICAL_RIICHIENV_VERSION}",
    )
    if any(value not in pyproject for value in required):
        raise RuntimeError("historical Arena dependency pins differ")
    state["acquisition_ref"] = HISTORICAL_ARENA_REF
    state["resolved_acquisition_ref"] = matching[0]
    return state


def preflight_value(
    *,
    snapshot_path: str | Path,
    s2_path: str | Path,
    lisjong_repo: str | Path,
    engine_repo: str | Path,
    arena_repo: str | Path,
    creation_software_revision: str,
) -> dict[str, object]:
    creation_revision = _full_revision(
        creation_software_revision, "creation_software_revision"
    )
    snapshot, s2, artifact_files = verify_frozen_arms(snapshot_path, s2_path)
    sources = {
        "lisjong": _repository_state(
            lisjong_repo,
            HISTORICAL_REVISIONS["lisjong"],
            HISTORICAL_TREES["lisjong"],
        ),
        "lisjong_engine": _repository_state(
            engine_repo,
            HISTORICAL_REVISIONS["lisjong_engine"],
            HISTORICAL_TREES["lisjong_engine"],
        ),
        "lisjong_arena": _verify_historical_arena(arena_repo),
    }
    value = {
        "preflight_schema_version": PREFLIGHT_SCHEMA_VERSION,
        "protocol_identity": PROTOCOL_ID,
        "creation_software_revision": creation_revision,
        "frozen_arms": {
            "snapshot": {
                "artifact_logical_identity": snapshot_logical_identity(
                    snapshot.manifest
                ),
                "weights_sha256": snapshot.manifest["weights_sha256"],
                "parameter_count": snapshot.manifest["parameter_count"],
                "model": snapshot.manifest["model"],
                "feature_semantics_id": snapshot.manifest["feature_semantics_id"],
            },
            "s2": {
                "artifact_logical_identity": s2_logical_identity(s2.manifest),
                "weights_sha256": s2.manifest["weights_sha256"],
                "parameter_count": s2.manifest["parameter_count"],
                "selected_epoch": s2.manifest["selected_epoch"],
                "candidate": s2.manifest["candidate"],
                "model": s2.manifest["model"],
                "feature_semantics_id": s2.manifest["feature_semantics_id"],
                "sequence_semantics_id": s2.manifest["sequence_semantics_id"],
                "previous_belief_semantics": s2.manifest["previous_belief_semantics"],
                "initial_state_semantics": s2.manifest["initial_state_semantics"],
                "self_rollout_semantics": s2.manifest["self_rollout_semantics"],
                "test_partition_evaluated": s2.manifest["test_partition_evaluated"],
            },
        },
        "artifact_files": artifact_files,
        "historical_generation": {
            "sources": sources,
            "effective_rules": {
                "name": "project-standard-v1",
                "version": 1,
                "fingerprint": LOCKED_RULE_FINGERPRINT,
            },
            "policy_population": HISTORICAL_POLICY_POPULATION,
            "riichienv_version": HISTORICAL_RIICHIENV_VERSION,
            "generation_protocol": "first-party-hand-belief-raw-v1",
        },
        "holdout": {
            "role": HOLDOUT_ROLE,
            "ordered_seeds": list(HOLDOUT_SEEDS),
            "game_count": HOLDOUT_GAME_COUNT,
            "contamination_search": "no concrete prior snapshot/S2 use found",
        },
        "formal_holdout_generated": False,
        "formal_holdout_evaluated": False,
    }
    value["preflight_identity"] = _sha256(_canonical_json(value))
    return value


def validate_preflight(value: object) -> dict[str, object]:
    fields = {
        "preflight_schema_version",
        "protocol_identity",
        "creation_software_revision",
        "frozen_arms",
        "artifact_files",
        "historical_generation",
        "holdout",
        "formal_holdout_generated",
        "formal_holdout_evaluated",
        "preflight_identity",
    }
    if type(value) is not dict or set(value) != fields:
        raise ValueError("preflight fields are not exact")
    identity = value["preflight_identity"]
    unsigned = {key: item for key, item in value.items() if key != "preflight_identity"}
    if identity != _sha256(_canonical_json(unsigned)):
        raise ValueError("preflight logical identity differs")
    if value["preflight_schema_version"] != PREFLIGHT_SCHEMA_VERSION:
        raise ValueError("preflight schema differs")
    if value["protocol_identity"] != PROTOCOL_ID:
        raise ValueError("preflight protocol differs")
    _full_revision(value["creation_software_revision"], "creation revision")
    if value["formal_holdout_generated"] is not False:
        raise ValueError("preflight must precede formal generation")
    if value["formal_holdout_evaluated"] is not False:
        raise ValueError("preflight must precede formal evaluation")
    holdout = value["holdout"]
    if holdout != {
        "role": HOLDOUT_ROLE,
        "ordered_seeds": list(HOLDOUT_SEEDS),
        "game_count": HOLDOUT_GAME_COUNT,
        "contamination_search": "no concrete prior snapshot/S2 use found",
    }:
        raise ValueError("preflight holdout lock differs")
    arms = value["frozen_arms"]
    if arms["snapshot"]["artifact_logical_identity"] != SNAPSHOT_ARTIFACT_IDENTITY:
        raise ValueError("preflight snapshot identity differs")
    if arms["snapshot"]["weights_sha256"] != SNAPSHOT_WEIGHTS_SHA256:
        raise ValueError("preflight snapshot weights differ")
    if (
        arms["snapshot"]["parameter_count"] != SNAPSHOT_PARAMETER_COUNT
        or arms["snapshot"]["feature_semantics_id"] != FEATURE_SEMANTICS_ID
    ):
        raise ValueError("preflight snapshot config differs")
    if arms["s2"]["artifact_logical_identity"] != S2_ARTIFACT_IDENTITY:
        raise ValueError("preflight S2 identity differs")
    if arms["s2"]["weights_sha256"] != S2_WEIGHTS_SHA256:
        raise ValueError("preflight S2 weights differ")
    if (
        arms["s2"]["candidate"] != Candidate.S2.value
        or arms["s2"]["parameter_count"] != S2_PARAMETER_COUNT
        or arms["s2"]["selected_epoch"] != S2_SELECTED_EPOCH
        or arms["s2"]["feature_semantics_id"] != FEATURE_SEMANTICS_ID
        or arms["s2"]["sequence_semantics_id"] != SEQUENCE_SEMANTICS_ID
        or arms["s2"]["test_partition_evaluated"] is not False
    ):
        raise ValueError("preflight S2 config differs")
    generation = value["historical_generation"]
    if generation["policy_population"] != HISTORICAL_POLICY_POPULATION:
        raise ValueError("preflight Policy population differs")
    if generation["riichienv_version"] != HISTORICAL_RIICHIENV_VERSION:
        raise ValueError("preflight RiichiEnv version differs")
    if generation["effective_rules"]["fingerprint"] != LOCKED_RULE_FINGERPRINT:
        raise ValueError("preflight rule fingerprint differs")
    for name, revision in HISTORICAL_REVISIONS.items():
        source = generation["sources"][name]
        required_fields = {
            "declared_revision",
            "resolved_revision",
            "tree",
            "checkout_revision",
            "checkout_tree",
            "origin",
            "acquisition_method",
        }
        if name == "lisjong_arena":
            required_fields |= {"acquisition_ref", "resolved_acquisition_ref"}
        if type(source) is not dict or set(source) != required_fields:
            raise ValueError(f"preflight {name} source fields differ")
        if (
            source["declared_revision"] != revision
            or source["resolved_revision"] != revision
            or source["checkout_revision"] != revision
            or source["tree"] != HISTORICAL_TREES[name]
            or source["checkout_tree"] != HISTORICAL_TREES[name]
        ):
            raise ValueError(f"preflight {name} revision differs")
    arena = generation["sources"]["lisjong_arena"]
    if arena["acquisition_ref"] != HISTORICAL_ARENA_REF:
        raise ValueError("preflight Arena acquisition ref differs")
    return value


def save_preflight(path: str | Path, value: dict[str, object]) -> Path:
    validate_preflight(value)
    path = Path(path)
    if path.exists():
        raise FileExistsError("Phase 9 preflight destination already exists")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(_canonical_json(value))
    return path


def load_preflight(path: str | Path) -> dict[str, object]:
    data = Path(path).read_bytes()
    try:
        value = json.loads(data)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("preflight is not strict JSON") from error
    if _canonical_json(value) != data:
        raise ValueError("preflight bytes are not canonical JSON")
    return validate_preflight(value)


def require_formal_execution_authorization() -> None:
    if os.environ.get(FORMAL_EXECUTION_ENVIRONMENT) != FORMAL_EXECUTION_VALUE:
        raise RuntimeError(
            "formal Phase 9 commands require the explicit post-merge execution guard"
        )


def verify_historical_runtime(python: str | Path) -> dict[str, object]:
    script = """
import importlib.metadata as m, json, platform
from lisjong.policies import TwoStepUkeirePolicy
from lisjong_engine.rules import RuleSet
from lisjong_arena.phase2_training_anchor.rule_provenance import effective_rule_provenance
def revision(name):
    value=json.loads(m.distribution(name).read_text('direct_url.json'))
    return value['vcs_info']['commit_id']
policies=tuple(TwoStepUkeirePolicy() for _ in range(4))
rules=effective_rule_provenance(RuleSet.default())
print(json.dumps({'python':platform.python_version(),'revisions':{name.replace('-','_'):revision(name) for name in ('lisjong','lisjong-engine','lisjong-arena')},'riichienv':m.version('riichienv'),'rule_fingerprint':rules.fingerprint,'policy_count':len(policies),'distinct_policy_instances':len({id(value) for value in policies})},sort_keys=True))
"""
    environment = dict(os.environ)
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    result = subprocess.run(
        (str(Path(python)), "-c", script),
        check=True,
        capture_output=True,
        text=True,
        env=environment,
    )
    try:
        value = json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise RuntimeError("historical runtime probe returned invalid JSON") from error
    if tuple(int(part) for part in value["python"].split(".")[:2]) != (3, 14):
        raise RuntimeError("historical generation requires CPython 3.14")
    if value["revisions"] != HISTORICAL_REVISIONS:
        raise RuntimeError("historical runtime source revisions differ")
    if value["riichienv"] != HISTORICAL_RIICHIENV_VERSION:
        raise RuntimeError("historical runtime RiichiEnv version differs")
    if value["rule_fingerprint"] != LOCKED_RULE_FINGERPRINT:
        raise RuntimeError("historical runtime effective rules differ")
    if (value["policy_count"], value["distinct_policy_instances"]) != (4, 4):
        raise RuntimeError("historical Policy population is not four fresh instances")
    return value


def generate_formal_raw_corpus(
    *, historical_python: str | Path, destination: str | Path
) -> dict[str, object]:
    """Invoke only the exact historical Phase 4 generator, behind the formal guard."""
    require_formal_execution_authorization()
    destination = Path(destination)
    if destination.exists():
        raise FileExistsError("Phase 9 raw destination already exists")
    runtime = verify_historical_runtime(historical_python)
    script = """
import json, sys
from lisjong_arena.phase4_raw_corpus.generation import generate_phase4_raw_corpus_for_seeds
report=generate_phase4_raw_corpus_for_seeds(sys.argv[1], tuple(range(160,180)))
print(json.dumps({'raw_corpus_identity':report.persisted.corpus_identity,'ordered_seeds':[game.seed for game in report.persisted.corpus.games],'hanchan_count':report.measurements.hanchan_count,'turn_anchor_count':report.measurements.derived_turn_samples,'failure_count':report.failure_count,'phase2_equality_verified':report.phase2_equality_verified},sort_keys=True))
"""
    environment = dict(os.environ)
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    result = subprocess.run(
        (str(Path(historical_python)), "-c", script, str(destination)),
        check=True,
        capture_output=True,
        text=True,
        env=environment,
    )
    value = json.loads(result.stdout)
    if value["ordered_seeds"] != list(HOLDOUT_SEEDS):
        raise RuntimeError("historical generation returned wrong seeds")
    if value["hanchan_count"] != HOLDOUT_GAME_COUNT:
        raise RuntimeError("historical generation returned wrong game count")
    if value["failure_count"] != 0 or value["phase2_equality_verified"] is not True:
        raise RuntimeError("historical generation validation failed")
    return {"runtime": runtime, "generation": value}


def generation_report_value(
    preflight_identity: str, execution: dict[str, object]
) -> dict[str, object]:
    report = {
        "generation_report_schema_version": GENERATION_REPORT_SCHEMA_VERSION,
        "protocol_identity": PROTOCOL_ID,
        "preflight_identity": preflight_identity,
        "runtime": execution["runtime"],
        "generation": execution["generation"],
    }
    report["generation_report_identity"] = _sha256(_canonical_json(report))
    return validate_generation_report(report)


def validate_generation_report(value: object) -> dict[str, object]:
    fields = {
        "generation_report_schema_version",
        "protocol_identity",
        "preflight_identity",
        "runtime",
        "generation",
        "generation_report_identity",
    }
    if type(value) is not dict or set(value) != fields:
        raise ValueError("generation report fields are not exact")
    identity = value["generation_report_identity"]
    unsigned = {
        key: item for key, item in value.items() if key != "generation_report_identity"
    }
    if identity != _sha256(_canonical_json(unsigned)):
        raise ValueError("generation report identity differs")
    if value["generation_report_schema_version"] != GENERATION_REPORT_SCHEMA_VERSION:
        raise ValueError("generation report schema differs")
    if value["protocol_identity"] != PROTOCOL_ID:
        raise ValueError("generation report protocol differs")
    _digest = value["preflight_identity"]
    if (
        type(_digest) is not str
        or len(_digest) != 64
        or any(character not in "0123456789abcdef" for character in _digest)
    ):
        raise ValueError("generation report preflight identity is invalid")
    runtime = value["runtime"]
    if (
        type(runtime) is not dict
        or set(runtime)
        != {
            "python",
            "revisions",
            "riichienv",
            "rule_fingerprint",
            "policy_count",
            "distinct_policy_instances",
        }
        or type(runtime.get("python")) is not str
        or tuple(int(part) for part in runtime["python"].split(".")[:2]) != (3, 14)
        or runtime.get("revisions") != HISTORICAL_REVISIONS
        or runtime.get("riichienv") != HISTORICAL_RIICHIENV_VERSION
        or runtime.get("rule_fingerprint") != LOCKED_RULE_FINGERPRINT
        or runtime.get("policy_count") != 4
        or runtime.get("distinct_policy_instances") != 4
    ):
        raise ValueError("generation runtime provenance differs")
    generation = value["generation"]
    if type(generation) is not dict or set(generation) != {
        "raw_corpus_identity",
        "ordered_seeds",
        "hanchan_count",
        "turn_anchor_count",
        "failure_count",
        "phase2_equality_verified",
    }:
        raise ValueError("generation report fields differ")
    raw_identity = generation.get("raw_corpus_identity")
    if (
        type(raw_identity) is not str
        or len(raw_identity) != 64
        or any(character not in "0123456789abcdef" for character in raw_identity)
        or generation.get("ordered_seeds") != list(HOLDOUT_SEEDS)
        or generation.get("hanchan_count") != HOLDOUT_GAME_COUNT
        or generation.get("failure_count") != 0
        or generation.get("phase2_equality_verified") is not True
        or type(generation.get("turn_anchor_count")) is not int
        or generation["turn_anchor_count"] <= 0
    ):
        raise ValueError("generation report holdout result differs")
    return value


def save_generation_report(
    path: str | Path, preflight_identity: str, execution: dict[str, object]
) -> dict[str, object]:
    value = generation_report_value(preflight_identity, execution)
    path = Path(path)
    if path.exists():
        raise FileExistsError("Phase 9 generation report destination already exists")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(_canonical_json(value))
    return value


def load_generation_report(path: str | Path) -> dict[str, object]:
    data = Path(path).read_bytes()
    try:
        value = json.loads(data)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("generation report is not strict JSON") from error
    if _canonical_json(value) != data:
        raise ValueError("generation report bytes are not canonical JSON")
    return validate_generation_report(value)


__all__ = [
    "FORMAL_EXECUTION_ENVIRONMENT",
    "FORMAL_EXECUTION_VALUE",
    "GENERATION_REPORT_SCHEMA_VERSION",
    "PREFLIGHT_SCHEMA_VERSION",
    "artifact_file_state",
    "generate_formal_raw_corpus",
    "generation_report_value",
    "load_generation_report",
    "load_preflight",
    "preflight_value",
    "require_formal_execution_authorization",
    "save_preflight",
    "save_generation_report",
    "validate_generation_report",
    "validate_preflight",
    "verify_artifact_state",
    "verify_current_checkout_revision",
    "verify_frozen_arms",
    "verify_formal_evaluation_runtime",
    "verify_historical_runtime",
]
