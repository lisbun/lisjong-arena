"""Shared artifact plumbingの責務中立なprimitive tests。"""

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from lisjong_arena._artifact_io import sha256_bytes, staged_artifact_directory


class Sha256BytesTest(unittest.TestCase):
    def test_known_digest_is_deterministic_lowercase_hex(self) -> None:
        expected = "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
        self.assertEqual(sha256_bytes(b"abc"), expected)
        self.assertEqual(sha256_bytes(b"abc"), expected)


class StagedArtifactDirectoryTest(unittest.TestCase):
    def test_success_publishes_exact_staged_content(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            destination = root / "artifact"

            with staged_artifact_directory(destination) as staging:
                self.assertEqual(staging.parent, destination.parent)
                self.assertFalse(destination.exists())
                (staging / "payload.bin").write_bytes(b"payload")

            self.assertEqual((destination / "payload.bin").read_bytes(), b"payload")
            self.assertFalse(
                any(
                    path.name.startswith(".artifact-staging-")
                    for path in root.iterdir()
                )
            )

    def test_failure_cleans_staging_without_partial_publish(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            destination = root / "artifact"

            with self.assertRaisesRegex(RuntimeError, "validation failed"):
                with staged_artifact_directory(destination) as staging:
                    (staging / "payload.bin").write_bytes(b"partial")
                    raise RuntimeError("validation failed")

            self.assertFalse(destination.exists())
            self.assertFalse(
                any(
                    path.name.startswith(".artifact-staging-")
                    for path in root.iterdir()
                )
            )


if __name__ == "__main__":
    unittest.main()
