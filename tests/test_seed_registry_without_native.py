"""#537: seed registryはlisjong / Rust native moduleなしで動く。

#531以降、``python -m lisjong_arena.seed_registry`` は親packageがlisjongを
eager importしていたため、native wheelのないrunner（``Arena Seed Registry``
workflow）で全operationが失敗した。ここではsubprocessで ``lisjong`` と
``_lisjong_native`` のimportを遮断し、workflowと同じ手順をCLI経路で通す。
ゲーム側のRust必須（lisjongを読むmoduleのimport失敗）とpython backendの拒否は
変わらないことも確かめる。
"""

import importlib.metadata
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BOOTSTRAP = ROOT / "src" / "lisjong_arena" / "seed-ledger.json"
DOMAIN = "lisjong-engine-project-standard-v1-hanchan-v1"

_BLOCKED = """
import runpy, sys
for name in {blocked!r}:
    sys.modules[name] = None  # importはImportErrorになる
sys.argv = ["seed_registry", *sys.argv[1:]]
code = 0
try:
    runpy.run_module({module!r}, run_name="__main__", alter_sys=True)
except SystemExit as stop:
    code = stop.code or 0
loaded = sorted(n for n in sys.modules if n.split(".")[0] in ("lisjong", "_lisjong_native") and sys.modules[n] is not None)
print("LOADED=" + ",".join(loaded), file=sys.stderr)
sys.exit(code)
"""


def _run(*args, blocked=("lisjong", "_lisjong_native"), env=None):
    script = _BLOCKED.format(
        blocked=tuple(blocked), module="lisjong_arena.seed_registry"
    )
    environment = dict(os.environ)
    environment.pop("LISJONG_SHANTEN_BACKEND", None)
    environment.update(env or {})
    return subprocess.run(
        [sys.executable, "-c", script, *map(str, args)],
        capture_output=True,
        text=True,
        cwd=ROOT,
        env=environment,
    )


def _reserve(ledger, seeds):
    return _run(
        "--ledger",
        ledger,
        "reserve",
        "--owner-issue",
        "lisbun/lisjong-arena#999",
        "--protocol",
        "test-protocol-v1",
        "--seed-domain",
        DOMAIN,
        "--purpose",
        "#537 test",
        "--population",
        "test-population",
        "--split",
        "DEVELOPMENT",
        "--seeds",
        seeds,
        "--arena-revision",
        "e" * 40,
        "--protocol-revision",
        "test-protocol-v1",
        "--provenance-reference",
        "https://github.com/lisbun/lisjong-arena/issues/537",
        "--allocation-timestamp",
        "2026-10-24T00:00:00Z",
    )


class SeedRegistryWithoutNativeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.bootstrap = self.tmp / "bootstrap.json"
        self.live = self.tmp / "live.json"
        shutil.copyfile(BOOTSTRAP, self.bootstrap)
        shutil.copyfile(BOOTSTRAP, self.live)

    def _ok(self, process):
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertIn("LOADED=\n", process.stderr + "\n")
        self.assertNotIn("LOADED=lisjong", process.stderr)
        return process.stdout

    def test_workflow_sequence_runs_without_lisjong(self):
        bootstrap = json.loads(
            self._ok(_run("--ledger", self.bootstrap, "validate-bootstrap"))
        )
        self.assertEqual(
            (bootstrap["mode"], bootstrap["status"]), ("BOOTSTRAP", "PASS")
        )
        self.assertEqual(
            json.loads(self._ok(_run("--ledger", self.live, "validate-ledger")))[
                "mode"
            ],
            "LIVE",
        )
        self._ok(
            _run(
                "--ledger",
                self.live,
                "validate-branch",
                "--base-ledger",
                self.bootstrap,
            )
        )
        self._ok(_run("--ledger", self.live, "list", "--seed-domain", DOMAIN))
        check = json.loads(
            self._ok(
                _run(
                    "--ledger",
                    self.live,
                    "check",
                    "--seed-domain",
                    DOMAIN,
                    "--seeds",
                    "537000..537003",
                )
            )
        )
        self.assertTrue(check["fresh"])
        reserved = json.loads(self._ok(_reserve(self.live, "537000..537003")))
        identity = reserved["allocation_identity"]
        shown = json.loads(self._ok(_run("--ledger", self.live, "show", identity)))
        self.assertEqual(shown["allocation"]["state"], "RESERVED")
        self.assertEqual(shown["allocation"]["owner_issue"], "lisbun/lisjong-arena#999")
        self.assertEqual(shown["binding"]["allocation_identity"], identity)
        self._ok(
            _run(
                "--ledger",
                self.live,
                "validate-branch",
                "--base-ledger",
                self.bootstrap,
            )
        )
        committed = json.loads(
            self._ok(_run("--ledger", self.live, "commit", identity))
        )
        self.assertEqual(committed["state"], "COMMITTED")

    def test_collisions_and_transitions_still_fail_closed(self):
        identity = json.loads(self._ok(_reserve(self.live, "537000..537003")))[
            "allocation_identity"
        ]
        check = _run(
            "--ledger",
            self.live,
            "check",
            "--seed-domain",
            DOMAIN,
            "--seeds",
            "537003..537005",
        )
        self.assertEqual(check.returncode, 2)
        self.assertFalse(json.loads(check.stdout)["fresh"])
        overlap = _reserve(self.live, "537002..537004")
        self.assertEqual(overlap.returncode, 2)
        self.assertIn("STOP / INVALID", overlap.stderr)
        retired = _run("--ledger", self.live, "commit", identity, "--state", "RETIRED")
        self.assertEqual(retired.returncode, 0, retired.stderr)
        revived = _run("--ledger", self.live, "commit", identity)
        self.assertEqual(revived.returncode, 2)
        self.assertIn("STOP / INVALID", revived.stderr)
        # 改変されたledgerは拒否する
        document = json.loads(self.live.read_text(encoding="utf-8"))
        document["allocations"][-1]["owner_issue"] = "lisbun/lisjong-arena#1"
        self.live.write_text(json.dumps(document), encoding="utf-8", newline="\n")
        tampered = _run("--ledger", self.live, "validate-ledger")
        self.assertEqual(tampered.returncode, 2)


def _installed_lisjong_is_the_pin() -> bool:
    """lisjongをimportせずに、installed lisjongがproject pinのrevisionか確かめる。"""
    pin = re.search(
        r"lisjong\.git@([0-9a-f]{40})", (ROOT / "pyproject.toml").read_text("utf-8")
    )
    try:
        direct = importlib.metadata.distribution("lisjong").read_text("direct_url.json")
    except importlib.metadata.PackageNotFoundError:
        return False
    commit = json.loads(direct or "{}").get("vcs_info", {}).get("commit_id")
    return pin is not None and commit == pin.group(1)


class GameSideStillRequiresNativeTest(unittest.TestCase):
    @unittest.skipUnless(
        _installed_lisjong_is_the_pin(),
        "installed lisjong is not the Rust-required project pin",
    )
    def test_game_modules_still_fail_without_the_extension(self):
        script = (
            "import sys; sys.modules['_lisjong_native'] = None\n"
            "import lisjong_arena.heuristic_candidate_engine_aabb.call_value_shadow\n"
        )
        process = subprocess.run(
            [sys.executable, "-c", script], capture_output=True, text=True, cwd=ROOT
        )
        self.assertNotEqual(process.returncode, 0)
        self.assertIn("_lisjong_native", process.stderr)

    def test_package_reexports_still_resolve(self):
        script = (
            "import lisjong_arena\n"
            "from lisjong_arena import run_comparison, ComparisonPlan\n"
            "assert lisjong_arena.PolicySpec.__module__ == 'lisjong_arena.model'\n"
            "assert set(lisjong_arena.__all__) <= set(dir(lisjong_arena))\n"
            "print('ok')\n"
        )
        process = subprocess.run(
            [sys.executable, "-c", script], capture_output=True, text=True, cwd=ROOT
        )
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertEqual(process.stdout.strip(), "ok")

    def test_python_backend_is_still_refused_at_package_import(self):
        process = _run(
            "--ledger",
            BOOTSTRAP,
            "validate-ledger",
            env={"LISJONG_SHANTEN_BACKEND": "python"},
        )
        self.assertNotEqual(process.returncode, 0)
        self.assertIn("ShantenBackendVerificationError", process.stderr)


if __name__ == "__main__":
    unittest.main()
