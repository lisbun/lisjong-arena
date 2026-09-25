"""Issue #375 Heuristic candidate AABB half-game protocol v1 tests.

実RiichiEnvの半荘も実formal evaluationも実行しない。comparison結果は
syntheticに合成し、merged-main execution disciplineの読み取りとexecution境界
だけを差し替える。
"""

from __future__ import annotations

import copy
import io
import json
import unittest
from collections.abc import Callable, Iterator
from contextlib import contextmanager, redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from lisjong.policy_contract import Seat

import lisjong_arena.artifact as artifact_module
import lisjong_arena.heuristic_candidate_aabb.lock as lock_module
from lisjong_arena._artifact_io import canonical_json_text
from lisjong_arena.artifact import ExecutionProvenance
from lisjong_arena.comparison import _seat_assignment, aggregate_policy_metrics
from lisjong_arena.heuristic_candidate_aabb.__main__ import main as cli_main
from lisjong_arena.heuristic_candidate_aabb.experiment import (
    run_candidate_evaluation,
)
from lisjong_arena.heuristic_candidate_aabb.lock import (
    HeuristicCandidateLockError,
    build_lock_document,
    load_lock_document,
    locked_participants,
    locked_seeds,
    save_lock_document,
)
from lisjong_arena.heuristic_candidate_aabb.protocol import (
    ALLOCATION_POPULATION,
    ALLOCATION_SPLIT,
    CANDIDATE_SLOT,
    CANDIDATE_SUPERIOR_LABEL,
    GAME_MODE,
    INCONCLUSIVE_LABEL,
    INCUMBENT_SUPERIOR_LABEL,
    MAX_STEPS,
    OKA,
    OWNER_ISSUE,
    PROTOCOL_ID,
    ROTATION_COUNT,
    ROTATION_PLAN,
    SEAT_COUNT,
    SEED_BLOCK_COUNT,
    SEED_DOMAIN,
    UMA,
    HeuristicCandidateProtocolError,
    final_score_units,
    require_participants,
    require_population,
)
from lisjong_arena.heuristic_candidate_aabb.result import (
    HeuristicCandidateResultError,
    verify_candidate_bundle,
)
from lisjong_arena.model import ComparisonPlan, ComparisonResult, PolicySpec, SeatResult
from lisjong_arena.overall_champion_aabb.protocol import ParticipantBinding
from lisjong_arena.seed_registry import (
    allocation_binding,
    new_ledger,
    reserve_allocation,
    transition_allocation,
)
from lisjong_arena.single_round_artifact import SingleRoundExecutionProvenance

CANDIDATE_IDENTITY = "placement-aware-speed-call-fixture"
INCUMBENT_IDENTITY = "heuristic-champion-fixture"
ARENA_REVISION = "a" * 40
LISJONG_REVISION = "b" * 40
SEEDS = tuple(range(375_000, 375_000 + SEED_BLOCK_COUNT))
SCORE_BY_RANK = {1: 40_000, 2: 30_000, 3: 20_000, 4: 10_000}
CANDIDATE_ADVANTAGE = ((1, 2), (3, 4))
INCUMBENT_ADVANTAGE = ((3, 4), (1, 2))


class StubPolicy:
    def choose_action(self, decision: object) -> object:
        raise AssertionError("fixtures must not execute policies")


def stub_candidate_policy() -> StubPolicy:
    return StubPolicy()


def stub_incumbent_policy() -> StubPolicy:
    return StubPolicy()


def binding(role: str, **overrides: object) -> ParticipantBinding:
    values: dict[str, object] = {
        "family": "heuristic",
        "policy_identity": (
            CANDIDATE_IDENTITY if role == "candidate" else INCUMBENT_IDENTITY
        ),
        "factory_binding": f"test_heuristic_candidate_aabb:stub_{role}_policy",
        "implementation_source": "lisjong",
        "implementation_revision": LISJONG_REVISION,
    }
    values.update(overrides)
    return ParticipantBinding(**values)  # type: ignore[arg-type]


def lock_provenance() -> SingleRoundExecutionProvenance:
    return SingleRoundExecutionProvenance(
        execution_environment="riichienv",
        lisjong_arena_version="0.1.0",
        lisjong_arena_revision=ARENA_REVISION,
        lisjong_version="0.1.0",
        lisjong_revision=LISJONG_REVISION,
        lisjong_engine_version="0.1.0",
        lisjong_engine_revision="c" * 40,
        riichienv_version="0.4.10",
        python_version="3.14.6",
    )


def comparison_provenance() -> ExecutionProvenance:
    return ExecutionProvenance(
        execution_environment="riichienv",
        lisjong_arena_version="0.1.0",
        lisjong_version="0.1.0",
        lisjong_revision=LISJONG_REVISION,
        riichienv_version="0.4.10",
        python_version="3.14.6",
    )


def ledger_with_allocation(
    *,
    seeds: tuple[int, ...] = SEEDS,
    owner_issue: str = OWNER_ISSUE,
    protocol: str = PROTOCOL_ID,
    retire: bool = False,
) -> tuple[dict[str, object], dict[str, object]]:
    ledger, record = reserve_allocation(
        new_ledger(),
        owner_issue=owner_issue,
        protocol=protocol,
        seed_domain=SEED_DOMAIN,
        purpose="#375 fixture",
        population=ALLOCATION_POPULATION,
        split=ALLOCATION_SPLIT,
        seeds=seeds,
        arena_revision=ARENA_REVISION,
        protocol_revision=PROTOCOL_ID,
        provenance_reference="fixture",
        allocation_timestamp="2026-09-25T00:00:00Z",
    )
    identity = str(record["allocation_identity"])
    bound = allocation_binding(ledger, identity)
    if retire:
        ledger = transition_allocation(ledger, identity, state="RETIRED")
    return ledger, bound


@contextmanager
def merged_main_execution(
    provenance: SingleRoundExecutionProvenance | None = None,
) -> Iterator[None]:
    live = provenance or lock_provenance()
    with (
        mock.patch.object(lock_module, "_require_environment_consistent"),
        mock.patch.object(
            lock_module, "collect_execution_provenance", return_value=live
        ),
        mock.patch.object(
            lock_module,
            "require_clean_arena_head",
            return_value=live.lisjong_arena_revision,
        ),
        mock.patch.object(
            lock_module,
            "require_merged_arena_revision",
            return_value=live.lisjong_arena_revision,
        ),
    ):
        yield


def seat_rows(
    profile_of: Callable[[int], tuple[tuple[int, int], tuple[int, int]]],
) -> tuple[SeatResult, ...]:
    rows: list[SeatResult] = []
    for seed in SEEDS:
        candidate_ranks, incumbent_ranks = profile_of(seed)
        for rotation in range(ROTATION_COUNT):
            remaining = {
                "A": list(candidate_ranks),
                "B": list(incumbent_ranks),
            }
            for seat in range(SEAT_COUNT):
                slot = ROTATION_PLAN[rotation][seat]
                rank = remaining[slot].pop(0)
                rows.append(
                    SeatResult(
                        seed=seed,
                        rotation=rotation,
                        game_mode=GAME_MODE,
                        seat=Seat(seat),
                        policy_identity=(
                            CANDIDATE_IDENTITY
                            if slot == CANDIDATE_SLOT
                            else INCUMBENT_IDENTITY
                        ),
                        score=SCORE_BY_RANK[rank],
                        rank=rank,
                    )
                )
    return tuple(rows)


def comparison_result(profile_of: Callable[[int], object]) -> ComparisonResult:
    rows = seat_rows(profile_of)  # type: ignore[arg-type]
    return ComparisonResult(
        plan=ComparisonPlan(
            policy_a=PolicySpec(CANDIDATE_IDENTITY, stub_candidate_policy),
            policy_b=PolicySpec(INCUMBENT_IDENTITY, stub_incumbent_policy),
            seeds=SEEDS,
            game_mode=GAME_MODE,
            max_steps=MAX_STEPS,
        ),
        seat_results=rows,
        metrics_a=aggregate_policy_metrics(CANDIDATE_IDENTITY, rows),
        metrics_b=aggregate_policy_metrics(INCUMBENT_IDENTITY, rows),
    )


def specs() -> dict[str, PolicySpec]:
    return {
        "candidate_spec": PolicySpec(CANDIDATE_IDENTITY, stub_candidate_policy),
        "incumbent_spec": PolicySpec(INCUMBENT_IDENTITY, stub_incumbent_policy),
    }


class FinalScoreTest(unittest.TestCase):
    def test_uma_oka_constants_are_fixed(self) -> None:
        self.assertEqual(UMA, (30, 10, -10, -30))
        self.assertEqual(OKA, (20, 0, 0, 0))

    def test_final_score_examples(self) -> None:
        self.assertEqual(final_score_units(40_000, 1), 60_000)
        self.assertEqual(final_score_units(30_000, 2), 10_000)
        self.assertEqual(final_score_units(20_000, 3), -20_000)
        self.assertEqual(final_score_units(10_000, 4), -50_000)
        self.assertEqual(final_score_units(-2_300, 4), -62_300)

    def test_final_scores_are_zero_sum_when_points_are_conserved(self) -> None:
        for points in ((40_000, 30_000, 20_000, 10_000), (25_000,) * 4):
            total = sum(
                final_score_units(value, rank)
                for rank, value in enumerate(points, start=1)
            )
            self.assertEqual(total, 0)

    def test_invalid_rank_or_type_is_rejected(self) -> None:
        with self.assertRaises(HeuristicCandidateProtocolError):
            final_score_units(25_000, 5)
        with self.assertRaises(TypeError):
            final_score_units(25_000.0, 1)  # type: ignore[arg-type]


class ProtocolTest(unittest.TestCase):
    def test_rotation_plan_matches_generic_comparison(self) -> None:
        plan = ComparisonPlan(
            policy_a=PolicySpec("A", stub_candidate_policy),
            policy_b=PolicySpec("B", stub_incumbent_policy),
            seeds=(1,),
        )
        for rotation in range(ROTATION_COUNT):
            generic = tuple(spec.identity for spec in _seat_assignment(plan, rotation))
            self.assertEqual(generic, ROTATION_PLAN[rotation])

    def test_population_must_be_exactly_100_unique_seeds(self) -> None:
        self.assertEqual(require_population(SEEDS), SEEDS)
        for bad in (SEEDS[:-1], SEEDS[:-1] + (SEEDS[0],)):
            with self.assertRaises(HeuristicCandidateProtocolError):
                require_population(bad)

    def test_participants_must_be_distinct_heuristic_without_checkpoint(self) -> None:
        require_participants(binding("candidate"), binding("incumbent"))
        with self.assertRaises(HeuristicCandidateProtocolError):
            require_participants(binding("candidate"), binding("candidate"))
        with self.assertRaises(HeuristicCandidateProtocolError):
            require_participants(
                binding("candidate", family="learning"), binding("incumbent")
            )
        with self.assertRaises(HeuristicCandidateProtocolError):
            require_participants(
                binding(
                    "candidate",
                    checkpoint_binding="x:y",
                    checkpoint_identity="c",
                    checkpoint_digest="d" * 64,
                ),
                binding("incumbent"),
            )


class _Bundle(unittest.TestCase):
    def setUp(self) -> None:
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.lock_path = self.root / "candidate-lock.json"
        self.comparison_path = self.root / "comparison.json"
        self.result_path = self.root / "candidate-result.json"
        self.ledger, self.allocation = ledger_with_allocation()

    def build_lock(self, **overrides: object) -> dict[str, object]:
        arguments: dict[str, object] = {
            "destinations": {
                "comparison_artifact": self.comparison_path,
                "candidate_result": self.result_path,
            },
            "candidate": binding("candidate"),
            "incumbent": binding("incumbent"),
            "seeds": SEEDS,
            "max_workers": 2,
            "seed_ledger": self.ledger,
            "allocation_binding": self.allocation,
        }
        arguments.update(overrides)
        with merged_main_execution():
            return build_lock_document(**arguments)  # type: ignore[arg-type]

    def run_event(
        self, profile_of: Callable[[int], object], **kwargs: object
    ) -> dict[str, object]:
        save_lock_document(self.build_lock(), self.lock_path)

        def execute(plan: ComparisonPlan, *, max_workers: int) -> ComparisonResult:
            self.assertEqual(plan.game_mode, GAME_MODE)
            self.assertEqual(plan.max_steps, MAX_STEPS)
            self.assertEqual(plan.seeds, SEEDS)
            self.assertEqual(max_workers, 2)
            return comparison_result(profile_of)

        with (
            merged_main_execution(),
            mock.patch.object(
                artifact_module,
                "_collect_execution_provenance",
                return_value=comparison_provenance(),
            ),
        ):
            outcome = run_candidate_evaluation(
                lock_path=self.lock_path,
                seed_ledger=self.ledger,
                execute=execute,
                **specs(),  # type: ignore[arg-type]
                **kwargs,  # type: ignore[arg-type]
            )
        return outcome.candidate_result


class LockTest(_Bundle):
    def test_lock_binds_protocol_participants_and_allocation(self) -> None:
        save_lock_document(self.build_lock(), self.lock_path)
        loaded = load_lock_document(self.lock_path)
        self.assertEqual(loaded["protocol"]["protocol_id"], PROTOCOL_ID)
        self.assertEqual(
            loaded["protocol"]["classification"]["final_score"]["uma"], list(UMA)
        )
        self.assertEqual(loaded["seed_allocation_binding"], self.allocation)
        self.assertIs(loaded["result_exposed"], False)
        candidate, incumbent = locked_participants(loaded)
        self.assertEqual(candidate.policy_identity, CANDIDATE_IDENTITY)
        self.assertEqual(incumbent.policy_identity, INCUMBENT_IDENTITY)
        self.assertEqual(locked_seeds(loaded), SEEDS)

    def test_lock_is_write_once(self) -> None:
        document = self.build_lock()
        save_lock_document(document, self.lock_path)
        with self.assertRaises(HeuristicCandidateLockError):
            save_lock_document(document, self.lock_path)

    def test_allocation_must_belong_to_this_protocol_and_be_active(self) -> None:
        for ledger, bound in (
            ledger_with_allocation(owner_issue="lisbun/lisjong-arena#1"),
            ledger_with_allocation(protocol="other-protocol"),
            ledger_with_allocation(retire=True),
        ):
            with self.assertRaisesRegex(HeuristicCandidateLockError, "allocation"):
                self.build_lock(seed_ledger=ledger, allocation_binding=bound)

    def test_allocation_membership_must_equal_the_locked_seeds(self) -> None:
        other = tuple(range(1, 1 + SEED_BLOCK_COUNT))
        ledger, bound = ledger_with_allocation(seeds=other)
        with self.assertRaises(HeuristicCandidateLockError):
            self.build_lock(seed_ledger=ledger, allocation_binding=bound)

    def test_implementation_revision_must_match_live_provenance(self) -> None:
        with self.assertRaisesRegex(HeuristicCandidateLockError, "revision"):
            self.build_lock(
                candidate=binding("candidate", implementation_revision="d" * 40)
            )

    def test_tampered_lock_is_rejected(self) -> None:
        document = self.build_lock()
        for mutate in (
            lambda doc: doc["protocol"].__setitem__("max_steps", 1),
            lambda doc: doc["protocol"]["classification"]["final_score"].__setitem__(
                "uma", [20, 10, -10, -20]
            ),
            lambda doc: doc.__setitem__("result_exposed", True),
            lambda doc: doc.__setitem__("max_workers", 3),
        ):
            tampered = copy.deepcopy(document)
            mutate(tampered)
            path = self.root / f"tampered-{id(mutate)}.json"
            path.write_text(canonical_json_text(tampered), encoding="utf-8")
            with self.assertRaises(HeuristicCandidateLockError):
                load_lock_document(path)


class EvaluationTest(_Bundle):
    def test_candidate_advantage_is_candidate_superior(self) -> None:
        document = self.run_event(lambda seed: CANDIDATE_ADVANTAGE)
        self.assertEqual(document["classification"]["label"], CANDIDATE_SUPERIOR_LABEL)
        block = document["primary"]["seed_blocks"][0]
        # candidate: 60 + 10, incumbent: -20 - 50 per hanchan.
        self.assertEqual(block["candidate_mean_final_score"], 35.0)
        self.assertEqual(block["incumbent_mean_final_score"], -35.0)
        self.assertEqual(block["delta"], 70.0)
        diagnostics = document["secondary_diagnostics"]
        self.assertEqual(diagnostics["candidate"]["average_rank"], 1.5)
        self.assertEqual(diagnostics["average_rank_difference"], -2.0)
        self.assertIs(diagnostics["overrides_primary_classification"], False)
        verified = verify_candidate_bundle(
            lock_path=self.lock_path,
            comparison_path=self.comparison_path,
            result_path=self.result_path,
        )
        self.assertEqual(verified, document)

    def test_incumbent_advantage_is_champion_superior(self) -> None:
        document = self.run_event(lambda seed: INCUMBENT_ADVANTAGE)
        self.assertEqual(document["classification"]["label"], INCUMBENT_SUPERIOR_LABEL)

    def test_mixed_population_is_inconclusive(self) -> None:
        document = self.run_event(
            lambda seed: CANDIDATE_ADVANTAGE if seed % 2 else INCUMBENT_ADVANTAGE
        )
        self.assertEqual(document["classification"]["label"], INCONCLUSIVE_LABEL)
        self.assertEqual(
            document["primary"]["block_sign_counts"],
            {
                "negative_block_count": 50,
                "positive_block_count": 50,
                "zero_block_count": 0,
            },
        )

    def test_progress_callback_is_forwarded(self) -> None:
        save_lock_document(self.build_lock(), self.lock_path)
        seen: list[tuple[int, int]] = []

        def execute(plan, *, max_workers, progress_callback):  # type: ignore[no-untyped-def]
            progress_callback(1, 400)
            return comparison_result(lambda seed: CANDIDATE_ADVANTAGE)

        with (
            merged_main_execution(),
            mock.patch.object(
                artifact_module,
                "_collect_execution_provenance",
                return_value=comparison_provenance(),
            ),
        ):
            run_candidate_evaluation(
                lock_path=self.lock_path,
                seed_ledger=self.ledger,
                execute=execute,
                progress_callback=lambda done, total: seen.append((done, total)),
                **specs(),  # type: ignore[arg-type]
            )
        self.assertEqual(seen, [(1, 400)])

    def test_self_consistent_result_tamper_is_rejected(self) -> None:
        document = self.run_event(lambda seed: INCUMBENT_ADVANTAGE)
        tampered = copy.deepcopy(document)
        tampered["classification"]["label"] = CANDIDATE_SUPERIOR_LABEL
        payload = {k: v for k, v in tampered.items() if k != "result_identity"}
        tampered["result_identity"] = lock_module.document_identity(payload)
        forged = self.root / "forged-result.json"
        forged.write_text(canonical_json_text(tampered), encoding="utf-8")
        with self.assertRaises(HeuristicCandidateResultError):
            verify_candidate_bundle(
                lock_path=self.lock_path,
                comparison_path=self.comparison_path,
                result_path=forged,
            )

    def test_mismatched_policy_spec_is_rejected_before_execution(self) -> None:
        save_lock_document(self.build_lock(), self.lock_path)

        def execute(*args: object, **kwargs: object) -> ComparisonResult:
            raise AssertionError("execution must not start")

        with merged_main_execution(), self.assertRaises(HeuristicCandidateLockError):
            run_candidate_evaluation(
                lock_path=self.lock_path,
                candidate_spec=PolicySpec(CANDIDATE_IDENTITY, stub_incumbent_policy),
                incumbent_spec=PolicySpec(INCUMBENT_IDENTITY, stub_incumbent_policy),
                seed_ledger=self.ledger,
                execute=execute,
            )

    def test_live_target_drift_rejects_before_execution(self) -> None:
        save_lock_document(self.build_lock(), self.lock_path)

        def execute(*args: object, **kwargs: object) -> ComparisonResult:
            raise AssertionError("execution must not start")

        live = lock_provenance()
        live_values = {
            name: getattr(live, name)
            for name in (
                "execution_environment",
                "lisjong_arena_version",
                "lisjong_arena_revision",
                "lisjong_version",
                "lisjong_revision",
                "lisjong_engine_version",
                "lisjong_engine_revision",
                "riichienv_version",
                "python_version",
            )
        }
        live_values["lisjong_revision"] = "e" * 40
        with (
            merged_main_execution(SingleRoundExecutionProvenance(**live_values)),
            self.assertRaises(HeuristicCandidateLockError),
        ):
            run_candidate_evaluation(
                lock_path=self.lock_path,
                seed_ledger=self.ledger,
                execute=execute,
                **specs(),  # type: ignore[arg-type]
            )

    def test_wrong_hanchan_count_is_rejected(self) -> None:
        save_lock_document(self.build_lock(), self.lock_path)
        full = comparison_result(lambda seed: CANDIDATE_ADVANTAGE)
        short = ComparisonResult(
            plan=full.plan,
            seat_results=full.seat_results[:-16],
            metrics_a=aggregate_policy_metrics(
                CANDIDATE_IDENTITY, full.seat_results[:-16]
            ),
            metrics_b=aggregate_policy_metrics(
                INCUMBENT_IDENTITY, full.seat_results[:-16]
            ),
        )
        with merged_main_execution(), self.assertRaises(HeuristicCandidateResultError):
            run_candidate_evaluation(
                lock_path=self.lock_path,
                seed_ledger=self.ledger,
                execute=lambda plan, *, max_workers: short,
                **specs(),  # type: ignore[arg-type]
            )
        self.assertFalse(self.comparison_path.exists())

    def test_cli_verify_reports_classification(self) -> None:
        self.run_event(lambda seed: CANDIDATE_ADVANTAGE)
        output = io.StringIO()
        with redirect_stdout(output):
            code = cli_main(
                [
                    "verify",
                    "--lock",
                    str(self.lock_path),
                    "--comparison",
                    str(self.comparison_path),
                    "--result",
                    str(self.result_path),
                ]
            )
        self.assertEqual(code, 0)
        self.assertIn(f"classification={CANDIDATE_SUPERIOR_LABEL}", output.getvalue())
        summary_line = next(
            line
            for line in output.getvalue().splitlines()
            if line.startswith("primary_summary=")
        )
        self.assertEqual(json.loads(summary_line.split("=", 1)[1])["mean_delta"], 70.0)


if __name__ == "__main__":
    unittest.main()
