# 风格参考（现行说明）

> 风格参考模块的**现行说明**，对应 2026-09-23 风格参考 v3、2026-09-24 清理与优化、2026-09-29 全系统重构以及 2026-10-14 评审统一
> （§4.5、§6、§7a）之后的代码（迁移见[数据库迁移](migrations.md)）。
> 本轮重构的设计、问题台账与完成日志见 [风格参考 v3 变更记录](style-reference-v3-2026-09-23.md)；更早的设计、评估与实施账本归档在
> [history/style/](history/style/)，只描述当时的实现。本文与代码冲突时以代码为准，并回头改本文。

## 1. 它是做什么的

- 作者导入一本参考书（下文以『龙族』为例），系统学会这位作者**怎么写**，起草时让模型用这位作者的手笔写作者自己的故事。
  目标是**全面模仿**：叙述口吻、幽默、比喻取向、节奏、对白、价值姿态一律以参考为标准。
- 有风格绑定时，雪花构思只提供**框架**（发生什么、谁在场、因果、顺序、视点与人称）；设计文字里的调性词不再约束成文，气质以参考为准。
- 只学写法、不复刻：原文句子不许进正文，由唯一抄袭门把关（§8）；本书专名（人名、地名、组织、作者自造的词）起草时要求避开，用到只提醒、不拦。
- **严格 LLM**：分类、学习、对照检查都要模型；没有可用模型时 409 `STYLE_REFERENCE_LLM_REQUIRED` 并引导去系统配置，产品路径没有
  启发式兜底（启发式分类器只留作测试夹具模式 `IngestService(llm_enabled=False)`）。

## 2. 三步（外加第四步「对照检查」）

「风格」页（`frontend-react/src/ws-styleref*.jsx`；说法在 `ws-styleref-model.js`，请求与缓存在 `ws-styleref-store.js`（写操作；底座
`ws-styleref-store-core.js`、活动清单 `ws-styleref-store-activity.js`），16 维 / 段落类型 / 场面标签的唯一词表在 `labels/style-reference.js`）：

1. **参考书**：导入 txt / md，选「原文能发到哪里」、勾权属声明；请求只做准备（解码、切段、剥副文本、记场界、落库），然后建 `classify` 作业。
2. **学习文风**：一个按钮、一个 `learn` 作业（§4.3）；下面就是文风画像（气质、16 维文风卡、✓ / ✗、声音、尺度、禁用词）。
3. **用于作品**：参考方式、样例窗数、起草方式，直接写绑定（一个目标只有一条生效绑定）；「本场预览」与起草同一套选窗。
4. **对照检查**：随时可用的第四步（§4.4）——贴一段文字或选本作品的一场，看它像不像这位作者。
5. **配置对比**：随时可用的第五个标签（§7b）——改风格配置（模型、样例窗数、样例位置）之前，用留出的原文验收换了以后是不是更像。

左栏「参考书活动」列出在跑与刚结束的作业（段落分类、学习文风、对照检查、校准评审、配置对比）：在跑的都可取消（对照检查也可以），失败或取消的可继续；
不可续跑的失败（如正文太短）只给「重新学习」，正文太短另可「仍然学习」。

## 3. 数据模型

| 表 | 内容 |
|---|---|
| `style_reference_books` | 参考书：`cloud_policy`、`text_checksum`（唯一，重复导入 409）、`status`（`ingesting` / `ready` / `failed`；v3 之前的行可能残留 `pending` / `cancelling`，分类作业接手时修正）、`stats_json` |
| `style_reference_paragraphs` | 段落：`paragraph_index`、`paragraph_type`（8 类，枚举 `schemas.ParagraphType`；分类前 `unclassified`）、原文 |
| `style_reference_jobs` | 统一作业表（§4） |
| `style_reference_windows` | 持久化的全书样例窗口：起止段、章号、章内位置、字数、段型构成、对白占比、典型度、测量核特征、标签 |
| `…_runs` / `…_extractions` / `…_findings` / `…_evidences` / `…_quotes` | 学习作业的抽取血缘：每条发现带 2–4 处逐字核对过的原文引文，文风卡句引用它们 |
| `style_reference_profiles` | 画像 `profile_json` v3；重新学习**就地更新**同一行（`version_tag` +1，绑定不动）。v3 之前的画像（没有文风卡）由迁移 `20260924_0092` 归档；有绑定的归档画像在下一次学习时就地复活 |
| `style_reference_injection_bindings` | 绑定：`scope` ∈ project / scene / character（旧 global 只读兼容）、`config_json` v3（迁移 `20260924_0092` 把旧行的策略 / 强度一次性回填成 v3 键）；`strategy` 列恒写 `mixed`（删列要重建表，暂留） |
| `style_reference_banned_terms` | 生成期禁用词：作者录入、预置（不可删）、学习作业自动登记的受保护专名（`source="protected_auto"`） |
| `style_reference_scene_windows` | 每场冻结一次的选窗（§5.2）；`params_json` 带 `book_id` / `profile_id`，删书 / 破坏式重新分类时随书删 |
| `style_fidelity_readings` | 「像不像」读数（§7），按场景 / 画像记，不存章 id |
| `style_reference_metric_events` | 只追加的审计遥测；清扫线程启动时与之后每 24 小时清一次 90 天前的行（`cleanup.cleanup_metric_events`） |

作业表同样有保留期（`cleanup.prune_style_jobs`，批准 #23，随清扫线程每 24 小时一次）：结束了的对照检查作业留 30 天（读数留在读数表里，
「像不像」的走势不受影响；留下的行里送检的原文换成哈希与字数），分类 / 学习作业每本书每种只留最近一个；没结束的、还在活动面板上的不删。

迁移 `20260923_0090` 建作业 / 窗口 / 读数 / 每场选窗四张表；`20260923_0091` 删掉 👍/👎 表 `style_reference_finding_feedback`、旧回测表
`style_reference_validation_reports` 与 `style_reference_findings.base_confidence`（降级只恢复结构）；`20260924_0092` 只改数据（绑定配置回填、
旧画像归档、旧待办行删除，降级为空操作）。删书与破坏式重新分类共用 `cleanup.purge_derived_data`；读数属于作品侧历史，删书时保留，
这本书的每场选窗行一并删除。

**`book.stats_json`**：`paragraph_root_sha256` / `paragraph_count`（段落根哈希；改段落的写入者负责 `pop`）、`paragraph_types_revision`
（分类每成功一次 +1）、`classification_provenance`（`llm` / `legacy_heuristic` + 一致率）、`window_index`（窗口表的有效标记：根哈希或索引版本
变了整组重建，类型或测量核变了就地重算、保留标签）、`scene_breaks`（导入时记下的场界）。窗口标签（`tags_json`，`window_tags_v2`）=
场面 / 情绪 / **维度**（这一窗最能示范的 ≤3 个维度键）/ 梗概，与文风卡无关：重新学习时已有 v2 标签的窗口不再重打。
**示范证据**（`tags_json["evidence"]`，2026-10-19，`learn_evidence`）：学习定稿时把文风卡每句依据的逐字引文（通读全书
时展开到归并前各批的全部引文）落到窗口上，按维计数——一维在一窗里至少 2 处引文才算这一窗示范了它，每窗留前 3 维，
另记示范了哪几句卡（`version` / `profile_id` / `dimensions` / `counts` / `lines`）。选窗（`inject.selection.load_index`）
挑「示范某一维」的窗时有证据的窗用证据维，没有的窗用标签维；重打标签保留证据，索引重建时随窗口一起清空。

**画像 `profile_json` v3**（`learn_job._profile_json`）：

| 键 | 内容 |
|---|---|
| `dimension_card` | 文风卡（`card.DimensionCard`）：16 维按辨识度排，每维一句概括、通用模型的默认写法、1–3 条「这位作者这样写」（`do`）与「作者不这么写」（`avoid`，每句带证据引文，允许 ≤11 字的原话作例子）、手法名（画像页显示）、可测特征；顶层 `temperament`（气质，至多 4 条必须体现） |
| `card_line_states` | 作者对每句的 ✓（`pinned`，每场都带）/ ✗（`excluded`，不再用）；不让画像失效，重新学习时沿用 |
| `situation_card` | 按场面的写法（通读全书才有，`situation_card` 模块）：`{"version", "situations": [{"situation", "windows", "lines": [{"line_id", "text", "finding_ids", "evidence_quote_ids", "source"}]}]}`；进冻结契约，起草时这一场的场面对上了才带 |
| `foreshadow_lines` | 伏笔手法句的原句与 id（通读全书才有，`learn_foreshadow`）：`[{"line_id", "text", "source"}]`；规划用的是 `planning_guidance` 里「伏笔：」开头的那几句，这里只为下次在上一版上修订 |
| `line_evidence` | 每句（文风卡句与场面句）的依据落在哪几章：`{line_id: {"quotes", "chapters": [[章, 引文数], …]}}`；只给画像页（「贯穿全书」「集中在第 X–Y 章」），不进契约 |
| `voice` | 测量核声音特征 + 具体习惯句（作者自己的高频词与大致频率）+ 作者自身的参照分布 |
| `voice_signature` | **过渡别名**：与 `voice` 同源、不带分布。运行时契约的画像白名单与刻意复沓旗标 `deliberate_repetition` 的读者（新鲜度预算、场景诊断）还在用——保留 |
| `structure_card` / `planning_guidance` / `narrative_guidance` | 结构画像（章长、开合类型、章首章尾样例、场长、章题样式；通读全书后另有量出来的场与 `chapter_craft` 章法）、场景手法、叙事机制 |
| `qualitative_summary` | 概述（v2 的 `metrics_baseline` / `sub_dimensions` 已随指标包络一起删除，旧画像里的残留键不再读） |
| `reference_basis` / `learned_from` / `protected_terms_version` | 学自哪本书；学时的类型版本与根哈希（据此提示「段落类型已更新 / 正文变过，建议重新学习」）；专名集合指纹 |

没有 `dimension_card` 的旧画像不再充当卡替身：迁移 `20260924_0092` 把它们归档，界面对这本书显示「没学过」；「学习文风」就地更新
（有绑定的归档画像复活为 active，绑定不动）。

**绑定 `config_json` v3**（唯一解释者 `binding_config.normalize_binding_config`）：

| 键 | 取值 | 作用 |
|---|---|---|
| `reference_mode` | `full`（默认）/ `samples_only` / `card_only` | 全面模仿（样例窗 + 文风卡 + 声音 + 红线）/ 只用原文样例（对照）/ 只用文风卡与声音、不发原文；`segments_only` 的书一律压成 `card_only` |
| `sample_windows` | 0–16，默认 12 | 每场的样例窗数 |
| `dimension_states` | 各维 `emphasize` / `normal`（默认）/ `exclude` | 重点：排卡首、多带一句、选窗偏向它、读数权重 ×2、优先修改；不学：从卡、读数、修改里去掉 |
| `draft_mode` | `style_first`（默认）/ `neutral_first` | 作者手笔直起 / 先中性后润色（阅读对照组）；冻结进运行时契约 |

旧绑定由迁移 `20260924_0092` 一次性回填成 v3 键（策略 A → `card_only`，其余 → `full`；`intensity` i → `round(3 + 9·i/100)` 窗，
没有就 12；`draft_mode` 不回填，缺键时仍在契约构建期取 yaml 默认）；代码里不再有旧映射。

## 4. 作业

### 4.1 作业表与工人（`style_reference/jobs.py`）

- queued → running → succeeded / failed / cancelled。认领时 `attempt` +1、换新 `owner_token`；之后的每一次写（心跳、进度、游标、结束）
  都以「owner_token 仍是我、state 仍是 running」为条件——被清扫重排、取消、删书的作业，旧工人的写全部落空，自然停下。
- 心跳 15 s，超过 60 s 算过期。FastAPI lifespan 启动常驻清扫线程（`start_job_sweeper`：启动时一次，之后每 30 s），把过期的 running
  放回 queued、派发所有 queued 到有界线程池（分类 / 学习 2 个工人；对照检查单独一条车道 2 个工人，不在长作业后面排队）；重复派发无害。
  同一条线程启动时与之后每 24 小时跑两项维护：清 90 天前的审计遥测行、作业表保留期。
  处理器与维护任务由 `workers.install_workers()` 显式登记（lifespan 在启动清扫线程之前调用；不经应用直接跑作业的工具与测试也先调它）。
- **进程退出不算失败**：lifespan 结束（`--reload`、停服）时 `shutdown_job_workers` 把「工人代」+1，在跑的处理器在下一个检查点
  （两秒内）抛 `JobInterrupted`，作业放回 queued（游标、attempt 保留），下次启动的清扫接着跑；Ctrl-C 同样放回队列。LLM 调用跑在守护线程里
  （`DaemonCallPool`），退出时不等在飞的网络请求（结果丢弃，续跑时重发那一两批；记账预留按 TTL 回收）。被 SIGKILL 的进程什么也做不了：
  作业留在 running，心跳过期后由清扫放回队列。
- **互斥**：同一本书的分类与学习互斥，各自也只能有一个活动作业。建作业「先插入、再查」——INSERT 已拿到 SQLite 的写锁，几乎同时的两个请求
  在这里串行化，后到的一定看得见先到的（`409 …_ALREADY_ACTIVE / …_BOOK_LEARNING / …_BOOK_CLASSIFYING`）；续跑放回队列之后同样复查；
  破坏式重分类先写书行拿锁再查、再清派生数据。
- 取消（三种作业都可以：`…/classification/cancel`、`…/learn/cancel`、`…/checks/{job_id}/cancel`）：排队中或心跳过期的作业在请求里直接收尾，
  运行中的在下一个检查点收尾；工人死前被要求取消的作业由清扫直接收尾。框架里收尾的取消都跑
  这类作业登记的收尾钩子（分类：书的状态；学习：run 行）。放回队列是条件写：已经成功的作业不会被「继续」拉回 queued。
- `GET /api/v2/style-reference/activity` 只列作业表条目（`job:<id>`：**全部**在跑 / 排队的 + 十分钟内结束的至多 100 条）；界面把它当完整清单，
  清单里不再有的在跑条目会被收掉。各个建作业的响应都带 `job_id`。

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
   日常词 / 范畴词表里，`protected_terms.parse_protected_terms`）→ 写成 `protected_auto` 禁用词（作者录入的行不动；作者在画像里删掉过的
   自动专名记在 `profile_json.protected_terms_dismissed`，重新学习不再加回来、也不再拿它滤卡片）；
6. `tags` 给还没有 v2 标签（或标签版本变了）的窗口打场面 / 情绪 / **维度**标签（`style_ref_tag_windows` v2，每批 ≤8 窗；词表 `tags.py`，
   维度 = 这一窗最能示范的 ≤3 个维度键，不再依赖文风卡的手法）；全书已打过就跳过（重新学习约 6 次调用），`{"retag": true}` 强制重打；
7. `finalize` 专名与原文重合过滤卡片、沿用 ✓ / ✗、写画像，一个事务；卡片被滤空则作业失败，在用的画像不动。
   同一个事务里写窗口的示范证据（见上）与画像的 `line_evidence`（每句的引文落在哪几章，`learn_evidence.line_coverage`）。

**两种学习方式**（2026-10-19，`POST …/learn {"mode": "quick" | "full"}`，缺省 `quick`；界面缺省选通读全书）：

- **快速学习**（`quick`）就是上面七步。第 2 步的种子是书的校验和，同一本书每次读**同一组**窗口——重学只是让模型对同一份
  材料再答一遍；它的用处是换模型 / 改提示词之后拿来对比（材料不变才比得出差别）。
- **通读全书**（`full`）：
  - `select` 改为按整章切批（`learn_select.plan_full_batches`）：相连的整章装进一批（≤4.8 万字），超长的章按窗口边界切成
    几部分；章与章之间的零碎段归前一章，全书从第一个窗口读到最后一个窗口；每批按真实载荷核过抽取节点的预算，装不下把
    上限乘 0.85 重切。按整章切是为了叙事层（节奏的起落、插叙的进出、信息什么时候放出来要看整章）；语言 / 场景层在哪种
    切法下都看得到，主题层要的是覆盖全书——各学习目标共用这一套切法，不再按层分窗。
  - `extract` 逐批、逐层各一次调用（载荷带 `scope`：第几批 / 共几批 / 哪几章；最多 3 个并行），每份读完即落库、记游标；
    调用失败退避重试（3 次），某一维在这一批里空着不重问（别的批会补上）。实库《龙族》：49 批 × 4 层 = 196 次。
  - `consolidate`（只有通读才有，`learn_consolidate`）：每维一次调用（模板 `style_ref_consolidate_findings`，走
    `style_ref_synthesize_profile` 节点的路由，不另设节点）把各批的发现归并成组；组里的编号必须是这一维、同一类的发现，
    一条只进一组；一组的**支持** = 它的发现出自几个不同的批，每维按支持数留观察 ≤5、避免 ≤2，落成 `status="consolidated"`
    的发现（抽取行 `purpose="consolidate"`），证据指向成员已逐字核对过的引文（优先取自不同的批），置信度按支持占比
    （≥三成批 high、≥一成 medium）。
  - `synthesize` 读归并后的发现，每条带 `seen_in` / `of`（全书几批里见到过）：见得越多越是常态，`mandatory` 从这里挑。
  - 读完、归并完才写画像；`learned_from.extraction` 记 `mode` / 批数 / 章数 / 字数 / 是否沿用。
  - **顺带学的结构**（`learn_structure`）：叙事层那次调用同时交这一批的 `scene_starts`（新的一场从哪一段开始、靠时间 /
    地点 / 视点 / 事件哪种转换；每章第一段不算）与 `chapters`（每个整章片段：这一章的功能、章末怎么收、交织几条线、
    几个视点；只收类别与数字，不收人名情节）。逐份记进游标、读完记进 run 行（`coverage_json["full"]["structure"]`，
    沿用通读时一起沿用）。结构画像据此（书本身没有空行分场时）改用量出来的场：`scene_break_style="learned"`、每章场数
    与场长分布、`scene_shift_distribution`——「一场多长」（`structure_card.reference_scene_scale`）与分章的参考场数
    （`snowflake_chaptering.scale`）按 `scene_sizes_measured` 用它；各章章法汇成 `chapter_craft`（功能与章末收法的占比、
    每章几条线 / 几个视点、高潮章之间隔几章），渲染进 `[结构画像]` 块（规划、文风卡合成、画像页都看得到）。
  - **伏笔与回收**（`foreshadow` 步，排在归并之后、写文风卡之前，`learn_foreshadow`）：叙事层那次调用另交这一批的
    `plants`（埋下了什么：物件 / 台词 / 人物细节 / 悬念提问 / 预言暗示 / 场景细节 / 反常举动 + 一句梗概）与 `payoffs`
    （回收了前文的什么：揭晓真相 / 反转 / 物件再现 / 台词呼应 / 人物兑现 / 情感回收 + 梗概 + 它回收的是什么样的铺垫），
    随结构一起记进游标与 run 行。读完后全书的埋 P1…与收 R1…交给模型配对（模板 `style_ref_foreshadow_match`，装不下
    预算就把「收」分几次送；每处收至多配一处埋、埋不晚于收），配对按「通读身份 + 模板 + 模型」记进 run 行，沿用通读时
    一起沿用。由配对确定性地算 `structure_card.foreshadowing`（埋了多少、几成回收、从埋到收隔几章、同章就收 / 隔十章
    以上的占比、埋与收的类别占比、每章末同时悬着几处），渲染进 `[结构画像]`；再一次调用（`style_ref_foreshadow_craft`，
    ≤60 对、近收远收均匀取）写 ≤4 句「怎么埋、怎么收」，在上一版上修订（`keep`，原句存 `foreshadow_lines`），定稿时
    过文风卡同一道过滤，至多 3 句以「伏笔：」开头进 `planning_guidance`（规划手法共 10 句，合成的句从后往前让位）。
    情节梗概只在载荷里给模型配对 / 归纳，不进画像与任何写作 / 规划提示。
  - **按场面的写法**（`situations` 步，排在 `tags` 之后，`learn_situations`）：窗口的场面标签 × 各批原始发现的引文——
    一条发现落在某类场面窗口里的引文占比，比这类场面占全书窗口的比例高出 1.5 倍以上、且至少 2 处，就算这类场面的
    特别做法；至少 8 窗的场面才做，每类按「集中度 × 条数」送 ≤40 条，一次调用（模板 `style_ref_situation_lines`，走
    `style_ref_synthesize_profile` 节点的路由）写 2–4 句，不重复文风卡已有的、在上一版上修订（`keep`）；没有依据 /
    带统计数字 / 超长的丢，定稿时过文风卡同一道过滤（本书专名、作者禁用词、≥12 字原文重合），写进画像
    `situation_card`。起草时（`inject.blocks.build_card_source`）这一场的场面标签（蓝图给的，或由场景设计推，与选窗
    同一口径）对上了，至多 2 类、每类 3 句放进文风卡块的「这一场的场面」一节——与近期常见偏差同一档，排在各维第一句
    之前（`card.plan_card_block`，拟合单元 `__situations__`）。
  - **沿用**：一次通读的身份（段落根哈希、窗口索引版本、切法版本与上限、切出来的区间、读了哪几层、各层抽取模板版本与
    模型）记在血缘 run 行的 `coverage_json["full"]`；下一次通读身份相同就不再读书，归并身份（加上归并模板版本与模型）
    也相同就连归并一起沿用，只重写文风卡。`{"reread": true}` 从头再读。`GET …/learn` 的 `estimate.full`（批数、调用数、
    `reusable`）按窗口字数粗切、按身份里除切法外的部分预告能不能沿用，作业开工时按完整身份再判。

**在上一版上修订**（两种方式都是）：合成的载荷带上一版文风卡（`previous_card`，每句带 id；作者 ✗ 掉的单列
`rejected_lines`）。模型认定新的发现仍支持上一版某一句时在 `keep` 里填它的 id，`learn_card.assemble_card` 就用上一版的
原句与原 id（`source="kept"`）——没有新证据就不改措辞，句子的 id（内容哈希）不变，作者的 ✓ / ✗ 跟着句子走；`keep` 指向
✗ 掉的句子或新写的句子与 ✗ 掉的一字不差 → 丢（`excluded_by_author`）。结果里 `card.kept_from_previous` 是沿用的句数。

建作业时就拒：书未就绪 `STYLE_REFERENCE_BOOK_NOT_READY`、正在分类 `…_BOOK_CLASSIFYING`、已在学 `…_LEARN_ALREADY_ACTIVE`、学习节点没有
路由或模板是旧版 `…_LEARN_CONFIG_MISSING`、正文太短 `…_INPUT_TOO_SMALL`（`{"force": true}` 仍可学）（均 409）。学习在跑时重新分类一律
409 `STYLE_REFERENCE_BOOK_LEARNING`。失败的作业只有 `error.retryable` 不为 false 时才可「继续学习」（`learn.resumable`）；
正文太短这类失败只能「重新学习」或「仍然学习」。

### 4.4 对照检查（`check_job.py`，kind=check）

`POST /api/v2/style-reference/checks`：`text`（≤6 万字）与 `scene_id` 恰好给一个，并说明对照哪份参考（`profile_id` 或 `project_id`）。
给 `scene_id` 时作品一律取这一场所属的作品（建作业与跑作业时都按场景取，不信客户端给的 `project_id`），读数记在那部作品下。
一场「现在的正文」= 写作台上看到的那份（`check_job.scene_current_text`）：有字的作者稿 → 当前终稿 → 最近的有效稿 / 风格稿 / 首稿
（2026-10-20 之前终稿优先：作者在写作台改过之后，检查的仍是改前的终稿，改法也钉不到写作台的段落上）。
作业做三件事：确定性读数 → 参考评审（模板 `style_reference_judge`，与起草流程里的软 QC 参考评审同一份、提示按字相同，走 `soft_qc` 节点
路由；冻结选窗前 4 窗 + 文风卡 + 声音 + 红线；16 维各 0–10 分，按模板声明的刻度换算、越界的分丢掉；**同一份提示并发问
`judge_samples` 次（缺省 3）、每维取中位**，§7a；总分由代码算，按画像的评审校准折算）→ 抄袭门（只记计数），写一条 `source=manual_check`
的读数（`judge_json.samples` 记问了几次，每维 `spread` 记几次原始分的最高减最低；`reading_json.visible_sha256` 记可见文字的哈希）。
几次评审全失败作业才失败（不降级成只有读数）；有失败的就用答成的那几次。
检查结果在三处是同一份：风格页的结果先列「要改的几维」与评审的改法（查的是作品的一场时给「去写作台按这些改」）；起草台证据栏同样；
写作台深改把它并进场景诊断（来源「风格对照」，`docs/scene-diagnosis-2026-09-22.md` §9），可按改法改写、可忽略。`POST /checks/{job_id}/cancel` 取消（排队中立即收尾，运行中在评审回来的检查点收尾；已结束 → 409 `…_CHECK_NOT_ACTIVE`）。

- 评审的参考块按 `soft_qc` 的**实际路由**判云策略（§11）：「仅本机」的书遇云端路由 → 作业以 409 `STYLE_REFERENCE_CLOUD_POLICY_BLOCKED`
  失败，参考一个字都不发；并按评审模板的输入预算压（与 soft_qc 同一档，`NOVEL_SYSTEM_SCENE_INPUT_TOKEN_BUDGET` 收紧时照收紧；待查文字
  长到一窗参考都装不下 → 409 `…_CHECK_REFERENCE_EMPTY`，`details.reason = "input_budget"`）。
- 所有权：每个进度写之后立刻提交（不带着写锁去建窗口索引、跑抄袭门）；评审回来先确认作业还是自己的（没被取消、没被清扫重排给别的
  工人、书没被删）再记读数；进度写或「完成」写落空 → `JobLost`，读数随事务一起回滚。

### 4.5 校准评审（`judge_calibration.py`，kind=calibrate）

评审给「像不像」打的分本身没有零点（实库 2026-10-13：同一份稿子流程内评审 8.6、对照检查 2.7）。校准给它一个参照：同一个评审模型、
同一份评审口径的参考块，给两类文字打分——这本书里 N 段**不在评审样例里**的原文（作者本人写的），以及同一段原文让起草模型改写成通用
现代中文叙述的「普通改写」（模板 `style_reference_neutralize`，走 `style_draft` 路由——要比的正是起草模型不带参考时的默认写法；改写稿
只用来校准，不落库、不进正文）。

- 一次 = N 次改写 + 2N 段文字各评审 `judge_samples` 次取中位（`judge_calibration_anchors`，缺省 N = 3、3 次，共 21 次调用）；一段文字
  的几次评审并发，段与段顺序跑，每段之后一个检查点。改写提示写明
  原文字数与下限（八成）；改写稿短于原文四成（模型把这一段压缩了）就带着「上一次写短了」重试一次，两次都短才失败；走对照检查那条
  短车道。锚点：评审样例之外、≥600 字、典型度高、一章至多一段，按画像版本定种；截到约 2400 字（按段）。
- 结果存在画像 `profile_json.judge_calibration`（不进冻结契约）：两类的平均分与差、每一维的两个锚点、分得清的维、评审的「身份」
  （soft_qc 路由的 provider / model、评审模板版本、画像版本）。两类平均分差 ≥ `judge_min_separation`（2）且至少一维差
  ≥ `judge_min_dimension_separation`（1）→ 可用，否则「评审不可用」。
- 口径（`judge.calibration_state`）：已校准 / 不可用 / 没校准（missing）/ 过期（stale：身份任何一项变了——换了评审模型、改了评审
  模板、重新学过画像）。过期与没校准都按模型原始分走（界面标「未校准」）。
- 学习文风成功后自动排一个（`start_after_learn`；没有模型 / 节点没配好 / 云策略不许 / 已在校准 → 不排，记日志）；画像页「评审校准」卡
  可手动启动 / 取消：`POST /api/v2/style-reference/profiles/{id}/judge-calibration`、`…/judge-calibration/cancel`；结果随
  `GET /profiles/{id}` 的 `judge_calibration` 给出。失败不续跑（重新校准从头来）。错误码 `STYLE_REFERENCE_CALIBRATION_*`
  （`CONFIG_MISSING` / `BOOK_NOT_READY` / `ALREADY_ACTIVE` / `NOT_ACTIVE` / `NO_REFERENCE` / `NO_WINDOWS` / `CALL_FAILED`）。

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
倾向 + 窗数 + 维度状态。配额（k=12，其它 k 按比例）：章首 / 章末位置 ≤3、场面标签 ≈4、重点维 / 改稿维 / 近期偏差维的**维度示范** ≈2
（按窗口的维度标签重合挑），
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
   `reading_failed`、提示说实话；读数在保存点里读，出错不弄坏会话、不耽误检查点。读数说修改稿更近时，再做一次**成对比较**
   （§7a；执行步键 `style_draft:0:compare:ab` / `…:ba`）：评审两次都说首稿更像 → 仍保留首稿（`pairwise_first_draft_closer`）。
5. 软 QC = **参考评审**（统一评审模板 `style_reference_judge`，不带场景上下文；样例 4 窗 + 文风卡；16 维各 0–10 分，按校准折算，§7a）→
   **放行由代码定**：作数的维里低于阈值的（重点维在前，至多 4 维）各出一条补丁说明（评审给这一维的改法），没有就放行；评审不可用时
   不据它补丁（waive 并留说明）。评审判出了不像的维时，补丁是**按维整场修改**（`style_first.dimension_patch_plan`，2026-10-14 第 2 项：
   这几维的评审分、说明与改法、读数在这几维的差异、文风卡句，样例窗换成示范这几维的窗，正常温度；已经像作者的句子原样留下）——口吻、
   气质、对白写法这类贯穿全篇的差距，「只动点名的句子」的局部补丁改不动；只有确定性问题时照旧局部补丁、低温（0.3）。补丁尝试记
   `patch_mode`（`dimension_revision` / `local_patch`），去留记录带 `revised_dimensions`。补丁的去留问**成对比较**（补丁前 vs 补丁稿，执行步键 `style_patch_keep:compare:ab` / `…:ba`，在补丁的
   检查点之后、`soft_qc:1` 之前）：两次都说补丁前更像 → 退回（`pairwise_original_closer`）；都说补丁更像 → 留下；没有明确结论 →
   `distance` 变大超过 `patch_max_distance_increase` 才退回（`distance_worse`）。退回时 `STYLE_PATCH_REVERTED`。
6. 准定稿评审（按参考判，分数带范围）。评审要改写时，定稿改写稿进第二轮评审之前先过两道确定性门：相对来源稿的基础安全回退
   （必写事实、禁用内容、文本完整性——含「整场挤成一段」、长度；`scene_generation.assess_rewrite_regressions`，只算改写稿新添的问题），
   以及作者手笔直起时读数比来源稿远出 `patch_max_distance_increase` 以上。没过就丢掉改写稿，终稿保留来源稿，评审意见随稿留痕
   （skip_reason `rewrite_rejected_base_safety` / `rewrite_rejected_moved_away`，前者 Q2、后者 Q3 提示）。
7. 归档：唯一抄袭门 + 终稿读数。

房风规则只在 `policy.defers_house_taste()` 时让位（反模板门只提示、成稿门的文学阈值按绑定书校准、新鲜度只留逐字
n-gram、长度带放宽）；事实、必含、禁止、抄袭、禁用词这些硬门从不让位。`neutral_first` 是对照组：中性首稿再由 `style_draft` 改成作者手笔，
有绑定时改完读一次读数，不比中性稿更像就交付中性稿（与直起同一条「永不越改越远」规则）；旧的 21 指标包络（段密度合并、形态修复、
改写不退步）已删除，改写不退步只看读数（`patch_max_distance_increase`）。
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

## 7a. 参考评审：一份评审、校准、代码定放行、成对比较（2026-10-14）

`style_reference/judge.py`（纯函数）+ `judge_calibration.py`（调用与作业）：

- **一份模板**：`style_reference_judge`，起草流程的软 QC（有绑定且参考带上了；带不上 → 退回 `soft_qc` 模板的润色口径）与对照检查共用，
  走 `soft_qc` 路由，提示由 `judge.bare_prompt` 拼（只有评审口径的参考块 + 待评文字，不带场景设计）。同一份文字在两处拿到的提示**按字
  相同**：system 都由 `judge.review_system_prompt` 收尾（参考块 + 模板 + 不可信数据约束），结构约束都是 `judge.judge_response_schema`
  （测试按字核对）。2026-10-20 之前起草流程少了那一句约束，两边差 186 个字，而校准量的是作业侧那一份。
  16 维必填（`score` / `applicable` / `note` / `fix` / `anchor`），模型不给总分、不给放行结论；只判「怎么写」，故事本身（题材、事件、人物）不判。
  `anchor`（模板 `2026-10-20.v1`）：改法落在哪里，从待评文字里逐字抄 4–20 字——改法常只说「在某某出场时」，写作台钉不到段落；
  合成几次评审时优先取给了锚点的那一次，补丁说明与按维修改的提示都带上「位置：「…」」，写作台深改的风格对照先按它钉段。
  模板版本变了，评审校准随之过期（身份里有模板版本），要在画像页重新校准一次（约 21 次调用）。
- **折算**：校准可用时，每一维按它的两个锚点折成 0–10（普通改写 = 0，作者原文 = 10，截在两端）；分不清的维不作数（分为空，只留原始分）。
  没校准 / 过期 → 作数的分就是原始分。评审不可用 → 没有作数的分。总分 = 作数各维的平均；「不学」的维整维不出现。
- **放行线**：校准后的分低于 `judge_pass_score`（6）、或没校准时原始分低于 `judge_raw_pass_score`（7）的维要补。
- **几次取中位**（2026-10-20，`judge.combine_judge_samples` + `judge_calibration.call_judge_panel`）：单问一次的评审噪声很大——实库
  CH01_SC10 终稿同一份正文、同一份提示问了 11 次，折算后的总分在 6.0–9.0 之间跳（标准差约 1 分），单维 0.6–1.1 分；起草时给 9.0、
  第二天检查给 6.0 的「两把尺子」就是这么来的，不是提示不同。作者看的评审（对照检查）与校准锚点各问 `judge_samples` 次（缺省 3，
  `injection_budget.yaml`，1–5；并发，每次一条守护线程），每一维取原始分的中位数，说明与改法取分数离中位数 1 分以内、优先引了原文（「…」，钉得到写作台的段落）的那一次，一维算「有证据」
  要至少一半的回答这么说。起草流程里的软 QC 仍问一次（它的放行有成对比较兜底；管线的节点调度器不能并发），界面照实标「起草时评审一次」。
- **成对比较**（模板 `style_reference_compare`，同一套参考块）：「两个版本哪一个更像」，A / B 两个次序各问一次，两次指向同一边才算数
  （`judge.compare_verdict`：base / candidate / tie / unavailable）。评审不可用或参考带不上时不比（unavailable），去留退回读数规则。
- **落库**：评审记录（`judge.judge_record`：`overall` / `dimensions.*.score` 是作数的分，`raw_overall` / `dimensions.*.raw` 是模型原始分，
  `calibration.state` 说口径；旧读者认的 `style_score` / `dimension_scores` 一并给出）进软 QC 报告的 `reference_judge` 条目、尝试记录与读数的
  `judge_json`。作品汇总的按维评审平均只数同一种口径（有校准后的就只数它们，`judge_scale`）。界面在每处评审分旁标口径
  （已校准 / 未校准 / 校准已过期 / 评审不可用）。

## 7b. 风格棘轮：改配置之前先验收（2026-10-18）

`style_reference/ratchet.py` + 运维工具 `python -m novel_system.tools.style_ratchet`（默认干跑，`--execute` 才调用模型）。思路借自
QMAI 的文风自进化：拿作者写过、模型没见过的一段原文，只把「发生什么」交给模型，看两套配置谁写得离原文更近——内容固定，差别只剩写法。
起因是实库读数：首稿之后的风格阶段 5 场里 3 场让测量核距离变大，三把尺子（分位 / 软 QC / 对照检查）又互相不一致，改模板、换模型、
减样例之前没有可复现的验收办法。

- **留出段**：书的窗口里挑 N 段（缺省 4，`judge_calibration.choose_anchor_windows`，种子不同于校准）。渲染样例时整窗跳过
  （`StyleRenderRequest.exclude_windows`，只在没有场景的渲染里用，不进冻结选窗；进渲染缓存键）；文风卡引文、声音特征、别的样例窗若覆盖
  留出段一成以上的字（抄袭门同一把 12 字尺子，`reference_copy_gate.shared_runs`），这一段换掉。
- **情节要点**：模板 `style_ratchet_plot_points`（`style_draft` 路由）只说发生什么、不带原文措辞、专名全换新；与原文有一处连续 ≥12 字
  相同就带着原因重试一次，仍不行这一段不比。
- **起草**：真实的 `style_first_draft` 模板 + 真实的样例渲染，场景卡 / 结构段换成要点，字数给成原段的 ±15%。配置项：`model`（同一服务
  换模型）、`windows`（0–16 窗）、`placement`（`user_tail` 现在的做法 / `system` 样例进前缀、吃提示缓存的做法）、`prompts`（另一份
  prompts.yaml）、`rep`（同一配置的第几份；不给 `--b` = A/A，量噪声）。
- **量与判**：距原文 = 测量核各特征与原段之差 ÷ 作者窗口分布的尺度（|z| 封顶 6），按读数同一套维权重平均；另附对全书分布的分位作参照。
  B 每一段都更近、且平均近 ε 以上 → B 更像；反之 A；其余分不出。ε 取给定值（缺省 0.2）与 **A/A 实测噪声**（不给 `--b` = 同一配置写两遍，
  每段之差的平均记进 `<out_dir>/noise.json`，按去掉「第几份」的配置签名分开记）中较大的那个。实库第一次 A/A（2026-10-18，flash、12 窗、
  2 段）每段差 0.18–0.24：噪声比直觉大得多，先跑 A/A 再比别的，段数宁多勿少。字数偏离原段三成以上的稿子在报告里带 *。这只是表面统计：
  另出盲读页 `reading.md`（原文 / A / B 打乱成甲乙丙，答案在 `key.json`），像不像由作者读了算。
- **文件**：`.codex-run/style-ratchet/<套件>/`：`suite.json`、`passages/`（原文 + 要点）、`drafts/<配置签名>/`（同一配置写过的稿子换对手时
  复用；签名含模型、样例窗数与位置、模板版本与正文哈希、画像版本、书的根哈希）、`runs/<这一次>/`（`report.md`、`reading.md`、`key.json`、
  `result.json`）。内含参考书原文，只留在本机。调用照常记账（`scope_type = style_reference_ratchet`）；账本摘要里有供应商报的缓存读
  token（`cached_input_tokens`，`llm_providers/usage.usage_cache_tokens`）时报告一并给出。实库那条中转报这个数，但同一份 4.5 万 token
  的提示两分钟内连发两次，缓存读都是 0——经它没有提示缓存的收益，「样例挪进前缀吃缓存」暂不做。
- **成本**：一次比较 ≈ N 次要点 + 2N 次起草（每次起草带一整份样例，现库约 4.6 万字）；缓存里有的不再调用。

**界面「配置对比」（2026-10-18，`ratchet_job.py`，作业表 kind=ratchet）**：风格参考页随时可用的第五个标签，同一套比较做成后台作业（走对照检查
那条短车道，同一本书同一时间一个，与分类互斥；进程退出放回队列，重跑时缓存里有的要点与稿子不再调用模型；失败 / 取消不续跑，重开一次即可）。
与命令行共用 `.codex-run/style-ratchet/`、同一个种子 `ratchet`，缓存与噪声记录互通；界面不能指定提示词文件。

- `GET /profiles/{id}/ratchet`：能不能比（不能就给原因）、现在的配置（风格稿路由的模型、绑定的样例窗数、位置）、同一服务上配好的模型、噪声记录、
  在跑的、历史（最近 30 次）；`POST …/ratchet/preview`：只读预估（留出段、哪些在缓存、要调几次模型、多少字输入），不建作业；
  `POST …/ratchet` / `…/ratchet/cancel`：开跑 / 取消（`{a, b, passages}`，配置项 `model` / `windows` / `placement` / `rep`，不给 = 现在的配置；
  B = A 加 `rep: 2` 就是 A/A）。
- **先读后看**：结果写进作业行（结论与逐段数字、盲读三份文字与答案、作者的选择、`revealed`）。揭晓之前 `GET /ratchet-runs/{job}` 不给答案、
  不给结论与逐段数字（逐段字数就能对出哪份是哪份），历史与活动条目也不带结论；`PUT …/votes`（每段 `{best, worst}` 甲乙丙，`reveal`）
  存选择、揭晓；揭晓之后选择不再能改（409）。揭晓后另给作者读出来的结果（每段 A / B 谁排得前、原文被认作最像几次），与数字相反时以读的为准。
- 配置对比作业不进保留期清理：它就是对比的历史记录。

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

绑定的书已不在书库、绑定指向的画像不是 active（`STYLE_REFERENCE_PROFILE_NOT_ACTIVE`）、或风格策略解析降级：那一边**没有查成**（`unavailable`），不能当成「查过、没重合」——风格稿门报
`unavailable`（软 QC 挂 Q2 复核、起草链路发 `STYLE_GATE_UNAVAILABLE`），成稿门报不拦的警告 `source_safety:unavailable`。
检查本身出错（库读不出）才 fail-closed `SOURCE_SAFETY_UNAVAILABLE`。

## 9. 接口

前缀 `/api/v2/style-reference`，包 `api/routes/style_reference/`（写操作都要 `X-Idempotency-Key`）：

| 模块 | 接口 |
|---|---|
| `books.py` | `POST /books/import-upload`（≤10 MB）· `POST /books/import-path`（要配 `NOVEL_SYSTEM_STYLE_REFERENCE_IMPORT_ROOTS`，还要 `X-Admin-Token`；没配 `NOVEL_SYSTEM_ADMIN_TOKEN` 时只放行回环客户端）· `GET /runtime`（有没有模型、分类节点是否本机、推荐的 `default_cloud_policy`）· `GET /books` · `GET /books/{id}` · `GET /books/{id}/paragraphs?start=&end=`（≤80 段）· `GET /books/{id}/classification/estimate` · `POST /books/{id}/reclassify`（缺省破坏式；`{"mode":"retype"}` 就地；`{"resume":true}` 续跑）· `POST /books/{id}/classification/cancel` · `DELETE /books/{id}` · `POST /books/bulk-delete` |
| `learn.py` | `POST /books/{id}/learn`（`{"mode":"full"}` 通读全书、缺省快速学习；`{"reread":true}` 通读时不沿用上一次；`{"resume":true}` 续跑；`{"force":true}` 正文太短仍学；`{"retag":true}` 重打窗口标签；`profile_id` 指定就地更新哪份）· `GET /books/{id}/learn`（`learn` + `estimate`（快速学习的；`full` 是通读全书的批数 / 调用数 / 能否沿用；含 `windows_to_tag`；`?retag=true` 按全书重打估）+ 各学习节点的 `routes`）· `POST /books/{id}/learn/cancel` |
| `profiles.py` | `GET /profiles/{id}/card-lines/{line_id}/examples`（一句——文风卡句或场面句——在全书的全部例子：引文按原书顺序、带段落序号与章，另给按章计数；`?limit=` 1–2000，缺省 400；不在这份画像上 404 `STYLE_REFERENCE_CARD_LINE_NOT_FOUND`）· `GET /profiles/{id}`（文风画像页，每句带 `coverage`，另有 `book_chapters` 与 `situations`；含 `judge_calibration` 校准卡；画像摘要在书库载荷里）· `POST /profiles/{id}/judge-calibration` · `POST /profiles/{id}/judge-calibration/cancel`（§4.5）· `POST /profiles/{id}/card-lines/{line_id}`（✓ / ✗）· `GET/POST /profiles/{id}/banned-terms` · `DELETE /banned-terms/{id}` · `POST /profiles/{id}/injection-preview`（本场预览，只读） |
| `bindings.py` | `POST /profiles/{id}/apply {scope, scope_ref_id, config}`（同一目标只留一条生效绑定，旧的在 `replaced` 里说出来）· `PATCH /bindings/{id}`（维度状态按维合并）· `DELETE /bindings/{id}` · `GET /profiles/{id}/bindings` · `GET /projects/{id}/style-binding` |
| `checks.py` | `POST /checks` · `GET /checks/{job_id}` · `POST /checks/{job_id}/cancel`（§4.4、§7） |
| `activity.py` | `GET /activity` |

`api/routes/style_fidelity.py`：两个 `style-fidelity` 读接口（`/api/v1/scenes/{id}/style-fidelity`、`/api/v1/projects/{id}/style-fidelity`）。
已删除、不要加回来（`backend/tests/test_retired_surface.py` 钉着）：`/imports/{key}/progress`、旧抽取 run 与 `/runs/{id}/synthesize`、
`/books/{id}/runs`、`/runs/{id}/findings`、`GET /profiles`、`/injection/layers`、`/readings/{id}`、示例预览 `/profiles/{id}/preview`、回测
`/profiles/{id}/validate` 与 `/reports`、`/bindings/{id}/injection-preview`、`/injection/task-defaults`、👍/👎。

## 10. 配置

`config/style_reference/`：

| 文件 | 用途 |
|---|---|
| `injection_budget.yaml` | 单窗上限 5000、文风卡预算 2600、`draft_mode_default`、直起长度放宽 0.5、参考场长上限 5000、上一场尾部节选 900、`fidelity:` 阈值 |
| `function_words.yaml` | 测量核与专名候选共用的闭类虚词表 |
| `voice_baseline.yaml` | 黄金语料分位基线，现在只判「刻意复沓」；重建：`python -m novel_system.tools.build_voice_baseline build-baseline`（`inspect PATH` 查看） |
| `input_thresholds.yaml` | 每层抽取的语料量门槛 |
| `banned_adjectives.yaml` | 空泛评价词：抽取陈述里出现就丢掉那条发现 |
| `anti_plagiarism_template.txt` | 红线段 |

`fidelity:` 阈值（`style_step.fidelity_thresholds`；2026-09-24 用作者的真实路由在 3 场 × 3 臂上核过，见变更记录 §7）：`style_step_max_percentile` 90、
`revision_min_improvement` 0.03、`patch_max_distance_increase` 0.05；评审（2026-10-14，§7a）：`judge_pass_score` 6、`judge_raw_pass_score` 7、
`judge_min_separation` 2、`judge_min_dimension_separation` 1、`judge_calibration_anchors` 3。旧的 `judge_tolerance`（两次绝对评审分相减的容差）
已退役——补丁的去留改由成对比较定。评审节点的分数按模板 `structured_schema` 声明的刻度（`maximum`）逐个换算，越界的分丢掉。

模型节点（默认值只写在 `llm_node_registry.py` 的 spec 里）：分类两节点关推理、输出 8192；四个抽取节点与文风卡合成 16384；
`style_ref_protected_terms` 8192；`style_ref_tag_windows` 4096、关推理。起草与评审复用现有路由：`style_first_draft` / `style_targeted_revision`
走 `style_draft`，参考评审 `style_reference_judge` 与成对比较 `style_reference_compare` 走 `soft_qc`（5000），校准的普通改写
`style_reference_neutralize` 走 `style_draft`，`scene_blueprint_facts` 走 `scene_blueprint`。

## 11. 部署与运维

- **提示词**：只读仓库的 `config/prompts.yaml`（2026-10 起系统配置不再有提示词快照，部署不用另做什么）。学习作业解析输出要用的
  模板字段由 `tests/test_style_reference_learn_units.py` 的模板契约守着，开工时不再逐个查。
- **新节点要路由**：保存过 models 快照的安装上，快照里没有路由的节点 fail-closed（`LLM_ROUTE_NOT_CONFIGURED`）；到系统配置「一键补齐」。
- **输出预算**：models 快照只存作者为每个节点选的服务与模型（迁移 `20260929_0096` 把旧快照里抄进去的参数都去掉了），温度、输出预算、
  推理档位解析时取 spec——spec 里修好的默认值随发布直接生效。要高于默认值（例如某个中转的思考 token 特别多）时用
  `python -m novel_system.tools.raise_llm_output_budget --node <id> --floor <n> --execute` 写成快照里的显式覆盖；分类节点的推理在 spec 里已关。
- **云策略**：`local_only` 的书只有这一步**实际调用的节点路由**是本机模型（`ollama` 或回环地址）时才放行，否则 409
  `STYLE_REFERENCE_CLOUD_POLICY_BLOCKED`；`segments_only` 的书可被云端模型读来分类 / 学习，起草只送文风卡。
  - 参考进提示（起草、改稿、评审、规划、本场预览、对照检查）同样按**接收这份提示的节点**判，与全局运行时模型无关——全局是本机而起草节点
    走云端 → 不送；全局是云端而起草节点走本机 → 照送（`policy.decide_reference_route`）。「仅本机」的书遇云端节点时**一个字都不送**
    （没有样例、文风卡、声音、专名表），注入适配器原样抛 409，不降级成没有参考的提示去照样调用那个节点。起草管线（首稿、定向修改、
    风格稿与补丁）因此停下并带 `author_action`；包着适配器的各处（软 QC、准定稿评审、写作台深评 / 补丁 / 建议、场景蓝图）同样原样抛出，
    不降级；bundle 里由这本书派生的段（叙事机制指引、参考尺度）按读 bundle 的全部节点判，不许送时不进 bundle。
  - 节点从哪来：调用方给 `inject_style_reference_prefix(..., node_id=...)`；不给就按 `prompt["template_name"]` 推
    （`inject/routing.TEMPLATE_NODE_IDS`：`style_first_draft` / `style_targeted_revision` → `style_draft`；`style_draft` 模板 → `style_draft`
    + `style_patch`（软补丁、去模板、安全修复借这份提示在 `style_patch` 下派发）；`style_length_patch` / `style_salvage_patch` →
    `style_patch`；`scene_literary_rewrite` → 它自己 + `style_patch`；`scene_blueprint_facts` → `scene_blueprint`；`style_reference_judge` /
    `style_reference_compare` → `soft_qc`；`writer_passage_review` → `writer_deep_review`；其余模板名本身就是注册节点的用它）。有几个候选节点时每一个都要满足；
    说不出节点 → 「仅本机」的书按不许送处理。
  - 规划参考块（`planning_context`）不说节点时按全部消费节点判：项目级（雪花 09 / 10、起章名、章规划四节点）、场景级（场景蓝图、
    章架构、人物压力）、起章名；「仅本机」的书有一个消费节点走云端就不给参考块（规划照常，不报错）。
  - 未知 / 空策略：本机节点只送文风卡，云端节点 409 `STYLE_REFERENCE_CLOUD_POLICY_INVALID`。
- **迁移**：停服、`python -m novel_system.tools.db_backup --backup <src.db> <dst.db>`，再 `alembic upgrade head`（风格参考相关的是 0090、0091、0092、
  0094；各版本见[数据库迁移](migrations.md)）。
  0092 之后窗口标签是 v2：之前学过的书在下一次「学习文风」时重打全书标签（『龙族』520 窗约 65 次调用），此后重新学习只剩约 6 次调用。
- **工具**（`backend/` 下，默认干跑、`--execute` 才写库，先备份）：`purge_style_reference_books --book ID`（可重复）和 / 或 `--id-prefix PREFIX`
  （至少 4 个字符；删书及全部派生数据，就是书库删除的 `cleanup.delete_reference_book`，绑定范围内的规划产物一并作废）；`refresh_style_reference_books --book ID | --all`（就绪的书剥副文本、重编号、保留场界、重算段型统计、顺带删掉旧库里的死键 `metrics` / `prose_shape_metrics`；段落变了
  会 `pop` 根哈希、窗口随之重建；空行场界只有重新导入才能恢复；有排队 / 运行中的分类或学习作业的书跳过——就地重标时书一直是 ready；
  `--execute` 时每本书先拿写锁、在锁里重新核对并重算计划，`stats_json` 只合并本工具管的键）；`build_voice_baseline build-baseline`（重建 `voice_baseline.yaml`；`inspect PATH` 查看）。

## 12. 排障

| 现象 / 错误码 | 原因与处理 |
|---|---|
| `STYLE_REFERENCE_LLM_REQUIRED`（409） | 没有可用模型；去系统配置 |
| `STYLE_REFERENCE_CLOUD_POLICY_BLOCKED`（409） | 「仅本机」的书遇到云端节点路由（分类、学习、对照检查，也包括起草 / 改稿 / 评审 / 本场预览时参考要进的那个节点；`details.node_id` 是哪个节点，`details.reason = "node_unknown"` 表示调用方没说清节点）；把那个节点换成本机模型，或换一档范围重新导入 |
| `STYLE_REFERENCE_CLOUD_POLICY_INVALID`（409） | 书的云策略认不出来，参考不能进云端节点；重新导入并选一档 |
| `…_SEND_RIGHTS_REQUIRED` / `…_SEND_RIGHTS_DECLARATION_REQUIRED`（409）/ `…_RIGHTS_DECLARATION_INVALID`（400） | 非本机范围没有确认发送权 / 声明字段不合法；重新导入并勾选 |
| `…_BOOK_DUPLICATE`（409）/ `…_BOOK_EMPTY`（400）/ `…_UPLOAD_TOO_LARGE`（413）/ `…_BOOK_FORMAT_UNSUPPORTED` | 同一份文本已在书库（`details.book_id`）/ 没有正文 / 超过 10 MB / 不是 txt、md |
| `…_CLASSIFICATION_FAILED`（502） | 某批重试后仍失败；游标保留，「继续分类」 |
| `…_ALREADY_ACTIVE` / `…_BOOK_LEARNING` / `…_BOOK_CLASSIFYING` / `…_BOOK_NOT_READY` | 同一本书有冲突的作业在跑，或分类没完成；等它结束、取消或「继续分类」 |
| `…_LEARN_CONFIG_MISSING` | 学习节点没有路由（`details.missing_routes` 列出哪几个）；「一键补齐」 |
| `…_INPUT_TOO_SMALL` / 学习失败 `card_filtered_empty` | 正文太少 / 卡句全被专名、禁用词或原文重合滤掉（在用的画像不受影响） |
| `…_PROFILE_STALE`（409） | 画像的依据变过；重新学习再用于作品 |
| `…_CHECK_NOT_BOUND` / `…_CHECK_TARGET_INVALID` / `…_CHECK_JUDGE_FAILED` / `…_CHECK_NOT_ACTIVE` | 对照检查没有可对照的参考 / `text` 与 `scene_id` 没有恰好给一个（超过 6 万字的 `text` 在请求校验就被拒）/ 评审调用失败 / 取消一个已结束的检查 |
| `…_PROFILE_NOT_ACTIVE` | 绑定指向的画像已归档（v3 之前的画像在迁移 0092 归档）；对这本书「学习文风」就地复活，或重新应用 |
| `SOURCE_SAFETY_BLOCKED`（409） | 与参考书原文连续 12 字以上相同（唯一的硬门）；按 `author_action` / `details.reference_copy` 给的位置改写 |
| 成稿门警告 `source_safety:protected_term` | 正文用了画像禁用词表里的专名——不拦；是参考书的专名就换掉，日常词被误收就到文风画像的禁用词里删掉 |
| 成稿门警告 `source_safety:unavailable` / 风格稿门 `unavailable` | 绑定的书已删、画像不是 active 或绑定解析失败，那一边没做原文重合检查；恢复绑定后可再做对照检查 |
| 提示 `STYLE_REFERENCE_BOOK_CHANGED` / `…_SAMPLES_BLOCKED` / `…_NO_WINDOWS` / `…_BOOK_MISSING` | 冻结后书被改过（按当前索引选窗）/ 样例被云策略挡下（没有发送权声明、或冻结时不许送云）/ 还没有窗口 / 书已删除（冻结快照是送云策略时文风卡照送、原文不送；快照说不清策略时什么都不送） |
| 作业「卡住」（`stalled`） | 心跳过期 60 s 后清扫线程放回队列；也可取消或继续 |

## 13. 测试

`backend/tests/test_style_reference_*.py`、`test_style_fidelity_*.py`、`test_reference_copy_gate.py`、`test_prompt_template_contracts.py`；共享造数
`tests/style_reference_factories.py`，路由测试导书用 `tests/style_reference_route_helpers.import_book`。只用合成文本（真书只能经
`NOVEL_SYSTEM_STYLE_REF_LOCAL_CORPUS` 本机读，永不提交）。分句、启发式或测量核口径变了：重生成 `tests/golden/style_reference/expected/*.json`
（`regen_expected.py`）与声音基线，并在提交说明里写原因。
