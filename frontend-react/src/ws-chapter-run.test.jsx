import React, { act } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("./lib/client.js", () => ({
  apiGet: vi.fn(),
  apiPost: vi.fn(),
}));

vi.mock("./ws-works.jsx", () => ({
  WsWorks: { activeId: vi.fn(() => "project-1") },
}));

vi.mock("./ws-diagnosis-summary.jsx", () => ({
  WsDiagnosis: { refreshChapter: vi.fn(() => Promise.resolve()), refreshScene: vi.fn(() => Promise.resolve()) },
}));
vi.mock("./ws-catalog.jsx", () => ({
  WsCatalog: {
    __refresh: vi.fn(() => Promise.resolve()),
    get: vi.fn(() => []),
  },
}));

import { apiGet, apiPost } from "./lib/client.js";
import { WsCatalog } from "./ws-catalog.jsx";
import { WsDiagnosis } from "./ws-diagnosis-summary.jsx";
import { WsWorks } from "./ws-works.jsx";
import { ArrChapterRunAction, normalizeRun } from "./ws-chapter-run.jsx";

globalThis.IS_REACT_ACT_ENVIRONMENT = true;

const mounted = [];
const CHAPTER = { id: "ch01", backendId: "chapter-1", title: "盐场的早班", state: "writing", current: true };

async function renderRun(props = {}) {
  const host = document.createElement("div");
  document.body.appendChild(host);
  const root = createRoot(host);
  const record = { host, root, live: true };
  mounted.push(record);
  await act(async () => {
    root.render(<ArrChapterRunAction chapter={CHAPTER} pollIntervalMs={25} {...props} />);
    await Promise.resolve();
    await Promise.resolve();
  });
  return {
    host,
    button: () => host.querySelector('[data-testid="chapter-run-start"]'),
    async rerender(nextProps = {}) {
      await act(async () => {
        root.render(<ArrChapterRunAction chapter={CHAPTER} pollIntervalMs={25} {...props} {...nextProps} />);
        await Promise.resolve();
        await Promise.resolve();
      });
    },
    async unmount() {
      if (!record.live) return;
      record.live = false;
      await act(async () => root.unmount());
      host.remove();
    },
  };
}

async function click(node) {
  await act(async () => {
    node.dispatchEvent(new MouseEvent("click", { bubbles: true }));
    await Promise.resolve();
    await Promise.resolve();
  });
}

function runPayload(status, overrides = {}) {
  return {
    job_id: "job-1",
    status,
    scene_count: 3,
    completed_count: 0,
    progress_pct: 0,
    latest_error: null,
    offline_demo: false,
    ...overrides,
  };
}

beforeEach(() => {
  vi.useFakeTimers();
  vi.clearAllMocks();
  // 运行本章前有一道确认（外壳提示层没挂载时 wsConfirm 退回 window.confirm）
  vi.spyOn(window, "confirm").mockReturnValue(true);
  WsWorks.activeId.mockReturnValue("project-1");
  WsCatalog.__refresh.mockResolvedValue();
  WsCatalog.get.mockReturnValue([]);
  apiGet.mockResolvedValue(runPayload("idle", { job_id: null, scene_count: 3 }));
});

afterEach(async () => {
  vi.restoreAllMocks();
  while (mounted.length) {
    const record = mounted.pop();
    if (record.live) {
      record.live = false;
      await act(async () => record.root.unmount());
      record.host.remove();
    }
  }
  vi.useRealTimers();
});

describe("章节编排 · 运行本章真实接线", () => {
  it("idle 是后端可水合的真实状态，不是无效返回", () => {
    expect(normalizeRun(runPayload("idle", { job_id: null })).status).toBe("idle");
  });

  it("只提交真实 run-job 空载荷，防双击，并从 pending 轮询到 running/completed", async () => {
    let resolveStart;
    apiPost.mockReturnValueOnce(new Promise((resolve) => { resolveStart = resolve; }));
    apiGet
      .mockResolvedValueOnce(runPayload("idle", { job_id: null }))
      .mockResolvedValueOnce(runPayload("running", { completed_count: 1, progress_pct: 33, current_scene_id: "scene-2" }))
      .mockResolvedValueOnce(runPayload("completed", { completed_count: 3, progress_pct: 100 }));
    const onCatalogRefresh = vi.fn();
    const onOpenReview = vi.fn();
    const view = await renderRun({ onCatalogRefresh, onOpenReview });

    await act(async () => {
      view.button().dispatchEvent(new MouseEvent("click", { bubbles: true }));
      view.button().dispatchEvent(new MouseEvent("click", { bubbles: true }));
    });
    expect(apiPost).toHaveBeenCalledTimes(1);
    expect(apiPost).toHaveBeenCalledWith(
      "/api/v1/projects/project-1/chapters/chapter-1/run-job",
      {},
    );
    expect(apiPost.mock.calls[0][1]).not.toHaveProperty("offline_demo");

    await act(async () => {
      resolveStart({ run: runPayload("pending") });
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(view.host.textContent).toContain("已排队");
    expect(view.button().disabled).toBe(true);

    await act(async () => { await vi.advanceTimersByTimeAsync(25); });
    expect(apiGet).toHaveBeenNthCalledWith(2, "/api/v1/chapters/chapter-1/run-status");
    expect(view.host.textContent).toContain("正在运行");
    expect(view.host.textContent).toContain("1 / 3 个场景");
    expect(view.host.textContent).toContain("33%");
    /* 后台作业归档了第一场：这一章的诊断角标随之刷新 */
    expect(WsDiagnosis.refreshChapter).toHaveBeenCalledWith("chapter-1");

    await act(async () => {
      await vi.advanceTimersByTimeAsync(25);
      await Promise.resolve();
    });
    expect(view.host.textContent).toContain("本章已完成");
    expect(view.host.textContent).toContain("去成稿中心审阅");
    expect(WsCatalog.__refresh).toHaveBeenCalledTimes(1);
    expect(WsCatalog.__refresh).toHaveBeenCalledWith("project-1");
    expect(onCatalogRefresh).toHaveBeenCalledWith([]);
    expect(WsDiagnosis.refreshChapter).toHaveBeenCalledTimes(2);

    await click(view.host.querySelector('[data-testid="chapter-run-review"]'));
    expect(onOpenReview).toHaveBeenCalledTimes(1);
  });

  it("模型未配置时明确提示“请配置模型”，不会假装运行成功", async () => {
    apiPost.mockRejectedValueOnce(Object.assign(new Error("LLM is disabled"), {
      code: "LLM_DISABLED_FOR_CHAPTER_RUN",
    }));
    const onConfigureModel = vi.fn();
    const view = await renderRun({ onConfigureModel });

    await click(view.button());

    expect(view.host.textContent).toContain("当前未配置可用模型");
    expect(view.host.textContent).toContain("请配置模型");
    expect(view.host.textContent).not.toContain("本章已完成");
    expect(apiGet).toHaveBeenCalledTimes(1);
    expect(WsCatalog.__refresh).not.toHaveBeenCalled();

    const configure = [...view.host.querySelectorAll("button")].find((node) => node.textContent.includes("请配置模型"));
    await click(configure);
    expect(onConfigureModel).toHaveBeenCalledTimes(1);
  });

  it("缺少 backendId 时诚实报错，绝不发送伪造章节请求", async () => {
    const view = await renderRun({ chapter: { id: "local-only", title: "未同步章节" } });

    await click(view.button());

    // 旁边是短句，整句原因在悬停提示里（窄屏章头也放得下）
    expect(view.host.querySelector(".arr-run-reason").textContent).toBe("尚未同步到后端");
    expect(view.host.querySelector(".arr-run-reason").title).toContain("当前章节尚未同步到后端");
    expect(view.button().title).toContain("当前章节尚未同步到后端");
    expect(apiPost).not.toHaveBeenCalled();
    expect(apiGet).not.toHaveBeenCalled();
  });

  it.each([
    ["blocked", "运行受阻", { code: "CHAPTER_RUN_BACKFILL_PENDING", message: "仍有回填内容待处理。" }],
    ["failed", "运行失败", { code: "CHAPTER_RUN_FAILED", message: "场景执行失败。" }],
  ])("展示后端 %s 终态及原始处理提示", async (status, label, latestError) => {
    apiPost.mockResolvedValueOnce({
      run: runPayload(status, {
        completed_count: 1,
        progress_pct: 33,
        latest_error: latestError,
      }),
    });
    const view = await renderRun();

    await click(view.button());

    expect(view.host.textContent).toContain(label);
    expect(view.host.textContent).toContain(latestError.message);
    expect(view.host.textContent).toContain("33%");
    expect(apiGet).toHaveBeenCalledTimes(1);
  });

  it("卸载时清除 pending 轮询，不在离开页面后继续请求", async () => {
    apiPost.mockResolvedValueOnce({ run: runPayload("pending") });
    const view = await renderRun();
    await click(view.button());
    expect(view.host.textContent).toContain("已排队");
    const callsBeforeUnmount = apiGet.mock.calls.length;

    await view.unmount();
    await act(async () => { await vi.advanceTimersByTimeAsync(100); });

    expect(apiGet).toHaveBeenCalledTimes(callsBeforeUnmount);
  });

  it("mount 先水合 run-status，水合未完成时禁止启动", async () => {
    let resolveStatus;
    apiGet.mockReturnValueOnce(new Promise(resolve => { resolveStatus = resolve; }));
    const view = await renderRun();

    expect(apiGet).toHaveBeenCalledWith("/api/v1/chapters/chapter-1/run-status");
    expect(view.button().disabled).toBe(true);
    expect(view.button().textContent).toContain("同步状态中");
    await click(view.button());
    expect(apiPost).not.toHaveBeenCalled();

    await act(async () => {
      resolveStatus(runPayload("running", { completed_count: 1, progress_pct: 33 }));
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(view.host.textContent).toContain("正在运行");
    expect(view.button().disabled).toBe(true);
  });

  it("切章立即重新水合，迟到的旧章状态不会覆盖新章", async () => {
    let resolveOld;
    apiGet
      .mockReturnValueOnce(new Promise(resolve => { resolveOld = resolve; }))
      .mockResolvedValueOnce(runPayload("idle", { job_id: null }));
    const view = await renderRun();

    await view.rerender({ chapter: { ...CHAPTER, id: "ch02", backendId: "chapter-2" } });
    expect(apiGet).toHaveBeenNthCalledWith(2, "/api/v1/chapters/chapter-2/run-status");
    expect(view.button().disabled).toBe(false);

    await act(async () => {
      resolveOld(runPayload("completed", { completed_count: 3, progress_pct: 100 }));
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(view.host.textContent).not.toContain("本章已完成");
    expect(view.button().disabled).toBe(false);
  });

  it("已批准章与非 current 章在水合后仍禁止运行", async () => {
    const nonCurrent = await renderRun({ chapter: { ...CHAPTER, current: false, backendId: "chapter-old" } });
    expect(nonCurrent.button().disabled).toBe(true);
    expect(nonCurrent.button().title).toContain("当前章");
    await click(nonCurrent.button());
    expect(apiPost).not.toHaveBeenCalled();

    const approved = await renderRun({ chapter: { ...CHAPTER, state: "approved", backendId: "chapter-approved" } });
    expect(approved.button().disabled).toBe(true);
    expect(approved.button().title).toContain("终稿");
    await click(approved.button());
    expect(apiPost).not.toHaveBeenCalled();
  });

  it("水合失败显示重试，失败期间不会用本地 idle 启动", async () => {
    apiGet
      .mockRejectedValueOnce(new Error("运行状态服务不可用"))
      .mockResolvedValueOnce(runPayload("idle", { job_id: null }));
    const view = await renderRun();

    expect(view.host.textContent).toContain("运行状态服务不可用");
    expect(view.button().disabled).toBe(true);
    await click(view.host.querySelector('[data-testid="chapter-run-hydration-retry"]'));
    await act(async () => { await Promise.resolve(); await Promise.resolve(); });

    expect(apiGet).toHaveBeenCalledTimes(2);
    expect(view.button().disabled).toBe(false);
  });

  it("运行本章先确认要跑几场；作者取消就不提交任务", async () => {
    window.confirm.mockReturnValue(false);
    const view = await renderRun();

    await click(view.button());

    expect(window.confirm).toHaveBeenCalledTimes(1);
    expect(window.confirm.mock.calls[0][0]).toContain("本章的 3 场");
    expect(apiPost).not.toHaveBeenCalled();
    expect(view.button().disabled).toBe(false);
  });

  it("打开一章时读到「上次已完成」：只给一枚安静的小标签，不弹浮层、不重拉目录；点开能看、能收起", async () => {
    apiGet.mockResolvedValueOnce(runPayload("completed", { completed_count: 3, progress_pct: 100 }));
    const view = await renderRun();

    expect(view.host.querySelector(".arr-run-card")).toBeNull();
    const chip = view.host.querySelector(".arr-run-chip");
    expect(chip.textContent).toBe("上次运行：已完成");
    expect(WsCatalog.__refresh).not.toHaveBeenCalled();

    await click(chip);
    expect(view.host.querySelector(".arr-run-card").textContent).toContain("本章已完成");
    expect(view.host.querySelector('[data-testid="chapter-run-review"]')).not.toBeNull();
    await click(view.host.querySelector(".arr-run-close"));
    expect(view.host.querySelector(".arr-run-card")).toBeNull();
    expect(view.host.querySelector(".arr-run-chip")).not.toBeNull();
  });

  it("亲眼看着跑完的完成卡也能收起", async () => {
    apiPost.mockResolvedValueOnce({ run: runPayload("running", { completed_count: 1, progress_pct: 33 }) });
    apiGet
      .mockResolvedValueOnce(runPayload("idle", { job_id: null }))
      .mockResolvedValueOnce(runPayload("completed", { completed_count: 3, progress_pct: 100 }));
    const view = await renderRun();
    await click(view.button());
    await act(async () => { await vi.advanceTimersByTimeAsync(25); await Promise.resolve(); });
    expect(view.host.querySelector(".arr-run-card").textContent).toContain("本章已完成");
    expect(WsCatalog.__refresh).toHaveBeenCalledTimes(1);

    await click(view.host.querySelector(".arr-run-close"));
    expect(view.host.querySelector(".arr-run-card")).toBeNull();
    expect(view.host.querySelector(".arr-run-chip").textContent).toBe("上次运行：已完成");
  });

  it("不能运行的原因直接写在按钮旁边", async () => {
    const nonCurrent = await renderRun({ chapter: { ...CHAPTER, current: false, backendId: "chapter-old" } });
    expect(nonCurrent.host.querySelector(".arr-run-reason").textContent).toBe("只能运行当前章");
    const unsynced = await renderRun({ chapter: { id: "local-only", title: "未同步章节", current: true } });
    expect(unsynced.host.querySelector(".arr-run-reason").textContent).toContain("尚未同步到后端");
    expect(unsynced.host.querySelector(".arr-run-card")).toBeNull();
  });
});
