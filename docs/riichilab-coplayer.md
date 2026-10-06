# RiichiLab上位botの期間限定履歴取得（Issue #441）

自botの対局履歴（#269 self-history）には同卓者のidentityがない。#441の上位bot別比較では、
#170で固定した上位bot（`TARGET_BOTS`: FuuroMaster 126、Mortal-v4b 120、zero-test2 294）の
公開履歴`/api/v1/bots/{bot_id}/games`を指定期間だけ取得し、game IDで自botの対局と照合する。

- credential-free public GETのみ。#269と同じbounded retry・0.5秒間隔の逐次requestを使う
- metadataだけを取得し、MJAI logは取得しない（自bot側の取得済みlogを使う）
- 対象は`TARGET_BOTS`に限る。bot一覧の総当たりはしない
- 新しい順にpageを読み、`--played-from`より古い行を含むpageを読み終えた時点で止める
- `--played-from` / `--played-to`はtimezone-naiveで、`played_at`と同じwall clockとして比較する
- 取得中の先頭追加でpage境界に出る同一内容の重複は除外する。内容の異なる重複、total減少、
  `played_at`の順序違反、`--max-pages`超過はfail closed
- 出力先はGit worktree外の存在しないか空のdirectory。`pages/`にraw page、全検証後に
  `window.json`（対象期間の行、件数、`window_identity`）を書く。出力はcommitしない

```bash
python -m lisjong_arena.riichilab_coplayer fetch \
  --bot-id 120 \
  --played-from 2026-09-30T15:04:13 \
  --played-to 2026-10-01T07:19:36 \
  --max-pages 2000 \
  --output-dir "$HOME/lisjong-artifacts/riichilab/i441-coplayer/bot-120"
```

照合と集計は#441側の診断で行い、このtoolは同卓者identityの取得だけを担う。
