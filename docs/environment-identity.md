# Cross-repository development environment identity

## Purpose

lisjong ecosystem の internal Python dependency は、package version だけでなく
**declared exact VCS revision** を runtime identity として扱う。

現在の package version は `0.1.0` を維持しているため、次は同値ではない。

```text
installed version == 0.1.0

installed source revision == consumer pyproject.toml の exact VCS pin
```

consumer checkout を更新した後、既存 virtualenv に同一 version の古い VCS
distribution が残ると、import 自体は成功しても別 revision の semantics で
evaluation / smoke / artifact generation を実行できてしまう。この文書はその
fail-open を防ぐ current contract を定める。

## Triggering incident: 2026-09-19

`lisjong-play #41` の post-merge RiichiLab live-viewer smoke 前に、current
`lisjong-play/pyproject.toml` は次を要求していた。

```text
lisjong        f29d129c67e5232d06563c6e457754377734ed14
lisjong-engine 8735e89e1aea000ab59368d0368d476787827741
lisjong-arena  60cf4df92d68ed7bc347bf87d6bd0a2544da7120
Arena 60cf4df -> riichienv==0.4.10
```

既存 Windows virtualenv で checkout 更新後に通常の editable install を行った際、
`riichienv` は 0.4.10 へ更新された一方で installed `lisjong-arena 0.1.0`
の PEP 610 provenance は古い revision
`1d309787755af298229b6b1244f0d735ef1f2100` のまま残った。

その installed Arena metadata は古い

```text
lisjong @ 1bc334b...
lisjong-engine @ 0ba2e83...
riichienv==0.4.8
```

を要求しており、`python -m pip check` は Arena と `riichienv 0.4.10` の
不整合を検出した。Arena を明示 refresh すると、次は stale `lisjong 0.1.0`
が露出し、current Policy symbol の ImportError になった。

incident environment で確認できている条件は次。

```text
OS / shell     Windows / PowerShell
Python         CPython 3.14.6
pip            26.1.2
package policy internal packages all 0.1.0
cache state    not captured
outcome        REPRODUCED operationally
```

cache state は incident 時に固定されていない。このため、この問題を特定 pip
version / cache behavior の workaround として解かない。current verifier は
install algorithm を推測せず、**install 後の authoritative metadata identity**
を検証する。

## Canonical identity contract

internal VCS dependency の installed identity は次の組で判定する。

```text
distribution name
declared repository URL
declared expected full commit
installed repository URL from direct_url.json
installed commit_id from direct_url.json
installed package version
```

expected direct pin の source of truth は、verification 対象 checkout の
`pyproject.toml [project].dependencies` である。commit constant を別ファイルへ
複製しない。

relevant transitive internal dependency は、installed distribution の
`Requires-Dist` に現れる exact VCS pin を辿る。root checkout が同じ package を
別 revision で direct pin している場合、revision disagreement 自体を error にする。

この設計により triggering incident のように

```text
current lisjong-play -> current lisjong pin
stale installed Arena -> old lisjong pin
```

が同時に存在する状態を、import 前の metadata だけで検出できる。

## Verification command

consumer repository root で実行する。

```powershell
python -m lisjong_arena.environment_verify --project pyproject.toml
```

成功:

```text
ENVIRONMENT CONSISTENT
  lisjong        <40-hex commit> (0.1.0)
  lisjong-arena  <40-hex commit> (0.1.0)
  lisjong-engine <40-hex commit> (0.1.0)
  pip check: OK
```

failure 例:

```text
ENVIRONMENT MISMATCH
  lisjong-arena: stale internal dependency:
  expected: ...
  installed: ...
```

mismatch、missing `direct_url.json`、repository URL mismatch、malformed VCS
metadata、internal pin disagreement、または `pip check` failure は non-zero。

verification は package metadata と local `pyproject.toml` だけを読み、
network access を行わない。

## What pip check proves

`python -m pip check` は installed package metadata の version requirement
consistency を検証するため併用する。

ただし、同一 version の VCS package が別 commit であることは、それだけでは
証明できない。したがって、

```text
pip check success
!= exact VCS revision success
```

である。VCS identity は PEP 610 `direct_url.json` を別に検証する。

手動 debug:

```powershell
python -c "from importlib import metadata; print(metadata.distribution('lisjong-arena').read_text('direct_url.json'))"
```

## Canonical repair / bootstrap

stale existing environment の canonical repair は **clean virtualenv bootstrap**
とする。blind package-by-package uninstall / reinstall を標準手順にしない。

Windows PowerShell:

```powershell
deactivate
Remove-Item -Recurse -Force .venv
py -3.14 -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
python -m lisjong_arena.environment_verify --project pyproject.toml
```

POSIX:

```bash
deactivate 2>/dev/null || true
rm -rf .venv
python3.14 -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[dev]"
python -m lisjong_arena.environment_verify --project pyproject.toml
```

最後の verifier が PASS することまでを bootstrap completion とする。clean venv
を選んだ理由は、既存 environment の package selection history / uninstall order /
cache history を repair algorithm の前提にしないためである。

## Mandatory verification points

次の前には current consumer checkout で verifier を PASS させる。

- test / important smoke after an internal VCS pin change
- RiichiLab validation / ranked / continuous ranked / live-viewer smoke
- result-producing Policy evaluation
- formal / locked evaluation preflight
- durable artifact generation where package provenance matters

formal protocol がさらに exact non-editable install 等を要求する場合は、その
stronger requirement も満たす。environment verifier は protocol-specific
provenance validation の代替ではない。

## Versioning decision

current choice:

```text
A. keep 0.1.0
   + exact VCS commit identity
   + fail-closed verifier
```

採用しないもの:

- every-commit semantic version bump
- この問題だけを理由にした lockfile / second dependency authority
- Poetry / uv / Conda migration
- generic package-manager abstraction

semantic version は source revision identity の代用として使わない。将来 independent
release lifecycle や published compatibility contract が必要になった場合は、versioning
policy を別 Issue で再検討する。

## Scope and limitations

- current internal repository detection は `https://github.com/lisbun/lisjong*`
  exact VCS dependency を対象とする
- full lowercase 40-hex commit を要求する
- internal VCS dependency marker は current workflow では使わず、見つけた場合は
  unsupported state として fail closed する
- verifier は environment を自動修復しない
- formal evaluation 中に package を silent upgrade しない
- local editable source checkout 自体の git HEAD identity を証明するものではない
  （formal protocol が必要なら既存の checkout / artifact provenance preflight を併用する）
