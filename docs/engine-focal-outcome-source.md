# L0.3 lisjong-engine focal outcome source

Issue: lisbun/lisjong-arena#370（parent lisbun/lisjong-project#79）。

`arena-offense-l0.3-lisjong-engine-focal-outcome-source-v1`は、RiichiEnv source
（[focal-outcome-source.md](focal-outcome-source.md)、
`arena-offense-l0.3-focal-outcome-source-v1`）とは**別のbackend / source lineage**である。
#362のRiichiEnv sourceをrescue・retry・再解釈するものではない。
RiichiEnv schemaの意味とhandlingは変更しない。

Arenaは事実だけを記録する。target
`lisjong-offense-l0.3-focal-kyoku-point-delta-1000-v1`
（`target_q = (points_after_kyoku[focal] - points_before_kyoku[focal]) / 1000`）は
lisjongが所有し、Arenaは算出も公開もしない。

実装は`src/lisjong_arena/focal_outcome_source/engine_source.py`にある。

## Kyoku boundary

RiichiEnv event deltaからの再構成は行わない。lisjong-engineの
`CompletedMatch.history`（`CompletedRound`）のauthoritative settlementだけを使う。

```text
round identity        CompletedRound.position_before（prevailing_wind / hand_number / honba）
dealer_seat           position_before.dealer_seat
riichi_sticks_before  position_before.riichi_sticks
riichi_sticks_after   settlement.riichi_sticks_after
point_deltas          settlement.point_deltas
points_after_kyoku    scores_after_settlement
points_before_kyoku   scores_after_settlement - point_deltas
```

最後の式は精算の再実装ではない。`MatchState.settle_active_round()`自体が
`scores_after = scores_before.add(settlement.point_deltas)`をhanchan最終配分の前に
計算しており、その厳密な逆である。

- 最終kyokuでも`CompletedMatch.final_raw_scores`を`points_after_kyoku`に使わない。
  残存供託の最終配分はgame summaryの`final_riichi_stick_awards`と
  `hanchan_final_raw_scores`へaudit factとして別に保存する
- producerとreaderは次をfail closedで検証する
  - `points_before_kyoku + point_deltas == points_after_kyoku`（4 seat）
  - `sum(point_deltas) + 1000 * (riichi_sticks_after - riichi_sticks_before) == 0`
  - kyoku間のscore / 供託本数の完全な連続（producerは加えて
    `previous.next_position == position_before`）
  - round identityがgame内で一意
  - `sum(awards) == 1000 * final.riichi_sticks_after`、かつ
    `hanchan_final_raw_scores == final.points_after_kyoku + awards`
- 全員聴牌の流局（RiichiEnv #247で`ryukyoku.deltas`が不整合になったpattern）も
  engine settlementをそのまま使うため、特別扱いを必要としない

## 対局構成

RiichiEnv sourceと同じ#79 A4構成である。

```text
focal seat      = game_ordinal % 4
                  既存FocalExplorationPolicy（select_residual_exploration + 既存token規則）
other 3 seats   ConstantResidualRuntime().create_policy()（game / seatごとにfresh）
rules           RuleSet.default()（project-standard-v1 / version 1）
execution       lisjong_arena.lisjong_engine.hanchan.run_policy_hanchan
```

focal decisionはadapterのcaptureを、PolicyInputの`round_wind / hand_number / honba`で
completed kyokuへbindする。一致が0件・複数件、dealer seat不一致はfail closedである。

## RiichiEnv v1 schemaとの差分

wire layout（`manifest.json` / `game-NNN/kyokus.jsonl` /
`game-NNN/focal-decisions.jsonl`）、canonical JSON、seal、`behavior`、token規則は同じ。
fieldの差分は次のとおり（すべてstrict。未知・欠損fieldはreject）。

| 場所 | RiichiEnv v1 | engine v1 |
|---|---|---|
| manifest `schema` | `arena-offense-l0.3-focal-outcome-source-v1` | `arena-offense-l0.3-lisjong-engine-focal-outcome-source-v1` |
| manifest `population_role` | `CALIBRATION` / `SCIENTIFIC` | `DIAGNOSTIC` / `CALIBRATION` / `SCIENTIFIC` |
| `source_contract` | `arena_revision`, `backend{name,version}`, `dependencies`, `game_mode`, `python` | `arena_revision`, `backend: "lisjong-engine"`, `dependencies{lisjong, lisjong-engine}`, `rules`, `python` |
| game summary | `hanchan_final_scores`, `hanchan_final_riichi_sticks` | `hanchan_final_raw_scores`, `final_riichi_stick_awards[{recipient_seat, amount}]`, `match_end_reason` |
| kyoku row | — | `point_deltas`を追加 |
| kyoku `end.draw_kind` | MJAI `ryukyoku.reason` | `exhaustive`、または`AbortiveDrawReason.value` |
| decision row | `step_ordinal`あり | `step_ordinal`なし（engineにauthoritativeな対応factがない） |

decision rowは`kyoku_ordinal`が非減少であることを要求する（`step_ordinal`の狭義増加
検査の代わり）。

## Provenance

`source_contract`は次を固定する。

```text
arena_revision   実行時のclean Arena checkout HEAD
backend          lisjong-engine
dependencies     lisjong        aed9c840bc120471e557fc0c8444965c0b81a9c3
                 lisjong-engine 96b9796c76ef5db8f3968f689a1ca6f3dfc9aa3b
rules            {"constructor": "RuleSet.default", "name": "project-standard-v1", "version": 1}
```

lisjong-engine revisionは#366 / #367で400 / 400 PASSしたものであり、repository全体の
`pyproject.toml` pinとは独立にこのsourceが固定する。`build_source_contract()`は
installed `direct_url.json`のexact VCS revisionを照合するため、生成は両revisionを
installした独立環境で行う。

population roleごとのallocation:

- `DIAGNOSTIC`: `allocation_bindings`は空objectに限る。scientific evidenceにならない
- `CALIBRATION` / `SCIENTIFIC`: splitごとのper-split bindingを完全一致で要求し、
  `seed_domain`は`lisjong-engine-project-standard-v1-hanchan-v1`に限る

## Readback

`verify_engine_focal_outcome_source()`がArena側のstrict readerである。schema / kind /
seal / canonical JSON / field集合 / file digest / population / provenance /
kyoku continuityを検証し、各decisionでtokenを再導出し、lisjongの
`select_residual_exploration()`を再実行してsurvivorとselected actionを照合する。
他schema（RiichiEnv v1を含む）はrejectする。

lisjongの現在のconsumerはRiichiEnv v1 schemaだけを受け付ける。engine schemaの
consumer対応は#79で最小のlisjong側変更として扱う。

## Diagnostic smoke

```text
python scripts/smoke_l03_engine_source_370.py --arena-checkout <clean checkout> --output-dir <new dir>
```

seed `910000..910015`（focal seatごとに4 hanchan）をDIAGNOSTIC roleで生成し、strict
readbackする。Seed Registry reservation、target構築、training、strength比較は行わない。

## C0 / C2 runner（#372）

`scripts/l03_engine_source_372.py`はSeed Registryで予約済みのCALIBRATION（C0）または
TRAIN / SELECT（C2）allocationから、上記producerをsequentialに1回実行する。
producerとsource schemaは変更しない。

```text
# producer環境（lisjong aed9c84 / lisjong-engine 96b9796、clean Arena checkout）
python scripts/l03_engine_source_372.py generate --arena-checkout <checkout> \
    --output-dir <new dir> --population-role CALIBRATION \
    --owner-issue lisbun/lisjong-arena#372 --seed-ledger <live ledger> \
    --allocation CALIBRATION <allocation_identity> <authorizing ledger>

# consumer環境（lisjong 8d2ada48）
python scripts/l03_engine_source_372.py readback --source <dir>/source \
    --generation <dir>/generation.json --output <new report>
```

- `generate`はlive ledgerでallocationのowner / engine seed domain / split / active
  stateを検証し、allocationの`arena_revision`が実行checkoutのHEADと一致すること、
  diagnostic seed `910000..910399`と重ならないことを要求する
- game順はsplit順（TRAIN -> SELECT）、split内はseed membership順。wall-clockと
  worker数（1）は`generation.json`へ記録し、sourceには入れない
- `readback`はlisjongだけをimportし、consumer revisionを照合してから
  `summarize_outcome_targets()`をsplitごとに実行する。support sanity check
  （CALIBRATIONまたはTRAINのcanonical-first / non-canonical-first selected >= 20%）と、
  CALIBRATIONでは#372 §4のC1 sizing（`N = 4 * ceil(K / (4k))`、整数演算）を出す
