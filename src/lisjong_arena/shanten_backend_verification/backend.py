"""Current wheel identity and per-process shanten backend checks (#400, #409).

lisjong selects its numeric shanten core once per process from
``LISJONG_SHANTEN_BACKEND`` (``python`` / ``rust``) and already fails closed
when ``rust`` is requested but ``_lisjong_native`` cannot be imported.  Arena
adds the checks that lisjong cannot make on its own:

- the wheel file is exactly the frozen ``main`` CI build (file name and
  SHA-256), checked before it is installed;
- the backend is selected explicitly (an unset variable is refused here, so a
  run cannot silently measure the default);
- the installed lisjong commit is the frozen pin, and for ``rust`` the
  extension reports the same build revision (``SOURCE_REVISION``) and native
  entry-point set (``API_VERSION``), because the extension mirrors that lisjong
  revision's tables and dispatch;
- the extension files that Python actually loads are the files of the frozen
  wheel (``verify_installed_native``), because the #400 and #409 wheels share
  one file name and package version and pip may skip reinstalling it;
- the selected core actually runs: for ``rust`` the native call counter moves
  on a public ``calculate_shanten()`` call; for ``python`` the extension is not
  even imported.

The ``EXPECTED_*`` values are the *current* combination (#409).  The frozen
#400 / #406 runs used the lisjong#217 combination, which ``plan`` keeps for
judging that evidence; those runs are reproduced from their recorded Arena
commits, not with these values.

Every failure raises ``ShantenBackendVerificationError``.  Nothing falls back
to the other backend.
"""

from __future__ import annotations

import hashlib
import os
import sys
import zipfile
from pathlib import Path

from lisjong_arena.environment_identity import (
    EnvironmentIdentityError,
    _installed_identity,
)

BACKEND_ENVIRONMENT_VARIABLE = "LISJONG_SHANTEN_BACKEND"
PYTHON_BACKEND = "python"
RUST_BACKEND = "rust"
BACKENDS = (PYTHON_BACKEND, RUST_BACKEND)

EXPECTED_LISJONG_REVISION = "8bdfd3f942ced49830bcee1894aefe3d2e0acc3a"
"""lisjong#225 merge on ``main``; the project pin and the wheel build source."""

EXPECTED_WHEEL_FILENAME = "lisjong_native-0.1.0-cp314-cp314-manylinux_2_28_x86_64.whl"
EXPECTED_WHEEL_SHA256 = (
    "22ce171059416ba8e25c8801ec2ef425afeacfda792ae67d837865610269e7f8"
)
"""``lisjong-native-wheel-<revision>`` artifact of lisjong CI run 36296356964
(push to ``main``)."""

EXPECTED_NATIVE_API_VERSION = 2
"""``_lisjong_native.API_VERSION`` of the lisjong#225 extension; a wheel built
before lisjong#224 has no such attribute (lisjong treats it as 1)."""

NATIVE_MODULE = "_lisjong_native"

# 13 closed tiles; any hand works, the check only needs one public call.
_PROBE_HAND = "123m456p789s1122z"


class ShantenBackendVerificationError(RuntimeError):
    """The requested shanten backend is not the frozen, working configuration."""


def file_sha256(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_wheel_file(path: str | Path) -> dict[str, object]:
    """Check that ``path`` is the frozen wheel before it is installed."""
    wheel = Path(path)
    if not wheel.is_file():
        raise ShantenBackendVerificationError(f"wheel is missing: {wheel}")
    if wheel.name != EXPECTED_WHEEL_FILENAME:
        raise ShantenBackendVerificationError(
            f"unexpected wheel file name {wheel.name!r}; "
            f"expected {EXPECTED_WHEEL_FILENAME!r}"
        )
    digest = file_sha256(wheel)
    if digest != EXPECTED_WHEEL_SHA256:
        raise ShantenBackendVerificationError(
            f"wheel SHA-256 mismatch: {digest}; expected {EXPECTED_WHEEL_SHA256}"
        )
    return {"file": wheel.name, "sha256": digest, "bytes": wheel.stat().st_size}


def verify_installed_native(path: str | Path) -> dict[str, object]:
    """Check that the loaded ``_lisjong_native`` files are the frozen wheel's.

    Call it after ``require_shanten_backend("rust")``.  The wheel is checked
    first, then every file of its ``_lisjong_native`` package is compared
    byte for byte with the file next to the imported module.
    """
    wheel = verify_wheel_file(path)
    native = sys.modules.get(NATIVE_MODULE)
    if native is None or getattr(native, "__file__", None) is None:
        raise ShantenBackendVerificationError(
            f"{NATIVE_MODULE} is not imported in this process"
        )
    package_directory = Path(native.__file__).parent
    prefix = f"{NATIVE_MODULE}/"
    compared = []
    with zipfile.ZipFile(path) as archive:
        members = sorted(
            name
            for name in archive.namelist()
            if name.startswith(prefix) and not name.endswith("/")
        )
        if not members:
            raise ShantenBackendVerificationError(
                f"the wheel has no {NATIVE_MODULE} package files"
            )
        for name in members:
            installed = package_directory / name[len(prefix) :]
            if not installed.is_file():
                raise ShantenBackendVerificationError(
                    f"{installed} from the wheel is not installed"
                )
            expected = hashlib.sha256(archive.read(name)).hexdigest()
            if file_sha256(installed) != expected:
                raise ShantenBackendVerificationError(
                    f"installed {installed} differs from the wheel's {name}; "
                    "reinstall the wheel with --force-reinstall"
                )
            compared.append(name)
    return {**wheel, "installed_directory": str(package_directory), "files": compared}


def _selected_backend(expected: str) -> str:
    if expected not in BACKENDS:
        raise ShantenBackendVerificationError(
            f"expected backend must be one of {BACKENDS}, got {expected!r}"
        )
    selected = os.environ.get(BACKEND_ENVIRONMENT_VARIABLE)
    if selected != expected:
        raise ShantenBackendVerificationError(
            f"{BACKEND_ENVIRONMENT_VARIABLE}={selected!r} in this process; "
            f"expected an explicit {expected!r}"
        )
    return selected


def _installed_lisjong_revision(expected_revision: str) -> str:
    try:
        revision = _installed_identity("lisjong").revision
    except EnvironmentIdentityError as error:
        raise ShantenBackendVerificationError(str(error)) from error
    if revision != expected_revision:
        raise ShantenBackendVerificationError(
            f"installed lisjong {revision} is not the pinned {expected_revision}"
        )
    return revision


def _probe_tiles():
    from lisjong.policy_contract.tile import Tile, TileCategory, TileType

    categories = {
        "m": TileCategory.MANZU,
        "p": TileCategory.PINZU,
        "s": TileCategory.SOUZU,
        "z": TileCategory.HONOR,
    }
    tiles = []
    ranks = []
    for character in _PROBE_HAND:
        if character.isdigit():
            ranks.append(int(character))
            continue
        tiles.extend(Tile(TileType(categories[character], rank)) for rank in ranks)
        ranks = []
    return tiles


def require_shanten_backend(
    expected: str, *, expected_revision: str = EXPECTED_LISJONG_REVISION
) -> dict[str, object]:
    """Verify this process's shanten backend and return its execution record.

    Call it in the parent before any game and once in every worker process
    (the workers inherit ``LISJONG_SHANTEN_BACKEND`` from the parent
    environment under ``spawn``).
    """
    backend = _selected_backend(expected)
    revision = _installed_lisjong_revision(expected_revision)
    if backend == PYTHON_BACKEND and NATIVE_MODULE in sys.modules:
        raise ShantenBackendVerificationError(
            f"{NATIVE_MODULE} is already imported in a python-backend process"
        )

    try:
        from lisjong.hand_evaluation import calculate_shanten
    except Exception as error:
        # lisjong raises ShantenBackendError at import time when rust is
        # selected and the extension is missing or unusable.
        raise ShantenBackendVerificationError(
            f"lisjong could not load the {backend} shanten backend: {error}"
        ) from error

    record: dict[str, object] = {
        "backend": backend,
        "lisjong_revision": revision,
        "pid": os.getpid(),
    }
    if backend == PYTHON_BACKEND:
        calculate_shanten(_probe_tiles())
        if NATIVE_MODULE in sys.modules:
            raise ShantenBackendVerificationError(
                f"the python backend imported {NATIVE_MODULE}"
            )
        record["native"] = None
        return record

    native = sys.modules.get(NATIVE_MODULE)
    if native is None:
        # lisjong imports the extension at import time for rust; reaching this
        # point means the rust core was not wired in.
        raise ShantenBackendVerificationError(
            f"lisjong did not load {NATIVE_MODULE} for the rust backend"
        )
    source_revision = getattr(native, "SOURCE_REVISION", None)
    if source_revision != expected_revision:
        raise ShantenBackendVerificationError(
            f"{NATIVE_MODULE}.SOURCE_REVISION={source_revision!r} does not match "
            f"the pinned lisjong {expected_revision}"
        )
    # lisjong reads a missing attribute as 1 and refuses it at import time;
    # checking again here ties the recorded value to this process.
    api_version = getattr(native, "API_VERSION", 1)
    if api_version != EXPECTED_NATIVE_API_VERSION:
        raise ShantenBackendVerificationError(
            f"{NATIVE_MODULE}.API_VERSION={api_version!r}; "
            f"expected {EXPECTED_NATIVE_API_VERSION}"
        )
    before = native.standard_shanten_call_count()
    calculate_shanten(_probe_tiles())
    calls = native.standard_shanten_call_count() - before
    if calls < 1:
        raise ShantenBackendVerificationError(
            "calculate_shanten() did not reach the native core"
        )
    record["native"] = {
        "module_file": native.__file__,
        "source_revision": source_revision,
        "api_version": api_version,
        "probe_native_calls": calls,
    }
    return record


def native_call_count() -> int | None:
    """Native core call counter of this process, or ``None`` without Rust."""
    native = sys.modules.get(NATIVE_MODULE)
    return None if native is None else native.standard_shanten_call_count()


def native_discard_evaluation_count() -> int | None:
    """Batched discard evaluation counter (lisjong#224), or ``None`` without Rust.

    Only the 0004 reference Policy's structural evaluation calls it, so a zero
    delta is expected for other Policies.
    """
    native = sys.modules.get(NATIVE_MODULE)
    return None if native is None else native.discard_evaluation_call_count()
