from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
GUARD_PATH = ROOT / ".claude" / "hooks" / "workflow_guard.py"
GLOBAL_OPTION_GUARD_PATH = ROOT / ".claude" / "hooks" / "git_global_option_guard.py"
SPEC = importlib.util.spec_from_file_location("claude_workflow_guard", GUARD_PATH)
assert SPEC is not None and SPEC.loader is not None
guard = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(guard)


class DirectMainPushTests(unittest.TestCase):
    def test_allows_feature_branch_push(self) -> None:
        self.assertFalse(
            guard.is_direct_main_push(
                "git push -u origin issue-188-claude-workflow-pilot",
                "issue-188-claude-workflow-pilot",
            )
        )

    def test_blocks_default_push_from_main(self) -> None:
        self.assertTrue(guard.is_direct_main_push("git push", "main"))

    def test_blocks_explicit_main_refspecs(self) -> None:
        commands = [
            "git push origin main",
            "git push origin HEAD:main",
            "git push origin HEAD:refs/heads/main",
            "git push --repo origin main",
            "git push --repo=origin HEAD:main",
            "git push origin :main",
            "git push origin --delete main",
            "git push --all origin",
            "git push --mirror origin",
            "echo ok && git push origin main",
        ]
        for command in commands:
            with self.subTest(command=command):
                self.assertTrue(
                    guard.is_direct_main_push(
                        command,
                        "issue-188-claude-workflow-pilot",
                    )
                )

    def test_blocks_head_alias_pushes_from_main(self) -> None:
        for command in ("git push origin HEAD", "git push origin @"):
            with self.subTest(command=command):
                self.assertTrue(guard.is_direct_main_push(command, "main"))

    def test_does_not_treat_remote_named_main_as_main_ref(self) -> None:
        self.assertFalse(
            guard.is_direct_main_push(
                "git push main",
                "issue-188-claude-workflow-pilot",
            )
        )

    def test_allows_non_updating_push_modes_from_main(self) -> None:
        for command in ("git push --dry-run origin main", "git push --tags origin"):
            with self.subTest(command=command):
                self.assertFalse(guard.is_direct_main_push(command, "main"))

    def test_ignores_non_push_command(self) -> None:
        self.assertFalse(guard.is_direct_main_push("git status", "main"))


class ForbiddenPathTests(unittest.TestCase):
    def test_rejects_credential_and_model_artifact_classes(self) -> None:
        paths = [
            ".env",
            ".envrc",
            ".pypirc",
            "config/.env.production",
            "keys/id_ed25519",
            "keys/private.key",
            "certs/client.pem",
            "model/checkpoint.ckpt",
            "model/policy.pt",
            "model/policy.pth",
            "model/policy.onnx",
            "model/policy.safetensors",
        ]
        for path in paths:
            with self.subTest(path=path):
                self.assertTrue(guard.is_forbidden_path(path))

    def test_allows_documented_env_templates_and_normal_source(self) -> None:
        paths = [
            ".env.example",
            ".env.sample",
            ".env.template",
            "src/lisjong_arena/evaluation.py",
            "tests/test_evaluation.py",
            "docs/protocol.md",
        ]
        for path in paths:
            with self.subTest(path=path):
                self.assertFalse(guard.is_forbidden_path(path))


class GitInspectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.repo = Path(self.temp_dir.name)
        subprocess.run(["git", "init"], cwd=self.repo, check=True, capture_output=True)
        subprocess.run(
            ["git", "config", "user.email", "test@example.invalid"],
            cwd=self.repo,
            check=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "Test User"],
            cwd=self.repo,
            check=True,
        )
        (self.repo / "tracked.txt").write_text("base\n", encoding="utf-8")
        (self.repo / ".env.example").write_text("TOKEN=\n", encoding="utf-8")
        subprocess.run(["git", "add", "."], cwd=self.repo, check=True)
        subprocess.run(
            ["git", "commit", "-m", "base"],
            cwd=self.repo,
            check=True,
            capture_output=True,
        )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_candidate_commit_paths_include_staged_files(self) -> None:
        secret = self.repo / ".env.production"
        secret.write_text("not-a-real-secret\n", encoding="utf-8")
        subprocess.run(
            ["git", "add", "-f", ".env.production"],
            cwd=self.repo,
            check=True,
        )

        paths = guard._candidate_commit_paths(self.repo, "git commit -m test")

        self.assertIn(".env.production", paths)

    def test_commit_all_also_inspects_tracked_worktree_changes(self) -> None:
        tracked = self.repo / "tracked.txt"
        tracked.write_text("changed\n", encoding="utf-8")

        paths = guard._candidate_commit_paths(self.repo, "git commit -am test")

        self.assertIn("tracked.txt", paths)


class PrePrGateTests(unittest.TestCase):
    def test_pre_pr_gate_uses_lightweight_checks_only(self) -> None:
        with (
            mock.patch.object(guard, "_find_base_ref", return_value="main"),
            mock.patch.object(guard, "_git_output", return_value="abc123\n"),
            mock.patch.object(guard, "_branch_changed_paths", return_value=[]),
            mock.patch.object(guard, "_run_check") as run_check,
        ):
            guard._guard_pre_pr(Path("."))

        commands = [call.args[1] for call in run_check.call_args_list]
        self.assertIn(["git", "diff", "--check", "abc123..HEAD"], commands)
        self.assertIn(
            [sys.executable, "-m", "ruff", "format", "--check", "."],
            commands,
        )
        self.assertIn([sys.executable, "-m", "ruff", "check", "."], commands)
        flattened = " ".join(" ".join(command) for command in commands)
        self.assertNotIn("unittest", flattened)
        self.assertNotIn("pytest", flattened)


class LoopProtectedPathTests(unittest.TestCase):
    def test_protects_evaluation_system_paths(self) -> None:
        paths = [
            "src/lisjong_arena/seed-ledger.json",
            "src/lisjong_arena/seed_registry.py",
            "src/lisjong_arena/measurement_allocation_guard.py",
            "src/lisjong_arena/overall_champion_aabb/protocol.py",
            "src/lisjong_arena/overall_champion_aabb/lock.py",
            "src/lisjong_arena/learned_policy_offline_q/fh_curriculum_lock.py",
            "src/lisjong_arena/learned_policy_offline_q/p1_gate_b_comparator.py",
            "src/lisjong_arena/riichilab_downstream_qualification/classification.py",
            "src/lisjong_arena/learned_policy_stage2/decision_rule.py",
            "src/lisjong_arena/stage_a0_tenpai_execution/gate.py",
            "src/lisjong_arena/stage_a0_tenpai_protocol_lock/materialize.py",
            "src/lisjong_arena/new_loop_package/protocol.py",
            "src\\lisjong_arena\\seed_registry.py",
            ".github/workflows/seed-registry.yml",
            ".github/workflows/ci.yml",
            ".claude/hooks/workflow_guard.py",
            ".claude/settings.json",
        ]
        for path in paths:
            with self.subTest(path=path):
                self.assertTrue(guard.is_loop_protected_path(path))

    def test_leaves_candidate_and_ordinary_paths_unprotected(self) -> None:
        paths = [
            "src/lisjong_arena/overall_champion_aabb/runner.py",
            "src/lisjong_arena/policy_catalog.py",
            "src/lisjong_arena/durable_seed_checkpoint.py",
            "src/lisjong_arena/riichilab/protocol_bridge.py",
            "src/lisjong_arena/pure_offense_benchmark/blocklist.py",
            "tests/test_seed_registry.py",
            "docs/seed-registry.md",
            "scripts/protocol.py",
            ".claude/skills/implement-issue/SKILL.md",
        ]
        for path in paths:
            with self.subTest(path=path):
                self.assertFalse(guard.is_loop_protected_path(path))

    def test_loop_session_is_identified_by_non_empty_environment_value(self) -> None:
        self.assertFalse(guard.is_loop_session({}))
        self.assertFalse(guard.is_loop_session({guard.LOOP_SESSION_ENV: ""}))
        self.assertFalse(guard.is_loop_session({guard.LOOP_SESSION_ENV: "  "}))
        self.assertTrue(guard.is_loop_session({guard.LOOP_SESSION_ENV: "1"}))
        self.assertTrue(guard.is_loop_session({guard.LOOP_SESSION_ENV: "0"}))


class LoopSessionGuardTests(unittest.TestCase):
    PROTOCOL = "src/lisjong_arena/sample/protocol.py"
    LEDGER = "src/lisjong_arena/seed-ledger.json"
    CANDIDATE = "src/lisjong_arena/sample/candidate.py"

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.repo = Path(self.temp_dir.name)
        self._git("init", "-b", "main")
        self._git("config", "user.email", "test@example.invalid")
        self._git("config", "user.name", "Test User")
        for path in (self.PROTOCOL, self.LEDGER, self.CANDIDATE):
            self._write(path, "base\n")
        self._git("add", ".")
        self._git("commit", "-m", "base")

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _git(self, *args: str) -> None:
        subprocess.run(["git", *args], cwd=self.repo, check=True, capture_output=True)

    def _write(self, path: str, text: str) -> None:
        target = self.repo / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")

    def _decision(
        self,
        command: str,
        *,
        loop: bool,
        guard_path: Path = GUARD_PATH,
    ) -> str | None:
        env = {k: v for k, v in os.environ.items() if k != guard.LOOP_SESSION_ENV}
        if loop:
            env[guard.LOOP_SESSION_ENV] = "1"
        completed = subprocess.run(
            [sys.executable, str(guard_path), "commit-artifacts"],
            input=json.dumps(
                {
                    "tool_name": "Bash",
                    "tool_input": {"command": command},
                    "cwd": str(self.repo),
                }
            ),
            text=True,
            capture_output=True,
            check=False,
            env=env,
        )
        self.assertEqual(0, completed.returncode, completed.stderr)
        if not completed.stdout.strip():
            return None
        output = json.loads(completed.stdout)["hookSpecificOutput"]
        self.assertEqual("deny", output["permissionDecision"])
        return output["permissionDecisionReason"]

    def test_loop_session_denies_commit_of_staged_protected_change(self) -> None:
        self._write(self.PROTOCOL, "changed\n")
        self._git("add", self.PROTOCOL)

        reason = self._decision("git commit -m test", loop=True)

        self.assertIsNotNone(reason)
        self.assertIn(self.PROTOCOL, reason)
        self.assertIn(guard.LOOP_SESSION_ENV, reason)

    def test_normal_session_allows_the_same_commit(self) -> None:
        self._write(self.PROTOCOL, "changed\n")
        self._write(self.LEDGER, "changed\n")
        self._git("add", ".")

        self.assertIsNone(self._decision("git commit -m test", loop=False))

    def test_loop_session_allows_commit_without_protected_change(self) -> None:
        self._write(self.CANDIDATE, "changed\n")
        self._git("add", self.CANDIDATE)

        self.assertIsNone(self._decision("git commit -m test", loop=True))

    def test_loop_session_denies_deleted_and_renamed_protected_files(self) -> None:
        self._git("rm", self.LEDGER)
        self._git("mv", self.PROTOCOL, "src/lisjong_arena/sample/moved.py")

        reason = self._decision("git commit -m test", loop=True)

        self.assertIsNotNone(reason)
        self.assertIn(self.LEDGER, reason)
        self.assertIn(self.PROTOCOL, reason)

    def test_loop_session_denies_new_protected_file(self) -> None:
        added = "src/lisjong_arena/another/lock.py"
        self._write(added, "new\n")
        self._git("add", added)

        reason = self._decision("git commit -m test", loop=True)

        self.assertIsNotNone(reason)
        self.assertIn(added, reason)

    def test_loop_session_denies_unstaged_protected_change(self) -> None:
        # A pathspec commit takes the worktree content without staging it.
        self._write(self.LEDGER, "changed\n")
        self._write(self.CANDIDATE, "changed\n")
        self._git("add", self.CANDIDATE)

        reason = self._decision(f"git commit -m test -- {self.LEDGER}", loop=True)

        self.assertIsNotNone(reason)
        self.assertIn(self.LEDGER, reason)

    def test_loop_session_denies_commit_behind_git_global_option(self) -> None:
        self._write(self.PROTOCOL, "changed\n")
        self._git("add", self.PROTOCOL)
        command = "git -c user.name=loop commit -m test"

        denied = self._decision(command, loop=True, guard_path=GLOBAL_OPTION_GUARD_PATH)
        allowed = self._decision(
            command, loop=False, guard_path=GLOBAL_OPTION_GUARD_PATH
        )

        self.assertIsNotNone(denied)
        self.assertIn(self.PROTOCOL, denied)
        self.assertIsNone(allowed)

    def test_loop_session_pre_pr_denies_branch_with_protected_change(self) -> None:
        self._git("switch", "-c", "issue-branch")
        self._write(self.PROTOCOL, "changed\n")
        self._git("commit", "-am", "protected change")

        with mock.patch.object(guard, "_run_check"):
            with mock.patch.dict(os.environ, {guard.LOOP_SESSION_ENV: "1"}):
                with self.assertRaises(guard.GuardError) as raised:
                    guard._guard_pre_pr(self.repo)
            with mock.patch.dict(os.environ):
                os.environ.pop(guard.LOOP_SESSION_ENV, None)
                guard._guard_pre_pr(self.repo)

        self.assertIn(self.PROTOCOL, str(raised.exception))


class HookInputTests(unittest.TestCase):
    def test_malformed_input_denies_protected_operation(self) -> None:
        completed = subprocess.run(
            [sys.executable, str(GUARD_PATH), "direct-main-push"],
            input="{not-json",
            text=True,
            capture_output=True,
            check=False,
        )

        self.assertEqual(0, completed.returncode)
        payload = json.loads(completed.stdout)
        output = payload["hookSpecificOutput"]
        self.assertEqual("PreToolUse", output["hookEventName"])
        self.assertEqual("deny", output["permissionDecision"])


if __name__ == "__main__":
    unittest.main()
