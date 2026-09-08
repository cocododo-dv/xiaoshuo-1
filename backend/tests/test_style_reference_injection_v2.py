"""风格模仿 v2 · W4 注入重写(规格 §1.2 / §1.4 / §1.5 / §2.W4)。

真实规模画像下:intensity 三档单调且互不相同(A / B / C / MIXED 都消费 intensity)、
`[声音特征]` 渲染与缺失退化、量化断言软化保留机制句、few-shot 连续段落窗口
(多段、总长受控、场景段型配额、k(i))、多层前缀信息量 ≥ 单层且无半截行 / 空标题、
同画像跨作用域去重、冻结契约冻结相邻段哈希(篡改后退化为单段窗口)、预览 stats、
多层审计读数描述合并后的前缀。
"""

from __future__ import annotations

import hashlib
import math

import pytest

from novel_system.db.session import SessionLocal
from novel_system.services.style_reference import injection as injection_module
from novel_system.services.style_reference.config_loader import clear_config_cache
from novel_system.services.style_reference.injection import (
    InjectionService,
    _allocate_abstract_budget,
    _few_shot_k,
    _intensity_total_chars,
    _metric_level,
    _metric_tendency,
    _soften_quantitative_guidance,
    _soften_summary_clauses,
    _truncate_lines,
    fit_fragments_to_input_budget,
)
from novel_system.services.style_reference.repository import StyleReferenceRepository
from novel_system.services.style_reference.runtime_contract import (
    build_style_runtime_contract,
    extract_style_generation_context,
)
from novel_system.services.style_reference.schemas import (
    InjectionStrategy,
    SystemPromptFragments,
)
from novel_system.services.style_reference.voice_signature import (
    compute_voice_signature,
    render_voice_habits,
)


@pytest.fixture(autouse=True)
def _reset_yaml_cache():
    clear_config_cache()
    yield
    clear_config_cache()


# 对白 / 叙述交替的参考段落(无真实作者原文;只求段型与长度真实)
_PARAGRAPHS: list[tuple[str, str]] = [
    ("narration", "雨在旧檐边停了一瞬，灯影便向里缩了缩。他把湿伞靠在墙角，没有立刻进屋，只听院门外那阵水声慢慢过去，才抬手去拨灯芯。"),
    ("dialogue", "“你来了。”她说，声音很轻，像是怕惊动屋里的什么。"),
    ("narration", "他没有回答，先把袖口的水拧了拧。桌上摆着两只碗，一只是干的，一只还盛着凉透的茶，茶面上浮着一层薄薄的灰。"),
    ("dialogue", "“路上耽搁了。”他终于说，“渡口的船等了一个多时辰，船家不肯开。”"),
    ("dialogue", "“我知道。”她把那只干碗推过去，“先喝口热的。”"),
    ("narration", "灯芯亮起来，屋子却显得更小了。墙上挂着的旧衣裳在光里晃了晃，又静下去。他坐下，手放在膝上，像一个等人问话的孩子。"),
    ("psychology", "他心里明白，这一次回来，和从前每一次都不一样。有些话说出来便收不回去，可不说，又要在心里再压上一年。"),
    ("narration", "外头的雨又密起来了。院子里那株桂树被打得低了头，叶子上滚下的水珠一颗一颗落在石阶上，声音清脆得近乎不合时宜。"),
    ("dialogue", "“她们都还好吗？”他问。"),
    ("dialogue", "“好。”她顿了顿，“老二去年成了亲，在镇上开了间铺子。”"),
    ("narration", "他点点头，把茶碗端起来又放下。热气扑到脸上，他忽然觉得眼睛有些酸，便把头转向窗外，假装看那株低头的桂树。"),
    ("description_env", "窗纸被雨打湿了一角，透进来的光是青灰色的，落在地上像一块洗旧的布。屋角的水缸满了，水面微微颤着，映出一小片摇晃的屋顶。"),
    ("narration", "她没有再说话，只是起身往灶间去了。他听见火钳碰到灶膛的声音，听见水开始在锅里响，那声音把整个屋子都填满了。"),
    ("dialogue", "“明天，”他对着灶间的方向说，“我去看看她们。”"),
    ("narration", "灶间没有回答，只有锅盖被掀开时的一声轻响。他等了等，把碗里的凉茶倒进门边的水缸，水面晃了几晃，又平了。"),
    ("dialogue", "“路不好走。”她端着一碗热汤出来，放在他面前，“山那边的桥去年冲了，还没修。”"),
    ("narration", "他捧起碗，热气把手指烫得一缩，却没有放下。汤里有一点姜的味道，是他记得的那种，多年没有再闻到。"),
    ("dialogue", "“你还是老样子，”她在他对面坐下来，“喝汤先烫手。”"),
    ("psychology", "他想笑，又觉得笑不出来。这屋子里的每一件东西都比他记得的更旧，唯独她说话的腔调，一点没变。"),
    ("narration", "夜深了些，雨声反而稀了。院里的水沟哗哗地响过一阵，也渐渐低下去，只剩下檐角一滴一滴的余声。"),
    ("dialogue", "“睡吧。”她起身收碗，“西屋的床我早铺好了。”"),
    ("narration", "他没有动，看她把碗一只一只叠好，看她把灯芯又压低了些。屋子暗下来，墙上的影子便大了一圈。"),
    ("dialogue", "“灯留着吧。”他说。她停了一下，把灯芯又拨回原处。"),
    ("narration", "西屋的窗户对着后山。他躺下时听见山上有鸟叫了一声，很短，像是被雨呛住了，随后再没有声音。"),
]

_NARRATIVE_PATTERNS = [
    "关键信息放段首一次给出，之后不回头解释",
    "对白之间用一两句动作把停顿落到实物上",
    "情绪只在动作里泄露，不直接命名",
    "场景收束落在一个具体物件或声音上",
    "回忆只以一句嵌进当下动作，不另起段",
    "人物的判断后置，先给可见线索",
]
_STYLE_FEATURES = [
    "用「便」「却」承接，少用「然而」「于是」",
    "对白多无引导词，有引导词时置于引语后",
    "短句主导，连续短句切断长句的地方多在转折处",
    "逗号密集、句号稀疏，一句常含三到四个停顿",
    "四字格偏低，不堆成语",
    "名词具体到器物层面，形容词克制",
    "总是让动作先于解释出现",
    "至少每千字出现一次对物件的静观描写",
    "人称以第三人称为主，偶有「我们」的抽离视角",
    "声音描写多于视觉描写",
    "句尾少用语气词，问句极少",
    "段落以叙述句起，以动作或声音收",
]
# 真实规模补足:合成期一个画像常有上百条陈述;这里再派生 30 条无数字的机制句,
# 让 intensity 三档在 A / C(无样例)下也必然截到不同位置。
_PLACES = ("转折处", "段首", "段末", "对白之间", "回忆嵌入处", "场景收束处")
_CARRIERS = ("器物", "声音", "光线", "动作", "停顿")
_STYLE_FEATURES += [
    f"在{place}让{carrier}承担情绪，不直接命名"
    for place in _PLACES
    for carrier in _CARRIERS
]
_CALIBRATION = [
    "若解释过多就改回动作",
    "若比喻密集就删到只剩一个",
    "若段落过碎就合并同一叙事单元",
    "若对白连续三轮无动作就插入一处停顿",
]
_BANNED_RULES = [
    "禁复用参考书的专名与独特意象",
    "禁堆砌华丽形容词",
    "禁排比抒情",
    "禁用「仿佛」「似乎」连续开头",
    "禁在段末点题",
    "禁模仿参考书的情节走向",
]
_HABITS_FALLBACK = [
    "连接词多用便、却、又，少用然而、于是",
    "对白多无引导词；有引导词时置于引语后",
    "逗号密集、句号稀疏，一句常含三到四个停顿",
    "四字格偏低，不堆成语",
    "第三人称为主，偶有第一人称复数抽离",
    "句尾语气词少，问句少",
]


def _seed_book(repo: StyleReferenceRepository, seed: str, *, cloud_policy: str = "segments_only") -> str:
    book_id = f"v2_book_{seed}"
    repo.create_book(
        book_id=book_id,
        title="匿名参考",
        source_kind="upload",
        cloud_policy=cloud_policy,
        text_checksum=hashlib.sha256(seed.encode("utf-8")).hexdigest(),
        total_chars=sum(len(text) for _ptype, text in _PARAGRAPHS),
        status="ready",
        stats_json=(
            {"rights_declaration": {"declared": True, "analysis_rights": True, "send_rights": True}}
            if cloud_policy != "local_only"
            else {}
        ),
    )
    repo.create_run(run_id=f"v2_run_{seed}", book_id=book_id, status="done", phase="done")
    for index, (ptype, text) in enumerate(_PARAGRAPHS):
        repo.create_paragraph(
            paragraph_id=f"v2_p_{seed}_{index}",
            book_id=book_id,
            paragraph_index=index,
            paragraph_type=ptype,
            start_offset=0,
            end_offset=len(text),
            text=text,
            char_count=len(text),
            classifier_confidence=0.9,
        )
    return book_id


def _seed_quotes(repo: StyleReferenceRepository, seed: str, book_id: str) -> dict[str, list[str]]:
    """每个段型取若干段的首句作 quote,建 scene_samples_index。"""
    index: dict[str, list[str]] = {}
    for pidx, (ptype, text) in enumerate(_PARAGRAPHS):
        quote_text = text[: min(len(text), 18)]
        quote_id = f"v2_q_{seed}_{pidx}"
        repo.create_quote(
            quote_id=quote_id,
            book_id=book_id,
            paragraph_id=f"v2_p_{seed}_{pidx}",
            span_start=0,
            span_end=len(quote_text),
            quote_text=quote_text,
            illustrates_dims=["language.rhythm"],
            extracted_features={},
        )
        index.setdefault(ptype, []).append(quote_id)
    return index


def _profile_json(samples_index: dict[str, list[str]] | None, *, with_voice: bool = True) -> dict:
    texts = [text for _ptype, text in _PARAGRAPHS]
    payload: dict = {
        "narrative_summary": "克制观察，动作先于解释；对白短促，停顿落在器物上。",
        "qualitative_summary": "克制观察，动作先于解释；对白短促，停顿落在器物上。",
        "style_features": list(_STYLE_FEATURES),
        "narrative_patterns": list(_NARRATIVE_PATTERNS),
        "calibration_guidance": list(_CALIBRATION),
        "banned_replication_rules": list(_BANNED_RULES),
        "narrative_guidance": list(_NARRATIVE_PATTERNS[:5]),
        "metrics_baseline": {
            "avg_sentence_length": {"mean": 11.0, "std": 3.0},
            "short_sentence_ratio": {"mean": 0.45, "std": 0.08},
            "paragraph_mean_chars": {"mean": 62.0, "std": 20.0},
            "paragraphs_per_1k": {"mean": 15.0, "std": 3.0},
            "punctuation_density_per_1k": {"mean": 170.0, "std": 15.0},
            "question_density_per_1k": {"mean": 1.2, "std": 0.6},
            "classical_word_ratio": {"mean": 0.02, "std": 0.01},
            "colloquial_marker_ratio": {"mean": 0.03, "std": 0.01},
        },
    }
    if samples_index is not None:
        payload["scene_samples_index"] = samples_index
    if with_voice:
        signature = compute_voice_signature(texts)
        habits = render_voice_habits(signature) or list(_HABITS_FALLBACK)
        payload["voice_signature"] = {**signature, "habits": habits}
    return payload


def _seed_profile(
    repo: StyleReferenceRepository,
    seed: str,
    book_id: str,
    *,
    profile_json: dict,
    profile_id: str | None = None,
) -> str:
    profile_id = profile_id or f"v2_profile_{seed}"
    repo.create_profile(
        profile_id=profile_id,
        book_id=book_id,
        run_id=f"v2_run_{seed}",
        title="匿名风格",
        status="active",
        profile_json=profile_json,
        coverage_json={},
        source_finding_ids_json=[],
    )
    return profile_id


def _bind(
    repo: StyleReferenceRepository,
    *,
    binding_id: str,
    profile_id: str,
    scope: str,
    scope_ref_id: str,
    strategy: str,
    config_json: dict | None = None,
):
    return repo.create_binding(
        binding_id=binding_id,
        profile_id=profile_id,
        scope=scope,
        scope_ref_id=scope_ref_id,
        task_type="scene_generation",
        strategy=strategy,
        config_json=config_json or {},
        status="active",
    )


def _seed_full(seed: str, *, with_voice: bool = True, cloud_policy: str = "segments_only") -> tuple[str, str]:
    """book + 段落 + 引文 + 画像(含 voice_signature);返回 (book_id, profile_id)。"""
    with SessionLocal() as session:
        repo = StyleReferenceRepository(session)
        book_id = _seed_book(repo, seed, cloud_policy=cloud_policy)
        index = _seed_quotes(repo, seed, book_id)
        profile_id = _seed_profile(
            repo, seed, book_id, profile_json=_profile_json(index, with_voice=with_voice)
        )
        session.commit()
    return book_id, profile_id


def _render(profile_id: str, strategy: str, config: dict, *, context_text: str | None = None):
    with SessionLocal() as session:
        svc = InjectionService(session)
        svc.context_text = context_text
        profile = StyleReferenceRepository(session).get_profile(profile_id)
        fragments, stats = svc.render_preview(profile, InjectionStrategy(strategy), config)
    return fragments, stats


def _entry_lines(block: str) -> list[str]:
    return [line for line in block.splitlines() if line.startswith("- ")]


def _assert_no_half_lines_or_empty_headings(fragments: SystemPromptFragments, source_lines: set[str]) -> None:
    for block, check_source in (
        (fragments.positive_block, True),
        (fragments.forbidden_block, True),
        (fragments.metric_anchor_block, False),  # 软分布行由基线渲染,不在来源集合里
        (fragments.voice_block, True),
    ):
        lines = block.splitlines()
        if not lines:
            continue
        # 无孤立标题:最后一行不是标题
        assert not lines[-1].rstrip().endswith(("]", ":", "：")), block
        assert not lines[-1].startswith("["), block
        if not check_source:
            continue
        # 无半截行:每条 `- ` 条目都是完整的来源行
        for line in lines:
            if line.startswith("- "):
                body = line[2:]
                for tag in ("[表达机制] ", "[叙事机制] ", "[偏离校准] "):
                    if body.startswith(tag):
                        body = body[len(tag):]
                assert body in source_lines, line


# ---------------------------------------------------------------------------
# 预算 / 强度语义(§1.4 / §1.5)
# ---------------------------------------------------------------------------


def test_budget_allocation_and_k_follow_spec_formulas() -> None:
    assert _intensity_total_chars(0) == 900
    assert _intensity_total_chars(50) == 1650
    assert _intensity_total_chars(100) == 2400
    alloc = _allocate_abstract_budget(100, 1)
    assert set(alloc) == {"positive", "forbidden", "metric", "voice"}
    assert alloc == {"positive": 1080, "forbidden": 480, "metric": 360, "voice": 480}
    assert sum(_allocate_abstract_budget(0, 1).values()) <= 900
    # 多层:×(1 + 0.35×(n-1)),上限 ×1.7
    assert sum(_allocate_abstract_budget(100, 2).values()) <= 2400 * 1.35
    assert sum(_allocate_abstract_budget(100, 3).values()) <= 2400 * 1.7
    assert sum(_allocate_abstract_budget(100, 5).values()) <= 2400 * 1.7
    assert sum(_allocate_abstract_budget(100, 5).values()) > 2400 * 1.6
    assert _few_shot_k(0) == 2 and _few_shot_k(50) == 4 and _few_shot_k(100) == 6
    assert _few_shot_k(25) == 3  # round(2 + 4 × 0.25) = 3(四舍五入,不用银行家舍入)


@pytest.mark.parametrize("strategy", ["A", "B", "C", "mixed"])
def test_intensity_three_levels_are_distinct_and_monotone(strategy: str) -> None:
    """真实规模画像下 intensity 0 / 50 / 100 三档前缀互不相同且长度单调(四种策略)。"""
    _book_id, profile_id = _seed_full(f"int_{strategy}")
    prefixes = []
    for intensity in (0, 50, 100):
        fragments, stats = _render(profile_id, strategy, {"intensity": intensity})
        prefixes.append(fragments.to_system_prompt_prefix())
        assert stats["intensity_effective_total_chars"] == _intensity_total_chars(intensity)
        assert stats["total_prefix_chars"] == len(prefixes[-1])
        if strategy in ("B", "mixed"):
            assert stats["few_shot_k"] == _few_shot_k(intensity)
            assert 1 <= stats["few_shot_windows"] <= stats["few_shot_k"]
        else:
            assert stats["few_shot_k"] == 0 and stats["few_shot_windows"] == 0
    assert len({p for p in prefixes}) == 3, "三档前缀必须互不相同"
    assert len(prefixes[0]) < len(prefixes[1]) < len(prefixes[2])
    # 每档都带红线段(任一风格块非空即随注)
    assert all("严格禁止" in p or "严禁" in p for p in prefixes)


def test_strategy_a_has_no_examples_and_c_has_no_few_shot() -> None:
    _book_id, profile_id = _seed_full("excl")
    a, _ = _render(profile_id, "A", {"intensity": 100})
    assert a.few_shot_block == "" and a.rag_block == ""
    assert a.voice_block.startswith("[声音特征]")
    c, c_stats = _render(profile_id, "C", {"intensity": 100})
    assert c.few_shot_block == ""  # C 与 few-shot 互斥
    assert c.metric_anchor_block == ""
    assert c.voice_block.startswith("[声音特征]")
    assert len(c.forbidden_block) <= 200
    assert c.rag_block  # memory 后端:ensure_rag_index 重建后真召回
    assert c_stats["rag_snippets"] >= 1
    assert "[UNTRUSTED_REFERENCE_DATA:rag]" in c.rag_block


# ---------------------------------------------------------------------------
# voice_block(§1.2 / §2.W4.2)
# ---------------------------------------------------------------------------


def test_voice_block_renders_habits_and_degrades_when_missing() -> None:
    _book_id, with_voice = _seed_full("voice_yes")
    _book_id2, without_voice = _seed_full("voice_no", with_voice=False)
    fragments, stats = _render(with_voice, "mixed", {"intensity": 80})
    assert fragments.voice_block.startswith("[声音特征]")
    assert stats["voice_lines"] == len(_entry_lines(fragments.voice_block)) >= 1
    assert not any(char.isdigit() for char in fragments.voice_block)
    prefix = fragments.to_system_prompt_prefix()
    # 顺序 §1.2:metric → voice → positive → forbidden → few_shot → 红线
    assert prefix.index("风格分布指导") < prefix.index("[声音特征]") < prefix.index("[正向风格特征]")
    assert prefix.index("[正向风格特征]") < prefix.index("[禁忌模式]") < prefix.index("风格样例")
    assert prefix.index("风格样例") < prefix.index("严格禁止")
    legacy, legacy_stats = _render(without_voice, "mixed", {"intensity": 80})
    assert legacy.voice_block == ""
    assert "[声音特征]" not in legacy.to_system_prompt_prefix()
    assert legacy_stats["voice_lines"] == 0


def test_voice_block_is_truncated_by_ratio_on_whole_lines() -> None:
    _book_id, profile_id = _seed_full("voice_cut")
    with SessionLocal() as session:
        repo = StyleReferenceRepository(session)
        profile = repo.get_profile(profile_id)
        payload = dict(profile.profile_json)
        voice = dict(payload["voice_signature"])
        voice["habits"] = [f"习惯{stem}：连接词多用便与却，少用然而与于是，对白引导词后置" for stem in "甲乙丙丁戊己庚辛壬癸子丑寅卯"]
        payload["voice_signature"] = voice
        profile.profile_json = payload
        session.commit()
    low, _ = _render(profile_id, "A", {"intensity": 0})
    high, _ = _render(profile_id, "A", {"intensity": 100})
    assert len(low.voice_block) <= 180  # 900 × 0.20
    assert len(high.voice_block) <= 480
    assert len(_entry_lines(low.voice_block)) < len(_entry_lines(high.voice_block))
    assert all(line.endswith("后置") for line in _entry_lines(low.voice_block))


def test_mixed_include_voice_switch() -> None:
    _book_id, profile_id = _seed_full("voice_switch")
    fragments, _ = _render(profile_id, "mixed", {"intensity": 80, "include_voice": False})
    assert fragments.voice_block == ""
    assert fragments.positive_block


# ---------------------------------------------------------------------------
# 量化断言软化(§2.W4.4)
# ---------------------------------------------------------------------------


def test_soften_keeps_short_sentence_mechanisms_and_strips_numbers() -> None:
    baseline = {
        "avg_sentence_length": {"mean": 10.0, "std": 2.0},
        "short_sentence_ratio": {"mean": 0.5, "std": 0.05},
        "question_density_per_1k": {"mean": 0.3, "std": 0.1},
    }
    assert _soften_quantitative_guidance("短句主导，转折处用短句切断长句", baseline) == "短句主导，转折处用短句切断长句"
    softened = _soften_quantitative_guidance("连续3个短句切断长句，总是落在转折处", baseline)
    assert softened == "连续若干个短句切断长句，多落在转折处"
    assert not any(char.isdigit() for char in softened)
    # 与基线(问号极少)方向相反的频率断言 → 丢弃
    assert _soften_quantitative_guidance("高频设问，每千字至少2次", baseline) is None
    # 方向线索指向「少」且与基线一致 → 软化保留(至少 / 每千字 剥除,从不 → 少)
    assert _soften_quantitative_guidance("每千字至少出现2次设问，从不解释", baseline) == "出现若干次设问，少解释"
    assert _soften_quantitative_guidance("从不在段末解释", baseline) == "少在段末解释"
    # 方向相反的频率断言丢弃:基线句均 10(短句主导)vs「长句主导」
    assert _soften_quantitative_guidance("长句主导，句子频繁拖长", baseline) is None
    # 量化摘要(≥2 个数字 token)整行丢弃
    assert _soften_quantitative_guidance("句均约18.0字、短句约32%", baseline) is None
    # 标识符里的数字不算量化 token
    assert _soften_quantitative_guidance("第3人称叙述为主", baseline) == "第3人称叙述为主"
    # 无基线:不判冲突,只软化
    assert _soften_quantitative_guidance("大量使用短句", {}) == "多使用短句"


def test_positive_block_keeps_softened_mechanism_lines() -> None:
    _book_id, profile_id = _seed_full("soften")
    fragments, _ = _render(profile_id, "A", {"intensity": 100})
    block = fragments.positive_block
    assert "短句主导，连续短句切断长句的地方多在转折处" in block
    assert "多让动作先于解释出现" in block  # 总是 → 多
    assert "出现一次对物件的静观描写" in block  # 至少 / 每千字 剥除,机制保留
    assert "至少" not in block and "每千字" not in block
    assert "概述:克制观察" in block


def test_soften_strips_bare_numbers_ranges_and_per_thousand_suffix() -> None:
    """裸比例 / 小数 / 「/千字」/ 区间数字同样不得进入 [正向风格特征]。"""
    for line in (
        "短句占比0.6",
        "问号比例0.02",
        "标点密度180/千字",
        "句均字数15.3，短句占比0.6",
        "每段2-3句",
        "对白轮次约占4成，句均12.5字左右",
    ):
        softened = _soften_quantitative_guidance(line, {})
        assert softened is None or not any(char.isdigit() for char in softened), (line, softened)
    # 纯「指标 + 数字」的量化摘要没有机制可保留 → 丢弃(而不是留下「短句占比」)
    assert _soften_quantitative_guidance("短句占比0.6", {}) is None
    assert _soften_quantitative_guidance("标点密度180/千字", {}) is None
    assert _soften_quantitative_guidance("句均字数15.3，短句占比0.6", {}) is None
    # 区间只留上界再按量词软化,不留半截连字符;机制保留
    assert _soften_quantitative_guidance("每段2-3句，句间用动作停顿", {}) == "每段若干句，句间用动作停顿"
    # 标识符里的数字仍保留
    assert _soften_quantitative_guidance("OS1式旁白偏多", {}) == "OS1式旁白偏多"
    assert _soften_quantitative_guidance("第3人称叙述主导", {}) == "第3人称叙述主导"


def test_soften_uses_sentence_fraction_bands_and_keeps_corroborated_claims() -> None:
    """短句 / 长句占比按句子比例分档;同组内有指标同向即不算冲突(W4.4:只丢反向断言)。"""
    assert _metric_level("short_sentence_ratio", 0.22) is None
    assert _metric_level("short_sentence_ratio", 0.45) == "high"
    assert _metric_level("short_sentence_ratio", 0.10) == "low"
    assert _metric_level("long_sentence_ratio", 0.29) is None
    assert _metric_level("long_sentence_ratio", 0.10) == "low"
    long_baseline = {
        "avg_sentence_length": {"mean": 24.0, "std": 3.0},
        "short_sentence_ratio": {"mean": 0.22, "std": 0.05},
        "long_sentence_ratio": {"mean": 0.29, "std": 0.05},
    }
    # 长句作者:「短句偏少」与句均 24 同向 → 保留,不能被通用 ratio 档误判成冲突
    assert _soften_quantitative_guidance("短句偏少，句子多绵长", long_baseline) == "短句偏少，句子多绵长"
    assert _soften_quantitative_guidance("短句偏少", long_baseline) == "短句偏少"
    assert _soften_quantitative_guidance("长句偏多", long_baseline) == "长句偏多"
    # 反向断言仍丢弃
    assert _soften_quantitative_guidance("短句密集", long_baseline) is None
    # 同组一个指标误判 / 矛盾时,另一个指标已证实同向 → 不丢
    contradictory = {
        "avg_sentence_length": {"mean": 24.0, "std": 3.0},
        "short_sentence_ratio": {"mean": 0.45, "std": 0.05},
    }
    assert _soften_quantitative_guidance("短句偏少", contradictory) == "短句偏少"
    # 真实基线(短句 0.32 / 长句 0.29)不再被同时标成「较高频」
    assert _metric_tendency("short_sentence_ratio", 0.316, std=None) == "适量"
    assert _metric_tendency("long_sentence_ratio", 0.294, std=None) == "适量"
    assert _metric_tendency("short_sentence_ratio", 0.45, std=None) == "偏多"
    assert _metric_tendency("long_sentence_ratio", 0.10, std=None) == "偏少"


def test_summary_line_is_softened_per_clause_instead_of_deleted() -> None:
    """W4.4:概述行按分句处理——只丢反向的「短句密集」,其余机制分句保留。"""
    baseline = {"avg_sentence_length": {"mean": 23.0, "std": 3.0}}
    with SessionLocal() as session:
        repo = StyleReferenceRepository(session)
        book_id = _seed_book(repo, "clause")
        payload = _profile_json(None)
        payload["qualitative_summary"] = "短句密集，动作先于解释，停顿落在器物上"
        payload["narrative_summary"] = payload["qualitative_summary"]
        payload["metrics_baseline"] = dict(baseline)
        profile_id = _seed_profile(repo, "clause", book_id, profile_json=payload)
        session.commit()
    fragments, _ = _render(profile_id, "A", {"intensity": 100})
    summary_lines = [line for line in fragments.positive_block.splitlines() if line.startswith("概述:")]
    assert len(summary_lines) == 1, fragments.positive_block
    assert "动作先于解释" in summary_lines[0] and "停顿落在器物上" in summary_lines[0]
    assert "短句密集" not in fragments.positive_block
    # 分句级单元:只丢反向分句;全部被丢 → 空串(概述行省略)
    assert _soften_summary_clauses("短句密集，动作先于解释，停顿落在器物上", baseline) == "动作先于解释，停顿落在器物上"
    assert _soften_summary_clauses("动作先于解释；短句密集。", baseline) == "动作先于解释"
    assert _soften_summary_clauses("短句密集。", baseline) == ""


# ---------------------------------------------------------------------------
# few-shot 连续段落窗口(§2.W4.5)
# ---------------------------------------------------------------------------


def test_few_shot_windows_are_multi_paragraph_and_bounded() -> None:
    _book_id, profile_id = _seed_full("windows")
    fragments, stats = _render(profile_id, "B", {"intensity": 100})
    block = fragments.few_shot_block
    assert "[UNTRUSTED_REFERENCE_DATA:few_shot]" in block
    assert "风格样例" in block
    assert stats["few_shot_windows"] == stats["few_shot_k"] == 6
    assert "连续" in block and "段窗口" in block  # 多段窗口
    # 窗口内段落以换行分隔(模型能看到换段)
    windows = [seg.split("」")[0] for seg in block.split("「")[1:] if "」" in seg]
    assert any("\n" in window for window in windows)
    assert stats["few_shot_chars"] <= 3600
    inner = block.split("[UNTRUSTED_REFERENCE_DATA:few_shot]")[1].split("[/UNTRUSTED_REFERENCE_DATA]")[0]
    assert len(inner) <= 3600 + 200
    # 窗口不重叠:同一段落不出现两次
    for _ptype, text in _PARAGRAPHS:
        assert block.count(text[:20]) <= 1
    assert fragments.anti_plagiarism_block


def test_few_shot_window_count_follows_intensity() -> None:
    _book_id, profile_id = _seed_full("kwin")
    counts = []
    for intensity in (0, 50, 100):
        _fragments, stats = _render(profile_id, "mixed", {"intensity": intensity})
        counts.append(stats["few_shot_windows"])
        assert stats["few_shot_windows"] == _few_shot_k(intensity)
    assert counts == [2, 4, 6]


def test_dialogue_heavy_scene_gets_dialogue_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    """对白占比高的中性稿 → 预览 / 渲染都必须真的把对白配额 ceil(k/2) 交给选段。"""
    _book_id, profile_id = _seed_full("dlg")
    dialogue_context = "\n".join(
        [
            "“你今晚还走吗？”她问。",
            "“不走了。”他说。",
            "“那把灯留着。”",
            "“好。”",
            "他把灯芯拨亮了些。",
        ]
    )
    calls: list[dict[str, int]] = []
    original_pick = injection_module._pick_sample_windows

    def _spy(candidates, *, k, dialogue_quota):
        calls.append({"k": k, "dialogue_quota": dialogue_quota})
        return original_pick(candidates, k=k, dialogue_quota=dialogue_quota)

    monkeypatch.setattr(injection_module, "_pick_sample_windows", _spy)
    fragments, stats = _render(profile_id, "B", {"intensity": 100}, context_text=dialogue_context)
    # render_preview 必须把调用方设置的 context_text 传给 _render:配额真的生效
    assert calls and calls[-1] == {"k": 6, "dialogue_quota": math.ceil(6 / 2)}
    block = fragments.few_shot_block
    windows = [seg.split("」")[0] for seg in block.split("「")[1:]]
    assert len(windows) == stats["few_shot_windows"] >= 4
    with_dialogue = [w for w in windows if "“" in w]
    assert len(with_dialogue) * 2 >= len(windows), "对白占比高的场景至少一半窗口含对白"
    # 无上下文时不设配额(对照组)
    calls.clear()
    _render(profile_id, "B", {"intensity": 100})
    assert calls and calls[-1] == {"k": 6, "dialogue_quota": 0}


def test_few_shot_respects_local_only_and_missing_index() -> None:
    _book_id, local_profile = _seed_full("local", cloud_policy="local_only")
    fragments, stats = _render(local_profile, "B", {"intensity": 100})
    assert fragments.few_shot_block == "" and stats["few_shot_windows"] == 0
    assert fragments.positive_block  # 抽象块照常
    with SessionLocal() as session:
        repo = StyleReferenceRepository(session)
        book_id = _seed_book(repo, "noindex")
        profile_id = _seed_profile(repo, "noindex", book_id, profile_json=_profile_json(None))
        session.commit()
    fragments, stats = _render(profile_id, "B", {"intensity": 100})
    assert fragments.few_shot_block == "" and stats["few_shot_windows"] == 0


def _seed_scattered_quotes(
    repo: StyleReferenceRepository, seed: str, book_id: str, indices: list[int]
) -> dict[str, list[str]]:
    """只给零散的几段各一条引文(真实画像:引文是 finding 证据片段,相邻段通常无引文)。"""
    index: dict[str, list[str]] = {}
    for pidx in indices:
        ptype, text = _PARAGRAPHS[pidx]
        quote_text = text[: min(len(text), 18)]
        quote_id = f"v2_q_{seed}_{pidx}"
        repo.create_quote(
            quote_id=quote_id,
            book_id=book_id,
            paragraph_id=f"v2_p_{seed}_{pidx}",
            span_start=0,
            span_end=len(quote_text),
            quote_text=quote_text,
            illustrates_dims=[],
            extracted_features={},
        )
        index.setdefault(ptype, []).append(quote_id)
    return index


def _few_shot_entries(block: str) -> list[str]:
    return [line for line in block.splitlines() if line.startswith("- (")]


def test_frozen_contract_windows_only_use_hashed_paragraphs() -> None:
    """冻结契约冻结 quote 父段 **及其连续相邻段** 的哈希:生产(冻结)路径下零散引文
    仍得到多段窗口,MIXED@80 ≥4 个窗口且为多段;窗口只用契约里有哈希的段落。"""
    seed = "frozen"
    project_id = "v2_proj_frozen"
    scattered = [2, 6, 10, 14, 18, 22]  # 相邻段都没有引文
    with SessionLocal() as session:
        repo = StyleReferenceRepository(session)
        book_id = _seed_book(repo, seed)
        samples_index = _seed_scattered_quotes(repo, seed, book_id, scattered)
        profile_id = _seed_profile(repo, seed, book_id, profile_json=_profile_json(samples_index))
        binding = _bind(
            repo,
            binding_id="v2_bind_frozen",
            profile_id=profile_id,
            scope="project",
            scope_ref_id=project_id,
            strategy="mixed",
            config_json={"intensity": 80},
        )
        session.flush()
        contract = build_style_runtime_contract(repo, [binding], task_type="scene_generation")
        assert contract is not None
        refs = contract["layers"][0]["sample_paragraph_refs"]
        frozen_ids = {ref["paragraph_id"] for ref in refs}
        # 父段 + 每侧 (few_shot_window_paragraphs − 1) = 2 段连续相邻段,只冻哈希不冻原文
        expected_ids = {
            f"v2_p_{seed}_{index}"
            for center in scattered
            for index in range(center - 2, center + 3)
            if 0 <= index < len(_PARAGRAPHS)
        }
        assert frozen_ids == expected_ids
        for ref in refs:
            pidx = int(ref["paragraph_id"].rsplit("_", 1)[1])
            assert ref["paragraph_sha256"] == hashlib.sha256(_PARAGRAPHS[pidx][1].encode("utf-8")).hexdigest()
        for _ptype, text in _PARAGRAPHS:
            assert text[:12] not in str(contract)
        context = extract_style_generation_context("她在门外停步。", source_kind="generation_source")
        svc = InjectionService(session)
        frozen = svc.fragments_for_contract(contract, project_id=project_id, context=context)
        frozen_stats = dict(svc.last_render_stats or {})
        live = InjectionService(session).fragments_for(project_id, "scene_generation")
    frozen_entries = _few_shot_entries(frozen.few_shot_block)
    # 验收(规格 §3):MIXED@80 → few-shot ≥4 个窗口且为多段——在生产走的冻结路径上成立
    assert frozen_stats["few_shot_windows"] == len(frozen_entries) >= 4
    assert all("段窗口" in line for line in frozen_entries), frozen_entries
    assert not any("完整参考段落" in line for line in frozen_entries)
    # 相邻段原文真的进了窗口(第 2 段的邻段 1 / 3 之一)
    assert _PARAGRAPHS[1][1][:10] in frozen.few_shot_block or _PARAGRAPHS[3][1][:10] in frozen.few_shot_block
    # 只用契约里有哈希的段落
    for _ptype, text in _PARAGRAPHS:
        if text[:12] in frozen.few_shot_block:
            assert any(text == _PARAGRAPHS[int(pid.rsplit("_", 1)[1])][1] for pid in frozen_ids)
    # 实时路径同样是多段窗口
    assert any("段窗口" in line for line in _few_shot_entries(live.few_shot_block))


def test_frozen_contract_tampered_neighbour_degrades_to_single_paragraph() -> None:
    """回放不可变性:相邻段冻结后被改动 → sha256 失配 → 该侧不再展开,退化为单段窗口。"""
    seed = "tamper"
    project_id = "v2_proj_tamper"
    with SessionLocal() as session:
        repo = StyleReferenceRepository(session)
        book_id = _seed_book(repo, seed)
        # 只给第 5 段(叙述,两侧 4 / 6 是对白)一条引文
        samples_index = _seed_scattered_quotes(repo, seed, book_id, [5])
        profile_id = _seed_profile(repo, seed, book_id, profile_json=_profile_json(samples_index))
        binding = _bind(
            repo,
            binding_id="v2_bind_tamper",
            profile_id=profile_id,
            scope="project",
            scope_ref_id=project_id,
            strategy="B",
            config_json={"intensity": 100},
        )
        session.flush()
        contract = build_style_runtime_contract(repo, [binding], task_type="scene_generation")
        assert contract is not None
        refs = [ref["paragraph_id"] for ref in contract["layers"][0]["sample_paragraph_refs"]]
        assert refs[0] == f"v2_p_{seed}_5"
        assert set(refs) == {f"v2_p_{seed}_{index}" for index in range(3, 8)}
        context = extract_style_generation_context("她在门外停步。", source_kind="generation_source")
        intact = InjectionService(session).fragments_for_contract(contract, project_id=project_id, context=context)
        intact_entries = _few_shot_entries(intact.few_shot_block)
        assert intact_entries and all("段窗口" in line for line in intact_entries)
        assert _PARAGRAPHS[4][1][:10] in intact.few_shot_block or _PARAGRAPHS[6][1][:10] in intact.few_shot_block
        # 冻结后两侧相邻段都被改过
        for index in (4, 6):
            paragraph = repo.get_paragraph(f"v2_p_{seed}_{index}")
            paragraph.text = f"“这段对白在冻结之后被改过了。”她说了第{index}遍。"
        session.flush()
        tampered = InjectionService(session).fragments_for_contract(contract, project_id=project_id, context=context)
    tampered_entries = _few_shot_entries(tampered.few_shot_block)
    assert tampered_entries and all("完整参考段落" in line for line in tampered_entries)
    assert not any("段窗口" in line for line in tampered_entries)
    assert "被改过了" not in tampered.few_shot_block
    assert _PARAGRAPHS[4][1][:10] not in tampered.few_shot_block
    assert _PARAGRAPHS[6][1][:10] not in tampered.few_shot_block
    assert _PARAGRAPHS[5][1][:10] in tampered.few_shot_block


# ---------------------------------------------------------------------------
# 多层(§2.W4.6)
# ---------------------------------------------------------------------------


def test_layered_prefix_keeps_examples_and_has_no_half_lines() -> None:
    project_id = "v2_proj_layers"
    with SessionLocal() as session:
        repo = StyleReferenceRepository(session)
        base_book = _seed_book(repo, "lay_base")
        base_index = _seed_quotes(repo, "lay_base", base_book)
        base_profile = _seed_profile(repo, "lay_base", base_book, profile_json=_profile_json(base_index))
        scene_book = _seed_book(repo, "lay_scene")
        scene_index = _seed_quotes(repo, "lay_scene", scene_book)
        scene_payload = _profile_json(scene_index)
        scene_payload["style_features"] = [f"场景层{line}" for line in _STYLE_FEATURES]
        scene_payload["banned_replication_rules"] = ["禁场景层独有的复刻", *_BANNED_RULES]
        scene_profile = _seed_profile(repo, "lay_scene", scene_book, profile_json=scene_payload)
        _bind(repo, binding_id="v2_bind_lay_p", profile_id=base_profile, scope="project", scope_ref_id=project_id, strategy="mixed", config_json={"intensity": 70})
        _bind(repo, binding_id="v2_bind_lay_s", profile_id=scene_profile, scope="scene", scope_ref_id="v2_scene_lay", strategy="mixed", config_json={"intensity": 70})
        session.commit()
    with SessionLocal() as session:
        single = InjectionService(session).fragments_for(project_id, "scene_generation")
        layered = InjectionService(session).fragments_for(project_id, "scene_generation", scene_id="v2_scene_lay")
    single_prefix = single.to_system_prompt_prefix()
    layered_prefix = layered.to_system_prompt_prefix()
    # 信息量 ≥ 单层:正向条目更多、禁忌合并去重、样例不丢
    assert len(_entry_lines(layered.positive_block)) >= len(_entry_lines(single.positive_block))
    assert len(layered_prefix) >= len(single_prefix)
    assert layered.few_shot_block, "多层不得丢弃最具体层的样例"
    assert any(text[:10] in layered.few_shot_block for _ptype, text in _PARAGRAPHS)
    assert any(
        "段窗口" in line for line in layered.few_shot_block.splitlines() if line.startswith("- (")
    )
    assert layered.forbidden_block.count("[禁忌模式]") == 1
    assert layered.forbidden_block.count("禁堆砌华丽形容词") == 1
    assert "禁场景层独有的复刻" in layered.forbidden_block
    assert layered.positive_block.count("[正向风格特征]") == 1
    assert layered.voice_block.startswith("[声音特征]")
    source_lines = set()
    for payload_lines in (_STYLE_FEATURES, _NARRATIVE_PATTERNS, _CALIBRATION, _BANNED_RULES):
        source_lines.update(payload_lines)
        source_lines.update(f"场景层{line}" for line in payload_lines)
    source_lines.add("禁场景层独有的复刻")
    # 软化后的行也算来源行
    from novel_system.services.style_reference.injection import _soften_quantitative_guidance as soften

    for line in list(source_lines):
        softened = soften(line, {})
        if softened:
            source_lines.add(softened)
    with SessionLocal() as session:
        base = StyleReferenceRepository(session).get_profile("v2_profile_lay_base")
        scene = StyleReferenceRepository(session).get_profile("v2_profile_lay_scene")
        for profile in (base, scene):
            source_lines.update(profile.profile_json["voice_signature"]["habits"])
    _assert_no_half_lines_or_empty_headings(layered, source_lines)
    # metric 块的条目行不在来源集合里:单独检查它没有孤立标题
    assert not layered.metric_anchor_block.rstrip().endswith("]")


def test_same_profile_across_scopes_renders_once() -> None:
    _book_id, profile_id = _seed_full("dedupe")
    project_id = "v2_proj_dedupe"
    with SessionLocal() as session:
        repo = StyleReferenceRepository(session)
        _bind(repo, binding_id="v2_bind_dd_p", profile_id=profile_id, scope="project", scope_ref_id=project_id, strategy="mixed", config_json={"intensity": 60})
        _bind(repo, binding_id="v2_bind_dd_s", profile_id=profile_id, scope="scene", scope_ref_id="v2_scene_dd", strategy="mixed", config_json={"intensity": 60})
        session.commit()
    with SessionLocal() as session:
        svc = InjectionService(session)
        layered = svc.fragments_for(project_id, "scene_generation", scene_id="v2_scene_dd")
        audit = dict(svc.last_runtime_audit)
        single = InjectionService(session).fragments_for(project_id, "scene_generation")
        described = InjectionService(session).describe_binding_layers(project_id, "scene_generation", scene_id="v2_scene_dd")
    assert layered.positive_block == single.positive_block
    assert layered.positive_block.count(_STYLE_FEATURES[0]) == 1
    assert audit["layer_count"] == 1
    assert described["merged"]["layer_count"] == 1
    assert [d["binding_id"] for d in described["deduplicated"]] == ["v2_bind_dd_p"]


# ---------------------------------------------------------------------------
# 截断 / 输入预算 / 审计
# ---------------------------------------------------------------------------


def test_truncate_lines_drops_orphan_bracket_heading() -> None:
    assert _truncate_lines("[声音特征]\n- 只有一条很长很长很长很长的习惯句", 12) == ""
    assert _truncate_lines("[声音特征]\n- 短\n- 第二条较长的习惯", 12) == "[声音特征]\n- 短"


def test_fit_input_budget_tracks_voice_block() -> None:
    fragments = SystemPromptFragments(
        positive_block="[正向风格特征]\n- 使用具体动词。\n- 让短句承担转折。",
        voice_block="[声音特征]\n- 连接词多用便、却。\n- 对白引导词后置。",
        metric_anchor_block="[量化锚点]\n" + "\n".join(f"- 指标{i}:" + "适度调整节奏" * 8 for i in range(6)),
        anti_plagiarism_block="## 严格禁止\n- 不得复用参考原文完整句子。",
        strategy=InjectionStrategy.A,
    )
    from novel_system.services.context_budget import estimate_tokens

    full = estimate_tokens(fragments.to_system_prompt_prefix() + "BASE") + estimate_tokens("正文")
    fitted, audit = fit_fragments_to_input_budget(
        fragments, base_system_prompt="BASE", user_prompt="正文", target_input_tokens=full - 30
    )
    assert audit["compacted"] is True
    assert fitted.voice_block == fragments.voice_block  # 先削 metric,声音特征不动
    assert "voice_block" not in audit["trimmed_blocks"]
    tiny, audit2 = fit_fragments_to_input_budget(
        fragments, base_system_prompt="BASE", user_prompt="正文", target_input_tokens=60
    )
    assert audit2["policy"].startswith(("trim_abstract_lines", "omit_style_payload"))
    assert tiny.anti_plagiarism_block in ("", fragments.anti_plagiarism_block)


def test_layered_runtime_audit_render_stats_describe_the_merged_prefix() -> None:
    """多层(project + scene,不同画像)的 render_stats 必须与真正发出的合并前缀一致
    ——实时路径与冻结契约路径都是。"""
    project_id = "v2_proj_stats"
    scene_id = "v2_scene_stats"
    with SessionLocal() as session:
        repo = StyleReferenceRepository(session)
        base_book = _seed_book(repo, "stats_base")
        base_profile = _seed_profile(
            repo, "stats_base", base_book, profile_json=_profile_json(_seed_quotes(repo, "stats_base", base_book))
        )
        scene_book = _seed_book(repo, "stats_scene")
        scene_payload = _profile_json(_seed_quotes(repo, "stats_scene", scene_book))
        scene_payload["style_features"] = [f"场景层{line}" for line in _STYLE_FEATURES]
        scene_profile = _seed_profile(repo, "stats_scene", scene_book, profile_json=scene_payload)
        _bind(
            repo,
            binding_id="v2_bind_stats_p",
            profile_id=base_profile,
            scope="project",
            scope_ref_id=project_id,
            strategy="mixed",
            config_json={"intensity": 70},
        )
        _bind(
            repo,
            binding_id="v2_bind_stats_s",
            profile_id=scene_profile,
            scope="scene",
            scope_ref_id=scene_id,
            strategy="mixed",
            config_json={"intensity": 70},
        )
        session.commit()

    def _check(svc: InjectionService, fragments: SystemPromptFragments) -> None:
        audit = dict(svc.last_runtime_audit)
        stats = audit["render_stats"]
        assert audit["layer_count"] == 2
        assert stats["total_prefix_chars"] == audit["prefix_chars"] == len(fragments.to_system_prompt_prefix())
        assert stats["positive_lines"] == len(_entry_lines(fragments.positive_block))
        assert stats["forbidden_lines"] == len(_entry_lines(fragments.forbidden_block))
        assert stats["voice_lines"] == len(_entry_lines(fragments.voice_block))
        assert stats["metric_lines"] == len(_entry_lines(fragments.metric_anchor_block))
        assert stats["few_shot_windows"] == len(_few_shot_entries(fragments.few_shot_block)) >= 1
        assert stats["few_shot_k"] == _few_shot_k(70)
        assert stats["intensity_effective_total_chars"] == svc._budget_total(intensity=70, layer_count=2)

    with SessionLocal() as session:
        svc = InjectionService(session)
        layered = svc.fragments_for(project_id, "scene_generation", scene_id=scene_id)
        _check(svc, layered)
        # 合并后的正向块含两层条目:读数不能只是最后一层
        single = InjectionService(session).fragments_for(project_id, "scene_generation")
        assert len(_entry_lines(layered.positive_block)) > len(_entry_lines(single.positive_block))
        layers = svc.resolve_binding_layers(project_id, "scene_generation", scene_id=scene_id)
        contract = build_style_runtime_contract(StyleReferenceRepository(session), layers, task_type="scene_generation")
        assert contract is not None and contract["layer_count"] == 2
        frozen_svc = InjectionService(session)
        frozen = frozen_svc.fragments_for_contract(
            contract,
            project_id=project_id,
            context=extract_style_generation_context("她在门外停步。", source_kind="generation_source"),
        )
        _check(frozen_svc, frozen)


def test_runtime_audit_carries_render_stats_and_rag_outcome() -> None:
    _book_id, profile_id = _seed_full("audit")
    with SessionLocal() as session:
        repo = StyleReferenceRepository(session)
        _bind(repo, binding_id="v2_bind_audit", profile_id=profile_id, scope="project", scope_ref_id="v2_proj_audit", strategy="mixed", config_json={"intensity": 90})
        session.commit()
    with SessionLocal() as session:
        svc = InjectionService(session)
        fragments = svc.fragments_for("v2_proj_audit", "scene_generation")
        audit = svc.last_runtime_audit
    assert audit["outcome"] == "hit"
    assert audit["rag_outcome"] is None  # 非 C 策略未走 RAG
    assert audit["render_stats"]["few_shot_windows"] >= 1
    assert audit["render_stats"]["total_prefix_chars"] == len(fragments.to_system_prompt_prefix())
    assert audit["render_stats"]["voice_lines"] >= 1
