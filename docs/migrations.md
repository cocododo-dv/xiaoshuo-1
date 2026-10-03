# 数据库迁移

数据库结构只由 Alembic 迁移建立与升级（`backend/alembic/versions/`）；应用自己从不建表。当前代码认的版本是
`backend/src/novel_system/db/schema_contract.py` 的 `CURRENT_SCHEMA_REVISION`，它必须等于唯一的 Alembic head
（`cd backend && python -m alembic heads`；`tests/test_schema_contract_revision.py` 钉着两者相等）。其他文档不再各写一遍 head。

## 1. 升级一个已有的库

启动脚本（`start-dev.cmd`、`scripts/start-backend-linux.sh` / `start-all-linux.sh`、React 契约 E2E 通道）每次启动都先跑
`alembic upgrade head`，库已是最新时什么都不做。手工升级：

```bash
cd backend
.venv/bin/python -m alembic current
.venv/bin/python -m alembic upgrade head
```

（Windows 用 `.\.venv\Scripts\python.exe`。在共用 venv 的 git worktree 里，命令前加 `PYTHONPATH=<该检出>/backend/src`；
Alembic 的 `env.py` 与每个 `novel_system.tools.*` 都会拒绝导入另一个检出的代码，工具以退出码 2 退出。）

- **先停服、再备份。** 库是 WAL 模式，后端在跑时直接复制文件会漏掉还在 `novel_system.db-wal` 里的已提交写入。备份用
  `python -m novel_system.tools.db_backup --backup <源库> <备份>`（另有 `--verify PATH`、`--restore BACKUP DST`；写旁路清单、
  核对 SHA-256、完整性与外键），演练脚本 `scripts/db_backup_drill.sh` / `.ps1` 只动临时副本。下表里不可逆的迁移升级前必须备份。
- **外键核对。** 一次真正应用了迁移的运行在第一个迁移之前、最后一个之后各扫一遍 `PRAGMA foreign_key_check`：本次运行新造出
  违反外键的行就失败（退出码非 0，启动脚本随之停下，后端不会起来）；库里原本就有的违反只记警告。SQLite 每个迁移各自提交，
  失败时这些迁移已经应用、再跑一次也不会再查——先用升级前的备份 `db_backup --restore`，或修好报出来的行，再启动后端。
  库已是最新的 `upgrade head`（每次启动都是）与 `alembic current` 不扫。
- **库与代码对不上时。** `/ready` 与每个 `/api/*` 请求都回 503 `SERVICE_NOT_READY`：库落后于代码是
  「数据库结构需要升级：请重启后端（启动脚本会自动升级）」（`details.reason = schema_revision_mismatch`，缺表 / 缺列是
  `schema_tables_missing` / `schema_columns_missing`）；库比代码新（换回了旧代码）是 `schema_revision_ahead`，
  「请换回与数据库匹配的代码版本」，重启不会让库降级。不重启、原地 `alembic upgrade head` 也可以：结构跟上之后的第一个
  `/api/*` 请求或 `/ready` 探测会补跑启动时推迟的启动恢复与后台清扫。`--reload` 的开发后端在迁移落地之前就会热加载新模型，
  这段时间看到的就是这个 503。
- 会写库的运维工具（`reset_author_state`、`sync_prompt_templates`、`raise_llm_output_budget`、`refresh_style_reference_books`、
  `purge_style_reference_books`）在库版本与代码不一致时拒绝 `--execute`（退出码 2，与 `/ready` 同一条标准）；干跑照常。
- `20260802_0077` 合并过两条曾经发布过的分支（`20260717_0074 → 20260717_0075` 与 `20260722_0074 → 20260725_0076`）：停在
  任一分支上的库都直接 `alembic upgrade head`，不要手改 `alembic_version`。
- `20260523_0036` 只在旧 `reference_learning` 表里真有行时要求 `backups/style_reference_legacy_*.json`（先导出再删）；新库与
  没有这些行的库直接升级。`STYLE_REFERENCE_REPO_ROOT` 只是这道守卫的测试覆盖（`tests/test_style_reference_schema.py`）。

## 2. 写一个新迁移

1. 文件 `backend/alembic/versions/<日期>_<四位序号>_<说明>.py`，`down_revision` 接当前 head。
2. 写显式 DDL / SQL，**不 import 应用的 ORM 或服务**（历史迁移是冻结的；`tests/test_metadata_isolation.py` 会查）。
   数据迁移要幂等、能在 400 多 MB 的真实库上跑完；SQLite 删列 / 改约束走整表重建（`batch_alter_table`）。
3. ORM 在 `backend/src/novel_system/db/models/` 同步改；只靠迁移建的索引在模型的 `__table_args__` 里声明。
   `test_metadata_isolation.py` 用 `create_all` 与 `alembic upgrade head` 各建一次库、逐表逐列逐索引比对——测试套件用
   `create_all` 建库，漏写迁移只会在这里变红，否则要到运行时才 `no such column`。
4. **把 `CURRENT_SCHEMA_REVISION` 改成新 revision。** 忘了改，每个升到新 head 的安装都会一直报
   `schema_revision_mismatch`、启动脚本等不到 `/ready`（`20260913_0084` 出过一次）；现在 `test_schema_contract_revision.py`
   让测试先红。
5. 迁移测试升到**自己的** revision（`tests/support/migrations.py` 的 `migrate(path, revision, monkeypatch)` 只经 Alembic 建库），
   不要升到 `head`——之后的迁移会让断言失效。读 `CURRENT_SCHEMA_REVISION` 的老测试（0077 / 0080 / 0081 / 0082 / 0085 / 0086）
   随常量走。
6. 加了表就看三份清单守卫：`tests/test_trash_purge_completeness.py`（能指向作品对象的表要在永久清除里做选择）、
   `tests/test_reset_author_state.py`（每张表要么是作者态重置的目标，要么写进 `PRESERVED_TABLES`）、`tests/test_scene_rehome.py`
   （同时带 `scene_id` 与 `chapter_id` 的表要在场景换章里做选择）。

## 3. 各版本

「可逆」指 `alembic downgrade` 能不能回到上一版的**结构**；删掉的数据在任何情况下都回不来。

| 版本 | 做了什么 | 可逆 | 部署注意 |
|---|---|---|---|
| `20260716_0073` | 历史 LLM 审计里的提示词、草稿、模型输出、供应商错误正文改写成有界指纹；账本数值与哈希不动 | 否 | 先备份 |
| `20260717_0074` / `20260717_0075` | （real-only 分支）退役演示作品与离线生成配置；非人工的评测证据删除 | 否 | 由 0077 合并 |
| `20260722_0074` | 系统配置快照里机器写下的 30 秒 LLM 超时上限去掉（作者手写的值不动） | 否（空操作） | |
| `20260725_0075` | 雪花场景 `scene_id` 防撞号、场景计划软删除 | 是 | |
| `20260725_0076` | 构思侧章表 `snowflake_chapter_plans`，场景计划的 `chapter_plan_id` | 是 | |
| `20260802_0077` | 合并上面两条分支；修好并强制章表归属的外键 | 否 | |
| `20260802_0078` | 运行时产物的归属外键、活跃章 / 场序的唯一索引（先修再强制） | 否 | |
| `20260802_0079` / `0080` | 场景作者笔记、深评偏好（乐观锁修订号） | 是 | |
| `20260805_0081` | 外键查找列的索引 | 是（外键保留） | |
| `20260818_0082` | 正史核对的提交 / 候选 / 连续性快照 | 是 | |
| `20260904_0083` | 删掉 2026-09 减法退役功能的表（结果治理、知识提升、长篇控制塔、叙事旁注等） | 否 | 先备份 |
| `20260913_0084` | `snowflake_scene_plans.rendering_mode`（完整场 / 概述），历史行回填 `full` | 是 | |
| `20260914_0085` | 场景计划 `expected_reader_emotion` / `story_time` | 是 | |
| `20260915_0086` | 场景计划 `exception_reason`（破例理由） | 是 | |
| `20260916_0087` | `snowflake_direction_briefs`：每步的作者意图要点 | 是 | |
| `20260917_0088` | 教练回合 `turn_kind`（回填 `chat`）/ `candidates_json` / `brief_delta_json` / `adoption_json` | 是 | |
| `20260920_0089` | `snowflake_chapter_plans.catalog_chapter_id`：章在目录里的 id 钉在章计划上，能确定的已有章一次钉好 | 是 | |
| `20260923_0090` | 风格参考 v3 四张表：作业、窗口索引、读数、每场冻结选窗 | 是 | |
| `20260923_0091` | 删旧回测报告表、发现反馈表与 `base_confidence` 列 | 结构可回 | 先备份 |
| `20260924_0092` | 只改数据：绑定配置回填成 v3 四键、v3 之前的画像归档、旧版风格待办行删除 | 否（空操作） | 先备份 |
| `20260929_0093` | 只改数据：雪花作品手加的人物 id 加作品前缀（`<作品>_c1`），引用与审批快照签名同步换算；每部改过的作品记一条操作日志 | 否（空操作） | 先备份 |
| `20260929_0094` | 只改数据：旧抽取流程遗留的「运行中」run、没有作业可续的「分类中」书标为失败——开机不再扫这两样 | 否（空操作） | |
| `20260929_0095` | 诊断 / 深评 / 作者稿逐场查询的四个索引（已存在的同名索引跳过） | 是 | |
| `20260929_0096` | 成本看板的三个记账索引；活动 models 快照瘦身：只留作者为每个节点选的服务与模型，另存一版激活，旧版留在历史 | 是（还原活动快照） | |
| `20260929_0097` | 只改数据：各作品活跃章的 `display_order` 一次压实成 1..n（有终审章的作品不动），目录读取从此不写库 | 否（空操作） | |
| `20260929_0098` | 删声线卡 / 关系卡两张表与 13 个没有读写者的死列；万一卡片表还有行，先导出到库文件旁的 `<库文件>.0098-voice-relation-cards.json`（不入仓库） | 结构可回 | 先备份 |

更早的版本只在 `backend/alembic/versions/` 的模块说明里。

## 4. 2026-09-29 重构的部署顺序

0092–0098 与重构的代码一起上线（作者的安装还在 0091：2026-09-24 的风格参考「清理与优化」在 origin/main 上、从没部署过，迁移 0092 与它的界面变化这次一起上线）；后端与前端必须是同一版（成本看板以 token 为主的接口与看板界面、07 章表只读的后端与构思界面
互相配套，都已在这一版里，不能只换一半）。顺序：

1. 停掉后端与前端（`stop-dev.cmd` / `scripts/stop-all-linux.sh`）。
2. `db_backup --backup` 做一份校验过的备份，**作为有名字的存档永久保留**：0096 之后的压缩会清掉幂等重放缓存，其中一些构思
   草稿的中间状态只存在于那里。
3. 代码快进到新版本，在 `backend/` 下手工 `alembic upgrade head`（不要等启动脚本去升级：第 4 步要在后端启动之前做）。
4. `python -m novel_system.tools.compact_db <库文件>` 先干跑（报告库多大、会清掉多少行），再加 `--execute`：清掉过期的幂等重放缓存，
   把 `auto_vacuum` 切成 INCREMENTAL，`VACUUM`，完整性与外键检查，截断 WAL。库还在被使用时它拒绝执行；需要约一份库大小的空闲磁盘。
5. 保存过提示词快照的安装：`python -m novel_system.tools.sync_prompt_templates`（干跑）再 `--execute`，把这一轮改过的模板同步进库。
6. 启动，确认 `/ready` 返回 `ready`；每台设备上打开着的工作台页面，在新界面里改任何东西之前全部刷新一次：前后端一起换了版本，没刷新的旧页面还在调用已经删掉的接口，它手里的旧构思缓存也可能盖掉你在新界面里的改动。

之后幂等重放缓存按 72 小时保留期每 6 小时清理一次（启动后的第一拍不清），风格参考作业记录按保留期每天清理一次。
