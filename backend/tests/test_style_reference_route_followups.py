"""风格参考 v3 复核（H1 的跟进）：云策略按接收提示的节点判，注入适配器之外的几处也要守住。

- 写作台（深评 / 局部补丁）、作者稿建议、场景蓝图包着注入适配器的 ``except Exception``：云策略判「不许送」的 409
  必须原样抛出，不能被吞掉换成一份没有参考的提示（作者看不到原因）；其余渲染失败照旧回退基础提示；
- bundle 里由参考书派生的段（叙事机制指引、参考尺度）会进读 bundle 的每一个节点：「仅本机」的书遇到云端路由时
  这些段不进 bundle；本机路由 / 送云策略的书照旧；
- 对照检查的评审预算与管线同一张表（``prompt_builder.default_input_token_budget``）；
- 规划调用点说清是哪个节点收参考（章规划四个节点 / 雪花步骤生成 / 场景蓝图）。

全部是合成内容。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from novel_system.db.models import (
    StyleReferenceBook,
    StyleReferenceInjectionBinding,
    StyleReferenceProfile,
    StyleReferenceRun,
)
from novel_system.services.bundle_builder import BundleBuilder
from novel_system.services.style_reference import policy as policy_module
from novel_system.services.style_reference.errors import CloudPolicyBlockedError
from tests.test_scene_blueprint import PROJECT_ID, SCENE_ID, _seed_scene


def _blocked(*_args, **_kwargs):
    raise CloudPolicyBlockedError(book_id="sr_book_route", operation="style_reference_draft", node_id="style_draft")


def _seed_binding(session, *, cloud_policy: str) -> None:
    session.add(
        StyleReferenceBook(
            book_id="sr_book_route",
            title="合成书",
            source_kind="path",
            cloud_policy=cloud_policy,
            text_checksum="checksum-route",
            stats_json={"rights_declaration": {"declared": True, "send_rights": True}},
        )
    )
    session.add(StyleReferenceRun(run_id="sr_run_route", book_id="sr_book_route", status="done"))
    session.add(
        StyleReferenceProfile(
            profile_id="sr_profile_route",
            book_id="sr_book_route",
            run_id="sr_run_route",
            title="合成画像",
            status="active",
            profile_json={"narrative_guidance": ["关键信息放段首一次给出", "收场落在动作上"]},
        )
    )
    session.add(
        StyleReferenceInjectionBinding(
            binding_id="sr_bind_route",
            profile_id="sr_profile_route",
            scope="project",
            scope_ref_id=PROJECT_ID,
            task_type="scene_generation",
            strategy="mixed",
            config_json={"draft_mode": "style_first"},
            status="active",
        )
    )
    session.commit()


# ---------------------------------------------------------------------------
# bundle 里由参考书派生的段
# ---------------------------------------------------------------------------


def test_bundle_withholds_book_derived_sections_for_a_local_only_book_on_cloud_routes(session) -> None:
    _seed_scene(session)
    _seed_binding(session, cloud_policy="local_only")
    snapshot = BundleBuilder(session).build(SCENE_ID)["snapshot"]
    refs = snapshot["source_version_refs"]
    assert refs["style_reference_bundle_sections"].startswith("withheld:")
    assert "style_narrative_guidance" not in snapshot["inline_digests"]
    assert "_style_reference_scene_scale" not in snapshot["inline_digests"]
    assert not any(item.get("slot") == "style_narrative_guidance" for item in snapshot["ordered_injections"])


def test_bundle_keeps_the_sections_when_every_reading_node_is_local(session, monkeypatch) -> None:
    monkeypatch.setattr(policy_module, "_route_locality", lambda nodes, client: (True, None))
    _seed_scene(session)
    _seed_binding(session, cloud_policy="local_only")
    snapshot = BundleBuilder(session).build(SCENE_ID)["snapshot"]
    assert "style_reference_bundle_sections" not in snapshot["source_version_refs"]
    assert "关键信息放段首一次给出" in snapshot["inline_digests"]["style_narrative_guidance"]


def test_bundle_keeps_the_sections_for_a_cloud_policy_book(session) -> None:
    _seed_scene(session)
    _seed_binding(session, cloud_policy="segments_only")
    snapshot = BundleBuilder(session).build(SCENE_ID)["snapshot"]
    assert "style_reference_bundle_sections" not in snapshot["source_version_refs"]
    assert "收场落在动作上" in snapshot["inline_digests"]["style_narrative_guidance"]


# ---------------------------------------------------------------------------
# 包着适配器的 except Exception：云策略的 409 原样抛出，其余失败照旧回退
# ---------------------------------------------------------------------------


def _writer_service(session):
    from novel_system.services.writer_deep_review import WriterDeepReviewService

    service = object.__new__(WriterDeepReviewService)
    service.session = session
    return service


def _author_service(session):
    from novel_system.services.author_drafts import AuthorDraftService

    service = object.__new__(AuthorDraftService)
    service.session = session
    return service


def _blueprint_service(session):
    from novel_system.services.scene_blueprint import SceneBlueprintService

    service = object.__new__(SceneBlueprintService)
    service.session = session
    return service


def _call(kind: str, session):
    prompt = {"template_name": "x", "system_prompt": "基础", "user_prompt": "正文"}
    if kind == "writer":
        return _writer_service(session)._inject_style_reference_prefix(
            prompt,
            object_type="scene",
            object_id="S1",
            chapter_id=None,
            scene_id="S1",
            context_text=None,
            final_user_prompt="正文",
        )
    if kind == "author":
        return _author_service(session)._inject_style_reference_prefix(
            prompt, {"scene_id": "S1", "object_type": "scene", "object_id": "S1"}, context_text=None, final_user_prompt="正文"
        )
    return _blueprint_service(session)._inject_style_reference_prefix(
        prompt, SimpleNamespace(scene_id="S1"), {"style_runtime_contract": {"contract_hash": "h"}}, final_user_prompt="正文"
    )


@pytest.mark.parametrize(
    "kind, module",
    [
        ("writer", "novel_system.services.writer_deep_review"),
        ("author", "novel_system.services.author_drafts"),
        ("blueprint", "novel_system.services.scene_blueprint"),
    ],
)
def test_adapter_wrappers_let_the_cloud_policy_refusal_through(session, monkeypatch, kind: str, module: str) -> None:
    monkeypatch.setattr(f"{module}.inject_style_reference_prefix", _blocked)
    if kind != "blueprint":
        monkeypatch.setattr(f"{module}.resolve_style_scope", lambda *_a, **_k: SimpleNamespace(scene_id="S1"))
    with pytest.raises(CloudPolicyBlockedError):
        _call(kind, session)

    def broken(*_args, **_kwargs):
        raise RuntimeError("render failed")

    monkeypatch.setattr(f"{module}.inject_style_reference_prefix", broken)
    assert _call(kind, session)["system_prompt"] == "基础"


# ---------------------------------------------------------------------------
# 对照检查的评审预算与管线同一张表
# ---------------------------------------------------------------------------


def test_check_judge_budget_comes_from_the_shared_table(monkeypatch) -> None:
    from novel_system.services import prompt_builder
    from novel_system.services.style_reference.check_job import CHECK_TEMPLATE, judge_input_budget

    template = prompt_builder.load_prompt_templates()[CHECK_TEMPLATE]
    assert CHECK_TEMPLATE in prompt_builder.RUNTIME_MIN_INPUT_BUDGETS
    monkeypatch.delenv(prompt_builder.SCENE_INPUT_TOKEN_BUDGET_ENV, raising=False)
    assert judge_input_budget(template) == prompt_builder.default_input_token_budget(template)
    assert judge_input_budget(template) >= prompt_builder.RUNTIME_MIN_INPUT_BUDGETS[CHECK_TEMPLATE]
    monkeypatch.setenv(prompt_builder.SCENE_INPUT_TOKEN_BUDGET_ENV, "9000")
    assert judge_input_budget(template) == 9000


# ---------------------------------------------------------------------------
# 规划调用点说清是哪个节点收参考
# ---------------------------------------------------------------------------


def test_planning_call_sites_name_the_receiving_nodes(session, monkeypatch) -> None:
    from novel_system.services import chapter_planning_context, snowflake_workspace_llm

    seen: list[tuple[str, tuple[str, ...]]] = []

    def capture(tag):
        def _resolve(_session, _project_id, *, node_ids=None, **_kwargs):
            seen.append((tag, tuple(node_ids or ())))
            return None

        return _resolve

    monkeypatch.setattr(chapter_planning_context, "resolve_project_style_reference", capture("chapter"))
    monkeypatch.setattr(snowflake_workspace_llm, "resolve_project_style_reference", capture("snowflake"))
    builder = object.__new__(chapter_planning_context.ChapterPlanningContextBuilder)
    builder.session = session
    assert builder._style_reference_slot("P1", {}) is None
    llm_service = object.__new__(snowflake_workspace_llm.SnowflakeWorkspaceLLMService)
    llm_service.session = session
    llm_service._style_reference_cache = {}
    assert llm_service._project_style_reference("P1") is None
    assert seen == [
        ("chapter", chapter_planning_context.CHAPTER_PLANNING_REFERENCE_NODE_IDS),
        ("snowflake", ("snowflake_step_generate",)),
    ]
