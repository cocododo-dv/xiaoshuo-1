# 风格参考 · 保真修补（2026-09-14，post-Step 1–2 评估的修复方案）

> 依据：2026-09-14 对模块的第三次评估（三份代码审计 + 本机 190 万字参考书上的真实数据探针）。
> 目标不变：最大程度模仿参考作者，从小到大、从大到小每一层都像；保守行为只做开关，不做默认。
> 本文是方案；§8 在实施后补写。无绑定与 `neutral_first` 的行为逐字不变（阅读对照组）。

## 0. 评估结论摘要

设计方向已对（样例占首稿提示一半以上、房风门确实让位、硬门保留），但：

1. **覆盖率天花板**：样例窗口只能来自抽取证据引文（每段型前 12 条、轮换池前 30 个），一个项目最多看到全书约 6%，一场约 2%；16 个子维度的观察语句根本不进画像。
2. **确定性层在真实书上出错**：章题正则漏掉带空格 / 双语副题（「第一幕 卡塞尔之门 The Gate to Cassell」），前两卷被当成两三个巨型章；盗版站声明与脚注成了章首 / 章尾样例；人称统计不排除对白，第三人称小说判成「混用」；文言标记表含「也 / 其 / 者」，现代网文得到「文言虚词比例较高频」。
3. **管线仍在稀释声音**：安全修复与长度补丁不带 `[STYLE_REFERENCE]` 且指令写死房风扩缩法；段落归一化器按全书均值机械合并、不认对白行；`style_draft` 系统提示「never imitate their length」与用户提示「作者尺度优先」矛盾；近终稿默认重写简报是房风清单、未知失败类被强转为可自动重写类；`soft_qc` 走小模型且目标含「emotional clarity」，补丁整场重发；新鲜度预算把作者手笔当复读；规划节点与写手侧路径看不到参考。
4. **作者看不见**：六种 `STYLE_*` 通知无人渲染；预览不带场景种子；哪一场用了哪几个窗口无记录；没有任何「像不像」的读数。
5. **死代码**：候选重排 / 风格反馈 / 盲选门（N=1）、量化验证（不产 issue）、语义 critic（看不到原文、只能手动）、benchmark 与 RAG A/B（无 CLI）、指标聚合器（无调用方）、materialization ReviewItem（喂给已删除的 versioning）。

## 1. 工作包

### WP1 · 管线让位补全（有绑定时）

| 项 | 改动 |
|---|---|
| 1.1 修复带风格 | 安全修复、长度补丁及其后续在 `style_bound` 时注入 `[STYLE_REFERENCE]`（`context_text=None`，不按被拒稿的异常篇幅算「当前」偏差）；修复 / 补丁指令与 `style_length_patch` v4、`style_salvage_patch` v2 模板：扩缩用作者自己的手段，不用「加动作反应、删修饰」 |
| 1.2 形状归一化让位 | `_normalize_style_paragraph_shape`（机械合并段落）与 `_assess_style_anchor_conformance`（段密度 / 分号包络 → 去模板改写）在 `style_bound` 时只记 `deferred_to_reference`，不改稿、不触发 |
| 1.3 复读模板自洽 | `style_draft` v11：长度带是工作范围、作者尺度优先；偏好画像句加「不与作者手法冲突」限定 |
| 1.4 近终稿自动重写收紧 | `_normalize_acceptance_payload` 记录 `failure_class_coerced`；`style_bound` 时被强转的失败类或没有评审简报 → `auto_rewrite_blocked`，停在 `revision_required` 交给作者，不再用房风默认简报整场重写 |
| 1.5 soft_qc | 默认路由 gpt-5-mini → gpt-5（`models.yaml` + 节点规格）；`soft_qc` v7：有风格块时不以「emotional clarity」为目标；软补丁在 `style_bound` 时温度 0.3、未点名的句子逐字保留 |
| 1.6 新鲜度预算 | `style_bound` 时不再发动作模板 / 意象场 / 句形三张表（它们是从本系统自己按作者手笔写成的前几场里挖出来的），只保留近场 n-gram、语义复读、全书禁用表达（非刻意复沓时） |
| 1.7 成稿门警告 | `style_bound` 时 21 维词表的房风风险不再生成 Q3 警告（`literary_warnings_unresolved` 不因词表撞车永久为真）；`risky_dimensions` 仍进审计 |

### WP2 · 导入层按真实书修

| 项 | 改动 |
|---|---|
| 2.1 章题正则 | 副题允许含空格与双语（不含句末标点、≤40 字），「第一幕 卡塞尔之门 The Gate to Cassell」可识别 |
| 2.2 副文本 | `text_utils.is_paratext_paragraph`：站点声明（域名 / URL / 「用户上传」「本站」+「电子书 / 下载 / 存储」）、脚注（`[n]` / `【n】` / `注：`）；新导入的书在切段前剥离；已导入的书在结构样例、样例窗口、抽取采样处过滤 |
| 2.3 人称 | 声音签名的人称占比只统计叙述（剥离引号内对白）；重建 `voice_baseline.yaml`，再生成黄金期望 |
| 2.4 文言标记 | `classical_word_ratio` 只认文言用法：句末 也 / 矣 / 焉 / 哉 / 乎 / 兮，排除 之后 / 之前 / 其他 / 其实 / 或者 / 作者 等现代复合词 |
| 2.5 结构样例 | 章首 / 章尾取第一 / 最后一个正文段；副文本不入样例 |

### WP3 · 选窗与覆盖（全书可进样例）

| 项 | 改动 |
|---|---|
| 3.1 全书窗口索引 | 合成期从段落表确定性切出 `exemplar_windows`（≤60 段 / ≤4000 字、不跨章题、不含副文本），每窗记起止段、字数、段型构成、对白占比、位置（开章 / 收章 / 中段）、辨识度分；写入 `profile_json`（冻结键） |
| 3.2 渲染期选窗 | 根哈希一致时候选 = 全书窗口；键 =（漂移优先, 场景段型匹配, 位置匹配, 辨识度, 完整度）；轮换池 = 前 k×倍数（倍数 3→6）；跨书分散（相邻窗口不同时入选）；旧画像无索引 → 现有证据引文路径不变 |
| 3.3 预算 | `few_shot_k` 10→12、`few_shot_block_max_chars` 40000→60000、`STYLE_PASS_INPUT_TOKEN_BUDGET` 64000→96000；小上下文仍用 `NOVEL_SYSTEM_SCENE_INPUT_TOKEN_BUDGET` 收紧 |
| 3.4 首稿场景上下文 | `context_text=None` 时按场景卡（scene_type / rendering_mode / is_chapter_last / scene_seq）推段型偏好与位置 |

### WP4 · 作者看得见

| 项 | 改动 |
|---|---|
| 4.1 窗口记录 | 渲染读数带 `few_shot_window_refs`（起止段、段型、字数）→ 运行时审计 → 工作台 `generation_summary.style_windows` |
| 4.2 渲染 | 场景运行视图渲染 `generation_summary.notices` 与本场参考窗口列表（可展开原文：`GET /api/v2/style-reference/books/{book_id}/paragraphs?start=&end=`） |
| 4.3 预览按场景 | `injection-preview` 接受 `scene_id`：同一轮换种子与场景上下文 |

### WP5 · 结构层到场级

| 项 | 改动 |
|---|---|
| 5.1 场分隔 | 导入时识别显式场分隔（`***`、`———`、纯符号行），记入 `stats_json.scene_breaks`；无分隔时按对白 / 叙述块估计 |
| 5.2 画像与分章 | 结构画像加「每章约 N 场、场长中位 X 字」；`propose_from_scenes` 在项目无显式章数时按参考章长中位推 `scenes_per_chapter` |

### WP6 · 规划与写手侧看到参考

| 项 | 改动 |
|---|---|
| 6.1 规划快照 | 章架构 / 人物压力的规划快照带结构画像 + 场景手法；`character_pressure_blueprint` v3 的「without explanatory summary」加限定 |
| 6.2 蓝图带样例 | `style_bound` 时 `scene_blueprint` 拿到 `[STYLE_REFERENCE]`（k=3 窗口），预算按风格通道放大 |
| 6.3 写手侧 | `author_proposal_generate`（整稿 / 续写 / 近终稿改写 / 语言 / 对白）、`writer_passage_patch`、`writer_deep_review` 解析项目绑定并注入前缀（评审与补丁用 k=3） |

### WP7 · 文档、回归、提交

`CLAUDE.md`、`docs/README.md`、本文 §8；全量非 Chroma 回归 + ruff + vitest；`sync_prompt_templates --execute` 部署提示。

## 2. 明确不做（本轮）

- **删除死代码簇**（候选重排 / 风格反馈 / benchmark / RAG A/B / 指标聚合器 / ReviewItem 物化）：删与复活是产品决定，本轮不动；评估里已列清单。
- **「像不像」度量体系**：作者决定由阅读评测。
- **微调（Step 3）**：需要 GPU 机器。
- **基线语料换代**：仓库只能放公版文本，鲁迅 + 朱自清基线保留，改为修正明显失真的指标（人称、文言标记）。

## 3. 部署

- 提示词改动对存过 prompts 快照的安装静默无效：`cd backend && python -m novel_system.tools.sync_prompt_templates` → `--execute`。
- `models.yaml` 的 `soft_qc` 路由改动同样被库内 models 快照盖过：已存过路由的安装在系统配置界面把 `soft_qc` 改到强模型。
- 已导入的书：副文本过滤与选窗改动在**重新合成画像**后生效（`exemplar_windows` 合成期写入）；人称、文言指标、副文本剥离与符号场界可以不删书就地刷新：`python -m novel_system.tools.refresh_style_reference_books [--book ID] --execute`（默认干跑；空行型场界只有重新导入原文才能恢复；段落根哈希会变，已冻结的契约退回引文兜底路径）。

## 8. 完成记录（2026-09-14）

全部落在工作区（本文写就当日实施）。有绑定时的行为按上表落地；无绑定与 `neutral_first` 逐字不变。

- **WP1 管线让位补全**：`scene_generation._run_de_template_pass` 在 `style_bound` 时给安全修复、长度补丁及后续注入 `[STYLE_REFERENCE]`（`context_text=None`），修复 / 补丁指令加 `_STYLE_BOUND_REPAIR_CLAUSE`，`_style_safety_repair_brief` / `_style_length_patch_instruction` 的扩写句改为「作者自己的手段」；`_normalize_style_paragraph_shape` 与 `_assess_style_anchor_conformance` 在 `style_bound` 时记 `deferred_to_reference`；`generate_style_patch` 在 `style_bound` 时温度 0.3 并要求未点名句子逐字保留；`near_final._normalize_acceptance_payload` 记 `failure_class_coerced`，`_apply_style_bound_rewrite_policy` 在被强转或无评审简报时写 `auto_rewrite_blocked`（`_should_rewrite` 认它）；`bundle_builder._literary_freshness_budget` 在 `style_bound` 时只发内容级三项（`voice_lists: deferred_to_reference`）；`final_text_gate` 在 `style_bound` 时不生成房风 Q3 警告；`soft_qc` 路由 gpt-5-mini → gpt-5（`models.yaml` + `llm_node_registry`，输出 2600）；模板 `style_draft` v11、`soft_qc` v7、`style_length_patch` v4、`style_salvage_patch` v2、`scene_literary_rewrite` v4。
- **WP2 导入层**：`_TITLE_RE` 副题允许空格 / 双语、`_TITLE_MAX_CHARS` 48；`text_utils.is_paratext_paragraph`（脚注 / 站点声明 / 短行网址；正文里「键入网址」不算）在导入时剥离（`stats_json.paratext_dropped`），旧书在结构样例、样例窗口、抽取池处过滤；`voice_signature` 人称只数叙述（`_QUOTED_SPAN_RE`）；`metrics._classical_usage_ratio` 取代字面命中；`voice_baseline.yaml` 重建（`build-baseline --block-chars 1500`），黄金期望再生成（朱自清语料少了一条真正的「①原注」脚注）。真实书复核：章 59 → 91、人称「混用」→ 第三人称 82%、文言比例 0.17 → 0.016、章首 / 章尾样例全是正文。
- **WP3 选窗与覆盖**：新模块 `style_reference/exemplar_index.py`（`build_exemplar_window_index`，不跨章题 / 场界、副文本不入窗、<600 字不入索引）；合成期写 `profile_json.exemplar_windows`（不冻结），`injection._exemplar_index_for` 对旧画像惰性复算并按 (book, 根哈希, profile) 缓存；`_render_few_shot_from_index` + `_pick_index_windows`（对白配额、段型覆盖、位置配额 ⌈k/2⌉、跨章分散）；`scene_sampling_hints` 由注入器与预览端点设置；`injection_budget.yaml` k 12 / 整块 60000 / 轮换池倍数 6 / `exemplar_index_min_window_chars` 600；`STYLE_PASS_INPUT_TOKEN_BUDGET` 96000（七个风格通道模板同步）。真实书实测：522 窗、首次渲染 ≈2 s（惰性建索引）后 ≈0.3 s、每场 12 窗 ≈4.7 万字、跨章分散、开章 / 收章位置匹配生效。
- **WP4 作者看得见**：运行时审计带 `few_shot_window_refs`；`generation_summary.style_windows`；`GET /api/v2/style-reference/books/{book_id}/paragraphs?start=&end=`（≤80 段）；预览端点接受 `scene_id`、返回 `window_refs`；`ws-scene-run.jsx` 渲染通知条与「本场参考窗口」面板（可展开原文）。
- **WP5 场级结构**：`text_utils.is_scene_break_paragraph` / `explicit_scene_breaks`，导入记 `stats_json.scene_breaks`；结构画像 `scene_break_style` / `scenes_per_chapter` / `scene_chars` 与「场：…」行，跳过书名页 / 简介前置块与「全文完 / The End」，样例只取 ≥1200 字的章；窗口索引不跨场；`propose_from_scenes` 无显式章数 / 每章场数时按参考章长推 `scenes_per_chapter`（操作日志 `reference_hint`）。
- **WP6 规划与写手侧**：`InjectionService.few_shot_k_cap` + `inject_style_reference_prefix(few_shot_k_cap=, runtime_contract=)`，`resolve_style_scope`；`planning_context.build_planning_style_reference` 为蓝图与近终稿规划共用；`scene_blueprint` v9 带 `[STYLE_REFERENCE]`（k≤3）；章架构 / `character_pressure_blueprint` v3 快照带结构画像 + 场景手法 + 叙事机制；`author_proposal_generate` v3（完整 k）、`writer_passage_patch` v3、`writer_deep_review` v4（k≤3）注入；`PLANNING_STYLE_INPUT_TOKEN_BUDGET` 24000，作者提案走风格通道预算。
- **测试**：新增 `test_style_reference_exemplar_index.py`（12）、`test_style_reference_scene_breaks.py`（6）、`test_style_windows_surface.py`（7）、`test_style_reference_planning_writer_injection.py`（18），`ws-scene-run.test.jsx` 新增 8 例；既有套件按新契约更新（k 3→12、预算 64000→96000、模板版本钉）。分包回归：WP1 150 通过、WP2 359 通过、WP3 269 通过、WP4 后端 102 + vitest 405 / build 通过、WP5 129 通过、WP6 194 通过。全量非 Chroma 回归见下一条。
- **全量回归（2026-09-14）**：非 Chroma 两片：shard 0 = 1538 通过 / 5 跳过，shard 1 = 1422 通过；ruff 全绿；前端 vitest 41 文件 405 例通过，`npm run build` 成功。本机无真实模型，未生成实际文稿；「像不像」仍由作者阅读判断。未提交。

## 9. 死代码簇的评估与处理（2026-09-14，同日）

逐簇按「谁引用、谁消费、前端有没有入口、测试盖了什么」排查后的结论与动作：

| 簇 | 排查结论 | 动作 |
|---|---|---|
| 跨内容基准包 `services/style_reference/benchmark/`（2,926 行）+ 两份基准清单 | src 内无任何引用；批次 1 起无 CLI、无路由、从未用真实模型跑过；「像不像」由作者阅读评测 | **删除**（含 `test_style_reference_benchmark.py`）；`config/evals/style_reference/` 只留 RAG A/B 清单，README 重写 |
| RAG A/B `rag_evaluation.py` | 被 `test_style_reference_rag*.py` / `test_style_reference_style_signature.py` 当作 Strategy C 检索机制的确定性回归 | **保留**（Strategy C 仍是界面可选策略）；C 本身是下一个减法候选 |
| 指标聚合器 `metrics_aggregator.py` + `GET …/metrics`、`metrics/daily` | 前端不调用；事件表由 `MetricsRecorder` 写、`style_continuity` 读漂移事件，聚合器只是无人看的报表 | **删除**端点、聚合器、缓存与三份测试；事件表与 `MetricsRecorder` 保留 |
| ReviewItem 物化（`apply_profile` 步骤 1–2） | 消费方 `services/versioning/review_materialization` 早已删除；生成期读 `profile_json` 与冻结契约 | **删除**写入与响应字段 `review_ids` / `item_type_counts`；`cleanup` 仍清理旧行 |
| 候选重排 `candidate_rerank.py`（903 行） | 重排层（shadow / active、yaml、基准报告哈希授权）从未改变过候选顺序；评分核（目标包络 + 贴合读数 + 抄袭守卫）被风格修复的不退步检查与 neutral_first 的形状包络活用 | **切分**：删重排层、yaml、文档，留 314 行评分核；`scene_generation._candidate_style_assessment` 直接给每个候选写读数 |
| 风格反馈 `style_feedback.py` | 记录「作者选择 vs 机器领先者」的一致性，`policy_evidence_eligible` 恒 False，无任何策略消费 | **删除**（终选路由只留决定历史与偏好标签） |
| 盲选终选门（`_offer_candidates_for_selection`、三条路由、`resume_after_selection`、检查点 `selection_wait`） | 前端 `ws-scene.jsx` 有完整的「关键场景 · 匿名候选终选」面板，后端 1,000 多行门测试；唯一死因是 `_best_of_n_count` 恒返回 1（原先由已删除的基准证据授权） | **保留并复活**为显式开关 `NOVEL_SYSTEM_SCENE_BEST_OF_N_ENABLED`（默认关 = 现状；开 = 按场景关键度出 2–3 个候选，关键场停在盲选门让作者读完再选）；新增 `tests/test_best_of_n_switch.py` |

理由：删的五项在产品里没有入口也没有消费方，删除不改变任何可达行为；盲选门是「让作者读两版再选」的完整功能，正好服务「由阅读判断像不像」的目标，删掉可惜、复活便宜。
- **减法后的全量回归**：非 Chroma 两片：shard 0 = 1428 通过 / 1 跳过，shard 1 = 1467 通过 / 4 跳过；ruff 全绿；前端未改动。删除净减约 5,000 行（基准包 2,926 + 重排层约 590 + 风格反馈 315 + 指标聚合器 172 + 物化约 100 + 六份测试约 1,000），新增开关测试 1 份。
