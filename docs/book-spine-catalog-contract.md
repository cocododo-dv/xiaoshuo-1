# 一条书脊：构思 → 目录 → 三张台子（阶段 X，2026-09-19）

作者的原话：「雪花生成的章节感觉是孤立的，没有同步到 AI 起草台和写作台。」

这份文档是现行契约：雪花整理出来的章与场怎样真的长在目录里，章节编排、写作台、AI 起草台怎样读到**同一份**东西，以及构思改了之后场景卡怎样跟上。它取代散落在各处的旧假设（位置式场景 id、手工队列、三处各自为政的「当前场景」规则）。雪花十步本身的契约见[雪花方法契约](snowflake-method-contract.md)，分章面板见其中第 6 行与阶段 V / W。

## 0. 真实项目上对出来的原因

在作者真实项目（17 场）的数据库副本上逐项复现，不是猜的：

| # | 现象 | 根因 |
|---|---|---|
| 1 | 章节编排的看板上只有手建的那一章，雪花的 5 章一张都不显示（指标却写着「章节 6」） | 物化把幕写成整数 `1 / 2 / 3`，看板按 `act === "act1"` 分卷 |
| 2 | 雪花的「第 1 章」显示成第 02 章，目录里两章同名；写作台默认开在一张空白的「开场」上 | 雪花做完之前点过一次 `创建第一章 · 开场`，那张一个字没写的占位章排在最前面；新章只能接在计划之外的章后面 |
| 3 | AI 起草台：「运行队列还是空的」 | 左栏是一份手工挑出来的队列，物化不会往里放任何东西 |
| 4 | 写作台的场景上下文：POV / 时间 / 地点全是「—」，反应场的三拍套着 Goal / Conflict / Setback 的标签 | 目录只把三拍交给台子；「场景定位」读的是**章**上的字段，雪花的章上没有 |
| 5 | 主页指着雪花的第一场，「进入写作房间」却打开另一场 | 「现在该写哪一场」三处三条规则（主页：章里第一场；概览接口：章里**最后**一场；写作台：全书任何一场标着「在写」的场） |
| 6 | 改完 09 / 10、点了「确认本步」，台子上还是旧三拍 | 物化之后构思和目录是两份数据，场景卡要等作者再去横幅上点一次「同步到目录」 |
| 7 | 目录里的「第 4 章」装着第 4、10–13 场 | 已保存的分章可以不是故事序上的连续切片（阶段 V 之前交错洗过的归属），面板原样摆出来、作者用「另起一章」继续切、`save` 照单全收 |
| 8 | 空白页上有一段「【章节目标】…【场景目标】…」，还算进了字数 | 旧作者台时代的做法：空白稿 = 把场景卡抄成脚手架塞进正文 |
| 9 | （潜伏）目录一动，正文可能存进别的场的草稿 | 场景 `slug` 是位置式的 `ch02s1`，而写作台的草稿绑定与读缓存、起草台的运行记录与队列、场景笔记全拿它当身份 |

## 1. 目录是唯一的交接面

`GET /api/v2/projects/{id}/catalog` 的载荷就是台子能知道的全部——台子不读雪花工作台。

**章**（新增键）：`origin`（`snowflake` / `manual`）、`summary`（构思里的章摘要）、`goal`（章目标）、`spine`（灾一 / 灾二 / 灾三）。`act` 永远是 `act1 / act2 / act3`：物化写字符串，读取时对旧数据归一（`catalog.normalize_act`），前端 store 再守一道——认不出的值落到第一幕，绝不让一章从看板上消失。

**场**（新增键）：

- `slug` = `scene_id`（稳定身份）；`legacy_slug` = 位置式旧 slug，只供前端迁移本机旧键、给旧深链兜底。
- `title` = 构思里起过的短题名；没起过就从摘要取一个短题（第一个分句，≤ 18 字）。`summary` = 09 的整句摘要。
- `design`：`origin`、`crucible`、`location`、`story_time`、`cast[]`、`reader_emotion`、`must_include`、`must_withhold`、`cost`、`length_band`、`rendering_mode`、`followup`（一场接着的另一组三拍）、`exception_reason`、`protagonist`、`is_chapter_last`、`owner`（`plan` | `desk`：这张卡的设计在哪里改，见 §10）。
- `work`：`run_status`（管线状态）、`has_final`、`has_words`——目录的 `state` 只是作者手打的标签，这才是事实。

读取按整本书批量取角色名与管线状态（`CatalogService.read_context`），不逐场查询。

## 2. 场景的身份跟着行走

场景 `slug` 过去是 `章slug + "s" + scene_seq`。任何一次结构变动——雪花重新分章 / 回流搬场、手动增删章、场景重排、占位章被移走——都会让同一个 slug 指向另一场；而前端按场景落地的本机状态全拿它当身份，于是正文存进别的场的草稿、A 场的 AI 稿被采用到 B 场。

现在 `slug = scene_id`。前端（`ws-catalog.jsx`）：

- `sid` = 后端给的 slug；`legacySid` = 位置式旧 slug。
- `WsCatalog.sceneById(x)`：直接命中 → 会话内别名（乐观创建时的临时 sid `tmp_…`，建好之后经 `catTrackAliases` 仍解析到同一场）→ `legacySid`（待办卡 / 旧深链里存下来的 `ch08s3`，语义与从前一样：「现在排在那个位置上的场」）。
- 一次性迁移（每部作品一次，标记 `ws_sid_migrated_v1::<work>`）：`wr-doc:` / `wr-doc-pending:` / `wr-notes:` / `wr-notes-pending:` / `scn-run:` 键与 `scn-queue:v1` / `scn-queue-dismissed:v1` 名单从旧 sid 挪到新 sid。映射取当刻的目录位置，与升级前「下次打开会读到的那一场」完全一致。
- 概览接口的 `resume.scene_slug` 同样是稳定 id；章内第几场另给 `resume.scene_no`。

章 slug 仍是位置式的 `chNN`：它只当显示序号和界面键用，章上的写操作都在调用当刻解析后端 id。后端的章 id 自阶段 Y 起钉在章计划上、不再有位置含义（§9）。

## 3. 「现在该写哪一场」只有一条规则

当前章（作者的书签 `current` → 第一章「在写」→ 末章）里：在写的那一场 → 第一场没写完的 → 末场。当前章一场都没有时（2026-09-21 起）：从当前章往后找第一场没写完的，找不到再从书头绕回来找；全书都写完时停在当前章之前最近的一场。实现在 `WsCatalog.focusScene()`（`writingScene()` 委托给它），后端镜像是 `catalog.focus_scene_payload`（概览接口用）——后端还没有跟上这条「空章往后找」的兜底，当前章没有场景时概览接口不返回续写位置，这是已知差异。主页的焦点卡、「进入写作房间」、写作台的落点、AI 起草台的落点、交付条的 `去写作台` 全走它。

**书签不被物化重置**：`approve_outline_plan` 只在书签为空、或指着的章已经不在目录里（占位章刚被移走、旧章重新分章后空了）时才把它放到这一版的第一章。

## 4. 空白占位章

`services/catalog_placeholders.py`（叶子模块）。物化在落位**之前**把满足全部条件的章移入回收站：目录 API 手建、章名还是系统起的「第 N 章」、不在这一版分章里、未终审、章上没填过任何叙事字段、回收站里没有它的卡、章下每张卡都是没动过的占位场（系统题名、三拍没填、零字、无笔记、管线没跑过、无定稿 / attempt / LLM 调用、作者稿是第 1 版且为空或只有系统脚手架）。章和卡用同一个时间戳进回收站（恢复时一起回来），`trashed_by = snowflake_placeholder`，事件 `snowflake_placeholder_chapter_trashed`，回包键 `trashed_placeholder_chapters`。分章面板用同一条判定提前给出 `catalog_placeholder_chapters` 提示；作者动过的手建章仍是 `catalog_hand_made_chapters`（原样保留，新章接在后面）。

## 5. 确认即同步

`POST …/steps/{step_key}/approve` 接受 `{"sync_catalog": true}`（工作台总是带；脚本 / 旧调用方不带，行为不变）。确认 09 / 10 之后，服务端在同一事务里把已物化、且落后于构思的场景卡回流（`_auto_sync_catalog` → `resync_materialized_scenes`，`actor_ref = auto_sync:<操作者>`），回包 `catalog_sync {synced_count, synced_scene_ids, held_count, held[], trashed_empty_chapters, notice?}`。运行时失效本来就在确认这一刻按改动范围标了——卡跟上才是一致的。

留给显式回流（差异预览）的两种卡（`held[].reason`）：

| reason | 含义 |
|---|---|
| `plan_not_confirmed` | 这一场的规划不是 `approved`（还在改，或被上游标了需复核） |
| `would_trash_written_scene` | 同步要把一张已经有活儿的卡送进回收站（略过 / 该重写 / 待删） |

阶段 X 还有第三种 `desk_edited`（作者在台子上改过这张雪花卡的设计，自动回流绕开它）。阶段 Y 起设计只有一处可改（§10），台面与构思不会再各说各话，这一条连同 `writer_brief_json.desk_edited_at` 记号一起取消；旧记号在下一次回流时被清掉。

要搬去的章目录里还没有的卡照常同步内容、只是不搬（与显式回流同一口径），回包的 `notice`（`CHAPTER_MOVE_NEEDS_MATERIALIZE`）会说清楚。

**没跑过的场不谈失效**：`ProjectRuntimeInvalidationService` 只把真的有运行时产物的场（执行契约 / 草稿 / QC / 定稿，或运行态已经离开 `ready`）打回 `needs_replan`，回包多一个 `invalidated_scene_ids`；从没进过管线的场保持 `ready`，作品状态也不因此变成 `chapter_blocked`。确认即同步让「改设计 → 确认」变成常态——过去这会让一场没写过的场变成起草台上的失败稿（`author_state = generation_failed`）、待办里的一张「这稿需要重新规划」，可根本没有稿。

台子用轻量读口 `GET …/snowflake-workspace/resync-status`（`pending_scenes[]` 带 `plan_status`；不是雪花法的作品答 `supported: false` 而不是 409——台子对每部作品都会问这一句）；前端 `WsDesignSync` 只认 `plan_status == approved` 的条目，设计卡上出「构思已更新 · 同步这一场」。`ws:catalog-changed` 连字数回写都会广播，事件触发的重拉节流 20 秒。

场景题名：物化 / 回流写 `title` + `seeded_title`。之后题名还等于 `seeded_title` = 作者没改过，跟构思走；作者在台子上改过的名字跨回流、跨重新物化保留。`seeded_title` 键在的卡（阶段 X 之后播的）题名才参与「待同步」比较，升级当刻不会全书集体报待同步。同一处修掉了一个旧缺陷：物化只写主形态那三个键，一场接着的另一组三拍要等一次回流才进得了场景卡。

## 6. 章是故事序上连续的一段——由服务端守住

- `heal_assignment`：保住章序的最长不降子序列，离群的场并入故事序上前一场的章。只拖了一场就只有这一场换章（旧的「章序只许不降」拉平会把后面整本书拽进它原来的那一章）。
- `preview(auto)`：已保存的分章不连续 → 直接给 `from_scenes`，带 `chapter_order_healed` 提示；`keep_current` 摆出来的永远是并好之后的那一版（= 会落库的那一版）。
- `save`：落库前过一遍 `heal_assignment`，被并回的场记在 `snowflake_chapter_plan_saved` 的 `healed_scene_plan_ids`。

## 7. 三张台子

- **共用设计卡** `ws-scene-design.jsx`（`sceneDesignModel` 纯函数 + `SceneDesignCard`）：写作台正文上方（compact）、写作台 `上下文 · 戏剧`、AI 起草台预检，渲染的是同一个模型。三拍按形态命名；系统占位（「（本场目标待规划）」）按没填处理；雪花的场给 `在构思里改`（`ws:snow-step` + `ws:snow-scene`），手建的场给 `编辑卡`。
- **AI 起草台** `ws-scene.jsx`：左栏 = 全书书脊（`SceneSpine`）。工作项分在办（持久化）与 transient（只是点开看看，不落盘，点别的场就收走）；`整章入列`、入列意图、开始起草、后端恢复的管线状态会把一场变成在办。在办的行沿用原队列的测试标识与移出 / 多选契约，其余的行是 `scene-spine-item`。预检清单只列能从卡上读出来的事实。`ScenePicker` 与「加入场景」已删除。
- **写作台** `ws-writer.jsx`：落点走 `focusScene`；大纲的章状态用与后端同一份词表的中文、脊柱标记、落点所在章自动展开、场题名一行放完（整句摘要在提示里）；`下一场` 提示读目录里真的下一场；空白页的占位句不计入字数。
- **章节编排** `ws-author.jsx`（场景行在 `ws-author-detail.jsx`，判定用 `ws-author-derive.js` 的 `arrIsPlanScene`）：雪花整理出来的场（`design.owner == plan`）三拍 / POV 只读、不能拖、不能切形态，行上有 `在构思里改`（§10）；幕归一之后雪花的章回到看板上；章卡在没有「章承诺」时显示构思里的章摘要，章级 POV 空着时列出各场的 POV，脊柱标记在状态旁；（2026-09-21 起不再有单独的「全书进度」统计块：字数与「已完 N / M 场」并进了结构镜头的汇总条。）阶段 Z 起它是构思分章的第二扇门，见 §13。
- **构思** `ws-snow.jsx`：确认写入之后是一条常驻的交付条（几章几场、顺手移走了什么、`去写作台` / `去 AI 起草台` / `章节编排`）；确认 09 / 10 后的同步结果给一句回执（`ws:snow-catalog-synced`）。
- **空白稿就是空白**：`author_drafts._blank_source_for_target` 不再把场景卡抄成脚手架。

## 8. 测试

后端 `tests/test_catalog_book_spine.py`；前端 `ws-book-spine.test.jsx`（目录 store / 设计卡 / 同步状态）、`ws-scene-spine.test.jsx`、`ws-writer-spine.test.jsx`、`ws-snow-sync.test.jsx` 的阶段 X 用例。改动过契约的旧用例：`test_catalog_api.py`、`test_catalog_single_source.py`、`test_project_overview_v2.py`（slug）、`test_author_drafts.py`（空白稿）、`test_snowflake_chaptering.py`（幕）、`test_snowflake_chaptering_story_order.py`（手建章、拖场后的修复）、`scripts/smoke-phase3.mjs`（sid）。

阶段 Y：`tests/test_snowflake_stable_chapter_ids.py`（身份沿用、铸号、章序落位、手加场跟随）、`tests/test_scene_rehome.py`（运行时行跟着场换章 + 「每张带 scene_id / chapter_id 的表都做过选择」守卫 + 空旧章清得出回收站）、`tests/test_migration_0089_chapter_plan_catalog_chapter_id.py`、`test_catalog_book_spine.py` 的设计归属用例、`test_snowflake_chaptering_followups.py` 的回收 / 取回用例；前端 `ws-author.test.jsx`（只读行）、`ws-writer-spine.test.jsx`（大纲不可拖）、`ws-scene-spine.test.jsx`（决策条）。

## 9. 章的身份（阶段 Y，2026-09-20）

**过去**：物化目标章号 = `{project}_CH{章序:02d}`。章序一变（拆章、并章、换一个每章场数），后面每一章的号都平移：`CH02` 变成另一组场，章名 / 「当前章」书签 / 章状态 / 终审挂在「位置」上而不是挂在那一章上；场景卡成批跨章搬动，而它们的草稿 / QC / 定稿 / 正史行上冗余的 `chapter_id` 还指着旧章——起草上下文查不到自己写过的稿，空了的旧章进回收站之后清空回收站会撞外键（500）。

**现在**：

- `SnowflakeChapterPlan.catalog_chapter_id`（迁移 `20260920_0089`）：这一章在目录里的 id，铸一次、钉住。号是**序列号**（`{project}_CH{serial:02d}`，第一次物化仍然是 CH01…CHnn），不表示顺序；下一个号 = 目录里现有的章（含回收站）与所有钉过的号（含已软删的章计划）里最大的序列号 + 1——回收站里的号随时可能被作者取回，所以不复用。`SnowflakeChapteringService.catalog_chapter_id(chapter, mint=)` 是唯一的铸号口：`save` 按章表顺序铸，物化兜底铸，预览 `mint=False`（没落库的提议章 `chapter_id` 为空）。迁移只给「全部场都指着同一个目录里存在的章、且没有第二个章计划来争」的章计划回填；从场景行章戳反推章表时，只钉目录里真有的章或本作品序列号形状的章戳（`_is_pinnable_chapter_id`）。
- **还是不是同一章**（`match_chunks_to_chapters`，`from_scenes` 预览与 `propose_from_scenes` 共用）：重新按场景提议的一段与已有的一章，公共场不少于两边各自的一半 = 同一章（沿用 `row_uid` → 同一行目录章）。恰好对半时归故事序上靠前的那一个——和面板手势同一个口径：`从这里另起一章` 是前半截留着原章，`并入上一章` 是上一章留着。沿故事序逐段认领，一段取公共场最多的候选，一章只被认领一次。这一条没说话的时候还有一条兜底——**整拆 / 整并**：一章被整个拆成几小段、或几章整个并成一段，谁都不过半，身份归**开头对得上**的那一个（这一段的第一场就是那一章的第一场，并且一方整个包在另一方里）。否则一章 7 场拆成 2 / 2 / 3，原来那一行会整个进回收站，作者起的章名和戏剧卡跟着不见了（真实项目的库副本上验证过：每章 2 场重切 17 场，拆细的两章各留在开头那一段，没有一章进回收站）。作者起的章名 / 章目标留着，从场上抄来的章摘要按新的末场重算。
- **目录章序 = 这一版章表的顺序**（`_CatalogPlacement.settle_chapter_order`，批准计划的最后一步）：计划内的章按章表排；计划之外的章（手建的、里面还留着东西的旧章）原来跟在哪一章后面还跟在哪一章后面，原来在最前面的还在最前面；已终审的章不挪（与 `CatalogService.reorder_chapters` 同一条规矩：相对顺序与目录位置都锁着）——算出来的顺序会挪动终审章时整个不排，新章接在最后，回包 `chapter_order_held: true`，回执提醒作者重新打开后再整理一次。
- **手加的场跟着锚点场走**：原来紧跟在哪张计划内的卡后面，现在还紧跟在它后面，哪怕那张卡换了章；排在本章第一张计划内的卡之前的，跟着那张卡、排在它前面；整章一张计划内的活跃卡都没有时原地不动。这是「确认写入」（物化）这条路的规则；回流（`resync`，只在已有的章之间搬卡）仍只在章内重排手加的场，不带它们换章。
- **场换章时运行时行跟着走**（`services/scene_rehome.py::rehome_scenes`，物化与回流两条搬卡路径都调）：`REHOMED_MODELS` 里每张表按 `scene_id` 改 `chapter_id`，场景级 `ContinuitySnapshot` 同样；这一场有正史时两头的章级快照按新成员重建，场全搬走的空章删掉它的章级快照（留着会挡住清空回收站）。刻意不动的表写在 `NOT_REHOMED_TABLES`（记账流水 `llm_calls`、章级任务 `chapter_run_jobs`……）；守卫测试要求每张同时带 `scene_id` 与 `chapter_id` 的表在两个清单里选一个。
- 空章回收 / 取回（阶段 W）照旧，判定改看来源（`writer_brief_json.source`）而不是 id 的形状。**章**的取回只在「章计划钉着的那一行此刻躺在回收站里」时发生：一章留在章表里、场被挪空后又挪回来，或作者手动删过那一章。随章进回收站的**场景卡**另有一条规则，不看它落进哪一章（§11）。
- 面板提示 `catalog_leftover_chapters_kept` 的口径跟着变：留得下来的只有回收站里的卡，和整章一张计划内的活跃卡都没有的章里的卡。

## 10. 设计只有一处可改（阶段 Y）

阶段 X 的做法：台子上照样能改雪花场景卡的三拍 / 形态 / POV，改了打一个 `desk_edited` 记号，自动回流绕开它、等作者去看差异。构思和目录从此各说各话。把台面的改动**写回**构思，要在 09 / 10 的步骤稿和前端雪花缓存之间再开一条服务端写通道——那条缓存的合并规则是「本机为准」，正是出过整步抹空事故的地方。所以反过来（`services/scene_design_ownership.py`，叶子模块）：

| | `owner = plan` | `owner = desk` |
|---|---|---|
| 哪些卡 | 雪花物化 / 回流出来的卡，**并且**构思里那一行还在 | 手加的场；构思里那一行已经删掉、因为写过字被作者留下的雪花场 |
| 形态 / 两组三拍 / POV / 离场变化 / 钩子 | 只在构思第 10 步改。`PATCH …/catalog/scenes/{id}` 真的改了其中一项 → 409 `CATALOG_SCENE_DESIGN_OWNED_BY_PLAN`（`details.fields` + 直达那一场的 `author_action`）；值没变的整卡回写照常通过 | 在章节编排里改 |
| 彼此的先后 | = 构思第 9 步的行序。`POST /api/v1/chapters/{id}/scene-order` 改变了这些卡的相对顺序 → 409 `CATALOG_SCENE_ORDER_OWNED_BY_PLAN`；手加的场可以挪到任何两场之间 | 可拖 |
| 章节规划 AI | `sanitize_plan_patch(plan_owned_scene_ids=…)` 丢弃设计槽（`design_owned_by_plan`），提示词载荷里每张卡带 `design_owner`（`chapter_scene_plan_fill` v4 / `chapter_plan_review` v3）；离线缺口清单注明「在构思第 10 步补」 | 只填空 |
| 题名、状态、字数、删除、交给 AI / 自己写 | 照常 | 照常 |

预检的 `SCENE_STRUCTURE_INCOMPLETE` 按归属指路。前端：章节编排的行只读 + `在构思里改`，抓手不可拖、形态不可切；写作台大纲不可拖；设计卡与起草台决策条按 `owner` 给 `在构思里改` / `编辑卡`。

## 11. 删了旧章再回来重新分章（2026-09-20）

真实故障：作者嫌目录里的章乱，先在章节编排里把旧章删了，再回构思里「整理为章节结构」并确认写入——目录里 4 章只看得见 1 场，另外 16 场「不见了」。删章会把章内的场景卡一起送进回收站（`AuthorLifecycleService.trash_chapters`：卡盖上和章**相同的** `trashed_at`）；阶段 Y 之后重新分章铸的是新章号，而「随章一起删的卡跟着回来」只发生在目标章恰好就是被取回的那一章时。于是卡被搬进了新章，自己却还躺在回收站里。

现在（`services/catalog_trash_cascade.py`，叶子模块，物化与分章面板共用）：

- **删章是整理结构，不是对场景的裁定。** 这一版章表里有这一场、它的卡是随某一章级联进回收站的（`trashed_at` 与本作品回收站里某一章的一模一样）→ `approve_outline_plan` 在落位**之前**取回（`revive_cascade_trashed_scene_cards`），不管它这一版落进哪一章；回包 `restored_scene_ids`，事件 `snowflake_scene_cards_revived`，确认回执「N 场随旧章进了回收站的场景卡已取回」。必须在 `_CatalogPlacement` 之前：落位只认活跃的卡。取回时原来的章内序号还空着就原样取回，被别的活跃卡占了就先停到高位（`(chapter_id, scene_seq)` 在活跃卡上唯一），最终序号由随后的落位统一重写。
- **作者单独删掉的场景卡是对那一场的裁定**（成稿中心「标待删」、章节编排里删一场：时间戳对不上任何一章）→ 确认写入不替作者取回。
- 分章面板事前说清楚：`catalog_trashed_scenes_return`（会取回的，带 `scene_ids`）/ `catalog_trashed_scenes_kept`（不会取回的：要写就到回收站恢复，不要这一场就在第 10 步裁定为「待删」）。略过（`skip`）与该重写 / 待删的场本来就不物化，不在这两条提示里。
- 已经被修复之前的确认写入搬进了活跃的章、自己还在回收站里的卡：旧章还在回收站里（时间戳还对得上）时，再确认一次就回来。

测试：`tests/test_snowflake_trashed_chapter_revival.py`（三条都在没有这条规则时变红）。

## 12. 起草的前提（2026-09-20）

真实故障：AI 起草台的任务总是「已阻断」。预检（`scene_run_preflight`）与 bundle 构建（`bundle_builder`，409 `BUNDLE_SOURCE_MISSING`）都把「POV 声线卡（`VOICE_<pov>`）」与「同场两人的关系卡（`REL_<a>_<b>`）」当作起草的硬前提——这两类卡是 2026-09 减法删掉的知识卡体系留下的，产品里已经没有任何地方能写，唯一的来路是预检自己铸一句占位套话（`POST …/preflight/create-cards`，内容还带着裸角色 id）再当事实喂给起草模型。于是每一部真实作品的每一场都在这里被拦下；起草台上那颗「补齐声线卡并重试」按钮又被终态任务恢复的 effect 盖掉，作者只看得到「任务已阻断…请检查阻断原因后重试」。

现在：

- 缺声线 / 关系卡**不拦起草**：预检不再有 `VOICE_PROFILE_MISSING` / `RELATION_PROFILE_MISSING`，载荷里不再有 `missing_dependencies` / `create_actions`；bundle 缺卡时只是没有 `POV Voice` / `Relation Digest` 这两节。库里真有这两类卡（测试夹具、旧数据）时照旧注入并记出处。角色的声音与关系来自构思：`Scene Design Context` 带着 POV 角色摘要、价值观、视角故事与同场角色一句话，角色身份契约（`character_contract`）的名字取自 `StoryCharacter`。
- 铸占位卡的整条支路退役：`POST /api/v1/scenes/{id}/preflight/create-cards`、`create_missing_cards`、前端 `scnCreateCards` / 「补齐声线卡并重试」。预检仍然拦的只有真正要作者先处理的事：执行契约缺字段 / 过期（`SCENE_EXECUTION_CONTRACT_BLOCKED` / `_STALE`）与场景卡自相矛盾（`SCENE_CONSTRAINT_CONFLICT`）。
- 起草台对终态任务只说一句话，而且是任务自己留下的原因：`scnTerminalJobMessage(job)`（`ws-scene-run.jsx`）同时供 `startRun` 的 catch 与「终态任务恢复」effect 使用——过去后者用一句笼统的话盖掉前者带着原因与出口的那句。修复之前留下的、带着 `VOICE_PROFILE_MISSING` 的旧任务行会如实显示「这项检查已经取消，直接重新起草即可」。任务控制条上的 `preflight_blocked` / `queued` / `blocked` / `cancelled` 有了作者可读的标签。

测试：`test_scene_workbench_preflight.py`（缺卡不拦、有卡照旧注入）、`test_scene_run_jobs.py`、`test_fe_scene_run_guards.py`、`test_orchestrator_flow.py`（缺卡照常跑完）、`ws-scene-run.test.jsx` 的「预检阻断」三条（都在旧行为下变红）。

**同一天查实的下一堵墙：防抄袭政策句被当成禁用词表。** 物化（`snowflake_workspace`、`projects.approve_outline_plan` 的兜底、v1 `snowflake_planner`）给每张场景卡的 `forbidden_text` 写「不得复制参考书原文表达、人物、设定或桥段。」——可这个字段的契约是「按字面查的禁用词，顿号 / 逗号分隔，`A|B` 为等价写法」（`qc_constraints.contains_forbidden_term`，硬质检 / 质量分级 `forbidden_text` = 已证实 Q1 / 终稿闸门 `continuity:forbidden_text` 共用）。这句话于是被拆成三个「禁用词」，其中一个是 **`人物`**：正文里出现「这号人物」「可疑人物」就是一条已证实的硬伤，归档被拦（真实作品的 17 张卡全部带着这句话）。和阶段 F 修过的 `must_include_text`（摘要冒充「必须包含」）是同一类毛病。现在：物化不再写这句话（计划没给禁用词就留空；重新物化顺手把旧卡上的清掉）；`qc_constraints.forbidden_terms` / `strip_reference_policy` 是读这个字段的唯一口径——字面检查、预检的约束冲突、分级证据、执行契约的 `must_withhold` 兜底、场景卡摘要里的 `Forbidden text:` 行都先剔掉政策句，作者真写的禁用词（含接在那句话后面的）照常生效。防抄袭本来就不靠这个字段：参考书 n-gram 查重（`style_plagiarism`，Q0）、受保护专名、风格注入里的红线段。测试：`test_qc_constraints.py`、`test_quality_classifier.py`、`test_scene_adopt_archive.py` 的 policy 用例（都在旧读法下变红）。

还没做：`VoiceProfile` / `RelationProfile` 两张表与 bundle 里这两个可选槽位本身还在（测试夹具在用），是下一批减法的候选；章级的 `must_not` / `emotional_target` / `ending_effect` 仍是物化写下的套话（只进提示词，不做字面检查）。

## 13. 一张章表、两扇门（阶段 Z，2026-09-20）

作者的原话：「章节编排功能和雪花的场景以及整理为章节结构有点孤立。」真实项目（4 章 17 场）上对出来的：

| # | 现象 | 根因 |
|---|---|---|
| 1 | 章节编排叫「编排」，却编排不了雪花的书：拆章 / 并章 / 挪章界 / 起章名只在构思里一张弹层上，这里连一扇门都没有 | 分章面板只挂在构思页头和 07 上；章节编排只有一颗「从雪花同步」（同步场景卡，和章无关），还摆在戏剧卡的标题栏里 |
| 2 | 在章节编排里给雪花的章改名，分章面板和 09 的章头还是旧名；「AI 起章名」会给起过名的章再起一遍。反过来 07 里改的章名要等下一次「确认写入」才到目录 | 章名有两份：章计划行（07 章节表 / 分章面板 / 09 章头的来源）和目录的 `narrative_json.title`，三扇门各改各的 |
| 3 | 雪花的章在看板上能拖去别的位置、别的卷 | 目录的章序 / 幕是台面可写的；而章是故事序上连续的一段，下一次确认写入 `settle_chapter_order` 再悄悄排回去 |
| 4 | 章节详情是一张空表：入口 / 出口、视角 · 时空全空，体检报「与上一章出口对齐 · 已对齐」「线索待交接 0 项」；构思里写好的章摘要、第几幕、哪个灾难、装着第几到第几场，一样都看不见 | 章节编排有一套自己的章级模型（张力 / 章级 POV / 时间 / 地点 / 入口出口 / 线索 / 对齐），**产品里没有任何地方能填**，也没有任何东西喂它——原型时代演示数据的遗留 |
| 5 | 全书编排的第一张图是一条 0.3 的平线（「故事弧线」），体检报「张力曲线健康」、一条「字数超额 · 01 第 1 章 · 1,803/0」、「视角分布： 4 章」 | 同上：张力永远是默认值；字数目标为 0 时任何字数都算超额；章级 POV 是空串 |
| 6 | 构思里说「第 6–12 场」，到了章节编排只剩 01…07 | 目录不知道一场在故事序上是第几场 |

**原则**：和阶段 Y 同一个——一处可改，并且在作者看着它的地方够得着。

### 13.1 结构只有一个编辑器，两扇门

- 分章面板 `WsChapterPlanPanel` 就是章结构（哪几场归哪一章、章的先后、幕、章名 / 章摘要）的编辑器。章节编排直接开得出来：全书编排页头 `整理章节结构`（`author-open-plan`；构思的闸门通过、**或**目录里已经有构思分出来的章时出现——某一步被改动、待重新确认时门不能跟着消失，面板自己会列出没过的那几项并带作者去补）、章节详情的构思条与右栏、全书体检的「还没起名的章」、空目录时的 `整理章节结构`（`author-empty-open-plan`）。同一个组件、同一条落库路径（`SnowSync.materialize`），确认后目录整份重拉，台面给一句回执。
- 反方向的门：09 场景列表的章头是一颗按钮（`ws:snow-chapter-plan` → 开面板）；面板里每一场有 `在构思里改这一场`（`onGoToScene`：先 `ws:snow-step planning` 再 `ws:snow-scene`，与成稿中心 / 章节编排回跳同一组意图）。
- **章结构的归属**（`services/chapter_structure_ownership.py`，叶子模块）：目录章来自雪花**并且**有一行没被软删的章计划钉着它（`catalog_chapter_id`）→ `structure.owner = plan`。这样的章：
  - 彼此的先后 → `POST …/catalog/chapter-order` 改变它们的相对顺序 → 409 `CATALOG_CHAPTER_ORDER_OWNED_BY_PLAN`；手建的章可以挪到任何两章之间；
  - 幕 → `PATCH …/catalog/chapters/{id}` 真的改了 `act` → 409 `CATALOG_CHAPTER_STRUCTURE_OWNED_BY_PLAN`（值没变的整卡回写照常通过）；
  - 前端：章卡与章节序列的抓手是个固定标记、`draggable=false`，提示指向页头的门；
  - 戏剧卡 / 章节蓝图 / 字数目标 / 删除 / 新建手建章照常归台面。删除雪花的章之前，确认框说清楚：删的只是目录里的章和场景卡，构思的分章还在，下一次确认写入会取回（§11）；要并章 / 拆章用面板，真不要这几场去 09 删行。
  - 章计划已经不在的雪花旧章（里面还留着东西、被留下的）回到 `desk`。

### 13.2 章名只有一个（`services/chapter_title_sync.py`，叶子模块）

三扇门改的是同一个名字：

| 门 | 怎么到另外两处 |
|---|---|
| 章节编排 | 目录 PATCH 的同一事务里 `adopt_catalog_title`：章计划行改名 → 绑在上面的场重盖章名戳（09 的章头读它）→ 07 草稿的 `chapters` 与 `fe_scaffold.chapters` 镜像 → 目录那一行的种子 `writer_brief_json.chapter_title` 也跟上。回包 `plan_title_synced: true`；前端 `WsCatalog` 据此调 `SnowSync.adoptServerChapters()`（先排空未上行的编辑）——本机雪花缓存的合并规则是「本机为准」，不接的话下一次 07 上行会把旧章名当成作者的编辑同步回去 |
| 07 章节表 | `_sync_chapter_plans` 末尾 `follow_plan_titles`：场的章名戳跟上；目录里那一章**还是上次由章表播下去的名字**时跟着改（和重新物化同一条规矩——阶段 Z 之前在台子上另起过名、没写穿的旧数据不被盖掉） |
| 分章面板 | `save` + 物化（阶段 W「目录章名跟随章表」），不变 |

种子 = 「上一次由章表播下去的名字」；两边一致时它等于当前章名，之后任何一扇门再改都还跟得上。把章名清空：雪花的章落回 `第 N 章`（不是「未命名章节」），`is_auto_chapter_title` 也把「未命名」认作占位——「AI 起章名」仍然会给它起名。

### 13.3 构思透到台面上：目录载荷多两样，章级事实从场上读

- 章 `structure {owner, row_uid, scene_range {first, last}, planned_scene_count, title_auto}`；场 `design.story_index`（故事序场次，1 起；不在构思里的场为 0）。编号与分章面板的 `story_index`、09 场景列表同一套（`chapter_structure_ownership.story_scene_numbers`，整本目录一次算完）。
- `ws-author-derive.js`（纯函数，不写 window）：一章的入口 = 第一场在做什么，出口 = 最后一场的离场变化；视角 · 时空 = 各场的 POV（按场次计数）/ 首尾两场的故事时间 / 出现最多的地点。章级字段作者真的填过（旧数据）就用作者的；派生出来的标「取自首尾两场 / 取自本章各场」。
- 章节详情：**构思条**（第几卷 · 哪个灾难 · 第几到第几场 · 还没起名 · 章摘要 / 章目标 + `整理章节结构` / `在构思里看这几场 ↗` / 有待同步时 `同步 N 场改动`）；交接条用派生的入口 / 出口（长句收三行，整句在提示里）；章节体检换成读得出来的事实——`三拍已规划 n/N`、`场景动笔`、`戏剧卡（可选）`、`字数预算`（没设目标 = `未设目标`，不再是「未开始」的警告）、`与构思同步`；「与上一章出口对齐」「线索待交接」两个假的对勾取消（线索块只在真有线索时出现）。
- 全书编排：默认镜头是**结构**（`ws-author-spine.jsx`：卷 → 章 → 场，场上的数字 = 构思里的场次，主动方 / 反应圆，已完实心，灾难场带金圈，手加的场虚线；点章进详情、点场落在那一场上）；`节奏镜头`的 POV 着色与泳道取自各场，没设目标的章不画目标虚影；`故事弧线` / `线索织布机`只在章上真的有张力 / 线索数据时才出现。全书体检：没设目标不报超额，没设过张力不报「张力曲线健康」，视角分布按场统计；雪花的书多三项带门的待办——构思的改动待同步（`同步到目录`）、还没起名的章（`整理章节结构`）、三拍还没规划的场——都清了才说「与构思一致」。

### 13.4 测试

后端 `tests/test_chapter_structure_ownership.py`（载荷、改名写穿与回流、07 改名、先后 / 幕的 409、面板照常能改章序）；`tests/test_service_architecture.py` 守着两个新叶子模块不闭环（`catalog` → `snowflake_chaptering` 会经 `trash` 闭环，所以写穿逻辑在叶子里）。前端 `ws-author-derive.test.js`（派生层 + 体检）、`ws-author.test.jsx` 的阶段 Z 六条、`ws-catalog.test.jsx`（结构归属映射、改名后接章表）、`ws-snow.test.jsx`（09 章头开面板）。真实项目的库副本上走过一遍浏览器旅程：章节编排改名 → 后端面板 / 本机 07 章节表 / 09 章头同名，之后没有任何上行把旧名推回去。

## 14. 没做的

- 章级的张力 / POV / 时间 / 地点 / 入口出口 / 线索 / 对齐这组字段（`narrative_json`、目录 API、`import_catalog`、章节规划上下文的 `_chapter_card_slot`）还在——现在只是没有数据时不再冒充事实；它们没有编辑入口，是下一批减法的候选；读它们的界面（`ArrTensionCurve` / `ws-author-loom.jsx`、故事弧线 / 线索织布机镜头）已在 2026-09-21 删除，只剩存储与接口字段。
- 章摘要 / 章目标在章节编排里只读（在分章面板 / 07 里改）。
- 新浏览器打开构思时 09 / 10 会各回声式 PATCH 两次（语义上是空操作，步骤状态不变）——与本阶段无关，未处理。
- 章 slug（`chNN`）与 `#writer` 深链里的章序号仍是位置式的显示序号。
- 章计划与目录章之间没有数据库级的唯一约束（一个目录章至多被一个活跃章计划钉住，由铸号与迁移回填的规则保证）。
