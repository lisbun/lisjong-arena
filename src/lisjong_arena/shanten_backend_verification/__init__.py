"""Opt-in Rust shanten backend verification (lisbun/lisjong-arena#400).

The parent Issue lisbun/lisjong#216 owns the decision criteria.  This package
is the Arena side of one concrete path: install the frozen prebuilt
``lisjong-native`` wheel on the AWS worker, select the backend explicitly,
check every worker process, and measure the Python and Rust backends under the
same conditions.  It is not a strength evaluation and it allocates no seeds.

Since #531 (lisjong#346) new execution is Rust only; the #400 Python/Rust
records stay readable (``compare`` / ``report``).

#409 moved the ``EXPECTED_*`` wheel identity to the current lisjong pin; the
frozen #400 run plan and its evidence checks keep the #400 combination
(``plan``).
"""

from .backend import (
    BACKEND_ENVIRONMENT_VARIABLE,
    EXECUTION_BACKENDS,
    EXPECTED_LISJONG_REVISION,
    EXPECTED_NATIVE_API_VERSION,
    EXPECTED_WHEEL_FILENAME,
    EXPECTED_WHEEL_SHA256,
    PYTHON_BACKEND,
    RECORDED_BACKENDS,
    RUST_BACKEND,
    ShantenBackendVerificationError,
    require_execution_backend,
    require_execution_environment,
    require_shanten_backend,
    verify_installed_native,
    verify_wheel_file,
)

__all__ = [
    "BACKEND_ENVIRONMENT_VARIABLE",
    "EXPECTED_LISJONG_REVISION",
    "EXPECTED_NATIVE_API_VERSION",
    "EXPECTED_WHEEL_FILENAME",
    "EXPECTED_WHEEL_SHA256",
    "EXECUTION_BACKENDS",
    "PYTHON_BACKEND",
    "RECORDED_BACKENDS",
    "RUST_BACKEND",
    "ShantenBackendVerificationError",
    "require_execution_backend",
    "require_execution_environment",
    "require_shanten_backend",
    "verify_installed_native",
    "verify_wheel_file",
]
