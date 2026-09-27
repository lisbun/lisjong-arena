"""Issue #411: secret-safe classification and redaction of transport reasons."""

from __future__ import annotations

import unittest

from lisjong_arena.riichilab.transport_diagnostics import (
    MAX_EXCERPT_LENGTH,
    RawTransportFailure,
    classify_reason,
    is_confirmed_secret_free,
    redact_excerpt,
    sanitize_transport_failure,
)

_TOKEN = "rl_live_4f9c2a7e1b3d5f6a"


class ClassifyReasonTest(unittest.TestCase):
    def test_classes(self) -> None:
        cases = {
            None: "none",
            "   ": "none",
            "Bot is already connected": "same_bot_already_active",
            "concurrent connection rejected": "same_bot_already_active",
            "bot is already in the queue": "same_bot_already_active",
            # Bare "already" / "in game" are too broad for a same-bot label.
            "request already processed": "other",
            "error in game server": "other",
            "Token verification failed: Bot is inactive": "token_or_bot_rejected",
            "keepalive ping timeout": "keepalive_timeout",
            "server shutting down": "other",
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(expected, classify_reason(text))


class RedactExcerptTest(unittest.TestCase):
    def test_plain_reason_is_kept(self) -> None:
        self.assertEqual(
            "Token verification failed: Bot is inactive",
            redact_excerpt("Token verification failed: Bot is inactive", (_TOKEN,)),
        )

    def test_exact_token_is_redacted(self) -> None:
        excerpt = redact_excerpt(f"rejected {_TOKEN} again", (_TOKEN,))
        self.assertEqual("rejected [redacted] again", excerpt)
        self.assertNotIn(_TOKEN, excerpt)

    def test_authorization_and_bearer_material_is_redacted(self) -> None:
        for text in (
            "bad Authorization: Bearer abc.def",
            "bearer xyz rejected",
            "AUTHORIZATION=abc",
        ):
            with self.subTest(text=text):
                excerpt = redact_excerpt(text, (_TOKEN,))
                self.assertIsNotNone(excerpt)
                self.assertNotIn("authorization", excerpt.lower())
                self.assertNotIn("bearer", excerpt.lower())

    def test_long_token_like_runs_are_redacted(self) -> None:
        excerpt = redact_excerpt("session a1b2c3d4e5f6a7b8c9d0e1f2a3 closed", ())
        self.assertEqual("session [redacted] closed", excerpt)

    def test_a_token_fragment_drops_the_excerpt(self) -> None:
        # A fragment that is neither the exact token nor a long run survives
        # redaction; the explicit secret check must then drop the excerpt.
        fragment = _TOKEN[3:17]
        self.assertIsNone(redact_excerpt(f"id {fragment} rejected", (_TOKEN,)))

    def test_excerpt_is_bounded_and_allow_listed(self) -> None:
        excerpt = redact_excerpt("x " * 200 + '<script>{"a":1}', ())
        self.assertLessEqual(len(excerpt), MAX_EXCERPT_LENGTH)
        excerpt = redact_excerpt('{"error":"a<b>"}\n\tend', ())
        self.assertEqual("??error?:?a?b??? end", excerpt)

    def test_empty_text_has_no_excerpt(self) -> None:
        self.assertIsNone(redact_excerpt(None, ()))
        self.assertIsNone(redact_excerpt("  ", ()))

    def test_secret_free_check(self) -> None:
        self.assertTrue(is_confirmed_secret_free("bot already active", (_TOKEN,)))
        self.assertFalse(is_confirmed_secret_free(f"x{_TOKEN}x", (_TOKEN,)))
        self.assertFalse(is_confirmed_secret_free("Bearer", ()))


class SanitizeTest(unittest.TestCase):
    def test_classification_is_canonical_and_excerpt_is_redacted(self) -> None:
        diagnostics = sanitize_transport_failure(
            RawTransportFailure(
                phase="before_start_game",
                operation="recv",
                close_code_received=4000,
                server_text=f"Bot already in game ({_TOKEN})",
                local_close_reason="keepalive ping timeout",
                requests_received=0,
            ),
            (_TOKEN,),
        )
        self.assertEqual("same_bot_already_active", diagnostics.server_reason_class)
        self.assertEqual(
            "Bot already in game ([redacted])", diagnostics.server_reason_excerpt
        )
        self.assertEqual("keepalive_timeout", diagnostics.local_close_reason_class)
        self.assertEqual(4000, diagnostics.close_code_received)
        self.assertNotIn(_TOKEN, repr(diagnostics))


if __name__ == "__main__":
    unittest.main()
