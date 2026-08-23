import ast
import inspect
import unittest

import lisjong_arena
import lisjong_arena.artifact
import lisjong_arena.comparison
import lisjong_arena.model
import lisjong_arena.riichilab.ranked
import lisjong_arena.single_round_evaluation


def _imported_root_modules(module: object) -> set[str]:
    tree = ast.parse(inspect.getsource(module))
    roots = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            roots.add(node.module.split(".")[0])
    return roots


class PackageTest(unittest.TestCase):
    def test_version_is_exposed(self) -> None:
        self.assertEqual(lisjong_arena.__version__, "0.1.0")

    def test_public_api_is_importable_from_the_package_root(self) -> None:
        self.assertEqual(
            sorted(lisjong_arena.__all__),
            [
                "ARTIFACT_SCHEMA_VERSION",
                "ArtifactPlan",
                "COMPARISON_PROTOCOL",
                "ComparisonArtifact",
                "ComparisonArtifactError",
                "ComparisonExecutionError",
                "ComparisonPlan",
                "ComparisonResult",
                "ExecutionProvenance",
                "PolicyFactoryNotSerializableError",
                "PolicyMetrics",
                "PolicySpec",
                "ROTATION_COUNT",
                "SINGLE_ROUND_GAME_MODE",
                "SINGLE_ROUND_ROTATION_COUNT",
                "SeatResult",
                "SingleRoundCandidateMetrics",
                "SingleRoundEvaluationError",
                "SingleRoundEvaluationPlan",
                "SingleRoundEvaluationResult",
                "SingleRoundGameResult",
                "load_comparison_artifact",
                "run_comparison",
                "run_comparison_parallel",
                "run_single_round_evaluation",
                "run_single_round_evaluation_parallel",
                "save_comparison_artifact",
            ],
        )
        for name in lisjong_arena.__all__:
            with self.subTest(name=name):
                self.assertTrue(hasattr(lisjong_arena, name))

    def test_arena_modules_do_not_import_riichienv(self) -> None:
        """現行physical implementationではArenaはRiichiEnvを直接importしない。

        RiichiEnv integration / Adapterはtarget ownershipとしてArenaへ移管予定だが、
        現時点ではまだlisjongにあり、Arena自身のmoduleから直接importしない。
        """
        for module in (
            lisjong_arena,
            lisjong_arena.artifact,
            lisjong_arena.comparison,
            lisjong_arena.model,
            lisjong_arena.riichilab.ranked,
            lisjong_arena.single_round_evaluation,
        ):
            with self.subTest(module=module.__name__):
                self.assertNotIn("riichienv", _imported_root_modules(module))

    def test_ranked_module_does_not_import_lisjong_legacy_orchestration(self) -> None:
        tree = ast.parse(inspect.getsource(lisjong_arena.riichilab.ranked))
        forbidden = {"RankedGameResult", "run_ranked_game"}

        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom) or node.module is None:
                continue
            if node.module not in {
                "lisjong.riichilab_client",
                "lisjong.riichilab_client.ranked",
            }:
                continue
            imported = {alias.name for alias in node.names}
            self.assertTrue(
                forbidden.isdisjoint(imported),
                f"Arena ranked module imports legacy orchestration: {imported & forbidden}",
            )


if __name__ == "__main__":
    unittest.main()
