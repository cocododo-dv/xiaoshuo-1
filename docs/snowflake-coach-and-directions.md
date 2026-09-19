# 雪花构思 · 教练、要点、方向与生成

状态：**现行契约**（2026-09-17 阶段 U 落地后核对）。本文写清雪花工作台里 AI 的四个概念怎么分工、界面上每个入口做什么、后端契约是什么、以及为什么把原来的「候选」页签并进了教练。代码与本文冲突时以代码为准，并回写本文。十步本身与原著的对照见[雪花方法契约](snowflake-method-contract.md)。

## 1. 为什么改

2026-09-16 之前，构思视图有三个互不知情的 AI 入口：「候选」页签抽三张卡、「教练」页签单发问答、整步生成只看上游。阶段 T 加了作者意图要点把它们连起来，但界面上又多出一层概念：候选正文、教练回复、教练补丁、要点、「以此为方向生成」、「采纳并结构化」、「仅作草稿」、「生成时带入」、「继承上游」……作者面对同一步至少有五种「让 AI 改这一步」的按钮，措辞各不相同，看不出区别。阶段 U 只留四个概念、一个动词。

## 2. 四个概念，一个动词

| 概念 | 是什么 | 谁写 | 存在哪 |
|---|---|---|---|
| **要点** | 你对这一步的意图：决定 / 否决 / 约束 / 待定，本步 / 全书 | 教练每轮蒸馏，你核对、改写、撤下、加条 | `snowflake_direction_briefs`（一步一行） |
| **方向** | 这一步的一个走向，只用于一次生成的蓝本 | 教练给（「先看 3 个方向」一次三个），或教练某轮回复本身 | 教练日志（`snowflake_assistant_turns`，`turn_kind=candidates`） |
| **改写** | 教练在你明确要求时直接给的本步字段改写 | 教练 | 教练日志（`candidate_patch`） |
| **生成** | 让 AI 按上游材料 + 要点（+ 可选的一个方向）把本步写出来 | 模型 | 步骤版本（`SnowflakeStepRun`，`health.direction` 记按哪个方向） |

动词只有一个：**生成**。它在界面上有几个入口，区别只是「什么变了」：

- 「AI 生成本步」——按上游材料 + 要点，从头写（01–08；09 的「AI 生成整表」与 10 的「AI 补全所有场景」在脚手架里，是同一件事）。
- 「按此生成本步」——同上，再加一个方向做蓝本（方向卡上 / 教练回复下）。多成员步骤（04 / 06 / 08 角色、10 场景）另有「只更新「X」」，只落到当前选中的成员。
- 「按最新要点重新生成」——要点改过而本步草稿还是按旧要点写的（编辑页工具条与要点卡都会提示）。
- 「按新上游重新生成」——上游改了、本步被标为需复核（失效横幅）。

要点**永远**带入生成、方向与分诊。阶段 U 去掉了「生成时带入」开关：不想让某条约束生成，撤下那条即可，不必背一个全局开关（后端 `use_direction_brief` 字段保留，缺省 true）。

## 3. 界面

页签：编辑 / 教练 / 历史 / 引用上下文（「候选」页签已删除）。

- **编辑页顶部的 AI 工具条**（每一步都有）：「AI 生成本步」（09 / 10 除外）、「先看 3 个方向」、「要点 N 条」（点去教练页）。第二行说本步当前版本怎么来的——「本稿：按方向「推进向」生成 · 带第 2 版要点」——要点落后时给「按最新要点重新生成」。AI 动作的失败原因也在这里显示（LLM 未配置的 409 一样）。
- **教练页**：上面是要点卡（继承上游开关、清空、逐条改；已撤条目可恢复），下面是一条日志：问答回合（回复、建议、「填入本步」的改写、「按此生成本步」）和方向回合（三张方向卡）混排、按时间顺序、服务端持久化。每一轮下面写这一轮对要点做了什么（「要点 +2 · 改 1」）。被采纳过的方向卡 / 回复打「已按此生成」。输入框旁边两个按钮：「发送」是问教练，「给 3 个方向」是让教练按输入框里的要求给方向（输入框可空）。第 10 步的方向只针对左侧选中的那一场。
- **02 一句话概括**是自由文本步：方向卡上是「就用这一句」，直接写进草稿，不再让模型转述一遍。
- **右栏「本步要点」**：只读镜像，随时看得到本步意图；编辑去教练页。
- 有脚手架的步骤不再有「仅作草稿」：方向一律「按此生成本步」进脚手架。旧版留下的自由草稿仍显示为「旧版采纳候选所得」，可清除。
- 删掉了右栏「关联与影响」里写死的「参考画像 · 冷峻短句 / 影响候选生成的节奏与句式」和引用页里写死的「风格 · 参考画像」卡——它们是原型遗留的假数据，和候选毫无关系。

## 4. 后端契约

- `POST …/steps/{step_key}/fe-candidates`：**fail-closed**（LLM 未启用 409 `SNOWFLAKE_LLM_NOT_CONFIGURED`，与教练同一条路；以前回 `source=fallback` + 空列表）。请求：`target_chars`、`ask`（作者对这一组方向的要求，≤600 字）、`draft_override`（与 generate / assistant 同源的本地最新规范草稿）、`focus_scene_id`（仅 `scene_details`）；旧字段 `context` / `draft` 仍接受。提示载荷：`author_ask`、`current_canonical_draft`（含本地最新编辑）、`focus_scene`、`author_direction_brief`。结果落成 `turn_kind=candidates` 的回合（`candidates_json = {items, target_chars}`）；回包 `{source, llm_call_id, candidates, turn_id, turn, assistant_history}`。模型给不出方向 → 502 `SNOWFLAKE_CANDIDATES_EMPTY`，不写回合。
- `POST …/steps/{step_key}/generate`：新增 `direction_turn_id`（方向来自哪一回合）与 `direction_index`（方向回合的第几条）。服务端按回合种类推出 `direction_kind`（方向回合 → `candidate`，问答回合 → `coach_reply`），生成成功后在回合上记 `adoption_json = {step_run_id, candidate_index, adopted_at}`，在这一版 `health_json.direction` 记 `{kind, turn_id, candidate_index, label, sha}`。回合不存在 / 不属于本作品 → 404 `SNOWFLAKE_DIRECTION_TURN_NOT_FOUND`；编号不对 → 400 `SNOWFLAKE_DIRECTION_INDEX_INVALID`；指了回合没带正文 → 400 `SNOWFLAKE_DIRECTION_TEXT_REQUIRED`。
- `POST …/assistant`：每轮的要点差异随回合落表（`brief_delta_json`），`assistant_history` 里每条回合带 `turn_kind` / `candidates` / `brief_delta` / `adoption`。教练看到的 `conversation.recent_turns` 里，方向回合是 `{kind: "candidates", message, directions[{label, tag, text}], chosen}`，问答回合是 `{kind: "chat", …, adopted_as_direction}`——作者选定的方向等于作者接受了它，教练可以把它记为要点里的「决定」。
- 迁移 `20260917_0088`：`snowflake_assistant_turns` 加 `turn_kind`（历史行回填 `chat`）、`candidates_json`、`brief_delta_json`、`adoption_json`。
- 提示词：`snowflake_workspace_assistant` v6、`snowflake_step_candidates` v6——已保存过提示词快照的安装要跑 `python -m novel_system.tools.sync_prompt_templates --execute`。

## 5. 有意不做的事

- 不把方向自动写进要点：方向是一次生成的蓝本，要点是持久的意图；选了方向 B 又换成 C 时，如果两条都自动成了「决定」，第二次生成就会同时被 B 约束。要让某个方向长期生效，让教练下一轮把它记成决定（它看得到你选了哪个），或自己在要点卡加一条。
- 不给方向做「对比草稿」视图与 1/2/3/C/R/↵ 快捷键：方向卡在对话里，比较的是三个走向，不是逐字 diff。
- 不做「纯探索」开关：撤下要点即可。

## 6. 同一批的两处加固（2026-09-18）

- **方向节点的输出预算** 1800 → 4096（`llm_node_registry` 与 `config/models.yaml` 两处一致）：三条方向各可到 400 字，再加思考 token，1800 每次都被截断，只能靠客户端的翻倍重试出结果。已存过 models 快照的安装用 `python -m novel_system.tools.raise_llm_output_budget --node snowflake_step_candidates --floor 4096 --execute`。
- **水合闸门与抹空保护**：新浏览器或清过缓存的会话里，水合失败（或只是比视图的首次自动保存慢）时，一份从没水合过的空白默认稿以前会强制覆盖十步，而未确认步骤在服务端是原位改写、没有历史可回。现在同步层在本会话读到服务端之前绝不上行，从没动过的空白步让位给服务端内容、也从不拿去覆盖服务端；服务端对「整步抹空」另起一版，旧稿留在历史里，可用 `POST …/steps/{step_key}/restore` 取回。底部同步状态会写「读不到服务器上的构思版本，已暂停上行以免覆盖服务器内容；本机版本已保留」，点重试即可。

## 7. 测试

后端 `tests/test_snowflake_coach_directions.py`、`tests/test_migration_0088_assistant_turn_kinds.py`、`tests/test_snowflake_wipe_guard.py`，以及改过契约的 `tests/test_snowflake_fe_candidates.py`（fail-closed、预算钉）；前端 `src/ws-snow-coach.test.jsx`（取代 `ws-snow-brief.test.jsx`）与 `src/ws-snow-sync.test.jsx` 的「水合闸门与空白步保护」一组。
