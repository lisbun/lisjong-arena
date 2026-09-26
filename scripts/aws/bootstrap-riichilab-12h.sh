#!/usr/bin/env bash
set -euo pipefail

# The bot supervisor uses `wait -n -p` (bash 5.1+).
if ((BASH_VERSINFO[0] < 5 || (BASH_VERSINFO[0] == 5 && BASH_VERSINFO[1] < 1))); then
    echo "bash 5.1 or newer is required" >&2
    exit 2
fi

ARENA_REVISION=""
REGION="ap-northeast-1"
SECRET_ID="lisjong/riichilab/lisjong-dev-token"
SECRET_ID_GIVEN=0
DURATION_SECONDS="43200"
WORK_ROOT="/var/lib/lisjong-riichilab-313"
REPOSITORY_URL="https://github.com/lisbun/lisjong-arena.git"
# Issue #386: explicit bots of this run, `--bot PROFILE=SECRET_ID` in launch
# order. Without --bot the run is the single lisjong-dev bot reading
# --secret-id, as before. Profiles, Policies and secrets are checked by
# lisjong_arena.riichilab.aws_instance_run before any credential is fetched.
BOT_SPECS=()
MAX_BOTS=4
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
            SECRET_ID_GIVEN=1
            shift 2
            ;;
        --bot)
            BOT_SPECS+=("$2")
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
if ((${#BOT_SPECS[@]} == 0)); then
    BOT_SPECS=("lisjong-dev=$SECRET_ID")
elif [[ "$SECRET_ID_GIVEN" == "1" ]]; then
    echo "--bot cannot be combined with --secret-id" >&2
    exit 2
fi
if ((${#BOT_SPECS[@]} > MAX_BOTS)); then
    echo "--bot accepts at most $MAX_BOTS bots" >&2
    exit 2
fi
BOT_PROFILES=()
BOT_SECRET_IDS=()
declare -A SEEN_PROFILES=()
declare -A SEEN_SECRET_IDS=()
for spec in "${BOT_SPECS[@]}"; do
    if [[ ! "$spec" =~ ^([a-z0-9-]+)=([A-Za-z0-9/_+=.@-]+)$ ]]; then
        echo "--bot must be PROFILE=SECRET_ID" >&2
        exit 2
    fi
    if [[ -n "${SEEN_PROFILES[${BASH_REMATCH[1]}]:-}" ]]; then
        echo "--bot profiles must be unique" >&2
        exit 2
    fi
    if [[ -n "${SEEN_SECRET_IDS[${BASH_REMATCH[2]}]:-}" ]]; then
        echo "--bot secret ids must be unique" >&2
        exit 2
    fi
    SEEN_PROFILES[${BASH_REMATCH[1]}]=1
    SEEN_SECRET_IDS[${BASH_REMATCH[2]}]=1
    BOT_PROFILES+=("${BASH_REMATCH[1]}")
    BOT_SECRET_IDS+=("${BASH_REMATCH[2]}")
done
# One loopback viewer port per bot: --spectate-port + index, in launch order.
if [[ "$SPECTATE" == "1" ]] && ((SPECTATE_PORT + ${#BOT_PROFILES[@]} - 1 > 65535)); then
    echo "--spectate-port leaves no room for one port per bot" >&2
    exit 2
fi
if [[ "$(id -u)" != "0" ]]; then
    echo "bootstrap must run as root under SSM" >&2
    exit 2
fi

unset AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY AWS_SESSION_TOKEN AWS_SECURITY_TOKEN
unset RIICHILAB_TRACE_PATH

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
# Per-bot evidence: $BOTS_DIR/<profile>/{records,continuous.log,exit_code,stop_utc}.
# Profiles are unique, so no two bots share a record path or log.
BOTS_DIR="$WORK_ROOT/bots"
REPO_DIR="$WORK_ROOT/repo"
PLAY_DIR="$WORK_ROOT/play"
# Issue #383: one instance-wide stop request shared by every bot of this run.
# Each bot checks it before starting a new hanchan, so a hanchan in progress
# always finishes. The first writer records why (see request_stop):
#   operator            stop-riichilab.ps1 through SSM
#   bot-exited:<name>   a bot of this run exited, so the others stop as well
STOP_FILE="$WORK_ROOT/stop-requested"

if [[ -e "$REPO_DIR" || -e "$PLAY_DIR" || -e "$BOTS_DIR" ]]; then
    echo "work root is not fresh" >&2
    exit 1
fi

# Normal teardown: poweroff (-> terminate) five minutes later. The Windows
# launcher / collector normally terminates the instance sooner, but this timer
# prevents an expired/disconnected local SSO session from leaving the instance
# billable after the run has ended. The five minutes let SSM finalize the
# command status and output first.
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
        /usr/bin/systemctl poweroff || return 1
    NORMAL_TEARDOWN_ARMED=1
}

# Issue #337: from here on this instance belongs to this run, so any non-zero
# exit is a confirmed instance-side failure (bootstrap, secret/configuration,
# bot or verification failure, duration-bound or until-stopped alike). Runtime
# secrets are dropped first, then the same five-minute teardown is armed. The
# exit code and output stay in the SSM command for the collector. Checks above
# (arguments, root, OS, fresh work root) leave the instance to the collector
# and the independent long cost fail-safe, which this never replaces. The bash
# process exits only after every bot of this run has exited, except on an
# unexpected supervisor error, where the poweroff stops the remaining bots.
on_exit() {
    local exit_code=$?
    unset BOT_TOKENS TOKEN SECRET_RESPONSE_JSON
    if [[ "$exit_code" != "0" ]]; then
        if arm_normal_teardown; then
            echo "run: failed exit_code=$exit_code failure_teardown_armed=5min" >&2
        else
            echo "run: failed exit_code=$exit_code failure_teardown_not_armed; the long cost fail-safe remains armed" >&2
        fi
    fi
}
trap on_exit EXIT

if [[ -e /root/.aws/credentials || -e /home/ec2-user/.aws/credentials ]]; then
    echo "static AWS credential file is present; refusing live run" >&2
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
    echo "preflight: spectate=on play_revision=$PLAY_REVISION base_port=$SPECTATE_PORT bind=127.0.0.1"
fi

# The stop request is always enabled for the Arena runner. A lisjong-play
# viewer that does not forward --stop-file keeps the previous duration-only
# behavior. An until-stopped run could then never stop normally, and with
# several bots one bot's exit could not stop the others, so those combinations
# fail closed.
STOP_FILE_ENABLED=1
if [[ "$SPECTATE" == "1" ]] &&
    ! "$PYTHON" -m lisjong_play.riichilab_html --help | grep -q -- "--stop-file"; then
    if [[ "$UNTIL_STOPPED" == "1" ]]; then
        echo "lisjong-play viewer does not expose --stop-file; --until-stopped cannot stop normally" >&2
        exit 1
    fi
    if ((${#BOT_PROFILES[@]} > 1)); then
        echo "lisjong-play viewer does not expose --stop-file; several bots cannot stop together" >&2
        exit 1
    fi
    STOP_FILE_ENABLED=0
fi
echo "preflight: stop_file=$([[ "$STOP_FILE_ENABLED" == "1" ]] && echo on || echo off) until_stopped=$UNTIL_STOPPED"

# Known profile, explicit expected Policy (checked against the runtime
# profile), distinct credential variables, collision-free viewer ports.
BOT_CONFIG_ARGS=()
for spec in "${BOT_SPECS[@]}"; do
    BOT_CONFIG_ARGS+=(--bot "$spec")
done
if [[ "$SPECTATE" == "1" ]]; then
    BOT_CONFIG_ARGS+=(--spectate-base-port "$SPECTATE_PORT")
fi
BOT_CONFIG="$("$PYTHON" -m lisjong_arena.riichilab.aws_instance_run check-config "${BOT_CONFIG_ARGS[@]}")"
declare -A BOT_ENV_VARS=()
declare -A BOT_PORTS=()
bot_index=0
while IFS=$'\t' read -r profile env_var policy port; do
    if [[ "$profile" != "${BOT_PROFILES[$bot_index]:-}" ]]; then
        echo "bot configuration preflight mismatch" >&2
        exit 1
    fi
    BOT_ENV_VARS[$profile]="$env_var"
    BOT_PORTS[$profile]="$port"
    echo "preflight: bot=$profile policy=$policy spectate_port=$port"
    bot_index=$((bot_index + 1))
done <<<"$BOT_CONFIG"
if ((bot_index != ${#BOT_PROFILES[@]})); then
    echo "bot configuration preflight mismatch" >&2
    exit 1
fi

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
VERIFY_BOUND_ARGS=("${BOT_CONFIG_ARGS[@]}" --stop-file "$STOP_FILE")
if [[ "$UNTIL_STOPPED" == "1" ]]; then
    VERIFY_BOUND_ARGS+=(--until-stopped)
else
    RUNNER_BOUND_ARGS+=(--duration-seconds "$DURATION_SECONDS")
    VERIFY_BOUND_ARGS+=(--expected-duration-seconds "$DURATION_SECONDS")
fi
if [[ "$STOP_FILE_ENABLED" == "1" ]]; then
    RUNNER_BOUND_ARGS+=(--stop-file "$STOP_FILE")
fi

mkdir "$BOTS_DIR"
chmod 700 "$BOTS_DIR"
for profile in "${BOT_PROFILES[@]}"; do
    mkdir -p "$BOTS_DIR/$profile/records"
    chmod -R 700 "$BOTS_DIR/$profile"
    probe="$BOTS_DIR/$profile/records/.write-probe"
    : >"$probe"
    rm "$probe"
done

# Every configured bot's credential is resolved before any bot starts; one
# failure starts no bot. Retrieve the full response as JSON (rather than
# --output text) so any CR/LF stored inside SecretString survives as a JSON
# escape sequence instead of a raw trailing newline byte that command
# substitution would strip before the secret-shape validator ever sees it
# (Issue #336). Tokens stay in this shell's memory: none is exported, and each
# bot process receives only its own profile's credential variable.
declare -A BOT_TOKENS=()
for index in "${!BOT_PROFILES[@]}"; do
    profile="${BOT_PROFILES[$index]}"
    SECRET_RESPONSE_JSON="$(
        aws secretsmanager get-secret-value \
            --region "$REGION" \
            --secret-id "${BOT_SECRET_IDS[$index]}" \
            --output json
    )"
    TOKEN="$(
        printf '%s' "$SECRET_RESPONSE_JSON" |
            "$PYTHON" -m lisjong_arena.riichilab.secret_contract
    )"
    unset SECRET_RESPONSE_JSON
    if [[ -z "$TOKEN" ]]; then
        echo "runtime secret could not be resolved for bot $profile" >&2
        exit 1
    fi
    for other in "${!BOT_TOKENS[@]}"; do
        if [[ "${BOT_TOKENS[$other]}" == "$TOKEN" ]]; then
            echo "bots $other and $profile resolve the same runtime token" >&2
            exit 1
        fi
    done
    BOT_TOKENS[$profile]="$TOKEN"
    unset TOKEN
done
echo "preflight: credentials_resolved=${#BOT_TOKENS[@]}"

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
# tracked by PID -> profile. When any bot exits (normally or not), the others
# are asked to finish their hanchan in progress and stop.
declare -A BOT_NAMES=()
start_bot() {
    local profile="$1"
    local directory="$BOTS_DIR/$profile"
    local args=(--profile "$profile" "${RUNNER_BOUND_ARGS[@]}" --record-dir "$directory/records")
    if [[ "$SPECTATE" == "1" ]]; then
        # Same Arena continuous runner and summary output, plus the
        # loopback-only live viewer in the same process, on this bot's own
        # port. Reach it with SSM port forwarding.
        args+=(--port "${BOT_PORTS[$profile]}")
        (
            export "${BOT_ENV_VARS[$profile]}=${BOT_TOKENS[$profile]}"
            unset BOT_TOKENS
            exec "$PYTHON" -m lisjong_play.riichilab_html --continuous "${args[@]}"
        ) >"$directory/continuous.log" 2>&1 &
    else
        (
            export "${BOT_ENV_VARS[$profile]}=${BOT_TOKENS[$profile]}"
            unset BOT_TOKENS
            exec "$PYTHON" -m lisjong_arena.riichilab.continuous_ranked "${args[@]}"
        ) >"$directory/continuous.log" 2>&1 &
    fi
    BOT_NAMES[$!]="$profile"
    echo "run: bot_started name=$profile"
}

for profile in "${BOT_PROFILES[@]}"; do
    start_bot "$profile"
done

RUNNING_BOT_PIDS=("${!BOT_NAMES[@]}")
while ((${#RUNNING_BOT_PIDS[@]})); do
    set +e
    wait -n -p EXITED_BOT_PID "${RUNNING_BOT_PIDS[@]}"
    EXITED_BOT_CODE=$?
    set -e
    EXITED_BOT="${BOT_NAMES[$EXITED_BOT_PID]}"
    printf '%s\n' "$EXITED_BOT_CODE" >"$BOTS_DIR/$EXITED_BOT/exit_code"
    date -u '+%Y-%m-%dT%H:%M:%SZ' >"$BOTS_DIR/$EXITED_BOT/stop_utc"
    request_stop "bot-exited:$EXITED_BOT"
    echo "run: bot_exited name=$EXITED_BOT exit_code=$EXITED_BOT_CODE stop_requested_for_others=1"
    REMAINING_BOT_PIDS=()
    for pid in "${RUNNING_BOT_PIDS[@]}"; do
        if [[ "$pid" != "$EXITED_BOT_PID" ]]; then
            REMAINING_BOT_PIDS+=("$pid")
        fi
    done
    RUNNING_BOT_PIDS=("${REMAINING_BOT_PIDS[@]}")
done

# Every bot has exited. Verify each bot independently; every bot's evidence
# is scanned for all runtime tokens of this run. The verifier alone receives
# all credential variables.
set +e
SUMMARY_JSON="$(
    for profile in "${BOT_PROFILES[@]}"; do
        export "${BOT_ENV_VARS[$profile]}=${BOT_TOKENS[$profile]}"
    done
    unset BOT_TOKENS
    exec "$PYTHON" -m lisjong_arena.riichilab.aws_instance_run verify \
        --work-root "$WORK_ROOT" \
        --expected-arena-revision "$ARENA_REVISION" \
        --start-utc "$START_UTC" \
        "${VERIFY_BOUND_ARGS[@]}"
)"
VERIFY_EXIT_CODE=$?
set -e
unset BOT_TOKENS

if [[ "$VERIFY_EXIT_CODE" -gt 1 || -z "$SUMMARY_JSON" ]]; then
    echo "run: instance verification could not run (exit code $VERIFY_EXIT_CODE)" >&2
    exit 1
fi

# The secret-safe instance summary is returned for PASS and FAIL alike, so a
# failing bot stays attributable. Every bot has exited: PASS arms the teardown
# here, FAIL arms it through on_exit.
SUMMARY_B64="$(printf '%s' "$SUMMARY_JSON" | base64 -w0)"
if [[ "$VERIFY_EXIT_CODE" -eq 0 ]]; then
    arm_normal_teardown
    echo "run: verification=PASS normal_teardown_armed=5min"
else
    echo "run: verification=FAIL" >&2
fi
printf 'LISJONG_COMPLETION_JSON_B64=%s\n' "$SUMMARY_B64"
exit "$VERIFY_EXIT_CODE"
