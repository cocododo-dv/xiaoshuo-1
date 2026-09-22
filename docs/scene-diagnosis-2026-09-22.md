# 场景诊断：一份记录、一处展示（2026-09-22）

作者的原话：「文学质量模块检测出来的问题，和写作深改台里的深改诊断是不是有些孤立，各是各的诊断，没有打通」——然后：「统一优化，做成最好的」。

这份文档是现行契约：一场正文「哪里有问题」只有**一份记录**，写作台的深改面板是**唯一的展示处**；文学质量视图、成稿门、起草台的评审和 AI 深评都读写这一份。

## 0. 改之前是什么样

在代码里逐项核对（不是猜的）：

| # | 引擎 | 在哪 | 词汇 | 谁看 |
|---|---|---|---|---|
| 1 | 21 维规则体检 | `services/literary_quality.py`，每次请求现算、不落库 | 21 维；blocking / revision / taste / info | 文学质量视图 |
| 2 | 三条浏览器本地正则（贴邻叠句 / 段落偏长 / 句首重复） | `ws-deep.jsx`，跑在编辑器 DOM 上 | echo / dump / rdn；high / mid / low | 写作台深改姿态 |
| 3 | LLM 深评 `writer_deep_review`（10 维 × 5 镜头，落 `writer_evaluations`） | 后端 | 10 维；blocking / revision / taste / ignore_ok | **没有任何界面调用过**；设置里却能给它配模型；无模型时按「保护 / 真相 / 公开 / 隐藏」这类写死的词给套话 |
| 4 | 同一批 21 维规则在管线里再跑 | 去模板门、成稿门（Q3 警告）、工作台载荷 `anti_template_quality_summary` | 同 1 | 成稿中心看警告；工作台那一块前端从不读 |

页面之间只有跳转：文学质量的「去写作台处理这一场」、成稿中心的「直达深改」、起草台的「在写作台深改」都只发两个事件（定位场景、切到深改姿态），维度、证据、改法、id 全部丢在路上；写作台到了就跑自己的三条正则，作者点的是「模型腔」，看到的是「第 3 段偏长」。改写请求把工具条的自由文本（「润色」）当 `issue_dimension` 发给后端，`quality_signal_id` 从没填过，修补类别永远是 `local_patch`，偏好画像学到的是「润色」两个字。忽略清单按前端键（`echo:3:安静`）存，只对那三条正则有效。

## 1. 一种发现形状

`services/scene_diagnosis.py` 是聚合者（叶子模块，只依赖 `literary_quality`、模型与风格绑定解析）。每条发现：

```
signal_id        稳定 id，作者的「忽略」按它记
source           rules | craft | review | ai
dimension, label 维度键 + 中文名（服务端给，前端不再各自维护对照表）
lens             AI 深评的镜头（story / character / prose / reader / theme），其余为空
severity         blocking | revision | taste | info（深评的 ignore_ok 归为 info）
issue, recommendation, why   中文
evidence         { excerpt, paragraph_index, start, end } | null
context          更宽的证据窗口，只供显示
ignored          在这一场的忽略清单里
stale            评审 / 深评的证据在当前正文里已找不到（多半改掉了）
house_taste      有风格绑定时，规则 / 节奏发现是房风词表的意见
origin           评审 / 深评行的 id 与时间
patch            { candidate_category, revision_strategy }：从这条发现发起改写时后端会用的类别与策略
```

**四个来源：**

- `rules`：`literary_quality.analyze_literary_quality` 的 21 维。每条规则发现现在带 `needle`（命中的词 / 句）与 `anchor`（`text` / `ending` / `scene`），`rule_signal_id` 由「命中了什么」而不是「在第几段」算出（`rules:<dimension>:<8 hex of needle>`，缺席类 `rules:<dimension>:scene`，结尾类 `…:ending`）——前面加一段、别处改几个字，id 不变，「忽略」跟着发现走。中文问题 / 改法在 `literary_quality.DIMENSION_NOTES`，唯一一份。
- `craft`：原写作台的三条本地规则搬到服务端（`craft_findings`）：贴邻叠句（taste）、段落偏长（info，>170 字）、连续三句同字开头（info）。有风格绑定时按参考作者校准（§7.1）：阈值取参考书段长的长尾，参考作者常用的叠句 / 句首重复不提示。
- `review`：起草台准定稿评审（`writer_evaluations` 里 rubric `near_final_acceptance_v1`）最近一行的发现，含 `failure_class` 与 `revision_brief`。
- `ai`：写作台的 AI 深评（rubric `literary_revision_v1`）最近一行的发现、`revision_brief`、各镜头分数。

**钉到段落**：正文是作者稿 HTML；`manuscript_html.manuscript_paragraphs` 按写作台编辑器的 `querySelectorAll("p, blockquote")` 同一规则拆段（含嵌套、文档序），`paragraph_index` 就是编辑器里的段序号，`start / end` 是这一段可见文字里的偏移；先按偏移取 Range，对不上再按证据文字在那一段找，再全文找。`plain_manuscript_text` 是规则引擎、成稿门与诊断共同吃的那份可见文字——过去规则直接吃 HTML，「第一句」里带着 `<p>`，同一条发现在两个页面算出两个 id。

**时效**：`ai.status` / `review.status` ∈ `not_run | current | stale`。评审的 `source_text_ref` 是 `final_scene:<row>` 时比正文是否相同（作者稿常是从终稿复制出来的）；是 `author_draft:<id>` 时看草稿的 `updated_at` 是否晚于评审的 `created_at`（空保存不改时间戳）。逐条的 `stale` 看证据还在不在。

**忽略**：`SceneCard.deep_review_ignored_keys_json` 存 signal id（原有的偏好接口与修订号不变）。载荷里被忽略的发现仍在（`ignored: true`），`summary.open` 不计；文学质量视图的场景条目不再列出它们（`ignored_count`、`ignored_findings`、`open_dimensions`）；成稿门（`final_text_gate._literary`）对「这一维度的发现全被忽略」的维度不再挂 Q3 警告（`ignored_dimensions`，`literary_quality.ignored_rule_dimensions`）。分数照算——分数是文本的事实，忽略是作者的决定。

## 2. 接口

- `GET /api/v1/scenes/{id}/deep-review` → 统一诊断载荷：`text {layer, ref, sha256, paragraph_count, chars}`、`style_bound`、`findings[]`（按严重度、段落、来源排序，同一 id 只留一条）、`summary`、`ai {status, evaluation_id, overall_score, revision_brief, lenses, llm_call_id, created_at}`、`review {status, failure_class, revision_brief, …}`、`preferences {revision_no, decision_log, ignored_issue_keys}`、`patch_candidates[]`（最近 20 条），加上旧契约的键（`status / latest_evaluation / lens_evaluations / rubric_id …`）。没有正文时 `text.layer = "none"`、`findings = []`。
- `POST /api/v1/scenes/{id}/deep-review` → 跑一次 `writer_deep_review` 节点，返回同一载荷。**拒绝式**：无模型 409 `WRITER_DEEP_REVIEW_LLM_REQUIRED` + `author_action` → 系统配置，不动历史；模型答完了旧的一轮才 `superseded`。提示词 v5：`evidence_excerpt` 逐字引自原文（≤80 字、不加省略号，系统按它定位）；载荷带这一场的「Scene Structure (Snowflake)」与「Scene Design Context」，按作者设计的这一场判断。本地词表兜底 `_diagnose_by_lens` 已删除。
- `PATCH /api/v1/scenes/{id}/deep-review/preferences` 不变（键改为 signal id）。
- `POST /api/v1/passages/patch-candidates` 新增 `instruction`（作者 / 诊断给的改法）与 `issue_note`（发现的问题句）；`issue_dimension` 从此只放维度键（发现的 `dimension`，或工具条自由改写的 `author_instruction`）。修补类别按维度推（`candidate_category_for_dimension` 认 21 维与深评十维），改写策略 = 指令，偏好标签按类别（自由改写记作者的那句话）。提示词多两行：`Diagnosed Issue:` / `Author Instruction:`。
- 工作台载荷不再带 `anti_template_quality_summary`（每次轮询都在重算 21 维，而前端从不读）。
- 文学质量三个端点的发现改为统一形状（`signal_id`、中文 `issue / recommendation / label`、`ignored` 过滤、`recommended_next_action.signal_id`）。

## 3. 写作台

- 进深改：先把未落盘的改动存下去（诊断要对着存下去的字），再 `GET deep-review`；标注按 `paragraph_index + start/end` 落在那句上（`mark.wr-dx`，按严重度四档上色；整段的钉整段）。
- 面板：AI 深评块（还没跑过 / 时间 · 总分 / 改前的深评——重新深评；无模型给「去系统设置」）、有绑定时的房风提示、来源筛选（全部 / 规则 / 节奏 / 起草评审 / AI 深评）、发现列表（展开一条：证据、改法、为什么；「按诊断改写」「选中这一句去改写」「忽略这一项」）、「已忽略 N 项」（逐条恢复）、最近的决定。「重新诊断」重新取，不丢忽略。
- 「按诊断改写」：回到起草、选中那一句，工具条捕获选区后按发现的改法直接出候选；「选中这一句去改写」只选中。两者都把发现交给工具条（`rewriteFinding`）：这一轮的每一次改写请求（润色 / 更凝练 / 自定义…）都带 `quality_signal_id / issue_dimension / issue_note / instruction / candidate_category / source_draft_id`；作者把选区换到别处、关掉弹层、换场，发现作废。
- 深链：`ws:writer-posture` 的 detail 可以是 `"deep"` 或 `{ posture: "deep", signal_id }`。文学质量的每条发现与条目级按钮、成稿中心「直达深改」、待办卡都能带 id；到了诊断就选中那一条并滚过去，当前作者稿里没有它就提示。
- 文学质量视图：问题 / 改法读服务端中文；条目上有「已忽略 N」；风险维度只算还开着的。

## 4. 开放项（第三轮收尾之后）

第二轮留下的四项（局部深评只看一段、规则不按参考书校准、章级通读整章一次、计数按需拉取）第三轮全部做完（§8），第三轮留下的三项（阈值是定值、无分界的书收尾不校准、后台作业归档后角标不更新）同日收尾（§8.5）。还开着的：

- 局部深评一次最多 40 段；整场超过 12,000 字时远段只留开头。
- 只通读改过的场时，未改的场只沿用上一轮钉在它上面的发现；上一轮钉不到任何一场的章级发现由模型重说，不沿用。
- 校准的统计口径本身（泊松尾概率 1/10、Wilson 80% 下界、一半 / 四分之一两档）是约定的口径，不是从作者的采纳 / 忽略里学的；作者在面板里忽略过的发现只按 id 生效，还没有反过来调整这本书的校准。
- 另一个标签页 / 别的进程改了正文，本页的角标要等它自己的写入或下次挂载。

## 5. 测试

后端：`tests/test_scene_diagnosis.py`（段落拆分与定位、规则 / 节奏发现的形状与稳定 id、载荷、忽略在三处生效、AI 深评拒绝式 / 并入 / 改稿后 stale、准定稿评审并入、改写请求带发现）、改写后的 `tests/test_writer_deep_review.py`；前端：`ws-writer-deep.test.jsx`（服务端诊断、深链选中、忽略 / 恢复、AI 深评与无模型、按诊断改写）、`ws-quality.test.jsx`。

## 6. 部署

无迁移。有已保存提示词快照的安装需要 `cd backend; python -m novel_system.tools.sync_prompt_templates --execute`（`writer_deep_review` v5）。设置里的「写作台：深度审读」节点从此真的会被调用。

## 7. 第二轮（同日）：四个开放项做完

作者：「把没做的部分也做了」。

**7.1 节奏检查按参考作者校准。** 有风格绑定的场，`SceneDiagnosisService.craft_calibration` 按绑定画像的参考书算三个读数（进程内按「书、段落数、最新段落时间」缓存；『龙族』26,616 段首算约 1.1 秒）：段长 p95 → 「段落偏长」的阈值 = max(170, p95)（『龙族』：194 字）；每千段贴邻叠句数 ≥ 5、每千段三句同字开头 ≥ 10、或画像标了 `deliberate_repetition` → 那条检查对这位作者不提示（『龙族』：贴邻叠句不提示）。载荷带 `craft_calibration {source, book_title, long_paragraph_chars, echo_per_1k, same_opening_per_1k, flag_echo, flag_same_opening, deliberate_repetition, note}`，面板的绑定提示里把 `note` 说出来（「按《龙族》校准：段落超过 194 字才提示；贴邻叠句不提示（这位作者常这么写）。」）。

**7.2 「AI 看这一处」（局部深评）。** `POST /api/v1/scenes/{id}/deep-review/passage`，body `{signal_id}`（复核一条发现）或 `{paragraph_index | excerpt}`（独立看一段），可带 `question`。模板 `writer_passage_review` v1（走 `writer_deep_review` 的节点路由，预算 24000 = 局部改写档）：只看焦点段 + 前后各一段（`passage_window`，焦点段标【焦点段】），返回 `verdict ∈ holds | partly | does_not_hold | no_finding`、`assessment`、只落在焦点段的 `findings`（证据逐字）、`rewrite_brief`。结果落成一行 rubric `literary_revision_passage_v1` 的 `WriterEvaluation`（`lens=passage`，`contract_field_refs_json` 记段落 / 复核的发现 id / 判定 / 评语 / 改法 / 问题；同一段或同一条发现再看一次，旧的 `superseded`）。统一诊断把它并进来：复核的意见挂在那条发现上（`finding.opinion`），新看出的发现进清单（`origin.kind = passage`），`passage_reviews[]` 列出有效的几次。面板：每条发现的「AI 看这一处」；意见块（AI：成立 / 部分成立 / 不成立 + 评语 + AI 的改法，「按 AI 的改法改写」「按 AI 的判断忽略」）；深改姿态里选中一段的工具条多一个「AI 看这一段」，独立结果显示为「AI 看了第 N 段」并可「按这个改法改写这一段」。

**7.3 「AI 通读本章」（成稿中心 · 诊断页签）。** `GET/POST /api/v1/chapters/{id}/deep-review` 改为章级诊断载荷（`chapter_payload`）：`ai {status not_run | current | stale（任何一场作者稿在它之后改过）, overall_score, revision_brief}`、`chapter_findings[]`（钉不到任何一场的章级判断：承诺 / 升级 / 兑现）、`scenes[] {scene_id, summary, ai_status, review_status, findings_from_chapter[]}`、`summary`。通读的发现按证据钉到哪一场就落到哪一场（写作台那一场的深改面板里也是同一条，`origin.kind = chapter`；`chapter_review {status, findings_here}`），整章文本按场标出「【第 N 场】」。成稿中心的「诊断」页签（`ws-manuscripts-diagnosis.jsx`）：通读按钮与状态、整章的判断、各场行（开着 N · 阻断 N · 深评状态 · 落到这一场的通读发现）、「去写作台看」/「在写作台看这一处」深链；结构页签的场景行与左栏章行带「诊断 N」。

**7.4 全书计数。** `GET /api/v1/projects/{id}/diagnosis-summary`（`project_summary`）：每场 `{open, blocking, revision, taste, info, ignored, stale, ai_status, review_status, text_layer}`、每章 `{open, blocking, chapter_level, scenes, scenes_with_findings, ai_status}`、`totals`。前端 `ws-diagnosis-summary.jsx`（`WsDiagnosis` / `useDiagnosisSummary`，与 `ws-design-sync` 同一套节流：挂载拉一次，`ws:diagnosis-changed` 立刻重拉，目录事件 20 秒内不重拉；深改面板的忽略 / 恢复 / 深评 / 局部深评之后广播）。主页章卡「诊断 N」+ 进度脊「诊断待改 N」；成稿中心如 7.3。

**7.5 顺手修好的两件事。** (1) 深评的用户消息从来没带过正文：`PromptBuilder` 只渲染 `inline_digests` 里的 section，而深评把 `scene_summary` 放在快照顶层——节点在接进面板之前从未被调用，所以没人发现。现在正文（可见文字，不是作者稿的 HTML）、结构简报、设计背景都作为 inline digest 进用户消息，章级通读的正文按场标出。(2) 全书计数一开始要 4.5 秒：`InjectionService.resolve_binding_layers` 每场加载一次带整本书窗口索引的 `profile_json`（0.66 秒）。`binding_profile` 改为轻量解析（只读绑定行与画像状态，`json_extract` 取 `deliberate_repetition`；scene > character > project > global 与 `_binding_rank` 同一条规则，测试钉住），规则 / 节奏发现按（场、正文哈希、校准、绑定）缓存在进程里。真实项目 17 场：冷 1.4 秒（含参考书首算），热 0.43 秒；章级载荷 0.16 秒。

测试：`tests/test_scene_diagnosis.py` 第二轮块（校准、轻量绑定解析、局部深评与它的守卫、章级通读落场与拒绝式、全书计数）；前端 `ws-writer-deep.test.jsx`（AI 看这一处 / 看这一段）、`ws-manuscripts-diagnosis.test.jsx`、`ws-diagnosis-summary.test.js`、`ws-manuscripts-flow.test.jsx` 与 `ws-home.test.jsx` 的计数用例。部署：无迁移；新模板 `writer_passage_review` 走既有节点路由；有提示词快照的安装需要 `sync_prompt_templates --execute`。


## 8. 第三轮（同日）：「重构修复优化」——第二轮的四个开放项

作者把第二轮的「还开着的」四条原样贴回来：「重构修复优化」。

**8.1 局部深评看整场（跨段的矛盾）。** `POST …/deep-review/passage` 的焦点可以是一段（`paragraph_index` / `excerpt`）、一段范围（`paragraph_start`–`paragraph_end`，含两端；一次最多 40 段）或一条发现所在的段（`signal_id`；跨段的发现把另一段也算进焦点）。模型看到的不再是「焦点段 ± 一段」而是**整场**（`passage_scope`：焦点段标【焦点段 N】、前后段标【上下文 N】、其余段标【第 N 段】全文；整场超过 12,000 字时远段只留开头，标【第 N 段·略】），用户消息尾部多一节「Cross-Paragraph Check」：焦点段与本场任何一段的事实 / 物件 / 时间 / 谁知道什么矛盾、重复、承接失落，都要报，并给 `related_excerpt`（另一段的原话，≤80 字）、`related_paragraph_index`（那一段标记里的序号）、`relation ∈ contradiction | repetition | continuity`。统一发现多一个 `related {excerpt, paragraph_index, start, end, kind, label, stale}`（另一段的原话钉到段；那句已不在正文里就 `stale`）。评审行记 `focus_paragraphs / paragraph_start / paragraph_end / whole_scene / about_signal_ids`；焦点段有交集、或复核同一条发现的旧行退位。模板 `writer_passage_review` v2（findings 成员的 properties 写明——schema-enforcing 中转对没声明 properties 的成员只会解码成 `{}`；`writer_deep_review` v6 同样补上）。写作台：深改姿态里选中跨几段的字，工具条按钮变成「AI 看这几段」（POST 范围）；面板行上标「与第 N 段矛盾 / 重复 / 承接」，展开给另一段的原话与「看第 N 段」，正文里另一段那句也标出来（`mark.wr-dx.is-related`）；独立看范围的结果显示「AI 看了第 2–3 段」。

**8.2 21 维规则按参考书校准词表与维度。** `literary_quality.RuleCalibration`（数据在 `literary_quality`，算法在 `scene_diagnosis`，import 方向不变）：`compute_reference_rules` 把参考书按标题段 / 场分隔行切成单元、单元内按 ~2,400 字切窗口（最多 48 个，均匀取），量两样——(a) **词表词的密度**：「命中即毛病」的 14 张词表（`FAULT_LEXICONS`：模型腔、说明式对白、汇报式对白、总结式收尾、重复动作、意象、氛围意象、虚假清晰、装饰意象、动机说明、诗化收尾、感知过滤、冲突 / 和解）里每个词每万字的次数，一遍正则；每万字 ≥ 1 次的是这位作者的常用词（`habitual_needles`），规则不再按它们提示——除非稿子里的密度到了参考的 4 倍以上且至少 3 次（过量，照提示）；「缺席才是毛病」的词表（抉择 / 压力 / 代价 / 收尾动作）不动。(b) **每条规则在窗口上响的比例**：≥ 50% 的规则是这位作者的常态（`habitual_dimensions`），发现降为 `info`、带 `calibrated {kind: dimension_habit, share}`，`why` 说「参考作者的场里约 N% 也是这样，只作提示」；收尾三条（总结式收尾 / 收束驱动 / 诗化收尾）只在真实的单元末尾上量（最多 48 个，至少 8 个才算）。读数与节奏读数存在同一个进程缓存里（『龙族』：规则读数 ≈0.8 秒一次）；`craft_calibration.rules {habitual_needles, habitual_dimensions, dimension_shares, windows, endings, chars}`，`note` 把常用词与常态维度说出来。文学质量视图读同一份校准：路由把 `SceneDiagnosisService.rule_calibration_for_scene` 注进 `LiteraryQualityService(rule_calibration_resolver=…)`（`literary_quality` 不能 import `scene_diagnosis`），条目带 `rule_calibration`。真实项目（『龙族』绑定）：26 个常用词（看、手、血、眼、风、光、走、门、火、笑、因为…），常态维度 7 个（意象同质 100%、句式单调 100%、重复动作 85%、意象场复用 81%、抉择压力 58%、说明式对白 56%、动机说明 56%）；一场 25 段的作者稿从 5 条修订变成 2 条修订 + 3 条提示。没有绑定时词表与行为逐字不变。

**8.3 只通读改过的场。** 通读行的 `contract_field_refs_json` 记 `{kind: chapter, scope: all | changed, scenes: [{scene_id, scene_seq, sha256, layer, ref}], reviewed_scene_ids, carried_scene_ids, carried_from}`；章级 `ai.status` 从此按每场正文的哈希判（哪一场的字变了、多了一场有字的、少了一场 → `stale`；老的行没有哈希，退回时间戳），并给 `changed_scene_ids / changed_count / incremental_available / scope / reviewed_scene_ids / carried_scene_ids / carried_from`，每场条目带 `changed_since_review / carried`。`POST …/chapters/{id}/deep-review {scope: "all" | "changed"}`：`changed` 时改过的场全文（【第 N 场 · 本次通读】），未改的场只给开头 / 结尾 / 上一轮钉在它上面的发现（【第 N 场 · 未改 · 摘要】），用户消息尾部一节「Read-Through Scope」+ 上一轮的章级发现（让模型重说哪些还成立）；模型答完后未改的场沿用上一轮的发现（`carried_from`，写作台里 `origin.carried_from`，同一条 id），改过的场按模型的新发现，章级发现以模型的为准。上次之后没有场改过 → 不调模型，载荷带 `notice.code = CHAPTER_REVIEW_UP_TO_DATE`；上一轮没记哈希 → 退回整章。`_chapter_source` 从此只从各场的诊断正文拼（不再取章级作者稿 / 章记忆：发现要钉到各场的字上）。成稿中心：改前的通读且算得出改过的场时给「只通读改过的 N 场」（主）与「整章重新通读」，状态行说「改过 N 场：第 x 场」「上次只通读了改过的 N 场，其余沿用更早的通读」，场行标「通读后改过」「沿用上次通读」，发现标「沿用上次通读」。

**8.4 计数随写回传（推送）。** 每一次会改动发现的写入都在响应里带 `diagnosis_rollup {project_id, chapter_id, chapters: {id: 章条目}, scenes: {id: 场条目}}`（这一场所在那一章的章条目 + 章里每一场的条目；`scene_rollup` / `chapter_rollup`，与 `project_summary` 同一种条目形状）：`PATCH /api/v1/author-drafts/{id}`（字改了才带）、`PATCH …/deep-review/preferences`（忽略 / 恢复）、`GET/POST …/scenes/{id}/deep-review`、`POST …/deep-review/passage`、`GET/POST …/chapters/{id}/deep-review`；另有 `GET /api/v1/scenes/{id}/diagnosis-rollup`（起草台归档终稿之后拉这一章）。前端 `WsDiagnosis`：整本书只在挂载 / 换作品时读一次；`applyRollup` 合进表（章里旧的场条目换成新的，进了回收站的场随之消失）、`totals` 本地按服务端 `summarize_counts` 同一条规则汇总（`__summarize` 与服务端 totals 相等，测试钉住）；忽略 / 恢复先按面板清单 `applySceneFindings` 记一笔、章按差额重算；`ws:diagnosis-changed` 带 `rollup` 或 `findings` 就用它，什么都没带才重拉；`ws:catalog-changed` 只剪掉不在目录里的场 / 章（`reconcile`），20 秒节流没有了。作者稿保存（`WrDocs.pushSave`）、深改面板（`useDeepPosture`）、成稿中心通读、起草台归档（`ws-scene-api` adopt-current → `refreshScene`）都接上了。章条目多 `chapter_level_blocking`。

测试：`tests/test_scene_diagnosis_round3.py`（校准的词表 / 维度 / 过量 / 房风不变、绑定场与文学质量视图一致、`passage_scope`、范围深评与跨段发现与退位规则、只通读改过的场全流程与退回、计数随写回传与汇总相等）、`tests/test_scene_diagnosis.py` 的两处标记断言；前端 `ws-diagnosis-summary.test.js`（推送模型）、`ws-manuscripts-diagnosis.test.jsx`（只通读改过的场）、`ws-writer-deep.test.jsx`（AI 看这几段、跨段发现、忽略后本地计数）。部署：无迁移；有提示词快照的安装需要 `sync_prompt_templates --execute`（`writer_deep_review` v6、`writer_passage_review` v2）。

### 8.5 收尾（同日）：第三轮留下的三项

作者把 §8 末尾的「还开着的」三条贴回来：「把这也完成完善优化」。

**阈值从参考书学，不是定值。** (1) 词表词不再有「每万字 ≥ 1 次算常用词」和「4 倍 + 3 次算过量」两个定值：记的是参考作者每万字用某个词的次数（`needle_rates`），诊断一稿时按这位作者的密度算这个词在**一场的量**（至少 `RULE_JUDGE_WINDOW_CHARS` = 2,400 字——校准量的就是这种窗口，短稿不吃亏）里的期望次数，稿子里的次数在泊松分布下的尾概率 ≥ `RULE_NEEDLE_TAIL_ALPHA`（1/10）就是这位作者的寻常用法、不提示；更小（作者从不用的词，或用得比作者密得多）才照提示——一条规则同时管「放过」与「过量」（`literary_quality.calibrate_lexicons`，`poisson_tail`）。放过了哪些词随载荷回来：`craft_calibration.waived_in_scene [{term, count, reference_per_10k, expected, probability}]`，写作台的绑定提示说「本场按这位作者的密度放过 N 个词：看 ×5、光 ×2…」。(2) 维度不再是「一半以上算常态」的单一门槛：每条规则在参考书窗口上响的比例按 80% 置信的 Wilson 下界定档（`wilson_lower_bound` / `dimension_level`）——下界 ≥ 一半是常态（发现降为 `info`），≥ 四分之一是常见（降为 `taste`，只降不升）；窗口少的书要观察到更高的比例才算数（48 个窗口里响 28 个说不上一半以上，96 个里响 56 个就说得上），所以采样窗口从 48 提到 96。`calibrated {kind, level habit|common, share, lower_bound, n}`，`why` 说「参考作者的场里约 N% 也是这样（M 个窗口），只作提示 / 按审美看」；面板行上标「参考作者的常态」/「参考作者也常见」。(3) 画像的话直接算数：`voice_signature.deliberate_repetition` 为真时重复一族（`RULE_REPETITION_DIMENSIONS`：重复动作 / 模板句 / 自我重复 / 意象同质 / 意象场复用）不看比例就是常态（`calibrated.kind = profile_deliberate_repetition`，行上标「画像：刻意重复」）。口径本身（1/10、80%、一半 / 四分之一）写进 `craft_calibration.rules.thresholds` 与文档，不再散在代码里。

**没有标题段和场分隔行的书也校准收尾。** 参考书切单元的分界有三种：标题段、纯符号分隔行、**导入时记下的场界**（`StyleReferenceBook.stats_json.scene_breaks`，含段落表本身看不出来的空行分界）；结构分界不到 4 个真实收尾时再按**转场段**补：分类器标为 `transition` 的段之前的那一段也是一个收尾（`_reference_units`）。`endings_source ∈ units | units+transitions | transitions | none`（`none` = 连转场段都没有：收尾三条如实不校准，注记说出来）；收尾 / 窗口都至少要 4 个才算数（Wilson 下界撑不起更少的样本）。校准读参考书时连段型一起读。

**后台作业归档终稿之后角标即时更新。** 新路由 `GET /api/v1/chapters/{id}/diagnosis-rollup`（`chapter_rollup`）；`WsDiagnosis.refreshChapter(chapterId)`（同一章在途的只拉一次）。章运行（`ws-chapter-run.jsx`）轮询到 `completed_count` 比上一次多、且是亲眼看着的运行时拉这一章的 rollup（打开一章时水合到的「上次已完成」不拉）；起草台的 `scnRun` 在后端原子归档（`scene_status = archived`）时拉这一场所在的章（`refreshScene`）；起草台的采纳归档已在第三轮接上。

真实项目（『龙族』绑定，实库只读探针）：收尾按 96 个章末校准（`endings_source = units`）；常态维度 6 个（意象同质 / 句式单调 / 重复动作 / 意象场复用 / 说明式对白 / 过度解释动机），常见 3 个（抉择压力 / 收束驱动 / 模型腔——48 个窗口下抉择压力曾被判成常态，96 个窗口的 Wilson 下界把它放到「常见」）；那一场 25 段的作者稿按密度放过 13 个词（手 ×9、光 ×4、眼 ×4、冷 ×3……），规则发现 1 条修订 + 2 条审美 + 2 条提示。

测试：`tests/test_scene_diagnosis_round3.py` 的统计助手、学阈值、分档与画像声明、收尾来源四个用例 + 绑定场用例（无标题段、靠场界）+ 章 rollup 路由；前端 `ws-chapter-run.test.jsx`（完成一场就刷新）、`ws-diagnosis-summary.test.js`（`refreshChapter`）、`ws-writer-deep.test.jsx`（放过的词与常态 / 常见标记）。部署：无迁移、无提示词改动。
