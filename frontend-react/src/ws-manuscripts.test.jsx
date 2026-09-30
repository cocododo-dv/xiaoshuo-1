// WsManuStore store 层单测（Wave 1 · 结果闭环治理 §5.2）：
// 成稿中心正文换源——唯一正文来源是后端章节聚合（chapter-manuscripts，
// FinalScene 为源），localStorage 的 wr-doc 缓存不再作为章节正文来源。
// 完成门可复算证明：「缓存清除不丢稿」= 清空 localStorage 后正文仍完整来自 API。
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { installApiRouter } from "./test-helpers.js";

vi.mock("./lib/client.js", () => ({
  apiGet: vi.fn(),
  apiPost: vi.fn(),
  apiPatch: vi.fn(),
  apiDelete: vi.fn(),
}));

const T = { timeout: 5000, interval: 25 };

const DETAIL_URL = /^\/api\/v1\/chapter-manuscripts\/([^/?]+)$/;

const ARCHIVED_DETAIL = {
  chapter: { chapter_id: "c1", title: "盐场的早班" },
  completion_status: "complete",
  assembled: {
    content: "潮水退去，她看清了闸门上的名字。",
    char_count: 16,
    scene_count: 1,
    generated_scene_count: 1,
    missing_scene_ids: [],
  },
  aggregate: null,
  canon_continuity: {
    complete: true,
    scene_count: 1,
    synced_scene_count: 1,
    pending_scene_ids: [],
    missing_final_scene_ids: [],
    pending_candidate_count: 0,
    scenes: [],
  },
  scenes: [
    {
      scene_id: "s1",
      scene_seq: 1,
      final_scene: {
        row_id: "final_s1_v1",
        content: "潮水退去，她看清了闸门上的名字。",
        char_count: 16,
        created_at: "2026-07-11",
      },
    },
  ],
};

const EMPTY_DETAIL = {
  chapter: { chapter_id: "c1", title: "盐场的早班" },
  completion_status: "empty",
  assembled: { content: "", char_count: 0, scene_count: 1, generated_scene_count: 0, missing_scene_ids: ["s1"] },
  aggregate: null,
  canon_continuity: {
    complete: false,
    scene_count: 1,
    synced_scene_count: 0,
    pending_scene_ids: ["s1"],
    missing_final_scene_ids: ["s1"],
    pending_candidate_count: 0,
    scenes: [],
  },
  scenes: [{ scene_id: "s1", scene_seq: 1, final_scene: null }],
};

function routeDetail(client, detailByChapter) {
  const base = client.apiGet.getMockImplementation();
  client.apiGet.mockImplementation((url) => {
    const hit = DETAIL_URL.exec(url);
    if (hit) {
      const detail = detailByChapter[hit[1]];
      return detail ? Promise.resolve(detail) : Promise.reject(new Error("not found"));
    }
    return base(url);
  });
}

async function loadStore(detailByChapter = { c1: ARCHIVED_DETAIL }) {
  const client = await import("./lib/client.js");
  installApiRouter(client);
  routeDetail(client, detailByChapter);
  // 键是后端 chapter_id，store 不依赖 WsWorks/目录——直接可用
  const mod = await import("./ws-manuscripts-store.jsx");
  return { mod, client };
}

describe("WsManuStore（成稿中心正文换源到后端聚合）", () => {
  beforeEach(() => {
    vi.resetModules();
  });
  afterEach(() => vi.restoreAllMocks());

  it("refresh 拉后端 detail；body 给出逐场归档正文（来源=FinalScene，非 wr-doc）", async () => {
    const { mod, client } = await loadStore();
    await mod.WsManuStore.refresh("c1");
    expect(client.apiGet).toHaveBeenCalledWith("/api/v1/chapter-manuscripts/c1");

    const body = mod.WsManuStore.body("c1");
    expect(body).not.toBeNull();
    expect(body.completion).toBe("complete");
    expect(body.scenes.length).toBe(1);
    expect(body.scenes[0].sceneId).toBe("s1");
    expect(body.scenes[0].paras).toEqual(["潮水退去，她看清了闸门上的名字。"]);
    expect(body.scenes[0].live).toBe(true);
    expect(body.canonContinuity.complete).toBe(true);
    expect(mod.WsManuStore.snapshot("c1").status).toBe("ready");
  });

  it("显式区分 idle / loading / ready，不用 null 猜测加载阶段", async () => {
    const client = await import("./lib/client.js");
    installApiRouter(client);
    let resolveDetail;
    client.apiGet.mockReturnValueOnce(new Promise(resolve => { resolveDetail = resolve; }));
    const mod = await import("./ws-manuscripts-store.jsx");

    expect(mod.WsManuStore.snapshot("c1")).toEqual({ status: "idle", body: null, error: null });
    const pending = mod.WsManuStore.refresh("c1");
    expect(mod.WsManuStore.snapshot("c1")).toEqual({ status: "loading", body: null, error: null });

    resolveDetail(ARCHIVED_DETAIL);
    await pending;
    expect(mod.WsManuStore.snapshot("c1").status).toBe("ready");
    expect(mod.WsManuStore.snapshot("c1").body.scenes[0].live).toBe(true);
  });

  it("完成门：清空 localStorage 后正文仍完整来自 API（缓存清除不丢稿）", async () => {
    const { mod } = await loadStore();
    await mod.WsManuStore.refresh("c1");
    // 模拟「清缓存」：清空 localStorage 后 store 的正文不受影响
    window.localStorage.clear();
    const body = mod.WsManuStore.body("c1");
    expect(body.scenes[0].paras).toEqual(["潮水退去，她看清了闸门上的名字。"]);
    // 强断言：store 从不把 wr-doc 缓存当正文来源
    expect(Object.keys(window.localStorage).filter((k) => k.includes("wr-doc"))).toEqual([]);
  });

  it("无归档正文的章 → 场景无稿（live=false、无段落），不再拿 wr-doc 假装成稿", async () => {
    const { mod } = await loadStore({ c1: EMPTY_DETAIL });
    // 就算 wr-doc 缓存里有内容，也不得作为成稿正文来源
    window.localStorage.setItem("ws:prj-main:wr-doc:ch01s1", "<p>缓存里的假成稿</p>");
    await mod.WsManuStore.refresh("c1");
    const body = mod.WsManuStore.body("c1");
    expect(body.completion).toBe("empty");
    expect(body.scenes[0].live).toBe(false);
    expect(body.scenes[0].paras).toEqual([]);
  });

  it("detail 拉取失败：进入 error 并保留服务端错误，body 不伪造正文", async () => {
    const { mod } = await loadStore({});
    await mod.WsManuStore.refresh("c1");
    expect(mod.WsManuStore.body("c1")).toBeNull();
    expect(mod.WsManuStore.snapshot("c1").status).toBe("error");
    expect(mod.WsManuStore.snapshot("c1").error.message).toContain("not found");
  });

  it("计划中章节一旦有归档场景，也必须进入成稿中心以提供首次聚合入口", async () => {
    const { mod } = await loadStore();
    expect(mod.manuscriptChapterEligible({
      state: "planned",
      words: { cur: 0 },
      scenes: [{ state: "done" }],
    })).toBe(true);
    expect(mod.manuscriptChapterEligible({
      state: "planned",
      words: { cur: 0 },
      scenes: [{ state: "planned" }],
    })).toBe(false);
    expect(mod.manuscriptDisplayState("planned")).toBe("plan");
  });

  it("「规划中」的章动了笔（有一场在写、场上记着字）也进左栏——与页头读作写作中同一条规则（复核 Q4-R2）", async () => {
    const { mod } = await loadStore();
    expect(mod.manuscriptChapterEligible({ state: "planned", words: { cur: 0 }, scenes: [{ state: "writing", words: 0 }] })).toBe(true);
    expect(mod.manuscriptChapterEligible({ state: "planned", words: { cur: 0 }, scenes: [{ state: "todo", words: 120 }] })).toBe(true);
    expect(mod.manuscriptChapterEligible({ state: "planned", words: { cur: 0 }, scenes: [{ state: "todo", words: 0 }] })).toBe(false);
    expect(mod.manuscriptChapterEligible(null)).toBe(false);
  });

  it("读取的新鲜度（审计 F04-08）：maxAgeMs 内、目录也没变过就用已读到的快照；目录一变就重读；不给 maxAgeMs 总是重读", async () => {
    const { mod, client } = await loadStore();
    await mod.WsManuStore.refresh("c1");
    const reads = () => client.apiGet.mock.calls.filter(([url]) => url === "/api/v1/chapter-manuscripts/c1").length;
    expect(reads()).toBe(1);
    await mod.WsManuStore.refresh("c1", { maxAgeMs: 30_000 });
    expect(reads()).toBe(1);
    expect(mod.WsManuStore.snapshot("c1").status).toBe("ready");
    // 别处归档 / 采纳 / 提升之后目录会变：快照照旧显示，但下一次读取不再拿它充数
    window.dispatchEvent(new CustomEvent("ws:catalog-changed"));
    expect(mod.WsManuStore.snapshot("c1").status).toBe("ready");
    await mod.WsManuStore.refresh("c1", { maxAgeMs: 30_000 });
    expect(reads()).toBe(2);
    await mod.WsManuStore.refresh("c1");
    expect(reads()).toBe(3);
    // 失败的读取不算新鲜
    routeDetail(client, {});
    await mod.WsManuStore.refresh("c1");
    await mod.WsManuStore.refresh("c1", { maxAgeMs: 30_000 });
    expect(reads()).toBe(5);
  });

  it("送审与退回等待目录服务端确认，不做本地假流转", async () => {
    const { mod, client } = await loadStore();

    await mod.WsManuStore.setReviewState("p1", "c1", "review");
    await mod.WsManuStore.setReviewState("p1", "c1", "draft");

    expect(client.apiPatch).toHaveBeenNthCalledWith(1, "/api/v2/projects/p1/catalog/chapters/c1", { state: "review" });
    expect(client.apiPatch).toHaveBeenNthCalledWith(2, "/api/v2/projects/p1/catalog/chapters/c1", { state: "draft" });
    await expect(mod.WsManuStore.setReviewState("p1", "c1", "approved")).rejects.toThrow("审阅状态无效");
  });

  it("批准终稿：「已通读」随「确认定稿」一次提交，绑定读到的那一份正文的哈希（批准 #10）", async () => {
    const { mod, client } = await loadStore({ c1: { ...ARCHIVED_DETAIL, body_hash: "hash-read" } });
    client.apiPost.mockResolvedValue({ project: { status: "chapter_ready" }, approved_chapter_id: "c1" });
    // 还没读到这一章的聚合：没有哈希可绑，不提交
    await expect(mod.WsManuStore.approveFinal("p1", "c1", { readNote: "x" })).rejects.toThrow("还没读到");
    expect(client.apiPost).not.toHaveBeenCalled();

    await mod.WsManuStore.refresh("c1");
    await mod.WsManuStore.approveFinal("p1", "c1", { readNote: "已核对人物与时间线", revisionNotes: "下一章承接盐钟线索" });

    expect(client.apiPost).toHaveBeenCalledTimes(1);
    expect(client.apiPost).toHaveBeenCalledWith("/api/v1/projects/p1/chapters/c1/approve-final", {
      revision_notes: "下一章承接盐钟线索",
      read_confirmation: { body_hash: "hash-read", note: "已核对人物与时间线" },
    });
    expect(client.apiPost.mock.calls.some(([url]) => String(url).endsWith("/read-confirm"))).toBe(false);
    expect(mod.WsManuStore).not.toHaveProperty("confirmRead");
    // 批准之后这一章的快照作废，下次读取重读
    expect(mod.WsManuStore.snapshot("c1").status).toBe("idle");
  });

  it("正史候选裁决后刷新同章权威状态", async () => {
    const { mod, client } = await loadStore();
    client.apiPost.mockResolvedValue({ candidate: { candidate_id: "fc1", status: "accepted" } });
    client.apiGet.mockClear();

    await mod.WsManuStore.decideCanonCandidate("p1", "c1", "fc1", {
      action: "accept",
      expected_final_scene_row_id: "final_s1_v1",
    });

    expect(client.apiPost).toHaveBeenCalledWith(
      "/api/v1/projects/p1/canon/candidates/fc1/decision",
      { action: "accept", expected_final_scene_row_id: "final_s1_v1" },
    );
    expect(client.apiGet).toHaveBeenCalledWith("/api/v1/chapter-manuscripts/c1");
  });

  it("重新打开终稿必须给出理由并调用项目级审计端点", async () => {
    const { mod, client } = await loadStore();
    await expect(mod.WsManuStore.reopenFinal("p1", "c1", "  ")).rejects.toThrow("请填写");

    await mod.WsManuStore.reopenFinal("p1", "c1", "需要修正第三场的时间线");

    expect(client.apiPost).toHaveBeenCalledWith(
      "/api/v1/projects/p1/chapters/c1/reopen-final",
      { reason: "需要修正第三场的时间线" },
    );
  });
});

describe("成稿中心的「对比」只载入写作台的版本模块", () => {
  beforeEach(() => {
    vi.resetModules();
    window.localStorage.clear();
  });
  afterEach(() => vi.restoreAllMocks());

  /* 写作台 store 的门面 wr-doc-store.jsx 登记「目录装载后跟随目录、预热在写那一场」，预热会替那一场发作者稿 ensure
     （没有作者稿就建一份）。成稿中心只读版本历史：载入「对比」不能把这份登记带进来（复核 I5-R1） */
  it("载入「对比」之后目录重读，不会因为它替在写那一场发作者稿 ensure", async () => {
    const client = await import("./lib/client.js");
    installApiRouter(client);
    const { WsCatalog } = await import("./ws-catalog.jsx");
    await vi.waitFor(() => expect(WsCatalog.get().length).toBeGreaterThan(0), T);
    await import("./ws-manuscripts-diff.jsx");

    await WsCatalog.refresh();
    await new Promise((resolve) => setTimeout(resolve, 200));

    const ensures = client.apiPost.mock.calls.filter(([url]) => /\/author-drafts\/scene\/[^/]+\/ensure$/.test(url));
    expect(ensures).toEqual([]);
  });
});
