"""风格参考 v3（P5b，V6）—「对照检查」作业：一段文字（或一场的当前正文）像不像参考作者。

取代旧校验层（「回测」：量化回测分不清两位作者、同步裁决不会失败、禁用词为空、语义评审从未运行、失败被吞）。
一次检查 = 作业表上一个 ``kind=check`` 作业（``POST /api/v2/style-reference/checks`` 建，作业工人跑）：

1. **读数**（确定性）：测量核特征对作者自己窗口分布的 ``distance`` / ``percentile`` / 越界特征 / 按维分；
2. **参考评审**（一次模型调用，模板 ``style_reference_judge``，走现有的 ``soft_qc`` 节点路由——与起草流程里的软 QC
   参考评审是同一份模板、同一种解析，2026-10-14 评审统一）：评审口径的 ``[STYLE_REFERENCE]`` 块（冻结选窗的前 4 窗
   样例 + 文风卡 + 声音 + 红线）+ 待查的文字 → 16 维各 0–10 分、每维一句说明与改法；总分由代码算，分数按这份画像
   的评审校准折算（``judge_calibration``；没校准就是模型原始分，读数里标明口径）；
3. **抄袭门**（``reference_copy_gate``）：只记旗标与计数；
4. 一条 ``style_fidelity_readings``（``source=manual_check``，``stage=manual``），作业结果 ``{reading_id, ...}``。

严格 LLM：没有可用模型 → 建作业时就 409 ``STYLE_REFERENCE_LLM_REQUIRED``；评审调用失败 / 没给出分数 → 作业失败
（错误码原样），不静默降级成只有读数的检查。

评审的参考块按 ``soft_qc`` 节点的实际路由判云策略（H1，渲染请求带 ``node_ids``：「仅本机」的书遇云端的 soft_qc
路由 → 作业以 409 ``STYLE_REFERENCE_CLOUD_POLICY_BLOCKED`` 失败，参考一个字都不发），并按评审模板的输入预算压
（L7，与管线同一口径：``NOVEL_SYSTEM_SCENE_INPUT_TOKEN_BUDGET`` 收紧时照收紧）。

**作业所有权**：每个进度写之后立刻提交（不带着 SQLite 的写锁去建窗口索引、跑抄袭门、调模型）；进度写 / 结束写
落空（被取消、被清扫重排、书被删）→ ``JobLost``，框架回滚——评审回来之后先 ``check_continue`` 再记读数，
``succeed`` 落空就连读数一起回滚，旧工人不会在别人的作业上留下一条读数。检查点走 ``job_runtime.JobRun``
（与分类 / 学习共用）。

**取消**（2026-09-24 §8 C1）：``cancel_check_job`` / ``POST …/checks/{job_id}/cancel``——排队中 / 心跳过期的作业在请求里
直接收尾为 cancelled，运行中的置取消标记、工人在下一个 ``check_continue`` 处停（评审回来也不记读数）；已结束的
409 ``STYLE_REFERENCE_CHECK_NOT_ACTIVE``。

**评审分的刻度**：按模板 ``structured_schema`` 声明的刻度逐个换算（越界的分丢掉、不夹），见 ``judge.parse_judge_output``。
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from types import SimpleNamespace
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from novel_system.db.models import (
    AuthorDraft,
    FinalScene,
    SceneCard,
    SceneDraft,
    SceneRunState,
    StyleReferenceBook,
    StyleReferenceJob,
    StyleReferenceProfile,
    StyleFidelityReading,
)
from novel_system.services.errors import DomainError
from novel_system.services.llm_accounting import LLMCallContext, execute_accounted_call
from novel_system.services.manuscript_html import plain_manuscript_text
from novel_system.services.style_reference import readings
from novel_system.services.style_reference.errors import LLMRequiredError, profile_not_found
from novel_system.services.style_reference.job_runtime import JobRun
from novel_system.services.style_reference.jobs import (
    JOB_KIND_CHECK,
    TERMINAL_STATES,
    ClaimedJob,
    JobLost,
    StyleJobService,
    job_activity_entry,
)
from novel_system.services.style_reference.judge import JUDGE_NODE_ID, JUDGE_TEMPLATE, judge_record, judge_user_prompt
from novel_system.services.style_reference.judge_calibration import (
    call_judge,
    judge_input_budget,
    profile_policy,
    scope_for,
    verdict_for,
)
from novel_system.services.style_reference.llm_nodes import NodeConfigUnavailable, NodeRuntime, load_node_runtimes
from novel_system.services.style_reference.policy import ensure_cloud_llm_allowed
from novel_system.services.style_reference.untrusted_data import UNTRUSTED_SYSTEM_INSTRUCTION
from novel_system.services.scene_lookup import get_scene_or_404

logger = logging.getLogger(__name__)

CHECK_NODE_ID = JUDGE_NODE_ID
CHECK_TEMPLATE = JUDGE_TEMPLATE
CHECK_STEP = "style_reference_judge"
CHECK_MAX_TEXT_CHARS = 60_000

CHECK_TARGET_INVALID_CODE = "STYLE_REFERENCE_CHECK_TARGET_INVALID"
CHECK_NOT_BOUND_CODE = "STYLE_REFERENCE_CHECK_NOT_BOUND"
CHECK_NO_TEXT_CODE = "STYLE_REFERENCE_CHECK_NO_TEXT"
CHECK_CONFIG_MISSING_CODE = "STYLE_REFERENCE_CHECK_CONFIG_MISSING"
CHECK_NO_REFERENCE_CODE = "STYLE_REFERENCE_CHECK_NO_REFERENCE"
CHECK_REFERENCE_EMPTY_CODE = "STYLE_REFERENCE_CHECK_REFERENCE_EMPTY"
CHECK_JUDGE_FAILED_CODE = "STYLE_REFERENCE_CHECK_JUDGE_FAILED"
CHECK_NOT_FOUND_CODE = "STYLE_REFERENCE_CHECK_NOT_FOUND"
CHECK_NOT_ACTIVE_CODE = "STYLE_REFERENCE_CHECK_NOT_ACTIVE"

TARGET_TEXT = "text"
TARGET_SCENE = "scene"
POLICY_MODE_CHECK = "check"


def resolve_check_client() -> tuple[Any | None, bool]:
    """按**当前**运行时配置取 LLM 客户端（请求时查一次、作业开工时再取一次）；测试在这里打桩。"""
    from novel_system.services.llm_service_base import runtime_llm_client_and_enabled

    return runtime_llm_client_and_enabled()


# ---------------------------------------------------------------------------
# 目标：文字 + 策略
# ---------------------------------------------------------------------------


def scene_current_text(session: Session, scene_id: str) -> tuple[str, str | None]:
    """一场的当前正文与它的出处：当前终稿 → 最近的有效稿 / 风格稿 / 首稿 → 作者稿（HTML 原样，测量核按编辑器段切）。"""
    state = session.get(SceneRunState, scene_id)
    if state is not None and state.current_final_scene_row_id:
        final = session.get(FinalScene, state.current_final_scene_row_id)
        if final is not None and final.scene_id == scene_id and (final.content or "").strip():
            return final.content, f"final_scene:{final.row_id}"
    if state is not None:
        for row_id in (
            state.latest_valid_draft_row_id,
            state.current_style_draft_row_id,
            state.current_neutral_draft_row_id,
        ):
            if not row_id:
                continue
            draft = session.get(SceneDraft, row_id)
            if draft is not None and draft.scene_id == scene_id and (draft.content or "").strip():
                return draft.content, f"scene_draft:{draft.row_id}"
    author = session.execute(
        select(AuthorDraft).where(
            AuthorDraft.object_type == "scene",
            AuthorDraft.object_id == scene_id,
            AuthorDraft.status == "current",
        )
    ).scalars().first()
    if author is not None and (author.content or "").strip():
        return author.content, f"author_draft:{author.draft_id}:rev{int(author.revision_no or 0)}"
    return "", None


def _light_policy(session: Session, params: Mapping[str, Any]) -> Any:
    """建作业时用的轻量策略（只查几列，不冻结契约）：确认绑没绑、参考的是哪本书。"""
    from novel_system.services.style_policy import UNBOUND, style_policy_live

    if params.get("profile_id"):
        profile = session.get(StyleReferenceProfile, str(params["profile_id"]))
        if profile is None:
            raise profile_not_found(params['profile_id'])
        return SimpleNamespace(bound=True, profile_id=profile.profile_id, book_id=profile.book_id)
    if params.get("scene_id"):
        scene = session.get(SceneCard, str(params["scene_id"]))
        return style_policy_live(session, scene, freeze_contract=False) if scene is not None else UNBOUND
    if params.get("project_id"):
        return style_policy_live(session, scope_for(str(params["project_id"])), freeze_contract=False)
    return UNBOUND


def _full_policy(session: Session, params: Mapping[str, Any]) -> tuple[Any, Any]:
    """作业里用的完整策略（冻结契约，渲染参考要用）与渲染用的作用域（场景行或只带作品的作用域）。"""
    from novel_system.services.style_policy import style_policy_live

    if params.get("profile_id"):
        scene = session.get(SceneCard, str(params["scene_id"])) if params.get("scene_id") else None
        return profile_policy(session, str(params["profile_id"])), scene or scope_for(params.get("project_id"))
    if params.get("scene_id"):
        scene = get_scene_or_404(session, str(params["scene_id"]))
        return style_policy_live(session, scene), scene
    scope = scope_for(str(params.get("project_id") or "") or None)
    return style_policy_live(session, scope), scope


def _not_bound_error(params: Mapping[str, Any]) -> DomainError:
    return DomainError(
        CHECK_NOT_BOUND_CODE,
        "这段文字没有可对照的参考：这一场 / 这部作品还没有绑定参考书的文风画像，或者给一个画像再检查。",
        status_code=409,
        details={
            "scene_id": params.get("scene_id"),
            "project_id": params.get("project_id"),
            "author_action": {"action": "bind_style_profile", "view": "styleref", "label": "去绑定参考书的文风画像"},
        },
    )


def _judge_runtime() -> NodeRuntime:
    """评审节点的路由（``soft_qc``）与模板（``style_reference_judge``）；读不出来 / 没有路由 / 模板不在 → 409。
    建作业时查一次，作业开工时再载一次。"""
    try:
        load = load_node_runtimes((CHECK_NODE_ID,), template_names={CHECK_NODE_ID: CHECK_TEMPLATE})
    except NodeConfigUnavailable as exc:
        raise DomainError(
            CHECK_CONFIG_MISSING_CODE,
            "对照检查的评审节点（文学质检 soft_qc）或它的提示词模板读不出来。",
            status_code=409,
            details={"reason": type(exc.cause).__name__},
        ) from exc.cause
    if load.missing_routes:
        raise DomainError(
            CHECK_CONFIG_MISSING_CODE,
            "对照检查的评审节点（文学质检 soft_qc）或它的提示词模板读不出来。",
            status_code=409,
            details={"reason": "route_missing", "node_id": CHECK_NODE_ID},
        )
    runtime = load.runtimes.get(CHECK_NODE_ID)
    if runtime is None:
        # 提示词只读仓库的 config/prompts.yaml：模板缺了只能是程序的问题（CI 的模板清点先拦），作者在设置里补不了
        raise DomainError(
            CHECK_CONFIG_MISSING_CODE,
            "对照检查的评审节点（文学质检 soft_qc）或它的提示词模板读不出来。",
            status_code=409,
            details={"reason": "template_missing", "node_id": CHECK_NODE_ID, "template": CHECK_TEMPLATE},
        )
    return runtime


# ---------------------------------------------------------------------------
# 建作业
# ---------------------------------------------------------------------------


def start_check_job(
    session: Session,
    *,
    text: str | None = None,
    scene_id: str | None = None,
    profile_id: str | None = None,
    project_id: str | None = None,
    op_key: str | None = None,
    llm_client: Any | None = None,
    llm_enabled: bool | None = None,
) -> StyleReferenceJob:
    """建一个对照检查作业（调用方提交后派发）。

    ``text`` 与 ``scene_id`` 恰好给一个：``scene_id`` → 这一场的当前正文与它现在的绑定（给了 ``profile_id`` 时改按
    那份画像）；读数记在**这一场所属的作品**名下（``project_id`` 按场景本身定，客户端给的不用）；``text`` → 给定的
    画像，或给定作品（``project_id``）当前的绑定。
    """
    has_text = bool(str(text or "").strip())
    has_scene = bool(str(scene_id or "").strip())
    if has_text == has_scene:
        raise DomainError(
            CHECK_TARGET_INVALID_CODE,
            "对照检查要么给一段文字（text），要么给一场（scene_id），二选一。",
            status_code=400,
        )
    if has_text and not (profile_id or project_id):
        raise DomainError(
            CHECK_TARGET_INVALID_CODE,
            "检查一段文字时要说对照哪份参考：给画像（profile_id）或作品（project_id，用它当前的绑定）。",
            status_code=400,
        )
    if has_text and len(str(text)) > CHECK_MAX_TEXT_CHARS:
        raise DomainError(
            CHECK_TARGET_INVALID_CODE,
            f"一次最多检查 {CHECK_MAX_TEXT_CHARS} 字；把文字分段检查。",
            status_code=400,
            details={"max_chars": CHECK_MAX_TEXT_CHARS},
        )
    if llm_client is None and llm_enabled is None:
        llm_client, llm_enabled = resolve_check_client()
    enabled = bool(llm_enabled) if llm_enabled is not None else llm_client is not None
    if not enabled or llm_client is None:
        raise LLMRequiredError(operation="style_check")
    params: dict[str, Any] = {
        "target": TARGET_TEXT if has_text else TARGET_SCENE,
        "profile_id": str(profile_id) if profile_id else None,
        "project_id": str(project_id) if project_id else None,
        "scene_id": str(scene_id) if has_scene else None,
    }
    if has_scene:
        scene = get_scene_or_404(session, str(scene_id))
        # 一场属于哪部作品只看场景本身，不信客户端给的 project_id：界面换过作品时带来的是另一部作品的 id，
        # 读数（与记账）会记到别的作品名下，进了那部作品的走势与按维平均
        params["project_id"] = readings.scene_project_id(session, scene)
        content, _ref = scene_current_text(session, scene.scene_id)
        if not content.strip():
            raise DomainError(
                CHECK_NO_TEXT_CODE,
                "这一场还没有正文可检查（没有终稿、草稿或作者稿）。",
                status_code=409,
                details={"scene_id": scene.scene_id},
            )
    else:
        params["text"] = str(text)
    light = _light_policy(session, params)
    if not getattr(light, "bound", False) or not getattr(light, "book_id", None):
        raise _not_bound_error(params)
    _judge_runtime()
    ensure_cloud_llm_allowed(
        session.get(StyleReferenceBook, str(light.book_id)),
        operation="style_check",
        node_ids=(CHECK_NODE_ID,),
        llm_client=llm_client,
    )
    job = StyleJobService(session).create(
        JOB_KIND_CHECK,
        book_id=str(light.book_id),
        profile_id=str(getattr(light, "profile_id", "") or "") or None,
        op_key=op_key or None,
        params=params,
        phase="queued",
        allow_parallel=True,
    )
    job.progress_json = {"phase": "queued", "phase_label": "排队中", "done": 0, "total": 3}
    session.flush()
    return job


def check_job_or_404(session: Session, job_id: str) -> StyleReferenceJob:
    job = session.get(StyleReferenceJob, str(job_id))
    if job is None or job.kind != JOB_KIND_CHECK:
        raise DomainError(CHECK_NOT_FOUND_CODE, f"style check {job_id!r} not found", status_code=404)
    return job


def cancel_check_job(session: Session, job_id: str) -> StyleReferenceJob:
    """取消一个对照检查作业（与分类 / 学习的取消同形）：排队中 / 心跳过期的在这里直接收尾为 cancelled，运行中的置
    取消标记、工人在下一个 ``check_continue`` 处停；已结束的 409 ``STYLE_REFERENCE_CHECK_NOT_ACTIVE``；没有 404。"""
    job = check_job_or_404(session, job_id)
    if job.state in TERMINAL_STATES:
        raise DomainError(
            CHECK_NOT_ACTIVE_CODE,
            "这次对照检查已经结束（完成、失败或已取消），没有可取消的。",
            status_code=409,
            details={"job_id": job.job_id, "state": job.state},
        )
    return StyleJobService(session).request_cancel(job.job_id)


# ---------------------------------------------------------------------------
# 参考评审
# ---------------------------------------------------------------------------


def _judge_base_system(template: Any) -> str:
    return str(template.system_prompt or "").rstrip() + "\n\n" + UNTRUSTED_SYSTEM_INSTRUCTION


def run_reference_judge(
    session: Session,
    *,
    policy: Any,
    scope: Any,
    text: str,
    llm_client: Any,
    context_scope_id: str,
    project_id: str | None,
) -> dict[str, Any]:
    """一次参考评审调用 → 评审记录（``judge.judge_record``：按校准折算的按维分与总分、原始分、口径）；渲染不出参考 /
    调用失败 / 没给分数 → ``DomainError``（作业失败）。"""
    from novel_system.services.style_prompt_injection import (
        PLACEMENT_SYSTEM,
        ROLE_REVIEW,
        style_render_request_for_scene,
    )
    from novel_system.services.style_reference.inject.fit import fit_rendered
    from novel_system.services.style_reference.inject.render import render_style

    runtime = _judge_runtime()
    template = runtime.template
    # 参考块按 soft_qc 的实际路由判云策略（H1）：「仅本机」的书遇云端路由 → 409，参考一个字都不渲染
    request = style_render_request_for_scene(
        session, scope, policy, role=ROLE_REVIEW, placement=PLACEMENT_SYSTEM, node_ids=(CHECK_NODE_ID,)
    )
    rendered = render_style(session, policy, request, scene=scope if getattr(scope, "scene_id", None) else None)
    # 选窗 / 窗口索引可能刚写了库：记账用自己的会话，发调用之前先把这边的写提交掉（SQLite 一次只有一个写者）
    session.commit()
    if rendered.empty or not rendered.system_prefix:
        raise DomainError(
            CHECK_REFERENCE_EMPTY_CODE,
            "这份画像渲染不出可对照的参考（没有样例窗口也没有文风卡）：先让这本书「学习文风」。",
            status_code=409,
            details={"profile_id": getattr(policy, "profile_id", None)},
        )
    # L7：评审提示也按模板的输入预算压（整窗 / 整句地去，红线不截；与管线同一口径）
    rendered, budget_fit = fit_rendered(
        rendered,
        base_system_prompt=_judge_base_system(template),
        user_prompt=judge_user_prompt(template.task_prompt, text),
        target_input_tokens=judge_input_budget(template),
    )
    if rendered.empty or not rendered.system_prefix:
        raise DomainError(
            CHECK_REFERENCE_EMPTY_CODE,
            "待查的文字太长，装不下任何参考：把文字分段再检查，或放宽评审节点的输入预算。",
            status_code=409,
            details={
                "profile_id": getattr(policy, "profile_id", None),
                "reason": "input_budget",
                "target_input_tokens": budget_fit.get("target_input_tokens"),
            },
        )
    scores, llm_call_id = call_judge(
        llm_client,
        runtime,
        system_prefix=rendered.system_prefix,
        text=text,
        context=LLMCallContext(
            scope_type="style_reference_check",
            scope_id=str(context_scope_id),
            node_id=CHECK_NODE_ID,
            step=CHECK_STEP,
            project_id=project_id,
        ),
        execute=execute_accounted_call,
        failure_code=CHECK_JUDGE_FAILED_CODE,
    )
    judge = judge_record(verdict_for(session, policy, scores), source="manual_check")
    judge["llm_call_id"] = llm_call_id
    judge["window_count"] = len(rendered.window_refs)
    return judge


# ---------------------------------------------------------------------------
# 作业处理器
# ---------------------------------------------------------------------------


def run_check_job(session: Session, claimed: ClaimedJob, service: StyleJobService) -> None:
    """``check`` 作业处理器。终态由框架写（取消 / 失败没有附带状态要落，所以不走 ``JobRun.run``；失败记什么按
    ``jobs.job_failure``，与 ``JobRun`` 同一条规则），检查点
    （``check_continue`` / ``checkpoint``：进度写落空 → ``JobLost``，写成了立刻提交，不带着 SQLite 的写锁去建窗口
    索引、跑抄袭门、等模型）与分类 / 学习共用 ``JobRun``。"""
    run = JobRun(session, claimed, service)
    params = dict(claimed.params or {})
    run.check_continue()
    run.checkpoint(phase="measure", phase_label="读数", done=0, total=3)
    policy, scope = _full_policy(session, params)
    if not getattr(policy, "bound", False):
        raise _not_bound_error(params)
    scene_id = params.get("scene_id")
    project_id = params.get("project_id")
    if scene_id:
        # 作品按场景本身定（修正之前建的作业，参数里可能还是客户端给的另一部作品）
        scene_row = get_scene_or_404(session, str(scene_id))
        project_id = readings.scene_project_id(session, scene_row)
        text, text_ref = scene_current_text(session, str(scene_id))
        if not text.strip():
            raise DomainError(CHECK_NO_TEXT_CODE, "这一场还没有正文可检查。", status_code=409, details={"scene_id": scene_id})
    else:
        text, text_ref = str(params.get("text") or ""), None
    # 作者稿是编辑器 HTML：读数照原样测（测量核按编辑器段切），评审与抄袭门看可见正文
    visible_text = plain_manuscript_text(text) if "<" in text and ">" in text else text
    reading = readings.reading_for_text(session, policy, text)
    if reading is None:
        raise DomainError(
            CHECK_NO_REFERENCE_CODE,
            "参考书还没有可用的读数尺子（没有样例窗口）：等段落分类完成、学习文风之后再检查。",
            status_code=409,
            details={"book_id": getattr(policy, "book_id", None)},
        )
    from novel_system.services.reference_copy_gate import check_reference_copy

    copy_check = check_reference_copy(session, visible_text, policy=policy)
    run.check_continue()
    run.checkpoint(phase="judge", phase_label="参考评审", done=1, total=3)
    llm_client, llm_enabled = resolve_check_client()
    if not llm_enabled or llm_client is None:
        raise LLMRequiredError(operation="style_check")
    judge = run_reference_judge(
        session,
        policy=policy,
        scope=scope,
        text=visible_text,
        llm_client=llm_client,
        context_scope_id=claimed.job_id,
        project_id=project_id,
    )
    # 评审可能走了很久：期间作业被取消 / 被清扫重排给别的工人 / 书被删了——先确认还是自己的，再记读数
    run.check_continue()
    run.checkpoint(phase="record", phase_label="记录读数", done=2, total=3, llm_calls_delta=1)
    row = readings.record_fidelity_reading(
        session,
        policy=policy,
        text=text,
        source=readings.SOURCE_MANUAL_CHECK,
        stage=readings.STAGE_MANUAL,
        scene_id=str(scene_id) if scene_id else None,
        project_id=project_id,
        draft_ref=text_ref,
        judge=judge,
        copy_check=copy_check,
        reading=reading,
        strict=True,
    )
    if row is None:  # pragma: no cover — 上面已确认绑定且读得出
        raise DomainError(CHECK_NO_REFERENCE_CODE, "读数没有记下来。", status_code=409)
    # 读数与「完成」同一个事务：结束写落空就连读数一起回滚（JobLost → 框架 rollback）
    run.checkpoint(commit=False, phase="done", phase_label="完成", done=3, total=3)
    succeeded = service.succeed(
        claimed,
        {
            "reading_id": row.reading_id,
            "percentile": row.percentile,
            "distance": row.distance,
            "within_range": bool((row.reading_json or {}).get("within_range")),
            "judge_overall": judge.get("overall"),
            "copy_blocked": bool(getattr(copy_check, "blocked", False)),
        },
    )
    if not succeeded:
        raise JobLost(claimed.job_id)


def check_job_payload(session: Session, job: StyleReferenceJob) -> dict[str, Any]:
    """作业条目 + 读数（作业成功后）。"""
    reading = None
    result = dict(job.result_json or {})
    if result.get("reading_id"):
        reading = readings.reading_payload(session.get(StyleFidelityReading, str(result["reading_id"])))
    return {"job": job_activity_entry(job), "reading": reading}


def check_never_resumes(_job: StyleReferenceJob) -> bool:
    """对照检查没有续跑:失败 / 取消了就「重新检查」(新作业),活动条目不给「继续」。"""
    return False


def finished_check_params(params: Mapping[str, Any]) -> dict[str, Any]:
    """对照检查作业结束(成功 / 失败 / 取消)时的参数:送检的原文(至多 6 万字)不再留在作业行上,只留哈希与字数
    ——读数表本来就只存 ``text_sha256``(同一个哈希),检查作业从不续跑,结束后没人再读这段文字。"""
    finished = dict(params)
    text = finished.pop("text", None)
    if isinstance(text, str) and text:
        finished["text_sha256"] = readings.text_sha256(text)
        finished["text_chars"] = len(text)
    return finished



__all__ = [
    "CHECK_CONFIG_MISSING_CODE",
    "CHECK_JUDGE_FAILED_CODE",
    "CHECK_MAX_TEXT_CHARS",
    "CHECK_NODE_ID",
    "CHECK_NOT_ACTIVE_CODE",
    "CHECK_NOT_BOUND_CODE",
    "CHECK_NOT_FOUND_CODE",
    "CHECK_NO_REFERENCE_CODE",
    "CHECK_NO_TEXT_CODE",
    "CHECK_REFERENCE_EMPTY_CODE",
    "CHECK_TARGET_INVALID_CODE",
    "CHECK_TEMPLATE",
    "cancel_check_job",
    "check_job_or_404",
    "check_job_payload",
    "resolve_check_client",
    "check_never_resumes",
    "finished_check_params",
    "run_check_job",
    "run_reference_judge",
    "scene_current_text",
    "start_check_job",
]
