"""构思视图「先看 3 个方向」节点（snowflake_step_candidates）与「按此生成本步」接缝
（generate 的 adopted_direction / require_llm）。阶段 U（2026-09-17）起方向是教练日志里的回合，
fail-closed；回合 / 采纳 / 教练记忆的契约在 test_snowflake_coach_directions.py。"""

from __future__ import annotations

import json

from novel_system.services.llm_client import LLMResponse
from novel_system.services.llm_node_registry import get_llm_node_spec
from novel_system.services.snowflake_steps import get_step_definition
from novel_system.services.snowflake_workspace_llm import _collect_generation_gaps, _normalize_candidates_output
from tests.accounted_llm_fakes import accounted_generate_method


def _prompt_payload(prompt: str) -> dict:
    """把 Working payload 那段 JSON 解出来（与渲染格式无关）。"""
    try:
        body = prompt.split("Working payload:\n", 1)[1].rsplit("\n\nRequired top-level", 1)[0]
        parsed = json.loads(body)
    except (IndexError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _root_step_key(prompt: str) -> str:
    """提示词根层的 step_key。

    替身**不能**用裸子串匹配判断"这是哪一步"：upstream_steps 里带着每个上游步骤
    自己的 step_key，裸匹配会把「深化 scene_details」误判成「生成 scene_list」。
    也**不能**认缩进：载荷以紧凑 JSON 渲染（省输入预算），根层键不再有固定缩进——
    要按结构从解析后的载荷里取。
    """
    return str(_prompt_payload(prompt).get("step_key") or "")


def _fake_generate_capturing(captured: list, payload: dict):
    """monkeypatch 用：捕获发往 LLM 的请求，回放固定 structured_output。"""

    def fake_generate(self, request):  # noqa: ANN001
        captured.append(request)
        return LLMResponse(
            request_id="resp_fe_candidates",
            provider="fake-provider",
            model=request.model,
            text=json.dumps(payload, ensure_ascii=False),
            structured_output=payload,
            response_format="json_object",
            raw_response={"id": "resp_fe_candidates"},
            usage={"input_tokens": 10, "output_tokens": 20, "total_tokens": 30},
            finish_reason="stop",
        )

    return accounted_generate_method(fake_generate)


def _create_project(client, key: str = "fe-cands-project", title: str = "候选之书") -> str:
    response = client.post(
        "/api/v2/projects",
        json={"title": title, "outline_text": "构思候选验证用项目。"},
        headers={"X-Idempotency-Key": key},
    )
    assert response.status_code == 200, response.text
    return response.json()["data"]["project"]["project_id"]


def test_fe_candidates_is_fail_closed_without_llm_and_records_no_turn(client) -> None:
    """阶段 U（2026-09-17）：「先看 3 个方向」与教练同一条路——LLM 未启用即 409，不再回 source=fallback + 空列表
    让前端自己猜；也不往教练日志里写回合。"""
    pid = _create_project(client)
    response = client.post(
        f"/api/v2/projects/{pid}/snowflake-workspace/steps/one_sentence_summary/fe-candidates",
        json={"context": "【01 读者定位】文学悬疑", "draft": "她发现恩师改写了档案。", "target_chars": 120},
        headers={"X-Idempotency-Key": "fe-cands-fallback"},
    )
    assert response.status_code == 409, response.text
    error = response.json()["error"]
    assert error["code"] == "SNOWFLAKE_LLM_NOT_CONFIGURED"
    assert error["details"]["author_action"]
    workspace = client.get(f"/api/v2/projects/{pid}/snowflake-workspace").json()["data"]
    assert workspace["assistant_history"] == []


def test_fe_candidates_rejects_unknown_step(client) -> None:
    pid = _create_project(client)
    response = client.post(
        f"/api/v2/projects/{pid}/snowflake-workspace/steps/nonsense/fe-candidates",
        json={},
        headers={"X-Idempotency-Key": "fe-cands-bad-step"},
    )
    assert response.status_code in {400, 404}


def test_normalize_candidates_output_clips_to_contract_shape() -> None:
    raw = {
        "candidates": [
            {"label": "一个超长的标签会被截断", "tag": "x" * 40, "text": "  候选正文一  ", "notes": ["很长的要点会被截断啊", "短", "", "第四条被丢弃"]},
            {"label": "", "tag": "", "text": "候选正文二", "notes": "not-a-list"},
            {"text": "   "},
            {"text": "候选正文三"},
            {"text": "第五条被截掉"},
        ]
    }
    out = _normalize_candidates_output(raw)["candidates"]
    assert [c["text"] for c in out] == ["候选正文一", "候选正文二", "候选正文三"]
    assert len(out[0]["label"]) <= 8
    assert len(out[0]["tag"]) <= 16
    assert all(len(n) <= 10 for n in out[0]["notes"]) and len(out[0]["notes"]) == 2
    assert out[1]["label"].startswith("方向")
    assert out[1]["notes"] == []


def test_fe_candidates_prompt_grounded_in_backend_truth_not_fe_fold(client, monkeypatch) -> None:
    """候选提示必须以后端权威材料为主：approved 上游规范草稿 + 当前步压力诊断入 payload；
    fe_* 写穿缓存键（脚手架 JSON/状态）不得泄漏进提示，作者自由草稿以 author_free_draft 显式保留。"""
    pid = _create_project(client)
    patched = client.patch(
        f"/api/v2/projects/{pid}/snowflake-workspace/steps/book_brief",
        json={
            "draft": {
                "category": "文学悬疑",
                "target_reader": "想看旧案、家庭代价与高压女主的读者",
                "story_kind": "代价沉重的家庭真相悬疑",
                "delight_reason": "每条线索都在逼近真相同时抬高个人代价",
                "genre_promise": "真相越清晰，女主失去的越多",
                "expected_reader_emotion": "压迫、怀疑与向前的拉力",
                "fe_text": "作者的自由草稿基调",
                "fe_scaffold": {"genre": "文学悬疑", "内部键": "不应进提示"},
                "fe_state": "done",
            }
        },
    )
    assert patched.status_code == 200, patched.text
    approved = client.post(f"/api/v2/projects/{pid}/snowflake-workspace/steps/book_brief/approve", json={})
    assert approved.status_code == 200, approved.text

    monkeypatch.setenv("NOVEL_SYSTEM_LLM_ENABLED", "true")
    captured: list = []
    payload = {
        "candidates": [
            {"label": "情绪向", "tag": "情绪压强", "text": "候选一", "notes": ["锁定代价"]},
            {"label": "推进向", "tag": "情节推进", "text": "候选二", "notes": []},
            {"label": "对照向", "tag": "道德对照", "text": "候选三", "notes": []},
        ]
    }
    monkeypatch.setattr(
        "novel_system.services.llm_client.LLMClient.generate_accounted",
        _fake_generate_capturing(captured, payload),
    )

    response = client.post(
        f"/api/v2/projects/{pid}/snowflake-workspace/steps/one_sentence_summary/fe-candidates",
        json={"context": "【01 读者定位】前端折叠上下文", "draft": "她发现恩师改写了档案。", "target_chars": 120},
    )
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["source"] == "llm"
    assert [c["text"] for c in data["candidates"]] == ["候选一", "候选二", "候选三"]

    assert len(captured) == 1
    user_prompt = captured[0].messages[1]["content"]
    # 后端权威上下文：上游 01 步 + 当前步压力诊断都在提示里
    assert '"upstream_steps"' in user_prompt
    assert "读者定位" in user_prompt
    assert '"current_pressure_diagnosis"' in user_prompt
    assert '"pressure_rubric"' in user_prompt
    # FE 折叠文本降级为补充信号
    assert '"fe_local_context"' in user_prompt and "前端折叠上下文" in user_prompt
    # fe_* 写穿缓存键不泄漏；作者自由草稿显式保留
    assert "fe_scaffold" not in user_prompt
    assert "不应进提示" not in user_prompt
    assert '"author_free_draft"' in user_prompt and "作者的自由草稿基调" in user_prompt


def test_generate_step_carries_adopted_direction_into_prompt(client, monkeypatch) -> None:
    """「采纳并结构化」：direction_text 必须以 adopted_direction 进入生成提示。"""
    pid = _create_project(client)
    monkeypatch.setenv("NOVEL_SYSTEM_LLM_ENABLED", "true")
    captured: list = []
    payload = {
        "category": "文学悬疑",
        "target_reader": "想看旧案与家庭代价的读者",
        "story_kind": "代价沉重的家庭真相悬疑",
        "delight_reason": "线索逼近真相的同时抬高个人代价",
        "genre_promise": "真相越清晰失去越多",
        "expected_reader_emotion": "压迫与向前的拉力",
        "safety_rules": ["只借鉴抽象手法。", "不复制人物设定。"],
    }
    monkeypatch.setattr(
        "novel_system.services.llm_client.LLMClient.generate_accounted",
        _fake_generate_capturing(captured, payload),
    )

    response = client.post(
        f"/api/v2/projects/{pid}/snowflake-workspace/steps/book_brief/generate",
        json={"direction_text": "沿着「修复师在恩师档案里发现父亲」这个方向展开", "require_llm": True},
    )
    assert response.status_code == 200, response.text
    assert response.json()["data"]["step"]["draft"]["category"] == "文学悬疑"

    assert len(captured) == 1
    user_prompt = captured[0].messages[1]["content"]
    assert '"adopted_direction"' in user_prompt
    assert "修复师在恩师档案里发现父亲" in user_prompt
    assert "方向蓝本" in user_prompt  # how_to_use 指令在场


def test_generate_step_require_llm_refuses_fallback_when_llm_disabled(client) -> None:
    """require_llm=true 且 LLM 未启用：诚实 409，绝不静默写入启发式 fallback 版本。"""
    pid = _create_project(client)
    response = client.post(
        f"/api/v2/projects/{pid}/snowflake-workspace/steps/book_brief/generate",
        json={"direction_text": "任意方向", "require_llm": True},
    )
    assert response.status_code == 409, response.text
    error = response.json()["error"]
    assert error["code"] == "SNOWFLAKE_LLM_REQUIRED"
    # 没有偷偷落一版草稿
    ws = client.get(f"/api/v2/projects/{pid}/snowflake-workspace").json()["data"]
    step = next(s for s in ws["steps"] if s["step_key"] == "book_brief")
    assert step.get("artifact") in (None, {})


def _load_generate_template(step_key: str) -> dict:
    import yaml
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    prompts = yaml.safe_load((root / "config" / "prompts.yaml").read_text(encoding="utf-8"))
    template = prompts.get("templates", {}).get(f"snowflake_generate_{step_key}")
    assert template, f"missing template snowflake_generate_{step_key}"
    return template


def _template_item_keys(template: dict, *, prefix: str = "") -> list[str]:
    keys: list[str] = []
    for key, value in template.items():
        keys.append(key)
        if isinstance(value, dict):
            keys.extend(_template_item_keys(value))
    return keys


def test_generate_templates_mention_every_canonical_collection_key() -> None:
    """契约守卫（残缺 bug 的根因回归）：清洗器只保留编辑器模板键，生成模板的
    task_prompt 必须逐一提及这些规范键名——否则模型换键名输出，内容会被清洗
    静默丢弃、整步残缺。服务端指派的身份键豁免。"""
    exempt_global = {"row_uid", "chapter_id", "chapter_title", "chapter_goal"}
    extra_exempt = {"scene_details": {"primary_form"}}  # 服务端以 scene_type 镜像 primary_form
    collection_steps = {
        "character_sheets": "characters",
        "character_synopses": "characters",
        "character_bibles": "characters",
        "scene_list": "scenes",
        "scene_details": "scenes",
    }
    for step_key, field_key in collection_steps.items():
        editor_field = next(
            field
            for field in get_step_definition(step_key)["editor"]["fields"]
            if field.get("key") == field_key
        )
        task_prompt = _load_generate_template(step_key)["task_prompt"]
        exempt = exempt_global | extra_exempt.get(step_key, set())
        missing = [
            key
            for key in _template_item_keys(editor_field.get("template") or {})
            if key not in exempt and key not in task_prompt
        ]
        assert not missing, f"{step_key} 模板未提及契约键：{missing}"

    # 反向守卫：禁止再出现「脱离契约的游离键名」（模型照做即内容全丢）
    rogue_keys = {
        "entry_point", "information_gap", "scene_behavior", "core_line",
        "result_or_change", "value_conflict", "scene_potential", "visible_behavior",
    }
    for step_key in collection_steps:
        task_prompt = _load_generate_template(step_key)["task_prompt"]
        leaked = sorted(key for key in rogue_keys if key in task_prompt)
        assert not leaked, f"{step_key} 模板出现契约外键名：{leaked}"

    # 前端可解析格式的两处标记：背景故事六前缀行（阶段 D 加「视角故事：」）/ 大纲 = 恰好五段展开 + 结构化章表
    synopses_prompt = _load_generate_template("character_synopses")["task_prompt"]
    for prefix in ("信念：", "旧伤：", "欲望：", "恐惧：", "关系：", "视角故事："):
        assert prefix in synopses_prompt
    outline_prompt = _load_generate_template("long_synopsis")["task_prompt"]
    assert "exactly 5 paragraphs" in outline_prompt and "灾一" in outline_prompt
    assert "章名：" not in outline_prompt, "阶段 D：paragraphs 不再是章行镜像"


def test_collect_generation_gaps_drills_into_collections() -> None:
    """修复重试的靶子：完备性只看顶层，_collect_generation_gaps 必须下钻集合项。"""
    gaps = _collect_generation_gaps(
        "character_synopses",
        {"characters": [{"character_id": "c1", "display_name": "林岑", "role": "主角", "synopsis": ""}]},
    )
    assert "characters[林岑].synopsis" in gaps
    assert not any(gap.endswith(".display_name") for gap in gaps)

    gaps = _collect_generation_gaps(
        "character_bibles",
        {"characters": [{
            "character_id": "c1", "display_name": "林岑", "role": "主角",
            "physical_profile": {"age": "", "height": "", "appearance": "", "style": ""},
            "personality_profile": {"strongest_trait": "沉静"},
            "environment_profile": {},
            "psychological_profile": {"philosophy": "记录即救赎"},
        }]},
    )
    assert "characters[林岑].physical_profile" in gaps
    assert "characters[林岑].environment_profile" in gaps
    assert "characters[林岑].personality_profile" not in gaps  # 整块非空即不算灾难性残缺

    gaps = _collect_generation_gaps(
        "scene_list",
        {"scenes": [{"scene_id": "SC01", "summary": "她认出年份", "pov_character_id": "c1", "location": "", "crucible": "", "chapter_role": "起疑"}]},
    )
    assert "scenes[SC01].location" in gaps and "scenes[SC01].crucible" in gaps
    assert "scenes[SC01].summary" not in gaps

    assert _collect_generation_gaps("one_sentence_summary", {"summary": "她发现恩师改写了档案。"}) == []


def test_generate_step_repairs_empty_fields_with_one_targeted_retry(client, monkeypatch) -> None:
    """首轮输出清洗后有空字段 → 自动带 completeness_repair 重试一次并采用更完整版；
    首轮已完整 → 只调一次，不多花钱。"""
    monkeypatch.setenv("NOVEL_SYSTEM_LLM_ENABLED", "true")
    complete = {
        "category": "文学悬疑",
        "target_reader": "想看旧案与家庭代价的读者",
        "story_kind": "家庭真相悬疑",
        "delight_reason": "线索逼近真相的同时抬高代价",
        "genre_promise": "真相越清晰失去越多",
        "expected_reader_emotion": "压迫与向前的拉力",
        "safety_rules": ["只借鉴抽象手法。", "不复制人物设定。", "不逐句模仿参考文风。"],
    }
    incomplete = {k: v for k, v in complete.items() if k not in {"expected_reader_emotion", "genre_promise"}}

    captured: list = []

    def fake_generate(self, request):  # noqa: ANN001
        captured.append(request)
        payload = incomplete if len(captured) == 1 else complete
        return LLMResponse(
            request_id=f"resp_repair_{len(captured)}",
            provider="fake-provider",
            model=request.model,
            text=json.dumps(payload, ensure_ascii=False),
            structured_output=payload,
            response_format="json_object",
            raw_response={"id": f"resp_repair_{len(captured)}"},
            usage={"input_tokens": 10, "output_tokens": 20, "total_tokens": 30},
            finish_reason="stop",
        )

    monkeypatch.setattr(
        "novel_system.services.llm_client.LLMClient.generate_accounted",
        accounted_generate_method(fake_generate),
    )

    pid = _create_project(client)
    response = client.post(f"/api/v2/projects/{pid}/snowflake-workspace/steps/book_brief/generate", json={})
    assert response.status_code == 200, response.text
    draft = response.json()["data"]["step"]["draft"]
    assert draft["expected_reader_emotion"] == "压迫与向前的拉力"
    assert draft["genre_promise"] == "真相越清晰失去越多"

    assert len(captured) == 2
    first_prompt = captured[0].messages[1]["content"]
    repair_prompt = captured[1].messages[1]["content"]
    assert "completeness_repair" not in first_prompt
    assert "completeness_repair" in repair_prompt
    assert "expected_reader_emotion" in repair_prompt and "genre_promise" in repair_prompt

    # 首轮已完整：只调一次，不多花钱
    captured.clear()

    def fake_generate_complete(self, request):  # noqa: ANN001
        captured.append(request)
        return LLMResponse(
            request_id="resp_complete_once",
            provider="fake-provider",
            model=request.model,
            text=json.dumps(complete, ensure_ascii=False),
            structured_output=complete,
            response_format="json_object",
            raw_response={"id": "resp_complete_once"},
            usage={"input_tokens": 10, "output_tokens": 20, "total_tokens": 30},
            finish_reason="stop",
        )

    monkeypatch.setattr(
        "novel_system.services.llm_client.LLMClient.generate_accounted",
        accounted_generate_method(fake_generate_complete),
    )
    pid3 = _create_project(client, key="repair-complete-once", title="候选之书二")
    response = client.post(f"/api/v2/projects/{pid3}/snowflake-workspace/steps/book_brief/generate", json={})
    assert response.status_code == 200, response.text
    assert len(captured) == 1


_SCENE_LIST_PAYLOAD = {"scenes": [
    {"scene_seq": 1, "pov_character_id": "c1", "summary": "她要在交班前比对墨迹年份，被缺页阻拦，确认补写却不敢声张。",
     "primary_form": "proactive", "scene_type": "proactive", "location": "地下修复室", "crucible": "工期压着，上报先查她", "chapter_role": "起疑"},
    {"scene_seq": 2, "pov_character_id": "c1", "summary": "她为是否越权调副本挣扎，两个选项都有代价，决定夜里去比对台。",
     "primary_form": "reactive", "scene_type": "reactive", "location": "宿舍", "crucible": "权限有限，越查越像内鬼", "chapter_role": "抉择"},
]}


def _scene_detail_items(scene_ids: list[str], *, goal_prefix: str = "拿到") -> list[dict]:
    items = []
    for index, scene_id in enumerate(scene_ids, start=1):
        proactive = index == 1
        items.append({
            "scene_id": scene_id, "title": f"场景{index}", "summary": f"第 {index} 场推进一句。",
            "scene_type": "proactive" if proactive else "reactive",
            "location": "地下修复室" if proactive else "宿舍",
            "scene_crucible": "走不掉的压力", "crucible": "走不掉的压力",
            "goal": f"{goal_prefix}证据（限今晚）" if proactive else "",
            "conflict": "① 试→阻 ② 再试→更糟" if proactive else "", "setback": "结尾比开场更糟" if proactive else "",
            "reaction": "" if proactive else "手先抖，才想明白", "dilemma": "" if proactive else "认罪或抵抗，均有代价",
            "decision": "" if proactive else "选坏选项，引出下一场目标",
            "exit_change": "不可逆变化", "hook": "翻页钩子", "target_length_band": "medium",
            "must_include_text": "", "beats_json": ["起", "转", "落"],
        })
    return items


def test_generate_step_focus_scene_only_touches_target(client, monkeypatch) -> None:
    """「AI 补全这一场」：focus_scene_refs 只深化目标场——其余场景按 scene_id 合并保持不动；
    焦点外场景的缺口不触发修复重试；提示里带 focus_scenes 指令。"""
    pid = _create_project(client, key="focus-scene", title="单场定向之书")
    monkeypatch.setenv("NOVEL_SYSTEM_LLM_ENABLED", "true")
    captured: list = []

    def fake_generate(self, request):  # noqa: ANN001
        captured.append(request)
        prompt = request.messages[1]["content"]
        if _root_step_key(prompt) == "scene_list":
            payload = _SCENE_LIST_PAYLOAD
        elif '"focus_scenes"' in prompt:
            # 定向判别必须认 payload 里的 JSON 键，不能认裸词——task_prompt 本身
            # 就要向模型解释 focus_scenes，裸词匹配会把整表生成误判成定向生成。
            # 单场定向：只回焦点场（scene 2），换一个可断言的 dilemma
            payload = {"scenes": [dict(_scene_detail_items(["IGNORED", _focus_ids[1]])[1], dilemma="聚焦后的新两难：交出母本或守住职位")]}
        else:
            payload = {"scenes": _scene_detail_items(_focus_ids)}
        return LLMResponse(
            request_id=f"resp_focus_{len(captured)}", provider="fake-provider", model=request.model,
            text=json.dumps(payload, ensure_ascii=False), structured_output=payload,
            response_format="json_object", raw_response={"id": "resp"},
            usage={"input_tokens": 10, "output_tokens": 20, "total_tokens": 30}, finish_reason="stop",
        )

    monkeypatch.setattr(
        "novel_system.services.llm_client.LLMClient.generate_accounted",
        accounted_generate_method(fake_generate),
    )

    # 1) 生成场景列表（服务端指派 scene_id）
    listed = client.post(f"/api/v2/projects/{pid}/snowflake-workspace/steps/scene_list/generate", json={})
    assert listed.status_code == 200, listed.text
    _focus_ids = [s["scene_id"] for s in listed.json()["data"]["step"]["draft"]["scenes"]]
    assert len(_focus_ids) == 2

    # 2) 全量深化一次，形成两场都完整的底稿
    detailed = client.post(f"/api/v2/projects/{pid}/snowflake-workspace/steps/scene_details/generate", json={})
    assert detailed.status_code == 200, detailed.text
    base_scenes = {s["scene_id"]: s for s in detailed.json()["data"]["step"]["draft"]["scenes"]}
    scene1_before = base_scenes[_focus_ids[0]]

    # 3) 单场定向：只补第 2 场
    calls_before = len(captured)
    focused = client.post(
        f"/api/v2/projects/{pid}/snowflake-workspace/steps/scene_details/generate",
        json={"focus_scene_refs": [_focus_ids[1]], "require_llm": True},
    )
    assert focused.status_code == 200, focused.text
    scenes = {s["scene_id"]: s for s in focused.json()["data"]["step"]["draft"]["scenes"]}
    assert scenes[_focus_ids[1]]["dilemma"] == "聚焦后的新两难：交出母本或守住职位"
    # 焦点外场景原样保留（按 scene_id 合并）
    assert scenes[_focus_ids[0]]["goal"] == scene1_before["goal"]
    assert scenes[_focus_ids[0]]["conflict"] == scene1_before["conflict"]
    # 焦点场完整 → 不追加修复重试；提示带 focus_scenes 指令
    assert len(captured) - calls_before == 1
    focus_prompt = captured[-1].messages[1]["content"]
    assert '"focus_scenes"' in focus_prompt and "只深化" in focus_prompt
    assert _focus_ids[1] in focus_prompt

    # 4) 指错场景 → 诚实 409
    missing = client.post(
        f"/api/v2/projects/{pid}/snowflake-workspace/steps/scene_details/generate",
        json={"focus_scene_refs": ["NO_SUCH_SCENE"], "require_llm": True},
    )
    assert missing.status_code == 409
    assert missing.json()["error"]["code"] == "SNOWFLAKE_FOCUS_SCENE_NOT_FOUND"


def test_scene_triage_suggest_items_carry_row_uid(client, monkeypatch) -> None:
    """分诊条目必须带 row_uid——FE 场景规划以 row_uid 为键，缺它就对不上位。"""
    pid = _create_project(client, key="triage-rowuid", title="分诊对位之书")
    monkeypatch.setenv("NOVEL_SYSTEM_LLM_ENABLED", "true")
    scene_ids: list[str] = []

    def fake_generate(self, request):  # noqa: ANN001
        prompt = request.messages[1]["content"]
        if _root_step_key(prompt) == "scene_list":
            payload = _SCENE_LIST_PAYLOAD
        elif "triage_rules" in prompt:
            payload = {"items": []}  # 归一化会回落到确定性诊断底稿
        else:
            payload = {"scenes": _scene_detail_items(scene_ids)}
        return LLMResponse(
            request_id="resp_triage", provider="fake-provider", model=request.model,
            text=json.dumps(payload, ensure_ascii=False), structured_output=payload,
            response_format="json_object", raw_response={"id": "resp"},
            usage={"input_tokens": 10, "output_tokens": 20, "total_tokens": 30}, finish_reason="stop",
        )

    monkeypatch.setattr(
        "novel_system.services.llm_client.LLMClient.generate_accounted",
        accounted_generate_method(fake_generate),
    )
    listed = client.post(f"/api/v2/projects/{pid}/snowflake-workspace/steps/scene_list/generate", json={})
    assert listed.status_code == 200, listed.text
    scene_ids.extend(s["scene_id"] for s in listed.json()["data"]["step"]["draft"]["scenes"])
    assert client.post(f"/api/v2/projects/{pid}/snowflake-workspace/steps/scene_details/generate", json={}).status_code == 200

    suggested = client.post(f"/api/v2/projects/{pid}/snowflake-workspace/scene-triage/suggest", json={})
    assert suggested.status_code == 200, suggested.text
    items = suggested.json()["data"]["items"]
    assert items, "triage items should not be empty"
    for item in items:
        assert item["scene_plan_id"]
        assert "row_uid" in item
        assert item["status"] in {"pass", "maybe", "rewrite"}


def test_focus_scene_payload_matches_row_uid() -> None:
    """教练/单场聚焦允许 row_uid 指场（FE 场景键），不只 scene_id。"""
    from novel_system.services.snowflake_workspace_llm import _focus_scene_payload

    step = {"draft": {"scenes": [{"scene_id": "PRJ_CH01_SC01", "row_uid": "S01", "summary": "第一场"}]}}
    assert _focus_scene_payload(step, "S01")["summary"] == "第一场"
    assert _focus_scene_payload(step, "PRJ_CH01_SC01")["summary"] == "第一场"
    assert _focus_scene_payload(step, "S99") == {"scene_id": "S99"}


def test_assistant_endpoint_replies_and_persists_history(client, monkeypatch) -> None:
    """驻场教练：回合服务端持久化，历史随回包增长。阶段 T 起 LLM 未启用即 409——没有规则回退。"""
    pid = _create_project(client, key="coach", title="教练之书")
    off = client.post(
        f"/api/v2/projects/{pid}/snowflake-workspace/assistant",
        json={"step_key": "book_brief", "message": "这一步还缺什么？"},
    )
    assert off.status_code == 409, off.text
    assert off.json()["error"]["code"] == "SNOWFLAKE_LLM_NOT_CONFIGURED"

    monkeypatch.setenv("NOVEL_SYSTEM_LLM_ENABLED", "true")
    captured: list = []
    monkeypatch.setattr(
        "novel_system.services.llm_client.LLMClient.generate_accounted",
        _fake_generate_capturing(
            captured,
            {"reply": "先把目标读者写实。", "suggestions": ["写清楚快感来源"], "candidate_label": "", "candidate_patch": {},
             "brief_update": {"lines": []}},
        ),
    )
    first = client.post(
        f"/api/v2/projects/{pid}/snowflake-workspace/assistant",
        json={"step_key": "book_brief", "message": "这一步还缺什么？", "draft_override": {"category": "文学悬疑"}},
    )
    assert first.status_code == 200, first.text
    data = first.json()["data"]
    assert data["reply"] == "先把目标读者写实。"
    assert data["source"] == "llm"
    assert data["step_key"] == "book_brief"
    assert len(data["assistant_history"]) == 1
    assert data["assistant_history"][0]["message"] == "这一步还缺什么？"

    second = client.post(
        f"/api/v2/projects/{pid}/snowflake-workspace/assistant",
        json={"step_key": "book_brief", "message": "帮我把压力抬高一档。"},
    )
    assert second.status_code == 200, second.text
    assert len(second.json()["data"]["assistant_history"]) == 2

    # workspace 全量回包也带历史（FE 教练页懒加载的正源）
    ws = client.get(f"/api/v2/projects/{pid}/snowflake-workspace").json()["data"]
    assert len(ws["assistant_history"]) == 2


def test_save_scene_triage_reuses_triage_id_without_duplicating_rows(client, monkeypatch) -> None:
    """FE 分诊落库流：suggest → save（推荐态）拿到 triage_id；复诊携带 triage_id 再存
    → 原行更新、不堆新行（triage_items 数量与 id 集合稳定）。"""
    pid = _create_project(client, key="triage-save", title="分诊存档之书")
    monkeypatch.setenv("NOVEL_SYSTEM_LLM_ENABLED", "true")
    scene_ids: list[str] = []

    def fake_generate(self, request):  # noqa: ANN001
        prompt = request.messages[1]["content"]
        if _root_step_key(prompt) == "scene_list":
            payload = _SCENE_LIST_PAYLOAD
        elif "triage_rules" in prompt:
            payload = {"items": []}
        else:
            payload = {"scenes": _scene_detail_items(scene_ids)}
        return LLMResponse(
            request_id="resp_triage_save", provider="fake-provider", model=request.model,
            text=json.dumps(payload, ensure_ascii=False), structured_output=payload,
            response_format="json_object", raw_response={"id": "resp"},
            usage={"input_tokens": 10, "output_tokens": 20, "total_tokens": 30}, finish_reason="stop",
        )

    monkeypatch.setattr(
        "novel_system.services.llm_client.LLMClient.generate_accounted",
        accounted_generate_method(fake_generate),
    )
    listed = client.post(f"/api/v2/projects/{pid}/snowflake-workspace/steps/scene_list/generate", json={})
    scene_ids.extend(s["scene_id"] for s in listed.json()["data"]["step"]["draft"]["scenes"])
    assert client.post(f"/api/v2/projects/{pid}/snowflake-workspace/steps/scene_details/generate", json={}).status_code == 200

    suggested = client.post(f"/api/v2/projects/{pid}/snowflake-workspace/scene-triage/suggest", json={}).json()["data"]

    def save(items):
        resp = client.post(f"/api/v2/projects/{pid}/snowflake-workspace/scene-triage", json={"items": items})
        assert resp.status_code == 200, resp.text
        return resp.json()["data"]["items"]

    first_items = save([
        {"scene_plan_id": it["scene_plan_id"], "scene_id": it["scene_id"], "recommended_status": it["status"],
         "fix_steps": it["fix_steps"], "repair_patch": it["repair_patch"], "notes": it["notes"]}
        for it in suggested["items"]
    ])
    ids_by_plan = {it["scene_plan_id"]: it["triage_id"] for it in first_items}
    assert all(ids_by_plan.values())
    # 推荐态存档：不产生人工裁定
    assert all(not it.get("manual_status") for it in first_items)

    second_items = save([
        {"triage_id": ids_by_plan[it["scene_plan_id"]], "scene_plan_id": it["scene_plan_id"],
         "scene_id": it["scene_id"], "recommended_status": it["status"]}
        for it in suggested["items"]
    ])
    assert len(second_items) == len(first_items)
    assert {it["triage_id"] for it in second_items} == set(ids_by_plan.values())


def test_candidates_node_registered_with_routing_and_template() -> None:
    """三件套铁律：registry + models.yaml task_routing + prompts.yaml 模板缺一不可。"""
    import yaml
    from pathlib import Path

    node = get_llm_node_spec("snowflake_step_candidates")
    assert node is not None, "registry missing snowflake_step_candidates"
    root = Path(__file__).resolve().parents[2]
    models = yaml.safe_load((root / "config" / "models.yaml").read_text(encoding="utf-8"))
    assert "snowflake_step_candidates" in models.get("task_routing", {})
    prompts = yaml.safe_load((root / "config" / "prompts.yaml").read_text(encoding="utf-8"))
    template = prompts.get("templates", {}).get("snowflake_step_candidates")
    assert template and template.get("structured_schema", {}).get("required") == ["candidates"]


# ---- 集合步定向生成（focus_character_refs）与成员保全合并 ----

def _char_sheet_item(cid: str, name: str, **over) -> dict:
    item = {
        "character_id": cid, "display_name": name, "role": "主角",
        "goal": "在交班前拿到母本", "ambition": "被当成人而不是继承者看见",
        "values": ["没有什么比真相更重要"], "conflict": "恩师挡在门前",
        "epiphany": "真相要交给活人", "one_sentence_summary": f"{name}要在代价抬高前把真相带出档案馆。",
        "one_paragraph_summary": f"{name}从起疑到摊牌的完整弧线概括。",
    }
    item.update(over)
    return item


def _char_response(payload: dict, seq: int, model: str) -> LLMResponse:
    return LLMResponse(
        request_id=f"resp_char_{seq}", provider="fake-provider", model=model,
        text=json.dumps(payload, ensure_ascii=False), structured_output=payload,
        response_format="json_object", raw_response={"id": f"resp_char_{seq}"},
        usage={"input_tokens": 10, "output_tokens": 20, "total_tokens": 30}, finish_reason="stop",
    )


def test_generate_step_full_regen_keeps_manual_members(client, monkeypatch) -> None:
    """整步重写的合并底稿是「当前草稿」而非空骨架：作者手工加的角色，
    模型这轮没回传也必须幸存；模型部分回传的成员，空字段不清空既有内容。"""
    pid = _create_project(client, key="keep-members", title="成员保全之书")
    monkeypatch.setenv("NOVEL_SYSTEM_LLM_ENABLED", "true")
    captured: list = []
    rounds: list[dict] = [
        {"characters": [_char_sheet_item("c1", "林岑"), _char_sheet_item("c2", "周岚", role="对立面", goal="守住她改写的版本")]},
        {"characters": [_char_sheet_item("c1", "林岑", goal="第二轮的新目标"), _char_sheet_item("c2", "周岚", role="对立面", goal="守住她改写的版本")]},
        {"characters": [{"character_id": "c1", "display_name": "林岑", "role": "", "goal": "第三轮定向目标",
                         "ambition": "", "values": [], "conflict": "", "epiphany": "第三轮顿悟",
                         "one_sentence_summary": "", "one_paragraph_summary": ""}]},
    ]

    def fake_generate(self, request):  # noqa: ANN001
        captured.append(request)
        return _char_response(rounds[min(len(captured), len(rounds)) - 1], len(captured), request.model)

    monkeypatch.setattr(
        "novel_system.services.llm_client.LLMClient.generate_accounted",
        accounted_generate_method(fake_generate),
    )

    first = client.post(f"/api/v2/projects/{pid}/snowflake-workspace/steps/character_sheets/generate", json={})
    assert first.status_code == 200, first.text
    chars = {c["character_id"]: c for c in first.json()["data"]["step"]["draft"]["characters"]}
    assert set(chars) == {"c1", "c2"}

    # 作者手工加第三个角色（模拟 FE 自动保存上行）
    manual = _char_sheet_item("c9", "王五", role="帮手", goal="替她递出关键钥匙")
    patched = client.patch(
        f"/api/v2/projects/{pid}/snowflake-workspace/steps/character_sheets",
        json={"draft": {"characters": [chars["c1"], chars["c2"], manual]}, "force": True},
    )
    assert patched.status_code == 200, patched.text

    # 整步重写：模型只回传 c1/c2 → 王五必须原样幸存，且名册顺序不变
    second = client.post(f"/api/v2/projects/{pid}/snowflake-workspace/steps/character_sheets/generate", json={})
    assert second.status_code == 200, second.text
    second_chars = second.json()["data"]["step"]["draft"]["characters"]
    chars2 = {c["character_id"]: c for c in second_chars}
    assert [c["character_id"] for c in second_chars] == ["c1", "c2", "c9"]
    assert chars2["c1"]["goal"] == "第二轮的新目标"
    assert chars2["c9"]["display_name"] == "王五"
    assert chars2["c9"]["goal"] == "替她递出关键钥匙"

    # 部分回传：c1 只带 goal/epiphany，其余空字段不清空既有内容；c2/c9 不动
    third = client.post(f"/api/v2/projects/{pid}/snowflake-workspace/steps/character_sheets/generate", json={})
    assert third.status_code == 200, third.text
    chars3 = {c["character_id"]: c for c in third.json()["data"]["step"]["draft"]["characters"]}
    assert set(chars3) == {"c1", "c2", "c9"}
    assert chars3["c1"]["goal"] == "第三轮定向目标"
    assert chars3["c1"]["ambition"] == "被当成人而不是继承者看见"
    assert chars3["c1"]["conflict"] == "恩师挡在门前"
    assert chars3["c2"]["goal"] == "守住她改写的版本"


def test_generate_step_focus_character_only_touches_target(client, monkeypatch) -> None:
    """「AI 补全这个角色」：focus_character_refs 只深化目标角色——其余角色保持不动；
    模型违约复述焦点外角色时，服务端硬过滤不让它落地；指错角色诚实 409。"""
    pid = _create_project(client, key="focus-char", title="单角色定向之书")
    monkeypatch.setenv("NOVEL_SYSTEM_LLM_ENABLED", "true")
    captured: list = []
    full_payload = {"characters": [_char_sheet_item("c1", "林岑"), _char_sheet_item("c2", "周岚", role="对立面", goal="守住她改写的版本")]}
    focus_payload = {"characters": [
        # 模型违约：把焦点外的 c1 也改了——服务端必须过滤掉
        _char_sheet_item("c1", "林岑", goal="不应生效的改写"),
        _char_sheet_item("c2", "周岚", role="对立面", goal="聚焦后的新目标：亲手交出母本"),
    ]}

    def fake_generate(self, request):  # noqa: ANN001
        captured.append(request)
        prompt = request.messages[1]["content"]
        payload = focus_payload if '"focus_characters"' in prompt else full_payload
        return _char_response(payload, len(captured), request.model)

    monkeypatch.setattr(
        "novel_system.services.llm_client.LLMClient.generate_accounted",
        accounted_generate_method(fake_generate),
    )

    seeded = client.post(f"/api/v2/projects/{pid}/snowflake-workspace/steps/character_sheets/generate", json={})
    assert seeded.status_code == 200, seeded.text

    calls_before = len(captured)
    focused = client.post(
        f"/api/v2/projects/{pid}/snowflake-workspace/steps/character_sheets/generate",
        json={"focus_character_refs": ["c2"], "require_llm": True},
    )
    assert focused.status_code == 200, focused.text
    focused_chars = focused.json()["data"]["step"]["draft"]["characters"]
    chars = {c["character_id"]: c for c in focused_chars}
    assert chars["c2"]["goal"] == "聚焦后的新目标：亲手交出母本"
    assert chars["c1"]["goal"] == "在交班前拿到母本"  # 焦点外角色未被违约输出改写
    assert [c["character_id"] for c in focused_chars] == ["c1", "c2"]  # 定向不打乱名册顺序
    assert len(captured) - calls_before == 1  # 焦点角色完整 → 不追加修复重试
    focus_prompt = captured[-1].messages[1]["content"]
    assert '"focus_characters"' in focus_prompt and "只深化" in focus_prompt
    focus_members = _prompt_payload(focus_prompt)["focus_characters"]["characters"]
    assert [m["character_id"] for m in focus_members] == ["c2"]

    missing = client.post(
        f"/api/v2/projects/{pid}/snowflake-workspace/steps/character_sheets/generate",
        json={"focus_character_refs": ["NO_SUCH_CHAR"], "require_llm": True},
    )
    assert missing.status_code == 409
    assert missing.json()["error"]["code"] == "SNOWFLAKE_FOCUS_CHARACTER_NOT_FOUND"


def test_generate_step_focus_character_resolves_from_roster(client, monkeypatch) -> None:
    """06/08 定向：焦点角色还没在本步草稿里立档时，用 04 名册兜底解析并带种子入焦点上下文。"""
    pid = _create_project(client, key="focus-char-roster", title="名册兜底之书")
    monkeypatch.setenv("NOVEL_SYSTEM_LLM_ENABLED", "true")
    captured: list = []

    def fake_generate(self, request):  # noqa: ANN001
        captured.append(request)
        prompt = request.messages[1]["content"]
        if _root_step_key(prompt) == "character_sheets":
            payload = {"characters": [_char_sheet_item("c1", "林岑"), _char_sheet_item("c2", "周岚", role="对立面")]}
        else:
            payload = {"characters": [{"character_id": "c2", "display_name": "周岚", "role": "对立面",
                                       "synopsis": "二十年前那场潮汐夜里，她亲手改写了记录，也把自己钉在原地。"}]}
        return _char_response(payload, len(captured), request.model)

    monkeypatch.setattr(
        "novel_system.services.llm_client.LLMClient.generate_accounted",
        accounted_generate_method(fake_generate),
    )

    assert client.post(f"/api/v2/projects/{pid}/snowflake-workspace/steps/character_sheets/generate", json={}).status_code == 200

    # 本步（06 角色背景）还没有任何草稿，直接按名册 id 定向
    focused = client.post(
        f"/api/v2/projects/{pid}/snowflake-workspace/steps/character_synopses/generate",
        json={"focus_character_refs": ["c2"], "require_llm": True},
    )
    assert focused.status_code == 200, focused.text
    chars = {c["character_id"]: c for c in focused.json()["data"]["step"]["draft"]["characters"]}
    assert "c2" in chars and "亲手改写了记录" in chars["c2"]["synopsis"]
    focus_prompt = captured[-1].messages[1]["content"]
    assert '"focus_characters"' in focus_prompt and "周岚" in focus_prompt


def test_generate_step_draft_override_beats_stale_artifact(client, monkeypatch) -> None:
    """draft_override（FE 与上行 PATCH 同源的本地最新草稿）盖在存档之上作生成底稿：
    刚加的角色即使还没自动保存上行，也进提示上下文、并在合并结果中幸存。"""
    pid = _create_project(client, key="draft-override", title="竞态消除之书")
    monkeypatch.setenv("NOVEL_SYSTEM_LLM_ENABLED", "true")
    captured: list = []
    payload = {"characters": [_char_sheet_item("c1", "林岑"), _char_sheet_item("c2", "周岚", role="对立面")]}

    def fake_generate(self, request):  # noqa: ANN001
        captured.append(request)
        return _char_response(payload, len(captured), request.model)

    monkeypatch.setattr(
        "novel_system.services.llm_client.LLMClient.generate_accounted",
        accounted_generate_method(fake_generate),
    )

    assert client.post(f"/api/v2/projects/{pid}/snowflake-workspace/steps/character_sheets/generate", json={}).status_code == 200

    fresh = _char_sheet_item("c9", "王五", role="帮手", goal="替她递出关键钥匙")
    override = {"characters": [_char_sheet_item("c1", "林岑"), _char_sheet_item("c2", "周岚", role="对立面"), fresh]}
    regenerated = client.post(
        f"/api/v2/projects/{pid}/snowflake-workspace/steps/character_sheets/generate",
        json={"draft_override": override, "require_llm": True},
    )
    assert regenerated.status_code == 200, regenerated.text
    chars = {c["character_id"]: c for c in regenerated.json()["data"]["step"]["draft"]["characters"]}
    # 模型只回传 c1/c2：override 里的王五靠「当前草稿为合并底稿」幸存
    assert "c9" in chars and chars["c9"]["goal"] == "替她递出关键钥匙"
    # 且王五进了生成提示的 current_draft（模型看得见刚加的角色）
    assert "王五" in captured[-1].messages[1]["content"]
