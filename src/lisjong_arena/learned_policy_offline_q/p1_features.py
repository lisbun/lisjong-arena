"""P1 derived representation — locked v1 + keep-shanten discard mask (Issue #158).

`lisbun/lisjong-arena #158`は、#152が確認した`HAND-PROGRESSION DEGRADATION
IDENTIFIED`に対して、**deterministic Mahjong featureをちょうど1 family**だけ
current simple Offline Qへ追加したときにhand progressionが改善するかを、
既存retained artifactだけでboundedに検証する。このmoduleはその1 familyの
derived representation contractを所有する。

```text
locked v1 row            8204   arena-policy-input-feature-v1（値をverbatimに保持）
keep-shanten discard mask  37   canonical TILE_AXIS順
--------------------------------
P1 derived row           8241   arena-learned-policy-offlineq-p1-keep-shanten-feature-v1
```

**v1をbreaking changeしない。** `learned_policy_input`のsemantics ID、tensor
schema version、dimension、fingerprintはこのmoduleから一切変更されない。P1は
experiment-localな**派生**representationとして別のsemantics ID / schema
version / fingerprintを持ち、base v1 fingerprintを自分のfingerprintへ取り込む
ことで「どのbaseから派生したか」を機械的に固定する。

## 37-tile keep-shanten mask

各tile entryのvalueは:

```text
1.0   own concealed handにそのexact tile identityが1枚以上あり、
      exactly 1枚discardしたpost-discard shantenがpre-discard shantenと等しい
0.0   それ以外（handに無い場合を含む）
```

derivationはplayer-safeなown concealed handだけを入力にする。legal mask、
opponent hidden hand、wall truth、future state / outcome、teacher internal
analysisを読まない。shanten semanticsはArenaで再実装せず、#152が確立した

```text
locked v1 own_hand feature
  -> reconstruct_concealed_tiles()   exact整数枚数への逆写像 / 曖昧ならfail closed
  -> lisjong.hand_evaluation.calculate_shanten()
```

を`hand_progression.keep_shanten_tile_mask()`としてsingle source of truthのまま
再利用する。ukeire / waits / value / defense / HandBelief / current-shanten
one-hot等の第二feature familyはこのIssueで追加しない。

## Derived data/view

新しいgame、seed、hanchanを生成しない。locked retained transition rowsから
派生させるだけであり、row order、row identity、split membership、behavior
action、reward、terminal、legal maskは一切変更しない。

`next_features`にも同じderivationを適用してsource / nextのschemaを一致させる。
ただしterminal rowの`next_features`は既存contract上all-zero placeholderであり
（有効性は`terminal`が決める）、P1でも新しいsemanticsを発明せず**appended 37
entryもall-zero**にする。この規約はschema fingerprintへ含める。

一意にown handを復元できないrowは推測で補わず`OfflineQAmbiguousStateError`で
fail closedする。呼び出し側はこれを`P1 EVIDENCE INSUFFICIENT`として扱う。
"""

import hashlib
from dataclasses import dataclass

from lisjong_arena.learned_policy_input.feature import (
    FEATURE_SEMANTICS_ID,
    TILE_AXIS_SIZE,
)
from lisjong_arena.learned_policy_input.tensor import (
    FEATURE_DIM,
    FEATURE_INDEX_DESCRIPTORS,
    PADDING_SEMANTICS,
    TENSOR_DTYPE,
    TENSOR_SCHEMA_VERSION,
    TILE_AXIS_LABELS,
    schema_fingerprint,
)

from .artifact import feature_block
from .errors import OfflineQProtocolError
from .hand_progression import keep_shanten_tile_mask
from .protocol import (
    FEATURE_DIMENSION,
    LOCKED_FEATURE_SCHEMA_FINGERPRINT,
    LOCKED_FEATURE_SEMANTICS_ID,
    LOCKED_TENSOR_SCHEMA_VERSION,
)
from .replacement_test import ReplacementTestTensors
from .split_tensors import OfflineQSplitTensors

P1_FEATURE_SEMANTICS_ID = "arena-learned-policy-offlineq-p1-keep-shanten-feature-v1"
P1_TENSOR_SCHEMA_VERSION = "arena-learned-policy-offlineq-p1-keep-shanten-tensor-v1"
P1_TENSOR_DTYPE = TENSOR_DTYPE

KEEP_SHANTEN_DIMENSION = TILE_AXIS_SIZE
P1_FEATURE_DIMENSION = FEATURE_DIMENSION + KEEP_SHANTEN_DIMENSION

TERMINAL_NEXT_STATE_PADDING = "terminal_next_state_padding=all-zero"
"""terminal rowのnext-state placeholderはappended blockもall-zeroである。"""

KEEP_SHANTEN_INDEX_DESCRIPTORS = tuple(
    f"p1_keep_shanten_discard.tile[{label}]:binary" for label in TILE_AXIS_LABELS
)
P1_INDEX_DESCRIPTORS = (*FEATURE_INDEX_DESCRIPTORS, *KEEP_SHANTEN_INDEX_DESCRIPTORS)

LOCKED_P1_SCHEMA_FINGERPRINT = (
    "beae3eb8d3dc79b4f837ec98ed3b90eb7530e6f931102b8cdf4854bcf0a15409"
)
"""Issue #158のpreflight commentがresult exposure前にlockしたfingerprint。

training結果やGate A結果を見てこの値やdescriptorを変更しない。
"""


def p1_schema_fingerprint() -> str:
    """derived schemaのfingerprintを、全index descriptorとheaderから算出する。

    構成はv1 `learned_policy_input.tensor.schema_fingerprint()`と同じ
    （`"\\n".join(lines) + "\\n"`のUTF-8 sha256）で、headerだけをderived schema
    用に拡張し、base v1 fingerprintを取り込む。
    """
    lines = (
        f"derived_feature_semantics_id={P1_FEATURE_SEMANTICS_ID}",
        f"derived_tensor_schema_version={P1_TENSOR_SCHEMA_VERSION}",
        f"base_feature_semantics_id={FEATURE_SEMANTICS_ID}",
        f"base_tensor_schema_version={TENSOR_SCHEMA_VERSION}",
        f"base_schema_fingerprint={schema_fingerprint()}",
        f"dtype={P1_TENSOR_DTYPE}",
        f"base_feature_dim={FEATURE_DIM}",
        f"appended_feature_dim={KEEP_SHANTEN_DIMENSION}",
        f"feature_dim={P1_FEATURE_DIMENSION}",
        *PADDING_SEMANTICS,
        TERMINAL_NEXT_STATE_PADDING,
        *(f"{index}:{value}" for index, value in enumerate(P1_INDEX_DESCRIPTORS)),
    )
    return hashlib.sha256(("\n".join(lines) + "\n").encode("utf-8")).hexdigest()


if (
    FEATURE_DIMENSION != FEATURE_DIM
    or LOCKED_FEATURE_SEMANTICS_ID != FEATURE_SEMANTICS_ID
    or LOCKED_TENSOR_SCHEMA_VERSION != TENSOR_SCHEMA_VERSION
    or LOCKED_FEATURE_SCHEMA_FINGERPRINT != schema_fingerprint()
):
    raise RuntimeError(
        "the locked v1 feature contract drifted; the P1 derived schema is defined "
        "relative to it and must not be silently re-based"
    )
if (
    KEEP_SHANTEN_DIMENSION != 37
    or P1_FEATURE_DIMENSION != 8241
    or len(KEEP_SHANTEN_INDEX_DESCRIPTORS) != KEEP_SHANTEN_DIMENSION
    or len(P1_INDEX_DESCRIPTORS) != P1_FEATURE_DIMENSION
    or len(set(P1_INDEX_DESCRIPTORS)) != P1_FEATURE_DIMENSION
    or P1_INDEX_DESCRIPTORS[:FEATURE_DIMENSION] != FEATURE_INDEX_DESCRIPTORS
):
    raise RuntimeError("the P1 derived index descriptor layout drifted")
if p1_schema_fingerprint() != LOCKED_P1_SCHEMA_FINGERPRINT:
    raise RuntimeError(
        "the P1 derived schema fingerprint differs from the value locked in the "
        "Issue #158 preflight comment"
    )


def p1_feature_block() -> dict[str, object]:
    """derived schema identityを、base v1 identityと一緒に記録するblock。"""
    return {
        "semantics_id": P1_FEATURE_SEMANTICS_ID,
        "tensor_schema_version": P1_TENSOR_SCHEMA_VERSION,
        "dtype": P1_TENSOR_DTYPE,
        "dimension": P1_FEATURE_DIMENSION,
        "schema_fingerprint": LOCKED_P1_SCHEMA_FINGERPRINT,
        "base_feature": feature_block(),
        "appended_dimension": KEEP_SHANTEN_DIMENSION,
        "appended_feature_family": "keep_shanten_discard",
        "appended_index_descriptors": list(KEEP_SHANTEN_INDEX_DESCRIPTORS),
        "terminal_next_state_padding": "all-zero",
    }


def _error(message: str) -> OfflineQProtocolError:
    return OfflineQProtocolError(message)


def derive_p1_row(feature_values) -> tuple[float, ...]:
    """1つのv1 source state rowをP1 rowへ変換する。

    base 8204 valueは書き換えず、そのまま先頭へ保持する。
    """
    if len(feature_values) != FEATURE_DIMENSION:
        raise _error(f"a v1 feature row must have {FEATURE_DIMENSION} values")
    return (
        *(float(value) for value in feature_values),
        *keep_shanten_tile_mask(feature_values),
    )


def _appended_block(features, terminal):
    """(N, 37)のkeep-shanten blockを作る。terminal rowはall-zeroのまま残す。"""
    import torch

    row_count = int(features.shape[0])
    appended = torch.zeros((row_count, KEEP_SHANTEN_DIMENSION), dtype=torch.float32)
    derived = 0
    for index in range(row_count):
        if terminal is not None and bool(terminal[index]):
            continue
        appended[index] = torch.tensor(
            keep_shanten_tile_mask(features[index]), dtype=torch.float32
        )
        derived += 1
    return appended, derived


def derive_p1_feature_tensor(features, *, terminal=None):
    """(N, 8204) float32 tensorを(N, 8241)へ変換し、derived row数を返す。

    `terminal`を渡した場合、そのrowはnext-state placeholderとして扱い、
    appended blockをall-zeroのままにする（base側も既にall-zeroである）。
    """
    import torch

    if features.dim() != 2 or int(features.shape[1]) != FEATURE_DIMENSION:
        raise _error(f"features must be a (N, {FEATURE_DIMENSION}) tensor")
    if features.dtype is not torch.float32:
        raise _error("features must be a float32 tensor")
    if terminal is not None:
        if terminal.dtype is not torch.bool:
            raise _error("terminal must be a bool tensor")
        if int(terminal.shape[0]) != int(features.shape[0]):
            raise _error("terminal and features describe different row counts")
    appended, derived = _appended_block(features, terminal)
    return torch.cat((features, appended), dim=1).contiguous(), derived


@dataclass(frozen=True, slots=True)
class P1DerivationCoverage:
    """1 populationのderivation coverage。fail closedなので常に完全である。"""

    row_count: int
    source_rows_derived: int
    nonterminal_next_rows_derived: int
    terminal_next_rows_zero_padded: int

    def __post_init__(self) -> None:
        if self.source_rows_derived != self.row_count:
            raise _error(
                "P1 derivation did not cover every source row; ambiguous rows are "
                "failed closed, never imputed"
            )
        if (
            self.nonterminal_next_rows_derived + self.terminal_next_rows_zero_padded
            != self.row_count
        ):
            raise _error("P1 next-state derivation does not partition the rows")

    def to_document(self) -> dict[str, object]:
        return {
            "row_count": self.row_count,
            "source_rows_derived": self.source_rows_derived,
            "nonterminal_next_rows_derived": self.nonterminal_next_rows_derived,
            "terminal_next_rows_zero_padded": self.terminal_next_rows_zero_padded,
            "imputed_row_count": 0,
        }


def derive_split_tensors(
    tensors: OfflineQSplitTensors,
) -> tuple[OfflineQSplitTensors, P1DerivationCoverage]:
    """1 splitのtensorをP1 schemaへ変換する。feature以外は同一値を保持する。"""
    if not isinstance(tensors, OfflineQSplitTensors):
        raise TypeError("tensors must be an OfflineQSplitTensors")
    features, source_derived = derive_p1_feature_tensor(tensors.features)
    next_features, next_derived = derive_p1_feature_tensor(
        tensors.next_features, terminal=tensors.terminal
    )
    coverage = P1DerivationCoverage(
        row_count=tensors.row_count,
        source_rows_derived=source_derived,
        nonterminal_next_rows_derived=next_derived,
        terminal_next_rows_zero_padded=int(tensors.terminal.sum()),
    )
    derived = OfflineQSplitTensors(
        split=tensors.split,
        features=features,
        legal_mask=tensors.legal_mask,
        behavior_action_index=tensors.behavior_action_index,
        reward=tensors.reward,
        terminal=tensors.terminal,
        next_features=next_features,
        next_legal_mask=tensors.next_legal_mask,
        row_indices=tensors.row_indices,
    )
    return derived, coverage


def derive_all_split_tensors(tensors: dict) -> tuple[dict, dict]:
    """split単位のtensor dictをまとめてP1 schemaへ変換する。"""
    derived: dict = {}
    coverage: dict = {}
    for split, entry in tensors.items():
        derived[split], coverage[split] = derive_split_tensors(entry)
    return derived, coverage


def derive_replacement_test_tensors(
    tensors: ReplacementTestTensors,
) -> tuple[ReplacementTestTensors, P1DerivationCoverage]:
    """replacement TEST tensorをP1 schemaへ変換する。"""
    if not isinstance(tensors, ReplacementTestTensors):
        raise TypeError("tensors must be a ReplacementTestTensors")
    features, source_derived = derive_p1_feature_tensor(tensors.features)
    next_features, next_derived = derive_p1_feature_tensor(
        tensors.next_features, terminal=tensors.terminal
    )
    coverage = P1DerivationCoverage(
        row_count=tensors.row_count,
        source_rows_derived=source_derived,
        nonterminal_next_rows_derived=next_derived,
        terminal_next_rows_zero_padded=int(tensors.terminal.sum()),
    )
    derived = ReplacementTestTensors(
        features=features,
        legal_mask=tensors.legal_mask,
        behavior_action_index=tensors.behavior_action_index,
        reward=tensors.reward,
        terminal=tensors.terminal,
        next_features=next_features,
        next_legal_mask=tensors.next_legal_mask,
        row_count=tensors.row_count,
    )
    return derived, coverage


__all__ = [
    "KEEP_SHANTEN_DIMENSION",
    "KEEP_SHANTEN_INDEX_DESCRIPTORS",
    "LOCKED_P1_SCHEMA_FINGERPRINT",
    "P1_FEATURE_DIMENSION",
    "P1_FEATURE_SEMANTICS_ID",
    "P1_INDEX_DESCRIPTORS",
    "P1_TENSOR_DTYPE",
    "P1_TENSOR_SCHEMA_VERSION",
    "TERMINAL_NEXT_STATE_PADDING",
    "P1DerivationCoverage",
    "derive_all_split_tensors",
    "derive_p1_feature_tensor",
    "derive_p1_row",
    "derive_replacement_test_tensors",
    "derive_split_tensors",
    "p1_feature_block",
    "p1_schema_fingerprint",
]
