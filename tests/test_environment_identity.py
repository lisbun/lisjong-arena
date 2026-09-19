import json
import tempfile
import unittest
from importlib import metadata
from pathlib import Path
from unittest.mock import Mock, patch

from lisjong_arena.environment_identity import verify_environment

A = "a" * 40
B = "b" * 40
C = "c" * 40


class FakeDistribution:
    def __init__(
        self,
        name,
        revision,
        *,
        url=None,
        version="0.1.0",
        requires=(),
        direct=True,
    ):
        self.name = name
        self.version = version
        self.requires = list(requires)
        self._direct = direct
        self._url = url or f"https://github.com/lisbun/{name}.git"
        self._revision = revision

    def read_text(self, filename):
        if filename != "direct_url.json" or not self._direct:
            return None
        return json.dumps(
            {
                "url": self._url,
                "vcs_info": {
                    "vcs": "git",
                    "commit_id": self._revision,
                },
            }
        )


def project_text(*requirements):
    dependencies = ",\n".join(f'    "{requirement}"' for requirement in requirements)
    return (
        "[project]\n"
        'name = "consumer"\n'
        'version = "0.1.0"\n'
        "dependencies = [\n"
        f"{dependencies}\n"
        "]\n"
    )


class EnvironmentIdentityTest(unittest.TestCase):
    def run_check(
        self,
        content,
        distributions,
        pip_returncode=0,
        pip_output="No broken requirements found.",
    ):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory, "pyproject.toml")
            path.write_text(content, encoding="utf-8")

            def lookup(name):
                try:
                    return distributions[name]
                except KeyError as exc:
                    raise metadata.PackageNotFoundError(name) from exc

            completed = Mock(
                returncode=pip_returncode,
                stdout=pip_output,
                stderr="",
            )
            with (
                patch(
                    "lisjong_arena.environment_identity.metadata.distribution",
                    side_effect=lookup,
                ),
                patch(
                    "lisjong_arena.environment_identity.subprocess.run",
                    return_value=completed,
                ),
            ):
                return verify_environment(path)

    def test_exact_commit_passes(self):
        result = self.run_check(
            project_text(f"lisjong @ git+https://github.com/lisbun/lisjong.git@{A}"),
            {"lisjong": FakeDistribution("lisjong", A)},
        )
        self.assertTrue(result.ok)
        self.assertEqual(result.identities[0].revision, A)

    def test_same_version_stale_commit_fails(self):
        result = self.run_check(
            project_text(f"lisjong @ git+https://github.com/lisbun/lisjong.git@{B}"),
            {
                "lisjong": FakeDistribution(
                    "lisjong",
                    A,
                    version="0.1.0",
                )
            },
        )
        self.assertFalse(result.ok)
        self.assertTrue(
            any("stale internal dependency" in item for item in result.errors)
        )

    def test_missing_direct_url_fails(self):
        result = self.run_check(
            project_text(f"lisjong @ git+https://github.com/lisbun/lisjong.git@{A}"),
            {
                "lisjong": FakeDistribution(
                    "lisjong",
                    A,
                    direct=False,
                )
            },
        )
        self.assertFalse(result.ok)
        self.assertTrue(
            any("direct_url.json is missing" in item for item in result.errors)
        )

    def test_repository_mismatch_fails(self):
        result = self.run_check(
            project_text(f"lisjong @ git+https://github.com/lisbun/lisjong.git@{A}"),
            {
                "lisjong": FakeDistribution(
                    "lisjong",
                    A,
                    url=("https://github.com/lisbun/lisjong-fork.git"),
                )
            },
        )
        self.assertFalse(result.ok)
        self.assertTrue(any("repository mismatch" in item for item in result.errors))

    def test_transitive_pin_disagreement_fails(self):
        result = self.run_check(
            project_text(
                f"lisjong @ git+https://github.com/lisbun/lisjong.git@{B}",
                f"lisjong-arena @ git+https://github.com/lisbun/lisjong-arena.git@{C}",
            ),
            {
                "lisjong": FakeDistribution("lisjong", B),
                "lisjong-arena": FakeDistribution(
                    "lisjong-arena",
                    C,
                    requires=(
                        f"lisjong @ git+https://github.com/lisbun/lisjong.git@{A}",
                    ),
                ),
            },
        )
        self.assertFalse(result.ok)
        self.assertTrue(
            any("incompatible internal pins" in item for item in result.errors)
        )

    def test_pip_check_failure_is_not_ignored(self):
        result = self.run_check(
            project_text(f"lisjong @ git+https://github.com/lisbun/lisjong.git@{A}"),
            {"lisjong": FakeDistribution("lisjong", A)},
            pip_returncode=1,
            pip_output=("demo 0.1 has requirement other==1, but you have other 2."),
        )
        self.assertFalse(result.ok)
        self.assertTrue(any("pip check failed" in item for item in result.errors))


if __name__ == "__main__":
    unittest.main()
