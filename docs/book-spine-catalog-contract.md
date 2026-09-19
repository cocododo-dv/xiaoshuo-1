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
- `design`：`origin`、`crucible`、`location`、`story_time`、`cast[]`、`reader_emotion`、`must_include`、`must_withhold`、`cost`、`length_band`、`rendering_mode`、`followup`（一场接着的另一组三拍）、`exception_reason`、`protagonist`、`is_chapter_last`、`desk_edited`。
- `work`：`run_status`（管线状态）、`has_final`、`has_words`——目录的 `state` 只是作者手打的标签，这才是事实。

读取按整本书批量取角色名与管线状态（`CatalogService.read_context`），不逐场查询。

## 2. 场景的身份跟着行走

场景 `slug` 过去是 `章slug + "s" + scene_seq`。任何一次结构变动——雪花重新分章 / 回流搬场、手动增删章、场景重排、占位章被移走——都会让同一个 slug 指向另一场；而前端按场景落地的本机状态全拿它当身份，于是正文存进别的场的草稿、A 场的 AI 稿被采用到 B 场。

现在 `slug = scene_id`。前端（`ws-catalog.jsx`）：

- `sid` = 后端给的 slug；`legacySid` = 位置式旧 slug。
- `WsCatalog.sceneById(x)`：直接命中 → 会话内别名（乐观创建时的临时 sid `tmp_…`，建好之后经 `catTrackAliases` 仍解析到同一场）→ `legacySid`（待办卡 / 旧深链里存下来的 `ch08s3`，语义与从前一样：「现在排在那个位置上的场」）。
- 一次性迁移（每部作品一次，标记 `ws_sid_migrated_v1::<work>`）：`wr-doc:` / `wr-doc-pending:` / `wr-notes:` / `wr-notes-pending:` / `scn-run:` 键与 `scn-queue:v1` / `scn-queue-dismissed:v1` 名单从旧 sid 挪到新 sid。映射取当刻的目录位置，与升级前「下次打开会读到的那一场」完全一致。
- 概览接口的 `resume.scene_slug` 同样是稳定 id；章内第几场另给 `resume.scene_no`。

章 slug 仍是位置式的 `chNN`：它只当显示序号和界面键用，章上的写操作都在调用当刻解析后端 id。

## 3. 「现在该写哪一场」只有一条规则

当前章（作者的书签 `current` → 第一章「在写」→ 末章）里：在写的那一场 → 第一场没写完的 → 末场。实现在 `WsCatalog.focusScene()`（`writingScene()` 委托给它），后端镜像是 `catalog.focus_scene_payload`（概览接口用）。主页的焦点卡、「进入写作房间」、写作台的落点、AI 起草台的落点、交付条的 `去写作台` 全走它。

**书签不被物化重置**：`approve_outline_plan` 只在书签为空、或指着的章已经不在目录里（占位章刚被移走、旧章重新分章后空了）时才把它放到这一版的第一章。

## 4. 空白占位章

`services/catalog_placeholders.py`（叶子模块）。物化在落位**之前**把满足全部条件的章移入回收站：目录 API 手建、章名还是系统起的「第 N 章」、不在这一版分章里、未终审、章上没填过任何叙事字段、回收站里没有它的卡、章下每张卡都是没动过的占位场（系统题名、三拍没填、零字、无笔记、管线没跑过、无定稿 / attempt / LLM 调用、作者稿是第 1 版且为空或只有系统脚手架）。章和卡用同一个时间戳进回收站（恢复时一起回来），`trashed_by = snowflake_placeholder`，事件 `snowflake_placeholder_chapter_trashed`，回包键 `trashed_placeholder_chapters`。分章面板用同一条判定提前给出 `catalog_placeholder_chapters` 提示；作者动过的手建章仍是 `catalog_hand_made_chapters`（原样保留，新章接在后面）。

## 5. 确认即同步

`POST …/steps/{step_key}/approve` 接受 `{"sync_catalog": true}`（工作台总是带；脚本 / 旧调用方不带，行为不变）。确认 09 / 10 之后，服务端在同一事务里把已物化、且落后于构思的场景卡回流（`_auto_sync_catalog` → `resync_materialized_scenes`，`actor_ref = auto_sync:<操作者>`），回包 `catalog_sync {synced_count, synced_scene_ids, held_count, held[], trashed_empty_chapters, notice?}`。运行时失效本来就在确认这一刻按改动范围标了——卡跟上才是一致的。

留给显式回流（差异预览）的三种卡（`held[].reason`）：

| reason | 含义 |
|---|---|
| `plan_not_confirmed` | 这一场的规划不是 `approved`（还在改，或被上游标了需复核） |
| `desk_edited` | 作者在台子上改过这张雪花卡的设计：三拍 / 形态 / POV / 钩子 / 离场变化（`writer_brief_json.desk_edited_at`，由 `catalog.update_scene` 打上，回流 / 重新物化清掉；改状态、改题名不算——题名走 `seeded_title`） |
| `would_trash_written_scene` | 同步要把一张已经有活儿的卡送进回收站（略过 / 该重写 / 待删） |

要搬去的章目录里还没有的卡照常同步内容、只是不搬（与显式回流同一口径），回包的 `notice`（`CHAPTER_MOVE_NEEDS_MATERIALIZE`）会说清楚。

**没跑过的场不谈失效**：`ProjectRuntimeInvalidationService` 只把真的有运行时产物的场（执行契约 / 草稿 / QC / 定稿，或运行态已经离开 `ready`）打回 `needs_replan`，回包多一个 `invalidated_scene_ids`；从没进过管线的场保持 `ready`，作品状态也不因此变成 `chapter_blocked`。确认即同步让「改设计 → 确认」变成常态——过去这会让一场没写过的场变成起草台上的失败稿（`author_state = generation_failed`）、待办里的一张「这稿需要重新规划」，可根本没有稿。

台子用轻量读口 `GET …/snowflake-workspace/resync-status`（`pending_scenes[]` 带 `plan_status` 与 `desk_edited`；不是雪花法的作品答 `supported: false` 而不是 409——台子对每部作品都会问这一句）；前端 `WsDesignSync` 只认 `plan_status == approved` 的条目，设计卡上出「构思已更新 · 同步这一场」。`ws:catalog-changed` 连字数回写都会广播，事件触发的重拉节流 20 秒。

场景题名：物化 / 回流写 `title` + `seeded_title`。之后题名还等于 `seeded_title` = 作者没改过，跟构思走；作者在台子上改过的名字跨回流、跨重新物化保留。`seeded_title` 键在的卡（阶段 X 之后播的）题名才参与「待同步」比较，升级当刻不会全书集体报待同步。同一处修掉了一个旧缺陷：物化只写主形态那三个键，一场接着的另一组三拍要等一次回流才进得了场景卡。

## 6. 章是故事序上连续的一段——由服务端守住

- `heal_assignment`：保住章序的最长不降子序列，离群的场并入故事序上前一场的章。只拖了一场就只有这一场换章（旧的「章序只许不降」拉平会把后面整本书拽进它原来的那一章）。
- `preview(auto)`：已保存的分章不连续 → 直接给 `from_scenes`，带 `chapter_order_healed` 提示；`keep_current` 摆出来的永远是并好之后的那一版（= 会落库的那一版）。
- `save`：落库前过一遍 `heal_assignment`，被并回的场记在 `snowflake_chapter_plan_saved` 的 `healed_scene_plan_ids`。

## 7. 三张台子

- **共用设计卡** `ws-scene-design.jsx`（`sceneDesignModel` 纯函数 + `SceneDesignCard`）：写作台正文上方（compact）、写作台 `上下文 · 戏剧`、AI 起草台预检，渲染的是同一个模型。三拍按形态命名；系统占位（「（本场目标待规划）」）按没填处理；雪花的场给 `在构思里改`（`ws:snow-step` + `ws:snow-scene`），手建的场给 `编辑卡`。
- **AI 起草台** `ws-scene.jsx`：左栏 = 全书书脊（`SceneSpine`）。工作项分在办（持久化）与 transient（只是点开看看，不落盘，点别的场就收走）；`整章入列`、入列意图、开始起草、后端恢复的管线状态会把一场变成在办。在办的行沿用原队列的测试标识与移出 / 多选契约，其余的行是 `scene-spine-item`。预检清单只列能从卡上读出来的事实。`ScenePicker` 与「加入场景」已删除。
- **写作台** `ws-writer.jsx`：落点走 `focusScene`；大纲的章状态用与后端同一份词表的中文、脊柱标记、落点所在章自动展开、场题名一行放完（整句摘要在提示里）；`下一场` 提示读目录里真的下一场；空白页的占位句不计入字数。
- **章节编排** `ws-author.jsx`：幕归一之后雪花的章回到看板上；章卡在没有「章承诺」时显示构思里的章摘要，章级 POV 空着时列出各场的 POV，脊柱标记在状态旁；全书进度在没有字数目标时是 0% 而不是 NaN%。
- **构思** `ws-snow.jsx`：确认写入之后是一条常驻的交付条（几章几场、顺手移走了什么、`去写作台` / `去 AI 起草台` / `章节编排`）；确认 09 / 10 后的同步结果给一句回执（`ws:snow-catalog-synced`）。
- **空白稿就是空白**：`author_drafts._blank_source_for_target` 不再把场景卡抄成脚手架。

## 8. 测试

后端 `tests/test_catalog_book_spine.py`；前端 `ws-book-spine.test.jsx`（目录 store / 设计卡 / 同步状态）、`ws-scene-spine.test.jsx`、`ws-writer-spine.test.jsx`、`ws-snow-sync.test.jsx` 的阶段 X 用例。改动过契约的旧用例：`test_catalog_api.py`、`test_catalog_single_source.py`、`test_project_overview_v2.py`（slug）、`test_author_drafts.py`（空白稿）、`test_snowflake_chaptering.py`（幕）、`test_snowflake_chaptering_story_order.py`（手建章、拖场后的修复）、`scripts/smoke-phase3.mjs`（sid）。

## 9. 没做的

- 台子上改雪花场景卡的设计不会写回构思（只打 `desk_edited` 记号，回流时给差异）；要改设计，走卡上的 `在构思里改`。
- 章 id 仍是位置式的（章级运行时状态跟位置走）。
