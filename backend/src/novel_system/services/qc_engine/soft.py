"""软质检引擎：可读性 / 参考评审（带与风格稿相同的 [STYLE_REFERENCE] 前缀）、风格稿的原文重合门、受控补丁、
软风险接受与人工复核。"""

from __future__ import annotations

import logging
from typing import Any

from novel_system.contracts.qc import SoftQCOutput
from novel_system.db.models import QcReport, SceneCard, SceneRunState
from novel_system.services.hash_engine import sha256_text
from novel_system.services.qc_engine.base import (
    QcEngineBase,
    SoftQcDecision,
    _build_qc_report_id,
    _qc_apply_issue_tracking,
    _qc_clear_downstream_outputs,
    _qc_record_attempt,
    _with_run_context,
)
from novel_system.services.qc_engine.degradation import _qc_run_node_with_degradation
from novel_system.services.qc_engine.issues import (
    _append_unique_rewrite_briefs,
    _dedupe_issues,
    _qc_apply_deterministic_quality_gates,
    _rewrite_briefs_for_deterministic_issues,
)
from novel_system.services.qc_engine.scores import _prompt_carries_reference, _reference_judge_record
from novel_system.services.qc_engine.styled_gate import (
    STYLE_BANNED_TERM_ISSUE_KEY,
    STYLE_GATE_UNAVAILABLE_ISSUE_KEY,
    STYLE_PLAGIARISM_ISSUE_KEY,
    STYLE_VALIDATION_PLAGIARISM_TRIGGER,
    STYLED_GATE_UNAVAILABLE_VERDICT,
    _STYLED_GATE_MAX_HITS,
    run_styled_draft_style_gate,
)
from novel_system.services.qc_validator import validate_qc_report
from novel_system.services.quality_classifier import (
    classify_issue,
    classify_issues,
    has_blocking,
)
from novel_system.services.style_reference.policy import STYLE_REFERENCE_FAIL_CLOSED_ERRORS

_LOGGER = logging.getLogger(__name__)


def _style_deviation_instruction(deviation: Any) -> str:
    """一条 style_deviations → 修补简报里的一句：「维度：改法」，带上评审引的那段原稿（补丁据此找到位置）。"""
    brief = str(getattr(deviation, "patch_brief", "") or "").strip()
    if not brief:
        return ""
    dimension = str(getattr(deviation, "dimension", "") or "").strip()
    text = brief if not dimension or dimension == "style" or brief.startswith(dimension) else f"{dimension}：{brief}"
    evidence = str(getattr(deviation, "evidence", "") or "").strip()
    if evidence and evidence not in text:
        text += f"（原稿：「{evidence[:80]}」）"
    return text


def _soft_block_human(payload: dict[str, Any], *, issue: dict[str, Any], brief: str) -> dict[str, Any]:
    """软质检结论改判「要人工复核」：追加一条已分级的 issue 与一句修改简报，清掉随稿携带的备注。"""
    rewrite_brief = [
        item
        for item in payload.get("rewrite_brief", [])
        if isinstance(item, str) and item.strip()
    ]
    return {
        **payload,
        "resolution_code": "soft_block_human",
        "pass_flag": False,
        "next_action": "human_review_required",
        "issues": _dedupe_issues([*(payload.get("issues") or []), issue]),
        "rewrite_brief": _append_unique_rewrite_briefs(rewrite_brief, [brief]),
        "carry_forward_note": False,
        "note_scope": None,
        "carry_note_text": None,
    }


class SoftQcEngine(QcEngineBase):
    def evaluate(
        self,
        *,
        scene_id: str,
        bundle: dict[str, Any],
        source_draft_row_id: str,
        source_draft_content: str,
        execution_step_key: str = "soft_qc:0",
    ) -> SoftQcDecision:
        scene = self.session.get(SceneCard, scene_id)
        state = self.session.get(SceneRunState, scene_id)
        # v2（规格 §2.W5.4）：soft_qc 评审看到的是与 style_draft **相同**的 [STYLE_REFERENCE]
        # 前缀（同一冻结契约、同一 task_type），这样才谈得上「对照 [声音特征] /
        # [正向风格特征] 检查偏离」。注入审计随 attempt 落库。
        style_runtime_audit: dict[str, Any] | None = None
        # 风格参考 v3（L6）：这一遍是不是参考评审（提示里真的带着参考）——决定分数记不记成「参考评审总分」
        reference_carried = False

        def _decorate_soft_qc_prompt(
            prompt: dict[str, Any], final_user_prompt: str
        ) -> dict[str, Any] | None:
            nonlocal style_runtime_audit, reference_carried
            injected = self._inject_style_reference_prefix(
                prompt,
                scene,
                bundle,
                context_text=source_draft_content,
                final_user_prompt=final_user_prompt,
            )
            audit = (
                injected.get("_style_reference_runtime_audit")
                if isinstance(injected, dict)
                else None
            )
            if isinstance(audit, dict):
                style_runtime_audit = dict(audit)
            reference_carried = _prompt_carries_reference(injected)
            return injected

        # Wave 2（§5.4/§7.7）：软 QC 执行失败不再断头——降级为 waive + Q2 警告
        # 继续交付；确定性 gates 照跑，verified Q0/Q1 仍能阻断。
        llm_call_id, degraded_reason, payload = _qc_run_node_with_degradation(
            self.session,
            prompt_builder=self.prompt_builder,
            llm_runner=self._llm_runner,
            scene=scene,
            state=state,
            scene_id=scene_id,
            bundle=bundle,
            source_draft_row_id=source_draft_row_id,
            source_draft_content=source_draft_content,
            execution_step_key=execution_step_key,
            step="soft_qc",
            message_prefix="soft QC",
            degraded_payload_factory=self._degraded_waive_payload,
            prompt_decorator=_decorate_soft_qc_prompt,
        )

        payload = _qc_apply_deterministic_quality_gates(
            scene, source_draft_content, payload, qc_type="soft_qc"
        )
        payload = self._apply_quality_grading(scene, source_draft_content, payload)
        # v2（规格 §2.W5.5）styled-draft gate：风格稿的确定性抄袭 / 生成禁用词检查。
        # 抄袭 → Q0 阻断并升级人工复核（不允许软风险接受）；禁用词 → 要求人工复核
        # （作者可接受软风险）；quant 只记诊断。
        styled_gate = run_styled_draft_style_gate(
            self.session,
            scene,
            source_draft_content,
            stage="soft_qc",
            bundle=bundle,
        )
        styled_gate_trigger: str | None = None
        if styled_gate is not None:
            if styled_gate.get("verdict") == "plagiarism":
                payload = self._style_plagiarism_block_payload(
                    scene, source_draft_content, payload, styled_gate
                )
                styled_gate_trigger = STYLE_VALIDATION_PLAGIARISM_TRIGGER
            elif styled_gate.get("forbidden_hits"):
                payload = self._style_banned_term_review_payload(
                    scene, source_draft_content, payload, styled_gate
                )
            elif styled_gate.get("verdict") == STYLED_GATE_UNAVAILABLE_VERDICT:
                # gate 没跑成 ≠ 无绑定：风格稿带着样例前缀生成，却没有过抄袭 / 禁用词
                # 检查——挂 Q2 并要求人工复核（可软风险接受），绝不静默交付。
                payload = self._style_gate_unavailable_review_payload(
                    scene, source_draft_content, payload, styled_gate
                )
        attempt_details_extra: dict[str, Any] = {}
        if style_runtime_audit is not None:
            attempt_details_extra["style_reference_runtime"] = style_runtime_audit
        # 分数在 _qc_run_node_with_degradation 里已按模板声明的刻度换算成 0–1（只换一次）
        judge_record = _reference_judge_record(payload, carried=reference_carried)
        if judge_record is not None:
            attempt_details_extra["reference_judge"] = judge_record
        if styled_gate is not None:
            attempt_details_extra["styled_draft_gate"] = styled_gate
        validate_qc_report("soft_qc", payload)  # 组合合法性校验（不回写 dump）
        branch = self._branch_for(payload["next_action"])
        if branch == "patch" and state.soft_patch_count >= 1:
            if has_blocking(payload.get("issues", [])):
                payload = self._block_repeat_patch_payload(payload)
            else:
                payload = self._waive_repeat_patch_payload(payload)
            validate_qc_report("soft_qc", payload)
            branch = self._branch_for(payload["next_action"])
        elif branch == "waive" and has_blocking(payload.get("issues", [])):
            payload = self._block_repeat_patch_payload(payload)
            validate_qc_report("soft_qc", payload)
            branch = self._branch_for(payload["next_action"])

        qc_report = self._persist_qc_report(
            scene=scene,
            state=state,
            bundle=bundle,
            source_draft_row_id=source_draft_row_id,
            payload=payload,
            reference_carried=reference_carried,
        )

        if branch == "human_review_required" and styled_gate_trigger is not None:
            # 抄袭红线是来源安全（Q0）：与 hard_qc 的同名触发原因一致，不开放软风险接受。
            _qc_apply_issue_tracking(state, payload["issues"])
            _qc_clear_downstream_outputs(state)
            return self._escalate_existing_report(
                scene=scene,
                state=state,
                bundle=bundle,
                source_draft_row_id=source_draft_row_id,
                qc_report=qc_report,
                branch=branch,
                failure_reason=(
                    "style_reference styled-draft gate found deterministic plagiarism "
                    "evidence in the styled draft; human review is required."
                ),
                trigger_reason=styled_gate_trigger,
                llm_call_id=llm_call_id,
                execution_step_key=execution_step_key,
                details_extra=attempt_details_extra,
            )

        if branch == "human_review_required":
            blocking_issue = has_blocking(payload.get("issues", []))
            trigger_reason = (
                "blocking_soft_qc_issue"
                if blocking_issue
                else "soft_qc_requested_human_review"
            )
            source_draft_content_hash = sha256_text(source_draft_content)
            accepted_waiver = self.human_review_manager.accepted_soft_risk_waiver(
                scene_id=scene.scene_id,
                trigger_reason=trigger_reason,
                source_draft_content_hash=source_draft_content_hash,
            )
            if accepted_waiver is not None:
                state.current_human_review_event_id = None
                state.scene_status = "soft_qc_passed_with_author_acceptance"
                _qc_apply_issue_tracking(state, payload["issues"])
                self._record_attempt(
                    scene_id=scene.scene_id,
                    chapter_id=scene.chapter_id,
                    source_bundle_id=bundle["bundle_id"],
                    source_draft_row_id=source_draft_row_id,
                    branch="accepted_soft_risk",
                    qc_report_id=qc_report.qc_report_id,
                    resolution_code=qc_report.resolution_code or "",
                    next_action=qc_report.next_action or "",
                    human_review_event_id=accepted_waiver["event_id"],
                    rewrite_brief=payload["rewrite_brief"],
                    llm_call_id=llm_call_id,
                    execution_step_key=execution_step_key,
                    details_extra=attempt_details_extra,
                )
                self.session.flush()
                return SoftQcDecision(
                    branch="waive",
                    qc_report_id=qc_report.qc_report_id,
                    human_review_event_id=accepted_waiver["event_id"],
                    resolution_code=qc_report.resolution_code or "",
                    next_action=qc_report.next_action or "",
                    should_continue=True,
                    stop_reason=f"accepted_soft_risk:{accepted_waiver['event_id']}",
                    llm_call_id=llm_call_id,
                    execution_step_key=execution_step_key,
                )
            _qc_clear_downstream_outputs(state)
            return self._escalate_existing_report(
                scene=scene,
                state=state,
                bundle=bundle,
                source_draft_row_id=source_draft_row_id,
                qc_report=qc_report,
                branch=branch,
                failure_reason=(
                    "blocking soft_qc issue prevents finalization."
                    if blocking_issue
                    else "soft_qc explicitly requested human review before finalization."
                ),
                trigger_reason=trigger_reason,
                source_draft_content_hash=source_draft_content_hash,
                llm_call_id=llm_call_id,
                execution_step_key=execution_step_key,
                details_extra=attempt_details_extra,
            )

        _qc_apply_issue_tracking(state, payload["issues"])
        if branch == "patch":
            state.scene_status = "soft_qc_patch_required"
        elif branch == "waive":
            state.scene_status = "soft_qc_passed_with_notes"
        else:
            state.scene_status = "soft_qc_passed"

        self._record_attempt(
            scene_id=scene.scene_id,
            chapter_id=scene.chapter_id,
            source_bundle_id=bundle["bundle_id"],
            source_draft_row_id=source_draft_row_id,
            branch=branch,
            qc_report_id=qc_report.qc_report_id,
            resolution_code=qc_report.resolution_code or "",
            next_action=qc_report.next_action or "",
            human_review_event_id=None,
            rewrite_brief=payload["rewrite_brief"],
            llm_call_id=llm_call_id,
            execution_step_key=execution_step_key,
            details_extra=attempt_details_extra,
        )
        self.session.flush()
        return SoftQcDecision(
            branch=branch,
            qc_report_id=qc_report.qc_report_id,
            human_review_event_id=None,
            resolution_code=qc_report.resolution_code or "",
            next_action=qc_report.next_action or "",
            should_continue=branch in {"continue", "waive"},
            stop_reason=degraded_reason,
            llm_call_id=llm_call_id,
            execution_step_key=execution_step_key,
        )

    @staticmethod
    def _branch_for(next_action: str) -> str:
        return {
            "pass": "continue",
            "patch": "patch",
            "pass_with_notes": "waive",
            "human_review_required": "human_review_required",
        }[next_action]

    def _inject_style_reference_prefix(
        self,
        prompt: dict[str, Any],
        scene: SceneCard,
        bundle: dict[str, Any],
        *,
        context_text: str,
        final_user_prompt: str,
    ) -> dict[str, Any] | None:
        """v2：复用 scene_generation 的模块级注入器，把同一 [STYLE_REFERENCE] 前缀
        prepend 到 soft_qc 系统提示（task_type 不变：读同一个冻结契约）。

        注入器内部已吞掉召回 / 渲染异常并记 ``outcome="degraded"``；这里再兜一层
        import / 意外错误，保证 soft_qc 永不因风格前缀失败而中断。
        """
        try:
            from novel_system.services.style_prompt_injection import (
                ROLE_REVIEW,
                inject_style_reference_prefix,
            )

            # 风格参考 v3（L4）：评审节点按评审口径渲染——冻结选窗的前 4 窗样例（窗数由角色决定，
            # inject.request.ROLE_K_CAPS 是唯一定义），标题用评审口径
            return inject_style_reference_prefix(
                self.session,
                prompt,
                scene,
                bundle,
                task_type="scene_generation",
                context_text=context_text,
                final_user_prompt=final_user_prompt,
                role=ROLE_REVIEW,
            )
        except STYLE_REFERENCE_FAIL_CLOSED_ERRORS:
            # 云策略不许把这本书派生的任何东西送给软 QC 的节点：整遍软 QC 409（带 author_action），不降级成没有参考的提示
            raise
        except Exception:  # noqa: BLE001 — 可选增强，不阻断 soft_qc
            _LOGGER.warning(
                "soft_qc style reference prefix skipped for scene %s",
                getattr(scene, "scene_id", None),
                exc_info=True,
            )
            return None

    @staticmethod
    def _style_plagiarism_block_payload(
        scene: SceneCard,
        content: str,
        payload: dict[str, Any],
        gate: dict[str, Any],
    ) -> dict[str, Any]:
        """styled-draft gate 抄袭命中 → Q0 ``style_plagiarism`` + soft_block_human。"""
        issue = classify_issue(
            {
                "issue_key": STYLE_PLAGIARISM_ISSUE_KEY,
                "message": (
                    "style_reference plagiarism check hit on the styled draft "
                    "(deterministic n-gram overlap with the reference source)"
                ),
                "source": "deterministic",
                "evidence_spans": [
                    {
                        "start": int(hit.get("position") or 0),
                        "end": int(hit.get("position") or 0)
                        + int(hit.get("matched_length") or 0),
                    }
                    for hit in (gate.get("plagiarism_hits") or [])[:5]
                    if isinstance(hit, dict)
                ],
                "details": {
                    "stage": "styled_draft_gate",
                    "profile_id": gate.get("profile_id"),
                    "runtime_contract_hash": gate.get("runtime_contract_hash"),
                    "plagiarism_hit_count": gate.get("plagiarism_hit_count"),
                },
            },
            scene=scene,
            content=content,
        )
        return _soft_block_human(
            payload,
            issue=issue,
            brief=(
                "风格稿与参考作品原文存在确定性连续重叠：人工复核后重写重叠段落，"
                "只保留句法 / 节奏机制，不得沿用参考原文的字句。"
            ),
        )

    @staticmethod
    def _style_banned_term_review_payload(
        scene: SceneCard,
        content: str,
        payload: dict[str, Any],
        gate: dict[str, Any],
    ) -> dict[str, Any]:
        """styled-draft gate 生成禁用词 / 受保护专名命中 → Q2 issue + 要求人工复核（可软风险接受）。

        风格参考 v3（H1）：词表与成稿门同一张**现行**的表（抄袭门的受保护专名），成稿门对同样的命中只报不拦的警告；
        作者在这里接受风险后稿子照常往下走。"""
        terms = [
            str(hit.get("matched_excerpt") or hit.get("pattern_statement") or "")
            for hit in (gate.get("forbidden_hits") or [])
            if isinstance(hit, dict)
        ]
        terms = [term for term in dict.fromkeys(terms) if term][:_STYLED_GATE_MAX_HITS]
        issue = classify_issue(
            {
                "issue_key": STYLE_BANNED_TERM_ISSUE_KEY,
                "message": (
                    "styled draft uses generation-banned term(s) / protected name(s) of the "
                    f"style reference: {', '.join(terms)}"
                ),
                "source": "deterministic",
                "evidence_spans": [{"text": term} for term in terms[:5]],
                "details": {
                    "stage": "styled_draft_gate",
                    "profile_id": gate.get("profile_id"),
                    "runtime_contract_hash": gate.get("runtime_contract_hash"),
                    "terms": terms,
                },
            },
            scene=scene,
            content=content,
        )
        return _soft_block_human(
            payload,
            issue=issue,
            brief=(
                "风格稿用了参考画像的生成禁用词 / 受保护专名（"
                + "、".join(terms)
                + "）：人工复核——是参考书的专名就换成自己的；若只是日常用词被误收进了专名表，可以接受这一处，"
                "并到文风画像的禁用词里删掉它。"
            ),
        )

    @staticmethod
    def _style_gate_unavailable_review_payload(
        scene: SceneCard,
        content: str,
        payload: dict[str, Any],
        gate: dict[str, Any],
    ) -> dict[str, Any]:
        """styled-draft gate 未能执行 → Q2 issue + 要求人工复核（可软风险接受）。

        与禁用词命中同一形状：检查没跑不是正文的错，不阻断，但作者必须知道这份风格稿
        没有经过抄袭 / 禁用词核对。
        """
        error = str(gate.get("error") or "unknown")
        error_code = gate.get("error_code")
        issue = classify_issue(
            {
                "issue_key": STYLE_GATE_UNAVAILABLE_ISSUE_KEY,
                "message": (
                    "style_reference styled-draft gate could not run on this styled "
                    f"draft ({error}"
                    + (f": {error_code}" if error_code else "")
                    + "); the plagiarism / frozen banned-term check did not execute"
                ),
                "source": "deterministic",
                "evidence_spans": [],
                "details": {
                    "stage": "styled_draft_gate",
                    "gate_stage": gate.get("stage"),
                    "error": error,
                    "error_code": error_code,
                    "profile_id": gate.get("profile_id"),
                    "runtime_contract_hash": gate.get("runtime_contract_hash"),
                },
            },
            scene=scene,
            content=content,
        )
        return _soft_block_human(
            payload,
            issue=issue,
            brief=(
                "风格稿的抄袭 / 禁用词检查未能执行：人工复核该稿是否沿用参考原文字句或"
                "复刻了参考画像的生成禁用词，再决定是否接受。"
            ),
        )

    @staticmethod
    def _degraded_waive_payload(
        *,
        issue_key: str,
        message: str,
        continuity_warning: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """软 QC 执行失败的降级形状：waive + Q2 警告 issue（§5.4——不撤销已有正文）。"""
        issue: dict[str, Any] = {"issue_key": issue_key, "message": message}
        if continuity_warning is not None:
            issue["continuity_warning"] = continuity_warning
        return {
            "resolution_code": "soft_waive",
            "pass_flag": True,
            "next_action": "pass_with_notes",
            "issues": [issue],
            "rewrite_brief": [],
            "carry_forward_note": True,
            "note_scope": "scene_memory",
            "carry_note_text": f"soft QC degraded ({issue_key}): {message}"[:500],
        }

    def _apply_quality_grading(
        self, scene: SceneCard, source_draft_content: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        """Wave 2（§5.4）：统一分级 + 阻断裁决单一来源（软 QC 侧）。

        - 全部 issue 过分类器；确定性 Q1/Q2 在 LLM 说 pass 时仍触发一次受控补丁
          （自动修订 ≤2 的第一次），Q3（tension/theme 之外的风格层）不再强制补丁。
        - LLM 主动要求人工审阅但无 verified Q0/Q1 → 降级为 waive 携带 carry note，
          正文照常交付（G-03）。
        """
        classified = classify_issues(
            payload.get("issues") or [], scene=scene, content=source_draft_content
        )
        graded = {**payload, "issues": classified}
        if graded.get("next_action") == "pass" and any(
            issue.get("source") == "deterministic"
            and issue.get("quality_level") in ("Q1", "Q2")
            for issue in classified
        ):
            rewrite_brief = [
                item
                for item in graded.get("rewrite_brief", [])
                if isinstance(item, str) and item.strip()
            ]
            rewrite_brief = _append_unique_rewrite_briefs(
                rewrite_brief, _rewrite_briefs_for_deterministic_issues(classified)
            ) or ["修复确定性质检发现的问题后重检。"]
            graded = {
                **graded,
                "resolution_code": "soft_patch",
                "pass_flag": False,
                "next_action": "patch",
                "rewrite_brief": rewrite_brief,
                "carry_forward_note": False,
                "note_scope": None,
                "carry_note_text": None,
            }
        if graded.get("next_action") == "human_review_required" and not has_blocking(
            classified
        ):
            graded = self._waive_no_blocking_payload(graded)
        return graded

    @staticmethod
    def _waive_no_blocking_payload(payload: dict[str, Any]) -> dict[str, Any]:
        briefs = [
            item.strip()
            for item in payload.get("rewrite_brief", [])
            if isinstance(item, str) and item.strip()
        ]
        if not briefs:
            briefs = [
                str(issue.get("message") or issue.get("issue_key") or "").strip()
                for issue in payload.get("issues", [])
                if isinstance(issue, dict)
            ][:3]
        summary = (
            "; ".join(item for item in briefs if item) or "soft QC advisory retained"
        )
        return {
            **payload,
            "resolution_code": "soft_waive",
            "pass_flag": True,
            "next_action": "pass_with_notes",
            "carry_forward_note": True,
            "note_scope": "scene_memory",
            "carry_note_text": f"软性质检意见无确定性 Q0/Q1 佐证，正文照常交付；意见随行：{summary}"[
                :500
            ],
        }

    @staticmethod
    def _block_repeat_patch_payload(payload: dict[str, Any]) -> dict[str, Any]:
        rewrite_brief = [
            item
            for item in payload.get("rewrite_brief", [])
            if isinstance(item, str) and item.strip()
        ]
        if not rewrite_brief:
            rewrite_brief = ["阻塞级质量问题仍未解决，请人工复核后再归档。"]
        return {
            **payload,
            "resolution_code": "soft_block_human",
            "pass_flag": False,
            "next_action": "human_review_required",
            "rewrite_brief": rewrite_brief,
            "carry_forward_note": False,
            "note_scope": None,
            "carry_note_text": None,
        }

    @staticmethod
    def _waive_repeat_patch_payload(payload: dict[str, Any]) -> dict[str, Any]:
        rewrite_brief = [
            item
            for item in payload.get("rewrite_brief", [])
            if isinstance(item, str) and item.strip()
        ]
        carry_note_text = (
            "Repeated soft QC patch request after one controlled patch pass."
        )
        if rewrite_brief:
            carry_note_text = f"{carry_note_text} Carry forward: {'; '.join(item.strip() for item in rewrite_brief)}"
        return {
            **payload,
            "resolution_code": "soft_waive",
            "pass_flag": True,
            "next_action": "pass_with_notes",
            "carry_forward_note": True,
            "note_scope": "scene_memory",
            "carry_note_text": carry_note_text,
        }

    @staticmethod
    def _serialize_rewrite_brief(report: Any, *, reference_carried: bool = True) -> list[dict[str, Any]]:
        entries = [{"instruction": item} for item in report.rewrite_brief]
        if report.resolution_code == "soft_patch":
            # 批准#13b（B04-20）：评审按维给的定位改法（style_deviations 的 patch_brief：哪一段、照样例的哪种手法改）
            # 写进修补简报——提示词本来就要它、校验也收了，以前落库时丢掉，补丁只拿到笼统的一句「这一维不像」。
            # 补丁读的是 instruction（orchestrator._rewrite_brief_from_report），起草台的质检摘要也照样列出。
            seen = {str(item).strip() for item in report.rewrite_brief}
            for deviation in report.style_deviations:
                instruction = _style_deviation_instruction(deviation)
                if instruction and instruction not in seen:
                    seen.add(instruction)
                    entries.append(
                        {
                            "instruction": instruction,
                            "kind": "style_deviation",
                            "dimension": deviation.dimension,
                            "severity": deviation.severity,
                        }
                    )
        if report.resolution_code == "soft_waive" and report.carry_forward_note:
            entries.append(
                {
                    "kind": "carry_forward_note",
                    "note_scope": report.note_scope,
                    "carry_note_text": report.carry_note_text,
                }
            )
        # 风格参考 v3（V7）：参考评审的按维分与总分随报告落库（此前 style_score 校验完就丢了）。
        # 读简报的地方只认 instruction / carry_forward_note，这一条对它们不可见。
        # L6：只有参考评审才记（提示带着参考，或回答给了按维分）——没绑定的润色口径顺手给的 style_score 不算。
        judge = _reference_judge_record(
            {"style_score": report.style_score, "dimension_scores": dict(report.dimension_scores or {})},
            carried=reference_carried,
        )
        if judge is not None:
            entries.append(judge)
        return entries

    def _persist_qc_report(
        self,
        *,
        scene: SceneCard,
        state: SceneRunState,
        bundle: dict[str, Any],
        source_draft_row_id: str,
        payload: dict[str, Any],
        reference_carried: bool = False,
    ) -> QcReport:
        # 分数已在 _qc_run_node_with_degradation 里按模板声明的刻度换算成 0–1（只换一次；再换会被除两遍）
        report = SoftQCOutput.model_validate(
            {
                **payload,
                "issues": [
                    {
                        "issue_key": issue.get("issue_key", "ok"),
                        "message": issue.get("message", ""),
                    }
                    for issue in payload["issues"]
                ],
            }
        )
        qc_report = QcReport(
            qc_report_id=_build_qc_report_id(scene.scene_id),
            scene_id=scene.scene_id,
            chapter_id=scene.chapter_id,
            qc_type="soft_qc",
            source_draft_row_id=source_draft_row_id,
            source_bundle_id=bundle["bundle_id"],
            resolution_code=payload["resolution_code"],
            pass_flag=1 if payload["pass_flag"] else 0,
            next_action=payload["next_action"],
            issues_json=payload["issues"],
            rewrite_brief_json=self._serialize_rewrite_brief(report=report, reference_carried=reference_carried),
        )
        self.session.add(qc_report)
        self.session.flush()
        state.current_qc_report_id = qc_report.qc_report_id
        return qc_report

    def _record_attempt(
        self,
        *,
        scene_id: str,
        chapter_id: str,
        source_bundle_id: str,
        source_draft_row_id: str,
        branch: str,
        qc_report_id: str,
        resolution_code: str,
        next_action: str,
        human_review_event_id: str | None,
        rewrite_brief: list[str],
        llm_call_id: str | None = None,
        execution_step_key: str = "soft_qc:0",
        error_code: str | None = None,
        retryable: bool | None = None,
        continuity_warning: dict[str, Any] | None = None,
        details_extra: dict[str, Any] | None = None,
    ) -> None:
        _qc_record_attempt(
            self.session,
            step="soft_qc",
            scene_id=scene_id,
            chapter_id=chapter_id,
            source_bundle_id=source_bundle_id,
            branch=branch,
            qc_report_id=qc_report_id,
            resolution_code=resolution_code,
            next_action=next_action,
            human_review_event_id=human_review_event_id,
            execution_step_key=execution_step_key,
            llm_call_id=llm_call_id,
            error_code=error_code,
            retryable=retryable,
            continuity_warning=continuity_warning,
            # soft 侧独有的 details_json 键（checkpoint 契约键名不变）；v2 追加
            # style_reference_runtime / styled_draft_gate 诊断键（可选）。
            details_extra={
                "source_draft_row_id": source_draft_row_id,
                "rewrite_brief": rewrite_brief,
                **(details_extra or {}),
            },
        )

    def _escalate_existing_report(
        self,
        *,
        scene: SceneCard,
        state: SceneRunState,
        bundle: dict[str, Any],
        source_draft_row_id: str,
        qc_report: QcReport,
        branch: str,
        failure_reason: str,
        trigger_reason: str,
        continuity_warning: dict[str, Any] | None = None,
        llm_call_id: str | None = None,
        execution_step_key: str = "soft_qc:0",
        error_code: str | None = None,
        retryable: bool | None = None,
        source_draft_content_hash: str | None = None,
        details_extra: dict[str, Any] | None = None,
    ) -> SoftQcDecision:
        replay_context = {
            "scene_id": scene.scene_id,
            "chapter_id": scene.chapter_id,
            "source_bundle_id": bundle["bundle_id"],
            "source_bundle_hash": bundle["bundle_snapshot_hash"],
            "source_draft_row_id": source_draft_row_id,
            "current_qc_report_id": qc_report.qc_report_id,
            "scene_status_before_block": state.scene_status,
            "soft_patch_count": state.soft_patch_count,
        }
        if source_draft_content_hash is not None:
            replay_context["source_draft_content_hash"] = source_draft_content_hash
        event = self._open_generation_blocker(
            scene=scene,
            state=state,
            draft_row_id=source_draft_row_id,
            failure_reason=failure_reason,
            trigger_reason=trigger_reason,
            replay_context=_with_run_context(
                replay_context,
                llm_call_id=llm_call_id,
                error_code=error_code,
                retryable=retryable,
                continuity_warning=continuity_warning,
            ),
            # 软风险接受只给「阻断级软质检意见 / 软质检要人工」这两类，而且要有这份稿子的内容哈希
            allow_soft_risk_acceptance=(
                source_draft_content_hash is not None
                and trigger_reason in {"blocking_soft_qc_issue", "soft_qc_requested_human_review"}
            ),
        )
        self._record_attempt(
            scene_id=scene.scene_id,
            chapter_id=scene.chapter_id,
            source_bundle_id=bundle["bundle_id"],
            source_draft_row_id=source_draft_row_id,
            branch="human_review_required",
            qc_report_id=qc_report.qc_report_id,
            resolution_code=qc_report.resolution_code or "",
            next_action=qc_report.next_action or "",
            human_review_event_id=event.event_id,
            rewrite_brief=qc_report.rewrite_brief_json or [],
            llm_call_id=llm_call_id,
            execution_step_key=execution_step_key,
            error_code=error_code,
            retryable=retryable,
            continuity_warning=continuity_warning,
            details_extra=details_extra,
        )
        self.session.flush()
        return SoftQcDecision(
            branch="human_review_required",
            qc_report_id=qc_report.qc_report_id,
            human_review_event_id=event.event_id,
            resolution_code=qc_report.resolution_code or "",
            next_action=qc_report.next_action or "",
            should_continue=False,
            stop_reason=trigger_reason,
            llm_call_id=llm_call_id,
            execution_step_key=execution_step_key,
        )
