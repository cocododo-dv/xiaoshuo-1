import { spawnSync } from "node:child_process";
import fs from "node:fs";
import os from "node:os";
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

// 唯一允许写 window 全局命名空间的模块：开发态测试接缝（契约 E2E 冒烟经 window.__wsStores 拿 store），
// 而且只在 import.meta.env.DEV 时写（生产构建里整段被摇掉）。原型期挂在 window 上的 store 全局（WsWorks /
// WsCatalog / WrDocs / SnowSync / rv* / LIB_* …）已全部退役；其它模块一律不写，用 ES 导出 / 导入或事件。
const TEST_SEAM = "ws-test-seam.js";
const KNOWN_WINDOW_WRITERS = [TEST_SEAM];

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

  it("QA2 runs every check on the fixture work it names: go() waits until that work is the current one", () => {
    // 书架（/api/v2/projects）只列雪花作品，不在上面的作品打开时应用退回第一部。以前为了一条从 v1 列表找单章项目的
    // 检查（AUTHOR-04，它找到的项目书架从来打不开，等于空过；已删），go() 只等应用装好、不认当前作品，
    // 别的检查也就可能悄悄跑在别的作品上
    const source = fs.readFileSync(path.join(scriptsDir, "qa2-ui.mjs"), "utf8");
    const go = source.match(/^async function go\(work, view\) \{[\s\S]*?^\}/m)?.[0] || "";
    expect(go).toContain("await waitForApp({ work });");
    expect(source).not.toContain("${API}/api/v1/projects");
    // 全视图 console 巡检也走章节编排：AUTHOR-04 删掉以后，这一页有没有 console error 只剩巡检在看
    const sweep = source.match(/ctx = "console-sweep";[\s\S]*?for \(const v of (\[[^\]]*\])\)/)?.[1] || "[]";
    expect(JSON.parse(sweep)).toContain("author");
  });

  it("前端启动脚本先查 Node（下限与 package.json engines 一致）再停旧实例，也不改写操作者的 NODE_OPTIONS", () => {
    const pkg = JSON.parse(fs.readFileSync(path.join(frontendRoot, "package.json"), "utf8"));
    const lifecycle = fs.readFileSync(path.join(repoRoot, "scripts", "lib", "dev-lifecycle.sh"), "utf8");
    const floor = ["MAJOR", "MINOR"].map((part) => (lifecycle.match(new RegExp(`^DEV_NODE_FLOOR_${part}=(\\d+)$`, "m")) || [])[1]);
    expect(pkg.engines && pkg.engines.node).toBe(`>=${floor.join(".")}`);
    // 换错 Node 重启时要先报错退出：要是检查排在停旧实例之后，正在跑的前端会先被停掉、再拒绝启动。
    // 一键启动（start-all-linux.sh，作者平时用的就是它）在起前端这条腿之前自己先停前后端，所以它也要先查
    for (const script of ["start-frontend-linux.sh", "start-all-linux.sh"]) {
      const code = fs.readFileSync(path.join(repoRoot, "scripts", script), "utf8")
        .split("\n").filter((line) => !line.trim().startsWith("#")).join("\n");
      const check = code.indexOf("dev_check_frontend_toolchain");
      expect(check, script).toBeGreaterThan(-1);
      // 查的是起前端时会用的那个 Node：先按同样的顺序找 Node，再查
      const useNode = code.indexOf("dev_use_frontend_node");
      expect(useNode, script).toBeGreaterThan(-1);
      expect(useNode, script).toBeLessThan(check);
      for (const stop of ["dev_stop_pidfile", "dev_stop_port"]) {
        expect(code.indexOf(stop), `${script}: ${stop}`).toBeGreaterThan(check);
      }
      // 操作者自己设的 NODE_OPTIONS 原样传给 Vite（以前为 Node 16 预加载 crypto-polyfill.cjs，把它整个盖掉）
      expect(code, script).not.toMatch(/NODE_OPTIONS=/);
    }
  });

  // 启动脚本的 Node 查找与检查（scripts/lib/dev-lifecycle.sh）照真的跑一遍：假 HOME、只会报版本号的假 node / npm，
  // PATH 里只有这些假目录（外加 cat），与这台机器上装了哪个 Node 无关。启动脚本只在 Linux 上用，Windows 上跳过
  const bash = process.platform === "win32" ? "" : ["/bin/bash", "/usr/bin/bash"].find((file) => fs.existsSync(file)) || "";
  it.skipIf(!bash)("Node 按文档的先后找（nvm 没给出自己的 node 就退到 ~/.local/node），太旧时只提真会生效的办法", () => {
    const lifecycle = path.join(repoRoot, "scripts", "lib", "dev-lifecycle.sh");
    const [major, minor] = ["MAJOR", "MINOR"].map((part) => Number(fs.readFileSync(lifecycle, "utf8").match(new RegExp(`^DEV_NODE_FLOOR_${part}=(\\d+)$`, "m"))[1]));
    const root = fs.mkdtempSync(path.join(os.tmpdir(), "dev-node-"));
    try {
      const fakeNode = (dir, version) => {
        fs.mkdirSync(dir, { recursive: true });
        fs.writeFileSync(path.join(dir, "node"), `#!/bin/sh\necho ${version}\n`, { mode: 0o755 });
        fs.writeFileSync(path.join(dir, "npm"), "#!/bin/sh\necho 10.0.0\n", { mode: 0o755 });
      };
      // 假 nvm 只做真 nvm 被 source 时做的那件事：有默认别名，才把它自己的那个 node 放到 PATH 最前
      const fakeNvm = (home, defaultVersion) => {
        fs.mkdirSync(path.join(home, ".nvm", "alias"), { recursive: true });
        fs.writeFileSync(path.join(home, ".nvm", "nvm.sh"),
          'if [ -f "$NVM_DIR/alias/default" ]; then read -r v < "$NVM_DIR/alias/default"; PATH="$NVM_DIR/versions/node/$v/bin:$PATH"; fi\n');
        if (!defaultVersion) return;
        fs.writeFileSync(path.join(home, ".nvm", "alias", "default"), `${defaultVersion}\n`);
        fakeNode(path.join(home, ".nvm", "versions", "node", defaultVersion, "bin"), defaultVersion);
      };
      const frontend = path.join(root, "frontend-react");
      fs.mkdirSync(path.join(frontend, "node_modules"), { recursive: true });
      const tools = path.join(root, "tools");
      fs.mkdirSync(tools);
      const cat = ["/bin/cat", "/usr/bin/cat"].find((file) => fs.existsSync(file));
      if (cat) fs.symlinkSync(cat, path.join(tools, "cat"));
      const run = (home, env = {}) => {
        const result = spawnSync(bash, ["-c",
          'set -euo pipefail; source "$1"; dev_use_frontend_node; dev_check_frontend_toolchain "$2"; command -v node',
          "dev-node-probe", lifecycle, frontend], { env: { HOME: home, PATH: tools, ...env }, encoding: "utf8" });
        return { status: result.status, node: result.stdout.trim(), hint: result.stderr };
      };

      // 装了 nvm 但没有默认别名：nvm 不给 node，退到 ~/.local/node，照常启动
      const nvmNoDefault = path.join(root, "nvm-no-default");
      fakeNvm(nvmNoDefault);
      fakeNode(path.join(nvmNoDefault, ".local", "node", "bin"), "v22.23.2");
      expect(run(nvmNoDefault)).toMatchObject({ status: 0, node: path.join(nvmNoDefault, ".local", "node", "bin", "node") });

      // nvm 的默认别名是 16：它赢过 ~/.local/node，所以提示让改 nvm 的默认别名，不叫人解压到 ~/.local/node
      const nvmDefault16 = path.join(root, "nvm-default-16");
      fakeNvm(nvmDefault16, "v16.20.2");
      fakeNode(path.join(nvmDefault16, ".local", "node", "bin"), "v22.23.2");
      const nvmOld = run(nvmDefault16);
      expect(nvmOld.status).toBe(1);
      expect(nvmOld.hint).toContain("'v16.20.2'");
      expect(nvmOld.hint).toContain("nvm alias default 22");
      expect(nvmOld.hint).not.toMatch(/unpack/);

      // NOVEL_SYSTEM_NODE_BIN 盖过别的一切：提示只说改它或去掉它
      const node16 = path.join(root, "node16", "bin");
      fakeNode(node16, "v16.20.2");
      const override = run(nvmNoDefault, { NOVEL_SYSTEM_NODE_BIN: node16 });
      expect(override.status).toBe(1);
      expect(override.hint).toContain("NOVEL_SYSTEM_NODE_BIN");
      expect(override.hint).not.toMatch(/unpack|nvm alias/);

      // 下限就是 package.json engines 那个版本：低一点拒，正好放行
      const bare = path.join(root, "bare-home");
      fs.mkdirSync(bare);
      const below = minor > 0 ? `v${major}.${minor - 1}.9` : `v${major - 1}.99.0`;
      for (const [version, status] of [[below, 1], [`v${major}.${minor}.0`, 0]]) {
        const bin = path.join(root, version, "bin");
        fakeNode(bin, version);
        expect(run(bare, { NOVEL_SYSTEM_NODE_BIN: bin }).status, version).toBe(status);
      }

      // 哪儿都没有 node：说清楚缺什么，给的办法是解压到 ~/.local/node 或 NOVEL_SYSTEM_NODE_BIN
      const nothing = run(bare);
      expect(nothing.status).toBe(1);
      expect(nothing.hint).toContain("node/npm not found");
      expect(nothing.hint).toContain("unpack it at ~/.local/node");
    } finally {
      fs.rmSync(root, { recursive: true, force: true });
    }
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

  it("只有开发态测试接缝写 window 全局命名空间（别的模块一律不写）", () => {
    // 正则故意从严：`window.x ===` 这样的比较也算写
    const writers = sourceModules()
      .filter((file) => /Object\.assign\(window|window\.[A-Za-z_$][A-Za-z0-9_$]*\s*=/.test(fs.readFileSync(file, "utf8")))
      .map((file) => path.relative(srcDir, file).split(path.sep).join("/"))
      .sort();
    expect(writers, "只有 ws-test-seam.js 可以写 window：别的模块改用 ES 导出 / 导入或事件").toEqual(KNOWN_WINDOW_WRITERS);
  });

  it("测试接缝只在开发态写 window：写的只有 window.__wsStores，而且整段包在 import.meta.env.DEV 里", () => {
    const source = fs.readFileSync(path.join(srcDir, TEST_SEAM), "utf8");
    const writes = [...source.matchAll(/window\.([A-Za-z_$][A-Za-z0-9_$]*)\s*=(?!=)/g)].map((match) => match[1]);
    expect(writes).toEqual(["__wsStores"]);
    expect(source).not.toMatch(/Object\.assign\(window/);
    expect(source).toMatch(/if \(!import\.meta\.env\.DEV\b[^\n]*\) return;/);
    // 一个 store 都不静态 import：静态 import 会把懒加载的 store 拉进入口块（只用 DEV 分支里的动态 import）
    expect(source).not.toMatch(/^import\s/m);
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
