"""Mortal upstream Docker imageをmjai stdin/stdoutで駆動する具体runtime。"""

from __future__ import annotations

import hashlib
import json
import queue
import subprocess
import threading
import uuid
from collections import deque
from dataclasses import dataclass, field
from math import isfinite
from pathlib import Path
from typing import TextIO


class MortalRuntimeError(Exception):
    """Mortal processの起動、応答、終了処理が失敗した場合。"""


class MortalResponseTimeoutError(MortalRuntimeError):
    """decisionに必要なMortal responseが制限時間内に届かなかった場合。"""


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


@dataclass(frozen=True, slots=True)
class MortalDockerConfig:
    """公式Mortal Docker imageを再現可能に起動するための固定設定。"""

    image: str
    implementation_revision: str
    model_path: Path
    response_timeout_seconds: float = 30.0
    docker_executable: str = "docker"
    cleanup_timeout_seconds: float = 5.0
    model_sha256: str = field(init=False)

    def __post_init__(self) -> None:
        for name in ("image", "implementation_revision", "docker_executable"):
            value = getattr(self, name)
            if type(value) is not str:
                raise TypeError(f"{name} must be a str")
            if not value.strip():
                raise ValueError(f"{name} must not be empty")

        model_path = Path(self.model_path).resolve()
        if model_path.name != "mortal.pth":
            raise ValueError("model_path must point to the upstream mortal.pth file")
        if not model_path.is_file():
            raise ValueError(f"model_path is not a file: {model_path}")

        for name in ("response_timeout_seconds", "cleanup_timeout_seconds"):
            value = getattr(self, name)
            if type(value) not in (int, float):
                raise TypeError(f"{name} must be a number")
            if not isfinite(value) or value <= 0:
                raise ValueError(f"{name} must be positive")

        object.__setattr__(self, "model_path", model_path)
        object.__setattr__(
            self, "response_timeout_seconds", float(self.response_timeout_seconds)
        )
        object.__setattr__(
            self, "cleanup_timeout_seconds", float(self.cleanup_timeout_seconds)
        )
        object.__setattr__(self, "model_sha256", _sha256_file(model_path))


_QUEUE_EOF = object()


class MortalDockerRuntime:
    """1 game専用のMortal Docker subprocess。

    upstream Dockerfileのentrypointへplayer IDを渡し、model directoryを
    ``/mnt``へread-only mountする。imageの暗黙pullは``--pull=never``で禁止する。
    """

    __slots__ = (
        "_closed",
        "_config",
        "_container_name",
        "_process",
        "_stderr_done",
        "_stderr_lines",
        "_stdout_queue",
        "_threads",
    )

    def __init__(
        self,
        config: MortalDockerConfig,
        process: subprocess.Popen[str],
        *,
        container_name: str,
    ) -> None:
        self._config = config
        self._process = process
        self._container_name = container_name
        self._stdout_queue: queue.Queue[str | object | BaseException] = queue.Queue()
        self._stderr_lines: deque[str] = deque(maxlen=50)
        self._stderr_done = threading.Event()
        self._closed = False
        stdout_thread = threading.Thread(
            target=self._read_stdout,
            name=f"{container_name}-stdout",
            daemon=True,
        )
        stderr_thread = threading.Thread(
            target=self._read_stderr,
            name=f"{container_name}-stderr",
            daemon=True,
        )
        self._threads = (stdout_thread, stderr_thread)
        for thread in self._threads:
            thread.start()

    @classmethod
    def start(
        cls,
        config: MortalDockerConfig,
        *,
        player_id: int,
    ) -> MortalDockerRuntime:
        if not isinstance(config, MortalDockerConfig):
            raise TypeError("config must be a MortalDockerConfig")
        if type(player_id) is not int or player_id not in range(4):
            raise ValueError("player_id must be an int between 0 and 3")

        container_name = f"lisjong-arena-mortal-{uuid.uuid4().hex}"
        mount = f"type=bind,source={config.model_path.parent},target=/mnt,readonly"
        command = [
            config.docker_executable,
            "run",
            "--interactive",
            "--rm",
            "--pull=never",
            "--name",
            container_name,
            "--mount",
            mount,
            config.image,
            str(player_id),
        ]
        try:
            process = subprocess.Popen(
                command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="strict",
                bufsize=1,
            )
        except OSError as exc:
            raise MortalRuntimeError("failed to launch Mortal Docker process") from exc
        if process.stdin is None or process.stdout is None or process.stderr is None:
            process.kill()
            raise MortalRuntimeError("Mortal Docker process pipes were not created")
        return cls(config, process, container_name=container_name)

    def _read_stdout(self) -> None:
        stdout = self._process.stdout
        assert stdout is not None
        try:
            for line in stdout:
                self._stdout_queue.put(line.rstrip("\r\n"))
        except BaseException as exc:
            self._stdout_queue.put(exc)
        finally:
            self._stdout_queue.put(_QUEUE_EOF)

    def _read_stderr(self) -> None:
        stderr = self._process.stderr
        assert stderr is not None
        try:
            for line in stderr:
                self._stderr_lines.append(line.rstrip("\r\n"))
        except BaseException as exc:
            self._stderr_lines.append(f"stderr reader failed: {exc}")
        finally:
            self._stderr_done.set()

    def _stderr_summary(self) -> str:
        if not self._stderr_lines:
            return ""
        return f"; stderr: {' | '.join(self._stderr_lines)}"

    def request_action(self, events: list[str]) -> str:
        """全new_eventsを送信・flush後、decision用responseを1つだけ待つ。"""
        if self._closed:
            raise MortalRuntimeError("Mortal Docker runtime is already closed")
        if not isinstance(events, list) or not events:
            raise MortalRuntimeError("Mortal decision requires non-empty new_events")
        if any(type(event) is not str or not event.strip() for event in events):
            raise MortalRuntimeError("Mortal new_events must contain non-empty strings")

        stdin = self._process.stdin
        assert stdin is not None
        try:
            for event in events:
                stdin.write(event)
                stdin.write("\n")
            stdin.flush()
        except OSError as exc:
            raise MortalRuntimeError(
                "failed to send events to Mortal" + self._stderr_summary()
            ) from exc

        try:
            response = self._stdout_queue.get(
                timeout=self._config.response_timeout_seconds
            )
        except queue.Empty:
            raise MortalResponseTimeoutError(
                "Mortal did not return an action within "
                f"{self._config.response_timeout_seconds:g} seconds"
                + self._stderr_summary()
            ) from None

        if response is _QUEUE_EOF:
            self._stderr_done.wait(
                timeout=min(self._config.cleanup_timeout_seconds, 0.5)
            )
            raise MortalRuntimeError(
                "Mortal terminated before returning an action" + self._stderr_summary()
            )
        if isinstance(response, BaseException):
            raise MortalRuntimeError("failed to read Mortal stdout") from response
        assert isinstance(response, str)
        try:
            decoded = json.loads(response)
        except json.JSONDecodeError as exc:
            raise MortalRuntimeError("Mortal returned malformed JSON") from exc
        if type(decoded) is not dict or type(decoded.get("type")) is not str:
            raise MortalRuntimeError("Mortal response must be an mjai JSON object")
        return response

    def close(self) -> None:
        """stdin EOFで正常終了を促し、名前付きcontainerの不在まで確認する。"""
        if self._closed:
            return
        self._closed = True
        errors: list[str] = []

        stdin: TextIO | None = self._process.stdin
        if stdin is not None:
            try:
                stdin.close()
            except OSError as exc:
                errors.append(f"failed to close Mortal stdin: {exc}")

        client_timed_out = False
        try:
            self._process.wait(timeout=self._config.cleanup_timeout_seconds)
        except subprocess.TimeoutExpired:
            client_timed_out = True

        try:
            cleanup = subprocess.run(
                [
                    self._config.docker_executable,
                    "rm",
                    "--force",
                    self._container_name,
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=self._config.cleanup_timeout_seconds,
                check=False,
            )
            cleanup_error = cleanup.stderr.strip()
            already_absent = (
                "No such container" in cleanup_error
                or "No such object" in cleanup_error
            )
            if cleanup.returncode != 0 and not already_absent:
                errors.append(
                    "docker rm --force failed: "
                    + (cleanup_error or f"exit code {cleanup.returncode}")
                )
        except (OSError, subprocess.TimeoutExpired) as exc:
            errors.append(f"failed to remove Mortal container: {exc}")

        if client_timed_out:
            try:
                self._process.wait(timeout=self._config.cleanup_timeout_seconds)
            except subprocess.TimeoutExpired:
                self._process.kill()
                try:
                    self._process.wait(timeout=self._config.cleanup_timeout_seconds)
                except subprocess.TimeoutExpired:
                    errors.append("Mortal Docker client did not terminate after kill")

        if errors:
            raise MortalRuntimeError("; ".join(errors))


__all__ = [
    "MortalDockerConfig",
    "MortalDockerRuntime",
    "MortalResponseTimeoutError",
    "MortalRuntimeError",
]
