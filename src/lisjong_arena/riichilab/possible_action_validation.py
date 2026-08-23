"""送信予定Actionと、server提示`possible_actions`との送信前semantic validation(Arena-local canonical、Issue #27)。

lisjong Issue #38の中心責務。raw dict完全一致やlist indexへ依存せず、送信予定の
Bot-to-Server responseとserver candidateの両方を、同一の
`possible_actions` candidate semantic identityへprojectionしてから照合する。

```text
send-ready Bot response --projection--> candidate semantic identity
server candidate        --projection--> candidate semantic identity
                                        -> semantic equality
```

RiichiLab公式Protocolの`possible_actions` candidate schemaは、Bot-to-Server
response schemaより小さい最小表現である(lisjong Issue #38 review、
`docs/riichilab-protocol-bridge.md`参照)。そのためidentityは、公式candidateが
identityとして持つfield(`type` / `pai` / `consumed`)だけで構成し、
`actor` / `target` / `tsumogiri`をcandidateへ要求しない。

ただし、公式Protocolは`possible_actions`の例とAction別field表の間に記述差が
あり、candidateへこれらのfieldが付随し得ることまでは否定できない
(Issue #38 再レビュー)。そのためcandidate側に`actor` / `target`が実際に
存在する場合だけは、送信予定responseと矛盾しないことも確認する
(`_optional_fields_agree`)。

`hora`はさらに一歩進めて、公式Protocolの`request_action`例が
`{"type": "hora"}`というminimal candidateを示す一方、Action別field表には
`pai`等の追加fieldが記載されているという記述差がある(Issue #38 第3回
レビュー)。そのため`hora`の必須identityは`type`のみとし、`pai`はcandidate
側に存在する場合だけ送信予定responseと矛盾しないことを確認する
(`_optional_tile_consistency_agrees`)。存在するのに不正な牌表記であれば、
無視せずcandidate malformedとしてvalidation全体をfail closedする。

- semantic match 0件 -> reject
- semantic match 1件以上 -> accept

lisjong Issue #39の実RiichiLab `/ws/validate`で、同じsemantic Actionへ一致する
candidateが2件提示されることを確認した。candidate list内の重複はAction
identityや送信payloadの選択には使わず、合法性を確認できる一致が1件以上
あれば受理する。

`possible_actions`内に1件でもmalformed candidate、または未知のAction typeの
candidateが含まれる場合、他のcandidateが一致するかどうかにかかわらず
validation全体をfail closedする(Issue #38 再レビュー: forward compatibility
として許容するのは既知Action typeのunknown追加fieldであり、legal candidate
そのもののunknown Action typeやrequired field欠落ではない)。

`possible_actions[0]`等のarbitrary fallbackはこのmoduleを含め一切行わない。

lisjong PR #93がこのvalidation contractの表現を修正済み(docstring correction
のみ、runtime behavior変更なし)。lisjong Issue #38/#39/PR #93で確立した
contractをbehavior-preservingにArenaへphysical migrationしたものである。
"""

from collections.abc import Mapping, Sequence

from lisjong.policy_contract.tile import Tile, tile_sort_key

from lisjong_arena.riichienv.adapter import tile_from_mjai
from lisjong_arena.riichilab.adapter_errors import PossibleActionsValidationError

# `pai`(識別に使う牌1枚)をidentityへ持つAction type。`hora`はここに含めない
# (下記`_OPTIONAL_TILE_CONSISTENCY_FIELDS`を参照)。
_PAI_ONLY_TYPES = frozenset({"dahai"})

# `consumed`(手牌等から消費する牌の組)の枚数。RiichiEnv 0.4.8の
# `Action.to_mjai()`実測とlisjong Issue #38レビューで確認した公式candidate
# schemaに基づく。正本は`docs/riichilab-protocol-bridge.md`を参照。
#
# `kakan`は、公式candidate schemaが`pai`(加える牌)に加えて`consumed`
# (元Ponの3枚)を持つ(Issue #38 再レビューのblocking finding)。`pai`だけを
# identityとすると、同じ加槓牌でも元Pon構成が異なるcandidateを誤って同一
# 合法Actionとして受理し得るため、`consumed`もidentityへ含める。
_CONSUMED_COUNTS = {"chi": 2, "pon": 2, "daiminkan": 3, "ankan": 4, "kakan": 3}

# `pai`と`consumed`の両方をidentityへ持つAction type。`ankan`は`pai`を
# identityとして使わず、`consumed`の4枚だけで一意に定まる。
_PAI_AND_CONSUMED_TYPES = frozenset({"chi", "pon", "daiminkan", "kakan"})

# 追加のsemantic fieldを持たず、typeだけでidentityが定まるAction type。
# `hora`もここに含める: 公式`request_action`例は`{"type": "hora"}`という
# minimal candidateを示すため、`pai`をcandidate必須identityにしない
# (Issue #38 第3回レビュー)。`pai`が実際に存在する場合の整合確認は
# `_OPTIONAL_TILE_CONSISTENCY_FIELDS`で別途行う。
_TYPE_ONLY_TYPES = frozenset({"reach", "none", "ryukyoku", "hora"})

# candidate側に存在する場合だけ、送信予定responseと矛盾しないことを確認する
# field。`tsumogiri`は含めない: 公式candidate例は`tsumogiri`を持たず、
# 打牌は`pai`で一意に定まるため、candidate側の`tsumogiri`は仮に付随しても
# identityでも矛盾判定材料でもない(Issue #38 review: candidateへ
# `tsumogiri`を要求しない)。
_OPTIONAL_CONSISTENCY_FIELDS = ("actor", "target")

# Action typeごとに、candidate側で任意(optional)に持つtile fieldの名前。
# fieldがcandidateに存在しなければminimal candidateとして許容し、存在すれば
# 送信予定responseの同名fieldと矛盾しないことを確認する。存在するのに牌として
# parseできない場合はcandidate malformedとして扱う(Issue #38 第3回
# レビュー: `hora`のfield表とrequest例の記述差への対応)。
_OPTIONAL_TILE_CONSISTENCY_FIELDS = {"hora": "pai"}

# 単一のexcept節で複数typeを指定するとparenthesizeが必要になるが、ローカルの
# ruff format実行環境で括弧が意図せず削除される既知の問題があるため、named
# constantへ切り出して単一nameのexceptにしている。
_TILE_FROM_MJAI_ERRORS = (TypeError, ValueError)


class _IdentityProjectionError(Exception):
    """MJAI相当mappingをcandidate semantic identityへprojectionできない場合の内部例外。

    このmodule内部だけで使用し、呼び出し側へは
    `PossibleActionsValidationError`として送出しなおす(projection対象が
    server candidateか送信予定responseかで、報告すべき原因が異なるため)。
    """


def _sorted_tiles(tiles: Sequence[Tile]) -> tuple[Tile, ...]:
    return tuple(sorted(tiles, key=tile_sort_key))


def _tile_field(source: Mapping, field: str) -> Tile:
    value = source.get(field)
    if not isinstance(value, str):
        raise _IdentityProjectionError(f"{field} must be an mjai tile string")
    try:
        return tile_from_mjai(value)
    except _TILE_FROM_MJAI_ERRORS as error:
        raise _IdentityProjectionError(f"{field} is not a valid mjai tile") from error


def _tile_multiset_field(
    source: Mapping, field: str, expected_count: int
) -> tuple[Tile, ...]:
    value = source.get(field)
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise _IdentityProjectionError(f"{field} must be a list of mjai tile strings")
    items = tuple(value)
    if len(items) != expected_count:
        raise _IdentityProjectionError(
            f"{field} must contain exactly {expected_count} tiles, got {len(items)}"
        )

    tiles = []
    for item in items:
        if not isinstance(item, str):
            raise _IdentityProjectionError(f"{field} entries must be mjai tile strings")
        try:
            tiles.append(tile_from_mjai(item))
        except _TILE_FROM_MJAI_ERRORS as error:
            raise _IdentityProjectionError(
                f"{field} contains an invalid mjai tile"
            ) from error
    return _sorted_tiles(tiles)


def _semantic_identity(source: Mapping) -> tuple:
    """MJAI相当のaction mappingを`possible_actions` candidate identityへprojectionする。

    server candidateにも、送信予定のBot responseにも同じ関数を適用する
    ことで、raw dict完全一致に頼らず、かつ両者のschema差(candidate側に
    無い`actor` / `target` / `tsumogiri`等)へ影響されない照合を行う。
    """
    if not isinstance(source, Mapping):
        raise _IdentityProjectionError("action must be a mapping")

    action_type = source.get("type")
    if not isinstance(action_type, str):
        raise _IdentityProjectionError("action is missing a string type")

    if action_type in _TYPE_ONLY_TYPES:
        return (action_type,)

    if action_type in _PAI_ONLY_TYPES:
        return (action_type, _tile_field(source, "pai"))

    if action_type == "ankan":
        return (
            action_type,
            _tile_multiset_field(source, "consumed", _CONSUMED_COUNTS[action_type]),
        )

    if action_type in _PAI_AND_CONSUMED_TYPES:
        return (
            action_type,
            _tile_field(source, "pai"),
            _tile_multiset_field(source, "consumed", _CONSUMED_COUNTS[action_type]),
        )

    # forward compatibilityとして許容するのは既知Action typeのunknown追加
    # fieldまでであり、未知のAction type自体はsilent ignoreせずfail closed
    # する(Issue #38 再レビュー)。
    raise _IdentityProjectionError(f"unknown action type: {action_type!r}")


def _optional_fields_agree(candidate: Mapping, response: Mapping) -> bool:
    """candidateが任意で持つsemantic fieldが、送信予定responseと矛盾しないか確認する。

    公式Protocolは`possible_actions`の例とAction別field表の間に記述差が
    あるため、candidateが`actor` / `target`を持ち得ないとは断言できない
    (Issue #38 再レビュー)。candidate側に存在する場合だけ照合し、
    存在しなければidentityだけで判定する(minimal candidate形を拒否しない)。

    矛盾するcandidateは「別のAction候補」として非一致に倒す。結果として
    一致0件になればfail closedするため、誤受理は起こらない。
    """
    for field in _OPTIONAL_CONSISTENCY_FIELDS:
        if field not in candidate:
            continue
        expected = response.get(field)
        if expected is None:
            continue
        candidate_value = candidate[field]
        if isinstance(candidate_value, bool) or not isinstance(candidate_value, int):
            return False
        if candidate_value != expected:
            return False
    return True


def _optional_tile_field_agrees(
    candidate: Mapping, response: Mapping, field: str
) -> bool:
    """candidateが任意で持つtile fieldが、送信予定responseと矛盾しないか確認する。

    `field`がcandidateに存在しなければ、minimal candidate形として許容する
    (`True`を返す)。存在する場合は牌としてparseし、`response`側の同名
    fieldと一致するかどうかを返す。candidate側の値がmjai tile文字列として
    parseできない場合は、無視せず`_IdentityProjectionError`を送出する
    (呼び出し側でcandidate malformedとしてvalidation全体をfail closedする)。
    """
    if field not in candidate:
        return True

    candidate_tile = _tile_field(candidate, field)

    try:
        expected_tile = _tile_field(response, field)
    except _IdentityProjectionError:
        # responseがこのfieldを持たない、またはparseできない場合は判定材料が
        # ないため、candidate側の値だけを理由に拒否しない。
        return True

    return candidate_tile == expected_tile


def validate_against_possible_actions(
    response: Mapping, possible_actions: Sequence[object]
) -> None:
    """送信予定`response`が`possible_actions`へsemantic matchすることを確認する。

    `response`は`build_mjai_response()`が構築した、これからserverへ送ろうと
    しているBot-to-Server response相当のMJAI dictである。canonical
    `InternalAction`ではなく実際の送信内容を照合対象にすることで、
    `KakanAction`のようにInternalAction側が保持しない外部semantic情報
    (元Ponの`consumed`)も落とさずに検証できる。

    次のいずれもsend-ready payloadを返さずfail closedする。

    - `possible_actions`内にmalformed candidate、または未知Action typeの
      candidateが1件でも存在する
    - semantic match 0件

    semantic matchは1件以上あれば受理する。lisjong Issue #39のlive
    validationで、同じsemantic Actionに対応するwell-formed candidateが実
    serverから複数提示されることを確認したためである。

    一致したcandidateの値そのものは戻り値として使わない(送信payloadは
    あくまでresolve済みcanonical Actionから構築済みのものを使う)。
    """
    try:
        response_identity = _semantic_identity(response)
    except _IdentityProjectionError as error:
        raise PossibleActionsValidationError(
            f"send-ready response could not be projected onto the "
            f"possible_actions candidate schema: {error}"
        ) from error

    match_count = 0
    for index, candidate in enumerate(possible_actions):
        try:
            candidate_identity = _semantic_identity(candidate)
        except _IdentityProjectionError as error:
            raise PossibleActionsValidationError(
                f"possible_actions[{index}] is not a well-formed candidate: {error}"
            ) from error

        if candidate_identity != response_identity:
            continue

        action_type = candidate_identity[0]
        optional_tile_field = _OPTIONAL_TILE_CONSISTENCY_FIELDS.get(action_type)
        if optional_tile_field is not None:
            try:
                if not _optional_tile_field_agrees(
                    candidate, response, optional_tile_field
                ):
                    continue
            except _IdentityProjectionError as error:
                raise PossibleActionsValidationError(
                    f"possible_actions[{index}] is not a well-formed candidate: {error}"
                ) from error

        if not _optional_fields_agree(candidate, response):
            continue
        match_count += 1

    if match_count == 0:
        raise PossibleActionsValidationError(
            "selected action matches no possible_actions candidate"
        )
