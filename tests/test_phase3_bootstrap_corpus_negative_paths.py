"""Focused negative-path coverage for Issue #83 Phase 3 artifact validation."""

import json
import tempfile
import unittest
from pathlib import Path

from _phase3_bootstrap_fixtures import (
    fixed_extractions,
    normalized_default_rules,
    resolved_provenance,
)

from lisjong_arena.phase3_bootstrap_corpus.artifact import (
    SCHEMA_VERSION,
    Phase3BootstrapArtifactError,
    build_phase3_bootstrap_value,
    load_phase3_bootstrap_corpus,
)


def _value():
    provenance = resolved_provenance()
    return build_phase3_bootstrap_value(
        fixed_extractions(provenance),
        provenance,
        normalized_default_rules(),
    )


def _write_raw(path: Path, value: object) -> None:
    path.write_text(
        json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n",
        encoding="utf-8",
        newline="\n",
    )


class Phase3NegativePathContractTests(unittest.TestCase):
    def _assert_value_rejected(self, mutate) -> None:
        value = _value()
        mutate(value)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bad.json"
            _write_raw(path, value)
            with self.assertRaises(Phase3BootstrapArtifactError):
                load_phase3_bootstrap_corpus(path)

    def test_unknown_schema_version_is_rejected(self):
        self._assert_value_rejected(
            lambda value: value.__setitem__("schema_version", SCHEMA_VERSION + 1)
        )

    def test_malformed_json_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "malformed.json"
            path.write_text('{"schema_version":1,\n', encoding="utf-8")
            with self.assertRaises(Phase3BootstrapArtifactError):
                load_phase3_bootstrap_corpus(path)

    def test_invalid_label_count_range_is_rejected(self):
        def mutate(value):
            counts = value["games"][0]["samples"][0]["labels"]["expected_counts"][0][
                "counts"
            ]
            counts[0] = 5

        self._assert_value_rejected(mutate)

    def test_each_semantics_identity_change_is_rejected(self):
        for field in (
            "anchor_semantics_id",
            "evidence_cutoff_semantics_id",
            "label_semantics_id",
        ):
            with self.subTest(field=field):
                self._assert_value_rejected(
                    lambda value, field=field: value["provenance"].__setitem__(
                        field, f"changed-{field}"
                    )
                )

    def test_tampered_evidence_coverage_count_is_rejected(self):
        self._assert_value_rejected(
            lambda value: value["counts"].__setitem__(
                "riichi_evidence_prefix_occurrences",
                value["counts"]["riichi_evidence_prefix_occurrences"] + 1,
            )
        )


if __name__ == "__main__":
    unittest.main()
