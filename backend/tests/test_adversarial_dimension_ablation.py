"""Blueprint §6.5 / §17 — empirical ABLATION of the adversarial dimension set.

§17 终极问题:「每个模块都必须知道自己…什么时候该被砍掉。完美的方案不是模块最全的,
而是每个模块都可证伪的。」The prior metrics_audit classified each dimension by *intent*
(adversarial vs conformity). This goes one step further and measures, on a real Chinese
corpus, whether each adversarial dimension actually *discriminates* AI-slop from clean
prose — i.e. whether it earns its keep on THIS project's actual language.

A falsifiable hypothesis was tested and REFUTED here, which is the point of §17:
  Hypothesis: "several adversarial dims key only on English terms (moon / turned /
               because …) and are therefore dead weight on a Chinese novel system."
  Result:     REFUTED. The term lists are bilingual; every adversarial dimension fires
               on Chinese when its trigger condition is met. The dims that looked dead
               in a first pass were merely condition/threshold-gated (repetitive_action
               needs the same beat ≥4×; false_poetic_closure needs a poetic close with
               NO concrete action; self_repetition needs cross-scene context).

The one genuine nuance the ablation surfaces: the structural cost/choice dims
(painless_scene / no_choice_scene / choice_pressure, §4) fire ~uniformly on BOTH slop
and clean atmosphere prose, so they contribute ≈0 discrimination on the *prose-quality*
axis (no_choice_scene even slightly inverts). They measure scene structure, not AI-flavor.
For same-scene Best-of-N ranking that is a near-constant offset (defensible), but it means
the prose ranker's discriminating power comes from the 12 prose dims, not the 4 structural
ones. Documented, not "fixed" — they earn their keep elsewhere (§4 QC).

This test locks those CORRECT facts so a future refactor that (a) strips Chinese coverage
from a term list, or (b) flips a dimension's polarity, breaks loudly.

Run standalone:  pytest tests/test_adversarial_dimension_ablation.py -s
"""
from __future__ import annotations


from novel_system.services.literary_quality import (
    ADVERSARIAL_DIMS,
    adversarial_rank_score,
    analyze_literary_quality,
)

# Structural (§4) dims — measure scene structure, not prose AI-flavor.
STRUCTURAL_DIMS = ("painless_scene", "no_choice_scene", "choice_pressure", "conflict_too_clean")

# Chinese AI-slop (common real failure modes) vs clean show-don't-tell prose.
SLOP = [
    "他觉得心口发闷,悲伤涌了上来。他看到她转身,心里一片冰凉。"
    "他意识到自己错了,却已经太迟。他知道,一切都无法挽回。"
    "他叹了口气,仿佛命运早已注定。一切都变得不同了。",
    "她感到一阵恐惧,浑身发冷。她看到门开了,她意识到危险。"
    "她知道必须逃走,却动弹不得。她转身,慌乱地想跑。"
    "她叹了口气,仿佛命运在催促。一切都变得模糊。",
    "他低头看着地面,沉默了片刻。他低头看着手,沉默了片刻。"
    "他低头看着远方,沉默了片刻。他点头,又点头,再点头。",
    "“你要知道,因为我是为了你好,”他解释道。“事实上,真相是这样的,”"
    "她回答。“让我解释一下,”他说,“正如你所知,这一切都有原因。”",
    "他握紧拳头,愤怒地质问。她却笑了,温和地原谅了他,点头表示理解。"
    "两人很快和好,所有的冲突都烟消云散,彼此释然地拥抱在一起。",
    "月光洒下,影子拉长。月光里有雾,雾中有影,影子在月光下颤抖。"
    "黑暗中,月光与阴影交织,雾气弥漫,一切都笼罩在朦胧的光里。",
]
CLEAN = [
    "风从窗缝钻进来,吹得烛火歪了歪。林远把那封信折好,又展开,"
    "指节压过纸面的折痕。“你早就料到了。”苏晚没有回头。"
    "他没有答话,只把信凑近火苗,看纸角卷起焦黑。",
    "门轴吱呀响了一声。苏晚的手停在半空,茶杯沿还贴着下唇。"
    "走廊尽头的脚步声不紧不慢,一下,又一下。她把杯子搁回桌面,"
    "指尖却把桌布攥出了褶。",
    "门没锁。林远用肩膀抵开它,血痂在袖口结成硬壳。"
    "灶台是凉的,锅底积了一层灰。他蹲下去,探了探那点早就熄了的余烬。",
    "“账我已经平了。”老周把一沓纸推过桌面,纸角磨得发毛。"
    "陈九没去接,只盯着那道在桌沿压出的折痕,良久,才把茶碗往旁边挪了挪。",
    "雨停了。屋檐还在滴水,一滴,砸在青石板的同一个凹处。"
    "阿满把湿透的鞋脱在门口,光脚踩过堂屋,留下一串很快就淡掉的脚印。",
    "她把最后一格窗推开。麦垛在场院里堆成几座小山,新割的秸秆气味漫进来。"
    "远处有人在打谷,连枷起落,声音钝钝的,隔着热气传过来。",
]


def _fire_rate(dim: str, corpus: list[str]) -> float:
    hits = sum(1 for t in corpus if analyze_literary_quality(t)[0].get(dim, {}).get("risk"))
    return hits / len(corpus)


def _discrimination(dim: str) -> float:
    return _fire_rate(dim, SLOP) - _fire_rate(dim, CLEAN)


class TestAdversarialAblation:

    def test_ranker_discriminates_on_chinese(self, capsys) -> None:
        slop_mean = sum(adversarial_rank_score(t) for t in SLOP) / len(SLOP)
        clean_mean = sum(adversarial_rank_score(t) for t in CLEAN) / len(CLEAN)
        rows = sorted(((d, _discrimination(d)) for d in ADVERSARIAL_DIMS),
                      key=lambda r: r[1], reverse=True)
        with capsys.disabled():
            print("\n  §6 adversarial dimension ablation (Chinese corpus)")
            print(f"  ranker mean: slop={slop_mean:.3f}  clean={clean_mean:.3f}  "
                  f"margin={clean_mean - slop_mean:+.3f}")
            for d, disc in rows:
                kind = "structural" if d in STRUCTURAL_DIMS else "prose"
                print(f"    {d:24} disc={disc:+.2f}  [{kind}]")
        assert clean_mean > slop_mean + 0.05, "ranker fails to separate slop from clean on Chinese"

    def test_broad_chinese_coverage(self) -> None:
        """≥8 adversarial dims must positively discriminate — guards against a refactor
        that strips Chinese terms and silently turns dims into English-only dead weight."""
        discriminating = [d for d in ADVERSARIAL_DIMS if _discrimination(d) > 0.0]
        assert len(discriminating) >= 8, (
            f"only {len(discriminating)} dims discriminate on Chinese: {discriminating}"
        )

    def test_condition_gated_dims_are_not_dead(self) -> None:
        """The dims that don't fire on generic slop are gated, not English-only dead.
        Prove they fire on Chinese when their trigger condition is actually met."""
        ra = "他转身,又转身,再转身,最后还是转身离开。"  # same beat ×4 ≥ threshold
        fpc = "他久久没有动。一切都变得不同了,仿佛命运早已写好结局。"  # poetic close, no action
        assert analyze_literary_quality(ra)[0]["repetitive_action"]["risk"] is True
        assert analyze_literary_quality(fpc)[0]["false_poetic_closure"]["risk"] is True

    def test_structural_dims_do_not_discriminate_prose(self) -> None:
        """§4 structural dims fire ~uniformly on slop & clean → ≈0 prose discrimination.
        This is the measured nuance: the prose ranker's power is in the prose dims."""
        for dim in ("painless_scene", "no_choice_scene", "choice_pressure"):
            assert _discrimination(dim) <= 0.2, (
                f"{dim} unexpectedly discriminates ({_discrimination(dim):+.2f}) — "
                f"re-examine whether it belongs in the prose ranker"
            )

    def test_no_dimension_has_inverted_polarity(self) -> None:
        """No prose dimension may fire MORE on clean than slop by a wide margin —
        that would mean the metric rewards exactly what it should penalize (a polarity bug)."""
        for dim in ADVERSARIAL_DIMS:
            if dim in STRUCTURAL_DIMS:
                continue
            assert _discrimination(dim) >= -0.2, (
                f"{dim} has inverted polarity (disc={_discrimination(dim):+.2f})"
            )
