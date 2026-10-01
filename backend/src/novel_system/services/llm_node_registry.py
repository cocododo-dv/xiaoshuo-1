from __future__ import annotations

from dataclasses import dataclass, fields
from typing import Any, Literal


# 节点注册表是模型路由默认值的唯一来源(2026-09-30 重评 R4:config/models.yaml 的 task_routing 逐字段抄了一份,已删):
# 没有 models 快照时每个节点的路由、快照路由缺的参数,都取这里的 spec。
# 重评 R15b:四个从不调模型的「保留」节点(章节摘要 / 连续性压缩 / 归档与索引 / 章节汇总)删了,
# 注册表里只剩真正调模型的节点。status / requires_llm 仍随目录与路由载荷发出(设置页按它们筛选),取值只有这一种。
NodeStatus = Literal["active"]


@dataclass(frozen=True, slots=True)
class LLMNodeSpec:
    node_id: str
    label: str
    group: str
    status: NodeStatus = "active"
    requires_llm: bool = True
    template_name: str | None = None
    provider: str = "openai_compatible"
    model: str = "gpt-5"
    temperature: float = 0.2
    max_output_tokens: int = 2200
    response_format: str = "json_object"
    reasoning_level: str = "medium"
    api_mode: str = "responses"
    # §7 anti-mean sampling — decoding-level penalties; a DB node route that does not carry them
    # gets them from here at parse time (route_defaults), so the UI path never drops them to None.
    frequency_penalty: float | None = None
    presence_penalty: float | None = None
    top_p: float | None = None

    def catalog_entry(self, order: int) -> dict[str, Any]:
        return {
            "node_id": self.node_id,
            "label": self.label,
            "group": self.group,
            "status": self.status,
            "requires_llm": self.requires_llm,
            "template_name": self.template_name,
            "default_provider": self.provider,
            "default_model": self.model,
            "temperature": self.temperature,
            "max_output_tokens": self.max_output_tokens,
            "response_format": self.response_format,
            "reasoning_level": self.reasoning_level,
            "api_mode": self.api_mode,
            "frequency_penalty": self.frequency_penalty,
            "presence_penalty": self.presence_penalty,
            "top_p": self.top_p,
            "order": order,
        }

    def route_defaults(self) -> dict[str, Any]:
        """节点的默认参数:温度、输出预算、响应格式、推理档位、解码惩罚。

        models 快照里的路由只存作者的选择(见 ``route_payload``);解析时这些字段缺哪个就从这里补,
        代码里修好的默认值(输出预算、温度……)于是随发布直接到达已配置过的安装(批准#5a)。
        """
        defaults: dict[str, Any] = {
            "temperature": self.temperature,
            "max_output_tokens": self.max_output_tokens,
            "response_format": self.response_format,
            "reasoning_level": self.reasoning_level,
        }
        # §7 anti-mean sampling:只有声明了惩罚的节点(风格化)才带这几个键,其余节点用服务默认
        for key in ("frequency_penalty", "presence_penalty", "top_p"):
            value = getattr(self, key)
            if value is not None:
                defaults[key] = value
        return defaults

    def default_route(self) -> dict[str, Any]:
        """没有 models 快照、API 配置也来自环境变量时这个节点的路由(测试 / E2E / 本机假服务走这条)。"""
        return {
            "provider": self.provider,
            "model": self.model,
            "api_mode": self.api_mode,
            **self.route_defaults(),
        }

    def route_payload(
        self,
        *,
        provider_id: str,
        provider: str | None = None,
        model: str | None = None,
        account_id: str | None = None,
        api_mode: str | None = None,
        credential_mode: str | None = None,
    ) -> dict[str, Any]:
        """设置页(一键补齐 / 分工)写进 models 快照的一条路由:只有作者的选择——服务、模型、端点模式、
        凭据方式、账号。其余参数不抄进快照,解析时取 ``route_defaults``。"""
        payload: dict[str, Any] = {
            "provider": provider or self.provider,
            "provider_id": provider_id,
            "model": model or self.model,
            "api_mode": api_mode or self.api_mode,
        }
        if account_id:
            payload["account_id"] = account_id
        if credential_mode:
            payload["credential_mode"] = credential_mode
        return payload


_NODE_SPECS: tuple[LLMNodeSpec, ...] = (
    LLMNodeSpec(
        "extraction",
        "Generic extraction",
        "reference",
        # 这个节点实际派发的模板（成稿正文抽持久状态变化：prose_event_extractor 经 run_task 的
        # narrative_event_extract → extraction 路由别名）；以前填的是并不存在的 "extraction"。
        template_name="narrative_event_extract",
        model="gpt-5-mini",
        temperature=0.1,
        max_output_tokens=1200,
    ),
    # FE-ALIGN P6: 资料库半自动派生（归档正文 → 候选实体/时间线事件 → 待办确认）
    # FE-ALIGN G5: 构思视图的步骤候选生成（3 条不同方向，原型契约形状）
    LLMNodeSpec(
        "snowflake_step_candidates",
        "Snowflake step candidates",
        "project",
        template_name="snowflake_step_candidates",
        temperature=0.7,
        # 三条方向各可到 400 字，外加标签 / 要点与 reasoning 模型的思考 token：1800 连可见输出的最坏情况都
        # 装不下，每次都要靠客户端的截断阶梯翻倍重试一遍（白花一次完整调用）。
        max_output_tokens=4096,
    ),
    # 2026-09-23 风格参考 v3:按字数分批(≤6,000 字 / ≤100 段)的一批结果 ≤~4k token;分类不需要思考
    # token,推理默认关;8192 给不肯关思考的中转留余量。
    LLMNodeSpec(
        "style_ref_paragraph_classify_anchor",
        "Style Reference 段落分类(锚定集,quality_strong)",
        "style_reference",
        template_name="style_ref_paragraph_classify_anchor",
        model="gpt-5",
        temperature=0.1,
        max_output_tokens=8192,
        reasoning_level="off",
    ),
    LLMNodeSpec(
        "style_ref_paragraph_classify_bulk",
        "Style Reference 段落分类(余下,local_fast)",
        "style_reference",
        template_name="style_ref_paragraph_classify_bulk",
        model="gpt-5-mini",
        # 分析节点必须可复算；创作随机性只留给生成节点。
        temperature=0.0,
        max_output_tokens=8192,
        reasoning_level="off",
    ),
    # 2026-09-23 风格参考 v3「学习文风」作业:四层各一次调用读同一组约 4 万字的窗口、文风卡合成、受保护专名、窗口标签。
    # 四层各一次调用读同一组约 4 万字的窗口,每层 4 维 × (≤5 观察 + ≤2 避免) × 2–4 处引文,输出可达 ~1.1 万字;
    # 文风卡合成 16 维 + 气质 + 规划手法 ~1.3 万字;都留 16384(含思考 token)。受保护专名 ≤300 个词;
    # 窗口标签每批 8 窗,关推理。
    LLMNodeSpec(
        "style_ref_extract_language",
        "Style Reference 学习文风 · 语言层(同一组窗口,4 维)",
        "style_reference",
        template_name="style_ref_extract_language",
        model="gpt-5",
        temperature=0.0,
        max_output_tokens=16384,
    ),
    LLMNodeSpec(
        "style_ref_extract_narrative",
        "Style Reference 学习文风 · 叙事层(同一组窗口,4 维)",
        "style_reference",
        template_name="style_ref_extract_narrative",
        model="gpt-5",
        temperature=0.0,
        max_output_tokens=16384,
    ),
    LLMNodeSpec(
        "style_ref_extract_scene",
        "Style Reference 学习文风 · 场景层(同一组窗口,4 维)",
        "style_reference",
        template_name="style_ref_extract_scene",
        model="gpt-5",
        temperature=0.0,
        max_output_tokens=16384,
    ),
    LLMNodeSpec(
        "style_ref_extract_theme",
        "Style Reference 学习文风 · 主题层(同一组窗口,4 维)",
        "style_reference",
        template_name="style_ref_extract_theme",
        model="gpt-5",
        temperature=0.0,
        max_output_tokens=16384,
    ),
    LLMNodeSpec(
        "style_ref_synthesize_profile",
        "Style Reference 学习文风 · 写文风卡(16 维 → 文风卡)",
        "style_reference",
        template_name="style_ref_synthesize_profile",
        model="gpt-5",
        temperature=0.0,
        max_output_tokens=16384,
    ),
    LLMNodeSpec(
        "style_ref_protected_terms",
        "Style Reference 学习文风 · 识别本书专名(受保护专名)",
        "style_reference",
        template_name="style_ref_protected_terms",
        model="gpt-5",
        temperature=0.0,
        max_output_tokens=8192,
    ),
    LLMNodeSpec(
        "style_ref_tag_windows",
        "Style Reference 学习文风 · 给全书片段打标签(场面 / 情绪 / 手法)",
        "style_reference",
        template_name="style_ref_tag_windows",
        model="gpt-5-mini",
        temperature=0.0,
        max_output_tokens=4096,
        reasoning_level="off",
    ),
    LLMNodeSpec(
        "snowflake_step_generate",
        "Snowflake step generate",
        "snowflake",
        template_name="snowflake_step_generate",
        temperature=0.25,
        # 整步生成一次要吐出全表（角色表多人多字段、场景列表 / 场景规划几十场），reasoning 模型的
        # 思考 token 同样吃这个预算：3200 装不下。8192 是客户端截断阶梯的上限（MAX_OUTPUT_TOKENS_CEILING）。
        max_output_tokens=8192,
    ),
    LLMNodeSpec(
        "snowflake_workspace_assistant",
        "Snowflake workspace assistant",
        "snowflake",
        template_name="snowflake_workspace_assistant",
        temperature=0.35,
        max_output_tokens=3200,
    ),
    LLMNodeSpec(
        "snowflake_scene_triage",
        "Snowflake scene triage",
        "snowflake",
        template_name="snowflake_scene_triage_suggest",
        temperature=0.15,
        max_output_tokens=2200,
    ),
    LLMNodeSpec(
        "snowflake_chapter_plan",
        "Snowflake chapter plan suggestion",
        "snowflake",
        template_name="snowflake_chapter_plan_suggest",
        temperature=0.2,
        max_output_tokens=2600,
    ),
    LLMNodeSpec(
        "scene_blueprint",
        "Scene blueprint",
        "scene_generation",
        template_name="scene_blueprint",
        temperature=0.25,
        max_output_tokens=1800,
    ),
    LLMNodeSpec(
        "character_pressure_blueprint",
        "Character pressure blueprint",
        "scene_generation",
        template_name="character_pressure_blueprint",
        temperature=0.25,
        max_output_tokens=1800,
    ),
    LLMNodeSpec(
        "chapter_story_architecture",
        "Chapter story architecture",
        "scene_generation",
        template_name="chapter_story_architecture",
        temperature=0.25,
        max_output_tokens=2200,
    ),
    # 章节编排 LLM 规划三通道（docs/chapter-arrangement-llm-design-2026-07-16.md §4）
    LLMNodeSpec(
        "chapter_scene_plan_candidates",
        "Chapter scene plan candidates",
        "project",
        template_name="chapter_scene_plan_candidates",
        temperature=0.6,
        max_output_tokens=3200,
    ),
    LLMNodeSpec(
        "chapter_scene_plan_fill",
        "Chapter scene plan fill",
        "project",
        template_name="chapter_scene_plan_fill",
        temperature=0.2,
        max_output_tokens=3200,
    ),
    LLMNodeSpec(
        "chapter_plan_review",
        "Chapter plan review",
        "project",
        template_name="chapter_plan_review",
        temperature=0.15,
        max_output_tokens=2600,
    ),
    LLMNodeSpec(
        "neutral_draft",
        "Neutral draft",
        "scene_generation",
        template_name="neutral_draft",
        temperature=0.6,
        max_output_tokens=6000,
    ),
    LLMNodeSpec(
        "style_draft",
        "Style draft",
        "scene_generation",
        template_name="stylize",
        temperature=0.8,
        max_output_tokens=6000,
        frequency_penalty=0.0,
        presence_penalty=0.0,
    ),
    LLMNodeSpec(
        "style_patch",
        "Style patch",
        "scene_generation",
        template_name="stylize",
        temperature=0.8,
        max_output_tokens=6000,
        frequency_penalty=0.0,
        presence_penalty=0.0,
    ),
    LLMNodeSpec(
        "scene_literary_rewrite",
        "Scene literary rewrite",
        "rewrite",
        template_name="scene_literary_rewrite",
        temperature=0.55,
        max_output_tokens=6000,
    ),
    LLMNodeSpec(
        "hard_qc",
        "Hard QC",
        "quality",
        template_name="hard_qc",
        temperature=0.2,
        max_output_tokens=2200,
    ),
    LLMNodeSpec(
        "soft_qc",
        "Soft QC",
        "quality",
        template_name="soft_qc",
        temperature=0.2,
        # 2026-09-22:2600 装不下 Claude 一级模型带证据串的评审 JSON(真实运行三次顶到上限作废)
        max_output_tokens=5000,
    ),
    LLMNodeSpec(
        "near_final_acceptance_review",
        "Near-final acceptance review",
        "quality",
        template_name="near_final_acceptance_review",
        temperature=0.15,
        max_output_tokens=5000,
    ),
    LLMNodeSpec(
        "chapter_near_final_review",
        "Chapter near-final review",
        "quality",
        template_name="chapter_near_final_review",
        temperature=0.15,
        max_output_tokens=3200,
    ),
    LLMNodeSpec(
        "writer_passage_patch",
        "Writer passage patch",
        "rewrite",
        template_name="writer_passage_patch",
        temperature=0.45,
        # 2026-09-30 重评 R12:改写候选不再逐项回抄原文,一次要装下两版近 2000 字的改写(含思考 token),
        # 2600 装不下。
        max_output_tokens=8192,
    ),
    LLMNodeSpec(
        "writer_deep_review",
        "Writer deep review",
        "deep_review",
        template_name="writer_deep_review",
        temperature=0.15,
        max_output_tokens=3600,
    ),
    LLMNodeSpec(
        "author_proposal_generate",
        "Author proposal generate",
        "writer_review",
        template_name="author_proposal_generate",
        temperature=0.45,
        max_output_tokens=2600,
    ),
)


def llm_node_specs() -> tuple[LLMNodeSpec, ...]:
    return _NODE_SPECS


def llm_node_catalog() -> dict[str, dict[str, Any]]:
    return {
        spec.node_id: spec.catalog_entry(index)
        for index, spec in enumerate(_NODE_SPECS)
    }


def active_llm_node_ids() -> list[str]:
    return [spec.node_id for spec in _NODE_SPECS]


def get_llm_node_spec(node_id: str) -> LLMNodeSpec | None:
    for spec in _NODE_SPECS:
        if spec.node_id == node_id:
            return spec
    return None


_NEUTRAL_ROUTE_FIELDS = ("temperature", "max_output_tokens", "response_format", "reasoning_level")


def neutral_route_defaults() -> dict[str, Any]:
    """没有 spec 的路由(节点后来从注册表删掉了)缺参数时的中性占位:``LLMNodeSpec`` 自己的字段默认值。

    快照只存作者的选择(``route_payload``),节点一退役,它那条路由就没有 spec 可补参数了。这样的路由不会被派发
    (运行时只按注册表里的节点查路由),补上占位只为让它照旧解析得了、不拖垮整张路由表:设置页列为
    stale_routes,下一次一键补齐 / 分工剪掉——和以前整份抄进快照的退役路由一样。
    """
    defaults = {spec_field.name: spec_field.default for spec_field in fields(LLMNodeSpec)}
    return {key: defaults[key] for key in _NEUTRAL_ROUTE_FIELDS}


def default_task_config_payload(
    node_id: str,
    *,
    provider_id: str,
    provider: str | None = None,
    model: str | None = None,
    account_id: str | None = None,
    api_mode: str | None = None,
    credential_mode: str | None = None,
) -> dict[str, Any]:
    spec = get_llm_node_spec(node_id)
    if spec is None:
        raise KeyError(node_id)
    return spec.route_payload(
        provider_id=provider_id,
        provider=provider,
        model=model,
        account_id=account_id,
        api_mode=api_mode,
        credential_mode=credential_mode,
    )


# ---- 角色分工槽位(writer-facing routing) ---------------------------------
# 写作者视角的三个分工槽位,按节点分组批量路由;覆盖全部节点。前端「设置 → AI 模型 → 分工」用。


@dataclass(frozen=True, slots=True)
class RoleSlotSpec:
    slot_id: str
    label_zh: str
    description_zh: str
    groups: tuple[str, ...]

    def catalog_entry(self) -> dict[str, Any]:
        return {
            "slot_id": self.slot_id,
            "label_zh": self.label_zh,
            "description_zh": self.description_zh,
            "groups": list(self.groups),
            "node_ids": role_slot_node_ids(self.slot_id),
        }


ROLE_SLOTS: tuple[RoleSlotSpec, ...] = (
    RoleSlotSpec(
        "drafting",
        "写作主力",
        "续写、初稿、风格化、改写与构思生成",
        ("scene_generation", "rewrite", "snowflake"),
    ),
    RoleSlotSpec(
        "review",
        "审稿质检",
        "QC、验收评审、文学评估与写作者诊断",
        ("quality", "evaluation", "writer_review", "deep_review"),
    ),
    RoleSlotSpec(
        "extraction",
        "提炼整理",
        "资料抽取、风格画像与项目级提炼",
        ("reference", "style_reference", "project"),
    ),
)


def get_role_slot_spec(slot_id: str) -> RoleSlotSpec | None:
    for slot in ROLE_SLOTS:
        if slot.slot_id == slot_id:
            return slot
    return None


def role_slot_node_ids(slot_id: str) -> list[str]:
    slot = get_role_slot_spec(slot_id)
    if slot is None:
        raise KeyError(slot_id)
    groups = set(slot.groups)
    return [spec.node_id for spec in _NODE_SPECS if spec.group in groups]


def role_slot_catalog() -> list[dict[str, Any]]:
    return [slot.catalog_entry() for slot in ROLE_SLOTS]
