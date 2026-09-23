> 历史文档：描述当时的实现，现行说明见 [docs/style-reference.md](../../style-reference.md)

# 风格参考 · Phase 3 完成记录

> 状态：立项 A/B/C 已于 2026-06-18 全部完成。本文件保留为代码注释所引用的设计依据，不再作为待办清单使用。
>
> 滚动日志见 `docs/style-reference-progress.md`，权威设计见
> `docs/style_reference_module_design_v1.1.md`。本文件把"已收口的主线之外、
> 仍待做"的工作拆成 **3 个相互独立的立项**，每个自带背景 / 范围 / 接入点 /
> 验收 / 依赖 / 工作量，**任何一个都可在全新会话里照此独立开工**，不依赖之前的
> 对话记忆。建议优先级 A → B → C（A/B 偏小、C 是大件）。
>
> 状态复核：2026-07-16 · 当前主线 `main`

## 0. 共同前置（三个立项都建立在此之上）

主线已收口的事实（开工前默认成立，不必重做）：

- **风格参考六个深层页已全部真后端化**（书库 / 概览 / 维度矩阵+审核 / 风格画像+预览 /
  注入应用 / 回测校验），均「有真数据显真、否则回退演示」。
- **前端集成接缝**：`frontend-react/src/ws-styleref.jsx` 顶部的 `SR_DEEP` 深层 store +
  `useSrDeep(book)` hook + `sr:deep-changed` 事件，是所有深层页的取数单点；
  `window.srLoadDeep / srDeepFor / srInjectionPreview / srUnbind / srSynthesize /
  srReviewFinding / srPreviewSamples` 为已暴露的 helper。新增交互优先复用这套。
- **审核效果接缝**：决策卡的 `effect:{type, ...}` 在收件箱批准时由
  `backend/src/novel_system/services/review_effects.py:run_effect` 执行；
  `bind_style_profile` 已转发 scope/scope_ref_id/strategy/intensity/sub_dimensions/include_*。
- **验证车道**：后端 `cd backend && python -m pytest`（Windows 安全，1212 测试基线）；
  前端 `cd frontend-react && npx vite build`；黄金语料端到端见
  `backend/tests/test_style_reference_golden.py`（+ `regen_expected.py` 重生成期望）。
- **改后端 alembic/pytest 用 Anaconda python**（见记忆 backend-windows-python）。

---

## 立项 A：场景 / 角色级 apply 绑定目标选择器

> ✅ **已完成(2026-06-18,见 progress 第十轮)**。`SrApply` 解除 lockNonProject,真模式经 apiGet
> 拉 `/catalog` 场景 + `/library` 角色填目标下拉,选中 id 入 effect.scope_ref_id;后端 `_bind_style_profile`
> 加 scene/character 缺 ref 抛错守卫;端到端验收测试 + 3 视角审查(采纳 4 修复)。**Phase 3 立项 A/B/C 全部完成。**
> 下文为原立项书,保留作实现参照。

### 背景与动机
注入应用页（`SrApply`）当前**只支持项目级真绑定**。scope 选择器里的「场景 / 角色」
在真模式被禁用（`ws-styleref.jsx` 里 `lockNonProject = realMode && id !== "project"`），
原因是缺少"选哪个场景 / 哪个角色"的目标选择器——`scope_ref_id` 无来源，强行绑定会落成
项目 id（脏数据）。后端早已就绪：`bind_style_profile` effect 接受 `scope` + `scope_ref_id`，
注入选取 `resolve_active_binding` 的优先级是 `scene(0) > character(1) > project(2) > global(3)`。
**只差前端把目标 id 选出来填进 effect。**

### 范围
- **做**：apply 页在 scope=场景/角色时，弹出/展开一个目标下拉，列出当前项目的
  场景（章·场）或角色，选中后其 id 作为决策卡 effect 的 `scope_ref_id`。
- **不做**：后端改动（已就绪）；多目标批量绑定；跨项目绑定。

### 接入点（精确）
- 场景数据源：`GET /api/v2/projects/{project_id}/catalog`（`backend/.../routes/catalog.py:32`）
  → `chapters[].scenes[].scene_id`；前端 `ws-catalog.jsx` 已有 catalog store（`catLoad/catFetch`）
  可直接复用，避免重复取数。
- 角色数据源：**第一步先定位**——项目角色来自 snowflake character plans / story_characters；
  前端 `ws-snow.jsx` / `ws-palette.jsx` 已有角色数据，确认其取数 helper 后复用（无直接
  `GET .../characters` 路由时，从 snowflake workspace 详情里取 `character_id` + 名称）。
- 改动文件：仅 `frontend-react/src/ws-styleref.jsx` 的 `SrApply`——
  (1) 解除 `lockNonProject`；(2) scope=scene/character 时渲染目标下拉；
  (3) 把选中 id 填入 apply 决策卡 `effect.scope_ref_id`（effect 构造已在 onClick 内）。
- 当前活动项目 id：`WsWorks.active()`（注意 effect 默认 scope_ref_id 回退到卡片 project_id，
  仅项目级正确；场景/角色级必须显式传）。

### 验收标准
- 选场景级 + 某 scene → 决策卡 effect 带 `scope="scene"` + 该 `scene_id`；批准后
  `GET /profiles/{id}/bindings` 出现 scope=scene 且 scope_ref_id=该 scene_id 的绑定。
- 角色级同理（scope=character + character_id）。
- 该场景生成时注入命中（`resolve_active_binding` scene rank 最高）——可用后端
  现有注入测试模式断言（seed 一个 scene-scope binding，`fragments_for(scene_id=...)` 命中）。
- `npx vite build` 通过；项目级原路径零回归。

### 依赖 / 风险 / 工作量
- 依赖：角色取数 helper 的定位（唯一未确认点）。
- 风险：低（后端零改动，纯前端取数 + 填字段）。
- 工作量：**中偏小**（1 个前端视图改动 + 复用既有 store；后端 0）。

---

## 立项 B：finding 用户反馈聚合（👍 / 👎 持续校准回路）

> ✅ **已完成(2026-06-18,见 progress 第九轮)**。迁移 0058 新表 `style_reference_finding_feedback`
> (一人一票 uq)+ findings.base_confidence 列;`finding_feedback.apply_feedback` 聚合 net 按
> `feedback.yaml`(promote_net=2/demote_net=-2)±1 档调 confidence,base 保留可逆;路由
> `POST /findings/{id}/user-feedback`(幂等);前端 `srFindingFeedback` + FindingCard 接线;
> 16 用例 + 5 视角审查(采纳 6 修复)。下文为原立项书,保留作实现参照。

### 背景与动机
设计 §5 把 `POST /findings/{finding_id}/user-feedback` 列为 Phase 3 的 🆕 项，**当前后端未实现**；
维度矩阵 finding 卡（`FindingCard`）的 👍👎 投票按钮目前仅本地 state（`vote`），不落盘、不聚合。
目标是让用户对每条 finding 打分 → 持久化 → 聚合后更新该 finding 的 `confidence`，
形成设计承诺的"持续校准回路"（卡片脚注已写"反馈聚合后更新 confidence"）。

### 范围
- **做**：后端反馈端点 + 持久化 + confidence 聚合规则；前端投票按钮接端点。
- **不做**：跨用户加权 / ML 重排；反馈驱动的自动重抽。

### 接入点（精确）
- 后端新端点：`POST /api/v2/style-reference/findings/{finding_id}/user-feedback`
  body `{vote: "up"|"down"}`，走幂等（同 operator 同 finding 一次有效计票，
  参考 `_with_idem` + 既有 review 端点）。落在
  `backend/src/novel_system/api/routes/style_reference.py`。
- 持久化二选一（**开工先定**）：(a) 复用 `style_reference_metric_events`（event_kind
  如 `finding_feedback`，无新表，聚合时 GROUP BY）；(b) 新增 `style_reference_finding_feedback`
  表（finding_id, operator_ref, vote, created_at；uq(finding_id, operator_ref) 保一人一票）。
  推荐 (a) 起步（无 migration），量大再迁 (b)。
- confidence 聚合规则（**需在立项内定稿**）：例如净赞（up−down）≥ +N 升一档
  （low→medium→high）、净踩 ≤ −N 降一档；阈值外置 `config/style_reference/`。
  聚合写回 `style_reference_findings.confidence`（注意：synthesize 已落的 profile
  sub_dimensions 置信度是否回溯刷新——本立项可只改 finding，profile 重合成时自然带新值）。
- 前端：`ws-styleref.jsx` 的 `FindingCard` 投票按钮 onClick → 新 helper
  `window.srFindingFeedback(findingId, vote, bookId)`（仿 `srReviewFinding` 模式：
  POST 后 `srLoadDeep(force)` 重载）；real 模式接真后端，demo 保持本地。

### 验收标准
- 投票持久化且幂等（重复投同向不重复计；改向更新）。
- 聚合按规则改 `finding.confidence`；维度矩阵单元格 / 画像维度摘要的置信度随之变化
  （deep 重载后体现）。
- 后端单测：投票→聚合→confidence 改档；幂等用例。`vite build` 通过。

### 依赖 / 风险 / 工作量
- 依赖：confidence 聚合规则需产品定稿（升降档阈值）。
- 风险：中（涉及置信度语义，需想清楚与 synthesize 既有置信度的关系）。
- 工作量：**中**（后端端点+存储+聚合+测试 + 前端接线）。

---

## 立项 C：策略 C（RAG）三粒度向量召回

> ✅ **已完成(2026-06-18,见 progress 第八轮)**。`services/style_reference/rag.py` 落地三粒度
> 索引 + 确定性召回/rerank(无 LLM,守 inject<50ms)+ C 分支真召回 + 防漂移按上下文重召回 +
> 生命周期(synthesize 建 / purge 删)+ 14 用例。后续可选:LLM rerank 接非实时路径
> (`style_ref_rag_rerank` hook 已就绪)、真实黄金语料 + chroma(WSL)上的 hit@5 正式评测。
> 下文为原立项书,保留作实现参照。

### 背景与动机
注入策略 A（System Prompt）/ B（Few-shot，已真实读 `scene_samples_index`）已实装；
**C（RAG）是设计明确的 Phase 3 大件**，当前 strategy C 在 `injection.py` 退化为 A 的变体
（forbidden 摘要 + 不注入 metric，无真实召回）。目标：生成场景时**实时**从参考书检索
最相关的句子 / 段落 / 场景级样例动态注入，尤其服务长文续写防漂移（带最新上下文重召回）。

### 范围
- **做**：三粒度（sentence / paragraph / scene）向量索引 + 召回 + rerank；
  `injection.py` 的 C 分支真召回；索引随 book/profile 生命周期构建与清理。
- **不做**（本立项外）：换向量后端、跨书检索、训练自有 embedding。

### 接入点（精确）
- 向量层：`backend/src/novel_system/services/vector_store.py`（已有 chroma/memory 抽象，
  `NOVEL_SYSTEM_VECTOR_BACKEND=memory` 为 Windows 确定性后端）。三粒度索引为新增。
- 注入：`injection.py` 的 `_render` strategy C 分支真召回 + rerank；query 来源为
  `InjectionRequest.context_text`（续写最新上下文，设计 §12 已定义防漂移按字数重注入）。
- 模型路由：`config/models.yaml` **当前无** `style_ref_rag_rerank` / `style_ref_fewshot_select`
  任务（设计 §6.1 列了但未落）——**第一步补 task 路由 + prompt 模板**。
- 索引生命周期：构建时机（synthesize 后较合适，profile 就绪即建）；清理挂到
  `cleanup.py` / 删书路径（`purge_derived_data`）。

### 验收标准（沿用设计 §10 Phase 3）
- 三粒度召回各自 hit@5 ≥ 0.7（需评测语料，黄金语料可复用）。
- 长文续写场景 inject 被调用 ≥3 次且 RAG snippet 随上下文变化（防漂移）。
- chroma 集成测试走 WSL（`@pytest.mark.chroma_integration`，Windows 自动跳过）；
  memory 后端给确定性单测。

### 依赖 / 风险 / 工作量
- 依赖：chroma 仅 Linux/WSL；召回质量评测需语料与人工校核。
- 风险：高（检索质量、索引一致性、与防漂移循环的联动）。
- 工作量：**大**（独立 Phase 3 工程，建议单独排期，含评测）。

---

## 备注

- 三项**相互独立**，可任意顺序、任意会话单独启动；A、B 不互相依赖，C 完全独立。
- 启动任一项时：先读本节对应立项 + `docs/style-reference-progress.md` 第八节最新一轮，
  确认主线状态未变，再按"接入点"开工。
- 与"量化容差收紧"（已于 2026-06-13 第七轮完成）无关，勿重复。
