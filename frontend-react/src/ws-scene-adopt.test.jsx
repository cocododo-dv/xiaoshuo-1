// 起草台「采纳并归档」的预检：先等服务器上的作者稿（F03-05）。
// WrDocs.hydrate 遇到别处正在进行的水合会立刻返回、出错也不抛，预检不能只靠它。
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { installApiRouter } from "./test-helpers.js";

vi.mock("./lib/client.js", () => ({
  apiGet: vi.fn(), apiPost: vi.fn(), apiPatch: vi.fn(), apiDelete: vi.fn(),
}));

const T = { timeout: 5000, interval: 25 };

function deferred() {
  let resolve;
  let reject;
  const promise = new Promise((ok, fail) => { resolve = ok; reject = fail; });
  return { promise, resolve, reject };
}

async function loadAdoption() {
  const client = await import("./lib/client.js");
  installApiRouter(client);
  await import("./ws-catalog.jsx");
  await vi.waitFor(() => expect(window.WsWorks && window.WsWorks.activeId()).toBe("prj-main"), T);
  await vi.waitFor(() => expect(window.WsCatalog && window.WsCatalog.get().length).toBeGreaterThan(0), T);
  const api = await import("./ws-scene-api.js");
  const store = await import("./wr-doc-store.jsx");
  return { client, api, store };
}

const DRAFT = [{ id: "p1", text: "AI 起草的一段。" }];

beforeEach(() => {
  vi.resetModules();
  window.localStorage.clear();
});
afterEach(() => vi.restoreAllMocks());

describe("scnPrepareAdoption · 预检先等服务器上的作者稿", () => {
  it("写作台的水合还在路上：等它落地，看得见作者正文（不当成空稿直接覆盖）", async () => {
    const { client, api, store } = await loadAdoption();
    const ensure = deferred();
    client.apiPost.mockImplementation((url) => (
      /\/author-drafts\/scene\/s1\/ensure$/.test(url) ? ensure.promise : Promise.resolve({})
    ));
    store.WrDocs.load("ch01s1"); // 写作台的预热水合先发出去
    const preview = api.scnPrepareAdoption("ch01s1", DRAFT);
    ensure.resolve({ draft: { draft_id: "d1", revision_no: 2, content: "<p>作者自己写的正文。</p>" } });
    const result = await preview;
    expect(result.hasReal).toBe(true);
    expect(result.existing).toContain("作者自己写的正文");
  });

  it("服务器读不到：停下来说「无法核对服务器上的作者稿」", async () => {
    const { client, api } = await loadAdoption();
    const down = Object.assign(new Error("offline"), { code: "NETWORK_ERROR" });
    client.apiPost.mockImplementation((url) => (
      /\/author-drafts\/scene\/s1\/ensure$/.test(url) ? Promise.reject(down) : Promise.resolve({})
    ));
    await expect(api.scnPrepareAdoption("ch01s1", DRAFT)).rejects.toMatchObject({ code: "AUTHOR_DRAFT_PREFLIGHT_FAILED" });
  });
});
