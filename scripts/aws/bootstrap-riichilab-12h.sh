#!/usr/bin/env bash
set -euo pipefail

ARENA_REVISION=""
REGION="ap-northeast-1"
SECRET_ID="lisjong/riichilab/lisjong-dev-token"
DURATION_SECONDS="43200"
WORK_ROOT="/var/lib/lisjong-riichilab-313"
REPOSITORY_URL="https://github.com/lisbun/lisjong-arena.git"
PROFILE="lisjong-dev"
POLICY="MechanismRiichiDefenseYakuhaiCallPolicy"
# Optional live spectating (Issue #381). Both values are required together.
SPECTATE_PORT=""
PLAY_REVISION=""
PLAY_REPOSITORY_URL="https://github.com/lisbun/lisjong-play.git"
# Issue #383: run without a duration bound until the operator stop request.
UNTIL_STOPPED=0
DURATION_GIVEN=0

while (($#)); do
    case "$1" in
        --arena-revision)
            ARENA_REVISION="$2"
            shift 2
            ;;
        --region)
            REGION="$2"
            shift 2
            ;;
        --secret-id)
            SECRET_ID="$2"
            shift 2
            ;;
        --duration-seconds)
            DURATION_SECONDS="$2"
            DURATION_GIVEN=1
            shift 2
            ;;
        --until-stopped)
            UNTIL_STOPPED=1
            shift
            ;;
        --work-root)
            WORK_ROOT="$2"
            shift 2
            ;;
        --spectate-port)
            SPECTATE_PORT="$2"
            shift 2
            ;;
        --play-revision)
            PLAY_REVISION="$2"
            shift 2
            ;;
        *)
            echo "unknown argument: $1" >&2
            exit 2
            ;;
    esac
done

if [[ ! "$ARENA_REVISION" =~ ^[0-9a-f]{40}$ ]]; then
    echo "--arena-revision must be a full lowercase commit SHA" >&2
    exit 2
fi
if [[ "$UNTIL_STOPPED" == "1" ]]; then
    if [[ "$DURATION_GIVEN" == "1" ]]; then
        echo "--until-stopped cannot be combined with --duration-seconds" >&2
        exit 2
    fi
    DURATION_SECONDS=""
elif [[ ! "$DURATION_SECONDS" =~ ^[1-9][0-9]*$ ]]; then
    echo "--duration-seconds must be a positive integer" >&2
    exit 2
fi
SPECTATE=0
if [[ -n "$SPECTATE_PORT" || -n "$PLAY_REVISION" ]]; then
    if [[ ! "$SPECTATE_PORT" =~ ^[1-9][0-9]{3,4}$ ]] ||
        ((SPECTATE_PORT < 1024 || SPECTATE_PORT > 65535)); then
        echo "--spectate-port must be an integer between 1024 and 65535" >&2
        exit 2
    fi
    if [[ ! "$PLAY_REVISION" =~ ^[0-9a-f]{40}$ ]]; then
        echo "--play-revision must be a full lowercase commit SHA" >&2
        exit 2
    fi
    SPECTATE=1
fi
# The bot supervisor uses `wait -n -p` (bash 5.1+).
if ((BASH_VERSINFO[0] < 5 || (BASH_VERSINFO[0] == 5 && BASH_VERSINFO[1] < 1))); then
    echo "bash 5.1 or newer is required" >&2
    exit 2
fi
if [[ "$(id -u)" != "0" ]]; then
    echo "bootstrap must run as root under SSM" >&2
    exit 2
fi

unset AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY AWS_SESSION_TOKEN AWS_SECURITY_TOKEN
unset RIICHILAB_TRACE_PATH

if [[ -e /root/.aws/credentials || -e /home/ec2-user/.aws/credentials ]]; then
    echo "static AWS credential file is present; refusing live run" >&2
    exit 1
fi

source /etc/os-release
if [[ "${ID:-}" != "amzn" || "${VERSION_ID:-}" != "2023" ]]; then
    echo "expected Amazon Linux 2023" >&2
    exit 1
fi
if [[ "$(uname -m)" != "x86_64" ]]; then
    echo "expected x86_64 architecture" >&2
    exit 1
fi

mkdir -p "$WORK_ROOT"
chmod 700 "$WORK_ROOT"
BOOTSTRAP_LOG="$WORK_ROOT/bootstrap.log"
RUNNER_LOG="$WORK_ROOT/continuous.log"
RECORD_DIR="$WORK_ROOT/records"
REPO_DIR="$WORK_ROOT/repo"
PLAY_DIR="$WORK_ROOT/play"
# Issue #383: one instance-wide stop request shared by every bot of this run.
# Each bot checks it before starting a new hanchan, so a hanchan in progress
# always finishes. The first writer records why (see request_stop):
#   operator            stop-riichilab.ps1 through SSM
#   bot-exited:<name>   a bot of this run exited, so the others stop as well
STOP_FILE="$WORK_ROOT/stop-requested"

if [[ -e "$REPO_DIR" || -e "$PLAY_DIR" || -e "$RECORD_DIR" || -e "$RUNNER_LOG" ]]; then
    echo "work root is not fresh" >&2
    exit 1
fi

echo "preflight: os=Amazon Linux 2023 architecture=x86_64"

dnf -q install -y git python3.14 python3.14-pip >>"$BOOTSTRAP_LOG" 2>&1
if ! command -v aws >/dev/null 2>&1; then
    echo "AWS CLI is not available on the instance" >&2
    exit 1
fi
python3.14 --version

git clone -q "$REPOSITORY_URL" "$REPO_DIR" >>"$BOOTSTRAP_LOG" 2>&1
git -C "$REPO_DIR" checkout -q --detach "$ARENA_REVISION" >>"$BOOTSTRAP_LOG" 2>&1
ACTUAL_REVISION="$(git -C "$REPO_DIR" rev-parse HEAD)"
if [[ "$ACTUAL_REVISION" != "$ARENA_REVISION" ]]; then
    echo "Arena checkout revision mismatch" >&2
    exit 1
fi
if [[ -n "$(git -C "$REPO_DIR" status --porcelain)" ]]; then
    echo "Arena checkout is not clean" >&2
    exit 1
fi

python3.14 -m venv "$REPO_DIR/.venv" >>"$BOOTSTRAP_LOG" 2>&1
PYTHON="$REPO_DIR/.venv/bin/python"
"$PYTHON" -m pip install --disable-pip-version-check -e "$REPO_DIR" >>"$BOOTSTRAP_LOG" 2>&1

if [[ "$SPECTATE" == "1" ]]; then
    # The viewer runs inside the process that holds the RiichiLab token, so
    # lisjong-play must be an exact revision whose Arena pin is this run's
    # Arena revision. Any mismatch fails closed before the live run.
    git clone -q "$PLAY_REPOSITORY_URL" "$PLAY_DIR" >>"$BOOTSTRAP_LOG" 2>&1
    git -C "$PLAY_DIR" checkout -q --detach "$PLAY_REVISION" >>"$BOOTSTRAP_LOG" 2>&1
    if [[ "$(git -C "$PLAY_DIR" rev-parse HEAD)" != "$PLAY_REVISION" ]]; then
        echo "lisjong-play checkout revision mismatch" >&2
        exit 1
    fi
    if [[ -n "$(git -C "$PLAY_DIR" status --porcelain)" ]]; then
        echo "lisjong-play checkout is not clean" >&2
        exit 1
    fi
    # Arena stays the verified editable checkout: durable record provenance
    # resolves lisjong_arena_revision from that clean Git work tree. lisjong-play
    # is therefore installed without dependencies after proving that every one
    # of its dependencies is an internal pin identical to this run's Arena
    # revision or to Arena's own pins (which environment_verify checks below).
    PLAY_PIN_CHECK="$(
        "$PYTHON" - "$PLAY_DIR/pyproject.toml" "$REPO_DIR/pyproject.toml" "$ARENA_REVISION" <<'PY'
import re
import sys
import tomllib

PIN = re.compile(
    r"^(lisjong|lisjong-engine|lisjong-arena) @ "
    r"git\+https://github\.com/lisbun/(lisjong|lisjong-engine|lisjong-arena)\.git"
    r"@([0-9a-f]{40})$"
)


def pins(path, *, internal_only):
    with open(path, "rb") as handle:
        dependencies = tomllib.load(handle)["project"]["dependencies"]
    result = {}
    for dependency in dependencies:
        match = PIN.match(dependency)
        if match is None:
            if internal_only or dependency.split()[0].startswith("lisjong"):
                return None
            continue
        if match.group(1) != match.group(2) or match.group(1) in result:
            return None
        result[match.group(1)] = match.group(3)
    return result


# lisjong-play must depend on internal pins only (it is installed --no-deps).
play = pins(sys.argv[1], internal_only=True)
arena = pins(sys.argv[2], internal_only=False)
expected = None if arena is None else {**arena, "lisjong-arena": sys.argv[3]}
print("match" if play is not None and play == expected else "mismatch")
PY
    )"
    if [[ "$PLAY_PIN_CHECK" != "match" ]]; then
        echo "lisjong-play pins do not match this exact Arena revision and its pins" >&2
        exit 1
    fi
    "$PYTHON" -m pip install --disable-pip-version-check --no-deps -e "$PLAY_DIR" >>"$BOOTSTRAP_LOG" 2>&1
fi

cd "$REPO_DIR"
"$PYTHON" -m lisjong_arena.environment_verify --project pyproject.toml     >>"$BOOTSTRAP_LOG" 2>&1
echo "preflight: environment_verify=PASS arena_revision=$ARENA_REVISION"
if [[ "$SPECTATE" == "1" ]]; then
    if ! "$PYTHON" -m lisjong_play.riichilab_html --help | grep -q -- "--continuous"; then
        echo "lisjong-play viewer does not expose --continuous" >&2
        exit 1
    fi
    echo "preflight: spectate=on play_revision=$PLAY_REVISION port=$SPECTATE_PORT bind=127.0.0.1"
fi

# The operator stop request is always enabled for the Arena runner. A
# lisjong-play viewer that does not forward --stop-file keeps the previous
# duration-only behavior; an until-stopped run could then never stop normally,
# so that combination fails closed.
STOP_FILE_ENABLED=1
if [[ "$SPECTATE" == "1" ]] &&
    ! "$PYTHON" -m lisjong_play.riichilab_html --help | grep -q -- "--stop-file"; then
    if [[ "$UNTIL_STOPPED" == "1" ]]; then
        echo "lisjong-play viewer does not expose --stop-file; --until-stopped cannot stop normally" >&2
        exit 1
    fi
    STOP_FILE_ENABLED=0
fi
echo "preflight: stop_file=$([[ "$STOP_FILE_ENABLED" == "1" ]] && echo on || echo off) until_stopped=$UNTIL_STOPPED"

PROFILE_POLICY="$(
    "$PYTHON" - <<'PY'
from lisjong_arena.riichilab.profile import resolve_profile
profile = resolve_profile("lisjong-dev")
print(f"{profile.name}:{type(profile.policy_factory()).__name__}")
PY
)"
if [[ "$PROFILE_POLICY" != "$PROFILE:$POLICY" ]]; then
    echo "profile or Policy preflight mismatch" >&2
    exit 1
fi
echo "preflight: profile=$PROFILE policy=$POLICY"

CONTINUOUS_HELP="$("$PYTHON" -m lisjong_arena.riichilab.continuous_ranked --help)"
if ! grep -q -- "--duration-seconds" <<<"$CONTINUOUS_HELP"; then
    echo "continuous_ranked does not expose --duration-seconds" >&2
    exit 1
fi
if ! grep -q -- "--stop-file" <<<"$CONTINUOUS_HELP"; then
    echo "continuous_ranked does not expose --stop-file" >&2
    exit 1
fi

RUNNER_BOUND_ARGS=()
VERIFY_BOUND_ARGS=()
if [[ "$UNTIL_STOPPED" == "1" ]]; then
    VERIFY_BOUND_ARGS+=(--until-stopped)
else
    RUNNER_BOUND_ARGS+=(--duration-seconds "$DURATION_SECONDS")
    VERIFY_BOUND_ARGS+=(--expected-duration-seconds "$DURATION_SECONDS")
fi
if [[ "$STOP_FILE_ENABLED" == "1" ]]; then
    RUNNER_BOUND_ARGS+=(--stop-file "$STOP_FILE")
    VERIFY_BOUND_ARGS+=(--stop-file "$STOP_FILE")
fi

mkdir "$RECORD_DIR"
chmod 700 "$RECORD_DIR"
probe="$RECORD_DIR/.write-probe"
: >"$probe"
rm "$probe"

# Retrieve the full response as JSON (rather than --output text) so any
# CR/LF stored inside SecretString survives as a JSON escape sequence
# instead of a raw trailing newline byte that command substitution would
# strip before the secret-shape validator ever sees it (Issue #336).
SECRET_RESPONSE_JSON="$(
    aws secretsmanager get-secret-value         --region "$REGION"         --secret-id "$SECRET_ID"         --output json
)"
TOKEN="$(
    printf '%s' "$SECRET_RESPONSE_JSON" |
        "$PYTHON" -m lisjong_arena.riichilab.secret_contract
)"
unset SECRET_RESPONSE_JSON
if [[ -z "$TOKEN" ]]; then
    echo "runtime secret could not be resolved" >&2
    exit 1
fi
export LISJONG_DEV_BOT_TOKEN="$TOKEN"
unset TOKEN

# Normal teardown: poweroff (-> terminate) five minutes later. The Windows
# launcher / collector normally terminates the instance sooner, but this timer
# prevents an expired/disconnected local SSO session from leaving the instance
# billable after the bot has stopped.
NORMAL_TEARDOWN_ARMED=0
arm_normal_teardown() {
    if [[ "$NORMAL_TEARDOWN_ARMED" == "1" ]]; then
        return 0
    fi
    systemd-run \
        --quiet \
        --unit=lisjong-normal-teardown \
        --on-active=5min \
        --timer-property=AccuracySec=30s \
        /usr/bin/systemctl poweroff
    NORMAL_TEARDOWN_ARMED=1
}

# In an until-stopped run the only other stop is the long cost fail-safe, so
# once the bots have started, any exit (including bot or verification failure)
# also arms the normal teardown. The bootstrap exits only after every bot of
# this run has exited, so no bot is left running on the instance.
RUNNER_STARTED=0
on_exit() {
    unset LISJONG_DEV_BOT_TOKEN
    if [[ "$UNTIL_STOPPED" == "1" && "$RUNNER_STARTED" == "1" ]]; then
        arm_normal_teardown || true
    fi
}
trap on_exit EXIT

START_EPOCH="$(date +%s)"
START_UTC="$(date -u -d "@$START_EPOCH" '+%Y-%m-%dT%H:%M:%SZ')"
if [[ "$UNTIL_STOPPED" == "1" ]]; then
    echo "run: start_utc=$START_UTC until_stopped=1 stop_file=$STOP_FILE"
else
    CUTOFF_EPOCH="$((START_EPOCH + DURATION_SECONDS))"
    CUTOFF_UTC="$(date -u -d "@$CUTOFF_EPOCH" '+%Y-%m-%dT%H:%M:%SZ')"
    VERIFY_BOUND_ARGS+=(--cutoff-utc "$CUTOFF_UTC")
    echo "run: start_utc=$START_UTC cutoff_utc=$CUTOFF_UTC duration_seconds=$DURATION_SECONDS"
fi

# The first writer wins (noclobber), so the recorded reason is the first one.
request_stop() {
    (
        set -C
        printf '%s\n' "$1" >"$STOP_FILE"
    ) 2>/dev/null || true
}

# Bot supervisor. All bots of a run are started by this one bootstrap and are
# tracked by PID -> bot name. When any bot exits (normally or not), the others
# are asked to finish their hanchan in progress and stop. The run currently
# starts exactly one bot.
declare -A BOT_NAMES=()
declare -A BOT_EXIT_CODES=()
RUNNER_STARTED=1
if [[ "$SPECTATE" == "1" ]]; then
    # Same Arena continuous runner and summary output, plus the loopback-only
    # live viewer in the same process. Reach it with SSM port forwarding.
    "$PYTHON" -m lisjong_play.riichilab_html         --continuous         --profile "$PROFILE"         "${RUNNER_BOUND_ARGS[@]}"         --record-dir "$RECORD_DIR"         --port "$SPECTATE_PORT"         >"$RUNNER_LOG" 2>&1 &
else
    "$PYTHON" -m lisjong_arena.riichilab.continuous_ranked         --profile "$PROFILE"         "${RUNNER_BOUND_ARGS[@]}"         --record-dir "$RECORD_DIR"         >"$RUNNER_LOG" 2>&1 &
fi
BOT_NAMES[$!]="$PROFILE"
echo "run: bot_started name=$PROFILE"

RUNNING_BOT_PIDS=("${!BOT_NAMES[@]}")
while ((${#RUNNING_BOT_PIDS[@]})); do
    set +e
    wait -n -p EXITED_BOT_PID "${RUNNING_BOT_PIDS[@]}"
    EXITED_BOT_CODE=$?
    set -e
    BOT_EXIT_CODES[$EXITED_BOT_PID]=$EXITED_BOT_CODE
    request_stop "bot-exited:${BOT_NAMES[$EXITED_BOT_PID]}"
    echo "run: bot_exited name=${BOT_NAMES[$EXITED_BOT_PID]} exit_code=$EXITED_BOT_CODE stop_requested_for_others=1"
    REMAINING_BOT_PIDS=()
    for pid in "${RUNNING_BOT_PIDS[@]}"; do
        if [[ "$pid" != "$EXITED_BOT_PID" ]]; then
            REMAINING_BOT_PIDS+=("$pid")
        fi
    done
    RUNNING_BOT_PIDS=("${REMAINING_BOT_PIDS[@]}")
done

RUNNER_EXIT_CODE=0
for pid in "${!BOT_EXIT_CODES[@]}"; do
    if [[ "${BOT_EXIT_CODES[$pid]}" -ne 0 ]]; then
        RUNNER_EXIT_CODE="${BOT_EXIT_CODES[$pid]}"
    fi
done

STOP_EPOCH="$(date +%s)"
STOP_UTC="$(date -u -d "@$STOP_EPOCH" '+%Y-%m-%dT%H:%M:%SZ')"
ELAPSED_SECONDS="$((STOP_EPOCH - START_EPOCH))"

if [[ "$RUNNER_EXIT_CODE" -ne 0 ]]; then
    echo "run: continuous_ranked failed with exit code $RUNNER_EXIT_CODE" >&2
    exit "$RUNNER_EXIT_CODE"
fi

SUMMARY_JSON="$(
    "$PYTHON" -m lisjong_arena.riichilab.aws_run_verify         --record-dir "$RECORD_DIR"         --runner-log "$RUNNER_LOG"         --expected-arena-revision "$ARENA_REVISION"         --expected-profile "$PROFILE"         --expected-policy "$POLICY"         "${VERIFY_BOUND_ARGS[@]}"         --start-utc "$START_UTC"         --stop-utc "$STOP_UTC"         --elapsed-seconds "$ELAPSED_SECONDS"
)"

unset LISJONG_DEV_BOT_TOKEN

# After durable verification and secret unset have succeeded, arm the normal
# teardown for every mode.
arm_normal_teardown

SUMMARY_B64="$(printf '%s' "$SUMMARY_JSON" | base64 -w0)"
echo "run: verification=PASS normal_teardown_armed=5min"
printf 'LISJONG_COMPLETION_JSON_B64=%s\n' "$SUMMARY_B64"
