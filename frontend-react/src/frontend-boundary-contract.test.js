import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { afterEach, describe, expect, it } from "vitest";

import { apiConnectOrigin, withApiConnectSrc } from "../build-csp.js";


const srcRoot = path.dirname(fileURLToPath(import.meta.url));
const projectRoot = path.dirname(srcRoot);


describe("frontend security, navigation, and accessibility boundaries", () => {
  it("records user navigation in browser history while keeping alias redirects replace-only", () => {
    const source = fs.readFileSync(path.join(srcRoot, "ws-app.jsx"), "utf8");

    expect(source).toContain('history.pushState(null, "", "#" + v)');
    expect(source).toContain('history.replaceState(null, "", "#" + target)');
  });

  it("uses a keyboard-focusable button for the inbox navigation action", () => {
    const source = fs.readFileSync(path.join(srcRoot, "ws-home.jsx"), "utf8");

    expect(source).toContain('<button type="button" className="home-card-go home-card-go-btn"');
    expect(source).not.toContain('<span className="home-card-go home-card-go-btn"');
  });

  it("ships a local-only font path and a baseline content security policy", () => {
    const html = fs.readFileSync(path.join(projectRoot, "index.html"), "utf8");

    expect(html).toContain('http-equiv="Content-Security-Policy"');
    expect(html).toContain("object-src 'none'");
    expect(html).not.toContain("fonts.googleapis.com");
    expect(html).not.toContain("fonts.gstatic.com");
    expect(html).not.toContain("潮汐档案");
  });
});


describe("CSP connect-src 跟着构建时的后端地址走", () => {
  const html = fs.readFileSync(path.join(projectRoot, "index.html"), "utf8");
  const connectSrc = (text) => (text.match(/connect-src ([^;"]*)/) || [])[1].split(/\s+/);
  const LOOPBACK_DEFAULTS = ["'self'", "http://127.0.0.1:*", "http://localhost:*", "ws://127.0.0.1:*", "ws://localhost:*"];
  const savedApiBase = process.env.VITE_NOVEL_SYSTEM_API_BASE;
  afterEach(() => {
    if (savedApiBase === undefined) delete process.env.VITE_NOVEL_SYSTEM_API_BASE;
    else process.env.VITE_NOVEL_SYSTEM_API_BASE = savedApiBase;
  });

  it("index.html 默认只放行本机回环后端（开发服务器的 HMR 也走回环）", () => {
    expect(connectSrc(html)).toEqual(LOOPBACK_DEFAULTS);
  });

  it("回环、相对地址或写错的地址不改策略；内网 / https 后端补进它的 origin", () => {
    for (const base of [undefined, "", "http://127.0.0.1:8000", " http://localhost:8009/ ", "/api", "not a url"]) {
      expect(apiConnectOrigin(base), String(base)).toBeNull();
      expect(withApiConnectSrc(html, base), String(base)).toBe(html);
    }
    expect(apiConnectOrigin("https://127.0.0.1:8443")).toBe("https://127.0.0.1:8443");
    const remote = withApiConnectSrc(html, "http://10.0.0.5:8000");
    expect(connectSrc(remote)).toEqual([...LOOPBACK_DEFAULTS, "http://10.0.0.5:8000"]);
    const proxied = withApiConnectSrc(html, "https://novel.example.test/api/");
    expect(connectSrc(proxied)).toEqual([...LOOPBACK_DEFAULTS, "https://novel.example.test"]);
    // 只动 connect-src：其它指令一字不改，重复处理不重复追加
    expect(proxied.replace(" https://novel.example.test", "")).toBe(html);
    expect(withApiConnectSrc(proxied, "https://novel.example.test")).toBe(proxied);
  });

  it("vite.config.js 按 VITE_NOVEL_SYSTEM_API_BASE 装上这条策略", async () => {
    const { default: config } = await import("../vite.config.js");
    const transformWith = (apiBase) => {
      if (apiBase === undefined) delete process.env.VITE_NOVEL_SYSTEM_API_BASE;
      else process.env.VITE_NOVEL_SYSTEM_API_BASE = apiBase;
      const resolved = config({ mode: "production", command: "build" });
      const plugin = resolved.plugins.flat().find((p) => p && p.name === "novel-system-csp-connect-src");
      expect(plugin).toBeTruthy();
      return plugin.transformIndexHtml(html);
    };
    expect(transformWith(undefined)).toBe(html);
    expect(transformWith("http://127.0.0.1:8009")).toBe(html);
    expect(connectSrc(transformWith("http://10.0.0.5:8000"))).toContain("http://10.0.0.5:8000");
  });
});
