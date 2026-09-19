"""CLI for lisbun/lisjong-arena#259 Stage A0 protocol lock.

Operator order:

1. lock-a        (normal current environment; no VALIDATION target inspection)
2. materialize   (#258 historical source-semantic environment)
3. lock-b        (locked downstream/current environment)
4. verify

No command in this module trains a model or runs A/T interactive games.
"""

from __future__ import annotations

import argparse
import json

from .artifact import (
    build_lock_a,
    build_lock_b,
    load_lock_a,
    load_lock_b,
    save_lock,
)
from .materialize import materialize_retained_scientific_data


def _lock_a(arguments: argparse.Namespace) -> int:
    document = build_lock_a(
        dataset_path=arguments.dataset,
        feasibility_report_path=arguments.feasibility_report,
        qualified_sidecar_path=arguments.qualified_sidecar,
    )
    save_lock(document, arguments.output)
    readback = load_lock_a(arguments.output)
    print(
        json.dumps(
            {
                "lock": "A",
                "identity": readback["lock_identity"],
                "retained_dataset_identity": readback["dataset"]["identity"],
                "protected_test_payload_read": False,
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


def _materialize(arguments: argparse.Namespace) -> int:
    lock_a = load_lock_a(arguments.lock_a)
    sidecar, public, summary = materialize_retained_scientific_data(
        arguments.dataset,
        sidecar_destination=arguments.sidecar,
        public_keys_destination=arguments.public_keys,
    )
    if summary["dataset_identity"] != lock_a["dataset"]["identity"]:
        raise RuntimeError("materialized dataset identity differs from Lock A")
    print(
        json.dumps(
            {
                "lock_a_identity": lock_a["lock_identity"],
                "scientific_sidecar_identity": sidecar.identity,
                "public_keys_identity": public.identity,
                "scientific_seed_count": len(summary["scientific_seeds"]),
                "row_count": summary["row_count"],
                "cell_count": summary["cell_count"],
                "protected_test_unread": summary["protected_test_unread"],
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


def _lock_b(arguments: argparse.Namespace) -> int:
    document = build_lock_b(
        lock_a_path=arguments.lock_a,
        scientific_sidecar_path=arguments.scientific_sidecar,
        public_keys_path=arguments.public_keys,
    )
    save_lock(document, arguments.output)
    readback = load_lock_b(arguments.output)
    print(
        json.dumps(
            {
                "lock": "B",
                "identity": readback["lock_identity"],
                "hard_outcome": readback["hard_outcome"],
                "downstream_status": readback["downstream_status"],
                "eligible_train_cells": readback["baseline_parameters"][
                    "eligible_cell_count"
                ],
                "baseline_fingerprint": readback["baseline_parameters"]["fingerprint"],
                "downstream_blocks": readback["precision_preflight"][
                    "projected_block_count"
                ],
                "downstream_total_games": readback["contract"]["downstream"][
                    "total_games"
                ],
                "protected_test_payload_read": False,
                "validation_target_summary_exposed": False,
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


def _verify(arguments: argparse.Namespace) -> int:
    document = load_lock_b(arguments.lock_b)
    print(
        json.dumps(
            {
                "identity": document["lock_identity"],
                "hard_outcome": document["hard_outcome"],
                "contract_fingerprint": document["contract_fingerprint"],
                "protected_test_payload_read": document["exposure"][
                    "protected_test_payload_read"
                ],
                "validation_target_summary_exposed": document["exposure"][
                    "validation_target_summary_exposed"
                ],
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    lock_a = commands.add_parser("lock-a")
    lock_a.add_argument("--dataset", required=True)
    lock_a.add_argument("--feasibility-report", required=True)
    lock_a.add_argument("--qualified-sidecar", required=True)
    lock_a.add_argument("--output", required=True)
    lock_a.set_defaults(handler=_lock_a)

    materialize = commands.add_parser("materialize")
    materialize.add_argument("--lock-a", required=True)
    materialize.add_argument("--dataset", required=True)
    materialize.add_argument("--sidecar", required=True)
    materialize.add_argument("--public-keys", required=True)
    materialize.set_defaults(handler=_materialize)

    lock_b = commands.add_parser("lock-b")
    lock_b.add_argument("--lock-a", required=True)
    lock_b.add_argument("--scientific-sidecar", required=True)
    lock_b.add_argument("--public-keys", required=True)
    lock_b.add_argument("--output", required=True)
    lock_b.set_defaults(handler=_lock_b)

    verify = commands.add_parser("verify")
    verify.add_argument("--lock-b", required=True)
    verify.set_defaults(handler=_verify)
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    return int(arguments.handler(arguments))


if __name__ == "__main__":
    raise SystemExit(main())
