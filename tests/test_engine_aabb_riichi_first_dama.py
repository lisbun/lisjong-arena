"""#544 riichi-first-dama-branch-v1（R1 pilot）の観測・照合・分散・費用判定・plan・run・verify。

実engineの対局・Rust wheel・実Policyの半荘実行は使わない（engine呼び出しとworkerの
playを差し替える）。lisjongの特徴APIとR1 wrapper（lisjong #357）は実物を使う。
"""

import os
import random
import statistics
import tempfile
import unittest
from dataclasses import replace
from fractions import Fraction
from pathlib import Path

import _engine_aabb_riichi_dama_branch_fixtures as v1
import _engine_aabb_riichi_first_dama_fixtures as fx
from _engine_aabb_record_fixtures import SCRIPT_X, play
from lisjong.policies import PlacementAwareSpeedCallPolicy
from lisjong.policy_contract.action import RiichiAction
from lisjong.policy_contract.seat import Seat
from lisjong_engine.round_completion import project_round_completion
from lisjong_engine.seat import Seat as EngineSeat

from lisjong_arena import seed_registry
from lisjong_arena.heuristic_candidate_engine_aabb import riichi_first_dama as core
from lisjong_arena.heuristic_candidate_engine_aabb import (
    riichi_first_dama_plan as plan_module,
)
from lisjong_arena.heuristic_candidate_engine_aabb import (
    riichi_first_dama_worker as worker,
)
from lisjong_arena.heuristic_candidate_engine_aabb.call_value_shadow_worker import (
    canonical_digest,
    encode_action,
)
from lisjong_arena.heuristic_candidate_engine_aabb.executor import RunStopped
from lisjong_arena.heuristic_candidate_engine_aabb.kyoku_record import (
    SeatAssignment,
    project_row,
)
from lisjong_arena.heuristic_candidate_engine_aabb.protocol import (
    EngineAabbProtocolError,
)

RIICHI = RiichiAction(actor=Seat.SEAT_0)
SEED = plan_module.SEEDS[0]


def _facts(seed=SEED):
    return [project_round_completion(item) for item in play(seed, SCRIPT_X).history]


def _engine(seat: int) -> EngineSeat:
    return list(EngineSeat)[seat]


def scripted_runner(rounds, facts):
    """局ごとに (seat, decision) を順にPolicyへ渡し、局の終わりにfactを配る。"""

    def runner(policies, *, seed, rules, on_delivery=None, on_selector_call=None):
        for index, decisions in enumerate(rounds):
            for seat, decision in decisions:
                policies[_engine(seat)].choose_action(decision)
            on_delivery((facts[index],))
        return None

    return runner


def _task(round_ordinal=0):
    branch = {
        "declaration": encode_action(v1.DECLARATION),
        "round": v1.context_round(
            v1.BRANCH_POINTS.get(round_ordinal, (0, v1.K0_DECISION))[1]
        ),
        "round_ordinal": round_ordinal,
        "seat": 0,
    }
    return worker.BranchTask(
        seed=SEED,
        branch_ordinal=0,
        branch=branch,
        incumbent=v1.plan_module.champion(fx.LISJONG_REVISION).to_document(),
        wheel_path=fx.WHEEL_PATH,
    )


# ---------------------------------------------------------------------------
# worker
# ---------------------------------------------------------------------------


class WorkerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.facts = _facts()
        self.other = v1.discard_decision(v1.KANCHAN, seat=Seat.SEAT_1)

    def test_r_records_every_decision_with_its_legal_position(self) -> None:
        observer = worker.RFirstObserver()
        observer.observe(1, self.other, self.other.legal_actions[2])
        observer.observe(0, v1.K0_DECISION, RIICHI)
        declaration = v1.declaration_decision(v1.KANCHAN, opponent_riichi=True)
        observer.observe(0, declaration, v1.DECLARATION)
        observer.on_delivery((self.facts[0],))
        analysis = observer.finish(1)
        self.assertEqual(len(analysis["actions"]), 3)
        first = analysis["actions"][0]
        self.assertEqual(
            first,
            [
                0,
                1,
                encode_action(self.other.legal_actions[2]),
                2,
                len(self.other.legal_actions),
                worker.legal_digest(self.other),
            ],
        )
        self.assertEqual(analysis["actions"][1][2], encode_action(RIICHI))
        self.assertEqual(analysis["actions"][1][3], 0)
        # v1の記録はそのまま（分岐点・宣言牌・digest）
        (record,) = analysis["branches"]
        self.assertEqual(record["declaration_decision_index"], 2)

    def test_d_arm_returns_to_the_champion_including_riichi(self) -> None:
        rounds = [[(1, self.other), (0, v1.K0_DECISION), (0, v1.K0_DECISION)]]
        record = worker.d_arm(_task(), runner=scripted_runner(rounds, self.facts))
        self.assertEqual(record["invalid"], [])
        self.assertEqual(record["status"], "branched")
        self.assertEqual(record["branch_decision_index"], 1)
        # 分岐点は宣言牌をリーチせずに打ち、次の判断のリーチはそのまま通る
        self.assertEqual(record["branch_action"], encode_action(v1.DECLARATION))
        self.assertEqual(record["riichi_after_branch"], 1)
        self.assertEqual(record["wrapper_riichi_after_branch"], 1)
        self.assertEqual(
            record["first_riichi_after_branch"],
            {"decision_index": 2, "seat_decisions_after_branch": 1},
        )
        self.assertEqual(
            record["prefix_sha256"],
            canonical_digest(
                [
                    [
                        1,
                        encode_action(
                            PlacementAwareSpeedCallPolicy().choose_action(self.other)
                        ),
                    ]
                ]
            ),
        )

    def test_riichi_outside_the_target_round_is_not_counted(self) -> None:
        k9 = v1.K9_DECISION
        rounds = [[(0, v1.K0_DECISION)], [(0, k9), (0, k9)]]
        task = replace(
            _task(),
            branch={
                **_task().branch,
                "round": v1.context_round(k9),
                "round_ordinal": 1,
            },
        )
        record = worker.d_arm(
            task, runner=scripted_runner(rounds, [self.facts[0], self.facts[9]])
        )
        self.assertEqual(record["invalid"], [])
        self.assertEqual(record["branch_decision_index"], 1)
        self.assertEqual(record["riichi_after_branch"], 1)
        self.assertEqual(record["first_riichi_after_branch"]["decision_index"], 2)

    def test_wrapper_version_is_required(self) -> None:
        api = worker.analysis_api()
        self.assertEqual(api.RIICHI_FIRST_DAMA_BRANCH_VERSION, worker.WRAPPER_VERSION)


# ---------------------------------------------------------------------------
# 1分岐の照合
# ---------------------------------------------------------------------------


def _evaluate(**d_changes):
    line = {
        **{key: None for key in core._BRANCH_FIELDS},
        "branch_id": f"{SEED}-0-0",
        "branch_ordinal": 0,
        "kind": "branch",
        "seed": SEED,
        "seat": 0,
    }
    record = fx.r_record(SEED, 0)
    for name, key in core._R_BRANCH_KEYS:
        line[name] = record[key]
    row = {
        "kyoku_record": project_row(
            play(SEED, SCRIPT_X),
            seed=SEED,
            keys={"placement": 0},
            seats=(SeatAssignment("B", fx.CHAMPION),) * 4,
            selector_call_counts=(fx.DECISIONS,) * 4,
            policy_call_counts=(fx.DECISIONS,) * 4,
            layout=core.LAYOUT,
        )
    }
    line["r_settlement"] = core.r_settlement(row["kyoku_record"], 0)
    task_branch = core.task_branch(record)
    line["d"] = fx.d_record(SEED, 0, task_branch, "better", **d_changes)
    feature = {"error": None, "features": record["features"]["features"]}
    return core.evaluate_branch(line, feature, row)


class EvaluateTest(unittest.TestCase):
    def test_resumed_riichi_after_the_branch_is_valid(self) -> None:
        result = _evaluate(
            riichi_after_branch=1,
            wrapper_riichi_after_branch=1,
            first_riichi_after_branch={
                "decision_index": 6,
                "seat_decisions_after_branch": 2,
            },
        )
        self.assertEqual(result["invalid"], [])
        self.assertEqual(result["riichi_after_branch"], 1)
        self.assertEqual(result["delta"], result["g_d"] - result["g_r"])

    def test_riichi_at_the_branch_point_is_invalid(self) -> None:
        result = _evaluate(branch_action=encode_action(RIICHI))
        self.assertIn(
            "the D branch point did not discard the declaration tile",
            result["invalid"],
        )

    def test_riichi_counts_must_agree(self) -> None:
        cases = [
            {"riichi_after_branch": 1, "wrapper_riichi_after_branch": 0},
            {"riichi_after_branch": 1, "wrapper_riichi_after_branch": 1},
            {
                "first_riichi_after_branch": {
                    "decision_index": 6,
                    "seat_decisions_after_branch": 1,
                }
            },
        ]
        for changes in cases:
            with self.subTest(changes=changes):
                result = _evaluate(**changes)
                self.assertTrue(
                    any("riichi after the branch" in item for item in result["invalid"])
                )
        early = _evaluate(
            riichi_after_branch=1,
            wrapper_riichi_after_branch=1,
            first_riichi_after_branch={
                "decision_index": 2,
                "seat_decisions_after_branch": 1,
            },
        )
        self.assertIn(
            "the riichi after the branch is not after the branch point",
            early["invalid"],
        )


# ---------------------------------------------------------------------------
# 分散・σ_u・bootstrap・費用判定
# ---------------------------------------------------------------------------


def _rows(values):
    return [
        {"g_d": d, "g_r": r, "seed": seed} for seed, pairs in values for r, d in pairs
    ]


def _reference_sigma_squared(clusters):
    sums = [sum(d - r for r, d in pairs) for pairs in clusters]
    counts = [len(pairs) for pairs in clusters]
    mu = Fraction(sum(sums), sum(counts))
    units = [s - mu * n for s, n in zip(sums, counts, strict=True)]
    n_bar = Fraction(sum(counts), len(counts))
    return statistics.variance(units) / (n_bar * n_bar)


class VarianceTest(unittest.TestCase):
    DATA = [
        (1, [(10, -20), (-10, 0), (30, 30)]),
        (2, [(0, -10)]),
        (3, [(-20, -10), (5, 25)]),
        (4, [(12, 0), (0, 0), (40, -40), (-8, 2)]),
    ]

    def test_branch_moments_and_sigma_u_match_the_reference(self) -> None:
        rows = _rows(self.DATA)
        summary = core.variance_summary(rows)
        r = [row["g_r"] for row in rows]
        d = [row["g_d"] for row in rows]
        delta = [b - a for a, b in zip(r, d, strict=True)]
        branch = summary["branch"]
        as_fraction = lambda value: Fraction(value["numerator"], value["denominator"])  # noqa: E731
        self.assertEqual(
            as_fraction(branch["var_r"]), statistics.variance(map(Fraction, r))
        )
        self.assertEqual(
            as_fraction(branch["var_d"]), statistics.variance(map(Fraction, d))
        )
        self.assertEqual(
            as_fraction(branch["var_delta"]), statistics.variance(map(Fraction, delta))
        )
        self.assertEqual(
            as_fraction(summary["ratio"]),
            statistics.variance(map(Fraction, delta))
            / (
                statistics.variance(map(Fraction, r))
                + statistics.variance(map(Fraction, d))
            ),
        )
        self.assertEqual(
            as_fraction(summary["sigma_u_squared"]),
            _reference_sigma_squared([pairs for _, pairs in self.DATA]),
        )
        self.assertEqual(summary["seeds"], 4)

    def test_bootstrap_matches_the_reference_resampling(self) -> None:
        clusters = [core._cluster(_rows([item])) for item in self.DATA]
        result = core.variance_bootstrap(clusters)
        rng = random.Random(core.BOOTSTRAP_SEED)
        sigmas = []
        for _ in range(core.BOOTSTRAP_REPLICATES):
            drawn = [self.DATA[rng.randrange(4)][1] for _ in range(4)]
            sigmas.append(_reference_sigma_squared(drawn))
        sigmas.sort()
        p90 = sigmas[8999]
        self.assertEqual(
            Fraction(
                result["sigma_u_squared"]["P90"]["numerator"],
                result["sigma_u_squared"]["P90"]["denominator"],
            ),
            p90,
        )
        self.assertEqual(core.variance_bootstrap(clusters), result)

    def test_one_seed_is_not_estimable(self) -> None:
        summary = core.variance_summary(_rows(self.DATA[:1]))
        self.assertIsNone(summary["sigma_u_squared"])
        self.assertIsNone(summary["bootstrap"]["sigma_u_squared"])
        cost = core.cost_screening(summary, _timings(), 16)
        self.assertEqual(cost["verdict"], plan_module.COST_NOT_ESTIMABLE)

    def test_cost_screening_boundary(self) -> None:
        z = statistics.NormalDist().inv_cdf(0.95) + statistics.NormalDist().inv_cdf(0.8)
        # cost_per_seed = 50 + (32 / 16) × 25 = 100秒。予算43,200秒 → (K + 2) × 100 + 900 <= 43,200
        budget_k = (43_200 - 900) // 100 - 2
        for k, verdict in (
            (budget_k, plan_module.COST_WITHIN_BUDGET),
            (budget_k + 1, plan_module.COST_OVER_BUDGET),
        ):
            # K = ceil(z² σ² / 25) = k となるσ²を作る
            sigma_squared = Fraction(k * 25) / Fraction(z * z) - Fraction(1, 10**9)
            variance = {
                "bootstrap": {
                    "sigma_u_squared": {
                        "P90": {
                            "denominator": sigma_squared.denominator,
                            "numerator": sigma_squared.numerator,
                        }
                    }
                }
            }
            with self.subTest(k=k):
                cost = core.cost_screening(variance, _timings(), 16)
                self.assertEqual(cost["seeds_needed"], k)
                self.assertEqual(cost["cost_per_seed_seconds"], 100.0)
                self.assertEqual(cost["verdict"], verdict)
                self.assertTrue(cost["not_a_learning_or_adoption_decision"])


def _timings():
    tasks = [{"arm": "R", "wall_seconds": 50.0}] * 16
    tasks += [{"arm": "D", "wall_seconds": 25.0}] * 32
    return {"tasks": tasks}


# ---------------------------------------------------------------------------
# plan
# ---------------------------------------------------------------------------


class PlanTest(unittest.TestCase):
    def setUp(self) -> None:
        self._dir = tempfile.TemporaryDirectory()
        self.tmp = Path(self._dir.name)

    def tearDown(self) -> None:
        self._dir.cleanup()

    def test_fixed_invariants(self) -> None:
        plan = fx.plan_document(self.tmp)
        self.assertEqual(
            plan["population"]["ordered_seeds"], list(range(544_000, 544_016))
        )
        # verifyで再実行するのは予約範囲の最小と最大
        self.assertEqual(plan["verify_replay_seeds"], [544_000, 544_015])
        self.assertEqual(plan["execution"]["timeout_seconds"], 600)
        self.assertEqual(plan["resource_limits"]["process_tree_rss_bytes"], 4 * 1024**3)
        self.assertEqual(plan["resource_limits"]["wall_seconds"], 3 * 3600.0)
        self.assertEqual(plan["wrapper_version"], "riichi-first-dama-branch-wrapper-v1")
        self.assertEqual(plan["criteria"]["cost_screening"]["budget_seconds"], 43_200)
        self.assertEqual(plan["participant"]["behavior_configuration"], {})
        # Arena・lisjong・engineのrevisionは具体的な値でplanへ束縛される
        self.assertEqual(plan["execution_target"]["revision"], fx.ARENA_REVISION)
        self.assertEqual(plan["participant"]["lisjong_revision"], fx.LISJONG_REVISION)
        self.assertIn("lisjong_engine_revision", plan["environment"]["provenance"])
        rehearsal = fx.plan_document(self.tmp, run_kind="rehearsal", name="r")
        self.assertEqual(rehearsal["population"]["ordered_seeds"], [472_005, 472_006])
        self.assertEqual(rehearsal["verify_replay_seeds"], [])
        self.assertIsNone(rehearsal["start_condition"])
        self.assertEqual(rehearsal["resource_limits"]["wall_seconds"], 3600.0)

    def test_drift_is_detected(self) -> None:
        plan = fx.plan_document(self.tmp)
        drifts = {
            "criteria": {**plan["criteria"], "order": []},
            "verify_replay_seeds": [544_001, 544_014],
            "protocol_version": "riichi-dama-branch-v1",
            "wrapper_version": "riichi-dama-branch-v1",
            "execution": {**plan["execution"], "timeout_seconds": 900},
            "resource_limits": {**plan["resource_limits"], "wall_seconds": 10**6},
            "population": {
                **plan["population"],
                "ordered_seeds": list(range(544_001, 544_017)),
            },
            "execution_target": {**plan["execution_target"], "revision": "f" * 40},
            "rules_digest": "0" * 64,
        }
        for name, value in drifts.items():
            with self.subTest(name=name), self.assertRaises(EngineAabbProtocolError):
                plan_module.parse_plan({**plan, name: value})
        with self.assertRaises(plan_module.RiichiFirstDamaError):
            plan_module.parse_plan({**plan, "plan_digest": "0" * 64})

    def test_start_condition_puts_the_4_hour_total_first(self) -> None:
        estimate = fx.time_estimate()
        projected = estimate["projected_main_seconds"]
        self.assertLess(projected, plan_module.MAIN_BLOCK_SECONDS)
        # 残り（4時間 − 経過）がちょうど見積もりと等しければ開始できる
        boundary = plan_module.TOTAL_LIMIT_SECONDS - projected
        plan = fx.plan_document(self.tmp, elapsed_before_seconds=boundary)
        self.assertEqual(plan["start_condition"]["verdict"], plan_module.WITHIN_BUDGET)
        self.assertEqual(plan["resource_limits"]["wall_seconds"], projected)
        with self.assertRaisesRegex(plan_module.RiichiFirstDamaError, "NOT-STARTED"):
            fx.plan_document(self.tmp, name="late", elapsed_before_seconds=boundary + 1)
        slow = fx.time_estimate(d_wall=60.0)
        self.assertGreater(
            slow["projected_main_seconds"], plan_module.MAIN_BLOCK_SECONDS
        )
        with self.assertRaisesRegex(plan_module.RiichiFirstDamaError, "NOT-STARTED"):
            fx.plan_document(self.tmp, name="slow", time_estimate=slow)
        with self.assertRaisesRegex(
            plan_module.RiichiFirstDamaError, "rehearsal elapsed"
        ):
            fx.plan_document(self.tmp, name="small", elapsed_before_seconds=100.0)
        with self.assertRaises(plan_module.RiichiFirstDamaError):
            fx.plan_document(self.tmp, name="none", elapsed_before_seconds=None)
        other = fx.time_estimate(environment_digest="9" * 64)
        with self.assertRaisesRegex(
            plan_module.RiichiFirstDamaError, "another environment"
        ):
            fx.plan_document(self.tmp, name="env", time_estimate=other)
        tampered = {**fx.time_estimate(), "projected_main_seconds": 1.0}
        with self.assertRaises(plan_module.RiichiFirstDamaError):
            fx.plan_document(self.tmp, name="tamper", time_estimate=tampered)

    def test_estimate_formula(self) -> None:
        values = plan_module.estimate_values(
            r_walls=[40.0, 60.0],
            d_walls=[20.0, 30.0],
            branches=10,
            rehearsal_seat_hanchans=8,
            rehearsal_elapsed_seconds=1000.0,
        )
        # 10/8 < 286/96 なので n̂ = 143/48 × 64 = 572/3、verifyは 143/48 × 8 = 143/6
        self.assertEqual(values["n_hat"], {"denominator": 3, "numerator": 572})
        self.assertEqual(values["n_hat_verify"], {"denominator": 6, "numerator": 143})
        run = 16 * 50.0 + float(Fraction(572, 3)) * 25.0
        verify = 2 * 50.0 + float(Fraction(143, 6)) * 25.0
        self.assertEqual(values["projected_run_seconds"], run)
        self.assertEqual(values["projected_main_seconds"], run + verify + 900)

    def test_owner_and_rehearsal_rules(self) -> None:
        for owner in ("lisbun/lisjong-arena#544", "lisbun/lisjong-arena#100", "x"):
            with (
                self.subTest(owner=owner),
                self.assertRaises(plan_module.RiichiFirstDamaError),
            ):
                fx.plan_document(self.tmp, owner_issue=owner)
        with self.assertRaises(plan_module.RiichiFirstDamaError):
            fx.plan_document(self.tmp, observation="unobserved")
        with self.assertRaises(plan_module.RiichiFirstDamaError):
            fx.plan_document(
                self.tmp, run_kind="rehearsal", time_estimate=fx.time_estimate()
            )

    def test_seed_overlap_with_another_allocation_is_rejected(self) -> None:
        other = v1.allocation(
            seeds=(544_010,),
            owner="lisbun/lisjong-arena#999",
            population="other",
            domain=seed_registry.RIICHIENV_HALF_HANCHAN_SEED_DOMAIN,
        )
        document = fx.ledger(fx.RECORD, other)
        with self.assertRaises(plan_module.RiichiFirstDamaError):
            fx.plan_document(self.tmp, ledger=document)

    def test_plan_is_write_once(self) -> None:
        path, document = fx.write_plan(self.tmp)
        with self.assertRaises(FileExistsError):
            plan_module.save_plan(document, path)
        self.assertEqual(plan_module.load_plan(path), document)


# ---------------------------------------------------------------------------
# run / verify / compare / estimate
# ---------------------------------------------------------------------------


class _RunCase(unittest.TestCase):
    def setUp(self) -> None:
        self._dir = tempfile.TemporaryDirectory()
        self.tmp = Path(self._dir.name)

    def tearDown(self) -> None:
        self._dir.cleanup()

    def _ledger_path(self):
        path = self.tmp / "ledger.json"
        if not path.exists():
            seed_registry.write_ledger(path, fx.ledger())
        return path

    def run_allocated(self, play_r, play_d, *, name="run"):
        path, plan = fx.write_plan(self.tmp, name=name)
        summary = core.run_plan(
            path,
            ledger_path=self._ledger_path(),
            play_r=play_r,
            play_d=play_d,
            live_gate=fx.gate,
        )
        return path, plan, summary

    def verify(self, path, play_r=fx.fake_r_both, play_d=fx.fake_d_better, **kwargs):
        return core.verify_plan(
            path, environment=fx.ENVIRONMENT, play_r=play_r, play_d=play_d, **kwargs
        )


class RunTest(_RunCase):
    def test_pilot_complete_publishes_and_verifies_with_replay(self) -> None:
        path, plan, summary = self.run_allocated(fx.fake_r_both, fx.fake_d_better)
        self.assertEqual(summary["decision"], plan_module.PILOT_COMPLETE)
        self.assertEqual(summary["branches"], 32)
        self.assertEqual(summary["invalid"]["count"], 0)
        self.assertEqual(summary["variance"]["seeds"], 16)
        self.assertEqual(summary["quality"]["resumed_riichi"]["resumed"], 16)
        self.assertEqual(summary["quality"]["resumed_riichi"]["branches"], 32)
        self.assertEqual(
            summary["cost_screening"]["verdict"], plan_module.COST_WITHIN_BUDGET
        )
        self.assertTrue(summary["descriptive"]["not_used_for_any_decision"])
        output = Path(plan["artifact_destinations"]["output"])
        self.assertEqual(
            sorted(item.name for item in output.iterdir()),
            sorted(
                [
                    "SHA256SUMS",
                    "actions.jsonl",
                    "branches.jsonl",
                    "features.jsonl",
                    "hanchans.json",
                    "summary.json",
                    "timings.json",
                ]
            ),
        )
        verified = self.verify(path)
        self.assertEqual(verified["decision"], plan_module.PILOT_COMPLETE)
        self.assertEqual(verified["cost_screening"], plan_module.COST_WITHIN_BUDGET)
        self.assertEqual(verified["rederived_lisjong_features"], 32)
        self.assertEqual(verified["replayed"], {"arms": 4, "seeds": [544_000, 544_015]})
        actions = fx.read_lines(output / "actions.jsonl")
        self.assertEqual(actions[0]["schema"], core.ACTIONS_SCHEMA)
        self.assertEqual(len(actions[1]["decisions"]), 48)

    def test_varied_seeds_have_a_positive_sigma_u(self) -> None:
        _, _, summary = self.run_allocated(fx.fake_r_both, fx.fake_d_varied)
        self.assertEqual(summary["decision"], plan_module.PILOT_COMPLETE)
        self.assertGreater(summary["variance"]["sigma_u"], 0)
        self.assertIsNotNone(summary["variance"]["bootstrap"]["ratio"])

    def test_riichi_at_the_branch_point_is_stop_invalid(self) -> None:
        _, _, summary = self.run_allocated(fx.fake_r_both, fx.fake_d_riichi_at_branch)
        self.assertEqual(summary["decision"], plan_module.STOP_INVALID)
        self.assertIsNone(summary["cost_screening"])
        self.assertEqual(
            summary["invalid"]["branches"],
            [
                {
                    "branch_id": "544005-0-0",
                    "reasons": [
                        "the D branch point did not discard the declaration tile"
                    ],
                }
            ],
        )

    def test_riichi_count_mismatch_is_stop_invalid(self) -> None:
        _, _, summary = self.run_allocated(fx.fake_r_both, fx.fake_d_count_mismatch)
        self.assertEqual(summary["decision"], plan_module.STOP_INVALID)
        self.assertEqual(summary["invalid"]["branches"][0]["branch_id"], "544005-9-0")

    def test_failure_keeps_the_evidence_without_a_result(self) -> None:
        path, plan = fx.write_plan(self.tmp)
        with self.assertRaises(RunStopped):
            core.run_plan(
                path,
                ledger_path=self._ledger_path(),
                play_r=fx.fake_r_failure,
                play_d=fx.fake_d_better,
                live_gate=fx.gate,
            )
        self.assertFalse(Path(plan["artifact_destinations"]["output"]).exists())
        stops = list(Path(plan["artifact_destinations"]["diagnostics"]).iterdir())
        self.assertEqual(len(stops), 1)
        stop = core.read_document(stops[0], "stop")
        self.assertEqual(stop["decision"], plan_module.STOP_INCOMPLETE)
        self.assertTrue(stop["not_a_result"])
        self.assertIn("synthetic worker failure", stop["error"])
        # 失敗前に完了したtask（R 2半荘）の実行記録を残す
        self.assertEqual([row["seed"] for row in stop["tasks"]], [544_000, 544_001])

    def test_output_is_write_once(self) -> None:
        path, _, _ = self.run_allocated(fx.fake_r_both, fx.fake_d_better)
        with self.assertRaises(plan_module.RiichiFirstDamaError):
            core.run_plan(
                path,
                ledger_path=self._ledger_path(),
                play_r=fx.fake_r_both,
                play_d=fx.fake_d_better,
                live_gate=fx.gate,
            )


class VerifyTamperTest(_RunCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._shared = tempfile.TemporaryDirectory()
        shared = Path(cls._shared.name)
        cls.path, cls.plan = fx.write_plan(shared)
        ledger_path = shared / "ledger.json"
        seed_registry.write_ledger(ledger_path, fx.ledger())
        core.run_plan(
            cls.path,
            ledger_path=ledger_path,
            play_r=fx.fake_r_both,
            play_d=fx.fake_d_better,
            live_gate=fx.gate,
        )
        cls.output = Path(cls.plan["artifact_destinations"]["output"])
        cls.pristine = {item.name: item.read_bytes() for item in cls.output.iterdir()}

    @classmethod
    def tearDownClass(cls) -> None:
        cls._shared.cleanup()

    def tearDown(self) -> None:
        for item in list(self.output.iterdir()):
            if item.name not in self.pristine:
                item.unlink()
        for name, data in self.pristine.items():
            (self.output / name).write_bytes(data)
        super().tearDown()

    def _resum(self):
        (self.output / "SHA256SUMS").write_text(
            core._sums_text(self.output), encoding="utf-8", newline="\n"
        )

    def _rejects(self, pattern):
        with self.assertRaisesRegex(EngineAabbProtocolError, pattern):
            self.verify(self.path)

    def test_changed_file_without_resum(self) -> None:
        lines = fx.read_lines(self.output / "actions.jsonl")
        lines[1]["decisions"][0][3] = 1
        fx.write_lines(self.output / "actions.jsonl", lines)
        self._rejects("SHA256SUMS")

    def test_action_tamper_with_resum(self) -> None:
        cases = {
            "order": lambda d: d.reverse(),
            "position": lambda d: d[0].__setitem__(3, 5),
            "seat": lambda d: d[0].__setitem__(1, 2),
            "riichi": lambda d: d[3].__setitem__(2, d[0][2]),
            "missing": lambda d: d.pop(),
        }
        for name, change in cases.items():
            with self.subTest(name=name):
                lines = fx.read_lines(self.output / "actions.jsonl")
                change(lines[1]["decisions"])
                fx.write_lines(self.output / "actions.jsonl", lines)
                self._resum()
                self._rejects("actions|summary differs")
                self.tearDown()

    def test_changed_settlement_with_resum(self) -> None:
        lines = fx.read_lines(self.output / "branches.jsonl")
        settlement = lines[3]["d"]["settlement"]
        settlement["point_deltas"] = [value * 2 for value in settlement["point_deltas"]]
        fx.write_lines(self.output / "branches.jsonl", lines)
        self._resum()
        self._rejects("summary differs|inputs")

    def test_missing_and_extra_files(self) -> None:
        (self.output / "actions.jsonl").unlink()
        self._resum()
        self._rejects("expected set")
        self.tearDown()
        (self.output / "extra.json").write_text("{}", encoding="utf-8")
        self._resum()
        self._rejects("expected set")

    def test_replay_mismatch_is_detected(self) -> None:
        with self.assertRaisesRegex(EngineAabbProtocolError, "replayed D arm differs"):
            self.verify(self.path, play_d=fx.fake_d_same)
        with self.assertRaisesRegex(EngineAabbProtocolError, "replayed R actions"):
            self.verify(self.path, play_r=fx.fake_r_other_actions)


class RehearsalTest(_RunCase):
    def _run(self, observation, play_r, play_d=fx.fake_d_better, name="r"):
        path, plan = fx.write_plan(
            self.tmp, run_kind="rehearsal", observation=observation, name=name
        )
        summary = core.run_plan(path, play_r=play_r, play_d=play_d, live_gate=fx.gate)
        return path, plan, summary

    def test_match_estimate_and_allocated_plan(self) -> None:
        observed, _, summary = self._run("observed", fx.fake_r_both)
        self.assertIsNone(summary["decision"])
        self.assertIsNone(summary["variance"])
        self.assertIsNone(summary["cost_screening"])
        unobserved, _, _ = self._run("unobserved", fx.fake_r_unobserved)
        compared = core.compare_rehearsals(
            observed, unobserved, environment=fx.ENVIRONMENT
        )
        self.assertTrue(compared["match"])
        estimate = core.estimate_budget(
            observed,
            unobserved,
            output=self.tmp / "estimate.json",
            environment=fx.ENVIRONMENT,
        )
        self.assertEqual(estimate["branches"], 4)
        self.assertGreaterEqual(estimate["rehearsal_elapsed_seconds"], 0.0)
        plan = fx.plan_document(
            self.tmp,
            name="main",
            time_estimate=plan_module.load_time_estimate(self.tmp / "estimate.json"),
        )
        self.assertEqual(plan["start_condition"]["verdict"], plan_module.WITHIN_BUDGET)

    def test_unobserved_difference_fails(self) -> None:
        observed, _, _ = self._run("observed", fx.fake_r_both)
        unobserved, _, _ = self._run("unobserved", fx.fake_r_unobserved_other)
        with self.assertRaisesRegex(EngineAabbProtocolError, "STOP-INVALID"):
            core.compare_rehearsals(observed, unobserved, environment=fx.ENVIRONMENT)

    def test_d_arm_repeat_difference_fails(self) -> None:
        marker = self.tmp / "repeat-marker"
        os.environ["RIICHI_FIRST_TEST_REPEAT_MARKER"] = str(marker)
        self.addCleanup(os.environ.pop, "RIICHI_FIRST_TEST_REPEAT_MARKER", None)
        observed, _, summary = self._run("observed", fx.fake_r_both, fx.fake_d_unstable)
        self.assertEqual(summary["invalid"]["count"], 1)
        unobserved, _, _ = self._run("unobserved", fx.fake_r_unobserved)
        with self.assertRaisesRegex(EngineAabbProtocolError, "STOP-INVALID"):
            core.compare_rehearsals(observed, unobserved, environment=fx.ENVIRONMENT)


class CliTest(unittest.TestCase):
    def test_commands_are_registered(self) -> None:
        from lisjong_arena.heuristic_candidate_engine_aabb import __main__ as cli

        for command in (
            "riichi-first-plan",
            "riichi-first-run",
            "riichi-first-verify",
            "riichi-first-rehearsal-compare",
            "riichi-first-estimate",
        ):
            with self.subTest(command=command), self.assertRaises(SystemExit) as stop:
                cli.main([command, "--help"])
            self.assertEqual(stop.exception.code, 0)


if __name__ == "__main__":
    unittest.main()
