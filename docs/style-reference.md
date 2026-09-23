# 风格参考（现行说明）

> 风格参考模块的**现行说明**，对应 2026-09-23 风格参考 v3 之后的代码（Alembic head `20260923_0091`）。
> 本轮重构的设计、问题台账与完成日志见 [风格参考 v3 变更记录](style-reference-v3-2026-09-23.md)；更早的设计、评估与实施账本归档在
> [history/style/](history/style/)，只描述当时的实现。本文与代码冲突时以代码为准，并回头改本文。

## 1. 它是做什么的

- 作者导入一本参考书（下文以『龙族』为例），系统学会这位作者**怎么写**，起草时让模型用这位作者的手笔写作者自己的故事。
  目标是**全面模仿**：叙述口吻、幽默、比喻取向、节奏、对白、价值姿态一律以参考为标准。
- 有风格绑定时，雪花构思只提供**框架**（发生什么、谁在场、因果、顺序、视点与人称）；设计文字里的调性词不再约束成文，气质以参考为准。
- 只学写法、不复刻：原文句子与本书专名（人名、地名、组织、设定词）不许进正文，由唯一抄袭门把关（§8）。
- **严格 LLM**：分类、学习、对照检查都要模型；没有可用模型时 409 `STYLE_REFERENCE_LLM_REQUIRED` 并引导去系统配置，产品路径没有
  启发式兜底（启发式分类器只留作测试夹具模式 `IngestService(llm_enabled=False)`）。

## 2. 三步

「风格」页（`frontend-react/src/ws-styleref*.jsx`；说法在 `ws-styleref-model.js`，请求与缓存在 `ws-styleref-store.js`，16 维 /
段落类型 / 场面标签的唯一词表在 `ws-labels.js`）：

1. **参考书**：导入 txt / md，选「原文能发到哪里」、勾权属声明；请求只做准备（解码、切段、剥副文本、记场界、落库），然后建 `classify` 作业。
2. **学习文风**：一个按钮、一个 `learn` 作业（§4.3）；下面就是文风画像（气质、16 维文风卡、✓ / ✗、声音、尺度、禁用词）。
3. **用于作品**：参考方式、样例窗数、起草方式，直接写绑定（一个目标只有一条生效绑定）；「本场预览」与起草同一套选窗。

左栏「参考书活动」列出在跑与刚结束的作业（段落分类、学习文风、对照检查），可取消、可继续。

## 3. 数据模型

| 表 | 内容 |
|---|---|
| `style_reference_books` | 参考书：`cloud_policy`、`text_checksum`（唯一，重复导入 409）、`status`（`ingesting` / `cancelling` / `ready` / `failed`）、`stats_json` |
| `style_reference_paragraphs` | 段落：`paragraph_index`、`paragraph_type`（8 类，枚举 `schemas.ParagraphType`；分类前 `unclassified`）、原文 |
| `style_reference_jobs` | 统一作业表（§4） |
| `style_reference_windows` | 持久化的全书样例窗口：起止段、章号、章内位置、字数、段型构成、对白占比、典型度、测量核特征、标签 |
| `…_runs` / `…_extractions` / `…_findings` / `…_evidences` / `…_quotes` | 学习作业的抽取血缘：每条发现带 2–4 处逐字核对过的原文引文，文风卡句引用它们 |
| `style_reference_profiles` | 画像 `profile_json` v3；重新学习**就地更新**同一行（`version_tag` +1，绑定不动） |
| `style_reference_injection_bindings` | 绑定：`scope` ∈ project / scene / character（旧 global 只读兼容）、`config_json` v3；`strategy` 列恒写 `mixed` |
| `style_reference_banned_terms` | 生成期禁用词：作者录入、预置（不可删）、学习作业自动登记的受保护专名（`source="protected_auto"`） |
| `style_reference_scene_windows` | 每场冻结一次的选窗（§5.2） |
| `style_fidelity_readings` | 「像不像」读数（§7），按场景 / 画像记，不存章 id |
| `style_reference_metric_events` | 只追加的审计遥测（`cleanup.cleanup_metric_events` 90 天留存） |

迁移 `20260923_0090` 建作业 / 窗口 / 读数 / 每场选窗四张表；`20260923_0091` 删掉 👍/👎 表 `style_reference_finding_feedback`、旧回测表
`style_reference_validation_reports` 与 `style_reference_findings.base_confidence`（降级只恢复结构）。删书与破坏式重新分类共用
`cleanup.purge_derived_data`；读数与每场选窗属于作品侧历史，删书时保留。

**`book.stats_json`**：`paragraph_root_sha256` / `paragraph_count`（段落根哈希；改段落的写入者负责 `pop`）、`paragraph_types_revision`
（分类每成功一次 +1）、`classification_provenance`（`llm` / `legacy_heuristic` + 一致率）、`window_index`（窗口表的有效标记：根哈希或索引版本
变了整组重建，类型或测量核变了就地重算、保留标签）、`scene_breaks`（导入时记下的场界）。

**画像 `profile_json` v3**（`learn_job._profile_json`）：

| 键 | 内容 |
|---|---|
| `dimension_card` | 文风卡（`card.DimensionCard`）：16 维按辨识度排，每维一句概括、通用模型的默认写法、1–3 条「这位作者这样写」（`do`）与「作者不这么写」（`avoid`，每句带证据引文，允许 ≤11 字的原话作例子）、手法名（选窗用）、可测特征；顶层 `temperament`（气质，至多 4 条必须体现） |
| `card_line_states` | 作者对每句的 ✓（`pinned`，每场都带）/ ✗（`excluded`，不再用）；不让画像失效，重新学习时沿用 |
| `voice` | 测量核声音特征 + 具体习惯句（作者自己的高频词与大致频率）+ 作者自身的参照分布 |
| `voice_signature` | **过渡别名**：与 `voice` 同源、不带分布。运行时契约的画像白名单与刻意复沓旗标 `deliberate_repetition` 的读者（新鲜度预算、场景诊断）还在用——保留 |
| `structure_card` / `planning_guidance` / `narrative_guidance` | 结构画像（章长、开合类型、章首章尾样例、场长、章题样式）、场景手法、叙事机制 |
| `qualitative_summary` / `metrics_baseline` / `sub_dimensions` | 概述、量化基线（旧读者用）、按维摘要 |
| `reference_basis` / `learned_from` / `protected_terms_version` | 学自哪本书；学时的类型版本与根哈希（据此提示「段落类型已更新 / 正文变过，建议重新学习」）；专名集合指纹 |

没有 `dimension_card` 的旧画像照样能用：旧的正向特征 / 禁忌陈述充当卡替身（含数字的行不带），界面标「旧版画像」。

**绑定 `config_json` v3**（唯一解释者 `binding_config.normalize_binding_config`）：

| 键 | 取值 | 作用 |
|---|---|---|
| `reference_mode` | `full`（默认）/ `samples_only` / `card_only` | 全面模仿（样例窗 + 文风卡 + 声音 + 红线）/ 只用原文样例（对照）/ 只用文风卡与声音、不发原文；`segments_only` 的书一律压成 `card_only` |
| `sample_windows` | 0–16，默认 12 | 每场的样例窗数 |
| `dimension_states` | 各维 `emphasize` / `normal`（默认）/ `exclude` | 重点：排卡首、多带一句、选窗偏向它、读数权重 ×2、优先修改；不学：从卡、读数、修改里去掉 |
| `draft_mode` | `style_first`（默认）/ `neutral_first` | 作者手笔直起 / 先中性后润色（阅读对照组）；冻结进运行时契约 |

旧绑定一次映射：策略 A → `card_only`，B / C / mixed → `full`，`intensity` i → `round(3 + 9·i/100)` 窗。

## 4. 作业

### 4.1 作业表与工人（`style_reference/jobs.py`）

- queued → running → succeeded / failed / cancelled。认领时 `attempt` +1、换新 `owner_token`；之后的每一次写（心跳、进度、游标、结束）
  都以「owner_token 仍是我、state 仍是 running」为条件——被清扫重排、取消、删书的作业，旧工人的写全部落空，自然停下。
- 心跳 15 s，超过 60 s 算过期。FastAPI lifespan 启动常驻清扫线程（`start_job_sweeper`：启动时一次，之后每 30 s），把过期的 running
  放回 queued、派发所有 queued 到有界线程池（分类 / 学习 2 个工人；对照检查单独一条车道 2 个工人，不在长作业后面排队）；重复派发无害。
  处理器在模块导入时注册（`import_job` / `learn_job` / `check_job`）。
- **进程退出不算失败**：lifespan 结束（`--reload`、停服）时 `shutdown_job_workers` 把「工人代」+1，在跑的处理器在下一个检查点
  （两秒内）抛 `JobInterrupted`，作业放回 queued（游标、attempt 保留），下次启动的清扫接着跑；Ctrl-C 同样放回队列。LLM 调用跑在守护线程里
  （`DaemonCallPool`），退出时不等在飞的网络请求（结果丢弃，续跑时重发那一两批；记账预留按 TTL 回收）。被 SIGKILL 的进程什么也做不了：
  作业留在 running，心跳过期后由清扫放回队列。
- **互斥**：同一本书的分类与学习互斥，各自也只能有一个活动作业。建作业「先插入、再查」——INSERT 已拿到 SQLite 的写锁，几乎同时的两个请求
  在这里串行化，后到的一定看得见先到的（`409 …_ALREADY_ACTIVE / …_BOOK_LEARNING / …_BOOK_CLASSIFYING`）；续跑放回队列之后同样复查；
  破坏式重分类先写书行拿锁再查、再清派生数据。
- 取消：排队中或心跳过期的作业在请求里直接收尾，运行中的在下一个检查点收尾；工人死前被要求取消的作业由清扫直接收尾。框架里收尾的取消都跑
  这类作业登记的收尾钩子（分类：书的状态；学习：run 行）。放回队列是条件写：已经成功的作业不会被「继续」拉回 queued。
- `GET /api/v2/style-reference/activity` 只列作业表条目（`job:<id>`，在跑的 + 十分钟内结束的）；各个建作业的响应都带 `job_id`。

### 4.2 段落分类（`import_job.py` + `segmentation/llm.py`，kind=classify）

- 模式：`import`（书 `ingesting` → `ready` / `failed`）、`reclassify`（破坏式：先清派生数据）、`retype`（就地重标：正文、画像、绑定都保留，书全程 `ready`）。
- 全书分层抽样的 200 段锚定集交强模型（`style_ref_paragraph_classify_anchor`）与快模型（`…_bulk`）对照，一致率 ≥0.85 余段交快模型，否则强模型。
- 一批 ≤6,000 字且 ≤100 段，3 批并行，每批至多 3 次调用（退避重试两次）。输出**按条收**：段号属于本批、类型在枚举内、只出现一次的条目
  先收下，下一次只重发还没分出来的段（模型在长列表里漏一段时不再整批重发）；重试用尽仍有段没分出来，作业失败
  （`STYLE_REFERENCE_CLASSIFICATION_FAILED`，`details.unresolved` / `first_unresolved_index`），这一批里已分出来的段照样落库，
  「继续分类」只重发没分出来的段。一批失败时不再派发新批，已经在飞的批等它们回来、照常落库。每批前重查所有权、取消、书是否还在、
  云策略是否允许**这个节点的实际路由**。

### 4.3 学习文风（`learn_job.py` + `learn_*.py` + `protected_terms.py`，kind=learn）

七步，每步写游标，重启 / `--reload` / 「继续学习」都从游标续：

1. `windows` 整理全书样例窗口（`windows.ensure_window_index`）；
2. `select` 挑约 12 窗 / 4 万字（章首、章末、对白、叙述、心理、动作、描写分层配额，一章至多一窗，按书的校验和定种）；
3. `extract` 四层（`style_ref_extract_<layer>`）各一次调用读**同一组**原文，引文逐字核对，失败重试一次并合并有效项；语料不足的层跳过；
4. `synthesize` 一次调用写文风卡（`style_ref_synthesize_profile`）：无依据或带统计数字的行丢掉，按实测声音对账去矛盾；
5. `protected` 受保护专名：统计候选（日常词不送）→ 模型确认（`style_ref_protected_terms` v2：只要专有名称与作者自造的词，
   日常词、通用范畴词、单字一律不要）→ 确定性筛子（必须在原书里原样出现、2–12 字、不是单字、全书至少出现 3 次、不在
   日常词 / 范畴词表里，`protected_terms.parse_protected_terms`）→ 写成 `protected_auto` 禁用词（作者录入的行不动）；
6. `tags` 给全书每个窗口打场面 / 情绪 / 手法标签（`style_ref_tag_windows`，每批 ≤8 窗；词表 `tags.py`）；
7. `finalize` 专名与原文重合过滤卡片、沿用 ✓ / ✗、写画像，一个事务；卡片被滤空则作业失败，在用的画像不动。

建作业时就拒：书未就绪 `STYLE_REFERENCE_BOOK_NOT_READY`、正在分类 `…_BOOK_CLASSIFYING`、已在学 `…_LEARN_ALREADY_ACTIVE`、学习节点没有
路由或模板是旧版 `…_LEARN_CONFIG_MISSING`（均 409）。学习在跑时重新分类一律 409 `STYLE_REFERENCE_BOOK_LEARNING`。

### 4.4 对照检查（`check_job.py`，kind=check）

`POST /api/v2/style-reference/checks`：`text`（≤6 万字）与 `scene_id` 恰好给一个，并说明对照哪份参考（`profile_id` 或 `project_id`）。
作业做三件事：确定性读数 → 参考评审（模板 `style_ref_check_judge`，走 `soft_qc` 节点路由；冻结选窗前 4 窗 + 文风卡 + 声音 + 红线；
16 维各 0–10 分 + 总分）→ 抄袭门（只记计数），写一条 `source=manual_check` 的读数。评审失败作业就失败，不降级成只有读数。

- 评审的参考块按 `soft_qc` 的**实际路由**判云策略（§11）：「仅本机」的书遇云端路由 → 作业以 409 `STYLE_REFERENCE_CLOUD_POLICY_BLOCKED`
  失败，参考一个字都不发；并按评审模板的输入预算压（与 soft_qc 同一档，`NOVEL_SYSTEM_SCENE_INPUT_TOKEN_BUDGET` 收紧时照收紧；待查文字
  长到一窗参考都装不下 → 409 `…_CHECK_REFERENCE_EMPTY`，`details.reason = "input_budget"`）。
- 所有权：每个进度写之后立刻提交（不带着写锁去建窗口索引、跑抄袭门）；评审回来先确认作业还是自己的（没被取消、没被清扫重排给别的
  工人、书没被删）再记读数；进度写或「完成」写落空 → `JobLost`，读数随事务一起回滚。

## 5. 注入：参考怎样进每一个提示

### 5.1 StylePolicy 与运行时契约

- `services/style_policy.py` 的 `StylePolicy` 是「有没有绑定、要不要让位、怎么送参考」的**唯一**判断：`style_policy_for_bundle`（场景管线，
  按契约**载荷的内容指纹 + 冻结状态**记忆——不信载荷自报的 `contract_hash`，改过内容的载荷永远拿不回 frozen）、`style_policy_live`
  （写作台等没有 bundle 的节点）。`defers_house_taste()` = 有绑定且作者手笔直起。
- 运行时契约 v2（`runtime_contract.py`）在 bundle 构建时只冻结**最具体的一层**绑定（scene > POV 角色 > 其余角色 > project > global）：
  白名单内的画像键、规范化后的绑定配置、书快照（校验和、云策略、段落根哈希、窗口索引版本），不冻结原文。书被改过时按当前窗口索引
  渲染并记 `STYLE_REFERENCE_BOOK_CHANGED`；bundle 冻结了「没有绑定」时，后来的绑定不改变已建场景的重放。
- 书快照的 `cloud_llm_allowed_at_freeze` 是**与节点路由无关**的策略口径（`policy.book_allows_cloud`：非本地策略 + 严格发送权声明；
  「仅本机」恒 False）。能不能送、送什么在每一次渲染时按接收提示的节点判（§11）。渲染时参考方式还要按书**现在**的云策略压一次
  （v1 契约的书快照没有云策略，`segments_only` 的书照样只送文风卡）。

### 5.2 每场冻结选窗（`inject/selection.py`）

只看本场设计、不看草稿：窗口表 + 种子（`scene_id`）+ 章内位置 + 场面标签（蓝图给的 `situation_tags`，没有就从设计推）+ 对白 / 概述
倾向 + 窗数 + 维度状态 + 文风卡手法。配额（k=12，其它 k 按比例）：章首 / 章末位置 ≤3、场面标签 ≈4、重点维 / 近期偏差维的手法示范 ≈2，
其余在全书按典型度加权抽样，一章至多一窗。结果冻结在 `style_reference_scene_windows`，同一场的首稿、修改、评审、补丁看同一组窗；
评审节点取前 4 窗（`REVIEW_K`），规划节点前 3 窗（`PLAN_K`），改稿至多把 2 窗换成示范要改那几维的窗。呈现时按原书顺序。

没有 bundle 的节点（对照检查的评审、写作台的深评 / 局部深评 / 局部补丁 / 建议）先看这一场 `SceneRunState.current_bundle_id` 冻结的
那一行：契约哈希就是现在这份策略的契约哈希、索引根哈希也没变，就用那一组窗（不另写一行 "live"）；否则才自己选窗、冻结。覆盖过期的
冻结行在保存点里写，写失败不弄坏调用方的事务。

### 5.3 渲染（`inject/render.py`；入口 `style_prompt_injection.inject_style_reference_prefix`）

- 输入是 `inject/request.StyleRenderRequest`：`role`（`draft` / `revise` / `review` / `plan`）、落点、窗数上限、场景设计、改稿维、近期偏差、
  **接收这份提示的节点** `node_ids`（适配器的 `node_id=` 参数，不给就按 `prompt["template_name"]` 推，见 §11）。
- **参考方式说到做到**：`full` = 文风卡 + 声音 + 样例窗 + 红线；`samples_only` = 样例窗 + 红线；`card_only` = 文风卡 + 声音 + 红线，卡句
  后面至多挂一个 **≤11 字**的原话例子（取自这句证据引文里最长的一个短分句；含受保护专名 / 禁用词的不用，引文里有疑似指令的整条不用）。
- **落点**：起草 / 改稿把样例放在 **user 消息末尾**、紧挨输出（收口指令；章首 / 章末场加开章 / 收章指令），调用方用 `apply_style_user_tail`
  接上；system 里是文风卡（气质与必须体现在前）、声音习惯、近期常见偏差（≤3 行）、红线。默认 12 窗时一场约带 4–4.7 万字原文。
  评审 / 规划的样例留在 system 里，标题用评审 / 规划的口径。
- **这一次一窗样例都没带**（只用文风卡、原文不许送、还没有窗口、预算拟合去光了窗）：在样例原本的位置写一句「本次没有附参考作者的原文
  样例……照文风卡与声音特征写」（起草 / 改稿在 user 尾部、仍以「篇幅 / 只返回 JSON」收尾；评审 / 规划在 system 前缀里），审计记
  `no_samples_note`。这句话不用 `[风格样例]` 标签（带这个标签就是真附了原文样例）。
- **文风卡的预算**（`card_budget_chars`，默认 2600 字）：气质与 ✓ 钉住 / 标「必须」的句先占、永不因预算去掉（要去只能整张卡不发）→
  「作者不这么写」有 25% 的保底份额 → 近期常见偏差 → 每一维的第一句（先让每一维都在）→ 例子 → 每一维的第二、三句 → 保底之外的
  「作者不这么写」。输出次序：气质 → 各维（重点维在前）→ `[作者不这么写]` → `[近期常见偏差]`（卡的末尾）。
- **不学的维**：卡里整维不出现，声音块里这一维的习惯句、近期常见偏差里这一维的条目也不带。
- **红线**：反抄袭模板（`anti_plagiarism_template.txt`）+ 画像禁用词（含受保护专名），带了任一块参考就随注，永不截断。
- 预算：`inject/fit.py` 按整窗、整句贪心地去——样例窗从保留次序的末尾去（改稿换进来示范要改那几维的窗最后去），文风卡按它的保留次序
  倒过来去（先去多出来的句与例子，最后才让整维消失；钉住 / 必须的句不单独去）；风格通道模板输入下限 96000
  （`prompt_builder.STYLE_PASS_INPUT_TOKEN_BUDGET`），小上下文模型用 `NOVEL_SYSTEM_SCENE_INPUT_TOKEN_BUDGET` 收紧。审计不含正文。

### 5.4 规划与结构跟随

- 有绑定且直起时，场景蓝图改跑事实版 `scene_blueprint_facts`（仍走 `scene_blueprint` 路由）：只写事实与 1–3 个场面标签，不预写台词、
  不指定意象、不定收尾方式；标签随 bundle 冻结，选窗按它挑作者写同类场面的原文。
- 场景规划、章架构、人物压力蓝图读结构画像与场景手法（`planning_context.py`）；按参考章长 ÷ 本章场数推「一场多长」并抬起草长度上限；
  「AI 起章名」带参考章题样例。
- 绑定变了（新绑定、改配置、换画像、解除、删书）或设计确认后，作用范围内的场景蓝图 / 人物压力蓝图 / 章架构作废，下次运行重做。

## 6. 起草流程（有绑定且作者手笔直起）

1. 场景蓝图（事实版）。
2. 首稿 `style_first_draft`（占中性稿的步位，检查点次序不变）。
3. 事实 QC `hard_qc`（写法从来不是硬违规）。
4. **读数 → 风格步**（`style_reference/style_step.py`）：首稿百分位 ≤ `style_step_max_percentile` 且重点维没越界 → **不调模型**，首稿即风格稿；
   读不出或不可信（不到 600 可见字、参照窗口不到 8 个）同样保留首稿。否则**定向修改** `style_targeted_revision`（走 `style_draft` 路由）：
   只改越界特征所在的维（至多 4 维，重点维在前），带测得的差异与这几维的卡句；改完再读，`distance` 至少小 `revision_min_improvement`
   且过了抄袭门与安全门才采用，否则保留首稿。抄袭门只算修改稿**新带进来**的重合（`introduced_copy`）——首稿里本来就有、
   修改稿照旧留着的不算这次修改的错（首稿自己的重合由硬 QC / 成稿门对全文把关）。读数出错（不是「书没有尺子」）另记原因
   `reading_failed`、提示说实话；读数在保存点里读，出错不弄坏会话、不耽误检查点。
5. 软 QC = **参考评审**（样例 4 窗 + 文风卡，16 维各 0–10 分）→ 有不像的维才补丁；补丁后评分变差、或 `distance` 变大而评分没提高 →
   退回补丁前的稿子（`STYLE_PATCH_REVERTED`）。
6. 准定稿评审（按参考判，分数带范围）。
7. 归档：唯一抄袭门 + 终稿读数。

房风规则只在 `policy.defers_house_taste()` 时让位（反模板门只提示、自动批评不出补丁、成稿门的文学阈值按绑定书校准、新鲜度只留逐字
n-gram、长度带放宽）；事实、必含、禁止、抄袭、禁用词这些硬门从不让位。`neutral_first` 是对照组：中性首稿再由 `style_draft` 改成作者手笔。
`NOVEL_SYSTEM_SCENE_BEST_OF_N_ENABLED` 打开时候选 = 首稿 + (N−1) 个定向修改，先把被抄袭门拦的候选排到最后，再按 `distance` 排序
（首稿赢平手）；续跑时槽位数不少于已经落下检查点的槽位；按正文去重后不到两份就不开关键场景的终选门。风格链路的 `STYLE_*`
提示码挂在尝试记录上，起草台工作台按本次运行读出，连同 `style_fidelity`（各阶段读数与决定）。

## 7. 读数：像不像

- `fidelity.py`（纯函数）：参照分布 = 这本书**自己的**全部窗口在测量核特征上的分布（稳健中位数 / 尺度 + 下限）。`distance` = 各特征 |z|
  （封顶 6）的加权平均（每个可测维合计权重 1，重点 ×2，不学 0）；`percentile` = 作者自己的窗口（留一）里 distance ≤ 它的比例（作者自己的
  一窗平均约 50，越高越不像）；`out_of_band` = |z| ≥ 2 的特征（带维度与白话短语）；`dimension_scores` = 可测维的 0–10 分。
- 测量核 `measure.py`（`KERNEL_VERSION`）是唯一的测量入口：分段规则与编辑器同口径、以可见字为单位、全文汇总、唯一虚词表与对白定义。
  改了任何口径都要升版本（窗口特征与读数随之重算）。
- 入库只有 `readings.record_fidelity_reading`：`source` ∈ pipeline / adopt / archive / manual_check / author_draft，`stage` ∈ first_draft /
  revision / patched / final / manual；同一稿行幂等（终稿不分来源：管线归档之后再确认 / 重新归档同一终稿行同一段文字，还是那一条，
  走势不出重复点）；抄袭门只记计数；失败不阻断管线。
- 展示跟着**选中**的稿子：Best-of-N 时工作台的风格步决定与风格链路提示取选中的那一份候选（终选门选的 → 进软 QC 的 →
  运行态指针），不是最后一个槽位；补丁被退回时评审分给补丁前那一轮；没绑定的场景不给「参考评审总分」（润色口径的软 QC
  顺手给的分不记成参考评审，界面也不画）。
- **近期常见偏差**：同一作品最近 5 次首稿读数里越界 ≥3 次的特征，进下一场首稿文风卡的末尾（≤3 行），选窗也多挑示范这些维的窗。
- 接口：`GET /api/v1/scenes/{id}/style-fidelity`（各阶段最新读数、风格步与补丁的决定、评审分）、`GET /api/v1/projects/{id}/style-fidelity`
  （走势、近期偏差、按维平均，每场一票）、`GET /api/v2/style-reference/readings/{id}`、起草台工作台的 `style_fidelity`。

## 8. 唯一抄袭门与受保护专名（只有原文重合会拦）

`services/reference_copy_gate.py` 是**唯一**的抄袭门，凡是进正文的文字都过它：成稿门（归档）、起草台「采用」/ 再确认、成稿中心、写作台
采纳 AI 建议与局部改写（生成时就筛、采用时再拦，只算建议新带进来的命中），以及定向修改与候选的去留。

1. **原文重合**：规范化（去空白、标点、符号，小写）后与绑定参考书连续 ≥12 字相同即命中（与 `validation/plagiarism.check_plagiarism`
   同口径）；每本书在进程里建一次 12 字元哈希索引，命中再逐字复核，同一段文字只扫一次。
2. **受保护专名**：画像**现行**的生成期禁用词（含 `protected_auto`）+ 环境变量 `NOVEL_SYSTEM_PROTECTED_SOURCE_TERMS_JSON` 的全局词。
   **从不拦**归档 / 采纳 / 提升（专名表是模型认的，难免收进日常词）：成稿门报一条不拦的警告 `source_safety:protected_term`（带命中的词
   与次数，不带参考原文；起草台采用、写作台提升之后说一句中文）；管线在软 QC 里请作者复核（Q2 `reference_banned_term_replicated`，
   作者可以接受）。每道门（抄袭门、软 QC、成稿门）只比对现行的表：作者删掉误收的词立刻不再命中；冻结契约里记下的禁用词只用来
   渲染提示词的红线，不参与判定。

原文重合是唯一的硬门（Q0，每条路径都拦、没有豁免）。只记哈希与位置，不印参考原文。拦下时正文不动：409 `SOURCE_SAFETY_BLOCKED`
+ `author_action`（「第 N–M 字与参考书原文连续 12 字以上相同……」）+ `details.reference_copy`（检查记录：位置 / 计数 / 哈希；每条走
成稿门的路径都带，前端据此说人话）。

绑定的书已不在书库、或风格策略解析降级：那一边**没有查成**（`unavailable`），不能当成「查过、没重合」——风格稿门报
`unavailable`（软 QC 挂 Q2 复核、起草链路发 `STYLE_GATE_UNAVAILABLE`），成稿门报不拦的警告 `source_safety:unavailable`。
检查本身出错（库读不出）才 fail-closed `SOURCE_SAFETY_UNAVAILABLE`。

## 9. 接口

前缀 `/api/v2/style-reference`，包 `api/routes/style_reference/`（写操作都要 `X-Idempotency-Key`）：

| 模块 | 接口 |
|---|---|
| `books.py` | `POST /books/import-upload`（≤10 MB）· `POST /books/import-path`（要配 `NOVEL_SYSTEM_STYLE_REFERENCE_IMPORT_ROOTS`）· `GET /runtime`（有没有模型、分类节点是否本机、推荐的 `default_cloud_policy`）· `GET /books` · `GET /books/{id}` · `GET /books/{id}/paragraphs?start=&end=`（≤80 段）· `GET /books/{id}/classification/estimate` · `POST /books/{id}/reclassify`（缺省破坏式；`{"mode":"retype"}` 就地；`{"resume":true}` 续跑）· `POST /books/{id}/classification/cancel` · `DELETE /books/{id}` · `POST /books/bulk-delete` |
| `learn.py` | `POST /books/{id}/learn`（`{"resume":true}` 续跑）· `GET /books/{id}/learn` · `POST /books/{id}/learn/cancel` · `GET /books/{id}/runs` · `GET /runs/{id}/findings` |
| `profiles.py` | `GET /profiles`（摘要）· `GET /profiles/{id}`（文风画像页）· `POST /profiles/{id}/card-lines/{line_id}`（✓ / ✗）· `GET/POST /profiles/{id}/banned-terms` · `DELETE /banned-terms/{id}` · `POST /profiles/{id}/injection-preview`（本场预览，只读） |
| `bindings.py` | `POST /profiles/{id}/apply {scope, scope_ref_id, config}`（同一目标只留一条生效绑定，旧的在 `replaced` 里说出来）· `PATCH /bindings/{id}`（维度状态按维合并）· `DELETE /bindings/{id}` · `GET /profiles/{id}/bindings` · `GET /projects/{id}/style-binding` · `GET /injection/layers` |
| `activity.py` | `GET /activity` |

`api/routes/style_fidelity.py`：两个 `style-fidelity` 读接口、`GET /readings/{id}`、`POST /checks`、`GET /checks/{job_id}`（§4.4、§7）。
已删除、不要加回来：`/imports/{key}/progress`、旧抽取 run 与 `/runs/{id}/synthesize`、示例预览 `/profiles/{id}/preview`、回测
`/profiles/{id}/validate` 与 `/reports`、`/bindings/{id}/injection-preview`、`/injection/task-defaults`、👍/👎。

## 10. 配置

`config/style_reference/`：

| 文件 | 用途 |
|---|---|
| `injection_budget.yaml` | 单窗上限 5000、文风卡预算 2600、`draft_mode_default`、直起长度放宽 0.5、参考场长上限 5000、上一场尾部节选 900、`fidelity:` 阈值 |
| `function_words.yaml` | 测量核与专名候选共用的闭类虚词表 |
| `voice_baseline.yaml` | 黄金语料分位基线，现在只判「刻意复沓」；重建：`python -m novel_system.services.style_reference.voice_signature build-baseline` |
| `input_thresholds.yaml` | 每层抽取的语料量门槛 |
| `tolerance_floors.yaml` | 量化目标包络下限（`candidate_rerank.build_style_target`，候选审计与改写不退步检查） |
| `banned_adjectives.yaml` | 空泛评价词：抽取陈述里出现就丢掉那条发现 |
| `anti_plagiarism_template.txt` | 红线段 |

`fidelity:` 阈值（`style_step.fidelity_thresholds`）都是**临时值**，上线前用真实模型小规模 A/B 定：`style_step_max_percentile` 90、
`revision_min_improvement` 0.03、`patch_max_distance_increase` 0.05、`judge_tolerance` 0.1（评审总分 0–1 尺度，= 10 分制上的 1 分：
两次独立评审的噪声常有半分到一分，更细的容差会把好补丁当成变差退回）。评审节点的分数按模板 `structured_schema` 声明的刻度
（`maximum`）逐个换算，越界的分丢掉（`review_scores`）；模板没声明刻度（旧提示词快照）时才按一次回答推断量级。

模型节点（`llm_node_registry.py` 与 `config/models.yaml` 同名 task 必须一致）：分类两节点关推理、输出 8192；四个抽取节点与文风卡合成 16384；
`style_ref_protected_terms` 8192；`style_ref_tag_windows` 4096、关推理。起草与评审复用现有路由：`style_first_draft` / `style_targeted_revision`
走 `style_draft`，`style_ref_check_judge` 走 `soft_qc`（5000），`scene_blueprint_facts` 走 `scene_blueprint`。

## 11. 部署与运维

- **提示词同步**：保存过提示词快照的安装读库内快照。改了任何风格模板都要 `python -m novel_system.tools.sync_prompt_templates`（干跑）再
  `--execute`；学习作业开工前核对模板契约，旧模板直接拒为 `STYLE_REFERENCE_LEARN_CONFIG_MISSING`，不白花调用。
- **新节点要路由**：`style_ref_protected_terms` / `style_ref_tag_windows` 在保存过 models 快照的安装上要到系统配置「一键补齐」。
- **输出预算**：库内快照优先于仓库文件，`node_routing` 优先于 `task_routing`；用 `python -m novel_system.tools.raise_llm_output_budget
  --node <id> … --floor <n> --execute` 抬（分类 8192、抽取与合成 16384、`soft_qc` / `near_final_acceptance_review` 5000），分类节点另在系统配置里关推理。
- **云策略**：`local_only` 的书只有这一步**实际调用的节点路由**是本机模型（`ollama` 或回环地址）时才放行，否则 409
  `STYLE_REFERENCE_CLOUD_POLICY_BLOCKED`；`segments_only` 的书可被云端模型读来分类 / 学习，起草只送文风卡。
  - 参考进提示（起草、改稿、评审、规划、本场预览、对照检查）同样按**接收这份提示的节点**判，与全局运行时模型无关——全局是本机而起草节点
    走云端 → 不送；全局是云端而起草节点走本机 → 照送（`policy.decide_reference_route`）。「仅本机」的书遇云端节点时**一个字都不送**
    （没有样例、文风卡、声音、专名表），注入适配器原样抛 409，不降级成没有参考的提示去照样调用那个节点。起草管线（首稿、定向修改、
    风格稿与补丁）因此停下并带 `author_action`；软 QC、准定稿评审、写作台深评 / 补丁 / 建议、场景蓝图的调用方目前自己接住这个错，
    退回不带参考前缀的基础提示照常调用（参考前缀同样没有送出）。
  - 节点从哪来：调用方给 `inject_style_reference_prefix(..., node_id=...)`；不给就按 `prompt["template_name"]` 推
    （`inject/routing.TEMPLATE_NODE_IDS`：`style_first_draft` / `style_targeted_revision` → `style_draft`；`style_draft` 模板 → `style_draft`
    + `style_patch`（软补丁、去模板、安全修复借这份提示在 `style_patch` 下派发）；`style_length_patch` / `style_salvage_patch` →
    `style_patch`；`scene_literary_rewrite` → 它自己 + `style_patch`；`scene_blueprint_facts` → `scene_blueprint`；`style_ref_check_judge` →
    `soft_qc`；`writer_passage_review` → `writer_deep_review`；其余模板名本身就是注册节点的用它）。有几个候选节点时每一个都要满足；
    说不出节点 → 「仅本机」的书按不许送处理。
  - 规划参考块（`planning_context`）不说节点时按全部消费节点判：项目级（雪花 09 / 10、起章名、章规划四节点）、场景级（场景蓝图、
    章架构、人物压力）、起章名；「仅本机」的书有一个消费节点走云端就不给参考块（规划照常，不报错）。
  - 未知 / 空策略：本机节点只送文风卡，云端节点 409 `STYLE_REFERENCE_CLOUD_POLICY_INVALID`。
- **迁移**：停服、`python -m novel_system.tools.db_backup --backup <src.db> <dst.db>`，再 `alembic upgrade head`（0090、0091）。
- **工具**（`backend/` 下，默认干跑、`--execute` 才写库，先备份）：`purge_style_reference_books --book ID`（可重复）和 / 或 `--id-prefix PREFIX`
  （至少 4 个字符；删书及全部派生数据，就是书库删除的 `cleanup.delete_reference_book`，绑定范围内的规划产物一并作废）；`refresh_style_reference_books --book ID | --all`（就绪的书剥副文本、重编号、保留场界、重算统计；段落变了
  会 `pop` 根哈希、窗口随之重建；空行场界只有重新导入才能恢复；有排队 / 运行中的分类或学习作业的书跳过——就地重标时书一直是 ready；
  `--execute` 时每本书先拿写锁、在锁里重新核对并重算计划，`stats_json` 只合并本工具管的键）。

## 12. 排障

| 现象 / 错误码 | 原因与处理 |
|---|---|
| `STYLE_REFERENCE_LLM_REQUIRED`（409） | 没有可用模型；去系统配置 |
| `STYLE_REFERENCE_CLOUD_POLICY_BLOCKED`（409） | 「仅本机」的书遇到云端节点路由（分类、学习、对照检查，也包括起草 / 改稿 / 评审 / 本场预览时参考要进的那个节点；`details.node_id` 是哪个节点，`details.reason = "node_unknown"` 表示调用方没说清节点）；把那个节点换成本机模型，或换一档范围重新导入 |
| `STYLE_REFERENCE_CLOUD_POLICY_INVALID`（409） | 书的云策略认不出来，参考不能进云端节点；重新导入并选一档 |
| `…_SEND_RIGHTS_REQUIRED` / `…_DECLARATION_REQUIRED` | 非本机范围没有确认发送权；重新导入并勾选 |
| `…_BOOK_DUPLICATE`（409）/ `…_BOOK_EMPTY`（400）/ `…_UPLOAD_TOO_LARGE` / `…_BOOK_FORMAT_UNSUPPORTED` | 同一份文本已在书库（`details.book_id`）/ 没有正文 / 超过 10 MB / 不是 txt、md |
| `…_CLASSIFICATION_FAILED`（502） | 某批重试后仍失败；游标保留，「继续分类」 |
| `…_ALREADY_ACTIVE` / `…_BOOK_LEARNING` / `…_BOOK_CLASSIFYING` / `…_BOOK_NOT_READY` | 同一本书有冲突的作业在跑，或分类没完成；等它结束、取消或「继续分类」 |
| `…_LEARN_CONFIG_MISSING` | 学习节点没有路由或模板是旧版；「一键补齐」+ `sync_prompt_templates --execute` |
| `…_INPUT_TOO_SMALL` / 学习失败 `card_filtered_empty` | 正文太少 / 卡句全被专名、禁用词或原文重合滤掉（在用的画像不受影响） |
| `…_PROFILE_STALE`（409） | 画像的依据变过；重新学习再用于作品 |
| `…_CHECK_NOT_BOUND` / `…_CHECK_TARGET_INVALID` / `…_CHECK_JUDGE_FAILED` | 对照检查没有可对照的参考 / `text` 与 `scene_id` 没有恰好给一个 / 评审调用失败 |
| `SOURCE_SAFETY_BLOCKED`（409） | 与参考书原文连续 12 字以上相同（唯一的硬门）；按 `author_action` / `details.reference_copy` 给的位置改写 |
| 成稿门警告 `source_safety:protected_term` | 正文用了画像禁用词表里的专名——不拦；是参考书的专名就换掉，日常词被误收就到文风画像的禁用词里删掉 |
| 成稿门警告 `source_safety:unavailable` / 风格稿门 `unavailable` | 绑定的书已删或绑定解析失败，那一边没做原文重合检查；恢复绑定后可再做对照检查 |
| 提示 `STYLE_REFERENCE_BOOK_CHANGED` / `…_SAMPLES_BLOCKED` / `…_NO_WINDOWS` / `…_BOOK_MISSING` | 冻结后书被改过（按当前索引选窗）/ 样例被云策略挡下（没有发送权声明、或冻结时不许送云）/ 还没有窗口 / 书已删除（冻结快照是送云策略时文风卡照送、原文不送；快照说不清策略时什么都不送） |
| 作业「卡住」（`stalled`） | 心跳过期 60 s 后清扫线程放回队列；也可取消或继续 |

## 13. 测试

`backend/tests/test_style_reference_*.py`、`test_style_fidelity_*.py`、`test_reference_copy_gate.py`、`test_prompt_template_contracts.py`；共享造数
`tests/style_reference_factories.py`，路由测试导书用 `tests/style_reference_route_helpers.import_book`。只用合成文本（真书只能经
`NOVEL_SYSTEM_STYLE_REF_LOCAL_CORPUS` 本机读，永不提交）。分句、启发式或测量核口径变了：重生成 `tests/golden/style_reference/expected/*.json`
（`regen_expected.py`）与声音基线，并在提交说明里写原因。
