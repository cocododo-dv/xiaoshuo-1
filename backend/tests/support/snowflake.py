"""雪花构思测试共用的造作品、生成 / 确认与假模型助手（B06-22：以前散在各测试文件、互相 import）。

- 经 API 的基本动作（各文件只绑定自己的样例数据与幂等键写法）：``create_project``（建作品，回 ``project``；与
  ``key_header`` 一起住在 ``api_client``，这里转出）、``patch_step`` / ``approve_step``（断言 200，回 ``data``）、
  ``post_generate`` / ``post_approve``（回响应本身，用例自己看状态码）、``workspace_step``（GET 工作台取一步）。
  ``key=None`` 由 AutoKeyTestClient 每次配新键，给了键就照用（同键同载荷即重放）。英文样例作品「雨城旧信」的
  请求体是 ``rain_city_fields()``，十步按序是 ``ALL_STEPS``。
- 经服务直接编作品：``seed_render_project``（三场，第 10 步带呈现方式）、``seed_synopsis_project``（空作品）。
- 经 API 走构思：``create_workspace_project`` + ``generate_workspace_step`` / ``approve_workspace_step``（每次新意图键）；
  ``create_closeout_project`` + ``approve_through``（键按作品 + 步固定，重复调用即重放）。
- 假模型：``install_snowflake_llm``（换掉雪花生成的记账调用）、``install_coach_llm``（换掉 LLMClient、录请求）。

雪花骨架生成器（不调模型、按大纲直通出各步）在 ``tests/snowflake_skeleton.py``。
"""

from __future__ import annotations

import json
from itertools import count

from novel_system.db.models import OutlinePlan, SnowflakeScenePlan, StoryProject
from novel_system.services.llm_client import LLMResponse
from novel_system.services.projects import PLAN_STATUS_PENDING_REVIEW, ProjectService
from novel_system.services.snowflake_chaptering import SnowflakeChapteringService
from novel_system.services.snowflake_scene_rows import SCENE_LIST_OWNED_FIELDS
from novel_system.services.snowflake_workspace import SnowflakeWorkspaceService
from tests.accounted_llm_fakes import accounted_generate_method
from tests.support.api_client import create_project, key_header


# ---------------------------------------------------------------- 经 API 的基本动作：建作品、存 / 确认 / 生成一步


#: 英文样例作品「雨城旧信」的三行大纲
RAIN_CITY_OUTLINE = (
    "An old letter pulls the heroine back to Rain City.\n"
    "The cold case turns out to be tied to her family.\n"
    "She must decide whether the truth is worth the cost."
)


def rain_city_fields(**overrides) -> dict:
    """「雨城旧信」的建作品请求体（两章、十二万字），``overrides`` 改其中几项。"""
    return {
        "title": "Rain City Signal",
        "genre": "Urban Mystery",
        "target_chapter_count": 2,
        "target_word_count": 120000,
        "outline_text": RAIN_CITY_OUTLINE,
        **overrides,
    }


def step_url(project_id: str, step_key: str) -> str:
    return f"/api/v2/projects/{project_id}/snowflake-workspace/steps/{step_key}"


def patch_step(
    client, project_id: str, step_key: str, draft: dict, *, force: bool = True, lean: bool = False, key: str | None = None
) -> dict:
    """``PATCH …/steps/{step_key}`` 存一步草稿，回 ``data``。``force`` 与 React 客户端一样随请求体带上（服务端只读
    ``draft``，它只进幂等指纹）；``lean`` 即 ``include_workspace=false``，只回 ``{step, step_run}``。"""
    body = {"draft": draft, "force": True} if force else {"draft": draft}
    url = step_url(project_id, step_key) + ("?include_workspace=false" if lean else "")
    response = client.patch(url, json=body, headers=key_header(key))
    assert response.status_code == 200, response.text
    return response.json()["data"]


def post_approve(client, project_id: str, step_key: str, *, key: str | None = None):
    """``POST …/steps/{step_key}/approve``（空请求体），回响应本身。"""
    return client.post(f"{step_url(project_id, step_key)}/approve", json={}, headers=key_header(key))


def approve_step(client, project_id: str, step_key: str, *, key: str | None = None) -> dict:
    """确认一步：断言 200，回 ``data``（``step`` / ``workspace`` / ``catalog_sync`` …）。"""
    response = post_approve(client, project_id, step_key, key=key)
    assert response.status_code == 200, response.text
    return response.json()["data"]


def post_generate(client, project_id: str, step_key: str, body: dict | None = None, *, key: str | None = None):
    """``POST …/steps/{step_key}/generate``，回响应本身（失败路径的用例自己看状态码）。"""
    return client.post(f"{step_url(project_id, step_key)}/generate", json=body or {}, headers=key_header(key))


def workspace_step(client, project_id: str, step_key: str) -> dict:
    """``GET`` 整个工作台，取其中一步。"""
    return step_of(workspace_payload(client, project_id), step_key)


#: 雪花十步，按顺序
ALL_STEPS = (
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
)


# ---------------------------------------------------------------- 直接经服务编三场（主动 / 反应 / 反应）的作品，第 10 步带呈现方式（test_snowflake_rendering_mode）


RENDER_PROJECT_ID = "prj-render"


def render_scene_rows() -> list[dict]:
    return [
        {"row_uid": "u1", "scene_seq": 1, "summary": "取账本", "primary_form": "proactive", "scene_type": "proactive",
         "location": "码头", "crucible": "退不出的困局", "pov_character_id": "c1", "chapter_role": "起疑"},
        {"row_uid": "u2", "scene_seq": 2, "summary": "消化挫败", "primary_form": "reactive", "scene_type": "reactive",
         "location": "旅馆", "crucible": "无人可信", "pov_character_id": "c1", "chapter_role": "转向"},
        {"row_uid": "u3", "scene_seq": 3, "summary": "再受挫", "primary_form": "reactive", "scene_type": "reactive",
         "location": "码头", "crucible": "只剩一晚", "pov_character_id": "c1", "chapter_role": "转向"},
    ]


def seed_render_project(session) -> SnowflakeWorkspaceService:
    session.add(
        StoryProject(
            project_id=RENDER_PROJECT_ID,
            title="概述两段",
            outline_text="概述两段大纲",
            planning_mode="snowflake",
            snowflake_workflow_mode="explore",
            target_word_count=100000,
        )
    )
    session.flush()
    service = SnowflakeWorkspaceService(session)
    service.update_step(RENDER_PROJECT_ID, "scene_list", {"draft": {"scenes": render_scene_rows()}})
    scenes = service.workspace(RENDER_PROJECT_ID)
    listed = next(step for step in scenes["steps"] if step["step_key"] == "scene_list")["draft"]["scenes"]
    details = []
    for scene in listed:
        row = {
            **scene,
            "title": scene["summary"],
            "goal": "拿到账本" if scene["primary_form"] == "proactive" else "",
            "conflict": "三轮受阻" if scene["primary_form"] == "proactive" else "",
            "setback": "账本被烧" if scene["primary_form"] == "proactive" else "",
            "reaction": "" if scene["primary_form"] == "proactive" else "手抖，半天说不出话。",
            "dilemma": "" if scene["primary_form"] == "proactive" else "报警伤弟弟；不报警明天轮到自己。",
            "decision": "" if scene["primary_form"] == "proactive" else "去找当年的证人。",
            "cost_requirement": "失去遗物",
        }
        if scene["row_uid"] == "u1":
            row["rendering_mode"] = "summary"  # 阶段 N：主动场也可以按叙述概述写
        elif scene["row_uid"] == "u2":
            row["rendering_mode"] = "summary"
        else:
            row["rendering_mode"] = "bogus"  # 非法值：full
        details.append(row)
    service.update_step(RENDER_PROJECT_ID, "scene_details", {"draft": {"scenes": details}})
    return service


#: 已有场景计划上只归 09 改的字段（第 10 步的草稿不改它们）
LIST_OWNED_FIELDS = SCENE_LIST_OWNED_FIELDS


def edit_scene_plan(service: SnowflakeWorkspaceService, row_uid: str, **fields) -> None:
    """作者改一场的真实路径（R15a 删掉了逐场的 PATCH …/scenes/{id}）：形态 / 视角在 09 改（整张场景表），
    其余字段在 10 改（整张场景规划表，只动这一行）。"""
    listed = {key: value for key, value in fields.items() if key in LIST_OWNED_FIELDS}
    if listed:
        if "primary_form" in listed:
            listed["scene_type"] = listed["primary_form"]
        rows = step_rows(service, "scene_list")
        for row in rows:
            if row["row_uid"] == row_uid:
                row.update(listed)
        service.update_step(RENDER_PROJECT_ID, "scene_list", {"draft": {"scenes": rows}})
    detailed = {key: value for key, value in fields.items() if key not in LIST_OWNED_FIELDS}
    if detailed:
        rows = step_rows(service, "scene_details")
        for row in rows:
            if row["row_uid"] == row_uid:
                row.update(detailed)
        service.update_step(RENDER_PROJECT_ID, "scene_details", {"draft": {"scenes": rows}})


def step_rows(service: SnowflakeWorkspaceService, step_key: str) -> list[dict]:
    workspace = service.workspace(RENDER_PROJECT_ID)
    step = next(item for item in workspace["steps"] if item["step_key"] == step_key)
    return [dict(row) for row in step["draft"]["scenes"]]


def scene_plan(session, row_uid: str) -> SnowflakeScenePlan:
    return next(
        plan
        for plan in session.query(SnowflakeScenePlan).filter(SnowflakeScenePlan.project_id == RENDER_PROJECT_ID).all()
        if plan.row_uid == row_uid
    )


def materialize_render_project(session, service: SnowflakeWorkspaceService) -> dict:
    service.update_step(
        RENDER_PROJECT_ID,
        "long_synopsis",
        {"draft": {"paragraphs": ["第一幕"], "chapters": [{"act": 1, "title": "第一章", "summary": "全书一章", "chapter_goal": "推进"}]}},
    )
    SnowflakeChapteringService(session).autoassign(RENDER_PROJECT_ID, "even")
    project = session.get(StoryProject, RENDER_PROJECT_ID)
    plan_json = service._build_chaptered_outline_plan(project, service._scene_plans(RENDER_PROJECT_ID))
    outline = OutlinePlan(
        plan_id="outline_plan_prj-render_01",
        project_id=RENDER_PROJECT_ID,
        version=1,
        status=PLAN_STATUS_PENDING_REVIEW,
        plan_json=plan_json,
    )
    session.add(outline)
    session.flush()
    ProjectService(session).approve_outline_plan(RENDER_PROJECT_ID, outline.plan_id)
    session.flush()
    return plan_json


# ---------------------------------------------------------------- 经 API 生成 / 确认一步，每次一个新意图键（test_snowflake_workspace_v2）


_INTENT_SEQUENCE = count()


def intent_key(prefix: str) -> str:
    """Each helper call represents a new user intent, not a transport retry."""

    return f"{prefix}-{next(_INTENT_SEQUENCE)}"


def create_workspace_project(
    client,
    *,
    key: str = "workspace-v2",
    title: str = "Rain City Signal",
    genre: str = "Urban Mystery",
    outline_text: str | None = None,
) -> dict:
    fields = rain_city_fields(title=title, genre=genre, outline_text=outline_text or RAIN_CITY_OUTLINE)
    return create_project(client, key=f"create-v2-{key}", **fields)


def generate_workspace_step(client, project_id: str, step_key: str, payload: dict | None = None) -> dict:
    response = post_generate(client, project_id, step_key, payload, key=intent_key(f"generate-v2-{project_id}-{step_key}"))
    assert response.status_code == 200, response.text
    return response.json()["data"]


def approve_workspace_step(client, project_id: str, step_key: str) -> dict:
    return approve_step(client, project_id, step_key, key=intent_key(f"approve-v2-{project_id}-{step_key}"))


def approve_generated_step(client, project_id: str, step_key: str) -> None:
    generate_workspace_step(client, project_id, step_key)
    approve_workspace_step(client, project_id, step_key)



def patch_llm_client_generate(monkeypatch, generate):
    """``LLMClient.generate_accounted`` 换成经记账钩子调 ``generate(self, request)``：回包原样交回（不补用量字段），
    只有 ``Exception`` 记成失败尝试。"""

    def generate_accounted(self, request, *, accounting_hook):  # noqa: ANN001
        handle = accounting_hook.before_dispatch(
            request=request,
            dispatch_kind="initial",
        )
        try:
            response = generate(self, request)
        except Exception as exc:
            accounting_hook.after_error(
                handle,
                request=request,
                error=exc,
                raw_response=None,
                provider_request_id=None,
                latency_ms=1,
            )
            raise
        accounting_hook.after_response(
            handle,
            request=request,
            response=response,
            latency_ms=1,
        )
        return response

    monkeypatch.setattr(
        "novel_system.services.llm_client.LLMClient.generate_accounted",
        generate_accounted,
    )


# ---------------------------------------------------------------- 经 API 按步生成并确认到某一步，键按作品 + 步固定（test_snowflake_row_identity_and_ancestry）


def create_closeout_project(client, *, key: str) -> dict:
    return create_project(client, key=f"create-closeout-{key}", **rain_city_fields())


def closeout_generate(client, project_id: str, step_key: str) -> dict:
    response = post_generate(client, project_id, step_key, key=f"gen-closeout-{project_id}-{step_key}")
    assert response.status_code == 200, response.text
    return response.json()["data"]


def closeout_approve(client, project_id: str, step_key: str) -> dict:
    return approve_step(client, project_id, step_key, key=f"app-closeout-{project_id}-{step_key}")


def workspace_payload(client, project_id: str) -> dict:
    response = client.get(f"/api/v2/projects/{project_id}/snowflake-workspace")
    assert response.status_code == 200, response.text
    return response.json()["data"]


def step_of(workspace: dict, step_key: str) -> dict:
    return next(step for step in workspace["steps"] if step["step_key"] == step_key)


def revise_and_approve(client, project_id: str, step_key: str, draft: dict) -> dict:
    """Patch a step's draft (creating a pending revision) then re-approve it."""
    patch_step(client, project_id, step_key, draft, force=False)
    return approve_step(client, project_id, step_key, key=f"reapprove-{project_id}-{step_key}-{draft.get('_rev', 'x')}")


def approve_through(client, project_id: str, last_step: str) -> None:
    for step_key in ALL_STEPS[: ALL_STEPS.index(last_step) + 1]:
        closeout_generate(client, project_id, step_key)
        closeout_approve(client, project_id, step_key)


# ---------------------------------------------------------------- 教练与要点：录下发往模型的请求、回放固定的结构化输出（test_snowflake_direction_brief）


def create_brief_project(client, key: str) -> str:
    return create_project(client, key=f"brief-{key}", title="要点之书", outline_text="作者意图要点验证用项目。")["project_id"]


def _recording_generate(captured: list, payload: dict):
    """捕获发往 LLM 的请求，回放固定 structured_output。"""

    def fake_generate(self, request):  # noqa: ANN001
        captured.append(request)
        return LLMResponse(
            request_id=f"resp_{request.node_id}_{len(captured)}",
            provider="fake-provider",
            model=request.model,
            text=json.dumps(payload, ensure_ascii=False),
            structured_output=payload,
            response_format="json_object",
            raw_response={"id": f"resp_{request.node_id}"},
            usage={"input_tokens": 10, "output_tokens": 20, "total_tokens": 30},
            finish_reason="stop",
        )

    return accounted_generate_method(fake_generate)


def coach_working_payload(request) -> dict:
    content = request.messages[-1]["content"]
    start = content.index("Working payload:\n") + len("Working payload:\n")
    end = content.index("\n\nRequired top-level JSON keys")
    return json.loads(content[start:end])


COACH_REPLY = {
    "reply": "先把主角的被动写实。",
    "suggestions": ["第一灾之前他不出手"],
    "candidate_label": "",
    "candidate_patch": {},
    "brief_update": {
        "lines": [
            {"kind": "decision", "scope": "step", "text": "主角是被动卷入，第一灾才出手"},
            {"kind": "constraint", "scope": "book", "text": "基调冷，不热血"},
            {"kind": "pending", "scope": "step", "text": "结局是否团圆"},
        ]
    },
}


def install_coach_llm(monkeypatch, captured: list, payload: dict) -> None:
    monkeypatch.setenv("NOVEL_SYSTEM_LLM_ENABLED", "true")
    monkeypatch.setattr("novel_system.services.llm_client.LLMClient.generate_accounted", _recording_generate(captured, payload))


# ---------------------------------------------------------------- 直接换掉雪花生成的记账调用：按请求现编回包（test_snowflake_method_contract 等）


def install_snowflake_llm(monkeypatch, responder):
    """雪花生成走 ``responder(request) -> LLMResponse``，不经真实服务商、不写记账行。

    记账父行由这个桩件跳过了，所以清洗失败的标记路径（``mark_postprocess_failure``）也换成空操作，免得它反过来把
    真实错误吃掉——场景规划整表生成会分批派发（见 test_snowflake_scene_details_batching），只认一个场景的假模型
    必然让后续批次走到这条路径。"""
    from novel_system.services import snowflake_workspace_llm as mod

    monkeypatch.setattr(
        mod, "execute_accounted_call", lambda session, client, request, context, *, llm_call_id: responder(request)
    )
    monkeypatch.setattr(mod, "mark_postprocess_failure", lambda session, llm_call_id, **kwargs: None)
    monkeypatch.setattr(mod.SnowflakeWorkspaceLLMService, "_llm_enabled", lambda self: True)
    monkeypatch.setattr(mod.SnowflakeWorkspaceLLMService, "_client", lambda self: object())
    monkeypatch.setattr(mod, "supplement_accounted_call", lambda session, llm_call_id, **kwargs: None)


def working_payload_of(request) -> dict:
    """提示词里 ``Working payload:`` 那一段 JSON（各条消息拼起来找）。"""
    prompt = "\n".join(str(m.get("content", "")) for m in request.messages)
    return json.loads(prompt.split("Working payload:\n", 1)[1].rsplit("\n\nRequired top-level", 1)[0])


def llm_payload_response(payload: dict) -> LLMResponse:
    return LLMResponse(
        request_id="r",
        provider="p",
        model="m",
        text=json.dumps(payload, ensure_ascii=False),
        structured_output=payload,
        response_format="json_object",
        raw_response={},
        usage={},
        finish_reason="stop",
    )


def seed_synopsis_project(session, project_id: str) -> None:
    session.add(
        StoryProject(
            project_id=project_id,
            title="五段契约",
            outline_text="五段契约大纲",
            planning_mode="snowflake",
            snowflake_workflow_mode="explore",
            target_word_count=100000,
        )
    )
    session.flush()
