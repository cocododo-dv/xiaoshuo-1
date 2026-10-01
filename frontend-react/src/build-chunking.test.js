import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

import { productionChunk } from "../build-chunks.js";

describe("生产构建分块", () => {
  it("只把第三方依赖固定进 vendor，业务模块交给懒加载路由切分", () => {
    expect(productionChunk("E:\\repo\\frontend-react\\node_modules\\react\\index.js")).toBe("vendor");
    expect(productionChunk("E:/repo/frontend-react/node_modules/react/index.js")).toBe("vendor");
    expect(productionChunk("E:/repo/frontend-react/src/ws-writer.jsx")).toBeUndefined();
    expect(productionChunk("E:/repo/frontend-react/src/ws-app.jsx")).toBeUndefined();
    const viteConfig = fs.readFileSync(path.join(path.dirname(fileURLToPath(import.meta.url)), "..", "vite.config.js"), "utf8");
    expect(viteConfig).toContain("manualChunks: productionChunk");
  });

  it("入口不再用副作用导入预加载全部业务模块", () => {
    const srcRoot = path.dirname(fileURLToPath(import.meta.url));
    const main = fs.readFileSync(path.join(srcRoot, "main.jsx"), "utf8");
    const sideEffectJsImports = [...main.matchAll(/import\s+["'](\.\/[^"']+\.(?:js|jsx))["']/g)]
      .map((match) => match[1]);
    expect(sideEffectJsImports).toEqual([]);
    expect(main).toContain("<React.StrictMode>");
  });

  it("业务页面由路由动态导入，并以挂载握手投递跨页指令", () => {
    const srcRoot = path.dirname(fileURLToPath(import.meta.url));
    const app = fs.readFileSync(path.join(srcRoot, "ws-app.jsx"), "utf8");
    for (const moduleName of [
      "ws-home.jsx", "ws-snow.jsx", "ws-styleref.jsx", "ws-library.jsx",
      "ws-author.jsx", "ws-scene.jsx", "ws-manuscripts.jsx", "ws-quality.jsx",
      "ws-cost.jsx", "ws-settings.jsx", "ws-writer.jsx",
    ]) {
      expect(app, moduleName).toContain(`import("./${moduleName}")`);
    }
    expect(app).toContain("<ViewReady view={view}>");
    expect(app).not.toMatch(/setTimeout\([^\n]+dispatchEvent/);
  });

  it("雪花与作者路由会同时装配 SnowSync，不能只把同步模块留在源码里", () => {
    // 两条路由的模块经静态 import 走得到 ws-snow-sync.jsx：路由块一加载，同步层就跟着装好（以前 ws-app 另外动态
    // import 它一次补装——那时页面经 window.SnowSync 取它，没有静态依赖）
    const srcRoot = path.dirname(fileURLToPath(import.meta.url));
    const staticDeps = (file) => [...fs.readFileSync(file, "utf8").matchAll(/(?:import|export)\s+(?:[^'"]*?\s+from\s+)?['"](\.[^'"]+)['"]/g)]
      .map((match) => path.resolve(path.dirname(file), match[1]))
      .map((base) => [base, `${base}.js`, `${base}.jsx`].find((candidate) => fs.existsSync(candidate) && fs.statSync(candidate).isFile()))
      .filter(Boolean);
    const closure = (entry) => {
      const seen = new Set();
      const stack = [path.join(srcRoot, entry)];
      while (stack.length) {
        const file = stack.pop();
        if (seen.has(file)) continue;
        seen.add(file);
        stack.push(...staticDeps(file));
      }
      return [...seen].map((file) => path.relative(srcRoot, file));
    };
    for (const route of ["ws-snow.jsx", "ws-author.jsx"]) {
      expect(closure(route), route).toContain("ws-snow-sync.jsx");
    }
    const app = fs.readFileSync(path.join(srcRoot, "ws-app.jsx"), "utf8");
    expect(app).toMatch(/LazyWsConstruct\s*=\s*lazyNamed\(\(\)\s*=>\s*import\("\.\/ws-snow\.jsx"\)/);
    expect(app).toMatch(/LazyWsAuthor\s*=\s*lazyNamed\(\(\)\s*=>\s*import\("\.\/ws-author\.jsx"\)/);
  });
});
