"""curated alias / explicit import reference共通resolverのunit test。"""

import contextlib
import io
import unittest
from unittest import mock

from lisjong.policies import MinimalPolicy, ShantenPolicy

from lisjong_arena.policy_catalog import POLICY_CATALOG
from lisjong_arena.policy_reference import (
    PolicyReferenceError,
    resolve_policy_reference,
)
from lisjong_arena.single_round_compare import _run_cli

_SHANTEN_REFERENCE = "lisjong.policies:ShantenPolicy"
_MINIMAL_REFERENCE = "lisjong.policies:MinimalPolicy"


class PolicyReferenceResolutionTest(unittest.TestCase):
    def test_existing_catalog_alias_resolves_to_existing_spec(self) -> None:
        self.assertIs(resolve_policy_reference("combined"), POLICY_CATALOG["combined"])

    def test_explicit_class_resolves_to_policy_spec_with_caller_identity(self) -> None:
        spec = resolve_policy_reference(
            _SHANTEN_REFERENCE, explicit_identity="experiment A"
        )

        self.assertEqual(spec.identity, "experiment A")
        self.assertIs(spec.factory, ShantenPolicy)
        self.assertIsInstance(spec.factory(), ShantenPolicy)

    def test_missing_identity_does_not_derive_one_from_class_name(self) -> None:
        with self.assertRaisesRegex(PolicyReferenceError, "explicit identity"):
            resolve_policy_reference(_SHANTEN_REFERENCE)

    def test_empty_or_blank_explicit_identity_is_rejected(self) -> None:
        for identity in ("", "   "):
            with self.subTest(identity=identity):
                with self.assertRaisesRegex(PolicyReferenceError, "non-empty"):
                    resolve_policy_reference(
                        _SHANTEN_REFERENCE, explicit_identity=identity
                    )

    def test_invalid_reference_syntax_is_rejected(self) -> None:
        for reference in (
            "lisjong.policies:",
            ":ShantenPolicy",
            "lisjong.policies:ShantenPolicy:extra",
            "other_package.policies:ShantenPolicy",
        ):
            with self.subTest(reference=reference):
                with self.assertRaisesRegex(PolicyReferenceError, "invalid explicit"):
                    resolve_policy_reference(reference, explicit_identity="experiment")

    def test_nonexistent_module_is_rejected(self) -> None:
        with self.assertRaisesRegex(PolicyReferenceError, "could not import"):
            resolve_policy_reference(
                "lisjong.not_a_real_module:Policy",
                explicit_identity="experiment",
            )

    def test_nonexistent_attribute_is_rejected(self) -> None:
        with self.assertRaisesRegex(PolicyReferenceError, "has no attribute"):
            resolve_policy_reference(
                "lisjong.policies:NotARealPolicy",
                explicit_identity="experiment",
            )

    def test_non_callable_attribute_is_rejected(self) -> None:
        with self.assertRaisesRegex(PolicyReferenceError, "must be callable"):
            resolve_policy_reference(
                "lisjong.policies:__doc__", explicit_identity="experiment"
            )

    def test_explicit_identity_cannot_collide_with_curated_or_mortal_identity(
        self,
    ) -> None:
        for identity in ("combined", "mortal"):
            with self.subTest(identity=identity):
                with self.assertRaisesRegex(PolicyReferenceError, "reserved"):
                    resolve_policy_reference(
                        _SHANTEN_REFERENCE, explicit_identity=identity
                    )

    def test_catalog_alias_rejects_explicit_identity_override(self) -> None:
        with self.assertRaisesRegex(PolicyReferenceError, "may only be used"):
            resolve_policy_reference("combined", explicit_identity="experiment")

    def test_unknown_alias_does_not_fallback_to_import_or_default(self) -> None:
        with self.assertRaisesRegex(PolicyReferenceError, "unknown policy alias"):
            resolve_policy_reference("unknown-policy")


class SingleRoundCliResolutionTest(unittest.TestCase):
    def _run_serial(self, argv: list[str]):
        with (
            mock.patch(
                "lisjong_arena.single_round_compare.run_single_round_evaluation",
                return_value=object(),
            ) as serial,
            mock.patch(
                "lisjong_arena.single_round_compare.format_summary",
                return_value="ok",
            ),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            return_code = _run_cli(argv)

        self.assertEqual(return_code, 0)
        return serial.call_args.args[0]

    def test_explicit_candidate_and_catalog_baseline_use_serial_path(self) -> None:
        plan = self._run_serial(
            [
                "--candidate",
                _SHANTEN_REFERENCE,
                "--candidate-id",
                "shanten-experiment",
                "--baseline",
                "two-step",
                "--seeds",
                "0",
            ]
        )

        self.assertEqual(plan.candidate.identity, "shanten-experiment")
        self.assertIs(plan.candidate.factory, ShantenPolicy)
        self.assertIs(plan.baseline, POLICY_CATALOG["two-step"])

    def test_catalog_candidate_and_explicit_baseline_use_serial_path(self) -> None:
        plan = self._run_serial(
            [
                "--candidate",
                "two-step",
                "--baseline",
                _MINIMAL_REFERENCE,
                "--baseline-id",
                "minimal-experiment",
                "--seeds",
                "0",
            ]
        )

        self.assertIs(plan.candidate, POLICY_CATALOG["two-step"])
        self.assertEqual(plan.baseline.identity, "minimal-experiment")
        self.assertIs(plan.baseline.factory, MinimalPolicy)

    def test_explicit_candidate_and_baseline_identity_collision_is_rejected(
        self,
    ) -> None:
        with (
            mock.patch(
                "lisjong_arena.single_round_compare.run_single_round_evaluation"
            ) as serial,
            contextlib.redirect_stderr(io.StringIO()) as stderr,
        ):
            return_code = _run_cli(
                [
                    "--candidate",
                    _SHANTEN_REFERENCE,
                    "--candidate-id",
                    "same-experiment",
                    "--baseline",
                    _MINIMAL_REFERENCE,
                    "--baseline-id",
                    "same-experiment",
                    "--seeds",
                    "0",
                ]
            )

        self.assertEqual(return_code, 2)
        self.assertIn("distinct identities", stderr.getvalue())
        serial.assert_not_called()


if __name__ == "__main__":
    unittest.main()
