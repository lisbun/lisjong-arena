# RiichiLab development profile

`lisjong-dev` is the development RiichiLab execution identity owned by Arena.

## Current Policy mapping

As of Issue #402, the RiichiLab serving mappings are:

```text
profile             lisjong-dev
credential source   LISJONG_DEV_BOT_TOKEN
runtime namespace   lisjong-dev
Policy              PlacementAwareSpeedCallPolicy

profile             lisjong-baseline
credential source   LISJONG_BASELINE_BOT_TOKEN
runtime namespace   lisjong-baseline
Policy              PlacementAwareSpeedCallPolicy
```

`lisjong-dev` and `lisjong-baseline` therefore run the same current Heuristic Champion while keeping separate credentials and runtime namespaces. Production profile `lisjong` remains on `MinimalPolicy`.

The `lisjong-dev` profile is shared by the first-party validation and ranked entry points, so both use the same current Policy mapping:

```powershell
python -m lisjong_arena.riichilab.validation --profile lisjong-dev
python -m lisjong_arena.riichilab.ranked --profile lisjong-dev
```

The startup summary derives the Policy label from the actual Policy instance. A ranked run should therefore report:

```text
profile: lisjong-dev
policy: PlacementAwareSpeedCallPolicy
mode: ranked
```

## One-hanchan observation and acquisition smoke

For a one-off live RiichiLab smoke with durable acquisition:

```powershell
python -m lisjong_arena.riichilab.ranked `
  --profile lisjong-dev `
  --record-dir C:\Dev\lisjong-artifacts\riichilab
```

The existing ranked CLI remains a one-game primitive: one connection, one ranked hanchan, then return after `end_game`. This profile change does not add retry, reconnect, or automatic requeue.

During the game, use the RiichiLab game viewer to confirm that the live game can be observed. After completion, confirm that the durable record was published and can be strict-read.

A completed record should preserve the execution identity in provenance:

```text
profile_identity = lisjong-dev
policy_identity  = PlacementAwareSpeedCallPolicy
```

The durable record is the information visible to lisjong during the game. It is not an omniscient server log and does not provide opponents' concealed hands, the wall, or future draws as ground truth.

## Interpretation boundary

The selected Policy is the current Heuristic Champion. Binding it to the two RiichiLab profiles is a serving/configuration change; a live RiichiLab run does not by itself establish:

- hanchan superiority,
- RiichiLab superiority,
- universal superiority,
- production `lisjong` default status.

The first live run should be treated as a connectivity, observation, and data-acquisition smoke rather than a strength evaluation.
