# 发布检查清单

改动推上 `main` 之前、以及把新版本部署到作者的安装之前，照这份清单核对。仓库直接提交到 `main`，没有草稿 PR 流程；
GitHub Actions 在每次推送（以及 PR）上跑同一套检查。

## 1. 自动检查（`.github/workflows/ci.yml`）

- **Backend Quality Gates**：带哈希安装 `requirements.lock` → `pip check` → `ruff check src tests` → `pip-audit` →
  `tests/test_service_architecture.py` 与 `tests/test_dependency_lock.py`。
- **Backend Tests**：后端全量按 `backend/scripts/pytest_shard.py` 分四片（按 `tests/.durations.json` 的耗时装箱），每片一份 JUnit。
- **Frontend Tests (React mainline)**：`npm ci` → `npm audit --omit=dev --audit-level=high`（生产依赖有高危公告就红）→
  `npm run lint`（ESLint：`react-hooks/rules-of-hooks` 报错即红，`exhaustive-deps` 只警告）→ vitest → `npm run build`。
- **React Contract E2E**：全新迁移的隔离库 + 注入中性夹具的后端 + React 开发服务器 + 真 Chromium，跑
  `frontend-react/scripts/run-smokes.mjs` 的全部套件（`bash scripts/verify_react_e2e.sh`）。

四项全绿才算可以部署。

## 2. 本机检查

- Linux（日常开发机）：
  - 后端：`cd backend && .venv/bin/python scripts/pytest_shard.py --shard-index N --shard-count 4 -- -q`（N = 0…3，四片全过
    = 全量；小机器上要半小时左右，放后台跑）；两个 pytest 同时跑时各给一个 `--basetemp`。
  - 前端：`cd frontend-react && npm run lint && npm test && npm run build`。
  - 契约 E2E：`NOVEL_SYSTEM_PYTHON=$PWD/backend/.venv/bin/python bash scripts/verify_react_e2e.sh`（默认后端 `:8009`、React
    `:5176`，端口已被占用时直接拒绝；从不碰开发用的 `:8000` / `:5174`）。
- Windows：`powershell -ExecutionPolicy Bypass -File scripts/verify_windows.ps1`（ruff、pip-audit、四片后端、vitest、构建；
  分片 JUnit 写在 `backend/.test-results/`），`scripts/verify_react_e2e.ps1`，或一次跑完两者的 `scripts/verify_release.ps1`。
- 只改了一个领域时，至少跑该领域的测试文件与漂移守卫（`tests/test_service_architecture.py`、`tests/test_metadata_isolation.py`、
  `tests/test_schema_contract_revision.py`、`tests/test_docs_links.py`、`tests/test_prompt_template_contracts.py` 等，清单见
  [CLAUDE.md](../CLAUDE.md) 的「Drift guards」）；动了前端就带上五个前端守卫测试。

## 3. 按改动类型补的检查

- **迁移**：照[数据库迁移](migrations.md)写；`CURRENT_SCHEMA_REVISION` 改成新的 head；迁移测试升到自己的版本；
  `tests/test_metadata_isolation.py` 绿；不可逆或只改数据的迁移在说明里写清楚，部署前必须停服备份。
- **提示词**：`config/prompts.yaml` 里改过的模板都升了 `version`；部署时在保存过提示词快照的安装上跑
  `python -m novel_system.tools.sync_prompt_templates --execute`。
- **新的 LLM 节点**：默认值只写在 `backend/src/novel_system/services/llm_node_registry.py` 的 spec 里；配置过模型的安装要到
  「设置 · AI 模型」点「一键补齐」给它分路由，否则这个节点 fail-closed。
- **删接口**：加进 `backend/tests/test_retired_surface.py`，按需下调 `tests/test_api_openapi_contract.py` 的数量下限；React 不再调用它。
- **公开仓库**：新加的测试与文档只用中性的合成名字（林昭 / 雨城 / 旧信 …），不写作者真实作品的人名、地名与情节。

## 4. 部署到作者的安装

1. 停掉后端与前端（`scripts/stop-all-linux.sh` / `stop-dev.cmd`）。
2. `python -m novel_system.tools.db_backup --backup <库> <备份>` 做一份校验过的备份；涉及不可逆迁移或压缩库时，这份备份作为
   有名字的存档长期保留。
3. 代码快进到新版本；`alembic upgrade head`。
4. 按需：`compact_db <库> --execute`（重构上线那一次必须做，见[数据库迁移](migrations.md) §4）、`sync_prompt_templates --execute`、
   「一键补齐」。
5. 启动（`scripts/start-all-linux.sh` / `start-dev.cmd`），确认 `/ready` 是 `ready`；浏览器里开着的工作台页面全部刷新。

## 5. 不由这里证明的东西

本清单的检查全部跑在假供应商与中性夹具上（`NOVEL_SYSTEM_LLM_ENABLED=false` 或测试替身）。真实供应商的生成质量、文学质量、
内容政策、版权与长篇耐久要单独记录证据，不能由离线回归代替。
