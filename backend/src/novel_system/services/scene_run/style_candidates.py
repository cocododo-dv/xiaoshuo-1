"""风格稿候选：工作项（每个槽位的底稿 / 成稿子检查点）、候选读回与复验、Best-of-N 份数、匿名终选门。

``Orchestrator`` 直接继承 ``StyleCandidatesMixin``。只有 style_first 的 bundle 起多份候选（[批准#2]）；
``_best_of_n_count`` 测试在类或实例上覆盖。
"""

from __future__ import annotations

from copy import deepcopy
import logging
import random
from typing import Any
from uuid import uuid4

from sqlalchemy import select

from novel_system.db.models import (
    AttemptTracker,
    HumanReviewEvent,
    LlmCall,
    LlmCallAttempt,
    SceneDraft,
)
from novel_system.services.scene_generation import LINEAGE_FIRST_DRAFT_ACCEPTED, StyleGenerationResult
from novel_system.services.scene_run_checkpoint import checkpoint_corrupt
from novel_system.services.style_policy import style_policy_for_bundle
from novel_system.settings import get_settings

_LOGGER = logging.getLogger(__name__)


class StyleCandidatesMixin:
    @staticmethod
    def _style_slot_identity(slot_key: str) -> tuple[str, int]:
        # [批准#2] 补候选（topup:N）随先中性后润色的多稿删掉，工作项只有 initial:N 槽位
        parts = slot_key.split(":") if isinstance(slot_key, str) else []
        if len(parts) != 2 or parts[0] != "initial" or not parts[1].isdigit():
            raise checkpoint_corrupt("style work-item slot key is invalid")
        return parts[0], int(parts[1])

    def _style_artifact_descriptor(
        self,
        product: StyleGenerationResult,
        *,
        phase: str,
        source_neutral_draft_row_id: str,
        source_base_row_id: str | None,
    ) -> dict[str, Any]:
        row = self.session.get(SceneDraft, product.row_id)
        if row is None:
            self._raise_checkpoint_output_missing(row_id=product.row_id)
        assert row is not None
        owner = product.artifact_execution_id or self._execution_id
        if not isinstance(owner, str):
            raise checkpoint_corrupt("style artifact owner is missing")
        descriptor = {
            "phase": phase,
            "row_id": product.row_id,
            "content_hash": self._text_hash(product.content),
            "stage": row.stage,
            "source_neutral_draft_row_id": source_neutral_draft_row_id,
            "source_base_row_id": source_base_row_id,
            "bundle_id": product.bundle_id,
            "bundle_hash": product.bundle_hash,
            "llm_call_id": product.llm_call_id,
            "execution_step_key": product.execution_step_key,
            "artifact_execution_id": owner,
        }
        if product.lineage == LINEAGE_FIRST_DRAFT_ACCEPTED:
            # 风格参考 v3（P5b）：首稿即风格稿——没有自己的模型调用，谱系沿用首稿那次调用
            descriptor["lineage"] = LINEAGE_FIRST_DRAFT_ACCEPTED
        return descriptor

    def _update_style_work_item(
        self,
        work_items: list[dict[str, Any]],
        *,
        slot_key: str,
        phase: str,
        product: StyleGenerationResult,
        metadata: dict[str, Any],
        neutral_draft_row_id: str,
    ) -> None:
        kind, slot_index = self._style_slot_identity(slot_key)
        slot_order = metadata.get("slot_order")
        if (
            phase not in {"base", "final"}
            or not isinstance(slot_order, int)
            or slot_order < 0
            or metadata.get("source_neutral_draft_row_id") != neutral_draft_row_id
        ):
            raise checkpoint_corrupt("style product callback metadata is invalid")
        item = next(
            (
                candidate
                for candidate in work_items
                if candidate.get("slot_key") == slot_key
            ),
            None,
        )
        if phase == "base":
            if item is not None or slot_order != len(work_items):
                raise checkpoint_corrupt("style base phase is not a monotonic prefix")
            descriptor = self._style_artifact_descriptor(
                product,
                phase="base",
                source_neutral_draft_row_id=neutral_draft_row_id,
                source_base_row_id=None,
            )
            if descriptor["stage"] != "style_draft":
                raise checkpoint_corrupt("style base product has the wrong stage")
            work_items.append(
                {
                    "slot_key": slot_key,
                    "slot_order": slot_order,
                    "kind": kind,
                    "slot_index": slot_index,
                    "base": descriptor,
                    "gate_decision": None,
                    "de_template_outcome": None,
                    "final": None,
                }
            )
            return
        if (
            item is None
            or item.get("slot_order") != slot_order
            or item.get("final") is not None
        ):
            raise checkpoint_corrupt("style final phase has no matching base prefix")
        gate_decision = metadata.get("gate_decision")
        de_template_outcome = metadata.get("de_template_outcome")
        source_base_row_id = metadata.get("source_base_row_id")
        if (
            not isinstance(gate_decision, dict)
            or not isinstance(gate_decision.get("triggered"), bool)
            or not isinstance(de_template_outcome, dict)
            or de_template_outcome.get("status")
            not in {"not_required", "completed", "failed", "rejected"}
            or source_base_row_id != item["base"]["row_id"]
            or (
                not gate_decision["triggered"]
                and de_template_outcome.get("status") != "not_required"
            )
            or (
                gate_decision["triggered"]
                and de_template_outcome.get("status") == "not_required"
            )
        ):
            raise checkpoint_corrupt("style final gate/source metadata is invalid")
        if de_template_outcome["status"] == "completed" and (
            de_template_outcome.get("llm_call_id") != product.llm_call_id
            or de_template_outcome.get("execution_step_key")
            != product.execution_step_key
            or de_template_outcome.get("accounting_status") != "settled"
        ):
            raise checkpoint_corrupt("completed de-template outcome is invalid")
        if de_template_outcome["status"] == "failed" and (
            not isinstance(de_template_outcome.get("llm_call_id"), str)
            or not isinstance(de_template_outcome.get("execution_step_key"), str)
            or not isinstance(de_template_outcome.get("artifact_execution_id"), str)
            or de_template_outcome.get("accounting_status")
            not in {"failed", "rejected"}
            or not isinstance(de_template_outcome.get("error_code"), str)
        ):
            raise checkpoint_corrupt("failed de-template outcome is invalid")
        if de_template_outcome["status"] == "rejected" and (
            not isinstance(de_template_outcome.get("llm_call_id"), str)
            or not isinstance(de_template_outcome.get("execution_step_key"), str)
            or not isinstance(de_template_outcome.get("artifact_execution_id"), str)
            or de_template_outcome.get("accounting_status") != "settled"
            or not isinstance(de_template_outcome.get("row_id"), str)
            or not isinstance(de_template_outcome.get("acceptance"), dict)
            or de_template_outcome["acceptance"].get("accepted") is not False
        ):
            raise checkpoint_corrupt("rejected de-template outcome is invalid")
        descriptor = self._style_artifact_descriptor(
            product,
            phase="final",
            source_neutral_draft_row_id=neutral_draft_row_id,
            source_base_row_id=source_base_row_id,
        )
        expected_stage = (
            "de_template"
            if de_template_outcome["status"] == "completed"
            else "style_draft"
        )
        if descriptor["stage"] != expected_stage:
            raise checkpoint_corrupt("style final product contradicts its gate")
        item["gate_decision"] = deepcopy(gate_decision)
        item["de_template_outcome"] = deepcopy(de_template_outcome)
        item["final"] = descriptor

    def _validate_style_artifact_descriptor(
        self,
        descriptor: Any,
        *,
        scene_id: str,
        bundle: dict[str, Any],
        expected_phase: str,
        expected_stage: str,
        expected_step_key: str,
        source_neutral_draft_row_id: str,
        source_base_row_id: str | None,
    ) -> StyleGenerationResult:
        if not isinstance(descriptor, dict):
            raise checkpoint_corrupt("style artifact descriptor is invalid")
        row_id = descriptor.get("row_id")
        row = self._require_checkpoint_row(SceneDraft, row_id)
        owner = self._validate_artifact_execution_owner(
            descriptor.get("artifact_execution_id")
        )
        self._validate_checkpoint_llm_output(
            scene_id=scene_id,
            llm_call_id=descriptor.get("llm_call_id"),
            execution_step_key=descriptor.get("execution_step_key"),
            execution_id=owner,
        )
        accepted_lineage = descriptor.get("lineage") == LINEAGE_FIRST_DRAFT_ACCEPTED
        if accepted_lineage:
            # 风格参考 v3（P5b）：首稿即风格稿的产品没有自己的模型调用——它的调用就是首稿那次（步位键是首稿的），
            # 正文必须与首稿逐字相同；其余身份照常校验。
            neutral_row = self.session.get(SceneDraft, source_neutral_draft_row_id)
            if (
                expected_stage != "style_draft"
                or neutral_row is None
                or neutral_row.scene_id != scene_id
                or neutral_row.generation_llm_call_id != descriptor.get("llm_call_id")
                or self._text_hash(neutral_row.content) != descriptor.get("content_hash")
            ):
                raise checkpoint_corrupt(
                    "accepted first-draft style product is detached from its first draft",
                )
            expected_step_key = str(descriptor.get("execution_step_key") or "")
        if (
            descriptor.get("phase") != expected_phase
            or descriptor.get("stage") != expected_stage
            or descriptor.get("execution_step_key") != expected_step_key
            or descriptor.get("source_neutral_draft_row_id")
            != source_neutral_draft_row_id
            or descriptor.get("source_base_row_id") != source_base_row_id
            or descriptor.get("bundle_id") != bundle["bundle_id"]
            or descriptor.get("bundle_hash") != bundle["bundle_snapshot_hash"]
            or row.scene_id != scene_id
            or row.stage != expected_stage
            or row.source_bundle_id != bundle["bundle_id"]
            or row.source_bundle_hash != bundle["bundle_snapshot_hash"]
            or row.generation_llm_call_id != descriptor.get("llm_call_id")
            or self._text_hash(row.content) != descriptor.get("content_hash")
        ):
            raise checkpoint_corrupt("style artifact identity/source/hash mismatch")
        attempt_step = (
            "de_template" if expected_stage == "de_template" else "style_draft"
        )
        matching_attempts = []
        for attempt in self.session.execute(
            select(AttemptTracker).where(
                AttemptTracker.scene_id == scene_id,
                AttemptTracker.step == attempt_step,
                AttemptTracker.status == "completed",
                AttemptTracker.source_bundle_id == bundle["bundle_id"],
            )
        ).scalars():
            details = attempt.details_json or {}
            expected_source_key = (
                "source_style_draft_row_id"
                if expected_stage == "de_template"
                else "source_draft_row_id"
            )
            expected_source = (
                source_base_row_id
                if expected_stage == "de_template"
                else source_neutral_draft_row_id
            )
            if (
                details.get("row_id") == row.row_id
                and details.get("llm_call_id") == row.generation_llm_call_id
                and details.get(expected_source_key) == expected_source
            ):
                matching_attempts.append(attempt)
        if len(matching_attempts) != 1:
            raise checkpoint_corrupt("style artifact attempt ledger is invalid")
        attempt_details = matching_attempts[0].details_json or {}
        return StyleGenerationResult(
            row_id=row.row_id,
            content=row.content,
            llm_call_id=row.generation_llm_call_id,
            bundle_id=bundle["bundle_id"],
            bundle_hash=bundle["bundle_snapshot_hash"],
            execution_step_key=expected_step_key,
            artifact_execution_id=owner,
            lineage=LINEAGE_FIRST_DRAFT_ACCEPTED if accepted_lineage else None,
            style_step=(
                deepcopy(attempt_details["style_step"])
                if isinstance(attempt_details.get("style_step"), dict)
                else None
            ),
        )

    def _validate_style_work_items(
        self,
        work_items: Any,
        *,
        scene_id: str,
        expected_initial_count: int,
        require_complete: bool,
    ) -> list[tuple[StyleGenerationResult, StyleGenerationResult | None]]:
        if not isinstance(work_items, list) or (require_complete and not work_items):
            raise checkpoint_corrupt("style work-item cursor is invalid")
        bundle = self._load_checkpoint_bundle(scene_id)
        neutral_row_id = self._checkpoint_artifact(
            "neutral_draft_row_id", expected_node_at_least="neutral_ready"
        )
        products: list[tuple[StyleGenerationResult, StyleGenerationResult | None]] = []
        saw_partial = False
        for order, item in enumerate(work_items):
            if (
                saw_partial
                or order >= expected_initial_count
                or not isinstance(item, dict)
                or item.get("slot_key") != f"initial:{order}"
                or item.get("slot_order") != order
                or item.get("kind") != "initial"
                or item.get("slot_index") != order
            ):
                raise checkpoint_corrupt("style work-item prefix/slot identity is invalid")
            base_step_key = f"style_draft:{order}"
            base = self._validate_style_artifact_descriptor(
                item.get("base"),
                scene_id=scene_id,
                bundle=bundle,
                expected_phase="base",
                expected_stage="style_draft",
                expected_step_key=base_step_key,
                source_neutral_draft_row_id=neutral_row_id,
                source_base_row_id=None,
            )
            final_descriptor = item.get("final")
            if final_descriptor is None:
                if (
                    require_complete
                    or item.get("gate_decision") is not None
                    or item.get("de_template_outcome") is not None
                    or order != len(work_items) - 1
                ):
                    raise checkpoint_corrupt("style work-item final prefix is incomplete")
                saw_partial = True
                products.append((base, None))
                continue
            gate = item.get("gate_decision")
            outcome = item.get("de_template_outcome")
            if (
                not isinstance(gate, dict)
                or not isinstance(gate.get("triggered"), bool)
                or not isinstance(outcome, dict)
                or outcome.get("status")
                not in {"not_required", "completed", "failed", "rejected"}
                or (not gate["triggered"] and outcome.get("status") != "not_required")
                or (gate["triggered"] and outcome.get("status") == "not_required")
            ):
                raise checkpoint_corrupt("style work-item gate decision is invalid")
            final_stage = (
                "de_template" if outcome["status"] == "completed" else "style_draft"
            )
            final_step_key = (
                f"{base_step_key}:de_template"
                if outcome["status"] == "completed"
                else base_step_key
            )
            final = self._validate_style_artifact_descriptor(
                final_descriptor,
                scene_id=scene_id,
                bundle=bundle,
                expected_phase="final",
                expected_stage=final_stage,
                expected_step_key=final_step_key,
                source_neutral_draft_row_id=neutral_row_id,
                source_base_row_id=base.row_id,
            )
            if outcome["status"] in {"not_required", "failed", "rejected"} and (
                final.row_id != base.row_id
                or final.llm_call_id != base.llm_call_id
                or final.content != base.content
            ):
                raise checkpoint_corrupt("fallback style product is not base=final")
            if outcome["status"] == "completed" and final.row_id == base.row_id:
                raise checkpoint_corrupt("de-template product does not have independent lineage")
            if outcome["status"] == "completed" and (
                outcome.get("llm_call_id") != final.llm_call_id
                or outcome.get("execution_step_key") != final.execution_step_key
                or outcome.get("artifact_execution_id") != final.artifact_execution_id
                or outcome.get("accounting_status") != "settled"
            ):
                raise checkpoint_corrupt("completed de-template outcome is misbound")
            if outcome["status"] == "failed":
                self._validate_failed_style_de_template_outcome(
                    outcome,
                    scene_id=scene_id,
                    bundle=bundle,
                    execution_step_key=f"{base_step_key}:de_template",
                    source_base_row_id=base.row_id,
                )
            if outcome["status"] == "rejected":
                self._validate_rejected_style_de_template_outcome(
                    outcome,
                    scene_id=scene_id,
                    bundle=bundle,
                    execution_step_key=f"{base_step_key}:de_template",
                    source_base_row_id=base.row_id,
                )
            products.append((base, final))
        return products

    def _validate_rejected_style_de_template_outcome(
        self,
        outcome: dict[str, Any],
        *,
        scene_id: str,
        bundle: dict[str, Any],
        execution_step_key: str,
        source_base_row_id: str,
    ) -> None:
        owner = self._validate_artifact_execution_owner(
            outcome.get("artifact_execution_id")
        )
        call = self._validate_checkpoint_llm_output(
            scene_id=scene_id,
            llm_call_id=outcome.get("llm_call_id"),
            execution_step_key=outcome.get("execution_step_key"),
            execution_id=owner,
        )
        row_id = outcome.get("row_id")
        row = self.session.get(SceneDraft, row_id) if isinstance(row_id, str) else None
        acceptance = outcome.get("acceptance")
        if (
            call.step != "de_template"
            or outcome.get("status") != "rejected"
            or outcome.get("execution_step_key") != execution_step_key
            or outcome.get("accounting_status") != "settled"
            or not isinstance(acceptance, dict)
            or acceptance.get("accepted") is not False
            or row is None
            or row.scene_id != scene_id
            or row.stage != "de_template"
            or row.status != "rejected"
            or row.source_bundle_id != bundle["bundle_id"]
            or row.source_bundle_hash != bundle["bundle_snapshot_hash"]
            or row.generation_llm_call_id != call.llm_call_id
        ):
            raise checkpoint_corrupt("rejected de-template artifact ledger is invalid")
        matching_attempts = []
        for attempt in self.session.execute(
            select(AttemptTracker).where(
                AttemptTracker.scene_id == scene_id,
                AttemptTracker.step == "de_template",
                AttemptTracker.status == "completed",
                AttemptTracker.source_bundle_id == bundle["bundle_id"],
            )
        ).scalars():
            details = attempt.details_json or {}
            if (
                details.get("row_id") == row.row_id
                and details.get("llm_call_id") == call.llm_call_id
                and details.get("source_style_draft_row_id")
                == source_base_row_id
                and details.get("acceptance") == acceptance
            ):
                matching_attempts.append(attempt)
        if len(matching_attempts) != 1:
            raise checkpoint_corrupt("rejected de-template attempt is missing or duplicated")

    def _validate_failed_style_de_template_outcome(
        self,
        outcome: dict[str, Any],
        *,
        scene_id: str,
        bundle: dict[str, Any],
        execution_step_key: str,
        source_base_row_id: str,
    ) -> None:
        owner = self._validate_artifact_execution_owner(
            outcome.get("artifact_execution_id")
        )
        llm_call_id = outcome.get("llm_call_id")
        call = (
            self.session.get(LlmCall, llm_call_id)
            if isinstance(llm_call_id, str)
            else None
        )
        if (
            call is None
            or call.scene_id != scene_id
            or call.step != "de_template"
            or call.execution_id != owner
            or call.execution_step_key != execution_step_key
            or call.accounting_status not in {"failed", "rejected"}
            or outcome.get("execution_step_key") != execution_step_key
            or outcome.get("accounting_status") != call.accounting_status
            or not isinstance(outcome.get("error_code"), str)
            or outcome.get("error_code") != call.error_code
            or (
                call.accounting_status == "failed"
                and call.request_dispatched_at is None
            )
            or (
                call.accounting_status == "rejected"
                and call.request_dispatched_at is not None
            )
        ):
            raise checkpoint_corrupt("failed de-template call ledger is invalid")
        provider_attempts = (
            self.session.execute(
                select(LlmCallAttempt).where(
                    LlmCallAttempt.llm_call_id == call.llm_call_id
                )
            )
            .scalars()
            .all()
        )
        if provider_attempts:
            ordinals = [attempt.provider_attempt_no for attempt in provider_attempts]
            numeric_fields = (
                "estimated_tokens",
                "reserved_tokens",
                "budget_charged_tokens",
                "prompt_tokens",
                "completion_tokens",
                "total_tokens",
                "latency_ms",
            )
            aggregate_fields = numeric_fields
            if (
                sorted(ordinals) != list(range(len(ordinals)))
                or any(
                    attempt.accounting_status
                    in {"reserved", "usage_exceeds_reservation"}
                    or any(
                        not isinstance(getattr(attempt, field), int)
                        or getattr(attempt, field) < 0
                        for field in numeric_fields
                    )
                    or attempt.budget_charged_tokens > attempt.reserved_tokens
                    or attempt.total_tokens
                    != attempt.prompt_tokens + attempt.completion_tokens
                    for attempt in provider_attempts
                )
                or any(
                    getattr(call, field)
                    != sum(getattr(attempt, field) for attempt in provider_attempts)
                    for field in aggregate_fields
                )
                or (
                    call.accounting_status == "failed"
                    and not any(
                        attempt.request_dispatched_at is not None
                        for attempt in provider_attempts
                    )
                )
                or (
                    call.accounting_status == "rejected"
                    and any(
                        attempt.request_dispatched_at is not None
                        for attempt in provider_attempts
                    )
                )
            ):
                raise checkpoint_corrupt("failed de-template provider-attempt ledger is invalid")
        failed_attempts = []
        for attempt in self.session.execute(
            select(AttemptTracker).where(
                AttemptTracker.scene_id == scene_id,
                AttemptTracker.step == "de_template",
                AttemptTracker.status == "failed",
                AttemptTracker.source_bundle_id == bundle["bundle_id"],
            )
        ).scalars():
            details = attempt.details_json or {}
            if (
                details.get("llm_call_id") == call.llm_call_id
                and details.get("source_draft_row_id") == source_base_row_id
                and details.get("error_code") == outcome.get("error_code")
            ):
                failed_attempts.append(attempt)
        if len(failed_attempts) != 1:
            raise checkpoint_corrupt("failed de-template attempt is missing or duplicated")

    def _load_partial_style_work_items(
        self,
        scene_id: str,
        *,
        expected_initial_count: int,
    ) -> list[dict[str, Any]]:
        state = self._active_checkpoint_state()
        payload = state.run_checkpoint_json or {}
        refs = payload.get("artifact_refs") if isinstance(payload, dict) else None
        work_items = refs.get("style_work_items") if isinstance(refs, dict) else None
        if work_items is None:
            return []
        if (
            state.run_checkpoint != "hard_qc_ready"
            or refs.get("style_initial_candidate_count") != expected_initial_count
            or self._json_hash(work_items) != self._checkpoint_hash("style_work_items")
        ):
            raise checkpoint_corrupt("partial style work-item cursor is invalid")
        products = self._validate_style_work_items(
            work_items,
            scene_id=scene_id,
            expected_initial_count=expected_initial_count,
            require_complete=False,
        )
        last_order = len(products) - 1
        last_phase = 1 if products and products[-1][1] is not None else 0
        if payload.get("sub_index") != last_order * 2 + last_phase:
            raise checkpoint_corrupt("partial style subcursor does not match its phase")
        return deepcopy(work_items)

    def _style_resume_products(
        self,
        work_items: list[dict[str, Any]],
        *,
        scene_id: str,
    ) -> tuple[dict[str, StyleGenerationResult], dict[str, StyleGenerationResult]]:
        if not work_items:
            return {}, {}
        initial_count = len(work_items)
        products = self._validate_style_work_items(
            work_items,
            scene_id=scene_id,
            expected_initial_count=initial_count,
            require_complete=False,
        )
        bases: dict[str, StyleGenerationResult] = {}
        finals: dict[str, StyleGenerationResult] = {}
        for item, (base, final) in zip(work_items, products, strict=True):
            if final is None:
                bases[item["slot_key"]] = base
            else:
                finals[item["slot_key"]] = final
        return bases, finals

    def _load_style_checkpoint_candidates(
        self, scene_id: str
    ) -> list[StyleGenerationResult]:
        row_ids = self._checkpoint_artifact(
            "candidate_row_ids", expected_node_at_least="style_ready"
        )
        selected = self._checkpoint_artifact(
            "style_draft_row_id", expected_node_at_least="style_ready"
        )
        if not isinstance(row_ids, list) or not row_ids or row_ids[0] != selected:
            raise checkpoint_corrupt("style candidate checkpoint is invalid")
        work_items = self._checkpoint_artifact(
            "style_work_items", expected_node_at_least="style_ready"
        )
        initial_count = self._checkpoint_artifact(
            "style_initial_candidate_count", expected_node_at_least="style_ready"
        )
        if (
            not isinstance(initial_count, int)
            or initial_count < 1
            or self._json_hash(work_items) != self._checkpoint_hash("style_work_items")
        ):
            raise checkpoint_corrupt("style work-item completion ledger is invalid")
        lineage_products = self._validate_style_work_items(
            work_items,
            scene_id=scene_id,
            expected_initial_count=initial_count,
            require_complete=True,
        )
        final_by_row_id = {
            final.row_id: final
            for _base, final in lineage_products
            if final is not None
        }
        if len(final_by_row_id) != len(lineage_products) or set(row_ids) != set(
            final_by_row_id
        ):
            raise checkpoint_corrupt("style candidate ordering is detached from work-item lineage")
        results: list[StyleGenerationResult] = []
        bundle = self._load_checkpoint_bundle(scene_id)
        llm_call_ids = self._checkpoint_artifact(
            "llm_call_ids", expected_node_at_least="style_ready"
        )
        step_keys = self._checkpoint_artifact(
            "style_execution_step_keys", expected_node_at_least="style_ready"
        )
        execution_ids = self._checkpoint_artifact(
            "style_artifact_execution_ids", expected_node_at_least="style_ready"
        )
        checkpoint_payload = self._active_checkpoint_state().run_checkpoint_json or {}
        checkpoint_refs = checkpoint_payload.get("artifact_refs") or {}
        checkpoint_hashes = checkpoint_payload.get("artifact_hashes") or {}
        ranking_audits = checkpoint_refs.get("style_candidate_rankings")
        if (
            not isinstance(llm_call_ids, list)
            or not isinstance(step_keys, list)
            or len(llm_call_ids) != len(row_ids)
            or len(step_keys) != len(row_ids)
            or not isinstance(execution_ids, list)
            or len(execution_ids) != len(row_ids)
        ):
            raise checkpoint_corrupt("style candidate ledger references are invalid")
        if ranking_audits is not None and (
            not isinstance(ranking_audits, list)
            or len(ranking_audits) != len(row_ids)
            or self._json_hash(ranking_audits)
            != checkpoint_hashes.get("style_candidate_rankings")
        ):
            raise checkpoint_corrupt("style candidate ranking audit is invalid")
        for index, row_id in enumerate(row_ids):
            lineage_result = final_by_row_id.get(row_id)
            if lineage_result is None:
                raise checkpoint_corrupt("style candidate has no final work item")
            row = self._require_checkpoint_row(SceneDraft, row_id)
            self._validate_checkpoint_llm_output(
                scene_id=scene_id,
                llm_call_id=llm_call_ids[index],
                execution_step_key=step_keys[index],
                execution_id=self._validate_artifact_execution_owner(
                    execution_ids[index]
                ),
            )
            if (
                row.scene_id != scene_id
                or row.stage not in {"style_draft", "de_template"}
                or row.source_bundle_id != bundle["bundle_id"]
                or row.source_bundle_hash != bundle["bundle_snapshot_hash"]
                or row.generation_llm_call_id != llm_call_ids[index]
                or lineage_result.llm_call_id != llm_call_ids[index]
                or lineage_result.execution_step_key != step_keys[index]
                or lineage_result.artifact_execution_id != execution_ids[index]
                or self._text_hash(row.content)
                != self._checkpoint_hash(f"style_ready_candidate_{index}")
            ):
                raise checkpoint_corrupt("style candidate identity/source/hash mismatch")
            if index == 0 and self._text_hash(row.content) != self._checkpoint_hash(
                "selected_draft"
            ):
                raise checkpoint_corrupt("selected style draft hash mismatch")
            results.append(
                StyleGenerationResult(
                    row_id=row.row_id,
                    content=row.content,
                    llm_call_id=llm_call_ids[index],
                    bundle_id=bundle["bundle_id"],
                    bundle_hash=bundle["bundle_snapshot_hash"],
                    execution_step_key=step_keys[index],
                    artifact_execution_id=execution_ids[index],
                    ranking_audit=(
                        deepcopy(ranking_audits[index])
                        if isinstance(ranking_audits, list)
                        and isinstance(ranking_audits[index], dict)
                        else None
                    ),
                    lineage=lineage_result.lineage,
                    style_step=deepcopy(lineage_result.style_step),
                )
            )
        return results

    def _load_selected_style_checkpoint(self, scene_id: str) -> StyleGenerationResult:
        candidates = self._load_style_checkpoint_candidates(scene_id)
        refs = (self._active_checkpoint_state().run_checkpoint_json or {}).get(
            "artifact_refs",
            {},
        )
        selected_row_id = refs.get("selected_row_id")
        if selected_row_id is None:
            return candidates[0]
        selected = [
            candidate for candidate in candidates if candidate.row_id == selected_row_id
        ]
        if len(selected) != 1:
            raise checkpoint_corrupt("selected style candidate is absent or ambiguous in the durable prefix")
        return selected[0]

    def _best_of_n_count(self, contract, *, criticality=None) -> int:
        """Number of style-draft candidates the switch and the scene's criticality allow.

        Best-of-N is a plain opt-in switch (``NOVEL_SYSTEM_SCENE_BEST_OF_N_ENABLED``, default off → one
        candidate). When enabled the scene's criticality decides: transition scenes draft one, standard
        scenes ``initial_best_of_n`` (2), critical scenes 3 and pause at the blinded author terminal
        selection (``human_gate``). Only a style_first bundle drafts several — the pipeline caps every
        other draft mode at one ([批准#2]). Tests may still override this method directly.
        """
        try:
            enabled = bool(getattr(get_settings(), "scene_best_of_n_enabled", False))
        except Exception:  # noqa: BLE001 — settings 读不到就按关闭
            enabled = False
        if not enabled or criticality is None:
            return 1
        return max(1, int(getattr(criticality, "initial_best_of_n", 1) or 1))

    @staticmethod
    def _distinct_candidate_count(candidates: list[Any]) -> int:
        """候选里不同（非空）正文的份数（终选门按正文去重，见 :meth:`_offer_candidates_for_selection`）。"""
        return len({(getattr(cand, "content", "") or "").strip() for cand in candidates} - {""})

    def _offer_candidates_for_selection(
        self, scene, state, bundle, candidates
    ) -> list[str] | None:
        """Wave 3（§4.4/§5.5）：确定性坏稿淘汰后建立匿名候选终选 gate。

        机器只淘汰空文本与抄袭门（``reference_copy_gate``，候选排序时已查、结论在 ``ranking_audit``）确认抄了
        参考书原文的候选（不按机器分数删，§4.4；受保护专名只提醒、从不淘汰，[批准#12]）；全部无效时返回 None——
        管线继续，由 QC 层裁决，不装作可选。候选按正文去重后不到两份时调用方根本不开这道门
        （:meth:`_distinct_candidate_count`）。
        blinded_order 是随机置换（§5.5 展示顺序必须随机化并记录）。

        风格参考 v3（S2 a）：有绑定时，抄袭门「没检查成」的候选（``plagiarism_checked`` 不为 True——读数 / 抄袭门
        抛过异常）也不交给作者盲选（fail-closed；成稿门仍是最后一道）；未绑定的场景没有抄袭门，照旧交付。
        """
        style_bound = bool(getattr(style_policy_for_bundle(bundle), "bound", False))
        valid_candidates: list[Any] = []
        offered_texts: set[str] = set()
        for cand in candidates:
            content = (getattr(cand, "content", "") or "").strip()
            if not content:
                continue
            if content in offered_texts:
                # 风格参考 v3（P5b）：作者手笔直起时没过门的修改槽位保留首稿原文——同样的正文只给作者看一次
                continue
            ranking = getattr(cand, "ranking_audit", None) or {}
            if (
                ranking.get("plagiarism_checked") is True
                and ranking.get("plagiarism_passed") is False
            ):
                continue
            if style_bound and ranking.get("plagiarism_checked") is not True:
                _LOGGER.warning(
                    "candidate %s of scene %s was never copy-checked; not offered for blind selection",
                    getattr(cand, "row_id", None),
                    scene.scene_id,
                )
                continue
            offered_texts.add(content)
            valid_candidates.append(cand)
        valid_row_ids = [str(candidate.row_id) for candidate in valid_candidates]
        if not valid_row_ids:
            _LOGGER.warning(
                "no deterministically valid candidate to offer for scene %s; pipeline continues",
                scene.scene_id,
            )
            return None
        blinded_order = list(valid_row_ids)
        random.shuffle(blinded_order)
        event = HumanReviewEvent(
            event_id=f"hre_sel_{uuid4().hex[:12]}",
            scene_id=scene.scene_id,
            chapter_id=scene.chapter_id,
            object_ref=f"candidate_selection:{scene.scene_id}",
            event_source="candidate_selection",
            priority="high",
            status="awaiting_review",
            # 终选一次写入，不再有「重开改选」（重评 R2 复核补充 4）
            allowed_actions_json=["select"],
            details_json={
                "gate_type": "style_candidate_selection",
                "candidate_row_ids": valid_row_ids,
                "blinded_order": blinded_order,
                "decision_status": "awaiting",
                "selected_row_id": None,
                "tokens_used": int(state.scene_tokens_used or 0),
                "decision_history": [],
            },
            default_action="select",
        )
        self.session.add(event)
        state.scene_status = "awaiting_candidate_selection"
        state.current_human_review_event_id = event.event_id
        self.session.flush()
        return valid_row_ids
