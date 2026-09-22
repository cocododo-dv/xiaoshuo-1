// 回收站视图：随章回收的场景挂在那一章下面、恢复不了的场景禁用并说明原因、
// 永久删除 / 清空先确认、打开就刷新列表。
import React, { act } from "react";
import { createRoot } from "react-dom/client";
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";

globalThis.IS_REACT_ACT_ENVIRONMENT = true;

vi.mock("./lib/client.js", () => ({
  apiGet: vi.fn(() => Promise.resolve({ items: [] })),
  apiPost: vi.fn(() => Promise.resolve({})),
  apiPatch: vi.fn(() => Promise.resolve({})),
  apiDelete: vi.fn(() => Promise.resolve({})),
}));

const trash = vi.hoisted(() => ({ items: [], load: { status: "ready", message: "" } }));

vi.mock("./ws-catalog.jsx", () => ({
  WsTrashStore: {
    subscribe: () => () => {},
    list: () => trash.items,
    loadState: () => trash.load,
    restore: vi.fn(() => true),
    purge: vi.fn(),
    clear: vi.fn(() => Promise.resolve(true)),
    refresh: vi.fn(() => Promise.resolve()),
  },
  WsCatalog: { get: () => [], subscribe: () => () => {} },
  useCatalogChapters: () => [],
}));

const NOW = Date.now();
function sampleItems() {
  return [
    { id: "chapter:CH_A", kind: "章节", title: "第 1 章", removedAt: NOW - 3600e3, restorable: true, payload: { type: "chapter" } },
    { id: "scene:SC_1", kind: "场景", title: "开场", removedAt: NOW - 3600e3, restorable: false, chapterId: "CH_A", payload: { type: "scene" } },
    { id: "scene:SC_2", kind: "场景", title: "夜渡", removedAt: NOW - 3600e3, restorable: false, chapterId: "CH_A", payload: { type: "scene" } },
    { id: "scene:SC_3", kind: "场景", title: "另一场", removedAt: NOW - 7200e3, restorable: true, chapterId: "CH_LIVE", payload: { type: "scene" } },
    { id: "scene:SC_4", kind: "场景", title: "孤场", removedAt: NOW - 7200e3, restorable: false, payload: { type: "scene" } },
    { id: "work:W_1", kind: "作品", title: "《旧稿》· 整部", removedAt: NOW - 86400e3, restorable: true, payload: { type: "work" } },
  ];
}

async function mountTrash() {
  const { WsTrash } = await import("./ws-trash.jsx");
  const catalog = await import("./ws-catalog.jsx");
  const host = document.createElement("div");
  document.body.appendChild(host);
  const root = createRoot(host);
  await act(async () => root.render(<WsTrash go={vi.fn()} />));
  return {
    host, store: catalog.WsTrashStore,
    unmount: async () => { await act(async () => root.unmount()); host.remove(); },
  };
}

const rowByTitle = (host, title) => Array.from(host.querySelectorAll("tbody tr")).find(tr => tr.textContent.includes(title));
const btn = (el, text) => Array.from(el.querySelectorAll("button")).find(b => b.textContent.includes(text));
const click = async (el) => { await act(async () => { el.dispatchEvent(new MouseEvent("click", { bubbles: true })); }); };

describe("回收站视图", () => {
  beforeEach(() => {
    vi.resetModules();
    trash.items = sampleItems();
    trash.load = { status: "ready", message: "" };
    window.localStorage.clear();
  });

  it("读不到回收站：只给一处错误和重试，不说「回收站是空的」；在读时说在读；读到了才是真的空", async () => {
    trash.items = [];
    trash.load = { status: "error", message: "无法连接后端。" };
    const view = await mountTrash();
    try {
      const notice = view.host.querySelector('[data-testid="trash-load-error"]');
      expect(notice.getAttribute("role")).toBe("alert");
      expect(notice.textContent).toContain("无法连接后端。");
      expect(view.host.textContent).not.toContain("回收站是空的");
      const calls = view.store.refresh.mock.calls.length;
      await click(btn(notice, "重试"));
      expect(view.store.refresh.mock.calls.length).toBe(calls + 1);
    } finally {
      await view.unmount();
    }

    trash.load = { status: "loading", message: "" };
    const loading = await mountTrash();
    try {
      expect(loading.host.textContent).toContain("正在读取回收站");
      expect(loading.host.textContent).not.toContain("回收站是空的");
    } finally {
      await loading.unmount();
    }

    trash.load = { status: "ready", message: "" };
    const empty = await mountTrash();
    try {
      expect(empty.host.textContent).toContain("回收站是空的");
      expect(empty.host.querySelector('[data-testid="trash-load-error"]')).toBeNull();
    } finally {
      await empty.unmount();
    }
  });
  afterEach(() => vi.restoreAllMocks());

  it("打开就向 store 要一次最新列表", async () => {
    const view = await mountTrash();
    try {
      expect(view.store.refresh).toHaveBeenCalled();
    } finally {
      await view.unmount();
    }
  });

  it("store 还没有 refresh() 时只重新拉回收站，不广播 ws:trash-changed（审阅队列也在听它）", async () => {
    const catalog = await import("./ws-catalog.jsx");
    const store = catalog.WsTrashStore;
    const saved = store.refresh;
    delete store.refresh;
    store.push = vi.fn();
    const heard = vi.fn();
    window.addEventListener("ws:trash-changed", heard);
    const view = await mountTrash();
    try {
      expect(store.push).toHaveBeenCalledTimes(1);
      expect(heard).not.toHaveBeenCalled();
    } finally {
      window.removeEventListener("ws:trash-changed", heard);
      store.refresh = saved;
      delete store.push;
      await view.unmount();
    }
  });

  it("随章回收的场景挂在那一章下面；章节行说「含 N 场」且不含「场景」二字", async () => {
    const view = await mountTrash();
    try {
      const rows = Array.from(view.host.querySelectorAll("tbody tr"));
      const chapterIdx = rows.findIndex(tr => tr.textContent.includes("第 1 章"));
      expect(chapterIdx).toBeGreaterThanOrEqual(0);
      const chapterRow = rows[chapterIdx];
      expect(chapterRow.textContent).toContain("含 2 场");
      expect(chapterRow.textContent).not.toContain("场景");
      expect(rows[chapterIdx + 1].textContent).toContain("开场");
      expect(rows[chapterIdx + 2].textContent).toContain("夜渡");
      expect(rows[chapterIdx + 1].classList.contains("is-nested")).toBe(true);
      // 所在章没进回收站的场景不挂靠
      expect(rowByTitle(view.host, "另一场").classList.contains("is-nested")).toBe(false);
    } finally {
      await view.unmount();
    }
  });

  it("恢复不了的场景：恢复按钮禁用并写明原因；能恢复的照常可点", async () => {
    const view = await mountTrash();
    try {
      const nested = btn(rowByTitle(view.host, "开场"), "恢复");
      expect(nested.disabled).toBe(true);
      expect(nested.getAttribute("title")).toContain("恢复这一章");
      const orphan = rowByTitle(view.host, "孤场");
      expect(btn(orphan, "恢复").disabled).toBe(true);
      expect(orphan.textContent).toContain("先恢复那一章");
      const ok = btn(rowByTitle(view.host, "另一场"), "恢复");
      expect(ok.disabled).toBe(false);
      await click(ok);
      expect(view.store.restore).toHaveBeenCalledWith("scene:SC_3");
    } finally {
      await view.unmount();
    }
  });

  it("恢复 / 永久删除的可及名称带上是哪一项（Tab 走下去不是一串同名按钮），看得见的字不变", async () => {
    const view = await mountTrash();
    try {
      const row = rowByTitle(view.host, "另一场");
      const restore = btn(row, "恢复");
      const purge = btn(row, "永久删除");
      expect(restore.getAttribute("aria-label")).toBe("恢复「另一场」");
      expect(purge.getAttribute("aria-label")).toBe("永久删除「另一场」");
      expect(restore.textContent.trim()).toBe("恢复");
      expect(purge.textContent.trim()).toBe("永久删除");
      const names = Array.from(view.host.querySelectorAll("tbody button")).map((b) => b.getAttribute("aria-label"));
      expect(new Set(names).size).toBe(names.length);
    } finally {
      await view.unmount();
    }
  });

  it("永久删除是危险按钮，先确认；取消就不删", async () => {
    const confirm = vi.spyOn(window, "confirm").mockReturnValueOnce(false).mockReturnValueOnce(true);
    const view = await mountTrash();
    try {
      const purge = btn(rowByTitle(view.host, "《旧稿》· 整部"), "永久删除");
      expect(purge.classList.contains("btn-danger")).toBe(true);
      await click(purge);
      expect(view.store.purge).not.toHaveBeenCalled();
      await click(purge);
      await vi.waitFor(() => expect(view.store.purge).toHaveBeenCalledWith("work:W_1"));
      expect(confirm).toHaveBeenCalledTimes(2);
    } finally {
      await view.unmount();
    }
  });

  it("清空回收站先确认，确认后才清", async () => {
    vi.spyOn(window, "confirm").mockReturnValue(true);
    const view = await mountTrash();
    try {
      const clear = btn(view.host, "清空回收站");
      expect(clear.classList.contains("btn-danger")).toBe(true);
      await click(clear);
      await vi.waitFor(() => expect(view.store.clear).toHaveBeenCalled());
    } finally {
      await view.unmount();
    }
  });

  it("空回收站给出空态说明", async () => {
    trash.items = [];
    const view = await mountTrash();
    try {
      expect(view.host.textContent).toContain("回收站是空的");
      expect(view.host.querySelector("table")).toBeNull();
    } finally {
      await view.unmount();
    }
  });
});
