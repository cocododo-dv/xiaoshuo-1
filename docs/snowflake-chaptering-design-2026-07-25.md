# 雪花「整理成章节结构」重新设计（2026-07-25）

状态：**Phase 1、2、3 全部已实施**，遗留 Vue 兼容面已收尾（见 §9、§10）。没有 Phase 4。实施前以当前代码为准重新核对行号与函数名。

> 2026-09-18（阶段 V）之后的现行口径见[雪花方法契约](snowflake-method-contract.md)第 6 行与变更记录 V：故事序只有一个来源（09 草稿的行序），`scene_seq` 只表示章内位置；面板打开用 `auto`，「按场景分章」是不落库的预览（`from_scenes`），确认时 `replace_chapters=true` 整张落库；面板只许挪章界、拆章、并章。本文 §4–§6 描述的「默认 `spine_anchor`」「章内顺序即载荷顺序」「移到相邻章 = 追加到末尾」已被取代。

## 1. 问题诊断

### 1.1 根因：整条雪花管线里「章」没有归属者

| 环节 | 是否承载章结构 | 证据 |
|---|---|---|
| 07 长篇大纲 | 前端脚手架里作者真的编了章（`{id, act, title, summary, spine}`），但后端只存 `{"paragraphs": ["","","",""]}` 四段自由文本 | `snowflake_steps.py` `long_synopsis.default_draft`；`ws-snow-sync.jsx` `canonFromFE("outline")` 把章折成 `NN 标题：摘要（灾一）` 文本行 |
| 09 场景列表 | 无章字段 | 脚手架 `{id,type,line,pov,place,event,crucible,fn,spine}`（`ws-snow.jsx` `addScene`）；`canonFromFE("scenes")` 不发 `chapter_id` |
| 10 场景规划 | 无章字段 | `canonFromFE("planning")` 同上 |
| LLM 提示词 | 明确禁止 LLM 分章 | `config/prompts.yaml` `snowflake_generate_scene_list`：`Leave row_uid/scene_id/chapter_id … as "" — the server assigns identities` |
| 服务端 | **并不分章** | `snowflake_workspace.py` `_sync_scene_plans`：`current_chapter_id = f"{project_id}_CH01"`，且作者传入的 `chapter_id` 被 `patch.pop("chapter_id", None)` 丢弃 |

提示词把分章责任交给服务端，服务端把所有场景都放进 `CH01`。**没有任何一方真正在分章。**

实测（24 场、07 里编了 12 章、`target_chapter_count=12`，按前端 `canonFromFE` 的字段形状走 PATCH + approve）：

```
distinct chapter_id on scene plans: ['PRJ_B34867933F_CH01']
outline plan chapters: 1
    chapter PRJ_B34867933F_CH01 | PRJ_B34867933F_CH01 | scenes: 24 | goal: 推进雪花法场景：第 1 场事件
created_chapter_count: 1     created_scene_count: 24
```

章标题就是章 id（`_build_outline_plan` 里 `title = item.get("chapter_title") or chapter_id`，而 `chapter_title` 在 `_sync_scene_plans` 里被初始化成 `chapter_id`），章目标是自动拼的占位串。

`long_synopsis` 还是 warning-only 步骤（`MATERIALIZATION_WARNING_STEPS`），可以整步跳过 —— 与「物化时根本不读它」互相印证。

### 1.2 同一个按钮三条路径，越做对越糟

`ws-catalog.jsx` `adoptOutline` 的分叉：

| 条件 | 走哪条 | 结果 |
|---|---|---|
| `SnowSync.readyToMaterialize()` 为真 | 后端 `materialize` + `outline/approve` | **1 章 / 24 场** |
| 闸门未过、`window.s2Materialize` 可用 | `s2MaterializePreview` 按灾一/灾二/灾三脊柱锚点把场铺进 07 的章 | **12 章，这条才是对的** |
| 以上都不行 | `__adoptByDiff` | 只建空壳章，每章塞一个「开场」占位场 |

雪花做得越完整，反而掉进最差的那条。文案随之失真：`s2AdoptOutline` 的确认框问「把大纲中的 **12** 章并入目录？」，落库 1 章，toast 报「已整理并写入 **1** 章」；顶部按钮 title 写着「07 章节 + 09 场景 + 10 规划 → 章节目录」，而 07 那部分在主路径上完全没被读取。

第四条野路径：09 步的「采用到当前章」（`ws-snow.jsx` `adoptScenes`）把整份场景表倒进 `WsCatalog.currentChapter()`，绕过后端。

### 1.3 缺陷 A：scene_id 撞号 → 静默丢场

`_sync_scene_plans`：`scene_id = f"{chapter_id}_SC{scene_seq:02d}"`，创建时铸死、之后永不更新；`scene_seq` 则每次 PATCH 按传入列表重算。

复现：写 4 场 → 删掉第 3 场 → 末尾新增一场。新场拿到 `scene_seq=4` → `CH01_SC04`，与原第 4 场（`scene_id` 仍是 `CH01_SC04`，但 `scene_seq` 已被压到 3）撞号：

```
构思侧场景计划: [(S01,…_SC01,1,事件1), (S02,…_SC02,2,事件2), (S03,…_SC03,3,事件3),
                (S04,…_SC04,3,事件4), (S05,…_SC04,4,新加的一场)]
章: …_CH01 场数: 5 [(…_SC01,事件1), (…_SC02,事件2), (…_SC03,事件3),
                     (…_SC04,新加的一场), (…_SC04,新加的一场)]
created_scene_count: 4
落库 SceneCard: 4 → 「事件4」彻底消失
```

两个放大点：`_build_outline_plan` 的 `detail_by_id` 按 `scene_id` 建字典（后者覆盖前者）；`approve_outline_plan` 用 `session.get(SceneCard, scene_id)`（第二条更新第一条而不是新建）。`SnowflakeScenePlan` 只有 `(project_id, row_uid)` 唯一索引，`scene_id` 没有任何唯一性约束兜底。

### 1.4 缺陷 B：幽灵场堵死闸门

`_sync_scene_plans` 只增不改删，全仓库没有任何删除 `SnowflakeScenePlan` 的代码路径。作者在前端删掉的场，后端行永远留着；它拿不到第 10 步细化，`diagnose_scene_detail` 判成 `rewrite`，于是：

```
SNOWFLAKE_NOT_READY / SNOWFLAKE_TRIAGE_BLOCKED
blockers: ["「事件3」（…_CH01_SC03） 系统建议重写，请先确认急救判断。"]
```

作者在场景列表里看不到这一场（急救面板能看到，但作者的认知是「我明明删了」），于是物化被一个不存在的场景永久堵死。上面那次实测里，把它在急救面板判 `pass` 之后，它还真的落库成了正式场景卡。

---

## 2. 设计目标与非目标

**目标**

1. 「章」在构思侧成为一等结构，有稳定身份、有作者可编辑的标题与目标。
2. 「整理成章节结构」从黑盒动作变成**可预览、可调整、可确认**的一次编排：作者按下按钮之前就知道会得到什么。
3. 一个入口一条契约：删掉降级路径与野路径，前端不再持有第二套分章逻辑。
4. 修掉 A（撞号丢场）与 B（幽灵场），并用结构约束（唯一索引）防止复发。
5. 物化后仍可重新分章，且不丢正文。

**非目标**

- 不改雪花十步的步骤数与顺序（分章是物化环节的一部分，不是第 11 步）。
- 不引入 LLM 自动分章作为主路径（可作为后续可选建议通道，见 §9 Phase 3）。
- 不在 `frontend/` 的 Vue 兼容面上实现分章（最终仅把一处误导跳转改成诚实提示，见 §10）。

---

## 3. 数据模型改动

### 3.1 新表 `snowflake_chapter_plans`

与 `SnowflakeScenePlan` 对称，承载构思侧的章。

| 列 | 类型 | 说明 |
|---|---|---|
| `chapter_plan_id` | String, PK | `snowflake_chapter_plan_{project_id}_{row_uid}` |
| `project_id` | FK → `story_projects` | |
| `row_uid` | String | 系统铸造、不可变的稳定锚；唯一索引 `(project_id, row_uid)` |
| `chapter_seq` | Integer | 1-based 章序，物化时决定 `display_order` 与章 id |
| `act` | Integer | 1/2/3，来自 07 的三幕 |
| `title` | String | 作者写的章标题（「雨夜来信」） |
| `summary` | Text | 07 的本章一句话推进 |
| `spine` | String | `灾一`/`灾二`/`灾三`/`""`，结构铰链锚点 |
| `chapter_goal` | Text | 物化进 `ChapterGoal.chapter_goal`；缺省回退 `summary` |
| `status` | String | `draft` / `approved` |
| `source_step_run_id` | String, nullable | 来自哪次 07 步保存 |
| `removed_at` / `removed_by` | String, nullable | 软删（见 §3.3） |
| `created_at` / `updated_at` | String | |

索引：`ix_snowflake_chapter_plans_row_uid (project_id, row_uid) unique`、`ix_snowflake_chapter_plans_seq (project_id, chapter_seq)`。

### 3.2 `snowflake_scene_plans` 改动

| 改动 | 说明 |
|---|---|
| 新增 `chapter_plan_id` (String, FK → `snowflake_chapter_plans`, nullable) | 构思侧的章归属。`NULL` = 尚未分章 |
| `chapter_id` 语义变更 | 不再是创建时铸死的身份，而是**物化目标章 id**，由分章结果推导：`f"{project_id}_CH{chapter_seq:02d}"`；分章保存时写入 |
| `scene_id` 铸造规则变更 | 新建行一律 `f"{project_id}_SC_{row_uid}"`，与章解绑、天然唯一 |
| 新增唯一索引 `ix_snowflake_scene_plans_scene_id (project_id, scene_id) unique` | 撞号从静默覆盖变成硬错误 |
| 新增 `removed_at` / `removed_by` (String, nullable) | 软删（见 §3.3） |
| 新增 `orphaned_flag` (Integer, default 0) | 构思侧已删、但目录侧已有 `SceneCard` 的场（不能直接删，交作者裁决） |

`_apply_scene_patch` 继续拒绝作者直接写 `scene_id`；`chapter_id` 也继续从 `_sanitize_scene_patch` 里 pop —— 章归属只能通过 §4 的分章契约变更，不能通过步骤 PATCH 夹带。

### 3.3 软删语义

`_sync_scene_plans` / 新增的 `_sync_chapter_plans` 在全量 draft 落库时执行收口：

- incoming 列表里没出现的 `row_uid`：
  - 若该场**未物化**（无对应 `SceneCard`）→ 置 `removed_at`，从 workspace payload、`_scene_board`、诊断、`_materialization_gate`、`_resync_status`、物化输入里一律排除。
  - 若该场**已物化** → 置 `orphaned_flag=1`（保留 `removed_at` 为空），在分章预览的告警区列出，由作者选择「一并从目录删除」或「保留在目录」。绝不静默删除已有正文的场。
- 防误删护栏：`scenes` 数组为空时**不执行收口**（避免 FE 传空 draft 时清空全书）；每次收口写 `OperationLog(event_type="snowflake_scene_plan_removed")`，行不物理删除，可恢复。

### 3.4 `long_synopsis` 草稿契约升级

```jsonc
// 现在
{ "paragraphs": ["…", "…", "…", ""] }

// 之后（paragraphs 保留为派生可读文本，chapters 是真相）
{
  "paragraphs": ["…", "…", "…", ""],
  "chapters": [
    { "row_uid": "", "chapter_seq": 1, "act": 1, "title": "雨夜来信",
      "summary": "一封旧信把她拉回雨城。", "spine": "", "chapter_goal": "" }
  ]
}
```

`row_uid` 由服务端铸造并回写进 `run.draft_json`（沿用 `_sync_scene_plans` 现有的回写模式）。前端 `canonFromFE("outline")` 直接发 `chapters`，不再把章折成文本行；`feFromCanon("outline")` 优先读 `chapters`，`chapters` 缺席时才回退到现有的正则解析（兼容历史草稿与旧 LLM 输出）。

提示词 `snowflake_generate_long_synopsis` 的 `structured_schema` 增加 `chapters` 数组，`task_prompt` 改为直接产出结构化章表；现有的 `"NN 章名：一句话"` 文本格式作为 `paragraphs` 的可读镜像保留。

---

## 4. API 契约

### 4.1 新增：分章预览（只读，不写库）

```
POST /api/v2/projects/{project_id}/snowflake-workspace/chapter-plan/preview
body: { "strategy": "spine_anchor" | "even" | "keep_current", "target_chapter_count": int? }
```

响应：

```jsonc
{
  "strategy": "spine_anchor",
  "chapters": [
    { "row_uid": "…", "chapter_seq": 1, "act": 1, "title": "雨夜来信", "spine": "",
      "chapter_goal": "…", "scene_count": 2,
      "scenes": [ { "scene_plan_id": "…", "row_uid": "…", "title": "…", "scene_seq": 1,
                    "anchored": false, "planned": true } ] }
  ],
  "unassigned": [ { "scene_plan_id": "…", "title": "…", "reason": "no_anchor_segment" } ],
  "removed_scenes": [ { "scene_plan_id": "…", "title": "事件3", "orphaned": false } ],
  "warnings": [
    { "kind": "unassigned_scenes", "severity": "warning", "message": "2 场未分配（S23 S24）" },
    { "kind": "empty_chapter", "severity": "warning", "message": "「06 世界观碎」没有分到任何场" },
    { "kind": "oversized_chapter", "severity": "warning", "message": "「05 父亲的谎」分到 9 场，是均值的 4.5 倍" },
    { "kind": "orphaned_scene", "severity": "blocker", "message": "「事件3」已从场景列表删除，但目录里已有正文" }
  ]
}
```

分章算法（`strategy: spine_anchor`，服务端实现，取自现有前端 `s2MaterializePreview` 的规则）：

1. 锚定：`spine` 相同的场与章互相锁定（`灾一`/`灾二`/`灾三`），只保留场序与章序**同时单调递增**的锚。
2. 铺展：相邻锚点之间的场，按顺序均匀铺进相邻锚点之间的章。
3. 兜底：某区间没有可用章 → 归入前一个锚点所在章；仍无 → 归入首章。
4. `even`：忽略锚点，按 `ceil(场数 / 章数)` 均分。`keep_current`：保持已有 `chapter_plan_id`，只为新场找位置。

### 4.2 新增：保存分章

```
PATCH /api/v2/projects/{project_id}/snowflake-workspace/chapter-plan
body: {
  "chapters": [ { "row_uid": "…", "chapter_seq": 1, "act": 1, "title": "…",
                  "spine": "", "chapter_goal": "…" } ],
  "assignments": [ { "scene_plan_id": "…", "chapter_row_uid": "…", "scene_seq": 1 } ],
  "removed_scene_action": { "…scene_plan_id": "delete_from_catalog" | "keep_in_catalog" }
}
```

单事务写 `chapter_plan_id`、`chapter_id`、`scene_seq`；返回 `{ chapter_plan, workspace }`。

校验：每个 `scene_plan_id` 必须属于本项目且未软删；每个 `chapter_row_uid` 必须存在；同一章内 `scene_seq` 必须唯一且连续。

### 4.3 变更：物化

```
POST /api/v2/projects/{project_id}/snowflake-workspace/materialize
body: { "chapters": [...], "assignments": [...] }   // 可选，带则先落分章再物化，同一事务
```

- 分章未完成（存在 `chapter_plan_id IS NULL` 的活跃场）→ `409 SNOWFLAKE_CHAPTER_PLAN_REQUIRED`，带 `author_action` 指向分章面板。
- `_build_outline_plan` 改为读 `SnowflakeChapterPlan` 分组，不再从场景行反推章：

  | OutlinePlan 章字段 | 来源 |
  |---|---|
  | `chapter_id` | `f"{project_id}_CH{chapter_seq:02d}"` |
  | `title` | `chapter_plan.title` |
  | `chapter_goal` | `chapter_plan.chapter_goal or chapter_plan.summary` |
  | `writer_brief_json.chapter_title` | `chapter_plan.title` |
  | `narrative_json` | `{ "title", "act", "spine", "tension" }` |
  | `display_order` | `chapter_seq` |

  `approve_outline_plan` 相应补写 `ChapterGoal.narrative_json` 与 `display_order` —— 这是目录侧 `catalog.chapter_title()` 的首选字段，修好之后目录里显示「01 雨夜来信」而不是 `PRJ_XXXX_CH01`。

### 4.4 变更：回流同步

`_scene_card_resync_patch` 增加 `chapter_id` 与 `scene_seq`；`resync` 在 `chapter_id` 变更时移动 `SceneCard.chapter_id`（复用 `catalog` 已有的 move 语义与 `scene_seq` 重排），并在响应的 `affected_runtime` 里明示「N 场将换章」。正文（`AuthorDraft` / `FinalScene`）跟 `scene_id` 走，换章不影响。

---

## 5. 前端改动

### 5.1 新增分章预览面板

`s2AdoptOutline` 的 `window.confirm` 弹窗替换为一个预览面板（新文件 `frontend-react/src/ws-snow-chapters.jsx`，或作为 `ws-snow.jsx` 内的抽屉组件）：

```
整理为章节结构 · 预览
─────────────────────────────────
第一幕
  01 雨夜来信      ← S01 S02        [2 场]
  02 旧案卷宗      ← S03            [1 场]
  03 第一个证人    ← S04 S05        [2 场]
  04 被迫卷入 灾一 ← S06 ●锚定      [1 场]
第二幕
  05 父亲的谎      ← S07 S08        [2 场]
  06 世界观碎 灾二 ← S09 ●锚定      [1 场]
  ...
─────────────────────────────────
⚠ 2 场未分配（S23 S24）→ 归入末章 / 手动指派
⚠ 「事件3」已从场景列表删除 → 一并清理

         [ 取消 ]  [ 确认写入 12 章 / 24 场 ]
```

行为：

- 打开时 `POST …/chapter-plan/preview`（默认 `spine_anchor`）。
- 顶部提供策略切换（脊柱锚点 / 均分 / 保持现状）与「重算」。
- 场可在章之间拖拽或用上下键移动；章可改标题、改序、增删。改动只在前端状态里，不即时写库。
- 告警区置顶且不可折叠；`severity: blocker` 未处理时确认按钮禁用。
- 确认 → 一次 `POST …/materialize`（带 `chapters` + `assignments`）→ `POST …/outline/approve` → `WsCatalog.reset()`。
- 已物化项目打开时，标题改为「重新分章」，告警区额外显示「N 场已落库场景将换章」。

### 5.2 路径合一（删除项）

| 删除 | 理由 |
|---|---|
| `ws-snow.jsx` `s2MaterializePreview` / `s2MaterializeApply` / `window.s2Materialize` | 分章算法搬到后端，前端不再持有第二套 |
| `ws-catalog.jsx` `adoptOutline` 的降级分支与 `__adoptByDiff` | 唯一路径就是分章面板；建空壳章改为目录侧显式的「新建章」动作 |
| `ws-snow.jsx` `adoptScenes`（09 步「采用到当前章」） | 第四条野路径，绕过后端把所有场倒进当前章 |
| `s2AdoptOutline` 里的 `confirmFn` 文案分支 | 由预览面板取代；不再存在「说 12 章写 1 章」的可能 |

07 步的「采用到章节编排」与顶部「整理为章节结构」指向同一个面板。

### 5.3 SnowSync 契约

- `SnowSync.materialize()` 接受 `{chapters, assignments}` 并透传。
- 新增 `SnowSync.chapterPreview(strategy)` / `SnowSync.saveChapterPlan(payload)`。
- `canonFromFE("outline")` 发结构化 `chapters`；`feFromCanon("outline")` 优先读 `chapters`。
- `mergeCanon` 的 `CANON_ID_KEYS` 增加 `chapter_plan_id`，使章数组按 id 对位合并（与场景一致）。

---

## 6. 迁移（`20260725_0075`，当前 head 为 `20260722_0074`）

1. 建表 `snowflake_chapter_plans`。
2. `snowflake_scene_plans` 加列 `chapter_plan_id` / `removed_at` / `removed_by` / `orphaned_flag`。
3. **scene_id 去重修复**（唯一索引的前置条件）：
   - 逐 project 扫描。该 project 无任何 `SceneCard` → 全部 `scene_id` 重铸为 `f"{project_id}_SC_{row_uid}"`。
   - 已物化的 project → 只重铸「`scene_id` 重复」且「无对应 `SceneCard`」的行，保住已落库的关联；若一组重复行**全部**有 `SceneCard`（理论上不可能，PK 唯一），记 `OperationLog` 并中止迁移，交人工处理。
   - `row_uid` 为空的历史行先补铸 `row_uid`。
4. 建唯一索引 `ix_snowflake_scene_plans_scene_id (project_id, scene_id)`。
5. 从现有 `long_synopsis` 草稿的 `paragraphs` 解析出章表（复用 `feFromCanon("outline")` 的同一套正则），回填 `snowflake_chapter_plans`；解析不出章的项目留空，作者进面板时按 `even` 策略起草。
6. 已物化项目：按现有 `SceneCard.chapter_id` 反推 `chapter_plan` 与 `chapter_plan_id`，保证「打开分章面板 = 看到现状」而不是「看到一个空白重排」。

降级（`downgrade`）：删索引与新列、删表；`scene_id` 的重铸**不可逆**，需在迁移文件顶部注明并建议先备份（与 `0073` 同样的处置）。

> 修订号提醒：`0075` 这个号在 2026-07-18 的 real-only 重构里被占用过一次，那次连同代码一起撤回、运行库靠 `bak-0075-20260718` 备份重建。当前 head 确认为 `20260722_0074`，`0075` 已空出可用；但**落号前必须现场跑 `python -m alembic heads` 复核**，并且在实施 Phase 1 前先备份运行库。

> 2026-08-02 追记：审计发现仍有运行库保留 `20260717_0075` 戳，因此该历史不能视为已完全撤回。原始 real-only 迁移已恢复，并通过合并迁移 `20260802_0077` 与当前分章链汇合；`0074`/`0075` 的短序号因分支歧义不再作为预检别名。

> 注意 `test_metadata_isolation.py::test_migration_built_schema_matches_orm_models` —— 新表、新列、新索引必须同时出现在 ORM 模型（含 `__table_args__` 里的索引声明）和迁移里，否则漂移守卫会红。

---

## 7. 测试计划

**后端（`backend/tests/`）**

| 用例 | 断言 |
|---|---|
| `test_snowflake_chaptering.py::test_materialize_produces_authored_chapters` | 24 场 + 07 编 12 章 → 物化出 12 章；`ChapterGoal.narrative_json["title"] == "雨夜来信"`；`catalog.chapter_title()` 不返回章 id（**回归 §1.1 的 1 章 bug**） |
| `…::test_scene_id_survives_delete_then_add` | 删场再加场后 `scene_id` 不重复；物化后场景卡数 == 活跃场景计划数（**回归缺陷 A**） |
| `…::test_removed_scene_does_not_block_gate` | 删场后闸门不再被幽灵场堵死，且不落库（**回归缺陷 B**） |
| `…::test_orphaned_scene_requires_author_decision` | 已物化的场被从构思删除 → 预览给 `blocker`，未裁决前 `materialize` 409 |
| `…::test_materialize_without_chapter_plan_returns_author_action` | 未分章直接物化 → 409 `SNOWFLAKE_CHAPTER_PLAN_REQUIRED` + `author_action` |
| `…::test_spine_anchor_preview_is_deterministic` | 同一输入两次 preview 结果逐字节相同；锚点单调性、区间均分、兜底三条规则各一例 |
| `…::test_rechapter_moves_scene_cards_without_losing_drafts` | 重新分章后 `SceneCard.chapter_id` 迁移，`AuthorDraft` / `FinalScene` 不变 |
| `test_metadata_isolation.py` | 现有漂移守卫通过 |
| 迁移测试 | 构造含重复 `scene_id` 的库 → `upgrade head` 成功、唯一索引建得上、已物化行的 `scene_id` 未变 |

**前端（`frontend-react/src/*.test.jsx`，vitest）**

| 用例 | 断言 |
|---|---|
| `ws-snow-chapters.test.jsx` | preview 渲染章/场分组；拖拽改归属只改本地状态；`blocker` 未处理时确认按钮 disabled；确认发出的 payload 与面板显示一致 |
| `ws-catalog.test.jsx`（改） | 删掉降级路径的三个用例（`s2Materialize` 分支、`__adoptByDiff` 兜底），换成「唯一路径 + 失败上抛」 |
| `ws-snow-sync.test.jsx`（改） | `canonFromFE("outline")` 产出结构化 `chapters`；`feFromCanon` 对无 `chapters` 的历史草稿仍能回退解析 |

**E2E**：`frontend-react/scripts/run-smokes.mjs` 的雪花冒烟增加一段「10 步做完 → 开分章面板 → 确认 → 目录里出现 N 章且标题正确」。

---

## 8. 风险与取舍

| 风险 | 处置 |
|---|---|
| `scene_id` 重铸不可逆 | 迁移顶部注明；建议先 `cp novel_system.db novel_system.db.bak-0075`；已物化行不动 |
| 收口误删 | `scenes` 空数组不收口；行软删不物删；每次写 `OperationLog`；已物化场只标 `orphaned` 不删 |
| 前端删掉降级路径后，闸门未过就无法整理 | 这是**有意的**：整理前必须过闸门，否则得到的就是半成品结构。闸门未过时面板显示具体阻断项与跳转按钮，而不是偷偷走另一条算法 |
| 07 章表与 09 场景表脱节（作者改了章但没重分场） | workspace 增加 `chapter_plan_status`：章表 `updated_at` 晚于最后一次分章 → 面板顶部提示「章表已变更，建议重算」；沿用现有 stale 机制的语汇，不新造概念 |
| 已实现的「章节编排 LLM 接入」（`docs/chapter-arrangement-llm-design-2026-07-16.md`）与本设计的关系 | 那是目录侧「章节蓝图」的 LLM 通道，消费的是已物化的 `ChapterGoal`；本设计只改「构思 → 目录」这一刀，不动它。实施时需核对 `narrative_json` 字段是否被它读写 |

---

## 9. 实施顺序

**Phase 1 — 止血（已实施，2026-07-25）**

| 改动 | 位置 |
|---|---|
| `scene_id` 铸造不再依赖 `chapter_id + scene_seq` | `snowflake_workspace._mint_scene_id` / `_sync_scene_plans` |
| 唯一索引 `(project_id, scene_id)` | `db/models.py` `SnowflakeScenePlan.__table_args__` + 迁移 |
| 同一 payload 内 `row_uid` 重号自动拆行 | `_sync_scene_plans` 的 `seen_row_uids` / `duplicate_in_payload` |
| 软删收口 + `orphaned_flag` + `OperationLog` | `_reconcile_removed_scene_plans` |
| 软删行从所有读路径消失 | `_scene_plans`、`_scene_plan_for_triage_item`、`apply_scene_triage_repair`、`update_scene_plan`、`causal_chain_validator`、`longform_tower.derive_structure` |
| 前端场景行铸号不再撞已存在的号 | `ws-snow.jsx` `s2NextSceneRowId`（从组件内提出的纯函数，可单测） |
| 迁移 + 历史数据修复 | `alembic/versions/20260725_0075_scene_plan_identity_and_soft_delete.py` |

实施中发现的一处关键约束（设计稿原先没写到）：**草稿自带 `scene_id` 时必须沿用它，不能一律用 row_uid 铸**。规划器骨架与 LLM 结构化输出都只回显 `scene_id`（提示词明确要求 `row_uid` 留空），第 9→10 步之间正是靠这个字符串对位；一律重铸会让第 10 步认不回第 9 步建下的行，于是每次生成都复制一份，场景数翻倍且原行拿不到细化、被诊断成 rewrite。最终规则是：**带 `scene_id` 且未被占用 → 沿用；否则 → `f"{project_id}_SC_{row_uid}"`**。前端 `canonFromFE("scenes")` 恰好不发 `scene_id`，所以作者手改场景表这一路始终走 row_uid 基、天然不撞号。

回归测试：`backend/tests/test_snowflake_scene_identity.py`（10 条）、`backend/tests/test_migration_0075_scene_plan_identity.py`（历史库修复路径）、`frontend-react/src/ws-snow-scene-id.test.jsx`（6 条）。

Phase 1 之后「1 章」问题仍在，但不再丢数据、不再被幽灵场堵死。

**Phase 2 — 分章主体（已实施，2026-07-25）**

| 改动 | 位置 |
|---|---|
| `SnowflakeChapterPlan` 表 + `scene_plans.chapter_plan_id` / `.spine` | `db/models.py`、迁移 `20260725_0076` |
| 分章服务（预览 / 保存 / 自动分配 / 只读状态 / 惰性派生） | 新文件 `services/snowflake_chaptering.py` |
| 07 草稿契约升级为 `{paragraphs, chapters}` + `_sync_chapter_plans` | `snowflake_steps.py`、`snowflake_workspace.py` |
| `chapter-plan/preview`、`PATCH chapter-plan`、`materialize` 接受 `{chapters, assignments}` 或 `strategy` | `api/routes/snowflake_workspace.py` |
| 物化改读章表分组，章名/幕/脊柱/顺序落到目录 | `_build_chaptered_outline_plan`、`projects.approve_outline_plan` |
| 分章闸门（未分章 → 409 + `open_chapter_plan`） | `_materialization_gate` |
| resync 支持换章（`chapter_id` / `scene_seq` 进回流补丁） | `_scene_card_resync_patch` |
| 前端分章预览面板 | 新文件 `ws-snow-chapters.jsx` |
| 路径合一：删除 `s2MaterializePreview` / `s2MaterializeApply` / `s2AdoptOutline` / `s2MaterializedSid` / 09 步「采用到当前章」/ `adoptOutline` 的降级链 | `ws-snow.jsx`、`ws-catalog.jsx` |
| `spine` 与结构化 `chapters` 往返保真 | `ws-snow-sync.jsx` `canonFromFE` / `feFromCanon` |
| 提示词产出结构化章表 + 场景标脊柱 | `config/prompts.yaml`（`long_synopsis` v4、`scene_list` v5）+ `_sanitize_chapter_items` |

实施中发现、设计稿没预料到的四件事：

1. **场景的脊柱标记根本到不了后端。** `canonFromFE("scenes")` 不发 `spine`，`feFromCanon` 还硬写回 `""` —— 作者在第 9 步标的灾一/灾二/灾三 一刷新就没了。脊柱锚点分章要靠它，所以补了 `SnowflakeScenePlan.spine` 并让它往返保真。
2. **散文式 07 草稿会被编造成假章。** 回退解析原本把任意非空行都当一章，规划器骨架那种「四段散文」的草稿于是产出一堆假章，还抢在真正的章归属之前落库。改成只认 `NN 章名：…` 的真章行 —— 宁可解析不出章，也不造假章。
3. **场景行自带的 `chapter_id` 是真实归属数据，不该丢。** 规划器骨架与任何回填了 `chapter_id` 的 LLM 输出都带着章归属；沿用它是对的。但前端那一路所有场都是退化默认值 `…_CH01`，把它当「已分章」正是要修的老毛病。判据定为**出现 ≥2 个不同章号才认**。
4. **闸门必须和物化说同一句话。** 物化前会 `ensure_chapter_plans` 派生并绑定既有归属，而工作台闸门是只读的、看不到这一点 —— 一度出现「UI 报 blocked，实际却能物化」。`SnowflakeChapteringService.status` 因此做成只读地复算同一套派生优先级。

另外修了一处我自己引入的回归：物化写 `narrative_json` 会覆盖作者在章节编排里改过的章名（既有契约明确 `narrative_json` 是目录侧权威字段）。改为**只在新建章时播种**。

回归测试：`backend/tests/test_snowflake_chaptering.py`（10 条）、`frontend-react/src/ws-snow-chapters.test.jsx`（8 条），以及改写后的 `ws-catalog.test.jsx` / `ws-snow.test.jsx`（旧降级路径用例换成「唯一路径 + 失败上抛」「点击只开预览、不落库」）。

**Phase 3 — 增强（已实施，2026-07-25）**

| 改动 | 位置 |
|---|---|
| `POST …/chapter-plan/suggest`：LLM 分章建议（只读，回包形状与 preview 一致 + `rationale`） | `SnowflakeChapteringService.suggest`、`SnowflakeWorkspaceLLMService.chapter_plan_suggestions` |
| 新 LLM 节点 `snowflake_chapter_plan` + 模板 `snowflake_chapter_plan_suggest` | `llm_node_registry.py`、`config/models.yaml`、`config/prompts.yaml` |
| 节奏体检（每章场数分布、三幕配比、灾难落点是否在铰链上） | `_rhythm_report`，进 `preview` 回包的 `rhythm` |
| 面板「AI 建议」按钮 + 节奏条 + 建议理由条 | `ws-snow-chapters.jsx`、`rhythmSummary` |

三条实施决定：

1. **suggest 是 fail-closed，不给 `fallback_payload`。** 顾问型端点在本仓库通常降级返回规则结果（`source="fallback"`），但这里不行：作者点的是「让 AI 建议分章」，返回一份规则算出来的东西并称之为建议就是撒谎；而规则分章本来就以 `spine_anchor` 策略明明白白摆在面板上，随时能用，不需要伪装。
2. **模型输出被硬约束回安全范围**（`_normalize_chapter_plan_output`）：只认白名单内的 `scene_plan_id` / `chapter_row_uid`，一个场只认第一次出现，模型没提到的场保留确定性提案的归属并在 `kept_from_deterministic` 里如实列出。建议允许不完美，但不能因为模型编了个不存在的 id 就把作者的场丢掉。
3. **节奏体检只报结构本身看得出的东西**：每章场数分布、三幕章场配比、三个灾难是否还在它该在的幕（灾一/灾三 要在本幕最后一章，灾二 只看幕）。**不做「张力曲线评分」**——这一步还没有正文，谈张力是空话。所有提示都是 `advisory`，从不阻断：作者故意把灾二后置是合法选择。

回归测试：`test_snowflake_chaptering.py` 追加 3 条（节奏体检、缺脊柱只提示不阻断、suggest fail-closed），`ws-snow-chapters.test.jsx` 追加 4 条（`rhythmSummary` 的结论与「沉默不等于合格」）。

---

## 10. 遗留 Vue 兼容面的处置（2026-07-25，无 Phase 4）

三个阶段完成后，`frontend/`（遗留 Vue，5173）出现一处**死路**：分章闸门挡住整理按钮（disabled），
而闸门项 `chapter_plan_required` 带着 `step_key=long_synopsis`，被 `gateItemTargetsStep` 送到第 07 步——
但 `SnowflakePlanningStage.vue` 的 `field.kind` 分支链里**没有 `chapters` 这一支、也没有兜底 `v-else`**，
第 07 步的章节表在 Vue 里根本渲染不出来。作者被送进一片空白。

先核实了两件更要紧的事，结论都是**安全**：

- **没有数据损坏风险。** Vue 保存步骤时整份 `draft` 原样回传（`snowflakeWorkbench.js:858` 的
  `{ draft: step.draft || {} }`），store 的写入又都是按 key 定点改的，所以新增的 `chapters` 数组在 Vue 里
  是穿透的、不会被抹掉。后端 `_sync_chapter_plans` 的 `if not incoming: return`（空草稿不收口）是第二道锁。
- 章节表在 Vue 里不可见但不丢失：LLM 在第 07 步产出的 `chapters` 仍会正常落进 `snowflake_chapter_plans`。

**决定：不在 Vue 里建任何分章能力，只把误导性跳转改成诚实提示。** 三条理由：

1. 在 Vue 里实现分章会把本设计第 3 条目标（一个入口一条契约）当场推翻——「越做对结果越糟」的
   根子正是前端藏了第二套分章算法，在兼容面上再写一遍等于自愿回到起点。
2. 让 Vue 的整理按钮偷偷带 `strategy` 自动分章比不做更糟：那是个无预览的黑盒动作，正是本次要消灭的
   手感问题；而且 `ensure_chapter_plans` 把已存在章表视为「作者的分章成果，绝不覆盖」，误点一下就先
   落一份机器分配。
3. Vue 依赖在开发机上从未安装过，且**这台 CentOS 7 / Node 16 主机跑不了它的完整测试**（见下），
   在本地验证不了、且注定要删除的界面上写逻辑，风险收益比很差。

改动只有一处：`SnowflakeMaterializationPanel.vue` `goToGateItem` 对 `kind === "chapter_plan_required"`
提前返回，提示分章在潮汐工作台（React，默认 `http://127.0.0.1:5174`）完成，**不再跳到第 07 步**。
这段代码将来随 `frontend/` 一起删除，不留维护债。

回归测试：`frontend/tests/snowflakeWorkbench.spec.js` 新增一条**挂载式**用例（不是 `readSource` 字符串断言）——
挂载面板、点「去分章」、断言提示里出现「潮汐工作台」且 `selectStep` / `setWorkbenchMode("planning")` 均未被调用。
已验证可证伪：摘掉分支后该用例报 `expected '已跳到对应雪花步骤。' to contain '潮汐工作台'`。

### 本机跑遗留 Vue 测试的三个坑（CI 用 Node 22，本机 Node 16 是天花板）

`frontend/node_modules` 原本不存在，装完后 `npx vitest` 会去 npx 缓存取 vitest 4（Node 16 跑不了），
必须直接用 `./node_modules/.bin/vitest` 且 **cwd 必须是 `frontend/`**（多条用例用 `process.cwd()` 拼源码路径）。
在此基础上还有三层 Node 16 限制：

| 现象 | 原因 | 本机绕法 |
|---|---|---|
| `ERR_REQUIRE_ESM @exodus/bytes` | 锁文件钉的 jsdom 29 要 Node ≥18 | 临时把 `frontend/node_modules/jsdom` 指向 `frontend-react` 的 jsdom 25（仅动 node_modules，跑完必须还原） |
| `crypto.hash is not a function` | `@vitejs/plugin-vue` 6 用 Node 20.12+ 的 `crypto.hash()` 编译 SFC | 在 `crypto-polyfill.cjs` 之上再补一个 `crypto.hash` 垫片（临时文件，不入仓库） |
| `styleReferenceApi.spec.js` 12 条 `fetch does not exist` | Node 16 无全局 `fetch` | **无解，本机既有失败**，与本次改动无关；CI（Node 22）正常 |

绕开前两层后本机结果：**59 个测试文件 58 通过，542 条 542-12=530 通过**，
唯一失败的文件就是上表第三行那个环境性失败；`node tests/smoke.mjs` 亦通过。
