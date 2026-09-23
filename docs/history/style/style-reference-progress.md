> 历史文档：描述当时的实现，现行说明见 [docs/style-reference.md](../../style-reference.md)

# Style Reference 模块重构 — 进度总览

> 依据《风格参考模块重构执行手册 v1.1》。本文件随 PR 推进滚动更新。
> 历史实施账本，最后功能记录截至 2026-07-08；Phase 3 立项 A/B/C 已全部完成。当前运行边界以 `README.md`、`docs/runtime-safety.md` 和代码为准。
> 测试规模:后端目标 ~343 测试 / 前端 524 测试(57 文件)+ smoke,全绿。

## 一、阶段与 commit 清单

| Phase | PR | commit | 内容 |
|---|---|---|---|
| **1 落地** | PR-1 | `cb5ddf4` | schema 落地(表 + ORM + migration) |
| | hotfix | `a61e9de` | CloudPolicy enum 值对齐代码事实 |
| | hotfix | `e11157d` | findings UNIQUE 改 statement_hash + 4 列复合(允许同维多观察) |
| | PR-2 | `9f1c9df` | ingest + 分段 segmentation + metrics 骨架 |
| | PR-3 | `2df3307` | extractors 半盘(language + narrative) |
| | PR-4 | `21a0978` | synthesize + materialization + Phase 1 routes + preview |
| | PR-5 | `65a5699` | 前端 Phase 1 |
| **2 抽取/注入** | PR-6 | `dcaa5a2` | extractors 全盘(scene + theme) |
| | PR-7 | `110d98d` | validation 三路 + 双路径 |
| | PR-8 | `c8e3573` | injection + qc gate + `_call_llm` 统一 |
| | PR-9 | `a77813b` | 前端组件 + injection 微扩展 |
| **3 可观测/性能/a11y/精化** | PR-10 | `4d47954` | §13 可观测性 + metrics 上报(MetricsRecorder/Aggregator) |
| | PR-11 | `ab792a8` | E2E 重写 + metric_events cleanup |
| | PR-12 | `5b66cd4` | 性能(metrics TTL cache + findings 虚拟滚动) |
| | PR-13 | `c530e77` | a11y 轨道(ARIA + 键盘导航 + 局部对比度) |
| | PR-14 | `1d50465` | character scope binding |
| | PR-15 | `84a4474` | scene scope binding |
| | PR-16 | `e8000b8` | fragments 两层叠加合并(base + 最具体增量) |
| | PR-17 | `55e9e08` | a11y 深化(完整 focus trap + radiogroup 导航) |
| | PR-18 | `96df6cd` | onstage 多角色 character 匹配(pov ∪ onstage) |
| | PR-19 | `73e46b3` | 三层叠加 base+character+scene(加权预算) |
| | PR-20 | `96eca2f` | 多 character 叠加(onstage 配角全叠) |
| | PR-21 | `402ea52` | Dialog a11y 推广(useFocusTrap composable) |
| | PR-22 | `745db6c` | metric 趋势图(无依赖 SVG) |

## 二、Phase 3 叠加注入弧线(PR-14~PR-20)

注入从「单选一个 binding」逐步演进为「多层加权全叠」:

- **优先级 rank**(单一真相源 `_binding_rank` + `_char_order`):`scene=0 > character=1 > project=2 > global=3`,不匹配=99。
- **PR-14/15**:落 character / scene scope binding(单选优先级)。
- **PR-16**:两层叠加 = base(project/global)+ 最具体增量一层;token 各半;forbidden 行级去重;metric 取最具体。
- **PR-18**:character 匹配集 = `ordered_character_ids(pov, onstage)`(pov 排首 + onstage 去重)。
- **PR-19**:三层全叠 `[base, character, scene]`,由泛到具体;token **加权** `weights=range(1,n+1)`(越具体预算越多,scene 最大);单层零回归走原路径。
- **PR-20**:character 层从单选进化为 **onstage 多配角全叠**,`[base, char_pov, char_b, …, scene]`;同角色多 binding 按 created_at 去重(每角色一层)。
- **不变**:`resolve_active_binding`(qc gate 单选)、调用方接口(`fragments_for` 返合并单 fragments)始终透明。

## 三、Phase 3 其余轨道(PR-21~PR-22)

- **PR-21 Dialog a11y 推广**:抽 PR-17 内联 focus trap 为 `useFocusTrap(dialogElRef) → {onTab}`(纯 JS,无依赖);ProfileApplyDialog 迁入(零回归)+ 推广到 SnowflakeSkipStepDialog、WriterRoomView reject dialog(后者补 esc / 初始聚焦 / aria-labelledby)。全站 3 dialog 键盘可达性一致。
- **PR-22 metric 趋势图**:后端 `MetricsAggregator.daily_injection_counts`(`substr(created_at,1,10)` GROUP BY + Python 零填充连续日期轴,window 钳 [1,90],不缓存)+ `GET /metrics/daily`;前端手写 SVG 柱状 `MetricsTrendChart.vue`(无 Chart.js,峰值归一 / 零值空档 / role=img),入 MetricsPanel,window 7/30/90 映射。

## 四、全局纪律(PR-1~PR-22 全程遵守)

- **A** 文档/代码冲突 → 以**代码事实**为准(不改 v1.1 手册,累积 v1.2 修订清单)。
- **B** **不新增 Enum**(event_kind / scope 等用文档约束的字符串常量)。
- **C** 新模块**不依赖旧模块**(如不 import reference_learning)。
- **D** v1.1 内部矛盾 → 按**产品意图**裁决。
- **贯穿**:继续优先**无新依赖**(PR-21 手写 composable、PR-22 手写 SVG + 纯 SQL,均未引入依赖)。

## 五、v1.2 文档修订清单(常驻,累计 9 条)

| # | 来源 | 章节 | 问题 |
|---|---|---|---|
| 1 | PR-1 | §4.1 | `review_finding_%` → `review_reffind_%` |
| 2 | PR-1 | §14 | 文件清单未列 cleanup.py |
| 3 | PR-1 | §4.1 | backup 路径未指定 |
| 4 | PR-1 | §14 | migration 日期 `20260521_*` 已过期 |
| 5 | PR-1 | — | `0001_init_schema` create_all → `if X not in tables` 防御 |
| 6 | hotfix | §附录B | cloud_policy 正确三档 |
| 7 | PR-2 | §6.1 | `quality_balanced` model_profile 不存在 |
| 8 | PR-3 | §4.2/§6.5 | findings UNIQUE → `statement_hash` + 4 列复合 |
| 9 | PR-8 | §11 | auto_rewrite 触发不改 `SceneAutoRewriteService.run` 签名,trigger_source 落 `qc_report.resolution_code` |

## 六、已知遗留 / 剩余工作

- **既有 flaky → 已根治(2026-06-23 复核)**:`test_qc_engine.py::test_run_scene_soft_qc_patch_repeat_waives_with_carry_note` 等按 `created_at` 排序的断言。**根因**:Windows 粗粒度时钟(~15.6ms)下同 tick 多次插入产生 `created_at` 字符串碰撞,使 `order_by(created_at)` 退化为非确定序。**已根治**:`db/models.py` 进程内严格单调 `utcnow()`(commit 5c0c2a3)+ `order_by` 次级 tiebreaker `qc_report_id`(ad67a09);并由 `tests/test_utcnow_monotonic.py`(1 万次单调 + 跨线程唯一)永久守门。复测:目标 + 两个同形兄弟用例组合跑 **0/25 失败**。
- **PR-23+ 候选**:
  - scheduled cleanup CLI(复用 `cleanup_metric_events` + OS cron,无依赖)/ 或 APScheduler(需依赖)
  - i18n(vue-i18n + 文案抽取,需依赖)
  - 趋势图进阶(多序列 / 比率趋势 / Chart.js 升级)
  - 多 character 叠加进阶 / 其余 dialog a11y 细节

## 七、关键文件索引

- 注入核心:`backend/src/novel_system/services/style_reference/injection.py`
- metrics:`.../style_reference/metrics_aggregator.py`、`metrics_recorder.py`、`cleanup.py`
- 路由:`backend/src/novel_system/api/routes/style_reference.py`(`PATH_PREFIX = /api/v2/style-reference`)
- 前端组件:`frontend/src/components/styleReference/`(ProfileApplyDialog / StyleReferenceMetricsPanel / MetricsTrendChart / InjectionStrategyPicker 等)
- 前端 composable:`frontend/src/composables/useFocusTrap.js`
- 宿主视图:`frontend/src/views/KnowledgeConsoleView.vue`(metrics 面板)、`ReferenceLearningView.vue`

## 八、2026-06-12 防线加固(审查修复轮)

针对模块审查发现的「防线静默失效」问题集中修复(`tests/test_style_reference_hardening.py` 24 用例覆盖):

- **反抄袭事前预防接线**:`SystemPromptFragments` 新增 `anti_plagiarism_block` 第 4 块(§A.5 红线模板 + banned_terms scope=generation 填充,配置缺失有内置兜底);任一风格 block 非空时必随注入、永不参与预算截断;多层叠加时 banned_terms 取全层并集。前端 SrApply 面板展示的「第 4 块」自此为真实契约。
- **抄袭检测升级**:语料从 profile quotes 扩为**全书段落**(`_load_plagiarism_corpus`,按 checksum 进程内小缓存);匹配前做规范化(去空白/标点、统一小写,Unicode P*/S*),防「插空格/换标点」绕过;倒排索引建在 generated 侧,全书语料复杂度 O(n+m) 不变。
- **cloud_policy 强制执行**(新 `policy.py`):`local_only` 的书在 start_run / reclassify / synthesize / preview 一律 409 `STYLE_REFERENCE_CLOUD_POLICY_BLOCKED`;导入分类强制降级启发式;async_full 语义路跳过且 verdict 至多为 `partial`，避免缺少语义证据仍误报完整 `pass`。此前该字段只存不查。
- **校验语义闭合**:sync 快路径补上 quantitative(§4.3 设计对齐,qc gate 自此有量化对照);semantic 路**尝试执行但失败**时 PASS 封顶 PARTIAL(空集不再静默折叠成满分);sync/preview 的 report 落盘 quantitative_json。
- **卡死回收**:RUNNING 超 60 分钟的僵尸 run 在下次同书启动时自动降级 FAILED(`failure_reason=stale_running_reaped`);pending 超 10 分钟的 async report 在轮询端点惰性降级 fail;报告序列化新增 `status` 字段(pending/done)。
- **错误与边界**:`LLMRequiredError` / `CloudPolicyBlockedError` 继承 DomainError → 409 + author_action(原 LLMRequired 落通用 500);上传体积上限 10MB(413);注入截断改行边界感知(损失过半时退回字符截断);MIXED intensity 三块截断额之和封顶 `system_prompt_max_tokens`(原 1.5x 溢出预算)。
- **测试夹具勘误**:既有夹具中装饰性的 `cloud_policy="local_only"` 统一改 `segments_only`(local_only 拒绝行为由专门用例覆盖)。

**遗留(本轮明确不做)**:start_run 后台任务化 + 进度轮询(架构改动大);黄金语料扩容到真实规模 + expected 指标(需外部公版语料);前端 intensity 滑块 apply 端到端打通;few-shot(B)/RAG(C) 真实现(现为预算变体);binding UNIQUE 约束 + apply 事务化(需 migration)。

### 第二轮(同日,清遗留)

- **幂等层半提交修复(全应用级)**:`execute_with_idempotency` 原失败路径直接 `record.status="failed"; commit()`,会把 action 已 flush 的半成品一并提交;现改为**先 rollback 再单独落失败标记**(`_rollback_and_mark_failed`,补 `idempotency_failed` 审计日志)。materialization 等所有幂等端点的部分失败自此整体回滚。
- **start_run 后台化**:`StartRunRequest.background=true` → 立即返回 RUNNING + run_id,抽取在单 worker 线程独立 session 执行(`_RUN_EXECUTOR`);层粒度进度写 `coverage_json.progress`(layers_total/done/current_layer),层边界协作响应 cancel。inline 模式(默认)保持单事务零回归。React 端 rerun 改走 background + 2.5s 轮询(`srPollRun`,20 分钟上限),完成/失败提示。
- **前端 cloud_policy 勘误(关键)**:`srImportBook` 原硬编码 `local_only`——配合策略强制执行会卡死一切 UI 导入书的抽取;改默认 `segments_only`,并对 `STYLE_REFERENCE_CLOUD_POLICY_BLOCKED` 给专门文案。
- **apply 注入配置端到端(后端面)**:`ApplyProfileRequest` 新增 intensity / sub_dimensions / include_* → `MaterializationService.apply_profile(config_json=...)` 落 binding.config_json(MIXED 注入路径既有消费);重复 apply 复用 binding 并更新 strategy/config。React apply 深层页仍为原型绑定(未调 /apply),滑块前端接线待 styleref 深层 store 化。
- **binding 目标唯一约束**:ORM + migration `20260612_0053`(先按 created_at 去重再 batch 重建加 UNIQUE(profile_id, scope, scope_ref_id, task_type)),并发 apply 竞态从「应用层先查后建」升级为 DB 兜底。
- **Strategy B 真 few-shot**:新增 `SystemPromptFragments.few_shot_block`(第 5 块):B 策略从 `scene_samples_index` 直读样例引文(每 paragraph_type 取 1,k/单条/整块上限入 `injection_budget.yaml`),few-shot 在场时红线段强制随注;叠加路径不传 few-shot(多书样例混叠稀释信号)。C 策略 RAG 仍未实现。
- 测试:`test_style_reference_hardening.py` 扩至 30 用例(后台 run 完成/进度、半提交防护、唯一约束、few-shot、apply config);migration 契约测试 head 更新至 `20260612_0053`。全量 944 通过。

### 第三轮(2026-06-13,黄金语料落地 + 本地私有语料通道)

- **黄金语料从占位变真实**(§9.2):维基文库(REST `page/html` + zh-hans 变体)抓取公版全文,HTMLParser 提取 `<p>` 正文(修复了正则剥标签被 data-mw 属性内 `/>` 截断的噪声问题)。主力集鲁迅短篇 11 篇 66,352 字(language/scene high · narrative medium · theme low),对照集朱自清散文 4 篇 8,296 字(全 skip,作指标对照),下限集单篇孔乙己 2,637 字(全层 skip)。来源与版权依据记录在 golden README;原 老舍/沈从文 占位文件删除(两者版权状态存疑,设计文档原推荐有误)。
- **expected 体系**:`regen_expected.py` 在隔离临时库跑真实 ingest(启发式分类,确定性)生成 26 metrics + input_assessment + 段型分布期望 JSON;`test_style_reference_golden.py` 8 用例:语料规模/篇目、**高特异性 IP 关键词守卫**(龙族/路明非/路鸣泽/楚子航;「江南」为通用地名在公版文本合法出现 2 次,不入守卫)、ingest 输出全量回归(rel 1e-9)、下限集全 skip、抄原书段落(含标点微改)必判 plagiarism、原创文本 pass、伪华丽腔句长锚点必超容差。
- **校准观察(设计风险 9 应验)**:段落级 std 使自适应容差整体偏宽——鲁迅 vs 朱自清整书均值对照 26 项指标**零项**超容差(如 avg_sentence_length std 16.3 → 容差 20.4)。量化 gate 现状只拦极端偏差;后续可考虑用「分块均值的 std」替代「段落级 std」收紧容差。
- **本地私有语料通道**(用户用《龙族》验证的合规出口):`test_style_reference_local_corpus.py`,`NOVEL_SYSTEM_STYLE_REF_LOCAL_CORPUS` 指向本地 TXT 时运行(未设自动 skip),书内容只进当次临时库。实测《龙族》(3.7MB GB18030):摄取/26 指标/四层 high、自抄 40 字+插空格→plagiarism 命中、原创→pass。
- **切分器退化兜底**(《龙族》实测暴露):单换行分段的网文 TXT 在空行切分下整章并为一「段」(3.7MB 仅 116 段,段级统计失真);`split_paragraphs` 平均段长 >1500 字时自动改按单换行切分(修复后 26,677 段),黄金语料与既有路径零影响。
- 全量 954 通过 / 15 跳过(12 chroma + 3 本地语料未设环境变量)。

### 第四轮(2026-06-13,React 注入应用 stage 真后端化)

把 `frontend-react/src/ws-styleref.jsx` 的「注入应用」深层页从演示接到真后端——用户点名的最大假象(apply 按钮的卡片 action 无 `effect`,批准什么都不做)修复。

- **后端 effect 扩展**:`review_effects.py:_bind_style_profile` 原只转发 scope/strategy,现新增 `_style_injection_config` 把 intensity / sub_dimensions / include_* 组装为 config_json 传给 `apply_profile`(上轮已支持),与 `ApplyProfileRequest.injection_config` 同构。即「apply 决策卡批准」与「直接 /apply」落同样 binding.config_json。
- **前端深层 store**(范式同 srSyncBooks):`SR_DEEP` 按 bookId 懒加载 profile(`GET /profiles?book_id`)+ bindings,`sr:deep-changed` 事件广播;`srInjectionPreview`(dryrun)/`srUnbind`(DELETE binding)helper。
- **apply 按钮真化**:真书有就绪画像 → `realMode`。apply 推送决策卡,主 action 携带 `effect:{type:"bind_style_profile", profile_id, scope, task_type, strategy, intensity, sub_dimensions, include_*}`;收件箱批准 → 后端事务执行 effect → 真 bind(项目级 scope_ref_id 默认取卡片 project_id)。无画像(演示书/未合成)→ 回退原型卡(无 effect)。
- **真注入预览**:右侧 SystemPromptFragments 卡在 realMode 下拉 `POST /profiles/{id}/injection-preview`(debounce 350ms),渲染**真实** positive/forbidden/metric/few_shot/anti_plagiarism 五块 + 注入字数预算;空画像显示「需先抽取合成」。
- **真绑定列表 + 解绑**:realMode 渲染 `GET /profiles/{id}/bindings`,解绑走 `DELETE /bindings/{id}` 后强制重载。场景/角色级 scope 因缺目标选择器暂禁用(只支持项目级真绑定,有 title 提示)。
- 测试:`run_effect` config 转发单测 ×2(hardening,32→ 含)、**review-items HTTP 往返**(`test_review_cards.py::test_resolve_bind_style_profile_forwards_injection_config`:建卡→resolve→断言带 config 的 binding)。`npx vite build` 通过。全量后端 **1209 通过 / 15 跳过 / 0 失败**(1224 收集)。

### 第五轮(2026-06-13,概览 / 维度矩阵 / 风格画像三深层页真后端化)

把剩余展示页接到真后端,采用「有真数据显真、否则演示」双模(同书库 SR_REAL 范式)。

- **后端新增**`GET /books/{id}/runs`(按 created_at 倒序 + status 过滤):矩阵在**合成画像之前**就需要定位最新 run + 其 findings,此前无 list-runs 端点只能从 profile.run_id 反推(合成后才有)。
- **深层 store 扩展**(`srLoadDeep`):一次拉齐 book 详情(stats_json)/ 最新 run / 该 run 的 findings(`?include=evidence`,按 sub_dim 分组 + obs/fp/quote 计数 + 置信度聚合)/ profile / bindings,组装富 `deep` 对象;新增 `srSynthesize` / `srReviewFinding` / `srPreviewSamples` helper;`useSrDeep(book)` hook 统一懒加载 + 订阅 `sr:deep-changed`。
- **概览页(无需 LLM,全真可验证)**:硬指标基线(stats_json.metrics 26 项按展示名取真实 mean/std)、输入量评估(input_assessment 四层)、段落类型分布(真实占比,动态归一)、分类器校准(anchor_size / 一致率 / 是否降级)全部读真实 ingest 产物。**任意导入书(含启发式分类)即全真**。
- **维度矩阵页**:单元格置信度/观察/引文/禁忌数叠加真实 dimCounts,skip 由 input_assessment 判定;findings 抽屉读真实 finding + 证据,审核(通过/驳回)走 `POST /findings/{id}/review`;「合成风格画像」按钮真调 `POST /runs/{id}/synthesize`(LLM 未启用→409 引导);合计数实时。无 findings(未抽取/无 LLM)→ 回退演示。
- **风格画像页**:概述/维度摘要/指标基线/场景样例索引读真实 profile_json(narrative_summary / sub_dimensions / metrics_baseline / scene_samples_index / style_features);预览 tab 真调 `POST /profiles/{id}/preview`(3 段示例 + sync_only verdict)。无画像 → 回退演示。
- 数据形状逐一核对后端序列化器(profile_synthesizer 的 sub_dimensions 键名 observation_count/forbidden_pattern_count/quote_count/confidence 等);后端 `test_list_book_runs_*` 测试 + `npx vite build` 三页分别通过。

### 第六轮(2026-06-13,回测校验页真后端化 → 深层页套件收口)

`ws-styleref-val.jsx`(`window.SrValidation` + `ValidationReportCard`)整文件重构为真后端:

- **输入 + 双路径运行**:受控 textarea + sync_only / async_full 切换;运行真调 `POST /profiles/{id}/validate`。sync_only 直接用响应的 `sync_result` 渲染(量化 + 抄袭,**无需 LLM**);async_full 拿 `report_id` 后轮询 `GET /reports/{id}`(≤60s)直到 verdict 落定。LLM 未启用 → 引导改用同步快路径。
- **report 归一化映射**(`srvNormalize`):quantitative_json(target_mean/std/actual/**tolerance**/passed/deviation_ratio,26 metric key → 中文名 + pct 标记)→ 量化条(真实项直接用后端 tolerance/passed,演示项现算);semantic_json(dimension/score 0-10)→ 雷达(≥3 轴)或评分列表;plagiarism_json(hits/matched_length/ngram_size/threshold_chars)→ 重叠计 + flags;forbidden_hits_json(pattern_statement/matched_excerpt/severity)→ 禁忌触发列表(硬触发标红)。verdict 直接用后端结论。
- **四路汇总 / 改写建议**真实派生(通过率、语义均分、抄袭、禁忌数;建议从触发禁忌 + 最大偏离量化项现算)。无画像 / 演示书 → 全回退原型演示数据。
- 数据形状对照 schema 逐一核对(QuantitativeReportItem / SemanticReportItem / PlagiarismReport / ForbiddenHit / ValidateResponse);`vite build` 通过。

**至此深层页套件全部真后端化**:书库 · 概览 · 维度矩阵+审核 · 风格画像+预览 · 注入应用 · 回测校验,均「有真数据显真、否则演示」。

### 第七轮(2026-06-13,量化容差收紧 — 核心引擎校准)

修掉第三轮发现的核心缺陷:`MetricsEngine.compute_with_variance` 的 std 原按**逐段**计算,
噪声极大——段落短、且 paragraph_type 比例指标逐段取 0/1(段级 std 退化到 ~0.5)——使
`tolerance = max(std×1.25, floor)` 宽到几乎不拦截(原作互比 26 项零超容差,量化门形同虚设)。

- **修法(最小且有原则)**:mean 不变(仍 == `compute_all` 全文单值,黄金 expected 的 mean 不变),
  只把 **std 改为块间标准差**——`_chunk_by_chars(paragraphs, 1500)` 把语料按累计字数切成
  ≈场景大小的块,std = 块间波动。回测对照的是「整段生成文本(≈一个块)的单值」,故 tolerance
  应反映**作者自身块到块的自然波动**,这才有区分力;floor 仍兜底低波动指标。
- **验证**:`test_chunk_variance_tightens_std_vs_paragraph_level`(真实鲁迅语料:块间 std 严格 <
  逐段 std 且脱离 ~0.5 退化区,mean 不变);metrics 测试改造为块间语义(+ 单块 std=0 用例);
  黄金 expected 重生成(std 收紧,mean 不变);伪华丽腔/既有量化用例全绿。

### 第八轮(2026-06-18,立项 C — Strategy C(RAG)三粒度向量召回)

把 C 策略从"退化变体"(positive 全文 + forbidden 摘要 + 不注 metric,**无真实召回**)升级为
真实三粒度 RAG 召回,并修复"假防漂移"缺陷(续写循环每段重注入但**不传已生成正文**,导致 RAG
每次召回相同 → 防漂移对 RAG 形同虚设)。

- **核心模块** `services/style_reference/rag.py`(新增):
  - 三粒度索引(sentence 切句 / paragraph 全段 / scene 连续段落按 1500… 实为 600 字窗口聚合),
    建立在既有 `vector_store` 抽象上(memory 确定性 / chroma 走 WSL);collection 命名
    `style_ref_rag_{profile_id}_{granularity}`。
  - **召回 + 确定性 rerank**(无 LLM,守 §11 风险6 inject<50ms):统一打分 = query 字符覆盖率
    `|set(query)∩set(snippet)|/|set(query)|`(长度归一,跨粒度可比)× 粒度权重;排序均带
    `snippet_id` 稳定 tiebreaker;`retrieve_per_granularity` 供 hit@k 独立测量,`retrieve`
    合并去重截断供注入。`style_ref_rag_rerank` LLM 节点仅作离线/预览 hook 落地(registry 标
    `reserved` + prompt 模板),**不在热路径调用**。
- **C 分支真召回**:`injection._render` 加 `context_text`;`_render_rag` 按 context_text(为空回退
  profile 叙事概述)检索 → `render_rag_block` 渲染 `[风格检索样例]`;`SystemPromptFragments` 加
  `rag_block` 字段并入 `to_system_prompt_prefix`;**rag_block 非空时红线段强制随注**(§11 风险11);
  空召回优雅退化。多层叠加路径沿用既有决定丢弃 rag(同 few-shot,多书样例混叠稀释)。
- **防漂移接线**:`scene_generation._inject_style_reference` 加 `context_text`,经
  `InjectionService.context_text` 实例属性透传;长文续写循环首段传 `source_content[-2000:]`,
  后续段传累计 `existing_continuation` 尾部 → RAG 召回随上下文变化(防漂移真实生效)。
- **生命周期**:`profile_synthesizer.synthesize` 末尾建索引(容错:向量后端不可用/失败不阻断);
  `cleanup.purge_derived_data` 删 profile 三粒度 collection。
- **配置**:`injection_budget.yaml` 加 rag.* 预算(top_k/inject_max/quote_max/block_max/scene_target/
  权重等);`models.yaml` + `prompts.yaml` + `llm_node_registry.py` 落地 `style_ref_rag_rerank`(reserved)。
- **测试** `tests/test_style_reference_rag.py`(21 用例,memory 确定性):索引构建三粒度计数 / 切句 /
  scene 聚合 / 三粒度召回命中 / 合并去重截断 / 确定性 / 渲染红线契约 / 删除 / **C 注入红线随注** /
  **防漂移随上下文变化** / 无索引优雅退化 / purge 删索引;补强后增:**local_only 跳过 RAG** /
  非 active profile 退化 / 向量后端不可用退化 / 红线 iff 风格块活跃 / drift snippet id 集合差异 /
  inject_max 边界。全后端 1233 passed + 15 skipped(Windows chroma)。
- **对抗审查(5 视角)+ 修复**:正确性 / 反抄袭红线 / 确定性性能 / 防漂移设计(verdict=clean)/ 测试充分性。
  采纳并修复 3 项:(1)跨粒度 rerank tiebreaker 改用粒度 rank(`_GRAN_RANK`,由细到粗)替代字母序;
  (2)`local_only` 书 C 策略跳过 RAG(原文不送云端,附录 B;positive 抽象特征仍注入;B/few-shot 同类
  张力记为独立跟进);(3)多层叠加丢弃 rag_block 在 `_cap_fragments`/`_merge_fragments` 补设计注释
  (同 few_shot,per-profile 原文混叠稀释)。核实驳回 4 项误报(红线触发已含 positive / `[-N:]` 切片
  本即确定 / profile active 由调用方保证 / rfind 截断已防负值)。

### 第九轮(2026-06-18,立项 B — finding 用户反馈聚合 → confidence 持续校准回路)

设计 §5 标注的 🆕「持续校准回路」:用户对 finding 投 👍/👎 → 持久化 → 聚合更新该 finding 的 confidence。

- **数据层**:迁移 `20260618_0058` 新表 `style_reference_finding_feedback`(feedback_id PK / finding_id FK
  ondelete=CASCADE / operator_ref / vote;**uq(finding_id, operator_ref) 一人一票**)+ `style_reference_findings`
  加 `base_confidence` 列(合成基线,NULL=未经反馈)。全链 upgrade/downgrade 实测通过。
- **聚合规则**(`config/style_reference/feedback.yaml`,可外置):`net = #up − #down`(去重用户);
  `net ≥ promote_net(2)` 升一档 / `net ≤ demote_net(-2)` 降一档,clamp 到 low/medium/high。**温和校准**
  (仅 ±1 档);base_confidence 首次反馈回填且永不覆盖 → **net 回阈值内 confidence 可逆回基线**。
- **服务/仓库**:`finding_feedback.apply_feedback`(回填 base → upsert → 聚合 → 调档写回);
  `repository.upsert_finding_feedback`(一人一票,确定性 feedback_id;**SAVEPOINT 兜底并发竞态**)
  + `aggregate_finding_feedback`。
- **路由**:`POST /findings/{id}/user-feedback`(`_with_idem` 幂等,operator_ref 入幂等 payload 分区)。
- **前端**:`srFindingFeedback` helper + FindingCard 投票按钮接线(真模式发后端 up/down、演示本地 toggle);
  `_serialize_finding` 补 `base_confidence`(前端可展示漂移)。
- **测试** `tests/test_style_reference_finding_feedback.py`(16 用例):持久化 / 一人一票幂等 / 改向 /
  升降档 / clamp / **可逆回 base** / net=0 复位 / 404 / 非法 vote / 路由幂等不翻倍 / **fp finding 投票** /
  **purge 级联删反馈** / base 不被覆盖。迁移头部哨兵测试同步 0057→0058。`vite build` 通过。
- **对抗审查(5 视角)+ 修复**:正确性 / 聚合可逆性 / 迁移安全 / 数据完整性 / 测试+前端。采纳并修复 6 项:
  (1)`upsert` 捕获 IntegrityError → SAVEPOINT 回退 + 退更新(跨后端并发健壮);(2)operator_ref 入幂等 payload
  (防不同用户同 vote 误归因重放);(3)`_load_thresholds` 校验 promote>demote,非法则告警+回退默认;
  (4)**purge_derived_data 显式删反馈行 + FK ondelete=CASCADE**(SQLite 未启 FK pragma,显式删为有效兜底);
  (5)`_serialize_finding` 补 base_confidence;(6)补 fp/net=0/幂等不翻倍/purge 级联测试。
  核实驱回:并发 race(SQLite 串行写已规避,SAVEPOINT 已兜底跨后端)、enum vs str(与 review_finding 既有
  约定一致)、`_actor` 'operator' 回退(全系统既定)、downgrade batch(实测直接 drop 通过)。

### 第十轮(2026-06-18,立项 A — 场景/角色级 apply 绑定目标选择器)

注入应用页(`SrApply`)此前真模式只支持项目级绑定(scope 选择器的「场景/角色」被 `lockNonProject` 禁用,
因缺目标选择器 → `scope_ref_id` 无来源)。本轮补齐前端目标选择器,解锁场景/角色级真绑定。后端早已就绪
(`review_effects._bind_style_profile` 转发 `scope_ref_id`;`resolve_active_binding` 优先级 scene>character>project)。

- **前端**(仅 `ws-styleref.jsx` 的 `SrApply`):新增 `scopeRefId`/`scopeOpts` state;真模式按当前活动项目经
  `apiGet` 拉 `/catalog`(场景 {scene_id,title})与 `/library`(角色 {character_id,name})填选项(自包含,
  不依赖 catalog 缓存);移除「真模式强制项目级」逻辑,解除 `lockNonProject`;scope=场景/角色时渲染目标下拉;
  effect 补 `scope_ref_id`(项目级=project_id,场景/角色级=选中 id),dedupe_key 含之;apply 按钮在未选目标时禁用。
- **后端防御**:`_bind_style_profile` 对 scope=scene/character 缺 `scope_ref_id` **抛 400**(防静默回退 project_id
  落成「场景级绑定却指向项目」脏数据);项目级仍回退 project_id。
- **测试**:`test_style_reference_hardening.py` 新增端到端验收 `test_bind_style_profile_effect_scene_and_character_scope`
  (scene/character effect → binding scope+scope_ref_id 正确;resolve 命中 scene **与** character 级)+
  `test_bind_style_profile_effect_scene_requires_scope_ref_id`(缺 ref → 400)。`vite build` 通过。
- **对抗审查(3 视角)+ 修复**:前端正确性 / 边界UX / 后端契约。采纳 4 项:(1)`WsWorks.active()` 空安全
  (projId/workTitle 防 works 空列表崩溃);(2)`activeProjId` 入 loader 依赖 + null 重置(防切换活动项目后选项陈旧、
  跨项目数据污染);(3)后端 scene/character 缺 scope_ref_id 抛错;(4)测试补角色级 resolve 命中 + 缺 ref 守卫。
  核实驳回:`book&&book.id` dep(实为稳定字符串)、selLabel 罕见回显、`c.id` 无害 fallback。

### 第十一轮(2026-06-18,立项 B 收尾 — 投票高亮跨刷新持久化)

闭合第十轮审查标记的 HIGH UX 缺口:此前 FindingCard 投票高亮是本地 state,刷新后丢失(虽 confidence
调档已持久且可见,但用户看不到「我已投过票」)。本轮按 operator 回显:`repository.operator_votes_for_findings`
(单查询批量取该 operator 对一组 finding 的票)→ 深层端点 `GET /runs/{id}/findings` 经 `_actor` 取 operator,
`_serialize_finding` 加 `user_vote` 字段 → 前端 `srAdaptFinding` 映射 `vote` + FindingCard 从 `finding.vote`
初始化并随 deep 重载同步。17 用例(+`test_deep_findings_returns_operator_user_vote`:投票后该 operator 回显
user_vote、他人为空)。`vite build` + 全后端绿。**持续校准回路 UX 完整闭合。**

### 第十二轮(2026-06-18,立项 C 验收闭合 — RAG 真实 chroma 后端 hit@5)

此前 RAG 全部单测用 `memory` 后端(字符集交集打分),`rag.py` 的 **chroma 代码路径从未运行**,且设计 §10
「三粒度 hit@5 ≥ 0.7」从未实测。本轮新增 `tests/test_style_reference_rag_chroma.py`(`@pytest.mark.chroma_integration`,
Windows 自动跳过、WSL 跑;只导入 rag/repository/models/vector_store 干净链,不触发 system_config 重导入):
真实 `ChromaVectorStore`(chromadb 1.5.7,确定性 64 维字符频率 embedding)上验证三粒度索引构建 / 召回 /
C 注入 / 清理,并以「每段 60% 前缀作 query、源段须在 top-5」测 paragraph 粒度 hit@5。
**WSL 实测:paragraph hit@5 = 8/8 = 1.00(≥0.7 达成);sentence/scene 召回非空;C rag_block + 红线随注;
delete 清空三 collection — ALL CHROMA RAG CHECKS PASSED。** 立项 C 的量化验收标准至此**经真实后端实证闭合**。

**剩余工作 → 独立立项**(已全部完成):
- ~~**立项 A** 场景/角色级 apply 绑定目标选择器~~ — **已完成(第十轮)**。
- ~~**立项 B** finding 👍👎 反馈聚合 → confidence 持续校准~~ — **已完成(第九轮)**。
- ~~**立项 C** 策略 C(RAG)三粒度向量召回~~ — **已完成(第八轮);hit@5 经真实 chroma 实证(第十二轮)**。
  剩余纯可选:LLM rerank 接非实时路径(`style_ref_rag_rerank` hook 已就绪);真实大模型 embedding 的语义 hit@5
  (当前为确定性 embedding,需接入真实 embedding 服务才有语义意义)。

主线(审查 → 三轮后端加固 → 黄金语料/本地通道 → 三轮前端真化 → 量化容差校准 → 立项 C RAG → 立项 B 校准回路
→ 立项 A 场景/角色绑定)**全部收口;Phase 3 backlog 三个立项 A/B/C 均已完成**。

### 第十三轮(2026-07-06,深度评审修复轮 — 全模块逐文件审读)

对模块 ~7700 行逐文件深读(含 scene_generation / qc_engine / review_effects 集成面),确认并修复
11 项测试覆盖不到的真实缺陷(`tests/test_style_reference_review_fixes.py` 13 用例 + 既有文件补锚点):

- **D1 few-shot 段型索引名存实亡**:extractor 落 quote 时 `extracted_features` 只写 `anchor_kind`,
  而 `_build_scene_samples_index` 读 `paragraph_type` → 全部引文落 "narration" 桶:Strategy B
  few-shot「每段型取 1、k=3」实际只注 1 条、漂移修正段型优先级空转、Preview 的
  dialogue/description_env/psychology 种子引文恒空。修:落库冗余段型 + 合成时经段落表反查
  (既有数据一并修复);**反例/负空间引文(counter_example / author_avoidance)不入索引**
  (此前 LLM 合成的"反例文本"可能被 few-shot 当风格范例注入);quotes 改 **run-scoped**
  (同书多次抽取时旧 run 引文不再混入新 profile 的索引/计数/few-shot 池)。
- **D2 输入量门槛(§6.4)只算不执行**:`assess_input_size` 的 skip 评估仅入库供前端展示,
  RunOrchestrator 照跑全四层——字数不足的书白烧 16 sub_dim 的 LLM 调用,且与矩阵页展示的
  skip 相互矛盾。修:start_extract_run 剔除 skip 层(记入 `coverage_json.skipped_layers`),
  全 skip → 409 `STYLE_REFERENCE_INPUT_TOO_SMALL`;新增 `force` 逃生门(API + StartRunRequest);
  React 端 rerun 对该 409 给专门文案 + 一键强制重试。无 input_assessment 的历史数据不门控。
- **D3 量化对照 8 个段型比例指标是系统性伪信号**:回测把生成文本全部标 narration(无分类器),
  narration_ratio 恒 1、其余恒 0,对话密集的参考书必然把 pass_rate 拖到 0.8 以下 → qc gate
  恒 PARTIAL 进人审。修:`check_quantitative` 排除 8 项分类器标签依赖指标(`TYPE_RATIO_METRICS`),
  对照面收敛为 18 项纯文本统计;8 项仍保留在 baseline/抽取锚点/前端展示。
- **D4 metrics 字符集勘误(影响面最广)**:`question_density` 用 `"??"`(两个 ASCII ?)、
  `semicolon_density` 用 `";;"` ——**全角 ？/； 完全不被统计**,中文文本两指标恒 ≈0
  (鲁迅黄金语料实测 0.0 → 修后 9.51 / 3.32 每千字);`_PUNCT_CHARS` 含重复字符(`—`/`"`/`:`
  双计)且缺全角 `：`。修字符集 + 计数函数防御性去重 + 黄金 expected 重生成。
- **D5 补证晋升绕过 span 校验**:两级重试中 shim 的原始 evidence(首轮 LLM 产出)因 Pydantic
  在 ≥2 条校验处提前失败而从未过 span 校验,伪造引文可借补证晋升入库(再经 few-shot 注入)。
  修:抽共享 `_span_valid_evidence`,合并前过滤原始条目(与补证同一套规则)。
- **D6 resolve 不校验 profile 状态**:draft/archived profile 的 binding 仍被
  `resolve_active_binding` 选中——注入侧渲染为空(no-op),qc gate 却照样以该 profile 做风格
  裁决(「从未注入却被风格门拦下」),多层叠加时空层白占预算权重。修:选取单点
  `_active_bindings` 统一过滤,注入 / qc gate 语义一致。
- **D7 终态 run 复活**:排队中的后台 run 被 60 分钟僵尸回收标 FAILED 后,worker 接手照跑,
  把终态拉回 RUNNING→DONE,并与同书新 run 并发互撞。修:层边界 `_observed_status` 检查,
  CANCELLED 之外的终态直接退出不复活。
- **D8 binding 决平精度**:`_ts_to_int` 截到秒([:14]),同秒创建的多条 binding「最新优先」
  退化为非确定插入序;补齐微秒([:20])。
- **D9 抽取数量上限不执行**:§6.5 的 obs ≤8 / fp ≤3 只写在 schema 注释里,LLM 超发全部入库;
  解析处截断。
- **D10 import-path 任意文件读取面**:`ingest_path` 按任意服务器路径读文件入库并可经 API 回读
  (/etc/passwd、.env、*.db);收窄为 .txt/.md/.markdown 白名单(path 模式必须有后缀)。
- **D11 apply 死绑定**(与并行提示词优化批次共同落地):scope_ref_id 为空的 binding 任何
  rank 都不命中,静默不生效;apply 一律要求非空 scope_ref_id(400)。

**明确不改、记为已知限制**:memory 向量后端重启后 RAG 索引不持久(C 策略优雅退化,chroma
为生产后端);metrics 逐段均值不按段长加权(基线与回测同法,对照公平);多层叠加 positive
块标题逐层重复(纯观感);`_load_plagiarism_corpus` 进程缓存删书后不失效(按 checksum 键,
无正确性影响)。

## 九、2026-05-31 收口勘误

- 运行时已下线旧 `/api/v1/reference-books/*` 入口，应用仅保留 `/api/v2/style-reference/*` 作为参考书学习主公开面。
- `POST /api/v2/style-reference/books/{book_id}/reclassify` 已从占位状态改为真实执行：会重跑段落分类、回写 `classifier_calibration` / `paragraph_type_distribution`，并清理旧 runs / findings / profiles / bindings / validation reports / banned terms / 相关 ReviewItem。
- 前端主流程已切到四层全开，默认覆盖 `language + narrative + scene + theme` 共 16 个 sub-dim；文案不再以 “8 sub-dim” 作为主路径描述。
- 当前仓库运行时已移除旧 `reference_books` 路由、旧 `reference_*` ORM 映射和 `reset_style_reference` 工具；当前 `cleanup.py` 仅保留 metric events 清理，历史 migration 语义仍只作为历史记录保留。
> 2026-05-31 运行时对齐说明：正式注入契约以 `SystemPromptFragments + injection-preview` 为准，对外不再公开 `/inject` / `InjectionBundle`。
> 长文续写已收口到 `SceneGenerationService.generate_long_form_continuation(...)`；legacy 运行时依赖(`reference_learning.py` / `/api/v1/reference-books/*` / fallback / legacy ORM / reset tool)已切断，历史 Alembic migration 保留仅作历史记录。

## 十、2026-07-03 全链路实证审查 + 缺陷修复（第十三轮）

**背景**：作者反馈「参考风格模块看到的都是演示数据，疑似只有代码层面完成」。本轮用
mock-LLM 隔离后端（独立 sqlite + memory 向量 + 本地 OpenAI 兼容假服务）把
导入 → LLM 重分类 → 后台抽取（16 sub-dim / 48 findings）→ 审核/投票 → 合成 →
预览 → 注入预览 → 绑定（直连 + review-effect 两路）→ 回测（sync_only / async_full）
整条运行链路真实跑通，实证「代码完成 ≠ 假功能」——但确实揪出并修复了以下缺陷：

- **MIXED 策略不注入 few-shot**（`injection.py`）：`_render_few_shot` 只在纯 B 策略
  被调用，而场景生成默认策略就是 mixed（契约语义 A+B）。已在 MIXED 分支补上
  few-shot 渲染（自带 few_shot_block_max_chars 预算）。新增混合策略回归测试。
- **scene_samples_index 全桶 narration + 反例污染**（`profile_synthesizer.py` /
  `extractors/base.py`）：quote 落库只存 anchor_kind，合成分桶时缺省全归 narration，
  且 counter_example 合成反例、author_avoidance 统计说明也进了 few-shot 样例索引
  （会把反面教材当风格范例注入）。修复：分桶按段落表实测类型（quote 落库同时冗余
  paragraph_type 双保险），只收 anchor_kind=paragraph_quote 的真实原文引文。
- **禁用词链路整体断裂**：`create_banned_term` 无生产调用方 → 注入红线段
  `{banned_terms_list}` 永远为空、extraction 域从未消费、前端编辑器纯本地 state。
  修复：新增 `GET/POST /profiles/{id}/banned-terms` + `DELETE /banned-terms/{id}`
  三端点（幂等、preset 保护、(profile,term,scope) 唯一冲突返既有行）；extraction 域
  接入 `_sample_paragraphs` 段落过滤；React 禁用词 tab 真模式接后端并在增删后刷新
  注入预览。新增 `tests/test_style_reference_banned_terms.py` 全链路测试。
- **apply 缺 scope_ref_id 校验**（`materialization.py`）：三种 scope 都按
  scope_ref_id 匹配（`_binding_rank`），缺 ref 的绑定 rank=99 永远解析不到（死绑定），
  直连 API 却允许创建。现统一拒绝（`STYLE_REFERENCE_APPLY_PARAM_INVALID`）。
- **forbidden_block 重复行**：同一禁忌 statement 跨 sub-dim 重复抽出时逐条罗列，
  现行级去重。
- **前端演示数据陷阱**（`ws-styleref.jsx` / `ws-styleref-val.jsx`）：真实书未抽取/
  未合成时，矩阵/画像/回测/注入各 stage 回退鲁迅演示数据，作者极易误读为「假功能」。
  现真实书全程只显示真实数据：未抽取 → 空态矩阵 + 引导启动抽取；无画像 → 画像/注入/
  回测均给空态引导；概览的趋势图/Evidence 重试链假面板仅演示书显示（真实书改显最近
  run 真实进展）；演示书标「演示」徽章；few-shot 子页真模式直读注入预览的
  few_shot_block 真实产物。

回归：style-reference pytest 子集 394 passed（新增 8 测试）；frontend-react vitest
50 passed + build 通过；隔离后端 stage C 复验全部通过（few-shot 有产物、样例索引
按 dialogue/description_env/action/description_char 分桶、禁用词进红线段、
无 ref apply 被拒、抄袭检测对语料重叠文本正确判 plagiarism）。

尚未闭合（后续可选）：注入应用页「叠加注入层」卡片仍为示意图（真实叠层预览需要
resolve_binding_layers 的只读 API）；`SR_TASKS` 的 per-TaskType 刷新参数展示值
未接 config。

### 第十三轮补遗（2026-07-03，两项遗留闭合）

- **叠加注入层接真**：新增 `InjectionService.describe_binding_layers()`（只读，复算
  `_resolve_fragments` 的权重/预算分配，附各层截断后 block 规模与合并概要，不写
  metric 事件）+ `GET /api/v2/style-reference/injection/layers`（query:
  project_id / task_type / scene_id / character_ids 逗号分隔）。React「叠加注入层」
  子页真模式改由该端点渲染（无绑定给空态引导；上下文自动带本画像已绑定的场景/角色
  id 使对应层亮起），SR_LAYER_STACK 示意图仅演示书保留。
- **TaskType 默认策略/刷新参数接真源**：补上设计手册 §5.1 规划却从未落地的默认表
  `DEFAULT_STRATEGY_BY_TASK`（injection.py）+ `GET /injection/task-defaults`。
  refresh_every_chars 从 `llm_node_registry` 的 `long_form_continuation` 节点直读
  （=8000，scene_generation 运行时真消费的值；此前前端写死的 1500/2000 为虚构）。
  React 任务卡片真模式水合该端点，失败回退静态值。

回归：style-reference pytest 子集 399 passed（新增 `test_style_reference_injection_layers.py`
5 例：默认表对齐节点注册表、空/单层/双层加权与只读性）；frontend-react vitest 50 passed
+ build 通过；`:8000` 活体验证 task-defaults 表与 layers 单层真数据（真实画像标题/分块
字数/合并前缀字数）。至此第十三轮"尚未闭合"清单清零。

### 第十三轮补遗 2（2026-07-03，抽取 404 修复）

**现象**：真实参考书（189 万字）导入正常，启动抽取 3 秒内 run failed：
`Responses API endpoint returned 404`。

**根因**：用户在系统设置把「提炼整理」角色槽配到了 chat-only 中转
（node_routing 里 11 个 style_ref 节点均为 provider_id=oneapi / api_mode=chat），
但 `_llm_helper.call_llm_node` **只读 task_routing**——而
`parse_model_routing_config` 的合并方向是 task_routing setdefault（yaml 赢），
DB 节点路由被 yaml 占位默认（gpt-5 / responses）遮蔽，请求打向中转不存在的
`/responses` 端点。`llm_task_runner` 与 `segmentation/llm.py` 都是
node_routing 优先的正确顺序，唯独这个 helper 漏了——抽取 4 层、补证据、合成、
预览、语义回测、RAG rerank 全走它。

**修复**：`call_llm_node` 对齐三处解析顺序（node_routing 优先，task_routing 兜底）。
新增 `tests/test_style_reference_llm_routing.py` 3 例（DB 覆盖 yaml / 无 DB 路由回退 /
双缺报 LLMNodeError）；受影响模块回归 46 passed；对真实运行库实测 5 个 style_ref
节点全部解析到用户配置的 chat 路由。

### 第十三轮补遗 3（2026-07-03，LLM 连通性系统级加固）

**现象**（路由修复后抽取仍失败）：中转后端推理引擎（lightllm/qwen3.5）不支持
JSON Schema 约束解码——`guided_grammar '<schema>' has compile_grammar_error:
Unsupported tokenizer type`，且按 5xx 重试三连击同一错误后 run 失败。

**定性**：这与此前的 /responses 404 同类——**provider 能力与请求形状不匹配**，
散落在各调用点修不完，一律收敛到 `LLMClient.generate` 统一加固（全部节点受益）：

1. **api_mode 入口归一化**：provider 声明的端点能力（chat/responses）优先于任务
   路由缺省；build 与响应解析共用归一后的 request.api_mode（避免"按 chat 发、按
   responses 解析"错位）。env 回退路径自动 no-op。
2. **连通性降级阶梯**（最多 3 跳，逐跳记 warning）：
   /responses 404 → api_mode 换 chat；json_schema 被拒（guided_grammar /
   compile_grammar_error / guided_json / response_format 等特征）→ 退 json_object
   → 仍被拒 → 不发 wire response_format（prompt 本身约束 JSON，客户端解析不变，
   新增 `LLMRequest.wire_response_format`）。429 限流与无特征硬错不降级。
   引擎级结构化输出错误在重试环内**早退**（重试不会自愈，不烧重试预算）。
3. **能力缓存**（进程内，按 provider_id+model）：降级成功的结论只降不升地缓存，
   后续调用直接按学到的档位发请求——重分类 500+ 批次不再每批浪费一个 400。
4. **按节点超时**：`TaskModelConfig.timeout_seconds` 新增并透传（_llm_helper +
   llm_task_runner）；style_ref 节点未显式配置时 120s 保底（30s 全局默认吃不下
   重抽取），models.yaml 给 4 个 extract + synthesize 显式 180s。

**实证**：对用户真实中转（sensenova hub / deepseek-v4-flash / chat）探测：
第 1 次调用触发一跳降级后成功返回正确分类；第 2 次调用零降级直接成功。
`tests/test_llm_client_connectivity.py` 9 例（归一化 / 404 降级 / schema 降级早退 /
json_object 三跳 / 缓存 / 429 不降级 / 无特征不降级 / 超时配置解析 / style_ref 超时保底）。
旧测试 `responses_404_includes_protocol_hint` 更新为降级链新契约。

### 第十三轮补遗 4（2026-07-03，空正文/写锁/schema 内联/并发守卫）

抽取再战三类新故障，全部修复并经真实中转实证：

- **空正文降级**：reasoning 模型把 max_tokens 烧在思考上，200 但 content 为空
  （`LLM_RESPONSE_MISSING_TEXT`），旧逻辑按"可重试"同参空转 3 次（每次一整个生成
  时长）。现空正文不做无脑重试，进降级阶梯：关 reasoning 参数 + 输出预算×2
  （封顶 8192）；"关 reasoning"进能力缓存。顺带兼容 completions 风格
  `choices[0].text` 正文。
- **schema 内联**：wire json_schema 降级为裸 json_object 后模型看不到输出形状，
  实测把 statement 写成 description、span 写成原文字符串——Pydantic(extra=forbid)
  全灭。现降级/缓存路径都会把 JSON Schema 内联进 system prompt；extractor 解析器
  另加键级容错归一（statement 别名、非法 span 置 None、未知键剔除；内容级校验
  照旧）。真实中转实测：3/3 findings statement 可用、6/6 引文命中原文。
- **SQLite 写锁跨 LLM 调用**（"database is busy"根因 1）：后台抽取只在层边界
  commit，一层 4 个 sub_dim 的写事务跨多次分钟级 LLM 调用持锁，并发 UI 写操作
  等满 busy_timeout(30s) 后报错。现 extractor 支持 checkpoint，后台模式每个
  sub_dim 落库即 commit，锁持有降到毫秒级。
- **同书并发 run 守卫**（"database is busy"根因 2）：连点「重跑抽取」会起两个
  后台线程并发抽同一本书互撞写锁。`start_extract_run` 现拒绝同书活跃 run
  （409 STYLE_REFERENCE_RUN_ALREADY_ACTIVE）。

测试：连通性 12 例（新增空正文降级+缓存、legacy text、schema 内联）、
extractor 归一 1 例、orchestrator checkpoint/并发守卫 2 例。

### 第十三轮补遗 5（2026-07-03，思考型模型空正文的最终解）

补遗 4 的"关 reasoning + 扩预算"对该中转不奏效——qwen 系模型**默认开思考**且不认
OpenAI 的 reasoning 参数,8192 预算照样烧光,run 再次失败。最终解:

- openai 家族 adapter 新增 `provider_options.extra_payload` 直通(降级机制与用户
  配置共用的 wire 参数逃生口);
- 空正文降级第一跳(仅 `openai_compatible` 类型,严格官方 API 不发未知参数)合并
  发送 `chat_template_kwargs.enable_thinking=false` + `enable_thinking=false`
  (lightllm/vllm/DashScope 三系通吃),结论进能力缓存(`disable_thinking`);
- `LLM_RESPONSE_MISSING_TEXT` 报错带诊断细节(finish_reason /
  has_reasoning_content / completion_tokens)。

真实中转 20 段(run 实际采样规模)实证:首调两跳降级后 68s 成功
(3/3 statement、6/6 引文命中原文);二调零降级 37s。
备注:deepseek-v4-flash 抽取产出较薄(同 payload 二调返回 0 观察),连通性已闭合,
产出质量属模型能力——建议「提炼整理」角色槽换更强模型。

## 十一、2026-08-19 风格运行时契约统一

- Bundle 同时冻结场景生成与长文续写的多层风格契约；Profile/Binding 后续修改不再改变
  本次生成。契约、层和原文引文引用均可校验，Bundle 只保存引文哈希，不复制原文。
- `frozen / absent / degraded` 状态封死新 Bundle 回退实时 Binding 的旁路；历史 Bundle
  保留兼容解析；四条消费链统一走同一状态解析器，冲突状态也按损坏契约降级。
- 初次风格化改用中性稿作 RAG 上下文，长文续写显式携带 Bundle 并随累计正文刷新；审计
  只保存上下文和注入前缀哈希。
- 候选评分、同步质检和章节漂移检测统一消费冻结多层基线；修复结构化基线参与标量运算
  导致漂移检测降级，以及漂移优先级读取错层的问题。
- 关键场景盲选新增显式“贴近参考风格”原因；服务端冻结无正文的评分快照并提供画像级
  聚合接口。反馈聚合前复核内容哈希，损坏记录不会污染一致率；该反馈只用于观察校准，
  永远不能直接激活生产重排。
- 冻结画像改为字段白名单；质检使用冻结禁用词，RAG/质检/主动重排核对参考书版本，
  避免 Profile 或语料在建包后变化造成“生成、评分、质检各读一版”。


## 2026-09-15 · 导入有界化（真实故障修补）

- 一本 26,677 段的书在 `segments_only` 下导入：锚定集之外 26,477 段按 25 段/批逐批过快模型
  = 1,059 次串行调用；路由到思考型中转（reasoning token 计入 `completion_tokens`、不受
  `max_tokens` 封顶）时实测 17.7 s/次 ≈ 5 小时，同步 HTTP 请求里作者只看到「导入没成功」，
  且上传路由是 `async def` 直接在事件循环里跑，整个工作台在导入期间假死。
- 记账层：供应商用量超出预留额（`usage_exceeds_reservation`）只在有栅栏武装时拦截响应；
  没有栅栏时按真实用量落账、照常交付（`llm_accounting._usage_overage_is_fenced`）。
- 分类器：余段只有 ≤ `LLM_BULK_PARAGRAPH_CAP`（1000 段 = 40 次调用）时才过 LLM；更大的书
  余段整体走顺序感知启发式，省掉快模型锚定对照，`classifier_calibration` 记
  `rest_classifier` / `heuristic_anchor_agreement` / `llm_classified_paragraphs` /
  `heuristic_classified_paragraphs`，总览页「锚定集之外」一行透出。整本 LLM 分类需要异步
  导入任务（未做）。
- 路由：`import_book_upload` 的同步导入改在线程池执行，导入期间其他请求不再挂起。
- 导入进度：两条导入路由按请求的 `X-Idempotency-Key` 在进程内登记簿
  （`import_progress.py`）记录阶段（解码切段 → 段落分类（按批推进）→ 统计 → 写入）与百分比，
  `GET …/imports/{key}/progress` 轮询读出（不落库，终态保留十分钟）；前端 `srRunImport` 在
  POST 挂起期间每秒轮询，参考书库左栏画进度条，成功后自动切到新书，不再弹「已导入」窗。

详细契约见 `docs/style-reference-runtime-contract.md`。

## 2026-09-15 · 参考书活动面板（全模块进度交互）

- 评估：九条耗时路径里只有导入有进度；抽取 run 有层粒度进度但前端不上屏、不续接、20 分钟即停；
  合成 / 重新分类 / 应用画像 / 回测 / 示例预览都是同步请求加按钮转圈。实测（『龙族』190 万字 +
  作者的思考型中转）：抽取 9 分 16 秒（17 次调用，单次 8–78 秒，层间最长 3.4 分钟无变化）；合成
  5–9 分钟，其中逐行源文重合过滤 2.5 秒/行 × 约百行；apply 在收件箱 resolve 事务里建 RAG 索引
  34.7 秒；回测前端 60 秒即报超时。
- 登记簿泛化：`import_progress.py` 成为模块级操作登记簿（kind：import / reclassify / synthesize /
  rag_index / validate，各自阶段表与权重；通用 `phase / plan_steps / set_steps / step_done /
  llm_call / succeed / fail`），导入契约不变。
- 活动清单：`activity.py` + `GET …/activity` 把登记簿条目与 durable 行（在跑的 run、十分钟内结束的
  run、回测报告）合成统一形状；抽取 run 的进度改为子维粒度写在 `coverage_json.progress`
  （`sub_dims_*` / `llm_calls` / `retries` / `sub_dim_seconds` → 预计剩余），无需迁移。
- 接线：重新分类走登记簿；合成按幂等键登记阶段并加同书活跃守卫（409
  `STYLE_REFERENCE_SYNTHESIS_ALREADY_ACTIVE`），源文重合过滤改用一次建好的 8-gram 哈希索引
  （`CorpusOverlapIndex`，判定与逐行 `check_plagiarism` 等价）；apply / 收件箱批准只落绑定，索引由
  提交后的后台 worker 建（同画像构建加锁，生成期惰性 ensure 等它建完）；回测 worker 先落本地三路
  再跑语义路；预览端点接受 `paragraph_types`。
- 前端：一张活动表 + 一个 `/activity` 轮询喂左栏「参考书活动」面板（类型标签、进度条、已用 /
  预计、抽取可取消、终态可关闭），刷新页面后续接；头部按钮在抽取 / 重新分类期间锁定，书库徽标与
  总览显示进行中；完成不再弹窗；回测去掉 60 秒上限、四路逐路点亮；预览逐张点亮。
- 未做：合成的 durable 任务行（登记簿在进程内，后端重启时在途合成与导入一样丢失）；策略 C 去留。

## 2026-09-15 · 严格 LLM（没有兜底）+ 后台分类任务

- 作者的要求：LLM 是系统运行的基础，LLM 可用时只准用 LLM，不可用就报错去买资源，不要任何兜底。
- 产品路由：导入 / 重新分类没有 LLM 就 409 `STYLE_REFERENCE_LLM_REQUIRED`；分类器不再降级到
  启发式（LLM 失败 → 502 `STYLE_REFERENCE_CLASSIFICATION_FAILED`）；余段上限删除，整本每一段都由
  LLM 分类；书不超过锚定集时不做快模型对照。启发式只剩服务层的离线夹具模式（测试 / 本地语料工具）。
- 后台任务（`import_job.py`）：请求只做准备工作（书 `ingesting` + 未分类段落行 + `stats_json.classification`
  游标），单线程执行器逐批分类、逐批提交，可续跑（`reclassify {resume:true}`）、可取消
  （`classification/cancel`，book.status=cancelling，下一批边界生效）、重启后由启动恢复续跑；
  完成后才算 `ready`，抽取对非 ready 的书 409 `STYLE_REFERENCE_BOOK_NOT_READY`。
- 「仅本机」策略：只能由本地模型（ollama 或回环 base_url）处理，云端模型一律 409
  `STYLE_REFERENCE_CLOUD_POLICY_BLOCKED`（author_action 指向系统配置）；有本地模型时抽取 / 合成 /
  回测都可用。
- 回测：全量三路没有 LLM 就 409；critic 调用失败即报告失败，不再降成 partial；同步快路径仍是显式的
  无 LLM 模式。
- 前端：书库徽标「分类中 / 取消中 / 未完成」，总览「段落分类」卡（进度 / 继续分类），活动面板的
  取消 / 继续分类，头部按钮在分类 / 抽取期间锁定并带原因（「重跑抽取没反应」就是这个锁没有反馈），
  矩阵空态按正在跑的操作说话。
- 未做：分批并行调用 / 更大的 BATCH_SIZE（缩短整本分类的小时级耗时）；合成的 durable 任务行。
