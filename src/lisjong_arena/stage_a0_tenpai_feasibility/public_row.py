"""current flat-BC public decision rowと、そのrow identity / digest。

Stage A0 feasibilityはpublic featureのcontractを変更しない。rowは既存の
canonical builderの出力をそのまま保持する。

```text
PolicyInput   -> build_policy_input_feature() -> tensor_values()   8204 float32
DecisionContext -> build_legal_action_mask()                        802 bool
validated InternalAction -> encode_action() / resolve_legal_action()
```

hidden opponent hand、wall truth、future state、future outcome、oracle
information、teacher-internal analysisはrowへ入れない。privileged annotationは
rowの外側にだけ存在し、`row_digest()`はannotationの有無に影響されない。
"""

import hashlib
from array import array
from dataclasses import dataclass

from lisjong.action_vocabulary import (
    build_legal_action_mask,
    encode_action,
    resolve_legal_action,
)
from lisjong.policy_contract import DecisionContext

from lisjong_arena.learned_policy_input import (
    build_policy_input_feature,
    tensor_values,
)

from .errors import StageA0AlignmentError, StageA0ProtocolError
from .protocol import FEATURE_DIMENSION, VOCABULARY_SIZE, Split, action_family_of

_FEATURE_ROW_BYTES = FEATURE_DIMENSION * 4


@dataclass(frozen=True, slots=True)
class DecisionRowIdentity:
    """1 decision rowのlogical identity。

    `source_identity`はretained dataset identity、またはfresh smoke runの
    population identityである。seedだけではsource間のrow取り違えを検出でき
    ないため、必ずsource identityと組で保持する。
    """

    source_identity: str
    seed: int
    step_ordinal: int
    decision_ordinal: int
    actor_seat: int

    def __post_init__(self) -> None:
        if type(self.source_identity) is not str or not self.source_identity:
            raise TypeError("source_identity must be a non-empty str")
        for name in ("seed", "step_ordinal", "decision_ordinal", "actor_seat"):
            if type(getattr(self, name)) is not int:
                raise TypeError(f"{name} must be an int")
        for name in ("step_ordinal", "decision_ordinal"):
            if getattr(self, name) < 0:
                raise ValueError(f"{name} must not be negative")
        if not 0 <= self.actor_seat <= 3:
            raise ValueError("actor_seat must be in 0..3")

    @property
    def decision_key(self) -> tuple[int, int, int, int]:
        """同一source内でdecisionを一意に決めるkey。"""
        return (self.seed, self.step_ordinal, self.decision_ordinal, self.actor_seat)

    def to_document(self) -> dict[str, object]:
        return {
            "source_identity": self.source_identity,
            "seed": self.seed,
            "step_ordinal": self.step_ordinal,
            "decision_ordinal": self.decision_ordinal,
            "actor_seat": self.actor_seat,
        }


@dataclass(frozen=True, slots=True)
class PublicDecisionRow:
    """player-safeなflat-BC row。privileged truthを一切持たない。"""

    identity: DecisionRowIdentity
    split: Split | None
    round_wind: str
    hand_number: int
    honba: int
    feature_values: tuple[float, ...]
    legal_mask: tuple[bool, ...]
    teacher_action_index: int
    teacher_action_family: str

    def __post_init__(self) -> None:
        if not isinstance(self.identity, DecisionRowIdentity):
            raise TypeError("identity must be a DecisionRowIdentity")
        if self.split is not None and not isinstance(self.split, Split):
            raise TypeError("split must be a Split or None")
        if len(self.feature_values) != FEATURE_DIMENSION:
            raise StageA0ProtocolError(
                f"feature dimension must be {FEATURE_DIMENSION}; "
                f"got {len(self.feature_values)}"
            )
        if len(self.legal_mask) != VOCABULARY_SIZE:
            raise StageA0ProtocolError(
                f"legal mask dimension must be {VOCABULARY_SIZE}; "
                f"got {len(self.legal_mask)}"
            )
        if any(type(value) is not bool for value in self.legal_mask):
            raise TypeError("legal_mask must contain only exact bools")
        if not 0 <= self.teacher_action_index < VOCABULARY_SIZE:
            raise StageA0ProtocolError("teacher action index is outside the vocabulary")
        if not self.legal_mask[self.teacher_action_index]:
            raise StageA0ProtocolError(
                "teacher action index is not legal in this decision"
            )
        if self.teacher_action_family != action_family_of(self.teacher_action_index):
            raise StageA0ProtocolError(
                "teacher action family does not match the vocabulary block"
            )

    @property
    def legal_action_count(self) -> int:
        return sum(self.legal_mask)

    def feature_bytes(self) -> bytes:
        payload = array("f", self.feature_values).tobytes()
        if len(payload) != _FEATURE_ROW_BYTES:
            raise StageA0ProtocolError("feature row byte width drifted")
        return payload

    def legal_mask_bytes(self) -> bytes:
        return bytes(self.legal_mask)

    def row_digest(self) -> str:
        """public row bytesのdigest。

        privileged annotationはこのdigestの入力に含まれない。annotation
        attachmentの前後でdigestが変わらないことがleakage boundaryの
        機械的な証拠になる。
        """
        digest = hashlib.sha256()
        for part in (
            self.identity.source_identity.encode("utf-8"),
            repr(self.identity.decision_key).encode("utf-8"),
            self.feature_bytes(),
            self.legal_mask_bytes(),
            str(self.teacher_action_index).encode("utf-8"),
        ):
            digest.update(len(part).to_bytes(8, "big"))
            digest.update(part)
        return digest.hexdigest()


def build_public_decision_row(
    identity: DecisionRowIdentity,
    context: DecisionContext,
    selected_action: object,
    *,
    split: Split | None = None,
) -> PublicDecisionRow:
    """validated teacher decisionをcurrent flat-BC contractのrowへencodeする。

    feature / legal mask / action encodingはすべて既存のcanonical builderを
    そのまま呼ぶ。Stage A0側でfeature semanticsを再定義・再計算しない。
    """
    if not isinstance(context, DecisionContext):
        raise TypeError("context must be a DecisionContext")
    policy_input = context.input
    if int(policy_input.self_seat) != identity.actor_seat:
        raise StageA0AlignmentError(
            "the decision context seat does not match the row identity actor seat"
        )

    teacher_index = encode_action(selected_action)
    if resolve_legal_action(teacher_index, context) is not selected_action:
        raise StageA0AlignmentError(
            "encode / resolve round trip did not return the canonical teacher action"
        )

    round_state = policy_input.round
    return PublicDecisionRow(
        identity=identity,
        split=split,
        round_wind=round_state.round_wind.value,
        hand_number=round_state.hand_number,
        honba=round_state.honba,
        feature_values=tensor_values(build_policy_input_feature(policy_input)),
        legal_mask=build_legal_action_mask(context),
        teacher_action_index=teacher_index,
        teacher_action_family=action_family_of(teacher_index),
    )


__all__ = [
    "DecisionRowIdentity",
    "PublicDecisionRow",
    "build_public_decision_row",
]
