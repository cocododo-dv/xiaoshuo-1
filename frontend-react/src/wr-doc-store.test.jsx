// WrDocs / WrDocVersions store 层单测：save(ensure+PATCH 带 base_revision_no) +
// words_rollup 回流 + 409 冲突重水合 + 非409只留底 + 跨作品 sid 前缀防污染 + 修订映射 + 句级 diff。
//
// 依赖链：WrDocs 经 window.WsCatalog.backendSceneId(slug)→scene_id、
//        缓存键经 window.wsKey 加 ::<activeId> 后缀、docMeta 经 metaKeyOf 加作品前缀。
// 故先 import ws-catalog（装 window.WsCatalog + 间接装 ws-works），settle 后再 import wr-doc-store。
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { installApiRouter, DEFAULT_CHAP, DEFAULT_PROJECT } from "./test-helpers.js";

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

/* 目录装载后的预热水合（WsCatalog.onLoaded → WrDocs.hydrateActive）停掉：数 ensure 次数的用例只数被测的那几个调用发的 */
function stopWarmHydrate(mod) {
  vi.spyOn(mod.WrDocs, "hydrateActive").mockImplementation(() => {});
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
    // 每一次 ensure 带自己的幂等键（复核二 W1-R2A-1：回包丢了的那一次不会被服务端按同一个键重放给之后的读取）
    await vi.waitFor(() => expect(client.apiPost).toHaveBeenCalledWith(
      "/api/v1/author-drafts/scene/s1/ensure", {}, expect.objectContaining({ idempotencyKey: expect.any(String) })), T);
    await vi.waitFor(() => expect(client.apiPatch).toHaveBeenCalledWith(
      "/api/v1/author-drafts/d1",
      { content: "<p>正文</p>", base_revision_no: 1 }), T);
  });

  it("save 成功把 words_rollup 经 WsCatalog.applyWordsRollup 回流", async () => {
    const { mod } = await loadDocs();
    const spy = vi.spyOn(window.WsCatalog, "applyWordsRollup");
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

  /* 作者拍板 #20c 的更严格做法（W1）：过去这里是「排在后面的较新本机稿静默赢」——带着刷新后的修订号把另一台设备
     同时存下的正文盖掉，同步与恢复里什么都没有。现在服务端版本上读缓存，两份本机稿都进同步与恢复，排队的不再发。 */
  it("旧请求 409、队列里还有较新的正文：两份本机稿都进同步与恢复，排队的不再发，读缓存换成服务端版本", async () => {
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
    const resolved = [];
    mod.WrDocs.subscribe((kind, detail) => { if (kind === "conflict-resolved") resolved.push(detail.html); });

    const firstSave = mod.WrDocs.save("ch01s1", "<p>已被后续编辑取代</p>");
    const firstRejected = expect(firstSave).rejects.toBe(conflict);
    await vi.waitFor(() => expect(client.apiPatch).toHaveBeenCalledTimes(1), T);
    const secondSave = mod.WrDocs.save("ch01s1", "<p>不能丢的最新正文</p>");
    const secondRejected = expect(secondSave).rejects.toMatchObject({ code: "AUTHOR_DRAFT_CONFLICT" });
    firstPatch.reject(conflict);

    await firstRejected;
    await secondRejected;
    await vi.waitFor(() => expect(resolved).toEqual(["<p>服务端并发版本</p>"]), T);
    expect(mod.WrDocs.cachedHTML("ch01s1")).toBe("<p>服务端并发版本</p>");
    expect(mod.WrRecovery.list().map((entry) => entry.html)).toEqual(expect.arrayContaining([
      "<p>已被后续编辑取代</p>",
      "<p>不能丢的最新正文</p>",
    ]));
    expect(mod.WrDocs.state("ch01s1")).toMatchObject({ revision: 5, dirty: false, conflictPending: false, lastSaveError: null });
    await vi.waitFor(() => expect(window.alert).toHaveBeenCalledWith(expect.stringContaining("同步与恢复")), T);
    // 排队的那一稿没有带着服务端的修订号发出去
    expect(client.apiPatch).toHaveBeenCalledTimes(1);
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
    // 409 = 服务端在别处往前走了：冲突之后的 ensure 读到的是另一台设备存下的 rev 2（读回来的修订号比撞上的那一次还旧，
    // WrDocs 当没读到、马上再读——这里按真实服务端回包，不再回一份不可能出现的旧快照）
    let patched = false;
    client.apiPost.mockImplementation((url) => {
      if (/\/author-drafts\/scene\/.+\/ensure$/.test(url)) {
        return Promise.resolve(patched
          ? { draft: { draft_id: "d1", revision_no: 2, content: "<p>另一台设备的正文</p>" } }
          : { draft: { draft_id: "d1", revision_no: 1, content: "" } });
      }
      return Promise.resolve({});
    });
    client.apiPatch.mockImplementationOnce(() => { patched = true; return Promise.reject(conflict); }); // 仅首次保存冲突
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
    // ensure 按当前激活作品返回不同 draft_id，以验证 docMeta 隔离（裸 sid 会复用上一部的 draftId）。
    // 两份草稿各自记着存上的修订号和正文（像后端）：回到一场时的后台复核读到的是它存上的那一版，不是一份永远的空稿
    const drafts = { "d-main": { revision: 1, content: "" }, "d-second": { revision: 1, content: "" } };
    const snapshotOf = (id) => ({ draft: { draft_id: id, revision_no: drafts[id].revision, content: drafts[id].content } });
    client.apiPost.mockImplementation((url) => {
      if (/\/author-drafts\/scene\/.+\/ensure$/.test(url)) {
        return Promise.resolve(snapshotOf(window.WsWorks.activeId() === "prj-second" ? "d-second" : "d-main"));
      }
      return Promise.resolve({});
    });
    client.apiPatch.mockImplementation((url, body) => {
      const id = url.split("/").pop();
      if (!drafts[id]) return Promise.resolve({});
      drafts[id].revision += 1;
      drafts[id].content = body.content;
      return Promise.resolve(snapshotOf(id));
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

  /* W1：过去配额不足时 store 干脆不读服务端版本、本机稿留在编辑器里，每次保存都再撞 409。现在服务端版本照样上屏
     （只进会话内存），本机稿留在本次会话的同步与恢复并醒目提示；本机存储里那一份本机稿和未同步标记留着——
     刷新之后按跨会话的路径再走一次冲突副本，那时空间够了就持久地留下。 */
  it("409 时冲突副本写不进本机存储：只留本次会话的记录并提示；服务端版本只进会话内存，本机存储里的本机稿留到刷新以后", async () => {
    const { mod, client } = await loadDocs();
    let ensureCount = 0;
    client.apiPost.mockImplementation((url) => {
      if (/\/author-drafts\/scene\/.+\/ensure$/.test(url)) {
        ensureCount += 1;
        return Promise.resolve(ensureCount === 1
          ? { draft: { draft_id: "d1", revision_no: 1, content: "" } }
          : { draft: { draft_id: "d1", revision_no: 3, content: "<p>另一台设备的正文</p>" } });
      }
      return Promise.resolve({});
    });
    const originalSetItem = Storage.prototype.setItem;
    const quota = vi.spyOn(Storage.prototype, "setItem").mockImplementation(function setItemWithQuota(key, value) {
      if (String(key).startsWith("wr-recovery:v1:")) {
        throw new DOMException("quota full", "QuotaExceededError");
      }
      return originalSetItem.call(this, key, value);
    });
    const conflict = Object.assign(new Error("conflict"), { code: "AUTHOR_DRAFT_CONFLICT" });
    client.apiPatch.mockRejectedValueOnce(conflict);

    await expect(mod.WrDocs.save("ch01s1", "<p>不能丢的本地稿</p>")).rejects.toBe(conflict);
    await vi.waitFor(() => expect(mod.WrDocs.cachedHTML("ch01s1")).toBe("<p>另一台设备的正文</p>"), T);

    expect(mod.WrDocs.state("ch01s1")).toMatchObject({
      dirty: false,
      conflictPending: false,
      localDurable: false,
      cacheError: expect.objectContaining({ code: "LOCAL_STORAGE_QUOTA" }),
    });
    expect(mod.WrRecovery.list()).toEqual([
      expect.objectContaining({ sid: "ch01s1", type: "conflict", durable: false, html: "<p>不能丢的本地稿</p>" }),
    ]);
    expect(window.alert).toHaveBeenCalledWith(expect.stringContaining("本次会话"));
    expect(window.localStorage.getItem(window.wsKey("wr-doc:ch01s1"))).toBe("<p>不能丢的本地稿</p>");
    expect(window.localStorage.getItem(window.wsKey("wr-doc-pending:ch01s1"))).not.toBeNull();
    expect(client.apiPatch).toHaveBeenCalledTimes(1);

    // 作者腾出了空间、刷新了页面：跨会话的路径把本机稿持久地放进同步与恢复，服务端版本上屏
    quota.mockRestore();
    vi.resetModules();
    const client2 = await import("./lib/client.js");
    installApiRouter(client2);
    client2.apiPost.mockImplementation((url) => (/\/author-drafts\/scene\/.+\/ensure$/.test(url)
      ? Promise.resolve({ draft: { draft_id: "d1", revision_no: 3, content: "<p>另一台设备的正文</p>" } })
      : Promise.resolve({})));
    await import("./ws-catalog.jsx");
    await settleActive("prj-main");
    await vi.waitFor(() => expect(window.WsCatalog.get().length).toBeGreaterThan(0), T);
    const mod2 = await import("./wr-doc-store.jsx");
    mod2.WrDocs.load("ch01s1");
    await vi.waitFor(() => expect(mod2.WrDocs.cachedHTML("ch01s1")).toBe("<p>另一台设备的正文</p>"), T);
    expect(mod2.WrRecovery.list()).toEqual([
      expect.objectContaining({ sid: "ch01s1", type: "conflict", durable: true, html: "<p>不能丢的本地稿</p>" }),
    ]);
    expect(window.localStorage.getItem(window.wsKey("wr-doc-pending:ch01s1"))).toBeNull();
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

/* 复核 Q1c-R1：目录给乐观新建、回包丢了的场记别名（W1-R7B-1）时认错了场，写作台就把作者在那一场写下的头几句挪进别的场、
   存上服务端，同步与恢复里什么也没有。这里是正文 store 一侧看到的结果：认对了跟过去，认不准照实进同步与恢复。 */
describe("WrDocs × 目录：回包丢了的新建场里写下的字（复核 Q1c-R1 · W1-R7B-1）", () => {
  beforeEach(() => {
    vi.resetModules();
    window.localStorage.clear();
    vi.spyOn(window, "alert").mockImplementation(() => {});
  });
  afterEach(() => vi.restoreAllMocks());

  /* 世界：目录里一章（c1 / s1）；作者稿服务端按 scene_id 各一份（ensure 建 d-<scene_id>，PATCH 按修订号比对）；
     POST …/chapters/c1/scenes 由用例的 create(第几次) 回答。→ { WsCatalog, mod, drafts, chapter, creates() } */
  async function loadWorld(create) {
    const chapter = { ...DEFAULT_CHAP, scenes: [...DEFAULT_CHAP.scenes] };
    const client = await import("./lib/client.js");
    installApiRouter(client, { catalog: [chapter] });
    const drafts = {};
    let creates = 0;
    client.apiPost.mockImplementation((url) => {
      const ensure = /\/author-drafts\/scene\/([^/]+)\/ensure$/.exec(url);
      if (ensure) {
        const d = drafts[ensure[1]] || (drafts[ensure[1]] = { id: `d-${ensure[1]}`, revision: 1, content: "" });
        return Promise.resolve({ draft: { draft_id: d.id, revision_no: d.revision, content: d.content } });
      }
      if (/\/catalog\/chapters\/c1\/scenes$/.test(url)) {
        creates += 1;
        return create(creates, chapter);
      }
      return Promise.resolve({});
    });
    client.apiPatch.mockImplementation((url, body) => {
      const hit = /\/author-drafts\/(d-[^/?]+)$/.exec(url);
      if (!hit) return Promise.resolve({});
      const d = Object.values(drafts).find((x) => x.id === hit[1]);
      if (Number(body.base_revision_no) !== d.revision) {
        return Promise.reject(Object.assign(new Error("conflict"), { code: "AUTHOR_DRAFT_CONFLICT", status: 409, details: { current_revision_no: d.revision } }));
      }
      d.revision += 1;
      d.content = body.content;
      return Promise.resolve({ draft: { draft_id: d.id, revision_no: d.revision, content: d.content } });
    });
    const { WsCatalog } = await import("./ws-catalog.jsx");
    await settleActive("prj-main");
    await vi.waitFor(() => expect(WsCatalog.get().length).toBeGreaterThan(0), T);
    const mod = await import("./wr-doc-store.jsx");
    return { WsCatalog, mod, drafts, chapter, creates: () => creates };
  }
  const sceneRow = (id) => ({ ...DEFAULT_CHAP.scenes[0], slug: id, scene_id: id, title: "新场景" });
  const typedIn = (html) => String(html || "").includes("头几句");
  const A_TEXT = "<p>A 场里作者写下的头几句。</p>";
  /* 写作台随后窗口重新聚焦、网回来了：本机还没同步上的字都会在这时补发 */
  const comeBack = () => { window.dispatchEvent(new Event("focus")); window.dispatchEvent(new Event("online")); };

  it("A 没建成、B 建成并落在 A 乐观时的位置：A 里写下的头几句从不进 B 的作者稿，照实进同步与恢复", async () => {
    let rejectA = null;
    const { WsCatalog, mod, drafts, creates } = await loadWorld((n, chapter) => {
      if (n === 1) return new Promise((_ok, reject) => { rejectA = reject; });
      chapter.scenes = [...chapter.scenes, sceneRow("s-b")];
      return Promise.resolve({ scene: { scene_id: "s-b", slug: "s-b" } });
    });

    WsCatalog.addScene("ch01");                                                // A：建场请求在路上
    const tmpA = WsCatalog.get()[0].scenes[1].sid;
    await vi.waitFor(() => expect(creates()).toBe(1), T);
    mod.WrDocs.load(tmpA);
    void mod.WrDocs.save(tmpA, A_TEXT).catch(() => {});                        // 它在等 A 的后端 id
    WsCatalog.addScene("ch01");                                                // B：建成了，还没写字
    await vi.waitFor(() => expect(creates()).toBe(2), T);
    await new Promise((resolve) => setTimeout(resolve, 30));
    rejectA(Object.assign(new Error("database is locked"), { status: 500, code: "DATABASE_ERROR", retryable: true }));
    await vi.waitFor(() => expect(WsCatalog.get()[0].scenes.map((s) => s.sid)).toEqual(["ch01s1", "s-b"]), T);
    mod.WrDocs.load("s-b");                                                    // 写作台随后打开 B
    comeBack();
    await vi.waitFor(() => expect(mod.WrRecovery.list().some((entry) => typedIn(entry.html))).toBe(true), T);
    await new Promise((resolve) => setTimeout(resolve, 300));

    expect(typedIn(drafts["s-b"] && drafts["s-b"].content)).toBe(false);
    expect(typedIn(mod.WrDocs.cachedHTML("s-b"))).toBe(false);
    expect(mod.WrRecovery.list().filter((entry) => typedIn(entry.html))).toEqual([
      expect.objectContaining({ sid: tmpA, type: "unsynced" }),
    ]);
    expect(window.alert.mock.calls.some(([message]) => String(message).includes("新建这一场时写下"))).toBe(true);
  });

  it("A 其实建好了、只是回包丢了：头几句跟到建好的那一场、存上服务端，同步与恢复里没有它（W1-R7B-1）", async () => {
    let rejectA = null;
    const { WsCatalog, mod, drafts, creates } = await loadWorld((n, chapter) => new Promise((_ok, reject) => {
      chapter.scenes = [...chapter.scenes, sceneRow("s-a")];                    // 服务端建好了
      rejectA = reject;
    }));

    WsCatalog.addScene("ch01");
    const tmpA = WsCatalog.get()[0].scenes[1].sid;
    await vi.waitFor(() => expect(creates()).toBe(1), T);
    mod.WrDocs.load(tmpA);
    void mod.WrDocs.save(tmpA, A_TEXT).catch(() => {});
    await new Promise((resolve) => setTimeout(resolve, 30));
    rejectA(Object.assign(new Error("连接断开"), { status: 0, code: "NETWORK_ERROR", retryable: true }));   // 回包在路上丢了
    await vi.waitFor(() => expect(WsCatalog.get()[0].scenes.map((s) => s.sid)).toEqual(["ch01s1", "s-a"]), T);
    expect(WsCatalog.sceneById(tmpA)).toMatchObject({ scene: { sid: "s-a" } });
    comeBack();
    await vi.waitFor(() => expect(typedIn(drafts["s-a"] && drafts["s-a"].content)).toBe(true), T);
    expect(mod.WrRecovery.list().filter((entry) => typedIn(entry.html))).toEqual([]);
  });
});

describe("WrDocVersions（修订列表映射 + 句级 diff 纯函数）", () => {
  beforeEach(() => {
    vi.resetModules();
    window.localStorage.clear();
    vi.spyOn(window, "alert").mockImplementation(() => {});
  });
  afterEach(() => vi.restoreAllMocks());

  it("list 只读当前作者稿（GET current）、分页取版本：revision_no/created_at 映射为 revisionNo/at，带下一页的游标", async () => {
    const { mod, client } = await loadDocs();
    stopWarmHydrate(mod);
    client.apiPost.mockClear();
    const gets = [];
    client.apiGet.mockImplementation((url) => {
      gets.push(url);
      if (url === "/api/v2/projects") return Promise.resolve({ items: [DEFAULT_PROJECT] });
      if (url === "/api/v1/author-drafts/scene/s1/current") return Promise.resolve({ draft: { draft_id: "d1", revision_no: 3 } });
      if (/\/author-drafts\/d1\/revisions\?/.test(url)) {
        return Promise.resolve(url.includes("cursor=")
          ? { items: [{ revision_no: 1, words: 5, origin: "edited", created_at: "2026-05-31" }], pagination: { has_next: false, next_cursor: null } }
          : { items: [{ revision_no: 2, words: 10, origin: "edited", created_at: "2026-06-01" }], pagination: { has_next: true, next_cursor: "c-2" } });
      }
      return Promise.resolve({});
    });
    const first = await mod.WrDocVersions.list("ch01s1");
    expect(first).toEqual({ items: [{ revisionNo: 2, words: 10, origin: "edited", at: "2026-06-01" }], nextCursor: "c-2" });
    expect(gets).toContain("/api/v1/author-drafts/d1/revisions?limit=50");
    const older = await mod.WrDocVersions.list("ch01s1", { cursor: "c-2" });
    expect(older).toEqual({ items: [{ revisionNo: 1, words: 5, origin: "edited", at: "2026-05-31" }], nextCursor: null });
    expect(gets).toContain("/api/v1/author-drafts/d1/revisions?limit=50&cursor=c-2");
    // 只读：看版本历史不替这一场建作者稿
    expect(client.apiPost.mock.calls.filter(([url]) => /\/author-drafts\//.test(url))).toEqual([]);
  });

  it("没有作者稿的一场打开「对比」：版本是空的，一个 POST 都不发（重评 R15a）", async () => {
    const { mod, client } = await loadDocs();
    stopWarmHydrate(mod);
    client.apiPost.mockClear();
    client.apiGet.mockImplementation((url) => {
      if (url === "/api/v2/projects") return Promise.resolve({ items: [DEFAULT_PROJECT] });
      if (url === "/api/v1/author-drafts/scene/s1/current") return Promise.resolve({ draft: null });
      return Promise.resolve({});
    });
    await expect(mod.WrDocVersions.list("ch01s1")).resolves.toEqual({ items: [], nextCursor: null });
    await expect(mod.WrDocVersions.paras("ch01s1", 1)).resolves.toEqual([]);
    expect(client.apiPost).not.toHaveBeenCalled();
    expect(client.apiGet.mock.calls.some(([url]) => /\/revisions/.test(url))).toBe(false);
  });

  it("同一场并发读 draftId 只发一次 ensure（开发模式 effect 连跑两遍）；读版本 / 正文不再 ensure", async () => {
    const { mod, client } = await loadDocs();
    // 目录装载后的预热水合另有自己的一次读取（水合总是读服务端，W1 复核四 W1-R4B-6；前一个用例留下的旧实例在它退役之前
    // 也可能预热一次）：这里只数被测的版本 / 正文 / draftId 这几个调用发出的 ensure
    stopWarmHydrate(mod);
    const ensure = deferred();
    let ensures = 0;
    client.apiPost.mockImplementation((url) => {
      if (/\/author-drafts\/scene\/.+\/ensure$/.test(url)) {
        ensures += 1;
        return ensure.promise;
      }
      return Promise.resolve({});
    });
    client.apiGet.mockImplementation((url) => {
      if (url === "/api/v2/projects") return Promise.resolve({ items: [DEFAULT_PROJECT] });
      if (url === "/api/v1/author-drafts/scene/s1/current") return Promise.resolve({ draft: { draft_id: "d1", revision_no: 1 } });
      if (/\/author-drafts\/d1\/revisions\?/.test(url)) return Promise.resolve({ items: [] });
      if (/\/author-drafts\/d1\/revisions\/\d+$/.test(url)) return Promise.resolve({ revision: { content: "<p>旧版一句。</p>" } });
      return Promise.resolve({});
    });

    const first = mod.WrDocVersions.list("ch01s1");
    const second = mod.WrDocVersions.list("ch01s1");
    const third = mod.WrDocVersions.paras("ch01s1", 1);
    const fourth = mod.WrDocs.draftId("ch01s1");
    const fifth = mod.WrDocs.draftId("ch01s1");
    await vi.waitFor(() => expect(ensures).toBe(1), T);
    await expect(first).resolves.toEqual({ items: [], nextCursor: null });
    await expect(second).resolves.toEqual({ items: [], nextCursor: null });
    await expect(third).resolves.toEqual(["旧版一句。"]);
    ensure.resolve({ draft: { draft_id: "d1", revision_no: 1, content: "" } });
    await expect(fourth).resolves.toBe("d1");
    await expect(fifth).resolves.toBe("d1");
    // 可证伪：去掉 in-flight 共享，两个 draftId 各发一次 ensure
    expect(ensures).toBe(1);
  });

  it("ensure 失败后并发调用方都拿到同一个错误，之后的调用重新请求", async () => {
    const { mod, client } = await loadDocs();
    stopWarmHydrate(mod);
    const failure = Object.assign(new Error("offline"), { code: "NETWORK_ERROR" });
    const firstEnsure = deferred();
    let ensures = 0;
    client.apiPost.mockImplementation((url) => {
      if (/\/author-drafts\/scene\/.+\/ensure$/.test(url)) {
        ensures += 1;
        return firstEnsure.promise;
      }
      return Promise.resolve({});
    });

    const a = mod.WrDocs.draftId("ch01s1");
    const b = mod.WrDocs.draftId("ch01s1");
    const aRejected = expect(a).rejects.toBe(failure);
    const bRejected = expect(b).rejects.toBe(failure);
    await vi.waitFor(() => expect(ensures).toBe(1), T);
    firstEnsure.reject(failure);
    await aRejected;
    await bRejected;

    // 结束即清掉 in-flight：再次调用会重新 POST（可证伪：失败的 promise 若被缓存，这里仍然拒绝）
    client.apiPost.mockImplementation((url) => {
      if (/\/author-drafts\/scene\/.+\/ensure$/.test(url)) {
        ensures += 1;
        return Promise.resolve({ draft: { draft_id: "d1", revision_no: 1, content: "" } });
      }
      return Promise.resolve({});
    });
    await expect(mod.WrDocs.draftId("ch01s1")).resolves.toBe("d1");
    expect(ensures).toBe(2);
  });

  it("diff 句级：新增句被标 add（纯函数，无需 mock）", async () => {
    const { mod } = await loadDocs();
    const r = mod.WrDocVersions.diff(["他走了。"], ["他走了。", "她留下。"]);
    expect(r.adds).toBe(1);
    expect(r.dels).toBe(0);
    expect(r.paras.flatMap(p => p.segs).some(s => s.t === "add" && s.text === "她留下。")).toBe(true);
  });
});
