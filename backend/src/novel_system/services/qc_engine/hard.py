"""硬质检引擎：事实、连续性与硬约束（模型意见 → 确定性核对 → 约束冲突注解 → 分级 → 中性步位稿的原文重合门
→ 熔断 / 人工复核）。"""

from __future__ import annotations

import logging
import time
from typing import Any

from novel_system.db.models import QcReport, SceneCard, SceneRunState
from novel_system.services.qc_constraints import (
    contains_forbidden_term,
    issue_mentions_source,
    required_groups_missing,
    source_field_satisfied,
)
from novel_system.services.qc_engine.base import (
    HardQcDecision,
    QcEngineBase,
    _build_qc_report_id,
    _qc_apply_issue_tracking,
    _qc_clear_downstream_outputs,
    _qc_record_attempt,
    _with_run_context,
)
from novel_system.services.qc_engine.degradation import _qc_run_node_with_degradation
from novel_system.services.qc_engine.issues import (
    _annotate_qc_issues,
    _constraint_conflicts_for_text,
    _evidence_spans_for_text,
    _issue_blob,
    _promote_constraint_conflicts_to_human_review,
    _qc_apply_deterministic_quality_gates,
    _reported_duplicate_appears_once,
    _scene_card_source_texts,
)
from novel_system.services.qc_engine.styled_gate import (
    HARD_QC_GATE_EVENT_KIND,
    _record_gate_event,
    _scene_has_gate_scope,
    scene_gate_style_policy,
)
from novel_system.services.qc_validator import validate_qc_report
from novel_system.services.quality_classifier import (
    classify_issue,
    classify_issues,
    has_blocking,
)

_LOGGER = logging.getLogger(__name__)


HARD_QC_REQUIRED_ISSUE_KEYS = {"missing_required_text", "missing_hard_constraint"}
HARD_QC_STYLE_ONLY_ISSUE_KEYS = {
    "style_compliance",
    "style_rule_violation",
}
HARD_QC_NON_BLOCKING_LLM_ISSUE_KEYS = {"character_role_inconsistency"}


class HardQcEngine(QcEngineBase):
    def evaluate(
        self,
        *,
        scene_id: str,
        bundle: dict[str, Any],
        neutral_draft_row_id: str,
        neutral_content: str,
        execution_step_key: str = "hard_qc:0",
    ) -> HardQcDecision:
        scene = self.session.get(SceneCard, scene_id)
        state = self.session.get(SceneRunState, scene_id)
        # Wave 2（§5.4/§7.7）：QC 自身执行失败不再撤销正文交付——降级为 pass +
        # Q2 警告 issue 继续管线；确定性 gates 照跑，verified Q0/Q1 仍能阻断。
        llm_call_id, degraded_reason, payload = _qc_run_node_with_degradation(
            self.session,
            prompt_builder=self.prompt_builder,
            llm_runner=self._llm_runner,
            scene=scene,
            state=state,
            scene_id=scene_id,
            bundle=bundle,
            source_draft_row_id=neutral_draft_row_id,
            source_draft_content=neutral_content,
            execution_step_key=execution_step_key,
            step="hard_qc",
            message_prefix="QC",
            degraded_payload_factory=self._degraded_pass_payload,
        )

        payload = self._apply_deterministic_sanity(scene, neutral_content, payload)
        payload = _qc_apply_deterministic_quality_gates(
            scene, neutral_content, payload, qc_type="hard_qc"
        )
        payload = _annotate_qc_issues(scene, neutral_content, payload)
        payload = _promote_constraint_conflicts_to_human_review(payload)
        payload = self._apply_quality_grading(scene, neutral_content, payload)
        validate_qc_report("hard_qc", payload)  # 组合合法性校验（不回写 dump）
        qc_report = self._persist_qc_report(
            scene=scene,
            state=state,
            bundle=bundle,
            neutral_draft_row_id=neutral_draft_row_id,
            neutral_content=neutral_content,
            payload=payload,
        )
        branch = self._branch_for(payload["next_action"])

        # PR-8 §6.6 — 抄袭门(qc pass 时二次裁决):只有确定性 n-gram 抄袭命中（Q0）保留阻断权。
        # (风格参考 v3:中性步位的门只会给 pass / plagiarism,原先 fail / partial 的诊断分支是死代码,已删。)
        if branch == "continue":
            style_verdict = self._apply_style_validation_gate(scene, neutral_content)
            if style_verdict == "plagiarism":
                plagiarism_issue = classify_issue(
                    {
                        "issue_key": "style_plagiarism",
                        "message": "style_reference plagiarism check hit (deterministic n-gram overlap)",
                        "source": "deterministic",
                    },
                    scene=scene,
                    content=neutral_content,
                )
                qc_report.resolution_code = "style_validation_plagiarism"
                qc_report.next_action = "human_review_required"
                qc_report.issues_json = [
                    *(qc_report.issues_json or []),
                    plagiarism_issue,
                ]
                self.session.flush()
                _qc_apply_issue_tracking(state, qc_report.issues_json)
                self._apply_branch_counters(state, "human_review_required")
                _qc_clear_downstream_outputs(state)
                return self._escalate_existing_report(
                    scene=scene,
                    state=state,
                    bundle=bundle,
                    neutral_draft_row_id=neutral_draft_row_id,
                    qc_report=qc_report,
                    branch="human_review_required",
                    failure_reason="style_reference validation found deterministic plagiarism evidence; human review is required.",
                    trigger_reason="style_validation_plagiarism",
                    llm_call_id=llm_call_id,
                    execution_step_key=execution_step_key,
                )

        _qc_apply_issue_tracking(state, payload["issues"])
        self._apply_branch_counters(state, branch)

        circuit_breaker_reason = self._circuit_breaker_reason(state, branch)
        if circuit_breaker_reason is not None:
            return self._escalate_existing_report(
                scene=scene,
                state=state,
                bundle=bundle,
                neutral_draft_row_id=neutral_draft_row_id,
                qc_report=qc_report,
                branch=branch,
                failure_reason=self._failure_reason_for_circuit_breaker(
                    circuit_breaker_reason, branch
                ),
                trigger_reason=circuit_breaker_reason,
                llm_call_id=llm_call_id,
                execution_step_key=execution_step_key,
            )

        if branch == "human_review_required":
            _qc_clear_downstream_outputs(state)
            return self._escalate_existing_report(
                scene=scene,
                state=state,
                bundle=bundle,
                neutral_draft_row_id=neutral_draft_row_id,
                qc_report=qc_report,
                branch=branch,
                failure_reason="hard_qc explicitly requested human review before style generation.",
                trigger_reason="hard_qc_requested_human_review",
                llm_call_id=llm_call_id,
                execution_step_key=execution_step_key,
            )

        if branch == "rewrite_partial":
            _qc_clear_downstream_outputs(state)
            state.scene_status = "hard_qc_partial_rewrite_required"
        elif branch == "rewrite_full":
            _qc_clear_downstream_outputs(state)
            state.scene_status = "hard_qc_full_rewrite_required"

        self._record_attempt(
            scene_id=scene.scene_id,
            chapter_id=scene.chapter_id,
            source_bundle_id=bundle["bundle_id"],
            branch=branch,
            qc_report_id=qc_report.qc_report_id,
            resolution_code=qc_report.resolution_code or "",
            next_action=qc_report.next_action or "",
            human_review_event_id=None,
            llm_call_id=llm_call_id,
            execution_step_key=execution_step_key,
        )
        self.session.flush()
        return HardQcDecision(
            branch=branch,
            qc_report_id=qc_report.qc_report_id,
            human_review_event_id=None,
            resolution_code=qc_report.resolution_code or "",
            next_action=qc_report.next_action or "",
            should_continue=branch == "continue",
            stop_reason=degraded_reason,
            llm_call_id=llm_call_id,
            execution_step_key=execution_step_key,
        )

    @staticmethod
    def _branch_for(next_action: str) -> str:
        return {
            "pass": "continue",
            "partial_rewrite": "rewrite_partial",
            "full_rewrite": "rewrite_full",
            "human_review_required": "human_review_required",
        }[next_action]

    @staticmethod
    def _degraded_pass_payload(
        *,
        issue_key: str,
        message: str,
        continuity_warning: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """QC 执行失败的降级形状：pass + Q2 警告 issue（§5.4——不撤销已有正文）。"""
        issue: dict[str, Any] = {"issue_key": issue_key, "message": message}
        if continuity_warning is not None:
            issue["continuity_warning"] = continuity_warning
        return {
            "resolution_code": "hard_pass",
            "pass_flag": True,
            "next_action": "pass",
            "issues": [issue],
            "rewrite_brief": [],
        }

    def _apply_quality_grading(
        self, scene: SceneCard, neutral_content: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        """Wave 2（§5.4）：统一分级 + 阻断裁决单一来源。

        全部 issue 过分类器（LLM 提案无确定性复核自动降 Q2）。存在 verified
        Q0/Q1 → 保留阻断分支（LLM 说 pass 也升级为 partial_rewrite）；否则任何
        非 pass 意见降级为 pass，原意见以 Q2/Q3 警告随报告交付（G-03：软性
        意见不再让作者无稿可用）。
        """
        classified = classify_issues(
            payload.get("issues") or [], scene=scene, content=neutral_content
        )
        graded = {**payload, "issues": classified}
        if has_blocking(classified):
            if graded.get("next_action") == "pass":
                rewrite_brief = (
                    graded.get("rewrite_brief")
                    if isinstance(graded.get("rewrite_brief"), list)
                    else []
                )
                return {
                    **graded,
                    "resolution_code": "hard_fail_partial",
                    "pass_flag": False,
                    "next_action": "partial_rewrite",
                    "rewrite_brief": rewrite_brief
                    or ["Resolve the verified hard-fact issue before continuing."],
                }
            return graded
        if graded.get("next_action") != "pass":
            return {
                **graded,
                "resolution_code": "hard_pass",
                "pass_flag": True,
                "next_action": "pass",
            }
        return graded

    @staticmethod
    def _serialize_rewrite_brief(
        rewrite_brief: list[str],
        *,
        scene: SceneCard | None = None,
        source_content: str = "",
        issues: list[dict[str, Any]] | None = None,
    ) -> list[dict[str, Any]]:
        issue_blob = _issue_blob(issues or [], rewrite_brief)
        entries: list[dict[str, Any]] = []
        for item in rewrite_brief:
            entry: dict[str, Any] = {"instruction": item}
            if scene is not None:
                blob = f"{item}\n{issue_blob}"
                evidence_spans = _evidence_spans_for_text(source_content, blob)
                conflicts = _constraint_conflicts_for_text(scene, blob)
                if evidence_spans or conflicts:
                    entry["constraint_source"] = "hard_qc"
                    entry["severity"] = "high" if conflicts else "medium"
                    entry["evidence_spans"] = evidence_spans
                    entry["conflicts_with"] = conflicts
            entries.append(entry)
        return entries

    def _apply_deterministic_sanity(
        self, scene: SceneCard, neutral_content: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        issues = payload.get("issues")
        if not isinstance(issues, list) or not issues:
            return payload
        rewrite_brief = payload.get("rewrite_brief")
        issue_blob = _issue_blob(
            issues, rewrite_brief if isinstance(rewrite_brief, list) else []
        )
        filtered = [
            issue
            for issue in issues
            if not (
                isinstance(issue, dict)
                and self._issue_contradicts_deterministic_scene_card(
                    scene, neutral_content, issue, issue_blob
                )
            )
        ]
        if len(filtered) == len(issues):
            return payload
        if filtered:
            return {**payload, "issues": filtered}
        return {
            **payload,
            "resolution_code": "hard_pass",
            "pass_flag": True,
            "next_action": "pass",
            "issues": [],
            "rewrite_brief": [],
        }

    def _issue_contradicts_deterministic_scene_card(
        self,
        scene: SceneCard,
        neutral_content: str,
        issue: dict[str, Any],
        issue_blob: str,
    ) -> bool:
        issue_key = str(issue.get("issue_key") or "").strip()
        if issue_key == "forbidden_text":
            return not contains_forbidden_term(scene.forbidden_text, neutral_content)
        if (
            issue_key in HARD_QC_STYLE_ONLY_ISSUE_KEYS
            or issue_key in HARD_QC_NON_BLOCKING_LLM_ISSUE_KEYS
            or issue_key.startswith("style_")
        ):
            return True
        if issue_key in HARD_QC_REQUIRED_ISSUE_KEYS:
            # 必写内容按组查（批准#11）：意见说的正是没写进去的那一组，就不是被场景卡否定的误报
            if any(
                issue_mentions_source(issue_blob, group)
                for group in required_groups_missing(scene.must_include_text, neutral_content)
            ):
                return False
            return self._source_field_satisfies_reported_issue(
                scene.must_include_text, neutral_content, issue_blob
            ) or any(
                self._source_field_satisfies_reported_issue(
                    source_text, neutral_content, issue_blob
                )
                for source_text in _scene_card_source_texts(scene)
            )
        if issue_key == "unsupported_event":
            return any(
                self._source_field_satisfies_reported_issue(
                    source_text, neutral_content, issue_blob
                )
                for source_text in _scene_card_source_texts(scene)
            )
        if issue_key == "duplicate_text":
            return _reported_duplicate_appears_once(issue_blob, neutral_content)
        return False

    @staticmethod
    def _source_field_satisfies_reported_issue(
        source_text: Any, neutral_content: str, issue_blob: str
    ) -> bool:
        if not isinstance(source_text, str) or not source_text.strip():
            return False
        return source_field_satisfied(
            source_text, neutral_content
        ) and issue_mentions_source(issue_blob, source_text)

    def _apply_style_validation_gate(
        self, scene: SceneCard, neutral_content: str
    ) -> str | None:
        """PR-8 §6.6 — 中性步位稿（中性稿 / style_first 首稿）的抄袭门。

        scene 无作用域 / 无绑定 / 检查失败 → None（qc 结论直通）；否则 "pass" / "plagiarism"。

        v2（规格 §2.W5.5）：这里只裁决确定性 n-gram 抄袭（Q0）；生成禁用词 / 量化容差不对中性步位稿产生
        fail / partial（风格稿的对应检查在 ``run_styled_draft_style_gate``）。风格参考 v3：绑定与否看
        StylePolicy（场景当前 bundle 冻结的契约 → 旧 bundle / 无 bundle 时按当前活动绑定轻量现解析），
        原文重合走唯一抄袭门（按书一次索引、同一稿不重复扫描）。
        """
        from novel_system.services.reference_copy_gate import check_reference_copy

        if not neutral_content or not _scene_has_gate_scope(scene):
            return None
        started_at = time.perf_counter()
        verdict: str | None = None
        profile_id: str | None = None
        binding_id: str | None = None
        runtime_contract_hash: str | None = None
        try:
            policy = scene_gate_style_policy(self.session, scene, None)
            if policy.error_code is not None:
                raise ValueError(policy.error_code)
            if not policy.bound:
                return None
            profile_id = policy.profile_id
            binding_id = policy.binding_id
            runtime_contract_hash = policy.contract_hash
            check = check_reference_copy(self.session, neutral_content, policy=policy)
            verdict = "plagiarism" if check.hits else "pass"
            return verdict
        except (
            Exception
        ):  # noqa: BLE001 — gate 不阻塞主流程，但降级必须可见（审计 P-11）
            _LOGGER.warning(
                "style validation gate degraded for scene %s",
                scene.scene_id,
                exc_info=True,
            )
            return None
        finally:
            # PR-10 §13 — 记录 qc gate 决策事件;无 active binding 时不记录
            if profile_id is not None:
                _record_gate_event(
                    self.session,
                    HARD_QC_GATE_EVENT_KIND,
                    scene_id=scene.scene_id,
                    profile_id=profile_id,
                    binding_id=binding_id,
                    outcome=verdict or "error",
                    started_at=started_at,
                    context={"runtime_contract_hash": runtime_contract_hash},
                )

    def _persist_qc_report(
        self,
        *,
        scene: SceneCard,
        state: SceneRunState,
        bundle: dict[str, Any],
        neutral_draft_row_id: str,
        payload: dict[str, Any],
        neutral_content: str = "",
    ) -> QcReport:
        qc_report = QcReport(
            qc_report_id=_build_qc_report_id(scene.scene_id),
            scene_id=scene.scene_id,
            chapter_id=scene.chapter_id,
            qc_type="hard_qc",
            source_draft_row_id=neutral_draft_row_id,
            source_bundle_id=bundle["bundle_id"],
            resolution_code=payload["resolution_code"],
            pass_flag=1 if payload["pass_flag"] else 0,
            next_action=payload["next_action"],
            issues_json=payload["issues"],
            rewrite_brief_json=self._serialize_rewrite_brief(
                payload["rewrite_brief"],
                scene=scene,
                source_content=neutral_content,
                issues=payload["issues"],
            ),
        )
        self.session.add(qc_report)
        self.session.flush()
        state.current_qc_report_id = qc_report.qc_report_id
        return qc_report

    @staticmethod
    def _apply_branch_counters(state: SceneRunState, branch: str) -> None:
        if branch == "rewrite_partial":
            state.hard_partial_rewrite_count += 1
        elif branch == "rewrite_full":
            state.hard_full_rewrite_count += 1

    @staticmethod
    def _circuit_breaker_reason(state: SceneRunState, branch: str) -> str | None:
        if state.repeat_issue_key and state.repeat_issue_count >= 2:
            return "repeat_issue_key_limit"
        if branch == "rewrite_partial" and state.hard_partial_rewrite_count > 2:
            return "hard_partial_rewrite_limit"
        if branch == "rewrite_full" and state.hard_full_rewrite_count > 1:
            return "hard_full_rewrite_limit"
        if state.total_attempt_count >= state.attempt_budget:
            return "attempt_budget_exhausted"
        return None

    @staticmethod
    def _failure_reason_for_circuit_breaker(trigger_reason: str, branch: str) -> str:
        if trigger_reason == "repeat_issue_key_limit":
            return "hard_qc surfaced the same issue key at least twice; human review is required."
        if trigger_reason == "hard_partial_rewrite_limit":
            return (
                "hard_qc exceeded the partial rewrite limit; human review is required."
            )
        if trigger_reason == "hard_full_rewrite_limit":
            return "hard_qc exceeded the full rewrite limit; human review is required."
        if trigger_reason == "attempt_budget_exhausted":
            return "scene generation exhausted the configured total attempt budget; human review is required."
        return f"hard_qc branch {branch} triggered the generation circuit breaker."

    def _record_attempt(
        self,
        *,
        scene_id: str,
        chapter_id: str,
        source_bundle_id: str,
        branch: str,
        qc_report_id: str,
        resolution_code: str,
        next_action: str,
        human_review_event_id: str | None,
        llm_call_id: str | None = None,
        execution_step_key: str = "hard_qc:0",
        error_code: str | None = None,
        retryable: bool | None = None,
        continuity_warning: dict[str, Any] | None = None,
    ) -> None:
        _qc_record_attempt(
            self.session,
            step="hard_qc",
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
        )

    def _escalate_existing_report(
        self,
        *,
        scene: SceneCard,
        state: SceneRunState,
        bundle: dict[str, Any],
        neutral_draft_row_id: str,
        qc_report: QcReport,
        branch: str,
        failure_reason: str,
        trigger_reason: str,
        continuity_warning: dict[str, Any] | None = None,
        llm_call_id: str | None = None,
        execution_step_key: str = "hard_qc:0",
        error_code: str | None = None,
        retryable: bool | None = None,
    ) -> HardQcDecision:
        replay_context = _with_run_context(
            {
                "scene_id": scene.scene_id,
                "chapter_id": scene.chapter_id,
                "source_bundle_id": bundle["bundle_id"],
                "source_bundle_hash": bundle["bundle_snapshot_hash"],
                "neutral_draft_row_id": neutral_draft_row_id,
                "current_qc_report_id": qc_report.qc_report_id,
                "scene_status_before_block": state.scene_status,
                "total_attempt_count": state.total_attempt_count,
            },
            llm_call_id=llm_call_id,
            error_code=error_code,
            retryable=retryable,
            continuity_warning=continuity_warning,
        )
        event = self._open_generation_blocker(
            scene=scene,
            state=state,
            draft_row_id=neutral_draft_row_id,
            failure_reason=failure_reason,
            trigger_reason=trigger_reason,
            replay_context=replay_context,
        )
        self._record_attempt(
            scene_id=scene.scene_id,
            chapter_id=scene.chapter_id,
            source_bundle_id=bundle["bundle_id"],
            branch="human_review_required",
            qc_report_id=qc_report.qc_report_id,
            resolution_code=qc_report.resolution_code or "",
            next_action=qc_report.next_action or "",
            human_review_event_id=event.event_id,
            llm_call_id=llm_call_id,
            execution_step_key=execution_step_key,
            error_code=error_code,
            retryable=retryable,
            continuity_warning=continuity_warning,
        )
        self.session.flush()
        return HardQcDecision(
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
