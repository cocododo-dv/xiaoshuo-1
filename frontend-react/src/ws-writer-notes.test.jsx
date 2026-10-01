import React, { act } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("./lib/client.js", () => ({
  apiGet: vi.fn(), apiPost: vi.fn(), apiPatch: vi.fn(), apiDelete: vi.fn(),
}));

function deferred() {
  let resolve;
  let reject;
  const promise = new Promise((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

async function flushPromises() {
  await act(async () => {
    await Promise.resolve();
    await Promise.resolve();
  });
}

async function changeTextarea(node, value) {
  const setter = Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, "value").set;
  await act(async () => {
    setter.call(node, value);
    node.dispatchEvent(new Event("input", { bubbles: true }));
    node.dispatchEvent(new Event("change", { bubbles: true }));
  });
}

async function loadNotes() {
  const client = await import("./lib/client.js");
  const { WsCatalog } = await import("./ws-catalog.jsx");
  vi.spyOn(WsCatalog, "backendSceneId").mockImplementation(async (scene) => `backend-${scene}`);
  const { WrCtxNotes } = await import("./ws-writer.jsx");
  return { client, WrCtxNotes };
}

describe("写作台场景笔记的异步隔离", () => {
  let host;
  let root;

  beforeEach(() => {
    vi.resetModules();
    vi.useFakeTimers();
    window.localStorage.clear();
    host = document.createElement("div");
    document.body.appendChild(host);
    root = createRoot(host);
  });

  afterEach(async () => {
    await act(async () => root.unmount());
    host.remove();
    vi.useRealTimers();
    vi.restoreAllMocks();
  });

  it("切换场景后不会把旧场景排队中的保存写到新场景", async () => {
    const firstPatch = deferred();
    const { client, WrCtxNotes } = await loadNotes();
    client.apiGet.mockResolvedValue({ notes: "", revision_no: 1 });
    client.apiPatch.mockImplementationOnce(() => firstPatch.promise);

    await act(async () => root.render(<WrCtxNotes scene="scene-a" />));
    await flushPromises();
    const textarea = host.querySelector("textarea");

    await changeTextarea(textarea, "first edit");
    await act(async () => {
      vi.advanceTimersByTime(500);
      await Promise.resolve();
    });
    expect(client.apiPatch).toHaveBeenCalledTimes(1);

    await changeTextarea(textarea, "queued edit");
    await act(async () => {
      vi.advanceTimersByTime(500);
      await Promise.resolve();
    });
    await act(async () => root.render(<WrCtxNotes scene="scene-b" />));
    await flushPromises();

    firstPatch.resolve({ revision_no: 2 });
    await flushPromises();

    // 排队中的那一次仍然存回旧场景（带旧场景自己的修订号），绝不会落到新场景上
    expect(client.apiPatch).toHaveBeenCalledTimes(2);
    expect(client.apiPatch.mock.calls[0][0]).toBe("/api/v1/scenes/backend-scene-a/author-notes");
    expect(client.apiPatch.mock.calls[1]).toEqual([
      "/api/v1/scenes/backend-scene-a/author-notes",
      { notes: "queued edit", base_revision_no: 2 },
    ]);
    expect(client.apiPatch.mock.calls.some(([url]) => url.includes("backend-scene-b"))).toBe(false);
  });

  it("没到自动保存就换场：离开时把这一场的笔记存上，回来不报「与其他设备冲突」（F03-06）", async () => {
    const { client, WrCtxNotes } = await loadNotes();
    const server = { "backend-scene-a": { notes: "", revision_no: 1 }, "backend-scene-b": { notes: "", revision_no: 1 } };
    client.apiGet.mockImplementation(async (url) => server[url.split("/")[4]]);
    client.apiPatch.mockImplementation(async (url, body) => {
      const key = url.split("/")[4];
      server[key] = { notes: body.notes, revision_no: body.base_revision_no + 1 };
      return server[key];
    });

    await act(async () => root.render(<WrCtxNotes scene="scene-a" />));
    await flushPromises();
    await changeTextarea(host.querySelector("textarea"), "伏笔：旧信");
    // 0.5 秒的防抖还没到就换场
    await act(async () => root.render(<WrCtxNotes scene="scene-b" />));
    await flushPromises();
    expect(client.apiPatch).toHaveBeenCalledWith(
      "/api/v1/scenes/backend-scene-a/author-notes",
      { notes: "伏笔：旧信", base_revision_no: 1 },
    );

    await act(async () => root.render(<WrCtxNotes scene="scene-a" />));
    await flushPromises();
    await flushPromises();
    expect(host.querySelector("textarea").value).toBe("伏笔：旧信");
    expect(host.querySelector(".wr-notes-state").textContent).toContain("已存服务器");
    expect(host.textContent).not.toContain("与其他设备冲突");
  });

  it("服务器初始读取晚到时不会覆盖用户刚输入的本地笔记", async () => {
    const initialLoad = deferred();
    const { client, WrCtxNotes } = await loadNotes();
    client.apiGet.mockImplementation(() => initialLoad.promise);

    await act(async () => root.render(<WrCtxNotes scene="scene-a" />));
    await flushPromises();
    const textarea = host.querySelector("textarea");
    await changeTextarea(textarea, "local draft");

    initialLoad.resolve({ notes: "server note", revision_no: 4 });
    await flushPromises();

    expect(textarea.value).toBe("local draft");
    expect([...Array(window.localStorage.length).keys()]
      .map((index) => window.localStorage.key(index))
      .some((key) => key.startsWith("wr-notes-pending:scene-a"))).toBe(true);
  });

  it("开着笔记时目录给这一场换了名字（临时 sid → scene_id）：没存上的笔记跟到新名字下、照样显示，状态是未同步", async () => {
    const { client, WrCtxNotes } = await loadNotes();
    const { WsCatalog } = await import("./ws-catalog.jsx");
    vi.spyOn(WsCatalog, "sceneById").mockImplementation((sid) => (sid === "tmp_1" || sid === "s9" ? { scene: { sid: "s9" } } : null));
    client.apiGet.mockResolvedValue({ notes: "", revision_no: 1 });
    client.apiPatch.mockRejectedValue(Object.assign(new Error("offline"), { code: "NETWORK_ERROR" }));

    await act(async () => root.render(<WrCtxNotes scene="tmp_1" />));
    await flushPromises();
    await changeTextarea(host.querySelector("textarea"), "新建时记下的伏笔");
    await act(async () => root.render(<WrCtxNotes scene="s9" />));
    await flushPromises();
    await flushPromises();
    await flushPromises();

    expect(host.querySelector("textarea").value).toBe("新建时记下的伏笔");
    expect(host.querySelector(".wr-notes-state").textContent).toContain("未同步");
    const keys = [...Array(window.localStorage.length).keys()].map((index) => window.localStorage.key(index));
    expect(keys.some((key) => key.startsWith("wr-notes-pending:s9"))).toBe(true);
    expect(keys.some((key) => key.startsWith("wr-notes:tmp_1") || key.startsWith("wr-notes-pending:tmp_1"))).toBe(false);
  });
});
