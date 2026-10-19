"""Issue #507のL2a動作確認CLI（Linux / Rust backend専用）。

```text
LISJONG_SHANTEN_BACKEND=rust python -m lisjong_arena.l2a_runtime_smoke \\
    --discard-artifact <m1> --reaction-artifact <r1> \\
    --discard-identity <m1 identity> --reaction-identity <r1 identity> \\
    --wheel <lisjong_native wheel> --output <new directory>
```

対局の前に、Rust backend・installed lisjong pin・native wheelとimport済みfileの
一致、Arena checkoutがcleanであること、L2a runtimeのidentityを確認する。
16半荘がすべてPASSなら0、FAILなら1、開始前の確認で止まれば2を返す。
"""

from __future__ import annotations

import argparse
import importlib.metadata
import json
import platform
import subprocess
import sys
import sysconfig
import time
from pathlib import Path

from .smoke import (
    OUTCOME_PASS,
    L2aRuntimeSmokeError,
    load_l2a_runtime,
    run_smoke,
)

_REPOSITORY = Path(__file__).resolve().parents[3]


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Issue #507 L2a runtime smoke")
    parser.add_argument("--discard-artifact", required=True)
    parser.add_argument("--reaction-artifact", required=True)
    parser.add_argument("--discard-identity", required=True)
    parser.add_argument("--reaction-identity", required=True)
    parser.add_argument("--wheel", required=True)
    parser.add_argument("--output", required=True)
    return parser


def _git(*arguments: str) -> str:
    return subprocess.check_output(
        ["git", "-C", str(_REPOSITORY), *arguments], text=True
    )


def _arena_revision() -> str:
    revision = _git("rev-parse", "HEAD").strip()
    if _git("status", "--porcelain"):
        raise L2aRuntimeSmokeError("the smoke requires a clean Arena checkout")
    return revision


def _environment(wheel: str) -> dict[str, object]:
    from lisjong_arena.shanten_backend_verification.backend import (
        RUST_BACKEND,
        require_shanten_backend,
        verify_installed_native,
    )

    if bool(sysconfig.get_config_var("Py_GIL_DISABLED")):
        raise L2aRuntimeSmokeError("free-threaded Python is not supported")
    backend = require_shanten_backend(RUST_BACKEND)
    native = verify_installed_native(wheel)
    import torch

    return {
        "arena_revision": _arena_revision(),
        "lisjong_revision": backend["lisjong_revision"],
        "shanten_backend": backend["backend"],
        "native": {
            "wheel_file": native["file"],
            "wheel_sha256": native["sha256"],
            "source_revision": backend["native"]["source_revision"],
            "api_version": backend["native"]["api_version"],
        },
        "riichienv_version": importlib.metadata.version("riichienv"),
        "torch_version": torch.__version__,
        "torch_num_threads": torch.get_num_threads(),
        "python_version": platform.python_version(),
        "platform": platform.platform(),
        "machine": platform.machine(),
    }


def _emit(value: object) -> None:
    print(json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True))


def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        environment = _environment(arguments.wheel)
        started = time.perf_counter()
        runtime = load_l2a_runtime(
            arguments.discard_artifact,
            arguments.reaction_artifact,
            expected_identity=(arguments.discard_identity, arguments.reaction_identity),
        )
        load_seconds = time.perf_counter() - started
    except Exception as error:
        _emit({"outcome": "BLOCKED", "reason": f"{type(error).__name__}: {error}"})
        return 2
    report = run_smoke(
        runtime,
        output=arguments.output,
        environment=environment,
        load_seconds=load_seconds,
    )
    _emit(
        {
            "outcome": report["outcome"],
            "totals": report["totals"],
            "memory": report["memory"],
            "failure": None
            if report["failure"] is None
            else {
                key: report["failure"][key]
                for key in ("ordinal", "seed", "error_type", "message")
            },
        }
    )
    return 0 if report["outcome"] == OUTCOME_PASS else 1


if __name__ == "__main__":
    sys.exit(main())
