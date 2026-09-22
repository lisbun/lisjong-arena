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
            shift 2
            ;;
        --work-root)
            WORK_ROOT="$2"
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
if [[ ! "$DURATION_SECONDS" =~ ^[1-9][0-9]*$ ]]; then
    echo "--duration-seconds must be a positive integer" >&2
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

if [[ -e "$REPO_DIR" || -e "$RECORD_DIR" || -e "$RUNNER_LOG" ]]; then
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

cd "$REPO_DIR"
"$PYTHON" -m lisjong_arena.environment_verify --project pyproject.toml     >>"$BOOTSTRAP_LOG" 2>&1
echo "preflight: environment_verify=PASS arena_revision=$ARENA_REVISION"

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

if ! "$PYTHON" -m lisjong_arena.riichilab.continuous_ranked --help     | grep -q -- "--duration-seconds"; then
    echo "continuous_ranked does not expose --duration-seconds" >&2
    exit 1
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
trap 'unset LISJONG_DEV_BOT_TOKEN' EXIT

START_EPOCH="$(date +%s)"
START_UTC="$(date -u -d "@$START_EPOCH" '+%Y-%m-%dT%H:%M:%SZ')"
CUTOFF_EPOCH="$((START_EPOCH + DURATION_SECONDS))"
CUTOFF_UTC="$(date -u -d "@$CUTOFF_EPOCH" '+%Y-%m-%dT%H:%M:%SZ')"
echo "run: start_utc=$START_UTC cutoff_utc=$CUTOFF_UTC duration_seconds=$DURATION_SECONDS"

set +e
"$PYTHON" -m lisjong_arena.riichilab.continuous_ranked     --profile "$PROFILE"     --duration-seconds "$DURATION_SECONDS"     --record-dir "$RECORD_DIR"     >"$RUNNER_LOG" 2>&1
RUNNER_EXIT_CODE=$?
set -e

STOP_EPOCH="$(date +%s)"
STOP_UTC="$(date -u -d "@$STOP_EPOCH" '+%Y-%m-%dT%H:%M:%SZ')"
ELAPSED_SECONDS="$((STOP_EPOCH - START_EPOCH))"

if [[ "$RUNNER_EXIT_CODE" -ne 0 ]]; then
    unset LISJONG_DEV_BOT_TOKEN
    trap - EXIT
    echo "run: continuous_ranked failed with exit code $RUNNER_EXIT_CODE" >&2
    exit "$RUNNER_EXIT_CODE"
fi

SUMMARY_JSON="$(
    "$PYTHON" -m lisjong_arena.riichilab.aws_run_verify         --record-dir "$RECORD_DIR"         --runner-log "$RUNNER_LOG"         --expected-arena-revision "$ARENA_REVISION"         --expected-profile "$PROFILE"         --expected-policy "$POLICY"         --expected-duration-seconds "$DURATION_SECONDS"         --start-utc "$START_UTC"         --cutoff-utc "$CUTOFF_UTC"         --stop-utc "$STOP_UTC"         --elapsed-seconds "$ELAPSED_SECONDS"
)"

unset LISJONG_DEV_BOT_TOKEN
trap - EXIT

# Normal teardown safety net. The Windows launcher normally terminates the
# instance sooner, but this timer prevents an expired/disconnected local SSO
# session from leaving a successful run billable. It is armed only after
# durable verification and secret unset have succeeded.
systemd-run     --quiet     --unit=lisjong-normal-teardown     --on-active=5min     --timer-property=AccuracySec=30s     /usr/bin/systemctl poweroff

SUMMARY_B64="$(printf '%s' "$SUMMARY_JSON" | base64 -w0)"
echo "run: verification=PASS normal_teardown_armed=5min"
printf 'LISJONG_COMPLETION_JSON_B64=%s\n' "$SUMMARY_B64"
