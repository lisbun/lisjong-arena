# AWS RiichiLab 12-hour bounded run

Issue #313 automates one disposable Amazon Linux 2023 EC2 instance for a
12-hour `lisjong-dev` RiichiLab ranked run.

This is an operational smoke, not a Policy-strength evaluation.

## Operator contract

Use short-lived AWS CLI authentication. For the current single-account learning
environment, the recommended local profile is `lisbun-admin` and authentication
is performed with `aws login`; do not create a static access key.

The recommended Issue #313 flow is deliberately staged:

```powershell
aws login --profile lisbun-admin --region ap-northeast-1

# 1. Read-only/live-resource validation. Creates no EC2 execution resource.
.\scripts\aws\start-riichilab-12h.ps1 `
  -AwsProfile lisbun-admin `
  -PreflightOnly

# 2. Launch, arm the independent cost fail-safe, submit SSM, then return.
.\scripts\aws\start-riichilab-12h.ps1 `
  -AwsProfile lisbun-admin `
  -ArenaRevision <exact-full-sha> `
  -SubmitOnly

# 3. Later, re-authenticate if needed and collect using the emitted state path.
aws login --profile lisbun-admin --region ap-northeast-1
.\scripts\aws\collect-riichilab-12h.ps1 `
  -AwsProfile lisbun-admin `
  -StatePath <state.json>
```

`-PreflightOnly` stops before `ec2 run-instances`, writes a secret-safe
`preflight.json`, and records a conservative known-cost estimate using the
independent fail-safe horizon. `-SubmitOnly` returns only after the EC2 instance is SSM
managed, the independent approximately 14-hour cost fail-safe is armed, and the
long-running SSM command has been accepted. It writes recovery identifiers to
`state.json`.

The collector is intentionally one-shot. If the SSM command is still
`Pending`, `InProgress`, or `Delayed`, it reports status and takes no
termination or teardown action. Run it again later. If the command has completed
successfully, it recovers the verified secret-safe summary, persists it before
teardown, and then verifies termination and residue cleanup.

The launcher uses AWS CLI only. It does not require AWS Console automation,
shared-browser control, SSH, a static access key, or a local RiichiLab token.
The original synchronous mode (no staged switch) remains available, but the
staged flow is preferred for the 12-hour run because local shell and login
lifetime are no longer part of the remote execution path.

Run this only after the automation PR containing these scripts has been merged.
By default the launcher resolves the current `main` commit and the EC2 bootstrap
checks out that exact full revision.

## Reused AWS resources

The launcher expects the known-good Issue #305 baseline unless explicit
overrides are supplied:

- region: `ap-northeast-1`
- IAM role: `lisjong-riichilab-smoke-ec2`
- security group: `lisjong-riichilab-smoke-305`
- secret: `lisjong/riichilab/lisjong-dev-token`
- instance type: `t3.small`

These names are not trusted merely because they exist. The launcher checks the
live resources before starting a ranked run.

The security group must have zero inbound rules. The instance role must be able
to read the intended secret and must not pass the launcher's deny-probe secret
simulation. The launcher reads IAM simulator per-resource decisions from
`ResourceSpecificResults`, matching the current AWS response contract for
multi-resource simulation. The resulting EC2 instance requires IMDSv2 and uses
SSM instead of inbound SSH.

## Live spectating (opt-in, Issue #381)

A run can be watched live in a browser without opening any inbound path:

```powershell
.\scripts\aws\start-riichilab-12h.ps1 `
  -AwsProfile lisbun-admin `
  -ArenaRevision <exact-full-sha> `
  -SpectatePort 8765 `
  -PlayRevision <lisjong-play-full-sha> `
  -SubmitOnly

# In another terminal, after submit (requires the AWS Session Manager plugin):
.\scripts\aws\watch-riichilab.ps1 -AwsProfile lisbun-admin -StatePath <state.json>
# then open http://localhost:8765/
```

```text
EC2 (security group inbound 0, no SSH)
  python -m lisjong_play.riichilab_html --continuous ... --port 8765
    ├─ Arena run_continuous_ranked_cli (token holder, same summary output)
    └─ live viewer bound to 127.0.0.1:8765 only
PC: aws ssm start-session AWS-StartPortForwardingSession
      portNumber=8765, localPortNumber=8765 -> http://localhost:8765/
```

- `-PlayRevision` defaults to the current lisjong-play `main`. The launcher
  reads that revision's `pyproject.toml` and refuses to continue unless it pins
  exactly `-ArenaRevision`. This check also runs with `-PreflightOnly`.
- The bootstrap checks out lisjong-play at the exact revision. It requires
  every lisjong-play dependency to be an internal pin equal to this Arena
  revision or to Arena's own `lisjong` / `lisjong-engine` pins. Only then does it
  install lisjong-play with `--no-deps`.
- Arena stays the verified, clean, editable checkout, so durable record
  provenance still resolves `lisjong_arena_revision`. `environment_verify` and
  `pip check` run afterwards, all before the token is fetched. The viewer code
  runs inside the token-holding process, so any mismatch fails closed.
- The runner log contains the same Arena summary lines, so
  `aws_run_verify` verifies records, token absence, and `duration_reached`
  exactly as without spectating.
- The viewer binds to `127.0.0.1` only and accepts only its own `Host`. The
  local and remote port numbers must therefore be equal;
  `watch-riichilab.ps1` always forwards the same number.
- The page shows only the bot seat's player-visible decision state, its
  selected action, and final scores. The page controls move the display only,
  and there is no endpoint that starts or influences a game.
- Stopping the watcher, closing the tab, or losing the SSM session does not
  affect the run. The viewer serves only while the run is active and closes
  with it.
- The operator identity needs `ssm:StartSession` for
  `AWS-StartPortForwardingSession` on the instance. The instance role and the
  security group need no change.

Without `-SpectatePort` the launcher and bootstrap behave exactly as before.

## Stop request and until-stopped participation (Issue #383)

A running run can be told to stop from the PC. Every bot finishes the hanchan
in progress and starts no new one. The records are then verified, and the EC2
instance powers off and terminates:

```powershell
# Participate continuously until a stop request (the fail-safe is the ceiling).
.\scripts\aws\start-riichilab-12h.ps1 `
  -AwsProfile lisbun-admin `
  -ArenaRevision <exact-full-sha> `
  -UntilStopped `
  -FailSafeHours 24 `
  -SubmitOnly

# Later: request the stop.
.\scripts\aws\stop-riichilab.ps1 -AwsProfile lisbun-admin -StatePath <state.json>

# After the hanchan in progress has finished: recover the summary and confirm teardown.
.\scripts\aws\collect-riichilab-12h.ps1 -AwsProfile lisbun-admin -StatePath <state.json>
```

```text
stop-riichilab.ps1
  -> confirms the long SSM command is still active
  -> SSM: write "operator" to /var/lib/lisjong-riichilab-313/stop-requested
     (first writer wins; an existing reason is kept)
each bot: continuous_ranked --stop-file .../stop-requested
  -> checked only before starting a new hanchan (or retry)
  -> the hanchan in progress finishes, its durable record is finalized
  -> stopped_reason=stop_requested
bootstrap bot supervisor
  -> waits until every bot of the run has exited
  -> when any bot exits, writes "bot-exited:<name>" (if no reason yet)
     so that the other bots also finish their hanchan and stop
  -> aws_run_verify (stop_requested accepted only if the stop file exists
     and names a known source)
  -> normal teardown timer: poweroff after 5 minutes -> terminate
```

### Several bots on one instance

The lifecycle is designed for several bots per instance, all launched by one
request and supervised by one bootstrap:

- There is one stop file per run (instance), shared by every bot.
- When any bot exits, for any reason (stop, duration, failure budget, crash),
  the supervisor writes the stop file. The remaining bots then finish their
  hanchan in progress and stop.
- The bootstrap verifies and arms the teardown only after every bot has
  exited. The instance therefore powers off only when no bot of the run is
  left running.
- The verified summary records the first reason as `stop_request_source`
  (`operator` or `bot-exited:<name>`). `operator_stop_requested` is true only
  for an operator stop.

The launcher currently starts exactly one bot (`lisjong-dev`). Actually
starting several additionally needs:

- a bot list in the launcher and bootstrap (profile, secret, expected Policy)
- IAM secret checks for each bot
- a record directory and runner log per bot
- one verification per bot
- a collector summary per bot
- a separate spectate port per bot

These are separate from this stop / teardown lifecycle, which does not need to
change.

- The stop request works for both modes. A duration-bound run that receives it
  stops early with `stopped_reason=stop_requested`; without it, behavior is
  unchanged (`duration_reached`).
- `-UntilStopped` passes no duration to the runner. It cannot be combined with
  `-DurationSeconds` and requires an explicit `-FailSafeHours` (1-47, limited by
  the SSM `executionTimeout` maximum of 48 hours). The fail-safe still powers the
  instance off at that ceiling even if no stop was requested, and may interrupt
  a hanchan in progress. The preflight cost estimate uses this ceiling.
- In `-UntilStopped` mode the teardown timer is also armed when a bot or the
  verification fails after the bots started, so a failed until-stopped run does
  not wait for the long fail-safe. Duration-bound
  runs keep the previous failure behavior (the collector terminates).
- A stop requested before the first hanchan completes verifies with zero
  records (`provenance` is then `null`).
- The verified summary is `schema_version: 2`: `requested_duration_seconds` and
  `cutoff_utc` are `null` for an until-stopped run, and `stop_request_source` /
  `operator_stop_requested` record who requested the stop first. Unrecognized
  stop file content fails verification.
- With `-SpectatePort`, the lisjong-play viewer must forward `--stop-file`.
  Until it does, the bootstrap fails closed for `-UntilStopped` and a
  duration-bound spectating run keeps the stop request disabled.
- `stop-riichilab.ps1` only writes the stop file. It never terminates,
  interrupts, or signals anything; a run that is no longer active is left to
  the collector.

## Normal stop versus cost fail-safe

The two stop mechanisms are deliberately separate.

Normal behavior:

```text
continuous_ranked --duration-seconds 43200
  -> reach the 12h monotonic cutoff
  -> do not start a new game/retry
  -> finish an already-running hanchan
  -> finalize + strict-read its durable record
  -> stopped_reason=duration_reached
```

Emergency cost behavior:

```text
instance-side systemd timer
  -> approximately 14h after arming
  -> systemctl poweroff
  -> EC2 instance-initiated shutdown behavior = terminate
```

The emergency timer is not the normal 12-hour stop and may interrupt work if the
normal path is badly stuck. Its purpose is to prevent an orphaned temporary EC2
instance from continuing to accrue cost.

After a successful strict verification the EC2 bootstrap also arms a separate
five-minute normal-teardown timer. The Windows launcher normally terminates the
instance sooner, but this timer covers a local AWS CLI/SSO disconnect after the
remote run has already passed.

The SSM command explicitly overrides `AWS-RunShellScript.executionTimeout`;
the default is only one hour and is not suitable for this run.

## What is verified before live ranked starts

The automation checks, among other things:

- Amazon Linux 2023 / x86_64
- exact detached Arena revision and clean checkout
- Python 3.14
- `environment_verify`
- pinned lisjong / lisjong-engine dependency identity
- RiichiEnv dependency identity
- `lisjong-dev` -> `MechanismRiichiDefenseYakuhaiCallPolicy`
- `--duration-seconds` availability
- fresh writable durable-record root
- diagnostic trace not enabled
- no static AWS credential file
- SSM connectivity
- IMDSv2 required
- zero inbound security-group rules
- instance-initiated shutdown behavior = terminate
- independent cost fail-safe armed

The RiichiLab token is retrieved by the instance role from Secrets Manager only
after these runtime checks. The value is not sent in user-data or SSM command
parameters and is not echoed.

## Post-run verification

`lisjong_arena.riichilab.aws_run_verify` strict-loads every published durable
record and checks:

- published record count equals completed-game count
- every record strict-reads
- record identities are unique
- record provenance is consistent
- expected profile / Policy / Arena revision match
- no provenance field is unresolved
- runner stopped because the duration was reached
- exact runtime token bytes are not present
- `Authorization` material is not present
- AWS static credential material is not present

Only a secret-safe JSON summary is returned through SSM stdout. The Windows
launcher writes that verified summary to `completion.json` **before** issuing
teardown API calls, so an SSO expiry after remote PASS does not erase the
already-recovered completion evidence. Raw durable records remain ephemeral for
Issue #313 and are lost when EC2 is terminated. S3 retention is intentionally
outside this issue.

## Local evidence

A successful launcher invocation writes a run directory below:

```text
%LOCALAPPDATA%\lisjong\aws-riichilab-313\<run-id>\
```

The important files are:

- `preflight.json`: live-resource validation result; preflight-only creates no
  EC2 execution resource
- `state.json`: instance / SSM command recovery identifiers, no secret values
- `completion.json`: secret-safe run + verification + teardown summary
- AWS CLI request JSON used by the launcher, containing configuration but no
  RiichiLab token or AWS static credential

Do not publish the whole local directory blindly. Review evidence before adding
anything to an Issue or PR.

## If local AWS authentication expires

The staged flow does not require the local AWS login session or PowerShell
process to remain alive for 12 hours. The remote SSM command and instance-side
fail-safe continue independently after `-SubmitOnly` returns.

When it is time to inspect or collect the result, authenticate again with the
same short-lived profile and run the collector against the saved `state.json`.
Do not start a second Issue #313 instance merely because local authentication
expired or the terminal was closed.

The instance-side approximately 14-hour fail-safe remains the billing safety
boundary even if collection is delayed.

## Cost notes

The launcher attempts to query the AWS Price List API for the current Tokyo
Linux on-demand hourly price and records an approximate EC2 compute cost.
If the caller lacks Pricing API access, the run can still proceed and the price
field is left unavailable unless `-HourlyPriceUsd` is supplied.

The launcher also accounts for the one in-use public IPv4 address attached to
the temporary EC2 instance. The default is `0.005 USD/hour`, matching the AWS
public IPv4 price at the time this automation was introduced; it can be
overridden with `-PublicIpv4HourlyPriceUsd` if AWS changes the price.

The estimate is not a billing statement. It still excludes EBS/data-transfer
details and any applicable T-family surplus CPU credit charges.

The reused Secrets Manager secret is intentionally retained by default and has
its own recurring charge. IAM roles and security groups themselves do not incur
a standalone hourly charge.

## Scope boundaries

This automation does not create:

- NAT Gateway
- load balancer
- RDS / Aurora
- ECS / Fargate
- ECR
- S3 retention
- Terraform / CDK / CloudFormation resources
- inbound SSH

Those remain separate design decisions.
