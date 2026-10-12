from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Any

MAIN_REF_NAMES = {"main", "refs/heads/main"}
MAIN_SOURCE_ALIASES = {"HEAD", "@"}
FORBIDDEN_SUFFIXES = {
    ".ckpt",
    ".key",
    ".onnx",
    ".p12",
    ".pem",
    ".pfx",
    ".pt",
    ".pth",
    ".safetensors",
}
ALLOWED_ENV_EXAMPLES = {".env.example", ".env.sample", ".env.template"}
FORBIDDEN_BASENAMES = {
    ".env",
    ".envrc",
    ".pypirc",
    "id_ed25519",
    "id_rsa",
    "secrets.toml",
}
PROTECTED_ACTIONS = {"direct-main-push", "commit-artifacts", "pre-pr"}
# lisbun/lisjong-arena#479: an autonomous loop session sets this variable to a
# non-empty value. Such a session must not commit changes to the evaluation
# system (protocol, classification / decision rule, lock, seed registry).
LOOP_SESSION_ENV = "LISJONG_LOOP_SESSION"
ARENA_PACKAGE_PREFIX = "src/lisjong_arena/"
LOOP_PROTECTED_FILES = {
    ".claude/settings.json",
    "src/lisjong_arena/measurement_allocation_guard.py",
    "src/lisjong_arena/seed-ledger.json",
    "src/lisjong_arena/seed_registry.py",
}
# The hooks themselves are protected so that a loop session cannot commit the
# guard away.
LOOP_PROTECTED_PREFIXES = (".claude/hooks/", ".github/workflows/")
LOOP_PROTECTED_MODULE = re.compile(
    r"(?:protocol|classification|decision_rule|lock|gate"
    r"|\w+_lock|\w*_gate(?:_\w+)?)\.py"
)
SHELL_SEPARATORS = re.compile(r"(?:&&|\|\||;|\n)")
GIT_PUSH = re.compile(r"(?<![\w-])git\s+push(?:\s|$)", re.IGNORECASE)


class GuardError(RuntimeError):
    """Raised when a protected operation cannot be inspected safely."""


def _deny(reason: str) -> None:
    payload = {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }
    print(json.dumps(payload, ensure_ascii=False))


def _read_hook_input() -> tuple[str, str, Path]:
    try:
        payload: Any = json.loads(sys.stdin.read())
    except json.JSONDecodeError as exc:
        raise GuardError("Malformed Claude Code hook input.") from exc

    if not isinstance(payload, dict):
        raise GuardError("Claude Code hook input must be a JSON object.")

    tool_name = payload.get("tool_name")
    tool_input = payload.get("tool_input")
    if tool_name not in {"Bash", "PowerShell"} or not isinstance(tool_input, dict):
        raise GuardError("Unexpected tool input for protected shell operation.")

    command = tool_input.get("command")
    if not isinstance(command, str) or not command.strip():
        raise GuardError("Protected shell operation is missing its command.")

    cwd_raw = payload.get("cwd") or os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()
    if not isinstance(cwd_raw, str) or not cwd_raw:
        raise GuardError("Unable to determine repository working directory.")

    return tool_name, command, Path(cwd_raw)


def _run_git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
    )


def _git_output(cwd: Path, *args: str) -> str:
    completed = _run_git(cwd, *args)
    if completed.returncode != 0:
        raise GuardError(f"Git inspection failed: git {' '.join(args)}")
    return completed.stdout


def _current_branch(cwd: Path) -> str:
    branch = _git_output(cwd, "branch", "--show-current").strip()
    if not branch:
        raise GuardError("Cannot validate a default push from detached HEAD.")
    return branch


def _push_tokens(segment: str) -> list[str]:
    match = GIT_PUSH.search(segment)
    if match is None:
        return []

    invocation = segment[match.start() :].strip()
    try:
        return shlex.split(invocation, posix=True)
    except ValueError as exc:
        raise GuardError("Cannot safely parse git push command.") from exc


def _positionals_after_push(tokens: list[str]) -> list[str]:
    if len(tokens) < 2 or tokens[0].lower() != "git" or tokens[1].lower() != "push":
        return []

    takes_value = {"--exec", "--push-option", "--receive-pack", "--repo", "-o"}
    positionals: list[str] = []
    skip_next = False
    for token in tokens[2:]:
        if skip_next:
            skip_next = False
            continue
        if token in takes_value:
            skip_next = True
            continue
        if token.startswith("-"):
            continue
        positionals.append(token)
    return positionals


def _targets_main(refspec: str) -> bool:
    normalized = refspec.lstrip("+")
    if normalized in MAIN_REF_NAMES:
        return True
    if ":" not in normalized:
        return False
    destination = normalized.rsplit(":", 1)[1]
    return destination in MAIN_REF_NAMES


def is_direct_main_push(command: str, current_branch: str) -> bool:
    for segment in SHELL_SEPARATORS.split(command):
        tokens = _push_tokens(segment)
        if not tokens:
            continue

        args = tokens[2:]
        if any(arg in {"--dry-run", "-n"} for arg in args):
            continue
        if any(arg in {"--all", "--mirror"} for arg in args):
            return True

        remote_via_option = "--repo" in args or any(
            arg.startswith("--repo=") for arg in args
        )
        positionals = _positionals_after_push(tokens)
        refspecs = positionals if remote_via_option else positionals[1:]
        if any(_targets_main(refspec) for refspec in refspecs):
            return True
        if current_branch == "main" and any(
            refspec.lstrip("+") in MAIN_SOURCE_ALIASES for refspec in refspecs
        ):
            return True

        # With no explicit refspec, git pushes the current branch according to
        # push.default/upstream configuration. A default push from main must not
        # be allowed to bypass the repository's no-direct-main-push policy.
        # --tags without a refspec is tag-only and therefore does not update main.
        if not refspecs and current_branch == "main" and "--tags" not in args:
            return True

    return False


def is_forbidden_path(path: str) -> bool:
    normalized = path.replace("\\", "/")
    name = normalized.rsplit("/", 1)[-1].lower()

    if name in ALLOWED_ENV_EXAMPLES:
        return False
    if name in FORBIDDEN_BASENAMES or name.startswith(".env."):
        return True
    return Path(name).suffix.lower() in FORBIDDEN_SUFFIXES


def is_loop_session(environ: dict[str, str] | None = None) -> bool:
    env = os.environ if environ is None else environ
    return bool(env.get(LOOP_SESSION_ENV, "").strip())


def is_loop_protected_path(path: str) -> bool:
    normalized = path.replace("\\", "/")
    if normalized in LOOP_PROTECTED_FILES:
        return True
    if normalized.startswith(LOOP_PROTECTED_PREFIXES):
        return True
    if not normalized.startswith(ARENA_PACKAGE_PREFIX):
        return False

    *packages, name = normalized[len(ARENA_PACKAGE_PREFIX) :].split("/")
    if any(package.endswith("_lock") for package in packages):
        return True
    return LOOP_PROTECTED_MODULE.fullmatch(name) is not None


def _nul_paths(output: str) -> list[str]:
    return [item for item in output.split("\0") if item]


def _candidate_commit_paths(cwd: Path, command: str) -> list[str]:
    staged = _nul_paths(
        _git_output(
            cwd,
            "diff",
            "--cached",
            "--name-only",
            "--diff-filter=ACMR",
            "-z",
        )
    )
    candidates = set(staged)

    tokens: list[str]
    try:
        tokens = shlex.split(command, posix=True)
    except ValueError as exc:
        raise GuardError("Cannot safely parse git commit command.") from exc

    commit_all = "--all" in tokens or any(
        token.startswith("-") and not token.startswith("--") and "a" in token[1:]
        for token in tokens
    )
    if commit_all:
        tracked_changes = _nul_paths(
            _git_output(cwd, "diff", "--name-only", "--diff-filter=ACMR", "-z")
        )
        candidates.update(tracked_changes)

    return sorted(candidates)


def _loop_session_commit_paths(cwd: Path) -> list[str]:
    # Unlike the artifact check, deletions and both sides of a rename count,
    # and tracked worktree changes are included even without --all so that a
    # pathspec commit (git commit -- <path>) cannot slip past the staged view.
    changed = ("diff", "--name-only", "--no-renames", "-z")
    paths = set(_nul_paths(_git_output(cwd, *changed, "--cached")))
    paths.update(_nul_paths(_git_output(cwd, *changed)))
    return sorted(paths)


def _deny_loop_protected(paths: list[str], operation: str) -> None:
    protected = [path for path in paths if is_loop_protected_path(path)]
    if protected:
        raise GuardError(
            f"{operation} changes evaluation-system files that a loop session "
            f"({LOOP_SESSION_ENV}) must not modify: {', '.join(protected)}"
        )


def _find_base_ref(cwd: Path) -> str:
    for candidate in ("origin/main", "main"):
        completed = _run_git(cwd, "rev-parse", "--verify", "--quiet", candidate)
        if completed.returncode == 0:
            return candidate
    raise GuardError("Cannot locate origin/main or main for pre-PR validation.")


def _branch_changed_paths(cwd: Path, merge_base: str) -> list[str]:
    return _nul_paths(
        _git_output(
            cwd,
            "diff",
            "--name-only",
            "--diff-filter=ACMR",
            "-z",
            f"{merge_base}..HEAD",
        )
    )


def _run_check(cwd: Path, command: list[str]) -> None:
    completed = subprocess.run(
        command,
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        display = " ".join(command)
        raise GuardError(f"Pre-PR deterministic check failed: {display}")


def _guard_direct_main_push(command: str, cwd: Path) -> None:
    branch = _current_branch(cwd)
    if is_direct_main_push(command, branch):
        raise GuardError(
            "Direct pushes that can update main are blocked. "
            "Push the Issue branch instead."
        )


def _guard_commit_artifacts(command: str, cwd: Path) -> None:
    forbidden = [
        path
        for path in _candidate_commit_paths(cwd, command)
        if is_forbidden_path(path)
    ]
    if forbidden:
        paths = ", ".join(forbidden)
        raise GuardError(
            f"Commit contains forbidden artifact/credential file class: {paths}"
        )
    if is_loop_session():
        _deny_loop_protected(_loop_session_commit_paths(cwd), "Commit")


def _guard_pre_pr(cwd: Path) -> None:
    base_ref = _find_base_ref(cwd)
    merge_base = _git_output(cwd, "merge-base", base_ref, "HEAD").strip()
    if not merge_base:
        raise GuardError("Cannot determine merge base for pre-PR validation.")

    forbidden = [
        path
        for path in _branch_changed_paths(cwd, merge_base)
        if is_forbidden_path(path)
    ]
    if forbidden:
        paths = ", ".join(forbidden)
        raise GuardError(
            f"PR contains forbidden artifact/credential file class: {paths}"
        )
    if is_loop_session():
        _deny_loop_protected(
            _nul_paths(
                _git_output(
                    cwd,
                    "diff",
                    "--name-only",
                    "--no-renames",
                    "-z",
                    f"{merge_base}..HEAD",
                )
            ),
            "PR",
        )

    _run_check(cwd, ["git", "diff", "--check", f"{merge_base}..HEAD"])
    _run_check(cwd, [sys.executable, "-m", "ruff", "format", "--check", "."])
    _run_check(cwd, [sys.executable, "-m", "ruff", "check", "."])


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 1 or args[0] not in PROTECTED_ACTIONS:
        print(
            "usage: workflow_guard.py <direct-main-push|commit-artifacts|pre-pr>",
            file=sys.stderr,
        )
        return 2

    action = args[0]
    try:
        _tool_name, command, cwd = _read_hook_input()
        if action == "direct-main-push":
            _guard_direct_main_push(command, cwd)
        elif action == "commit-artifacts":
            _guard_commit_artifacts(command, cwd)
        else:
            _guard_pre_pr(cwd)
    except GuardError as exc:
        _deny(str(exc))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
