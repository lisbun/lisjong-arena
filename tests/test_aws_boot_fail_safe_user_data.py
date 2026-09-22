"""The launch-time fail-safe must hold the EC2 launch clock, not cloud-init's.

``systemd-run --on-active`` counts from the moment cloud-init executes the user
data, which is some way into the first boot cycle. The admitted budget is a
launch-clock window, so these tests execute the rendered script under a stubbed
IMDS and assert the deadline it actually arms.
"""

from __future__ import annotations

import base64
import shutil
import stat
import subprocess
import tempfile
import time
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

from lisjong_arena import aws_operational_calibration as calibration

_GIT_BASH = Path(r"C:\Program Files\Git\bin\bash.exe")
_WINDOW = 16_500


def _bash():
    if _GIT_BASH.is_file():
        return str(_GIT_BASH)
    return shutil.which("bash")


def _posix(path: Path) -> str:
    """Return a path the shell itself can use, including under Git bash."""

    text = path.as_posix()
    if len(text) > 1 and text[1] == ":":
        return "/" + text[0].lower() + text[2:]
    return text


class BootFailSafeRenderingTest(unittest.TestCase):
    def test_the_script_is_an_lf_only_shebang_script(self):
        script = calibration.render_boot_fail_safe_user_data(_WINDOW)
        self.assertTrue(script.startswith("#!/bin/bash\n"))
        self.assertNotIn("\r", script)
        self.assertTrue(script.endswith("\n"))

    def test_the_script_resolves_the_ec2_launch_clock(self):
        script = calibration.render_boot_fail_safe_user_data(_WINDOW)
        # IMDSv2 token, then the instance identity document's pendingTime.
        self.assertIn("latest/api/token", script)
        self.assertIn("X-aws-ec2-metadata-token-ttl-seconds", script)
        self.assertIn("latest/dynamic/instance-identity/document", script)
        self.assertIn("pendingTime", script)
        # The armed window is the remainder of the launch-clock deadline.
        self.assertIn(f"WINDOW={_WINDOW}", script)
        self.assertIn("DEADLINE=$((LAUNCH_EPOCH + WINDOW))", script)
        self.assertIn("REMAINING=$((DEADLINE - $(date -u +%s)))", script)
        self.assertIn('--on-active="${REMAINING}s"', script)
        self.assertNotIn(f'--on-active="{_WINDOW}s"', script)

    def test_rejects_an_invalid_window_or_unsafe_substitution(self):
        for window in (0, -1, 1.5):
            with self.subTest(window=window):
                with self.assertRaises(calibration.AwsOperationalCalibrationError):
                    calibration.render_boot_fail_safe_user_data(window)
        with self.assertRaises(calibration.AwsOperationalCalibrationError):
            calibration.render_boot_fail_safe_user_data(_WINDOW, unit="a'b")

    def test_the_cli_emits_the_same_script(self):
        import contextlib
        import io

        stream = io.StringIO()
        with contextlib.redirect_stdout(stream):
            self.assertEqual(
                0,
                calibration.main(
                    [
                        "boot-fail-safe-user-data",
                        "--window-seconds",
                        str(_WINDOW),
                        "--base64",
                    ]
                ),
            )
        decoded = base64.b64decode(stream.getvalue().strip()).decode("utf-8")
        self.assertEqual(calibration.render_boot_fail_safe_user_data(_WINDOW), decoded)
        self.assertNotIn("\r", decoded)


class BootFailSafeExecutionTest(unittest.TestCase):
    """Run the rendered script with a stubbed IMDS and a stubbed poweroff."""

    def setUp(self):
        self.bash = _bash()
        if self.bash is None:
            self.skipTest("bash is unavailable")
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, True)
        self.stubs = self.root / "stubs"
        self.stubs.mkdir()
        self.calls = self.root / "calls.txt"
        self.deadline_file = self.root / "boot-failsafe.env"
        self.poweroff = self.root / "poweroff.sh"
        recorder = _posix(self.calls)
        self._write(
            self.poweroff,
            f'#!/bin/sh\nprintf "poweroff\\n" >>"{recorder}"\nexit 0\n',
        )
        self._write(
            self.stubs / "systemd-run",
            f'#!/bin/sh\nprintf "systemd-run %s\\n" "$*" >>"{recorder}"\nexit 0\n',
        )

    def _write(self, path, content):
        path.write_text(content, encoding="utf-8", newline="\n")
        path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)

    def _stub_imds(self, pending_time):
        """Stub curl so the token call and the identity document both answer.

        The responses are files the stub simply ``cat``s. Nothing is passed
        through a shell quoting or ``printf`` escape, because ``/bin/sh`` is
        dash on the CI runner and bash elsewhere, and the two disagree about
        backslash escapes in a format string.
        """
        if pending_time is None:
            self._write(self.stubs / "curl", "#!/bin/sh\nexit 22\n")
            return
        token_file = self.root / "imds-token"
        document_file = self.root / "imds-document"
        token_file.write_text("AQAE-token", encoding="utf-8", newline="\n")
        document_file.write_text(
            '{\n  "instanceId" : "i-0123456789abcdef0",\n'
            f'  "pendingTime" : "{pending_time}"\n}}\n',
            encoding="utf-8",
            newline="\n",
        )
        self._write(
            self.stubs / "curl",
            "#!/bin/sh\n"
            'case "$*" in\n'
            f'  *api/token*) cat "{_posix(token_file)}"; exit 0 ;;\n'
            f'  *instance-identity/document*) cat "{_posix(document_file)}"; '
            "exit 0 ;;\n"
            "esac\n"
            "exit 22\n",
        )

    def _run(self, *, pending_time, window=_WINDOW):
        self._stub_imds(pending_time)
        script = self.root / "user-data.sh"
        script.write_bytes(
            calibration.render_boot_fail_safe_user_data(
                window,
                deadline_file=_posix(self.deadline_file),
                poweroff_command=_posix(self.poweroff),
            ).encode("utf-8")
        )
        # PATH is prepended inside the shell, because a Windows-style PATH
        # handed to Git bash from here does not resolve the stubs.
        result = subprocess.run(
            [
                self.bash,
                "-c",
                f'PATH="{_posix(self.stubs)}:$PATH"; exec bash "{_posix(script)}"',
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=120,
        )
        recorded = self.calls.read_text(encoding="utf-8") if self.calls.exists() else ""
        deadline = (
            self.deadline_file.read_text(encoding="utf-8")
            if self.deadline_file.exists()
            else ""
        )
        return result, recorded, deadline

    @staticmethod
    def _stamp(offset_seconds):
        moment = datetime.now(UTC) + timedelta(seconds=offset_seconds)
        return moment.isoformat(timespec="seconds").replace("+00:00", "Z")

    def test_it_arms_only_the_window_left_since_ec2_launch(self):
        # The instance launched 600s ago, so cloud-init may only arm the rest.
        launched_at = int(time.time()) - 600
        result, recorded, deadline = self._run(pending_time=self._stamp(-600))
        # A broken stub would make the script fail closed and power off, so
        # surface what it actually saw rather than just the missing call.
        diagnostic = f"stderr={result.stderr!r} deadline={deadline!r}"
        self.assertIn("systemd-run", recorded, diagnostic)
        self.assertNotIn("poweroff\n", recorded.replace("systemd-run", ""))
        armed = recorded.split("--on-active=")[1].split("s ")[0]
        self.assertLessEqual(int(armed), _WINDOW - 595)
        self.assertGreaterEqual(int(armed), _WINDOW - 615)
        self.assertIn("--unit=lisjong-boot-failsafe", recorded)
        # The recorded deadline is the launch-clock deadline, not now + window.
        recorded_deadline = int(deadline.strip().split("=")[1])
        self.assertAlmostEqual(launched_at + _WINDOW, recorded_deadline, delta=5)

    def test_a_late_cloud_init_does_not_extend_the_deadline(self):
        _result, early, early_deadline = self._run(pending_time=self._stamp(-60))
        self.calls.unlink()
        self.deadline_file.unlink()
        _result, late, late_deadline = self._run(pending_time=self._stamp(-3600))
        early_armed = int(early.split("--on-active=")[1].split("s ")[0])
        late_armed = int(late.split("--on-active=")[1].split("s ")[0])
        # A later cloud-init start leaves strictly less time, never more.
        self.assertLess(late_armed, early_armed)
        self.assertAlmostEqual(early_armed - late_armed, 3540, delta=5)
        self.assertLess(
            int(late_deadline.strip().split("=")[1]),
            int(early_deadline.strip().split("=")[1]),
        )

    def test_an_exhausted_window_powers_off_instead_of_arming_a_timer(self):
        _result, recorded, deadline = self._run(
            pending_time=self._stamp(-(_WINDOW + 60))
        )
        self.assertIn("poweroff", recorded)
        self.assertNotIn("systemd-run", recorded)
        # The deadline it recorded is already in the past, which is exactly why
        # it powered off instead of arming a timer beyond the priced window.
        recorded_deadline = int(deadline.strip().split("=")[1])
        self.assertLess(recorded_deadline, int(time.time()))

    def test_an_unavailable_launch_clock_fails_closed(self):
        # If the launch clock cannot be established the script must not arm a
        # timer it cannot bound; it powers the instance off instead.
        _result, recorded, deadline = self._run(pending_time=None)
        self.assertIn("poweroff", recorded)
        self.assertNotIn("systemd-run", recorded)
        self.assertEqual(
            f"{calibration.BOOT_FAIL_SAFE_DEADLINE_KEY}=0", deadline.strip()
        )


if __name__ == "__main__":
    unittest.main()
