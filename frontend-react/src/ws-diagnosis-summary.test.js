// 诊断计数 store：整本书只在挂载 / 换作品时读一次；之后每一次写入的响应带 diagnosis_rollup，store 合进表、本地汇总 totals；
// 忽略 / 恢复按面板清单先记一笔；目录成员变了只剪掉不在目录里的条目——不再节流拉取。读不到就当没有（角标，不是闸门）。
import React, { act } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("./lib/client.js", () => ({ apiGet: vi.fn(), apiPost: vi.fn(), apiPatch: vi.fn(), apiDelete: vi.fn() }));
vi.mock("./ws-works.jsx", () => ({ WsWorks: { activeId: () => "prj-main" } }));
const catalog = { chapters: [] };
vi.mock("./ws-catalog.jsx", () => ({ WsCatalog: { get: () => catalog.chapters } }));

globalThis.IS_REACT_ACT_ENVIRONMENT = true;

const S1 = { chapter_id: "c1", text_layer: "author_draft", open: 3, blocking: 1, revision: 2, taste: 0, info: 0, ignored: 1, stale: 0, ai_status: "current", review_status: "not_run" };
const S2 = { chapter_id: "c1", text_layer: "author_draft", open: 1, blocking: 0, revision: 1, taste: 0, info: 0, ignored: 0, stale: 0, ai_status: "not_run", review_status: "not_run" };
const S3 = { chapter_id: "c2", text_layer: "none", open: 0, blocking: 0, revision: 0, taste: 0, info: 0, ignored: 0, stale: 0, ai_status: "not_run", review_status: "not_run" };
const C1 = { open: 5, blocking: 2, chapter_level: 1, chapter_level_blocking: 1, scenes: 2, scenes_with_findings: 2, ai_status: "current" };
const C2 = { open: 0, blocking: 0, chapter_level: 0, chapter_level_blocking: 0, scenes: 1, scenes_with_findings: 0, ai_status: "not_run" };
const PAYLOAD = {
  project_id: "prj-main",
  totals: { open: 5, blocking: 2, revision: 3, taste: 0, info: 0, ignored: 1, stale: 0, scenes: 3, scenes_with_text: 2, scenes_with_findings: 2, ai_reviewed_scenes: 1, chapters_reviewed: 1 },
  chapters: { c1: C1, c2: C2 },
  scenes: { s1: S1, s2: S2, s3: S3 },
};

const mounted = [];
afterEach(async () => {
  while (mounted.length) {
    const { root, host } = mounted.pop();
    await act(async () => root.unmount());
    host.remove();
  }
  vi.restoreAllMocks();
});

async function load() {
  vi.resetModules();
  const client = await import("./lib/client.js");
  client.apiGet.mockResolvedValue(PAYLOAD);
  const mod = await import("./ws-diagnosis-summary.jsx");
  mod.WsDiagnosis.__reset();
  return { client, ...mod };
}

async function mountProbe(useDiagnosisSummary) {
  function Probe() {
    const diag = useDiagnosisSummary();
    const counts = diag.sceneCounts("s1");
    const totals = diag.totals();
    return React.createElement("div", { "data-testid": "probe" }, counts ? `${counts.open}/${totals.open}` : "—");
  }
  const host = document.createElement("div");
  document.body.appendChild(host);
  const root = createRoot(host);
  mounted.push({ root, host });
  await act(async () => root.render(React.createElement(Probe)));
  return host;
}

describe("WsDiagnosis · 每场 / 每章开着的发现数", () => {
  beforeEach(() => { vi.clearAllMocks(); catalog.chapters = []; });

  it("读 /diagnosis-summary，按后端 id 给计数；没读到之前是 null，读到后不在表里的场是零；totals 与服务端一致", async () => {
    const { client, WsDiagnosis } = await load();
    expect(WsDiagnosis.sceneCounts("s1")).toBeNull();
    expect(WsDiagnosis.loaded()).toBe(false);
    await WsDiagnosis.refresh();
    expect(client.apiGet).toHaveBeenCalledWith("/api/v1/projects/prj-main/diagnosis-summary");
    expect(WsDiagnosis.loaded()).toBe(true);
    expect(WsDiagnosis.sceneCounts("s1")).toMatchObject({ open: 3, blocking: 1, ignored: 1, ai_status: "current" });
    expect(WsDiagnosis.sceneCounts("s9")).toMatchObject({ open: 0, blocking: 0 });
    expect(WsDiagnosis.chapterCounts("c1")).toMatchObject({ open: 5, chapter_level: 1, scenes: 2 });
    expect(WsDiagnosis.totals()).toMatchObject({ open: 5, blocking: 2 });
    /* 本地汇总规则与服务端同一条：章级发现算开着的 */
    expect(WsDiagnosis.__summarize(PAYLOAD.scenes, PAYLOAD.chapters)).toEqual(PAYLOAD.totals);
  });

  it("读不到就当没有计数（failed 为真、计数仍是 null），不抛也不弹", async () => {
    const { client, WsDiagnosis } = await load();
    client.apiGet.mockRejectedValueOnce(new Error("boom"));
    await WsDiagnosis.refresh();
    expect(WsDiagnosis.failed()).toBe(true);
    expect(WsDiagnosis.sceneCounts("s1")).toBeNull();
    expect(WsDiagnosis.loaded()).toBe(false);
  });

  it("写入的响应带 rollup：合进表、章里旧的场条目换成新的、totals 本地重算——不发请求", async () => {
    const { client, WsDiagnosis } = await load();
    await WsDiagnosis.refresh();
    expect(client.apiGet).toHaveBeenCalledTimes(1);
    /* 第一章的 rollup 里只剩 s1（s2 进了回收站），s1 改好了两条 */
    const applied = WsDiagnosis.applyRollup({
      project_id: "prj-main", chapter_id: "c1",
      chapters: { c1: { ...C1, open: 2, blocking: 1, chapter_level: 1, chapter_level_blocking: 1, scenes: 1, scenes_with_findings: 1 } },
      scenes: { s1: { ...S1, open: 1, blocking: 0, revision: 1 } },
    });
    expect(applied).toBe(true);
    expect(WsDiagnosis.sceneCounts("s1")).toMatchObject({ open: 1, blocking: 0 });
    expect(WsDiagnosis.sceneCounts("s2")).toMatchObject({ open: 0 });
    expect(WsDiagnosis.chapterCounts("c1")).toMatchObject({ open: 2, scenes: 1 });
    expect(WsDiagnosis.chapterCounts("c2")).toMatchObject({ scenes: 1 });
    expect(WsDiagnosis.totals()).toMatchObject({ open: 2, blocking: 1, scenes: 2, scenes_with_findings: 1 });
    expect(client.apiGet).toHaveBeenCalledTimes(1);
    /* 别的作品的 rollup 不认 */
    expect(WsDiagnosis.applyRollup({ project_id: "prj-other", chapters: {}, scenes: { s1: { ...S1, open: 9 } } })).toBe(false);
    expect(WsDiagnosis.sceneCounts("s1").open).toBe(1);
  });

  it("忽略 / 恢复：按面板里的清单先记一笔，章与 totals 按差额重算", async () => {
    const { WsDiagnosis } = await load();
    await WsDiagnosis.refresh();
    const findings = [
      { signal_id: "a", severity: "blocking", ignored: true, stale: false },
      { signal_id: "b", severity: "revision", ignored: false, stale: true },
      { signal_id: "c", severity: "taste", ignored: false, stale: false },
    ];
    expect(WsDiagnosis.applySceneFindings("s1", findings)).toBe(true);
    expect(WsDiagnosis.sceneCounts("s1")).toMatchObject({ open: 2, blocking: 0, revision: 1, taste: 1, ignored: 1, stale: 1, chapter_id: "c1", ai_status: "current" });
    /* 章：s1 2 + s2 1 + 章级 1 = 4；阻断：章级那一条 */
    expect(WsDiagnosis.chapterCounts("c1")).toMatchObject({ open: 4, blocking: 1, scenes_with_findings: 2 });
    expect(WsDiagnosis.totals()).toMatchObject({ open: 4, blocking: 1, ignored: 1 });
  });

  it("hook：挂载读一次；ws:diagnosis-changed 带 rollup 就用它、带清单就记一笔、什么都没带才重拉；目录事件只剪不在目录里的场", async () => {
    const { client, useDiagnosisSummary, announceDiagnosisChanged, WsDiagnosis } = await load();
    const host = await mountProbe(useDiagnosisSummary);
    await vi.waitFor(() => expect(host.textContent).toBe("3/5"));
    expect(client.apiGet).toHaveBeenCalledTimes(1);

    await act(async () => { announceDiagnosisChanged({ sid: "ch01s1", rollup: { project_id: "prj-main", chapter_id: "c1", chapters: { c1: { ...C1, open: 3, blocking: 1 } }, scenes: { s1: { ...S1, open: 2 }, s2: S2 } } }); });
    await vi.waitFor(() => expect(host.textContent).toBe("2/4")); // s1 2 + s2 1 + s3 0 + 章级 1
    expect(client.apiGet).toHaveBeenCalledTimes(1);

    await act(async () => { announceDiagnosisChanged({ sid: "ch01s1", sceneId: "s1", findings: [{ signal_id: "x", severity: "revision", ignored: false }] }); });
    await vi.waitFor(() => expect(host.textContent).toBe("1/3"));
    expect(client.apiGet).toHaveBeenCalledTimes(1);

    /* 目录里没有 s2 了：剪掉它，totals 随之变；仍不发请求 */
    catalog.chapters = [{ id: "ch01", backendId: "c1", scenes: [{ sid: "ch01s1", backendId: "s1" }] }, { id: "ch02", backendId: "c2", scenes: [{ sid: "ch02s1", backendId: "s3" }] }];
    await act(async () => { window.dispatchEvent(new CustomEvent("ws:catalog-changed")); });
    expect(WsDiagnosis.sceneCounts("s2")).toMatchObject({ open: 0 });
    expect(WsDiagnosis.chapterCounts("c1")).toMatchObject({ scenes: 1, open: 2 });
    expect(host.textContent).toBe("1/2");
    expect(client.apiGet).toHaveBeenCalledTimes(1);

    /* 什么都没带：重拉 */
    client.apiGet.mockResolvedValueOnce({ ...PAYLOAD, totals: { ...PAYLOAD.totals, open: 9 }, scenes: { ...PAYLOAD.scenes, s1: { ...S1, open: 7 } } });
    await act(async () => { announceDiagnosisChanged({ sid: "ch01s1" }); });
    await vi.waitFor(() => expect(host.textContent).toBe("7/9"));
    expect(client.apiGet).toHaveBeenCalledTimes(2);
  });

  it("refreshScene：起草台归档终稿后只拉这一章的 rollup", async () => {
    const { client, WsDiagnosis } = await load();
    await WsDiagnosis.refresh();
    client.apiGet.mockResolvedValueOnce({ project_id: "prj-main", chapter_id: "c1", chapters: { c1: { ...C1, open: 1, blocking: 0, chapter_level: 0, chapter_level_blocking: 0 } }, scenes: { s1: { ...S1, open: 1, blocking: 0 }, s2: { ...S2, open: 0 } } });
    await WsDiagnosis.refreshScene("s1");
    expect(client.apiGet).toHaveBeenLastCalledWith("/api/v1/scenes/s1/diagnosis-rollup");
    expect(WsDiagnosis.sceneCounts("s1")).toMatchObject({ open: 1 });
    expect(WsDiagnosis.totals()).toMatchObject({ open: 1, blocking: 0 });
  });

  it("refreshChapter：章运行的后台作业归档了几场，只拉这一章的 rollup（同一章在途的只拉一次）", async () => {
    const { client, WsDiagnosis } = await load();
    await WsDiagnosis.refresh();
    client.apiGet.mockResolvedValueOnce({ project_id: "prj-main", chapter_id: "c1", chapters: { c1: { ...C1, open: 3, blocking: 0, chapter_level: 0, chapter_level_blocking: 0 } }, scenes: { s1: { ...S1, open: 2, blocking: 0 }, s2: { ...S2, open: 1 } } });
    const first = WsDiagnosis.refreshChapter("c1");
    const second = WsDiagnosis.refreshChapter("c1");
    expect(second).toBe(first);
    await first;
    expect(client.apiGet).toHaveBeenLastCalledWith("/api/v1/chapters/c1/diagnosis-rollup");
    expect(client.apiGet).toHaveBeenCalledTimes(2);
    expect(WsDiagnosis.chapterCounts("c1")).toMatchObject({ open: 3 });
    expect(WsDiagnosis.totals()).toMatchObject({ open: 3, blocking: 0 });
  });
});
