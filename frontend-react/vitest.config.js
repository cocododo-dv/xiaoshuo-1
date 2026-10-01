import { configDefaults, defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";

// 单测配置与 vite.config.js 分离：build 配置不受影响。React 插件负责 .jsx 转换。
// 两个项目：
//   · dom  —— 默认。store 在 import 时就读写本机存储、挂窗口事件（ws-works.jsx 等），组件测试要 jsdom 的
//             window / document / localStorage；src/test-setup.js 统一打开 React 的 act 环境、用例后清存储。
//             单条上限 15 s：装配整间写作台 / 起草台的文件，第一条用例要付模块转换的冷启动，主机负载高时
//             默认的 5 s 会间歇超时（以前八个文件各自写一行 vi.setConfig）。
//   · node —— 下面列出的纯逻辑与读源码的守卫测试：不碰 DOM，省掉每个文件约 0.7 s 的 jsdom 装配。
// 新测试默认进 dom。确认它（连同它 import 的模块）不碰 DOM 再加进 NODE_TESTS；碰了会直接报
// window / document is not defined。
const NODE_TESTS = [
  "src/build-chunking.test.js",
  "src/design-guard.test.js",
  "src/frontend-boundary-contract.test.js",
  "src/runtime-truth-contract.test.js",
  "src/tooling-independence.test.js",
  "src/lib/messages.test.js",
  "src/lib/text.test.js",
  "src/ws-author-derive.test.js",
  "src/ws-fidelity-model.test.js",
  "src/ws-fidelity-store.test.js",
  "src/ws-home-derive.test.js",
  "src/ws-labels.test.js",
  "src/ws-manuscripts-compile.test.js",
  "src/ws-nav.test.js",
  "src/ws-snow-model.test.js",
  "src/ws-snow-scene-id.test.jsx",
  "src/ws-styleref-model.test.js",
  "src/ws-styleref-structure.test.js",
  "src/ws-writer-ai.test.js",
];

export default defineConfig({
  plugins: [react()],
  test: {
    globals: true,
    restoreMocks: true,
    clearMocks: true,
    projects: [
      {
        extends: true,
        test: {
          name: "dom",
          environment: "jsdom",
          testTimeout: 15_000,
          include: ["src/**/*.{test,spec}.{js,jsx}"],
          exclude: [...configDefaults.exclude, ...NODE_TESTS],
          setupFiles: ["src/test-setup.js"],
        },
      },
      {
        extends: true,
        test: {
          name: "node",
          environment: "node",
          include: NODE_TESTS,
        },
      },
    ],
  },
});
