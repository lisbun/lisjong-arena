import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_WORKFLOW = _ROOT / ".github" / "workflows" / "seed-registry.yml"


class SeedRegistryWorkflowTest(unittest.TestCase):
    def test_live_authority_is_serialized_and_never_force_pushed(self):
        text = _WORKFLOW.read_text(encoding="utf-8")
        self.assertIn("workflow_dispatch:", text)
        self.assertIn("contents: write", text)
        self.assertIn("group: arena-seed-registry-authority", text)
        self.assertIn("cancel-in-progress: false", text)
        self.assertIn("REGISTRY_BRANCH: seed-registry", text)
        self.assertIn(
            'git push origin "HEAD:refs/heads/${REGISTRY_BRANCH}"',
            text,
        )
        self.assertNotIn("--force", text)

    def test_reservation_requires_reviewed_main_revision_and_current_authority(self):
        text = _WORKFLOW.read_text(encoding="utf-8")
        self.assertIn(
            'git merge-base --is-ancestor "${ARENA_REVISION}" origin/main',
            text,
        )
        self.assertIn(
            'git fetch --no-tags origin "+refs/heads/${REGISTRY_BRANCH}:refs/remotes/origin/${REGISTRY_BRANCH}"',
            text,
        )
        self.assertIn(
            'python -m lisjong_arena.seed_registry --ledger "${live_ledger}" validate-ledger',
            text,
        )
        self.assertIn("validate-bootstrap", text)

    def test_workflow_exposes_read_and_mutating_operations(self):
        text = _WORKFLOW.read_text(encoding="utf-8")
        for operation in (
            "validate",
            "list",
            "show",
            "check",
            "reserve",
            "commit",
            "retire",
        ):
            with self.subTest(operation=operation):
                self.assertIn(f"          - {operation}\n", text)


if __name__ == "__main__":
    unittest.main()
