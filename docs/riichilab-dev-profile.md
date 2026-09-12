# RiichiLab development profile

`lisjong-dev` is the development RiichiLab execution identity owned by Arena.

## Current Policy mapping

As of Issue #223, the current mapping is:

```text
profile             lisjong-dev
credential source   LISJONG_DEV_BOT_TOKEN
runtime namespace   lisjong-dev
Policy              MechanismRiichiDefenseYakuhaiCallPolicy
```

`lisjong-baseline` and `lisjong` continue to use `MinimalPolicy`.

The `lisjong-dev` profile is shared by the first-party validation and ranked entry points, so both use the same current Policy mapping:

```powershell
python -m lisjong_arena.riichilab.validation --profile lisjong-dev
python -m lisjong_arena.riichilab.ranked --profile lisjong-dev
```

The startup summary derives the Policy label from the actual Policy instance. A ranked run should therefore report:

```text
profile: lisjong-dev
policy: MechanismRiichiDefenseYakuhaiCallPolicy
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
policy_identity  = MechanismRiichiDefenseYakuhaiCallPolicy
```

The durable record is the information visible to lisjong during the game. It is not an omniscient server log and does not provide opponents' concealed hands, the wall, or future draws as ground truth.

## Interpretation boundary

Arena #217 / #219 established positive evidence for this Policy under the locked ABBB / `4p-red-single` single-round protocol. Selecting it for `lisjong-dev` does not by itself establish:

- hanchan superiority,
- RiichiLab superiority,
- universal superiority,
- production `lisjong` default status.

The first live run should be treated as a connectivity, observation, and data-acquisition smoke rather than a strength evaluation.
