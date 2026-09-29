import { defineConfig, loadEnv } from "vite";
import react from "@vitejs/plugin-react";

import { productionChunk } from "./build-chunks.js";
import { cspConnectSrc } from "./build-csp.js";

export default defineConfig(({ mode }) => {
  // 与 src/lib/client.js 读的是同一个变量（进程环境或 .env 文件里的 VITE_ 前缀变量）
  const env = loadEnv(mode, process.cwd(), "VITE_");
  return {
    plugins: [react(), cspConnectSrc(env.VITE_NOVEL_SYSTEM_API_BASE)],
    build: {
      rollupOptions: {
        output: {
          manualChunks: productionChunk,
        },
      },
    },
    server: {
      host: "127.0.0.1",
      port: 5174,
    },
    preview: {
      host: "127.0.0.1",
      port: 5175,
    },
  };
});
