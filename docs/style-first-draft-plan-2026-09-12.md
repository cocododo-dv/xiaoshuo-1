# 风格参考 · 风格直起与结构跟随（2026-09-12，Step 2 计划，最大化版）

> 承接 `docs/style-exemplar-first-2026-09-09.md`（Step 1 样例优先，已落工作区、未提交）。
> 作者的决定：不做「像不像」度量，由作者阅读成品评测；**不要保守，目标是最大程度模仿作家的风格，从小到大、从大到小每一层都要像。**
> 本文是计划；§5 在实施后补写。第一版（保守版）已作废，本版替换。

## 0. 为什么第一步不够

第一步之后，风格稿看到约两万字原文样例，句子层面可以靠向作者。但：

- **场景层的取舍由「房风」先定型。** 中性稿在无任何风格信号下决定什么展示、什么略过、段落怎么切、开合是什么形状、解释到什么程度（`neutral_draft`：keep diction neutral / no summary ending / resist vivid phrasing）。风格稿拿到的骨架已经是房风骨架。
- **风格稿之后有三道不看风格块的自动改写门**：去模板门（`_anti_template_quality_gate`，14 维，任一命中即强制改一遍、改后不降分不算数）、规则版自动批评（`auto_critique`，命中即发风格补丁：删感知词、句式要多样、意象要有意义）、近终稿确定性门（`_model_voice_gate_findings` 11 词表 + `_missing_scene_machinery`「结尾必须是动作」→ 自动全篇重写）。写得越像，越被改回去。
- **蓝图与新鲜度预算写死房风**：蓝图九个必填字段要求「结尾是动作、不得概述、信息不得由叙述交代、意象必须新鲜」；新鲜度预算写死「以动作而非解释收尾」与两张词表，两个起草阶段都看。
- **结构层完全看不到参考。** 雪花场景清单 / 场景规划、章架构、章内场景规划、章级近终稿评审从不消费画像；章 / 场的长度、密度、开合方式、章尾钩子全部按系统自己的模板来。画像里的 `scene.*` / `theme.*` 子维度也只有 `narrative.*` 派生的 8 行进过蓝图。

## 1. 目标与原则

有绑定时：

1. **第一稿就以参考作者的手笔从 bundle 直接写**（风格直起）；`style_draft` 改为「再靠近一层」的复读，不是重组。
2. **所有以房风为准的自动改写全部让位**（仅记录，不改稿、不拒稿）；只保留硬门：事实与连续性（hard_qc）、必含项、禁止项、12 字抄袭门、禁用词、参考派生的形状包络。
3. **参考的结构进入规划层**：章 / 场的长度与密度、开章与收章方式、对白与叙述比重、主题与情绪走向，进入雪花场景清单与场景规划、章架构、章内场景规划、场景蓝图、章级近终稿评审。
4. **小层面再加码**：样例更多、默认强度拉满、首稿用续写式框架。
5. 无绑定的项目行为逐字不变；`draft_mode=neutral_first` 是阅读对照组。

## 2. 共享契约

### 2.1 开关 `binding.config_json.draft_mode`

- `style_first`（默认）/ `neutral_first`；缺省来自 `injection_budget.yaml: draft_mode_default: style_first`。
- bundle 构建时把生效值写进运行时契约顶层 `draft_mode`（最具体的绑定层说了算），随契约哈希冻结；校验只接受两个取值；旧契约缺键 → `neutral_first`，重放不变。
- `runtime_contract.effective_draft_mode(bundle)`；无契约 / absent / degraded → `neutral_first`。
- `style_bound`：契约 mode ∈ {frozen, frozen_legacy} 且 `draft_mode == style_first`。所有让位以它为条件。
- API：`ApplyProfileRequest.draft_mode` 落 `config_json`。

### 2.2 步位不动，内容换

`neutral_ready` 检查点、`stage="neutral_draft"` 行、attempt `step="neutral_draft"`、指针、账本字段全部保留（`_validate_advance` 只认 `bundle_ready → neutral_ready → hard_qc_ready`；恢复 / 取消 / 账本回收都建立在「中性步位必有一行 + 一笔已结算调用」上）。`style_first` 时该步位改跑 `style_first_draft` 模板、注入 `[STYLE_REFERENCE]`、走 `style_draft` 节点路由（不加新节点）。

### 2.3 模板版本

| 模板 | 动作 |
|---|---|
| `style_first_draft` | 新增：从 bundle 直接以作者手笔写；bundle 定「发生什么」，参考定「怎么讲」；蓝图八要素是要发生的事，怎么落纸由作者手法决定；长度带内用作者自己的扩缩手段；续写式框架（有前场声音锚时「接着这段往下写」）。预算 64000，`drafting` 口径 sections。 |
| `style_draft` v10 | 来源稿标签为「首稿（已按参考作者手笔写成）」时：保留已像的段落，把仍偏中性、偏解释、偏房风的段落再向样例靠一层；落实 hard_qc 意见；不重组、不中性化。 |
| `hard_qc` v3 | 换示例句（不再示范「改成动作而非概述」）。 |
| `scene_blueprint` v4 | 有结构画像 / 叙事机制块时按该作者的建场方式规划：`ending_action` 可为反思 / 议论 / 反讽 / 氛围收场，`anti_summary_rule` 可为「无」，`information_release` 可由叙述交代，`image_anchor` 可沿用作者惯用意象场；验证器接受「无」。 |
| `soft_qc` v5 | `summary_ending` / `model_voice` / `image_homogeneity` 只在参考作者本身不这样做时才算。 |
| `near_final_acceptance_review` v4 | 「概述 / 解释性因果 / 氛围收尾一律不过」仅在无风格块时生效。 |
| `scene_literary_rewrite` v4 | 「freshness / image necessity / ending drive」改为以参考作者的标准衡量。 |
| `chapter_story_architecture` / `chapter_scene_plan_candidates` / `chapter_scene_plan_fill` / `chapter_plan_review` / `snowflake_generate_scene_list` / `snowflake_generate_scene_details` / `chapter_near_final_review` | 各加一段：有「参考作者结构画像」块时按该作者的章 / 场尺度、开合方式、对白比重、章尾方式规划与评审；不复用其内容。 |

### 2.4 结构画像 `profile_json.structure_card`（Track B）

- 合成期由 `style_reference/structure.py` 从段落表确定性计算（无 LLM）：用 `segmentation/heuristic.is_title_paragraph` 找章标题；每章字数、段数、对白段占比、开章段型与首句、收章段型与末句；全书：章数、章长中位 / p10 / p90、每章段数、开章 / 收章段型分布、段型总比重、人称（取自 `voice_signature`）。无章标记 → 全书一章，画像注明「无章节标记」。
- 渲染 `render_structure_card(profile_json) -> str`（≤1,500 字，中文，**带数字**——规划层需要尺度）+ 「章首样例」「章尾样例」各 ≤3 条 × ≤150 字（原文，过 `secure_reference_block`）。
- 加入 `_FROZEN_PROFILE_JSON_KEYS`；旧画像无此键 → 不渲染。
- 另有「场景手法」块：`sub_dimensions` 里 `scene.*` + `theme.*` 的 observation 陈述（≤10 行，经 source_overlap 过滤），供规划层。

### 2.5 规划层注入点

| 节点 | 注入 | 绑定解析 |
|---|---|---|
| `scene_blueprint` | `_source_snapshot` 已带叙事机制，再加 `style_structure_card` + `style_scene_craft` | 已有 `resolve_scene_style_runtime_contract` |
| `chapter_story_architecture` / `chapter_scene_plan_*` / `chapter_plan_review` | `ChapterPlanningContextBuilder` 新 slot `style_reference` | 新 `resolve_project_style_runtime_contract(session, project_id)`（project + global 作用域，task_type `scene_generation`） |
| `snowflake_generate_scene_list` / `snowflake_generate_scene_details` | payload 新成员 `style_reference_structure`（`snowflake_prompt_budget` 按相关性可卸载，优先级中） | 同上 |
| `chapter_near_final_review` | 注入 `[STYLE_REFERENCE]` 前缀（以本章第一场解析绑定） | 现有 `inject_style_reference_prefix` |

### 2.6 小层面加码（Track C）

- `injection_budget.yaml`：`few_shot_k` 8→10、`few_shot_window_max_chars` 3500→4000、`few_shot_block_max_chars` 30000→40000；小上下文靠既有整窗口卸载。
- 新绑定默认强度 100（`_DEFAULT_INTENSITY` 与 UI 滑块默认）。
- 首稿续写式框架（§2.3）。

### 2.7 通知与审计

attempt `content_source="style_first_draft"`；notice `STYLE_FIRST_DRAFT`（信息级）；`_style_reference_runtime_audit` 加 `draft_mode`、`house_taste_gates`（每道门 `deferred|applied` 与被降级为仅记录的命中）；`styled_draft_gate_decided` 的 `stage` 允许 `neutral_draft`。

## 3. 工作包

### Track A · 场景层（串行）

**W1 开关与契约**：`injection_budget.yaml`（`draft_mode_default`、`style_first_length_slack: 0.5`、§2.6 三个数）、`runtime_contract.py`（`draft_mode` 冻结 / 校验 / `effective_draft_mode` / `is_style_bound`、`structure_card` 入冻结键）、`bundle_builder.py` 冻结处、`api/routes/style_reference.py` `ApplyConfigMixin`、tests。

**W2 首稿直起 + 复读**：`prompts.yaml`（`style_first_draft`、`style_draft` v10、`hard_qc` v3）、`scene_generation.py`（`generate_neutral_draft` 的 style_first 分支：模板 / 前缀 / drafting sections / 64000 / 节点 `style_draft` / `_style_first_length_instruction` / 修复同模板同前缀 / notice / audit；`_run_style_generation` 的 `source_label`；回退文案；`STYLED_DRAFT_GATE_STAGES` 含 `neutral_draft`）、`prompt_builder.py`（`STYLE_PASS` 组、`_task_kind_for_template`）、`context_budget.py`、`bundle_builder.latest_styled_draft_for_scene`（接受带标记的首稿为声音锚）、`api/routes/scenes.py`（`generation_summary.draft_mode`）、tests。

**W3 让位（全部）**：`_anti_template_quality_gate` 在 `style_bound` 下**整体仅记录**（不触发去模板改写；`style_anchor_audit` 保留为唯一触发）；`orchestrator` 规则版自动批评在 `style_bound` 下不发补丁；`near_final._apply_scene_near_final_gates` 在 `style_bound` 下跳过词表门与 `_missing_scene_machinery`；`final_text_gate` 在 `style_bound` 下不施加三个文学阈值（事实 / 安全门保留）；`_literary_freshness_budget` 去掉两张词表与收尾子句、`preserve_reference_repetition` 时跳过 `lifetime_banned_expressions`、n 8→14；长度带按 `style_first_length_slack` 放宽（1200–1800 → 600–2700）；`soft_qc` v5 / `near_final_acceptance_review` v4 / `scene_literary_rewrite` v4；`chapter_near_final_review` 注入前缀；tests 成对用例。

### Track B · 结构层（与 W2/W3 并行，文件不相交）

`style_reference/structure.py`（新）、`segmentation/heuristic.py` 导出 `is_title_paragraph`、`profile_synthesizer.py`（写 `structure_card`）、`scene_blueprint.py` + `scene_blueprint` v4、`chapter_planning_context.py` + `chapter_plan_llm.py`、`snowflake_workspace_llm.py` + `snowflake_prompt_budget.py`、规划类七个模板、`resolve_project_style_runtime_contract`、tests（含鲁迅公版语料上的章检测）。

### Track C · 前端（并行）

`ws-styleref.jsx`：绑定表单「起草方式」单选（默认作者手笔直起）、强度滑块默认 100；`ws-scene-run.jsx`：`current_step` 中文标签、运行日志句；vitest。

### Track D · 文档与回归

`CLAUDE.md`、`docs/README.md`、`docs/style-reference-runtime-contract.md` 附记、`docs/operator-manual.md` 一句、本文 §5；全量非 Chroma 回归 + ruff + vitest。

## 4. 验收

- `neutral_first` / 无绑定：既有测试逐字通过；提示词除版本号与「有风格块时」前提外不变。
- `style_first`（fake LLM）：首稿模板 / 前缀 / 预算 / sections / 节点路由 / 抄袭门 / 修复 / 回退 / 声音锚 / 检查点恢复；六道让位各有成对用例；结构画像在鲁迅语料上章检测正确、渲染 ≤1,500 字；规划层各节点 payload 含画像；章级评审注入前缀。
- 真实阅读 A/B（作者机器）：同一场景切 `draft_mode` 各生成一次；同一章重新规划一次看场景清单与章尾方式是否像。

## 5. 完成记录（2026-09-12）

全部落在工作区，未提交。

- **W1 开关与契约**：`injection_budget.yaml` 新键 `draft_mode_default: style_first`、`style_first_length_slack: 0.5`；
  `few_shot_k` 8→10、单窗 3500→4000、整块 30000→40000；`_DEFAULT_INTENSITY` 与预览请求缺省 50→100。契约顶层
  `draft_mode`（`runtime_contract.resolve_draft_mode` / `effective_draft_mode` / `is_style_bound`），校验只收两个取值，
  旧契约缺键 → neutral_first。`ApplyProfileRequest.draft_mode` 与决策卡 effect（`review_effects._style_injection_config`）
  同构落 `config_json`。冻结键新增 `structure_card` / `planning_guidance`。
- **W2 首稿直起 + 复读**：`style_first_draft` 模板（v1，64000，`drafting` 口径，`STYLE_PASS` 组）；`generate_neutral_draft`
  的 style_first 分支（前缀注入、`style_draft` 节点路由、修复稿同前缀、首稿过 styled-draft gate、attempt
  `content_source=style_first_draft`、notice `STYLE_FIRST_DRAFT`、审计 `draft_mode`）；neutral_first 的 attempt 明细逐字不变。
  `style_draft` v10 按来源稿标签切换重组 / 复读；回退稿标记 `first_draft_fallback`，`latest_styled_draft_for_scene` 可取首稿
  作声音锚。长度带放宽走 `_LENGTH_BAND_SLACK` 上下文变量（`_parse_numeric_length_band` 单一入口），
  `_style_first_length_instruction` 告诉模型「计划带 vs 硬范围，作者尺度优先」。`hard_qc` v3。工作台
  `generation_summary.draft_mode`，运行任务视图 `draft_mode`。
- **W3 让位（仅 style_bound）**：去模板门 `_defer_house_taste_gate`（仅记录 `advisory_findings`，`style_anchor_audit` 与安全门
  仍可触发；`_assess_de_template_rewrite(house_taste_deferred=True)` 不再用房风分数判回退）；规则版自动批评 `skip_critique`；
  `near_final._apply_scene_near_final_gates(style_bound=True)` 跳过词表门与「结尾必须是动作」；`final_text_gate` 三个文学阈值
  不施加（`house_taste_thresholds=deferred_to_reference`）；新鲜度预算去掉两张房风词表与「以动作收尾」子句，刻意复沓时跳过
  全书禁用表达表；`soft_qc` v5、`near_final_acceptance_review` v4；`chapter_near_final_review` 以本章第一场注入前缀。
- **Track B 结构层**：`style_reference/structure.py`（`compute_structure_card` / `render_structure_card` /
  `derive_planning_guidance` / `render_planning_guidance`）、`planning_context.resolve_project_style_reference`；合成期写
  `structure_card` / `planning_guidance`；蓝图 `_source_snapshot` 带 `style_structure_card` / `style_planning_guidance`，验证器
  只在有参考时接受「无」；章规划 `style_reference` slot（架构 / 候选 / 填充 / 评审）；雪花场景清单 / 场景规划
  `style_reference_structure` 成员，预算阶梯先卸样例再卸画像；七个规划模板加「按该作者的章 / 场尺度与开合方式规划」段并升版。
- **Track C 前端**：绑定表单「起草方式」单选（缺省作者手笔直起）、强度滑块缺省 100、绑定 / 叠加层显示起草方式；场景运行
  `current_step` 中文标签（style_first 下 `neutral_running` → 「首稿（作者手笔）」，运行中从任务视图 `draft_mode` 读，结束后从
  工作台读）、运行日志句更新；vitest 新增 12 例。
- **实测（鲁迅公版短篇 904 段 / 66k 字，`scratchpad/luxun_scale_probe.py`）**：强度 0 / 50 / 100 → 3 / 7 / 10 个窗口，
  约 11.5k / 22k / 28.5k 字原文进系统提示；单次渲染 ≈0.2 s。
- **验证**：新增 `tests/test_style_first_draft.py`（25 例）、`tests/test_style_reference_structure.py`（25 例）；既有套件按新契约更新
  （k 3→10、缺省强度 100、`STYLE_FIRST_DRAFT` 进封闭集、styled-draft gate 接受 `neutral_draft`、`style_draft` v10 措辞、
  新鲜度预算对照组钉住 neutral_first）。全量非 Chroma 回归、ruff、vitest 结果见下一条。
- **回归**：全量非 Chroma 回归两片（2026-09-12）：shard 0 = 1487 通过 / 4 跳过，shard 1 = 1318 通过 / 1 跳过；首轮各有 1–2 个失败，全是预期值更新（`test_llm_task_runner` 的 runner 调用点名单改为 `_generate_first_draft` / `_run_style_generation_inner`；`test_style_reference_injection_layers` 的缺省强度总额 1650 / 2228 → 2400 / 3240），修正后单文件重跑通过；ruff 全绿；前端 vitest 41 文件 372 例通过，`npm run build` 成功。本机无真实模型，未生成实际文稿。

## 6. 风险与回退

- 事实保真：首稿同时顾事实与手笔，守住的是确定性检查 + 一次修复 + hard_qc 不变；阅读发现事实错误增多 → 绑定切 `neutral_first`。
- 抄袭：首稿离原文更近；首稿也过 12 字 n-gram 门与禁用词门。
- 房风门全部让位后，真正的模型腔只剩 soft_qc（带样例）与近终稿评审（带样例）两道模型判断兜底——这是有意为之：参考是唯一的风格权威。
- 成本：调用次数不变；首稿输入最多约 64k token；规划类提示各增 ≤2k token。
- 部署：`sync_prompt_templates --execute`（14 个模板）；无新节点；`injection_budget.yaml` 不进配置快照。
- 全局回退：`draft_mode_default: neutral_first` 一行。

## 7. 明确不做

单次起草（跳过复读）、Best-of-N 保真选优（P4）、逐场手法卡的 LLM 抽取（P3 的 LLM 部分）、微调（Step 3）。
