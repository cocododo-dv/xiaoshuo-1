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
- 场景 / 章节结构仍由雪花方法固定，参考作者的章节形状进不来；书简的气质（严肃 / 戏谑）
  仍是绑定事实。
- 中转的 Gemini 线路地区限制是外部故障；Claude 走中转时 json_schema 被实现为强制工具调用，
  只能关 reasoning。
