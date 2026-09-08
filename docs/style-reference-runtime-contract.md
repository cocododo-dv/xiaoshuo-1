# 风格参考运行时契约与反馈闭环

## 目的

同一个场景从生成到候选评分、质检和章节漂移检测，必须使用同一版风格输入。
如果 Bundle 建成后 Profile、Binding 或禁用词被修改，本次运行不得在中途切换风格。

本机制只提高“与已选参考画像的可解释贴合度”。它不承诺规避 AI 检测，也不把某位
作者的身份或原文内容当成正向评分信号。

## 冻结内容

`BundleBuilder` 为 `scene_generation` 和 `long_form_continuation` 分别生成
`style_reference_runtime_contract_v1`，写入冻结 Bundle 并参与 Bundle 哈希。契约包含：

- 由泛到具体的 Binding 层顺序、策略和注入配置；
- Profile 的抽象画像、量化基线和版本（显式字段白名单，未来新增字段不会自动入包）；
- 审核后的禁忌描述与生成期禁用词；
- few-shot 引文 ID 与 SHA-256；
- 参考书校验和及冻结时的云发送许可。

契约不复制参考引文正文。真正取原文时，还必须同时满足冻结时有发送许可且调用时许可
仍有效；few-shot 逐条核对引文 SHA-256，RAG 核对参考书当前校验和与冻结值一致。
撤权、引文变化或参考书版本变化都会让对应原文块安全降级为空。

每个契约和每一层都有独立哈希，读取时会核对层顺序、Profile/Binding 血缘、任务类型、
状态和顶层 ID 列表。

## 冻结状态与兼容规则

Bundle 在 `source_version_refs` 中显式记录状态：

- `frozen`：必须读取嵌入契约；缺失或损坏时降级，不得改读实时 Binding；
- `absent`：建 Bundle 时没有风格 Binding；之后新增 Binding 也不能改变这次回放；
- `degraded`：冻结失败，运行可继续，但生成、评分、质检和漂移检测均不得用实时配置冒充；
- 无状态：仅历史 Bundle 可走旧的实时解析兼容路径。

因此，新 Bundle 不存在“冻结失败后悄悄换成当前配置”的旁路。
生成、重排、质检和漂移检测共用同一个状态解析器；状态与内嵌契约矛盾时也按损坏契约
处理，不会各自猜测。

## 统一消费链

- 生成注入：初次风格化使用中性稿作为检索上下文；长文续写使用最新累计正文尾部。
- 候选重排：从契约快照合成同一量化目标；只有当前实时契约与冻结哈希一致时，已授权的
  active 模式才可改序，否则回到 shadow。
- 质检：对契约中的全部层做一次合并量化校验，使用冻结禁用词做字面检查；参考书校验和
  改变时明确降级，不拿新版本语料冒充原冻结来源做复刻检查。
- 漂移检测：读取当前场景 Bundle 的合并均值，不再只找一个实时 project Binding；同时
  兼容结构化 `{mean, std}` 和历史标量基线。

多层基线沿用“由泛到具体、权重递增”，方差采用总体方差公式，包含层内方差和层间均值
差异。这样注入、候选评分、质检和漂移检测不会各自发明一个目标。

## 上下文与审计

统一上下文提取器会清理控制字符并只保留配置允许的末尾字符数。持久化审计只保存：

- 契约哈希、Profile/Binding ID 和层数；
- 上下文来源、字符数和 SHA-256；
- 注入前缀字符数和 SHA-256；
- 命中、未命中或降级状态。

上下文正文、候选正文和参考引文不会进入这些审计字段。

## 人工候选反馈

关键场景仍按随机顺序盲选，默认接口不返回机器风格分。作者可在终选时显式标记
`preference_tags`；只有 `style_match`、`rhythm`、`voice`、`imagery`、`dialogue`
会被视为“按风格选择”，`overall_quality` 和 `plot_fidelity` 只记录为总体选择。

终选 Gate 在展示前冻结一个只含分数、置信度、ID 和哈希的反馈快照，不含任何候选或
来源正文。选择后记录作者是否与机器风格首选一致。该数据始终为
`policy_evidence_eligible=false`：它可用于发现评分偏差，不能自训练、自动改 Profile
或直接开启主动重排。生产激活仍要求独立冻结、真人核验的盲评报告。每条反馈落库前
即校验内容哈希，篡改记录会被拒绝。

## 验证重点

- 修改实时 Profile/Binding 后，冻结注入和冻结候选分数保持不变；
- 缺失或篡改契约无法回退实时配置；
- 引文变更、发送权撤销时原文块不再注入；
- 中性稿和续写上下文只以哈希进入审计；
- 多层质检与漂移基线和候选评分使用相同合成目标；
- 反馈快照不含正文，未知反馈标签被 API 拒绝，反馈不能激活策略。

## v2 附记（2026-09-06，风格模仿 v2）

依据 `docs/style-imitation-v2-plan-2026-09-05.md` §1 共享契约。以下只是**追加**，不改变上文的
冻结 / 降级语义：旧画像没有新键、旧 Bundle 没有新 section、旧 yaml 没有新配置键时，对应块与
section 一律不渲染，其余链路照旧；反抄袭红线段与 fail-closed 语义不变。

### 冻结键新增

- `profile_json.voice_signature`（`{version, features, habits, deliberate_repetition}`，合成期由
  确定性声音签名 `voice_signature.py` 写入）与 `profile_json.narrative_guidance`（≤8 行确定性派生）
  进入 `runtime_contract._FROZEN_PROFILE_JSON_KEYS` 白名单，随契约冻结并参与哈希；`anchor_quotes_used`
  只是合成审计字段，不入契约。
- 注入前缀顺序：`metric → voice（[声音特征]）→ positive → forbidden → few_shot → rag → anti_plagiarism`。
  `[声音特征]` 只渲染冻结契约里的 `voice_signature.habits`，缺失即为空块。
- intensity∈[0,100] 同时决定抽象四块总额（`intensity_min_total_chars`=900 → `system_prompt_max_tokens`=2400
  字，线性）与 few-shot 窗口数（`few_shot_k_min`=2 → `few_shot_k`=6），A / B / C / MIXED 一致；多层总额
  ×(1 + 0.35 × (层数 − 1))、上限 ×1.7，样例只取最具体层且不再丢弃。few-shot 仍逐条核对引文 SHA-256 与
  发送许可，窗口只在许可有效时展开为相邻段落；契约构建时即把每条引文所在段的
  ±(`few_shot_window_paragraphs` − 1) 同书相邻段哈希一并冻结进 `sample_paragraph_refs`（遇 `paragraph_index`
  断档或空段即止，只存哈希不存原文），所以冻结路径的窗口也是多段的；相邻段被改动时 SHA-256 不符，
  该窗口退化为单段；旧契约（只有引文所在段）照常校验。

### Bundle 新 section 与可见性

| section | 标签 | neutral_draft | style_draft | 来源 |
|---|---|---|---|---|
| `style_narrative_guidance` | Style Reference — Narrative Mechanisms | 可见 | 可见 | 契约所有层的 `narrative_guidance` 合并去重，≤8 行；`scene_blueprint._source_snapshot` 也注入 |
| `previous_scene_voice_anchor` | Previous Scene Voice Anchor (own prose; keep the same voice) | 不可见 | 可见 | 同章上一场最新 `style_draft` / `de_template` / `style_patch` / `style_salvage` 稿（跳过内容等于中性稿的 `style_draft` 回退行）尾部 ≤`continuity_anchor_max_chars`（900）字；没有则上一章末场；没有则不注入 |
| `style_drift_calibration` | Style Drift Calibration | 不可见 | 可见 | 本章最近一次 `style_drift_observed` 事件的 `calibration_lines`，≤`drift_calibration_max_lines`（3）行 |

`context_budget.NEUTRAL_DRAFT_STYLE_SECTIONS` 追加了后两项，`style_narrative_guidance` 刻意不在其中——
叙事取舍机制正是中性稿要吸收的。`source_version_refs` 只记 `style_narrative_guidance_contract_hash` /
`_line_count`、`previous_scene_voice_anchor_scene_id` / `_draft_row_id` / `_stage`、
`style_drift_calibration_line_count` / `_before_scene_seq`；section 正文（自己的成稿尾部、校准行）不进审计。

### MetricEvent

- `style_drift_observed`（W6）：`target_kind="scene"`，`target_ref_id=scene_id`，`profile_id`，
  `context={"chapter_id","scene_seq","features":{名:{"value","baseline_mean","baseline_std","z"}},
  "deviations":[{"feature","direction","z"}],"calibration_lines":[...],"drift_ptype_priority":[...]}`；
  只在 |z|≥1.5 的特征上生成校准行（方向性、无数字）。归档期由
  `scene_archive_effects._detect_and_store_style_drift` → `style_continuity.observe_style_drift` 写入，
  下一场 Bundle 经 `style_continuity.latest_drift_calibration(session, chapter_id, before_scene_seq)` 读取；
  没有事件时返回空、section 不登记。
- `styled_draft_gate_decided`：styled-draft gate 每次裁决写一行（见下）。

### styled-draft gate（`qc_engine.run_styled_draft_style_gate`）

- 中性稿上的 style gate 只保留确定性 n-gram 抄袭（Q0 `style_plagiarism` → `human_review_required`，
  `resolution_code=style_validation_plagiarism`）；quant / 冻结禁用词不再对中性稿裁决。
- 风格稿（style_draft 落库后 / soft_qc 阶段）对 `style_content` 跑 plagiarism + 冻结 `banned_terms`：
  抄袭命中 → Q0 同上，不允许软风险接受；禁用词命中 → Q2 `reference_banned_term_replicated`，soft_qc
  要求人工复核（作者可接受软风险）；量化结果只记诊断计数，永不成为 issue。
- soft_qc 与 style_draft 消费同一 `[STYLE_REFERENCE]` 前缀（`qc_engine._inject_style_reference_prefix`，
  task_type 不变），因此质检对照的是与生成相同的冻结契约。

### notices

`scene_generation` 把 notices（`{code, severity, message}`）写进最近一次 style_draft 的
`AttemptTracker.details_json`，`api/routes/scenes.py:_attach_style_notices` 只读透传到 `run/full`
与工作台响应；同一 outcome 也进入 `_style_reference_runtime_audit`（仍只存哈希与计数，不存正文）。

| code | 含义 |
|---|---|
| `STYLE_DRAFT_FALLBACK_NEUTRAL` | style_draft 因长度 / 必含项被拒，回退中性稿 |
| `STYLE_INJECTION_MISS` | 契约已冻结但没渲染出任何块 |
| `STYLE_INJECTION_DEGRADED` | 注入 outcome 为 `degraded` / `degraded_budget` |
| `STYLE_PLAGIARISM_HIT` | styled-draft gate 抄袭命中（Q0） |
| `STYLE_BANNED_TERM_HIT` | styled-draft gate 冻结禁用词命中（Q2） |

### 同步提示词

`config/prompts.yaml` 的改动对已保存系统配置的安装无效（活动快照盖过仓库文件），须
`cd backend && python -m novel_system.tools.sync_prompt_templates`（干跑）→ `--execute`；该工具自 v2 起
默认覆盖全部模板（此前只有 `snowflake_*`），`--prefix` / `--template` 可收窄，界面改写过且版本号未变的
模板默认保留。
