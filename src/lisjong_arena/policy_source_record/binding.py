"""Teacher and runtime identity bound into a policy source record.

The teacher is named by an explicit ``policy_catalog`` identity. Its class,
factory and the exact imported lisjong source are recorded so that ``C0`` or
``main`` alone never identifies a teacher. The shanten backend is a process-wide
lisjong setting (``LISJONG_SHANTEN_BACKEND``); the backend actually loaded and,
for ``rust``, the native extension file digest are recorded as well.
"""

import hashlib
import subprocess
import sys
from collections.abc import Mapping
from importlib import metadata
from pathlib import Path

import lisjong
from lisjong.hand_evaluation import _shanten_backend

from lisjong_arena._artifact_io import canonical_json_text
from lisjong_arena.environment_identity import verify_environment
from lisjong_arena.model import PolicySpec
from lisjong_arena.policy_catalog import POLICY_CATALOG

from .errors import PolicySourceRecordError

PRODUCER = "lisjong_arena.policy_source_record"
RUNNER = "lisjong_arena.riichienv.local_game_runner.LocalGameRunner"
RECORDED_SEATS = "ALL"


def digest(value: object) -> str:
    return hashlib.sha256(canonical_json_text(value).encode()).hexdigest()


def _file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def resolve_teacher(
    identity: str, catalog: Mapping[str, PolicySpec] | None = None
) -> PolicySpec:
    catalog = POLICY_CATALOG if catalog is None else catalog
    if type(identity) is not str or identity not in catalog:
        raise PolicySourceRecordError(f"unknown teacher catalog identity: {identity!r}")
    spec = catalog[identity]
    if spec.identity != identity:
        raise PolicySourceRecordError("teacher catalog key/identity mismatch")
    return spec


def teacher_identity(
    identity: str, catalog: Mapping[str, PolicySpec] | None = None
) -> dict[str, object]:
    """Describe the exact teacher constructed from the catalog factory."""
    spec = resolve_teacher(identity, catalog)
    policy_type = type(spec.factory())
    factory = spec.factory
    configuration = {
        "factory": f"{factory.__module__}.{factory.__qualname__}",
        "arguments": {},
    }
    return {
        "catalog_identity": identity,
        "policy_class": f"{policy_type.__module__}.{policy_type.__qualname__}",
        "configuration": configuration,
        "configuration_digest": digest(configuration),
    }


def shanten_backend_identity() -> dict[str, object]:
    name = _shanten_backend.BACKEND_NAME
    if name == _shanten_backend.PYTHON_BACKEND:
        return {"name": name, "native": None}
    import _lisjong_native

    return {
        "name": name,
        "native": {
            "api_version": getattr(_lisjong_native, "API_VERSION", 1),
            "sha256": _file_sha256(Path(_lisjong_native.__file__)),
        },
    }


def lisjong_source_digest() -> str:
    root = Path(lisjong.__file__).parent
    sources = {
        str(path.relative_to(root)).replace("\\", "/"): _file_sha256(path)
        for path in sorted(root.rglob("*.py"))
    }
    return digest(sources)


def runtime_binding(project: str | Path = "pyproject.toml") -> dict[str, object]:
    """Bind the committed Arena checkout and the installed runtime actually used."""
    project = Path(project).resolve()
    if (
        project.parent != Path(__file__).resolve().parents[3]
        or project.name != "pyproject.toml"
    ):
        raise PolicySourceRecordError(
            "STOP / INVALID: project must own the imported Arena checkout"
        )
    check = verify_environment(project)
    if check.errors:
        raise PolicySourceRecordError("STOP / INVALID: " + "; ".join(check.errors))

    def git(*args: str) -> str:
        return subprocess.check_output(
            ["git", "-C", str(project.parent), *args], text=True
        ).strip()

    if git("status", "--porcelain", "--untracked-files=no") or git(
        "status", "--porcelain", "--untracked-files=normal", "--", "src"
    ):
        raise PolicySourceRecordError(
            "STOP / INVALID: tracked source changes must be committed"
        )
    return {
        "arena_revision": git("rev-parse", "HEAD"),
        "dependencies": {i.name: i.revision for i in check.identities},
        "lisjong_source_digest": lisjong_source_digest(),
        "shanten_backend": shanten_backend_identity(),
        "python": ".".join(map(str, sys.version_info[:3])),
        "riichienv": metadata.version("riichienv"),
    }


def source_contract(
    teacher: str,
    *,
    game_mode: str,
    project: str | Path = "pyproject.toml",
    catalog: Mapping[str, PolicySpec] | None = None,
) -> dict[str, object]:
    """The complete teacher / runtime / producer binding of one generation run."""
    return {
        "teacher": teacher_identity(teacher, catalog),
        "seat_policies": [teacher] * 4,
        "game_mode": game_mode,
        "runtime": runtime_binding(project),
        "producer": {
            "module": PRODUCER,
            "runner": RUNNER,
            "recorded_seats": RECORDED_SEATS,
        },
    }


REPLAY_RUNTIME_FIELDS = (
    "dependencies",
    "lisjong_source_digest",
    "shanten_backend",
    "python",
    "riichienv",
)
"""Runtime fields that must equal the manifest before a teacher replay.

``arena_revision`` is reported but not required: Arena only projects the
recorded typed values; the teacher decision is owned by the installed lisjong
source and backend bound above.
"""


__all__ = [
    "PRODUCER",
    "RECORDED_SEATS",
    "REPLAY_RUNTIME_FIELDS",
    "RUNNER",
    "digest",
    "lisjong_source_digest",
    "resolve_teacher",
    "runtime_binding",
    "shanten_backend_identity",
    "source_contract",
    "teacher_identity",
]
