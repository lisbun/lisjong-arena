from __future__ import annotations

import json
import re
import subprocess
import sys
import tomllib
from dataclasses import dataclass
from importlib import metadata
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

_FULL_COMMIT = re.compile(r"\A[0-9a-f]{40}\Z").fullmatch
_NAME = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._-]*\Z").fullmatch


@dataclass(frozen=True, slots=True)
class ExpectedVcsPin:
    name: str
    repository_url: str
    revision: str
    declared_by: str


@dataclass(frozen=True, slots=True)
class InstalledVcsIdentity:
    name: str
    version: str
    repository_url: str
    revision: str


@dataclass(frozen=True, slots=True)
class EnvironmentCheck:
    identities: tuple[InstalledVcsIdentity, ...]
    errors: tuple[str, ...]
    pip_check_output: str

    @property
    def ok(self) -> bool:
        return not self.errors


class EnvironmentIdentityError(ValueError):
    pass


def _normalize_name(value: str) -> str:
    return re.sub(r"[-_.]+", "-", value).lower()


def _normalize_repository_url(value: str) -> str:
    raw = value.removeprefix("git+")
    parsed = urlsplit(raw)
    if parsed.scheme.lower() != "https" or parsed.hostname is None:
        raise EnvironmentIdentityError(f"unsupported repository URL: {value!r}")
    host = parsed.hostname.lower()
    path = parsed.path.rstrip("/")
    if path.endswith(".git"):
        path = path[:-4]
    path = path.lower()
    return urlunsplit(("https", host, path, "", ""))


def _is_internal_repository(repository_url: str) -> bool:
    parsed = urlsplit(repository_url)
    parts = [part for part in parsed.path.split("/") if part]
    return (
        parsed.hostname == "github.com"
        and len(parts) == 2
        and parts[0] == "lisbun"
        and parts[1].startswith("lisjong")
    )


def _parse_exact_vcs_requirement(
    requirement: str, declared_by: str
) -> ExpectedVcsPin | None:
    if ";" in requirement:
        base, _marker = requirement.split(";", 1)
        if "git+" not in base:
            return None
        raise EnvironmentIdentityError(
            f"internal VCS dependency markers are unsupported: {requirement!r}"
        )
    if "git+" not in requirement:
        return None
    try:
        raw_name, raw_reference = requirement.split("@", 1)
    except ValueError as exc:
        raise EnvironmentIdentityError(
            f"malformed VCS requirement: {requirement!r}"
        ) from exc
    name = raw_name.strip()
    if _NAME(name) is None:
        raise EnvironmentIdentityError(f"malformed distribution name: {name!r}")
    reference = raw_reference.strip()
    if not reference.startswith("git+") or "@" not in reference:
        return None
    raw_url, revision = reference.rsplit("@", 1)
    if _FULL_COMMIT(revision) is None:
        raise EnvironmentIdentityError(
            f"internal VCS dependency must use a full lowercase commit: {requirement!r}"
        )
    repository_url = _normalize_repository_url(raw_url)
    if not _is_internal_repository(repository_url):
        return None
    return ExpectedVcsPin(
        name=_normalize_name(name),
        repository_url=repository_url,
        revision=revision,
        declared_by=declared_by,
    )


def _project_dependencies(project_path: Path) -> tuple[str, tuple[str, ...]]:
    try:
        document = tomllib.loads(project_path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise EnvironmentIdentityError(
            f"cannot read project metadata: {project_path}"
        ) from exc
    project = document.get("project")
    if type(project) is not dict:
        raise EnvironmentIdentityError("pyproject.toml lacks [project]")
    raw_name = project.get("name")
    dependencies = project.get("dependencies", [])
    if type(raw_name) is not str or not raw_name:
        raise EnvironmentIdentityError("[project].name must be a non-empty string")
    if type(dependencies) is not list or any(
        type(item) is not str for item in dependencies
    ):
        raise EnvironmentIdentityError("[project].dependencies must be a string array")
    return _normalize_name(raw_name), tuple(dependencies)


def _installed_identity(name: str) -> InstalledVcsIdentity:
    try:
        distribution = metadata.distribution(name)
    except metadata.PackageNotFoundError as exc:
        raise EnvironmentIdentityError(
            f"{name}: distribution is not installed"
        ) from exc
    direct_url_text = distribution.read_text("direct_url.json")
    if direct_url_text is None:
        raise EnvironmentIdentityError(f"{name}: direct_url.json is missing")
    try:
        direct_url = json.loads(direct_url_text)
    except json.JSONDecodeError as exc:
        raise EnvironmentIdentityError(f"{name}: direct_url.json is malformed") from exc
    if type(direct_url) is not dict or type(direct_url.get("vcs_info")) is not dict:
        raise EnvironmentIdentityError(f"{name}: VCS provenance is missing")
    url = direct_url.get("url")
    vcs_info = direct_url["vcs_info"]
    revision = vcs_info.get("commit_id")
    if type(url) is not str or vcs_info.get("vcs") != "git":
        raise EnvironmentIdentityError(f"{name}: VCS provenance is malformed")
    if type(revision) is not str or _FULL_COMMIT(revision) is None:
        raise EnvironmentIdentityError(
            f"{name}: installed commit is not a full lowercase commit"
        )
    return InstalledVcsIdentity(
        name=name,
        version=distribution.version,
        repository_url=_normalize_repository_url(url),
        revision=revision,
    )


def _add_pin(pins: dict[str, ExpectedVcsPin], pin: ExpectedVcsPin) -> str | None:
    previous = pins.get(pin.name)
    if previous is None:
        pins[pin.name] = pin
        return None
    if (
        previous.repository_url == pin.repository_url
        and previous.revision == pin.revision
    ):
        return None
    return (
        f"{pin.name}: incompatible internal pins: "
        f"{previous.declared_by} requires "
        f"{previous.repository_url}@{previous.revision}; "
        f"{pin.declared_by} requires {pin.repository_url}@{pin.revision}"
    )


def _collect_expected_pins(
    project_path: Path,
) -> tuple[dict[str, ExpectedVcsPin], list[str]]:
    project_name, dependencies = _project_dependencies(project_path)
    pins: dict[str, ExpectedVcsPin] = {}
    errors: list[str] = []
    for requirement in dependencies:
        pin = _parse_exact_vcs_requirement(
            requirement, f"{project_name} pyproject.toml"
        )
        if pin is not None:
            conflict = _add_pin(pins, pin)
            if conflict is not None:
                errors.append(conflict)

    queue = list(pins)
    visited: set[str] = set()
    while queue:
        name = queue.pop(0)
        if name in visited:
            continue
        visited.add(name)
        try:
            distribution = metadata.distribution(name)
        except metadata.PackageNotFoundError:
            continue
        for requirement in distribution.requires or ():
            pin = _parse_exact_vcs_requirement(
                requirement, f"installed {name} metadata"
            )
            if pin is None:
                continue
            is_new = pin.name not in pins
            conflict = _add_pin(pins, pin)
            if conflict is not None:
                errors.append(conflict)
            elif is_new:
                queue.append(pin.name)
    return pins, errors


def verify_environment(
    project_path: str | Path = "pyproject.toml",
) -> EnvironmentCheck:
    path = Path(project_path)
    pins, errors = _collect_expected_pins(path)
    identities: list[InstalledVcsIdentity] = []
    for name in sorted(pins):
        expected = pins[name]
        try:
            installed = _installed_identity(name)
        except EnvironmentIdentityError as exc:
            errors.append(str(exc))
            continue
        identities.append(installed)
        if installed.repository_url != expected.repository_url:
            errors.append(
                f"{name}: repository mismatch: "
                f"expected {expected.repository_url}; "
                f"installed {installed.repository_url}"
            )
        if installed.revision != expected.revision:
            errors.append(
                f"{name}: stale internal dependency: "
                f"expected {expected.revision}; "
                f"installed {installed.revision}"
            )

    process = subprocess.run(
        [sys.executable, "-m", "pip", "check"],
        check=False,
        capture_output=True,
        text=True,
    )
    pip_output = (process.stdout + process.stderr).strip()
    if process.returncode != 0:
        errors.append(f"pip check failed: {pip_output or f'exit {process.returncode}'}")
    return EnvironmentCheck(
        identities=tuple(identities),
        errors=tuple(errors),
        pip_check_output=pip_output,
    )
