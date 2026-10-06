# RiichiLab上位botの選定と期間限定履歴取得（Issue #441）

自botの対局履歴（#269 self-history）には同卓者のidentityがない。#441の上位bot別比較では、
固定規則で選んだ上位botの公開履歴`/api/v1/bots/{bot_id}/games`を指定期間だけ取得し、
game IDで自botの対局と照合する。

## 選定規則（`select`）

公開leaderboard（`/api/v1/leaderboard`、1 page最大100件）のsnapshotに次を機械的に適用する。

- rating 1800以上（R1800ロードマップの目標水準）
- `total_games`が1000より多い
- `last_played_at`が取得時刻（UTC wall clock）から30日以内。`last_played_at`は
  timezone-naiveなので変換しない。境界から24時間以内のbotは`borderline_idle`として報告に残す

rating 1800を下回るentryが出るまでoffsetを進めて読む（1 pass）。page間でbot_idが重複する、
ratingや順位が逆転している場合は不整合として停止する。続けてもう1 pass読み、両passの
bot_idの並び（閾値を下回る最初のentryまで）と各botの判定が一致したときだけ、2 pass目の値を
採用する。取得中の変動で一致しない場合は3回まで2 passをやり直し、それでも一致しなければ停止する。
閾値の下まで届かない場合も停止する。raw page（`leaderboard-pages.json`、attempt / pass付き）と、
全候補の判定理由・閾値を下回った最初のentry・選定結果（`selection.json`）を保存する。

`fetch`は`selection.json`を読む際に、規則が現行と同じこと、候補が必要なfieldをすべて持つこと、
並びが正しく閾値の下まで届いていることを確認し、候補情報と基準日時から判定を再計算する。
保存された判定・選定IDと一致しなければ停止する。

## 期間限定取得（`fetch`）

`selection.json`で選ばれた全botについて、新しい順にpageを読み、`--played-from`より古い
行を含むpageを読み終えた時点で止める。

- credential-free public GETのみ。#269と同じbounded retry・0.5秒間隔の逐次requestを使う
- metadataだけを取得し、MJAI logは取得しない（自bot側の取得済みlogを使う）
- `--played-from` / `--played-to`はtimezone-naiveで、`played_at`と同じwall clockとして比較する
- 取得中の先頭追加でpage境界に出る同一内容の重複は除外する。内容の異なる重複、total減少、
  `played_at`の順序違反、`--max-pages`超過（bot単位）はfail closed
- 出力はbotごとに`bot-<id>/pages/`のraw pageと、全検証後の`window.json`（対象期間の行、
  件数、`window_identity`）

出力先はいずれもGit worktree外の存在しないか空のdirectoryに限る。出力はcommitしない。

```bash
OUT="$HOME/lisjong-artifacts/riichilab/i441-coplayer"
python -m lisjong_arena.riichilab_coplayer select --output-dir "$OUT/selection"
python -m lisjong_arena.riichilab_coplayer fetch \
  --selection "$OUT/selection/selection.json" \
  --played-from 2026-09-30T15:04:13 \
  --played-to 2026-10-01T07:19:36 \
  --max-pages 2000 \
  --output-dir "$OUT/windows"
```

照合と集計は#441側の診断で行い、このtoolは選定と同卓者identityの取得だけを担う。
