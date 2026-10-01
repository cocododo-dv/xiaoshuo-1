// DEV 测试接缝（ws-test-seam.js）：契约 E2E 冒烟经 window.__wsStores.load(...) 拿到的，就是应用自己 import 的那个 store。
import { afterEach, describe, expect, it, vi } from "vitest";
import { installApiRouter } from "./test-helpers.js";

vi.mock("./lib/client.js", () => ({
  apiGet: vi.fn(),
  apiPost: vi.fn(),
  apiPatch: vi.fn(),
  apiDelete: vi.fn(),
}));

describe("DEV 测试接缝 window.__wsStores", () => {
  afterEach(() => {
    delete window.__wsStores;
    vi.resetModules();
  });

  it("开发态才装；load 按名字给出应用正在用的那个模块实例（含还没加载的资料库 store）", async () => {
    const client = await import("./lib/client.js");
    installApiRouter(client);
    const { installTestSeam } = await import("./ws-test-seam.js");
    expect(import.meta.env.DEV).toBe(true);
    installTestSeam();
    expect(window.__wsStores.names).toEqual(["WsWorks", "WsCatalog", "WrDocs", "rvOpenItems", "libLive", "LIB_persist"]);

    const { WsWorks } = await import("./ws-works.jsx");
    const { WsCatalog } = await import("./ws-catalog.jsx");
    const loaded = await window.__wsStores.load("WsWorks", "WsCatalog", "LIB_persist", "libLive");
    expect(loaded.WsWorks).toBe(WsWorks);
    expect(loaded.WsCatalog).toBe(WsCatalog);
    const library = await import("./ws-library-store.js");
    expect(loaded.LIB_persist).toBe(library.LIB_persist);
    expect(loaded.libLive).toBe(library.libLive);
    // 只是加载资料库的 store 不发请求：第一个订阅者才拉
    expect(client.apiGet.mock.calls.some(([url]) => String(url).includes("/library"))).toBe(false);
  });

  it("不认识的名字直接报错，并列出有哪些", async () => {
    const client = await import("./lib/client.js");
    installApiRouter(client);
    const { installTestSeam } = await import("./ws-test-seam.js");
    installTestSeam();
    await expect(window.__wsStores.load("SnowSync")).rejects.toThrow(/没有「SnowSync」.*WsWorks/);
  });
});
