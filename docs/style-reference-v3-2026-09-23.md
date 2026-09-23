# 风格参考 v3（2026-09-23）：以参考为标准的一次性重构

> 本文是本轮重构的**契约**：设计、接口、全部问题台账与完成日志都在这里。实现与本文冲突时先改本文再改代码。
> 本轮之前的评估与方案：`docs/style-reference-first-2026-09-22.md`（样例放 user 尾部、结构跟随）、
> `docs/style-fidelity-fixes-2026-09-14.md`（全书窗口索引）。本轮评估由 5 路并行审阅 + 实库快照实测完成。

## 0. 作者的决定（2026-09-23）

1. 『龙族』用模型重新标段落类型：**同意**（先修分类提示词与分批方式，就地重分类，保留画像与绑定）。
2. 《何来》学『龙族』到哪一层：**全学**——「风格参考就是做这个事情的，以风格参考为标准，雪花模块的定位只是为了搭建小说框架」。
   有风格绑定时：雪花设计只提供**框架**（发生什么、谁、因果、顺序、POV 与人称）；**怎么写、什么气质**（叙述口吻、幽默、
   比喻取向、节奏、对白、价值姿态）一律以参考为准，设计文字里的调性词（如「严肃」「窒息」）不再约束成文。
3. 22 本「匿名参考」测试书：**删除**。
4. 本轮发现的问题**全部处理、一次到位**；16 维度与策略选择器**深评后再定删还是改**（结论见 §1）。

## 1. 16 维度与策略选择器：评估结论

### 1.1 16 维度——保留，并变成核心

证据（实库『龙族』画像 `sr_profile_c18ab0b2086a`，67 条维度发现逐条读过）：
- 最能代表江南的特征都在维度里：`language.rhetoric`「俗世物件降维夸张的黑色幽默」「影视 / 游戏 / 亚文化喻体」；
  `language.vocabulary`「市井口语与青年亚文化词汇」「当代消费品、数码产品」；`theme.emotional_tone`「危急关头的自嘲吐槽」；
  `narrative.information_density`「量化指标与阶梯式数值」；`narrative.pacing`「倒计时与极短物理时间刻度」；
  `narrative.perspective`「视点人物的即时心理吐槽融入叙述」。
- 这些特征**确实进了起草提示**（渲染出的 `[正向风格特征]` 里有「降维比喻」「数码日用品」「自嘲诙谐」），但三场草稿几乎没兑现：
  opus 首稿只有两处像江南的俗世比喻（「平得像是在念菜单」「劣质的迷彩布」），没有吐槽、没有流行文化梗；
  软 QC 还要求把「劣质迷彩布」换掉。
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
（按包填写：做了什么、偏离设计之处、测试、实测数字。）
