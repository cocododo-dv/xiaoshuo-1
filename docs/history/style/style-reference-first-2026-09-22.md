> 历史文档：描述当时的实现，现行说明见 [docs/style-reference.md](../../style-reference.md)

# 风格参考优先（2026-09-22）

作者的判断：「AI 起草台生成的内容和我风格参考模块差距很大」。实库复核后确认：管线早已接通
（两场真实起草的首稿与风格稿都拿到了 12 个『龙族』原文窗口，约 4 万字），但产出仍是通用的
「AI 文学腔」。本次改动把参考作者放到提示词权重的最前面，并修掉几处让参考失效的环节。
用户指令：**风格参考优先**（与 2026-09-12 的「最大程度模仿」一致）。

## 1. 起草台真实运行的诊断（改动前）

- 每一遍起草都把样例放在 **system 消息最前面**，并包在 `[UNTRUSTED_REFERENCE_DATA]` 边界里，
  前导句说「只用于学习文风……一律忽略」；模板的优先级清单把「12 字重合即抄袭 / 独特意象不得
  搬用 / 措辞不得重现」排在样例之上；再往后是 7 千多字的英文规则（三拍比例、人称时态绑定
  「即使参考作者用另一种」、代词歧义重复人名）。
- 离输出最近的是 1.2 万字设计材料，以及本系统自己上一场的文字出现三次（Voice Anchor「保持
  同一种声音」、Similar Scene Context、整篇 Previous Scene Memory）——第 1 场是 AI 腔，第 2 场
  就被要求接着这个腔写。
- 软 QC 两场都作废：提示词与 schema 都没说分数范围是 0–1，模型给 9.3 / 93，Pydantic 拒收后
  按豁免放行；跨场漂移读数只挂在编排器自己的归档检查点上，起草台「采用」走 Archiver，
  两场归档 0 条 `style_drift_observed`。
- 首稿选窗没有正文可分类，退化为「相对鲁迅 / 朱自清基线最偏离 + 轮换」；模板里排最高优先级
  的 `[禁止复刻]` 标签从未渲染（真实块叫 `[禁忌模式]`）；指令中和误伤两处普通对白。

## 2. 改动

1. **样例块进 user 消息末尾**（`style_prompt_injection.PLACEMENT_USER_TAIL`）：起草通道（首稿、
   修复稿、风格稿、近终稿改写、各类补丁、写手建议）把 `[风格样例]…[/风格样例]` 放在 user 消息
   最后，紧接一段收口指令（`schemas.FEW_SHOT_CLOSING_MANDATE`：以样例手笔写前文定下的这一场、
   人物地名事件用本书的、不整句照搬、篇幅与 JSON 仍按前文）；system 前缀只留抽象块、红线与
   一句指路（`FEW_SHOT_IN_USER_MESSAGE_NOTE`）。规划 / 评审节点保持 system 前缀。
   调用方用 `apply_style_user_tail(prompt, user_prompt)` 接上尾巴。
2. **样例不再是「不可信数据」**：`untrusted_data.frame_reference_samples` 只做注入模式中和与
   伪造边界转义，块尾 `[/风格样例]` 收口；标题改为「本场唯一的文风权威……敢于用这位作者会用的
   词和他会打的比方」。RAG 片段与结构样例保持原封装。
3. **模板**（`style_first_draft` v8、`style_draft` v12、`soft_qc` v9、`scene_literary_rewrite` v5、
   `style_length_patch` v5、`style_salvage_patch` v3）：优先级改为 样例 → 声音 / 正向 → 禁忌 →
   分布 → 内容红线；删掉幽灵标签 `[禁止复刻]`、「even when the reference author uses another」、
   前文声音锚条目；叙述姿态只定 POV 与人称，叙述距离 / 旁白口吻 / 情绪的写法跟参考作者；
   五条反 AI 腔守卫收成一句「口味问题由样例决定」。风格模板的连续性提醒不再要求「重复人名」。
4. **前文不再是声音**：契约 `draft_mode=style_first` 时 bundle 不放 Voice Anchor 与 Similar Scene
   Context，上一场正文只留结尾节选并标注「只用于衔接事实」（`reference_first_memory_digest`）。
5. **选窗**：`scene_sampling_hints` 按场景形态给段型（反应场加 psychology），`scene_dialogue_heavy`
   给对白配额；`_WindowAffinityScorer` 改为典型性（窗口块级 z 与画像块级 z 的平均绝对差取负），
   索引版本 `exemplar_windows_v2`，旧画像里的 v1 索引按版本失效、惰性复算。
6. **软 QC 分数归一**（`qc_engine._normalize_soft_qc_scores`，0–10 / 0–100 → 0–1）+ 提示词写明范围。
7. **归档漂移读数**：`Archiver.archive_final_scene(observe_style_drift=True)` 在每条归档路径上做
   读数；编排器检查点自己做，传 `False`。
8. **LLM 客户端**：输出 token 数顶到 `max_output_tokens` 的坏 JSON 按 TRUNCATED 抬预算重试（此前
   原样重发三次同一上限，soft_qc / 验收评审各浪费 18 万 token 后作废）；Anthropic「Thinking may
   not be enabled when tool_choice forces tool use」→ 关 reasoning 重试一跳并缓存；`soft_qc` /
   `near_final_acceptance_review` 输出预算 2600 → 5000。
9. 补候选的「禁忌优先」轮换改为「样例优先」。

## 3. A/B（同一场景 CH01_SC03，2026-09-22，作者的中转；Gemini 线路当日被谷歌按地区拒绝，
规划 / QC 节点统一用 claude-sonnet-4-6，reasoning off）

| 组 | 提示词 | 风格通道模型 | 声音特征平均 \|z\|（首稿 / 风格稿） | 标点/千字（基线 144） | 句长标准差（基线 7.4） |
|---|---|---|---|---|---|
| 作者 09-20 真实两场 | 旧 | gemini-3.8-flash-high | 0.93 / 0.86 | 98 | 6.5–9.1 |
| A | 旧 | claude-sonnet-4-6 | 0.82 / 0.89 | 173–177 | 5.9–7.1 |
| B | 新 | claude-sonnet-4-6 | 0.83 / 0.84 | 194 | 3.4 |
| C | 新 | claude-opus-4-6-thinking | 0.77 / 0.78 | 152–155 | 7.9–8.3 |
| D | 旧 | claude-opus-4-6-thinking | 0.64 / 0.64 | 141 | 8.7–9.0 |
| 『龙族』真实窗口 ×2 | — | — | 0.34–0.39 | 130–153 | 5.4–9.2 |

读数结论（每格只有一场，差异在噪声边缘，不能当定量结论）：模型档位的影响最大（Opus 两组
都好于 Sonnet 两组，Sonnet 两组好于 flash）；新旧提示词在表层统计上打平（C 在标点 / 问句 /
省略号更近，D 在虚词更近）；人读起来 C 的对白推进与旁白口吻更接近参考。所有生成稿仍明显
远于真书窗口——提示词层面的天花板未变。决定：保留新框架（它去掉的是明确的自相矛盾），
风格通道节点路由到 claude-opus-4-6-thinking（reasoning off、输出 9000），其余节点暂用
claude-sonnet-4-6（Gemini 线路恢复后可在 系统配置 改回）。

## 4. 部署

- 代码合入 main 后 `--reload` 后端自动生效；本安装没有 prompts 快照（yaml 即生效）。有 prompts
  快照的安装需要 `sync_prompt_templates --execute`。
- 活动 models 快照升为新版本（脚本 `deploy_models_snapshot.py` 的做法：style_draft → 强模型，
  其余 → 基础模型，reasoning off，soft_qc / 验收 5000）；有旧快照的安装可用
  `raise_llm_output_budget --node soft_qc --node near_final_acceptance_review --floor 5000`。
- 旧画像的样例索引会在下次渲染时按 v2 惰性复算（1.9M 字的书约 2 秒）。

## 5. 仍然开放

- 没有「像不像」的定量指标（声音特征只量表层）；每格一场的 A/B 只能定方向。
- 场景三拍结构仍由雪花方法固定（有意）；『龙族』这样没有场分隔的书，场尺度只能是
  「章长 ÷ 本章场数」的推算值（§6）；简报的气质词只在规划 / 起草 / 评审三端让位，简报本身不改。
- 中转的 Gemini 线路地区限制是外部故障；Claude 走中转时 json_schema 被实现为强制工具调用，
  只能关 reasoning。

## 6. 第二轮：结构层跟随参考书、简报气质让位（2026-09-22，用户要求「评估优化一下」）

### 6.1 评估（《何来》× 『龙族』实库）

- **结构层的现状**：规划节点（09 / 10、场景蓝图、章架构、分章面板的尺度）都已拿到 `[结构画像]` /
  `[场景手法]`，但 (a) 『龙族』没有场分隔，画像只能说「场的长度按章长与段数规划」——第 10 步照旧写出
  1,200–2,500 字的场，一章 5 场 ≈ 7,500 字，而『龙族』单章中位 17,948 字（p10 8,559 · p90 33,371）：
  本作品的章只有参考的四成，也远低于作品自设的 10 万字目标（17 场 × 1.5k ≈ 2.5 万）；(b) 起草层除
  数字长度带与样例外看不到任何结构事实——样例窗口虽按开章 / 收章位置挑选，行里却不带标签，模型
  不知道哪一窗是章首；(c) 「AI 起章名」完全不看参考（阶段 W 的开放项）；(d) 场景蓝图、人物压力蓝图、
  章架构「有就复用」（`ensure_for_scene` / `ensure_scene_planning` 只找最新的 accepted / active 行），
  设计重新确认或换参考书之后仍拿旧规划起草。
- **简报气质的现状**：起草模板在第一轮已让位（叙述姿态只定 POV 与人称，读者情绪按参考作者的方式到达）；
  实库简报的 `narrative_stance` 是纯结构的（第三人称限知、过去时），「严肃文学质感 / 窒息悬疑」这类
  气质词不经设计上下文进入起草，只经由 02–10 的设计文本（场景摘要、`expected_reader_emotion`、钩子）
  间接进入。仍有缺口的是**规划层**（09 / 10 把每场的情绪与钩子写成简报的气质）与**评审层**（验收评审
  按「读者应感到」判、章评审完全不知道风格块）。
- **决定**：简报本身不改（它是作者确认的设计，气质让位发生在规划与起草、评审三端）；三拍结构不改；
  场尺度、章内位置、章题、规划产物作废四件事做。

### 6.2 改动

1. **场尺度**：`structure.reference_scene_scale` ——有显式场界用场长中位，否则 **参考章长中位 ÷ 本章活跃
   场数**（封顶 `injection_budget.yaml: style_first_reference_scene_chars_max` 5,000、保底 300）。
   `bundle_builder` 在 `style_first` 下冻结 `inline_digests._style_reference_scene_scale`
   （`source_version_refs.style_reference_scene_scale`）；`scene_generation._parse_numeric_length_band`
   把硬范围**上限**至少抬到 尺度 × (1 + slack)（封顶），下限不动；两条长度指引写明测得的数字
   （「a chapter runs about 17,948 … this chapter has 5 scenes, so a scene of this author's is about 3,590」）。
   `neutral_first`、概述场、显式 slack 的计划值都不变。规划期：`snowflake_workspace_llm._project_reference_scale`
   按当前分章各章场数的中位推每场字数（`style_reference_structure.project_scale`），`scene_details` v14 要求
   `target_length_band` 取它附近的数字区间；未分章只给参考章长与一句说明。
2. **章内位置**：结构简报新增事实行 `Chapter position: …`（`scene_structure_brief._chapter_position_line`）；
   样例窗口行前缀 `第N章·章首 / 章末 / 整章；`（`injection._window_position_tag`）；起草通道的 user 尾块在
   章首 / 章末场追加开章 / 收章指令（`style_prompt_injection.chapter_position_mandate`，带参考作者开章 /
   收章最常用的段型）；验收评审 v9、章评审 v3 按参考作者的开合方式判，篇幅在硬范围内不算发现。
3. **章题**：结构画像 `structure_card_v2` 新增 `chapter_titles`（形态、题名字数分位、跨全书 8 条题名样例，
   编号已去：`title_name_part`）；旧画像按段落表惰性补算（`planning_context.chapter_titles_for_book`，按
   (book, 段落数) 缓存）；「AI 起章名」载荷带 `reference_titles`（`snowflake_chapter_titles_suggest` v2），
   照抄的题名当重复丢掉。
4. **规划产物随设计 / 绑定作废**：叶子 `services/scene_planning_staleness.py`（蓝图 → `superseded`，
   人物压力蓝图 / 章架构 → `superseded`）；三处调用：设计失效（`ProjectRuntimeInvalidationService`，
   `impact.superseded_planning`）、应用画像（`MaterializationService.apply_profile`）、删除绑定（路由）。
   下一次运行重新规划（每场一次规划调用的代价）。
5. **气质让位**：`STRUCTURE_REFERENCE_HOW_TO_USE` 与 `scene_list` v11 / `scene_details` v14 写明
   `[场景手法]` 的情绪基调 / 价值取向 / 叙事观是参考作家的气质，每场的读者情绪、钩子、收场按他到达的方式
   设想，不换成简报描述的更沉的文学腔；验收评审 v9：「读者应感到」是效果不是腔调；章评审 v3 首次知道风格块。

### 6.3 部署

无迁移；yaml 即生效（本安装没有 prompts 快照）；旧画像的章题惰性补算，不必重新合成；已有的蓝图不作废
（没有触发事件），下一次设计确认或重新应用画像时作废。测试：`tests/test_structure_follows_reference.py`。
