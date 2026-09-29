from __future__ import annotations

from novel_system.services.qc_constraints import (
    REFERENCE_POLICY_SENTENCES,
    constraint_alternatives,
    constraint_terms,
    contains_forbidden_term,
    forbidden_hits,
    forbidden_terms,
    issue_mentions_source,
    required_groups_missing,
    source_field_satisfied,
    strip_reference_policy,
)


def test_constraint_terms_normalize_supported_delimiters_and_ignore_noise() -> None:
    assert constraint_terms("盐钟， 潮声; x\n旧名单") == ["盐钟", "潮声", "旧名单"]


def test_constraint_alternatives_supports_explicit_equivalent_spellings() -> None:
    assert constraint_alternatives("钢琴键|琴键｜白键") == ["钢琴键", "琴键", "白键"]
    assert source_field_satisfied("钢琴键|琴键", "他一直留着那枚琴键。") is True
    assert source_field_satisfied("钢琴键|琴键", "收音机仍在响。") is False


def test_forbidden_and_required_checks_share_the_same_leaf_contract() -> None:
    assert contains_forbidden_term("盐钟、潮声", "他听见潮声") is True
    assert contains_forbidden_term(None, "潮声") is False
    assert source_field_satisfied("必须找回旧名单", "他终于找回旧名单，却没有打开") is True
    assert source_field_satisfied("必须找回旧名单", "他离开了码头") is False
    assert issue_mentions_source("缺少：找回旧名单", "必须找回旧名单") is True


def test_the_reference_policy_sentence_is_not_a_list_of_forbidden_words() -> None:
    """2026-09-20 真实故障：物化把防抄袭政策句写进了每张场景卡的 forbidden_text——按顿号一拆，

    「人物」成了按字面查的禁用词，正文里一出现（这号人物、可疑人物……）就是 Q1 硬伤。
    """
    policy = REFERENCE_POLICY_SENTENCES[0]
    # 这正是过去的读法：政策句被当成禁用词表
    assert "人物" in constraint_terms(policy)

    assert forbidden_terms(policy) == []
    assert contains_forbidden_term(policy, "这号人物他见得多了，设定或桥段。都不新鲜。") is False
    assert strip_reference_policy(policy) == ""
    assert strip_reference_policy(None) == ""
    # 作者真写的禁用词照常生效——单独写、或者接在旧卡那句话后面
    assert forbidden_terms("死亡证明、铜钥匙|黄铜钥匙") == ["死亡证明", "铜钥匙|黄铜钥匙"]
    assert forbidden_terms(policy + "死亡证明") == ["死亡证明"]
    assert contains_forbidden_term(policy + "死亡证明", "他递过来一张死亡证明。") is True
    assert contains_forbidden_term(policy + "死亡证明", "这号人物他见得多了。") is False
    for sentence in REFERENCE_POLICY_SENTENCES:
        assert forbidden_terms(sentence) == []


def test_forbidden_hits_report_the_spelling_that_occurs() -> None:
    assert forbidden_hits("黑伞|雨伞、钥匙", "她撑开雨伞，钥匙落在地上，又捡起钥匙。") == ["雨伞", "钥匙"]
    assert forbidden_hits("黑伞|雨伞", "晴天。") == []
    assert forbidden_hits(REFERENCE_POLICY_SENTENCES[0] + "青花瓷", "这号人物端着青花瓷。") == ["青花瓷"]
    assert forbidden_hits(None, "雨伞") == []


def test_required_text_is_checked_group_by_group() -> None:
    """批准#11（B04-04）：必写内容按组查。整段口径只要沾上两个三字片段就算满足，一整组没写也放过。"""
    required = "主角交出钥匙，门外传来警笛"
    content = "他犹豫很久，最后主角交出钥匙。夜很静。"
    assert source_field_satisfied(required, content) is True  # 旧口径：整段算满足
    assert required_groups_missing(required, content) == ["门外传来警笛"]
    assert required_groups_missing(required, content + "门外传来警笛声。") == []
    # 组内 A|B 任一即可；容忍改写（同 source_field_satisfied）
    assert required_groups_missing("交出钥匙|递出钥匙、警笛", "她递出钥匙，远处响起警笛。") == []
    assert required_groups_missing("", "任何正文") == []
    assert required_groups_missing(None, "任何正文") == []
    # 一整段分不出 ≥2 字的组：按整段查
    assert required_groups_missing("伞", "她撑开伞。") == []
    assert required_groups_missing("伞", "她走进雨里。") == ["伞"]
