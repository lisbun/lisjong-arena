"""Integration tests for the #359 focal outcome source producer.

test専用seedの実RiichiEnv対局1 hanchanだけからsourceを生成し、pinned lisjong
(`PINNED_LISJONG_REVISION`)の``outcome_source.py`` consumerでstrict readする。
改ざんcaseは生成済みsourceのcopyに対して検証する。
"""

import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from _focal_outcome_source_fixtures import binding, source_contract
from lisjong.learning import (
    UnsupportedSourceSchemaError,
    build_outcome_targets,
    read_outcome_source,
)

from lisjong_arena.focal_outcome_source import source
from lisjong_arena.focal_outcome_source.accounting import FocalOutcomeSourceError
from lisjong_arena.offense_foundation.qualification import seal

# test専用seed。focal seat 0で短く終わる（3 kyoku）ことを確認済み。
SMOKE_SEED = 900000


def _read_lines(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def _write_lines(path, rows):
    path.write_text(
        "".join(source._canonical_line(row) for row in rows),
        encoding="utf-8",
        newline="\n",
    )


class _Tampered:
    """sourceのcopyを改ざんし、digest / sealを整合させ直すhelper。"""

    def __init__(self, original: Path, root: Path) -> None:
        self.path = root / "tampered"
        shutil.copytree(original, self.path)

    def manifest(self):
        return json.loads((self.path / "manifest.json").read_text(encoding="utf-8"))

    def write_manifest(self, body, *, reseal=True):
        body = dict(body)
        if reseal:
            body.pop("identity", None)
            body = seal(body)
        (self.path / "manifest.json").write_text(
            json.dumps(body, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
            encoding="utf-8",
            newline="\n",
        )

    def rewrite_game(self, ordinal, *, kyokus=None, decisions=None, summary=None):
        """payloadを書き換え、files digest / game summary / manifestをreseal。"""
        game_path = self.path / f"game-{ordinal:03d}"
        if kyokus is not None:
            _write_lines(game_path / source.KYOKU_PAYLOAD_FILENAME, kyokus)
        if decisions is not None:
            _write_lines(game_path / source.DECISION_PAYLOAD_FILENAME, decisions)
        manifest = self.manifest()
        game = {
            key: value
            for key, value in manifest["games"][ordinal].items()
            if key != "identity"
        }
        game["files"] = {
            name: source._file_info(game_path / name) for name in game["files"]
        }
        if summary is not None:
            game.update(summary)
        manifest["games"][ordinal] = seal(game)
        self.write_manifest(manifest)

    def kyokus(self, ordinal=0):
        return _read_lines(
            self.path / f"game-{ordinal:03d}" / source.KYOKU_PAYLOAD_FILENAME
        )

    def decisions(self, ordinal=0):
        return _read_lines(
            self.path / f"game-{ordinal:03d}" / source.DECISION_PAYLOAD_FILENAME
        )


class FocalOutcomeSourceIntegrationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.root = Path(cls._tmp.name)
        cls.path = cls.root / "source"
        cls.executions = []
        real_run = source.run_focal_game

        def run_and_keep(**kwargs):
            execution = real_run(**kwargs)
            cls.executions.append(execution)
            return execution

        with patch.object(source, "run_focal_game", side_effect=run_and_keep):
            cls.source = source.generate_focal_outcome_source(
                cls.path,
                population_role=source.CALIBRATION_ROLE,
                games=[(SMOKE_SEED, "CALIBRATION")],
                allocation_bindings={"CALIBRATION": binding([SMOKE_SEED])},
                source_contract=source_contract(),
            )

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def tamper(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        return _Tampered(self.path, Path(directory.name))

    def assert_rejected(self, tampered, pattern=None):
        with self.assertRaises(FocalOutcomeSourceError) as caught:
            source.verify_focal_outcome_source(tampered.path)
        if pattern is not None:
            self.assertRegex(str(caught.exception), pattern)

    def test_pinned_lisjong_consumer_strict_reads_the_smoke_source(self):
        consumed = read_outcome_source(self.path)
        self.assertEqual(consumed.identity, self.source.identity)
        (game,) = consumed.games
        self.assertEqual((game.game_ordinal, game.seed), (0, SMOKE_SEED))
        self.assertEqual(int(game.focal_seat), 0)
        self.assertEqual(consumed.behavior, source.BEHAVIOR)
        self.assertTrue(game.kyokus[-1].is_final_kyoku)
        targets = build_outcome_targets(consumed)
        self.assertEqual(targets.source_identity, consumed.identity)
        self.assertTrue(targets.rows)

    def test_focal_ordinal_matches_the_runner_policy_call_boundary(self):
        (execution,) = self.executions
        focal = [
            observation
            for step in execution.inspection.step_observations
            for observation in step.seat_decisions
            if observation.seat == execution.focal_seat
        ]
        (game,) = self.source.games
        # adapterが呼ばれた回数 == runnerがfocal seatへsurfaceしたDecisionContext数。
        self.assertEqual(len(execution.captures), len(focal))
        self.assertEqual(len(game.decisions), len(focal))
        for decision, observation in zip(game.decisions, focal, strict=True):
            self.assertEqual(decision.decision.input, observation.policy_input)
        # 探索したのはfocal seatだけで、他seatのdecisionは記録しない。
        non_focal = sum(
            1
            for step in execution.inspection.step_observations
            for observation in step.seat_decisions
            if observation.seat != execution.focal_seat
        )
        self.assertGreater(non_focal, 0)

    def test_non_final_kyoku_boundary_matches_the_next_start(self):
        (game,) = self.source.games
        for previous, current in zip(game.kyokus, game.kyokus[1:]):
            self.assertEqual(current.points_before_kyoku, previous.points_after_kyoku)
            self.assertEqual(current.riichi_sticks_before, previous.riichi_sticks_after)

    def test_generation_refuses_to_overwrite(self):
        with self.assertRaises(FileExistsError):
            source.generate_focal_outcome_source(
                self.path,
                population_role=source.CALIBRATION_ROLE,
                games=[(SMOKE_SEED, "CALIBRATION")],
                allocation_bindings={"CALIBRATION": binding([SMOKE_SEED])},
                source_contract=source_contract(),
            )

    def test_manifest_digest_tamper_fails_closed(self):
        tampered = self.tamper()
        manifest = tampered.manifest()
        manifest["population_role"] = source.SCIENTIFIC_ROLE
        tampered.write_manifest(manifest, reseal=False)
        self.assert_rejected(tampered, "identity")

    def test_manifest_missing_or_extra_field_fails_closed(self):
        for change in ("missing", "extra"):
            with self.subTest(change):
                tampered = self.tamper()
                manifest = tampered.manifest()
                if change == "missing":
                    del manifest["behavior"]
                else:
                    manifest["hidden_truth"] = {}
                tampered.write_manifest(manifest)
                self.assert_rejected(tampered)

    def test_payload_digest_tamper_fails_closed(self):
        tampered = self.tamper()
        kyokus = tampered.kyokus()
        kyokus[0]["honba"] += 1
        _write_lines(tampered.path / "game-000" / source.KYOKU_PAYLOAD_FILENAME, kyokus)
        self.assert_rejected(tampered, "digest")

    def test_game_ordinal_gap_duplicate_and_split_restart_fail_closed(self):
        tampered = self.tamper()
        tampered.rewrite_game(0, summary={"game_ordinal": 1})
        self.assert_rejected(tampered, "game_ordinal")

        # SCIENTIFICのTRAIN -> SELECT境界でordinalが0へrestartするsource。
        tampered = self.tamper()
        shutil.copytree(tampered.path / "game-000", tampered.path / "game-001")
        manifest = tampered.manifest()
        first = {k: v for k, v in manifest["games"][0].items() if k != "identity"}
        second = first | {"seed": SMOKE_SEED + 1, "split": "SELECT"}
        manifest["population_role"] = source.SCIENTIFIC_ROLE
        manifest["games"] = [seal(first | {"split": "TRAIN"}), seal(second)]
        manifest["allocation_bindings"] = {
            "TRAIN": binding([SMOKE_SEED]),
            "SELECT": binding([SMOKE_SEED + 1]),
        }
        tampered.write_manifest(manifest)
        self.assert_rejected(tampered, "game_ordinal")

    def test_row_field_missing_or_extra_fails_closed(self):
        tampered = self.tamper()
        kyokus = tampered.kyokus()
        kyokus[0]["privileged"] = True
        tampered.rewrite_game(0, kyokus=kyokus)
        self.assert_rejected(tampered)

        tampered = self.tamper()
        decisions = tampered.decisions()
        del decisions[0]["exploration_token"]
        tampered.rewrite_game(0, decisions=decisions)
        self.assert_rejected(tampered)

    def test_focal_decision_ordinal_gap_fails_closed(self):
        tampered = self.tamper()
        decisions = tampered.decisions()
        del decisions[1]
        tampered.rewrite_game(
            0, decisions=decisions, summary={"decision_count": len(decisions)}
        )
        self.assert_rejected(tampered, "focal_decision_ordinal")

    def test_arena_recomputes_the_exploration_token(self):
        tampered = self.tamper()
        decisions = tampered.decisions()
        # guard decisionはtokenを使わないため、lisjong consumerは値を検出しない。
        guard = next(
            index
            for index, row in enumerate(decisions)
            if row["producer_survivor_actions"] is None
        )
        decisions[guard]["exploration_token"] = "0" * 64
        tampered.rewrite_game(0, decisions=decisions)
        read_outcome_source(tampered.path)
        self.assert_rejected(tampered, "exploration token")

    def test_arena_audits_the_final_adjustment_boundary(self):
        tampered = self.tamper()
        kyokus = tampered.kyokus()
        final = kyokus[-1]
        # 保存則を保ったままseat間で1000点を移す。lisjong consumerは受理する。
        final["points_after_kyoku"][0] += 1000
        final["points_after_kyoku"][1] -= 1000
        tampered.rewrite_game(0, kyokus=kyokus)
        read_outcome_source(tampered.path)
        self.assert_rejected(tampered, "final")

    def test_arena_audits_kyoku_conservation(self):
        tampered = self.tamper()
        kyokus = tampered.kyokus()
        final = kyokus[-1]
        final["points_after_kyoku"][0] += 1000
        (game,) = tampered.manifest()["games"]
        scores = list(game["hanchan_final_scores"])
        scores[0] += 1000
        tampered.rewrite_game(
            0, kyokus=kyokus, summary={"hanchan_final_scores": scores}
        )
        self.assert_rejected(tampered, "conservation")

    def test_consumer_rejection_is_reported_as_an_arena_error(self):
        tampered = self.tamper()
        manifest = tampered.manifest()
        manifest["schema"] = "arena-offense-l0.3-focal-outcome-source-v2"
        tampered.write_manifest(manifest)
        with self.assertRaises(UnsupportedSourceSchemaError):
            read_outcome_source(tampered.path)
        self.assert_rejected(tampered, "invalid focal outcome source")


if __name__ == "__main__":
    unittest.main()
