// WsQuality store 层单测：巡检 overview 的 URL/参数 + summary/items 映射；
// 临时文本扫描 analyze 的端点/载荷；失败路径 error/alert（可证伪）。
// 视图不依赖 active project（端点不收 project_id），故无需 installApiRouter/settleActive。
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";

vi.mock("./lib/client.js", () => ({
  apiGet: vi.fn(),
  apiPost: vi.fn(),
  apiPatch: vi.fn(),
  apiDelete: vi.fn(),
}));

/* 视图从目录取章名 / 场景位置（后端 id → 第 N 章 · 第 M 场）；目录用夹具，不拉后端 */
const fx = vi.hoisted(() => ({ catalog: [] }));
vi.mock("./ws-catalog.jsx", () => ({
  WsCatalog: { get: () => fx.catalog, subscribe: () => () => {} },
  useCatalogChapters: () => fx.catalog,
}));

/* 巡检按当前作品过滤（WsWorks.activeId）；默认没有作品 → 退回全局 */
const works = vi.hoisted(() => ({ id: null }));
vi.mock("./ws-works.jsx", () => ({ WsWorks: { activeId: () => works.id } }));

const T = { timeout: 5000, interval: 25 };

function overviewPayload() {
  return {
    filters: {},
    summary: {
      object_count: 3, mean_score: 0.62, high_risk_count: 1,
      model_voice_count: 2, risk_cluster_count: 1, cross_scene_reuse_count: 0,
    },
    items: [
      {
        object_type: "scene", object_id: "s1", chapter_id: "c1", scene_id: "s1",
        text_layer: "author_draft_preferred", source_ref: "scene:s1", score: 0.55,
        signals: { model_voice: { risk: true, score: 0.3, evidence: "腔" }, valid_ambiguity: { risk: false, score: 0.9, evidence: "" } },
        findings: [{ dimension: "model_voice", severity: "revision", issue: "模型腔重", evidence_excerpt: "他笑了笑", recommendation: "去套话" }],
        fingerprint: {},
        recommended_next_action: { action: "open_deepdesk_patch", label: "去深改" },
      },
    ],
    risk_clusters: [], fingerprints: [], cross_scene_reuse: [],
    recommended_next_action: { action: "none", label: "暂无动作" },
  };
}

async function loadStore() {
  const client = await import("./lib/client.js");
  return { client, mod: await import("./ws-quality.jsx") };
}

describe("WsQuality store（overview 巡检）", () => {
  beforeEach(() => { vi.resetModules(); window.localStorage.clear(); vi.spyOn(window, "alert").mockImplementation(() => {}); });
  afterEach(() => vi.restoreAllMocks());

  it("qLoadOverview 以含 text_layer/min_severity 的正确 URL 调 apiGet，并映射 summary/items", async () => {
    const { client, mod } = await loadStore();
    // 按 URL 路由：overview 返回 payload；其余（boot 期 /api/v2/projects 等）给空对象
    client.apiGet.mockImplementation((u) => {
      if (String(u).includes("/literary-quality/overview")) return Promise.resolve(overviewPayload());
      return Promise.resolve({});
    });

    const data = await mod.qLoadOverview({ text_layer: "author_draft_preferred", min_severity: "revision", chapter_id: "", risk_type: "" });

    // 在所有 apiGet 调用里找 overview 那次（boot 期还有 /api/v2/projects 调用）
    const ovCall = client.apiGet.mock.calls.find((c) => String(c[0]).includes("/api/v1/literary-quality/overview"));
    expect(ovCall).toBeTruthy();
    const url = ovCall[0];
    expect(url).toContain("/api/v1/literary-quality/overview?");
    expect(url).toContain("text_layer=author_draft_preferred");
    expect(url).toContain("min_severity=revision");
    // 空串参数被丢弃（可证伪：若不过滤空值，会出现 chapter_id=）
    expect(url).not.toContain("chapter_id=");
    expect(data.summary.object_count).toBe(3);
    expect(mod.qSnapshot().overview.items[0].object_id).toBe("s1");
    expect(mod.qSnapshot().error).toBeNull();
  });

  it("qLoadOverview 失败时置 error 且返回 null（不抛）", async () => {
    const { client, mod } = await loadStore();
    client.apiGet.mockRejectedValueOnce(new Error("overview boom"));

    const data = await mod.qLoadOverview({ text_layer: "runtime_final_scene" });

    expect(data).toBeNull();
    expect(mod.qSnapshot().error).toContain("overview boom");
  });
});

describe("WsQuality store（临时文本扫描 analyze）", () => {
  beforeEach(() => { vi.resetModules(); window.localStorage.clear(); vi.spyOn(window, "alert").mockImplementation(() => {}); });
  afterEach(() => vi.restoreAllMocks());

  it("qAnalyzeText 以 {content} 打到 analyze-text 端点并存入 analyze", async () => {
    const { client, mod } = await loadStore();
    client.apiPost.mockResolvedValueOnce({ score: 0.4, span_findings: [{ dimension: "summary_ending", severity: "taste", start: 0, end: 3, evidence: "于是" }], signals: {} });

    const data = await mod.qAnalyzeText("  一段要体检的文字  ");

    expect(client.apiPost).toHaveBeenCalledWith(
      "/api/v1/literary-quality/analyze-text",
      { content: "一段要体检的文字" } // 去除首尾空白
    );
    expect(data.span_findings.length).toBe(1);
    expect(mod.qSnapshot().analyze.score).toBe(0.4);
  });

  it("空白文本不发请求", async () => {
    const { client, mod } = await loadStore();
    const data = await mod.qAnalyzeText("   ");
    expect(data).toBeNull();
    expect(client.apiPost).not.toHaveBeenCalled(); // 可证伪：若不守卫空串则会发请求
  });

  it("analyze 失败：只记下错误与出错的是哪一步，不弹浏览器对话框（可证伪）", async () => {
    const { client, mod } = await loadStore();
    client.apiPost.mockRejectedValueOnce(new Error("analyze boom"));

    await mod.qAnalyzeText("会失败的文字");

    expect(mod.qSnapshot().error).toContain("analyze boom");
    expect(mod.qSnapshot().errorScope).toBe("analyze");
    expect(window.alert).not.toHaveBeenCalled();
  });
});

describe("WsQuality 维度标签完整性", () => {
  beforeEach(() => { vi.resetModules(); });
  afterEach(() => vi.restoreAllMocks());

  it("21 维齐全且含蓝图 v2 新增三维中文标签", async () => {
    const { mod } = await loadStore();
    expect(mod.QUALITY_DIM_KEYS.length).toBe(21);
    expect(mod.QUALITY_DIMS.perception_filter).toBe("感知过滤");
    expect(mod.QUALITY_DIMS.self_repetition).toBe("自我重复");
    expect(mod.QUALITY_DIMS.conflict_too_clean).toBe("冲突过净");
    // 无 undefined 标签
    expect(mod.QUALITY_DIM_KEYS.every((k) => typeof mod.QUALITY_DIMS[k] === "string")).toBe(true);
  });
});

describe("WsQuality store（章组复审 chapter-set-review）", () => {
  beforeEach(() => { vi.resetModules(); window.localStorage.clear(); vi.spyOn(window, "alert").mockImplementation(() => {}); });
  afterEach(() => vi.restoreAllMocks());

  it("qChapterSetReview 以 {chapter_ids,protected_terms,text_layer} 打到 chapter-set-review 端点，并丢弃空值", async () => {
    const { client, mod } = await loadStore();
    client.apiPost.mockResolvedValueOnce({
      chapter_ids: ["c1"],
      summary: { chapter_count: 1, scene_count: 2, mean_score: 0.6, high_risk_count: 1, repeated_pattern_count: 0, reference_safety_finding_count: 0 },
      scores: { literary_quality: 0.6, cross_chapter_arc: 0.5, reference_safety: 1 },
      chapters: [], scenes: [], risk_clusters: [], repeated_patterns: [], reference_safety_findings: [],
      recommended_next_action: { action: "none" },
    });

    const data = await mod.qChapterSetReview({ chapter_ids: ["c1", ""], protected_terms: ["盐钟", ""], text_layer: "chapter_assembled" });

    // 可证伪：若不过滤空值，body 会含空串 chapter_id / protected_term
    expect(client.apiPost).toHaveBeenCalledWith(
      "/api/v1/literary-quality/chapter-set-review",
      { chapter_ids: ["c1"], protected_terms: ["盐钟"], text_layer: "chapter_assembled" }
    );
    expect(data.summary.chapter_count).toBe(1);
    expect(mod.qSnapshot().review.scores.reference_safety).toBe(1);
  });

  it("无 chapter_ids 时不发请求（可证伪）", async () => {
    const { client, mod } = await loadStore();
    const data = await mod.qChapterSetReview({ chapter_ids: [] });
    expect(data).toBeNull();
    expect(client.apiPost).not.toHaveBeenCalled();
  });

  it("章组复审失败：只记下错误与出错的是哪一步，不弹浏览器对话框（可证伪）", async () => {
    const { client, mod } = await loadStore();
    client.apiPost.mockRejectedValueOnce(new Error("review boom"));
    await mod.qChapterSetReview({ chapter_ids: ["c1"] });
    expect(mod.qSnapshot().error).toContain("review boom");
    expect(mod.qSnapshot().errorScope).toBe("review");
    expect(window.alert).not.toHaveBeenCalled();
  });
});

/* ---------- 视图：错误就地显示、对象用人话名字、证据不带标签 ---------- */
import React, { act } from "react";
import { createRoot } from "react-dom/client";

describe("WsQuality 视图", () => {
  let root;
  let host;
  beforeEach(() => {
    vi.resetModules();
    window.localStorage.clear();
    vi.spyOn(window, "alert").mockImplementation(() => {});
    globalThis.IS_REACT_ACT_ENVIRONMENT = true;
    fx.catalog = [{
      id: "ch01", backendId: "c1", n: "01", title: "盐场的早班", state: "writing",
      scenes: [{ sid: "sid-1", backendId: "s1", title: "交班", state: "done" }],
    }];
  });
  afterEach(async () => {
    if (root) await act(async () => root.unmount());
    if (host) host.remove();
    root = null;
    host = null;
    vi.restoreAllMocks();
  });

  async function mount(go = vi.fn()) {
    const { client, mod } = await loadStore();
    client.apiGet.mockImplementation((u) => (String(u).includes("/literary-quality/overview")
      ? Promise.resolve({
        ...overviewPayload(),
        items: [{ ...overviewPayload().items[0], findings: [{ dimension: "image_homogeneity", severity: "taste", issue: "The same image field repeats too often: 手.", evidence_excerpt: "</p><p>他伸出手", recommendation: "Keep one anchor image." }] }],
      })
      : Promise.resolve({})));
    host = document.createElement("div");
    document.body.appendChild(host);
    root = createRoot(host);
    await act(async () => root.render(<mod.WsQuality go={go} />));
    await act(async () => { await Promise.resolve(); await Promise.resolve(); });
    return { client, mod, go };
  }

  it("巡检对象显示为「第 N 章 · 第 M 场」，分数读作「N 分」，不再露出后端 id", async () => {
    await mount();
    const item = host.querySelector(".q-item");
    expect(item.textContent).toContain("第 1 章 · 第 1 场「交班」");
    expect(item.textContent).toContain("55 分");
    expect(item.textContent).not.toContain("第 55 分");
    expect(item.textContent).not.toContain("scene:s1");
    // 章筛选是目录里的章，不是手敲 chapter_id 的输入框
    expect(host.querySelector('input[placeholder="chapter_id"]')).toBeNull();
    expect([...host.querySelectorAll("option")].some((o) => o.value === "c1" && o.textContent === "第 1 章 · 盐场的早班")).toBe(true);
  });

  it("展开后：问题是中文、证据去掉 HTML 标签；「去写作台处理这一场」带着场景意图", async () => {
    const { go } = await mount();
    const row = host.querySelector(".q-item-row");
    expect(row.hasAttribute("aria-controls")).toBe(false);          // 收起时详情不在 DOM 里：不指向悬空的 id
    await act(async () => { row.click(); });
    const detail = host.querySelector(".q-item-detail");
    expect(document.getElementById(row.getAttribute("aria-controls"))).toBe(detail);
    expect(detail.textContent).toContain("同一个意象反复出现：手。");
    expect(detail.textContent).toContain("他伸出手");
    expect(detail.textContent).not.toContain("</p>");
    const button = [...detail.querySelectorAll("button")].find((b) => b.textContent.includes("去写作台处理这一场"));
    await act(async () => { button.click(); });
    expect(go).toHaveBeenCalledWith("writer", [
      { type: "ws:writer-scene", detail: "sid-1" },
      { type: "ws:writer-posture", detail: "deep" },
    ]);
  });

  it("临时扫描失败：错误显示在扫描区里（role=alert），不弹 window.alert", async () => {
    const { client } = await mount();
    client.apiPost.mockRejectedValueOnce(new Error("analyze boom"));
    const area = host.querySelector("textarea");
    const setter = Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, "value").set;
    await act(async () => {
      setter.call(area, "要扫描的一段文字");
      area.dispatchEvent(new Event("input", { bubbles: true }));
    });
    const run = [...host.querySelectorAll("button")].find((b) => b.textContent.includes("扫描这段文字"));
    await act(async () => { run.click(); });
    await act(async () => { await Promise.resolve(); });
    const alertBox = host.querySelector(".q-scan [role='alert']");
    expect(alertBox).not.toBeNull();
    expect(alertBox.textContent).toContain("analyze boom");
    expect(window.alert).not.toHaveBeenCalled();
  });

  it("巡检读不到时只有一处报错带「重试」：不再同时摆一排 0 和「没有可巡检的稿件」（那是在说稿件是空的）", async () => {
    const { client, mod } = await loadStore();
    client.apiGet.mockImplementation((u) => (String(u).includes("/literary-quality/overview")
      ? Promise.reject(Object.assign(new Error("读不到服务端"), { code: "NETWORK_ERROR" }))
      : Promise.resolve({})));
    host = document.createElement("div");
    document.body.appendChild(host);
    root = createRoot(host);
    await act(async () => root.render(<mod.WsQuality go={vi.fn()} />));
    await act(async () => { await Promise.resolve(); await Promise.resolve(); });
    const alertBox = host.querySelector(".q-notice[role='alert']");
    expect(alertBox.textContent).toContain("读不到服务端");
    expect(host.querySelector(".q-stat")).toBeNull();
    expect(host.querySelector(".q-empty")).toBeNull();
    expect(host.textContent).not.toContain("没有可巡检的稿件");
    // 重试就是再巡检一轮；这一轮读到了，页面回到正常样子
    client.apiGet.mockImplementation((u) => (String(u).includes("/literary-quality/overview") ? Promise.resolve(overviewPayload()) : Promise.resolve({})));
    const retry = [...alertBox.querySelectorAll("button")].find((b) => b.textContent.includes("重试"));
    await act(async () => { retry.click(); });
    await act(async () => { await Promise.resolve(); await Promise.resolve(); });
    expect(host.querySelector(".q-notice[role='alert']")).toBeNull();
    expect(host.querySelectorAll(".q-stat").length).toBe(6);
  });

  it("进入时的巡检按当前作品过滤；作品列表还没到时不带 project_id", async () => {
    works.id = "P1";
    const { client } = await mount();
    expect(client.apiGet).toHaveBeenCalledWith(expect.stringContaining("project_id=P1"));
    works.id = "__loading__";
    await act(async () => root.unmount());
    host.remove();
    const second = await mount();
    const overviewCalls = second.client.apiGet.mock.calls.map(([u]) => String(u)).filter((u) => u.includes("/literary-quality/overview"));
    expect(overviewCalls.at(-1)).not.toContain("project_id");
    works.id = null;
  });

  it("章组复审的章来自目录，不必先巡检", async () => {
    await mount();
    const tab = [...host.querySelectorAll('[role="radio"]')].find((b) => b.textContent === "章组复审");
    await act(async () => { tab.click(); });
    expect(host.textContent).toContain("第 1 章 · 盐场的早班");
    expect(host.querySelectorAll('.q-set-chapters input[type="checkbox"]').length).toBe(1);
  });
});
