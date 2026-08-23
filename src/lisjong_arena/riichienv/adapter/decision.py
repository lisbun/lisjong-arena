"""RiichiEnvの1 decisionを環境非依存なPolicy契約へ接続する境界。

lisbun/lisjong#28のseat-visible materialized state / ``PolicyInput``生成と、
lisbun/lisjong#29のdecision-local Action mappingを、同じseat・同じ
``Observation``について実行し、既存の``DecisionContext``と対応mappingを
不可分な組として返す。

このmoduleはPolicyを呼び出さず、対局loopやRiichiEnv本体も所有しない。また、
``RiichiEnvActionMappingSession``とは別のdecision IDやgenerationを導入しない。
"""

from dataclasses import dataclass

from lisjong.policy_contract.decision_context import DecisionContext
from riichienv import Observation

from lisjong_arena.riichienv.adapter.action_mapping import (
    RiichiEnvActionMapping,
    RiichiEnvActionMappingSession,
)
from lisjong_arena.riichienv.adapter.errors import AdapterSyncError
from lisjong_arena.riichienv.adapter.materialized_state import SeatMaterializedState
from lisjong_arena.riichienv.adapter.policy_input import build_policy_input


@dataclass(frozen=True, slots=True)
class RiichiEnvDecision:
    """同じRiichiEnv decisionから構築したPolicy contextとAction mapping。"""

    context: DecisionContext
    mapping: RiichiEnvActionMapping

    def __post_init__(self) -> None:
        if not isinstance(self.context, DecisionContext):
            raise TypeError("context must be a DecisionContext")
        if not isinstance(self.mapping, RiichiEnvActionMapping):
            raise TypeError("mapping must be a RiichiEnvActionMapping")

        if self.context.input.self_seat != self.mapping.self_seat:
            raise AdapterSyncError(
                "DecisionContext and Action mapping must belong to the same seat"
            )
        if self.context.legal_actions != self.mapping.candidates:
            raise AdapterSyncError(
                "DecisionContext legal_actions must come from the paired Action mapping"
            )


def build_decision(
    tracker: SeatMaterializedState,
    observation: Observation,
    mapping_session: RiichiEnvActionMappingSession,
) -> RiichiEnvDecision:
    """1つのObservationから検証済みPolicy contextと対応mappingを構築する。

    tracker、Observation、mapping sessionのseatを更新前に照合する。その後、同じ
    Observationからmappingを先に生成してsession generationを進め、続いて
    ``PolicyInput``を構築する。後段の同期または``DecisionContext``構築が失敗しても、
    以前のmappingを有効なまま残さないためである。

    ``DecisionContext``自身が持つ非空・actor一致・semantic重複禁止のvalidationは
    constructorへ委ね、この境界では重複実装しない。
    """
    if not isinstance(tracker, SeatMaterializedState):
        raise TypeError("tracker must be a SeatMaterializedState")
    if not isinstance(mapping_session, RiichiEnvActionMappingSession):
        raise TypeError("mapping_session must be a RiichiEnvActionMappingSession")

    if tracker.self_seat != mapping_session.self_seat:
        raise AdapterSyncError(
            "materialized state and Action mapping session must belong to the same seat"
        )
    if observation.player_id != int(tracker.self_seat):
        raise AdapterSyncError(
            "observation.player_id does not match the decision builder's seat"
        )

    mapping = mapping_session.build(observation)
    policy_input = build_policy_input(tracker, observation)
    context = DecisionContext(
        input=policy_input,
        legal_actions=mapping.candidates,
    )
    return RiichiEnvDecision(context=context, mapping=mapping)
