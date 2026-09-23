"""voice_signature(声音签名)单测。

- 黄金语料两位公版作者的签名在多个特征上可区分(含虚词组);
- 习惯句(2026-09-23 起)只描述作者自己:高频词与大致频率,≤12 行、不含阿拉伯数字、不与任何基线比「偏多 / 偏少」;
- 空文本 / 纯标点 / 极短文本安全;
- 叠词密集文本 deliberate_repetition 为 True(基线字面 p85);
- 基线 yaml 与生成器一致(测量核口径变了必须重跑 build-baseline)。
"""

from __future__ import annotations

import math
import re
import time
from pathlib import Path

import pytest
import yaml

from novel_system.services.style_reference import voice_signature as vs
from novel_system.services.style_reference.config_loader import load_yaml_config
from novel_system.services.style_reference.style_signature import query_view_for_granularity
from novel_system.services.style_reference.text_utils import normalize_text, split_paragraphs

CORPUS = Path(__file__).resolve().parent / "golden" / "style_reference" / "corpus"
BASELINE_PATH = Path(__file__).resolve().parents[2] / "config" / "style_reference" / "voice_baseline.yaml"
_DIGITS = re.compile(r"[0-9]")


def _paragraphs(name: str) -> list[str]:
    text = normalize_text((CORPUS / name).read_text(encoding="utf-8"))
    return [body for _start, _end, body in split_paragraphs(text)]


@pytest.fixture(scope="module")
def baseline() -> dict:
    loaded = vs.load_voice_baseline()
    assert loaded, "voice_baseline.yaml 必须存在且含 features"
    return loaded


@pytest.fixture(scope="module")
def luxun(baseline: dict) -> dict:
    return vs.compute_voice_signature(_paragraphs("luxun_short_stories.txt"), baseline=baseline)


@pytest.fixture(scope="module")
def zhuziqing(baseline: dict) -> dict:
    return vs.compute_voice_signature(_paragraphs("zhuziqing_benchmark_essays.txt"), baseline=baseline)


# ---------------------------------------------------------------------------
# 词表与基线文件
# ---------------------------------------------------------------------------


def test_function_words_yaml_groups_are_complete_and_disjoint() -> None:
    raw = load_yaml_config("function_words")
    groups = raw["groups"]
    assert set(groups) == set(vs.FUNCTION_WORD_GROUPS)
    seen: dict[str, str] = {}
    for group, entry in groups.items():
        assert entry["label"], group
        words = entry["words"]
        assert words, group
        for word in words:
            assert word not in seen, f"{word!r} 同时出现在 {seen.get(word)} 与 {group}"
            seen[word] = group
    lexicon = vs.load_voice_lexicon()
    pronoun_words = set(lexicon.group_words["pronoun"])
    for key in ("first", "second", "third"):
        assert lexicon.person[key], key
        assert set(lexicon.person[key]) <= pronoun_words
    assert lexicon.sentence_final_modal and lexicon.sentence_final_classical
    # 嵌套词的独占计数:「但是」命中后不再记入「但」
    assert "但是" in lexicon.containers["但"]


def test_baseline_yaml_matches_generator(baseline: dict) -> None:
    """基线文件必须能由 build-baseline 从黄金语料确定性再生成(分句 / 词表改动后需重跑)。"""
    assert baseline["version"] == vs.VOICE_BASELINE_VERSION
    assert baseline["signature_version"] == vs.VOICE_SIGNATURE_VERSION
    assert baseline["block_chars"] == vs.BASELINE_BLOCK_CHARS
    assert set(baseline["features"]) == set(vs.FEATURE_NAMES)
    regenerated = vs.build_voice_baseline(CORPUS)
    assert regenerated["block_count"] == baseline["block_count"] > 20
    for name in vs.FEATURE_NAMES:
        expected = regenerated["features"][name]
        actual = baseline["features"][name]
        for key in ("mean", "std", "p15", "p50", "p85"):
            assert actual[key] == pytest.approx(expected[key], rel=1e-6, abs=1e-6), (name, key)
        assert actual["p15"] <= actual["p50"] <= actual["p85"]
        assert actual["std"] >= 0
    assert regenerated["top_words"] == baseline["top_words"]


def test_build_baseline_cli_writes_loadable_yaml(tmp_path: Path) -> None:
    output = tmp_path / "voice_baseline.yaml"
    assert vs._main(["build-baseline", "--corpus-dir", str(CORPUS), "--output", str(output)]) == 0
    loaded = yaml.safe_load(output.read_text(encoding="utf-8"))
    assert loaded["version"] == vs.VOICE_BASELINE_VERSION
    assert set(loaded["features"]) == set(vs.FEATURE_NAMES)
    assert loaded["block_count"] > 0
    assert {record["file"] for record in loaded["corpus"]} == {path.name for path in CORPUS.glob("*.txt")}


# ---------------------------------------------------------------------------
# 签名形状与退化输入
# ---------------------------------------------------------------------------


def _assert_signature_shape(signature: dict) -> None:
    assert signature["version"] == vs.VOICE_SIGNATURE_VERSION
    assert tuple(signature["features"]) == vs.FEATURE_NAMES
    for name, value in signature["features"].items():
        assert isinstance(value, float), name
        assert math.isfinite(value), name
    assert set(signature["top_words"]) == set(vs.TOP_WORD_GROUPS)
    for group, entries in signature["top_words"].items():
        assert len(entries) <= vs.TOP_WORDS_PER_GROUP, group
        for word, share in entries:
            assert isinstance(word, str) and word
            assert 0.0 < share <= 1.0
    assert isinstance(signature["deliberate_repetition"], bool)
    assert set(signature["stats"]) == {"char_count", "sentence_count", "paragraph_count", "quote_count"}


def test_signature_shape_on_golden_corpus(luxun: dict, zhuziqing: dict) -> None:
    for signature in (luxun, zhuziqing):
        _assert_signature_shape(signature)
        assert signature["stats"]["char_count"] > 30000
        assert signature["stats"]["quote_count"] > 0
    # 密度类特征应为正,比例类特征落在 [0, 1]
    for signature in (luxun, zhuziqing):
        features = signature["features"]
        assert features["fw_total_per_1k"] > 100
        assert features["punct_comma_per_1k"] > 10
        for name in vs.FEATURE_NAMES:
            if name.endswith("_share") or name.endswith("_ratio") or name == "lexical_char_ttr":
                assert 0.0 <= features[name] <= 1.0, name
        assert features["dialogue_guide_pre_share"] + features["dialogue_guide_post_share"] + features[
            "dialogue_guide_none_share"
        ] == pytest.approx(1.0, abs=1e-5)
        assert sum(features[f"person_{key}_share"] for key in ("first", "second", "third")) == pytest.approx(
            1.0, abs=1e-5
        )


@pytest.mark.parametrize(
    "text",
    ["", "   \n\n  ", "。。。！！——……“”", "你好", "“走吧。”", "A.", "1234 5678"],
)
def test_degenerate_inputs_are_safe(text: str, baseline: dict) -> None:
    signature = vs.compute_voice_signature_for_text(text, baseline=baseline)
    _assert_signature_shape(signature)
    assert vs.render_voice_habits(signature, baseline) == []
    assert vs.distinctive_features(signature, baseline) is not None


def test_empty_paragraph_list_returns_all_zero(baseline: dict) -> None:
    signature = vs.compute_voice_signature([], baseline=baseline)
    assert all(value == 0.0 for value in signature["features"].values())
    assert all(entries == [] for entries in signature["top_words"].values())
    assert signature["deliberate_repetition"] is False
    assert vs.compute_voice_signature(["", "   "], baseline=baseline) == signature


def test_for_text_matches_paragraph_list(baseline: dict) -> None:
    paragraphs = _paragraphs("zhuziqing_essays.txt")
    from_list = vs.compute_voice_signature(paragraphs, baseline=baseline)
    from_text = vs.compute_voice_signature_for_text("\n\n".join(paragraphs), baseline=baseline)
    assert from_text["features"] == from_list["features"]
    assert from_text["stats"] == from_list["stats"]


# ---------------------------------------------------------------------------
# 两位作者可区分
# ---------------------------------------------------------------------------


def test_two_authors_differ_on_multiple_features(luxun: dict, zhuziqing: dict, baseline: dict) -> None:
    """块级 z(block_count=1)之差 ≥1 的特征至少 3 个,且其中含虚词组特征。"""
    z_lu = vs.feature_z_scores(luxun["features"], baseline["features"], block_count=1)
    z_zhu = vs.feature_z_scores(zhuziqing["features"], baseline["features"], block_count=1)
    separated = {name for name in vs.FEATURE_NAMES if abs(z_lu[name] - z_zhu[name]) >= 1.0}
    assert len(separated) >= 3, separated
    assert any(name.startswith("fw_") for name in separated), separated
    assert any(name.startswith(("punct_", "sent_")) for name in separated), separated
    # 方向相反:鲁迅连接词 / 体标记偏多、结构助词偏少;朱自清相反
    assert z_lu["fw_connective_per_1k"] > z_zhu["fw_connective_per_1k"]
    assert z_lu["fw_aspect_per_1k"] > z_zhu["fw_aspect_per_1k"]
    assert z_lu["fw_particle_per_1k"] < z_zhu["fw_particle_per_1k"]
    # 具体词偏好可从 top_words 读出(鲁迅「么」作句末疑问,朱自清「呢」)
    assert luxun["top_words"]["sentence_final"][0][0] == "么"
    assert zhuziqing["top_words"]["sentence_final"][0][0] == "呢"


def test_distinctive_features_for_whole_book_are_non_empty_and_sorted(luxun: dict, baseline: dict) -> None:
    distinctive = vs.distinctive_features(luxun, baseline, min_abs_z=1.0)
    assert len(distinctive) >= 3
    magnitudes = [abs(item["z"]) for item in distinctive]
    assert magnitudes == sorted(magnitudes, reverse=True)
    for item in distinctive:
        assert item["feature"] in vs.FEATURE_NAMES
        assert item["direction"] == ("high" if item["z"] > 0 else "low")
        assert abs(item["z"]) >= 1.0
    # 阈值抬高后只会变少
    assert len(vs.distinctive_features(luxun, baseline, min_abs_z=3.0)) <= len(distinctive)
    assert vs.distinctive_features(luxun, {}) == []


def test_z_scores_shrink_std_for_aggregated_signature(luxun: dict, baseline: dict) -> None:
    """整书签名聚合了 n 块,std 按 1/sqrt(min(n,16)) 收窄;显式 block_count=1 给字面块级 z。"""
    aggregated = vs.feature_z_scores(luxun, baseline["features"])
    block_level = vs.feature_z_scores(luxun["features"], baseline["features"])
    explicit = vs.feature_z_scores(luxun, baseline["features"], block_count=1)
    assert explicit == block_level
    factor = math.sqrt(vs.Z_MAX_AGGREGATION_BLOCKS)
    for name in ("fw_connective_per_1k", "punct_comma_per_1k", "sent_len_std"):
        assert aggregated[name] == pytest.approx(block_level[name] * factor, rel=1e-4)


# ---------------------------------------------------------------------------
# 渲染
# ---------------------------------------------------------------------------


def test_render_habits_are_bounded_digit_free_and_author_specific(
    luxun: dict, zhuziqing: dict, baseline: dict
) -> None:
    lines_lu = vs.render_voice_habits(luxun, baseline)
    lines_zhu = vs.render_voice_habits(zhuziqing, baseline)
    for lines in (lines_lu, lines_zhu):
        assert 3 <= len(lines) <= vs.MAX_HABIT_LINES
        assert len(set(lines)) == len(lines)
        for line in lines:
            assert isinstance(line, str) and line.strip()
            assert not _DIGITS.search(line), line
            assert len(line) <= 60, line
            # 只描述作者自己,不拿任何基线比方向(E1:1920 年代基线下的「偏少」对当代作者是反的)
            assert "偏多" not in line and "偏少" not in line, line
    assert lines_lu != lines_zhu
    # 具体:作者自己的高频连接词与句末语气词,频率用中文数字说
    connective_lu = [word for word, _share in luxun["top_words"]["connective"][:4]]
    line = next(line for line in lines_lu if line.startswith("连接多用"))
    assert all(word in line for word in connective_lu[:2]), (line, connective_lu)
    assert "每千字约" in line
    final = next(line for line in lines_lu if line.startswith("句末常带"))
    assert luxun["top_words"]["sentence_final"][0][0] in final and "大约每" in final
    assert any(line.startswith("段落平均约") for line in lines_zhu)


def test_render_habits_do_not_depend_on_any_baseline(luxun: dict, baseline: dict) -> None:
    """习惯句是作者自己的绝对描述:给不给基线、给哪份基线,结果都一样。"""
    with_baseline = vs.render_voice_habits(luxun, baseline)
    assert vs.render_voice_habits(luxun, {}) == with_baseline
    assert vs.render_voice_habits(luxun) == with_baseline


def test_render_accepts_features_only(luxun: dict, baseline: dict) -> None:
    features_only = vs.render_voice_habits(luxun["features"], baseline)
    assert features_only
    assert all(not _DIGITS.search(line) for line in features_only)
    # 没有 top_words 就没有「多用某某词」的行,只剩频率与形状
    assert not any(line.startswith("连接多用") or line.startswith("常用副词") for line in features_only)
    assert any(line.startswith("连接词每千字约") for line in features_only)
    assert vs.render_voice_habits({}, baseline) == []
    assert vs.render_voice_habits({"features": {}}, baseline) == []


def test_render_habit_frequencies_are_spelled_in_words() -> None:
    assert vs._every_n_sentences(0.1) == "大约每十句一次"
    assert vs._every_n_sentences(0.5) == "大约每两句一次"
    assert vs._rate_phrase(28.7, "个") == "每千字约二十九个"
    assert vs._rate_phrase(0.4, "处") == "每两千字约一处"
    assert vs._rate_phrase(0.05, "处") == ""
    assert vs._cn_int(61) == "六十一" and vs._cn_int(166) == "一百六十六" and vs._cn_int(105) == "一百零五"


def test_load_voice_baseline_missing_file_degrades(monkeypatch: pytest.MonkeyPatch, luxun: dict) -> None:
    monkeypatch.setattr(vs, "load_optional_yaml_config", lambda _name: {})
    assert vs.load_voice_baseline() == {}
    signature = vs.compute_voice_signature(_paragraphs("luxun_kongyiji.txt"))
    assert signature["deliberate_repetition"] is False
    assert vs.distinctive_features(luxun) == []
    assert isinstance(vs.render_voice_habits(luxun), list)


# ---------------------------------------------------------------------------
# 具体检测
# ---------------------------------------------------------------------------


def test_deliberate_repetition_on_reduplication_dense_text(baseline: dict) -> None:
    dense = "\n\n".join(
        [
            "风轻轻地吹，水慢慢地流，人静静地坐着，天渐渐地黑了。",
            "他悄悄地来，又悄悄地走，屋里空空荡荡，心里冷冷清清。",
            "路远远的，灯昏昏的，狗汪汪地叫，人影晃晃悠悠。",
            "她想了想，看了看，摇摇头，叹叹气，终于走走停停地出了门。",
        ]
        * 4
    )
    signature = vs.compute_voice_signature_for_text(dense, baseline=baseline)
    assert signature["features"]["redup_total_per_1k"] > baseline["features"]["redup_total_per_1k"]["p85"]
    assert signature["deliberate_repetition"] is True

    plain = "\n\n".join(
        [
            "他在傍晚回到那座临河的老屋，把行李放在门边，沿着走廊走到尽头的书房里去。",
            "窗外的河水在暮色里显得格外沉静，对岸的灯一盏一盏亮起来，他没有开灯，只坐在旧椅子上等着夜色完全落下。",
        ]
        * 6
    )
    assert vs.compute_voice_signature_for_text(plain, baseline=baseline)["deliberate_repetition"] is False


# 叠词密度 ≈ 12.7/千字(「一盏一盏」):落在基线 p50 与 p85 之间。
_MID_REDUP_PARAGRAPHS = [
    "他在傍晚回到那座临河的老屋，把行李放在门边，沿着走廊走到尽头的书房里去。",
    "窗外的河水在暮色里显得格外沉静，对岸的灯一盏一盏亮起来，他没有开灯，只坐在旧椅子上等着夜色完全落下。",
]
# 叠词密度远超 p85。
_DENSE_REDUP_PARAGRAPHS = [
    "风轻轻地吹，水慢慢地流，人静静地坐着，天渐渐地黑了。",
    "他悄悄地来，又悄悄地走，屋里空空荡荡，心里冷冷清清。",
    "路远远的，灯昏昏的，狗汪汪地叫，人影晃晃悠悠。",
    "她想了想，看了看，摇摇头，叹叹气，终于走走停停地出了门。",
]


def test_deliberate_repetition_uses_literal_p85_for_whole_book(luxun: dict, zhuziqing: dict, baseline: dict) -> None:
    """规格 §2.W3:≥ p85 才 True;整书签名(≥16 块)不套 1/sqrt(n) 的 p85 带收窄。

    黄金语料自身就是反例:鲁迅的短句连打、朱自清的叠词密度都落在 p50 与 p85 之间,
    收窄带(p50 + (p85 − p50) / 4)会把两部书都标成刻意重复,进而放松下游新鲜度守卫。
    (2026-09-23 起习惯句不再与基线挂钩,旗标只管新鲜度守卫。)
    """
    for signature, name in ((luxun, "sent_short_run_ratio"), (zhuziqing, "redup_total_per_1k")):
        assert vs._block_count_of(signature) >= vs.Z_MAX_AGGREGATION_BLOCKS
        stats = baseline["features"][name]
        value = signature["features"][name]
        assert stats["p50"] < value < stats["p85"], (name, value, stats)
        for feature in vs.REPETITION_FEATURES:
            assert signature["features"][feature] < baseline["features"][feature]["p85"], feature
        assert signature["deliberate_repetition"] is False


def test_deliberate_repetition_threshold_is_block_count_independent(baseline: dict) -> None:
    """同一密度的文本,1 块与 ≥16 块判定一致:p85 以下 False、p85 及以上 True。"""
    redup = baseline["features"]["redup_total_per_1k"]
    short_run_p85 = baseline["features"]["sent_short_run_ratio"]["p85"]

    below = vs.compute_voice_signature(_MID_REDUP_PARAGRAPHS * 320, baseline=baseline)
    assert vs._block_count_of(below) >= vs.Z_MAX_AGGREGATION_BLOCKS
    assert redup["p50"] < below["features"]["redup_total_per_1k"] < redup["p85"]
    assert below["features"]["sent_short_run_ratio"] < short_run_p85
    assert below["deliberate_repetition"] is False
    assert vs.compute_voice_signature(_MID_REDUP_PARAGRAPHS * 6, baseline=baseline)["deliberate_repetition"] is False

    above = vs.compute_voice_signature(_DENSE_REDUP_PARAGRAPHS * 300, baseline=baseline)
    assert vs._block_count_of(above) >= vs.Z_MAX_AGGREGATION_BLOCKS
    assert above["features"]["redup_total_per_1k"] >= redup["p85"]
    assert above["deliberate_repetition"] is True
    # 习惯句按作者自己的绝对密度说「常用叠词」
    assert "常用叠词" in vs.render_voice_habits(above, baseline)
    assert vs.compute_voice_signature(_DENSE_REDUP_PARAGRAPHS * 4, baseline=baseline)["deliberate_repetition"] is True


def test_dialogue_guide_positions_and_verbs(baseline: dict) -> None:
    text = "\n\n".join(
        [
            "他说：“来了。”",  # 冒号前置引导,说
            "“来了。”阿三道。",  # 后置引导,道
            "“来了。”",  # 无引导
            "母亲问，“吃过了么？”",  # 逗号前置引导,问
            "他知道，“这事不能说。”",  # 「知道」被剥离后无引导动词 → 无引导
            "“我不去。”她转身走了。",  # 后文无引导动词 → 无引导
        ]
    )
    signature = vs.compute_voice_signature_for_text(text, baseline=baseline)
    features = signature["features"]
    assert signature["stats"]["quote_count"] == 6
    assert features["dialogue_guide_pre_share"] == pytest.approx(2 / 6, abs=1e-6)
    assert features["dialogue_guide_post_share"] == pytest.approx(1 / 6, abs=1e-6)
    assert features["dialogue_guide_none_share"] == pytest.approx(3 / 6, abs=1e-6)
    assert features["speech_verb_shuo_share"] == pytest.approx(1 / 3, abs=1e-6)
    assert features["speech_verb_dao_share"] == pytest.approx(1 / 3, abs=1e-6)
    assert features["speech_verb_wen_share"] == pytest.approx(1 / 3, abs=1e-6)
    assert features["speech_verb_other_share"] == 0.0
    assert features["speech_verb_shuodao_share"] == 0.0
    assert dict(signature["top_words"]["speech_verb"]) == {"说": pytest.approx(1 / 3, abs=1e-6), "道": pytest.approx(1 / 3, abs=1e-6), "问": pytest.approx(1 / 3, abs=1e-6)}
    # 「笑道 / 说道」归入各自一脉
    compound = vs.compute_voice_signature_for_text("阿四笑道：“好。”\n\n阿五说道：“好。”", baseline=baseline)["features"]
    assert compound["speech_verb_dao_share"] == pytest.approx(0.5)
    assert compound["speech_verb_shuodao_share"] == pytest.approx(0.5)


def test_exclusive_counting_and_sentence_final_particles(baseline: dict) -> None:
    text = "但是他来了。但是她也来了。但是天黑了。但是灯亮了。但是门开了。"
    signature = vs.compute_voice_signature_for_text(text, baseline=baseline)
    connective = dict((word, share) for word, share in signature["top_words"]["connective"])
    assert connective["但是"] > 0
    assert "但" not in connective
    final = vs.compute_voice_signature_for_text("你来吗？我来了呢。天下大定也。", baseline=baseline)["features"]
    assert final["sentence_final_modal_ratio"] == pytest.approx(2 / 3, abs=1e-6)
    assert final["sentence_final_classical_ratio"] == pytest.approx(1 / 3, abs=1e-6)


def test_person_shares_and_four_char_segments(baseline: dict) -> None:
    first = vs.compute_voice_signature_for_text("我来了。我们走吧。我不知道。", baseline=baseline)["features"]
    assert first["person_first_share"] == pytest.approx(1.0)
    third = vs.compute_voice_signature_for_text("他来了，她走了，伊也去了。", baseline=baseline)["features"]
    assert third["person_third_share"] == pytest.approx(1.0)
    idioms = vs.compute_voice_signature_for_text("风平浪静，水波不兴，一叶孤舟。", baseline=baseline)
    assert idioms["features"]["four_char_segment_per_1k"] == pytest.approx(3 * 1000 / 12)


def test_feature_z_scores_handles_all_baseline_forms() -> None:
    features = {"a": 3.0, "b": 10.0, "c": 5.0, "missing": 1.0}
    numeric = vs.feature_z_scores(features, {"a": 1.0, "b": 10.0, "c": 0.0})
    # 仅均值时 std 退到下限(均值 5% 或 1e-6),z 裁到 ±8 且有限
    assert numeric["a"] == 8.0
    assert numeric["b"] == 0.0
    assert numeric["c"] == 8.0
    assert "missing" not in numeric
    mapped = vs.feature_z_scores(features, {"a": {"mean": 1.0, "std": 2.0}, "b": {"mean": 12.0, "std": 4.0}})
    assert mapped["a"] == pytest.approx(1.0)
    assert mapped["b"] == pytest.approx(-0.5)
    overridden = vs.feature_z_scores(features, {"a": {"mean": 1.0, "std": 2.0}}, {"a": 1.0})
    assert overridden["a"] == pytest.approx(2.0)
    assert all(math.isfinite(value) for value in {**numeric, **mapped, **overridden}.values())
    assert vs.feature_z_scores({"a": float("nan")}, {"a": {"mean": 0.0, "std": 1.0}})["a"] == 0.0
    assert vs.feature_z_scores(features, {}) == {}


def test_signature_is_deterministic_and_fast_on_golden_corpus(baseline: dict) -> None:
    paragraphs: list[str] = []
    for path in sorted(CORPUS.glob("*.txt")):
        paragraphs.extend(_paragraphs(path.name))
    started = time.perf_counter()
    first = vs.compute_voice_signature(paragraphs, baseline=baseline)
    elapsed = time.perf_counter() - started
    second = vs.compute_voice_signature(list(paragraphs), baseline=baseline)
    assert first == second
    # ~12 万字应远低于 3 秒(300 万字要求数秒内,单遍扫描)
    assert elapsed < 3.0, elapsed


# ---------------------------------------------------------------------------
# style_signature 分句复用
# ---------------------------------------------------------------------------


def test_query_view_sentence_keeps_terminal_punctuation_and_closing_quote() -> None:
    assert query_view_for_granularity("第一句。第二句！\n\n最后一段很短。", "sentence") == "最后一段很短。"
    # 闭引号紧随句末标点时,最后一句是整个引语而不是孤立的「”」
    assert query_view_for_granularity("他抬头看了看。她说：“走吧。”", "sentence") == "她说：“走吧。”"
    assert query_view_for_granularity("没有句末标点的尾句", "sentence") == "没有句末标点的尾句"
    assert query_view_for_granularity("……", "sentence") == "……"
