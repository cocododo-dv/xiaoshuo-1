// 诊断计数 store：读 /diagnosis-summary，按后端 scene_id / chapter_id 给计数；
// 读不到就当没有（角标，不是闸门）；深改面板广播 ws:diagnosis-changed 立刻重拉，目录事件节流。
import React, { act } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("./lib/client.js", () => ({ apiGet: vi.fn(), apiPost: vi.fn(), apiPatch: vi.fn(), apiDelete: vi.fn() }));
vi.mock("./ws-works.jsx", () => ({ WsWorks: { activeId: () => "prj-main" } }));

globalThis.IS_REACT_ACT_ENVIRONMENT = true;

const PAYLOAD = {
  project_id: "prj-main",
  totals: { open: 5, blocking: 1, scenes: 2, scenes_with_findings: 2 },
  chapters: { c1: { open: 5, blocking: 1, chapter_level: 1, scenes: 2, scenes_with_findings: 2, ai_status: "current" } },
  scenes: {
    s1: { chapter_id: "c1", open: 3, blocking: 1, revision: 2, taste: 0, info: 0, ignored: 1, stale: 0, ai_status: "current", review_status: "not_run" },
    s2: { chapter_id: "c1", open: 1, blocking: 0 },
  },
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

describe("WsDiagnosis · 每场 / 每章开着的发现数", () => {
  beforeEach(() => { vi.clearAllMocks(); });

  it("读 /diagnosis-summary，按后端 id 给计数；没读到之前是 null，读到后不在表里的场是零", async () => {
    const { client, WsDiagnosis } = await load();
    expect(WsDiagnosis.sceneCounts("s1")).toBeNull();
    expect(WsDiagnosis.loaded()).toBe(false);
    await WsDiagnosis.refresh();
    expect(client.apiGet).toHaveBeenCalledWith("/api/v1/projects/prj-main/diagnosis-summary");
    expect(WsDiagnosis.loaded()).toBe(true);
    expect(WsDiagnosis.sceneCounts("s1")).toMatchObject({ open: 3, blocking: 1, ignored: 1, ai_status: "current" });
    expect(WsDiagnosis.sceneCounts("s2")).toMatchObject({ open: 1, blocking: 0, ai_status: "not_run" });
    expect(WsDiagnosis.sceneCounts("s9")).toMatchObject({ open: 0, blocking: 0 });
    expect(WsDiagnosis.chapterCounts("c1")).toMatchObject({ open: 5, chapter_level: 1, scenes: 2 });
    expect(WsDiagnosis.chapterCounts("c9")).toMatchObject({ open: 0, scenes: 0 });
    expect(WsDiagnosis.totals()).toMatchObject({ open: 5 });
  });

  it("读不到就当没有计数（failed 为真、计数仍是 null），不抛也不弹", async () => {
    const { client, WsDiagnosis } = await load();
    client.apiGet.mockRejectedValueOnce(new Error("boom"));
    await WsDiagnosis.refresh();
    expect(WsDiagnosis.failed()).toBe(true);
    expect(WsDiagnosis.sceneCounts("s1")).toBeNull();
    expect(WsDiagnosis.loaded()).toBe(false);
  });

  it("hook：挂载拉一次；ws:diagnosis-changed 立刻重拉；ws:catalog-changed 在 20 秒内不重拉", async () => {
    const { client, useDiagnosisSummary, announceDiagnosisChanged } = await load();
    function Probe() {
      const diag = useDiagnosisSummary();
      const counts = diag.sceneCounts("s1");
      return React.createElement("div", { "data-testid": "probe" }, counts ? String(counts.open) : "—");
    }
    const host = document.createElement("div");
    document.body.appendChild(host);
    const root = createRoot(host);
    mounted.push({ root, host });
    await act(async () => root.render(React.createElement(Probe)));
    await vi.waitFor(() => expect(host.textContent).toBe("3"));
    expect(client.apiGet).toHaveBeenCalledTimes(1);

    await act(async () => { window.dispatchEvent(new CustomEvent("ws:catalog-changed")); });
    expect(client.apiGet).toHaveBeenCalledTimes(1);

    client.apiGet.mockResolvedValueOnce({ ...PAYLOAD, scenes: { ...PAYLOAD.scenes, s1: { ...PAYLOAD.scenes.s1, open: 2 } } });
    await act(async () => { announceDiagnosisChanged({ sid: "ch01s1" }); });
    await vi.waitFor(() => expect(host.textContent).toBe("2"));
    expect(client.apiGet).toHaveBeenCalledTimes(2);
  });
});
