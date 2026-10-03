> 历史设计记录：2026-07-16 的设计与实现记录，正文按当时写、保持原样；路径不动（代码注释按章节号引用它）。之后变了的：
> - **伏笔 / 张力 / 作者偏好三路输入已退役**：伏笔账本与张力曲线在 2026-09 减法里删除；章级的张力 / 线索 / 视角 · 时间 · 地点 · 入口 · 出口字段
>   2026-09-30 删除（没有任何地方能填），邻章交接、章结构邻域、近几章的视角分布改为从**各场**的真实数据现算；写作偏好学习 2026-09-30 删除。
> - 体检码 `FORESHADOW_OVERDUE` 删除；`TENSION_FLAT` 改为看本章各场的冲突 / 挫折与蓝图的升级路线（「压力没有升级」）。
> - **fail-closed**：没有可用模型时蓝图生成与 candidates / fill / review 一律 409 `CHAPTER_PLAN_LLM_NOT_CONFIGURED` + `author_action`，
>   不再有离线 fallback；新增 `GET …/plan/gaps`（按空槽算的待补清单，不需要模型）。写接口一律要 `X-Idempotency-Key`（偏差 ② 不再成立）。
> - 前端 store 是 `ws-author-ai-store.js`（`WsAuthorAi`），视图 `ws-author-ai.jsx`；文中的 `ws-chapter-plan.jsx` / `ws-author-plan.jsx` 已不存在。
>
> 现行实现以 `services/chapter_plan_llm.py`、`services/chapter_planning_context.py`、`api/routes/chapter_plan.py` 的模块说明为准；
> 雪花构思一侧的分章见[一条书脊：构思 → 目录 → 三张台子](book-spine-catalog-contract.md)。

# 章节编排 LLM 接入设计 —— 场景卡规划（2026-07-16）

> 状态：**已实现（P1–P4 全部落地，2026-07-16）**。对应模块：React 工作台「章节编排」
> （`frontend-react/src/ws-author.jsx`，目录真相源 `/api/v2/projects/{pid}/catalog`）。
> 实现落点：后端 `services/chapter_planning_context.py`（十槽位底座）+
> `services/chapter_plan_llm.py`（蓝图 CRUD / candidates / fill / review / apply + sanitize）+
> `api/routes/chapter_plan.py`（7 端点，蓝图 generate 与 plan/apply 幂等）；
> 前端 `ws-chapter-plan.jsx`（store）+ `ws-author-plan.jsx`（蓝图卡 / AI 编排面板 / AI 体检块）。
> 测试：`backend/tests/test_chapter_plan.py`（17 例）+ `frontend-react/src/ws-chapter-plan.test.jsx`（9 例）。
> 与设计的已知偏差：①路由/模板 nullable 字段用空串而非 JSON 联合类型（prompt 加载器限制）；
> ②candidates/fill/review 不套幂等键（与雪花 generate 先例一致，重生成是合法诉求）；
> ③orchestrator 无需改动——`ensure_scene_planning` 本就复用最新 active 蓝图，作者版
> （llm_call_id=None）与离线 fallback 同形，checkpoint 校验天然容忍。

## 0. 一句话

不给章节编排新造一个"孤立的生成按钮"，而是把系统已有的**雪花 canon、叙事事件账本、
伏笔生命周期、张力曲线、章节蓝图（chapter_story_architecture）、作者偏好**汇编成一个
**确定性的章节规划上下文底座**，在它之上挂三个结构化 LLM 节点（候选编排 / 保真补全 /
编排体检），产物全部以**咨询式补丁**回写目录；同时把已经存在但作者不可见的
`chapter_story_architecture` 提升为章节编排里的一等公民，让"作者在编排台确认的蓝图"
直接成为本章每一场 AI 起草的注入上下文——规划与起草闭环，这是本设计对"上下文铺垫"
的核心兑现。

## 1. 现状盘点

### 1.1 模块现状（ws-author.jsx）
- 两级视图：全书编排（张力弧线 / 线索织布机 / 节奏镜头 / 全书体检 ArrDoctor / 按卷看板）
  与章节详情（承接-交棒条 / 戏剧卡 6 字段 + 护栏 / 场景看板 / 章节体检 / 从雪花同步）。
- 场景卡字段（前端 ↔ 后端）：`title`、`kind`（主动/反应）、三拍 `goal/obstacle/turn`
  ↔ `writer_brief_json` 的 GCS（goal/conflict/setback）或 RDD（reaction/dilemma/decision）、
  `povName` ↔ `pov_character_id`（按名字自动建档）。`SceneCard` 上还有目录暂未暴露的
  `exit_change / hook / beats_json / must_include_text / forbidden_text / constraint_intensity`。
- 现有 LLM 能力：仅「运行本章」（ChapterRunJob 持久任务）。编排/规划本身零 LLM。
- 全书体检 ArrDoctor 是纯规则（悬空线索 / 字数超额 / 张力回落 / POV 分布）。

### 1.2 可复用的系统资产（本设计的"优势"来源）
| 资产 | 位置 | 对章节规划的价值 |
|---|---|---|
| 雪花 canon（已确认步骤） | `snowflake_workspace_llm._approved_step_context` | 一句话/一段话/长大纲/人物圣经/场景清单，是规划的世界观与主线约束 |
| 章节蓝图 | `GenerationPlanningArtifact(artifact_type=chapter_story_architecture)`，prompts.yaml 已有模板（promise/escalation_path/reveal_plan/payoff_target/character_shift/ending_question） | **已存在但只在场景 run 时懒生成、作者不可见**。是编排台与起草管线之间现成的桥 |
| 叙事事件账本 | `narrative_event_log`（append-only，回放到某场重建实体状态） | 提供"到本章开头为止世界处于什么状态"的事实基准 |
| 伏笔生命周期 | `foreshadow_lifecycle` / `ForeshadowTracker` | 已种未收的伏笔债，规划新场景时必须知道 |
| 张力曲线 | `tension_curve` + 目录 `narrative_json.tension` | 本章在全书张力走势中的邻域与目标 |
| 人物连续性/弧线/关系 | `character_continuity` / `character_arc` / `relationship_matrix` | POV 选择、人物变化字段的依据 |
| 作者偏好/指令 | `author_preferences` / `author_instructions`（prompt-safe 摘要） | 规划口味约束 |
| 复制护栏 | `source_safety` / `reference_safety` + 戏剧卡 `forbidden` / `must_not` | 输出红线 |
| LLM 基建 | `LLMNodeRunner` + `llm_node_registry` + `models.yaml task_routing` + `prompts.yaml structured_schema` + `llm_accounting`（预约→结算，配额先于 dispatch）+ `execute_with_idempotency` + `author_action` | 全部直接复用，不新造调用通道 |
| 交互先例 | 雪花双通道（`fe-candidates` 3 方向候选 → 采纳 → `generate` canon 保真合并；咨询式补丁：空值不清空、按 id 对位、不删成员）、scene-triage suggest、章任务轮询 | 前端契约照此延续，作者学习成本为零 |

## 2. 总体方案

```
                       ┌─────────────────────────────────────────┐
                       │  ChapterPlanningContextBuilder（新）      │
                       │  确定性槽位汇编 + 版本引用 + 降级记录        │
                       └───────────────┬─────────────────────────┘
                                       │ ordered_injections / inline_digests
        ┌──────────────────┬───────────┴────────────┬──────────────────┐
        ▼                  ▼                        ▼                  ▼
 chapter_architecture  chapter_scene_plan      chapter_scene_plan   chapter_plan
 （显式生成/作者改）     _candidates（3方向）      _fill（保真补全）      _review（体检）
        │                  │                        │                  │
        │ 持久为一等蓝图     │ 咨询候选（无状态）        │ 咨询式补丁          │ findings + 建议补丁
        ▼                  └────────────┬───────────┘                  │
 scene-run bundle 注入                   ▼                             ▼
 （bundle_builder 已消费） ── 服务端 sanitize → plan/apply（幂等原子回写目录）→ 章节体检 UI 合流
```

三条设计铁律：
1. **单一真相源不变**：LLM 产物永远是"建议"，落库只走 `CatalogService`（目录）与
   `GenerationPlanningArtifact`（蓝图），不新增平行的规划真相表。
2. **咨询式补丁纪律**（沿雪花 `candidate_patch` 语义并在服务端强制）：只填空、按
   `scene_id` 对位、新卡只追加到队尾、不删除、不覆盖作者非空文本；改名/改类型这类
   "覆盖型"意见降级为 finding 提示，不进补丁。
3. **上下文即契约**：每次调用的槽位、来源版本引用（source_version_refs）、降级槽
   （degraded_slots）随响应返回并进审计，可复现、可解释（沿用 bundle 冻结纪律）。

## 3. 上下文底座：ChapterPlanningContextBuilder

新服务 `services/chapter_planning_context.py`（与 `bundle_builder.py` 同族但独立——
bundle 面向"起草一场"，这里面向"规划一章"；共享底层查询助手，不复制逻辑就复用）。

槽位与优先级（预算不够时从尾部降级，降级项记入 `degraded_slots`）：

| 序 | 槽位 | 来源 | 说明 |
|---|---|---|---|
| 1 | `chapter_card` | `ChapterGoal.narrative_json`（drama 6 字段、entry/exit、promise、threads、tension、pov、words_target） | 作者意图，最高权重 |
| 2 | `scene_cards_current` | 本章 `SceneCard` 全量（含三拍/POV/state/exit_change/hook） | 已有卡是"不可覆盖"的既定事实 |
| 3 | `neighbor_handoff` | 上一章 exit + 末场 `exit_change/hook`；下一章 entry | 承接/交棒对齐 |
| 4 | `chapter_architecture` | 最新 active `chapter_story_architecture` | 若无則此槽降级（并提示可先生成蓝图） |
| 5 | `snowflake_canon_digest` | 已确认雪花步骤摘要：一句话/一段话 + 长大纲中对应本章位置的切片 + 在场人物的人物圣经摘要 | canon 约束 |
| 6 | `narrative_state_digest` | 事件账本回放至上一章末场 | 世界事实基准；无事件时降级 |
| 7 | `foreshadow_debts` | 开放伏笔（含逾期标记、计划收束位置） | 规划新场的必答题 |
| 8 | `tension_neighborhood` | 前后 N 章 tension 序列 + 本章目标值 + 所在卷/幕 | 升级压力的量化锚 |
| 9 | `character_positions` | 在场人物弧线位置 / 关系矩阵摘要 / POV 近期分布 | POV 与人物变化依据 |
| 10 | `author_constraints` | 作者偏好摘要 + 戏剧卡 `forbidden` + `must_not` + source-safety 保护词 | 红线，注入为硬约束块，永不降级（体量小，排尾只为汇编顺序） |

- 预算：走 `context_budget.py` 既有机制，模板 `input_token_budget` 约 3200；
  切片而非全文（长大纲只取本章邻域，人物圣经只取在场人物）。
- 冷启动：新作品无雪花/无事件/无伏笔时，槽位 5/6/7 全部降级但**功能仍可用**
  （只靠 1/2/3 也能补全三拍）；降级列表在 UI 显示为提示 chips
  （如「未接入伏笔账本：本作尚无叙事事件」），把"上下文越厚建议越准"变成作者可感知的正反馈。
- 演示门控：槽位全部按 `project_id` 取数，「潮汐档案」的演示剧情天然不会进入真实作品的
  prompt；对 `tide` 本身允许调用（演示体验），但输出照常打演示 provenance。

## 4. LLM 节点设计（3 新增 + 1 提升）

统一：注册进 `llm_node_registry._NODE_SPECS` + `config/models.yaml task_routing` +
`config/prompts.yaml`（`structured_schema` 强约束 JSON），执行统一走 `LLMNodeRunner.run_task`
（自动获得 accounting/审计/重试预算/离线确定性模式）。系统配置 UI 因此自动可为每个节点
路由 provider，无需额外工作。

### 4.1 `chapter_scene_plan_candidates` — 编排候选（发散通道）
- 路由建议：`quality_strong`，temperature 0.6（发散），`max_output_tokens` 3200，json_object。
- 输入：完整上下文底座 + 可选 `direction_hint`（作者一句话倾向）+ `focus`（整章 / 指定空洞）。
- 输出 schema（草案）：
```yaml
candidates:            # 恰好 3 个，方向必须互斥（模板里明示：结构策略不同，而非措辞不同）
  - label: string      # ≤12字方向名，如「压缩为双场对撞」
    rationale: string  # 为什么这个方向成立，必须引用上下文事实（伏笔/张力/交接）
    risk: string       # 这个方向的代价
    scene_plan:        # 完整的本章场景序列提案
      - ref_scene_id: string|null   # 对位现有卡则填 sid；新卡为 null
        title: string
        kind: proactive|reactive
        brief: {goal,conflict,setback} | {reaction,dilemma,decision}
        pov_character_name: string
        exit_change: string          # 本场结束世界/关系改变了什么
        hook: string
        tension_note: string         # 相对上一场的压力升级方式（换类型，不是加形容词）
        foreshadow_ops: [ {op: plant|payoff, tracker_ref: string|null, note: string} ]
```
- 语义：**无状态、纯咨询**（同雪花 fe-candidates 不落库）；采纳动作在前端把选中候选交给
  4.2 的 fill 通道做保真合并，不直接整体覆盖。

### 4.2 `chapter_scene_plan_fill` — 保真补全（收敛通道）
- 路由建议：`quality_strong`，temperature 0.2（收敛保真），json_object。
- 两种模式：
  - `fill`：默认。扫描本章现有卡的空槽（空三拍/空 POV/空 exit_change/空 hook/「（待规划）」），
    只产出填空补丁；
  - `adopt`：携带 4.1 的选中候选。已对位（ref_scene_id）的卡仍只填空 + 把差异降级为
    findings；候选里的新卡以"追加到队尾"的 `append_scenes` 给出。
- 输出 schema（草案）：
```yaml
patch:
  scenes:              # 按 scene_id 对位；值只允许出现在目标字段当前为空时
    - scene_id: string
      set: { brief.goal?: string, brief.conflict?: ..., pov_character_name?: string,
             exit_change?: string, hook?: string }
  append_scenes: [ {title, kind, brief, pov_character_name, exit_change, hook} ]
notes:                 # 无法进补丁的覆盖型意见（如"第2场建议改反应场"），供作者手动决定
  - {scene_id: string|null, field: string, suggestion: string, reason: string}
gaps: [string]         # 补完后仍缺什么（如"第4场 POV 无法从上下文推断"）
```
- **服务端 sanitize 兜底**（`_sanitize_plan_patch`，不信任模型自律）：剔除对非空字段的写入、
  剔除 delete/reorder 类意图、`append_scenes` 数量上限（如 ≤ 现有卡数 + 4）、POV 名称过
  source-safety 保护词与人物表校验（沿 `CatalogService` 的按名建档路径）。

### 4.3 `chapter_plan_review` — 编排体检（诊断通道）
- 路由建议：`quality_strong`，temperature 0.15。
- 输入：上下文底座（重点消费 3/6/7/8 槽）。输出：
```yaml
findings:
  - code: string       # 枚举：PROMISE_UNGROUNDED / SCENE_FUNCTION_DUPLICATE /
                       # REACTIVE_MISSING / TENSION_FLAT / FORESHADOW_OVERDUE /
                       # POV_FATIGUE / HANDOFF_MISMATCH / EXIT_NO_CHANGE ...
    severity: warn|info
    scene_id: string|null
    field: string|null
    evidence: string   # 必须引用上下文事实（哪条伏笔、哪个交接文本、哪段张力序列）
    suggestion_patch:  # 可选，形如 4.2 的单条填空补丁（同样过 sanitize）
```
- 前端合流进既有「章节体检」块与 ArrDoctor：规则项照旧即时计算，LLM findings 按需触发、
  带"AI"标识与跳转 chip；有 `suggestion_patch` 的项提供单条「应用建议」。规则与 LLM 不互相
  替代——规则免费兜底，LLM 提供规则算不出的结构性判断。

### 4.4 章节蓝图提升为一等公民（复用既有 `chapter_story_architecture` 节点）
- 现状：只在场景 run 的 planning 阶段懒生成，作者不可见不可改。
- 改造：
  - `GET /architecture`：读最新 active 蓝图（含 `created_by` 与来源 llm_call）；
  - `POST /architecture/generate`：显式生成/重生成（走既有节点与模板，上下文换成本底座），
    旧行 `superseded`；
  - `PUT /architecture`：作者直接编辑，落库为 `created_by="author"` 的新 active 行
    （旧行 superseded）——**作者手改优先，场景 run 不再懒生成覆盖**（orchestrator 侧规则：
    存在 active 蓝图即复用，不新生成；这与其现有"最新 active 优先"读取路径一致，改动很小）。
- 这是"铺垫"的闭环：编排台上作者确认的 promise/escalation/payoff，经 bundle_builder
  既有的 `chapter_story_architecture` 槽位注入本章**每一场**的起草 prompt；反过来
  4.1/4.2/4.3 又消费它。无需动 bundle 代码，注入路径已经存在。
- 无需迁移：`artifact_type` 仍是 `chapter_story_architecture`，CheckConstraint 不变。
  （若未来要持久化"编排提案"为新 artifact_type，才需要迁移 + 过 schema-drift guard。）

## 5. API 契约（v2，挂 catalog 路由族）

统一：响应走 `{ok,data,error,request_id}` envelope；POST 创建/生成类过
`execute_with_idempotency`（`X-Idempotency-Key` 必带，重放同响应）；`X-Operator-Ref` 进审计；
LLM 未配置/未启用不硬阻断，返回 `author_action`（导航到系统设置）+ 规则降级结果
（沿 `_fallback_triage_items` 先例）。

```
POST /api/v2/projects/{pid}/catalog/chapters/{chid}/plan/candidates
     body: { direction_hint?: string, focus?: "chapter"|"gaps" }
     → { candidates: [...], degraded_slots: [...], context_fingerprint, llm_call_id }

POST /api/v2/projects/{pid}/catalog/chapters/{chid}/plan/fill
     body: { mode: "fill"|"adopt", candidate?: {...}, scope?: {scene_ids?: [..]} }
     → { patch, notes, gaps, degraded_slots, context_fingerprint, llm_call_id }

POST /api/v2/projects/{pid}/catalog/chapters/{chid}/plan/apply
     body: { patch: {...}, source_llm_call_id?: string }
     → { applied: {scenes: n, appended: m, skipped: [...] }, catalog: {…} }
     # 服务端再 sanitize 一次后，在单事务内经 CatalogService 原子回写；
     # skipped 返回被拒条目及原因（如"字段已非空"——并发下作者可能刚手填过）。
     # 锁章（state=approved）在此统一 409（DomainError CHAPTER_LOCKED）。

POST /api/v2/projects/{pid}/catalog/chapters/{chid}/plan/review
     → { findings: [...], degraded_slots, llm_call_id }

GET  /api/v2/projects/{pid}/catalog/chapters/{chid}/architecture
POST /api/v2/projects/{pid}/catalog/chapters/{chid}/architecture/generate
PUT  /api/v2/projects/{pid}/catalog/chapters/{chid}/architecture
```

- 同步 vs 任务：四个 LLM 端点都是单次结构化调用（输出 ≤3200 tokens），走同步——与
  雪花 generate/fe-candidates 同级别；幂等租约 600s 已覆盖。整章多场逐一深规划这类
  长活不在本期（若将来要，套 ChapterRunJob 的持久任务模式，不新造机制）。
- `apply` 独立成端点而不是让前端逐字段 PATCH：一次补丁十几个字段，逐条 PATCH 无原子性、
  无统一锁章校验、审计碎片化；`apply` 单事务 + 单审计事件 + 返回整份目录增量，前端
  optimistic 失败可整体回滚。

## 6. 治理与护栏

- **计量**：全部经 `LLMNodeRunner` → `llm_accounting`（预约→dispatch→结算），配额在
  dispatch 前失败；审计行有界指纹（0073 契约）。规划类调用便宜（单次 2-3k out），
  不需要新配额档。
- **锁章**：`state=approved` 的章，candidates/fill/apply/architecture 写路径全部 409；
  review 只读放行（体检已锁章节也有意义）。
- **红线**：`author_constraints` 槽为硬约束块；输出侧 sanitize 对 title/brief 文本过
  source-safety 保护词过滤（防参考书专名渗入规划）；戏剧卡 `forbidden` 原文注入。
- **离线/未配置**：`NOVEL_SYSTEM_LLM_ENABLED=false` 或节点未路由时返回 author_action +
  规则降级（fill 降级为"列出空槽清单"，review 降级为 ArrDoctor 规则集的服务端版）。
- **演示纪律**：不给 `tide` 之外的作品注入任何演示种子；`tide` 上的产物照常演示 provenance。

## 7. 前端交互（ws-author.jsx 三个挂点）

1. **场景看板头部**：新增「AI 编排」按钮组 → 侧板双通道：
   - 「三个方向」：候选卡（label + rationale + risk + 迷你场景序列预览），点「采纳」→
     调 fill(adopt) → 展示 diff 式补丁（按场分组，逐字段 接受/忽略）→ 「应用」调 plan/apply；
   - 「一键补全」：直接 fill(fill)，同样走 diff 确认后 apply。
   - 补丁应用经 WsCatalog optimistic + 失败回滚/refetch（店内既有契约）；`notes`（覆盖型
     意见）以只读列表展示，点击定位到对应卡。
2. **章节体检块**：新增「AI 体检」按钮，findings 与规则项合流（AI 项带标识、evidence 折叠、
   suggestion_patch 单条应用）；全书体检 ArrDoctor 本期不接 LLM（见 §9 不做的事）。
3. **戏剧卡**：新增「章节蓝图」折叠区——展示/编辑/重生成 architecture 六字段，副文案明示
   "本蓝图会注入本章每一场的 AI 起草上下文"；戏剧卡 6 字段与蓝图字段有天然对应
   （promise↔chapter_promise、arc↔character_shift、ending↔ending_question），
   生成蓝图时把戏剧卡作为最高权重输入，反向"回填戏剧卡空槽"作为 fill 通道的一部分。
- **降级提示**：`degraded_slots` 渲染为提示 chips，指向对应补齐入口（去构思/去写作/去资料库）。
- **Store**：新建 `ws-chapter-plan.jsx`（`WsChapterPlan`，window 全局 + ESM 导出，同族约定），
  持有候选/补丁/体检的请求态与应用回滚；vitest 套件 `ws-chapter-plan.test.jsx`
  （`installApiRouter` mock，覆盖：补丁应用成功、apply 失败整体回滚 + alert、锁章 409 路径、
  author_action 降级路径——保持可证伪）。

## 8. 与「从雪花同步」的并存规则

- 方向固定：雪花 → 目录（resync 回流三拍/POV/章 brief）；AI 编排补丁 → 只写目录，
  **永不反写雪花**。
- 冲突策略：resync 与 AI 补丁都可能写同一空槽。apply 的"字段已非空则 skip"天然防覆盖；
  resync 侧保持既有语义不动。UI 在存在 pending resync（`resyncStatus.pendingCount>0`）时，
  在 AI 编排面板顶部提示"构思侧有 N 场改动待回流，建议先同步再编排"，不硬阻断。
- 材料化前的空白章：AI 编排可独立工作（槽位 5 降级），作为"不走雪花的轻量作者"路径；
  但引导文案仍以雪花为主线。

## 9. 明确不做的事（本期）

- 不做"整本书重排"的全书级 LLM（overview 层的编排调整继续走拖拽 + 规则体检；
  book-scope review 可作为 `chapter_plan_review` 的后续 scope 扩展）。
- 不做规划产物的独立版本树/新表（蓝图复用 GenerationPlanningArtifact，补丁即写即审计）。
- 不做自动应用：任何补丁必须经作者 diff 确认，没有"AI 静默改卡"。
- 不做长任务化：单章规划是同步调用；整卷批量规划等到有真实需求再套 ChapterRunJob。
- 不动 orchestrator 的场景 run 主链路（唯一改动是"存在 active 蓝图即复用"这一条读取规则）。

## 10. 分期与验收

| 期 | 内容 | 验收 |
|---|---|---|
| P1 | ChapterPlanningContextBuilder + 蓝图三端点 + 戏剧卡蓝图区 + orchestrator 复用规则 | 蓝图在编排台可见可改；改后跑一场 scene run，bundle 的 `chapter_story_architecture` 槽指向作者版 row_id |
| P2 | fill 通道 + plan/apply + 场景看板「一键补全」 | 空三拍/POV 补全、非空字段零覆盖（pytest 针对 sanitize 的性质测试）、apply 幂等重放同响应、锁章 409 |
| P3 | candidates 通道 + 采纳流 | 3 候选方向互斥性人工抽检；adopt 后 diff 只含填空 + 追加 |
| P4 | review 通道 + 体检合流 | findings 的 evidence 必须能定位到注入槽位内容（拒绝无据断言）；离线降级返回规则版 |

测试清单（后端）：上下文底座槽位快照测试（含降级路径）、sanitize 性质测试（非空不覆盖/
不删/追加上限）、幂等重放、锁章、LLM 未配置 author_action、accounting 结算行存在性；
（前端）store 套件见 §7；节点注册后 `models.yaml`/`prompts.yaml`/registry 三处一致性由
既有 prompt-handoff 盘点脚本覆盖（60 节点基线 +4）。
