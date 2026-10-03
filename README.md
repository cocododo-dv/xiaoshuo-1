# AI 小说创作系统

这是一个面向单机、单作者工作流的长篇小说创作系统。正式界面是 `frontend-react/` 的 React 工作台。

当前产品主线是：新建作品 → 雪花十步构思 → 物化章节与场景 → 逐场 AI 起草或人工写作 → 作者复核并提升权威正文 → 成稿中心终稿批准。系统提供带正文证据的正史事实链、连续性检查、内容与来源安全提示、LLM 用量记账和后台任务恢复，但不应被理解为多用户 SaaS、生产级高可用服务或自动出版裁决器。2026-09 的减法移除了结果治理/盲评实验、知识提升与索引发布台、长篇控制塔、互操作导出台、旧 Vue 前端以及一批只有旧界面才能触达的 v1 接口；2026-09-29 的全系统重构又删掉了 Chroma 向量库、全局 LLM 额度闸、声线卡 / 关系卡、v1 雪花规划器与写作偏好学习。这次重构给作者带来的变化见 [2026-09-29 重构：给作者的说明](docs/refactor-2026-09-29.md)。

## 当前界面与能力边界

启动后默认进入 `主页`，不是雪花页。作家模式的日常入口包括：

- `主页`：查看当前作品、全书各章所在阶段和下一步（原「流程」页已并入主页）。
- `构思`：雪花十步、场景分诊（AI 分诊 + 你的裁定）、整理章节结构与回流。
- `写作`：人工编辑、AI 候选、草稿保存、深改诊断、内容安全复核和权威正文提升。
- `风格`：参考书 → 学习文风 → 用于作品（外加随时可用的「对照检查」）——导入一本想学它文风的书，由模型学出文风卡，再用于当前作品；起草时每场带上按这一场挑的原文样例窗（默认 12 窗，放在 user 消息末尾、紧挨输出；system 里是文风卡、声音习惯与防照搬红线）。详见 [风格参考](docs/style-reference.md)。
- `待办`、`资料`：处理人工决策与故事资料。

切到高级模式后会显示 `章节编排`、`AI 起草台`、`成稿中心`、`文学质量` 和 `成本看板`。

当前需要特别区分：

- 新建作品、雪花步骤保存/批准、结构物化、逐场 AI 起草、草稿同步和权威正文提升都有真实后端链路。
- 演示作品与假生成已退役：不再有内置演示作品或离线确定性桩生成。所有生成节点一律 fail-closed——未配置可用 LLM 时返回 409/502 与 `author_action` 引导，绝不返回罐头文本。
- 高级 `章节编排` 的 `运行本章` 会启动持久化章节任务、轮询真实进度，并明确展示阻断、失败与模型未配置状态。
- `成稿中心` 的终稿批准必须先完成当前终稿的正史事实复核，再在批准对话框里勾选「我已从头到尾通读当前正文」并「确认通读并批准」——服务端把这次确认绑定到你读到的那一版正文的哈希；章按顺序逐章定稿。未接受的模型候选不会进入后续提示词；目录不能直接伪造 `approved`，重开终稿必须填写原因，并由服务端级联撤销受影响的后续批准。
- `成本看板` 以 token 为主；只有在 `config/pricing.yaml` 里写了单价的模型才折算金额，其余标「未定价」。

## 本地快速启动

依赖：Python 3.12（用 uv 装锁定的虚拟环境）、Node.js ≥ 20.19（推荐 22）与 npm。后端的传递依赖已锁定并带下载哈希；不要在仓库根目录安装 npm 包（根目录不是 Node 项目）。

Linux（日常开发机）：

```bash
cd backend && uv sync --locked --extra dev && cd ..
cd frontend-react && npm ci && cd ..
scripts/start-all-linux.sh      # 后端 + React，日志与 pid 在 .codex-run/
scripts/stop-all-linux.sh
```

Windows：

```powershell
cd backend
uv sync --locked --extra dev
cd ..\frontend-react
npm ci
cd ..
.\start-dev.cmd
```

启动脚本会先执行 `alembic upgrade head`，随后启动后端与 React 前端；生产启动链路不会注入测试夹具或演示作品。默认地址：

- React：`http://127.0.0.1:5174`
- 后端：`http://127.0.0.1:8000`
- 存活检查：`http://127.0.0.1:8000/live`
- 就绪检查：`http://127.0.0.1:8000/ready`

Windows 脚本在端口被占用时会往上找可用端口，并把实际地址写入 `.codex-run/backend.url`；停止与重启用 `.\stop-dev.cmd`、`.\restart-dev.cmd`。Linux 的一键启动 `scripts/start-all-linux.sh` 与前端启动脚本 `scripts/start-frontend-linux.sh` 在 Node 版本低于 18.18、找不到 node / npm 或缺 `node_modules` 时直接说明该装什么并退出，检查在停掉任何服务之前做。

需要主动升级 Python 依赖时，修改 `backend/pyproject.toml` 后用同一版 uv 重新生成并审查 `uv.lock` 与带哈希的导出锁文件（只导出 dev 附加依赖）：

```bash
cd backend
uv lock --python 3.12
uv export --locked --extra dev --no-emit-project --format requirements-txt --output-file requirements.lock
```

## 推荐创作路径

1. 从作品切换器选择 `新建作品`，填写书名、题材、一句话简介、目标字数和主色。
2. 进入 `构思`，逐步生成、编辑、保存并确认雪花十步。
3. 完成场景列表与场景规划后在第 10 步运行「AI 分诊」，再逐场给出你的裁定：`通过 / 需修补 / 该重写 / 待删`（该重写与待删的场整理时不建场景卡，但不阻断其余场景）。
4. 使用 `整理章节结构` 预览分章并 `确认写入`，把已确认内容物化为章与场景卡。
5. 直接进入 `写作`、在 `AI 起草台` 逐场生成，或在高级 `章节编排` 中运行当前整章并观察持久化进度。
6. 人工审阅 AI 候选；提升权威正文时按准确的内容安全发现码逐项确认。
7. 在 `成稿中心` 的「正史」页签核对正文事实候选，完成每场提交后再通读当前服务端正文并批准终稿；需要重开时填写可审计原因。文学质量提示始终需要作者判断。

雪花步骤允许带原因跳过，但读者定位、一句话概括、一段话概括、场景列表和场景规划是结构物化前的硬检查项。完整十步依次为：读者定位、一句话概括、一段话概括、角色摘要表、一页梗概、角色背景故事、长篇大纲、角色全档案、场景列表、场景规划。各步与 Ingermanson 雪花写作法的逐条对照、字段与有意的差异见 [雪花方法契约](docs/snowflake-method-contract.md)。

## 数据库与迁移

当前代码要求的 Alembic 版本以 `backend/src/novel_system/db/schema_contract.py` 的 `CURRENT_SCHEMA_REVISION` 为准，启动脚本每次启动都会先升级。不可逆的迁移（`20260716_0073`、`20260904_0083`，以及只改数据的 `20260924_0092`、`20260929_0093` 等）升级前必须停服备份；各版本做了什么、哪些不可逆、库与代码对不上时会看到什么、重构上线的部署顺序都在 [数据库迁移](docs/migrations.md)。

```bash
cd backend
.venv/bin/python -m alembic current
.venv/bin/python -m alembic upgrade head
```

（Windows 用 `.\.venv\Scripts\python.exe`。）`/live` 只表示进程存活；`/ready` 还会检查数据库连接、迁移版本和必需结构，部署探针应使用两者的不同语义。数据库结构落后于代码时，界面与每个 `/api/*` 请求都会得到「数据库结构需要升级：请重启后端（启动脚本会自动升级）」。

## 网络与令牌

后端默认 `NOVEL_SYSTEM_LOCAL_ONLY=true`，只接受回环请求并拒绝转发头。若明确需要远程访问，必须同时设置：

```powershell
$env:NOVEL_SYSTEM_LOCAL_ONLY = "false"
$env:NOVEL_SYSTEM_REMOTE_ACCESS_TOKEN = "使用足够长的随机值"
$env:NOVEL_SYSTEM_CORS_ORIGINS = "https://你的前端域名"
```

启动脚本仍只监听 `127.0.0.1`；以上变量不会自动把端口暴露到网络。远程部署还需要单独配置监听地址或可信反向代理，并遵守运行安全文档中的边界。

浏览器客户端通过 `X-Novel-Access-Token` 发送令牌；可用 `VITE_NOVEL_SYSTEM_ACCESS_TOKEN` 注入默认值，运行时值只保存在 `sessionStorage`。这是共享访问令牌，不是用户登录、RBAC 或租户隔离；构建进前端的值也不能视为对浏览器用户保密。

完整的网络、用量读数、内容复核、路径导入和恢复边界见 [运行安全与资源边界](docs/runtime-safety.md)；正文事实的候选、复核和提交规则见 [正史连续性与长篇记忆](docs/canon-continuity.md)；雪花十步、场景形态与闸门对照原著的契约见 [雪花方法契约](docs/snowflake-method-contract.md)。

完整文档入口、维护状态和历史资料边界见 [文档导航](docs/README.md)。日常使用以 [操作手册](docs/operator-manual.md) 为准；日期化的计划、证据和进度记录只说明当时状态，不替代 README、操作手册和运行时契约。

## 恢复与数据重置

服务启动时会恢复可安全重放的场景与章节任务（运行中每分钟再巡检一次），持久化租约用于避免重复接管；风格参考的段落分类、学习文风与对照检查是作业表上的持久作业，常驻清扫线程把心跳过期的作业放回队列接着跑。写作界面的 `同步与恢复` 会收集浏览器本地的冲突稿、没存上服务端的稿，可比较、导出、重试或恢复。

浏览器恢复记录不是服务端备份，清理站点数据、换浏览器/设备、无痕模式或存储配额耗尽都可能令其不可用。

数据库备份与恢复必须在服务停止后进行；工具会校验旁路清单、SHA-256、SQLite 完整性与外键。可用 `scripts/db_backup_drill.ps1`（Windows）或 `scripts/db_backup_drill.sh`（Linux）在临时副本上演练，详细命令见 [运行安全与资源边界](docs/runtime-safety.md)。

`reset_author_state` 会批量删除作者态项目与运行产物（参考书、文风画像与系统配置保留），不属于首次启动步骤。只有在已有数据库备份且确认要清空作者态时才执行：

```bash
cd backend
.venv/bin/python -m novel_system.tools.reset_author_state                  # 只看会删什么
.venv/bin/python -m novel_system.tools.reset_author_state --execute --yes
```

## 验证

```bash
cd frontend-react
npm run lint
npm test
npm run build
```

```bash
cd backend
for i in 0 1 2 3; do .venv/bin/python scripts/pytest_shard.py --shard-index $i --shard-count 4 -- -q; done
```

四片与 CI、`scripts/verify_windows.ps1` 使用同一分片规则，全部通过才等价于后端全量通过（小机器上要半小时左右）。

React 主线的浏览器契约验收会使用隔离 SQLite 数据库和中性测试夹具，不会接触日常开发库（默认后端 `:8009`、React `:5176`）：

```bash
NOVEL_SYSTEM_PYTHON=$PWD/backend/.venv/bin/python bash scripts/verify_react_e2e.sh
```

Windows 的等价入口是 `powershell -ExecutionPolicy Bypass -File scripts/verify_react_e2e.ps1`。GitHub Actions 在每次推送与 PR 上运行后端质量门、后端四片测试、React lint / 单测 / 构建和 React 契约 E2E；发布前的完整核对见 [发布检查清单](docs/release-checklist.md)。

关键代码入口：

- 前端导航：`frontend-react/src/ws-nav.js`（导航模型）、`frontend-react/src/ws-app.jsx`（路由与懒加载）
- 雪花工作台：`frontend-react/src/ws-snow.jsx`、同步层 `frontend-react/src/ws-snow-sync.jsx`
- 写作与本地恢复：`frontend-react/src/ws-writer.jsx`、`frontend-react/src/wr-doc-store.jsx`
- API 客户端：`frontend-react/src/lib/client.js`
- 后端应用与健康检查：`backend/src/novel_system/api/app.py`、`backend/src/novel_system/api/readiness.py`
- 场景执行与归档：`backend/src/novel_system/api/routes/scenes.py`、`backend/src/novel_system/services/orchestrator.py`
- 风格参考：`backend/src/novel_system/services/style_reference/`、`backend/src/novel_system/services/style_policy.py`、`backend/src/novel_system/services/reference_copy_gate.py`（说明见 [风格参考](docs/style-reference.md)）
- 开发约定（命令、守卫、测试约定、易错点）：[CLAUDE.md](CLAUDE.md)；前端工程说明：[frontend-react/README.md](frontend-react/README.md)
