"""Opt-in Rust shanten backend verification (lisbun/lisjong-arena#400).

The parent Issue lisbun/lisjong#216 owns the decision criteria.  This package
is the Arena side of one concrete path: install the frozen prebuilt
``lisjong-native`` wheel on the AWS worker, select the backend explicitly,
check every worker process, and measure the Python and Rust backends under the
same conditions.  It is not a strength evaluation, it allocates no seeds, and
it never changes the default (Python) backend.

#409 moved the ``EXPECTED_*`` wheel identity to the current lisjong pin; the
frozen #400 run plan and its evidence checks keep the #400 combination
(``plan``).
"""

from .backend import (
    BACKEND_ENVIRONMENT_VARIABLE,
    EXPECTED_LISJONG_REVISION,
    EXPECTED_NATIVE_API_VERSION,
    EXPECTED_WHEEL_FILENAME,
    EXPECTED_WHEEL_SHA256,
    PYTHON_BACKEND,
    RUST_BACKEND,
    ShantenBackendVerificationError,
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
    "PYTHON_BACKEND",
    "RUST_BACKEND",
    "ShantenBackendVerificationError",
    "require_shanten_backend",
    "verify_installed_native",
    "verify_wheel_file",
]
