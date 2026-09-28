"""Bounded calibration semantics and AWS safety ordering; never calls AWS."""

from __future__ import annotations

import copy
import json
import shutil
import subprocess
import unittest
from concurrent.futures import Future
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

import test_heuristic_candidate_423 as fixtures

from lisjong_arena.heuristic_candidate_aabb import calibration423 as cal
from lisjong_arena.heuristic_candidate_aabb import rust423
from lisjong_arena.seed_registry import (
    allocation_binding,
    new_ledger,
    reserve_allocation,
)

ROOT = Path(__file__).resolve().parents[1]
AWS = ROOT / "scripts/aws"
SEEDS = tuple(range(423000, 423008))


def documents():
    ledger, allocation = reserve_allocation(
        new_ledger(),
        owner_issue="lisbun/lisjong-arena#423",
        protocol=cal.PROTOCOL,
        seed_domain=cal.SEED_DOMAIN,
        purpose="test timing only",
        population=cal.POPULATION,
        split="DEVELOPMENT",
        seeds=SEEDS,
        arena_revision=fixtures.provenance().lisjong_arena_revision,
        protocol_revision=cal.PROTOCOL,
        provenance_reference="test",
        allocation_timestamp="2026-09-29T00:00:00Z",
    )
    binding = allocation_binding(ledger, allocation["allocation_identity"])
    document = cal._identity(
        {
            "protocol": cal.PROTOCOL,
            "purpose": "DEVELOPMENT timing only; no strength claim",
            "seeds": list(SEEDS),
            "allocation_binding": binding,
            "provenance": fixtures.process()["provenance"],
            "rust_execution": rust423.execution_contract(
                "/tmp/" + rust423.WHEEL_FILENAME
            ),
            "participants": [list(p) for p in rust423.PAIR],
            "workers": 8,
            "instance_type": "c7i.2xlarge",
            "vcpu": 8,
            "platform": "Linux-x86_64",
            "run_id": "fixture",
        }
    )
    return ledger, document


def block(seed):
    start = datetime(2026, 9, 29, tzinfo=UTC) + timedelta(seconds=SEEDS.index(seed) * 2)
    return {
        "seed": seed,
        "started_at": start.isoformat().replace("+00:00", "Z"),
        "completed_at": (start + timedelta(seconds=10))
        .isoformat()
        .replace("+00:00", "Z"),
        "duration_seconds": 10.0,
        "cpu_seconds": 9.0,
        "peak_process_rss_kib": 102400,
        "process": fixtures.process(200 + SEEDS.index(seed) % 8),
        "game_native_calls": 10,
    }


def receipts(document):
    return cal._identity(
        {
            "lock_identity": document["identity"],
            "parent": fixtures.process(),
            "blocks": [block(seed) for seed in SEEDS],
            "wall_seconds": 50.0,
        }
    )


class CalibrationTest(unittest.TestCase):
    def test_development_allocation_is_separate_from_formal(self):
        ledger, document = documents()
        cal.require_allocation(ledger, document["allocation_binding"], SEEDS)
        for seeds in (SEEDS[:4], SEEDS + (423008,), SEEDS[:7] + SEEDS[:1]):
            with self.assertRaises(ValueError):
                cal.require_allocation(ledger, document["allocation_binding"], seeds)
        with self.assertRaises(ValueError):
            from lisjong_arena.heuristic_candidate_aabb.lock import (
                require_seed_allocation,
            )

            require_seed_allocation(
                ledger, document["allocation_binding"], SEEDS, event=423
            )

    def test_allocation_revision_must_match_execution(self):
        ledger, document = documents()
        with self.assertRaises(ValueError):
            cal.require_allocation(
                ledger, document["allocation_binding"], SEEDS, arena_revision="f" * 40
            )

    def test_timing_summary_has_no_strength_data_and_rejects_incomplete_or_tampered(
        self,
    ):
        _, document = documents()
        raw = receipts(document)
        summary = cal.summarize(document, raw)
        self.assertEqual(summary["hanchan"], 32)
        self.assertEqual(summary["workers_observed"], 8)
        self.assertEqual(summary["max_process_rss_mib"], 100)
        self.assertGreater(summary["provisional_400_hanchan_seconds"], 0)
        for key in ("scores", "ranks", "seat_results", "candidate_result", "primary"):
            self.assertNotIn(key, json.dumps(raw))
        for mutate in (
            lambda r: r["blocks"].pop(),
            lambda r: r["blocks"].reverse(),
            lambda r: r["blocks"][0].update(game_native_calls=0),
            lambda r: r["blocks"][0].update(cpu_seconds=float("nan")),
            lambda r: r["blocks"][0].update(peak_process_rss_kib=-1),
            lambda r: r["blocks"][0]["process"]["backend"].update(pid=100),
            lambda r: r["blocks"][0]["process"]["provenance"].update(
                lisjong_revision="e" * 40
            ),
            lambda r: r.update(wall_seconds=1),
            lambda r: r["blocks"][0].update(scores=[25000] * 4),
        ):
            bad = copy.deepcopy(raw)
            mutate(bad)
            with self.assertRaises(ValueError):
                cal.summarize(document, cal._identity(bad))

    def test_lock_rejects_other_pair_machine_and_worker_overcommit(self):
        _, document = documents()
        for key, value in (
            ("participants", []),
            ("vcpu", 16),
            ("workers", 9),
            ("instance_type", "t3.small"),
            ("platform", "Windows-AMD64"),
        ):
            with self.subTest(key=key), self.assertRaises(ValueError):
                cal.validate_lock(cal._identity({**document, key: value}))

    def test_timed_block_drops_scores_and_preserves_native_proof(self):
        seed = SEEDS[0]
        with (
            mock.patch.object(
                cal,
                "_run_block",
                return_value=(
                    object(),
                    {"process": fixtures.process(200), "game_native_calls": 42},
                ),
            ),
            mock.patch.object(cal.time, "monotonic", side_effect=(0, 12)),
            mock.patch.object(cal.time, "process_time", side_effect=(0, 10)),
        ):
            row = cal._timed_block(seed, "wheel", fixtures.process()["provenance"])
        self.assertEqual(row["game_native_calls"], 42)
        self.assertEqual(row["duration_seconds"], 12)
        self.assertNotIn("rows_identity", row)
        self.assertTrue(row["started_at"].endswith("Z"))

    def test_run_durable_receipts_strict_readback_and_no_overwrite(self):
        ledger, document = documents()
        pool = mock.MagicMock()
        pool.__enter__.return_value = pool

        def submit(fn, seed, wheel, provenance):
            f = Future()
            f.set_result(block(seed))
            return f

        pool.submit.side_effect = submit
        with (
            TemporaryDirectory() as tmp,
            mock.patch.object(cal, "_require_environment_consistent"),
            mock.patch.object(
                cal,
                "require_clean_arena_head",
                return_value=fixtures.provenance().lisjong_arena_revision,
            ),
            mock.patch.object(cal, "require_merged_arena_revision"),
            mock.patch.object(cal, "verify_process", return_value=fixtures.process()),
            mock.patch.object(cal.os, "cpu_count", return_value=8),
            mock.patch.object(cal.platform, "system", return_value="Linux"),
            mock.patch.object(cal.platform, "machine", return_value="x86_64"),
            mock.patch.object(cal, "ProcessPoolExecutor", return_value=pool),
            mock.patch.object(cal.time, "monotonic", side_effect=(0, 50)),
        ):
            args = dict(
                seeds=SEEDS,
                ledger=ledger,
                binding=document["allocation_binding"],
                wheel="/tmp/" + rust423.WHEEL_FILENAME,
                workers=8,
                run_id="fixture",
                output=tmp,
                instance_type="c7i.2xlarge",
            )
            summary = cal.run(**args)
            self.assertEqual(cal.verify(tmp), summary)
            self.assertEqual(
                len(list((Path(tmp) / "receipts").glob("*.receipt.json"))), 8
            )
            with self.assertRaises(ValueError):
                cal.run(**args)
            path = Path(tmp) / "calibration-result.json"
            bad = json.loads(path.read_text())
            bad["wall_seconds"] = 1
            path.write_text(json.dumps(cal._identity(bad)))
            with self.assertRaises(ValueError):
                cal.verify(tmp)

    def test_failed_worker_terminates_pool_without_result(self):
        ledger, document = documents()
        pool = mock.MagicMock()
        pool.__enter__.return_value = pool
        failed = Future()
        failed.set_exception(RuntimeError("native mismatch"))
        pool.submit.return_value = failed
        with (
            TemporaryDirectory() as tmp,
            mock.patch.object(cal, "_require_environment_consistent"),
            mock.patch.object(
                cal,
                "require_clean_arena_head",
                return_value=fixtures.provenance().lisjong_arena_revision,
            ),
            mock.patch.object(cal, "require_merged_arena_revision"),
            mock.patch.object(cal, "verify_process", return_value=fixtures.process()),
            mock.patch.object(cal.os, "cpu_count", return_value=8),
            mock.patch.object(cal.platform, "system", return_value="Linux"),
            mock.patch.object(cal.platform, "machine", return_value="x86_64"),
            mock.patch.object(cal, "ProcessPoolExecutor", return_value=pool),
        ):
            with self.assertRaises(RuntimeError):
                cal.run(
                    seeds=SEEDS,
                    ledger=ledger,
                    binding=document["allocation_binding"],
                    wheel="/tmp/" + rust423.WHEEL_FILENAME,
                    workers=8,
                    run_id="fixture",
                    output=tmp,
                    instance_type="c7i.2xlarge",
                )
            pool.terminate_workers.assert_called_once()
            self.assertFalse((Path(tmp) / "calibration-result.json").exists())


class AwsContractTest(unittest.TestCase):
    def test_preflight_gates_wheel_allocation_cost_and_dryrun_before_billing(self):
        text = (AWS / "run-heuristic-calibration-423.ps1").read_text()
        launch = text.index('"create-bucket"')
        for gate in (
            "verify_wheel_file",
            "require_allocation",
            "merge-base --is-ancestor",
            "Get-PricingDimension -Filters",
            '"--dry-run"',
            "exposure_plus_margin_usd -gt",
        ):
            self.assertLess(text.index(gate), launch)
        self.assertIn("[ValidateRange(1800, 3600)][int]$FailSafeSeconds = 3600", text)
        self.assertIn("$SeedBlockCount = 8", text)
        self.assertNotIn("SecondsPerHanchan", text)
        self.assertIn("scientific = $false", text)
        self.assertIn(
            'Action = @("s3:GetObject"); Resource = @("arn:aws:s3:::$bucket/$runId/input/$WheelFilename")',
            text,
        )

    def test_state_precedes_wait_and_tags_and_collect_verifies_before_delete(self):
        text = (AWS / "run-heuristic-calibration-423.ps1").read_text()
        launch = text[text.index("# Launch (billable)") :]
        self.assertLess(
            launch.index('"state.json"'), launch.index('"instance-running"')
        )
        self.assertLess(
            launch.index("$eventSubmitted = $true"), launch.index('"create-tags"')
        )
        self.assertIn("No recorded command id;", text)
        collect = text[
            text.index("function Invoke-Collect") : text.index(
                'if ($Action -eq "Collect")'
            )
        ]
        self.assertLess(
            collect.index('"instance-terminated"'),
            collect.index("# Download every object"),
        )
        self.assertLess(
            collect.index("calibration423"),
            collect.index("Remove-TransferBucket -Bucket $bucket"),
        )
        self.assertIn("if (-not $KeepBucket -and -not $remoteFailed)", collect)

    def test_bootstrap_installs_verified_wheel_before_rust_games(self):
        path = AWS / "bootstrap-heuristic-calibration-423.sh"
        text = path.read_text()
        subprocess.run(["bash", "-n", str(path)], check=True)
        self.assertLess(
            text.index("verify_wheel_file"),
            text.index("pip install --no-deps --force-reinstall"),
        )
        self.assertLess(
            text.index("export LISJONG_SHANTEN_BACKEND=rust"),
            text.index("calibration423 run"),
        )
        self.assertLess(
            text.index("calibration423 run"), text.index("calibration423 verify")
        )
        self.assertNotIn("heuristic_candidate_aabb run", text)
        self.assertIn("receipts.tar.gz", text)
        self.assertIn("merge-base --is-ancestor", text)

    def test_powershell_parser(self):
        pwsh = shutil.which("pwsh")
        if not pwsh:
            self.skipTest("PowerShell is unavailable")
        path = AWS / "run-heuristic-calibration-423.ps1"
        subprocess.run(
            [
                pwsh,
                "-NoProfile",
                "-Command",
                f"$e=$null; [void][System.Management.Automation.Language.Parser]::ParseFile('{path}',[ref]$null,[ref]$e); if (@($e).Count) {{ $e; exit 1 }}",
            ],
            check=True,
        )


if __name__ == "__main__":
    unittest.main()
