> 历史文档：描述当时的实现，现行说明见 [docs/style-reference.md](../../style-reference.md)

# 风格参考模块（style_reference）重构执行手册 v1.1

> 状态复核（2026-07-16）：本文是已完成重构的设计基线，不是待执行任务书。Phase 3 A/B/C 均已落地；文中的历史文件行数、迁移编号和“待删除/待新增”措辞保留原始决策语境。当前运行事实以代码、`docs/runtime-safety.md` 和 `docs/style-reference-progress.md` 为准。
>
> 本文档是 style_reference 模块重构的**最终可执行指令书**,版本 v1.1。
>
> **版本说明：** v1.0 由 Claude Code 基于现有 `reference_learning.py` 产出工程骨架。v1.1 在 v1.0 基础上完成 26 项修订,补齐了能力建模层的关键设计(MetricsEngine、Forbidden Pattern、自适应阈值、抄袭事前预防、输入量门槛、Evidence 两级重试、classifier 锚定校准、风格强度滑块、长文本防漂移、双路径 validate 等)。
>
> **阅读优先级：** 背景 → 优势矩阵 → 决策快照 → 目录 → Schema → 路由 → LLM/Prompt → 前端 → 测试 → Phase → 风险 → 集成 → 不做清单 → PR 切分 → 验证 → 附录配置 → 实施指令。
>
> **Claude Code 阅读要求：** 任何"工程上无法实现"或"与现有代码硬冲突"的项,立即列出给用户决策,不要私自变通。不要在本文档评审通过前开始任何代码工作。

---

## 0. Context · 为什么要重构

现有"参考学习"模块(`backend/src/novel_system/services/reference_learning.py`,2446 行)已经把"导入 → 切片 → 轮次抽取 → 评审 → 画像 → 应用"链路跑通,但暴露了 6 个结构性缺口,会限制中文写作场景下风格还原的天花板:

1. **维度模型平铺**——只有 8 个并列 dim(`rhythm/imagery/chapter hook/...`),无层级;无法支撑"语言层 / 叙事层 / 场景层 / 主题层"的分层评估与按强度过滤。
2. **缺硬指标层**——所有 finding 由 LLM 自由文本产出,没有可被验证的量化锚(句长、感官词频、对话占比等),生成结果无法回测。
3. **Evidence 绑定不强制**——`ReferenceFinding` 单段单证据,没有 ≥2 evidence 的 Pydantic 校验;空泛形容词("文笔优美/画面感强")也不被拦截。
4. **缺反抄袭与验证闭环**——`PROFILE_BLOCKED_MARKERS` 是字面黑名单(写死了"江南/路明非/龙族"),不通用;没有 n-gram 反抄袭、没有量化对齐、没有语义评分。
5. **缺反样本机制**——只有正向 observation,没有 forbidden pattern(模式级反样本)。模仿张爱玲容易滑向堆砌华丽形容词、模仿江南容易滑向中二独白,字面禁词拦不住这类风格漂移。
6. **缺长文本防漂移机制**——长文本生成 5000 字以上必然回归 base 模型默认腔调,但现有 apply 路径是一次性消耗,无续写时重新注入的契约。

v1.0 设计稿给出了 6 层架构(ingest → extract → storage → inject → validate → interface)、4×16 sub-dim、Pydantic 契约、A/B/C 注入策略与三路并发验证。本次重构的目标是**全部吸收新设计的能力**,但**部署形态**改为嵌入式(落入现有 backend)以复用 LLMClient/ReviewItem/幂等合约/前端导航。

---

## 1. 现状 vs 新设计 · 优势矩阵与重构可行性

| 维度 | 现状(reference_learning) | 新设计(v1.1) | 重构后采纳 |
|---|---|---|---|
| 部署 | 嵌入 backend/src/novel_system | 独立顶级包 + CLI + 可选 FastAPI | **嵌入** |
| 持久化 | SQLAlchemy + SQLite 共库 | YAML 文件 + 独立 SQLite | **共库**(11 张新表)|
| 维度 | 8 平铺 dim | 4 层 × 16 sub-dim | **4×16** |
| 段类型 | 启发式正则 + SEGMENT_KIND_LABELS | LLM 分类器 + 8 种 ParagraphType + confidence + 锚定集校准 | **LLM 分类器 + 锚定校准** |
| 硬指标 | 无 | MetricsEngine(贯穿抽取/验证/预览三阶段)| **Phase 1 必做基础设施** |
| Evidence | 单段单证据,dict-based | ≥2 evidence + Pydantic validator + 两级重试 | **新增 + 两级重试** |
| 反样本 | 字面禁词黑名单 | observation + forbidden_pattern 并列,共享 schema | **finding_kind 区分** |
| 多维挂载 | 不支持 | 一条 quote 可挂多个 illustrates_dims | **quotes 独立表 + 多对多** |
| 注入 | apply_profile → ReviewItem → 4 集合物化 | A=System / B=Few-shot / C=RAG + TaskType 默认表 + 强度滑块 + 维度选择 | **新增 + 保留 ReviewItem 物化** |
| 抄袭防护 | 字面黑名单 | 事前预防(System Prompt 红线)+ 事后检测(8-gram / 12 char) | **事前 + 事后双层** |
| 量化阈值 | 无 | 自适应:tolerance = max(原作 std × 1.25, 绝对下限) | **新增** |
| 输入门槛 | 无 | assess_input_size 按层设阈值,skip 层不抽取 | **新增** |
| 防漂移 | 无 | InjectionBundle.refresh_every_chars + scene_execution 续写循环 | **新增** |
| 验证 | 无 | quantitative + semantic + plagiarism 三路并发 + 同步/异步双路径 | **新增 + 双路径** |
| 向量 | ReviewMaterialization 间接 | 三粒度(sentence/paragraph/scene) | **新增(Phase 3 保留三粒度)** |
| 评审 | 已与 ReviewInbox 集成 | 未提及 | **保留 ReviewItem 集成** |
| 离线策略 | cloud_policy 三档 | offline mode 提及 | **保留 cloud_policy** |
| 幂等/审计 | 完整(execute_with_idempotency + LlmCall) | 提及 | **保留** |
| 前端 | ReferenceLearningView 已就位 | CLI 优先 | **原地重构 Vue 视图 + IntensitySlider + DimensionMultiSelect** |

**各自优势小结：**

- 现状真正值得保留的是"系统工程脚手架"——ReviewItem 评审 gate、cloud_policy 离线降级、幂等合约、审计、前端集成、与 SnowflakeWorkbench 的导航关系。
- 新设计的真正价值在"建模质量"——分层维度、强制证据、硬指标、反样本机制、注入策略显式建模、回测闭环、抄袭事前预防、自适应阈值、防漂移。

**结论：可以重构,推荐路径是"嵌入式整体替换"。** 旧服务一次性下线,新服务复用脚手架。

---

## 2. 重构决策快照

| 项 | 决策 |
|---|---|
| 部署形态 | 嵌入式,落入 `backend/src/novel_system/services/style_reference/` |
| 数据处理 | 全清重抽(drop 旧 6 表 + 清理 ReviewItem 残留行)|
| 前端 | 原地重构 `ReferenceLearningView.vue` 与 `stores/referenceLearning.js`,保留路由位置 |
| 维度模型 | 4 层 × 16 sub-dim |
| Evidence | Pydantic 强制 ≥2 + 禁用空泛形容词 + 两级重试(单 obs 定向补抽 → 整 sub_dim 重抽)|
| 反样本 | observation 与 forbidden_pattern 并列,共享 schema,`finding_kind` 字段区分 |
| 硬指标 | MetricsEngine 是 Phase 1 必做基础设施,贯穿抽取上下文 / 验证对照 / 预览证据三个动作 |
| 注入 | A=System Prompt / B=Few-shot / C=RAG,TaskType 默认策略表 + style_intensity 滑块 + selected_dimensions 多选 |
| 抄袭防护 | 事前(System Prompt 固定红线段)+ 事后(n-gram 检测)双层 |
| 量化阈值 | 自适应,基于原作 metric std × 1.25,绝对下限外置配置 |
| 输入门槛 | `assess_input_size` 按层设阈值,skip 层完全跳过抽取 |
| 长文本防漂移 | `InjectionBundle.refresh_every_chars` 字段 + scene_execution 续写循环按字数刷新注入 |
| Validate | 三路并发,但 qc_engine 落盘 gate 走同步快路径(quantitative + plagiarism),semantic 异步轮询 |
| 段落分类 | 首次 run 前 200 段走 quality_balanced 作锚定集,余下用 local_fast 但需通过 ≥0.85 一致性检验,否则 fallback |
| CLI | 不做(项目是 Web 应用)|
| 本地 Qwen | 不做(Phase 3 才考虑)|
| 命名约定 | observation 与 forbidden_pattern 统一使用 `statement` 字段(不混用 text)|

---

## 3. 后端目录结构与文件清单

新增树(全部位于 `backend/src/novel_system/services/style_reference/`):

```
style_reference/
  __init__.py
  service.py                       # StyleReferenceService 顶层编排(取代 ReferenceLearningService)
  schemas.py                       # 全部 Pydantic 契约
  dimensions.py                    # 4×16 sub-dim 常量 + Enum + LAYER_TO_SUB_DIMS 映射
  ingest.py                        # 路径/上传导入 + checksum + 清洗 + assess_input_size
  segmentation.py                  # ParagraphType 切段 + 8 类型 LLM 分类器 + 锚定集校准
  banned_adjective.py              # 禁用空泛形容词词表(extractor 时校验)
  evidence.py                      # EvidenceRef 索引 / span 校验
  metrics.py                       # 硬指标计算(纯函数,无 LLM,贯穿三阶段使用)
  extractors/
    __init__.py
    base.py                        # 抽象 Extractor + 两级重试机制(单 obs 定向补抽 → 整 sub_dim 重抽)
    language.py                    # sentence_structure / vocabulary / rhetoric / punctuation
    narrative.py                   # perspective / pacing / time_handling / information_density
    scene.py                       # environment / character_portrayal / dialogue / sensory_priority
    theme.py                       # emotional_tone / values / motifs / narrative_philosophy
  profile_synthesizer.py           # 16 sub-profile → StyleProfile + metrics_baseline + scene_samples_index
  injection/
    __init__.py
    request.py                     # InjectionRequest / InjectionBundle / SystemPromptFragments / InjectionBudget
    strategy.py                    # TaskType → A/B/C 默认表 + REFRESH_INTERVALS + DEFAULT_BUDGETS
    system_prompt.py               # A 注入器,按 intensity 控制 obs 数量,固定 anti_plagiarism 段
    few_shot.py                    # B 注入器,从 profile.scene_samples_index 直读
    rag.py                         # C 注入器(Phase 3,保留三粒度索引)
  validation/
    __init__.py
    runner.py                      # 三路并发 + verdict 聚合 + 同步/异步双路径 + auto_rewrite 配置
    quantitative.py                # 自适应 tolerance,读取 profile.metrics_baseline
    semantic.py                    # critic LLM,强制 explanation 含 quote
    plagiarism.py                  # Rabin-Karp 或 Suffix Automaton,O(n+m)
  preview.py                       # 抽取完成后自动生成 3 段示例 + 自跑 validate
  materialization.py               # profile → ReviewItem → style_rules / narrative_patterns / banned_rule_clusters / calibration_lines
  repository.py                    # ORM CRUD
  errors.py                        # StyleReferenceError 体系
  cleanup.py                       # metric events 定期清理
```

辅助:
- `backend/src/novel_system/api/routes/style_reference.py` —— 新路由
- `backend/src/novel_system/tools/reset_style_reference.py` —— 历史草案中的开发者脚本(当前运行时已移除)

**新增配置目录** `config/style_reference/`(详见附录 A):
- `tolerance_floors.yaml` —— 量化对齐的绝对下限
- `input_thresholds.yaml` —— 输入量门槛
- `extraction.yaml` —— 抽样策略与重试策略
- `sensory_lexicon.yaml` —— 五感词表
- `anti_plagiarism_template.txt` —— System Prompt 抄袭红线段
- `prompts/*.txt` —— 各 LLM 节点的 prompt 模板

**删除：**
- `backend/src/novel_system/services/reference_learning.py`(2446 行)
- `backend/src/novel_system/api/routes/reference_books.py`
- `backend/src/novel_system/services/reference_safety.py` 中的 ReferenceBook 专用断言(保留通用 safety 部分)

**复用(不动)：** `services/llm_client.py` / `services/llm_task_runner.py` / `services/idempotency.py` / `services/hash_engine.py` / `services/system_config.py` / `api/response.py` / `services/versioning/review_materialization.py`。

---

## 4. 数据库 Schema 设计

迁移策略：**drop old → create new**,单次发布两个 Alembic revision。

### 4.1 删除旧表(按 FK 反向)

`reference_profiles` → `reference_findings` → `reference_learning_rounds` → `reference_learning_runs` → `reference_book_segments` → `reference_books`。同时清理 `review_items` 中 `review_id LIKE 'review_apply_%'` 与 `review_id LIKE 'review_finding_%'` 行。

> **强制保护：** drop 前以 JSON dump 旧 `reference_profiles` 表到 `backups/style_reference_legacy_{ts}.json`;dump 失败则整段中止。

### 4.2 新表(前缀 `style_reference_`,11 张)

| 表 | 主键 | 关键字段 | FK | 索引 |
|---|---|---|---|---|
| `style_reference_books` | book_id | title, author_label, source_kind, source_path, cloud_policy, text_checksum, total_chars, status, **stats_json**, created_at, updated_at | — | uq(text_checksum), idx(status, updated_at) |
| `style_reference_paragraphs` | paragraph_id | book_id, paragraph_index, paragraph_type, start_offset, end_offset, text, char_count, classifier_confidence | book_id | idx(book_id, paragraph_type), idx(book_id, paragraph_index) |
| `style_reference_extractions` | extraction_id | book_id, layer, sub_dimension, run_id, llm_call_id, raw_payload_json, status, validation_errors_json, **purpose** | book_id, run_id | idx(book_id, layer, sub_dimension), idx(run_id, status) |
| `style_reference_quotes` 🆕 | quote_id | book_id, paragraph_id, span_start, span_end, quote_text, **illustrates_dims**, extracted_features | book_id, paragraph_id | idx(book_id), idx(book_id, illustrates_dims) |
| `style_reference_evidences` | evidence_id | finding_id, quote_id, **anchor_kind**, **is_synthetic** | finding_id, quote_id | uq(finding_id, quote_id) |
| `style_reference_findings` | finding_id | book_id, run_id, extraction_id, sub_dimension, **finding_kind**, **statement**, confidence, status, review_id | book_id, run_id, extraction_id | idx(book_id, sub_dimension, finding_kind), uq(review_id), uq(extraction_id, sub_dimension, finding_kind) |
| `style_reference_runs` | run_id | book_id, status, phase, coverage_json, started_at, finished_at | book_id | idx(book_id, status) |
| `style_reference_profiles` | profile_id | book_id, run_id, title, status, **profile_json**, coverage_json, source_finding_ids_json, version_tag | book_id, run_id | idx(book_id, status), idx(version_tag) |
| `style_reference_injection_bindings` | binding_id | profile_id, scope, scope_ref_id, task_type, strategy, config_json, status | profile_id | idx(profile_id, scope, scope_ref_id), idx(task_type) |
| `style_reference_validation_reports` | report_id | profile_id, target_kind, target_ref_id, verdict, **quantitative_json**, **semantic_json**, **plagiarism_json**, **forbidden_hits_json**, mode_executed, created_at | profile_id | idx(profile_id, target_ref_id), idx(verdict) |
| `style_reference_banned_terms` | term_id | profile_id, term, replacement_hint, source, **scope**, created_at | profile_id | uq(profile_id, term, scope) |

> 🆕 标注 = 相比 v1.0 新增的表。
> **粗体字段** = 相比 v1.0 新增或语义变化的字段,详见下方各 schema 注释。

类型约束：`*_json` 用 SQLAlchemy `JSON`;status/verdict/strategy/phase/paragraph_type/finding_kind/anchor_kind/scope 用 `String` + Python Enum(不引入 PG ENUM,与仓库现有风格一致)。

### 4.3 关键字段语义

#### `style_reference_books.stats_json` 结构

```json
{
  "metrics": {
    "avg_sentence_length": {"mean": 18.4, "std": 12.1, "sample_count": 1247},
    "sentence_length_std": {"mean": 12.1, "std": 3.2, "sample_count": 1247},
    "metaphor_density_per_1k": {"mean": 4.7, "std": 2.3, "sample_count": 50}
  },
  "input_assessment": {
    "language": "high",
    "narrative": "medium",
    "scene": "high",
    "theme": "skip"
  },
  "classifier_calibration": {
    "anchor_size": 200,
    "fast_model_agreement": 0.87,
    "fallback_to_strong": false
  },
  "paragraph_type_distribution": {
    "dialogue": 0.32, "narration": 0.28, "psychology": 0.15,
    "description_env": 0.10, "action": 0.08, "...": "..."
  }
}
```

`metrics` 字段必须包含至少 25 个 MetricName(完整列表见 §6.3 MetricsEngine 规格)。`input_assessment` 字段值 ∈ `{skip, low, medium, high}`,见 §6.4 输入量门槛规格。

#### `style_reference_findings.finding_kind` 值域

```
'observation'         正向特征(原 v1.0 唯一类型)
'forbidden_pattern'   反向禁忌,与 observation 共享 schema,都要 ≥2 evidence
```

两种 kind 都使用 `statement` 字段(自然语言描述),都要 ≥2 evidence。`UNIQUE(extraction_id, sub_dimension, finding_kind)` 保证一次抽取在同一 sub_dim 的同一 kind 下唯一。

#### `style_reference_evidences.anchor_kind` 值域

```
'paragraph_quote'      正常的段落引文(observation 的常规证据)
'author_avoidance'     作者从未这样写的负空间证据(forbidden_pattern 的统计反推证据)
'counter_example'      LLM 生成的反例文本(forbidden_pattern 的合成证据,is_synthetic=1)
```

`counter_example` 类型的 evidence 允许 `quote_id` 指向**新增的合成 quote 行**(此时 quote.paragraph_id 为 NULL,需在 quotes 表上放宽该 FK 为可空)。

#### `style_reference_quotes.illustrates_dims` 多维挂载

```sql
illustrates_dims TEXT NOT NULL  -- JSON array
-- 示例: ["language.rhetoric", "scene.sensory_priority"]
```

一条精彩文本同时承载多维度特征时,**只在 quotes 表存一行**,通过 illustrates_dims 多维挂载。findings 通过 evidences 表与 quote 建立多对多关系,避免样本库虚胖。

#### `style_reference_profiles.profile_json` 结构

```json
{
  "narrative_summary": "...",
  "metrics_baseline": {
    "avg_sentence_length": {"mean": 18.4, "std": 12.1},
    "metaphor_density_per_1k": {"mean": 4.7, "std": 2.3}
  },
  "scene_samples_index": {
    "dialogue": ["q_001", "q_023", "q_045"],
    "action": ["q_067", "q_089"],
    "psychology": ["q_112", "..."],
    "description_env": ["..."],
    "description_char": ["..."],
    "narration": ["..."],
    "transition": ["..."],
    "flashback": ["..."]
  },
  "sub_dimensions": {
    "language.sentence_structure": {
      "confidence": "high",
      "observation_count": 6,
      "forbidden_pattern_count": 2,
      "quote_count": 15
    }
  }
}
```

`metrics_baseline` 用于 validation 阶段计算自适应 tolerance(见 §7 validation 规格)。`scene_samples_index` 是 Few-shot 召回的 O(1) 直读索引(避免绕 paragraph 表 JOIN)。

#### `style_reference_banned_terms.scope` 值域

```
'generation'     生成时禁用(在 prompt 中告知模型不要输出这些词)
'extraction'     抽取时跳过含此词的段落(避免污染抽样池)
```

默认 `generation`。该表用途**与上传时业务安全无关**(上传安全由 `reference_safety.py` 通用部分处理)。

#### `style_reference_extractions.purpose` 值域

```
'extract'                初次抽取
'supplement_evidence'    针对单 observation 的定向补抽(两级重试第一级)
'full_retry'             整 sub_dim 重抽(两级重试第二级)
```

用于成本审计与重试链路追溯。

#### `style_reference_validation_reports.mode_executed` 值域

```
'sync_only'      仅跑 quantitative + plagiarism,毫秒级返回(qc_engine 落盘 gate 用)
'async_full'     全跑三路并发,semantic 走 LLM 异步；语义路不可用/空结果时 verdict 至多 partial
```

`forbidden_hits_json` 是新增字段,存储 forbidden_pattern 的触发结果(任一触发 verdict=fail)。

### 4.4 Alembic Revisions

沿用现有 `YYYYMMDD_NNNN_*` 命名:

- `20260521_0036_drop_reference_learning.py` —— drop 6 张旧表 + 清理 `review_items` 残留 + 强制 backup dump
- `20260521_0037_style_reference_schema.py` —— create 11 张新表 + 索引

两个 revision 分文件,便于回滚(0037 单独 downgrade 即可保留新数据可重抽)。

---

## 5. API 路由设计

新前缀：`/api/v2/style-reference/*`。

所有写操作要求 `X-Idempotency-Key` 走 `execute_with_idempotency`;响应通过 `api.response.ok` 包络;中间件继续注入 `X-Operator-Ref`。

| Method | Path | 用途 | 幂等 |
|---|---|---|---|
| POST | `/books/import-path` | 路径导入 | 必须 |
| POST | `/books/import-upload` | 上传导入(multipart)| 必须 |
| GET | `/books` | 列表 | — |
| GET | `/books/{book_id}` | 详情(含 stats_json)| — |
| DELETE | `/books/{book_id}` | 级联删除 | 必须 |
| POST | `/books/{book_id}/reclassify` 🆕 | 重跑段落分类器(可选强模型 / 部分段落) | 必须 |
| POST | `/books/{book_id}/runs` | 启动一次抽取 run | 必须 |
| GET | `/runs/{run_id}` | run 状态 + 各 sub_dim 进度 | — |
| POST | `/runs/{run_id}/cancel` | 取消 | 必须 |
| GET | `/runs/{run_id}/findings` | findings 列表(支持 sub_dim/status/finding_kind 过滤)| — |
| POST | `/findings/{finding_id}/review` | 审核(内部走 ReviewItem)| 必须 |
| POST | `/findings/{finding_id}/user-feedback` 🆕 | 用户对 finding 打分(👍/👎,Phase 3 后)| 必须 |
| POST | `/runs/{run_id}/synthesize` | 16 sub-profile 聚合 → StyleProfile | 必须 |
| GET | `/profiles` | profile 列表 | — |
| GET | `/profiles/{profile_id}` | 完整 StyleProfile | — |
| POST | `/profiles/{profile_id}/preview` 🆕 | 自动生成 3 段示例 + 自跑 validate | 必须 |
| POST | `/profiles/{profile_id}/apply` | 绑定到 scope,内部走 materialization → ReviewItem → 4 集合 | 必须 |
| GET | `/profiles/{profile_id}/bindings` | 绑定列表 | — |
| DELETE | `/bindings/{binding_id}` | 删除绑定 | 必须 |
| POST | `/inject` | 返回 InjectionBundle | 否(读模型)|
| POST | `/validate` | 三路并发回测,支持 sync_only / async_full | 必须 |
| GET | `/profiles/{profile_id}/reports` | 回测历史 | — |
| GET | `/reports/{report_id}` | 轮询单个 report(async_full 用) | — |

🆕 标注 = 相比 v1.0 新增的端点。

### 5.1 InjectionRequest / InjectionBundle 契约(强类型化)

```python
class TaskType(str, Enum):
    PROJECT_INIT = "project_init"
    SCENE_GENERATION = "scene_generation"
    FINE_TUNING = "fine_tuning"
    LONG_FORM_CONTINUATION = "long_form_continuation"
    KEY_CHAPTER = "key_chapter"

class InjectionBudget(BaseModel):
    max_system_prompt_tokens: int = 3000
    few_shot_k: int = 5
    rag_top_k: int = 3

class InjectionRequest(BaseModel):
    profile_id: str
    task_type: TaskType
    scope_ref_id: str | None = None
    strategy: Literal["A", "B", "C", "mixed"] | None = None  # None 走默认表

    style_intensity: float = Field(0.8, ge=0.0, le=1.0)        # 🆕 借鉴强度滑块
    selected_dimensions: list[str] = []                          # 🆕 空 list = 全部
    budget: InjectionBudget | None = None                        # None 走 DEFAULT_BUDGETS

class SystemPromptFragments(BaseModel):
    """有序、命名分段,合并时按字段定义顺序用 \n\n 连接"""
    narrative_summary: str             # 必填,排序 1
    banned_pattern_block: str          # 必填,排序 2,来自 forbidden_patterns
    observations_by_dim: dict[str, str]  # 必填,排序 3,key 为 dim_path
    anti_plagiarism_block: str         # 必填,排序 4,固定模板

class InjectionBundle(BaseModel):
    system_prompt_fragments: SystemPromptFragments   # 🆕 强类型化(不再 list[str])
    few_shot_examples: list[dict]
    rag_snippets: list[dict]
    bound_rule_refs: list[str]
    refresh_every_chars: int           # 🆕 0 = 不刷新,>0 = 续写每 N 字重新调 /inject
    generated_at: datetime             # 🆕 便于下游判断 bundle 是否过期
    used_observation_ids: list[str] = []   # 审计字段
    used_quote_ids: list[str] = []
```

`scene_generation` / `qc_engine` 仅依赖这两个 schema,不直接读 ORM。

### 5.2 Validate 同步/异步双路径

```python
class ValidateRequest(BaseModel):
    profile_id: str
    generated_text: str
    target_kind: Literal["scene", "chapter", "manual"] = "manual"
    target_ref_id: str | None = None
    mode: Literal["sync_only", "async_full"] = "async_full"
    task_context: dict | None = None   # 关联的 TaskType 等

class ValidateResponse(BaseModel):
    report_id: str
    mode_executed: Literal["sync_only", "async_full"]
    sync_result: PartialValidationReport | None   # sync_only 时填
    polling_url: str | None                        # async_full 时填(指向 GET /reports/{id})
```

`qc_engine` 落盘 gate 调用 `mode="sync_only"`,verdict ∈ `{fail, plagiarism}` 时立即阻塞,UI 不卡。后台异步触发 `async_full`,完整 report 入库供 ReviewInbox 查看。

---

## 6. LLM 节点 & Prompt 重构

### 6.1 `config/models.yaml`

**删除：** `reference_sample_ranker` / `reference_style_structure_extract` / `reference_profile_synthesize`

**新增(前缀 `style_ref_`):**

| task_name | model route | 用途 |
|---|---|---|
| `style_ref_paragraph_classify_anchor` | quality_balanced | 段落分类锚定集(前 200 段)|
| `style_ref_paragraph_classify_bulk` | local_fast | 段落分类(余下,需通过 ≥0.85 一致性检验)|
| `style_ref_extract_language` | quality_strong | 4 个 language sub-dim |
| `style_ref_extract_narrative` | quality_strong | 4 个 narrative sub-dim |
| `style_ref_extract_scene` | quality_strong | 4 个 scene sub-dim |
| `style_ref_extract_theme` | quality_strong | 4 个 theme sub-dim |
| `style_ref_supplement_evidence` 🆕 | quality_balanced | Evidence 两级重试第一级:单 obs 定向补抽 |
| `style_ref_synthesize_profile` | quality_strong | 16 sub-profile → StyleProfile |
| `style_ref_validate_semantic` | quality_balanced | 回测语义评分,强制 explanation 含 quote |
| `style_ref_fewshot_select` | local_fast | B 策略样例排序 |
| `style_ref_rag_rerank` | local_fast | C 策略 rerank(Phase 3)|
| `style_ref_preview_generate` 🆕 | quality_balanced | 预览端点生成 3 段示例 |

**路由说明：** Phase 1 阶段 `local_fast` = `claude-haiku-4-5`(或同 tier 模型),**不允许指向未接入的 Qwen**。Phase 3 评估后才考虑切到本地 Qwen3-14B。

### 6.2 段落分类器锚定集校准

> **2026-09-15 更新（严格 LLM）**：下面这段「余段上限 + 启发式」的有界导入已经撤销——LLM 可用时整本每一段都由 LLM 分类，没有启发式兜底；大书的分类是可续跑的后台任务（`import_job.py`），进度在「参考书活动」面板，见 `style-reference-progress.md` 同日条目。
>
> **2026-09-15 补充（有界导入）：** 下文三步中「余下段落」只有在不超过
> `segmentation/llm.py::LLM_BULK_PARAGRAPH_CAP`（1000 段）时才逐批过 LLM；更大的书余段整体走
> 顺序感知启发式，快模型锚定对照省掉，`calibration` 额外记录 `rest_classifier` /
> `heuristic_anchor_agreement` / `llm_classified_paragraphs` / `heuristic_classified_paragraphs`。
> 原因：一本 26,677 段的书按本节设计要 1,059 次串行分类调用，同步导入请求扛不住。

为防止 `local_fast` 在 8 类段落分类上准确率不足(直接影响后续所有统计的输入质量),引入**锚定集校准策略**。

```python
# backend/src/novel_system/services/style_reference/segmentation.py

async def classify_with_calibration(self, paragraphs: list[Paragraph]):
    anchor_size = min(200, len(paragraphs))
    anchor = paragraphs[:anchor_size]
    
    # Step 1: 锚定集走强模型
    anchor_labels_strong = await self._classify(
        anchor, model_route="quality_balanced"
    )
    
    # Step 2: 锚定集再走快模型,计算一致性
    anchor_labels_fast = await self._classify(
        anchor, model_route="local_fast"
    )
    agreement = self._compute_agreement(anchor_labels_strong, anchor_labels_fast)
    
    if agreement < 0.85:
        # 快模型不达标,整本走强模型
        logger.warning(f"Fast classifier agreement {agreement:.2f} < 0.85, falling back")
        rest_labels = await self._classify(
            paragraphs[anchor_size:], model_route="quality_balanced"
        )
        return anchor_labels_strong + rest_labels, {
            "anchor_size": anchor_size,
            "fast_model_agreement": agreement,
            "fallback_to_strong": True,
        }
    
    # 快模型达标,余下走快模型
    rest_labels = await self._classify(
        paragraphs[anchor_size:], model_route="local_fast"
    )
    return anchor_labels_strong + rest_labels, {
        "anchor_size": anchor_size,
        "fast_model_agreement": agreement,
        "fallback_to_strong": False,
    }
```

校准元数据写入 `books.stats_json.classifier_calibration`。fallback 触发时前端 BooksDetail 页面显示:"分类器降级到高质量模式,本次抽取成本略高"。

### 6.3 MetricsEngine 规格

**贯穿三阶段使用：** ① 抽取时作为 prompt 上下文注入(`metrics_anchor`);② 验证时作为对照(`quantitative` 路径);③ 预览时作为置信度证据。

```python
# backend/src/novel_system/services/style_reference/metrics.py
from typing import Literal

MetricName = Literal[
    # 语言层
    "avg_sentence_length", "sentence_length_std",
    "short_sentence_ratio", "long_sentence_ratio",       # 阈值 ≤10 / ≥30
    "punctuation_density_per_1k",
    "dash_em_density_per_1k", "ellipsis_density_per_1k",
    "semicolon_density_per_1k", "question_density_per_1k",
    "classical_word_ratio", "colloquial_marker_ratio",
    "metaphor_density_per_1k", "personification_density_per_1k",
    # 叙事层
    "dialogue_ratio", "psychology_ratio",
    "description_env_ratio", "description_char_ratio",
    "action_ratio", "narration_ratio",
    "transition_ratio", "flashback_ratio",
    # 感官(场景层)
    "sensory_visual_per_1k", "sensory_auditory_per_1k",
    "sensory_olfactory_per_1k", "sensory_tactile_per_1k",
    "sensory_gustatory_per_1k",
]

class MetricsEngine:
    def compute_all(self, paragraphs: list[Paragraph]) -> dict[MetricName, float]:
        """整本计算单值,用于 stats_json.metrics 的 mean"""

    def compute_with_variance(
        self, paragraphs: list[Paragraph]
    ) -> dict[MetricName, tuple[float, float]]:
        """返回 {metric: (mean, std)},std 用于自适应阈值"""
```

**纯函数,不调用 LLM。** 中文分句按 `。!?…` 切分,引号内不切;`,` `;` 不算句子边界。感官词表见 `config/style_reference/sensory_lexicon.yaml`。

### 6.4 输入量门槛 `assess_input_size`

```python
# backend/src/novel_system/services/style_reference/ingest.py
INPUT_THRESHOLDS = {
    # layer: (min_chars_skip, min_chars_low, min_chars_high)
    "language":  (10000, 30000, 50000),
    "narrative": (20000, 50000, 80000),
    "scene":     (10000, 30000, 50000),
    "theme":     (30000, 80000, 150000),
}

def assess_input_size(total_chars: int) -> dict[str, Literal["skip", "low", "medium", "high"]]:
    result = {}
    for layer, (skip, low, high) in INPUT_THRESHOLDS.items():
        if total_chars < skip:
            result[layer] = "skip"
        elif total_chars < low:
            result[layer] = "low"
        elif total_chars < high:
            result[layer] = "medium"
        else:
            result[layer] = "high"
    return result
```

`runs` 启动时读取该字段,layer 为 `skip` 的整层不调度 extractor,**不消耗任何 LLM 调用**。前端 `DimensionMatrix` 把 `skip` 的格子置灰显示"语料不足"。

`INPUT_THRESHOLDS` 外置到 `config/style_reference/input_thresholds.yaml`。

### 6.5 `config/prompts.yaml` 模板 outline

#### Extractor 通用输出 schema(强约束)

```yaml
style_ref_extract_<layer>:
  input_schema:
    metrics_anchor:                  # 🆕 硬指标作为先验注入
      avg_sentence_length: float
      sentence_length_std: float
      # ... 当前 sub_dim 关心的子集
    paragraphs: [{paragraph_id, paragraph_type, text}]
    sub_dimension: enum
  
  output_schema:
    observations:                    # 正向特征,可 0-8 条
      - statement: str               # 禁空泛形容词
        confidence: enum
        evidence:                    # 强制 >= 2
          - paragraph_id: str
            span: [int, int]
            quote: str
            illustrates_dims: list[str]   # 🆕 多维挂载
    
    forbidden_patterns:              # 🆕 反向禁忌,可 0-3 条
      - statement: str               # "作者从不使用 X 式陈词滥调比喻"
        evidence:                    # 强制 >= 2,可来自两种锚点
          - anchor_kind: enum        # 'paragraph_quote' | 'author_avoidance' | 'counter_example'
            paragraph_id: str | null
            quote: str
            note: str                # 说明此证据如何支撑该禁忌
```

#### Prompt 模板核心段(metrics_anchor 注入)

```
下列硬指标已对全文计算完毕,你提取的 observation 必须与之一致;
若你的描述与硬指标矛盾,以硬指标为准重新组织语言。

{metrics_anchor_json}
```

#### Forbidden Pattern 提取指令

```
除了正向特征 observation,你还需要提取 0-3 条 forbidden_pattern——即该作者
明确不会使用的写作模式。这不是字面禁词,而是"反向写作习惯",例如:
- "作者从不使用'眼睛像星星'式陈词滥调比喻"
- "作者从不在比喻后追加解释性句子"
- "作者从不使用嵌套超过两层的复合比喻"

每条 forbidden_pattern 也必须 ≥2 evidence。evidence 可以是三类锚点之一:
- paragraph_quote: 段落中一段反向特征明显的文本(说明作者用了 Y 而非 X)
- author_avoidance: 统计反推证据(如 metaphor_density 高但 cliché 比喻为 0)
- counter_example: 你自己合成的反例,标注 is_synthetic=true

若实在没有可靠的 forbidden_pattern,可返回空数组,不要勉强凑数。
```

#### Critic Prompt(强制引用证据)

```
评分必须满足:
- explanation 字段必须包含生成文本中的 quote(用「」包裹)
- 若无法引用具体句子,score 上限为 4
- 引用必须是生成文本的原句,不允许改写或概括
```

`SemanticReport` 的 Pydantic validator 校验 `explanation` 是否含 `「...」` 结构,不含则 score 强制截至 ≤4。

### 6.6 Extractor 两级重试机制

`extractors/base.py` 校验装饰器:

```python
class ExtractionRetryPolicy(BaseModel):
    max_targeted_retries: int = 2   # 第一级:单 observation 定向补抽
    max_full_retries: int = 1       # 第二级:整 sub_dim 重抽

class BaseExtractor:
    async def extract_with_retry(self, ...) -> SubDimensionExtraction:
        # Step 1: 初次抽取
        result = await self._extract_once(..., purpose="extract")
        
        # Step 2: 校验所有 finding 的 evidence 数
        # (observation 与 forbidden_pattern 都要 ≥2 evidence)
        failed = [f for f in result.findings if len(f.evidence) < 2]
        if not failed:
            return result
        
        # Step 3: 第一级——针对每条失效 finding 单独补抽
        for attempt in range(self.policy.max_targeted_retries):
            for finding in list(failed):
                new_evidence = await self._supplement_evidence_for(
                    finding, paragraphs, purpose="supplement_evidence"
                )
                if new_evidence:
                    finding.evidence.extend(new_evidence)
                    if len(finding.evidence) >= 2:
                        failed.remove(finding)
            if not failed:
                return result
        
        # Step 4: 第二级——仍有失效,整 sub_dim 重抽一次
        if self.policy.max_full_retries > 0:
            return await self._extract_once(..., purpose="full_retry")
        
        # Step 5: 丢弃失效 finding,记 warning
        result.findings = [f for f in result.findings if len(f.evidence) >= 2]
        logger.warning(f"Dropped {len(failed)} findings after retries")
        return result
```

补抽 prompt 模板(`style_ref_supplement_evidence`)只展示该 finding.statement,要求从段落池中找支持样本,prompt 上下文极小,**成本预期 ≤30% 完整 extract**。

审计日志中 `extraction.purpose` 字段区分三种调用,便于成本分析。

### 6.7 校验装饰器(空泛形容词拦截)

`banned_adjective.py` 禁用词列表(配置在 `config/style_reference/banned_adjectives.yaml`,初始版):

```yaml
- 文笔优美
- 画面感强
- 叙事流畅
- 意境深远
- 笔触细腻
- 情感真挚
- 文采斐然
- 韵味无穷
```

extractor 校验装饰器:
- 任一 finding.statement 命中 → raise `BannedAdjectiveError`
- 任一 finding.evidence 数 < 2 → raise `EvidenceShortError`(触发两级重试)
- 任一 evidence.quote 不在段落 paragraph_id 对应文本中 → raise `EvidenceSpanError`

---

## 7. Validation 详细规格

### 7.1 三路并发与 verdict 聚合

```python
# backend/src/novel_system/services/style_reference/validation/runner.py

class ValidationConfig(BaseModel):
    quantitative_pass_threshold: float = 0.8
    semantic_pass_threshold: float = 0.8
    semantic_min_score: float = 6.0
    auto_rewrite_max_attempts: int = 2
    auto_rewrite_on_verdicts: list[ValidationVerdict] = [ValidationVerdict.PARTIAL]

class ValidationOrchestrator:
    async def validate(self, req: ValidateRequest) -> ValidateResponse:
        if req.mode == "sync_only":
            # 同步快路径:本地计算,毫秒级返回
            quant, plag, forbidden = await asyncio.gather(
                self.quantitative.check(req.generated_text, profile),
                self.plagiarism.check(req.generated_text, profile_id),
                self._check_forbidden_local(req.generated_text, profile),  # 字面词扫描
            )
            verdict = self._compute_partial_verdict(quant, plag, forbidden)
            # 后台异步起 semantic
            report_id = self._persist_partial(quant, plag, forbidden, verdict)
            asyncio.create_task(self._run_semantic_background(report_id, req, profile))
            return ValidateResponse(
                report_id=report_id, mode_executed="sync_only",
                sync_result=PartialValidationReport(quant=quant, plag=plag,
                                                    forbidden=forbidden, verdict=verdict),
            )
        else:
            # 全量并发
            quant, sem, plag, forbidden = await asyncio.gather(
                self.quantitative.check(req.generated_text, profile),
                self.semantic.check(req.generated_text, profile, req.task_context),
                self.plagiarism.check(req.generated_text, profile_id),
                self._check_forbidden_semantic(req.generated_text, profile),
            )
            verdict = self._compute_verdict(quant, sem, plag, forbidden)
            report_id = self._persist_full(quant, sem, plag, forbidden, verdict)
            return ValidateResponse(
                report_id=report_id, mode_executed="async_full",
                polling_url=f"/api/v2/style-reference/reports/{report_id}",
            )
```

### 7.2 自适应阈值

```python
# backend/src/novel_system/services/style_reference/validation/quantitative.py

# 配置外置,严禁硬编码
ABSOLUTE_FLOORS = load_yaml("config/style_reference/tolerance_floors.yaml")
# tolerance_floors.yaml 示例:
#   avg_sentence_length: 3.0
#   sentence_length_std: 2.0
#   dialogue_ratio: 0.05
#   metaphor_density_per_1k: 1.0

def compute_tolerance(metric: str, baseline_std: float) -> float:
    return max(baseline_std * 1.25, ABSOLUTE_FLOORS.get(metric, 0.1))

class QuantitativeChecker:
    def check(self, generated_text: str, profile: StyleProfile) -> list[QuantitativeReport]:
        # 把 generated 包装成临时 ParsedDocument
        temp_doc = self._wrap_as_doc(generated_text)
        gen_metrics = self.metrics_engine.compute_all(temp_doc.paragraphs)
        
        reports = []
        baseline = profile.profile_json["metrics_baseline"]
        for metric_name, base in baseline.items():
            actual = gen_metrics.get(metric_name)
            if actual is None: continue
            
            tolerance = compute_tolerance(metric_name, base["std"])
            deviation = abs(actual - base["mean"]) / max(tolerance, 1e-6)
            reports.append(QuantitativeReport(
                dimension=self._dim_for_metric(metric_name),
                metric=metric_name,
                target=base["mean"],
                target_std=base["std"],
                actual=actual,
                tolerance=tolerance,
                passed=deviation <= 1.0,
                deviation_ratio=deviation,
            ))
        return reports
```

### 7.3 抄袭检测算法

`validation/plagiarism.py` 使用 **Rabin-Karp 滚动哈希**,时间复杂度 O(n+m)。**严禁使用 naive O(n×m) 实现**(5 万字 profile × 1000 字生成会慢到不可用)。

```python
class PlagiarismGuard:
    def __init__(self, ngram_size: int = 8, threshold_chars: int = 12):
        self.ngram_size = ngram_size
        self.threshold = threshold_chars
    
    def check(self, generated: str, profile_id: str) -> PlagiarismReport:
        # 1. 加载 profile 所有 quote_text
        all_quotes = self.quote_store.list_by_profile(profile_id)
        
        # 2. 构建多模式串的 Rabin-Karp / Aho-Corasick 索引
        # 3. 对 generated 滚动扫描,O(len(generated) + sum(len(quotes)))
        ...
```

**性能要求：** 5 万字 profile × 1000 字生成的 plagiarism check 必须在 100ms 内完成。

### 7.4 Forbidden Pattern 检查

```python
class ForbiddenChecker:
    async def check_semantic(
        self, generated: str, profile: StyleProfile
    ) -> list[ForbiddenHit]:
        """对每个 forbidden_pattern finding,LLM 判断是否触发"""
        hits = []
        for fp in profile.forbidden_patterns:
            result = await self.llm.call(
                prompt=self._build_forbidden_check_prompt(generated, fp),
                response_format="json",
            )
            if result["triggered"]:
                hits.append(ForbiddenHit(
                    pattern_statement=fp.statement,
                    matched_excerpt=result["excerpt"],
                    severity="error",
                ))
        return hits
    
    def check_local(self, generated: str, profile: StyleProfile) -> list[ForbiddenHit]:
        """sync_only 模式下,只跑字面词扫描(banned_terms scope=generation)"""
        ...
```

Prompt 模板:

```
下列模式是该作者明确不会使用的写作禁忌。请判断生成文本是否触发了任一模式,
引用具体句子作为证据。任一触发即 triggered=true。

禁忌模式: {forbidden_pattern.statement}
该模式的反向证据(参考): {forbidden_pattern.evidence}

生成文本: {generated_text}

输出 JSON: {"triggered": bool, "excerpt": str|null, "reasoning": str}
```

### 7.5 Verdict 计算

```python
def _compute_verdict(quant, sem, plag, forbidden) -> ValidationVerdict:
    if not plag.passed:
        return ValidationVerdict.PLAGIARISM
    
    if any(h.severity == "error" for h in forbidden):
        return ValidationVerdict.FAIL
    
    quant_pass_rate = sum(r.passed for r in quant) / len(quant) if quant else 1.0
    sem_pass_rate = sum(r.passed for r in sem) / len(sem) if sem else 1.0
    
    if quant_pass_rate >= 0.8 and sem_pass_rate >= 0.8:
        return ValidationVerdict.PASS
    if quant_pass_rate >= 0.5 or sem_pass_rate >= 0.5:
        return ValidationVerdict.PARTIAL
    return ValidationVerdict.FAIL
```

### 7.6 Auto-rewrite 循环

`scene_execution` / `qc_engine` 收到 `verdict=partial` 时:
1. 调用 `style_reference.suggest_rewrite(report)` 拿到具体修改建议
2. 重新调 LLM 生成,带入建议作为补充 prompt
3. 重新 validate,最多 2 轮,仍 partial 走 ReviewItem

`fail` 与 `plagiarism` 不自动重写,直接走 ReviewItem(人工兜底)。

---

## 8. 前端重构清单

### 8.1 `ReferenceLearningView.vue`(原地重构)

**删除区块：** 旧 segmentation 选择器、round-based 审阅流、两阶段 finding 列表。

**新增区块：**
- `<DimensionMatrix>` — 4×16 热力图(layer 行 × sub_dim 列),**每格必须显示 confidence + observation_count + quote_count + forbidden_pattern_count**
- `<FindingsByDimension>` — 按 sub_dim 折叠的审阅卡片,支持 finding_kind 过滤(observation / forbidden_pattern)
- `<ValidationReportCard>` — 三路并发结果(quantitative bar / semantic radar / plagiarism flags / forbidden_hits)
- `<InjectionStrategyPicker>` — 选择 A/B/C/mixed + 预览 InjectionBundle,包含两个子控件:
  - `<IntensitySlider>` — 0-100% 滑块,实时显示"将注入 N 条观察/维度"
  - `<DimensionMultiSelect>` — 基于 DimensionMatrix 改造,16 格可勾选
- `<ProfileApplyDialog>` — scope(project/scene/character)+ scope_ref_id 选择 + apply
- `<BannedTermsEditor>` — 用户自定义禁词,支持选 scope(generation / extraction)
- `<PreviewPanel>` 🆕 — apply 前的预览面板,展示 3 段示例(对话/环境/动作或心理)及其 ValidationReport
- `<InjectionBundlePreview>` — 实时显示当前 strategy + intensity + selected_dimensions 组合下的 system_prompt 与 few_shot

**保留：** 上传/导入区、Profile 列表、ReviewInbox 跳转链。

### 8.2 DimensionMatrix 组件契约

```typescript
interface DimensionCell {
  dimPath: string;              // "language.rhetoric"
  confidence: "high" | "medium" | "low" | "skip";
  observationCount: number;
  quoteCount: number;
  forbiddenPatternCount: number;
  inputLevel: "high" | "medium" | "low" | "skip";  // 来自 assess_input_size
}
```

单元格样式规则:
- `skip`:灰色 + "语料不足"
- `low`:浅黄
- `medium`:浅绿
- `high`:深绿
- hover 显示 tooltip:`12 obs / 28 quotes / 2 forbidden`
- 点击进入 `FindingsByDimension` 视图过滤到该 dim

### 8.3 `stores/referenceLearning.js`

```js
state: {
  books: [],
  currentBook: null,
  paragraphTypeHistogram: {},
  classifierCalibration: null,
  inputAssessment: null,
  runs: [],
  findings: {                          // 按 sub_dim 分桶,含 finding_kind
    language: {
      sentence_structure: { observations: [], forbidden_patterns: [] },
      vocabulary: { observations: [], forbidden_patterns: [] },
      // ...
    },
    // narrative / scene / theme
  },
  profiles: [],
  currentProfile: null,
  bindings: [],
  validationReports: [],
  injectionPreview: null,
  previewSamples: null,        // 🆕 三段示例 + 各自 ValidationReport
}
```

删除：`currentRound` / `roundFindings`(round 概念整体作废)。

### 8.4 API 客户端

`frontend/src/lib/api/references.js` 重命名为 `frontend/src/lib/api/styleReference.js`;旧文件保留 deprecated stub 一个版本周期(所有方法返回 410)。新方法:

```
importPath / importUpload / listBooks / getBook / deleteBook / reclassifyBook
startRun / getRun / cancelRun / getFindings / reviewFinding / userFeedbackFinding / synthesize
listProfiles / getProfile / previewProfile / applyProfile / listBindings / deleteBinding
inject / validate / getReport / listReports
```

### 8.5 路由 metadata(`router.js`)

- 保留 `writerOrder` 位置不变
- `label` 改为"风格参考",`stepLabel` 改为"学习风格"
- `nextViews: ["workbench", "writerRoom", "reviewInbox"]`
- `cacheMode: "keep"`(findings 状态大,避免反复请求)

### 8.6 新增组件目录

`frontend/src/components/styleReference/`:
- `DimensionMatrix.vue` / `DimensionConfidenceBar.vue` / `FindingCard.vue`
- `ValidationReportCard.vue` / `InjectionStrategyPicker.vue` / `InjectionBundlePreview.vue`
- `IntensitySlider.vue` / `DimensionMultiSelect.vue`
- `ProfileApplyDialog.vue` / `BannedTermsEditor.vue` / `PreviewPanel.vue`

---

## 9. 测试策略

### 9.1 后端(全新建,旧 `test_reference_learning.py` 删除)

- `test_style_reference_ingest.py` — 路径/上传/checksum 去重/段类型分类/锚定校准/input_assessment
- `test_style_reference_metrics.py` 🆕 — 25 MetricName 的纯函数计算正确性(误差 <1%)
- `test_style_reference_extractors.py` — 4 层 16 sub-dim happy path + EvidenceShortError 触发两级重试 + BannedAdjectiveError + forbidden_pattern 提取
- `test_style_reference_evidence_retry.py` 🆕 — 三种重试路径(初次通过 / 第一级补抽通过 / 第二级全量重抽 / 全部失败丢弃)
- `test_style_reference_profile_synthesizer.py` — coverage 计算 / source_finding_ids 完整性 / metrics_baseline 与 scene_samples_index 落盘
- `test_style_reference_injection.py` — A/B/C 策略 InjectionBundle 形状 / TaskType 默认表 / intensity 控制 obs 数量 / selected_dimensions 过滤 / refresh_every_chars / anti_plagiarism_block 固定存在
- `test_style_reference_validation.py` — 三路并发顺序无关 / sync_only vs async_full 双路径 / plagiarism Rabin-Karp 边界与性能(100ms 内)/ 自适应 tolerance 同段文本对不同 profile 阈值不同 / verdict 聚合 / forbidden_pattern 触发 fail
- `test_style_reference_routes.py` — endpoint 黑盒 + 幂等头返回
- `test_style_reference_materialization.py` — apply 后 4 集合行数 / hash 稳定
- `test_style_reference_cleanup.py` — drop_old → create_new 后旧表不存在 / ReviewItem 残留清空
- `test_style_reference_preview.py` 🆕 — preview endpoint 生成 3 段且 ValidationReport 非空

**conftest 新增 fixture：** `style_reference_book_factory` / `fake_llm_extractor` / `style_reference_clean_db` / `mock_metrics_engine`。

### 9.2 黄金测试源材料硬约束

**必须使用公版作品,严禁使用江南、龙族,以及其他在 Anthropic 训练数据高概率出现的当代 IP。**

推荐源材料(公版,中文):
- 鲁迅短篇:《孔乙己》《故乡》《祝福》《阿Q正传》
- 老舍短篇:《断魂枪》《月牙儿》
- 沈从文:《边城》(节选)
- 朱自清散文:《背影》《荷塘月色》
- 张爱玲早期作品在不同地区版权状态不同,**避免使用**

测试样本组织:
```
backend/tests/golden/style_reference/
├── corpus/
│   ├── luxun_short_stories.txt        # ~80k 字,主力测试集
│   ├── laoshe_short_stories.txt       # ~50k 字,对照测试集
│   └── shenxc_biancheng_excerpt.txt   # ~30k 字,短篇下限测试
├── expected/
│   ├── luxun_profile_metrics.json     # 期望的硬指标 ± tolerance
│   ├── luxun_sub_dim_keywords.json    # 每 sub_dim 期望出现的关键词
│   └── faux_eileen_chang_sample.txt   # 故意构造的"伪张爱玲腔",必须 validation fail
└── README.md                           # 说明源材料版权状态
```

**CI 检查：** 任何 commit 中出现"江南""龙族""路明非""路鸣泽""昂热""恺撒"等关键词到 golden 目录立即 fail。

### 9.3 前端(全新建)

`styleReference.api.spec.js` / `styleReference.store.spec.js` / `styleReferenceView.spec.js` / `DimensionMatrix.spec.js`(4 种 confidence 状态)/ `InjectionStrategyPicker.spec.js` / `IntensitySlider.spec.js` / `DimensionMultiSelect.spec.js` / `ValidationReportCard.spec.js` / `ProfileApplyDialog.spec.js` / `PreviewPanel.spec.js`

---

## 10. Phase 切分

### Phase 1 · MVP(2-3 周)

**必须：**
- DB schema 11 张表 + 双 Alembic revision + cleanup 脚本
- `ingest` + `assess_input_size` + `segmentation`(8 段类型分类器 + 锚定集校准)
- `MetricsEngine` 全套(25+ MetricName)纯函数实现,作为基础设施
- 仅 `language` + `narrative` 两层(8 sub-dim)抽取 + Pydantic + ≥2 evidence + banned_adjective + 两级重试 + forbidden_pattern 共表
- `quotes` 表 + `evidences` 多对多 + `findings.finding_kind`
- `synthesize_profile` 含 `metrics_baseline` 与 `scene_samples_index` + `materialization` → ReviewItem → 4 集合
- API 路由:books / runs / findings / synthesize / profiles / preview / apply(**不含** inject / validate)
- 前端:`DimensionMatrix`(8 格,带 confidence)+ `FindingsByDimension` + `ProfileApplyDialog` + `PreviewPanel`

**验收：**
- 单本 5 万字小说从导入到 apply 端到端 < 8 分钟
- `stats_json.metrics` 至少 25 个 MetricName 有值,`input_assessment` 与 `classifier_calibration` 字段非空
- ReviewInbox 出现新 ReviewItem 且能 approve
- apply 后 `style_rules` 行数 ≥12
- `POST /profiles/{id}/preview` 返回 3 段示例 + 3 份 ValidationReport(此时 validate 走 sync_only 简化路径)
- DimensionMatrix 8 格全带 confidence 与 obs/quote 计数
- LLM 成本基线报告产出(单本 5 万字 token 中位数与 P95)

### Phase 2 · 全维度 + 验证(2-3 周)

- 补齐 `scene` / `theme` 两层(再 8 sub-dim)
- `validation/` 三路并发(quantitative + semantic + plagiarism + forbidden semantic)落地
- 自适应阈值(`tolerance = max(std × 1.25, 绝对下限)`)
- Validate 同步/异步双路径(`sync_only` / `async_full`)
- A/B 注入策略完整实装 + `style_intensity` + `selected_dimensions`
- `InjectionBundle.refresh_every_chars` + scene_execution 续写循环按字数刷新
- `anti_plagiarism_block` 固定嵌入 SystemPromptFragments
- Auto-rewrite 循环(`auto_rewrite_max_attempts=2`,partial verdict 自动重试)
- `inject` / `validate` endpoint + 接入 `scene_execution` / `qc_engine`
- 前端 `ValidationReportCard` / `InjectionStrategyPicker` / `IntensitySlider` / `DimensionMultiSelect` / `InjectionBundlePreview`

**验收：**
- scene_generation 调用 inject 成功,生成长文本 5000 字以上,inject 端点被调用 ≥3 次(防漂移生效)
- qc_engine 落盘 gate 调用 validate(sync_only)P95 延迟 < 500ms,UI 不卡顿
- async_full 模式三路并发,plagiarism flag 在阈值上可触发
- 故意构造"伪张爱玲腔"文本,validation 必须 fail(forbidden_pattern 触发)
- 同一段生成文本对村上 profile 与余华 profile 验证时,tolerance 必须不同
- DimensionMatrix 16 格全亮,带 confidence
- Phase 2 验收报告附 KEY_CHAPTER 调用占比(决定 RAG 是否提前到 Phase 2 末)

### Phase 3 · RAG + 性能 + 高级特性(1-2 周,可推迟)

- C 策略 RAG:`vector_store.py` 建**三粒度**索引(sentence/paragraph/scene)+ rerank,**不简化为单粒度**
- 抽取并发优化
- 持续校准回路:`POST /findings/{id}/user-feedback` + 前端 👍👎,聚合更新 confidence
- 预留 `style_ref_*` 在 `local_fast` 路由切到本地 Qwen3-14B 的 hook(不强制接入)

**验收：**
- 单本抽取 P95 时长比 Phase 2 下降 ≥40%
- RAG 召回 hit@5 各粒度独立测量 ≥0.7
- 用户 feedback 反映到 finding.confidence 字段(单测覆盖)

---

## 11. 风险与回滚

| # | 风险 | 概率 | 影响 | 缓解 | 回滚 |
|---|---|---|---|---|---|
| 1 | 旧 profile 数据丢失引发投诉 | 高 | 高 | drop 前 JSON 备份;前端公告"旧风格档案不可迁移,需重学" | 各 phase 前均可恢复旧表 |
| 2 | LLM 抽取成本失控(16 × N × 多轮重抽)| 高 | 中 | 每 sub_dim `samples_per_sub_dimension` 默认 20-25;两级重试中第一级定向补抽成本 ≤30% 完整 extract;input_assessment skip 层完全跳过;`STYLE_REF_DRY_RUN=1` env 切 mock | env flag 切 dry_run |
| 3 | 段落分类器准确率不足拖垮后续统计 | 中 | 高 | 锚定集校准(前 200 段 quality_balanced);agreement <0.85 自动 fallback 整本走强模型 | fallback 已自动 |
| 4 | 前端用户感知断档(UX 大改) | 中 | 中 | 保留 router 位置 + label;首次进入显示"模块已升级"引导;DimensionMatrix 必带 confidence 可视化让用户感知到提升 | 不回滚(不破坏数据)|
| 5 | 与 ReviewItem 评审流耦合点变更引入回归 | 中 | 高 | `materialization.py` 完全复用 `services/versioning/review_materialization.py`;新 review_id 前缀 `review_style_ref_*` 与旧 `review_apply_*` 物理隔离 | 关闭 apply endpoint |
| 6 | scene_execution / qc_engine 调用 inject/validate 超时 | 中 | 高 | inject < 50ms(库内拼装,无 LLM,scene_samples_index 直读);validate 双路径,sync_only < 500ms,async_full 异步 | `STYLE_REF_VALIDATE_ENABLED` flag 一键关 |
| 7 | Alembic drop 不可逆(SQLite 场景)| 中 | 高 | 0036 `downgrade()` 实现 schema-only 重建空旧表防误 down | 0037 单独 downgrade,保留新数据 |
| 8 | 4×16 抽取质量不达预期 | 中 | 中 | metrics_anchor 作为 prompt 先验拉对齐;黄金测试每 sub_dim 至少 1 条;Phase 1 仅 8 sub_dim 试水 | Phase 2 可冻结 scene/theme,交付半盘 |
| 9 | 自适应阈值校准困难 | 中 | 中 | 灰度发布,前 N 次生成的 quantitative report 人工复核;tolerance_floors.yaml 可调 | 临时把 floor 调大 |
| 10 | 长文本防漂移失效(scene_execution 不按 refresh_every_chars 重调 inject)| 中 | 高 | 集成测试模拟 5000 字续写,inject 调用次数断言 ≥3 | 关闭 LONG_FORM_CONTINUATION 任务,降级到分场景生成 |
| 11 | 抄袭事前预防失效(anti_plagiarism_block 没真的进 system prompt)| 低 | 高 | SystemPromptFragments 强类型保证 anti_plagiarism_block 字段必填;集成测试断言 system prompt 含该段 | 加大 plagiarism 阈值兜底 |

**回滚决策点：** Phase 2 验证端点接入 scene_generation / qc_engine **之前**可完整回滚到旧版本;接入后只能 feature flag 局部关停。

---

## 12. 与现有系统的集成

需要修改的现有文件:

- `backend/src/novel_system/services/projects.py` — 替换 `ReferenceLearningService` 引用为 `StyleReferenceService`;项目级 profile 绑定改走 apply endpoint
- `backend/src/novel_system/services/scene_execution.py` — 生成前调用 inject;**长文本生成(LONG_FORM_CONTINUATION)续写循环按 `bundle.refresh_every_chars` 重新调 inject**:

  ```python
  async def generate_long_form(scene_id, profile_id, ...):
      bundle = await style_reference.inject(InjectionRequest(
          profile_id=profile_id,
          task_type=TaskType.LONG_FORM_CONTINUATION,
          scope_ref_id=scene_id,
      ))
      
      accumulated = ""
      generated_chars_since_refresh = 0
      while not done:
          chunk = await llm.generate(prompt=build_prompt(bundle, accumulated), ...)
          accumulated += chunk
          generated_chars_since_refresh += len(chunk)
          
          # 防漂移触发
          if bundle.refresh_every_chars > 0 and \
             generated_chars_since_refresh >= bundle.refresh_every_chars:
              bundle = await style_reference.inject(InjectionRequest(
                  profile_id=profile_id,
                  task_type=TaskType.LONG_FORM_CONTINUATION,
                  scope_ref_id=scene_id,
                  context_text=accumulated[-2000:],   # 最新 context 给 RAG 用
              ))
              generated_chars_since_refresh = 0
  ```

- `backend/src/novel_system/services/qc_engine.py` — 落盘前调用 validate(`mode="sync_only"`),verdict ∈ {fail, plagiarism} 时阻塞 + 生成 ReviewItem;后台 async_full 完整 report 入库
- `backend/src/novel_system/api/app.py` — 删除 `reference_books` router,新增 `style_reference` router
- `backend/src/novel_system/api/routes/__init__.py` — 同步替换导入
- `backend/src/novel_system/tools/reset_author_state.py` — 删除对 ReferenceBook 的清理,改为调用 `style_reference.cleanup`
- `backend/src/novel_system/services/reference_safety.py` — 保留通用 safety,删除 ReferenceBook 专用断言

**Snowflake / WriterRoom 调用契约：** 两者均通过 `POST /api/v2/style-reference/profiles/{id}/apply` 把 profile 绑定到 `scope=project|scene|character`;后端创建 ReviewItem 进入 ReviewInbox;审通过后 materialization 把规则写入既有 `style_rules` / `narrative_patterns` / `banned_rule_clusters` / `calibration_lines` 4 集合——下游 prompt_builder / qc 链路无需改动。

---

## 13. 不做清单(明确边界)

1. **CLI 入口**:项目是 FastAPI Web 应用,不引入 click/typer
2. **本地 Qwen3-14B 接入**:仅预留 `local_fast` 路由 hook,Phase 3 后再评估
3. **旧 → 新 profile 迁移工具**:全清重抽,不投入
4. **多语言原文支持**:当前面向中文,i18n 推迟
5. **多本书合成单一 profile(多作者融合)**:单 book → 单 profile
6. **实时流式抽取展示(SSE)**:findings 走轮询
7. **profile diff / 版本对比 UI**:仅保留 `version_tag` 字段,可视化推迟
8. **段落手工编辑**:仅提供 reclassify endpoint 重跑分类器,不做单段编辑
9. **导出 profile 到 PDF/Markdown 报告**:仅 JSON 输出
10. **literary_eval ↔ profile 双向打通**:仅 qc_engine 单向调用 validate

---

## 14. PR 切分建议

按下列顺序提 PR,每个 PR 独立可 review、可回滚。每个 PR 描述中明确引用本文档对应章节:

1. **PR-1(schema 落地,无业务)**:新增 11 张表 + 双 Alembic + cleanup 脚本 + 仅 `schemas.py` / `dimensions.py` / `errors.py` / `repository.py`;CI 验证 migration up/down。覆盖 §4 全部。
2. **PR-2(ingest + metrics + segmentation)**:完成导入、`assess_input_size`、MetricsEngine 全套纯函数、段类型分类器 + 锚定集校准 + 黄金测试。覆盖 §6.2、§6.3、§6.4。
3. **PR-3(extractors 半盘)**:language + narrative 两层 + `base.py` 两级重试 + Pydantic 强校验 + `finding_kind` 双 kind 抽取 + `metrics_anchor` 注入。覆盖 §6.5、§6.6、§6.7。
4. **PR-4(synthesize + materialization + Phase 1 routes + preview)**:profile 聚合(含 metrics_baseline + scene_samples_index)+ ReviewItem 物化 + Phase 1 路由清单 + `preview` endpoint。
5. **PR-5(前端 Phase 1)**:原地重构 view + store + api client + DimensionMatrix 带 confidence + PreviewPanel + 删除旧文件 + deprecated stub。
6. **PR-6(extractors 全盘)**:scene + theme 两层。
7. **PR-7(validation 三路 + 双路径)**:quantitative 自适应阈值 + semantic 强制 quote 引用 + plagiarism Rabin-Karp + forbidden_pattern 检查 + sync/async runner。
8. **PR-8(injection A/B + scene_execution + qc_engine 接入 + auto-rewrite)**:`SystemPromptFragments` 强类型 + `style_intensity` + `selected_dimensions` + `refresh_every_chars` + `anti_plagiarism_block` 固定段 + 集成两个下游服务 + auto_rewrite 循环 + feature flag。
9. **PR-9(前端 Phase 2 组件)**:`ValidationReportCard` / `InjectionStrategyPicker` / `IntensitySlider` / `DimensionMultiSelect` / `InjectionBundlePreview`。
10. **PR-10(Phase 3 RAG + 持续校准,可选)**:`vector_store` 三粒度索引 + RAG + rerank + user-feedback endpoint。

---

## 15. 验证(端到端)

实施完成后,按以下步骤验证:

1. **数据库迁移：**
   ```powershell
   cd backend
   python -m alembic upgrade head
   python -m alembic downgrade -1   # 验证 0037 单独可回滚
   python -m alembic upgrade head
   ```

2. **后端单测：**
   ```powershell
   python -m pytest backend/tests/test_style_reference_*.py -v
   ```

3. **Windows 整盘 CI：**
   ```powershell
   powershell -ExecutionPolicy Bypass -File scripts/verify_windows.ps1
   ```

4. **前端单测 + smoke：**
   ```powershell
   cd frontend
   npm run test
   npm run test:e2e -- --grep "styleReference"
   ```

5. **端到端冒烟：**
   - 启动 `.\start-dev.cmd`
   - 进入"风格参考"视图 → 上传一份 5 万字鲁迅短篇集 TXT
   - 启动 run → 等待 16 个 sub-dim 各产出 finding(language/narrative 各 ≥3 obs,scene/theme 各 ≥2 obs;每 sub_dim 至少 1 个 forbidden_pattern)
   - DimensionMatrix 16 格全显示 confidence 与计数
   - 审核全部 finding(含 forbidden_pattern)→ 调用 synthesize 得到 profile
   - 调用 `POST /profiles/{id}/preview` → 拿到 3 段示例 + 3 份 ValidationReport,全部 PASS 或 PARTIAL
   - 调用 apply 绑定到 project → 进入 ReviewInbox 看到 `review_style_ref_*` 条目 → 批准 → 检查 `style_rules` 集合有新增
   - 调用 inject 接口(前端 InjectionBundlePreview)→ 拿到 SystemPromptFragments,确认包含 anti_plagiarism_block,refresh_every_chars 字段非空
   - 调整 IntensitySlider 到 0.3 → 重新 inject → 确认 observations_by_dim 中每维 obs 数减少
   - 取消选中"语言层"维度 → 重新 inject → 确认 system_prompt 不含语言层 observations
   - 调用 validate 接口(粘贴一段含原文 8-gram 重叠的文本)→ verdict 应为 `plagiarism`
   - 调用 validate(粘贴一段刻意构造的"伪张爱玲腔"文本,堆砌华丽形容词)→ verdict 应为 `fail`(forbidden_pattern 触发)

6. **回归验证：** SnowflakeWorkbench 走一次端到端,确认 scene_generation 在 profile 已 apply 时拿到 InjectionBundle 并落入 prompt;LONG_FORM_CONTINUATION 任务下生成 5000 字以上时,inject 端点被调用 ≥3 次(防漂移生效)。

---

## 16. Critical Files for Implementation

- `backend/src/novel_system/services/reference_learning.py`(待整体删除)
- `backend/src/novel_system/db/models.py`(line 1372-1466 待删除,新增 style_reference_* 模型)
- `backend/src/novel_system/api/routes/reference_books.py`(待删除)
- `backend/src/novel_system/api/app.py`(router 注册替换)
- `backend/src/novel_system/services/projects.py`(引用替换)
- `backend/src/novel_system/services/scene_execution.py`(接入 inject + 续写循环防漂移)
- `backend/src/novel_system/services/qc_engine.py`(接入 validate sync_only + 异步 async_full)
- `backend/src/novel_system/services/versioning/review_materialization.py`(保留,被新 materialization 调用)
- `backend/src/novel_system/tools/reset_author_state.py`(清理逻辑替换)
- `config/models.yaml`(task 路由增删)
- `config/prompts.yaml`(template 增删)
- `frontend/src/views/ReferenceLearningView.vue`(原地重写)
- `frontend/src/stores/referenceLearning.js`(state 重写)
- `frontend/src/lib/api/references.js`(重命名 + deprecated stub)
- `frontend/src/router.js`(metadata 更新)

---

## 附录 A · 配置文件清单

新增配置目录 `config/style_reference/`:

### A.1 `tolerance_floors.yaml`

```yaml
# Validation 自适应阈值的绝对下限
# 公式: tolerance = max(原作该 metric 的 std × 1.25, 此处下限)
avg_sentence_length: 3.0
sentence_length_std: 2.0
short_sentence_ratio: 0.05
long_sentence_ratio: 0.05
punctuation_density_per_1k: 5.0
dash_em_density_per_1k: 1.0
ellipsis_density_per_1k: 1.0
semicolon_density_per_1k: 0.5
question_density_per_1k: 1.0
classical_word_ratio: 0.02
colloquial_marker_ratio: 0.02
metaphor_density_per_1k: 1.0
personification_density_per_1k: 0.5
dialogue_ratio: 0.05
psychology_ratio: 0.05
description_env_ratio: 0.05
description_char_ratio: 0.05
action_ratio: 0.05
narration_ratio: 0.05
transition_ratio: 0.03
flashback_ratio: 0.03
sensory_visual_per_1k: 2.0
sensory_auditory_per_1k: 1.0
sensory_olfactory_per_1k: 0.5
sensory_tactile_per_1k: 0.5
sensory_gustatory_per_1k: 0.5
```

### A.2 `input_thresholds.yaml`

```yaml
# 每层的输入量门槛: (skip 下限, low 下限, high 下限)
language:
  skip: 10000
  low: 30000
  high: 50000
narrative:
  skip: 20000
  low: 50000
  high: 80000
scene:
  skip: 10000
  low: 30000
  high: 50000
theme:
  skip: 30000
  low: 80000
  high: 150000
```

### A.3 `extraction.yaml`

```yaml
metrics:
  use_all_paragraphs: true        # MetricsEngine 用全文计算

observations:
  samples_per_sub_dimension:
    language: 25
    narrative: 20
    scene: 25
    theme: 15
  stratified_sampling: true       # 按 paragraph_type 分层抽
  min_samples_per_type: 3         # 每种 type 至少抽 3 段

retry:
  max_targeted_retries: 2         # 第一级:单 obs 定向补抽
  max_full_retries: 1             # 第二级:整 sub_dim 重抽

forbidden_pattern_target_count: [0, 3]   # 每 sub_dim 期望产出 0-3 条
observation_target_count: [3, 8]         # 每 sub_dim 期望产出 3-8 条
```

### A.4 `banned_adjectives.yaml`

```yaml
- 文笔优美
- 画面感强
- 叙事流畅
- 意境深远
- 笔触细腻
- 情感真挚
- 文采斐然
- 韵味无穷
- 行云流水
- 跃然纸上
- 入木三分
```

### A.5 `anti_plagiarism_template.txt`

```
## 严格禁止
- 复用或微改任何参考样本中的完整句子
- 直接搬运超过 5 个连续字符的独特表达(常用词、人名、地名除外)
- 参考样本中的意象(如承载象征意义的具体物象)可重复使用,
  但承载这些意象的句子必须完全重写
- 若你不确定某个表达是否来自参考样本,默认认为是,改写它

此外,以下专有名词严禁出现在生成文本中(可能引发版权或角色混淆):
{banned_terms_list}
```

### A.6 `sensory_lexicon.yaml`

```yaml
visual:
  - 看 望 见 瞧 瞥 凝视 注视 端详 窥 瞄 扫 盯
  - 光 影 色 亮 暗 明 蓝 红 黑 白 灰 闪烁 朦胧 透明

auditory:
  - 听 闻 响 叫 喊 嚷 嘶 吼 啸 喃喃 呢喃 嗡嗡 哗哗 滴答
  - 声 音 响 静 寂 嘈杂 喧嚣 悦耳 刺耳

olfactory:
  - 闻 嗅
  - 香 臭 腥 膻 焦 霉 馥郁 芬芳 刺鼻 甘甜 苦涩 气味 味道

tactile:
  - 摸 触 碰 拍 抚 攥 握 捏 戳
  - 冷 热 凉 暖 烫 冰 软 硬 滑 糙 痒 麻 痛 钝 锐

gustatory:
  - 尝 品 嚼 咽 吞 含 啜
  - 甜 咸 酸 苦 辣 鲜 涩 腻 清淡 醇厚
```

### A.7 `prompts/` 子目录

包含所有 LLM 节点的 prompt 模板,文件名与 `models.yaml` 的 task_name 一致:

```
prompts/
├── style_ref_paragraph_classify.txt
├── style_ref_extract_language.txt
├── style_ref_extract_narrative.txt
├── style_ref_extract_scene.txt
├── style_ref_extract_theme.txt
├── style_ref_supplement_evidence.txt
├── style_ref_synthesize_profile.txt
├── style_ref_validate_semantic.txt
├── style_ref_validate_forbidden.txt
├── style_ref_fewshot_select.txt
├── style_ref_rag_rerank.txt
└── style_ref_preview_generate.txt
```

---

## 附录 B · 数据安全与隐私

- 用户上传作品不上传到任何外部服务(LLM 调用除外,需在 UI 明示)
- `cloud_policy` 三档以代码事实为准：`local_only / segments_only / allow_full_cloud`
- 所有 profile 与原文样本支持一键导出 / 删除
- 黄金测试用公版材料,严禁江南/龙族等当代 IP(详见 §9.2)

---

## 附录 C · 给 Claude Code 的最终实施指令

1. **读完本文档**。如有任何项"工程上无法实现"或"与现有代码库存在硬冲突",**立即列出**给用户决策。不要私自变通。
2. 按 §14 PR 切分顺序逐个提交。**每个 PR 描述中明确引用本文档对应章节编号**(如 PR-3 引用 §6.5/§6.6/§6.7)。
3. **三条不能让步的硬约束**(其他项可在 PR review 中讨论调整):
   - **A2 forbidden_pattern 与 observation 共表机制**——这是相比现状 reference_learning 能拉开质量差距的关键
   - **A6 Evidence 两级重试机制**——这是质量与成本平衡的关键
   - **B9 黄金测试源材料公版约束**——这是版权与训练数据污染的双重红线
4. 任何 PR 评审中提出的新问题,**以追加 commit 形式呈现,不要直接修改本文档**。文档版本由用户掌握。
5. PR-1 启动前,先确认你能找到本文档所有引用的 §章节锚点;如有歧义,**先提问再动手**。

---

## 附加勘误（2026-05-31）

- 本手册中关于旧 `reference_books` 路由与旧 `reference_*` ORM 的描述仅保留为历史迁移背景；当前运行时代码已完成下线，不再作为主链依赖。
- `reclassify` 在运行时代码中已真实落地，不再是占位接口；执行时会清理既有 style_reference 衍生数据并要求重新抽取。
- 若手册中仍出现 “Phase 1 仅 language + narrative 8 维” 的阶段性表述，应以当前产品事实为准：主前端路径默认覆盖 4 层 16 个 sub-dim。

**手册结束。v1.1 共 16 节 + 3 附录,Claude Code 拿到此文档应能直接开始 PR-1 实施。**
> 2026-05-31 运行时对齐说明(优先级高于本文早期草案描述)：
> 正式注入契约是 `SystemPromptFragments`，对外仅保留两个 `injection-preview` 端点，不再公开 `/inject` / `InjectionBundle` HTTP API。
> 长文续写现在由 `SceneGenerationService.generate_long_form_continuation(...)` 内部消费 `long_form_continuation` 节点，并按 `refresh_every_chars` 在续写片段之间重新注入。
> `/api/v1/reference-books/*` 与 `reference_learning.py` 已下线；legacy ORM / `reset_style_reference` 已从当前运行时移除，`cleanup.py` 仅保留 metric events 清理。历史 Alembic migration 保留仅作历史记录。
