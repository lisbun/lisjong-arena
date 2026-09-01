import json
import tempfile
import unittest
from dataclasses import dataclass
from pathlib import Path
from unittest import mock

import _mortal_decision_analysis_fixtures as fixtures
from lisjong.policy_contract import (
    AnalysisTrace,
    AnkanAction,
    DaiminkanAction,
    DecisionTrace,
    KakanAction,
    KyuushuKyuuhaiAction,
    PassAction,
    PonAction,
    RonAction,
    Seat,
    TileCategory,
    TsumoAction,
)

from lisjong_arena.mortal_decision_analysis_artifact import (
    DECISIONS_FILENAME,
    MANIFEST_FILENAME,
    MORTAL_DECISION_ANALYSIS_SCHEMA,
    MORTAL_DECISION_DIAGNOSTIC,
    MortalDecisionAnalysisArtifactError,
    _internal_action_to_dict,
    load_mortal_decision_analysis,
    save_mortal_decision_analysis,
)
from lisjong_arena.mortal_decision_comparison import RiichiEnvActionKind

_MODULE = "lisjong_arena.mortal_decision_analysis_artifact"


def _json_keys(document: object) -> set[str]:
    """JSON payload内へ現れる全key名を再帰的に集める。"""
    if isinstance(document, dict):
        keys = set(document)
        for value in document.values():
            keys |= _json_keys(value)
        return keys
    if isinstance(document, list):
        keys: set[str] = set()
        for value in document:
            keys |= _json_keys(value)
        return keys
    return set()


class _ArtifactTestCase(unittest.TestCase):
    def setUp(self) -> None:
        workspace = tempfile.TemporaryDirectory()
        self.addCleanup(workspace.cleanup)
        self.workspace = Path(workspace.name)
        self.config = fixtures.mortal_config(self.workspace)
        self.directory = self.workspace / "artifact"

    def result(self, **kwargs):
        return fixtures.evaluation_result(self.config, **kwargs)

    def save(self, result=None, path: Path | None = None) -> Path:
        target = self.directory if path is None else path
        with fixtures.patched_provenance():
            save_mortal_decision_analysis(
                self.result() if result is None else result, target
            )
        return target

    def manifest_document(self, path: Path | None = None) -> dict:
        target = self.directory if path is None else path
        return json.loads((target / MANIFEST_FILENAME).read_text(encoding="utf-8"))

    def decision_documents(self, path: Path | None = None) -> list[dict]:
        target = self.directory if path is None else path
        text = (target / DECISIONS_FILENAME).read_text(encoding="utf-8")
        return [json.loads(line) for line in text.splitlines()]


class ArtifactExportTest(_ArtifactTestCase):
    def test_exports_every_paired_decision_in_canonical_order(self) -> None:
        result = self.result(seeds=(7, 3))
        self.save(result)

        rows = self.decision_documents()
        self.assertEqual(len(rows), result.summary.total_paired_decisions)
        self.assertEqual(
            [(row["seed"], row["rotation"], row["decision_ordinal"]) for row in rows],
            [
                (seed, rotation, ordinal)
                for seed in (7, 3)
                for rotation in range(4)
                for ordinal in range(2)
            ],
        )
        self.assertEqual(
            [row["mortal_seat"] for row in rows],
            [rotation for _ in (7, 3) for rotation in range(4) for _ in range(2)],
        )

    def test_keeps_agreements_so_the_denominator_survives(self) -> None:
        result = self.result(seeds=(0, 1))
        self.save(result)

        rows = self.decision_documents()
        agreements = [row for row in rows if row["agreement"]]
        disagreements = [row for row in rows if not row["agreement"]]
        self.assertEqual(len(agreements), result.summary.agreements)
        self.assertEqual(len(disagreements), result.summary.disagreements_count)
        self.assertEqual(len(rows), len(agreements) + len(disagreements))

    def test_manifest_aggregate_matches_the_in_memory_summary(self) -> None:
        result = self.result(seeds=(4, 5, 6))
        self.save(result)

        manifest = self.manifest_document()
        summary = result.summary
        self.assertEqual(manifest["schema"], MORTAL_DECISION_ANALYSIS_SCHEMA)
        self.assertEqual(manifest["diagnostic"], MORTAL_DECISION_DIAGNOSTIC)
        self.assertEqual(manifest["game_mode"], "4p-red-single")
        self.assertEqual(manifest["shadow_policy_identity"], "combined")
        self.assertEqual(manifest["seeds"], [4, 5, 6])
        self.assertEqual(manifest["game_count"], 12)
        self.assertEqual(
            manifest["total_paired_decisions"], summary.total_paired_decisions
        )
        self.assertEqual(manifest["agreements"], summary.agreements)
        self.assertEqual(manifest["disagreements"], summary.disagreements_count)
        self.assertEqual(manifest["agreement_rate"], summary.agreement_rate)
        self.assertEqual(
            manifest["action_kind_pairs"],
            [
                {
                    "count": pair.count,
                    "driver_mortal_kind": pair.driver_mortal_kind.value,
                    "shadow_policy_kind": pair.shadow_policy_kind.value,
                }
                for pair in summary.action_kind_pairs
            ],
        )

    def test_manifest_records_mortal_and_execution_provenance(self) -> None:
        self.save()

        manifest = self.manifest_document()
        self.assertEqual(
            manifest["mortal"],
            {
                "image": self.config.image,
                "implementation_revision": self.config.implementation_revision,
                "model_sha256": self.config.model_sha256,
                "response_timeout_seconds": self.config.response_timeout_seconds,
            },
        )
        self.assertEqual(
            manifest["execution"]["lisjong_arena_revision"], fixtures.ARENA_REVISION
        )
        self.assertEqual(
            manifest["execution"]["lisjong_revision"], fixtures.LISJONG_REVISION
        )

    def test_omits_machine_local_paths_and_container_configuration(self) -> None:
        self.save()

        manifest_text = (self.directory / MANIFEST_FILENAME).read_text(encoding="utf-8")
        decisions_text = (self.directory / DECISIONS_FILENAME).read_text(
            encoding="utf-8"
        )
        for text in (manifest_text, decisions_text):
            self.assertNotIn(str(self.config.model_path), text)
            self.assertNotIn("mortal.pth", text)
            self.assertNotIn("docker", text)
        self.assertNotIn("model_path", manifest_text)
        self.assertNotIn("docker_executable", manifest_text)

    def test_projects_normalized_driver_and_shadow_actions_losslessly(self) -> None:
        result = self.result()
        self.save(result)

        artifact = load_mortal_decision_analysis(self.directory)
        self.assertEqual(
            [row.driver_mortal_action for row in artifact.decisions],
            [record.driver_mortal_action for record in result.summary.records],
        )
        self.assertEqual(
            [row.shadow_policy_action for row in artifact.decisions],
            [record.shadow_policy_action for record in result.summary.records],
        )
        chi = artifact.decisions[1].driver_mortal_action
        self.assertEqual(chi.kind, RiichiEnvActionKind.CHI)
        self.assertEqual(len(chi.consume_tiles), 2)
        self.assertIsNone(chi.tsumogiri)
        discard = artifact.decisions[0].driver_mortal_action
        self.assertIs(discard.tsumogiri, False)

    def test_projects_player_safe_policy_input_context(self) -> None:
        self.save(self.result())

        row = self.decision_documents()[0]
        policy_input = row["policy_input"]
        self.assertEqual(policy_input["self_seat"], 0)
        self.assertEqual(
            policy_input["round"],
            {
                "round_wind": "east",
                "hand_number": 1,
                "dealer_seat": 0,
                "honba": 0,
                "riichi_sticks": 1,
                "dora_indicators": [
                    {"category": "manzu", "rank": 5, "is_red": True},
                    {"category": "pinzu", "rank": 9, "is_red": False},
                ],
                "live_wall_tiles_remaining": 70,
            },
        )
        self.assertEqual(len(policy_input["players"]), 4)
        self.assertEqual(
            [player["score"] for player in policy_input["players"]],
            [25000, 26000, 27000, 28000],
        )
        self.assertEqual(policy_input["players"][1]["riichi"], "accepted")
        self.assertEqual(policy_input["players"][1]["discards"][0]["called_by"], 2)
        self.assertEqual(policy_input["players"][2]["melds"][0]["kind"], "pon")
        self.assertEqual(
            policy_input["own_hand"]["drawn_tile"],
            {"category": "souzu", "rank": 5, "is_red": True},
        )
        self.assertEqual(len(policy_input["own_hand"]["concealed_tiles"]), 4)

    def test_never_exports_hidden_or_oracle_information(self) -> None:
        self.save(self.result())

        row = self.decision_documents()[0]
        self.assertEqual(
            set(row["policy_input"]),
            {"self_seat", "round", "players", "own_hand"},
        )
        for player in row["policy_input"]["players"]:
            self.assertEqual(set(player), {"score", "discards", "melds", "riichi"})
        self.assertEqual(
            set(row["policy_input"]["own_hand"]), {"concealed_tiles", "drawn_tile"}
        )
        self.assertEqual(
            _json_keys(row),
            {
                "agreement",
                "decision_ordinal",
                "decision_trace",
                "driver_mortal_action",
                "mortal_seat",
                "policy_input",
                "rotation",
                "seed",
                "shadow_policy_action",
                "shadow_policy_identity",
                "legal_actions",
                "selected_action",
                "kind",
                "actor",
                "target",
                "tile",
                "tiles",
                "called_tile",
                "consumed_tiles",
                "consume_tiles",
                "tsumogiri",
                "category",
                "rank",
                "is_red",
                "self_seat",
                "round",
                "players",
                "own_hand",
                "round_wind",
                "hand_number",
                "dealer_seat",
                "honba",
                "riichi_sticks",
                "dora_indicators",
                "live_wall_tiles_remaining",
                "score",
                "discards",
                "melds",
                "riichi",
                "order",
                "called_by",
                "from_seat",
                "concealed_tiles",
                "drawn_tile",
            },
        )

    def test_projects_decision_trace_legal_and_selected_actions(self) -> None:
        self.save(self.result())

        trace = self.decision_documents()[0]["decision_trace"]
        self.assertEqual(set(trace), {"legal_actions", "selected_action"})
        self.assertEqual(
            [action["kind"] for action in trace["legal_actions"]],
            ["discard", "riichi", "chi", "pass"],
        )
        self.assertEqual(
            trace["selected_action"],
            {
                "kind": "discard",
                "actor": 0,
                "tile": {"category": "manzu", "rank": 1, "is_red": False},
                "tsumogiri": False,
            },
        )
        chi = trace["legal_actions"][2]
        self.assertEqual(chi["target"], 3)
        self.assertEqual(len(chi["consumed_tiles"]), 2)

    def test_analysis_payload_is_out_of_scope_and_not_faked(self) -> None:
        @dataclass(frozen=True, slots=True)
        class _Analysis(AnalysisTrace):
            note: str

        result = self.result()
        record = result.summary.records[0]
        traced = DecisionTrace(
            legal_actions=record.decision_trace.legal_actions,
            selected_action=record.decision_trace.selected_action,
            analysis=_Analysis(note="policy-owned"),
        )
        with mock.patch.object(type(record), "__setattr__", object.__setattr__):
            object.__setattr__(record, "decision_trace", traced)
        self.save(result)

        trace = self.decision_documents()[0]["decision_trace"]
        self.assertEqual(set(trace), {"legal_actions", "selected_action"})
        self.assertNotIn("policy-owned", json.dumps(trace))


class InternalActionProjectionTest(unittest.TestCase):
    def test_projects_every_lisjong_action_variant_explicitly(self) -> None:
        seat = Seat.SEAT_0
        souzu = fixtures.tile(TileCategory.SOUZU, 3)
        variants = (
            PonAction(
                actor=seat,
                target=Seat.SEAT_2,
                called_tile=souzu,
                consumed_tiles=(souzu, souzu),
            ),
            DaiminkanAction(
                actor=seat,
                target=Seat.SEAT_2,
                called_tile=souzu,
                consumed_tiles=(souzu, souzu, souzu),
            ),
            AnkanAction(actor=seat, tiles=(souzu, souzu, souzu, souzu)),
            KakanAction(
                actor=seat,
                added_tile=souzu,
                from_seat=Seat.SEAT_1,
                called_tile=souzu,
            ),
            RonAction(actor=seat, target=Seat.SEAT_1, winning_tile=souzu),
            TsumoAction(actor=seat, winning_tile=souzu),
            KyuushuKyuuhaiAction(actor=seat),
            PassAction(actor=seat),
        )
        kinds = [
            _internal_action_to_dict(action, "action")["kind"] for action in variants
        ]
        self.assertEqual(
            kinds,
            [
                "pon",
                "daiminkan",
                "ankan",
                "kakan",
                "ron",
                "tsumo",
                "kyuushu-kyuuhai",
                "pass",
            ],
        )
        kakan = _internal_action_to_dict(variants[3], "action")
        self.assertEqual(
            set(kakan), {"kind", "actor", "added_tile", "from_seat", "called_tile"}
        )

    def test_unsupported_semantic_value_fails_closed_without_repr_fallback(
        self,
    ) -> None:
        @dataclass(frozen=True, slots=True)
        class _UnknownAction:
            actor: Seat

        with self.assertRaises(MortalDecisionAnalysisArtifactError) as raised:
            _internal_action_to_dict(_UnknownAction(actor=Seat.SEAT_0), "action")

        message = str(raised.exception)
        self.assertIn("not a supported lisjong InternalAction variant", message)
        self.assertNotIn("_UnknownAction(actor=", message)


class ArtifactCompletionTest(_ArtifactTestCase):
    def test_does_not_overwrite_an_existing_artifact_by_default(self) -> None:
        self.save()
        original = (self.directory / MANIFEST_FILENAME).read_bytes()

        with self.assertRaises(FileExistsError):
            self.save(self.result(seeds=(9,)))

        self.assertEqual((self.directory / MANIFEST_FILENAME).read_bytes(), original)
        self.assertEqual(len(self.decision_documents()), 8)

    def test_serialization_failure_leaves_no_complete_artifact(self) -> None:
        with (
            fixtures.patched_provenance(),
            mock.patch(
                f"{_MODULE}._policy_input_to_dict",
                side_effect=MortalDecisionAnalysisArtifactError("unsupported value"),
            ),
            self.assertRaises(MortalDecisionAnalysisArtifactError),
        ):
            save_mortal_decision_analysis(self.result(), self.directory)

        self.assertFalse(self.directory.exists())
        self.assertEqual(
            sorted(item.name for item in self.workspace.iterdir()), ["mortal.pth"]
        )

    def test_write_failure_leaves_no_partial_staging_directory(self) -> None:
        with (
            fixtures.patched_provenance(),
            mock.patch(
                f"{_MODULE}.write_new_artifact_file",
                side_effect=OSError("disk full"),
            ),
            self.assertRaises(OSError),
        ):
            save_mortal_decision_analysis(self.result(), self.directory)

        self.assertFalse(self.directory.exists())
        self.assertEqual(
            sorted(item.name for item in self.workspace.iterdir()), ["mortal.pth"]
        )

    def test_missing_parent_directory_fails_closed(self) -> None:
        target = self.workspace / "missing" / "artifact"
        with (
            fixtures.patched_provenance(),
            self.assertRaises(MortalDecisionAnalysisArtifactError),
        ):
            save_mortal_decision_analysis(self.result(), target)

        self.assertFalse(target.exists())

    def test_unavailable_provenance_fails_before_creating_anything(self) -> None:
        with (
            mock.patch(
                f"{_MODULE}.collect_execution_provenance",
                side_effect=RuntimeError("git unavailable"),
            ),
            self.assertRaises(RuntimeError),
        ):
            save_mortal_decision_analysis(self.result(), self.directory)

        self.assertFalse(self.directory.exists())


class ArtifactReadbackTest(_ArtifactTestCase):
    def test_reads_back_all_decisions_and_supports_inspection_filters(self) -> None:
        result = self.result(seeds=(0, 1))
        self.save(result)

        artifact = load_mortal_decision_analysis(self.directory)
        self.assertEqual(len(artifact.decisions), result.summary.total_paired_decisions)
        self.assertEqual(len(artifact.select()), 16)
        self.assertEqual(len(artifact.select(first=3)), 3)
        self.assertEqual(len(artifact.disagreements()), 8)
        self.assertEqual(len(artifact.disagreements(first=2)), 2)
        self.assertEqual(
            len(artifact.disagreements(driver_kind=RiichiEnvActionKind.CHI)), 8
        )
        self.assertEqual(
            len(artifact.disagreements(driver_kind=RiichiEnvActionKind.DISCARD)), 0
        )
        self.assertEqual(
            len(artifact.disagreements(shadow_kind=RiichiEnvActionKind.PASS)), 8
        )
        self.assertEqual(
            len(
                artifact.select(agreement=True, driver_kind=RiichiEnvActionKind.DISCARD)
            ),
            8,
        )
        self.assertEqual(
            [(row.seed, row.rotation) for row in artifact.disagreements(first=5)],
            [(0, 0), (0, 1), (0, 2), (0, 3), (1, 0)],
        )

    def test_readback_exposes_projected_context_payloads(self) -> None:
        self.save(self.result())

        row = load_mortal_decision_analysis(self.directory).decisions[0]
        self.assertEqual(row.policy_input["self_seat"], 0)
        self.assertEqual(set(row.decision_trace), {"legal_actions", "selected_action"})
        with self.assertRaises(TypeError):
            row.policy_input["self_seat"] = 1

    def test_rejects_unknown_schema_version(self) -> None:
        self.save()
        document = self.manifest_document()
        document["schema"] = "lisjong-arena-mortal-decision-analysis-v2"
        (self.directory / MANIFEST_FILENAME).write_text(
            json.dumps(document), encoding="utf-8"
        )

        with self.assertRaisesRegex(
            MortalDecisionAnalysisArtifactError, "unsupported artifact schema"
        ):
            load_mortal_decision_analysis(self.directory)

    def test_rejects_manifest_and_row_count_inconsistency(self) -> None:
        self.save()
        rows = self.decision_documents()
        (self.directory / DECISIONS_FILENAME).write_text(
            "".join(json.dumps(row) + "\n" for row in rows[:-1]), encoding="utf-8"
        )

        with self.assertRaisesRegex(
            MortalDecisionAnalysisArtifactError, "total_paired_decisions"
        ):
            load_mortal_decision_analysis(self.directory)

    def test_rejects_manifest_aggregate_inconsistency(self) -> None:
        self.save()
        document = self.manifest_document()
        document["agreements"] = document["agreements"] + 1
        document["disagreements"] = document["disagreements"] - 1
        document["agreement_rate"] = (
            document["agreements"] / document["total_paired_decisions"]
        )
        (self.directory / MANIFEST_FILENAME).write_text(
            json.dumps(document), encoding="utf-8"
        )

        with self.assertRaisesRegex(
            MortalDecisionAnalysisArtifactError, "manifest agreements"
        ):
            load_mortal_decision_analysis(self.directory)

    def test_rejects_action_kind_pair_inconsistency(self) -> None:
        self.save()
        document = self.manifest_document()
        document["action_kind_pairs"][0]["driver_mortal_kind"] = "pon"
        (self.directory / MANIFEST_FILENAME).write_text(
            json.dumps(document), encoding="utf-8"
        )

        with self.assertRaisesRegex(
            MortalDecisionAnalysisArtifactError, "action_kind_pairs"
        ):
            load_mortal_decision_analysis(self.directory)

    def test_rejects_rows_outside_the_canonical_order(self) -> None:
        self.save(self.result(seeds=(0, 1)))
        rows = self.decision_documents()
        reordered = [rows[2], rows[3], rows[0], rows[1], *rows[4:]]
        (self.directory / DECISIONS_FILENAME).write_text(
            "".join(json.dumps(row) + "\n" for row in reordered), encoding="utf-8"
        )

        with self.assertRaisesRegex(
            MortalDecisionAnalysisArtifactError, "canonical seed/rotation order"
        ):
            load_mortal_decision_analysis(self.directory)

    def test_rejects_rows_with_unknown_fields(self) -> None:
        self.save()
        rows = self.decision_documents()
        rows[0]["shanten"] = 1
        (self.directory / DECISIONS_FILENAME).write_text(
            "".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8"
        )

        with self.assertRaises(MortalDecisionAnalysisArtifactError):
            load_mortal_decision_analysis(self.directory)

    def test_rejects_row_agreement_contradicting_the_projected_actions(self) -> None:
        self.save()
        rows = self.decision_documents()
        rows[0]["agreement"] = False
        (self.directory / DECISIONS_FILENAME).write_text(
            "".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8"
        )

        with self.assertRaises(MortalDecisionAnalysisArtifactError):
            load_mortal_decision_analysis(self.directory)

    def test_rejects_malformed_jsonl(self) -> None:
        self.save()
        (self.directory / DECISIONS_FILENAME).write_text("not json\n", encoding="utf-8")

        with self.assertRaisesRegex(
            MortalDecisionAnalysisArtifactError, "not valid JSON"
        ):
            load_mortal_decision_analysis(self.directory)

    def test_rejects_a_missing_artifact_directory(self) -> None:
        with self.assertRaises(OSError):
            load_mortal_decision_analysis(self.workspace / "absent")


if __name__ == "__main__":
    unittest.main()
