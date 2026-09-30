// AI 起草台 · 在办清单：队列成员的后端派生（scnBackendRunSids）与移出名单。
// 贯通轮遗留 ①：GET /scene-run-states 是队列成员真相源，localStorage 退化为读缓存——
// 这里验证「run-states → 目录 backendId 对位 → sid 列表」的派生契约与其兜底路径。
// （2026-09-29 从 ws-scene-run.test.jsx 原样拆出。）
import React, { act } from "react";
import { createRoot } from "react-dom/client";
import { readFileSync } from "node:fs";
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { installApiRouter, DEFAULT_CHAP, DEFAULT_PROJECT } from "./test-helpers.js";
import {
  T, RUN_STATES_URL, NON_DEMO_PROJECT, TWO_SCENE_CHAP, settleActive, routeRunStates, loadSceneRun,
  mountedRoots, renderRunJobControl, click, deferred, queueSceneIntent,
} from "./ws-scene-run.test-harness.jsx";

vi.mock("./lib/client.js", () => ({
  apiGet: vi.fn(),
  apiPost: vi.fn(),
  apiPatch: vi.fn(),
  apiDelete: vi.fn(),
}));
// 任务控制条的两个请求（ws-scene-job-api.js）
vi.mock("./ws-scene-job-api.js", () => ({ cancelRunJob: vi.fn(), getLatestSceneRunJob: vi.fn() }));

describe("scnBackendRunSids（队列成员的后端派生）", () => {
  beforeEach(() => {
    vi.resetModules();
    window.localStorage.clear();
  });
  afterEach(() => vi.restoreAllMocks());

  it("run-states 按目录 backendId 对位成 sid 列表；无对位的场丢弃", async () => {
    const { mod, client } = await loadSceneRun();
    // 等目录装载（派生依赖 backendId → sid 映射）
    const cat = await import("./ws-catalog.jsx");
    await vi.waitFor(() => expect(cat.WsCatalog.get().length).toBeGreaterThan(0), T);
    routeRunStates(client, () =>
      Promise.resolve({
        items: [
          { scene_id: "s1", scene_status: "human_review_required" },
          { scene_id: "s-ghost", scene_status: "archived" }, // 目录里没有：丢弃
        ],
      })
    );

    const sids = await mod.scnBackendRunSids();

    expect(sids).toEqual(["ch01s1"]);
    expect(client.apiGet).toHaveBeenCalledWith("/api/v1/scene-run-states?project_id=prj-main");
  });

  it("目录为空时先 __refresh 再对位（换浏览器冷启动路径）", async () => {
    // 启动装载吃到空目录（installApiRouter 的 catalog: []）；之后经包装路由
    // 返回真实章——模拟「目录还没就绪就进起草台」的竞态，派生应自行补拉
    const { mod, client } = await loadSceneRun({ catalog: [] });
    const cat = await import("./ws-catalog.jsx");
    await vi.waitFor(() => expect(cat.WsCatalog.ready()).toBe(true), T);
    expect(cat.WsCatalog.get().length).toBe(0);
    const base = client.apiGet.getMockImplementation();
    client.apiGet.mockImplementation((url) => {
      if (RUN_STATES_URL.test(url)) {
        return Promise.resolve({ items: [{ scene_id: "s1", scene_status: "soft_qc_patch_required" }] });
      }
      if (/\/api\/v2\/projects\/[^/]+\/catalog(\?|$)/.test(url)) {
        return Promise.resolve({ chapters: [DEFAULT_CHAP] });
      }
      return base(url);
    });

    const sids = await mod.scnBackendRunSids();

    expect(sids).toEqual(["ch01s1"]);
  });

  it("run-states 端点失败时返回 null（读不到 ≠ 没进过管线；本地队列照常可用，不炸）", async () => {
    const { mod, client } = await loadSceneRun();
    routeRunStates(client, () => Promise.reject(new Error("boom")));

    const sids = await mod.scnBackendRunSids();

    expect(sids).toBeNull();
  });
});

describe("scnQueueDismiss（移出名单：满了也不能让「移出」变成假动作）", () => {
  // 名单按作品持久化在 localStorage 里，用例之间必须自己隔离
  beforeEach(() => { localStorage.clear(); });

  it("名单满 200 之后，新移出的场仍然被记住", async () => {
    const { mod } = await loadSceneRun();
    const older = Array.from({ length: 200 }, (_, i) => `old-${i}`);
    mod.scnQueueDismissAdd(older);
    expect(mod.scnQueueDismissLoad()).toHaveLength(200);

    // 作者移出第 201 场：追加在尾部 + slice(0, 200) 会把它整个丢掉
    const stored = mod.scnQueueDismissAdd(["scene-201"]);
    expect(stored).toContain("scene-201");
    expect(mod.scnQueueDismissLoad()).toContain("scene-201");
    expect(mod.scnQueueDismissLoad()).toHaveLength(200);
    // 让位的是最老的一条，不是刚移出的那条
    expect(mod.scnQueueDismissLoad()).not.toContain("old-0");
  });

  it("scnQueueDismissAdd 返回的是真正落盘的名单", async () => {
    const { mod } = await loadSceneRun();
    mod.scnQueueDismissAdd(Array.from({ length: 260 }, (_, i) => `s-${i}`));
    // 调用方拿返回值当「现在的移出名单」用，它必须和 localStorage 里的一致
    expect(mod.scnQueueDismissAdd(["s-260"])).toEqual(mod.scnQueueDismissLoad());
  });

  it("重新入列时销名（原有契约不被截断改动打坏）", async () => {
    const { mod } = await loadSceneRun();
    mod.scnQueueDismissAdd(["a", "b", "c"]);
    expect(mod.scnQueueDismissClear(["b"])).toEqual(["a", "c"]);
    expect(mod.scnQueueDismissLoad()).toEqual(["a", "c"]);
  });
});
