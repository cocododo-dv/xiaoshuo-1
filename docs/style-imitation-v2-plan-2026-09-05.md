# 风格模仿 v2 执行方案（2026-09-05）

> 依据：`docs` 外的评估报告（2026-09-05，commit 24142c3）。本方案只针对报告「做不到」清单的前三项：
> **A 声音级模仿、B 叙事层迁移、C 跨场景 / 整章一致性**。
> 明确不做：作者判别器与「像不像」度量体系（盲评、基准 CLI、对比式评委）、多作品 / 作家级建模、长文续写循环、Best-of-N 恢复。
> 分支：`feature/style-imitation-v2`。本文件是实施代理的唯一规格；与代码冲突时以本文为准并回写本文。

## 0. 为什么现在做不到，v2 怎么解

| 目标 | 现状根因（已核实） | v2 手段 |
|---|---|---|
| A 声音级模仿 | 抽取陈述禁含原文词；量化层无虚词 / 助词 / 逗号节律 / 引导句 / 四字格；合成预算 5000 把上百条观察压成十几句甚至失败；默认注入只剩 5 条规则 + 3 段 130 字样例；强度 ≥58 饱和；过滤器删掉最像作者节拍的句子 | ① 修通管道（合成预算、注入预算与强度语义、截断与过滤）；② 新增**确定性声音签名**（内容安全的虚词 / 标点 / 引导句 / 四字格 / 叠词 / 人称特征）并渲染成生成器可执行的 `[声音特征]` 块；③ **原文样例成为主信号**：连续多段窗口、按场景段型匹配、按辨识度选段、数量随强度缩放 |
| B 叙事层迁移 | neutral_draft 硬性不见画像，style_draft 只能在「不可变事实骨架」上上色；规划层零感知 | ① 合成期生成 `narrative_guidance`；② 作为 bundle section 注入 **neutral_draft** 与 **scene_blueprint**；③ style_draft 放宽为「事实与因果不变，取舍 / 停顿 / 信息释放可重排」并保留既有事实守卫 |
| C 跨场景一致性 | 续写重注入与漂移检测已退役；每场一次性注入；新鲜度预算与反模板门会抹平作者标志性复沓 | ① 前一场成稿尾部作为 `[前文声音锚]` 进入下一场 style_draft；② 归档期用声音签名做**确定性漂移读数**（写 MetricEvent），下一场 bundle 渲染 `[漂移校准]`；③ 新鲜度预算对纯虚词 / 标点 n-gram 与画像标记的刻意复沓豁免 |

## 1. 共享契约（所有工作包必须遵守）

### 1.1 `profile_json` 新键（合成期写入，冻结进运行时契约）
```jsonc
"voice_signature": {                 // W3 产出，W1 落库
  "version": "voice_signature_v1",
  "features": { /* 见 §2.W3，全部 float，键名稳定 */ },
  "habits": [ "常用连接词：却、便、又；少用：然而、于是", "...≤12 行" ],
  "deliberate_repetition": false      // 叠词/短句连打显著高于基线时为 true
},
"narrative_guidance": [ "关键信息放段首一次给出，之后不回头解释", "...≤8 行" ],  // W1 确定性派生
"anchor_quotes_used": 12             // W1 审计：合成时随 payload 送了多少条锚引文
```
`runtime_contract._FROZEN_PROFILE_JSON_KEYS` 追加 `voice_signature`、`narrative_guidance`。旧画像没有这些键时一切路径必须优雅退化（不渲染对应块）。

### 1.2 `SystemPromptFragments` 新字段
`voice_block: str = ""`（渲染自 `voice_signature.habits`，标题 `[声音特征]`）。`to_system_prompt_prefix()` 顺序：`metric → voice → positive → forbidden → few_shot → rag → anti_plagiarism`。

### 1.3 bundle 新 section（`context_budget.SECTION_SPECS` + `bundle_builder` 同步登记）
| slot / digest_key | 标签 | neutral_draft 可见 | style_draft 可见 | 来源 |
|---|---|---|---|---|
| `style_narrative_guidance` | Style Reference — Narrative Mechanisms | **是** | 是 | 冻结契约里所有层的 `narrative_guidance` 合并去重，≤8 行 |
| `previous_scene_voice_anchor` | Previous Scene Voice Anchor (own prose; keep the same voice) | 否 | 是 | 同章上一场最新 style/de_template/style_patch 稿尾部 ≤`continuity_anchor_max_chars` 字；没有则上一章最后一场；没有则不注入 |
| `style_drift_calibration` | Style Drift Calibration | 否 | 是 | 本章最近的 `style_drift_observed` 事件 `calibration_lines`，≤`drift_calibration_max_lines` 行 |

`NEUTRAL_DRAFT_STYLE_SECTIONS` 追加 `previous_scene_voice_anchor`、`style_drift_calibration`（neutral 不看），**不得**加入 `style_narrative_guidance`。

### 1.4 配置键（`config/style_reference/injection_budget.yaml`）
```yaml
system_prompt_max_tokens: 2400        # 抽象四块（metric+voice+positive+forbidden）在 intensity=100 时的总上限（字符）
intensity_min_total_chars: 900        # intensity=0 时的总上限；中间线性
positive_block_ratio: 0.45
forbidden_block_ratio: 0.20
metric_anchor_block_ratio: 0.15
voice_block_ratio: 0.20               # 四项合计 1.0
few_shot_k: 6                         # intensity=100 的样例窗口数
few_shot_k_min: 2                     # intensity=0；k(i)=round(k_min+(k-k_min)*i/100)
few_shot_block_max_chars: 3600
few_shot_window_paragraphs: 3         # 每个样例为连续 1–3 段的窗口（含对白往返）
few_shot_window_max_chars: 900
few_shot_paragraph_max_chars: 700
layered_total_scale_per_layer: 0.35   # 多层：总额 ×(1+0.35×(层数-1))，上限 ×1.7；样例只取最具体层，不丢弃
continuity_anchor_max_chars: 900
drift_calibration_max_lines: 3
```
旧键保留兼容（`few_shot_quote_max_chars` 等），rag_* 不变。`_DEFAULT_BUDGET` 同步。

### 1.5 intensity 语义（所有策略一致）
`intensity∈[0,100]` 控制两件事：抽象总额 `total = min_total + (max_total - min_total)·i/100`，样例窗口数 `k(i)`。A 策略不含样例（定义如此）但同样按 total 截断；B / C / MIXED 按 k(i) 取样例。UI 读数改为消费预览端点返回的真实行数 / 样例数。

### 1.6 MetricEvent
`event_kind="style_drift_observed"`，`target_kind="scene"`，`target_ref_id=scene_id`，`profile_id`，`context={"chapter_id","scene_seq","features":{名:{"value","baseline_mean","baseline_std","z"}},"deviations":[{"feature","direction","z"}],"calibration_lines":[...],"drift_ptype_priority":[...]}`。只在 |z|≥1.5 的特征上生成校准行。

## 2. 工作包

文件所有权互斥；同一阶段并行的包不得改对方文件。每个包必须：写 / 改单测并跑本包测试（`--basetemp` 唯一）、跑 `tests/test_metadata_isolation.py`（若动 ORM）、更新本文 §5 的完成勾选。

### W1 · 合成：让完整抽取进得去、出得来（文件：`profile_synthesizer.py`、`errors.py`、`config/prompts.yaml` 仅 `style_ref_synthesize_profile`、`runtime_contract.py` 仅 allowlist、对应 tests）
1. `style_ref_synthesize_profile.input_token_budget: 5000 → 39000`（实施时实测：满载 payload 在估算器口径下为 31,763 token，×1.2 取整；原拟 20000 会把陈述压到 56 字，正是要消灭的现象）；测量分项已写进模板注释。
2. `_fit_synthesis_payload_to_budget` 改为全局分级降级：① 全量；② 去掉非必需指标（保留 `_METRIC_REQUIRED_ORDER` 对齐的必需项）；③ 全体 statement 统一压到 80、56 字；④ 按置信度 / 证据数轮转丢弃多余 finding，但**每子维保留 ≥1 observation 且 ≥1 forbidden_pattern（若存在）**；仍装不下才失败。审计字段保留。
3. payload 新增 `anchor_quotes`：每子维 ≤2 条、每条 ≤60 字的代表引文（来自 evidence，run-scoped），提示词明令「只作机制锚点，不得复述进任何输出字段」；输出仍过 `_contains_source_overlap`。
4. 确定性派生 `narrative_guidance`：`narrative_patterns` ∪ `sub_dimension` 以 `narrative.` 开头的 forbidden statements，去重、过滤原文重合，≤8 行。
5. 把 W3 提供的 `voice_signature.compute_voice_signature(paragraph_texts)` 与 `render_voice_habits(...)` 产物写入 `profile_json.voice_signature`；W3 未就绪时以 `try/except ImportError` 跳过并留空（W3 完成后删掉兜底）。合成 payload 附 `voice_habits`（≤12 行）供 LLM 对齐 style_features。
6. `SynthesizeError` 改为同时继承 `DomainError`：code `STYLE_REFERENCE_SYNTHESIZE_FAILED`，status 409，`details.reason_code∈{budget_unfit, empty_profile, source_overlap, text_integrity, llm_failed}`，附 `author_action`（回到维度矩阵 / 重试合成）。路由无需改。
7. 测试：用 **真实模板**（`load_prompt_templates()`）与 16 子维 × 6+2 条 × 120 字构造 payload，断言不失败、每维 ≥1 obs + 1 fp、审计字段正确；断言 `narrative_guidance` 派生；断言 409 映射。

### W2 · 输入保真（文件：`text_utils.py`、`segmentation/heuristic.py`、`extractors/base.py`、`sampling.py`、`run_orchestrator.py`、`api/routes/style_reference.py` 仅两个 import 路由、`tests/golden/style_reference/regen_expected.py` 及 expected、对应 tests）
1. `split_sentences`：紧随句末标点的闭引号 `”’」』` 归并前句；ASCII `.` 只在后随空白 / 行尾时切；不产生纯标点片段。`style_signature.py` 若自带分句正则，改为复用（属 W3 文件，由 W3 处理，W2 只改 text_utils）。
2. 启发式分类：`_DIALOGUE_QUOTES` 补 `‘’`；「含引号即对话」改为「引号内字数 ≥ 段落一半或以 `说：/道：/问：` 引导」；`<30 字` 不再一律 transition：无切换词的短段继承前一段类型（`narration` 兜底），只有含 `次日/回到/与此同时/后来` 等切换词或章节标题形态才判 transition。
3. 导入路由：两个 import 路由改用 `_get_llm_client_and_enabled()`（与 reclassify 一致），按 cloud_policy 自动选 LLM / 启发式，`stats_json.classification.fallback_to_heuristic` 如实记录。
4. `_build_metrics_anchor` 改读 `book.stats_json.metrics` / `prose_shape_metrics`（全书真值），子集规则不变；`extraction.yaml` 的 `use_all_paragraphs` 注释对齐。
5. rng：`RunOrchestrator` / `BaseExtractor` 以 `sha256(text_checksum + run_id)` 定种。
6. 采样：`language` / `scene` 层按段型真实分布比例分配名额（每型下限 1），`narrative` / `theme` 层改抽 **连续窗口**（3–6 个相邻段）并在 payload 里带 `paragraph_index` 与窗口边界；样本量随 `total_chars` 分档：`<50k` 用现值，`50k–200k` ×1.5，`>200k` ×2（上限 60 段 / 子维）。
7. `_extract_with_retry` Step 3 捕获 `_ExtractLLMError`，重抽失败时保留首抽有效 findings 而非整 run FAILED。
8. 回归：黄金 expected 重生成一次并在 commit message 里说明原因；新增分句（闭引号）、启发式（‘’ 对白、短叙述句）、锚点等于全书值、rng 可复现的测试。

### W3 · 声音签名（新文件：`services/style_reference/voice_signature.py`、`config/style_reference/voice_baseline.yaml`、`config/style_reference/function_words.yaml`；修改：`ingest.py` 写 `stats_json.voice_signature`、`style_signature.py` 分句复用、对应 tests）
纯函数、无新依赖、只用闭类词与标点（内容安全）。`compute_voice_signature(texts: list[str]) -> dict` 输出 `features`：
- 虚词：按 `function_words.yaml` 分组（结构助词 / 体标记 / 连词 / 副词 / 介词 / 代词 / 语气词 / 文言词）的每千字密度 + 组内 top-5 词及相对频率；
- 句末助词分布（吧呢啊嗯哦呀罢嘞么吗 + 也矣焉哉）；
- 标点：逗号、顿号、冒号、分号、感叹号、问号、省略号、破折号、引号对 每千字；
- 句长：均值、标准差、p10 / p90、短句连打（连续 ≤8 字句）平均长度、lag-1 自相关；
- 段落：段均字数、单句段占比、对白段占比；
- 对白引导：前置（X说：“…”）/ 后置（“…”X说）/ 无引导 三分比例；引导动词偏好（说 / 道 / 说道 / 问 / 答 / 其他）；
- 四字格密度（被标点或空白分隔、恰好 4 个 CJK 字的片段）、叠词（AA / AABB / ABAB）密度；
- 人称：第一 / 第二 / 第三人称代词占比；
- 词汇：2k 字窗口字级 TTR、二元组 hapax 比例。
`render_voice_habits(features, baseline) -> list[str]`：对照 `voice_baseline.yaml`（用 `tests/golden/style_reference/corpus` 全部文本计算的分位数，作为「一般中文小说」基线；实现时生成并写入文件，注明来源）输出 ≤12 行生成器可执行的中文习惯句，只说方向与具体词（如「连接词多用却、便、又，少用然而、于是」「对白多无引导词；有引导词时置于引语后，用道不用说道」「逗号密集、句号稀疏，一句常含三到四个停顿」「四字格偏低，不堆成语」），**不出现精确数字**。
`deliberate_repetition`：叠词密度或短句连打显著高于基线（≥p85）为 true。
`compute_voice_signature_for_text(text)` 供生成侧 / 漂移复用。测试：黄金语料两位作者的签名在引导句、虚词、四字格上可区分；空文本、纯标点安全；渲染无数字。

### W4 · 注入重写（文件：`injection.py`、`schemas.py` 仅 fragments 字段与预览响应、`injection_budget.yaml`、`rag.py` 仅 `ensure_rag_index` 调用点与 query 构造、对应 tests）
1. §1.4 / §1.5 的预算与强度语义；`_apply_budget` / MIXED 分支合并为一套 `_allocate(total, ratios)`；A / B / C 同样消费 intensity。
2. `voice_block` 渲染：`profile_json.voice_signature.habits` → `[声音特征]`；缺失则空。
3. `_truncate_lines`：孤立标题（以 `]`、`:`、`：` 结尾或以 `[` 开头的单行）置空；禁止字符级回退，宁可整块少一行。
4. `_is_metric_domain_guidance` 退役为 `_soften_quantitative_guidance(line)`：剥除数字与绝对量词（总是 / 至少 / 每千字 / 比例 / 占比…→ 相对措辞），保留机制；只有与冻结基线**方向相反**的频率断言才丢弃（复用 `_metric_direction` 判断）。概述行同样处理，不再整段删除。
5. few-shot：候选改为**窗口**（以 quote 所在段为中心的 1–3 个相邻段，含对白往返，≤`few_shot_window_max_chars`）；排序键 `(场景段型匹配, 辨识度, 完整度)`——辨识度 = 该窗口声音签名在「画像相对基线偏离显著的特征」上的偏离幅度（复用 W3 特征），而不是最接近均值；同一段型可多条；k(i) 控制数量；`cloud_llm_allowed` 守卫不变。
6. 多层：总额按 §1.4 放大；`_cap_fragments` 不再丢 `few_shot` / `rag`，取最具体层的样例；同画像跨作用域去重（按 profile_id）。
7. RAG（C）：`_render_rag` 前调用幂等 `ensure_rag_index`（memory 后端重建）；query 改为「画像声音签名代表向量 + 场景段型过滤」，只有存在已风格化前文时才用前文签名；空召回写 `rag_outcome=unavailable` 进 audit。
8. 预览端点响应增加 `stats: {positive_lines, forbidden_lines, metric_lines, voice_lines, few_shot_windows, few_shot_chars, total_prefix_chars}`（供 W7 UI 读数）。
9. 测试：真实规模画像下 intensity 0 / 50 / 100 三档内容单调且互不相同；A / B / C / MIXED 均消费 intensity；多层前缀信息量 ≥ 单层且无半截行 / 空标题；voice_block 渲染与缺失退化；过滤器保留「短句主导」类机制句；few-shot 输出为多段窗口且总长受控。

### W5 · 提示词与生成链路（文件：`config/prompts.yaml` 除 W1 模板外、`context_budget.py`、`bundle_builder.py` 仅 §1.3 三个 section 的登记与读取、`scene_generation.py`、`qc_engine.py`、`scene_blueprint.py`、新文件 `services/style_reference/narrative_guidance.py`、对应 tests）
1. `narrative_guidance.py`：`collect_narrative_guidance(contract) -> list[str]`（合并各层 `narrative_guidance`，去重，≤8）与 `render_section(...)`。
2. bundle：登记 §1.3 三个 section；`previous_scene_voice_anchor` 取同章上一场最新 `style_draft/de_template/style_patch` 稿（`SceneDraft`）尾部；`style_drift_calibration` 读取 W6 的 MetricEvent（W6 未就绪时为空，接口先定：`latest_drift_calibration(session, chapter_id, before_scene_seq) -> list[str]` 放在 `narrative_guidance.py` 旁的 `style_continuity.py`，由 W6 实现读取端也可——**约定：W5 只写调用点，W6 实现函数**）。
3. `context_budget.py`：`SECTION_SPECS` 追加三项；`NEUTRAL_DRAFT_STYLE_SECTIONS` 追加两项（见 §1.3）。
4. `prompts.yaml`：
   - `neutral_draft`：加一段「If a Style Reference — Narrative Mechanisms section is present, follow it for what to reveal first, where to pause, how much to disclose, and how time moves; keep diction neutral」；
   - `style_draft`：删除 “Style Feature Contract” 七维与已移除层的引用；显式列出真实块及消费顺序（禁忌 > 声音特征 > 正向机制 > 软分布 > 样例只学句法 / 节奏 / 换段，不抄内容 > 前文声音锚 > 漂移校准）；把 “immutable event-and-fact scaffold” 放宽为「事实、因果、结局功能、必含项不变；句序、停顿位置、信息释放顺序、段落取舍可按参考机制重排；不新增事件」；无绑定时说明「无风格参考，只做去中性化的自然改写」；
   - `soft_qc`：删除七维打分，改为「若提示中存在 [STYLE_REFERENCE] 块则对照其 [声音特征] / [正向风格特征] 检查偏离」——并由 `qc_engine` 在 soft_qc 阶段把同一前缀注入（复用 `_inject_style_reference`，task_type 不变）；
   - `scene_blueprint`：加「If Style Reference — Narrative Mechanisms is present, let it shape information_release, pacing and ending_action」；`scene_blueprint.py` 的 `_source_snapshot` 注入该 section。
   - 附注（W8，2026-09-06）：style_draft 实际措辞保留 “immutable event-and-fact scaffold only for what happened”（兼容 `tests/test_scene_generation.py` 与 `tests/test_prompt_builder.py` 的断言）；放宽语义由紧随其后的 “sentence order, pause placement, information-release order, and paragraph selection may be rearranged when the reference mechanisms call for it” 承载。
5. `qc_engine.py`：hard_qc 上的 `_apply_style_validation_gate(neutral)` 只保留确定性 n-gram 抄袭（Q0）；新增 **styled-draft gate**：style_draft 落库后对 `style_content` 跑 plagiarism + 冻结 banned_terms（命中 → 走既有 human_review 升级路径），quant 结果只记诊断。
6. `scene_generation.py`：style_draft 因长度 / 必含项被拒而回退中性稿时，在生成结果与场景运行 API 返回中加入 `notices=[{"code":"STYLE_DRAFT_FALLBACK_NEUTRAL","message":...}]`，并在 `_style_reference_runtime_audit` 记 `outcome`；注入前缀是否命中也进入同一 `notices`（`STYLE_INJECTION_MISS` / `STYLE_INJECTION_DEGRADED`）。
7. 测试：neutral prompt 含叙事块且不含语言层块与样例；style_draft 第二场含前文声音锚；soft_qc 收到前缀；styled-draft gate 抄袭命中升级；fallback notice 存在。

### W6 · 漂移读数与新鲜度豁免（文件：`scene_archive_effects.py`、`bundle_builder.py` 仅 `_literary_freshness_budget` 与 W5 预留的读取调用、新文件 `services/style_reference/style_continuity.py`、`metrics_recorder.py` 若需、对应 tests）
1. `style_continuity.py`：`observe_style_drift(session, scene, final_text, contract) -> dict`：用 W3 `compute_voice_signature_for_text` 与冻结基线（`voice_signature.features` + `metrics_baseline`）计算 z 值；|z|≥1.5 的特征生成 ≤3 行校准句（方向性、无数字），并给出 `drift_ptype_priority`；写 MetricEvent（§1.6）。`latest_drift_calibration(session, chapter_id, before_scene_seq)` 读取端。
2. `scene_archive_effects._detect_and_store_style_drift` 由 no-op 改为调用上项（无契约 / 无画像时返回 `no_op`）。
3. 新鲜度豁免：`_literary_freshness_budget` 生成的 `avoid_*` 列表剔除仅由虚词 / 标点组成的 n-gram（用 W3 的 function_words）；当任一层画像 `voice_signature.deliberate_repetition=true` 时，在预算里加 `preserve_reference_repetition: true` 并在 `style_draft` 提示里对应说明（W5 已预留一句：“If the freshness budget marks preserve_reference_repetition, keep the reference's deliberate cadence”——W5 写入）。
4. 测试：漂移事件写入与读取；连续两场第二场 bundle 含校准行；豁免逻辑。

### W7 · 前端（文件：`frontend-react/src/ws-styleref.jsx`、`ws-styleref.css`、`ws-settings-ai.jsx`、新增 vitest）
1. 抽取完成 / 失败 / reclassify / 删书后强制 `srLoadDeep(bookId, {force:true})`；合成按钮在「有新 run 或画像 stale」时允许再合成，画像页显示 status / stale 与「重新合成」。
2. 维度选择器改为按 `deep.profile.profile_json.sub_dimensions`（或 `book.stats_json.input_assessment`）动态生成，删除静态 `SR_LAYERS` 的 skip 硬编码。
3. 强度滑块刻度改为「轻 / 中 / 强」，读数改为消费预览端点 `stats`（规则行数、样例窗口数、总字数）；删除虚构公式。
4. 只保留 `scene_generation` 任务卡；删除设置页无消费方的「允许引用参考画像 / 复刻检查严格度」；导入入口只列 txt / md；同作用域重复绑定时提示「将遮蔽已有绑定」。
5. 新增 vitest：缓存失效、再合成可点、读数来自预览。`npm test` 与 `npm run build` 通过。

### W8 · 文档与配置收尾（文件：`CLAUDE.md` Style Reference 段、`docs/README.md`、本文 §5、`docs/style-reference-runtime-contract.md` 附记、`tools/sync_prompt_templates.py` 默认前缀）
1. `sync_prompt_templates` 默认处理全部模板（或至少把 `style_ref_` 与 `neutral_draft/style_draft/soft_qc/scene_blueprint` 纳入默认集），并在 CLAUDE.md 写明「改 prompts.yaml 必须同步活动快照」。
2. CLAUDE.md 修正：向量后端默认 memory、hit@5 表述、注入预算与强度语义、新增 section 与 profile_json 键。

## 3. 验收

自动化（全部必须通过）：
- `cd backend && NOVEL_SYSTEM_VECTOR_BACKEND=memory .venv/bin/python -m pytest tests/test_style_reference_*.py tests/test_scene_generation_injection.py tests/test_qc_engine_style_validation_gate.py tests/test_bundle_injection_efficacy.py tests/test_reference_injection_untrusted.py tests/test_styleref_redline_pass3.py tests/test_metadata_isolation.py tests/test_routes_all_manifest.py -q -p no:cacheprovider --basetemp=<唯一>`（chroma_integration 在本机因缺依赖失败可接受，需单列）；
- 全量 `pytest -m "not chroma_integration"` 在阶段 4 跑一次（后台，2 shard）；
- `frontend-react`: `npm test`、`npm run build`。

探针（阶段 4 重跑 `scratchpad/live_probe/test_probe.py` 的改编版）期望：
- 默认 MIXED@80：前缀含 `[声音特征]`、`[正向风格特征]` ≥ 画像规则的 80%、few-shot ≥4 个窗口且为多段、总前缀 4–6k 字；intensity 0 / 50 / 100 三档不同；
- neutral_draft 的 user prompt 含 “Narrative Mechanisms” 段且系统提示无 `[STYLE_REFERENCE]`；
- 同章第二场 style_draft 含 “Previous Scene Voice Anchor”；归档后存在 `style_drift_observed` 事件，第三场含 “Style Drift Calibration”；
- soft_qc 收到 `[STYLE_REFERENCE]` 前缀；
- 三层叠加前缀信息量 ≥ 单层，无半截行 / 空标题；
- 合成：16 子维 × 6+2 条 × 120 字 payload 不失败。

## 4. 执行顺序与并行约束
1. 阶段 1（并行）：W1、W2。
2. 阶段 2a：W3。 阶段 2b（并行）：W4、W5（W5 对 W6 只写调用点；W4 与 W5 文件互斥）。
3. 阶段 3（并行）：W6、W7。 之后：W8。
4. 阶段 4：集成验证 + 修复循环 + 探针。

## 5. 完成记录
- [x] W1（2026-09-05，预算 39000；SynthesizeError→409；narrative_guidance/voice_signature 入画像）· [x] W2（分句/启发式/全书锚点/定种/窗口采样/full_retry；黄金 expected 重生成）· [x] W3（voice_signature 52 特征 + 基线 + 习惯句渲染）· [x] W4（统一 intensity 语义 / `[声音特征]` / 连续窗口样例 / 多层不丢样例 / 软化过滤器 / RAG 画像签名 query + ensure_rag_index / 预览 stats；新增 test_style_reference_injection_v2.py）· [x] W5（叙事机制 section 进 neutral 与 scene_blueprint / 前文声音锚 / 漂移校准调用点 / styled-draft gate（抄袭 Q0 阻断、禁用词 Q2 复核）/ soft_qc 注入同一前缀 / notices 透传到 run/full 与 workbench；style_draft 保留 “immutable event-and-fact scaffold only for what happened” 措辞以兼容既有测试）· [ ] W6 · [x] W7（缓存失效 / 再合成 / 动态维度 / 读数来自 preview.stats / 死路清理 / 遮蔽提示；vitest 360 通过、build 通过）· [x] W8（2026-09-06，`sync_prompt_templates` 默认覆盖全部模板 + `--prefix` 收窄、`--all` 保留兼容，测试同步；CLAUDE.md Style Reference 段追加「2026-09 style-imitation v2」小节、修正向量后端默认 memory 与 hit@5 表述、写明改 prompts.yaml 必须同步；docs/README.md 索引本方案；runtime-contract 追加 v2 附记）· [ ] 阶段 4 验收
- [x] W6（`style_continuity.observe_style_drift` / `latest_drift_event` / 归档期读数 / 新鲜度豁免；上一章回退）· 阶段 4 集成改动（2026-09-06）：`scene_archive_checkpoint` 对**每一场**归档做漂移读数并接受 `observed`（原只在章末，且校验器只认 not_applicable/no_op/degraded）；`scene_archive_effects` 去掉 observed→no_op 别名；`scene_generation` 接受 JSON 字符串形式的 `_drift_ptype_priority`；启发式对白规则改为「引导词只在 ≤120 字的段或引语占比 ≥15% 的长段上判对白」（黄金 expected 再次重生成：鲁迅 dialogue 61%→59%）。
- 阶段 4 集成改动（续）：全量回归暴露架构守卫失败（qc_engine ↔ scene_generation 互相 import 形成环），把 `inject_style_reference_prefix` 抽到中立模块 `services/style_prompt_injection.py`，scene_generation 再导出同名符号，qc_engine 改从新模块导入；架构守卫恢复通过。
- [x] 阶段 4 验收（2026-09-06）：全量 `pytest -m "not chroma_integration"` 2722 通过 / 5 跳过（架构守卫修复后相关套件 114 通过复验）；风格与生成链路套件绿；vitest 360 / build 通过；ruff 全绿；运行时探针 A1–A4 通过（见 scratchpad/live_probe/test_accept.py）；仅 5 个 chroma_integration 用例因本机无 chromadb 未跑通（CI 独立 job 覆盖）。
- 阶段 4 保留项：`_compute_voice_signature_block` 的兜底保留（模块已就位，兜底只在异常时触发，无害）；若分句再变需重跑 `voice_signature build-baseline`。
- [x] 阶段 5 复审与修复（2026-09-06 → 09-08）：对 W1–W6 最高风险文件做 5 路聚焦复审 + 逐条反驳验证，确认 18 条缺陷（1 条驳回），全部修复并由独立验证者按原始失败场景复验。要点：① 近终稿改写（`near_final_rewrite`）纳入 styled-draft gate，抄袭裁决让 orchestrator 丢弃重写稿、回退到已过 gate 的来源稿（`near_final_skip_reason=rewrite_rejected_style_plagiarism`，Q2 警告，严格模式据 payload 停点，当前稿指针指回来源稿）；gate 异常给 `unavailable` 判定 + Q2 `reference_style_gate_unavailable` + 通知码 `STYLE_GATE_UNAVAILABLE`，不再当成无绑定；通知按当前 run 的 bundle 读取；`hit_count` 取真实总数。② 运行时契约同时冻结引文所在段的 ±(few_shot_window_paragraphs−1) 相邻段哈希，生产路径的多段窗口不再退化为单段（旧契约仍可校验；篡改相邻段退化为单段）。③ `_soften_quantitative_guidance` 剥裸数字/小数/比例/区间/「/千字」；`short_sentence_ratio` / `long_sentence_ratio` 用句占比自己的阈值档，按标记组判冲突（有指标佐证即保留）；概述按分句软化不整段删；层叠路径审计 `render_stats` 按合并后前缀重算（未渲染层不继承上一层读数）；`render_preview` 尊重 `context_text`。④ 声音锚点跳过内容等于中性稿的 `style_draft` 回退行（AttemptTracker `content_source` 标记 + 内容相等）；新鲜度预算只带 `preserve_reference_repetition` 标志，不再向 neutral_draft 泄漏目标声音措辞。⑤ `deliberate_repetition` 按基线块级 p85 字面判定（1/√n 收窄只用于漂移 z 值）；叙事层 forbidden 陈述并入 `narrative_guidance` 时加 `避免：` 极性标记（只认完整否定词，「不断 / 不同 / 无数 / 别出心裁 / 莫名其妙」等正面陈述仍加标记）；启发式段型剥掉 `function_words.yaml` 的 `speech_verbs.exclusions` 再匹配说话动词（golden expected 再生成一次）。⑥ 三处永真断言改为可证伪。

## 6. 风险与回退
- 样例预算放大到 ~3.6k 字后，抄袭风险上升：styled-draft gate（W5）是必配套；红线段不变。
- 放宽 style_draft 的骨架约束可能引入事实漂移：既有 must-include / forbidden / ending 守卫保留；若阶段 4 探针显示事实项丢失率上升，退回「句序可重排、段落取舍不可」的中间档。
- 所有新键都在 JSON 字段内，无数据库迁移；配置键有默认值，旧安装无 yaml 新键时行为回到默认。
- 任何 `prompts.yaml` 改动对已保存系统配置的安装无效，需 `sync_prompt_templates --all`（W8 修默认）。
