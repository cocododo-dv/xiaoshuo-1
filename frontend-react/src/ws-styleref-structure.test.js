// 风格参考前端的模块边界：store 只依赖 lib/client.js 与纯派生的 model；model 只依赖词表；这一族文件都不写 window
// （旧版挂在 window 上的 19 个全局已经删掉），也不再调用 v3 删掉的端点。
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

const srcDir = path.dirname(fileURLToPath(import.meta.url));
const read = (name) => fs.readFileSync(path.join(srcDir, name), "utf8");
const family = fs.readdirSync(srcDir).filter((name) => /^ws-styleref[\w-]*\.(js|jsx)$/.test(name) && !/\.test\./.test(name));
const imports = (source) => [...source.matchAll(/^import\s[^;]*?from\s+["']([^"']+)["']/gm)].map((m) => m[1]);

describe("风格参考前端的模块边界", () => {
  it("一族文件都在：外壳、store、说法、界面零件与各步", () => {
    expect(family.sort()).toEqual([
      "ws-styleref-activity.jsx", "ws-styleref-apply.jsx", "ws-styleref-check.jsx", "ws-styleref-fidelity.jsx",
      "ws-styleref-learn.jsx", "ws-styleref-library.jsx", "ws-styleref-model.js", "ws-styleref-overview.jsx",
      "ws-styleref-portrait.jsx", "ws-styleref-scene-preview.jsx", "ws-styleref-store.js", "ws-styleref-ui.jsx",
      "ws-styleref.jsx",
    ]);
  });

  it("store 只 import lib/client.js 与纯派生的 model；model 只 import 词表与纯文字工具", () => {
    expect(imports(read("ws-styleref-store.js")).sort()).toEqual(["./lib/client.js", "./ws-styleref-model.js"]);
    // lib/messages.js：「后端原话能不能给作者看」的纯函数（对照检查的 model 也用它），无状态、无副作用
    expect(imports(read("ws-styleref-model.js")).sort()).toEqual(["./lib/messages.js", "./ws-labels.js"]);
  });

  it("不写 window：没有 Object.assign(window…) 与 window.x = …；旧的全局名一个都不剩", () => {
    const legacyGlobals = [
      "SR_BOOKS", "SR_ACTIVITY", "SR_DETAIL", "srSyncBooks", "srSelectBook", "srRunImport", "srPollRun",
      "WsStyleRefStore", "SrActivityPanel",
    ];
    for (const name of family) {
      const source = read(name);
      expect(source, name).not.toMatch(/Object\.assign\(\s*window/);
      expect(source, name).not.toMatch(/window\.[A-Za-z_$][\w$]*\s*=[^=]/);
      for (const g of legacyGlobals) expect(source, `${name} 读了 window.${g}`).not.toContain(`window.${g}`);
    }
  });

  it("上传也走 lib/client（FormData），不再自己 fetch", () => {
    for (const name of family) expect(read(name), name).not.toMatch(/\bfetch\(/);
  });

  it("不再调用 v3 删掉的端点", () => {
    const removed = [
      /\/imports\//, /\/injection\/task-defaults/, /\/bindings\/[^"'`]*\/injection-preview/, /\/profiles\/[^"'`/]*\/preview["'`]/,
      /\/validate\b/, /\/reports\b/, /\/runs\/\$\{[^}]+\}["'`]/, /\/review-items/, /finding-feedback|\/feedback\b/,
    ];
    for (const name of family) {
      const source = read(name);
      for (const pattern of removed) expect(source, `${name} ~ ${pattern}`).not.toMatch(pattern);
    }
  });
});
