import hashlib
import io
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from lisjong_arena.mortal_runtime import (
    MortalDockerConfig,
    MortalDockerRuntime,
    MortalResponseTimeoutError,
    MortalRuntimeError,
)

_MODULE = "lisjong_arena.mortal_runtime"


class _RecordingInput(io.StringIO):
    def __init__(self) -> None:
        super().__init__()
        self.flush_calls = 0

    def flush(self) -> None:
        self.flush_calls += 1
        super().flush()


class _FakeProcess:
    def __init__(self, *, stdout: str = "", stderr: str = "") -> None:
        self.stdin = _RecordingInput()
        self.stdout = io.StringIO(stdout)
        self.stderr = io.StringIO(stderr)
        self.wait_calls = 0
        self.kill_calls = 0

    def wait(self, timeout: float) -> int:
        self.wait_calls += 1
        return 0

    def kill(self) -> None:
        self.kill_calls += 1


class _StuckProcess(_FakeProcess):
    def wait(self, timeout: float) -> int:
        self.wait_calls += 1
        if self.wait_calls == 1:
            raise subprocess.TimeoutExpired("docker", timeout)
        return 0


class MortalRuntimeTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.model_path = Path(self.temporary_directory.name) / "mortal.pth"
        self.model_path.write_bytes(b"test-mortal-model")
        absent = SimpleNamespace(returncode=1, stderr="No such container")
        cleanup_patcher = mock.patch(f"{_MODULE}.subprocess.run", return_value=absent)
        self.cleanup_run = cleanup_patcher.start()
        self.addCleanup(cleanup_patcher.stop)

    def config(self, **overrides: object) -> MortalDockerConfig:
        fields = {
            "image": "mortal@sha256:image",
            "implementation_revision": "0cff2b5",
            "model_path": self.model_path,
            "response_timeout_seconds": 0.1,
            "cleanup_timeout_seconds": 0.1,
        }
        fields.update(overrides)
        return MortalDockerConfig(**fields)

    def test_config_records_resolved_model_path_and_sha256(self) -> None:
        config = self.config()

        self.assertEqual(config.model_path, self.model_path.resolve())
        self.assertEqual(
            config.model_sha256,
            hashlib.sha256(b"test-mortal-model").hexdigest(),
        )

    def test_start_uses_upstream_docker_contract_without_implicit_pull(self) -> None:
        process = _FakeProcess(stdout='{"type":"none"}\n')
        with mock.patch(f"{_MODULE}.subprocess.Popen", return_value=process) as popen:
            runtime = MortalDockerRuntime.start(self.config(), player_id=2)
            response = runtime.request_action(
                ['{"type":"start_game"}', '{"type":"tsumo","actor":2}']
            )
            sent_events = process.stdin.getvalue()
            runtime.close()

        command = popen.call_args.args[0]
        self.assertEqual(
            command[0:5], ["docker", "run", "--interactive", "--rm", "--pull=never"]
        )
        self.assertIn("--name", command)
        self.assertIn("--mount", command)
        self.assertIn("target=/mnt,readonly", command[command.index("--mount") + 1])
        self.assertEqual(command[-2:], ["mortal@sha256:image", "2"])
        self.assertEqual(
            sent_events,
            '{"type":"start_game"}\n{"type":"tsumo","actor":2}\n',
        )
        self.assertEqual(process.stdin.flush_calls, 1)
        self.assertEqual(response, '{"type":"none"}')
        self.cleanup_run.assert_called_once()
        self.assertEqual(
            self.cleanup_run.call_args.args[0][0:3], ["docker", "rm", "--force"]
        )

    def test_launch_failure_is_fail_closed(self) -> None:
        with mock.patch(
            f"{_MODULE}.subprocess.Popen", side_effect=OSError("docker missing")
        ):
            with self.assertRaisesRegex(MortalRuntimeError, "failed to launch"):
                MortalDockerRuntime.start(self.config(), player_id=0)

    def test_unexpected_termination_is_fail_closed(self) -> None:
        process = _FakeProcess(stderr="model load failed\n")
        with mock.patch(f"{_MODULE}.subprocess.Popen", return_value=process):
            runtime = MortalDockerRuntime.start(self.config(), player_id=0)
            with self.assertRaisesRegex(MortalRuntimeError, "terminated") as raised:
                runtime.request_action(['{"type":"start_game"}'])
            runtime.close()

        self.assertIn("model load failed", str(raised.exception))

    def test_timeout_is_finite_and_fail_closed(self) -> None:
        process = _FakeProcess()
        with (
            mock.patch(f"{_MODULE}.subprocess.Popen", return_value=process),
            mock.patch(f"{_MODULE}.threading.Thread.start"),
        ):
            runtime = MortalDockerRuntime.start(self.config(), player_id=0)
            with self.assertRaises(MortalResponseTimeoutError):
                runtime.request_action(['{"type":"start_game"}'])
            runtime.close()

    def test_malformed_and_non_object_responses_are_rejected(self) -> None:
        for response in ("not-json\n", "[]\n", "{}\n"):
            with self.subTest(response=response):
                process = _FakeProcess(stdout=response)
                with mock.patch(f"{_MODULE}.subprocess.Popen", return_value=process):
                    runtime = MortalDockerRuntime.start(self.config(), player_id=0)
                    with self.assertRaises(MortalRuntimeError):
                        runtime.request_action(['{"type":"start_game"}'])
                    runtime.close()

    def test_cleanup_force_removes_a_stuck_container(self) -> None:
        process = _StuckProcess()
        completed = SimpleNamespace(returncode=0, stderr="")
        with (
            mock.patch(f"{_MODULE}.subprocess.Popen", return_value=process),
            mock.patch(f"{_MODULE}.subprocess.run", return_value=completed) as run,
        ):
            runtime = MortalDockerRuntime.start(self.config(), player_id=3)
            runtime.close()

        self.assertEqual(run.call_args.args[0][0:3], ["docker", "rm", "--force"])
        self.assertEqual(process.wait_calls, 2)

    def test_cleanup_failure_is_reported(self) -> None:
        process = _StuckProcess()
        completed = SimpleNamespace(returncode=1, stderr="permission denied")
        with (
            mock.patch(f"{_MODULE}.subprocess.Popen", return_value=process),
            mock.patch(f"{_MODULE}.subprocess.run", return_value=completed),
        ):
            runtime = MortalDockerRuntime.start(self.config(), player_id=1)
            with self.assertRaisesRegex(MortalRuntimeError, "permission denied"):
                runtime.close()


if __name__ == "__main__":
    unittest.main()
