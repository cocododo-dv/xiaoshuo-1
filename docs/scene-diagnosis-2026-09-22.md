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
- `craft`：原写作台的三条本地规则搬到服务端（`craft_findings`）：贴邻叠句（taste）、段落偏长（info，>170 字）、连续三句同字开头（info）。有风格绑定时不出「段落偏长」（参考作者的段落尺度说了算）。
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

## 4. 没做与开放项

- 深评仍是整场一次（10 维 × 5 镜头）；没有按段落 / 按发现的局部深评。
- `craft` 三条规则仍是 2026-09-21 那版的口径（170 字、三句同字），没有按参考作者校准。
- 章级深评接口（`POST /api/v1/chapters/{id}/deep-review`）保留、同样拒绝式，但没有界面。
- 目录载荷没有每场的「开着的发现数」；成稿中心 / 主页不显示诊断计数。

## 5. 测试

后端：`tests/test_scene_diagnosis.py`（段落拆分与定位、规则 / 节奏发现的形状与稳定 id、载荷、忽略在三处生效、AI 深评拒绝式 / 并入 / 改稿后 stale、准定稿评审并入、改写请求带发现）、改写后的 `tests/test_writer_deep_review.py`；前端：`ws-writer-deep.test.jsx`（服务端诊断、深链选中、忽略 / 恢复、AI 深评与无模型、按诊断改写）、`ws-quality.test.jsx`。

## 6. 部署

无迁移。有已保存提示词快照的安装需要 `cd backend; python -m novel_system.tools.sync_prompt_templates --execute`（`writer_deep_review` v5）。设置里的「写作台：深度审读」节点从此真的会被调用。
