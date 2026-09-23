# 风格参考 v3（2026-09-23）：以参考为标准的一次性重构

> 本文是本轮重构的**契约**：设计、接口、全部问题台账与完成日志都在这里。实现与本文冲突时先改本文再改代码。
> 本轮之前的评估与方案：`docs/history/style/style-reference-first-2026-09-22.md`（样例放 user 尾部、结构跟随）、
> `docs/history/style/style-fidelity-fixes-2026-09-14.md`（全书窗口索引）。本轮评估由 5 路并行审阅 + 实库快照实测完成。

## 0. 作者的决定（2026-09-23）

1. 『龙族』用模型重新标段落类型：**同意**（先修分类提示词与分批方式，就地重分类，保留画像与绑定）。
2. 作者的作品学『龙族』到哪一层：**全学**——「风格参考就是做这个事情的，以风格参考为标准，雪花模块的定位只是为了搭建小说框架」。
   有风格绑定时：雪花设计只提供**框架**（发生什么、谁、因果、顺序、POV 与人称）；**怎么写、什么气质**（叙述口吻、幽默、
   比喻取向、节奏、对白、价值姿态）一律以参考为准，设计文字里的调性词（如「严肃」「窒息」）不再约束成文。
3. 22 本「匿名参考」测试书：**删除**。
4. 本轮发现的问题**全部处理、一次到位**；16 维度与策略选择器**深评后再定删还是改**（结论见 §1）。

## 1. 16 维度与策略选择器：评估结论

### 1.1 16 维度——保留，并变成核心

证据（实库『龙族』画像，67 条维度发现逐条读过）：
- 最能代表江南的特征都在维度里：`language.rhetoric`「俗世物件降维夸张的黑色幽默」「影视 / 游戏 / 亚文化喻体」；
  `language.vocabulary`「市井口语与青年亚文化词汇」「当代消费品、数码产品」；`theme.emotional_tone`「危急关头的自嘲吐槽」；
  `narrative.information_density`「量化指标与阶梯式数值」；`narrative.pacing`「倒计时与极短物理时间刻度」；
  `narrative.perspective`「视点人物的即时心理吐槽融入叙述」。
- 这些特征**确实进了起草提示**（渲染出的 `[正向风格特征]` 里有「降维比喻」「数码日用品」「自嘲诙谐」），但三场草稿几乎没兑现：
  opus 首稿只有两处像江南的俗世比喻，没有吐槽、没有流行文化梗；
  软 QC 还要求把其中一处俗世比喻换成「工业硬质」的意象。
- 原因不在维度，在用法：(a) 合成把 16 维压平成 18 行不带维度的句子 + 12 行鲁迅基线下方向相反的声音习惯 + 11 行禁忌，
  约 50 行不分主次，纪律句（「视点严格绑定」「禁止……」）排在前面，模型和软 QC 都只执行纪律、丢掉气质；
  (b) 抽取只读全书 3.3% 的碎片，互相矛盾（「逗号微步切分」vs「逗号稀疏」）；(c) 绑定上的 16 个开关只过滤 ≤480 字的禁忌行；
  (d) 没有任何环节按维度检查草稿做到了没有。

**v3 用法**：
1. **文风卡（dimension card）**：每维 1–3 条「代替模型默认写法，这位作者这样写」的可执行句（允许 ≤11 字的作者原话作例子），
   带证据引文；按辨识度排序；气质（temperament）与招牌手法标为「必须体现」。
2. **维度状态**（每个作品的绑定上）：`emphasize` 重点 / `normal` 正常（默认，全学）/ `exclude` 不学。
   重点维：排最前、多一条、选窗偏向示范它的窗口、读数权重 ×2、优先定向修改；不学：从卡、读数、修改里去掉。
3. **按维度的「像不像」读数**：可测维用确定性特征（对作者自己的窗口分布求 z），不可测维用评审模型按样例打分。
4. **按维度定向修改**：只改不像的维，改完再测，不更像就不采用（取代越改越远的整体润色）。
5. **矩阵**里 ✓ = 这条永远带上（pin），✗ = 这条不再用（exclude），**不再让整份画像失效**；👍/👎 并入 ✓/✗ 后删除。
6. **近期常见偏差**：同一作品最近 5 次读数里 ≥3 次越界的维度，自动作为首稿的补充强调（≤3 行）。

### 1.2 策略选择器——删掉 A/B/C/MIXED 这四个选项，保留两个有用的想法

证据：A（只给抽象规则）没有原文样例（少样本 ≫ 零样本），像的程度必然下降；B 渲染出来与 MIXED 完全相同
（`_render` 对 B 同样渲染四个抽象块，而界面称 B 为「只用样例」）；C 每次合成 / 应用要 33 秒和 189 MB 内存，
换来 5 条截到 100 字的碎句（720 字 vs MIXED 的约 4.4 万字），重启即丢，重排节点注册了却从不调用。
作为「选项」，它们只会让结果更不像。

保留并做好的两个想法：
1. **C 的「检索」→「按本场挑样例」**：学习时让模型给全书窗口打标签（场面 / 情绪 / 手法），起草时按本场设计（蓝图给出的场面标签、
   场景形态、概述与否、章内位置）挑最对口的窗口；定向修改时挑示范那一维手法的窗口。
2. **「怎么送参考」→ 诚实的「参考方式」**：`full` 全面模仿（样例 + 文风卡 + 声音，推荐）/ `samples_only` 只用原文样例（对照用）/
   `card_only` 只用文风卡、不发原文（隐私用；`segments_only` 的书自动用这个）。强度滑块换成「样例窗数」`sample_windows`（默认 12）。

## 2. 目标架构

### 2.1 原则
- 参考即标准；样例是主信号；文风卡是可检查的显式描述；读数负责测；修改只针对不像的维；**永不越改越远**。
- 严格 LLM：学习链路每一步有 LLM 时只用 LLM；产品路径不留启发式兜底（启发式分类只留作测试夹具模式）。
- 一个概念一个真源：一个测量核、一份持久化窗口索引、一张作业表、每个 bundle 一个 `StylePolicy`、一道抄袭门、一套标签词表。
- 界面上的每个控件都以已知方式改变输出，并由读数体现。
- 长操作全部是可续跑的持久作业，重启不丢、不卡死。

### 2.2 数据（迁移 `20260923_0090`；清理迁移 `20260923_0091`）
新表：
- `style_reference_jobs`：统一作业表（`kind` ∈ classify / learn / check）。列：`job_id`、`kind`、`book_id`(FK, 可空)、`profile_id`、
  `op_key`、`state`（queued / running / succeeded / failed / cancelled）、`phase`、`cancel_requested`、`attempt`、`owner_token`、
  `heartbeat_at`、`params_json`、`cursor_json`、`progress_json`、`result_json`、`error_json`、`created_at` / `updated_at` /
  `started_at` / `finished_at`。索引 `(book_id, kind, state)`、`(state, heartbeat_at)`。
- `style_reference_windows`：持久化窗口索引（按书、索引版本、段落根哈希）。列：`window_id`、`book_id`(FK)、`index_version`、
  `root_sha256`、`window_no`、`start_index` / `end_index`、`chapter_no`、`position`（opening / closing / middle / whole）、`chars`、
  `paragraph_count`、`type_mix_json`、`dialogue_share`、`typicality`、`features_json`（测量核特征，读数的参照分布）、
  `tags_json`（situations / moods / devices / gist）、`tags_version`。唯一索引 `(book_id, index_version, window_no)`。
- `style_fidelity_readings`：像不像读数。列：`reading_id`、`project_id`、`scene_id`、`profile_id`、`binding_id`、`source`
  （pipeline / adopt / archive / manual_check / author_draft）、`stage`（first_draft / revision / patched / final / manual）、
  `draft_ref`、`text_sha256`、`char_count`、`percentile`、`distance`、`reading_json`（越界特征、按维确定性分、版本）、
  `judge_json`（评审模型按维打分）、`copy_check_json`、`created_at`。**不存 chapter_id**（场景改章不需要跟着搬）。
- `style_reference_scene_windows`：每场冻结的选窗（同一场所有工序同一组窗）。列：`selection_id`、`selection_key`（唯一）、
  `scene_id`、`bundle_id`、`contract_hash`、`window_refs_json`、`params_json`、`created_at`。

清理迁移 0091（代码删除之后）：删 `style_reference_finding_feedback` 表、`style_reference_findings.base_confidence` 列、
`style_reference_validation_reports` 表（对照检查改写 readings）。受保护专名**复用** `style_reference_banned_terms`（`source="protected_auto"`）。

JSON（无迁移）：
- `book.stats_json`：`paragraph_root_sha256` / `paragraph_count`（段落表的写入者负责维护）、`classification_provenance`
  （`llm` / `legacy_heuristic` + 份额 + 一致率）。
- `profile.profile_json` v3 键：`dimension_card`、`card_line_states`、`voice`（测量核特征 + 作者自身分布 + 具体习惯句）、
  `structure_card`（去掉 `chapters` 列表）、`planning_guidance`、`qualitative_summary`、`reference_basis`、`protected_terms_version`、
  `profile_version = "style_profile_v3"`。旧键（`style_features` 等）只作迁移前画像的只读兼容。
- `binding.config_json` v3：`reference_mode`、`sample_windows`、`dimension_states`、`draft_mode`。旧 `strategy` 列统一写 `mixed`，
  旧 `intensity` 读时映射为 `sample_windows = round(3 + 9·i/100)`。

### 2.3 模块
```
services/style_reference/
  jobs.py            统一作业：建 / 认领（owner_token + attempt 条件写）/ 心跳 / 进度 / 取消 / 结束 / 清扫（过期心跳重排）/ 活动列表
  measure.py         测量核（一遍算完：段 / 句 / 分句 / 标点 / 虚词 / 引号对白 / 段形；汇总口径；KERNEL_VERSION）
  fidelity.py        读数（纯函数）：参照分布（作者自己的窗口）→ 百分位 + 越界特征 + 按维确定性分
  windows.py         持久化窗口索引（切窗 + 特征 + 典型度 + 标签读写）
  card.py            文风卡数据结构、渲染、行状态、维度状态
  learn_job.py       「学习文风」作业：窗口 → 标签 → 分层抽取 → 文风卡 → 受保护专名
  import_job.py      分类作业（作业表之上；按字数分批、3 路并行、编号解析、就地重分类）
  inject/            渲染包：request / selection / render / fit / audit（取代 injection.py 的大部分）
services/style_policy.py          每个 bundle 解析一次的风格策略（所有管线节点只看它）
services/reference_copy_gate.py   唯一抄袭门（12 字 n-gram 对绑定的书 + 受保护专名），所有进正文的路径都过
```

### 2.4 起草流程（有绑定且 style_first）
1. 场景蓝图（有绑定时只写事实 + `situation_tags`；不预写台词、不指定意象、不定收尾规则）。
2. 首稿（style_first_draft）：user 尾部 = 本场冻结的样例窗；system = 文风卡（气质与必须体现在前）+ 声音 + 近期常见偏差 + 红线。
3. 事实 QC（hard_qc）。
4. **读数**（确定性）→ 风格步：读数在作者正常范围内（百分位 ≤ 阈值且重点维无越界）→ **不调模型**，首稿即风格稿；
   否则**定向修改**（只改越界的维，给测得的差异 + 示范这些维的窗口），改完再测，**不更像就保留首稿**。
5. 软 QC = **参考评审**（样例 k=4 + 文风卡；按 16 维打 0–10 分；只提参考依据的问题，不用房风规则）→ 有不像的维才补丁；
   补丁后再评，**评分没提高或确定性距离明显变差就回退**。
6. 准定稿评审（按参考判，分数有范围）。
7. 归档：**抄袭门**（所有路径）+ 读数入库（final）。

### 2.5 选窗（每场冻结一次，`style_reference_scene_windows`）
输入：本场设计（形态、概述、章内位置、台上人物、蓝图场面标签）、绑定配置（k、维度状态）、近期常见偏差、种子（scene_id）。
配额（k=12）：章首 / 章末位置匹配 ≤3；场面匹配（标签）≈4；重点维 / 常见偏差维的手法示范 ≈2；其余按典型度加权、按种子在**全书**
轮换（一章至多一窗）。选出后按原书顺序呈现；评审节点取其中 4 窗，规划节点 3 窗；同一场所有工序读同一份冻结结果。

## 3. 接口（各包按此对接）

```python
# jobs.py
class StyleJobService:
    def create(kind, *, book_id=None, profile_id=None, op_key=None, params=None) -> StyleJob
    def claim(job_id) -> ClaimedJob | None            # attempt += 1, owner_token 新值, state running
    def heartbeat(claimed) -> bool                     # 条件写：owner_token 仍是自己 → True
    def progress(claimed, *, phase, done, total, detail=None, llm_calls=None) -> bool
    def save_cursor(claimed, cursor: dict) -> bool
    def succeed(claimed, result) / fail(claimed, code, message, retryable) -> bool
    def request_cancel(job_id) -> StyleJob             # queued / 心跳过期 → 路由里直接收尾为 cancelled
    def sweep(now) -> list[str]                        # 心跳过期(>60 s)的 running → queued（attempt 不变），返回待派发
    def active_for_book(book_id, kind=None) -> list[StyleJob]
    def activity() -> list[dict]                       # 统一活动条目（取代登记簿 + /imports/{key}/progress）
# 工人：register_job_handler(kind, fn(session, claimed) -> None)；dispatch(job_id) 投到有界线程池；
# 心跳 15 s；清扫 30 s 一次（lifespan 启动的后台线程）+ 启动时一次。

# measure.py
KERNEL_VERSION = "measure_v1"
def measure_text(text: str) -> TextMeasure            # 段落按换行切（与 manuscript_paragraphs 同一规则）
def kernel_features(measure: TextMeasure) -> dict[str, float]   # 旧 voice 特征名尽量保留
# fidelity.py
def build_reference_distribution(window_features: list[dict]) -> ReferenceDistribution  # 稳健中位数 / MAD + 下限
def read_fidelity(text, dist, *, dimension_states=None) -> FidelityReading
#   .distance / .percentile / .out_of_band [{feature, dimension, z, phrase}] / .dimension_scores {dim: 0..10}
FEATURE_DIMENSIONS: dict[str, str]                    # 特征 → 维度映射（可测维）

# card.py
class CardLine: line_id, text, kind(do|avoid), source, evidence_quote_ids, distinctiveness, mandatory
class DimensionEntry: dimension, label, summary, model_default, lines, devices, measurable_features
class DimensionCard: version, dimensions (16, 按辨识度), temperament, generated_at
def render_card_block(card, *, states, line_states, recent_gaps, budget_chars) -> str
def effective_dimension_states(config_json) -> dict[str, str]

# style_policy.py
@dataclass(frozen=True)
class StylePolicy:
    bound: bool; style_first: bool; mode: str; contract; contract_hash
    profile_id; binding_id; book_id; reference_mode; sample_windows; dimension_states; error_code
    def defers_house_taste(self) -> bool
def style_policy_for_bundle(bundle, *, task_type="scene_generation") -> StylePolicy   # 按契约哈希记忆
def style_policy_live(session, scope) -> StylePolicy                                  # 写作台等无 bundle 的节点

# inject/
@dataclass(frozen=True)
class StyleRenderRequest: role (draft|revise|review|plan), placement, k_cap, scene_id, position, situation_tags, context
def render_style(session, policy, request) -> RenderedStyle   # .system_prefix / .user_tail / .window_refs / .stats / .audit

# reference_copy_gate.py
def check_reference_copy(session, text, *, policy=None, book_ids=None) -> CopyCheck  # .blocked / .hits(哈希+位置) / .protected_hits
```

### 3.1 跨包约定（并行开发时的接缝）
- `book.stats_json["paragraph_root_sha256"]` / `["paragraph_count"]`：改动段落**文本或行**的写入者（刷新工具、重新导入）
  负责 `pop` 这两个键；`windows.ensure_window_index` 发现缺失时现算并写回；契约构建读它（缺失时现算）。
- `book.stats_json["paragraph_types_revision"]`（int）：分类作业每次完成（导入、重分类、就地重分类）+1。
  `stats_json["window_index"] = {version, root, types_revision}` 由 `windows.py` 在建索引时写；三者任一不符即重建
  （类型变了只重算 type_mix / 对白比例，不动标签）。画像 `profile_json["learned_from"] = {types_revision, root, index_version}`
  由学习作业写，界面据此提示「段落类型已更新，建议重新学习文风」。
- 场面 / 情绪标签只有一张词表：`services/style_reference/tags.py`（学习作业打窗口标签、场景蓝图给 `situation_tags` 共用）。
- 读数入库只有一个入口：`services/style_reference/readings.py::record_fidelity_reading(session, *, policy, text, source,
  stage, scene_id=None, project_id=None, draft_ref=None, judge=None)`（P5b 实现；P1 提供 `fidelity.py`）。
- 旧登记簿 `import_progress.py`：P2 把分类迁出、P3 把合成迁出、P5 把校验迁出，P7 删除模块与 `/imports/{key}/progress`。
- 各包**不改本文**；完成情况写进最终报告，由主会话汇总到 §7。

## 4. 问题台账（本轮全部发现 → 处理 → 负责包）

前缀：L = 主评估实测；I = 导入 / 分类 / 运维；E = 抽取 / 合成 / 表示；J = 注入 / 契约；V = 校验 / 下游；U = 接口 / 界面 / 测试 / 文档；
N = 本轮新增的「用得更好」。负责包见 §5。

| ID | 问题 | 处理 | 包 |
|---|---|---|---|
| L1 | 润色步 3/3 场越改越远；opus 润色只改 1.8% 却花一次 6.4 万 tokens | 风格步按读数决定：在范围内不调模型；否则定向修改 + 不更像就保留首稿 | P5 |
| L2 | 软 QC 按房风批改（删旁白吐槽、收紧视点、换掉像江南的比喻） | 参考评审 v10：只按样例 + 文风卡、按 16 维打分 | P5 |
| L3 | 蓝图预写结尾台词、指定意象、禁止总结式收尾，首稿照抄 | 有绑定时蓝图只写事实 + 场面标签 | P5 |
| L4 | 同一块样例一场发 6 次；评审节点也拿 12 窗 | 评审 k=4、规划 k=3；风格步在范围内不调模型 | P4/P5 |
| L5 | 设计调性与参考气质冲突，气质丢失 | 全学：设计只当框架；文风卡气质在前且必须体现；各提示词改口径 | P3/P4/P5 |
| L6 | 实库 22 本测试书、无批量删、测试写进实库 | 清理工具 + 书库多选删除 + conftest 拒绝非临时库 | P2/P6 |
| L7 | 『龙族』99% 段落类型是启发式（一致率 0.45） | 分类提示词修正 + 就地重分类（保留画像 / 绑定）+ 来源标注 | P2 + 上线 |
| L8 | 新鲜度预算的构式 / 意象清单把像江南的比喻当「章内已禁用」 | 有绑定时只保留逐字 n-gram 防重复，去掉构式 / 语义层清单 | P5 |
| I1 | 重启 / `--reload` 让分类作业成孤儿，续跑 409、取消卡死 | 作业表心跳 + 过期清扫（常驻）+ 过期可续跑 / 取消 | P2 |
| I2 | 作业无身份，删书重导入时旧线程继续写、可能把本机书发云端 | owner_token 条件写；每批重查书与云策略 | P2 |
| I3 | 分类结果按位置对齐、缺一条整批错位并补「叙述」、类型不校验 | 按段号对齐、枚举校验、不一致整批重试 | P2 |
| I4 | 分类提示词留着「短段默认过渡」、无上下文、锚定集是前 200 段、同模型自我一致 | 提示词 v3（去掉该规则、±1 段只读上下文）、锚定集全书分层抽、同模型跳过校准 | P2 |
| I5 | 旧书显示「已完成」，唯一修法会删画像和绑定 | `classification_provenance` + 「用模型重新分类（保留画像）」+ 费用预估 | P2/P6 |
| I6 | 一批失败整作业失败；客户端在派发时捕获 | 每批 2 次退避重试；每批按当前配置取客户端 | P2 |
| I7 | 「仅本机」检查的是全局 provider 而不是节点路由 | 按分类节点的实际路由判断 | P2 |
| I8 | 重复 / 空书导入返回 500 | 409 `STYLE_REFERENCE_BOOK_DUPLICATE`（带书 id、打开动作）/ 400 | P2 |
| I9 | 默认「仅本机」在云模型下必失败、报英文错 | 默认按当前模型推断；中文错误 + author_action | P2/P6 |
| I10 | 空行场界编号错位、剥副文本时丢场界、「……」算场界 | 同一切分基准、剥离后重映射、排除省略号行 | P2 |
| I11 | 刷新工具丢空行场界、不重编号、不看状态、默认全书 | 保留场界、重编号、跳过未就绪、显式选书 | P2 |
| I12 | 登记簿只在进程内；合成不可续跑；检查后执行竞态；幽灵条目；进度倒退 | 作业表取代登记簿；学习作业可续跑 | P2/P3 |
| I13 | 每批重载模板 447 ms、逐行插入 12 s、续跑加载全部段、单线程 | 模板每作业载一次、批量插入、COUNT、有界并行 | P2 |
| I14 | `PRESERVED_LLM_NODE_PREFIXES` 过期 | 修正 | P2 |
| I15 | 死代码 / 错误码不一致（内联分类路径、死别名、仓储方法、未用错误类、未读提示词文件、400 vs 409、前缀混用、BookStatus 缺 cancelling） | 清理 | P2/P7 |
| I16 | 全书分类要 5–13 小时、520–850 万 tokens | 按字数分批（≤6000 字 / ≤100 段）、关推理、3 路并行、重试 | P2 |
| E1 | 声音习惯相对鲁迅 / 朱自清、方向相反；合成要求正向特征与之一致 | 作者自身分布 + 绝对具体措辞（高频词）；去掉耦合 | P1/P3 |
| E2 | 16 次碎片抽取只读 3.3%、产出占前缀 3.4%、四成重复 | 分层读同一组连续窗口（≈4 万字）、允许具体词、带「模型默认」对照 | P3 |
| E3 | 指标按段平均、感官词表按子串、三套虚词表、两次切章、无版本 | 测量核（汇总口径、版本号）；删感官词表指标 | P1 |
| E5 | 整体重试丢掉已验证的发现 | 新抽取合并首轮与重试的有效项 | P3 |
| E6 | 提示内自相矛盾、「平均句长若干字」、条件措辞规则被忽略、观察与禁忌矛盾 | 合成加对账步；卡片按维；不再注入数字软化句 | P3/P4 |
| E7 | 人名设定保护只有散文句 | 受保护专名表（模型确认）→ 禁用词表 + 抄袭门 + 红线列表 | P3/P5 |
| E8 | 👍/👎 单作者永远到不了阈值 | 删除（并入 ✓/✗） | P3/P6/P7 |
| E9 | 画像 68% 是过期窗口索引；契约冻结过宽；兼容分支堆积 | 窗口表；契约 v2 瘦身；删兼容分支 | P1/P4 |
| E10 | 界面处处「0 引文」 | 按证据计数 | P3/P6 |
| E11 | 死配置 / 死代码（extraction.yaml 键、未执行的抽取预算、未用 Row 模型 / 枚举、重排模板、`is_synthetic`、补证写 3 行、未读事件、合成重算声音、合成里建 RAG、抽取 / 合成提示词早于「最大模仿」） | 清理 / 重写 | P3/P7 |
| J1 | 每次注入重算根哈希（0.6 秒 ×10/场）；每个 bundle 建两次契约；42 处深拷贝校验 | 根哈希存 stats；一 bundle 一契约；校验按哈希记忆 | P4 |
| J2 | 同一场各工序看的窗不同（3/12、1/12） | 每场冻结选窗 | P4 |
| J3 | 轮换池 72/522、章首 / 章末场永远同几窗 | 全书按典型度加权的种子抽样 + 配额 | P4 |
| J4 | 选窗建立在启发式类型上、强塞心理窗、提示里印英文类型 | 模型类型 + 标签；提示里只用中文位置 / 场面标签 | P4 |
| J5 | 存的索引过期、每次进程重算、不持久化 | 窗口表 | P1/P3 |
| J6 | 契约 9.1 万字只服务回退路径；根哈希失配时样例悄悄砍 87% | 删回退；失配用当前索引 + 可见提示 | P4 |
| J7 | 多层合并把 POV 优先级弄反 | 最具体的一层说了算 | P4 |
| J8 | 界面称 B「只用样例」实际渲染规则 | 参考方式三选一、说到做到 | P4/P6 |
| J9 | 策略 C 昂贵无用、内存索引无上限、重排节点不调用 | 删除 | P4/P7 |
| J10 | 强度恒为 100、禁忌砍到 11/19 且有重复、各块上限无意义、45 个预算键 30 个是死的 | 样例窗数；卡片总预算；合成去重；预算文件精简 | P4 |
| J11 | 425 行数字软化一行没改、还造出「若干字」 | 删除 | P4 |
| J12 | 预览不设对白配额、滑动一次 0.75 秒 | 预览 = 同一选窗逻辑；根哈希已存 | P4/P6 |
| J13 | 样例索引为空时直接不给样例；窗数不足 k 的书不走索引 | 只走索引；小书用全部窗 | P4 |
| J14 | 预算拟合暴力搜索 6.3 秒 | 贪心 | P4 |
| J15 | 写作台节点走「legacy_live」、不记契约哈希 | 显式实时契约路径并记哈希 | P4 |
| J16 | 注入服务靠改属性传参、评审 / 规划节点拿到起草口径的标题 | `StyleRenderRequest`；按角色的标题 | P4 |
| J17 | injection.py 3,889 行 | 拆成 inject/ 包 | P4 |
| V1 | 成稿时抄袭检查根本没查绑定的书；写作台采用完全不查 | 唯一抄袭门 + 受保护专名，覆盖所有进正文的路径 | P5 |
| V2 | 漂移读数对作者自己的书 95–98% 报警 | 以作者自己的窗口为参照的读数取代 | P1/P5 |
| V3 | 第一次归档就会让漂移改选窗、关轮换 | 删漂移注入与漂移优先选窗 | P5 |
| V4 | 作者稿单换行分段，指标把整场当一段 | 统一分段规则（测量核） | P1 |
| V6 | 校验层测不出像不像（量化分不清龙族与鲁迅、同步路径不会失败、禁用词为空、评审从未运行、失败被吞） | 「对照检查」= 读数 + 参考评审 + 抄袭门；删旧校验层 | P5/P6/P7 |
| V7 | 准定稿评分饱和 1.0、软 QC 风格分被丢弃 | 分数带范围、按维评分入库 | P5 |
| V8 | 约 60 处散落判断、5 种「绑定」定义、同一文本 4–5 次抄袭扫描、两次建契约 | `StylePolicy`；抄袭门按（契约, 文本哈希）缓存 | P5 |
| V9 | `MetricsRecorder` 吞异常不开保存点 | `begin_nested` | P5 |
| V10 | 漂移事件按章键；死分支；只写不读的遥测；「后台补算中」假字；过期文档串 | 清理 | P5/P7 |
| V11 | 成稿门在绑定下丢掉全部文学警告 | 用参考书校准过的规则判 | P5 |
| U1 | 改绑定参数被静默吞掉（待办卡去重） | 直接绑定（/apply），去掉待办往返 | P6 |
| U3 | ✗ 让画像失效、✓ 无作用 | pin / exclude 语义 | P3/P6 |
| U5 | 「回测已完成」是预览顺手写的 | 预览不写报告；步骤以真实检查为准 | P6 |
| U6 | 示例预览用的是早已不用的引擎 | 「本场预览」：本场会拿到的窗 + 文风卡 | P6 |
| U7 | 不起作用的控件（16 开关、👍/👎、策略、强度、起草方式文案、全局作用域、未接的场景预览） | 维度状态、参考方式、样例窗数、诚实文案 | P4/P6 |
| U8 | 界面看不到像不像 | 起草台 + 成稿中心 + 矩阵趋势 | P6 |
| U9 | 合成后的全局待办卡会绑到当时打开的作品 | 删除 | P6 |
| U10 | /profiles 428 KB、/injection/layers 为取 id 全量渲染 | 摘要载荷；轻量层查询 | P6 |
| U11 | 合成同步、抽取默认同步 | 学习作业 | P3/P6 |
| U12 | 术语与三套标签不一致 | 一张标签表；白话 | P6 |
| U13 | 注入页数字误导、预览顺序与起草不一致 | 真实数字；预览与起草同序 | P6 |
| U14 | 校验页标签 / 解释 / 假文案 | 新「对照检查」页 | P6 |
| U15 | 19 个 window 全局、列表无排序、裸 fetch、GET 写库、路由文件混杂 | 清理 + 路由拆分 | P6 |
| U16 | 死接口（#13、#17、#20、#31、#33、#4）；路径导入无测试 | 删除 / 合并；路径导入补测试 | P6 |
| U17 | 测试钉版本号与措辞、夹具重复、死功能测试 | 提示词契约表测试、共享工厂、随功能删测试、补回归 | P7 |
| U18 | 文档与代码矛盾、README、CLAUDE.md 过大 | 一份现行文档 + 历史归档 + CLAUDE.md 压缩 | P7 |
| N1 | 文风卡 | 合成产出 + 渲染 | P3/P4 |
| N2 | 维度状态 | 绑定配置 + 渲染 + 读数权重 + 界面 | P4/P6 |
| N3 | 窗口标签 | 学习作业一步 | P3 |
| N4 | 按本场挑样例 | 选窗配额 | P4/P5 |
| N5 | 按维读数 | 读数表 + 评审分 + 界面 | P1/P5/P6 |
| N6 | 定向修改 + 不更像就不采用 | 风格步 / 补丁 | P5 |
| N7 | 近期常见偏差 | 首稿补充强调 | P4/P5 |
| N8 | 参考方式三选一 | 渲染 + 界面 | P4/P6 |
| N9 | 「学习文风」一个按钮一个作业 | 作业 + 界面 | P3/P6 |
| N10 | 默认值用真实模型 A/B 验证 | 上线前小规模 A/B | 主会话 |

## 5. 工作包

| 包 | 内容 | 主要文件 |
|---|---|---|
| P0（主会话） | 本文；迁移 0090 与模型；`jobs.py` 核心；`card.py` 数据结构；`style_policy.py` 骨架 | db/models.py、alembic、schema_contract、上述新文件 |
| P1 测量与读数 | `measure.py`、`fidelity.py`、`windows.py`（切窗 + 特征 + 典型度，写窗口表）；声音习惯改为作者自身分布 + 具体措辞；指标汇总口径；统一分段；金标重生成 | measure / fidelity / windows / voice_signature / metrics / exemplar_index / structure(切章共用) |
| P2 导入与分类 | 作业表之上的分类作业与全部 I 项；活动接口改读作业表；删登记簿；清理工具；测试护栏 | import_job / ingest / segmentation / text_utils / activity / import_progress(删) / routes 导入部分 / tools |
| P3 学习 | 学习作业（窗口 → 标签 → 分层抽取 → 文风卡 → 受保护专名）；删 RAG 构建、👍/👎 服务；矩阵行状态 | learn_job / extractors / profile_synthesizer / run_orchestrator / finding 相关 / prompts(style_ref_*) |
| P4 注入 | inject/ 包；选窗冻结；文风卡渲染；参考方式；契约 v2；删 C / 回退 / 软化 / 多层合并；StylePolicy 的渲染端 | injection.py → inject/、runtime_contract、style_prompt_injection、planning_context |
| P5 管线 | StylePolicy 贯穿；蓝图事实化；风格步按读数；参考评审；补丁回退；准定稿评分；抄袭门全路径；读数入库；删漂移 | scene_generation / qc_engine / near_final / final_text_gate / orchestrator / bundle_builder / scene_blueprint / author_drafts / scenes 路由采用路径 / prompts(起草与评审) |
| P6 接口与界面 | 路由拆分与清理；直接绑定；学习作业界面；文风画像矩阵；本场预览；读数展示；标签表；导入对话框 | api/routes/style_reference*、frontend-react ws-styleref*、ws-scene-*、ws-manuscripts-* |
| P7 收尾 | 清理迁移 0091；测试（契约表、工厂、回归）；文档归档；CLAUDE.md 压缩；全量回归 | tests、docs、CLAUDE.md |

## 6. 上线步骤（上线前停服、备份）
1. `db_backup` 备份实库；`alembic upgrade head`（0090、0091）。
2. 系统配置：分类两个节点关推理、提高输出预算；评审节点输出预算；（如有 prompts 快照）`sync_prompt_templates --execute`。
3. 删 22 本测试书（清理工具，先 dry-run）。
4. 『龙族』：就地重分类（作业，保留画像与绑定）→ 「学习文风」作业（窗口、标签、抽取、文风卡、专名）→ 绑定改为 v3 配置。
5. 小规模 A/B（opus，2–3 场）定默认阈值；结果写入 §7。
6. 提交、推送、CI。

## 7. 完成日志
（按包填写：做了什么、偏离设计之处、测试、实测数字。数字除注明外都在实库只读副本上测。）

### P0 地基（主会话）
迁移 0090 四张新表；`jobs.py`（owner_token 条件写、心跳 15 s、过期 60 s、常驻清扫 30 s、取消 / 续跑 / 活动条目 / 工人框架）；
`binding_config.py`（旧策略 A → card_only、B/C/mixed → full，intensity i → round(3 + 9·i/100) 窗）；`card.py`；`tags.py`；
`style_policy.py`。迁移测试的 head 钉改读 `schema_contract.CURRENT_SCHEMA_REVISION`，以后加迁移不再回头改六个测试文件。

### P1 测量与读数
- 测量核 `measure.py`（measure_v1）：可见字为单位、汇总口径（真实书问号密度 9.27 → 5.42 / 千字，句长标准差 7.45 → 20.3）、唯一虚词表、
  唯一对白定义（引号内可见字 ÷ 可见字，‘’ 作主引号的早期排版也算）、唯一分段规则（`\n` / `\n\n` / `<p>` 测得相同）；52 个旧声音特征名
  保留并加 5 个（对白字占比、段长离散、阿拉伯数字、带单位中文数量、英文词）；删感官词表 5 项指标（子串匹配会把人名里的字算成感官词）；
  导入期统计 12.9 s → 3.5 s。唯一切章器 `structure.split_book_chapters`（书名页不占章号，合集粘在章末的下一卷卷首页切掉；真实书两边都是 90 章）。
- `windows.py`（exemplar_windows_v3）：520 窗 / 88 章；冷建 5.4 s，热读 33–75 ms；典型度按本书自己的窗口分布算；类型变了就地重算
  段型构成、标签保留；测量核变了按窗口边界沿用标签。`paragraph_root.py` 快速根哈希（0.23–0.34 s，与契约口径逐位一致）。
- `fidelity.py`：稳健中位数 / MAD + 下限、精确留一参照距离、每个可测维同权（重点 ×2、不学 0）、越界 |z| ≥ 2 带白话短语、
  证据太少的比例类特征不进越界清单（`low_evidence`）。真实书留一距离 p50 0.72 / p95 1.20。真实草稿百分位：SC01 91.0 / 91.9，
  SC02 87.7 / 90.6，SC03（opus）83.1 / 87.5 / 87.7（首稿 / 润色 / 补丁——每一道后续工序都更远）；30 段随机原文中位 55。
- 黄金语料护栏钉住实测：鲁迅留出窗 91–100% 在范围内；朱自清窗 75–85% 在范围外（目标 90% 没达到：参照一半只有 11 窗、两位作者同一时代）。
- 声音习惯句改成作者自身的高频词与大致频率（「句末常带么、吧、啊（大约每十句一次）」「连接多用就、也、还、但」），不再相对 1920 年代基线说偏多偏少。

### P2 导入与分类
分类作业迁到作业表（重启 / `--reload` 后由清扫接着跑，取消与续跑在请求里就能收尾）；每批条件写 + 重查书与节点路由的云策略；按段号对齐、
类型枚举校验、不补「叙述」；提示词 v3（去掉「短段默认过渡」、±1 段只读上下文）；锚定集全书分层抽样、两节点同模型时跳过校准；
按字数分批（≤6000 字 / ≤100 段）、3 路并行、每批 3 次退避；逐行插入 15.1 s → 批量 1.1 s。就地重分类 `{"mode":"retype"}` 保留画像与绑定；
估算接口；`/runtime`；重复导入 409 `BOOK_DUPLICATE`、空书 400；场界与刷新工具修正；`purge_style_reference_books` 工具；pytest 拒绝打开仓库实库。
『龙族』重分类估算：315 次调用（原 1,073）、约 199 万输入 + 85 万输出 tokens、3 路并行约 29 分钟。

### P3 学习文风
一个可续跑的 `learn` 作业：窗口 → 挑样本（约 12 窗 / 4 万字，分层、按书校验和定种）→ 四层各一次调用读同一组连续原文（逐字引文校验、
失败重试合并有效项）→ 文风卡（每句引用发现、去数字、按实测声音对账去矛盾、必须体现 ≤4）→ 本书专名（统计候选 + 模型确认，写成
`protected_auto` 禁用词）→ 全书 520 窗打标签（每批 ≤8 窗）→ 就地写画像（同一 profile_id、版本 +1、绑定不动、✓/✗ 状态沿用）。
删旧 16 次抽取、同步合成、👍/👎、发现审阅让画像失效、合成后的全局待办卡。真实书一次学习 71 次调用（抽取 4、合成 1、专名 1、标签 65），
约 190 万输入 tokens（标签占 88%）；本地开销 27 s；画像 39–42k 字（旧 154k）。新节点 `style_ref_protected_terms` / `style_ref_tag_windows`
需要在系统配置里补路由。（主会话合并时补：学习作业在跑时重分类 / 就地重标 / 继续分类一律 409 `BOOK_LEARNING`——破坏式重分类原本先清派生数据再查。）

### P4 注入
`inject/` 包（request / bindings / selection / render / fit / audit / preview / gaps），`injection.py` 3,889 → 120 行兼容壳；
每场冻结选窗（同一场首稿 / 修改 / 评审 / 补丁同一组窗，不看草稿）；配额：位置 ≤3、场面标签 ≈4、手法 ≈2、其余按典型度加权在全书轮换、
一章一窗；覆盖模拟 50 / 100 / 200 / 1000 场 → 60% / 80% / 91% / 99.8%（旧轮换池只到 14%）。参考方式三选一（segments_only 强制只用文风卡）；
旧画像退回卡替身并丢掉含数字的行；贪心拟合 6.3 s → 79 ms。契约 v2 单层（最具体的绑定：scene > POV 角色 > 其余角色 > project），
去掉样例引用，119,878 → 16,677 字；根哈希失配时用当前索引并记 `STYLE_REFERENCE_BOOK_CHANGED`；预览与起草同一选窗。

### P5a 管线
StylePolicy 贯穿（架构守卫：只有 style_policy / runtime_contract 能调旧判定）；唯一抄袭门覆盖成稿门、采用 / 再确认、成稿中心、写作台
（AI 建议与局部改写在生成时就筛、采用时再拦）；首次建索引 2.8 s、之后每次约 25 ms；删漂移驾驶；有绑定时用事实版蓝图
`scene_blueprint_facts` v1（带场面标签，随 bundle 冻结；无绑定的蓝图逐字不变）；软 QC v10 参考评审按 16 维打 0–10 分并入库；
准定稿评审 v10 分数带范围（9.3 → 0.93，不再夹成 1.0）；首稿 v9「设计只是框架」；新鲜度预算在让位时只留逐字 n-gram；
成稿门按绑定书的规则校准；遥测写入进保存点。

### P5b 管线 · 第二部分
读数唯一入口 `readings.record_fidelity_reading`（按场景键、同一稿行幂等、失败不阻断管线）：首稿、定向修改、补丁、归档、采用 / 再确认、
成稿中心、写作台采纳都记。作者手笔直起时风格步按读数决定：首稿在作者范围内（或读数不可信）不调模型、首稿即风格稿；越界才跑定向修改
（新模板 `style_targeted_revision` v1，走 `style_draft` 节点路由，只改越界的维，≤4 维），改完再读，不更像 / 没过抄袭门就保留首稿；
软补丁后评审分变差或读数变远就退回；Best-of-N = 首稿 + (N−1) 个定向修改、按距离排序。旧回测层删除，改为「对照检查」作业
（`check_job`，新模板 `style_ref_check_judge` v1 走 `soft_qc` 节点路由：确定性读数 + 16 维参考评审 + 抄袭门）。读数接口：
`GET /api/v1/scenes/{id}/style-fidelity`、`GET /api/v1/projects/{id}/style-fidelity`、`GET /api/v2/style-reference/readings/{id}`、
`POST/GET /api/v2/style-reference/checks`；工作台摘要带 `style_fidelity`。阈值（`injection_budget.yaml` 的 `fidelity:` 段，暂定）：
风格步百分位上限 90、修改至少近 0.03、补丁距离最多远 0.05、评审容差 0.02。

### P6a 接口与界面 · 第一部分
路由拆成包（books / learn / profiles / bindings / activity）；书与画像列表只给摘要（画像列表不再带整份 profile_json）；直接绑定
（`POST /profiles/{id}/apply`，一个目标只有一个活动绑定，旧的停用并告知；`PATCH /bindings/{id}`；`GET /projects/{id}/style-binding`）；
`POST /books/bulk-delete`；删死接口（`GET /runs/{id}`、绑定预览、任务默认表、`/imports/{key}/progress`、示例预览与其节点、三个旧回测接口）；
列表按创建时间排序。界面：参考书库多选删除、导入默认值按当前模型、三档如实说明、中文错误；总览提示旧的启发式段落类型（带一致率）与
「用模型重新分类（保留画像）」（先给估算）；学习文风卡（估算、进度、取消 / 续跑、需重新学习提示）；文风画像（气质、16 维按层、
通用写法 vs 这位作者、作者不这么写、证据、✓ / ✗、每维 重点 / 正常 / 不学）；用于作品（参考方式三选一、样例窗数、起草方式、直接应用 / 解除）；
本场预览；唯一标签表（与后端 `card.py` / `tags.py` / 段型枚举对齐，有测试）。store 不再往 window 上挂全局。

### P6b 界面 · 第二部分
「对照检查」成为风格参考的第四步：贴文字（≤6 万字，超长直说不截）或选本作品的一场 → 后台作业 → 结果写成「在这位作者自己的段落里排第 N
位（百分位，越低越像）、在 / 不在正常范围内」，外加越界短语（白话，不给 z 值）、评审总分与每维分和一句说明、抄袭检查只给计数；读数不可信时
说明原因。起草台证据栏新增「像不像」面板：首稿名次 → 风格步的决定与原因 → 定向修改 → 补丁采用 / 退回 → 终稿，评审最弱的几维与说明，
「对照检查」按钮；STYLE_* 提示改成白话（「注入」→「带入起草」）并接上新代码；「本场参考窗口」每窗带梗概、场面 / 情绪 / 手法标签与入选配额。
成稿中心每场终稿的像度徽标、页头「N 场终稿里 M 场在作者范围内」。文风画像（书用于当前作品时）：「《作品》像不像」卡（范围内场数、近期常见偏差、
趋势图，可键盘 / 悬停、带表格视图）与每维的实测 / 评审均值和「近期常见偏差」标记。写作台抄袭门拒绝（409 `SOURCE_SAFETY_BLOCKED`）说清
重合在第几字、正文没有改动、可再试一次，从不显示参考原文。读数载荷补 `max_percentile` / 重点与不学维 / `reliable` 与原因 / 窗数门槛；
作品汇总补 `recent_gap_details` 与每场最新终稿读数；起草台窗口引用带窗口号、入选配额与标签梗概。

### P7 收尾
迁移 `20260923_0091` 删 `style_reference_finding_feedback`、`style_reference_validation_reports` 与 `style_reference_findings.base_confidence`
（降级只恢复结构）；删 RAG / 策略 C（`rag.py`、`rag_evaluation.py`、`style_signature.py`、评测清单、`rag_*` 预算键、重排节点）、进程内登记簿
`import_progress.py`（活动只读作业表）、`_llm_helper.py`，`exemplar_index.py` 并入 `windows.py`，`injection.py` 只剩 `InjectionService`；
评审 / 规划窗数只有 `inject.request.REVIEW_K` / `PLAN_K`。`tests/test_prompt_template_contracts.py` 一张表管所有模板（版本、预算、对象成员
必须声明 properties、分数带范围、关键模板的必含 / 禁用措辞），各测试里只钉版本号的断言删掉；`tests/style_reference_factories.py` 共享种子。
契约测试查出三个评审模板违约并修正（`near_final_acceptance_review` v11、`chapter_near_final_review` v4、`writer_deep_review` v7）；顺手修
准定稿改写简报读错键（评审给的 `target` / `issue` / `fix_direction` 原本被丢掉，改写退回房风默认简报）。现行说明 `docs/style-reference.md`，
11 份旧文档移到 `docs/history/style/`；CLAUDE.md 196 KB → 153 KB（风格参考一节约 6.5 KB）。


### 复核（合并后四路只读审阅 → 修正线）
P0–P7 合并后请四位审阅者只读审查：作业与数据生命周期、注入与契约、管线与门、界面与接口。发现逐条核实后分四条互不重叠的修正线
（作业 / 注入 / 管线 / 界面）外加主会话的两批跟进来改，每条修正都有「没有修正就失败」的回归测试（各线逐条回退验证过）。

- **作业**：进程退出（`--reload` / 停服 / Ctrl-C）不再算作业失败——「工人代」+1，处理器在下一个检查点把作业放回 queued（游标保留），
  下次启动接着跑；LLM 调用在守护线程里，退出不等在飞的网络请求。同一本书的分类与学习「先插入、再查」互斥；清扫收尾工人死前被要求
  取消的作业并跑收尾钩子；放回队列是条件写。分类输出按条收、只重发没分出来的段；一批失败时排空在飞的批；段落表在分类期间被改直接
  失败并说清原因。对照检查独立车道。清理工具 / 破坏式重分类删绑定前作废规划产物；刷新工具在写锁里重算、只合并自己的键；书库批量删除
  与幂等记录同一事务；pytest 拒绝任何检出的实库。
- **注入**：云策略按**接收提示的节点的实际路由**判（`policy.decide_reference_route`，模板 → 节点表 `inject/routing.py`）——「仅本机」
  的书遇云端节点，由这本书派生的东西一个字都不送、调用 409；包着适配器的各处（软 QC、准定稿、写作台、作者稿建议、场景蓝图）不再吞
  这个 409，bundle 的派生段按读 bundle 的全部节点判，起草各遍直传实际节点。包导入环；文风卡预算先保证钉住 / 必须的句与「作者不这么写」；
  只用文风卡时例子 ≤11 字且不含专名；每场冻结选窗对没有 bundle 的节点（对照检查、写作台）也生效；没带样例时明说；旧契约的
  segments_only、按原始载荷指纹缓存策略、保存点里写选窗、书已删的提示、「不学」也作用于声音与近期偏差；对照检查的评审压预算
  （与管线同一张表）、作业所有权。
- **管线**：抄袭门只拦原文连续 12 字以上相同；受保护专名改成成稿门不拦的提示（`source_safety:protected_term`），且只读活的禁用词表
  （作者删掉的误收词立刻不再命中）；学习作业的专名提示词 v2 只要专有名词与作者自造的词，另有确定性过滤（单字、出现不到 3 次、日常词）。
  采用路径的 409 带 `details.reference_copy`；定向修改只为自己新引入的重合负责，Best-of-N 把被拦的候选排最后；补丁退回的评审容差
  0.1；评审分数按模板声明的刻度换算、越界丢掉（软 QC、准定稿、写作台深评）；观察性工作放进保存点，不再能打断检查点；书已删 / 策略
  降级 = 未检查（不是通过）；未绑定的场不记「参考评审」；显示按选中的候选；修补 / 修复节点用改稿口径；接受修复稿的检查点续跑。
- **界面**：删书后不再留「进行中」的幻影作业（按书清活动条目、按完整清单收掉服务端不再列出的在跑条目）；「去设置模型」回来后重读运行时；
  旧版全局应用只读，不再被某一部作品的「保存 / 解除 / 逐维状态」改到所有作品；换回用过的书时沿用那本书原来的设置；删书确认列出所有在用
  的作品、删后刷新在用状态；隐私说法与后端一致（「起草不发原文」：分类 / 学习仍发整段，起草只送文风卡；「仅本机」云端节点一个字都不送）；
  对照检查不再跨作品查别的作品的一场（前端清掉不属于当前作品的场，后端按场景取作品）；就地重标失败可「继续分类」；在用列表随应用 / 解除
  刷新；活动表为空时按书上的状态补上在跑的作业并退避重试；只用文风卡的摘要照实显示；本书专名可逐个去掉且重新学习不再加回；不再露英文
  报错（前端一个口径 `lib/messages.js`）；续跑同一作业 id 时新状态盖过旧条目；关掉对话框后导入失败走提示。

### 上线与 A/B（2026-09-24）
推送 `main`（CI 8 个作业全绿）后在作者的安装上：`db_backup` 备份 → 快进 → `alembic upgrade head`（0090、0091）→ models 快照补丁
（分类两节点关推理 + 8192，抽取 / 合成 16384，补新节点路由，删退役节点；路由仍是作者选的 flash）→ 删 22 本测试书 → 从界面同一个接口
就地重标『龙族』（26,616 段全部由模型分类，315 次调用、0 次重试、约 50 分钟；输入 2.4M、输出 2.1M tokens——这个中转的 flash 把推理
算进输出）→ 学习文风（71 次调用、约 5 分钟、1.37M / 0.10M tokens；文风卡 16 维各 3 条「这样写」+ 1 条「不这么写」，四条气质句抓住了
「生死关头用市井烂话解构崇高」「吐槽底下的孤独」「狂欢里骤然切入死亡」「必输的赌局里硬撑抱团」；本书专名 106 个，全是专有名词；
520 窗全部打标签）→ 绑定改写成 v3 配置（全面模仿、12 窗、手笔直起）。

A/B：上线后的库复制三份，各跑三场从没起草过的场景（第 1 章第 4、5 场，第 6 章第 1 场），`run_policy=strict`。
读数 = 在作者自己的段落里的位次（越低越像，≤ 90 在作者范围内）：

| 场 | F 全面模仿（flash） | S 只用原文样例（flash） | O 全面模仿、起草走 opus |
|---|---|---|---|
| 第 1 章第 4 场 | 首稿 91.5；修改稿 94.2 更远被丢 → 91.5 | 98.3 → 修改 96.5 | 失败：中转回来的正文带乱码（U+FFFD），首稿与修复都被完整性门拒 |
| 第 1 章第 5 场 | 94.0 → 修改 92.7 → **定稿改写整场挤成一段 98.3** | 93.8 → 修改 85.8 | 首稿 85.4，在范围内 |
| 第 6 章第 1 场 | 92.1 → 修改 89.8 | 首稿 89.6，在范围内 | 首稿 88.5，在范围内 |

结论与处理：
- 风格步的「不更像就不采用」6 次决定全对（1 次丢掉更远的修改稿、4 次采用更近的、2 次首稿已在范围内不调模型）；阈值 90 / 0.03 保持。
- **发现并修好**：定稿改写（准定稿评审要求重写时）没有防「越改越远」，一次把整场挤成一段、从 92.7 退到 98.3，第二轮评审（同一个 flash）
  反而给了 0.93。现在定稿改写稿先过相对来源稿的基础安全门（「整场挤成一段」成为文本完整性标记，首稿 / 风格稿 / 定向修改同样会拒）与
  读数门，没过就保留来源稿（见现行说明 `docs/style-reference.md` §6 第 6 步）。
- 文风卡：同样用 flash，全面模仿的首稿平均 92.5、只用样例 93.9——文风卡不拖累「像」，首稿略好；保留 16 维（与 §1 的结论一致）。
- 模型：opus 起草的首稿最像（85.4 / 88.5，都在范围内，flash 首稿 89.6–98.3），但这个中转的 opus 在结构化输出里间歇带乱码（1/3 场失败，
  完整性门拦下、没有坏字进正文）。默认仍是作者选的 flash；想更像可以在系统配置里把 `style_draft` 路由换成 opus，失败的场重跑即可。
- 用量：每场 6–9 次调用、约 11–22 万输入 tokens（旧管线 opus 一场约 42 万）、2.5–5 分钟。个别场 soft QC / 准定稿评审在这个中转上
  推理爆量（一次 6.8 万输出 tokens、2.5 分钟）：评审节点的推理档可以在系统配置里调低。
