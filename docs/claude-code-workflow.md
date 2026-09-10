# Claude Code workflow (Skills / Hooks)

このdocumentは、`lisjong-arena`で運用しているrepository-local Claude Code
Skills / Hooksの現状を、operator向けに簡潔に説明する。

## 正本との関係

このdocumentはSkills / Hooksの現在の挙動の要約であり、repository policy、
architecture、ownership、承認境界の正本ではない。それらは常に
[`AGENTS.md`](../AGENTS.md)を正本とする。本documentと`AGENTS.md`が食い違う
場合は`AGENTS.md`を優先する。

Skillsは`AGENTS.md`が定める開発flow（Issue -> branch -> 実装 -> test -> commit
/ push -> PR）を繰り返し実行するための、repeatableな手順書に過ぎない。Skills
自体が新しいpolicyや承認境界を作ることはない。

## 現在のSkills

Skillsは`.claude/skills/<name>/SKILL.md`にあり、`/<name> <引数>`の形で
user-invokedとして実行する。

| Skill | 引数 | 用途 |
| --- | --- | --- |
| `/implement-issue <issue-number>` | Issue番号 | `AGENTS.md`とIssueを読み、branch作成から実装、focused test、commit/push、PR作成・Ready化までを一連で行う |
| `/self-review <issue-number>` | Issue番号 | 現在のbranch差分をmainと比較して独立review。fileを変更せず、blocking / non-blocking findingのみ報告する |
| `/address-review <pr-number>` | PR番号 | 既存PRのblocking review findingを、scopeを広げずに最小限修正しcommit/pushする |
| `/finalize-pr <pr-number>` | PR番号 | 既存PRの最終pre-review検証（test証跡、Ruff、CI状況、未解決review）を確認し、mergeはしない |
| `/evaluation-preflight <issue-number-or-protocol>` | Issue番号またはprotocol名 | costlyまたはresult-producingなArena評価実行の前に、protocol要件を read-only で検証する |

いずれのSkillも`disable-model-invocation: true`であり、side-effectを持つ
workflow Skillsはmodelが自発的に呼び出すものではなく、ユーザー（または
ユーザーの明示的な指示）からuser-invokedで起動する。

## 現在のHooksの責務

Hooksは`.claude/hooks/workflow_guard.py`と`.claude/hooks/git_global_option_guard.py`
にあり、`.claude/settings.json`のPreToolUse hookとして、`git push`・
`git commit`・`gh pr create`（およびGit global optionを挟んだ同等形）を
実行前に検査する。

Hooksが担うのは、次のdeterministicで常に無効・機械的に判定可能な操作の
guardのみである。

- `main`を更新しうる直接pushの拒否（default push、`main`を明示targetとする
  refspec、`--all` / `--mirror`等を含む）
- 明らかに禁止されたcredential / model-artifact系file名class（`.env`、
  秘密鍵、`.ckpt` / `.safetensors`等のmodel weight拡張子等）を含むcommitの
  拒否
- `gh pr create`前の軽量なdeterministic check（`git diff --check`、
  `ruff format --check`、`ruff check`、forbidden file classの再検査）の
  実行

通常のfeature branchへのpushはこのHooksの対象外であり、引き続き許可される。

## Hooksが担わないこと

- **merge判定はHooksで静的にblockされない。** merge可否は`AGENTS.md`が
  定めるユーザーの明示的承認に依存し、自動化されていない。
- **research / one-shot evaluationのintegrity保証はHooksの責務ではない。**
  評価protocolのseed固定・duplicate layout・one-shot制約等はapplication /
  protocol code側で強制するものであり、`/evaluation-preflight`が行う確認も
  read-onlyな事前確認にとどまる。Claude Hooksはあくまで追加のoperator-error
  guardであり、research-integrity boundaryそのものではない。
- **GitHub Actionsに代わるものではない。** `gh pr create`前のHooks checkは
  軽量なローカル確認であり、GitHub Actionsが実行するfull CIの代替ではない。
  full test suite等の正式なpre-merge判定は引き続きGitHub Actionsを正本とする。

## 関連

- [`AGENTS.md`](../AGENTS.md): repository policy / architecture / ownership /
  承認境界の正本
- `.claude/skills/`: 各SkillのSKILL.md本体
- `.claude/hooks/`、`.claude/settings.json`: Hooks実装とfiring条件
