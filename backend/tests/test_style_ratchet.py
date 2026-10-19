"""风格棘轮（``style_reference/ratchet.py`` + 运维工具 ``style_ratchet``）：配置的写法、判定规则、距离、
留出段在渲染时整窗跳过、端到端的一次比较（要点不带原文措辞、稿子缓存与复用、记账、报告与盲读页）、干跑不调用不写。

合成书的每一段都是随机常用字拼的（彼此没有连续 12 字相同的片段）——棘轮按抄袭门同一把 12 字尺子判泄漏，
模板句拼成的合成书会让每一段都「泄漏」。
"""

from __future__ import annotations

import json
import random
import re
from types import SimpleNamespace

import pytest

from novel_system.db.models import LlmCall, StyleReferenceJob
from novel_system.services.style_reference import policy as policy_module
from novel_system.services.style_reference import ratchet as R
from novel_system.services.style_reference.fidelity import build_reference_distribution
from novel_system.services.style_reference.inject.render import render_style
from novel_system.services.style_reference.inject.request import ROLE_DRAFT, PLACEMENT_USER_TAIL, StyleRenderRequest
from novel_system.services.reference_copy_gate import shared_runs
from tests.accounted_llm_fakes import AccountedGenerateMixin
from tests.style_reference_factories import RIGHTS_STATS, make_binding, make_book, make_profile, v3_profile_json

_POOL = "天地人山水风云雨雪花草木石门窗灯火车船桥路街巷城村田河海江湖鸟鱼马牛羊狗猫衣食住行春夏秋冬早晚日月星光影声色香味冷热明暗高低远近大小长短新旧多少快慢深浅轻重"
_GLUE = "的了是在有我他她你们这那就也还都又把被说看想走来去上下里外中前后"


def _prose(rng: random.Random, chars: int) -> str:
    out: list[str] = []
    while sum(len(part) for part in out) < chars:
        clause = "".join(rng.choice(_POOL if rng.random() < 0.7 else _GLUE) for _ in range(rng.randint(5, 12)))
        out.append(clause + rng.choice("，，，。"))
    text = "".join(out)
    return text[:-1] + "。" if text.endswith("，") else text


def _book_rows(seed: str, *, chapters: int = 14, per_chapter: int = 48) -> list[dict]:
    rng = random.Random(seed)
    rows: list[dict] = []
    for chapter in range(1, chapters + 1):
        rows.append({"paragraph_index": len(rows), "text": f"第{chapter}章 雨夜", "paragraph_type": "transition"})
        for _ in range(per_chapter):
            if rng.random() < 0.2:
                text, ptype = f"“{_prose(rng, rng.randint(8, 20))}”他说。", "dialogue"
            else:
                text, ptype = _prose(rng, rng.randint(30, 70)), "narration"
            rows.append({"paragraph_index": len(rows), "text": text, "paragraph_type": ptype})
    return rows


def _seed(session, key: str, *, sample_windows: int = 6) -> tuple[str, str]:
    book_id = make_book(session, f"rt_book_{key}", title="合成书", paragraphs=_book_rows(key), stats=RIGHTS_STATS)
    profile_id = make_profile(session, book_id, profile_id=f"rt_profile_{key}", profile_json=v3_profile_json())
    make_binding(session, profile_id, binding_id=f"rt_bind_{key}", config_json={"sample_windows": sample_windows})
    session.commit()
    return book_id, profile_id


@pytest.fixture
def local_routes(monkeypatch):
    monkeypatch.setattr(policy_module, "node_route_is_local", lambda node_id, **_k: True)
    monkeypatch.setattr(policy_module, "runtime_llm_is_local", lambda settings=None: True)


def _passage_line(user: str) -> str:
    """要点调用的 user 消息里原文的第一行够长的正文（伪造「带了原文措辞」的要点用）。"""
    body = user.split("## Passage", 1)[1].split("[UNTRUSTED_REFERENCE_DATA", 1)[1]
    for line in body.splitlines()[1:]:
        if len(re.findall(r"[一-鿿]", line)) >= 20:
            return line.strip()
    raise AssertionError("no passage line in the plot prompt")


class _FakeRatchetLLM(AccountedGenerateMixin):
    """要点调用：给 6 拍干净的要点（``leak_once`` 时头一次照抄原文一句、``thin_for`` 里的原文只给两拍）；
    起草调用：``model == "close"`` 按合成书同一种写法出一段，其余模型出满篇对白和问号的一段。"""

    def __init__(self, *, leak_once: bool = False, thin_for: tuple[str, ...] = ()) -> None:
        self.leak_once = leak_once
        self.thin_for = thin_for
        self.requests: list = []
        # 起草调用之前调一下（测试在这里插一个取消）
        self.on_draft = None

    def generate(self, request):  # noqa: ANN001
        self.requests.append(request)
        system = request.messages[0]["content"]
        user = request.messages[-1]["content"]
        if "plot outline" in system:
            beats = [f"第{index}拍：甲和乙在桥边说了几句" for index in range(6)]
            if any(marker in user for marker in self.thin_for):
                beats = beats[:2]
            elif self.leak_once:
                self.leak_once = False
                beats = [_passage_line(user)[:16], *beats]
            structured = {
                "narrative_person": "第三人称",
                "pov": "甲",
                "setting": "桥边，傍晚",
                "characters": [{"name": "甲", "role": "过路人"}, {"name": "乙", "role": "摆渡的"}],
                "beats": beats,
                "ending": "两人各自走开",
            }
        else:
            if self.on_draft is not None:
                self.on_draft()
            rng = random.Random(f"{request.model}:{len(self.requests)}")
            if request.model == "close":
                text = "\n".join(_prose(rng, rng.randint(30, 70)) for _ in range(40))
            else:
                text = "\n".join("“你去吗？你真的去吗？”“去！”他喊。" for _ in range(60))
            structured = {"scene_text": text}
        return SimpleNamespace(
            structured_output=structured,
            text=json.dumps(structured, ensure_ascii=False),
            usage={},
            finish_reason="stop",
            provider="fake",
            model=request.model,
            response_format="json_object",
            request_id=None,
            raw_response={},
        )


def _plan(session, profile_id: str, tmp_path, *, a: str = "model=far", b: str = "model=close", passages: int = 3):
    return R.plan_run(
        session,
        profile_id=profile_id,
        specs=(R.parse_arm("A", a), R.parse_arm("B", b)),
        passages=passages,
        seed="t",
        out_dir=tmp_path,
    )


# ---------------------------------------------------------------------------
# 纯函数
# ---------------------------------------------------------------------------


def test_arm_specs_parse_the_four_knobs_and_refuse_typos_and_out_of_range_values() -> None:
    spec = R.parse_arm("B", "model=m-1, windows=8, placement=system, prompts=/tmp/p.yaml, rep=2")
    assert (spec.model, spec.sample_windows, spec.placement, spec.prompts, spec.rep) == ("m-1", 8, "system", "/tmp/p.yaml", 2)
    assert R.parse_arm("A", "") == R.ArmSpec(label="A")
    assert R.parse_arm("A", "placement=tail").placement == "user_tail"
    for bad in ("windws=8", "windows=17", "windows=x", "placement=middle", "rep=0", "model="):
        with pytest.raises(R.RatchetError):
            R.parse_arm("B", bad)


@pytest.mark.parametrize(
    ("pairs", "winner"),
    [
        ([(1.0, 0.8), (0.9, 0.7), (1.2, 1.1)], "B"),
        ([(0.8, 1.0), (0.7, 0.9), (1.1, 1.2)], "A"),
        # B 平均更近，但有一段更远：分不出
        ([(1.0, 0.5), (0.9, 0.5), (1.0, 1.1)], None),
        # 每段都更近，但近得不到 ε：分不出
        ([(1.0, 0.99), (0.9, 0.89), (1.0, 0.98)], None),
    ],
)
def test_b_wins_only_when_it_is_nearer_on_every_passage_by_more_than_epsilon(pairs, winner) -> None:
    verdict = R.decide(pairs, epsilon=0.05)
    assert verdict.winner == winner
    assert verdict.passages == 3 and verdict.wins_a + verdict.wins_b == 3
    assert ("更像" in verdict.label()) == (winner is not None)


def test_feature_distance_is_zero_for_the_same_text_and_symmetric() -> None:
    rng = random.Random(3)
    windows = [R.text_features(_prose(rng, 1500)) for _ in range(12)]
    dist = build_reference_distribution(windows)
    one, two = windows[0], windows[1]
    assert R.feature_distance(one, one, dist) == 0.0
    assert R.feature_distance(one, two, dist) == pytest.approx(R.feature_distance(two, one, dist))
    far = R.text_features("“你去吗？你真的去吗？”“去！”他喊。" * 60)
    assert R.feature_distance(one, far, dist) > R.feature_distance(one, two, dist)


def test_leak_checks_use_the_twelve_character_ruler() -> None:
    passage = _prose(random.Random(5), 400)
    # 标点规范化掉之后再数：12 个字连着相同才算
    hanzi = [index for index, char in enumerate(passage) if char not in "，。"]
    twelve = passage[hanzi[100] : hanzi[111] + 1]
    eleven = passage[hanzi[100] : hanzi[110] + 1]
    assert shared_runs("前言" + twelve + "后记", [passage])
    assert not shared_runs("前言" + eleven + "后记", [passage])
    assert R.leaked_share(passage, "无关" + passage[:200]) == pytest.approx(0.5, abs=0.02)
    assert R.leaked_share(passage, "") == 0.0


# ---------------------------------------------------------------------------
# 留出段在渲染时整窗跳过
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("local_routes")
def test_excluded_windows_never_reach_the_rendered_samples(session) -> None:
    _book, profile_id = _seed(session, "excl", sample_windows=4)
    subject = R.resolve_subject(session, profile_id)
    plain = render_style(
        session, subject.policy, StyleRenderRequest(role=ROLE_DRAFT, placement=PLACEMENT_USER_TAIL, node_ids=("style_draft",)),
        persist_selection=False, use_cache=False,
    )
    used = {int(ref["window_no"]) for ref in plain.window_refs}
    assert len(used) == 4
    excluded = render_style(
        session,
        subject.policy,
        StyleRenderRequest(role=ROLE_DRAFT, placement=PLACEMENT_USER_TAIL, node_ids=("style_draft",), exclude_windows=tuple(used)),
        persist_selection=False,
        use_cache=False,
    )
    assert len(excluded.window_refs) == 4
    assert not used & {int(ref["window_no"]) for ref in excluded.window_refs}


# ---------------------------------------------------------------------------
# 端到端
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("local_routes")
def test_a_comparison_drafts_both_arms_from_clean_plot_points_and_writes_the_report_and_blind_sheet(session, tmp_path) -> None:
    _book, profile_id = _seed(session, "e2e")
    plan = _plan(session, profile_id, tmp_path)
    assert len(plan.passages) == 3 and not plan.plots and plan.arms[0].key != plan.arms[1].key
    fake = _FakeRatchetLLM(leak_once=True)

    result = R.execute_run(session, plan, llm_client=fake)

    assert result.verdict.winner == "B", result.rows
    assert not result.skipped
    plot_calls = [r for r in fake.requests if "plot outline" in r.messages[0]["content"]]
    draft_calls = [r for r in fake.requests if "plot outline" not in r.messages[0]["content"]]
    assert len(plot_calls) == 4, "头一次照抄了原文一句 → 那一段重提一次"
    assert len(draft_calls) == 6 and {r.model for r in draft_calls} == {"far", "close"}
    for request in draft_calls:
        system, user = request.messages[0]["content"], request.messages[1]["content"]
        assert system.startswith("[STYLE_REFERENCE]") and "[风格样例]" in user
        # 留出段整窗跳过：模型看到的东西里没有任何一段留出原文
        for passage in plan.passages:
            assert R.leaked_share(passage.text, system + user) == 0.0
    for passage in plan.passages:
        stored = json.loads((plan.suite_dir / "passages" / f"w{passage.window_no}.json").read_text(encoding="utf-8"))
        assert not shared_runs(R.plot_text(stored["plot"]), [passage.text])
    rows = session.query(LlmCall).filter(LlmCall.scope_type == R.SCOPE_TYPE).all()
    assert len(rows) == 10 and {row.scope_id for row in rows} == {plan.suite_id}
    assert result.usage["calls"] == 10

    run_dir = result.run_dir
    report = (run_dir / "report.md").read_text(encoding="utf-8")
    assert "## 结论：B 更像" in report and "模型 far" in report and "模型 close" in report
    sheet = (run_dir / "reading.md").read_text(encoding="utf-8")
    key = json.loads((run_dir / "key.json").read_text(encoding="utf-8"))
    assert len(key) == 3
    for index, entry in key.items():
        assert sorted(entry[label] for label in R.READING_LABELS) == ["A", "B", "original"]
        assert f"## 第 {index} 段" in sheet
    for passage in plan.passages:
        assert passage.text.strip() in sheet
    stored = json.loads((run_dir / "result.json").read_text(encoding="utf-8"))
    assert stored["verdict"]["winner"] == "B" and len(stored["rows"]) == 3


@pytest.mark.usefixtures("local_routes")
def test_a_new_opponent_reuses_the_suite_and_the_cached_drafts(session, tmp_path) -> None:
    _book, profile_id = _seed(session, "reuse")
    first = _FakeRatchetLLM()
    R.execute_run(session, _plan(session, profile_id, tmp_path), llm_client=first)
    assert len(first.requests) == 9

    plan = _plan(session, profile_id, tmp_path, b="model=other")
    assert len(plan.plots) == 3 and not plan.drafts_needed(plan.arms[0]) and len(plan.drafts_needed(plan.arms[1])) == 3
    second = _FakeRatchetLLM()
    result = R.execute_run(session, plan, llm_client=second)
    assert len(second.requests) == 3 and {r.model for r in second.requests} == {"other"}
    assert result.verdict.winner is None, "两边都是对白腔：分不出"


@pytest.mark.usefixtures("local_routes")
def test_an_a_a_run_measures_the_noise_and_later_comparisons_never_judge_below_it(session, tmp_path) -> None:
    _book, profile_id = _seed(session, "noise")
    aa = R.execute_run(session, _plan(session, profile_id, tmp_path, a="model=close", b="model=close,rep=2"), llm_client=_FakeRatchetLLM())
    assert aa.same_config and aa.noise > 0 and aa.headline().startswith("A/A")
    noise = json.loads((tmp_path / R.NOISE_FILE).read_text(encoding="utf-8"))
    assert len(noise) == 1 and len(next(iter(noise.values()))["diffs"]) == 3

    plan = _plan(session, profile_id, tmp_path, a="model=far", b="model=close")
    result = R.execute_run(session, plan, llm_client=_FakeRatchetLLM(), epsilon=0.0)
    assert result.verdict.epsilon == pytest.approx(aa.noise, abs=1e-3)
    assert result.epsilon_source == "两边配置的 A/A 实测噪声"
    # 量过噪声以后给一个更大的 ε：用给定值
    assert R.effective_epsilon(noise, plan.arms, 9.0) == (9.0, "给定值（大于 A/A 实测噪声）")
    # 一条噪声记录都没有：给定值，并提示先跑 A/A
    assert R.effective_epsilon({}, plan.arms, 0.2)[0] == 0.2


@pytest.mark.usefixtures("local_routes")
def test_a_passage_without_clean_plot_points_is_dropped_and_the_rest_are_compared(session, tmp_path) -> None:
    _book, profile_id = _seed(session, "thin")
    plan = _plan(session, profile_id, tmp_path)
    marker = plan.passages[0].text[:12]
    result = R.execute_run(session, plan, llm_client=_FakeRatchetLLM(thin_for=(marker,)))
    assert result.skipped == [plan.passages[0].window_no]
    assert len(result.rows) == 2 and result.verdict.winner == "B"


@pytest.mark.usefixtures("local_routes")
def test_the_tool_dry_run_prints_the_plan_and_neither_calls_a_model_nor_writes(session, tmp_path, capsys) -> None:
    from novel_system.tools import style_ratchet as tool

    _book, profile_id = _seed(session, "dry")
    out_dir = tmp_path / "ratchet"
    assert tool.main(["--profile", profile_id, "--b", "windows=4", "--out-dir", str(out_dir)]) == 0
    out = capsys.readouterr().out
    assert "干跑" in out and "配置 B" in out and "样例 4 窗" in out
    assert not out_dir.exists()
    assert session.query(LlmCall).count() == 0
    assert tool.main(["--profile", profile_id, "--b", "windws=4", "--out-dir", str(out_dir)]) == 2


# ---------------------------------------------------------------------------
# 界面：配置对比作业（ratchet_job）与它的接口
# ---------------------------------------------------------------------------

API = "/api/v2/style-reference"


@pytest.fixture
def ratchet_ui(monkeypatch, tmp_path):
    """配置对比作业：套件目录落在 tmp_path，作业与路由拿同一个假模型，派发不自动跑（测试自己 run_job_inline）。"""
    from novel_system.services.style_reference import ratchet_job as RJ

    fake = _FakeRatchetLLM()
    monkeypatch.setattr(R, "default_out_dir", lambda: tmp_path)
    monkeypatch.setattr(RJ, "resolve_client", lambda: (fake, True))
    monkeypatch.setattr("novel_system.api.routes.style_reference.ratchet.llm_client_and_enabled", lambda: (fake, True))
    monkeypatch.setattr("novel_system.api.routes.style_reference._common.dispatch", lambda job_id: None)
    return fake


def _run_ui_job(client, profile_id: str, body: dict) -> str:
    from novel_system.services.style_reference.jobs import run_job_inline

    started = client.post(f"{API}/profiles/{profile_id}/ratchet", json=body)
    assert started.status_code == 200, started.text
    job_id = started.json()["data"]["job_id"]
    run_job_inline(job_id)
    return job_id


@pytest.mark.usefixtures("style_workers", "local_routes")
def test_the_page_runs_a_comparison_and_keeps_the_answer_and_the_numbers_until_the_author_reveals(
    client, session, ratchet_ui
) -> None:
    _book, profile_id = _seed(session, "ui")
    overview = client.get(f"{API}/profiles/{profile_id}/ratchet").json()["data"]
    assert overview["available"] is True and overview["current"]["windows"] == 6 and overview["runs"] == []

    job_id = _run_ui_job(client, profile_id, {"a": {"model": "far"}, "b": {"model": "close"}, "passages": 3})
    assert len(ratchet_ui.requests) == 9

    detail = client.get(f"{API}/ratchet-runs/{job_id}").json()["data"]
    assert detail["job"]["status"] == "succeeded" and detail["job"]["steps"] == {"done": 9, "total": 9}
    assert detail["job"]["result"] is None, "活动条目不带结论也不带正文"
    assert detail["revealed"] is False and len(detail["reading"]) == 3
    for item in detail["reading"]:
        assert "key" not in item and [t["label"] for t in item["texts"]] == list(R.READING_LABELS)
        assert len(item["beats"]) == 6
    assert "verdict" not in detail["summary"] and "rows" not in detail["summary"], "逐段字数就能对出哪份是哪份"
    assert detail["entry"]["headline"] is None and detail["entry"]["winner"] is None
    listed = client.get(f"{API}/profiles/{profile_id}/ratchet").json()["data"]["runs"]
    assert [run["job_id"] for run in listed] == [job_id] and listed[0]["headline"] is None

    # 原文一定在三份里：找到它在哪一份，再给每段排名（最像 = 原文，最不像 = 对白腔的 A）
    session.expire_all()
    stored = session.get(StyleReferenceJob, job_id).result_json
    votes = {}
    for item in stored["reading"]:
        by_source = {source: label for label, source in item["key"].items()}
        votes[str(item["index"])] = {"best": by_source["original"], "worst": by_source["A"]}
    same = client.put(f"{API}/ratchet-runs/{job_id}/votes", json={"votes": {"1": {"best": "甲", "worst": "甲"}}})
    assert same.status_code == 422 and same.json()["error"]["code"] == "STYLE_RATCHET_VOTES_INVALID"
    unknown = client.put(f"{API}/ratchet-runs/{job_id}/votes", json={"votes": {"9": {"best": "甲"}}})
    assert unknown.status_code == 422
    saved = client.put(f"{API}/ratchet-runs/{job_id}/votes", json={"votes": votes})
    assert saved.status_code == 200 and saved.json()["data"]["revealed"] is False
    assert client.get(f"{API}/ratchet-runs/{job_id}").json()["data"]["entry"]["reading"] == {"done": 3, "total": 3}

    revealed = client.put(f"{API}/ratchet-runs/{job_id}/votes", json={"votes": votes, "reveal": True})
    assert revealed.status_code == 200 and revealed.json()["data"]["revealed"] is True
    detail = client.get(f"{API}/ratchet-runs/{job_id}").json()["data"]
    assert detail["summary"]["verdict"]["winner"] == "B" and len(detail["summary"]["rows"]) == 3
    assert all(sorted(item["key"].values()) == ["A", "B", "original"] for item in detail["reading"])
    assert detail["entry"]["winner"] == "B" and detail["entry"]["headline"].startswith("B 更像")
    assert detail["entry"]["reading"] == {"done": 3, "total": 3, "prefer": {"A": 0, "B": 3, "tie": 0}, "original_best": 3}
    changed = dict(votes, **{"1": {"best": votes["1"]["worst"], "worst": votes["1"]["best"]}})
    late = client.put(f"{API}/ratchet-runs/{job_id}/votes", json={"votes": changed})
    assert late.status_code == 409, "揭晓之后的选择不是盲的"
    assert client.put(f"{API}/ratchet-runs/{job_id}/votes", json={"votes": {}, "reveal": True}).status_code == 200


@pytest.mark.usefixtures("style_workers", "local_routes")
def test_the_preview_counts_the_calls_from_the_cache_and_creates_no_job(client, session, ratchet_ui) -> None:
    _book, profile_id = _seed(session, "uiprev")
    body = {"a": {"model": "far"}, "b": {"model": "close"}, "passages": 3}
    fresh = client.post(f"{API}/profiles/{profile_id}/ratchet/preview", json=body).json()["data"]
    assert fresh["calls"] == 9 and fresh["plot_calls"] == 3 and fresh["draft_calls"] == {"A": 3, "B": 3}
    assert fresh["same_config"] is False and fresh["input_chars_estimate"] > 0
    assert session.query(StyleReferenceJob).count() == 0 and session.query(LlmCall).count() == 0

    _run_ui_job(client, profile_id, body)
    # 换一个对手：要点与 A 的稿子都在缓存里，只写 B 的 3 段
    again = client.post(
        f"{API}/profiles/{profile_id}/ratchet/preview", json={**body, "b": {"model": "close", "windows": 2}}
    ).json()["data"]
    assert again["calls"] == 3 and again["draft_calls"] == {"A": 0, "B": 3}
    assert all(passage["plot_cached"] for passage in again["passages"])
    aa = client.post(
        f"{API}/profiles/{profile_id}/ratchet/preview", json={"a": {"model": "close"}, "b": {"model": "close", "rep": 2}, "passages": 3}
    ).json()["data"]
    assert aa["same_config"] is True
    bad = client.post(f"{API}/profiles/{profile_id}/ratchet/preview", json={"b": {"windows": 99}})
    assert bad.status_code == 422


@pytest.mark.usefixtures("style_workers", "local_routes")
def test_one_comparison_per_book_at_a_time_and_cancel_needs_an_active_one(client, session, ratchet_ui) -> None:
    _book, profile_id = _seed(session, "uionce")
    started = client.post(f"{API}/profiles/{profile_id}/ratchet", json={"b": {"windows": 2}})
    assert started.status_code == 200, started.text
    again = client.post(f"{API}/profiles/{profile_id}/ratchet", json={})
    assert again.status_code == 409 and again.json()["error"]["code"] == "STYLE_RATCHET_ALREADY_ACTIVE"
    active = client.get(f"{API}/profiles/{profile_id}/ratchet").json()["data"]["active"]
    assert active["job_id"] == started.json()["data"]["job_id"] and active["kind_label"] == "配置对比"
    cancelled = client.post(f"{API}/profiles/{profile_id}/ratchet/cancel", json={})
    assert cancelled.status_code == 200 and cancelled.json()["data"]["finished"] is True
    none_left = client.post(f"{API}/profiles/{profile_id}/ratchet/cancel", json={})
    assert none_left.status_code == 409 and none_left.json()["error"]["code"] == "STYLE_RATCHET_NOT_ACTIVE"
    overview = client.get(f"{API}/profiles/{profile_id}/ratchet").json()["data"]
    assert overview["active"] is None and overview["runs"][0]["status"] == "cancelled"
    assert client.get(f"{API}/ratchet-runs/nope").status_code == 404


@pytest.mark.usefixtures("style_workers", "local_routes")
def test_a_cancel_during_the_run_stops_before_the_next_model_call(client, session, ratchet_ui) -> None:
    from novel_system.services.style_reference.jobs import StyleJobService

    _book, profile_id = _seed(session, "uicancel")
    started = client.post(f"{API}/profiles/{profile_id}/ratchet", json={"a": {"model": "far"}, "b": {"model": "close"}, "passages": 3})
    job_id = started.json()["data"]["job_id"]

    def cancel_once() -> None:
        with _session_factory() as other:
            StyleJobService(other).request_cancel(job_id)
            other.commit()
        ratchet_ui.on_draft = None

    from novel_system.services.style_reference.jobs import _session_factory, run_job_inline

    ratchet_ui.on_draft = cancel_once
    run_job_inline(job_id)
    session.expire_all()
    job = session.get(StyleReferenceJob, job_id)
    assert job.state == "cancelled"
    drafts = [r for r in ratchet_ui.requests if "plot outline" not in r.messages[0]["content"]]
    assert len(drafts) == 1, "取消之后不再起草下一段"


@pytest.mark.usefixtures("style_workers", "local_routes")
def test_the_page_says_why_it_cannot_compare_a_profile_without_a_book(client, session) -> None:
    overview = client.get(f"{API}/profiles/nope/ratchet").json()["data"]
    assert overview["available"] is False and overview["reason"]
    refused = client.post(f"{API}/profiles/nope/ratchet/preview", json={})
    assert refused.status_code == 409 and refused.json()["error"]["code"] == "STYLE_RATCHET_REFUSED"


def test_comparison_jobs_are_never_pruned(session) -> None:
    from datetime import UTC, datetime, timedelta

    from novel_system.services.style_reference.cleanup import prune_style_jobs
    from novel_system.services.style_reference.jobs import JOB_KIND_RATCHET

    old = (datetime.now(UTC) - timedelta(days=400)).isoformat()
    _book, profile_id = _seed(session, "uiprune")
    for index in range(3):
        session.add(
            StyleReferenceJob(
                job_id=f"rt_old_{index}",
                kind=JOB_KIND_RATCHET,
                book_id=_book,
                profile_id=profile_id,
                state="succeeded",
                created_at=old,
                finished_at=old,
                params_json={},
                progress_json={},
                cursor_json={},
            )
        )
    session.commit()
    prune_style_jobs(session)
    session.commit()
    assert session.query(StyleReferenceJob).filter(StyleReferenceJob.kind == JOB_KIND_RATCHET).count() == 3
