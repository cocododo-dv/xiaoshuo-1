import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

const frontendRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const repoRoot = path.resolve(frontendRoot, "..");
const scriptsDir = path.join(frontendRoot, "scripts");
const srcDir = path.join(frontendRoot, "src");

function sourceModules() {
  const modules = [];
  const ignoredDirectories = new Set([".pytest_cache", "node_modules", ".test-results"]);
  const walk = (directory) => {
    for (const entry of fs.readdirSync(directory, { withFileTypes: true })) {
      const absolute = path.join(directory, entry.name);
      if (entry.isDirectory() && !ignoredDirectories.has(entry.name)) walk(absolute);
      else if (/\.(js|jsx)$/.test(entry.name) && !entry.name.includes(".test.")) modules.push(absolute);
    }
  };
  walk(srcDir);
  return modules;
}

// scripts/ 下的 .mjs（含子目录，例如 scripts/manual/），返回 [相对路径, 源码]
function scriptSources() {
  const out = [];
  const walk = (directory) => {
    for (const entry of fs.readdirSync(directory, { withFileTypes: true })) {
      const absolute = path.join(directory, entry.name);
      if (entry.isDirectory()) walk(absolute);
      else if (entry.name.endsWith(".mjs")) out.push([path.relative(scriptsDir, absolute), fs.readFileSync(absolute, "utf8")]);
    }
  };
  walk(scriptsDir);
  return out;
}

// 仍写 window 全局的过渡模块（旧 store 的运行时接缝）。棘轮：只删不加——哪个模块不再写 window，
// 就把它从这里删掉（测试会提醒）；其它模块一律不许写，新代码用 ES 导出 / 导入或事件。
const KNOWN_WINDOW_WRITERS = [
  "wr-doc-store.jsx",
  "ws-catalog.jsx",
  "ws-library-data.jsx",
  "ws-library-edit.jsx",
  "ws-review.jsx",
  "ws-snow-sync.jsx",
  "ws-snow.jsx",
  "ws-works.jsx",
];

function resolveRelativeModule(importer, specifier, knownModules) {
  const base = path.resolve(path.dirname(importer), specifier);
  return [base, `${base}.js`, `${base}.jsx`, path.join(base, "index.js"), path.join(base, "index.jsx")]
    .find((candidate) => knownModules.has(path.normalize(candidate))) || null;
}

describe("React 工具链独立性", () => {
  it("由本包直接、精确锁定 Playwright", () => {
    const pkg = JSON.parse(fs.readFileSync(path.join(frontendRoot, "package.json"), "utf8"));
    expect(pkg.devDependencies.playwright).toMatch(/^\d+\.\d+\.\d+$/);
    expect(fs.existsSync(path.join(frontendRoot, "node_modules", "playwright", "package.json"))).toBe(true);
  });

  it("浏览器脚本按自身位置解析依赖，不依赖调用者工作目录", () => {
    const browserScripts = scriptSources()
      .filter(([, source]) => source.includes('require("playwright")'));

    expect(browserScripts.length).toBeGreaterThan(0);
    for (const [name, source] of browserScripts) {
      expect(source, name).toContain("createRequire(import.meta.url)");
      expect(source, name).not.toContain('createRequire(path.join(process.cwd(), "package.json"))');
    }
  });

  it("QA2 uses the live project-list contract to discover its single-chapter fixture", () => {
    const source = fs.readFileSync(path.join(scriptsDir, "qa2-ui.mjs"), "utf8");
    expect(source).toContain("${API}/api/v1/projects");
    expect(source).not.toContain("${API}/api/v2/projects`");
    expect(source).toContain("catalog.chapters.length === 1");
  });

  it("静态 ESM 依赖图没有循环", () => {
    const modules = sourceModules();
    const knownModules = new Set(modules.map((file) => path.normalize(file)));
    const graph = new Map();
    for (const file of modules) {
      const source = fs.readFileSync(file, "utf8");
      const dependencies = [...source.matchAll(/(?:import|export)\s+(?:[^'\"]*?\s+from\s+)?['\"](\.[^'\"]+)['\"]/g)]
        .map((match) => resolveRelativeModule(file, match[1], knownModules))
        .filter(Boolean);
      graph.set(path.normalize(file), dependencies);
    }

    const state = new Map();
    const stack = [];
    const cycles = [];
    const visit = (file) => {
      state.set(file, "visiting");
      stack.push(file);
      for (const dependency of graph.get(file) || []) {
        if (!state.has(dependency)) visit(dependency);
        else if (state.get(dependency) === "visiting") {
          const start = stack.indexOf(dependency);
          cycles.push([...stack.slice(start), dependency].map((item) => path.relative(srcDir, item)));
        }
      }
      stack.pop();
      state.set(file, "visited");
    };
    for (const file of graph.keys()) if (!state.has(file)) visit(file);

    expect(cycles).toEqual([]);
  });

  it("用到图标对象 I 的模块都显式从 icons.jsx 导入它（原型期的 window.I 已不存在）", () => {
    // 文学质量 / 成本看板 曾因只写了 `/* global I */` 而在打开时崩溃（I is not defined），
    // 测试里的 `globalThis.I = {}` 把它掩盖了。
    const usesIcons = /(^|[^A-Za-z0-9_$.])I\.[A-Z]|<I\.[A-Z]|(^|[^A-Za-z0-9_$.])I\[/m;
    const importsIcons = /import\s*\{[^}]*\bI\b[^}]*\}\s*from\s*["']\.{1,2}\/(?:[^"']*\/)?icons\.jsx["']/;
    const missing = sourceModules()
      .filter((file) => path.basename(file) !== "icons.jsx")
      .filter((file) => {
        const source = fs.readFileSync(file, "utf8");
        return usesIcons.test(source) && !importsIcons.test(source);
      })
      .map((file) => path.relative(srcDir, file));
    expect(missing).toEqual([]);
  });

  it("只有已知的过渡模块写 window 全局命名空间（新模块一律不写，列表只减不增）", () => {
    // 正则故意从严：`window.x ===` 这样的比较也算写
    const writers = sourceModules()
      .filter((file) => /Object\.assign\(window|window\.[A-Za-z_$][A-Za-z0-9_$]*\s*=/.test(fs.readFileSync(file, "utf8")))
      .map((file) => path.relative(srcDir, file).split(path.sep).join("/"))
      .sort();
    const unexpected = writers.filter((name) => !KNOWN_WINDOW_WRITERS.includes(name));
    expect(unexpected, "这些模块新写了 window：改用 ES 导出 / 导入或事件").toEqual([]);
    const fixed = KNOWN_WINDOW_WRITERS.filter((name) => !writers.includes(name));
    expect(fixed, "这些已经不写 window 了，把它们从 KNOWN_WINDOW_WRITERS 里删掉").toEqual([]);
  });

  it("全局对象上只有 lib/events.js 的跨重载去重登记表（Symbol 键），别的模块不往 globalThis[…] 上写", () => {
    // 上面的 window 守卫只认 `window.x =`；把状态藏到 globalThis[键] 上同样是全局接缝，只许这一处
    const slotWriters = sourceModules()
      .filter((file) => /globalThis\[[^\]]+\]\s*=(?!=)/.test(fs.readFileSync(file, "utf8")))
      .map((file) => path.relative(srcDir, file).split(path.sep).join("/"))
      .sort();
    expect(slotWriters).toEqual(["lib/events.js"]);
  });
});
