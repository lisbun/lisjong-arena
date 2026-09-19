# Targeted honor-release independent confirmation (#270)

This package implements the independent confirmation owned by `lisbun/lisjong-arena#270`.

It does not extend, pool, rescue, or reclassify the completed development screen in #263.

## Fixed scientific contract

Exact participants:

- H: `TargetedHonorReleaseTerminalProgressionPolicy`
- C: `MechanismRiichiDefenseYakuhaiCallPolicy`
- T: the existing deterministic passive-tsumogiri comparator
- lisjong revision: `f29d129c67e5232d06563c6e457754377734ed14`

The confirmation population is exactly:

```text
seed blocks        2,200
rotations / seed   4
H games            8,800
C games            8,800
total games        17,600
game mode          4p-red-single
formal TEST        false
role               INDEPENDENT CONFIRMATION
```

The primary independent unit is one paired seed block. For each locked seed `s`:

```text
H_s = mean H focal score across four rotations
C_s = mean C focal score across the same four rotations
D_s = H_s - C_s
```

All 2,200 `D_s` values are persisted. The primary summary is the sample mean, sample standard deviation, standard error, and two-sided normal-approximation 95% interval over those 2,200 paired values.

The terminal classification is fixed:

```text
95% interval lower > 0
  TARGETED HONOR-RELEASE CONFIRMED POSITIVE

95% interval upper < 0
  TARGETED HONOR-RELEASE CONFIRMED NEGATIVE

otherwise
  TARGETED HONOR-RELEASE CONFIRMATION INCONCLUSIVE
```

Secondary diagnostics never override this classification.

## Independence from #263

The #263 development population `751..850` is treated as allocated historical evidence and is explicitly excluded from #270.

The #270 seed range is deliberately not committed to the repository. Immediately before the pre-execution lock, the operator must audit repository-declared populations, relevant open/closed Arena Issues, and known operator-local/private artifact allocations.

The lock command requires explicit external-freshness confirmation. Any collision found before result exposure requires `SEED PLAN REFORMULATE`. After result exposure there is no seed replacement, extension, or rescue path.

## Evidence

The confirmation retains write-once evidence for:

```text
pre-execution-lock.json
candidate-H.json
parent-C.json
candidate-H-trace.json
paired-result.json
classified-result.json
```

The H strength artifact is produced by the existing trace-preserving authoritative H execution path used by #263. The separate H trace artifact binds its strength artifact digest, exact seed/rotation/focal-seat alignment, provenance, raw typed diagnostics, and re-derived aggregate summary.

The paired result is verified against persisted H/C strength artifacts and the H trace artifact before the classified result is written. Human-readable stdout is not final evidence.

## Pre-execution environment

Real execution is post-merge only from a clean reviewed revision contained in `main`.

Before building the lock, verify the current environment:

```powershell
python -m lisjong_arena.environment_verify --project pyproject.toml
```

The lock also performs the same fail-closed internal VCS dependency check and binds exact Arena/lisjong/lisjong-engine/Python/RiichiEnv provenance.

## Operator flow

Choose `START:END` only after the live public and local/private allocation audit. The inclusive range must contain exactly 2,200 fresh contiguous seeds.

```powershell
$root = "C:\Dev\lisjong-artifacts\issue-270-targeted-honor-release-confirmation"
New-Item -ItemType Directory -Force $root | Out-Null

python -m lisjong_arena.environment_verify --project pyproject.toml

python -m lisjong_arena.targeted_honor_release_confirmation lock `
  --out "$root\pre-execution-lock.json" `
  --confirmation-seeds START:END `
  --workers 8 `
  --external-freshness-confirmed `
  --candidate-artifact "$root\candidate-H.json" `
  --parent-artifact "$root\parent-C.json" `
  --candidate-trace "$root\candidate-H-trace.json" `
  --paired-result "$root\paired-result.json" `
  --classified-result "$root\classified-result.json"
```

If the live audit found other known private/local allocations, pass them with `--additional-allocated-seeds`, for example `900:925,1000`.

Post the generated lock details to Issue #270 before exposing any confirmation result. Only then run:

```powershell
python -m lisjong_arena.targeted_honor_release_confirmation run `
  --lock "$root\pre-execution-lock.json"
```

Do not run the 17,600-game scientific confirmation from the implementation PR branch or in CI.

## No-rescue boundary

After result exposure, do not add/replace/extend seeds, rerun the consumed population for scientific rescue, change confidence level or primary interval method, change H/C/R5/target-gate/comparator semantics, pool #263 observations, or let secondary diagnostics override classification.

If a post-exposure provenance/artifact failure makes the run scientifically unrecoverable, the outcome is `STOP / INVALID`; the consumed population is not reused.

A positive confirmation applies only to this locked passive-x3 single-round protocol. It is not automatically a Champion-promotion, hanchan, RiichiLab, Mortal, or general Mahjong-strength claim.
