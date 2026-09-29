from novel_system.services import snowflake_steps as steps_mod


# 设计规定的雪花十步顺序 —— 单一事实源的「期望真值」,独立于实现派生。
# 钉成字面量:任何对 SNOWFLAKE_STEP_CATALOG 的重排 / 漏步 / 多步都会让下方断言变红。
# (原断言 [keys for SNOWFLAKE_STEPS] == [keys for catalog] 是同源自比,顺序写错也恒真。)
EXPECTED_STEP_ORDER = [
    "book_brief",
    "one_sentence_summary",
    "one_paragraph_summary",
    "character_sheets",
    "short_synopsis",
    "character_synopses",
    "long_synopsis",
    "character_bibles",
    "scene_list",
    "scene_details",
]
# 物化硬门步骤(设计的第 1/2/3/9/10 步);其余为可跳过的 warning 步骤。
EXPECTED_REQUIRED_STEPS = {
    "book_brief",
    "one_sentence_summary",
    "one_paragraph_summary",
    "scene_list",
    "scene_details",
}


def test_step_order_is_pinned_to_the_designed_literal() -> None:
    catalog = steps_mod.SNOWFLAKE_STEP_CATALOG
    # (a) 顺序正确性:对独立字面量钉死,而非 catalog 自比自(后者顺序写错也恒真)。
    assert [s["step_key"] for s in catalog] == EXPECTED_STEP_ORDER
    # (b) STEP_ORDER 是目录顺序的正确投影 —— 对字面量校验。
    assert steps_mod.STEP_ORDER == {key: idx for idx, key in enumerate(EXPECTED_STEP_ORDER)}
    # (c) 读接口返回的是同一份目录(工作台、概览、待办都从这里取步骤)。
    assert [s["step_key"] for s in steps_mod.list_step_definitions()] == EXPECTED_STEP_ORDER


def test_skippable_matches_the_materialization_hard_gate() -> None:
    catalog = steps_mod.SNOWFLAKE_STEP_CATALOG
    # 硬门集合本身钉成字面量真值,而非从 MATERIALIZATION_REQUIRED_STEPS 反推
    #(反推时漏标硬门 / 多标 warning 步骤会与派生公式同步漂移,断言永不变红)。
    assert set(steps_mod.MATERIALIZATION_REQUIRED_STEPS) == EXPECTED_REQUIRED_STEPS
    # 且「可跳过」==「非物化必需」—— 用独立真值判定,任何 skippable 误算都会变红。
    for s in catalog:
        assert bool(s.get("skippable")) == (s["step_key"] not in EXPECTED_REQUIRED_STEPS)
    # v1 规划器那份平行的步骤投影(planner_step_list)随 v1 规划器一起退役了
    assert not hasattr(steps_mod, "planner_step_list")
