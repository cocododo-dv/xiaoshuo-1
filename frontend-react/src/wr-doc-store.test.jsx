// WrDocs / WrDocVersions store 层单测：save(ensure+PATCH 带 base_revision_no) +
// words_rollup 回流 + 409 冲突重水合 + 非409只留底 + 跨作品 sid 前缀防污染 + 修订映射 + 句级 diff。
//
// 依赖链：WrDocs 经 window.WsCatalog.__backendSceneId(slug)→scene_id、
//        缓存键经 window.wsKey 加 ::<activeId> 后缀、docMeta 经 metaKeyOf 加作品前缀。
// 故先 import ws-catalog（装 window.WsCatalog + 间接装 ws-works），settle 后再 import wr-doc-store。
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { installApiRouter, DEFAULT_PROJECT } from "./test-helpers.js";

vi.mock("./lib/client.js", () => ({
  apiGet: vi.fn(),
  apiPost: vi.fn(),
  apiPatch: vi.fn(),
  apiDelete: vi.fn(),
}));

const T = { timeout: 5000, interval: 25 };

function deferred() {
  let resolve;
  let reject;
  const promise = new Promise((ok, fail) => { resolve = ok; reject = fail; });
  return { promise, resolve, reject };
}

const SALT_PROJECT = { project_id: "prj-second", title: "盐镇旧志", genre: "悬疑", is_demo: true, stats: { words_total: 0 } };

// ensure → draft d1/rev1；save PATCH(d1) → rev2 + words_rollup。
function wireDrafts(client) {
  client.apiPost.mockImplementation((url) => {
    if (/\/author-drafts\/scene\/.+\/ensure$/.test(url)) {
      return Promise.resolve({ draft: { draft_id: "d1", revision_no: 1, content: "" } });
    }
    return Promise.resolve({});
  });
  client.apiPatch.mockImplementation((url) => {
    if (/\/author-drafts\/d1$/.test(url)) {
      return Promise.resolve({ draft: { revision_no: 2 }, words_rollup: { chapter_words: 120, scene_words: 120 } });
    }
    return Promise.resolve({});
  });
}

async function settleActive(id = "prj-main") {
  await vi.waitFor(() => expect(window.WsWorks && window.WsWorks.activeId()).toBe(id), T);
}

async function loadDocs(opts = {}) {
  const client = await import("./lib/client.js");
  installApiRouter(client, opts);   // /api/v2/projects + catalog(DEFAULT_CHAP: ch01s1→s1) + dashboard
  wireDrafts(client);
  await import("./ws-catalog.jsx"); // 装 window.WsCatalog（间接 import ws-works）
  await settleActive("prj-main");
  await vi.waitFor(() => expect(window.WsCatalog.get().length).toBeGreaterThan(0), T);
  const mod = await import("./wr-doc-store.jsx");
  return { mod, client };
}

describe("WrDocs.save（ensure + PATCH 带 base_revision_no）", () => {
  beforeEach(() => {
    vi.resetModules();
    window.localStorage.clear();
    vi.spyOn(window, "alert").mockImplementation(() => {});
  });
  afterEach(() => vi.restoreAllMocks());

  it("缓存即时落地 + 经 ws-catalog 把 ch01s1→s1 ensure 后 PATCH（带 base_revision_no）", async () => {
    const { mod, client } = await loadDocs();
    await mod.WrDocs.save("ch01s1", "<p>正文</p>");
    // 同步缓存：视图零等待即可读到
    expect(mod.WrDocs.load("ch01s1")).toBe("<p>正文</p>");
    await vi.waitFor(() => expect(client.apiPost).toHaveBeenCalledWith(
      "/api/v1/author-drafts/scene/s1/ensure", {}), T);
    await vi.waitFor(() => expect(client.apiPatch).toHaveBeenCalledWith(
      "/api/v1/author-drafts/d1",
      { content: "<p>正文</p>", base_revision_no: 1 }), T);
  });

  it("save 成功把 words_rollup 经 WsCatalog.__applyWordsRollup 回流", async () => {
    const { mod } = await loadDocs();
    const spy = vi.spyOn(window.WsCatalog, "__applyWordsRollup");
    await mod.WrDocs.save("ch01s1", "<p>x</p>");
    await vi.waitFor(() => expect(spy).toHaveBeenCalledWith(
      "ch01s1", { chapter_words: 120, scene_words: 120 }), T);
  });

  it("同场景连续保存串行执行，旧请求完成时仍保持 dirty，直到最新版本成功", async () => {
    const { mod, client } = await loadDocs();
    const firstPatch = deferred();
    client.apiPatch
      .mockImplementationOnce(() => firstPatch.promise)
      .mockResolvedValueOnce({ draft: { draft_id: "d1", revision_no: 3 } });

    const firstSave = mod.WrDocs.save("ch01s1", "<p>第一版</p>");
    await vi.waitFor(() => expect(client.apiPatch).toHaveBeenCalledTimes(1), T);
    const secondSave = mod.WrDocs.save("ch01s1", "<p>第二版</p>");
    expect(mod.WrDocs.state("ch01s1").dirty).toBe(true);

    firstPatch.resolve({ draft: { draft_id: "d1", revision_no: 2 } });
    await firstSave;
    expect(mod.WrDocs.state("ch01s1").dirty).toBe(true);
    await secondSave;

    expect(mod.WrDocs.state("ch01s1").dirty).toBe(false);
    expect(mod.WrDocs.load("ch01s1")).toBe("<p>第二版</p>");
    expect(client.apiPatch).toHaveBeenNthCalledWith(2, "/api/v1/author-drafts/d1", {
      content: "<p>第二版</p>",
      base_revision_no: 2,
    });
  });

  it("旧请求发生 409 时保留队列中的较新正文，刷新 revision 后继续保存", async () => {
    const { mod, client } = await loadDocs();
    let ensureCount = 0;
    client.apiPost.mockImplementation((url) => {
      if (/\/author-drafts\/scene\/.+\/ensure$/.test(url)) {
        ensureCount += 1;
        return Promise.resolve({
          draft: {
            draft_id: "d1",
            revision_no: ensureCount === 1 ? 1 : 5,
            content: ensureCount === 1 ? "" : "<p>服务端并发版本</p>",
          },
        });
      }
      return Promise.resolve({});
    });
    const firstPatch = deferred();
    client.apiPatch
      .mockImplementationOnce(() => firstPatch.promise)
      .mockResolvedValueOnce({ draft: { draft_id: "d1", revision_no: 6 } });
    const conflict = Object.assign(new Error("conflict"), { code: "AUTHOR_DRAFT_CONFLICT" });

    const firstSave = mod.WrDocs.save("ch01s1", "<p>已被后续编辑取代</p>");
    const firstRejected = expect(firstSave).rejects.toBe(conflict);
    await vi.waitFor(() => expect(client.apiPatch).toHaveBeenCalledTimes(1), T);
    const secondSave = mod.WrDocs.save("ch01s1", "<p>不能丢的最新正文</p>");
    firstPatch.reject(conflict);

    await firstRejected;
    await secondSave;
    expect(mod.WrDocs.load("ch01s1")).toBe("<p>不能丢的最新正文</p>");
    expect(mod.WrRecovery.list()).toEqual([]);
    expect(window.alert).not.toHaveBeenCalled();
    expect(client.apiPatch).toHaveBeenNthCalledWith(2, "/api/v1/author-drafts/d1", {
      content: "<p>不能丢的最新正文</p>",
      base_revision_no: 5,
    });
  });
});

describe("WrDocs 409 冲突重水合（历史 bug 回归）", () => {
  beforeEach(() => {
    vi.resetModules();
    window.localStorage.clear();
    vi.spyOn(window, "alert").mockImplementation(() => {});
  });
  afterEach(() => vi.restoreAllMocks());

  it("PATCH 抛 AUTHOR_DRAFT_CONFLICT → 重新 ensure 重水合 + alert 提示", async () => {
    const { mod, client } = await loadDocs();
    const conflict = Object.assign(new Error("conflict"), { code: "AUTHOR_DRAFT_CONFLICT" });
    client.apiPatch.mockRejectedValueOnce(conflict); // 仅首次保存冲突
    client.apiPost.mockClear();

    await expect(mod.WrDocs.save("ch01s1", "<p>本地改动</p>")).rejects.toBe(conflict);

    // 409 → alert（强可证伪：非 409 路径只 console.warn）
    await vi.waitFor(() => expect(window.alert).toHaveBeenCalled(), T);
    // 重水合：draftId 被清后 hydrate 再次 ensure（共 2 次 ensure）
    await vi.waitFor(() => {
      const ensures = client.apiPost.mock.calls.filter(c => /\/scene\/s1\/ensure$/.test(c[0]));
      expect(ensures.length).toBe(2);
    }, T);
  });

  it("非 409 失败只留底不 alert（缓存仍在，下次重试）", async () => {
    const { mod, client } = await loadDocs();
    const failure = new Error("network");
    client.apiPatch.mockRejectedValueOnce(failure); // 无 .code
    await expect(mod.WrDocs.save("ch01s1", "<p>x</p>")).rejects.toBe(failure);
    expect(window.alert).not.toHaveBeenCalled();          // 可证伪：若把非 409 也 alert 则红
    expect(mod.WrDocs.load("ch01s1")).toBe("<p>x</p>");   // 缓存留底
  });
});

describe("WrDocs 跨作品 sid 前缀防污染（历史 bug 回归）", () => {
  beforeEach(() => {
    vi.resetModules();
    window.localStorage.clear();
    vi.spyOn(window, "alert").mockImplementation(() => {});
  });
  afterEach(() => vi.restoreAllMocks());

  it("同名 sid 在不同作品下：缓存键(wsKey)与 docMeta(metaKeyOf)双隔离", async () => {
    const { mod, client } = await loadDocs({ projects: [DEFAULT_PROJECT, SALT_PROJECT] });
    // ensure 按当前激活作品返回不同 draft_id，以验证 docMeta 隔离（裸 sid 会复用上一部的 draftId）
    client.apiPost.mockImplementation((url) => {
      if (/\/author-drafts\/scene\/.+\/ensure$/.test(url)) {
        const w = window.WsWorks.activeId();
        return Promise.resolve({ draft: { draft_id: w === "prj-second" ? "d-second" : "d-main", revision_no: 1, content: "" } });
      }
      return Promise.resolve({});
    });

    // —— 作品 prj-main：保存 ch01s1 ——
    await mod.WrDocs.save("ch01s1", "<p>潮汐的正文</p>");
    expect(mod.WrDocs.load("ch01s1")).toBe("<p>潮汐的正文</p>");
    await vi.waitFor(() => expect(client.apiPatch).toHaveBeenCalledWith(
      "/api/v1/author-drafts/d-main",
      { content: "<p>潮汐的正文</p>", base_revision_no: 1 }), T);

    // —— 切到作品 prj-second（真实 setActive，使 WS_ACTIVE_ID 切换，wsKey/metaKeyOf 同步）——
    window.WsWorks.setActive("prj-second");
    await settleActive("prj-second");
    await vi.waitFor(() => expect(window.WsCatalog.get().length).toBeGreaterThan(0), T);

    // 缓存键隔离（wsKey）：prj-second 下读不到 prj-main 的正文
    expect(mod.WrDocs.load("ch01s1")).toBeNull();

    // —— 作品 prj-second：保存同名 ch01s1 ——
    await mod.WrDocs.save("ch01s1", "<p>盐镇的正文</p>");
    expect(mod.WrDocs.load("ch01s1")).toBe("<p>盐镇的正文</p>");
    // docMeta 隔离（metaKeyOf）：prj-second 走自己的 d-second，而非复用 prj-main 的 d-main
    // 可证伪：metaKeyOf 改裸 sid → 复用 d-main，此断言转红
    await vi.waitFor(() => expect(client.apiPatch).toHaveBeenCalledWith(
      "/api/v1/author-drafts/d-second",
      { content: "<p>盐镇的正文</p>", base_revision_no: 1 }), T);

    // —— 切回 prj-main：正文互不覆盖 ——
    window.WsWorks.setActive("prj-main");
    await settleActive("prj-main");
    expect(mod.WrDocs.load("ch01s1")).toBe("<p>潮汐的正文</p>");
  });
});

/* ==========================================================
   Wave 1（结果闭环治理 · 设计项 5）：跨会话「本地较新」冲突检测。
   旧缺口：pushSave 失败后 dirty 只在内存 docMeta——重启浏览器丢失，
   下次 hydrate 用服务端旧版静默覆盖 localStorage 里较新的本地稿。
   新契约：保存失败 → 持久化 pending 标记；重启后 hydrate 发现标记且
   本地 ≠ 服务端 → 本地稿备份为冲突副本 + alert 让作者选择；保存成功清标记。
   ========================================================== */
describe("WrDocs 跨会话 pending 冲突（Wave 1）", () => {
  beforeEach(() => {
    vi.resetModules();
    window.localStorage.clear();
    vi.spyOn(window, "alert").mockImplementation(() => {});
  });
  afterEach(() => vi.restoreAllMocks());

  it("保存失败留持久化 pending 标记；保存成功清除", async () => {
    const { mod, client } = await loadDocs();
    const failure = new Error("network");
    client.apiPatch.mockRejectedValueOnce(failure);
    await expect(mod.WrDocs.save("ch01s1", "<p>没保上的本地稿</p>")).rejects.toBe(failure);
    const pendingKeys = () => Object.keys(window.localStorage).filter(k => k.includes("wr-doc-pending:ch01s1"));
    expect(pendingKeys().length).toBe(1);

    // 下一次保存成功 → 标记清除
    await mod.WrDocs.save("ch01s1", "<p>这次保上了</p>");
    await vi.waitFor(() => expect(pendingKeys().length).toBe(0), T);
  });

  it("重启会话后：pending 标记 + 本地≠服务端 → 冲突副本 + alert，服务端版本上屏", async () => {
    // —— 会话 1：保存失败留底 ——
    const first = await loadDocs();
    const failure = new Error("network");
    first.client.apiPatch.mockRejectedValue(failure);
    await expect(first.mod.WrDocs.save("ch01s1", "<p>重启前较新的本地稿</p>")).rejects.toBe(failure);
    expect(Object.keys(window.localStorage).some(k => k.includes("wr-doc-pending:ch01s1"))).toBe(true);

    // —— 会话 2：重启（resetModules 清 docMeta 内存态），服务端有旧版内容 ——
    vi.resetModules();
    const second = await (async () => {
      const client = await import("./lib/client.js");
      installApiRouter(client);
      client.apiPost.mockImplementation((url) => {
        if (/\/author-drafts\/scene\/.+\/ensure$/.test(url)) {
          return Promise.resolve({ draft: { draft_id: "d1", revision_no: 5, content: "服务端的旧版正文" } });
        }
        return Promise.resolve({});
      });
      await import("./ws-catalog.jsx");
      await settleActive("prj-main");
      await vi.waitFor(() => expect(window.WsCatalog.get().length).toBeGreaterThan(0), T);
      const mod = await import("./wr-doc-store.jsx");
      return { mod, client };
    })();

    second.mod.WrDocs.load("ch01s1"); // 触发 hydrate
    // 本地较新稿进入结构化恢复中心，作者可比较 / 恢复 / 导出
    await vi.waitFor(() => {
      const items = second.mod.WrRecovery.list();
      expect(items).toHaveLength(1);
      expect(items[0]).toMatchObject({
        sid: "ch01s1",
        workId: "prj-main",
        type: "conflict",
        html: "<p>重启前较新的本地稿</p>",
        durable: true,
      });
    }, T);
    // alert 让作者知道有副本可选（可证伪：静默覆盖则红）
    await vi.waitFor(() => expect(window.alert).toHaveBeenCalled(), T);
    // 服务端版本上屏（服务端优先）
    await vi.waitFor(() => expect(second.mod.WrDocs.load("ch01s1")).toContain("服务端的旧版正文"), T);
    // pending 标记已消费
    expect(Object.keys(window.localStorage).some(k => k.includes("wr-doc-pending:ch01s1"))).toBe(false);
  });

  it("pending 标记 + 本地==服务端（上次实际保上了）→ 静默清标记，无副本无 alert", async () => {
    const first = await loadDocs();
    const failure = new Error("network");
    first.client.apiPatch.mockRejectedValue(failure);
    await expect(first.mod.WrDocs.save("ch01s1", "<p>同一份内容</p>")).rejects.toBe(failure);

    vi.resetModules();
    const client = await import("./lib/client.js");
    installApiRouter(client);
    client.apiPost.mockImplementation((url) => {
      if (/\/author-drafts\/scene\/.+\/ensure$/.test(url)) {
        return Promise.resolve({ draft: { draft_id: "d1", revision_no: 5, content: "<p>同一份内容</p>" } });
      }
      return Promise.resolve({});
    });
    await import("./ws-catalog.jsx");
    await settleActive("prj-main");
    const mod = (await import("./wr-doc-store.jsx"));

    mod.WrDocs.load("ch01s1");
    await vi.waitFor(() => {
      expect(Object.keys(window.localStorage).some(k => k.includes("wr-doc-pending:ch01s1"))).toBe(false);
    }, T);
    expect(mod.WrRecovery.list()).toEqual([]);
    expect(window.alert).not.toHaveBeenCalled();
  });
});

describe("WrRecovery（配额保护 + 恢复重试）", () => {
  beforeEach(() => {
    vi.resetModules();
    window.localStorage.clear();
    vi.spyOn(window, "alert").mockImplementation(() => {});
  });
  afterEach(() => vi.restoreAllMocks());

  it("409 时若恢复副本写入触发 quota，不覆盖本地稿并暴露仅会话记录", async () => {
    const { mod, client } = await loadDocs();
    const originalSetItem = Storage.prototype.setItem;
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(function setItemWithQuota(key, value) {
      if (String(key).startsWith("wr-recovery:v1:")) {
        throw new DOMException("quota full", "QuotaExceededError");
      }
      return originalSetItem.call(this, key, value);
    });
    const conflict = Object.assign(new Error("conflict"), { code: "AUTHOR_DRAFT_CONFLICT" });
    client.apiPatch.mockRejectedValueOnce(conflict);

    await expect(mod.WrDocs.save("ch01s1", "<p>不能丢的本地稿</p>")).rejects.toBe(conflict);

    expect(mod.WrDocs.load("ch01s1")).toBe("<p>不能丢的本地稿</p>");
    expect(mod.WrDocs.state("ch01s1")).toMatchObject({
      dirty: true,
      localDurable: false,
      cacheError: expect.objectContaining({ code: "LOCAL_STORAGE_QUOTA" }),
    });
    expect(mod.WrRecovery.list()).toEqual([
      expect.objectContaining({ sid: "ch01s1", type: "conflict", durable: false, html: "<p>不能丢的本地稿</p>" }),
    ]);
    // 没有持久副本就不进行第二次 ensure / 水合覆盖。
    expect(client.apiPost.mock.calls.filter(c => /\/scene\/s1\/ensure$/.test(c[0]))).toHaveLength(1);
  });

  it("正文缓存触发 quota 时以内存中的新稿为准，不让旧 localStorage 值回盖", async () => {
    const { mod, client } = await loadDocs();
    const docKey = window.wsKey("wr-doc:ch01s1");
    window.localStorage.setItem(docKey, "<p>旧缓存</p>");
    const originalSetItem = Storage.prototype.setItem;
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(function setItemWithQuota(key, value) {
      if (String(key) === docKey) throw new DOMException("quota full", "QuotaExceededError");
      return originalSetItem.call(this, key, value);
    });
    client.apiPatch.mockRejectedValueOnce(new Error("offline"));

    await expect(mod.WrDocs.save("ch01s1", "<p>刚写下、尚未落盘的新稿</p>")).rejects.toThrow("offline");

    expect(window.localStorage.getItem(docKey)).toBe("<p>旧缓存</p>");
    expect(mod.WrDocs.load("ch01s1")).toBe("<p>刚写下、尚未落盘的新稿</p>");
    expect(mod.WrDocs.state("ch01s1")).toMatchObject({
      dirty: true,
      localDurable: false,
      cacheError: expect.objectContaining({ code: "LOCAL_STORAGE_QUOTA" }),
    });
    expect(mod.WrRecovery.list()).toEqual([
      expect.objectContaining({
        sid: "ch01s1",
        type: "unsynced",
        html: "<p>刚写下、尚未落盘的新稿</p>",
      }),
    ]);
  });

  it("跨作品查看恢复记录时，与记录所属作品的同名场景比较", async () => {
    const { mod } = await loadDocs({ projects: [DEFAULT_PROJECT, SALT_PROJECT] });
    window.localStorage.setItem(window.wsKey("wr-doc:ch01s1"), "<p>潮汐作者稿</p>");
    const entry = mod.WrRecovery.createCandidate("ch01s1", "<p>潮汐 AI 候选</p>");

    window.WsWorks.setActive("prj-second");
    await settleActive("prj-second");
    window.localStorage.setItem(window.wsKey("wr-doc:ch01s1"), "<p>盐镇作者稿</p>");

    const diff = mod.WrRecovery.diff(entry.id);
    expect(diff.current).toBe("<p>潮汐作者稿</p>");
    expect(diff.current).not.toContain("盐镇作者稿");
  });

  it("恢复不同正文前自动备份当前作者稿，恢复后仍可撤销", async () => {
    const { mod } = await loadDocs();
    window.localStorage.setItem(window.wsKey("wr-doc:ch01s1"), "<p>恢复前的作者正文</p>");
    const entry = mod.WrRecovery.createCandidate("ch01s1", "<p>准备恢复的候选正文</p>");

    const result = await mod.WrRecovery.restore(entry.id);

    expect(result.replacedBackup).toMatchObject({
      sid: "ch01s1",
      type: "backup",
      source: "author",
      html: "<p>恢复前的作者正文</p>",
      durable: true,
    });
    expect(mod.WrDocs.load("ch01s1")).toBe("<p>准备恢复的候选正文</p>");
    expect(mod.WrRecovery.list()).toEqual(expect.arrayContaining([
      expect.objectContaining({ id: entry.id, type: "candidate" }),
      expect.objectContaining({ id: result.replacedBackup.id, type: "backup" }),
    ]));
  });

  it("断网留下的恢复稿可重试同步，成功后自动移出列表", async () => {
    const { mod, client } = await loadDocs();
    const entry = mod.WrRecovery.create({
      sid: "ch01s1",
      html: "<p>离线时写下的正文</p>",
      type: "unsynced",
      reason: "断网",
    });
    client.apiPatch.mockResolvedValue({ draft: { draft_id: "d1", revision_no: 2 } });

    await mod.WrRecovery.retry(entry.id);

    expect(client.apiPatch).toHaveBeenCalledWith("/api/v1/author-drafts/d1", {
      content: "<p>离线时写下的正文</p>",
      base_revision_no: 1,
    });
    expect(mod.WrDocs.load("ch01s1")).toBe("<p>离线时写下的正文</p>");
    expect(mod.WrRecovery.list()).toEqual([]);
  });
});

describe("WrDocs 提升权威正文", () => {
  beforeEach(() => {
    vi.resetModules();
    window.localStorage.clear();
    vi.spyOn(window, "alert").mockImplementation(() => {});
  });
  afterEach(() => vi.restoreAllMocks());

  it("只提升已保存修订，并携带草稿与当前权威正文双基线", async () => {
    const { mod, client } = await loadDocs();
    client.apiPost.mockImplementation((url) => {
      if (/\/author-drafts\/scene\/.+\/ensure$/.test(url)) {
        return Promise.resolve({
          draft: {
            draft_id: "d1",
            revision_no: 1,
            content: "",
            last_promoted_revision_no: 1,
            last_promoted_final_scene_row_id: "final-old",
            canonical_dirty: false,
          },
          runtime_final_ref: "final_scene:final-old",
        });
      }
      if (url === "/api/v1/author-drafts/d1/promote-canonical") {
        return Promise.resolve({
          draft_id: "d1",
          draft_revision_no: 2,
          final_scene_row_id: "final-new",
          canonical_dirty: false,
          narrative_sync_status: "synced",
        });
      }
      return Promise.resolve({});
    });
    client.apiPatch.mockResolvedValue({
      draft: {
        draft_id: "d1",
        revision_no: 2,
        canonical_dirty: true,
        last_promoted_revision_no: 1,
        last_promoted_final_scene_row_id: "final-old",
      },
    });

    await mod.WrDocs.save("ch01s1", "<p>作者定稿</p>");
    expect(mod.WrDocs.state("ch01s1").canonicalDirty).toBe(true);
    const result = await mod.WrDocs.promote("ch01s1");

    expect(client.apiPost).toHaveBeenCalledWith(
      "/api/v1/author-drafts/d1/promote-canonical",
      {
        base_revision_no: 2,
        expected_current_final_scene_row_id: "final-old",
        narrative_effect: "requires_reconcile",
        accepted_warning_codes: [],
      },
    );
    expect(result.narrative_sync_status).toBe("synced");
    expect(mod.WrDocs.state("ch01s1")).toMatchObject({
      revision: 2,
      canonicalDirty: false,
      currentFinalSceneRowId: "final-new",
      lastPromotedRevisionNo: 2,
      lastPromotedFinalSceneRowId: "final-new",
      lastSaveError: null,
    });
  });

  it("最近一次草稿保存失败时拒绝提升，且不会调用提升接口", async () => {
    const { mod, client } = await loadDocs();
    const failure = new Error("network");
    client.apiPatch.mockRejectedValueOnce(failure);
    await expect(mod.WrDocs.save("ch01s1", "<p>未保存稿</p>")).rejects.toBe(failure);
    client.apiPost.mockClear();

    await expect(mod.WrDocs.promote("ch01s1")).rejects.toBe(failure);
    expect(client.apiPost.mock.calls.some(([url]) => url.includes("promote-canonical"))).toBe(false);
  });

  it("内容风险复核重试只把调用方逐项确认的 exact finding codes 交给后端", async () => {
    const { mod, client } = await loadDocs();
    client.apiPost.mockImplementation((url) => {
      if (/\/author-drafts\/scene\/.+\/ensure$/.test(url)) {
        return Promise.resolve({
          draft: { draft_id: "d1", revision_no: 3, content: "<p>待复核正文</p>", canonical_dirty: true },
          runtime_final_ref: "final_scene:final-old",
        });
      }
      if (url === "/api/v1/author-drafts/d1/promote-canonical") {
        return Promise.resolve({
          draft_id: "d1",
          draft_revision_no: 3,
          final_scene_row_id: "final-reviewed",
          canonical_dirty: false,
        });
      }
      return Promise.resolve({});
    });

    await mod.WrDocs.promote("ch01s1", {
      acceptedWarningCodes: [
        "sexual_content_with_minor_indicators",
        "actionable_self_harm_detail",
      ],
    });

    expect(client.apiPost).toHaveBeenCalledWith(
      "/api/v1/author-drafts/d1/promote-canonical",
      expect.objectContaining({
        accepted_warning_codes: [
          "sexual_content_with_minor_indicators",
          "actionable_self_harm_detail",
        ],
      }),
    );
  });
});

describe("WrDocVersions（修订列表映射 + 句级 diff 纯函数）", () => {
  beforeEach(() => {
    vi.resetModules();
    window.localStorage.clear();
    vi.spyOn(window, "alert").mockImplementation(() => {});
  });
  afterEach(() => vi.restoreAllMocks());

  it("list 把 revision_no/created_at 映射为 revisionNo/at", async () => {
    const { mod, client } = await loadDocs();
    client.apiGet.mockImplementation((url) => {
      if (url === "/api/v2/projects") return Promise.resolve({ items: [DEFAULT_PROJECT] });
      if (/\/author-drafts\/d1\/revisions$/.test(url)) {
        return Promise.resolve({ items: [{ revision_no: 2, words: 10, origin: "edited", created_at: "2026-06-01" }] });
      }
      return Promise.resolve({});
    });
    const list = await mod.WrDocVersions.list("ch01s1");
    expect(list).toEqual([{ revisionNo: 2, words: 10, origin: "edited", at: "2026-06-01" }]);
  });

  it("同一场并发读版本 / 正文 / draftId 只发一次 ensure（开发模式 effect 连跑两遍）", async () => {
    const { mod, client } = await loadDocs();
    const ensure = deferred();
    client.apiPost.mockImplementation((url) => {
      if (/\/author-drafts\/scene\/.+\/ensure$/.test(url)) return ensure.promise;
      return Promise.resolve({});
    });
    client.apiGet.mockImplementation((url) => {
      if (url === "/api/v2/projects") return Promise.resolve({ items: [DEFAULT_PROJECT] });
      if (/\/author-drafts\/d1\/revisions$/.test(url)) return Promise.resolve({ items: [] });
      if (/\/author-drafts\/d1\/revisions\/\d+$/.test(url)) return Promise.resolve({ revision: { content: "<p>旧版一句。</p>" } });
      return Promise.resolve({});
    });
    const ensureCalls = () => client.apiPost.mock.calls.filter(c => /\/scene\/s1\/ensure$/.test(c[0]));

    const first = mod.WrDocVersions.list("ch01s1");
    const second = mod.WrDocVersions.list("ch01s1");
    const third = mod.WrDocVersions.paras("ch01s1", 1);
    const fourth = mod.WrDocs.draftId("ch01s1");
    await vi.waitFor(() => expect(ensureCalls()).toHaveLength(1), T);
    ensure.resolve({ draft: { draft_id: "d1", revision_no: 1, content: "" } });

    await expect(first).resolves.toEqual([]);
    await expect(second).resolves.toEqual([]);
    await expect(third).resolves.toEqual(["旧版一句。"]);
    await expect(fourth).resolves.toBe("d1");
    // 可证伪：去掉 in-flight 共享，四个调用各发一次 ensure
    expect(ensureCalls()).toHaveLength(1);
  });

  it("ensure 失败后并发调用方都拿到同一个错误，之后的调用重新请求", async () => {
    const { mod, client } = await loadDocs();
    const failure = Object.assign(new Error("offline"), { code: "NETWORK_ERROR" });
    const firstEnsure = deferred();
    client.apiPost.mockImplementation((url) => {
      if (/\/author-drafts\/scene\/.+\/ensure$/.test(url)) return firstEnsure.promise;
      return Promise.resolve({});
    });
    const ensureCalls = () => client.apiPost.mock.calls.filter(c => /\/scene\/s1\/ensure$/.test(c[0]));

    const a = mod.WrDocs.draftId("ch01s1");
    const b = mod.WrDocs.draftId("ch01s1");
    const aRejected = expect(a).rejects.toBe(failure);
    const bRejected = expect(b).rejects.toBe(failure);
    await vi.waitFor(() => expect(ensureCalls()).toHaveLength(1), T);
    firstEnsure.reject(failure);
    await aRejected;
    await bRejected;

    // 结束即清掉 in-flight：再次调用会重新 POST（可证伪：失败的 promise 若被缓存，这里仍然拒绝）
    client.apiPost.mockImplementation((url) => {
      if (/\/author-drafts\/scene\/.+\/ensure$/.test(url)) {
        return Promise.resolve({ draft: { draft_id: "d1", revision_no: 1, content: "" } });
      }
      return Promise.resolve({});
    });
    await expect(mod.WrDocs.draftId("ch01s1")).resolves.toBe("d1");
    expect(ensureCalls()).toHaveLength(2);
  });

  it("diff 句级：新增句被标 add（纯函数，无需 mock）", async () => {
    const { mod } = await loadDocs();
    const r = mod.WrDocVersions.diff(["他走了。"], ["他走了。", "她留下。"]);
    expect(r.adds).toBe(1);
    expect(r.dels).toBe(0);
    expect(r.paras.flatMap(p => p.segs).some(s => s.t === "add" && s.text === "她留下。")).toBe(true);
  });
});
