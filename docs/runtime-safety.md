# 运行安全与资源边界

本系统默认按“单作者、本机桌面服务”运行。默认配置只接受回环地址请求；不要把本地开发服务直接暴露到公网，也不要把共享访问令牌当成多用户身份系统。

## 网络访问

- `NOVEL_SYSTEM_LOCAL_ONLY=true`：默认值，只允许 `127.0.0.1`、`::1`、`localhost`；`/live` 与 `/ready` 仍可用于健康探测。
- `NOVEL_SYSTEM_LOCAL_ONLY=false`：显式开启远程访问，同时必须设置 `NOVEL_SYSTEM_REMOTE_ACCESS_TOKEN`，所有非健康请求都要携带 `X-Novel-Access-Token`。
- 第一方 React 前端可在构建时设置 `VITE_NOVEL_SYSTEM_ACCESS_TOKEN`。该值会进入浏览器资产，只适合可信作者使用的受限网络；它不能替代账号、权限和租户隔离。
- 远程模式的共享 token 不是用户身份系统，因此服务端会忽略客户端 `X-Operator-Ref`，审计主体统一记为 `remote-access-token`；需要区分真实用户时必须在可信代理之后接入正式认证/RBAC。
- 远程模式下同时收紧 `NOVEL_SYSTEM_CORS_ORIGINS`，并在主机防火墙或可信反向代理处限制来源。构建时的
  `VITE_NOVEL_SYSTEM_API_BASE` 不是本机回环地址时，前端构建会把它的 origin 自动补进 `index.html` CSP 的 `connect-src`。
- 每个写接口都要带 `X-Idempotency-Key`（缺了 400 `IDEMPOTENCY_KEY_REQUIRED`）：同键同载荷重放上一次的响应，同键不同载荷 409。
  React 客户端每个写请求都带键。重放缓存只留 72 小时（见下文「定期维护」）。

## LLM 用量：记账与读数

每一次真正发给供应商的请求都经记账层（预留 → 派发 → 结算，三段各自短事务，等网络时不占写事务）；账本与成本看板的读数照常记录，
不依赖任何上限。

- **没有全局额度闸。** 以前六个只能用环境变量打开、默认全关的全局上限（日 / 月 / 本作品日 token、日请求数、并发数、日金额）
  与按环境变量单价算的「今日金额」已经删除（2026-09-30）。`NOVEL_SYSTEM_LLM_DAILY_TOKEN_LIMIT`、`…_MONTHLY_TOKEN_LIMIT`、
  `…_PROJECT_DAILY_TOKEN_LIMIT`、`…_DAILY_REQUEST_LIMIT`、`…_MAX_CONCURRENT_REQUESTS`、`…_DAILY_COST_LIMIT_USD`、
  `…_INPUT_COST_PER_MILLION_USD`、`…_OUTPUT_COST_PER_MILLION_USD` 还设着也没有效果，后端启动时记一条警告。
- 成本看板的「全局用量」只是读数：今日 / 本月 / 本作品今日的 token、今日请求数、正在飞的调用数（按已结算的物理尝试的实际
  `total_tokens` 算，UTC 日 / 月）。看板以 token 为主；金额只算 `config/pricing.yaml` 里写了单价的模型，其余标「未定价」。
- **唯一还在的闸是场景预算**，默认关（`NOVEL_SYSTEM_SCENE_TOKEN_BUDGET_MULTIPLIER=0`）。设成正数 N 时，新初始化的场景端到端
  上限是 N × 单发基线，派发前按场景做条件预留。
- 供应商真实用量超出预留（典型来源：思考型中转把 reasoning token 计入 `completion_tokens` 且不受 `max_tokens` 封顶）只在这一场的
  场景预算**武装着**时拦截响应（`LLM_USAGE_EXCEEDS_RESERVATION`）。没有武装时，响应照常交付并按真实用量落账，超出量记在审计
  摘要的 `usage_overage_tokens`，后端日志记一条 warning。
- `NOVEL_SYSTEM_LLM_RESERVATION_RECOVERY_TTL_SECONDS`（默认 `3600`）：启动对账只动这么久以前、没有场景 / 任务所有权的陈旧预留。

## 内容与来源安全

- `NOVEL_SYSTEM_CONTENT_SAFETY_MODE=review`（默认）：少数高风险复合启发式命中会阻止无人值守归档，正文不会丢失；作者逐项核对后可确认精确 finding code 再提交。
- `NOVEL_SYSTEM_CONTENT_SAFETY_MODE=audit`：只留痕和提示，不阻断归档。
- 启发式不能判断真实年龄、同意关系、叙事立场、隐喻或跨语言表达；未命中不等于安全，命中也不是法律或平台分级结论。
- 服务器路径导入参考书默认关闭。只有设置 `NOVEL_SYSTEM_STYLE_REFERENCE_IMPORT_ROOTS` 后，管理员才能从列出的根目录导入；Windows 多目录使用分号分隔。浏览器上传仍是推荐入口。

## 后台恢复边界

服务启动时会恢复尚未派发的场景/章节任务，并通过数据库租约和任务 CAS 防止同波重复执行；运行中每 60 秒再巡检一次租约过期的孤儿任务。已有活跃 lease 的任务不会被抢占。进程退出（停服、`--reload`）时，本进程持有的运行任务租约就地到期，重启后的恢复立刻接着跑，不必等租约自然过期。场景任务一次最多跑两条管线，章节任务一次一章。风格参考的段落分类、学习文风与对照检查是作业表（`style_reference_jobs`）上的持久作业：工人的每一次写都以作业的 `owner_token` 为条件，心跳超过 60 秒的作业由常驻清扫线程（启动时一次，之后每 30 秒）放回队列、从游标续跑，重启或 `--reload` 不需要人工介入。旧抽取流程留下的「运行中」记录与没有作业可续的「分类中」书由迁移 `20260929_0094` 一次标为失败（`STYLE_REFERENCE_RUN_RETIRED`），对那本书重新「学习文风」即可。

数据库结构落后于代码时（`--reload` 先于迁移加载了新代码），后端不拿旧结构跑启动恢复与后台清扫，`/api/*` 统一回 503 `SERVICE_NOT_READY`「数据库结构需要升级：请重启后端（启动脚本会自动升级）」；结构跟上之后的第一个 `/api/*` 请求或 `/ready` 探测补跑被推迟的那一段。

启动恢复还会对超过 `NOVEL_SYSTEM_LLM_RESERVATION_RECOVERY_TTL_SECONDS` 的 legacy LLM 预留做保守对账：仅处理没有 `scene_id`、没有 `run_job_id` 且不是 scene scope 的调用；未派发预留会释放，已派发但没有持久化结果的预留会标记失败并按估算用量落账。场景与任务所有权链路由各自的 lease/checkpoint 恢复负责，不进入这项扫描。

### 定期维护

运行任务的巡检线程顺带执行全系统的定期维护：幂等重放缓存按 72 小时保留期每 6 小时清理一次（启动后的第一拍不清，部署时的备份要先做完）；绑定了参考书的作品，参考校准的读数每 6 小时预热一次。风格作业清扫线程另跑两项：遥测按 90 天留存、作业表按保留期清理（对照检查的作业记录 30 天后删，读数留着；每本书的分类 / 学习作业只留最近一次）。SQLite 删行不会让库文件变小：部署时用 `compact_db` 压缩一次（见下一节）。

当前执行器仍是进程内线程池，不是外部持久队列。需要多主机、高可用或不可信多用户访问时，应另行引入身份授权、租户隔离、外部任务队列、密钥托管和集中审计；现有远程开关不代表这些能力已经具备。

## SQLite 备份与恢复

备份使用 SQLite 在线备份 API，可包含仍在 WAL 中但已经提交的写入。新快照只有在完整性、外键、页信息和 SHA-256 清单全部通过后才会替换同名旧备份；恢复拒绝没有 `.meta.json` 清单、被篡改、外键损坏或 WAL 正忙的来源/目标。

恢复仍然是停机操作。工具可以发现活动事务，却不能证明另一个空闲进程不会在检查后重新写入；先用 `stop-dev.cmd` 或对应 Linux 停止脚本停掉服务，再备份/恢复。不要把浏览器恢复记录或回收站当成数据库备份。

```powershell
cd backend
python -m novel_system.tools.db_backup --backup .\novel_system.db .\backups\novel-system.db
python -m novel_system.tools.db_backup --verify .\backups\novel-system.db
python -m novel_system.tools.db_backup --restore .\backups\novel-system.db .\novel_system.db
cd ..
powershell -ExecutionPolicy Bypass -File scripts\db_backup_drill.ps1
```

Linux 恢复演练入口为 `bash scripts/db_backup_drill.sh`。两个演练脚本都只破坏系统临时目录中的副本，不改动传入的真实源库。备份旁边的清单只记文件名（不记绝对路径）。

部署时压缩库（服务全部停掉、备份做完之后）：`python -m novel_system.tools.compact_db <库文件>` 先干跑看会清掉多少行，再加 `--execute`——清掉过期的重放缓存、把 `auto_vacuum` 切成 INCREMENTAL、`VACUUM`、完整性与外键检查、截断 WAL。库还在被使用时它拒绝执行。完整的部署顺序见[数据库迁移](migrations.md)。

## 历史 LLM 审计载荷脱敏

`LlmCall`、`LlmCallAttempt` 与幂等 `OperationLog` 是计费、追踪和故障恢复账本，不应成为第二份小说正文仓库。新写入只保留哈希、长度、消息角色、受限协议字段及有界结构摘要；0073 迁移会把旧记录中的完整提示词、作者草稿、模型输出和供应商错误正文改写为同类摘要。该迁移不可逆，升级前先按上节完成可验证备份。
