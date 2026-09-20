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

## Dedicated locked environment

Current `main` may pin a newer lisjong revision than the #259 downstream lock.
Do not change Lock B to follow `main`. Create a dedicated #262 virtual
environment, install the merged Arena checkout, and then override only lisjong
to the exact locked downstream revision:

```powershell
py -3.14 -m venv .venv-262
.\.venv-262\Scripts\Activate.ps1
python -m pip install -e ".[ml,dev]"
python -m pip install --no-deps --force-reinstall `
  "lisjong @ git+https://github.com/lisbun/lisjong.git@f29d129c67e5232d06563c6e457754377734ed14"
```

Do not run `pip install -e .` again in that environment afterward, because it
would restore the current repository pin. Phase E0 verifies the installed
lisjong, lisjong-engine and RiichiEnv identities before any model training.

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
