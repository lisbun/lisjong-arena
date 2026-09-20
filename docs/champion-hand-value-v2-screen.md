# Issue #297 — Champion + HandValue v2 bounded screen

This module owns the post-merge local execution contract for
`lisjong-arena#297`.

The scientific shape is fixed:

- candidate: `HandValueTradeoffTargetedHonorReleasePolicy`
- baseline: `TargetedHonorReleaseTerminalProgressionPolicy x3`
- lisjong: `15799e5f0fe47f2e2b2c39060de804d99c51492d`
- 100 fresh contiguous ordered seeds
- 4 focal-seat rotations per seed
- 400 `4p-red-single` games
- primary unit: ordered seed block
- normal-approximation 95% interval
- no automatic Champion promotion

## Post-merge local preparation

After the implementation PR is merged:

```powershell
cd C:\Dev\lisjong-arena
git switch main
git pull --ff-only
git status --short

python -m pip install -e .

python -c "from importlib.metadata import version; print('RiichiEnv:', version('riichienv'))"
python -c "from lisjong_arena.single_round_artifact import collect_execution_provenance; print(collect_execution_provenance())"
```

The working tree must be clean. The provenance must show the exact lisjong
revision above, lisjong-engine
`8735e89e1aea000ab59368d0368d476787827741`, and RiichiEnv `0.4.10`.

## Candidate fresh range

Public repository / Issue audit found no allocation for `52200..52299` at
implementation time. It is only a candidate until the operator-local/private
audit is completed immediately before the lock.

A practical local text-artifact audit is:

```powershell
$artifactRoot = "C:\Dev\lisjong-artifacts"
$hits = Get-ChildItem $artifactRoot -Recurse -File -ErrorAction SilentlyContinue |
  Select-String -Pattern '\b522\d\d\b' -ErrorAction SilentlyContinue

if ($hits) {
    $hits | Select-Object Path, LineNumber, Line
    throw "52200..52299 has local artifact hits; choose a different fresh range."
}

"NO LOCAL TEXT-ARTIFACT SEED HITS: 52200..52299"
```

Also account for any private/non-text allocations known to the operator. The
lock requires the explicit `--external-freshness-confirmed` flag and fails
closed on repository-known collisions.

## Lock

```powershell
$root = "C:\Dev\lisjong-artifacts\issue-297-champion-hand-value-v2"
New-Item -ItemType Directory -Force $root | Out-Null

python -m lisjong_arena.champion_hand_value_v2_screen lock `
  --out "$root\pre-execution-lock.json" `
  --seeds 52200:52299 `
  --workers 8 `
  --external-freshness-confirmed `
  --strength-artifact "$root\strength-artifact.json" `
  --composition-trace "$root\composition-trace.json" `
  --result "$root\result.json" `
  --classified-result "$root\classified-result.json"
```

Post the resulting lock identity and exact provenance to Issue #297 before
running the scientific screen.

## Run

```powershell
python -m lisjong_arena.champion_hand_value_v2_screen run `
  --lock "$root\pre-execution-lock.json"
```

The CLI reports progress every 10 games and at completion.

The write-once evidence is:

- `pre-execution-lock.json`
- `strength-artifact.json`
- `composition-trace.json`
- `result.json`
- `classified-result.json`

The result is exactly one of:

- `CHAMPION + HAND VALUE V2 POSITIVE SIGNAL`
- `CHAMPION + HAND VALUE V2 NEGATIVE SIGNAL`
- `CHAMPION + HAND VALUE V2 INCONCLUSIVE`
- technical/provenance failure before valid evidence: `STOP / INVALID`

Do not extend or replace the locked seeds after result exposure.
