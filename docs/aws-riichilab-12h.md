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

`-PreflightOnly` stops before `ec2 run-instances` and writes a secret-safe
`preflight.json`. `-SubmitOnly` returns only after the EC2 instance is SSM
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
simulation. The resulting EC2 instance requires IMDSv2 and uses SSM instead of
inbound SSH.

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