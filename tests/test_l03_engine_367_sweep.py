        self.assertIn("[double]$CostBudgetUsd = 5.0", text)
        self.assertIsNone(re.search(r"(?i)spot", text))
        for revision in driver.FROZEN_REVISIONS.values():
            self.assertIn(revision, text)
        # Pricing, dry-run and fail-safe precede the workload submission.
        self.assertLess(
            text.index("Get-PricingDimension -Filters"), text.index('"create-bucket"')
        )
        self.assertLess(text.index('"--dry-run"'), text.index('"create-bucket"'))
        self.assertLess(
            text.index("LISJONG_367_FAILSAFE_ARMED"),
            text.index("$sweepSubmitted = $true"),
        )

    def test_launcher_parses_ec2_termination_gmt_as_utc(self):
        text = _LAUNCHER.read_text(encoding="utf-8")
        self.assertIn("[Globalization.DateTimeStyles]::AssumeUniversal", text)
        self.assertIn("[Globalization.DateTimeStyles]::AdjustToUniversal", text)
        self.assertNotIn(
            "[datetime]::Parse($Matches[1], $invariant).ToUniversalTime()",
            text,
        )

    def test_launcher_has_valid_powershell_syntax_when_pwsh_is_available(self):
        pwsh = shutil.which("pwsh")
        if pwsh is None:
            self.skipTest("pwsh is unavailable")
        script = (
            "$e=$null; [void][System.Management.Automation.Language.Parser]::ParseFile("
            f"'{_LAUNCHER}',[ref]$null,[ref]$e); if (@($e).Count) {{ $e; exit 1 }}"
        )
        subprocess.run([pwsh, "-NoProfile", "-Command", script], check=True)


if __name__ == "__main__":
    unittest.main()