# frontend-react — 创作工作台

Vite + React 18 前端，默认端口 `5174`。仓库根目录的 `start-dev.cmd`（Linux：`scripts/start-all-linux.sh`）一次拉起后端与本工程。

## 当前产品入口

默认路由是 `#home`。作家模式显示主页、构思、写作、风格、待办、资料；高级模式另外显示章节编排、AI 起草台、成稿中心、文学质量和成本看板。设置、回收站、同步与恢复、主题切换和「排版与舒适度」固定在侧栏底部，任何高度下都看得到。`⌘K`（Windows / Linux 为 `Ctrl+K`）打开命令面板，可跳到任一页面或任一场景。

界面能力边界：

- 新建作品、雪花十步、结构物化、逐场 AI 起草、写作草稿同步和权威正文提升都连接真实 API；没有演示作品，也没有离线假生成——模型未配置时明确提示去「设置 · AI 模型」。
- 高级章节编排中的 `运行本章` 连接持久化章节任务，防重复启动并轮询进度；阻断和失败原样呈现。
- 成稿中心按「通读当前正文并绑定哈希 → 项目级终稿批准」两步执行；重新打开终稿需要原因，并由服务端撤销该章及其后的批准链。
- 写作房间的内容安全复核按当前响应中的精确发现码重新确认；旧确认不会自动覆盖新发现。
- 批注只保存在本机浏览器（`wr-anno:*`），不进草稿、不上服务器。
- 键盘：快捷键只作用于最上层——确认框、命令面板、作品切换这类模态层开着时，`⌘K` 和写作台的 `⌘J` / `⌘.` / `⌘1` / `⌘2` 都不响应，Esc 只关最上面那一层；输入法组字中的回车 / Esc 不当命令。写作台的 AI 续写托盘是模态的（焦点关在托盘里），候选快捷键 1 / 2 / 3 / 回车 / R 只在焦点停在候选栏上时生效；选中正文后按 `Alt+F10` 进入改写工具条（←→ 移动，Esc 回到正文、选区还在）。

## 命令

```powershell
cd frontend-react
npm ci             # 按 package-lock.json 装精确版本
npm run dev        # http://127.0.0.1:5174
npm test           # Vitest（含 tooling-independence / design-guard 等守卫测试）
npm run build      # dist/
```

## API 配置

- `VITE_NOVEL_SYSTEM_API_BASE`：后端地址，默认 `http://127.0.0.1:8000`。
- `VITE_NOVEL_SYSTEM_ACCESS_TOKEN`：远程模式的共享访问令牌默认值。
- `setRemoteAccessToken()`：集成层可写入当前标签页会话的令牌；值保存在 `sessionStorage`，不会写入 `localStorage`。

所有公共 API 请求会在存在令牌时发送 `X-Novel-Access-Token`。Vite 环境变量会进入浏览器构建产物，因此该令牌只能作为受控环境的共享门槛，不能替代用户认证或被当成浏览器端秘密。

localStorage 的 `novel-system-api-base` 覆盖只有在同时存在 `novel-system-api-base-default` 时才对回环地址生效；做浏览器探针时请另起端口并用 `VITE_NOVEL_SYSTEM_API_BASE` 指向副本库，不要占用作者正在用的 `5174`。

## 同步与恢复

写作草稿会携带服务端当前权威版本标识，避免在版本已变化时静默覆盖。离线、`409` 冲突、配额失败或 AI 覆盖风险会留下浏览器本地恢复记录；侧栏底部的「同步与恢复」支持差异查看、导出、重试、恢复和删除，新记录产生时会有一条带「打开」的提示。

这些记录局限于当前浏览器配置文件和站点存储，不是跨设备备份。清理站点数据、无痕会话结束或存储失败后可能丢失。

## 结构约定

- `src/` 是持续维护的正式源代码；早期设计原型和一次性迁移脚本已经退役，不要从历史提交重新生成或覆盖现有实现。
- **共享界面层**（2026-09-21 重构）：设计令牌在 `src/styles.css`（语义色 `--accent/--ok/--warn/--danger/--info` 及其 `-wash` / `-ink`、`--scrim`、`--on-accent`、`--z-*` 层级刻度、`--fs-*` 字号刻度）；共用组件在 `src/ws-ui.jsx` + `src/ws-ui.css`（页头、分段、页签、标签、提示条、空态、统计块、图标按钮等）；模态框用 `src/ws-dialog.jsx`；提示与确认用 `src/ws-notify.jsx`（`wsToast` / `wsConfirm`）；界面偏好的范围与默认值只在 `src/ws-prefs.js` 定义一份；章 / 场标签与章节状态词汇在 `src/ws-labels.js`。新代码与改到的地方用这些，不要再在视图里写私有的同类控件。
- `src/main.jsx` 的样式导入顺序具有层叠语义：`styles.css`、`ws-ui.css` 在最前（`design-guard.test.js` 会检查），各视图的样式在后；调整时必须做视觉回归。
- `window.*` 与部分同步 store 是现有运行时兼容接缝，新增代码优先使用模块导出；`tooling-independence.test.js` 禁止新的 ESM 模块写入 `window`。
- API 错误统一为 `ApiRequestError`；界面应优先使用稳定 `code` 和 `details`，不要解析错误文案。
- 界面文案不出现英文大写小标题、原始 JSON、内部 id 或英文枚举值；夜间主题下的文字颜色只用令牌。

## 关键文件与回归

- 壳层：`src/ws-app.jsx`（路由与懒加载入口）、`src/ws-nav.js`（导航模型）、`src/ws-rail.jsx`（侧栏）、`src/ws-work-switcher.jsx`、`src/ws-palette.jsx`
- 作品与远端状态：`src/ws-works.jsx`
- 雪花主线：`src/ws-snow.jsx`（入口）与 `src/ws-snow-*.jsx|js`、`src/ws-snow-sync.jsx`
- 写作与恢复：`src/ws-writer.jsx`（入口）与 `src/ws-writer-*.jsx|js`、`src/wr-doc-store.jsx`、`src/wr-recovery-center.jsx`
- 目录（章 / 场的唯一交接面，场景 sid = 后端 `scene_id`）：`src/ws-catalog.jsx`；共用场景设计卡 `src/ws-scene-design.jsx`、场景卡同步状态 `src/ws-design-sync.jsx`（契约见 [`../docs/book-spine-catalog-contract.md`](../docs/book-spine-catalog-contract.md)）
- AI 起草台（左栏是全书书脊）：`src/ws-scene.jsx` 与 `src/ws-scene-*.jsx|js`、`src/ws-scene-run.jsx`
- 章节编排：`src/ws-author.jsx` 与 `src/ws-author-*.jsx|js`；章节运行 `src/ws-chapter-run.jsx`
- 成稿中心：`src/ws-manuscripts.jsx` 与 `src/ws-manuscripts-*.jsx|js`、`src/ws-manuscripts-store.jsx`
- 风格参考：`src/ws-styleref.jsx` 与 `src/ws-styleref-*.jsx|js`（状态与接口在 `src/ws-styleref-store.js`）
- 资料 / 回收站 / 设置：`src/ws-library*.jsx`、`src/ws-trash.jsx`、`src/ws-settings*.jsx`
- API 客户端：`src/lib/client.js`

核心回归至少包括：

```powershell
npx vitest run src/tooling-independence.test.js src/build-chunking.test.js src/design-guard.test.js src/frontend-boundary-contract.test.js src/runtime-truth-contract.test.js
npx vitest run src/ws-works.test.jsx src/ws-snow.test.jsx src/ws-snow-sync.test.jsx
npx vitest run src/wr-doc-store.test.jsx src/wr-recovery-center.test.jsx src/ws-writer-content-safety.test.jsx
npx vitest run src/ws-scene-run.test.jsx src/lib/client.test.js
npx vitest run src/ws-catalog.test.jsx src/ws-book-spine.test.jsx src/ws-scene-spine.test.jsx src/ws-writer-spine.test.jsx
npx vitest run src/ws-chapter-run.test.jsx src/ws-manuscripts.test.jsx src/ws-manuscripts-flow.test.jsx
npm run build
```

仓库级 React 契约 E2E 由根目录脚本启动隔离后端、前端并自动清理。请从仓库根目录运行（Linux：`NOVEL_SYSTEM_PYTHON=$PWD/backend/.venv/bin/python bash scripts/verify_react_e2e.sh`，端口可用 `PLAYWRIGHT_BACKEND_PORT` / `PLAYWRIGHT_REACT_PORT` 改）：

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\verify_react_e2e.ps1
```

运行安全细节见 [`../docs/runtime-safety.md`](../docs/runtime-safety.md)。
