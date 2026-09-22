// 风格参考 2026-09-21 拆分（第二阶段）的结构契约：
//   (a) store 是叶子：只 import lib/client.js 与纯派生 ws-styleref-model.js，不碰任何界面模块；
//       model 不 import 任何东西；注入内容的只读视图不发请求；
//   (b) 风格参考只有外壳 ws-styleref.jsx 写 window，其余模块也不再经 window.srX 调自己的函数；
//   (c) store 单独加载就能用：没接界面时提示退回 alert、确认一律当「取消」、当前作品当没有；
//       界面层经 srConfigureHost 接上之后，提示 / 当前作品走接上的那一份。
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("./lib/client.js", () => ({
  apiGet: vi.fn(), apiPost: vi.fn(), apiPatch: vi.fn(), apiDelete: vi.fn(),
  buildUrl: (p) => p,
  getOperatorRef: () => "operator",
  getRemoteAccessToken: () => null,
}));

const srcDir = path.dirname(fileURLToPath(import.meta.url));
const read = (name) => fs.readFileSync(path.join(srcDir, name), "utf8");
const relativeImports = (source) => [...source.matchAll(/(?:import|export)\s+(?:[^'"]*?\s+from\s+)?['"](\.[^'"]+)['"]/g)].map((m) => m[1]);
const styleRefModules = () => fs.readdirSync(srcDir).filter((n) => /^ws-styleref.*\.(js|jsx)$/.test(n) && !n.includes(".test."));

describe("风格参考的模块边界", () => {
  it("store 只依赖 client 与纯派生；model 没有依赖；只读视图不发请求", () => {
    expect(relativeImports(read("ws-styleref-store.js")).sort()).toEqual(["./lib/client.js", "./ws-styleref-model.js"]);
    expect(relativeImports(read("ws-styleref-model.js"))).toEqual([]);
    const inject = relativeImports(read("ws-styleref-inject.jsx"));
    expect(inject).not.toContain("./ws-styleref-store.js");
    expect(inject).not.toContain("./lib/client.js");
  });

  it("除了外壳 ws-styleref.jsx，风格参考的模块都不直接调后端、不写 window、不经 window.srX 调自己", () => {
    const modules = styleRefModules();
    expect(modules).toEqual(expect.arrayContaining([
      "ws-styleref.jsx", "ws-styleref-store.js", "ws-styleref-ui.jsx", "ws-styleref-activity.jsx", "ws-styleref-library.jsx",
      "ws-styleref-overview.jsx", "ws-styleref-matrix.jsx", "ws-styleref-profile.jsx", "ws-styleref-apply.jsx", "ws-styleref-inject.jsx",
      "ws-styleref-val.jsx", "ws-styleref-model.js",
    ]));
    for (const name of modules) {
      const source = read(name);
      expect(source, name).not.toMatch(/window\.sr[A-Z]\w*\s*\(/);
      if (name === "ws-styleref.jsx") continue;
      expect(source, name).not.toMatch(/Object\.assign\(window|window\.[A-Za-z_$][\w$]*\s*=/);
      // 请求都在 store：界面模块不 import apiGet / apiPost / apiDelete
      if (name !== "ws-styleref-store.js") expect(source, name).not.toMatch(/\bapi(Get|Post|Patch|Delete)\b/);
    }
  });
});

describe("store 单独加载", () => {
  beforeEach(() => {
    vi.resetModules();
    vi.spyOn(window, "alert").mockImplementation(() => {});
    vi.spyOn(window, "confirm").mockReturnValue(true);
  });
  afterEach(() => { vi.restoreAllMocks(); });

  it("没接界面：失败提示退回 alert，「字数太少仍要抽取吗」一律当取消（不强制重跑）", async () => {
    const client = await import("./lib/client.js");
    client.apiGet.mockResolvedValue({ books: [] });
    client.apiPost.mockRejectedValue(Object.assign(new Error("太少"), { code: "STYLE_REFERENCE_INPUT_TOO_SMALL" }));
    const store = await import("./ws-styleref-store.js");
    await store.srBookAction("rerun", "bk1");
    expect(client.apiPost).toHaveBeenCalledTimes(1);
    expect(window.confirm).not.toHaveBeenCalled();

    client.apiPost.mockRejectedValue(Object.assign(new Error("坏了"), { code: "BOOM" }));
    await store.srBookAction("rerun", "bk1");
    expect(window.alert).toHaveBeenCalledWith("操作失败：坏了");
    store.srActivityStop();
  });

  it("接上界面后：提示走接上的 notify，确认走接上的 confirm，书库附加事实按接上的当前作品查询", async () => {
    const client = await import("./lib/client.js");
    client.apiGet.mockImplementation((url) => {
      if (url === "/api/v2/style-reference/books") return Promise.resolve({ books: [{ book_id: "bk1", title: "甲书", total_chars: 10, status: "ready" }] });
      if (url === "/api/v2/style-reference/profiles") return Promise.resolve({ profiles: [{ profile_id: "p1", book_id: "bk1", status: "active" }] });
      if (url.startsWith("/api/v2/style-reference/injection/layers?")) return Promise.resolve({ layers: [{ scope: "project", profile_id: "p1" }] });
      return Promise.resolve({});
    });
    let first = true;
    client.apiPost.mockImplementation(() => {
      if (first) { first = false; return Promise.reject(Object.assign(new Error("太少"), { code: "STYLE_REFERENCE_INPUT_TOO_SMALL" })); }
      return Promise.resolve({});
    });
    const store = await import("./ws-styleref-store.js");
    const notify = vi.fn();
    const confirm = vi.fn().mockResolvedValue(true);
    store.srConfigureHost({ activeWorkId: () => "prj-9", notify, confirm });

    await store.srBookAction("rerun", "bk1");
    expect(confirm).toHaveBeenCalledTimes(1);
    expect(client.apiPost.mock.calls.map((c) => c[1])).toEqual([{ background: true, force: false }, { background: true, force: true }]);

    await store.srSyncBooks();
    await store.srSyncMeta();
    const layersCall = client.apiGet.mock.calls.map((c) => c[0]).find((u) => u.startsWith("/api/v2/style-reference/injection/layers?"));
    expect(layersCall).toContain("project_id=prj-9");
    expect(store.srBookApplied("bk1")).toBe(true);

    client.apiPost.mockRejectedValue(Object.assign(new Error("忙"), { code: "STYLE_REFERENCE_RUN_ALREADY_ACTIVE" }));
    await store.srBookAction("rerun", "bk1");
    expect(notify).toHaveBeenCalledWith(expect.stringContaining("已经在抽取了"), "warn");
    expect(window.alert).not.toHaveBeenCalled();
    store.srActivityStop();
  });

  it("srSubscribe 返回退订函数：退订后不再收到广播", async () => {
    const store = await import("./ws-styleref-store.js");
    const listener = vi.fn();
    const off = store.srSubscribe("books", "deep")(listener);
    store.srDropDeep("bk1");
    expect(listener).toHaveBeenCalledTimes(1);
    off();
    store.srDropDeep("bk1");
    expect(listener).toHaveBeenCalledTimes(1);
  });
});
