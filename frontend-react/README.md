# frontend-react — 创作工作台

Vite + React 18 前端，默认端口 `5174`。仓库根目录的 `scripts/start-all-linux.sh`（Windows：`start-dev.cmd`）一次拉起后端与本工程。需要 Node.js ≥ 18.18（`package.json` 的 `engines`；推荐 22，CI 用 22）。

## 当前产品入口

默认路由是 `#home`。作家模式显示主页、构思、写作、风格、待办、资料；高级模式另外显示章节编排、AI 起草台、成稿中心、文学质量和成本看板。设置、回收站、同步与恢复、主题切换和「排版与舒适度」固定在侧栏底部，任何高度下都看得到。`⌘K`（Windows / Linux 为 `Ctrl+K`）打开命令面板，可跳到任一页面或任一场景。

界面能力边界：

- 新建作品、雪花十步、结构物化、逐场 AI 起草、写作草稿同步和权威正文提升都连接真实 API；没有演示作品，也没有离线假生成——模型未配置时明确提示去「设置 · AI 模型」。
- 高级章节编排中的 `运行本章` 连接持久化章节任务，防重复启动并轮询进度；阻断和失败原样呈现。
- 成稿中心的终稿批准是一个对话框：勾选「我已从头到尾通读当前正文」再「确认通读并批准」，服务端把确认绑定到读到的正文哈希；重新打开终稿需要原因，并由服务端撤销该章及其后的批准链。
- 写作房间的内容安全复核按当前响应中的精确发现码重新确认；旧确认不会自动覆盖新发现。
- 批注只保存在本机浏览器（`wr-anno:*`），不进草稿、不上服务器。
- 成本看板以 token 为主；只有在 `config/pricing.yaml` 写了单价的模型才折算金额，其余标「未定价」。
- 键盘：快捷键只作用于最上层——确认框、命令面板、作品切换这类模态层开着时，`⌘K` 和写作台的 `⌘J` / `⌘.` / `⌘1` / `⌘2` 都不响应，Esc 只关最上面那一层；输入法组字中的回车 / Esc 不当命令。写作台的 AI 续写托盘是模态的（焦点关在托盘里），候选快捷键 1 / 2 / 3 / 回车 / R 只在焦点停在候选栏上时生效；选中正文后按 `Alt+F10` 进入改写工具条（←→ 移动，Esc 回到正文、选区还在）。

## 命令

```bash
cd frontend-react
npm ci             # 按 package-lock.json 装精确版本
npm run dev        # http://127.0.0.1:5174
npm run lint       # ESLint：react-hooks/rules-of-hooks 报错即失败，exhaustive-deps 只警告，过期的 eslint-disable 注释本身报错
npm test           # Vitest（含 tooling-independence / design-guard 等守卫测试）
npm run build      # dist/
```

Vitest 分两个项目（`vitest.config.js`）：`dom` 用 jsdom，是默认，`src/test-setup.js` 在这里统一打开 React 的 act 环境、每个用例后清空 localStorage / sessionStorage，单条用例上限 15 秒；`node` 只跑 `NODE_TESTS` 里列出的纯逻辑与读源码的守卫测试（不碰 DOM，省掉 jsdom 装配）。新测试默认进 `dom`，确认它和它 import 的模块都不碰 DOM 再加进 `NODE_TESTS`。store 测试 `vi.mock("./lib/client.js")`、用 `src/test-helpers.js` 的 `installApiRouter` 按 URL 回包，`settleActiveWork` / `settleCatalog` 等作品与目录落定，再用 `vi.resetModules()` + 动态 import 让每个 store 单独加载。

## API 配置

- `VITE_NOVEL_SYSTEM_API_BASE`：后端地址，默认 `http://127.0.0.1:8000`。`index.html` 的 CSP `connect-src` 默认只放行本机回环；这个地址不是 http 回环时（内网地址、https 反向代理），`build-csp.js` 的 Vite 插件在 dev 与 build 时把它的 origin 补进 `connect-src`。
- `VITE_NOVEL_SYSTEM_ACCESS_TOKEN`：远程模式的共享访问令牌默认值。
- `setRemoteAccessToken()`：集成层可写入当前标签页会话的令牌；值保存在 `sessionStorage`，不会写入 `localStorage`。

所有公共 API 请求会在存在令牌时发送 `X-Novel-Access-Token`；每个写请求都带 `X-Idempotency-Key`（后端缺键就 400）。Vite 环境变量会进入浏览器构建产物，因此该令牌只能作为受控环境的共享门槛，不能替代用户认证或被当成浏览器端秘密。

localStorage 的 `novel-system-api-base` 覆盖只有在同时存在 `novel-system-api-base-default` 时才对回环地址生效（设好两个键后刷新页面）；做浏览器探针时请另起端口并用 `VITE_NOVEL_SYSTEM_API_BASE` 指向副本库，不要占用作者正在用的 `5174` 与 `8000`。

## 同步与恢复

写作台的正文按场是一台保存状态机（`src/wr-doc-sync.js`，本机一层在 `src/wr-doc-cache.js`，对外是 `src/wr-doc-store.jsx` 的 `WrDocs`）：

- 一场同一时刻只有一次保存在路上，发出去的永远是最新的一稿；存不上（断网、5xx）的字留在本机、显示「草稿保存失败」，之后每一次保存、窗口重新聚焦、联网或离开这一场时重发。
- 服务端回 `409`（这一场在另一台设备或另一个标签页里改过）时，编辑器换成服务端的版本，本机没存上的每一稿都放进「同步与恢复」并提示——两边的字都不丢；服务端版本还读不到时这一场暂不保存。服务端明确拒绝的保存（章已批准锁定等）同样先把字留进「同步与恢复」。
- 重新打开一场时会向服务端核对一次；「同步与恢复」里的「恢复」先与服务端对齐再写。订阅走 `WrDocs.subscribe`（`state` / `loaded` / `conflict-resolved`），另有 `WrDocs.flush` / `keepLocalCopy`。两个标签页同时开着时的保证写在 `wr-doc-sync.js` 的文件头。

侧栏底部的「同步与恢复」支持差异查看、导出、重试、恢复和删除，新记录产生时会有一条带「打开」的提示；每一场最多留 20 条记录，超过时提示清理、不自动删。这些记录局限于当前浏览器配置文件和站点存储，不是跨设备备份。清理站点数据、无痕会话结束或存储失败后可能丢失。版本对比（成稿中心「对比」）的版本列表按页读，「更早的版本」接着往下取（`WrDocVersions`）。

## 结构约定

- `src/` 是持续维护的正式源代码；早期设计原型和一次性迁移脚本已经退役，不要从历史提交重新生成或覆盖现有实现。
- **store 一律是 ES 模块**，谁用谁 `import`：`WsWorks`（`ws-works.jsx`）、`WsCatalog`（`ws-catalog.jsx`）、`SnowSync`（`ws-snow-sync.jsx`）、`WrDocs` / `WrDocVersions` / `WrRecovery`（`wr-doc-store.jsx`）、`WsManuStore`（`ws-manuscripts-store.jsx`）、`WsDiagnosis`（`ws-diagnosis-summary.jsx`）、`WsAuthorAi`（`ws-author-ai-store.js`）、待办（`ws-review-store.js`）、资料库（`ws-library-store.js`：`libLive` / `useLibraryLive` / `libEnsureLoaded`）、回收站（`ws-trash-store.js`）、风格参考（`ws-styleref-store.js` 写操作，底座 `ws-styleref-store-core.js`、活动清单 `ws-styleref-store-activity.js`）、成本看板（`ws-cost-store.js`）等。它们是带同步缓存的 API store：先改界面、再等服务端，失败回滚或重拉；变化经各自的 `subscribe(fn)` 通知；每个 store 的契约写在它的文件头。跨视图的窗口事件（`ws:*`）经 `lib/events.js` 的 `emit` / `useWindowEvents` 走；其中 `ws:work-changed` 只在当前作品换了或书架成员变了时发，各 store 听到它就重拉本作品的一切——字数、书名这类变化只通知 `WsWorks.subscribe`，不发窗口事件。
- **只有 `src/ws-test-seam.js` 写 `window`**：开发态（`import.meta.env.DEV`）装 `window.__wsStores.load(...)`，给契约 E2E 冒烟拿 store 用；生产构建里这一段整个摇掉。`tooling-independence.test.js` 的 `KNOWN_WINDOW_WRITERS` 只剩它，别的模块写 `window`（连 `window.x ===` 这种读法也算）会让测试失败；`globalThis` 上只有 `lib/events.js` 的跨重载去重登记表。
- **共享界面层**：设计令牌在 `src/styles.css`（语义色 `--accent/--ok/--warn/--danger/--info` 及其 `-wash` / `-ink`、`--scrim`、`--on-accent`、`--z-*` 层级刻度、`--fs-*` 字号刻度）；共用组件在 `src/ws-ui.jsx` + `src/ws-ui.css`（页头、分段、页签、标签 `Tag`、提示条、空态、统计块、图标按钮、进度条 `ProgressBar`、卡片单选 `RadioCards`、非模态浮层 `Popover` / `usePopover`、菜单按钮 `MenuButton` 等）；模态框用 `src/ws-dialog.jsx`（浮层与模态框同一个层栈：Esc 归最上层）；提示与确认用 `src/ws-notify.jsx`（`wsToast` / `wsNotify`（提示层没挂时退回 alert）/ `wsConfirm`，构思页的确认框也走它）；界面偏好的范围与默认值只在 `src/ws-prefs.js` 定义一份；词表按领域住在 `src/labels/`（`catalog.js` 章 / 场叫法、章节状态与戏剧卡栏名 `DRAMA_FIELDS`、`review.js`、`llm.js` 模型节点、`style-reference.js`、`finding.js` 诊断发现、`canon.js` 正史、`library.js` 资料库类别），`src/ws-labels.js` 是转出门面。旧的 `.pill` 样式已删，标签一律用 `<Tag>`。新代码与改到的地方用这些，不要再在视图里写私有的同类控件。
- **共用小工具的家**（不碰 store、不写 `window`）：`src/lib/client.js`（信封、幂等键、令牌、API 地址）、`lib/store-kit.js`（`createKeyedLoader` / `createStore` / `toStoreError`）、`lib/store-utils.js`（`createSubscribers` / `useStoreTick` / `storeAlert`，多数 store 的订阅与报错都用它）、`lib/events.js`（`emit` / `useWindowEvents`）、`lib/format.js`（时间与数字文案）、`lib/text.js`（`countChars`：按 code point 数字，与后端 `count_words` 同一口径）、`lib/ids.js`、`lib/keyboard.js`、`lib/work-id.js`、`lib/ready-work.js`（`readyWorkId`）、`lib/poll.js`（看页面可见性的轮询）、`lib/revisioned-doc.js`、`lib/platform.js`、`lib/messages.js`；正文 HTML 的消毒、拆段与转纯文本在 `src/manuscript-html.js`。
- `src/main.jsx` 的样式导入顺序具有层叠语义：`styles.css`、`ws-ui.css` 在最前（`design-guard.test.js` 会检查），各视图的样式在后；调整时必须做视觉回归。
- API 错误统一为 `ApiRequestError`；界面应优先使用稳定 `code` 和 `details`，不要解析错误文案（后端的说明已经是中文）。
- 界面文案不出现英文大写小标题、原始 JSON、内部 id 或英文枚举值；夜间主题下的文字颜色只用令牌。

## 关键文件与回归

- 壳层：`src/ws-app.jsx`（路由、懒加载入口与 `ViewReady` 握手）、`src/ws-nav.js`（导航模型）、`src/ws-view-intents.js`（跨页指令）、`src/ws-rail.jsx`（侧栏）、`src/ws-work-switcher.jsx`、`src/ws-palette.jsx`
- 作品与远端状态：`src/ws-works.jsx`
- 雪花主线：`src/ws-snow.jsx`（入口）与 `src/ws-snow-*.jsx|js`（分章面板 `ws-snow-chapters.jsx`、历史页签 `ws-snow-history.jsx`、分步文本 `ws-snow-text.js` …）、同步层 `src/ws-snow-sync.jsx`（水合 `ws-snow-hydrate.js`、上行 `ws-snow-push.js`）
- 写作与恢复：`src/ws-writer.jsx`（入口）与 `src/ws-writer-*.jsx|js`、`src/wr-doc-store.jsx`、`src/wr-recovery-center.jsx`
- 目录（章 / 场的唯一交接面，场景 sid = 后端 `scene_id`）：`src/ws-catalog.jsx`；共用场景设计卡 `src/ws-scene-design.jsx`、场景卡同步状态 `src/ws-design-sync.jsx`（契约见 [`../docs/book-spine-catalog-contract.md`](../docs/book-spine-catalog-contract.md)）
- AI 起草台（左栏是全书书脊）：`src/ws-scene.jsx` 与 `src/ws-scene-*.jsx|js`、`src/ws-scene-run.jsx`
- 章节编排：`src/ws-author.jsx` 与 `src/ws-author-*.jsx|js`（AI 编排 `ws-author-ai.jsx` + store `ws-author-ai-store.js`）；章节运行 `src/ws-chapter-run.jsx`
- 成稿中心：`src/ws-manuscripts.jsx` 与 `src/ws-manuscripts-*.jsx|js`、`src/ws-manuscripts-store.jsx`
- 风格参考：`src/ws-styleref.jsx` 与 `src/ws-styleref-*.jsx|js`（状态与接口在 `src/ws-styleref-store.js`）
- 资料 / 回收站 / 设置：`src/ws-library*.jsx|js`、`src/ws-trash.jsx`、`src/ws-settings*.jsx`
- API 客户端：`src/lib/client.js`

核心回归至少包括：

```bash
npx vitest run src/tooling-independence.test.js src/build-chunking.test.js src/design-guard.test.js src/frontend-boundary-contract.test.js src/runtime-truth-contract.test.js
npx vitest run src/ws-works.test.jsx src/ws-snow.test.jsx src/ws-snow-sync.test.jsx
npx vitest run src/wr-doc-store.test.jsx src/wr-doc-sync.test.js src/wr-recovery-center.test.jsx src/ws-writer-content-safety.test.jsx
npx vitest run src/ws-scene-job.test.jsx src/ws-scene-page.test.jsx src/lib/client.test.js
npx vitest run src/ws-catalog.test.jsx src/ws-book-spine.test.jsx src/ws-scene-spine.test.jsx src/ws-writer-spine.test.jsx
npx vitest run src/ws-chapter-run.test.jsx src/ws-manuscripts.test.jsx src/ws-manuscripts-flow.test.jsx
npm run lint && npm run build
```

## 契约 E2E

`scripts/` 下只有契约 E2E 的套件：`run-smokes.mjs` 按清单依次执行主验收、`smoke-phase2..7`、`smoke-ai-settings` 与 `qa2-ui`，每套之前重灌中性夹具；各套共用 `scripts/lib/harness.mjs`（`openApp`、`waitUntil`、`reseedFixtures`、默认地址，并在浏览器上下文里掐断对 `:8000` 的一切请求、拒绝对作者的开发后端跑）。套件经开发态才有的 `window.__wsStores` 拿 store，所以必须对着 `npm run dev` 起的开发服务器跑。不经仓库脚本、手工跑 `node scripts/run-smokes.mjs` 时，要先把 `NOVEL_SYSTEM_DATABASE_URL` 指到那个夹具后端用的一次性库：重灌夹具（`python tests/fixture_runtime.py`）照搬调用者的环境，指向本检出的 `backend/novel_system.db`（或没设）时直接拒跑、退出码 2。`scripts/manual/shoot-views.mjs` 是不在任何 lane 里的手动逐页截图工具，默认同样指向 5176 / 8009、拒绝 `:8000`。

写冒烟时不要把异步谓词交给 Playwright 的 `page.waitForFunction`：它拿到的 Promise 是真值，只求值一次就带着落定的值返回（哪怕是 `false`），条件不满足也不会等到超时；后端行、store 的 Promise 这类异步条件在 Node 一侧用 `harness.mjs` 的 `waitUntil` 轮询。

仓库级的契约 E2E 由根目录脚本启动隔离后端、前端并自动清理：默认后端 `:8009`、React `:5176`（从不是开发用的 `:8000` / `:5174`），选定的端口已被占用时直接拒绝（退出码 2）；端口可用 `PLAYWRIGHT_BACKEND_PORT` / `PLAYWRIGHT_REACT_PORT` 改（Windows 的 `verify_react_e2e.ps1` 用参数 `-BackendPort` / `-ReactPort`，不读这两个环境变量）。请从仓库根目录运行：

```bash
NOVEL_SYSTEM_PYTHON=$PWD/backend/.venv/bin/python bash scripts/verify_react_e2e.sh    # Windows：powershell -ExecutionPolicy Bypass -File .\scripts\verify_react_e2e.ps1
```

运行安全细节见 [`../docs/runtime-safety.md`](../docs/runtime-safety.md)。
