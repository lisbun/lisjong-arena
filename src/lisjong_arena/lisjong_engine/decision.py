"""engineの1 decisionを、lisjong-owned `DecisionContext`と対応mappingへ束ねる境界。

同じ`SeatObservation`と同じdescriptor列から`PolicyInput`とdecision-local
mappingを構築し、不可分な組として返す。このmoduleはPolicyを呼び出さず、
game state / rule state / match lifecycleも所有しない。

`DecisionContext`のsemantic contract（legal_actions非空、actor一致、semantic
重複禁止）はlisjong側constructorが所有するため重複実装しない。Arena境界では
observation viewer seat / mapping actor / legal action actorが同じseatを表す
ことだけを追加で維持する。
"""

from dataclasses import dataclass

from lisjong.policy_contract import DecisionContext
from lisjong_engine.observation import SeatObservation

from lisjong_arena.lisjong_engine.action_mapping import (
    EngineActionMapping,
    build_action_mapping,
)
from lisjong_arena.lisjong_engine.errors import SeatIdentityError
from lisjong_arena.lisjong_engine.policy_input import build_policy_input


@dataclass(frozen=True, slots=True, eq=False)
class EngineDecision:
    """同じengine decisionから構築したPolicy contextとdescriptor mapping。"""

    context: DecisionContext
    mapping: EngineActionMapping

    def __post_init__(self) -> None:
        if not isinstance(self.context, DecisionContext):
            raise TypeError("context must be a DecisionContext")
        if not isinstance(self.mapping, EngineActionMapping):
            raise TypeError("mapping must be an EngineActionMapping")
        if self.context.input.self_seat != self.mapping.self_seat:
            raise SeatIdentityError(
                "DecisionContext and action mapping must belong to the same seat"
            )
        if self.context.legal_actions != self.mapping.candidates:
            raise SeatIdentityError(
                "DecisionContext legal_actions must come from the paired mapping"
            )


def build_decision(observation: SeatObservation, options: object) -> EngineDecision:
    """1つのengine decisionから、検証済みcontextと対応mappingを構築する。"""
    if not isinstance(observation, SeatObservation):
        raise TypeError("observation must be a lisjong-engine SeatObservation")

    mapping = build_action_mapping(observation, options)
    policy_input = build_policy_input(observation)
    context = DecisionContext(
        input=policy_input,
        legal_actions=mapping.candidates,
    )
    return EngineDecision(context=context, mapping=mapping)
