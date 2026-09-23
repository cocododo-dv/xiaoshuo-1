import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

/* 设计系统守卫（2026-09-21 前端重构）。没有 CSS lint 时这几类错误都真的上线过：
   未定义的 --line 让成本 / 质量卡片在夜间主题画出亮边框，缺失的 @keyframes spin
   让加载圈不转，JSX 里注入的 <style> 绕开了样式表的层叠顺序。
   KNOWN_* 是棘轮：只能缩短——修好一项就把它从列表里删掉（测试会提醒）。 */

const srcDir = path.dirname(fileURLToPath(import.meta.url));

function sourceFiles() {
  const out = [];
  const walk = (dir) => {
    for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
      const abs = path.join(dir, entry.name);
      if (entry.isDirectory()) { if (entry.name !== "node_modules") walk(abs); }
      else if (/\.(css|jsx|js)$/.test(entry.name) && !entry.name.includes(".test.")) out.push(abs);
    }
  };
  walk(srcDir);
  return out.map((file) => ({ file: path.relative(srcDir, file), source: fs.readFileSync(file, "utf8") }));
}

const FILES = sourceFiles();

// 仍未定义、已知待修的自定义属性（引用处都带回退值）。修好就删。
const KNOWN_UNDEFINED_PROPERTIES = [];
// 仍缺 @keyframes 的动画名。修好就删。
const KNOWN_MISSING_KEYFRAMES = [];
// JSX 里仍注入 <style> 的文件数上限（棘轮，只降不升）。
const MAX_JSX_STYLE_TAG_FILES = 0;
// 已知没人引用、暂时留着的规则（"文件: 选择器"）。棘轮：只删不加。
// .help 是表单原语（.label / .input / .help 一组），暂时没有调用方。
const KNOWN_DEAD_CSS = [
  "styles.css: .help",
];
// 高于 40 的 z-index 字面量个数上限（棘轮，只降不升）。浮层一律走 styles.css 的 --z-* 刻度；
// 0–40 是组件内部的局部叠放，可以写字面量。
const MAX_Z_INDEX_LITERALS_ABOVE_40 = 0;
// 断点的文档刻度（styles.css 令牌注释）：宽 1440 / 1280 / 1100 / 900 / 640，矮窗口 820。
const DOCUMENTED_BREAKPOINTS = new Set(["max-width: 1440px", "max-width: 1280px", "max-width: 1100px",
  "max-width: 900px", "max-width: 640px", "min-width: 1441px", "min-width: 1281px", "min-width: 1101px",
  "min-width: 901px", "min-width: 641px", "max-height: 820px"]);
// 刻度外、暂时留着的断点（"文件: 条件"）。棘轮：只删不加——挪到刻度上会改变 1440 / 1100 两档
// 之外某个真实宽度的布局，要逐个看过再挪。
const KNOWN_OFF_SCALE_BREAKPOINTS = [
  "wr-desk.css: max-width: 1199px",
  "wr-desk.css: max-width: 999px",
  "wr-desk.css: min-width: 1000px",
  "wr-recovery.css: max-width: 760px",
  "wr-redesign.css: max-width: 999px",
  "ws-author.css: max-width: 1180px",
  "ws-author.css: max-width: 1360px",
  "ws-library.css: max-width: 820px",
  "ws-manuscripts.css: max-width: 1080px",
  "ws-manuscripts.css: max-width: 980px",
  "ws-scene.css: max-width: 1120px",
  "ws-scene.css: max-width: 1320px",
  "ws-home.css: max-width: 720px",
  "ws-home.css: max-width: 960px",
  "ws-shell.css: max-height: 680px",
  "ws-shell.css: max-width: 720px",
  "ws-snow.css: max-width: 1180px",
  "ws-snow.css: max-width: 1279px",
  "ws-snow.css: max-width: 760px",
  "ws-snow.css: min-width: 1181px",
];
// 颜料字压在同色 -wash 底上、但容器里只有 aria-hidden 图标的规则（"文件: 选择器"）。图形只需 3:1，
// 颜料在自己的 wash 上够用；容器里一旦放字，就得换成 --*-ink 并从这里删掉。
const ICON_ONLY_PIGMENT_ON_WASH = [
  "wr-redesign.css: .wr-tray-spark",
];
// JSX / JS（非测试）里写死颜色的行数上限（棘轮，只降不升；现在是 0）。
const MAX_JSX_RAW_COLOURS = 0;

const ANIMATION_KEYWORDS = new Set([
  "none", "infinite", "linear", "ease", "ease-in", "ease-out", "ease-in-out", "both", "forwards",
  "backwards", "alternate", "alternate-reverse", "reverse", "normal", "running", "paused",
  "step-start", "step-end", "inherit", "initial", "unset", "var", "steps", "cubic-bezier",
]);

describe("设计系统守卫", () => {
  it("每个 var(--x) 引用的自定义属性都有定义（未定义的会静默回退到白天主题的颜色）", () => {
    const defined = new Set();
    const used = new Map();
    for (const { file, source } of FILES) {
      for (const m of source.matchAll(/(--[A-Za-z0-9_-]+)\s*:/g)) defined.add(m[1]);
      for (const m of source.matchAll(/["'`](--[A-Za-z0-9_-]+)["'`]\s*[:,)]/g)) defined.add(m[1]);
      for (const m of source.matchAll(/var\(\s*(--[A-Za-z0-9_-]+)/g)) {
        if (!used.has(m[1])) used.set(m[1], new Set());
        used.get(m[1]).add(file);
      }
    }
    const undefinedNow = [...used.keys()].filter((name) => !defined.has(name)).sort();
    const unexpected = undefinedNow.filter((name) => !KNOWN_UNDEFINED_PROPERTIES.includes(name))
      .map((name) => `${name} ← ${[...used.get(name)].join(", ")}`);
    expect(unexpected, "新引用了未定义的自定义属性：先在 styles.css 定义令牌，或改用已有令牌").toEqual([]);
    const fixed = KNOWN_UNDEFINED_PROPERTIES.filter((name) => !undefinedNow.includes(name));
    expect(fixed, "这些已经修好了，把它们从 KNOWN_UNDEFINED_PROPERTIES 里删掉").toEqual([]);
  });

  it("每个 animation 用到的名字都有 @keyframes", () => {
    const keyframes = new Set();
    const used = new Map();
    for (const { file, source } of FILES) {
      for (const m of source.matchAll(/@keyframes\s+([A-Za-z0-9_-]+)/g)) keyframes.add(m[1]);
      for (const m of source.matchAll(/animation(?:-name)?\s*:\s*([^;}"`]+)/g)) {
        for (const token of m[1].split(/[\s,()]+/)) {
          if (!/^[A-Za-z][A-Za-z0-9_-]*$/.test(token) || ANIMATION_KEYWORDS.has(token)) continue;
          if (token.startsWith("--")) continue;
          if (!used.has(token)) used.set(token, new Set());
          used.get(token).add(file);
        }
      }
    }
    const missing = [...used.keys()].filter((name) => !keyframes.has(name)).sort();
    const unexpected = missing.filter((name) => !KNOWN_MISSING_KEYFRAMES.includes(name))
      .map((name) => `${name} ← ${[...used.get(name)].join(", ")}`);
    expect(unexpected, "动画名没有对应的 @keyframes（共用的是 ws-spin / ws-fade / ws-rise，定义在 ws-ui.css）").toEqual([]);
    const fixed = KNOWN_MISSING_KEYFRAMES.filter((name) => !missing.includes(name));
    expect(fixed, "这些已经修好了，把它们从 KNOWN_MISSING_KEYFRAMES 里删掉").toEqual([]);
  });

  it("JSX 注入 <style> 的文件只减不增", () => {
    const withStyleTags = FILES.filter(({ file, source }) => file.endsWith(".jsx") && /<style[\s>]/.test(source))
      .map(({ file }) => file);
    expect(withStyleTags.length, `这些文件在 JSX 里注入 <style>：${withStyleTags.join(", ")}；样式放进对应的 .css`)
      .toBeLessThanOrEqual(MAX_JSX_STYLE_TAG_FILES);
  });

  it("色调变量只由 [data-tone] 和零特异性的 :where() 默认值设置（否则会盖过作者指定的色调）", () => {
    // 曾经 .ws-tag / .ws-notice 用类选择器写默认 --tone，与 [data-tone] 同特异性且更靠后，
    // 所有警告 / 危险提示都画成了信息框。
    const offenders = [];
    for (const { file, source } of FILES.filter(({ file }) => file.endsWith(".css"))) {
      for (const m of source.matchAll(/([^{}]+)\{([^{}]*)\}/g)) {
        const selector = m[1].replace(/\/\*[\s\S]*?\*\//g, "").trim();
        if (!/(^|[\s;{])--tone(-wash|-ink)?\s*:/.test(m[2])) continue;
        // 按顶层逗号拆选择器列表（:where(a, b) 里的逗号不算）
        const parts = [];
        let depth = 0; let cur = "";
        for (const ch of selector) {
          if (ch === "(") depth += 1;
          if (ch === ")") depth -= 1;
          if (ch === "," && depth === 0) { parts.push(cur); cur = ""; } else cur += ch;
        }
        parts.push(cur);
        const ok = parts.every((part) => {
          const s = part.trim();
          return /^\[data-tone=/.test(s) || /^:where\(/.test(s) || /\[data-tone/.test(s);
        });
        if (!ok) offenders.push(`${file}: ${selector}`);
      }
    }
    expect(offenders).toEqual([]);
  });

  it("共享原语模块在位：ws-ui.css 紧跟 styles.css 导入", () => {
    const main = fs.readFileSync(path.join(srcDir, "main.jsx"), "utf8");
    const order = [...main.matchAll(/import\s+["']\.\/([^"']+\.css)["']/g)].map((m) => m[1]);
    expect(order[0]).toBe("styles.css");
    expect(order[1]).toBe("ws-ui.css");
  });

  it("每个样式表都由 main.jsx 导入且只导入一次（拆出来的文件忘了导入，整段样式会静默消失）", () => {
    const main = fs.readFileSync(path.join(srcDir, "main.jsx"), "utf8");
    const order = [...main.matchAll(/import\s+["']\.\/([^"']+\.css)["']/g)].map((m) => m[1]);
    const cssFiles = FILES.filter(({ file }) => file.endsWith(".css")).map(({ file }) => file.replaceAll("\\", "/")).sort();
    expect([...order].sort(), "src 下的 .css 与 main.jsx 的导入对不上").toEqual(cssFiles);
    expect(new Set(order).size, "同一个样式表导入了两次").toBe(order.length);
  });

  it("同名 @keyframes 只定义一次（后加载的同名定义会悄悄覆盖先前的）", () => {
    const seen = new Map();
    for (const { file, source } of FILES.filter(({ file }) => file.endsWith(".css"))) {
      for (const m of source.replace(/\/\*[\s\S]*?\*\//g, "").matchAll(/@keyframes\s+([A-Za-z0-9_-]+)/g)) {
        if (!seen.has(m[1])) seen.set(m[1], []);
        seen.get(m[1]).push(file);
      }
    }
    const dupes = [...seen].filter(([, files]) => files.length > 1).map(([name, files]) => `${name} ← ${files.join(", ")}`);
    expect(dupes, "共用的转圈 / 淡入 / 上浮是 ws-spin / ws-fade / ws-rise（ws-ui.css）").toEqual([]);
  });

  it("样式表里没有无人引用的规则（选择器里的类在源码、测试、冒烟脚本里都找不到）", () => {
    // 与重构时清理死样式的脚本同一口径：只要源码里出现过这个词（含 `foo-${x}` / "foo-" + x 拼出的前缀）
    // 就算有人用；一条规则的每个选择器都含一个没人用的类，才算死规则。宁可漏判，不误报。
    const root = path.join(srcDir, "..");
    const texts = FILES.filter(({ file }) => !file.endsWith(".css")).map(({ source }) => source);
    const walkTests = (dir) => {
      for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
        const abs = path.join(dir, entry.name);
        if (entry.isDirectory()) { if (entry.name !== "node_modules") walkTests(abs); }
        // 本文件不算：KNOWN_DEAD_CSS 里写着的类名不能让它自己「有人用」
        else if (/\.test\.(js|jsx)$/.test(entry.name) && entry.name !== "design-guard.test.js") texts.push(fs.readFileSync(abs, "utf8"));
      }
    };
    walkTests(srcDir);
    const scripts = path.join(root, "scripts");
    for (const entry of fs.readdirSync(scripts)) {
      if (/\.(mjs|js|cjs)$/.test(entry)) texts.push(fs.readFileSync(path.join(scripts, entry), "utf8"));
    }
    texts.push(fs.readFileSync(path.join(root, "index.html"), "utf8"));
    const tokens = new Set();
    const prefixes = new Set();
    for (const text of texts) {
      for (const m of text.matchAll(/(?<![A-Za-z0-9_-])[A-Za-z_][A-Za-z0-9_-]*/g)) tokens.add(m[0]);
      for (const m of text.matchAll(/([A-Za-z0-9_-]+-)\$\{/g)) prefixes.add(m[1]);
      for (const m of text.matchAll(/["']([A-Za-z0-9_-]+-)["']\s*\+/g)) prefixes.add(m[1]);
    }
    prefixes.delete("act-"); // 只拼 React key，不是类名
    const used = (cls) => tokens.has(cls) || [...prefixes].some((p) => cls.length > p.length && cls.startsWith(p));
    const splitTop = (selector) => {
      const parts = []; let depth = 0; let cur = "";
      for (const ch of selector) {
        if (ch === "(") depth += 1;
        if (ch === ")") depth -= 1;
        if (ch === "," && depth === 0) { parts.push(cur); cur = ""; } else cur += ch;
      }
      parts.push(cur);
      return parts.map((p) => p.trim()).filter(Boolean);
    };
    const classesOf = (selector) => [...selector
      .replace(/\[[^\]]*\]/g, "")
      .replace(/:not\((?:[^()]|\([^()]*\))*\)/g, "")
      .matchAll(/\.(-?[A-Za-z_][A-Za-z0-9_-]*)/g)].map((m) => m[1]);
    const dead = [];
    for (const { file, source } of FILES.filter(({ file }) => file.endsWith(".css"))) {
      const css = source.replace(/\/\*[\s\S]*?\*\//g, "");
      const walk = (text) => {
        let i = 0;
        while (i < text.length) {
          const open = text.indexOf("{", i);
          if (open < 0) break;
          const prelude = text.slice(i, open).replace(/^[\s;}]+/, "").trim();
          let depth = 1; let j = open + 1;
          while (j < text.length && depth) { if (text[j] === "{") depth += 1; else if (text[j] === "}") depth -= 1; j += 1; }
          const body = text.slice(open + 1, j - 1);
          if (/^@(media|supports|container|layer)\b/.test(prelude)) walk(body);
          else if (!prelude.startsWith("@")) {
            const alts = splitTop(prelude);
            if (alts.length && alts.every((alt) => classesOf(alt).some((cls) => !used(cls)))) {
              dead.push(`${file}: ${prelude.replace(/\s+/g, " ")}`);
            }
          }
          i = j;
        }
      };
      walk(css);
    }
    const unexpected = dead.filter((rule) => !KNOWN_DEAD_CSS.includes(rule));
    expect(unexpected, "这些规则没人用了：删掉它们（JSX 里改名 / 删类时，同一次改动里把样式也删掉）").toEqual([]);
    const fixed = KNOWN_DEAD_CSS.filter((rule) => !dead.includes(rule));
    expect(fixed, "这些已经不在了，把它们从 KNOWN_DEAD_CSS 里删掉").toEqual([]);
  });

  it("层级走 --z-* 刻度：高于 40 的 z-index 字面量只减不增", () => {
    // 以前 28 种字面量从 0 到 2200 互相打架：导入对话框盖住了 toast，托盘弹层压在侧栏下面。
    const above = [];
    for (const { file, source } of FILES) {
      const text = file.endsWith(".css") ? stripComments(source) : source;
      const re = file.endsWith(".css") ? /z-index\s*:\s*(-?\d+)/g : /zIndex\s*:\s*["']?(-?\d+)/g;
      for (const m of text.matchAll(re)) if (Number(m[1]) > 40) above.push(`${file}: ${m[0]}`);
    }
    expect(above.length, `浮层 / 抽屉 / 弹层 / toast 用 var(--z-*)（styles.css），不写字面量：${above.join("；")}`)
      .toBeLessThanOrEqual(MAX_Z_INDEX_LITERALS_ABOVE_40);
  });

  it("字号不小于 11px，刻度上的字号写成 --fs-* 令牌", () => {
    // 9–10.5px 的中文标签几乎认不出来（内容安全复核的证据曾是 9px）。12 / 13 / 14 / 16 / 18 / 22 / 28
    // 这几档都有令牌，写字面量的话改刻度时会漏掉。
    const TOKEN_OF = { 12: "--fs-xs", 13: "--fs-sm", 14: "--fs-md", 16: "--fs-lg", 18: "--fs-xl", 22: "--fs-2xl", 28: "--fs-3xl" };
    const tooSmall = [];
    const literal = [];
    for (const { file, source } of FILES) {
      if (file.endsWith(".css")) {
        for (const { prop, value } of cssDeclarations(source)) {
          if (prop !== "font-size" && prop !== "font") continue;
          if (/calc\(/.test(value)) continue;
          // font 简写里的字号是第一个前面不是「/」的 px 值（/ 后面是行高）
          const m = value.match(/(?<![/\d.])(\d+(?:\.\d+)?)px/);
          if (!m) continue;
          const px = Number(m[1]);
          if (px < 11) tooSmall.push(`${file}: ${prop}: ${value}`);
          else if (TOKEN_OF[px]) literal.push(`${file}: ${prop}: ${value} → var(${TOKEN_OF[px]})`);
        }
      } else {
        for (const m of source.matchAll(/fontSize\s*[:=]\s*\{?\s*["']?(\d+(?:\.\d+)?)(?:px)?["']?/g)) {
          if (Number(m[1]) < 11) tooSmall.push(`${file}: ${m[0]}`);
        }
      }
    }
    expect(tooSmall, "字号下限 11px（中文 9–10px 读不清）").toEqual([]);
    expect(literal, "这几档字号用令牌").toEqual([]);
  });

  it("--ink-4 只给占位符、禁用态和分隔符，带信息的字用 --ink-3", () => {
    // --ink-4 在白天约 2.6:1、夜间约 2.5:1，只适合「本来就不该被读」的东西。
    const allowed = /::?placeholder|:disabled|\[disabled\]|\[aria-disabled|-sep\b|\[data-empty/;
    const offenders = [];
    for (const { file, source } of FILES.filter(({ file }) => file.endsWith(".css"))) {
      for (const { selector, prop, value } of cssDeclarations(source)) {
        if (prop.startsWith("--") || !/var\(--ink-4\)/.test(value)) continue;
        if (!splitSelectorList(selector).every((s) => allowed.test(s))) offenders.push(`${file}: ${selector} { ${prop}: ${value} }`);
      }
    }
    for (const { file, source } of FILES.filter(({ file }) => !file.endsWith(".css"))) {
      for (const m of source.matchAll(/[^\n]*var\(--ink-4\)[^\n]*/g)) offenders.push(`${file}: ${m[0].trim()}`);
    }
    expect(offenders).toEqual([]);
  });

  it("页面视图共用一个版心：页头在各页之间不跳（宽度 --content-max、页边 --page-gutter，页头动作一个尺寸）", () => {
    // 以前三套：.ws-page 1080 / 56·48（主页、待办、质量、成本），.page > .page-narrow 1180 / 24·32（资料、设置、
    // 回收站），章节编排总览 1320。作者在页面之间切换时标题左右上下跳。
    const values = (file, selector, prop) => cssRules(FILES.find((f) => f.file === file).source)
      .filter((r) => r.selector === selector)
      .flatMap((r) => r.decls.filter((d) => d.prop === prop).map((d) => d.value));
    const column = "calc(var(--content-max) + 2 * var(--page-gutter))";
    const pad = "var(--page-gutter) var(--page-gutter) var(--page-pad-bottom)";
    expect(values("ws-shell.css", ".ws-page", "max-width")).toEqual([column]);
    expect(values("ws-shell.css", ".ws-page", "padding")).toEqual([pad]);
    expect(values("styles.css", ".page", "padding")).toEqual([pad]);
    expect(values("styles.css", ".page-narrow", "max-width")).toEqual(["var(--content-max)"]);
    expect(values("ws-author.css", ".arr-ov-head", "padding")).toEqual(["var(--page-gutter) var(--page-gutter) 0"]);
    expect(values("ws-author.css", ".arr-ov-head .ws-page-head", "max-width")).toEqual(["var(--content-max)"]);
    expect(values("ws-author.css", ".arr-ov-scroll", "max-width")).toEqual([column]);
    // 任何样式表都不再给这些版心另写左右页边（以前章节编排在 ≤1180 时单独改成 24px）
    const sideOverrides = FILES.filter(({ file }) => file.endsWith(".css")).flatMap(({ file, source }) => cssRules(source)
      .filter((r) => /(^|,\s*)\.(ws-page|page|arr-ov-head|arr-ov-scroll)(\s*,|$)/.test(r.selector))
      .flatMap((r) => r.decls.filter((d) => /^padding-(left|right|inline)$/.test(d.prop)).map((d) => `${file}: ${r.selector} { ${d.prop} }`)));
    expect(sideOverrides).toEqual([]);
    // 页头动作一个尺寸（默认按钮，和 Segmented 同高）：资料的「新建档案」和章节编排的「新建章节」一样大
    expect(values("ws-ui.css", ".ws-page-actions > .btn", "height")).toEqual(["34px"]);
  });

  it("断点落在文档刻度上（刻度外的只减不增）", () => {
    const found = new Set();
    for (const { file, source } of FILES.filter(({ file }) => file.endsWith(".css"))) {
      for (const m of stripComments(source).matchAll(/@media[^{]*/g)) {
        for (const q of m[0].matchAll(/((?:min|max)-(?:width|height))\s*:\s*(\d+)px/g)) {
          const cond = `${q[1]}: ${q[2]}px`;
          if (!DOCUMENTED_BREAKPOINTS.has(cond)) found.add(`${file}: ${cond}`);
        }
      }
    }
    const unexpected = [...found].filter((entry) => !KNOWN_OFF_SCALE_BREAKPOINTS.includes(entry)).sort();
    expect(unexpected, "新断点请用 1440 / 1280 / 1100 / 900 / 640（min-width 用 +1）").toEqual([]);
    const fixed = KNOWN_OFF_SCALE_BREAKPOINTS.filter((entry) => !found.has(entry));
    expect(fixed, "这些已经挪到刻度上了，把它们从 KNOWN_OFF_SCALE_BREAKPOINTS 里删掉").toEqual([]);
  });

  it("颜色只在令牌里写死：样式规则不用十六进制 / rgb() / black / white，也不拿墨色调半透明阴影", () => {
    // 写死的颜色不跟主题走：#fff 压在夜间变亮的强调色上不到 3:1，color-mix(--ink-1, transparent)
    // 的遮罩 / 阴影在夜间变成一层亮雾。遮罩图（mask-image）里的 #000 只取 alpha，不算颜色。
    const raw = /#[0-9a-fA-F]{3,8}\b|\brgba?\(|\bhsla?\(|(?<![-\w])(?:black|white)(?![-\w])/;
    const offenders = [];
    for (const { file, source } of FILES.filter(({ file }) => file.endsWith(".css"))) {
      for (const { selector, prop, value } of cssDeclarations(source)) {
        if (prop.startsWith("--") || /mask(-image)?$/.test(prop)) continue;
        if (raw.test(value)) offenders.push(`${file}: ${selector} { ${prop}: ${value} }`);
        else if (/color-mix\([^;]*var\(--ink-[1-4]\)\s*\d+%\s*,\s*transparent/.test(value)) {
          offenders.push(`${file}: ${selector} { ${prop}: ${value} }（用 var(--scrim)）`);
        }
      }
    }
    expect(offenders).toEqual([]);
  });

  it("字不用颜料本色压在同色的 -wash 底上（白天 / 护眼主题不到 4.5:1），用对应的 --*-ink", () => {
    // .btn-danger 曾是 rose 字压 rose-wash：白天 3.85:1、护眼 3.53:1，13px 的「删除作品」「清空回收站」
    // 都读不清；--danger-ink 是 5.5 / 5.05 / 6.4:1。语义别名按颜料归一（danger = rose 等）。
    const FAMILY = { crimson: "crimson", accent: "crimson", gold: "gold", warn: "gold", sage: "sage", ok: "sage",
      slate: "slate", info: "slate", rose: "rose", danger: "rose", tone: "tone" };
    const found = [];
    for (const { file, source } of FILES.filter(({ file }) => file.endsWith(".css"))) {
      for (const { selector, decls } of cssRules(source)) {
        const last = (...props) => [...decls].reverse().find(({ prop }) => props.includes(prop))?.value;
        const text = last("color")?.match(/^var\(--([a-z]+)(?![-\w])/);
        const fill = last("background", "background-color")?.match(/var\(--([a-z]+)-wash(?![-\w])/);
        if (text && fill && FAMILY[text[1]] && FAMILY[text[1]] === FAMILY[fill[1]]) found.push(`${file}: ${selector}`);
      }
    }
    const unexpected = found.filter((rule) => !ICON_ONLY_PIGMENT_ON_WASH.includes(rule));
    expect(unexpected, "wash 底上的字用 --accent-ink / --ok-ink / --warn-ink / --danger-ink / --info-ink（或 --tone-ink）").toEqual([]);
    const stale = ICON_ONLY_PIGMENT_ON_WASH.filter((rule) => !found.includes(rule));
    expect(stale, "这些已经不是颜料压 wash 了，把它们从 ICON_ONLY_PIGMENT_ON_WASH 里删掉").toEqual([]);
  });

  it("JSX / JS 里也不写死颜色：style 对象、fill / stroke / color 属性一律用 var(--x) 令牌", () => {
    // 审计时写死的颜色一半在 JSX 里（成本看板、文学质量、AI 设置、调参面板）：夜间主题下白字压在
    // 变亮的强调色上、黑线画在深纸上。样式表那条守卫看不到 style={{ … }}，这里补上。注释不算。
    const raw = /#[0-9a-fA-F]{3,8}(?![\w-])|\brgba?\(|\bhsla?\(|(?<![-\w])(?:black|white)(?![-\w])/;
    const named = /["'`]\s*(?:red|green|blue|gray|grey|orange|yellow|purple|pink|brown|silver|navy|maroon|teal|olive|lime|aqua|fuchsia)\s*["'`]/;
    const offenders = [];
    for (const { file, source } of FILES.filter(({ file }) => !file.endsWith(".css"))) {
      // 块注释换成等量换行（保住行号），再去掉行尾的 // 注释（前面是空白或标点才算，放过 http://）
      const code = source.replace(/\/\*[\s\S]*?\*\//g, (m) => m.replace(/[^\n]/g, ""));
      code.split("\n").forEach((line, index) => {
        const bare = line.replace(/(^|[\s;{}(),])\/\/.*$/, "$1");
        if (raw.test(bare) || named.test(bare)) offenders.push(`${file}:${index + 1}: ${bare.trim()}`);
      });
    }
    expect(offenders.length, `颜色写成 var(--x)（styles.css 的令牌；半透明用 color-mix(in srgb, var(--x) N%, transparent)）：${offenders.join("；")}`)
      .toBeLessThanOrEqual(MAX_JSX_RAW_COLOURS);
  });
});

function stripComments(css) {
  return css.replace(/\/\*[\s\S]*?\*\//g, "");
}

function splitSelectorList(selector) {
  const parts = []; let depth = 0; let cur = "";
  for (const ch of selector) {
    if (ch === "(") depth += 1;
    if (ch === ")") depth -= 1;
    if (ch === "," && depth === 0) { parts.push(cur); cur = ""; } else cur += ch;
  }
  parts.push(cur);
  return parts.map((p) => p.trim()).filter(Boolean);
}

// 每条普通规则：{ selector, decls: [{ prop, value }] }（@media 等条件规则里的也算，@keyframes 帧不算）
function cssRules(source) {
  const out = [];
  const walk = (text) => {
    let i = 0;
    while (i < text.length) {
      const open = text.indexOf("{", i);
      if (open < 0) break;
      const prelude = text.slice(i, open).replace(/^[\s;}]+/, "").trim();
      let depth = 1; let j = open + 1;
      while (j < text.length && depth) { if (text[j] === "{") depth += 1; else if (text[j] === "}") depth -= 1; j += 1; }
      const body = text.slice(open + 1, j - 1);
      if (/^@(media|supports|container|layer)\b/.test(prelude)) walk(body);
      else if (!prelude.startsWith("@")) {
        const decls = [];
        for (const decl of body.split(";")) {
          const colon = decl.indexOf(":");
          if (colon < 0) continue;
          decls.push({ prop: decl.slice(0, colon).trim().toLowerCase(), value: decl.slice(colon + 1).trim() });
        }
        out.push({ selector: prelude.replace(/\s+/g, " "), decls });
      }
      i = j;
    }
  };
  walk(stripComments(source));
  return out;
}

// 每条普通规则的每个声明：{ selector, prop, value }
function cssDeclarations(source) {
  return cssRules(source).flatMap(({ selector, decls }) => decls.map((decl) => ({ selector, ...decl })));
}
