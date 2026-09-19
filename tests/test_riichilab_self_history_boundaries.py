"""Issue #269 keeps the #170 and #168/#232 contracts unchanged."""

from __future__ import annotations

import inspect
import unittest

from lisjong_arena import riichilab_corpus, riichilab_self_history
from lisjong_arena.riichilab_corpus import models as corpus_models
from lisjong_arena.riichilab_corpus.validation import (
    validate_mjai_gzip,
    validate_mjai_log,
)
from lisjong_arena.riichilab_self_history import models as self_models
from tests._riichilab_corpus_fixtures import synthetic_mjai as corpus_mjai


class Issue170ContractTest(unittest.TestCase):
    def test_fixed_target_bots_and_hard_ceiling_are_unchanged(self) -> None:
        self.assertEqual(
            corpus_models.TARGET_BOTS,
            ((126, "FuuroMaster"), (120, "Mortal-v4b"), (294, "zero-test2")),
        )
        self.assertEqual(corpus_models.MAX_ACQUISITION_CEILING, 250)

    def test_corpus_public_surface_is_unchanged(self) -> None:
        self.assertEqual(
            sorted(riichilab_corpus.__all__),
            sorted(
                [
                    "AcquisitionFailed",
                    "AcquisitionPlan",
                    "CorpusError",
                    "MAX_ACQUISITION_CEILING",
                    "Participation",
                    "RecentGamesSnapshot",
                    "TARGET_BOTS",
                    "acquire_from_plan",
                    "create_acquisition_plan",
                    "snapshot_recent_games",
                    "validate_cached_corpus",
                ]
            ),
        )

    def test_validate_mjai_gzip_keeps_its_participation_signature(self) -> None:
        signature = inspect.signature(validate_mjai_gzip)
        self.assertEqual(list(signature.parameters), ["payload", "participations"])
        self.assertIs(
            signature.parameters["participations"].kind,
            inspect.Parameter.KEYWORD_ONLY,
        )

    def test_the_neutral_primitive_and_the_170_wrapper_agree(self) -> None:
        payload = corpus_mjai()
        participation = corpus_models.Participation(
            game_id="g1", bot_id=126, seat=2, played_at="2026-09-18T10:30:59"
        )
        self.assertEqual(
            validate_mjai_gzip(payload, participations=(participation,)),
            validate_mjai_log(payload, seats=(2,)),
        )

    def test_seat_join_rejection_is_preserved_by_the_neutral_primitive(self) -> None:
        payload = corpus_mjai(game_names=["a", "b", "c", "d"])
        with self.assertRaisesRegex(corpus_models.CorpusError, "seat cannot join"):
            validate_mjai_log(payload, seats=(4,))


class SelfHistoryIsolationTest(unittest.TestCase):
    def test_self_history_does_not_reuse_the_170_contract_models(self) -> None:
        exported = set(riichilab_self_history.__all__)
        for name in (
            "TARGET_BOTS",
            "RecentGamesSnapshot",
            "build_snapshot",
            "MAX_ACQUISITION_CEILING",
            "Participation",
        ):
            self.assertNotIn(name, exported)
            self.assertFalse(
                hasattr(self_models, name), f"{name} leaked into the #269 model"
            )

    def test_self_history_schema_ids_are_distinct_from_the_corpus_schema(self) -> None:
        corpus_ids = {
            corpus_models.SCHEMA_ID,
            corpus_models.SNAPSHOT_SCHEMA_ID,
            corpus_models.PLAN_SCHEMA_ID,
            corpus_models.REPORT_SCHEMA_ID,
        }
        self_ids = {
            self_models.HISTORY_SCHEMA_ID,
            self_models.PAGE_SCHEMA_ID,
            self_models.REPORT_SCHEMA_ID,
        }
        self.assertEqual(corpus_ids & self_ids, set())

    def test_self_history_does_not_import_the_durable_ranked_record_module(
        self,
    ) -> None:
        import pkgutil

        for module in pkgutil.iter_modules(riichilab_self_history.__path__):
            source = inspect.getsource(
                __import__(
                    f"lisjong_arena.riichilab_self_history.{module.name}",
                    fromlist=["_"],
                )
            )
            self.assertNotIn("durable_ranked_game_record", source)
            self.assertNotIn("riichilab_corpus.acquisition", source)


if __name__ == "__main__":
    unittest.main()
