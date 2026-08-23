"""paired mappingでresolveしたRiichiEnv Actionを、RiichiLab Bot-to-Server
response相当のMJAI dictへ変換する(Arena-local canonical、Issue #27)。

RiichiEnv 0.4.8の`Action.to_mjai()`を変換の基底として使用し、全Action
variantのMJAI serializationを独自に再実装しない。ただし実測の結果、
`to_mjai()`には次の欠落があることを確認した(lisjong Issue #38実測、
`docs/riichilab-protocol-bridge.md`「実測事実」を参照)。

- hora(ron/tsumo)で`pai`(和了牌)を含まない
- chi/pon/daiminkan/ronで`target`(呼ばれた/放銃した相手seat)を含まない

この2点だけを、resolve済みでcontext整合が保証されているcanonical
`InternalAction`の対応fieldから補う、必要最小限の正規化を行う。

lisjong Issue #38で確立したcontractをbehavior-preservingにArenaへphysical
migrationしたものである。
"""

import json

from lisjong.policy_contract import (
    ChiAction,
    DaiminkanAction,
    DiscardAction,
    InternalAction,
    PonAction,
    RonAction,
    TsumoAction,
)
from lisjong.riichienv_adapter import tile_to_mjai
from riichienv import Action as RiichiEnvAction

from lisjong_arena.riichilab.adapter_errors import ProtocolConversionError


def build_mjai_response(
    resolved_action: RiichiEnvAction, selected: InternalAction
) -> dict:
    """resolve済みRiichiEnv ActionとそのcanonicalなInternalActionからMJAI dictを作る。

    `selected`は`resolved_action`をpaired mappingでresolveした際に使用した、
    同じdecisionのcanonical Actionでなければならない。呼び出し側
    (`RiichiLabSeatAdapter`)がこの対応を保証する。
    """
    try:
        raw_response = resolved_action.to_mjai()
        response = json.loads(raw_response)
    except Exception as error:
        raise ProtocolConversionError(
            "resolved RiichiEnv Action could not be converted via to_mjai()"
        ) from error

    if not isinstance(response, dict):
        raise ProtocolConversionError("to_mjai() output must be a JSON object")

    # actorはmappingが返す元Actionのactorとcanonical selected.actorが常に
    # 一致する(paired mappingのresolve()契約)が、明示性のためselected側から
    # 上書きする。
    response["actor"] = int(selected.actor)

    if isinstance(selected, DiscardAction):
        response["tsumogiri"] = selected.tsumogiri
    elif isinstance(selected, (ChiAction, PonAction, DaiminkanAction)):
        response["target"] = int(selected.target)
    elif isinstance(selected, RonAction):
        response["target"] = int(selected.target)
        response["pai"] = tile_to_mjai(selected.winning_tile)
    elif isinstance(selected, TsumoAction):
        response["target"] = int(selected.actor)
        response["pai"] = tile_to_mjai(selected.winning_tile)

    return response
