"""`lisjong_arena.riichilab.possible_action_validation`のprotocol-facing correctness(Arena-owned、Issue #27)。

lisjong Issue #38/#39/PR #93で確立したcontractをbehavior-preservingにArenaへ
canonical physical migrationしたものである。
"""

import unittest

from lisjong_arena.riichilab.adapter_errors import PossibleActionsValidationError
from lisjong_arena.riichilab.possible_action_validation import (
    validate_against_possible_actions,
)


def _dahai_response(pai, actor=0, tsumogiri=False):
    """`build_mjai_response()`が作るdahai responseと同じ形。"""
    return {"type": "dahai", "actor": actor, "pai": pai, "tsumogiri": tsumogiri}


def _chi_response(pai, consumed, actor=1, target=0):
    return {
        "type": "chi",
        "actor": actor,
        "target": target,
        "pai": pai,
        "consumed": list(consumed),
    }


def _kakan_response(pai, consumed, actor=1):
    """RiichiEnv `Action.to_mjai()`実測どおり、kakan responseは`pai`(加える牌)と
    `consumed`(元Ponの3枚)の両方を持つ。"""
    return {"type": "kakan", "actor": actor, "pai": pai, "consumed": list(consumed)}


class ValidateAgainstPossibleActionsTest(unittest.TestCase):
    # --- 公式candidate schemaに基づく正常系(回帰防止) -------------------
    #
    # RiichiLab公式`possible_actions` candidateは、Bot-to-Server response
    # よりも小さい最小表現である(Issue #38 review、comment-5298618558)。
    # 以下のtestは、candidate schemaとBot response schemaを再び混同しない
    # ための回帰防止を目的とする。

    def test_accepts_the_official_minimal_dahai_candidate_shape(self) -> None:
        """公式形`{"type": "dahai", "pai": "1m"}`のように、actorもtsumogiriも
        持たないcandidateを合法として受理できること。"""
        validate_against_possible_actions(
            _dahai_response("3m"), [{"type": "dahai", "pai": "3m"}]
        )

    def test_tsumogiri_selection_still_matches_the_minimal_dahai_candidate(
        self,
    ) -> None:
        """送信予定responseが`tsumogiri=True`でも、tsumogiriを持たない公式形
        candidateへ一致できること。tsumogiriはcandidate identityではなく
        Bot response生成側の情報である。"""
        validate_against_possible_actions(
            _dahai_response("4m", tsumogiri=True), [{"type": "dahai", "pai": "4m"}]
        )

    def test_candidate_without_actor_field_matches_normally(self) -> None:
        """candidateへ`actor`が無くても、公式candidate schemaとして正常に
        照合できること(candidateへ一律actorを要求しない)。"""
        validate_against_possible_actions(
            {"type": "reach", "actor": 2}, [{"type": "reach"}]
        )

    def test_call_candidate_without_bot_response_target_field_matches(self) -> None:
        """chi/pon/daiminkanのcandidateへ、Bot response専用の`target`が
        無くても、公式candidateの`pai` + `consumed`だけで正常に照合
        できること。"""
        validate_against_possible_actions(
            _chi_response("3m", ["2m", "4m"]),
            [{"type": "chi", "pai": "3m", "consumed": ["2m", "4m"]}],
        )

    def test_hora_candidate_without_actor_or_target_matches(self) -> None:
        """horaのcandidateへ`actor`/`target`が無くても、公式candidateの
        `pai`(和了牌)だけで正常に照合できること。"""
        response = {"type": "hora", "actor": 0, "target": 1, "pai": "5m"}

        validate_against_possible_actions(response, [{"type": "hora", "pai": "5m"}])

    def test_accepts_unknown_extra_fields_on_a_known_candidate_type(self) -> None:
        """既知Action typeのcandidateへ将来fieldが増えても、それだけを理由に
        拒否しないこと(forward compatibility)。"""
        candidate = {
            "type": "dahai",
            "pai": "3m",
            "display_name": "discard 3m",
            "future_extra_field": {"anything": True},
        }

        validate_against_possible_actions(_dahai_response("3m"), [candidate])

    # --- kakanのconsumed(Issue #38 再レビュー blocking 1) ---------------
    #
    # 公式candidate schemaではkakan candidateが`pai`に加えて`consumed`
    # (元Ponの3枚)を持つ。`pai`だけでidentityを作ると、同じ加槓牌でも元Pon
    # 構成が異なるcandidateを誤って受理してしまう。

    def test_kakan_matches_on_added_tile_and_source_pon_composition(self) -> None:
        validate_against_possible_actions(
            _kakan_response("2p", ["2p", "2p", "2p"]),
            [{"type": "kakan", "pai": "2p", "consumed": ["2p", "2p", "2p"]}],
        )

    def test_kakan_rejects_same_pai_with_different_consumed_composition(self) -> None:
        """同じ加槓牌でも、元Pon構成(赤5の有無等)が異なるcandidateへは
        一致しないこと。"""
        response = _kakan_response("5m", ["5m", "5m", "5mr"])
        different_composition = {
            "type": "kakan",
            "pai": "5m",
            "consumed": ["5m", "5m", "5m"],
        }

        with self.assertRaises(PossibleActionsValidationError):
            validate_against_possible_actions(response, [different_composition])

    def test_kakan_consumed_is_compared_as_a_multiset(self) -> None:
        """consumedはmultisetとして扱い、list順序だけの差では拒否しないこと。"""
        response = _kakan_response("5m", ["5mr", "5m", "5m"])
        candidate = {"type": "kakan", "pai": "5m", "consumed": ["5m", "5m", "5mr"]}

        validate_against_possible_actions(response, [candidate])

    def test_kakan_requires_consumed_on_the_candidate(self) -> None:
        """`consumed`を欠くkakan candidateはmalformedとして扱うこと
        (`pai`だけでの受理へ戻していないことの回帰防止)。"""
        response = _kakan_response("2p", ["2p", "2p", "2p"])

        with self.assertRaises(PossibleActionsValidationError):
            validate_against_possible_actions(
                response, [{"type": "kakan", "pai": "2p"}]
            )

    # --- malformed / unknown candidateのfail closed(再レビュー blocking 2)

    def test_rejects_possible_actions_when_any_candidate_is_not_a_mapping(self) -> None:
        """一致するcandidateが別に存在しても、mappingでないcandidateが1件でも
        あればvalidation全体をfail closedすること。"""
        candidates = ["not-a-mapping", {"type": "none"}]

        with self.assertRaises(PossibleActionsValidationError):
            validate_against_possible_actions({"type": "none", "actor": 1}, candidates)

    def test_rejects_possible_actions_when_any_candidate_is_malformed(self) -> None:
        """known typeだがrequired fieldを欠くcandidateが混ざる場合、他に一致
        candidateがあってもvalidation全体をfail closedすること。"""
        candidates = [{"type": "dahai"}, {"type": "none"}]

        with self.assertRaises(PossibleActionsValidationError):
            validate_against_possible_actions({"type": "none", "actor": 1}, candidates)

    def test_rejects_unknown_possible_action_type(self) -> None:
        """未知Action typeのcandidateはsilent ignoreせずfail closedすること。
        forward compatibilityとして許容するのは既知typeのunknown追加field
        までである。"""
        candidates = [{"type": "future_action_type", "extra": True}, {"type": "none"}]

        with self.assertRaises(PossibleActionsValidationError):
            validate_against_possible_actions({"type": "none", "actor": 0}, candidates)

    def test_rejects_candidate_with_unparsable_tile(self) -> None:
        candidates = [{"type": "dahai", "pai": "99z"}]

        with self.assertRaises(PossibleActionsValidationError):
            validate_against_possible_actions(_dahai_response("3m"), candidates)

    def test_rejects_candidate_with_wrong_consumed_length(self) -> None:
        candidates = [{"type": "chi", "pai": "3m", "consumed": ["2m"]}]

        with self.assertRaises(PossibleActionsValidationError):
            validate_against_possible_actions(
                _chi_response("3m", ["2m", "4m"]), candidates
            )

    def test_rejects_when_the_send_ready_response_cannot_be_projected(self) -> None:
        """送信予定response側をcandidate identityへprojectionできない場合も、
        payloadを通さずfail closedすること。"""
        with self.assertRaises(PossibleActionsValidationError):
            validate_against_possible_actions(
                {"type": "dahai", "actor": 0}, [{"type": "dahai", "pai": "3m"}]
            )

    # --- candidateが任意で持つsemantic fieldとの整合 ---------------------

    def test_candidate_actor_that_contradicts_the_response_does_not_match(self) -> None:
        """公式Protocolは例とfield表に記述差があり、candidateが`actor`を持ち得
        ないとは断言できない。存在する場合は矛盾を無視せず、非一致として
        扱う(結果として一致0件ならfail closed)。"""
        candidates = [{"type": "dahai", "pai": "3m", "actor": 2}]

        with self.assertRaises(PossibleActionsValidationError):
            validate_against_possible_actions(
                _dahai_response("3m", actor=0), candidates
            )

    def test_candidate_target_that_agrees_with_the_response_matches(self) -> None:
        candidates = [
            {"type": "chi", "pai": "3m", "consumed": ["2m", "4m"], "target": 0}
        ]

        validate_against_possible_actions(
            _chi_response("3m", ["2m", "4m"], actor=1, target=0), candidates
        )

    def test_candidate_target_that_contradicts_the_response_does_not_match(
        self,
    ) -> None:
        candidates = [
            {"type": "chi", "pai": "3m", "consumed": ["2m", "4m"], "target": 3}
        ]

        with self.assertRaises(PossibleActionsValidationError):
            validate_against_possible_actions(
                _chi_response("3m", ["2m", "4m"], actor=1, target=0), candidates
            )

    # --- 従来からのsemantic matching / fail closed契約 -------------------

    def test_accepts_a_single_exact_dahai_match(self) -> None:
        candidates = [
            {"type": "dahai", "pai": "3m"},
            {"type": "dahai", "pai": "4m"},
        ]

        validate_against_possible_actions(_dahai_response("3m"), candidates)

    def test_is_independent_of_candidate_order(self) -> None:
        candidates = [
            {"type": "dahai", "pai": "3m"},
            {"type": "reach"},
            {"type": "dahai", "pai": "4m"},
        ]

        validate_against_possible_actions(
            _dahai_response("4m", tsumogiri=True), candidates
        )

    def test_ignores_semantically_irrelevant_extra_fields_on_candidates(self) -> None:
        candidates = [
            {
                "type": "reach",
                "actor": 2,
                # 意味を持たない付加field。誤って拒否理由にしない。
                "display_name": "declare riichi",
                "score_delta": -1000,
            }
        ]

        validate_against_possible_actions({"type": "reach", "actor": 2}, candidates)

    def test_distinguishes_red_five_from_normal_five(self) -> None:
        candidates = [{"type": "dahai", "pai": "5m"}]

        with self.assertRaises(PossibleActionsValidationError):
            validate_against_possible_actions(_dahai_response("5mr"), candidates)

    def test_chi_requires_matching_consumed_composition(self) -> None:
        response = _chi_response("3m", ["2m", "4m"])
        wrong_composition = {"type": "chi", "pai": "3m", "consumed": ["4m", "5m"]}
        matching = {
            "type": "chi",
            "pai": "3m",
            "consumed": ["4m", "2m"],  # 順序が違っても一致すること
        }

        with self.assertRaises(PossibleActionsValidationError):
            validate_against_possible_actions(response, [wrong_composition])

        validate_against_possible_actions(response, [wrong_composition, matching])

    def test_pon_candidate_without_target_still_matches_correctly(self) -> None:
        """公式candidateに`target`が無いため、candidate側は`pai` +
        `consumed`だけで識別する。`target`はBot response側でのみ使う情報
        であり、candidate validationへ混入させない。"""
        response = {
            "type": "pon",
            "actor": 2,
            "target": 1,
            "pai": "2p",
            "consumed": ["2p", "2p"],
        }
        candidate = {"type": "pon", "pai": "2p", "consumed": ["2p", "2p"]}

        validate_against_possible_actions(response, [candidate])

    def test_daiminkan_matches_on_full_semantic_key(self) -> None:
        response = {
            "type": "daiminkan",
            "actor": 3,
            "target": 2,
            "pai": "2p",
            "consumed": ["2p", "2p", "2p"],
        }
        candidate = {
            "type": "daiminkan",
            "pai": "2p",
            "consumed": ["2p", "2p", "2p"],
        }

        validate_against_possible_actions(response, [candidate])

    def test_ankan_matches_on_tile_multiset(self) -> None:
        response = {
            "type": "ankan",
            "actor": 0,
            "pai": "2p",
            "consumed": ["2p", "2p", "2p", "2p"],
        }
        candidate = {"type": "ankan", "consumed": ["2p", "2p", "2p", "2p"]}

        validate_against_possible_actions(response, [candidate])

    def test_ron_matches_on_winning_tile_regardless_of_target_field(self) -> None:
        response = {"type": "hora", "actor": 0, "target": 1, "pai": "5m"}
        wrong_pai = {"type": "hora", "pai": "4m"}
        matching = {"type": "hora", "pai": "5m"}

        with self.assertRaises(PossibleActionsValidationError):
            validate_against_possible_actions(response, [wrong_pai])

        validate_against_possible_actions(response, [wrong_pai, matching])

    def test_tsumo_matches_the_same_hora_candidate_shape_as_ron(self) -> None:
        response = {"type": "hora", "actor": 2, "target": 2, "pai": "5m"}

        validate_against_possible_actions(response, [{"type": "hora", "pai": "5m"}])

    # --- horaのminimal candidate(Issue #38 第3回レビュー) -----------------
    #
    # RiichiLab公式Protocolの`request_action`例には、`possible_actions`の
    # `hora` candidateとして`{"type": "hora"}`というminimal形が掲載されている
    # 一方、同ページのAction別field表には`pai`等の追加fieldが記載されており、
    # 公式文書内に記述差がある。`pai`をcandidate必須identityにすると、この
    # 公式例そのものをmalformedとして拒否してしまうため、`hora`の必須
    # identityは`type`のみとし、`pai`は存在する場合だけ整合確認する。

    def test_accepts_the_official_minimal_hora_candidate_shape_for_ron(self) -> None:
        """公式`request_action`例の`{"type": "hora"}`は、ron responseへも
        `pai`を要求せず一致できること。"""
        response = {"type": "hora", "actor": 0, "target": 1, "pai": "5m"}

        validate_against_possible_actions(response, [{"type": "hora"}])

    def test_accepts_the_official_minimal_hora_candidate_shape_for_tsumo(self) -> None:
        """公式`request_action`例の`{"type": "hora"}`は、tsumo responseへも
        `pai`を要求せず一致できること。"""
        response = {"type": "hora", "actor": 2, "target": 2, "pai": "5m"}

        validate_against_possible_actions(response, [{"type": "hora"}])

    def test_hora_candidate_with_matching_optional_pai_matches(self) -> None:
        response = {"type": "hora", "actor": 0, "target": 1, "pai": "5m"}

        validate_against_possible_actions(response, [{"type": "hora", "pai": "5m"}])

    def test_hora_candidate_with_mismatched_optional_pai_does_not_match(self) -> None:
        response = {"type": "hora", "actor": 0, "target": 1, "pai": "5m"}

        with self.assertRaises(PossibleActionsValidationError):
            validate_against_possible_actions(response, [{"type": "hora", "pai": "4m"}])

    def test_hora_candidate_with_malformed_optional_pai_fails_closed(self) -> None:
        """candidateが`pai`を持つのに不正な牌表記の場合、非一致として
        skipするのではなく、malformed candidateとしてvalidation全体を
        fail closedすること。"""
        response = {"type": "hora", "actor": 0, "target": 1, "pai": "5m"}

        with self.assertRaises(PossibleActionsValidationError):
            validate_against_possible_actions(
                response, [{"type": "hora", "pai": "99z"}]
            )

    def test_pass_matches_none_type(self) -> None:
        # `to_mjai()`はnone/ryukyokuでも意味を持たない`pai`を出力するが、
        # candidate identityには使わないため照合へ影響しない。
        response = {"type": "none", "actor": 1, "pai": "1m"}

        validate_against_possible_actions(response, [{"type": "none"}])

    def test_kyuushu_kyuuhai_matches_ryukyoku_type(self) -> None:
        response = {"type": "ryukyoku", "actor": 3, "pai": "1m"}

        validate_against_possible_actions(response, [{"type": "ryukyoku"}])

    def test_rejects_zero_matches(self) -> None:
        with self.assertRaises(PossibleActionsValidationError):
            validate_against_possible_actions({"type": "none", "actor": 0}, [])

    def test_accepts_duplicate_matches_observed_in_issue_39_live_validation(
        self,
    ) -> None:
        # Issue #39の実 `/ws/validate` で、同一semantic Actionに対応する
        # duplicate possible_actions candidateが実際に2件提示された。
        candidates = [
            {"type": "dahai", "pai": "1m"},
            {"type": "dahai", "pai": "1m"},
        ]

        validate_against_possible_actions(_dahai_response("1m"), candidates)

    def test_accepts_duplicate_matches_with_unrelated_valid_candidate(self) -> None:
        candidates = [
            {"type": "dahai", "pai": "1m"},
            {"type": "dahai", "pai": "1m"},
            {"type": "dahai", "pai": "2m"},
        ]

        validate_against_possible_actions(_dahai_response("1m"), candidates)

    def test_duplicate_matches_do_not_hide_malformed_candidate(self) -> None:
        candidates = [
            {"type": "dahai", "pai": "1m"},
            {"type": "dahai", "pai": "1m"},
            {"type": "dahai"},
        ]

        with self.assertRaises(PossibleActionsValidationError):
            validate_against_possible_actions(_dahai_response("1m"), candidates)

    def test_duplicate_matches_do_not_hide_unknown_action_type(self) -> None:
        candidates = [
            {"type": "dahai", "pai": "1m"},
            {"type": "dahai", "pai": "1m"},
            {"type": "future_action"},
        ]

        with self.assertRaises(PossibleActionsValidationError):
            validate_against_possible_actions(_dahai_response("1m"), candidates)

    def test_does_not_fall_back_to_first_or_last_or_arbitrary_candidate(self) -> None:
        # 送信予定Actionにまったく対応しない候補群であっても、既知typeが
        # 存在するというだけで代替受理しない。
        candidates = [
            {"type": "dahai", "pai": "4m"},
            {"type": "reach"},
        ]

        with self.assertRaises(PossibleActionsValidationError):
            validate_against_possible_actions(_dahai_response("3m"), candidates)

    def test_dahai_without_pai_field_is_treated_as_malformed(self) -> None:
        with self.assertRaises(PossibleActionsValidationError):
            validate_against_possible_actions(
                _dahai_response("3m"), [{"type": "dahai"}]
            )


if __name__ == "__main__":
    unittest.main()
