"""Issue #336: RiichiLab Secrets Manager `SecretString` raw-token contractの
instance-side preflight validationを固定する。

`extract_and_validate_secret_string()`は`aws secretsmanager get-secret-value
--output json`応答全文を受け取る実際のretrieval seamであり、fixtureは
`json.dumps()`でその応答形を再現する。secret値はすべてtest専用の合成値で
あり、本物のtokenは使用しない。
"""

from __future__ import annotations

import contextlib
import io
import json
import unittest
from unittest.mock import patch

from lisjong_arena.riichilab.secret_contract import (
    SECRET_FORMAT_BLOCKER_MESSAGE,
    SecretFormatError,
    _run_cli,
    extract_and_validate_secret_string,
    validate_secret_string,
)

_SYNTHETIC_RAW_TOKEN = "synthetic-riichilab-bot-token-abc123"


def _get_secret_value_response(secret_string: object) -> str:
    """`aws secretsmanager get-secret-value --output json`応答fixtureを作る。"""
    return json.dumps(
        {
            "ARN": "arn:aws:secretsmanager:ap-northeast-1:000000000000:secret:test",
            "Name": "lisjong/riichilab/lisjong-dev-token",
            "VersionId": "00000000-0000-0000-0000-000000000000",
            "SecretString": secret_string,
            "VersionStages": ["AWSCURRENT"],
        }
    )


class ValidateSecretStringTest(unittest.TestCase):
    def test_raw_opaque_token_passes_unchanged(self) -> None:
        self.assertEqual(
            validate_secret_string(_SYNTHETIC_RAW_TOKEN), _SYNTHETIC_RAW_TOKEN
        )

    def test_missing_value_is_rejected(self) -> None:
        with self.assertRaises(SecretFormatError):
            validate_secret_string(None)

    def test_empty_string_is_rejected(self) -> None:
        with self.assertRaises(SecretFormatError):
            validate_secret_string("")

    def test_trailing_lf_is_rejected(self) -> None:
        with self.assertRaises(SecretFormatError):
            validate_secret_string(_SYNTHETIC_RAW_TOKEN + "\n")

    def test_trailing_crlf_is_rejected(self) -> None:
        with self.assertRaises(SecretFormatError):
            validate_secret_string(_SYNTHETIC_RAW_TOKEN + "\r\n")

    def test_embedded_newline_is_rejected(self) -> None:
        with self.assertRaises(SecretFormatError):
            validate_secret_string("first-half\nsecond-half")

    def test_json_object_is_rejected(self) -> None:
        structured = json.dumps({"LISJONG_DEV_BOT_TOKEN": _SYNTHETIC_RAW_TOKEN})
        with self.assertRaises(SecretFormatError):
            validate_secret_string(structured)

    def test_json_array_is_rejected(self) -> None:
        structured = json.dumps([_SYNTHETIC_RAW_TOKEN])
        with self.assertRaises(SecretFormatError):
            validate_secret_string(structured)

    def test_invalid_value_is_not_auto_extracted(self) -> None:
        structured = json.dumps({"LISJONG_DEV_BOT_TOKEN": _SYNTHETIC_RAW_TOKEN})
        try:
            validate_secret_string(structured)
        except SecretFormatError as error:
            self.assertNotIn(_SYNTHETIC_RAW_TOKEN, str(error))
        else:
            self.fail("structured secret must be rejected, not extracted")

    def test_rejection_message_never_contains_the_supplied_value(self) -> None:
        for invalid in (
            "",
            _SYNTHETIC_RAW_TOKEN + "\n",
            json.dumps({"LISJONG_DEV_BOT_TOKEN": _SYNTHETIC_RAW_TOKEN}),
            json.dumps([_SYNTHETIC_RAW_TOKEN]),
        ):
            with self.subTest(invalid=invalid):
                try:
                    validate_secret_string(invalid)
                except SecretFormatError as error:
                    self.assertEqual(str(error), SECRET_FORMAT_BLOCKER_MESSAGE)
                    self.assertNotIn(_SYNTHETIC_RAW_TOKEN, str(error))
                else:
                    self.fail("expected SecretFormatError")


class ExtractAndValidateSecretStringTest(unittest.TestCase):
    """実際の`get-secret-value --output json`応答全文を通すend-to-end fixture。"""

    def test_raw_opaque_token_through_the_actual_response_shape_passes(self) -> None:
        response = _get_secret_value_response(_SYNTHETIC_RAW_TOKEN)
        self.assertEqual(
            extract_and_validate_secret_string(response), _SYNTHETIC_RAW_TOKEN
        )

    def test_trailing_lf_through_the_actual_response_shape_is_rejected(self) -> None:
        response = _get_secret_value_response(_SYNTHETIC_RAW_TOKEN + "\n")
        with self.assertRaises(SecretFormatError):
            extract_and_validate_secret_string(response)

    def test_trailing_crlf_through_the_actual_response_shape_is_rejected(
        self,
    ) -> None:
        response = _get_secret_value_response(_SYNTHETIC_RAW_TOKEN + "\r\n")
        with self.assertRaises(SecretFormatError):
            extract_and_validate_secret_string(response)

    def test_json_object_through_the_actual_response_shape_is_rejected(self) -> None:
        structured = json.dumps({"LISJONG_DEV_BOT_TOKEN": _SYNTHETIC_RAW_TOKEN})
        response = _get_secret_value_response(structured)
        with self.assertRaises(SecretFormatError):
            extract_and_validate_secret_string(response)

    def test_json_array_through_the_actual_response_shape_is_rejected(self) -> None:
        structured = json.dumps([_SYNTHETIC_RAW_TOKEN])
        response = _get_secret_value_response(structured)
        with self.assertRaises(SecretFormatError):
            extract_and_validate_secret_string(response)

    def test_missing_secret_string_field_is_rejected(self) -> None:
        response = json.dumps({"ARN": "arn:aws:secretsmanager:...:secret:test"})
        with self.assertRaises(SecretFormatError):
            extract_and_validate_secret_string(response)

    def test_null_secret_string_is_rejected(self) -> None:
        response = _get_secret_value_response(None)
        with self.assertRaises(SecretFormatError):
            extract_and_validate_secret_string(response)

    def test_malformed_response_envelope_is_rejected(self) -> None:
        with self.assertRaises(SecretFormatError):
            extract_and_validate_secret_string("not json at all")

    def test_shell_trailing_newline_normalization_does_not_hide_a_stored_lf(
        self,
    ) -> None:
        """command substitutionがtrailing raw newlineを除去しても、
        `SecretString`内部のLFはJSON escapeとして保持され続けることを
        固定する回帰test。
        """
        response = _get_secret_value_response(_SYNTHETIC_RAW_TOKEN + "\n")
        shell_normalized_response = response.rstrip("\n")
        self.assertEqual(response, shell_normalized_response)
        with self.assertRaises(SecretFormatError):
            extract_and_validate_secret_string(shell_normalized_response)


class SecretContractCliTest(unittest.TestCase):
    """bootstrapが`$(printf ... | python -m ...secret_contract)`で使う
    stdin -> stdout/stderr seamを固定する。
    """

    def test_valid_secret_is_written_to_stdout_with_no_extra_newline(self) -> None:
        response = _get_secret_value_response(_SYNTHETIC_RAW_TOKEN)
        stdout = io.StringIO()
        with (
            patch("sys.stdin", io.StringIO(response)),
            contextlib.redirect_stdout(stdout),
        ):
            return_code = _run_cli([])
        self.assertEqual(return_code, 0)
        self.assertEqual(stdout.getvalue(), _SYNTHETIC_RAW_TOKEN)

    def test_structured_secret_is_rejected_non_zero_and_secret_safe(self) -> None:
        structured = json.dumps({"LISJONG_DEV_BOT_TOKEN": _SYNTHETIC_RAW_TOKEN})
        response = _get_secret_value_response(structured)
        stdout = io.StringIO()
        stderr = io.StringIO()
        with (
            patch("sys.stdin", io.StringIO(response)),
            contextlib.redirect_stdout(stdout),
            contextlib.redirect_stderr(stderr),
        ):
            return_code = _run_cli([])
        self.assertEqual(return_code, 1)
        self.assertEqual(stdout.getvalue(), "")
        self.assertNotIn(_SYNTHETIC_RAW_TOKEN, stderr.getvalue())
        self.assertIn("RIICHILAB SECRET FORMAT BLOCKER", stderr.getvalue())

    def test_empty_secret_is_rejected_non_zero_and_secret_safe(self) -> None:
        response = _get_secret_value_response("")
        stdout = io.StringIO()
        stderr = io.StringIO()
        with (
            patch("sys.stdin", io.StringIO(response)),
            contextlib.redirect_stdout(stdout),
            contextlib.redirect_stderr(stderr),
        ):
            return_code = _run_cli([])
        self.assertEqual(return_code, 1)
        self.assertEqual(stdout.getvalue(), "")


if __name__ == "__main__":
    unittest.main()
