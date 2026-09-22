// lib/platform.js：快捷键提示按平台写（Mac ⌘，其余 Ctrl）——写作台、侧栏、命令面板共用这一份。
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";
import { isMacPlatform, modEnterShortcut, modKeyLabel, modShortcut } from "./platform.js";
import { modCombo, modKey } from "../ws-writer-keys.js";

const srcDir = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");

describe("lib/platform", () => {
  it("按 userAgentData → platform → userAgent 的顺序认平台", () => {
    expect(isMacPlatform({ platform: "MacIntel" })).toBe(true);
    expect(isMacPlatform({ userAgentData: { platform: "macOS" }, platform: "Win32" })).toBe(true);
    expect(isMacPlatform({ userAgentData: { platform: "Windows" }, platform: "MacIntel" })).toBe(false);
    expect(isMacPlatform({ userAgent: "Mozilla/5.0 (iPad; CPU OS 17_0 like Mac OS X)" })).toBe(true);
    expect(isMacPlatform({ platform: "Win32" })).toBe(false);
    expect(isMacPlatform({ platform: "Linux x86_64" })).toBe(false);
    // 显式 null（没有 navigator）按非 Mac 处理；读属性抛错也不让提示渲染失败
    expect(isMacPlatform(null)).toBe(false);
    expect(isMacPlatform({ get platform() { throw new Error("blocked"); } })).toBe(false);
  });

  it("修饰键与组合键：Mac 是 ⌘K，其余一律 Ctrl+K（与 Windows 惯例一致，不再有「Ctrl K」）", () => {
    expect(modKeyLabel({ platform: "MacIntel" })).toBe("⌘");
    expect(modKeyLabel({ platform: "Win32" })).toBe("Ctrl");
    expect(modShortcut("K", { platform: "MacIntel" })).toBe("⌘K");
    expect(modShortcut("K", { platform: "Win32" })).toBe("Ctrl+K");
    expect(modShortcut(".", { platform: "Linux x86_64" })).toBe("Ctrl+.");
    expect(modEnterShortcut({ platform: "MacIntel" })).toBe("⌘↵");
    expect(modEnterShortcut({ platform: "Win32" })).toBe("Ctrl+Enter");
  });

  it("不传 nav 时读当前浏览器（jsdom 不是 Mac）", () => {
    expect(isMacPlatform()).toBe(isMacPlatform(globalThis.navigator));
    expect(modShortcut("J")).toBe(isMacPlatform() ? "⌘J" : "Ctrl+J");
  });

  it("写作台的 modKey / modCombo 就是这一份实现（不是第二份拷贝）", () => {
    expect(modKey).toBe(modKeyLabel);
    expect(modCombo).toBe(modShortcut);
  });

  it("除 lib/platform.js 外没有模块自己嗅探平台（过去写作台与外壳各有一份，格式还不一样）", () => {
    const sniffers = [];
    const walk = (dir) => {
      for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
        const abs = path.join(dir, entry.name);
        if (entry.isDirectory()) { if (entry.name !== "node_modules") walk(abs); continue; }
        if (!/\.(js|jsx)$/.test(entry.name) || entry.name.includes(".test.")) continue;
        const source = fs.readFileSync(abs, "utf8");
        if (/userAgentData|navigator\.platform|\.platform\s*\|\|/.test(source)) sniffers.push(path.relative(srcDir, abs).replaceAll("\\", "/"));
      }
    };
    walk(srcDir);
    expect(sniffers).toEqual(["lib/platform.js"]);
  });

  it("界面文字里没有写死的 ⌘：提示一律经由这里按平台给（Windows / Linux 上是 Ctrl）", () => {
    const hardcoded = [];
    const walk = (dir) => {
      for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
        const abs = path.join(dir, entry.name);
        if (entry.isDirectory()) { if (entry.name !== "node_modules") walk(abs); continue; }
        if (!/\.(js|jsx)$/.test(entry.name) || entry.name.includes(".test.") || entry.name === "platform.js") continue;
        const lines = fs.readFileSync(abs, "utf8").split("\n");
        lines.forEach((line, i) => {
          const code = line.replace(/\/\/.*$|\/\*.*?\*\/|^\s*\*.*$|^\s*\/\*.*$/g, "");
          // 引号 / 模板串里的 ⌘，或 JSX 文本里的 ⌘（注释不算）
          if (/["'`][^"'`]*⌘/.test(code) || />[^<{]*⌘/.test(code)) hardcoded.push(`${path.relative(srcDir, abs)}:${i + 1}`);
        });
      }
    };
    walk(srcDir);
    expect(hardcoded).toEqual([]);
  });
});
