"""Arena's check of ``LISJONG_SHANTEN_BACKEND`` before lisjong reads it (#531).

lisjong#346 removed the Python shanten backend: ``lisjong.hand_evaluation.shanten``
raises ``ShantenBackendError`` at import time unless the variable is unset or
``rust``.  ``lisjong_arena`` imports lisjong Policies when the package itself is
imported, so ``lisjong_arena/__init__`` calls ``require_execution_environment``
first.  Every Arena entry point therefore refuses ``python`` and unknown values
with Arena's migration message, before lisjong is imported.

This module imports nothing from lisjong or from the rest of Arena.
"""

import os

BACKEND_ENVIRONMENT_VARIABLE = "LISJONG_SHANTEN_BACKEND"
PYTHON_BACKEND = "python"
"""Retired by lisjong#346. Only saved records carry it."""
RUST_BACKEND = "rust"

RETIRED_PYTHON_BACKEND_MESSAGE = (
    "the Python shanten backend was removed in lisjong#346 and Arena runs new "
    "work with the Rust backend only (#531): unset LISJONG_SHANTEN_BACKEND or "
    "set it to 'rust'. A saved Python-backend run is re-executed only with its "
    "recorded Arena / lisjong revision, wheel and environment "
    "(docs/rust-only-consumer-compatibility.md)"
)


class ShantenBackendVerificationError(RuntimeError):
    """The requested shanten backend is not the frozen, working configuration."""


def require_execution_environment() -> str | None:
    """Check ``LISJONG_SHANTEN_BACKEND`` before lisjong reads it.

    Unset and ``rust`` both select the native core and are returned as they
    are.  ``python`` and unknown values are refused here, so a run never
    starts and then stops on lisjong's import-time ``ShantenBackendError``.
    """
    selected = os.environ.get(BACKEND_ENVIRONMENT_VARIABLE)
    if selected is None or selected == RUST_BACKEND:
        return selected
    if selected == PYTHON_BACKEND:
        raise ShantenBackendVerificationError(
            f"{BACKEND_ENVIRONMENT_VARIABLE}={selected!r}: "
            + RETIRED_PYTHON_BACKEND_MESSAGE
        )
    raise ShantenBackendVerificationError(
        f"{BACKEND_ENVIRONMENT_VARIABLE} must be unset or {RUST_BACKEND!r}, "
        f"got {selected!r}"
    )
