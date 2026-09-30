"""文学质量规则引擎的逐字安全网（B04-01：拆 literary_quality 之前先钉住）。

几段固定的合成文本 → ``analyze_literary_quality``（信号、发现、统一形状与 signal id）、
``fingerprint_literary_quality``、``adversarial_rank_score``，外加按一份合成的参考书
校准之后的同一组分析。signal id 是按命中的词 / 句算的哈希，分句或词表口径一变这里就会红。

输出有意变化时用 ``LITERARY_GOLDEN_REGEN=1`` 重生成 golden，并在提交里说明原因——``git diff`` 应只显示有意的
那几行。全部是合成文本（林昭 / 雨城 / 旧信 / 案卷），不含任何真实作品。
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from novel_system.services.literary_quality import (
    RuleCalibration,
    adversarial_rank_score,
    analyze_literary_quality,
    calibrate_lexicons,
    fingerprint_literary_quality,
    unify_rule_finding,
)

GOLDEN_PATH = Path(__file__).parent / "golden" / "literary_quality" / "snapshots.json"

TEXTS: dict[str, str] = {
    "template_reuse": (
        "她低头看着钥匙，沉默了片刻。\n"
        "他低头看着录音，沉默了片刻。\n"
        "她低头看着门缝，沉默了片刻。\n"
        "月光、阴影、冷风和雾气反复压下来，月光又落在她手上。\n"
        "她忽然意识到这一切都变得不同了。她知道真相必须公开。"
    ),
    "dialogue_report": (
        "林昭把旧信放回案卷。“你知道，其实我解释过，真相是他为了保护证人才离开雨城。”老周说。"
        "“官方报告里写得很清楚，这是因为那晚的雨。”她觉得窗外的雨更冷了。"
        "因为她害怕，所以她没有回答。她不禁想起那封旧信，深吸一口气，把案卷推开。"
    ),
    "conflict_clean": (
        "雨城的码头上，两个人争吵起来。林昭愤怒地质问他为什么藏起旧信。他反驳，又拒绝交出案卷。"
        "片刻之后，她叹气，点头，理解了他的苦衷，两人握手和好，微笑着释然。"
        "仿佛命运早有安排，回声在雨里散开，一切都变得安静了。从此她知道，这一刻她明白了。"
    ),
    "english": (
        "The witness held the key. The lead had to choose the archive or the child, and every choice "
        "would cost her something. She noticed the rain. Somehow meaningful, the moon hung over the "
        "station, and the moon light fell on the moon-white floor. She opened the door before the bell stopped."
    ),
    "mechanical_loop": (
        "他把目光重新移回那扇紧闭的门边。" * 4
        + "他转身，转身，又转身，走到窗前。门开了，门又关上，门外有光，光落在门上。"
    ),
    "short": "她推开门。",
    "empty": "",
}

# 一份合成的参考书校准：几个词表词在参考作者那里很常见，两条规则是他的常态 / 常见，画像标了刻意复沓。
CALIBRATION = RuleCalibration(
    source="reference",
    needle_rates={"不禁": 5.0, "仿佛": 12.0, "她觉得": 3.0, "命运": 0.2, "月光": 9.0},
    dimension_stats={
        "syntax_monotony": {"fired": 40, "n": 48, "share": 0.833, "lower_bound": 0.75, "level": "habit"},
        "choice_pressure": {"fired": 20, "n": 48, "share": 0.417, "lower_bound": 0.33, "level": "common"},
    },
    deliberate_repetition=True,
    windows=48,
    endings=12,
    endings_source="units",
    chars=100_000,
)
CALIBRATED = ("template_reuse", "dialogue_report", "conflict_clean")


def _analysis(text: str, *, calibration: RuleCalibration | None = None) -> dict:
    signals, findings = analyze_literary_quality(text, calibration=calibration)
    return {
        "signals": signals,
        "findings": findings,
        "unified": [unify_rule_finding(finding) for finding in findings],
    }


def _snapshot() -> dict:
    snapshot: dict = {"texts": {}, "calibrated": {}}
    for name, text in TEXTS.items():
        snapshot["texts"][name] = {
            **_analysis(text),
            "fingerprint": fingerprint_literary_quality(text),
            "adversarial_rank_score": adversarial_rank_score(text),
        }
    for name in CALIBRATED:
        text = TEXTS[name]
        _lexicons, waived = calibrate_lexicons(CALIBRATION, text)
        snapshot["calibrated"][name] = {**_analysis(text, calibration=CALIBRATION), "waived": waived}
    snapshot["calibration"] = CALIBRATION.as_dict()
    snapshot["calibration_signature"] = CALIBRATION.signature
    return snapshot


def test_rule_engine_output_matches_the_golden_snapshot() -> None:
    # 过一遍 JSON：与 golden 的比较不受 tuple / list 之别影响，浮点按 repr 逐位往返
    actual = json.loads(json.dumps(_snapshot(), ensure_ascii=False))
    if os.environ.get("LITERARY_GOLDEN_REGEN") == "1":
        GOLDEN_PATH.parent.mkdir(parents=True, exist_ok=True)
        GOLDEN_PATH.write_text(json.dumps(actual, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    expected = json.loads(GOLDEN_PATH.read_text(encoding="utf-8"))
    assert actual == expected
