"""风格参考 · 校准参考评审（2026-10-14，作业表 ``kind=calibrate``）。

评审给的「像不像」分本身没有零点：同一个模型对同一份稿子可以给 8.6 也可以给 2.7（实库 2026-10-13）。校准让分数
有个参照——同一个评审模型、同一份评审口径的参考块，给两类文字打分：

- **原文**：这本书里几段**不在评审样例里**的原文（作者本人写的，应当接近 10）；
- **普通改写**：同一段原文让起草模型改写成通用的现代中文叙述（同样的事、同样的人、同样的先后，去掉这位作者的口吻；
  模板 ``style_reference_neutralize``，走 ``style_draft`` 节点路由——要比的正是起草模型不带参考时的默认写法；改写稿
  只用来校准，不落库）。

两类的平均分拉得开（≥ ``judge_min_separation``）→ 校准可用，之后每一次评审按每一维的两个锚点把分数折成「0 = 普通
文字，10 = 作者原文」（某一维拉不开，这一维的分不作数）；拉不开 → 这个评审模型分不清这位作者，它的分数不据以补丁、
不据以退回（``judge`` 模块）。结果存在画像 ``profile_json.judge_calibration``，带评审的「身份」（评审节点的
provider / model、评审模板版本、画像版本）——任何一项变了就算过期，回到「未校准」的口径，界面给「重新校准」。

**作业**：学习文风成功后自动排一个（:func:`start_after_learn`）；也可以在画像页手动启动
（``POST …/profiles/{id}/judge-calibration``）、取消（``…/judge-calibration/cancel``）。一次 = 锚点数 N 次改写 + 2N 段
文字各评审 ``judge_samples`` 次取中位（缺省 N = 3、3 次，共 21 次调用；改写稿偏短时那一段重试一次）；一段文字的几次
评审并发，段与段顺序跑，每段之后一个检查点；失败不续跑（重新校准从头来）。
走对照检查那条短车道，不在分类 / 学习后面排队。

**评审调用**：:func:`call_judge` 是作业侧（对照检查、校准）唯一的评审调用——同一份模板、同一种解析；起草流程里的
软 QC 走管线的节点调度器（``qc_engine.soft``），提示由 ``judge.bare_prompt`` 拼。两边发出去的提示按字相同：system 都由
``judge.review_system_prompt`` 收尾（参考块 + 模板 + 不可信数据约束），结构约束都是 ``judge.judge_response_schema``
（2026-10-20 之前起草流程少了那一句约束，同一份稿子两边拿到的提示不一样，校准只量了作业侧那一份）。
"""

from __future__ import annotations

import hashlib
import logging
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified

from novel_system.db.models import StyleReferenceBook, StyleReferenceJob, StyleReferenceProfile, utcnow
from novel_system.services.errors import DomainError
from novel_system.services.llm_accounting import LLMCallContext, execute_accounted_call
from novel_system.services.style_reference.binding_config import normalize_binding_config
from novel_system.services.style_reference.errors import LLMRequiredError, profile_not_found
from novel_system.services.style_reference.job_runtime import JobRun
from novel_system.services.style_reference.jobs import (
    JOB_KIND_CALIBRATE,
    JOB_KIND_CLASSIFY,
    TERMINAL_STATES,
    ClaimedJob,
    JobLost,
    StyleJobService,
    already_active_error,
)
from novel_system.services.style_reference.judge import (
    JUDGE_NODE_ID,
    JUDGE_TEMPLATE,
    NEUTRALIZE_NODE_ID,
    NEUTRALIZE_TEMPLATE,
    PROFILE_CALIBRATION_KEY,
    JudgeScores,
    JudgeVerdict,
    apply_calibration,
    calibration_brief,
    calibration_state,
    combine_judge_samples,
    compute_calibration,
    judge_identity,
    judge_response_schema,
    judge_user_prompt,
    neutralize_user_prompt,
    parse_judge_output,
    review_system_prompt,
    stored_calibration,
)
from novel_system.services.style_reference.llm_nodes import (
    NodeConfigUnavailable,
    NodeRuntime,
    accounted_call,
    load_node_runtimes,
)
from novel_system.services.style_reference.untrusted_data import UNTRUSTED_SYSTEM_INSTRUCTION

logger = logging.getLogger(__name__)

CALIBRATE_JUDGE_STEP = "style_reference_judge_calibration"
CALIBRATE_NEUTRALIZE_STEP = "style_reference_neutralize"
CALIBRATE_OPERATION = "judge_calibration"
# 锚点原文截到这么长（按段截）：与一场的篇幅相当，评审调用不至于比起草流程里的评审更重
ANCHOR_MAX_CHARS = 2400
ANCHOR_MIN_CHARS = 600
# 改写稿短于原文的这个比例 → 模型没好好改写（多半是截断），这次校准失败
NEUTRAL_MIN_RATIO = 0.4
MIN_ANCHORS = 2

CALIBRATION_CONFIG_MISSING_CODE = "STYLE_REFERENCE_CALIBRATION_CONFIG_MISSING"
CALIBRATION_NO_REFERENCE_CODE = "STYLE_REFERENCE_CALIBRATION_NO_REFERENCE"
CALIBRATION_NO_WINDOWS_CODE = "STYLE_REFERENCE_CALIBRATION_NO_WINDOWS"
CALIBRATION_CALL_FAILED_CODE = "STYLE_REFERENCE_CALIBRATION_CALL_FAILED"
CALIBRATION_ALREADY_ACTIVE_CODE = "STYLE_REFERENCE_CALIBRATION_ALREADY_ACTIVE"
CALIBRATION_NOT_ACTIVE_CODE = "STYLE_REFERENCE_CALIBRATION_NOT_ACTIVE"
CALIBRATION_BOOK_NOT_READY_CODE = "STYLE_REFERENCE_CALIBRATION_BOOK_NOT_READY"
JUDGE_CALL_FAILED_CODE = "STYLE_REFERENCE_JUDGE_FAILED"
# 只给画像（没有绑定）时造的检查用契约的策略模式（与对照检查的 check 同一个值）
POLICY_MODE_JUDGE = "check"


# ---------------------------------------------------------------------------
# 评审的身份与此刻的口径
# ---------------------------------------------------------------------------


def _config_missing(reason: str, **details: Any) -> DomainError:
    return DomainError(
        CALIBRATION_CONFIG_MISSING_CODE,
        "参考评审（文学质检 soft_qc）或改写节点（风格稿 style_draft）的路由或提示词模板读不出来：先在系统配置里补齐模型路由。",
        status_code=409,
        details={
            "reason": reason,
            **details,
            "author_action": {"action": "open_system_config", "view": "settings", "label": "去系统配置补齐模型路由"},
        },
    )


def load_runtimes(*, neutralize: bool) -> dict[str, NodeRuntime]:
    """评审（soft_qc 路由 + ``style_reference_judge``）与（校准时）改写（style_draft 路由 + 改写模板）；缺了 → 409。"""
    nodes = (JUDGE_NODE_ID, NEUTRALIZE_NODE_ID) if neutralize else (JUDGE_NODE_ID,)
    try:
        load = load_node_runtimes(
            nodes, template_names={JUDGE_NODE_ID: JUDGE_TEMPLATE, NEUTRALIZE_NODE_ID: NEUTRALIZE_TEMPLATE}
        )
    except NodeConfigUnavailable as exc:
        raise _config_missing(type(exc.cause).__name__) from exc.cause
    if load.missing_routes:
        raise _config_missing("route_missing", node_ids=load.missing_routes)
    if load.missing_templates:
        raise _config_missing("template_missing", node_ids=load.missing_templates)
    return dict(load.runtimes)


def judge_runtime_or_none() -> NodeRuntime | None:
    """评审节点此刻的路由与模板；说不出（没配路由 / 配置读不出）→ None（不报错：读口径的地方不该因此失败）。"""
    try:
        load = load_node_runtimes((JUDGE_NODE_ID,), template_names={JUDGE_NODE_ID: JUDGE_TEMPLATE})
    except NodeConfigUnavailable:
        return None
    return load.runtimes.get(JUDGE_NODE_ID)


def identity_for(profile: StyleReferenceProfile, runtime: NodeRuntime | None) -> dict[str, str] | None:
    if runtime is None:
        return None
    return judge_identity(
        route_key=runtime.route_key, template_version=runtime.prompt_version, profile_version=profile.version_tag
    )


@dataclass(frozen=True)
class JudgeContext:
    """一份画像此刻的评审口径：存着的校准、此刻的评审身份、两者合起来的状态（``judge.STATE_*``）。"""

    calibration: Mapping[str, Any] | None
    identity: Mapping[str, Any] | None
    state: str

    def brief(self) -> dict[str, Any]:
        return calibration_brief(self.calibration, self.state)


def judge_context(session: Session, profile_id: str | None) -> JudgeContext:
    from novel_system.services.style_reference.judge import STATE_MISSING

    profile = session.get(StyleReferenceProfile, str(profile_id)) if profile_id else None
    if profile is None:
        return JudgeContext(calibration=None, identity=None, state=STATE_MISSING)
    identity = identity_for(profile, judge_runtime_or_none())
    calibration = stored_calibration(profile.profile_json)
    return JudgeContext(calibration=calibration, identity=identity, state=calibration_state(calibration, identity))


def verdict_for(session: Session, policy: Any, scores: JudgeScores) -> JudgeVerdict:
    """一次评审的原始结果 → 按这份画像此刻的校准折算（未校准就是原始分）。"""
    from novel_system.services.style_reference.style_step import fidelity_thresholds

    context = judge_context(session, getattr(policy, "profile_id", None))
    return apply_calibration(
        scores,
        calibration=context.calibration,
        state=context.state,
        dimension_states=getattr(policy, "dimension_states", None),
        thresholds=fidelity_thresholds().judge(),
    )


# ---------------------------------------------------------------------------
# 一次评审 / 改写调用（作业侧）
# ---------------------------------------------------------------------------


def _call_failed(code: str, message: str, details: dict[str, Any]) -> DomainError:
    error = DomainError(code, message, status_code=502, details={**details, "retryable": True})
    error.retryable = True  # type: ignore[attr-defined]
    return error


def judge_messages(system_prefix: str, template: Any, text: str) -> list[dict[str, str]]:
    """评审消息：system = 评审口径的 ``[STYLE_REFERENCE]`` 块 + 模板 + 不可信数据约束；user = 任务 + 边界封装的待评文字。"""
    return [
        {"role": "system", "content": review_system_prompt(str(system_prefix or "") + str(template.system_prompt or ""))},
        {"role": "user", "content": judge_user_prompt(template.task_prompt, text)},
    ]


def call_judge(
    llm_client: Any,
    runtime: NodeRuntime,
    *,
    system_prefix: str,
    text: str,
    context: LLMCallContext,
    execute: Callable[..., Any],
    failure_code: str = JUDGE_CALL_FAILED_CODE,
) -> tuple[JudgeScores, str]:
    """一次评审调用 → (原始结果, llm_call_id)；调用失败 / 没有结构化结果 / 没给出可用的分 → 502（可重试）。"""
    from novel_system.services.llm_client import build_llm_request

    template = runtime.template
    request = build_llm_request(
        runtime.route,
        node_id=JUDGE_NODE_ID,
        messages=judge_messages(system_prefix, template, text),
        response_schema=judge_response_schema(template),
    )
    llm_call_id = f"llm_style_judge_{uuid.uuid4().hex}"
    response = accounted_call(
        llm_client,
        request,
        context=context,
        llm_call_id=llm_call_id,
        execute=execute,
        wrap_error=lambda exc: _call_failed(
            failure_code,
            "参考评审的模型调用失败：检查模型接入后重试。",
            {"error_type": type(exc).__name__, "llm_call_id": llm_call_id},
        ),
    )
    structured = getattr(response, "structured_output", None)
    scores = parse_judge_output(structured, schema=getattr(template, "structured_schema", None))
    if scores is None:
        raise _call_failed(
            failure_code,
            "参考评审没有给出可用的分数。",
            {"llm_call_id": llm_call_id, "reason": "no_scores" if isinstance(structured, Mapping) else "no_structured_output"},
        )
    return scores, str(getattr(response, "llm_call_id", None) or llm_call_id)


def call_judge_panel(
    llm_client: Any,
    runtime: NodeRuntime,
    *,
    system_prefix: str,
    text: str,
    context: LLMCallContext,
    execute: Callable[..., Any],
    samples: int,
    failure_code: str = JUDGE_CALL_FAILED_CODE,
) -> tuple[JudgeScores, list[str]]:
    """同一份提示问 ``samples`` 次（并发，每次一条守护线程——进程退出不等在飞的调用）→ 每维取中位的一份评审
    （``judge.combine_judge_samples``）与答成的各次调用 id。几次里有失败的就用答成的那几次；全失败 → 第一次的错误
    （502，可重试）。记账 / 控制面的错误原样抛出。"""
    count = max(1, int(samples or 1))
    if count == 1:
        scores, call_id = call_judge(
            llm_client, runtime, system_prefix=system_prefix, text=text, context=context, execute=execute,
            failure_code=failure_code,
        )
        return scores, [call_id]
    from novel_system.services.background_jobs import DaemonCallPool

    pool = DaemonCallPool(thread_name_prefix="sr_judge")
    futures = [
        pool.submit(
            call_judge, llm_client, runtime, system_prefix=system_prefix, text=text, context=context, execute=execute,
            failure_code=failure_code,
        )
        for _ in range(count)
    ]
    pool.shutdown()
    answered: list[tuple[JudgeScores, str]] = []
    failures: list[DomainError] = []
    for future in futures:
        try:
            answered.append(future.result())
        except DomainError as exc:
            failures.append(exc)
    if not answered:
        raise failures[0]
    return combine_judge_samples([scores for scores, _call_id in answered]), [call_id for _scores, call_id in answered]


def call_neutralize(
    llm_client: Any,
    runtime: NodeRuntime,
    *,
    passage: str,
    context: LLMCallContext,
    execute: Callable[..., Any],
) -> tuple[str, int]:
    """把一段原文改写成普通文字（校准的负锚点）→ (改写稿, 调用次数)。改写稿短于原文的 ``NEUTRAL_MIN_RATIO``（模型把
    这一段压缩了）→ 带着「上一次写短了」再要一次；两次都短 / 调用失败 → 502（可重试）。"""
    from novel_system.services.llm_client import build_llm_request

    template = runtime.template
    calls = 0
    text = ""
    llm_call_id = ""
    for retry in (False, True):
        request = build_llm_request(
            runtime.route,
            node_id=NEUTRALIZE_NODE_ID,
            messages=[
                {"role": "system", "content": str(template.system_prompt or "").rstrip() + "\n\n" + UNTRUSTED_SYSTEM_INSTRUCTION},
                {"role": "user", "content": neutralize_user_prompt(template.task_prompt, passage, retry=retry)},
            ],
            response_schema=getattr(template, "structured_schema", None),
        )
        llm_call_id = f"llm_style_neutralize_{uuid.uuid4().hex}"
        response = accounted_call(
            llm_client,
            request,
            context=context,
            llm_call_id=llm_call_id,
            execute=execute,
            wrap_error=lambda exc, call_id=llm_call_id: _call_failed(
                CALIBRATION_CALL_FAILED_CODE,
                "校准用的改写调用失败：检查模型接入后重新校准。",
                {"error_type": type(exc).__name__, "llm_call_id": call_id},
            ),
        )
        calls += 1
        structured = getattr(response, "structured_output", None)
        text = str(structured.get("text") or "").strip() if isinstance(structured, Mapping) else ""
        if len(text) >= NEUTRAL_MIN_RATIO * len(passage.strip()):
            return text, calls
    raise _call_failed(
        CALIBRATION_CALL_FAILED_CODE,
        "校准用的改写稿两次都比原文短太多（模型把这一段压缩了）：重新校准，或给「风格稿」换一个模型。",
        {"llm_call_id": llm_call_id, "reason": "neutral_too_short", "chars": len(text), "passage_chars": len(passage.strip())},
    )


# ---------------------------------------------------------------------------
# 锚点
# ---------------------------------------------------------------------------


def trim_passage(text: str, *, max_chars: int = ANCHOR_MAX_CHARS) -> str:
    """按段截到 ``max_chars`` 以内（至少留第一段）。"""
    kept: list[str] = []
    total = 0
    for paragraph in str(text or "").split("\n"):
        paragraph = paragraph.strip()
        if not paragraph:
            continue
        if kept and total + len(paragraph) > max_chars:
            break
        kept.append(paragraph)
        total += len(paragraph)
    return "\n".join(kept)


def choose_anchor_windows(windows: Sequence[Any], *, exclude: set[int], count: int, seed: str) -> list[Any]:
    """挑校准用的原文窗口：不在评审样例里、够长、典型度高；一章至多一窗；同一份画像版本每次挑同一组（按种子）。"""

    def order(window: Any) -> tuple[float, str]:
        digest = hashlib.sha256(f"{seed}:{int(window.window_no)}".encode("utf-8")).hexdigest()
        return (-float(getattr(window, "typicality", 0.0) or 0.0), digest)

    candidates = [
        window
        for window in windows
        if int(window.window_no) not in exclude and int(getattr(window, "chars", 0) or 0) >= ANCHOR_MIN_CHARS
    ]
    pool = sorted(candidates, key=order)[: max(count * 4, count)]
    pool.sort(key=lambda window: hashlib.sha256(f"{seed}:pick:{int(window.window_no)}".encode("utf-8")).hexdigest())
    chosen: list[Any] = []
    chapters: set[Any] = set()
    for window in pool:
        chapter = getattr(window, "chapter_no", None)
        if chapter is not None and chapter in chapters:
            continue
        chosen.append(window)
        chapters.add(chapter)
        if len(chosen) >= count:
            break
    return sorted(chosen, key=lambda window: int(window.window_no))


# ---------------------------------------------------------------------------
# 评审的作用域、策略与预算（对照检查与校准共用）
# ---------------------------------------------------------------------------


def resolve_calibration_client() -> tuple[Any | None, bool]:
    """按**当前**运行时配置取 LLM 客户端（建作业时查一次、作业开工时再取一次）；测试在这里打桩。"""
    from novel_system.services.llm_service_base import runtime_llm_client_and_enabled

    return runtime_llm_client_and_enabled()


def scope_for(project_id: str | None) -> Any:
    """只带作品（或什么都不带）的渲染作用域：没有场景时选窗按作品 / 画像定。"""
    return SimpleNamespace(project_id=project_id, scene_id=None, pov_character_id=None, onstage_chars_json=[])


def profile_policy(session: Session, profile_id: str) -> Any:
    """只给了画像（没有绑定）的检查：按画像 + 默认 v3 配置造一份检查用的契约（与本场预览同一种造法）。"""
    from novel_system.services.style_policy import policy_from_contract
    from novel_system.services.style_reference.inject.preview import preview_contract

    profile = session.get(StyleReferenceProfile, str(profile_id))
    if profile is None:
        raise profile_not_found(profile_id)
    contract = preview_contract(session, profile, normalize_binding_config({}))
    return policy_from_contract(contract, mode=POLICY_MODE_JUDGE)


def judge_input_budget(template: Any) -> int:
    """评审提示的输入预算（L7），与管线同一口径（``prompt_builder.default_input_token_budget``）：
    ``NOVEL_SYSTEM_SCENE_INPUT_TOKEN_BUDGET`` 设了正数就用它（小上下文的本机模型收紧），否则取模板值与
    ``RUNTIME_MIN_INPUT_BUDGETS`` 里这个模板的下限（与 soft_qc 同一档）的较大者。"""
    from novel_system.services import prompt_builder

    return prompt_builder.default_input_token_budget(template)


# ---------------------------------------------------------------------------
# 建作业 / 取消
# ---------------------------------------------------------------------------


def _profile_or_404(session: Session, profile_id: str) -> StyleReferenceProfile:
    profile = session.get(StyleReferenceProfile, str(profile_id))
    if profile is None:
        raise profile_not_found(profile_id)
    return profile


def latest_calibration_job(session: Session, book_id: str) -> StyleReferenceJob | None:
    return StyleJobService(session).latest_for_book(book_id, kind=JOB_KIND_CALIBRATE)


def active_calibration_job(session: Session, book_id: str) -> StyleReferenceJob | None:
    active = StyleJobService(session).active_for_book(book_id, kind=JOB_KIND_CALIBRATE)
    return active[0] if active else None


def _conflict(other: StyleReferenceJob) -> DomainError:
    if other.kind == JOB_KIND_CALIBRATE:
        return DomainError(
            CALIBRATION_ALREADY_ACTIVE_CODE,
            "这本书的参考评审正在校准：等它完成，或先取消。",
            status_code=409,
            details={"book_id": other.book_id, "job_id": other.job_id, "state": other.state},
        )
    return already_active_error(other)


def start_calibration_job(
    session: Session,
    profile_id: str,
    *,
    op_key: str | None = None,
    llm_client: Any | None = None,
) -> StyleReferenceJob:
    """建一个校准作业（调用方提交后派发）。画像不在 404；书没就绪（正在分类）409；评审 / 改写节点缺路由或模板 409；
    书的云策略不许把原文送给这两个节点的实际路由 409；同一本书已有校准在排队 / 运行 409。"""
    from novel_system.services.style_reference.policy import ensure_cloud_llm_allowed

    profile = _profile_or_404(session, profile_id)
    book = session.get(StyleReferenceBook, str(profile.book_id))
    if book is None or str(book.status or "") != "ready":
        raise DomainError(
            CALIBRATION_BOOK_NOT_READY_CODE,
            "这本书的段落分类还没完成：等分类完成、学习文风之后再校准评审。",
            status_code=409,
            details={"book_id": profile.book_id, "status": getattr(book, "status", None)},
        )
    load_runtimes(neutralize=True)
    ensure_cloud_llm_allowed(
        book, operation=CALIBRATE_OPERATION, node_ids=(JUDGE_NODE_ID, NEUTRALIZE_NODE_ID), llm_client=llm_client
    )
    job = StyleJobService(session).create(
        JOB_KIND_CALIBRATE,
        book_id=str(profile.book_id),
        profile_id=profile.profile_id,
        op_key=op_key or None,
        params={"profile_id": profile.profile_id},
        phase="queued",
        exclusive_with=(JOB_KIND_CLASSIFY,),
        conflict_error=_conflict,
    )
    job.progress_json = {"phase": "queued", "phase_label": "排队中"}
    session.flush()
    return job


def cancel_calibration(session: Session, profile_id: str) -> StyleReferenceJob:
    """取消这份画像所在的书正在排队 / 运行的校准（排队中或工人已死的直接收尾；运行中的在下一个检查点收尾）。"""
    profile = _profile_or_404(session, profile_id)
    job = active_calibration_job(session, str(profile.book_id))
    if job is None or job.state in TERMINAL_STATES:
        raise DomainError(
            CALIBRATION_NOT_ACTIVE_CODE,
            "这份画像没有正在排队或运行的评审校准。",
            status_code=409,
            details={"profile_id": profile_id},
        )
    return StyleJobService(session).request_cancel(job.job_id)


def start_after_learn(session: Session, profile_id: str | None) -> str | None:
    """学习文风成功之后自动排一个校准（提交；不在学习的工人线程里另起线程，由清扫线程在 30 秒内派发）。没有模型、
    评审 / 改写节点没配好、云策略不许、已有校准在跑 → 不排（记一条日志），画像照常可用、口径是「未校准」，作者在
    画像页可以手动校准。返回作业 id 或 None。"""
    if not profile_id:
        return None
    try:
        llm_client, enabled = resolve_calibration_client()
        if not enabled or llm_client is None:
            return None
        job = start_calibration_job(session, profile_id, llm_client=llm_client)
        session.commit()
    except Exception:  # noqa: BLE001 — 校准是学习之后的附加步骤，排不上不影响学习的结果
        session.rollback()
        logger.info("judge calibration not queued after learning profile %s", profile_id, exc_info=True)
        return None
    return job.job_id


# ---------------------------------------------------------------------------
# 作业处理器
# ---------------------------------------------------------------------------


def _context(claimed: ClaimedJob, *, node_id: str, step: str) -> LLMCallContext:
    return LLMCallContext(
        scope_type="style_reference_calibration",
        scope_id=str(claimed.job_id),
        node_id=node_id,
        step=step,
    )


def run_calibration_job(session: Session, claimed: ClaimedJob, service: StyleJobService) -> None:
    """``calibrate`` 作业处理器（见模块说明）。终态由框架写；进度写 / 结束写落空 → ``JobLost``。"""
    from novel_system.services.style_prompt_injection import (
        PLACEMENT_SYSTEM,
        ROLE_REVIEW,
        style_render_request_for_scene,
    )
    from novel_system.services.style_reference.inject.fit import fit_rendered
    from novel_system.services.style_reference.inject.render import render_style
    from novel_system.services.style_reference.policy import ensure_cloud_llm_allowed
    from novel_system.services.style_reference.style_step import fidelity_thresholds
    from novel_system.services.style_reference.windows import load_windows, window_texts

    run = JobRun(session, claimed, service)
    params = dict(claimed.params or {})
    profile_id = str(params.get("profile_id") or claimed.profile_id or "")
    thresholds = fidelity_thresholds().judge()
    count = max(MIN_ANCHORS, int(thresholds.calibration_anchors))
    total = 1 + 3 * count
    run.check_continue()
    run.checkpoint(phase="prepare", phase_label="挑选校准用的原文", done=0, total=total)
    profile = _profile_or_404(session, profile_id)
    book = session.get(StyleReferenceBook, str(profile.book_id))
    llm_client, enabled = resolve_calibration_client()
    if not enabled or llm_client is None:
        raise LLMRequiredError(operation=CALIBRATE_OPERATION)
    runtimes = load_runtimes(neutralize=True)
    ensure_cloud_llm_allowed(
        book, operation=CALIBRATE_OPERATION, node_ids=(JUDGE_NODE_ID, NEUTRALIZE_NODE_ID), llm_client=llm_client
    )
    identity = identity_for(profile, runtimes[JUDGE_NODE_ID])
    policy = profile_policy(session, profile_id)
    scope = scope_for(None)
    request = style_render_request_for_scene(
        session, scope, policy, role=ROLE_REVIEW, placement=PLACEMENT_SYSTEM, node_ids=(JUDGE_NODE_ID,)
    )
    rendered = render_style(session, policy, request, scene=None, persist_selection=False)
    session.commit()
    if rendered.empty or not rendered.system_prefix:
        raise DomainError(
            CALIBRATION_NO_REFERENCE_CODE,
            "这份画像渲染不出评审用的参考（没有样例窗口也没有文风卡）：先让这本书「学习文风」。",
            status_code=409,
            details={"profile_id": profile_id},
        )
    used = {int(ref["window_no"]) for ref in rendered.window_refs if ref.get("window_no") is not None}
    anchors = choose_anchor_windows(
        load_windows(session, str(profile.book_id)),
        exclude=used,
        count=count,
        seed=f"{profile_id}:{profile.version_tag or ''}",
    )
    texts = window_texts(session, anchors)
    passages = [(window, trim_passage(texts.get(int(window.window_no), ""))) for window in anchors]
    passages = [(window, text) for window, text in passages if len(text) >= ANCHOR_MIN_CHARS]
    if len(passages) < MIN_ANCHORS:
        raise DomainError(
            CALIBRATION_NO_WINDOWS_CODE,
            "这本书挑不出足够的原文段落来校准评审（评审样例之外、够长的段落不到两段）。",
            status_code=409,
            details={"profile_id": profile_id, "anchors": len(passages)},
        )
    judge = runtimes[JUDGE_NODE_ID]
    rendered, _fit = fit_rendered(
        rendered,
        base_system_prompt=review_system_prompt(str(judge.template.system_prompt or "")),
        user_prompt=judge_user_prompt(judge.template.task_prompt, max((text for _w, text in passages), key=len)),
        target_input_tokens=judge_input_budget(judge.template),
    )
    if rendered.empty or not rendered.system_prefix:
        raise DomainError(
            CALIBRATION_NO_REFERENCE_CODE,
            "评审节点的输入预算装不下任何参考：放宽评审节点的输入预算后重新校准。",
            status_code=409,
            details={"profile_id": profile_id, "reason": "input_budget"},
        )
    run.checkpoint(phase="neutralize", phase_label="把原文改写成普通文字", done=1, total=total)

    neutral: list[str] = []
    for index, (window, text) in enumerate(passages):
        run.check_continue()
        plain, calls = call_neutralize(
            llm_client,
            runtimes[NEUTRALIZE_NODE_ID],
            passage=text,
            context=_context(claimed, node_id=NEUTRALIZE_NODE_ID, step=CALIBRATE_NEUTRALIZE_STEP),
            execute=execute_accounted_call,
        )
        neutral.append(plain)
        run.checkpoint(
            phase="neutralize", phase_label="把原文改写成普通文字", done=2 + index, total=total, llm_calls_delta=calls
        )

    positives: list[JudgeScores] = []
    negatives: list[JudgeScores] = []
    anchors_meta: list[dict[str, Any]] = []
    done = 1 + len(passages)
    for (window, text), plain in zip(passages, neutral):
        pair: list[JudgeScores] = []
        for kind, body in (("original", text), ("plain", plain)):
            run.check_continue()
            scores, call_ids = call_judge_panel(
                llm_client,
                judge,
                system_prefix=rendered.system_prefix,
                text=body,
                context=_context(claimed, node_id=JUDGE_NODE_ID, step=CALIBRATE_JUDGE_STEP),
                execute=execute_accounted_call,
                samples=thresholds.samples,
                failure_code=CALIBRATION_CALL_FAILED_CODE,
            )
            pair.append(scores)
            done += 1
            run.checkpoint(
                phase="judge", phase_label="评审给两类文字打分", done=done, total=total, llm_calls_delta=len(call_ids)
            )
        positives.append(pair[0])
        negatives.append(pair[1])
        anchors_meta.append(
            {
                "window_no": int(window.window_no),
                "chapter_no": getattr(window, "chapter_no", None),
                "chars": len(text),
                "original": pair[0].mean(),
                "plain": pair[1].mean(),
            }
        )
    calibration = compute_calibration(
        positives,
        negatives,
        identity=identity or {},
        thresholds=thresholds,
        anchors=anchors_meta,
        calibrated_at=utcnow(),
    )
    run.check_continue()
    fresh = session.get(StyleReferenceProfile, profile_id, populate_existing=True)
    if fresh is None:
        raise JobLost(claimed.job_id)
    payload = dict(fresh.profile_json or {})
    payload[PROFILE_CALIBRATION_KEY] = calibration
    fresh.profile_json = payload
    flag_modified(fresh, "profile_json")
    # 校准与「完成」同一个事务：结束写落空就连校准一起回滚
    run.checkpoint(commit=False, phase="done", phase_label="完成", done=total, total=total)
    succeeded = service.succeed(
        claimed,
        {
            "profile_id": profile_id,
            "status": calibration["status"],
            "positive_mean": calibration["positive_mean"],
            "negative_mean": calibration["negative_mean"],
            "separation": calibration["separation"],
        },
    )
    if not succeeded:
        raise JobLost(claimed.job_id)


def calibration_never_resumes(_job: StyleReferenceJob) -> bool:
    """校准失败 / 取消后不续跑：重新校准从头来（十来次调用）。"""
    return False


def calibration_payload(session: Session, profile: StyleReferenceProfile) -> dict[str, Any]:
    """画像页的「评审校准」卡：此刻的口径、两类文字的平均分、分不清的维、评审是哪个模型、最近一次校准作业。"""
    from novel_system.services.style_reference.jobs import job_activity_entry

    runtime = judge_runtime_or_none()
    identity = identity_for(profile, runtime)
    calibration = stored_calibration(profile.profile_json)
    state = calibration_state(calibration, identity)
    latest = latest_calibration_job(session, str(profile.book_id))
    payload = calibration_brief(calibration, state)
    payload.update(
        {
            "judge": {"provider": (identity or {}).get("provider"), "model": (identity or {}).get("model")},
            "calibrated_judge": dict((calibration or {}).get("identity") or {}) or None,
            "anchors": list((calibration or {}).get("anchors") or []),
            "dimensions": dict((calibration or {}).get("dimensions") or {}),
            "job": job_activity_entry(latest) if latest is not None and latest.profile_id == profile.profile_id else None,
        }
    )
    return payload


__all__ = [
    "CALIBRATION_ALREADY_ACTIVE_CODE",
    "CALIBRATION_BOOK_NOT_READY_CODE",
    "CALIBRATION_CALL_FAILED_CODE",
    "CALIBRATION_CONFIG_MISSING_CODE",
    "CALIBRATION_NOT_ACTIVE_CODE",
    "CALIBRATION_NO_REFERENCE_CODE",
    "CALIBRATION_NO_WINDOWS_CODE",
    "JUDGE_CALL_FAILED_CODE",
    "JudgeContext",
    "active_calibration_job",
    "calibration_never_resumes",
    "calibration_payload",
    "call_judge",
    "call_judge_panel",
    "call_neutralize",
    "cancel_calibration",
    "choose_anchor_windows",
    "identity_for",
    "judge_context",
    "judge_messages",
    "judge_runtime_or_none",
    "latest_calibration_job",
    "judge_input_budget",
    "load_runtimes",
    "profile_policy",
    "resolve_calibration_client",
    "run_calibration_job",
    "scope_for",
    "start_after_learn",
    "start_calibration_job",
    "trim_passage",
    "verdict_for",
]
