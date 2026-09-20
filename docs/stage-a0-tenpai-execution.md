# Stage A0 Tenpai execution (#262)

This package executes the completed #259 Lock B exactly once. It does not
redesign the experiment.

## Locked identity

- Lock B:
  `a7f1c471a8c2f983c5efab9147651eb48b8788eecb1aa296222e21fd9418e93e`
- TRAIN: 245..264
- VALIDATION: 265..270
- protected TEST: 271..276 (payload remains unread)
- training seeds: 0 / 1 / 2
- interactive anchor: 0
- downstream opponent: `mechanism-riichi-defense x3`
- downstream ordered seeds: 37700..37905
- downstream budget: 206 paired blocks / 1,648 games

## One-shot execution

Run only after the implementation is merged and the local checkout is clean at
that merged revision. Use a **new** output directory. The CLI refuses an
existing output root so the scientific run cannot silently overwrite or extend
an exposed result.

```powershell
python -m lisjong_arena.stage_a0_tenpai_execution execute `
  --lock-b <path-to-259-lock-b.json> `
  --dataset <path-to-retained-dataset> `
  --sidecar <path-to-scientific-sidecar> `
  --public-keys <path-to-scientific-public-keys> `
  --out-root C:\Dev\lisjong-artifacts\issue-262-stage-a0-execution `
  --workers 8
```

Execution order is fixed:

1. strict Phase E0 preflight;
2. A/T training for seeds 0, 1, 2;
3. freeze and strict-read all six checkpoints;
4. Gate A0.5 exactly once;
5. only on `TENPAI LEARNABILITY PASS`, run the locked downstream A/T
   comparison;
6. write and strict-validate `result.json`.

The retained tensor reader reads only the byte prefix represented by scientific
seeds 245..270. It deliberately does not call the whole-artifact
`feature_bytes()` / `legal_mask_bytes()` helpers because the retained
artifact also contains protected TEST rows 271..276.

## Outputs

```text
<out-root>/
  preflight.json
  checkpoints/
    A_seed0/
    T_seed0/
    A_seed1/
    T_seed1/
    A_seed2/
    T_seed2/
  gate-a0.5.json
  downstream/                 # only after Gate PASS
    arm-a.json
    arm-t.json
    downstream-result.json
  result.json
```

Generated datasets, checkpoints and scientific result artifacts stay outside
Git.
