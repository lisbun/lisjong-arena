"""Current wheel identity and per-process shanten backend checks (#400, #409, #531).

Since lisjong#346 (pin ``89dd00d``) lisjong requires the native extension:
``LISJONG_SHANTEN_BACKEND`` unset or ``rust`` loads ``_lisjong_native``, and
``python`` or any other value raises ``ShantenBackendError`` when
``lisjong.hand_evaluation.shanten`` is imported.  New execution in Arena is
therefore Rust only (#531).  Arena refuses ``python`` and unknown values itself,
with a migration message, before lisjong reads the variable, and adds the
checks that lisjong cannot make on its own:

- the wheel file is exactly the frozen ``main`` CI build (file name and
  SHA-256), checked before it is installed;
- the installed lisjong commit is the frozen pin, and the extension reports the
  same build revision (``SOURCE_REVISION``) and native entry-point set
  (``API_VERSION``), because the extension mirrors that lisjong revision's
  tables and dispatch;
- the extension files that Python actually loads are the files of the frozen
  wheel (``verify_installed_native``), because wheels of different revisions
  share one file name and package version and pip may skip reinstalling it;
- the native core actually runs: the native call counter moves on a public
  ``calculate_shanten()`` call, and ``probe_r5_native`` moves the R5 counter
  through lisjong's production factory.

``RECORDED_BACKENDS`` keeps both names for *reading* saved records (#528 D3);
it is not a list of backends that can run.  A saved Python-backend run is
re-executed only with its recorded Arena / lisjong revision, wheel and
environment, never with these values.

The ``EXPECTED_*`` values are the *current* combination.  The frozen #400 /
#406 runs used the lisjong#217 combination, which ``plan`` keeps for judging
that evidence.

Every failure raises ``ShantenBackendVerificationError``.  Nothing falls back
to another backend.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import sys
import zipfile
from pathlib import Path

from lisjong_arena._shanten_environment import (
    BACKEND_ENVIRONMENT_VARIABLE as BACKEND_ENVIRONMENT_VARIABLE,
)
from lisjong_arena._shanten_environment import (
    PYTHON_BACKEND,
    RETIRED_PYTHON_BACKEND_MESSAGE,
    RUST_BACKEND,
    ShantenBackendVerificationError,
    require_execution_environment,
)
from lisjong_arena.environment_identity import (
    EnvironmentIdentityError,
    _installed_identity,
)

RECORDED_BACKENDS = (PYTHON_BACKEND, RUST_BACKEND)
"""Backend names that saved records may carry (readers only, #528 D3)."""
EXECUTION_BACKENDS = (RUST_BACKEND,)
"""Backends that new execution accepts (#531)."""

EXPECTED_LISJONG_REVISION = "d22c4c3e3c00a72b35705bda3220d5598884ffa1"
"""lisjong#357 merge (PR #358) on ``main``; the project pin and the wheel build source."""

EXPECTED_WHEEL_FILENAME = "lisjong_native-0.1.0-cp314-cp314-manylinux_2_28_x86_64.whl"
EXPECTED_WHEEL_SHA256 = (
    "69bf2efff99d6e87cf57d78c25f5e74c0a004a317cc774fdada43b70296f440f"
)
"""``lisjong-native-wheel-<revision>`` artifact of lisjong CI run 40184471486
(push to ``main``)."""

EXPECTED_NATIVE_API_VERSION = 3
"""``_lisjong_native.API_VERSION`` of the lisjong#233 extension; a wheel built
before lisjong#224 has no such attribute (lisjong treats it as 1)."""

NATIVE_MODULE = "_lisjong_native"

# 13 closed tiles; any hand works, the check only needs one public call.
_PROBE_HAND = "123m456p789s1122z"


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


def require_execution_backend(name: object) -> str:
    """Accept only ``rust`` for new execution; refuse ``python`` with guidance."""
    if name == RUST_BACKEND:
        return RUST_BACKEND
    if name == PYTHON_BACKEND:
        raise ShantenBackendVerificationError(RETIRED_PYTHON_BACKEND_MESSAGE)
    raise ShantenBackendVerificationError(
        f"shanten backend must be {RUST_BACKEND!r} for new execution, got {name!r}"
    )


def execution_backend_argument(value: str) -> str:
    """``argparse`` type for a new-execution ``--backend`` option."""
    try:
        return require_execution_backend(value)
    except ShantenBackendVerificationError as error:
        raise argparse.ArgumentTypeError(str(error)) from None


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
    expected: str = RUST_BACKEND, *, expected_revision: str = EXPECTED_LISJONG_REVISION
) -> dict[str, object]:
    """Verify this process's shanten backend and return its execution record.

    Call it in the parent before any game and once in every worker process
    (the workers inherit ``LISJONG_SHANTEN_BACKEND`` from the parent
    environment under ``spawn``).  Only ``rust`` is accepted (#531); the
    variable may be unset or ``rust``.  Both are checked before lisjong is
    imported.
    """
    backend = require_execution_backend(expected)
    require_execution_environment()
    revision = _installed_lisjong_revision(expected_revision)

    try:
        from lisjong.hand_evaluation import calculate_shanten
    except Exception as error:
        # lisjong raises ShantenBackendError at import time when the extension
        # is missing or its API_VERSION differs, and ShantenTableError when
        # the table artifact is broken.
        raise ShantenBackendVerificationError(
            f"lisjong could not load the {backend} shanten backend: {error}"
        ) from error

    record: dict[str, object] = {
        "backend": backend,
        "lisjong_revision": revision,
        "pid": os.getpid(),
    }
    native = sys.modules.get(NATIVE_MODULE)
    if native is None:
        # lisjong imports the extension at import time; reaching this point
        # means the native core was not wired in.
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


def probe_r5_native() -> int:
    """Run one tiny R5 evaluation through lisjong's production factory.

    Returns the R5 native call count delta (at least 1).  Call it after
    ``require_shanten_backend``.  The input tests wiring, not strength or
    latency.
    """
    from lisjong.policies.terminal_shanten_progression_mechanism_riichi_defense import (
        _new_progression_evaluator,
    )

    native = sys.modules.get(NATIVE_MODULE)
    if native is None:
        raise ShantenBackendVerificationError(
            f"{NATIVE_MODULE} is not imported in this process"
        )
    before = native.progression_evaluation_call_count()
    hand = (1,) * 13 + (0,) * 21
    remaining = (0,) * 13 + (3,) + (0,) * 20
    _new_progression_evaluator().evaluate_roots((hand,), remaining, 3)
    calls = native.progression_evaluation_call_count() - before
    if calls < 1:
        raise ShantenBackendVerificationError("R5 factory did not reach native")
    return calls


def native_call_count() -> int | None:
    """Native core call counter of this process, or ``None`` without Rust."""
    native = sys.modules.get(NATIVE_MODULE)
    return None if native is None else native.standard_shanten_call_count()


def native_discard_evaluation_count() -> int | None:
    """Batched discard evaluation counter (lisjong#224), or ``None`` without Rust.

    0004 reference and composite Policies use this structural evaluation.
    Policies without that path can have a zero delta.
    """
    native = sys.modules.get(NATIVE_MODULE)
    return None if native is None else native.discard_evaluation_call_count()


def native_progression_evaluation_count() -> int | None:
    """R5 progression evaluation counter (lisjong#232), or ``None`` without Rust."""
    native = sys.modules.get(NATIVE_MODULE)
    return None if native is None else native.progression_evaluation_call_count()
