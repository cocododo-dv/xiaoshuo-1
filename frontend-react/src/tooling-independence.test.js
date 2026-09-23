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
    const browserScripts = fs.readdirSync(scriptsDir)
      .filter((name) => name.endsWith(".mjs"))
      .map((name) => [name, fs.readFileSync(path.join(scriptsDir, name), "utf8")])
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

  it("新的 ESM-only 模块不再写入 window 全局命名空间", () => {
    const esmOnly = [
      "icons.jsx", "tweaks-panel.jsx", "ws-ai-providers.jsx", "ws-chapter-plan.jsx",
      "ws-cost.jsx", "ws-deep.jsx", "ws-home.jsx", "ws-home-derive.js",
      "ws-palette.jsx", "ws-quality.jsx", "ws-settings.jsx", "ws-settings-ai.jsx",
      "ws-settings-shared.jsx", "ws-styleref.jsx", "ws-styleref-model.js", "ws-styleref-store.js", "ws-styleref-ui.jsx", "ws-styleref-activity.jsx", "ws-styleref-library.jsx", "ws-styleref-overview.jsx", "ws-styleref-learn.jsx", "ws-styleref-portrait.jsx", "ws-styleref-apply.jsx", "ws-styleref-scene-preview.jsx", "ws-styleref-check.jsx", "ws-styleref-fidelity.jsx", "ws-scene-run.jsx",
      "ws-fidelity-model.js", "ws-fidelity-store.js", "ws-fidelity-ui.jsx", "ws-scene-fidelity.jsx", "ws-manuscripts-fidelity.jsx", "ws-copy-gate.js",
      "ws-author-data.jsx", "ws-author-doctor.jsx", "ws-author-derive.js", "ws-author-spine.jsx",
      "ws-author-pacing.jsx", "ws-author-ai.jsx", "ws-author-overview.jsx", "ws-author-detail.jsx",
      "ws-author-side.jsx", "ws-author-ui.jsx", "ws-author-hooks.js", "ws-library-derive.jsx",
      "ws-library-graph.jsx", "ws-library-overview.jsx", "ws-library-timeline.jsx", "ws-library-dossier.jsx", "ws-library-parts.jsx", "ws-trash.jsx", "ws-settings-ai-providers.jsx", "ws-settings-ai-routes.jsx", "ws-settings-ai-health.js",
      "ws-manuscripts-store.jsx", "ws-manuscripts.jsx", "ws-labels.js", "ws-quality-ui.jsx", "ws-author.jsx",
      "ws-manuscripts-compile.js", "ws-manuscripts-workflow.js", "ws-manuscripts-hero.jsx",
      "ws-manuscripts-reader.jsx", "ws-manuscripts-canon.jsx", "ws-manuscripts-diff.jsx", "ws-manuscripts-dialogs.jsx",
      "ws-scene.jsx", "ws-library.jsx", "ws-writer.jsx", "ws-dialog.jsx", "ws-ui.jsx",
      "ws-nav.js", "ws-lazy.jsx", "ws-rail.jsx", "ws-work-switcher.jsx", "ws-notify.jsx", "ws-prefs.js",
      "ws-cost-parts.jsx", "ws-cost-store.js", "ws-home-chapters.jsx", "ws-home-parts.jsx", "ws-home-states.jsx", "ws-quality-model.js",
      "ws-quality-store.js", "ws-snow-chapters-model.js", "ws-snow-chapters-parts.jsx", "ws-snow-reply.jsx", "ws-snow-scene-list.jsx", "ws-snow-scene-plan.jsx",
      "ws-scene-adopt.jsx", "ws-scene-api.js", "ws-scene-board-state.js", "ws-scene-decide.jsx", "ws-scene-derive.js", "ws-scene-evidence.jsx",
      "ws-scene-job.jsx", "ws-scene-spine.jsx", "ws-scene-stage.jsx", "ws-scene-store.js", "ws-design-sync.jsx", "ws-chapter-run.jsx",
      "ws-writer-ai.js", "ws-writer-annotations.js", "ws-writer-candidates.jsx", "ws-writer-catalog.js", "ws-writer-context.jsx", "ws-writer-deep-posture.js",
      "ws-writer-doc.js", "ws-writer-dock.jsx", "ws-writer-entities.jsx", "ws-writer-header.jsx", "ws-writer-hooks.js", "ws-writer-inline.jsx",
      "ws-writer-keys.js", "ws-writer-manuscript.js", "ws-writer-notes.jsx", "ws-writer-outline.jsx", "ws-writer-requests.js", "ws-writer-room.jsx",
      "ws-writer-tray.jsx", "ws-snow-chrome.jsx", "ws-snow-coach.jsx", "ws-snow-fields.jsx", "ws-snow-history.jsx", "ws-snow-hooks.js",
      "ws-snow-model.js", "ws-snow-rail.jsx", "ws-snow-scaffolds.jsx", "ws-snow-scenes.jsx",
    ];
    for (const name of esmOnly) {
      const source = fs.readFileSync(path.join(srcDir, name), "utf8");
      expect(source, name).not.toMatch(/Object\.assign\(window|window\.[A-Za-z_$][A-Za-z0-9_$]*\s*=/);
    }

    const remainingTransitionalModules = sourceModules().filter((file) => {
      const source = fs.readFileSync(file, "utf8");
      return /Object\.assign\(window|window\.[A-Za-z_$][A-Za-z0-9_$]*\s*=/.test(source);
    });
    expect(remainingTransitionalModules.length).toBeLessThanOrEqual(10);
  });
});
