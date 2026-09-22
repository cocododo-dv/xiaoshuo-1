"""MaterializationService — profile → binding 行编排(apply)。

apply_profile 只做三件事:校验画像可用(存在 / 未失效 / 未归档)、按 scope 幂等落 binding、
激活画像并幂等建 Strategy C 的 RAG 索引。生成期读的是 profile_json 与冻结进 bundle 的运行时契约,
不再经过 ReviewItem(2026-09-14 减法:review_style_ref_apply_* / review_style_ref_calib_* 的
物化与其早已删除的消费方 services/versioning/review_materialization 一起退役;cleanup 仍清理旧行)。
"""

from __future__ import annotations

import hashlib
import logging
import uuid
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.orm import Session

from novel_system.db.models import ReviewItem
from novel_system.services.errors import DomainError
from novel_system.services.style_reference.repository import StyleReferenceRepository
from novel_system.services.scene_planning_staleness import supersede_for_binding_scope
from novel_system.services.style_reference.schemas import (
    BindingScope,
    BindingStatus,
    InjectionStrategy,
    ProfileStatus,
    TaskType,
)

logger = logging.getLogger(__name__)


# 2026-09-14 减法:ReviewItem 物化(review_style_ref_apply_* / review_style_ref_calib_*)已删除;
# cleanup.purge_derived_data 仍按这两个字面前缀清理旧库里的残留行。


@dataclass
class MaterializeResult:
    """apply_profile 返回结构。"""

    profile_id: str
    binding_id: str
    rag_index: dict[str, Any] = field(default_factory=dict)


class MaterializationService:
    """Profile → binding 行编排(激活画像 + 幂等建 RAG 索引)。"""

    def __init__(self, session: Session) -> None:
        self.session = session
        self.repo = StyleReferenceRepository(session)

    def apply_profile(
        self,
        profile_id: str,
        *,
        scope: BindingScope | str,
        scope_ref_id: str | None,
        task_type: TaskType | str = TaskType.SCENE_GENERATION,
        strategy: InjectionStrategy | str | None = None,
        config_json: dict[str, Any] | None = None,
        build_rag_index: bool = True,
    ) -> MaterializeResult:
        """``config_json`` 落入 binding(intensity / sub_dimensions / include 开关),
        由 InjectionService._render 在注入时消费——前端强度滑块的端到端落点。"""
        # BindingScope 三种 scope(project/scene/character)都按 scope_ref_id 匹配
        # (_binding_rank),缺 ref 的绑定永远 rank=99,是解析不到的死绑定——拒绝落库
        if not (scope_ref_id and str(scope_ref_id).strip()):
            raise DomainError(
                "STYLE_REFERENCE_APPLY_PARAM_INVALID",
                f"scope={_enum_value(scope)} requires a non-empty scope_ref_id",
                status_code=400,
            )
        profile = self.repo.get_profile(profile_id)
        if profile is None:
            raise DomainError(
                "STYLE_REFERENCE_PROFILE_NOT_FOUND",
                f"profile {profile_id!r} not found",
                status_code=404,
            )
        coverage = profile.coverage_json or {}
        if coverage.get("stale"):
            raise DomainError(
                "STYLE_REFERENCE_PROFILE_STALE",
                "profile source findings changed after synthesis; synthesize a new profile before applying",
                status_code=409,
            )
        if profile.status == ProfileStatus.ARCHIVED.value:
            raise DomainError(
                "STYLE_REFERENCE_PROFILE_ARCHIVED",
                "archived profile cannot be applied",
                status_code=409,
            )

        if strategy is None:
            from novel_system.services.style_reference.injection import (
                default_injection_strategy,
            )

            strategy = default_injection_strategy(task_type)

        # 2026-09-14 减法:apply 不再把 finding / calibration 物化成 ReviewItem——它们的消费方
        # services/versioning/review_materialization 早已删除,生成期只读 profile_json 与冻结契约。

        # 3. 写 binding 行
        binding_id = self._upsert_binding(
            profile_id=profile_id,
            scope=scope,
            scope_ref_id=scope_ref_id,
            task_type=task_type,
            strategy=strategy,
            config_json=config_json,
        )
        # 2026-09-22 结构跟随参考书:参考变了(应用 / 重应用 / 调强度),作用范围内已做的场景蓝图、
        # 人物压力蓝图与章架构作废——它们是按旧参考(或没有参考)规划的,下一次运行重做。
        supersede_for_binding_scope(
            self.session,
            scope=_enum_value(scope),
            scope_ref_id=scope_ref_id,
            reason=f"style_binding_applied:{binding_id}",
        )

        # 4. Q1 修复：激活 profile。此前 synthesize 产 DRAFT、apply 只建 active binding，
        #    却从不把 profile 本身置 active；而注入(InjectionService / scene_execution)
        #    硬要求 profile.status=="active"，导致真实流程(导入→抽取→合成→应用)后
        #    风格注入恒为空(no-op)——整个风格参考在生成期失效。apply 即"让该 profile
        #    在某 scope 生效"，随 active binding 一并激活 profile，使绑定与注入一致生效。
        profile.status = ProfileStatus.ACTIVE.value
        self.session.flush()

        # 5. v2 内容克制 RAG 索引就绪检查。新画像在 synthesize 时通常已建好；
        #    老画像或曾中断的部分索引在 apply/re-apply 时自动、幂等升级。向量后端
        #    属于增强能力，失败不得撤销已经合法完成的绑定与 ReviewItem 写入。
        # 2026-09-15:``build_rag_index=False``(HTTP apply 与收件箱「批准应用」走这条)时,
        # 绑定与激活照常落库,RAG 索引留给调用方在事务提交后交给后台 worker
        # (``rag.start_style_reference_rag_index_worker``)——190 万字的书建索引要 35 秒,
        # 不该占着 SQLite 写锁,也该在「参考书活动」面板里看得见。
        if not build_rag_index:
            return MaterializeResult(
                profile_id=profile_id,
                binding_id=binding_id,
                rag_index={
                    "status": "scheduled",
                    "profile_id": profile.profile_id,
                    "book_id": profile.book_id,
                },
            )
        try:
            from novel_system.services.style_reference.rag import ensure_rag_index

            rag_index = ensure_rag_index(
                self.session,
                profile,
                book_id=profile.book_id,
            )
        except Exception:  # noqa: BLE001
            logger.warning(
                "rag index ensure failed for profile %s",
                profile.profile_id,
                exc_info=True,
            )
            rag_index = {"skipped": "build_failed"}

        return MaterializeResult(
            profile_id=profile_id,
            binding_id=binding_id,
            rag_index=rag_index,
        )

    # ------------------------------------------------------------- internals

    def _upsert_binding(
        self,
        *,
        profile_id: str,
        scope: BindingScope | str,
        scope_ref_id: str | None,
        task_type: TaskType | str,
        strategy: InjectionStrategy | str,
        config_json: dict[str, Any] | None = None,
    ) -> str:
        scope_value = _enum_value(scope)
        task_type_value = _enum_value(task_type)
        strategy_value = _enum_value(strategy)
        # 同 (profile, scope, scope_ref_id, task_type) 已存在则复用,
        # 重复 apply 更新 strategy / config(滑块调整后重新应用即生效)
        existing = self.repo.list_bindings(
            profile_id=profile_id, task_type=task_type_value
        )
        for b in existing:
            if b.scope == scope_value and b.scope_ref_id == scope_ref_id:
                b.strategy = strategy_value
                if config_json is not None:
                    b.config_json = config_json
                self.session.flush()
                return b.binding_id
        binding = self.repo.create_binding(
            binding_id=f"sr_bind_{uuid.uuid4().hex[:12]}",
            profile_id=profile_id,
            scope=scope_value,
            scope_ref_id=scope_ref_id,
            task_type=task_type_value,
            strategy=strategy_value,
            config_json=config_json or {},
            status=BindingStatus.ACTIVE.value,
        )
        return binding.binding_id


# ---------------------------------------------------------------------------
# 辅助:finding → item_type 分发规则
# ---------------------------------------------------------------------------





def _enum_value(value: Any) -> str:
    if hasattr(value, "value"):
        return value.value
    return str(value)
