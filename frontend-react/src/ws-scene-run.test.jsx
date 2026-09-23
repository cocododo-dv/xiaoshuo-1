// ws-scene-run store 层单测：队列成员的后端派生（scnBackendQueueSids）。
// 贯通轮遗留 ①：GET /scene-run-states 是队列成员真相源，localStorage 退化为读缓存——
// 这里验证「run-states → 目录 backendId 对位 → sid 列表」的派生契约与其兜底路径。
import React, { act } from "react";
import { createRoot } from "react-dom/client";
import { readFileSync } from "node:fs";
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { installApiRouter, DEFAULT_CHAP, DEFAULT_PROJECT } from "./test-helpers.js";

globalThis.IS_REACT_ACT_ENVIRONMENT = true;

vi.mock("./lib/client.js", () => ({
  apiGet: vi.fn(),
  apiPost: vi.fn(),
  apiPatch: vi.fn(),
  apiDelete: vi.fn(),
  cancelRunJob: vi.fn(),
  getLatestSceneRunJob: vi.fn(),
}));

// ws-scene-run 已不再依赖 ws-snow.jsx（离线起草链已退役）；
// ws-catalog 链上的 ws-snow-sync 只取 S2_BE_STEPS。mock 掉避免拉入整张雪花视图。
vi.mock("./ws-snow.jsx", () => ({ S2_BE_STEPS: [], s2ExportState: () => null }));

const T = { timeout: 5000, interval: 25 };

const RUN_STATES_URL = /^\/api\/v1\/scene-run-states\?/;
const NON_DEMO_PROJECT = { ...DEFAULT_PROJECT, project_id: "novel-1", title: "回归小说", is_demo: false };
const TWO_SCENE_CHAP = {
  ...DEFAULT_CHAP,
  scenes: [
    ...DEFAULT_CHAP.scenes,
    {
      ...DEFAULT_CHAP.scenes[0],
      slug: "ch01s2",
      scene_id: "s2",
      title: "回潮",
      brief: { goal: "追上证人", conflict: "潮水封路", setback: "证人失踪" },
    },
  ],
};

async function settleActive(projectId = "prj-main") {
  await vi.waitFor(() => expect(window.WsWorks && window.WsWorks.activeId()).toBe(projectId), T);
}

/* 在 installApiRouter 之上叠一层 scene-run-states 路由（贯通轮惯用法：包装现有实现） */
function routeRunStates(client, responder) {
  const base = client.apiGet.getMockImplementation();
  client.apiGet.mockImplementation((url) => {
    if (RUN_STATES_URL.test(url)) return responder(url);
    return base(url);
  });
}

async function loadSceneRun(opts) {
  const client = await import("./lib/client.js");
  installApiRouter(client, opts);
  // 场景采用依赖真实 author-draft 元数据；默认模拟器也应返回 draft/revision，
  // 避免测试继续走已经删除的“没有服务端作者稿也照样归档”旧路径。
  const basePost = client.apiPost.getMockImplementation();
  client.apiPost.mockImplementation((url, body, options) => {
    if (/\/api\/v1\/author-drafts\/scene\/s1\/ensure$/.test(url)) {
      return Promise.resolve({
        draft: {
          draft_id: "author_draft_scene_s1",
          revision_no: 1,
          content: "",
          last_promoted_revision_no: null,
          last_promoted_final_scene_row_id: null,
          canonical_dirty: true,
        },
        runtime_final_ref: null,
      });
    }
    return basePost(url, body, options);
  });
  const mod = await import("./ws-scene-run.jsx");
  await settleActive((opts && opts.projects && opts.projects[0] && opts.projects[0].project_id) || "prj-main");
  return { mod, client };
}

const mountedRoots = [];

async function renderRunJobControl(Component, props) {
  const host = document.createElement("div");
  document.body.appendChild(host);
  const root = createRoot(host);
  mountedRoots.push({ root, host });
  await act(async () => {
    root.render(<Component {...props} />);
  });
  return {
    host,
    rerender: async (nextProps) => {
      await act(async () => {
        root.render(<Component {...nextProps} />);
      });
    },
    unmount: async () => {
      const index = mountedRoots.findIndex((item) => item.root === root);
      if (index >= 0) mountedRoots.splice(index, 1);
      await act(async () => root.unmount());
      host.remove();
    },
  };
}

async function click(element) {
  await act(async () => {
    element.dispatchEvent(new MouseEvent("click", { bubbles: true }));
  });
}

function deferred() {
  let resolve;
  let reject;
  const promise = new Promise((ok, fail) => { resolve = ok; reject = fail; });
  return { promise, resolve, reject };
}

async function queueSceneIntent(detail) {
  const { queueViewIntent } = await import("./ws-view-intents.js");
  queueViewIntent("scene", "ws:scene-enqueue", detail);
}

describe("scene run cancellation client", () => {
  beforeEach(() => {
    window.localStorage.clear();
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("uses the authoritative latest path and the shared POST idempotency contract", async () => {
    const client = await vi.importActual("./lib/client.js");
    const fetchMock = vi.fn()
      .mockResolvedValueOnce({
        ok: true,
        status: 200,
        json: async () => ({ ok: true, data: { job_id: "job/latest" } }),
      })
      .mockRejectedValueOnce(new TypeError("Failed to fetch"))
      .mockResolvedValueOnce({
        ok: true,
        status: 200,
        json: async () => ({ ok: true, data: { job_id: "job/retry", status: "cancel_requested" } }),
      });
    vi.stubGlobal("fetch", fetchMock);

    await client.getLatestSceneRunJob("SC /一");
    await expect(client.cancelRunJob("job/retry")).rejects.toMatchObject({
      code: "NETWORK_ERROR",
      retryable: true,
    });
    await client.cancelRunJob("job/retry");

    expect(fetchMock.mock.calls[0][0]).toMatch(/\/api\/v1\/scenes\/SC%20%2F%E4%B8%80\/run\/jobs\/latest$/);
    const firstCancel = fetchMock.mock.calls[1];
    const retryCancel = fetchMock.mock.calls[2];
    expect(firstCancel[0]).toMatch(/\/api\/v1\/run-jobs\/job%2Fretry\/cancel$/);
    expect(firstCancel[1]).toMatchObject({ method: "POST", body: "{}" });
    expect(firstCancel[1].headers["X-Operator-Ref"]).toBe("operator");
    expect(firstCancel[1].headers["X-Idempotency-Key"]).toBeTruthy();
    expect(retryCancel[1].headers["X-Idempotency-Key"]).toBe(
      firstCancel[1].headers["X-Idempotency-Key"],
    );
  });

  it("forwards AbortSignal to fetch and reports an intentional abort faithfully", async () => {
    const client = await vi.importActual("./lib/client.js");
    let fetchSignal = null;
    vi.stubGlobal("fetch", vi.fn((url, init) => new Promise((resolve, reject) => {
      void url;
      void resolve;
      fetchSignal = init.signal;
      init.signal.addEventListener("abort", () => reject(new DOMException("aborted", "AbortError")), { once: true });
    })));
    const controller = new AbortController();

    const pending = client.getLatestSceneRunJob("SC01", { signal: controller.signal });
    controller.abort();

    await expect(pending).rejects.toMatchObject({ code: "REQUEST_ABORTED", retryable: true });
    expect(fetchSignal).not.toBe(controller.signal);
    expect(controller.signal.aborted).toBe(true);
    expect(fetchSignal.aborted).toBe(true);
    expect(fetchSignal.aborted).toBe(true);
  });
});

describe("SceneRunJobControl", () => {
  beforeEach(() => {
    vi.resetModules();
    window.localStorage.clear();
  });

  afterEach(async () => {
    while (mountedRoots.length) {
      const { root, host } = mountedRoots.pop();
      await act(async () => root.unmount());
      host.remove();
    }
    vi.restoreAllMocks();
  });

  it("treats latest 404 as an ordinary no-job state with accessible status", async () => {
    const { mod, client } = await loadSceneRun();
    client.getLatestSceneRunJob.mockRejectedValue(
      Object.assign(new Error("not found"), { status: 404, code: "RUN_JOB_NOT_FOUND" }),
    );

    const view = await renderRunJobControl(mod.SceneRunJobControl, { sceneId: "SC01" });

    await vi.waitFor(() => {
      expect(view.host.querySelector('[role="status"]')?.textContent).toContain("暂无运行任务");
    }, T);
    expect(view.host.querySelector('[role="alert"]')).toBeNull();
    expect(view.host.querySelector('[data-testid="scene-run-cancel-button"]')).toBeNull();
  });

  it.each(["queued", "running"])("cancels %s once despite repeated clicks", async (status) => {
    const { mod, client } = await loadSceneRun();
    client.getLatestSceneRunJob.mockResolvedValue({ job_id: `job-${status}`, scene_id: "SC01", status });
    let resolveCancel;
    client.cancelRunJob.mockImplementation(() => new Promise((resolve) => { resolveCancel = resolve; }));

    const view = await renderRunJobControl(mod.SceneRunJobControl, { sceneId: "SC01" });
    await vi.waitFor(() => expect(view.host.querySelector('[data-testid="scene-run-cancel-button"]')).toBeTruthy(), T);
    const button = view.host.querySelector('[data-testid="scene-run-cancel-button"]');
    await act(async () => {
      button.dispatchEvent(new MouseEvent("click", { bubbles: true }));
      button.dispatchEvent(new MouseEvent("click", { bubbles: true }));
    });

    expect(button.disabled).toBe(true);
    expect(client.cancelRunJob).toHaveBeenCalledTimes(1);
    await act(async () => {
      resolveCancel({ job_id: `job-${status}`, scene_id: "SC01", status: "cancel_requested" });
    });
    expect(view.host.querySelector('[role="status"]').textContent).toContain("正在取消");
    expect(view.host.querySelector('[data-testid="scene-run-cancel-button"]')?.disabled).toBe(true);
  });

  it("polls cancel_requested until cancelled and exposes every state through aria-live", async () => {
    const { mod, client } = await loadSceneRun();
    client.getLatestSceneRunJob
      .mockResolvedValueOnce({ job_id: "job-1", scene_id: "SC01", status: "cancel_requested" })
      .mockResolvedValue({ job_id: "job-1", scene_id: "SC01", status: "cancelled" });

    const view = await renderRunJobControl(mod.SceneRunJobControl, {
      sceneId: "SC01",
      pollIntervalMs: 5,
    });

    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 15));
    });

    await vi.waitFor(() => {
      const status = view.host.querySelector('[role="status"]');
      expect(status?.getAttribute("aria-live")).toBe("polite");
      expect(status?.textContent).toContain("已取消");
    }, T);
    expect(client.getLatestSceneRunJob.mock.calls.length).toBeGreaterThanOrEqual(2);
    expect(view.host.querySelector('[data-testid="scene-run-cancel-button"]')).toBeNull();
  });

  it("keeps a committed POST job newer than an older latest request", async () => {
    const { mod, client } = await loadSceneRun();
    let rejectOldLatest;
    client.getLatestSceneRunJob.mockImplementation(() => new Promise((resolve, reject) => {
      rejectOldLatest = reject;
    }));
    const view = await renderRunJobControl(mod.SceneRunJobControl, { sceneId: "SC01" });

    await view.rerender({
      sceneId: "SC01",
      observedJob: { job_id: "job-new", scene_id: "SC01", status: "running" },
    });
    await vi.waitFor(() => expect(view.host.querySelector('[role="status"]')?.textContent).toContain("运行中"), T);
    await act(async () => {
      rejectOldLatest(Object.assign(new Error("not found"), { status: 404, code: "RUN_JOB_NOT_FOUND" }));
    });

    expect(view.host.querySelector('[data-testid="scene-run-job-control"]')?.dataset.jobId).toBe("job-new");
  });

  it("does not regress cancel_requested when an older running observation arrives late", async () => {
    const { mod, client } = await loadSceneRun();
    client.getLatestSceneRunJob.mockResolvedValue({ job_id: "job-monotonic", scene_id: "SC01", status: "running" });
    client.cancelRunJob.mockResolvedValue({ job_id: "job-monotonic", scene_id: "SC01", status: "cancel_requested" });
    const view = await renderRunJobControl(mod.SceneRunJobControl, { sceneId: "SC01" });
    await vi.waitFor(() => expect(view.host.querySelector('[data-testid="scene-run-cancel-button"]')).toBeTruthy(), T);

    await click(view.host.querySelector('[data-testid="scene-run-cancel-button"]'));
    await vi.waitFor(() => expect(view.host.querySelector('[role="status"]')?.textContent).toContain("正在取消"), T);
    await view.rerender({
      sceneId: "SC01",
      observedJob: { job_id: "job-monotonic", scene_id: "SC01", status: "running" },
    });

    expect(view.host.querySelector('[data-testid="scene-run-job-control"]')?.dataset.status).toBe("cancel_requested");
    expect(view.host.querySelector('[data-testid="scene-run-cancel-button"]')?.disabled).toBe(true);
  });

  it("does not let a late cancel response for job A overwrite a newer latest job B", async () => {
    const { mod, client } = await loadSceneRun();
    client.getLatestSceneRunJob
      .mockResolvedValueOnce({ job_id: "job-a", scene_id: "SC01", status: "running" })
      .mockResolvedValue({ job_id: "job-b", scene_id: "SC01", status: "completed" });
    let resolveCancelA;
    client.cancelRunJob.mockImplementation(() => new Promise(resolve => { resolveCancelA = resolve; }));
    const view = await renderRunJobControl(mod.SceneRunJobControl, {
      sceneId: "SC01",
      pollIntervalMs: 5,
    });
    await vi.waitFor(() => expect(view.host.querySelector('[data-testid="scene-run-job-control"]')?.dataset.jobId).toBe("job-a"), T);

    await click(view.host.querySelector('[data-testid="scene-run-cancel-button"]'));
    await act(async () => { await new Promise(resolve => setTimeout(resolve, 15)); });
    expect(view.host.querySelector('[data-testid="scene-run-job-control"]')?.dataset.jobId).toBe("job-b");
    await act(async () => {
      resolveCancelA({ job_id: "job-a", scene_id: "SC01", status: "cancelled" });
    });

    await vi.waitFor(() => {
      const control = view.host.querySelector('[data-testid="scene-run-job-control"]');
      expect(control?.dataset.jobId).toBe("job-b");
      expect(control?.dataset.status).toBe("completed");
      expect(view.host.querySelector('[data-testid="scene-run-cancel-button"]')).toBeNull();
    }, T);
  });

  it("does not attach a late cancel failure for job A to a newer latest job B", async () => {
    const { mod, client } = await loadSceneRun();
    client.getLatestSceneRunJob
      .mockResolvedValueOnce({ job_id: "job-a", scene_id: "SC01", status: "running" })
      .mockResolvedValue({ job_id: "job-b", scene_id: "SC01", status: "completed" });
    let rejectCancelA;
    client.cancelRunJob.mockImplementation(() => new Promise((resolve, reject) => {
      void resolve;
      rejectCancelA = reject;
    }));
    const view = await renderRunJobControl(mod.SceneRunJobControl, {
      sceneId: "SC01",
      pollIntervalMs: 5,
    });
    await vi.waitFor(() => expect(view.host.querySelector('[data-testid="scene-run-job-control"]')?.dataset.jobId).toBe("job-a"), T);

    await click(view.host.querySelector('[data-testid="scene-run-cancel-button"]'));
    await act(async () => { await new Promise(resolve => setTimeout(resolve, 15)); });
    expect(view.host.querySelector('[data-testid="scene-run-job-control"]')?.dataset.jobId).toBe("job-b");
    await act(async () => {
      rejectCancelA(Object.assign(new Error("cancel A failed"), { code: "NETWORK_ERROR", retryable: true }));
    });

    await vi.waitFor(() => {
      const control = view.host.querySelector('[data-testid="scene-run-job-control"]');
      expect(control?.dataset.jobId).toBe("job-b");
      expect(control?.dataset.status).toBe("completed");
      expect(view.host.querySelector('[role="alert"]')).toBeNull();
    }, T);
  });

  it("refreshSignal refetches a terminal job so the banner converges to archived", async () => {
    // C2 状态一致性债务：归档后终态 job 不轮询，横幅停在旧暂停点
    // （awaiting_candidate_selection）；父组件归档成功后递增 refreshSignal
    // 强制重取 latest，后端视图层已把 current_step 收敛为 archived。
    const { mod, client } = await loadSceneRun();
    client.getLatestSceneRunJob
      .mockResolvedValueOnce({
        job_id: "job-adopt",
        scene_id: "SC01",
        status: "completed",
        current_step: "awaiting_candidate_selection",
      })
      .mockResolvedValue({
        job_id: "job-adopt",
        scene_id: "SC01",
        status: "completed",
        current_step: "archived",
        scene_status: "archived",
      });

    const view = await renderRunJobControl(mod.SceneRunJobControl, { sceneId: "SC01" });
    await vi.waitFor(() => {
      // 候选终选暂停点读作中文，不再原样回显英文 token
      expect(view.host.querySelector('[role="status"]')?.textContent).toContain("等你终选");
    }, T);
    expect(view.host.querySelector('[role="status"]')?.textContent).not.toContain("awaiting_candidate_selection");
    // 终态 job 不轮询：没有 refreshSignal 时不会自己刷新
    expect(client.getLatestSceneRunJob).toHaveBeenCalledTimes(1);

    await view.rerender({ sceneId: "SC01", refreshSignal: 1 });

    await vi.waitFor(() => {
      expect(view.host.querySelector('[role="status"]')?.textContent).toContain("已归档");
    }, T);
    expect(view.host.querySelector('[role="status"]')?.textContent).not.toContain("archived");
    expect(client.getLatestSceneRunJob).toHaveBeenCalledTimes(2);
    expect(view.host.querySelector('[data-testid="scene-run-job-control"]')?.dataset.status).toBe("completed");
  });

  it("still shows a cancel network failure when an intervening latest poll remains on job A", async () => {
    const { mod, client } = await loadSceneRun();
    client.getLatestSceneRunJob.mockResolvedValue({
      job_id: "job-a",
      scene_id: "SC01",
      status: "running",
    });
    let rejectCancelA;
    client.cancelRunJob.mockImplementation(() => new Promise((resolve, reject) => {
      void resolve;
      rejectCancelA = reject;
    }));
    const view = await renderRunJobControl(mod.SceneRunJobControl, {
      sceneId: "SC01",
      pollIntervalMs: 20,
    });
    await vi.waitFor(() => expect(view.host.querySelector('[data-testid="scene-run-job-control"]')?.dataset.jobId).toBe("job-a"), T);

    await click(view.host.querySelector('[data-testid="scene-run-cancel-button"]'));
    await act(async () => { await new Promise(resolve => setTimeout(resolve, 25)); });
    expect(client.getLatestSceneRunJob.mock.calls.length).toBeGreaterThanOrEqual(2);
    await act(async () => {
      rejectCancelA(Object.assign(new Error("cancel A failed"), { code: "NETWORK_ERROR", retryable: true }));
    });

    expect(view.host.querySelector('[data-testid="scene-run-job-control"]')?.dataset.jobId).toBe("job-a");
    expect(view.host.querySelector('[role="alert"]')?.textContent).toContain("cancel A failed");
    expect(view.host.querySelector('[data-testid="scene-run-cancel-button"]')?.disabled).toBe(false);
    await view.unmount();
  });

  it("publishes only the POST-created job and never feeds job-specific poll results into observedJob", async () => {
    const { mod, client } = await loadSceneRun();
    const baseGet = client.apiGet.getMockImplementation();
    client.apiPost.mockImplementation((url) => {
      if (/\/api\/v1\/scenes\/s1\/run\/jobs$/.test(url)) {
        return Promise.resolve({ job_id: "job-created", scene_id: "s1", status: "running" });
      }
      return Promise.resolve({});
    });
    client.apiGet.mockImplementation((url) => {
      if (url === "/api/v1/run-jobs/job-created") {
        return Promise.resolve({ job_id: "job-created", scene_id: "s1", status: "completed" });
      }
      if (url === "/api/v1/scenes/s1/workbench") {
        return Promise.resolve({
          neutral_draft: { content: "潮水退去。\n她留下了证词。" },
          scene_run_state: { scene_status: "ready" },
        });
      }
      return baseGet(url);
    });
    const onJobCreated = vi.fn();
    vi.useFakeTimers();
    try {
      const runPromise = mod.scnRun(
        { sid: "ch01s1", kind: "主动场景" },
        "",
        "",
        { onJobCreated },
      );
      await vi.runAllTimersAsync();
      await runPromise;
    } finally {
      vi.useRealTimers();
    }

    expect(onJobCreated).toHaveBeenCalledTimes(1);
    expect(onJobCreated).toHaveBeenCalledWith(
      expect.objectContaining({ job_id: "job-created", status: "running" }),
      "s1",
    );
    expect(client.apiPost).toHaveBeenCalledWith(
      "/api/v1/scenes/s1/run/jobs",
      { run_policy: "strict" },
    );
  });

  it("keeps a neutral draft non-archivable when the durable job is blocked by lifecycle budget", async () => {
    const { mod, client } = await loadSceneRun();
    client.apiPost.mockImplementation((url) => {
      if (/\/api\/v1\/scenes\/s1\/run\/jobs$/.test(url)) {
        return Promise.resolve({ job_id: "job-budget", scene_id: "s1", status: "running" });
      }
      return Promise.resolve({});
    });
    const baseGet = client.apiGet.getMockImplementation();
    client.apiGet.mockImplementation((url) => {
      if (url === "/api/v1/run-jobs/job-budget") {
        return Promise.resolve({
          job_id: "job-budget",
          scene_id: "s1",
          status: "blocked",
          current_step: "blocked",
          error_code: "LLM_SCENE_TOKEN_BUDGET_EXHAUSTED",
          error_text: "scene token budget exhausted before dispatch",
        });
      }
      if (url === "/api/v1/scenes/s1/workbench") {
        return Promise.resolve({
          neutral_draft: { content: "潮水退去。\n她留下了证词。" },
          scene_run_state: {
            scene_status: "bundle_built",
            lifecycle_budget: {
              scene_token_budget: 34200,
              scene_tokens_used: 13615,
              scene_tokens_reserved: 0,
              scene_tokens_remaining: 20585,
              baseline_tokens: 6840,
              recommended_topup_tokens: 6840,
              attempt_budget: 4,
              total_attempt_count: 2,
              provider_attempt_budget: 32,
              provider_attempts_used: 5,
            },
          },
          author_state: { author_state: "draft_ready", can_archive: true },
        });
      }
      return baseGet(url);
    });

    vi.useFakeTimers();
    let result;
    try {
      const pending = mod.scnRun({ sid: "ch01s1", kind: "主动场景" }, "", "");
      await vi.runAllTimersAsync();
      result = await pending;
    } finally {
      vi.useRealTimers();
    }

    expect(result.budgetBlock).toMatchObject({
      code: "LLM_SCENE_TOKEN_BUDGET_EXHAUSTED",
      topup: { extra_tokens: 6840 },
    });
    expect(result.gate.canArchive).toBe(false);
    expect(result.draft.length).toBeGreaterThan(0);
  });

  it("hydrates a no-content budget checkpoint with its durable author instruction", async () => {
    const { mod, client } = await loadSceneRun({ projects: [NON_DEMO_PROJECT] });
    const authorNote = "保留第一人称视角，并延续上一版的人物动机。";
    const baseGet = client.apiGet.getMockImplementation();
    client.apiGet.mockImplementation((url, options) => {
      if (url === "/api/v1/scenes/s1/workbench") {
        return Promise.resolve({
          scene_run_state: {
            scene_status: "bundle_built",
            lifecycle_budget: {
              baseline_tokens: 7200,
              recommended_topup_tokens: 7200,
            },
          },
        });
      }
      return baseGet(url, options);
    });

    const restored = await mod.scnHydrateFromBackend("ch01s1", {
      terminalJob: {
        job_id: "job-no-content-budget",
        scene_id: "s1",
        status: "blocked",
        current_step: "style_running",
        error_code: "LLM_SCENE_TOKEN_BUDGET_EXHAUSTED",
        error_text: "scene token budget exhausted before dispatch",
        author_note: authorNote,
      },
    });

    expect(restored).toMatchObject({
      state: "queued",
      draft: [],
      authorNote,
      recoveredWithoutDraft: true,
      budgetBlock: {
        code: "LLM_SCENE_TOKEN_BUDGET_EXHAUSTED",
        currentStep: "style_running",
        topup: { extra_tokens: 7200 },
      },
      gate: { canArchive: false, blockReason: "lifecycle_budget" },
    });
    expect(restored.error).toContain("尚未产出正文");
  });

  /* 契约对位：GET /scenes/{id}/workbench 以 hard_qc_summary / soft_qc_summary 透出质检
     （api/routes/scenes.py `_serialize_qc_summary`，rewrite_brief 为字符串列表）。
     起草台的运行记录必须从这两个真实键名取回改写指令，否则「按硬问题重写并复检」
     只能退到 issue_key 拼接，作者看不到质检给出的具体改法。 */
  const HARD_BLOCKED_WORKBENCH = {
    neutral_draft: { content: "潮水退去。\n她留下了证词。" },
    scene_run_state: { scene_status: "hard_qc_rewrite_required" },
    author_state: {
      author_state: "hard_blocked",
      blocking_findings: [{ issue_key: "missing_required_text", quality_level: "Q1", verified_by: "scene_card_required_text" }],
      quality_warnings: [],
      recommended_actions: ["review_pipeline_gate"],
      can_archive: false,
    },
    hard_qc_summary: {
      qc_report_id: "qc_hard_s1_001",
      qc_type: "hard_qc",
      pass_flag: false,
      resolution_code: "rewrite_partial_requested",
      issue_keys: ["missing_required_text"],
      next_action: "rewrite_partial",
      rewrite_brief: ["补齐推门动作", "明确主动销毁通行证"],
      created_at: "2026-09-03T10:00:00",
    },
    soft_qc_summary: null,
  };

  it("scnRun：workbench 的 hard_qc_summary.rewrite_brief 落到作者可见的运行记录", async () => {
    const { mod, client } = await loadSceneRun();
    client.apiPost.mockImplementation((url) => {
      if (/\/api\/v1\/scenes\/s1\/run\/jobs$/.test(url)) {
        return Promise.resolve({ job_id: "job-hard-brief", scene_id: "s1", status: "running" });
      }
      return Promise.resolve({});
    });
    const baseGet = client.apiGet.getMockImplementation();
    client.apiGet.mockImplementation((url) => {
      if (url === "/api/v1/run-jobs/job-hard-brief") {
        // run-jobs 视图的 latest_qc 不带 rewrite_brief（scene_run_jobs._latest_qc_summary），
        // 改写指令只存在于 workbench 摘要里——这里故意给出无指令的 latest_qc 以证明取数来源。
        return Promise.resolve({
          job_id: "job-hard-brief",
          scene_id: "s1",
          status: "blocked",
          current_step: "hard_qc",
          latest_qc: { qc_type: "hard_qc", pass_flag: false, issue_keys: ["missing_required_text"] },
        });
      }
      if (url === "/api/v1/scenes/s1/workbench") return Promise.resolve(HARD_BLOCKED_WORKBENCH);
      return baseGet(url);
    });

    vi.useFakeTimers();
    let result;
    try {
      const pending = mod.scnRun({ sid: "ch01s1", kind: "主动场景" }, "", "");
      await vi.runAllTimersAsync();
      result = await pending;
    } finally {
      vi.useRealTimers();
    }

    expect(result.state).toBe("ready");
    expect(result.gate.authorState).toBe("hard_blocked");
    expect(result.gate.canArchive).toBe(false);
    expect(result.rewriteBrief).toBe("补齐推门动作；明确主动销毁通行证");
  });

  it("scnHydrateFromBackend：换浏览器恢复时同样取回 hard_qc_summary 的改写指令", async () => {
    const { mod, client } = await loadSceneRun();
    const baseGet = client.apiGet.getMockImplementation();
    client.apiGet.mockImplementation((url, options) => {
      if (url === "/api/v1/scenes/s1/workbench") return Promise.resolve(HARD_BLOCKED_WORKBENCH);
      return baseGet(url, options);
    });

    const restored = await mod.scnHydrateFromBackend("ch01s1", {});

    expect(restored).toBeTruthy();
    expect(restored.state).toBe("ready");
    expect(restored.draft.length).toBeGreaterThan(0);
    expect(restored.gate.canArchive).toBe(false);
    expect(restored.rewriteBrief).toBe("补齐推门动作；明确主动销毁通行证");
  });

  it("topups only the exhausted lifecycle dimension through the audited author route", async () => {
    const { mod, client } = await loadSceneRun();
    client.apiPost.mockResolvedValue({ scene_token_budget: 41040 });

    await mod.scnTopupBudget("ch01s1", {
      code: "LLM_SCENE_TOKEN_BUDGET_EXHAUSTED",
      topup: { extra_tokens: 6840 },
    });

    expect(client.apiPost).toHaveBeenCalledWith(
      "/api/v1/scenes/s1/budget/topup",
      {
        extra_tokens: 6840,
        reason: "作者在起草台确认追加生命周期预算并从持久化检查点继续",
      },
    );
  });

  it("asks the server to resume its own budget-blocked checkpoint instead of starting a fresh execution", async () => {
    const { mod, client } = await loadSceneRun();
    client.apiPost.mockImplementation((url) => {
      if (url === "/api/v1/scenes/s1/run/jobs") {
        return Promise.resolve({ job_id: "job-resume-budget", scene_id: "s1", status: "completed" });
      }
      return Promise.resolve({});
    });
    const baseGet = client.apiGet.getMockImplementation();
    client.apiGet.mockImplementation((url) => {
      if (url === "/api/v1/scenes/s1/workbench") {
        return Promise.resolve({
          style_draft: { content: "潮水退去。\n她留下了证词。" },
          scene_run_state: { scene_status: "near_final" },
        });
      }
      return baseGet(url);
    });

    await mod.scnRun(
      { sid: "ch01s1", kind: "主动场景" },
      "",
      "",
      { resumeBudget: true },
    );

    expect(client.apiPost).toHaveBeenCalledWith(
      "/api/v1/scenes/s1/run/jobs",
      { run_policy: "strict", resume_budget: true },
    );
  });

  it("preserves the complete author instruction when resuming a budget-blocked checkpoint", async () => {
    const { mod, client } = await loadSceneRun();
    const authorNote = "保留此前的叙事视角，不要重置人物动机。".repeat(30);
    client.apiPost.mockImplementation((url) => {
      if (url === "/api/v1/scenes/s1/run/jobs") {
        return Promise.resolve({ job_id: "job-resume-note", scene_id: "s1", status: "completed" });
      }
      return Promise.resolve({});
    });
    const baseGet = client.apiGet.getMockImplementation();
    client.apiGet.mockImplementation((url) => {
      if (url === "/api/v1/scenes/s1/workbench") {
        return Promise.resolve({
          style_draft: { content: "潮水退去。\n她留下了证词。" },
          scene_run_state: { scene_status: "near_final" },
        });
      }
      return baseGet(url);
    });

    await mod.scnRun(
      { sid: "ch01s1", kind: "主动场景" },
      authorNote,
      "",
      { resumeBudget: true },
    );

    expect(client.apiPost).toHaveBeenCalledWith(
      "/api/v1/scenes/s1/run/jobs",
      { run_policy: "strict", author_note: authorNote, resume_budget: true },
    );
  });

  it("rejects an overlong author instruction instead of silently truncating it", async () => {
    const { mod, client } = await loadSceneRun();

    await expect(mod.scnRun(
      { sid: "ch01s1", kind: "主动场景" },
      "改".repeat(2001),
      "",
    )).rejects.toMatchObject({ code: "AUTHOR_NOTE_TOO_LONG" });

    expect(client.apiPost.mock.calls.filter(([url]) => url === "/api/v1/scenes/s1/run/jobs")).toEqual([]);
  });

  it("projects a server-archived completed run as archived instead of a blocked ready state", async () => {
    const { mod, client } = await loadSceneRun();
    client.apiPost.mockImplementation((url) => {
      if (url === "/api/v1/scenes/s1/run/jobs") {
        return Promise.resolve({ job_id: "job-archived", scene_id: "s1", status: "completed" });
      }
      return Promise.resolve({});
    });
    const baseGet = client.apiGet.getMockImplementation();
    client.apiGet.mockImplementation((url) => {
      if (url === "/api/v1/scenes/s1/workbench") {
        return Promise.resolve({
          final_scene: { content: "潮水退去。\n她留下了证词。" },
          scene_run_state: { scene_status: "archived" },
          author_state: { author_state: "archived", can_archive: false },
        });
      }
      return baseGet(url);
    });

    const result = await mod.scnRun({ sid: "ch01s1", kind: "主动场景" }, "", "");

    expect(result.state).toBe("archived");
    expect(result.gate).toMatchObject({ authorState: "archived", canArchive: false });
  });

  it("aborts an in-flight job-specific GET instead of merely ignoring its response", async () => {
    const { mod, client } = await loadSceneRun();
    client.apiPost.mockImplementation((url) => {
      if (/\/api\/v1\/scenes\/s1\/run\/jobs$/.test(url)) {
        return Promise.resolve({ job_id: "job-pending-get", scene_id: "s1", status: "running" });
      }
      return Promise.resolve({});
    });
    let getSignal = null;
    const baseGet = client.apiGet.getMockImplementation();
    client.apiGet.mockImplementation((url, { signal } = {}) => {
      if (url !== "/api/v1/run-jobs/job-pending-get") return baseGet(url);
      getSignal = signal;
      return new Promise((resolve, reject) => {
        void resolve;
        signal.addEventListener("abort", () => reject(new DOMException("aborted", "AbortError")), { once: true });
      });
    });
    const controller = new AbortController();
    vi.useFakeTimers();
    try {
      const pending = mod.scnRun(
        { sid: "ch01s1", kind: "主动场景" },
        "",
        "",
        { signal: controller.signal },
      );
      await vi.advanceTimersByTimeAsync(2000);
      await vi.waitFor(() => expect(getSignal).toBe(controller.signal), T);
      controller.abort();
      await expect(pending).rejects.toMatchObject({ code: "SCENE_RUN_UI_ABORTED" });
      expect(getSignal.aborted).toBe(true);
    } finally {
      vi.useRealTimers();
    }
  });

  it.each(["cancelled", "completed", "failed", "blocked"])(
    "renders terminal %s without an executable cancel action",
    async (status) => {
      const { mod, client } = await loadSceneRun();
      client.getLatestSceneRunJob.mockResolvedValue({ job_id: `job-${status}`, scene_id: "SC01", status });
      const view = await renderRunJobControl(mod.SceneRunJobControl, { sceneId: "SC01" });

      await vi.waitFor(() => {
        expect(view.host.querySelector('[data-testid="scene-run-job-control"]')?.dataset.status).toBe(status);
      }, T);
      expect(view.host.querySelector('[data-testid="scene-run-cancel-button"]')).toBeNull();
    },
  );

  it("shows backend 409 reason/details and refreshes the authoritative terminal state", async () => {
    const { mod, client } = await loadSceneRun();
    client.getLatestSceneRunJob
      .mockResolvedValueOnce({ job_id: "job-race", scene_id: "SC01", status: "running" })
      .mockResolvedValue({ job_id: "job-race", scene_id: "SC01", status: "completed" });
    client.cancelRunJob.mockRejectedValue(Object.assign(new Error("terminal scene run job cannot be cancelled"), {
      status: 409,
      code: "RUN_JOB_CANCEL_CONFLICT",
      details: { job_id: "job-race", status: "completed" },
    }));
    const view = await renderRunJobControl(mod.SceneRunJobControl, { sceneId: "SC01" });
    await vi.waitFor(() => expect(view.host.querySelector('[data-testid="scene-run-cancel-button"]')).toBeTruthy(), T);

    await click(view.host.querySelector('[data-testid="scene-run-cancel-button"]'));

    await vi.waitFor(() => {
      // 错误码只留在 data-code 里排查用；给作者的是一句中文：任务已经是什么状态
      const alert = view.host.querySelector('[role="alert"]');
      expect(alert?.dataset.code).toBe("RUN_JOB_CANCEL_CONFLICT");
      expect(alert?.textContent).toContain("「已完成」");
      expect(alert?.textContent).not.toMatch(/RUN_JOB_CANCEL_CONFLICT|completed|job-race/);
      expect(view.host.querySelector('[data-testid="scene-run-job-control"]')?.dataset.status).toBe("completed");
    }, T);
  });

  it("re-enables retry after a network failure without issuing a duplicate in-flight cancel", async () => {
    const { mod, client } = await loadSceneRun();
    client.getLatestSceneRunJob.mockResolvedValue({ job_id: "job-network", scene_id: "SC01", status: "running" });
    client.cancelRunJob
      .mockRejectedValueOnce(Object.assign(new Error("network down"), { code: "NETWORK_ERROR", retryable: true }))
      .mockResolvedValueOnce({ job_id: "job-network", scene_id: "SC01", status: "cancel_requested" });
    const view = await renderRunJobControl(mod.SceneRunJobControl, { sceneId: "SC01" });
    await vi.waitFor(() => expect(view.host.querySelector('[data-testid="scene-run-cancel-button"]')).toBeTruthy(), T);

    await click(view.host.querySelector('[data-testid="scene-run-cancel-button"]'));
    await vi.waitFor(() => {
      expect(view.host.querySelector('[role="alert"]')?.textContent).toContain("network down");
      expect(view.host.querySelector('[data-testid="scene-run-cancel-button"]')?.disabled).toBe(false);
    }, T);
    await click(view.host.querySelector('[data-testid="scene-run-cancel-button"]'));

    expect(client.cancelRunJob).toHaveBeenCalledTimes(2);
    await vi.waitFor(() => expect(view.host.querySelector('[role="status"]')?.textContent).toContain("正在取消"), T);
  });

  it("does not let a stale scene response overwrite the newly selected scene", async () => {
    const { mod, client } = await loadSceneRun();
    let resolveA;
    client.getLatestSceneRunJob.mockImplementation((sceneId) => {
      if (sceneId === "SC-A") return new Promise((resolve) => { resolveA = resolve; });
      return Promise.resolve({ job_id: "job-b", scene_id: "SC-B", status: "cancelled" });
    });
    const view = await renderRunJobControl(mod.SceneRunJobControl, { sceneId: "SC-A" });

    await view.rerender({ sceneId: "SC-B" });
    await vi.waitFor(() => expect(view.host.querySelector('[role="status"]')?.textContent).toContain("已取消"), T);
    await act(async () => resolveA({ job_id: "job-a", scene_id: "SC-A", status: "running" }));

    expect(view.host.querySelector('[data-testid="scene-run-job-control"]')?.dataset.jobId).toBe("job-b");
    expect(view.host.querySelector('[role="status"]')?.textContent).toContain("已取消");
  });

  it("cleans its polling timer on unmount and reloads latest after a fresh mount", async () => {
    const { mod, client } = await loadSceneRun();
    client.getLatestSceneRunJob.mockResolvedValue({ job_id: "job-live", scene_id: "SC01", status: "cancel_requested" });
    const first = await renderRunJobControl(mod.SceneRunJobControl, { sceneId: "SC01", pollIntervalMs: 80 });
    await vi.waitFor(() => expect(client.getLatestSceneRunJob).toHaveBeenCalledTimes(1), T);
    await first.unmount();
    await new Promise((resolve) => setTimeout(resolve, 120));
    expect(client.getLatestSceneRunJob).toHaveBeenCalledTimes(1);

    await renderRunJobControl(mod.SceneRunJobControl, { sceneId: "SC01", pollIntervalMs: 80 });
    await vi.waitFor(() => expect(client.getLatestSceneRunJob).toHaveBeenCalledTimes(2), T);
  });

  it("aborts an in-flight latest request when the control unmounts", async () => {
    const { mod, client } = await loadSceneRun();
    let latestSignal = null;
    client.getLatestSceneRunJob.mockImplementation((sceneId, { signal }) => new Promise((resolve, reject) => {
      void sceneId;
      void resolve;
      latestSignal = signal;
      signal.addEventListener("abort", () => reject(new DOMException("aborted", "AbortError")), { once: true });
    }));
    const view = await renderRunJobControl(mod.SceneRunJobControl, { sceneId: "SC01" });
    await vi.waitFor(() => expect(latestSignal).toBeTruthy(), T);

    await view.unmount();

    expect(latestSignal.aborted).toBe(true);
  });

  it("is mounted by the real scene page instead of shipping as a dead component", () => {
    // 页面只负责摆放：控制条挂在 ws-scene.jsx 的管线行里，它报来的任务由 useSceneRuns 记成权威任务
    const page = readFileSync("src/ws-scene.jsx", "utf8");
    expect(page).toMatch(/<SceneRunJobControl[\s\S]*?onJobChange=\{run\.onJobChange\}/);
    const state = readFileSync("src/ws-scene-board-state.js", "utf8");
    expect(state).toContain("authoritativeRunJob");
    expect(state).toMatch(/const onJobChange = \(job\) =>/);
  });

  it("restores latest running state and cancel control in the real scene page", async () => {
    const { client } = await loadSceneRun();
    await queueSceneIntent({ sid: "ch01s1" });
    client.getLatestSceneRunJob.mockResolvedValue({
      job_id: "job-page-refresh",
      scene_id: "s1",
      status: "running",
      current_step: "neutral_running",
    });
    const page = await import("./ws-scene.jsx");

    const view = await renderRunJobControl(page.WsScene, { go: vi.fn(), t: {} });

    await vi.waitFor(() => {
      expect(client.getLatestSceneRunJob).toHaveBeenCalledWith(
        "s1",
        expect.objectContaining({ signal: expect.anything() }),
      );
      expect(view.host.querySelector('[data-testid="scene-run-job-control"]')?.dataset.jobId).toBe("job-page-refresh");
      expect(view.host.querySelector('[data-testid="scene-run-cancel-button"]')).toBeTruthy();
      expect(view.host.querySelector('[role="status"]')?.textContent).toContain("中性稿");
    }, T);
    expect(view.host.querySelector('[role="status"]')?.textContent).not.toContain("neutral_running");
  });

  it("keeps authoritative queued distinct from running and suppresses a duplicate start", async () => {
    const { client } = await loadSceneRun();
    await queueSceneIntent({ sid: "ch01s1" });
    client.getLatestSceneRunJob.mockResolvedValue({
      job_id: "job-page-queued",
      scene_id: "s1",
      status: "queued",
      current_step: "queued",
    });
    const page = await import("./ws-scene.jsx");
    const view = await renderRunJobControl(page.WsScene, { go: vi.fn(), t: {} });

    await vi.waitFor(() => {
      expect(view.host.querySelector(".scn2-state-tag")?.textContent).toContain("排队");
      expect(view.host.querySelector('[role="status"]')?.textContent).toContain("排队中");
      expect(view.host.querySelector('[data-testid="scene-run-cancel-button"]')).toBeTruthy();
    }, T);
    const executableStart = Array.from(view.host.querySelectorAll("button"))
      .find((button) => button.textContent.includes("开始起草") && !button.disabled);
    expect(executableStart).toBeUndefined();
  });

  it("selects the first backend-restored scene from an empty local queue and aligns stage, row, and counts", async () => {
    const { client } = await loadSceneRun({ projects: [NON_DEMO_PROJECT] });
    routeRunStates(client, () => Promise.resolve({
      items: [{ scene_id: "s1", scene_status: "neutral_running" }],
    }));
    client.getLatestSceneRunJob.mockResolvedValue({
      job_id: "job-empty-restore",
      scene_id: "s1",
      status: "running",
      current_step: "neutral_running",
    });
    const page = await import("./ws-scene.jsx");
    const view = await renderRunJobControl(page.WsScene, { go: vi.fn(), t: {} });

    await vi.waitFor(() => {
      expect(view.host.textContent).not.toContain("运行队列还是空的");
      expect(view.host.querySelector('[data-testid="scene-run-job-control"]')?.dataset.jobId).toBe("job-empty-restore");
      expect(view.host.querySelector(".scn2-state-tag")?.textContent).toContain("运行");
      expect(view.host.querySelector(".scn2-qrow.is-active .scn2-chip")?.textContent).toContain("运行");
      expect(view.host.querySelector('[role="status"]')?.textContent).toContain("运行中 · 中性稿");
    }, T);
    // 书脊头：后端恢复的这一场按后端说的算——「1 场正在运行」，在办 1、待复核 0
    //（过去是四块统计：一场没提交过的在办场会被数成「排队」）
    expect(view.host.querySelector(".scn2-spine-live")?.textContent).toContain("1 场正在运行");
    expect(view.host.querySelector('[data-testid="scene-spine-filter-active"] .seg-count')?.textContent).toBe("1");
    expect(view.host.querySelector('[data-testid="scene-spine-filter-review"] .seg-count')?.textContent).toBe("0");
  });

  it.each([
    { terminal: "completed", expectedRow: "待复核", expectReview: true },
    { terminal: "cancelled", expectedRow: "待起草", expectReview: false },
  ])("reconciles stale local running after A → B → A when latest is $terminal", async ({ terminal, expectedRow, expectReview }) => {
    const { mod, client } = await loadSceneRun({
      projects: [NON_DEMO_PROJECT],
      catalog: [TWO_SCENE_CHAP],
    });
    mod.scnRunSave("ch01s1", {
      state: "running",
      progress: 0.4,
      attempt: 1,
      draft: [],
      metrics: [],
      alignment: [],
      cost: [],
      log: [],
    });
    await queueSceneIntent({ sids: ["ch01s1", "ch01s2"] });
    const notFound = Object.assign(new Error("not found"), { status: 404, code: "RUN_JOB_NOT_FOUND" });
    let aReads = 0;
    client.getLatestSceneRunJob.mockImplementation((sceneId) => {
      if (sceneId === "s2") return Promise.reject(notFound);
      aReads += 1;
      return Promise.resolve({
        job_id: "job-scene-a",
        scene_id: "s1",
        status: aReads === 1 ? "running" : terminal,
    });
  });

    const baseGet = client.apiGet.getMockImplementation();
    client.apiGet.mockImplementation((url, options) => {
      if (url === "/api/v1/scenes/s1/workbench") {
        return Promise.resolve({
          neutral_draft: { content: "潮水退去。\n她留下了证词。" },
          scene_run_state: { scene_status: "ready" },
        });
      }
      return baseGet(url, options);
    });
    const page = await import("./ws-scene.jsx");
    const view = await renderRunJobControl(page.WsScene, { go: vi.fn(), t: {} });
    await vi.waitFor(() => expect(view.host.querySelector('[role="status"]')?.textContent).toContain("运行中"), T);

    const rowByTitle = (title) => Array.from(view.host.querySelectorAll(".scn2-qrow"))
      .find(row => row.textContent.includes(title));
    await click(rowByTitle("回潮"));
    await vi.waitFor(() => expect(client.getLatestSceneRunJob.mock.calls.some(([sceneId]) => sceneId === "s2")).toBe(true), T);
    await click(rowByTitle("交班"));

    await vi.waitFor(() => {
      expect(view.host.querySelector('[data-testid="scene-run-job-control"]')?.dataset.status).toBe(terminal);
    }, T);
    if (terminal === "completed") {
      await vi.waitFor(() => expect(client.apiGet.mock.calls.some(([url]) => url === "/api/v1/scenes/s1/workbench")).toBe(true), T);
    }
    await act(async () => { await Promise.resolve(); });
    expect(view.host.querySelector(".scn2-qrow.is-active .scn2-chip")?.textContent).toContain(expectedRow);
    expect(view.host.querySelector(".scn2-run")).toBeNull();
    if (expectReview) {
      expect(view.host.querySelector(".scn2-review")).toBeTruthy();
    }
  });

  it("restores a fresh-browser no-content budget block and resumes with the original author instruction", async () => {
    const { mod, client } = await loadSceneRun({ projects: [NON_DEMO_PROJECT] });
    const authorNote = "保留此前完整的作者指令，不要重置叙事视角。".repeat(12);
    await queueSceneIntent({ sid: "ch01s1" });
    client.getLatestSceneRunJob.mockResolvedValue({
      job_id: "job-fresh-budget",
      scene_id: "s1",
      status: "blocked",
      current_step: "style_running",
      error_code: "LLM_SCENE_TOKEN_BUDGET_EXHAUSTED",
      error_text: "scene token budget exhausted before dispatch",
      author_note: authorNote,
    });
    let resumed = false;
    const baseGet = client.apiGet.getMockImplementation();
    client.apiGet.mockImplementation((url, options) => {
      if (url === "/api/v1/scenes/s1/workbench") {
        if (resumed) {
          return Promise.resolve({
            style_draft: { content: "潮水退去。\n她留下了证词。" },
            scene_run_state: { scene_status: "near_final" },
          });
        }
        return Promise.resolve({
          scene_run_state: {
            scene_status: "bundle_built",
            lifecycle_budget: {
              baseline_tokens: 6800,
              recommended_topup_tokens: 6800,
            },
          },
        });
      }
      return baseGet(url, options);
    });
    const basePost = client.apiPost.getMockImplementation();
    client.apiPost.mockImplementation((url, body, options) => {
      if (url === "/api/v1/scenes/s1/budget/topup") return Promise.resolve({});
      if (url === "/api/v1/scenes/s1/run/jobs") {
        resumed = true;
        return Promise.resolve({
          job_id: "job-fresh-budget-resumed",
          scene_id: "s1",
          status: "completed",
        });
      }
      return basePost(url, body, options);
    });
    const page = await import("./ws-scene.jsx");
    const view = await renderRunJobControl(page.WsScene, { go: vi.fn(), t: {} });

    await vi.waitFor(() => {
      expect(view.host.textContent).toContain("尚未产出正文");
      expect(view.host.querySelector('[data-testid="scene-budget-topup"]')).toBeTruthy();
      expect(mod.scnRunLoad("ch01s1")).toMatchObject({
        state: "queued",
        authorNote,
        budgetBlock: { code: "LLM_SCENE_TOKEN_BUDGET_EXHAUSTED" },
      });
    }, T);

    await click(view.host.querySelector('[data-testid="scene-budget-topup"]'));
    await vi.waitFor(() => {
      expect(client.apiPost).toHaveBeenCalledWith(
        "/api/v1/scenes/s1/run/jobs",
        { run_policy: "strict", author_note: authorNote, resume_budget: true },
        expect.objectContaining({ signal: expect.anything() }),
      );
    }, T);
  });

  it("clears stale running immediately while completed workbench recovery is still pending", async () => {
    const { mod, client } = await loadSceneRun({ projects: [NON_DEMO_PROJECT] });
    mod.scnRunSave("ch01s1", {
      state: "running",
      progress: 0.6,
      attempt: 1,
      draft: [],
      metrics: [],
      alignment: [],
      cost: [],
      log: [],
    });
    await queueSceneIntent({ sid: "ch01s1" });
    client.getLatestSceneRunJob.mockResolvedValue({
      job_id: "job-completed-pending-workbench",
      scene_id: "s1",
      status: "completed",
    });
    let workbenchSignal = null;
    const baseGet = client.apiGet.getMockImplementation();
    client.apiGet.mockImplementation((url, { signal } = {}) => {
      if (url !== "/api/v1/scenes/s1/workbench" || !signal) return baseGet(url);
      workbenchSignal = signal;
      return new Promise((resolve, reject) => {
        void resolve;
        signal.addEventListener("abort", () => reject(new DOMException("aborted", "AbortError")), { once: true });
      });
    });
    const page = await import("./ws-scene.jsx");
    const view = await renderRunJobControl(page.WsScene, { go: vi.fn(), t: {} });

    await vi.waitFor(() => {
      expect(workbenchSignal).toBeTruthy();
      expect(view.host.querySelector('[data-testid="scene-run-job-control"]')?.dataset.status).toBe("completed");
      expect(view.host.querySelector(".scn2-qrow.is-active .scn2-chip")?.textContent).toContain("待起草");
      expect(view.host.querySelector(".scn2-run")).toBeNull();
      expect(view.host.textContent).toContain("正在恢复产出");
    }, T);

    await view.unmount();
    expect(workbenchSignal.aborted).toBe(true);
  });

  it.each([
    { cachedState: "ready", expectedLabel: "待复核", rejectWorkbench: true },
    // 书脊上写完归档的场和目录里写完的场同一个词（ws-labels 的 SCENE_STATE_META.done）
    { cachedState: "archived", expectedLabel: "已完成", rejectWorkbench: false },
  ])("preserves a usable cached $cachedState result when completed workbench recovery has no data", async ({ cachedState, expectedLabel, rejectWorkbench }) => {
    const { mod, client } = await loadSceneRun({ projects: [NON_DEMO_PROJECT] });
    const cached = {
      ...mod.scnQC([{ id: "p1", text: "潮水退去，她留下了证词。" }]),
      state: cachedState,
      progress: 1,
      attempt: 1,
      attempts: [],
      cost: [],
      log: [],
    };
    mod.scnRunSave("ch01s1", cached);
    await queueSceneIntent({ sid: "ch01s1" });
    client.getLatestSceneRunJob.mockResolvedValue({
      job_id: `job-cached-${cachedState}`,
      scene_id: "s1",
      status: "completed",
    });
    const baseGet = client.apiGet.getMockImplementation();
    client.apiGet.mockImplementation((url, options) => {
      if (url !== "/api/v1/scenes/s1/workbench") return baseGet(url, options);
      return rejectWorkbench ? Promise.reject(new Error("temporary network")) : Promise.resolve({});
    });
    const page = await import("./ws-scene.jsx");
    const view = await renderRunJobControl(page.WsScene, { go: vi.fn(), t: {} });

    await vi.waitFor(() => {
      expect(client.apiGet.mock.calls.some(([url]) => url === "/api/v1/scenes/s1/workbench")).toBe(true);
      expect(view.host.querySelector('[data-testid="scene-run-job-control"]')?.dataset.status).toBe("completed");
      expect(view.host.querySelector(".scn2-qrow.is-active .scn2-chip")?.textContent).toContain(expectedLabel);
      expect(view.host.querySelector(".scn2-run")).toBeNull();
    }, T);
    expect(mod.scnRunLoad("ch01s1")).toMatchObject({ state: cachedState });
    expect(mod.scnRunLoad("ch01s1").draft.length).toBeGreaterThan(0);
  });

  it("真实起草台发现作者正文时打开差异决策，默认焦点落在“保存为候选”且不归档", async () => {
    const { mod, client } = await loadSceneRun({ projects: [NON_DEMO_PROJECT] });
    const cached = {
      ...mod.scnQC([{ id: "p1", text: "AI 写下了另一种开场。" }]),
      state: "ready", progress: 1, attempt: 1, attempts: [], cost: [], log: [],
    };
    mod.scnRunSave("ch01s1", cached);
    await queueSceneIntent({ sid: "ch01s1" });
    window.localStorage.setItem(window.wsKey("wr-doc:ch01s1"), "<p>作者亲写的开场。</p>");
    client.getLatestSceneRunJob.mockRejectedValue(Object.assign(new Error("not found"), { status: 404, code: "RUN_JOB_NOT_FOUND" }));
    const page = await import("./ws-scene.jsx");
    const view = await renderRunJobControl(page.WsScene, { go: vi.fn(), t: {} });

    await vi.waitFor(() => expect(view.host.querySelector('[data-testid="scene-archive"]')).toBeTruthy(), T);
    await click(view.host.querySelector('[data-testid="scene-archive"]'));
    const dialog = document.body.querySelector(".scn2-adopt");
    expect(dialog).toBeTruthy();
    expect(dialog.getAttribute("role")).toBe("dialog");
    // 头部说的是哪一场（章 · 场 · 题名），不是后端 id
    expect(dialog.textContent).toContain("第 1 章 · 第 1 场「交班」");
    expect(dialog.textContent).not.toContain("ch01s1");
    expect(dialog.textContent).toContain("作者当前稿");
    expect(dialog.textContent).toContain("AI 候选稿");
    const safe = dialog.querySelector('[data-testid="scene-save-candidate"]');
    const overwrite = dialog.querySelector('[data-testid="scene-confirm-overwrite"]');
    await vi.waitFor(() => expect(document.activeElement).toBe(safe), T);
    expect(overwrite.disabled).toBe(true);

    await click(safe);
    await vi.waitFor(() => expect(document.body.querySelector(".scn2-adopt")).toBeNull(), T);
    expect(client.apiPost.mock.calls.filter(([url]) => /adopt-current/.test(url))).toEqual([]);
    expect(window.localStorage.getItem(window.wsKey("wr-doc:ch01s1"))).toBe("<p>作者亲写的开场。</p>");
    expect(window.WrRecovery.list()).toEqual([expect.objectContaining({ type: "candidate", source: "ai" })]);
    expect(view.host.textContent).toContain("作者正文没有被改动");
  });

  it("真实起草台收到内容安全 409 后逐项确认，并只携 exact code 重试归档", async () => {
    const { mod, client } = await loadSceneRun({ projects: [NON_DEMO_PROJECT] });
    const cached = {
      ...mod.scnQC([{ id: "p1", text: "角色只有16岁，段落明确描写两人的性行为。" }]),
      state: "ready", progress: 1, attempt: 1, attempts: [], cost: [], log: [],
    };
    mod.scnRunSave("ch01s1", cached);
    await queueSceneIntent({ sid: "ch01s1" });
    client.getLatestSceneRunJob.mockRejectedValue(
      Object.assign(new Error("not found"), { status: 404, code: "RUN_JOB_NOT_FOUND" }),
    );
    const reviewError = Object.assign(new Error("review required"), {
      code: "CONTENT_SAFETY_REVIEW_REQUIRED",
      status: 409,
      details: {
        final_text_gate: {
          content_safety: {
            findings: [{
              code: "sexual_content_with_minor_indicators",
              review_required: true,
              acknowledged: false,
              severity: "high",
              confidence: "heuristic",
              message: "核对人物年龄与叙事目的。",
              evidence_terms: ["age:16", "性行为"],
            }],
            limitations: ["启发式不能判断完整语境。"],
          },
        },
      },
    });
    const basePost = client.apiPost.getMockImplementation();
    const adoptBodies = [];
    client.apiPost.mockImplementation((url, body, options) => {
      if (!/\/api\/v1\/scenes\/s1\/adopt-current$/.test(url)) {
        return basePost(url, body, options);
      }
      adoptBodies.push(body);
      return body.accepted_warning_codes.length
        ? Promise.resolve({
            scene_status: "archived",
            final_scene_row_id: "final_s1_v1",
            draft_id: body.exact_author_draft.draft_id,
            draft_revision_no: 2,
            author_draft: {
              ...body.exact_author_draft,
              revision_no: 2,
              last_promoted_revision_no: 2,
              last_promoted_final_scene_row_id: "final_s1_v1",
              canonical_dirty: false,
            },
          })
        : Promise.reject(reviewError);
    });
    const page = await import("./ws-scene.jsx");
    const view = await renderRunJobControl(page.WsScene, { go: vi.fn(), t: {} });

    await vi.waitFor(() => expect(view.host.querySelector('[data-testid="scene-archive"]')).toBeTruthy(), T);
    await click(view.host.querySelector('[data-testid="scene-archive"]'));
    await vi.waitFor(() => expect(document.body.querySelector(".wr-safety-dialog")).toBeTruthy(), T);
    const dialog = document.body.querySelector(".wr-safety-dialog");
    // 风险代码不再印给作者，挂在条目的 data-code 上（确认时原样回传）
    expect(dialog.querySelector('[data-code="sexual_content_with_minor_indicators"]')).toBeTruthy();
    const confirm = dialog.querySelector('[data-testid="content-safety-confirm"]');
    expect(confirm.disabled).toBe(true);

    await click(dialog.querySelector('input[type="checkbox"]'));
    expect(confirm.disabled).toBe(false);
    await click(confirm);

    await vi.waitFor(() => expect(document.body.querySelector(".wr-safety-dialog")).toBeNull(), T);
    expect(adoptBodies).toEqual([
      expect.objectContaining({
        accepted_warning_codes: [],
        exact_author_draft: expect.objectContaining({ draft_id: "author_draft_scene_s1", base_revision_no: 1 }),
      }),
      expect.objectContaining({
        accepted_warning_codes: ["sexual_content_with_minor_indicators"],
        exact_author_draft: expect.objectContaining({ draft_id: "author_draft_scene_s1", base_revision_no: 1 }),
      }),
    ]);
    expect(view.host.textContent).toContain("已归档并写入正文文档");
  });

  it("持久化运行记录保留预算续跑所需的原作者指令", async () => {
    const { mod } = await loadSceneRun({ projects: [NON_DEMO_PROJECT] });
    const authorNote = "保留原视角与人物动机。";

    mod.scnRunSave("ch01s1", {
      state: "queued",
      draft: [],
      metrics: [],
      alignment: [],
      log: [],
      attempts: [],
      budgetBlock: { code: "LLM_SCENE_TOKEN_BUDGET_EXHAUSTED" },
      authorNote,
    });

    expect(mod.scnRunLoad("ch01s1")).toMatchObject({ authorNote });
  });

  it("退回重写在界面上明确提示 2000 字上限，超限时不提交也不截断", async () => {
    const { mod, client } = await loadSceneRun({ projects: [NON_DEMO_PROJECT] });
    const cached = {
      ...mod.scnQC([{ id: "p1", text: "潮水退去，她留下了证词。" }]),
      state: "ready", progress: 1, attempt: 1, attempts: [], cost: [], log: [],
    };
    mod.scnRunSave("ch01s1", cached);
    await queueSceneIntent({ sid: "ch01s1" });
    client.getLatestSceneRunJob.mockRejectedValue(
      Object.assign(new Error("not found"), { status: 404, code: "RUN_JOB_NOT_FOUND" }),
    );
    const page = await import("./ws-scene.jsx");
    const view = await renderRunJobControl(page.WsScene, { go: vi.fn(), t: {} });
    await vi.waitFor(() => expect(view.host.querySelector('[data-testid="scene-archive"]')).toBeTruthy(), T);
    const rework = Array.from(view.host.querySelectorAll("button"))
      .find(button => button.textContent.includes("退回重写"));
    await click(rework);
    const textarea = view.host.querySelector(".scn2-rework-input");
    const overlong = "改".repeat(2001);
    await act(async () => {
      const setter = Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, "value").set;
      setter.call(textarea, overlong);
      textarea.dispatchEvent(new Event("input", { bubbles: true }));
    });

    expect(textarea.value).toBe(overlong);
    expect(textarea.getAttribute("aria-invalid")).toBe("true");
    expect(view.host.querySelector('[role="alert"]')?.textContent).toContain("不会静默截断");
    const submit = Array.from(view.host.querySelectorAll("button"))
      .find(button => button.textContent.includes("确认退回重写"));
    expect(submit.disabled).toBe(true);
    expect(client.apiPost.mock.calls.filter(([url]) => /\/run\/jobs$/.test(url))).toEqual([]);
  });

  /* 2026-09-20 真实故障：预检拦下的任务，作者只看到一句「任务已阻断…请检查阻断原因后重试」——原因没说，
     也没有地方可查。终态任务恢复（effect）与 startRun 的 catch 现在说同一句：任务自己留下的原因。 */
  // 裁决条那一句（页面上还有一条任务控制条，同样是 .scn2-decide）
  const decideSummary = (host) => host.querySelector('[data-testid="scene-start"]')
    ?.closest(".scn2-decide")?.querySelector(".scn2-decide-sum")?.textContent || "";
  it("预检阻断且没有草稿：起草台说任务留下的原因，而不是一句笼统的「请检查阻断原因」", async () => {
    const { client } = await loadSceneRun({ projects: [NON_DEMO_PROJECT] });
    await queueSceneIntent({ sid: "ch01s1" });
    client.getLatestSceneRunJob.mockResolvedValue({
      job_id: "job-preflight-blocked",
      scene_id: "s1",
      status: "blocked",
      current_step: "preflight_blocked",
      error_code: "SCENE_EXECUTION_CONTRACT_BLOCKED",
      error_text: "Fill the missing execution contract fields before drafting: scene_turn",
      missing_fields: ["scene_turn"],
    });
    const baseGet = client.apiGet.getMockImplementation();
    client.apiGet.mockImplementation((url, options) => {
      if (url === "/api/v1/scenes/s1/workbench") return Promise.resolve({ scene_run_state: { scene_status: "ready" } });
      return baseGet(url, options);
    });
    const page = await import("./ws-scene.jsx");
    const view = await renderRunJobControl(page.WsScene, { go: vi.fn(), t: {} });

    await vi.waitFor(() => expect(decideSummary(view.host)).toContain("执行契约还缺关键字段"), T);
    const summary = decideSummary(view.host);
    expect(summary).toContain("scene_turn");
    expect(summary).not.toContain("请检查阻断原因");
    expect(view.host.querySelector('[data-testid="scene-start"]')).toBeTruthy();
  });

  it("点了开始起草被预检拦下：catch 与终态恢复两条路落在同一句原因上", async () => {
    const { client } = await loadSceneRun({ projects: [NON_DEMO_PROJECT] });
    await queueSceneIntent({ sid: "ch01s1" });
    const blocked = {
      job_id: "job-click-blocked",
      scene_id: "s1",
      status: "blocked",
      current_step: "preflight_blocked",
      error_code: "SCENE_EXECUTION_CONTRACT_BLOCKED",
      error_text: "Fill the missing execution contract fields before drafting: scene_turn",
      missing_fields: ["scene_turn"],
    };
    let created = false;
    client.getLatestSceneRunJob.mockImplementation(() => (created
      ? Promise.resolve(blocked)
      : Promise.reject(Object.assign(new Error("not found"), { status: 404, code: "RUN_JOB_NOT_FOUND" }))));
    const baseGet = client.apiGet.getMockImplementation();
    client.apiGet.mockImplementation((url, options) => {
      if (url === "/api/v1/scenes/s1/workbench") return Promise.resolve({ scene_run_state: { scene_status: "ready" } });
      if (url === "/api/v1/run-jobs/job-click-blocked") return Promise.resolve(blocked);
      return baseGet(url, options);
    });
    const basePost = client.apiPost.getMockImplementation();
    client.apiPost.mockImplementation((url, body, options) => {
      if (url === "/api/v1/scenes/s1/run/jobs") { created = true; return Promise.resolve(blocked); }
      return basePost(url, body, options);
    });
    const page = await import("./ws-scene.jsx");
    const view = await renderRunJobControl(page.WsScene, { go: vi.fn(), t: {} });
    await vi.waitFor(() => expect(view.host.querySelector('[data-testid="scene-start"]')).toBeTruthy(), T);

    await click(view.host.querySelector('[data-testid="scene-start"]'));

    await vi.waitFor(() => expect(decideSummary(view.host)).toContain("执行契约还缺关键字段"), T);
    // 两条路都写完之后仍然是这一句（过去后写的那条会把它盖成「请检查阻断原因后重试」）
    await act(async () => { await new Promise(resolve => setTimeout(resolve, 60)); });
    const summary = decideSummary(view.host);
    expect(summary).toContain("scene_turn");
    expect(summary).not.toContain("请检查阻断原因");
    expect(summary).not.toContain("正在恢复");
  });

  it("取消之前留下的「声线 / 关系卡」阻断任务：如实说检查已取消，出口是重新起草，不再有补卡按钮", async () => {
    const { mod, client } = await loadSceneRun({ projects: [NON_DEMO_PROJECT] });
    expect(mod.scnCreateCards).toBeUndefined();
    await queueSceneIntent({ sid: "ch01s1" });
    client.getLatestSceneRunJob.mockResolvedValue({
      job_id: "job-legacy-voice",
      scene_id: "s1",
      status: "blocked",
      current_step: "preflight_blocked",
      error_code: "VOICE_PROFILE_MISSING",
      error_text: "请先补齐当前 POV 角色的可用声线档案，再执行完整场景运行。",
    });
    const baseGet = client.apiGet.getMockImplementation();
    client.apiGet.mockImplementation((url, options) => {
      if (url === "/api/v1/scenes/s1/workbench") return Promise.resolve({ scene_run_state: { scene_status: "ready" } });
      return baseGet(url, options);
    });
    const page = await import("./ws-scene.jsx");
    const view = await renderRunJobControl(page.WsScene, { go: vi.fn(), t: {} });

    await vi.waitFor(() => expect(decideSummary(view.host)).toContain("这项检查已经取消"), T);
    expect(view.host.querySelector('[data-testid="scene-create-cards"]')).toBeNull();
    expect(view.host.querySelector('[data-testid="scene-start"]')).toBeTruthy();
    expect(client.apiPost.mock.calls.some(([url]) => /preflight\/create-cards/.test(url))).toBe(false);
  });

  it("追加预算等待期间切换场景，续跑仍绑定原场景和原作者指令", async () => {
    const { mod, client } = await loadSceneRun({ projects: [NON_DEMO_PROJECT], catalog: [TWO_SCENE_CHAP] });
    const authorNote = "保留第一场的限知视角";
    mod.scnRunSave("ch01s1", {
      state: "queued", progress: 0, draft: [], metrics: [], alignment: [], cost: [], log: [], attempts: [],
      authorNote,
      budgetBlock: {
        code: "LLM_SCENE_TOKEN_BUDGET_EXHAUSTED",
        label: "本场 token 生命周期预算已到派发边界",
        topup: { extra_tokens: 6400 },
      },
    });
    await queueSceneIntent({ sids: ["ch01s1", "ch01s2"] });
    client.getLatestSceneRunJob.mockRejectedValue(
      Object.assign(new Error("not found"), { status: 404, code: "RUN_JOB_NOT_FOUND" }),
    );
    const topup = deferred();
    const basePost = client.apiPost.getMockImplementation();
    client.apiPost.mockImplementation((url, body, options = {}) => {
      if (url === "/api/v1/scenes/s1/budget/topup") return topup.promise;
      if (/\/api\/v1\/scenes\/s1\/run\/jobs$/.test(url)) {
        return new Promise((resolve, reject) => {
          void resolve;
          options.signal?.addEventListener("abort", () => reject(new DOMException("aborted", "AbortError")), { once: true });
        });
      }
      return basePost(url, body, options);
    });
    const page = await import("./ws-scene.jsx");
    const view = await renderRunJobControl(page.WsScene, { go: vi.fn(), t: {} });
    await vi.waitFor(() => expect(view.host.querySelector('[data-testid="scene-budget-topup"]')).toBeTruthy(), T);

    await click(view.host.querySelector('[data-testid="scene-budget-topup"]'));
    const rowB = Array.from(view.host.querySelectorAll(".scn2-qrow")).find(row => row.textContent.includes("回潮"));
    await click(rowB);
    await act(async () => { topup.resolve({}); await Promise.resolve(); });

    await vi.waitFor(() => expect(client.apiPost).toHaveBeenCalledWith(
      "/api/v1/scenes/s1/run/jobs",
      { run_policy: "strict", author_note: authorNote, resume_budget: true },
      expect.objectContaining({ signal: expect.anything() }),
    ), T);
    expect(client.apiPost.mock.calls.some(([url]) => url === "/api/v1/scenes/s2/run/jobs")).toBe(false);
  });

  it("归档预检同步防双击，切换场景后不弹出旧场景的作者稿决策", async () => {
    const { mod, client } = await loadSceneRun({ projects: [NON_DEMO_PROJECT], catalog: [TWO_SCENE_CHAP] });
    const ready = (text) => ({
      ...mod.scnQC([{ id: "p1", text }]),
      state: "ready", progress: 1, attempt: 1, attempts: [], cost: [], log: [],
    });
    mod.scnRunSave("ch01s1", ready("第一场 AI 稿。"));
    mod.scnRunSave("ch01s2", ready("第二场 AI 稿。"));
    await queueSceneIntent({ sids: ["ch01s1", "ch01s2"] });
    window.localStorage.setItem(window.wsKey("wr-doc:ch01s1"), "<p>第一场作者稿。</p>");
    client.getLatestSceneRunJob.mockRejectedValue(
      Object.assign(new Error("not found"), { status: 404, code: "RUN_JOB_NOT_FOUND" }),
    );
    const hydration = deferred();
    const hydrateSpy = vi.spyOn(window.WrDocs, "hydrate").mockImplementation((sid) => (
      sid === "ch01s1" ? hydration.promise : Promise.resolve(null)
    ));
    const page = await import("./ws-scene.jsx");
    const view = await renderRunJobControl(page.WsScene, { go: vi.fn(), t: {} });
    await vi.waitFor(() => expect(view.host.querySelector('[data-testid="scene-archive"]')).toBeTruthy(), T);

    await click(view.host.querySelector('[data-testid="scene-archive"]'));
    await vi.waitFor(() => {
      const button = view.host.querySelector('[data-testid="scene-archive"]');
      expect(button.disabled).toBe(true);
      expect(button.textContent).toContain("正在核对作者稿");
    }, T);
    await click(view.host.querySelector('[data-testid="scene-archive"]'));
    expect(hydrateSpy.mock.calls.filter(([sid]) => sid === "ch01s1")).toHaveLength(1);

    const rowB = Array.from(view.host.querySelectorAll(".scn2-qrow")).find(row => row.textContent.includes("回潮"));
    await click(rowB);
    await act(async () => { hydration.resolve(null); await Promise.resolve(); });
    await act(async () => { await Promise.resolve(); });

    expect(document.body.querySelector(".scn2-adopt")).toBeNull();
    expect(view.host.querySelector(".scn2-qrow.is-active")?.textContent).toContain("回潮");
    expect(client.apiPost.mock.calls.filter(([url]) => /adopt-current/.test(url))).toEqual([]);
  });


  it("尝试历史只把复盘意见加入当前稿重写，不宣称恢复历史正文 base", async () => {
    const { mod, client } = await loadSceneRun({ projects: [NON_DEMO_PROJECT] });
    mod.scnRunSave("ch01s1", {
      ...mod.scnQC([{ id: "p1", text: "当前复核稿。" }]),
      state: "ready", progress: 1, attempt: 2, cost: [], log: [],
      attempts: [
        { n: 2, time: "本次 · 待裁决", result: "待裁决", tone: "gold", note: "当前稿" },
        { n: 1, time: "昨日", result: "质检阻断", tone: "rose", note: "视角泄漏", cmp: { verdict: "第 1 次尝试存在视角泄漏。" } },
      ],
    });
    await queueSceneIntent({ sid: "ch01s1" });
    client.getLatestSceneRunJob.mockRejectedValue(
      Object.assign(new Error("not found"), { status: 404, code: "RUN_JOB_NOT_FOUND" }),
    );
    const basePost = client.apiPost.getMockImplementation();
    client.apiPost.mockImplementation((url, body, options = {}) => {
      if (/\/api\/v1\/scenes\/s1\/run\/jobs$/.test(url)) {
        return new Promise((resolve, reject) => {
          void resolve;
          options.signal?.addEventListener("abort", () => reject(new DOMException("aborted", "AbortError")), { once: true });
        });
      }
      return basePost(url, body, options);
    });
    const page = await import("./ws-scene.jsx");
    const view = await renderRunJobControl(page.WsScene, { go: vi.fn(), t: {} });
    await vi.waitFor(() => expect(view.host.querySelector(".scn2-try-view")).toBeTruthy(), T);

    await click(view.host.querySelector(".scn2-try-view"));
    expect(view.host.querySelector(".scn2-cmp")?.textContent).toContain("不包含可恢复的历史正文快照");
    expect(view.host.querySelector('[data-testid="scene-attempt-rewrite"]')?.textContent).toContain("参考该版复盘意见重写");
    await click(view.host.querySelector('[data-testid="scene-attempt-rewrite"]'));

    await vi.waitFor(() => expect(client.apiPost.mock.calls.some(([url]) => url === "/api/v1/scenes/s1/run/jobs")).toBe(true), T);
    const [, body] = client.apiPost.mock.calls.find(([url]) => url === "/api/v1/scenes/s1/run/jobs");
    expect(body.author_note).toContain("参考第 1 次尝试的复盘意见重写");
    expect(body.author_note).toContain("不恢复该版正文，以当前稿为输入");
    expect(body.author_note).not.toContain("为基础重写");
    expect(body).not.toHaveProperty("previous_draft");
  });

  it("stops the real page progress timer and scnRun polling after unmount", async () => {
    const { client } = await loadSceneRun();
    await queueSceneIntent({ sid: "ch01s1" });
    client.getLatestSceneRunJob.mockRejectedValue(
      Object.assign(new Error("not found"), { status: 404, code: "RUN_JOB_NOT_FOUND" }),
    );
    client.apiPost.mockImplementation((url) => {
      if (/\/api\/v1\/scenes\/s1\/run\/jobs$/.test(url)) {
        return Promise.resolve({ job_id: "job-unmount", scene_id: "s1", status: "running" });
      }
      return Promise.resolve({});
    });
    const page = await import("./ws-scene.jsx");
    const view = await renderRunJobControl(page.WsScene, { go: vi.fn(), t: {} });
    await vi.waitFor(() => {
      const start = Array.from(view.host.querySelectorAll("button"))
        .find((button) => button.textContent.includes("开始起草"));
      expect(start).toBeTruthy();
    }, T);
    const start = Array.from(view.host.querySelectorAll("button"))
      .find((button) => button.textContent.includes("开始起草"));
    await click(start);
    await vi.waitFor(() => {
      expect(client.apiPost).toHaveBeenCalledWith(
        "/api/v1/scenes/s1/run/jobs",
        { run_policy: "strict" },
        expect.objectContaining({ signal: expect.anything() }),
      );
    }, T);

    await view.unmount();
    client.apiGet.mockClear();
    await new Promise((resolve) => setTimeout(resolve, 2100));

    const stalePolls = client.apiGet.mock.calls.filter(([url]) => url === "/api/v1/run-jobs/job-unmount");
    expect(stalePolls).toEqual([]);
  });

  it("aborts a pending create-job POST when the real page unmounts", async () => {
    const { client } = await loadSceneRun();
    await queueSceneIntent({ sid: "ch01s1" });
    client.getLatestSceneRunJob.mockRejectedValue(
      Object.assign(new Error("not found"), { status: 404, code: "RUN_JOB_NOT_FOUND" }),
    );
    let postSignal = null;
    client.apiPost.mockImplementation((url, body, { signal } = {}) => {
      if (!/\/api\/v1\/scenes\/s1\/run\/jobs$/.test(url)) return Promise.resolve({});
      void body;
      postSignal = signal;
      return new Promise((resolve, reject) => {
        void resolve;
        signal.addEventListener("abort", () => reject(new DOMException("aborted", "AbortError")), { once: true });
      });
    });
    const page = await import("./ws-scene.jsx");
    const view = await renderRunJobControl(page.WsScene, { go: vi.fn(), t: {} });
    await vi.waitFor(() => expect(Array.from(view.host.querySelectorAll("button"))
      .some(button => button.textContent.includes("开始起草"))).toBe(true), T);
    const start = Array.from(view.host.querySelectorAll("button"))
      .find(button => button.textContent.includes("开始起草"));
    await click(start);
    await vi.waitFor(() => expect(postSignal).toBeTruthy(), T);

    await view.unmount();

    expect(postSignal.aborted).toBe(true);
  });
});

describe("scnBackendQueueSids（队列成员的后端派生）", () => {
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

    const sids = await mod.scnBackendQueueSids();

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

    const sids = await mod.scnBackendQueueSids();

    expect(sids).toEqual(["ch01s1"]);
  });

  it("run-states 端点失败时返回空列表（本地队列照常可用，不炸）", async () => {
    const { mod, client } = await loadSceneRun();
    routeRunStates(client, () => Promise.reject(new Error("boom")));

    const sids = await mod.scnBackendQueueSids();

    expect(sids).toEqual([]);
  });
});

/* ==========================================================
   采纳归档必须把浏览器当前正文、作者稿修订和 FinalScene 绑定为一次事务。
   · 成功路径 = POST exact_author_draft 保存+归档成功 → 吸收服务端修订并置 done
   · 后端拒绝（冲突/来源安全）→ 不置 done、不把待采用稿写成当前正文
   ========================================================== */
describe("scnAdoptToDoc（精确作者稿修订的原子归档）", () => {
  beforeEach(() => {
    vi.resetModules();
    window.localStorage.clear();
    vi.spyOn(window, "confirm").mockReturnValue(true);
  });
  afterEach(() => vi.restoreAllMocks());

  const DRAFT = [{ id: "p1", parts: [{ text: "潮水退去，她看清了闸门上的名字。" }] }];

  async function loadWithCatalog(opts) {
    const { mod, client } = await loadSceneRun(opts);
    const cat = await import("./ws-catalog.jsx");
    await vi.waitFor(() => expect(cat.WsCatalog.get().length).toBeGreaterThan(0), T);
    let revision = 1;
    let content = "";
    const basePost = client.apiPost.getMockImplementation();
    const basePatch = client.apiPatch.getMockImplementation();
    client.apiPost.mockImplementation((url, body, options) => {
      if (/\/api\/v1\/author-drafts\/scene\/s1\/ensure$/.test(url)) {
        return Promise.resolve({
          draft: {
            draft_id: "author_draft_scene_s1",
            revision_no: revision,
            content,
            last_promoted_revision_no: null,
            last_promoted_final_scene_row_id: null,
            canonical_dirty: true,
          },
          runtime_final_ref: null,
        });
      }
      return basePost(url, body, options);
    });
    client.apiPatch.mockImplementation((url, body, options) => {
      if (/\/api\/v1\/author-drafts\/author_draft_scene_s1$/.test(url)) {
        revision += 1;
        content = body.content;
        return Promise.resolve({ draft: { draft_id: "author_draft_scene_s1", revision_no: revision, content } });
      }
      return basePatch(url, body, options);
    });
    return { mod, client, cat };
  }

  it("成功：把确切正文和 base revision 原子提交，回包后才置 done + 同步缓存", async () => {
    const { mod, client, cat } = await loadWithCatalog();
    const basePost = client.apiPost.getMockImplementation();
    client.apiPost.mockImplementation((url, body, options) => {
      if (/\/api\/v1\/scenes\/s1\/adopt-current$/.test(url)) {
        return Promise.resolve({
          scene_id: "s1",
          scene_status: "archived",
          final_scene_row_id: "final_s1_v1",
          draft_id: body.exact_author_draft.draft_id,
          draft_revision_no: 2,
          content_hash: "hash-exact",
          author_draft: {
            draft_id: body.exact_author_draft.draft_id,
            revision_no: 2,
            content: body.exact_author_draft.content,
            last_promoted_revision_no: 2,
            last_promoted_final_scene_row_id: "final_s1_v1",
            canonical_dirty: false,
          },
        });
      }
      return basePost(url, body, options);
    });

    const result = await mod.scnAdoptToDoc("ch01s1", DRAFT);

    expect(result.ok).toBe(true);
    expect(client.apiPost).toHaveBeenCalledWith("/api/v1/scenes/s1/adopt-current", {
      accepted_warning_codes: [],
      exact_author_draft: {
        draft_id: "author_draft_scene_s1",
        base_revision_no: 1,
        expected_current_final_scene_row_id: null,
        content: "<p>潮水退去，她看清了闸门上的名字。</p>",
      },
    });
    expect(client.apiPatch.mock.calls.filter(([url]) => /\/author-drafts\//.test(url))).toEqual([]);
    // done 只由服务端 archived 响应映射，且写穿到目录 PATCH（mock 后端重拉
    // 会把乐观缓存收敛回 mock 值，故断言写穿动作而非最终缓存态）
    await vi.waitFor(() => expect(client.apiPatch).toHaveBeenCalledWith(
      expect.stringMatching(/\/scenes\/s1$/),
      expect.objectContaining({ state: "done" })
    ), T);
    void cat;
    // 正文写作器缓存同步（写穿主路径或缓存）
    const wrKeys = Object.keys(window.localStorage).filter(k => k.includes("wr-doc:ch01s1"));
    expect(wrKeys.length).toBeGreaterThan(0);
  });

  it("后端拒绝（409 无稿/来源安全）：不置 done、不写缓存、faithful 返回失败", async () => {
    const { mod, client, cat } = await loadWithCatalog();
    const blocked = Object.assign(new Error("blocked"), { code: "SOURCE_SAFETY_BLOCKED" });
    const basePost = client.apiPost.getMockImplementation();
    client.apiPost.mockImplementation((url, body, options) => {
      if (/\/adopt-current$/.test(url)) return Promise.reject(blocked);
      return basePost(url, body, options);
    });

    const result = await mod.scnAdoptToDoc("ch01s1", DRAFT);

    expect(result.ok).toBe(false);
    expect(result.reason).toContain("SOURCE_SAFETY_BLOCKED");
    // 可证伪：先本地置 done 的旧实现会发出 state:"done" 的目录 PATCH，此断言转红
    const donePatches = client.apiPatch.mock.calls.filter(c => c[1] && c[1].state === "done");
    expect(donePatches).toEqual([]);
    const scene = cat.WsCatalog.get()[0].scenes.find(s => s.sid === "ch01s1");
    expect(scene.state).not.toBe("done");
    expect(Object.keys(window.localStorage).filter(k => k.includes("wr-doc:ch01s1"))).toEqual([]);
  });

  it("内容安全 409 保留结构化错误，并仅把当前 exact finding codes 传回服务端", async () => {
    const { mod, client } = await loadWithCatalog();
    const reviewError = Object.assign(new Error("review required"), {
      code: "CONTENT_SAFETY_REVIEW_REQUIRED",
      status: 409,
      details: {
        final_text_gate: {
          content_safety: {
            findings: [{
              code: "sexual_content_with_minor_indicators",
              review_required: true,
              acknowledged: false,
            }],
          },
        },
      },
    });
    const basePost = client.apiPost.getMockImplementation();
    client.apiPost.mockImplementation((url, body, options) => {
      if (!/\/adopt-current$/.test(url)) return basePost(url, body, options);
      if (!body.accepted_warning_codes.length) return Promise.reject(reviewError);
      return Promise.resolve({
        scene_status: "archived",
        final_scene_row_id: "final_s1_v1",
        draft_id: body.exact_author_draft.draft_id,
        draft_revision_no: 2,
        author_draft: { ...body.exact_author_draft, revision_no: 2, canonical_dirty: false },
      });
    });

    const blocked = await mod.scnAdoptToDoc("ch01s1", DRAFT);
    expect(blocked).toMatchObject({ ok: false, error: reviewError });
    expect(client.apiPost).toHaveBeenNthCalledWith(
      client.apiPost.mock.calls.findIndex(([url]) => /\/adopt-current$/.test(url)) + 1,
      "/api/v1/scenes/s1/adopt-current",
      expect.objectContaining({
        accepted_warning_codes: [],
        exact_author_draft: expect.objectContaining({
          draft_id: "author_draft_scene_s1",
          base_revision_no: 1,
        }),
      }),
    );

    const accepted = await mod.scnAdoptToDoc("ch01s1", DRAFT, null, {
      acceptedWarningCodes: ["sexual_content_with_minor_indicators"],
    });
    expect(accepted.ok).toBe(true);
    expect(client.apiPost).toHaveBeenCalledWith(
      "/api/v1/scenes/s1/adopt-current",
      expect.objectContaining({
        accepted_warning_codes: ["sexual_content_with_minor_indicators"],
        exact_author_draft: expect.objectContaining({ draft_id: "author_draft_scene_s1" }),
      }),
    );
  });

  it("内容安全复核重试复用已验证的作者稿备份，不制造重复副本", async () => {
    const { mod, client } = await loadWithCatalog();
    const key = window.wsKey("wr-doc:ch01s1");
    window.localStorage.setItem(key, "<p>作者亲写的正文。</p>");
    const reviewError = Object.assign(new Error("review required"), {
      code: "CONTENT_SAFETY_REVIEW_REQUIRED",
      status: 409,
      details: { final_text_gate: { content_safety: { findings: [] } } },
    });
    const basePost = client.apiPost.getMockImplementation();
    client.apiPost.mockImplementation((url, body, options) => {
      if (!/\/adopt-current$/.test(url)) return basePost(url, body, options);
      return body.accepted_warning_codes.length
        ? Promise.resolve({
            scene_status: "archived",
            final_scene_row_id: "final_s1_v1",
            draft_id: body.exact_author_draft.draft_id,
            draft_revision_no: 2,
            author_draft: { ...body.exact_author_draft, revision_no: 2, canonical_dirty: false },
          })
        : Promise.reject(reviewError);
    });

    const blocked = await mod.scnAdoptToDoc("ch01s1", DRAFT, null, {
      mode: "overwrite",
      confirmed: true,
    });
    expect(blocked).toMatchObject({
      ok: false,
      authorBackup: expect.objectContaining({ type: "backup", durable: true }),
    });
    expect(window.WrRecovery.list()).toHaveLength(1);

    const accepted = await mod.scnAdoptToDoc("ch01s1", DRAFT, null, {
      mode: "overwrite",
      confirmed: true,
      authorBackupId: blocked.authorBackup.id,
      acceptedWarningCodes: ["sexual_content_with_minor_indicators"],
    });
    expect(accepted.ok).toBe(true);
    expect(window.WrRecovery.list()).toHaveLength(1);
  });

  it("目录未同步到后端（无 backendId）：不静默装成功", async () => {
    const { mod } = await loadSceneRun({ catalog: [] });
    const cat = await import("./ws-catalog.jsx");
    await vi.waitFor(() => expect(cat.WsCatalog.ready()).toBe(true), T);
    const result = await mod.scnAdoptToDoc("ch99s9", DRAFT);
    expect(result.ok).toBe(false);
  });

  it("已有作者稿时可默认保存为候选：不调用归档、不覆盖正文", async () => {
    const { mod, client } = await loadWithCatalog();
    const key = window.wsKey("wr-doc:ch01s1");
    window.localStorage.setItem(key, "<p>作者亲写的正文。</p>");

    const result = await mod.scnAdoptToDoc("ch01s1", DRAFT);

    expect(result).toMatchObject({ ok: true, archived: false, mode: "candidate" });
    expect(window.localStorage.getItem(key)).toBe("<p>作者亲写的正文。</p>");
    expect(client.apiPost.mock.calls.filter(([url]) => /adopt-current/.test(url))).toEqual([]);
    expect(window.WrRecovery.list()).toEqual([
      expect.objectContaining({ sid: "ch01s1", type: "candidate", source: "ai" }),
    ]);
  });

  it("调用层只声明 overwrite 但没有显式确认时也 fail closed", async () => {
    const { mod, client } = await loadWithCatalog();
    const key = window.wsKey("wr-doc:ch01s1");
    window.localStorage.setItem(key, "<p>作者亲写的正文。</p>");

    const result = await mod.scnAdoptToDoc("ch01s1", DRAFT, null, { mode: "overwrite" });

    expect(result).toMatchObject({ ok: false, confirmationRequired: true });
    expect(window.localStorage.getItem(key)).toBe("<p>作者亲写的正文。</p>");
    expect(window.WrRecovery.list()).toEqual([]);
    expect(client.apiPost.mock.calls.filter(([url]) => /adopt-current/.test(url))).toEqual([]);
  });

  it("作者正文后文恰好写到旧占位那句话：仍是作者稿，覆盖必须先确认", async () => {
    // 旧判定：整份草稿里出现过「在这里开始写这一场」就当空稿——这里会不经确认直接覆盖并发 adopt-current
    const { mod, client } = await loadWithCatalog();
    const key = window.wsKey("wr-doc:ch01s1");
    const authored = "<p>她把纸条翻过来。</p><p>背面只有一行字：在这里开始写这一场……</p>";
    window.localStorage.setItem(key, authored);

    const preview = await mod.scnPrepareAdoption("ch01s1", DRAFT);
    expect(preview.hasReal).toBe(true);

    const result = await mod.scnAdoptToDoc("ch01s1", DRAFT, null, { mode: "overwrite" });
    expect(result).toMatchObject({ ok: false, confirmationRequired: true });
    expect(window.localStorage.getItem(key)).toBe(authored);
    expect(client.apiPost.mock.calls.filter(([url]) => /adopt-current/.test(url))).toEqual([]);
  });

  it("旧草稿只剩开头那句占位：算空稿，不要求确认；占位后面接着写了字就算作者稿", async () => {
    const { mod } = await loadWithCatalog();
    const key = window.wsKey("wr-doc:ch01s1");
    window.localStorage.setItem(key, "<p><br></p><p>在这里开始写这一场……</p>");
    const empty = await mod.scnPrepareAdoption("ch01s1", DRAFT);
    expect(empty.hasReal).toBe(false);
    // 差异里也不把占位当成要删掉的一段
    expect(empty.diff.dels).toBe(0);

    window.localStorage.setItem(key, "<p>在这里开始写这一场……潮水涨上来了。</p>");
    const started = await mod.scnPrepareAdoption("ch01s1", DRAFT);
    expect(started.hasReal).toBe(true);
  });

  it("采用预检会先水合服务器作者稿：即使本机无缓存也不得直接覆盖", async () => {
    const { mod, client } = await loadWithCatalog();
    client.apiPost.mockImplementation((url) => {
      if (/\/author-drafts\/scene\/s1\/ensure$/.test(url)) {
        return Promise.resolve({ draft: { draft_id: "d-server", revision_no: 7, content: "<p>另一台设备写下的作者稿。</p>" } });
      }
      return Promise.resolve({});
    });

    const preview = await mod.scnPrepareAdoption("ch01s1", DRAFT);

    expect(preview.hasReal).toBe(true);
    expect(preview.existing).toBe("<p>另一台设备写下的作者稿。</p>");
    expect(preview.diff.dels).toBeGreaterThan(0);
  });

  it("明确覆盖时先持久备份作者稿，再归档并写入 AI 稿", async () => {
    const { mod, client } = await loadWithCatalog();
    const key = window.wsKey("wr-doc:ch01s1");
    window.localStorage.setItem(key, "<p>作者亲写的正文。</p>");
    const basePost = client.apiPost.getMockImplementation();
    client.apiPost.mockImplementation((url, body, options) => {
      if (/\/adopt-current$/.test(url)) return Promise.resolve({
        scene_status: "archived",
        final_scene_row_id: "final_s1_v1",
        draft_id: body.exact_author_draft.draft_id,
        draft_revision_no: 2,
        author_draft: { ...body.exact_author_draft, revision_no: 2, canonical_dirty: false },
      });
      return basePost(url, body, options);
    });

    const result = await mod.scnAdoptToDoc("ch01s1", DRAFT, null, { mode: "overwrite", confirmed: true });

    expect(result).toMatchObject({ ok: true, archived: true, authorBackup: expect.objectContaining({ durable: true }) });
    expect(window.WrRecovery.list()).toEqual([
      expect.objectContaining({ type: "backup", html: "<p>作者亲写的正文。</p>" }),
    ]);
    expect(window.localStorage.getItem(key)).toContain("潮水退去，她看清了闸门上的名字");
    expect(client.apiPost.mock.calls.some(([url]) => /adopt-current$/.test(url))).toBe(true);
  });

  it("覆盖前备份触发 quota 时 fail-safe：阻止归档，作者稿保持不变", async () => {
    const { mod, client } = await loadWithCatalog();
    const key = window.wsKey("wr-doc:ch01s1");
    window.localStorage.setItem(key, "<p>作者亲写的正文。</p>");
    const originalSetItem = Storage.prototype.setItem;
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(function quotaForBackup(storageKey, value) {
      if (String(storageKey).startsWith("wr-recovery:v1:")) throw new DOMException("full", "QuotaExceededError");
      return originalSetItem.call(this, storageKey, value);
    });

    const result = await mod.scnAdoptToDoc("ch01s1", DRAFT, null, { mode: "overwrite", confirmed: true });

    expect(result).toMatchObject({ ok: false, backupFailed: true });
    expect(window.localStorage.getItem(key)).toBe("<p>作者亲写的正文。</p>");
    expect(client.apiPost.mock.calls.filter(([url]) => /adopt-current/.test(url))).toEqual([]);
  });
});

/* ==========================================================
   Wave 2（结果闭环治理 §5.3/§5.4）：作者可见状态门。
   「无法继续」（hard_blocked = verified Q0/Q1，不可归档）与
   「已有稿但建议修改」（quality_warning = Q2/Q3，可归档）必须分开：
   · scnGateFrom 从 workbench/status 的 author_state 投影提取 gate
   · scnAdoptToDoc 对 canArchive=false 前置拦截（不发 adopt POST）
   · quality_warning 不拦归档
   ========================================================== */
describe("作者状态门（Wave 2：无法继续 vs 有稿建议修改）", () => {
  beforeEach(() => {
    vi.resetModules();
    window.localStorage.clear();
    vi.spyOn(window, "confirm").mockReturnValue(true);
  });
  afterEach(() => vi.restoreAllMocks());

  const DRAFT = [{ id: "p1", parts: [{ text: "潮水退去，她看清了闸门上的名字。" }] }];

  const HARD_BLOCKED_PROJECTION = {
    author_state: "hard_blocked",
    blocking_findings: [{ issue_key: "missing_required_text", quality_level: "Q1", verified_by: "scene_card_required_text" }],
    quality_warnings: [],
    recommended_actions: ["review_pipeline_gate"],
    can_archive: false,
  };
  const QUALITY_WARNING_PROJECTION = {
    author_state: "quality_warning",
    blocking_findings: [],
    quality_warnings: [{ issue_key: "pacing_flat", quality_level: "Q2" }],
    recommended_actions: ["adopt_or_patch"],
    can_archive: true,
  };

  it("scnGateFrom：hard_blocked 投影 → canArchive=false + 阻断条目", async () => {
    const { mod } = await loadSceneRun();
    const gate = mod.scnGateFrom({ author_state: HARD_BLOCKED_PROJECTION });
    expect(gate.authorState).toBe("hard_blocked");
    expect(gate.canArchive).toBe(false);
    expect(gate.blocking[0].issue_key).toBe("missing_required_text");
  });

  it("hard QC rewrite_brief becomes an actionable author rewrite instruction", async () => {
    const { mod } = await loadSceneRun();
    expect(mod.scnRewriteBriefFrom({
      hard_qc: { rewrite_brief: ["补齐推门动作", "明确主动销毁通行证"] },
      author_state: HARD_BLOCKED_PROJECTION,
    })).toBe("补齐推门动作；明确主动销毁通行证");
  });

  it("scnRewriteBriefFrom：读后端真实键名 hard_qc_summary / soft_qc_summary，硬优先于软", async () => {
    const { mod } = await loadSceneRun();
    // 服务端形状（api/routes/scenes.py `_serialize_qc_summary`）
    expect(mod.scnRewriteBriefFrom({
      hard_qc_summary: { qc_type: "hard_qc", pass_flag: false, rewrite_brief: ["补齐推门动作", "明确主动销毁通行证"] },
      soft_qc_summary: { qc_type: "soft_qc", pass_flag: false, rewrite_brief: ["收紧结尾三句"] },
      author_state: HARD_BLOCKED_PROJECTION,
    })).toBe("补齐推门动作；明确主动销毁通行证");
    // 硬质检已过（rewrite_brief 为空列表）→ 退到软质检的修补建议，而不是 issue_key 拼接
    expect(mod.scnRewriteBriefFrom({
      hard_qc_summary: { qc_type: "hard_qc", pass_flag: true, rewrite_brief: [] },
      soft_qc_summary: { qc_type: "soft_qc", pass_flag: false, rewrite_brief: ["收紧结尾三句"] },
      author_state: HARD_BLOCKED_PROJECTION,
    })).toBe("收紧结尾三句");
    // 两份摘要都为 null（尚未跑质检）→ 退到 author_state 阻断项的可读原因
    expect(mod.scnRewriteBriefFrom({
      hard_qc_summary: null,
      soft_qc_summary: null,
      author_state: HARD_BLOCKED_PROJECTION,
    })).toBe("missing_required_text");
  });

  it("scnRewriteBriefFrom：旧键名 hard_qc / soft_qc / latest_qc 仍可兜底，且服务端键名优先", async () => {
    const { mod } = await loadSceneRun();
    expect(mod.scnRewriteBriefFrom({
      hard_qc_summary: { rewrite_brief: ["服务端硬指令"] },
      hard_qc: { rewrite_brief: ["旧键硬指令"] },
    })).toBe("服务端硬指令");
    expect(mod.scnRewriteBriefFrom({ soft_qc: { rewrite_brief: ["旧键软指令"] } })).toBe("旧键软指令");
    expect(mod.scnRewriteBriefFrom({ latest_qc: { rewrite_brief: ["旧键最近指令"] } })).toBe("旧键最近指令");
    // qc-reports 明细路径的原始 rewrite_brief_json 条目（instruction / carry_note_text）也不能变成 [object Object]
    expect(mod.scnRewriteBriefFrom({
      hard_qc_summary: { rewrite_brief: [{ instruction: "补齐推门动作" }, { carry_note_text: "通行证已销毁" }, "", null] },
    })).toBe("补齐推门动作；通行证已销毁");
    expect(mod.scnRewriteBriefFrom(null)).toBe("");
  });

  it("scnGateFrom：quality_warning 投影 → 可归档 + 警告随行；无投影 → null", async () => {
    const { mod } = await loadSceneRun();
    const gate = mod.scnGateFrom({ author_state: QUALITY_WARNING_PROJECTION });
    expect(gate.authorState).toBe("quality_warning");
    expect(gate.canArchive).toBe(true);
    expect(gate.warnings.length).toBe(1);
    expect(mod.scnGateFrom({})).toBeNull();
  });

  it("scnAdoptToDoc：gate 不可归档 → 前置拦截，不发 adopt-current POST", async () => {
    const { mod, client } = await loadSceneRun();
    const cat = await import("./ws-catalog.jsx");
    await vi.waitFor(() => expect(cat.WsCatalog.get().length).toBeGreaterThan(0), T);
    const gate = mod.scnGateFrom({ author_state: HARD_BLOCKED_PROJECTION });

    const result = await mod.scnAdoptToDoc("ch01s1", DRAFT, gate);

    expect(result.ok).toBe(false);
    expect(result.reason).toContain("已证实的硬问题");
    // 拦下的原因是说给作者的：不念英文 issue_key
    expect(result.reason).not.toMatch(/[a-z]+_[a-z_]+/);
    const adoptCalls = client.apiPost.mock.calls.filter(c => /adopt-current/.test(c[0]));
    expect(adoptCalls).toEqual([]);
    // 正文保留、不置 done、不写缓存
    expect(Object.keys(window.localStorage).filter(k => k.includes("wr-doc:ch01s1"))).toEqual([]);
  });

  it("Wave 3 终选三函数：盲化取数 / 选择提交 / 续跑（sid→后端 id 对位）", async () => {
    const { mod, client } = await loadSceneRun();
    const cat = await import("./ws-catalog.jsx");
    await vi.waitFor(() => expect(cat.WsCatalog.get().length).toBeGreaterThan(0), T);
    const base = client.apiGet.getMockImplementation();
    client.apiGet.mockImplementation((url) => {
      if (/\/api\/v1\/scenes\/s1\/style-candidates$/.test(url)) {
        return Promise.resolve({
          blinded: true,
          candidates: [
            { row_id: "cand_b", content: "候选乙全文" },
            { row_id: "cand_a", content: "候选甲全文" },
          ],
          selection: { decision_status: "awaiting", selected_row_id: null },
        });
      }
      return base(url);
    });
    client.apiPost.mockImplementation((url) => Promise.resolve({ ok: true, url }));

    const list = await mod.scnCandidates("ch01s1");
    // 盲化契约：按后端 blinded_order 原样呈现，不重排、无分数字段
    expect(list.blinded).toBe(true);
    expect(list.candidates.map(c => c.row_id)).toEqual(["cand_b", "cand_a"]);
    expect(list.candidates.every(c => !("adversarial_score" in c))).toBe(true);

    await mod.scnSelectCandidate("ch01s1", "cand_b", {
      no_clear_difference: true,
      preference_tags: ["style_match"],
    });
    expect(client.apiPost).toHaveBeenCalledWith(
      "/api/v1/scenes/s1/style-candidates/cand_b/select",
      expect.objectContaining({ no_clear_difference: true, preference_tags: ["style_match"] })
    );

    await mod.scnResumeAfterSelection("ch01s1");
    expect(client.apiPost).toHaveBeenCalledWith("/api/v1/scenes/s1/resume-after-selection", expect.anything());
  });

  it("Wave 3 终选锁定：SELECTION_LOCKED 拒绝原样上抛（不静默吞掉）", async () => {
    const { mod, client } = await loadSceneRun();
    const cat = await import("./ws-catalog.jsx");
    await vi.waitFor(() => expect(cat.WsCatalog.get().length).toBeGreaterThan(0), T);
    const locked = Object.assign(new Error("selection locked"), { code: "SELECTION_LOCKED" });
    client.apiPost.mockImplementation((url) => {
      if (/\/select$/.test(url)) return Promise.reject(locked);
      return Promise.resolve({});
    });

    await expect(mod.scnSelectCandidate("ch01s1", "cand_x", {})).rejects.toMatchObject({ code: "SELECTION_LOCKED" });
  });

  it("scnAdoptToDoc：quality_warning 的 gate 不拦归档（Q2/Q3 照常交付）", async () => {
    const { mod, client } = await loadSceneRun();
    const cat = await import("./ws-catalog.jsx");
    await vi.waitFor(() => expect(cat.WsCatalog.get().length).toBeGreaterThan(0), T);
    const basePost = client.apiPost.getMockImplementation();
    client.apiPost.mockImplementation((url, body, options) => {
      if (/adopt-current$/.test(url)) {
        return Promise.resolve({
          scene_id: "s1",
          scene_status: "archived",
          final_scene_row_id: "final_s1_v1",
          draft_id: body.exact_author_draft.draft_id,
          draft_revision_no: 2,
          author_draft: {
            ...body.exact_author_draft,
            revision_no: 2,
            last_promoted_revision_no: 2,
            last_promoted_final_scene_row_id: "final_s1_v1",
            canonical_dirty: false,
          },
        });
      }
      return basePost(url, body, options);
    });
    const gate = mod.scnGateFrom({ author_state: QUALITY_WARNING_PROJECTION });

    const result = await mod.scnAdoptToDoc("ch01s1", DRAFT, gate);

    expect(result.ok).toBe(true);
    expect(client.apiPost).toHaveBeenCalledWith("/api/v1/scenes/s1/adopt-current", expect.anything());
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

/* 2026-09-12 风格直起（Step 2，Track C）：运行任务横幅的 current_step 中文标签——
   neutral_running 步位在 draft_mode=style_first 下写的是作者手笔首稿；起草方式来自
   workbench generation_summary.draft_mode，随运行记录持久化并由场景页传给横幅。 */
describe("scene run step labels（风格直起）", () => {
  beforeEach(() => {
    vi.resetModules();
    window.localStorage.clear();
  });

  afterEach(async () => {
    while (mountedRoots.length) {
      const { root, host } = mountedRoots.pop();
      await act(async () => root.unmount());
      host.remove();
    }
    vi.restoreAllMocks();
  });

  it("runJobStepLabel：管线词表映射中文；neutral_running 按起草方式切换；未知 token 原样回显", async () => {
    const { mod } = await loadSceneRun();
    expect(mod.runJobStepLabel("neutral_running", "style_first")).toBe("首稿（作者手笔）");
    expect(mod.runJobStepLabel("neutral_running", "neutral_first")).toBe("中性稿");
    expect(mod.runJobStepLabel("neutral_running", null)).toBe("中性稿");
    expect(mod.runJobStepLabel("planning_running")).toBe("规划蓝图");
    expect(mod.runJobStepLabel("bundle_built")).toBe("上下文已冻结");
    expect(mod.runJobStepLabel("hard_qc_running")).toBe("硬质检");
    expect(mod.runJobStepLabel("style_running", "style_first")).toBe("风格稿");
    expect(mod.runJobStepLabel("soft_qc_running")).toBe("软质检");
    expect(mod.runJobStepLabel("rewrite_running")).toBe("近终稿改写");
    expect(mod.runJobStepLabel("acceptance_review_running")).toBe("近终稿评审");
    expect(mod.runJobStepLabel("near_final")).toBe("近终稿");
    expect(mod.runJobStepLabel("archived")).toBe("已归档");
    expect(mod.runJobStepLabel("awaiting_candidate_selection")).toBe("等你终选");
    expect(mod.runJobStepLabel("some_future_step")).toBe("some_future_step");
    expect(mod.runJobStepLabel("")).toBe("");
    expect(mod.runJobStepLabel(null)).toBe("");
  });

  it("scnDraftModeFrom：只认 workbench generation_summary 里的两个取值", async () => {
    const { mod } = await loadSceneRun();
    expect(mod.scnDraftModeFrom({ generation_summary: { draft_mode: "style_first" } })).toBe("style_first");
    expect(mod.scnDraftModeFrom({ generation_summary: { draft_mode: "neutral_first" } })).toBe("neutral_first");
    expect(mod.scnDraftModeFrom({ generation_summary: { draft_mode: "whatever" } })).toBeNull();
    expect(mod.scnDraftModeFrom({ generation_summary: null })).toBeNull();
    expect(mod.scnDraftModeFrom(null)).toBeNull();
  });

  it("横幅按 draftMode 属性给 neutral_running 打标签：style_first → 首稿（作者手笔）", async () => {
    const { mod, client } = await loadSceneRun();
    client.getLatestSceneRunJob.mockResolvedValue({
      job_id: "job-style-first",
      scene_id: "SC01",
      status: "running",
      current_step: "neutral_running",
    });
    const view = await renderRunJobControl(mod.SceneRunJobControl, { sceneId: "SC01", draftMode: "style_first" });
    await vi.waitFor(() => {
      expect(view.host.querySelector('[role="status"]')?.textContent).toContain("运行中 · 首稿（作者手笔）");
    }, T);
    expect(view.host.querySelector('[role="status"]')?.textContent).not.toContain("neutral_running");

    await view.rerender({ sceneId: "SC01", draftMode: "neutral_first" });
    await vi.waitFor(() => {
      expect(view.host.querySelector('[role="status"]')?.textContent).toContain("运行中 · 中性稿");
    }, T);
    expect(view.host.querySelector('[role="status"]')?.textContent).not.toContain("作者手笔");
  });

  it("scnHydrateFromBackend：把 generation_summary.draft_mode 记到运行记录，并随 scnRunSave 持久化", async () => {
    const { mod, client } = await loadSceneRun({ projects: [NON_DEMO_PROJECT] });
    const baseGet = client.apiGet.getMockImplementation();
    client.apiGet.mockImplementation((url, options) => {
      if (url === "/api/v1/scenes/s1/workbench") {
        return Promise.resolve({
          neutral_draft: { content: "潮水退去。\n她留下了证词。" },
          scene_run_state: { scene_status: "neutral_running" },
          generation_summary: { draft_mode: "style_first", attempts: 1 },
        });
      }
      return baseGet(url, options);
    });
    const restored = await mod.scnHydrateFromBackend("ch01s1", {});
    expect(restored).toBeTruthy();
    expect(restored.draftMode).toBe("style_first");
    mod.scnRunSave("ch01s1", restored);
    expect(mod.scnRunLoad("ch01s1").draftMode).toBe("style_first");
  });

  it("scnHydrateFromBackend：无正文的预算断点同样带起草方式", async () => {
    const { mod, client } = await loadSceneRun({ projects: [NON_DEMO_PROJECT] });
    const baseGet = client.apiGet.getMockImplementation();
    client.apiGet.mockImplementation((url, options) => {
      if (url === "/api/v1/scenes/s1/workbench") {
        return Promise.resolve({
          scene_run_state: { scene_status: "bundle_built", lifecycle_budget: { baseline_tokens: 7200, recommended_topup_tokens: 7200 } },
          generation_summary: { draft_mode: "neutral_first" },
        });
      }
      return baseGet(url, options);
    });
    const restored = await mod.scnHydrateFromBackend("ch01s1", {
      terminalJob: {
        job_id: "job-budget",
        scene_id: "s1",
        status: "blocked",
        current_step: "neutral_running",
        error_code: "LLM_SCENE_TOKEN_BUDGET_EXHAUSTED",
        error_text: "scene token budget exhausted before dispatch",
      },
    });
    expect(restored).toMatchObject({ recoveredWithoutDraft: true, draftMode: "neutral_first" });
  });

  it("scnRun：运行记录带 draftMode，运行日志写新的管线句与首稿方式", async () => {
    const { mod, client } = await loadSceneRun();
    const baseGet = client.apiGet.getMockImplementation();
    client.apiPost.mockImplementation((url) => {
      if (/\/api\/v1\/scenes\/s1\/run\/jobs$/.test(url)) {
        return Promise.resolve({ job_id: "job-style", scene_id: "s1", status: "running" });
      }
      return Promise.resolve({});
    });
    client.apiGet.mockImplementation((url) => {
      if (url === "/api/v1/run-jobs/job-style") {
        return Promise.resolve({ job_id: "job-style", scene_id: "s1", status: "completed", current_step: "near_final" });
      }
      if (url === "/api/v1/scenes/s1/workbench") {
        return Promise.resolve({
          style_draft: { content: "潮水退去。\n她留下了证词。" },
          scene_run_state: { scene_status: "near_final" },
          generation_summary: { draft_mode: "style_first" },
        });
      }
      return baseGet(url);
    });
    vi.useFakeTimers();
    let result;
    try {
      const runPromise = mod.scnRun({ sid: "ch01s1", kind: "主动场景" }, "", "", {});
      await vi.runAllTimersAsync();
      result = await runPromise;
    } finally {
      vi.useRealTimers();
    }
    expect(result.draftMode).toBe("style_first");
    const texts = result.log.map((l) => l.text);
    expect(texts[0]).toBe("已提交后端起草任务（预检 → 蓝图 → 首稿 → 硬质检 → 风格稿 → 软质检 → 近终稿）");
    expect(texts.some((t) => t.includes("首稿 作者手笔"))).toBe(true);
  });

  it("真实场景页：从 workbench 恢复的起草方式传到横幅，running · neutral_running 读作首稿（作者手笔）", async () => {
    const { client } = await loadSceneRun({ projects: [NON_DEMO_PROJECT] });
    await queueSceneIntent({ sid: "ch01s1" });
    client.getLatestSceneRunJob.mockResolvedValue({
      job_id: "job-page-style-first",
      scene_id: "s1",
      status: "running",
      current_step: "neutral_running",
    });
    const baseGet = client.apiGet.getMockImplementation();
    client.apiGet.mockImplementation((url, options) => {
      if (url === "/api/v1/scenes/s1/workbench") {
        return Promise.resolve({
          neutral_draft: { content: "潮水退去。\n她留下了证词。" },
          scene_run_state: { scene_status: "neutral_running" },
          generation_summary: { draft_mode: "style_first" },
        });
      }
      return baseGet(url, options);
    });
    const page = await import("./ws-scene.jsx");
    const view = await renderRunJobControl(page.WsScene, { go: vi.fn(), t: {} });
    await vi.waitFor(() => {
      expect(view.host.querySelector('[data-testid="scene-run-job-control"]')?.dataset.jobId).toBe("job-page-style-first");
      expect(view.host.querySelector('[role="status"]')?.textContent).toContain("首稿（作者手笔）");
    }, T);
    expect(view.host.querySelector('[role="status"]')?.textContent).not.toContain("neutral_running");
  });
});

/* ---- 2026-09-14 风格保真修补 WP4「作者看得见」：风格链路提示条 + 本场参考窗口 ----
   后端早已算出 STYLE_* notices 与本场实际进入提示的参考书样例窗口，此前没有视图渲染。
   这里验证：规整（只认合法条目）、提示条按严重度着色与中文标签、窗口面板默认收起、
   展开时按区间取原文并缓存、随运行记录持久化、真实场景页能看见。 */
describe("风格链路提示与本场参考窗口（WP4）", () => {
  beforeEach(() => {
    vi.resetModules();
    window.localStorage.clear();
  });

  afterEach(async () => {
    while (mountedRoots.length) {
      const { root, host } = mountedRoots.pop();
      await act(async () => root.unmount());
      host.remove();
    }
    vi.restoreAllMocks();
  });

  const NOTICES = [
    { code: "STYLE_FIRST_DRAFT", severity: "info", message: "首稿已按参考作者手笔直接起草。" },
    { code: "STYLE_INJECTION_DEGRADED", severity: "warning", message: "输入预算不足，风格参考前缀被整体裁掉。" },
    { code: "STYLE_PLAGIARISM_HIT", severity: "blocking", message: "风格稿与参考作品原文存在确定性 n-gram 重叠。", hit_count: 2, stage: "style_draft" },
    { code: "STYLE_SOMETHING_NEW", severity: "error", message: "后端新增的提示" },
    { not: "a notice" },
    "garbage",
  ];
  const STYLE_WINDOWS = {
    book_id: "book-1",
    profile_id: "profile-1",
    step: "style_draft",
    windows: [
      { start: 0, end: 59, chapter: 1, position: "opening", paragraph_type: "narration", paragraphs: 60, chars: 3800 },
      { start: 640, end: 662, chapter: 9, position: "closing", paragraph_type: "dialogue", paragraphs: 23, chars: 1510 },
      { start: 5, end: 2 },
      { end: 9 },
    ],
  };
  const PARAGRAPHS_URL = /^\/api\/v2\/style-reference\/books\/([^/]+)\/paragraphs\?start=(\d+)&end=(\d+)$/;

  function routeParagraphs(client, responder) {
    const base = client.apiGet.getMockImplementation();
    const calls = [];
    client.apiGet.mockImplementation((url, options) => {
      const m = PARAGRAPHS_URL.exec(url);
      if (m) {
        calls.push(url);
        return responder({ bookId: m[1], start: Number(m[2]), end: Number(m[3]) }, calls.length);
      }
      return base(url, options);
    });
    return calls;
  }

  it("scnStyleNoticesFrom / scnStyleWindowsFrom：只认 generation_summary 里的合法条目并规整字段名", async () => {
    const { mod } = await loadSceneRun();
    const notices = mod.scnStyleNoticesFrom({ generation_summary: { notices: NOTICES } });
    expect(notices.map((n) => n.code)).toEqual(["STYLE_FIRST_DRAFT", "STYLE_INJECTION_DEGRADED", "STYLE_PLAGIARISM_HIT", "STYLE_SOMETHING_NEW"]);
    expect(notices.map((n) => n.severity)).toEqual(["info", "warning", "error", "error"]);
    expect(notices[2]).toMatchObject({ blocking: true, hitCount: 2, stage: "style_draft" });
    expect(notices[0].blocking).toBe(false);
    expect(mod.scnStyleNoticesFrom({ generation_summary: { notices: "nope" } })).toEqual([]);
    expect(mod.scnStyleNoticesFrom({ generation_summary: null })).toEqual([]);
    expect(mod.scnStyleNoticesFrom(null)).toEqual([]);

    const windows = mod.scnStyleWindowsFrom({ generation_summary: { style_windows: STYLE_WINDOWS } });
    expect(windows).toEqual({
      bookId: "book-1",
      profileId: "profile-1",
      step: "style_draft",
      windows: [
        { start: 0, end: 59, chapter: 1, position: "opening", paragraphType: "narration", paragraphs: 60, chars: 3800 },
        { start: 640, end: 662, chapter: 9, position: "closing", paragraphType: "dialogue", paragraphs: 23, chars: 1510 },
      ],
    });
    expect(mod.scnStyleWindowsFrom({ generation_summary: { style_windows: null } })).toBeNull();
    expect(mod.scnStyleWindowsFrom({ generation_summary: { style_windows: { book_id: "b", windows: [] } } })).toBeNull();
    expect(mod.scnStyleWindowsFrom({ generation_summary: { style_windows: { windows: [{ start: 3, end: 1 }] } } })).toBeNull();
    expect(mod.scnStyleWindowLabel(windows.windows[0])).toBe("第 1 章 · 章首 · 第 1–60 段（3,800 字）");
    expect(mod.scnStyleWindowLabel({ start: 4, end: 4, chapter: 0, position: "", paragraphType: "x_new", paragraphs: 1, chars: 12 })).toBe("第 5–5 段（12 字）");
    // 段落类型、配额、标签用风格参考同一张词表；词表外的段落类型不念给作者
    expect(mod.scnStyleWindowTags(windows.windows[1])).toEqual({ slot: "", tags: [], devices: [], paragraphType: "对话为主" });
    expect(mod.scnStyleWindowTags({ paragraphType: "x_new" }).paragraphType).toBe("");
  });

  it("提示条：每条一行、中文标签、按严重度着色；未知 code 回退为「code: message」", async () => {
    const { mod } = await loadSceneRun();
    const view = await renderRunJobControl(mod.SceneStyleNoticeStrip, {
      notices: mod.scnStyleNoticesFrom({ generation_summary: { notices: NOTICES } }),
    });
    const strip = view.host.querySelector('[data-testid="scene-style-notices"]');
    expect(strip).not.toBeNull();
    const rows = Array.from(strip.querySelectorAll("li"));
    expect(rows.length).toBe(4);
    expect(rows[0].className).toContain("sev-info");
    expect(rows[0].dataset.code).toBe("STYLE_FIRST_DRAFT");
    expect(rows[0].textContent).toContain(mod.STYLE_NOTICE_LABELS.STYLE_FIRST_DRAFT);
    expect(rows[0].textContent).toContain("写完先量像不像");
    expect(rows[1].className).toContain("sev-warning");
    expect(rows[1].textContent).toContain(mod.STYLE_NOTICE_LABELS.STYLE_INJECTION_DEGRADED);
    expect(rows[1].textContent).toContain("提示太长，文风被整块挤掉了");
    expect(rows[2].className).toContain("sev-error");
    expect(rows[2].className).toContain("is-blocking");
    expect(rows[2].textContent).toContain("风格稿里有与参考书原文连续相同的地方");
    expect(rows[2].textContent).toContain("共 2 处");
    // 后端原话里的术语不给作者看；「注入」一律说「带入起草」
    expect(strip.textContent).not.toMatch(/n-gram|soft_qc|注入|手笔直接起草。/);
    expect(rows[3].className).toContain("sev-error");
    expect(rows[3].textContent).toContain("STYLE_SOMETHING_NEW: 后端新增的提示");
    expect(rows.every((row) => row.className.includes("scn2-style-notice"))).toBe(true);
  });

  it("提示条：无 notices 时不渲染", async () => {
    const { mod } = await loadSceneRun();
    const empty = await renderRunJobControl(mod.SceneStyleNoticeStrip, { notices: [] });
    expect(empty.host.querySelector('[data-testid="scene-style-notices"]')).toBeNull();
    expect(empty.host.innerHTML).toBe("");
    const missing = await renderRunJobControl(mod.SceneStyleNoticeStrip, {});
    expect(missing.host.querySelector('[data-testid="scene-style-notices"]')).toBeNull();
  });

  it("参考窗口面板：一窗一行、默认收起；展开时按窗口区间取原文并只读展示，收起再展开不重复请求", async () => {
    const { mod, client } = await loadSceneRun();
    const calls = routeParagraphs(client, ({ bookId, start, end }) => Promise.resolve({
      book_id: bookId,
      start,
      end,
      capped: false,
      paragraphs: [
        { paragraph_index: start, paragraph_type: "narration", text: "潮水退去的时候，滩涂上只剩一只鞋。" },
        { paragraph_index: start + 1, paragraph_type: "dialogue", text: "“你来晚了。”" },
      ],
    }));
    const view = await renderRunJobControl(mod.SceneStyleWindowsPanel, {
      styleWindows: mod.scnStyleWindowsFrom({ generation_summary: { style_windows: STYLE_WINDOWS } }),
    });
    const panel = view.host.querySelector('[data-testid="scene-style-windows"]');
    expect(panel).not.toBeNull();
    expect(panel.textContent).toContain("本场参考窗口 · 2");
    expect(panel.textContent).toContain("风格稿");
    const rows = Array.from(panel.querySelectorAll('[data-testid="scene-style-window-row"]'));
    expect(rows.length).toBe(2);
    expect(rows[0].textContent).toContain("第 1 章 · 章首 · 第 1–60 段（3,800 字）");
    expect(rows[0].textContent).toContain("叙述为主");
    expect(rows[1].textContent).toContain("第 9 章 · 章末 · 第 641–663 段（1,510 字）");
    expect(view.host.querySelector('[data-testid="scene-style-window-text"]')).toBeNull();
    expect(calls).toEqual([]);

    const button = rows[0].querySelector("button");
    expect(button.getAttribute("aria-expanded")).toBe("false");
    expect(button.disabled).toBe(false);
    await click(button);
    await vi.waitFor(() => {
      expect(view.host.querySelector('[data-testid="scene-style-window-text"]')?.textContent).toContain("潮水退去的时候，滩涂上只剩一只鞋。");
    }, T);
    expect(calls).toEqual(["/api/v2/style-reference/books/book-1/paragraphs?start=0&end=59"]);
    expect(button.getAttribute("aria-expanded")).toBe("true");
    const text = view.host.querySelector('[data-testid="scene-style-window-text"]');
    expect(text.textContent).toContain("“你来晚了。”");
    expect(text.querySelectorAll("p").length).toBe(2);
    expect(text.querySelector("textarea, input, [contenteditable]")).toBeNull();

    await click(button);
    expect(view.host.querySelector('[data-testid="scene-style-window-text"]')).toBeNull();
    expect(button.getAttribute("aria-expanded")).toBe("false");
    await click(button);
    await vi.waitFor(() => {
      expect(view.host.querySelector('[data-testid="scene-style-window-text"]')?.textContent).toContain("潮水退去的时候");
    }, T);
    expect(calls.length).toBe(1);

    // 第二窗独立取回，区间是它自己的
    await click(rows[1].querySelector("button"));
    await vi.waitFor(() => expect(calls.length).toBe(2), T);
    expect(calls[1]).toBe("/api/v2/style-reference/books/book-1/paragraphs?start=640&end=662");
  });

  it("参考窗口面板：无窗口不渲染；参考书不可用时不能展开；取回失败显示错误并可重试；截断有提示", async () => {
    const { mod, client } = await loadSceneRun();
    const none = await renderRunJobControl(mod.SceneStyleWindowsPanel, { styleWindows: null });
    expect(none.host.querySelector('[data-testid="scene-style-windows"]')).toBeNull();
    const emptyList = await renderRunJobControl(mod.SceneStyleWindowsPanel, { styleWindows: { bookId: "b", windows: [] } });
    expect(emptyList.host.querySelector('[data-testid="scene-style-windows"]')).toBeNull();

    const noBook = await renderRunJobControl(mod.SceneStyleWindowsPanel, {
      styleWindows: mod.scnStyleWindowsFrom({ generation_summary: { style_windows: { ...STYLE_WINDOWS, book_id: null } } }),
    });
    const noBookButton = noBook.host.querySelector('[data-testid="scene-style-window-row"] button');
    expect(noBookButton.disabled).toBe(true);
    expect(noBook.host.textContent).toContain("参考书已不可用");

    const calls = routeParagraphs(client, ({ bookId, start, end }, n) => (n === 1
      ? Promise.reject(new Error("后端不可达"))
      : Promise.resolve({ book_id: bookId, start, end: start + 79, capped: true, paragraphs: [{ paragraph_index: start, paragraph_type: "narration", text: "第二次取回成功。" }] })));
    const view = await renderRunJobControl(mod.SceneStyleWindowsPanel, {
      styleWindows: mod.scnStyleWindowsFrom({ generation_summary: { style_windows: STYLE_WINDOWS } }),
    });
    const button = view.host.querySelector('[data-testid="scene-style-window-row"] button');
    await click(button);
    await vi.waitFor(() => {
      expect(view.host.querySelector('[data-testid="scene-style-window-text"] [role="alert"]')?.textContent).toContain("后端不可达");
    }, T);
    expect(calls.length).toBe(1);
    await click(button);
    await click(button);
    await vi.waitFor(() => {
      expect(view.host.querySelector('[data-testid="scene-style-window-text"]')?.textContent).toContain("第二次取回成功。");
    }, T);
    expect(calls.length).toBe(2);
    expect(view.host.querySelector('[data-testid="scene-style-window-text"]').textContent).toContain("只显示了这一窗的前 80 段");
    expect(view.host.querySelector('[role="alert"]')).toBeNull();
  });

  it("scnHydrateFromBackend：把 notices 与 style_windows 记到运行记录，并随 scnRunSave 持久化", async () => {
    const { mod, client } = await loadSceneRun({ projects: [NON_DEMO_PROJECT] });
    const baseGet = client.apiGet.getMockImplementation();
    client.apiGet.mockImplementation((url, options) => {
      if (url === "/api/v1/scenes/s1/workbench") {
        return Promise.resolve({
          style_draft: { content: "潮水退去。\n她留下了证词。" },
          scene_run_state: { scene_status: "near_final" },
          generation_summary: { draft_mode: "style_first", notices: NOTICES, style_windows: STYLE_WINDOWS },
        });
      }
      return baseGet(url, options);
    });
    const restored = await mod.scnHydrateFromBackend("ch01s1", {});
    expect(restored).toBeTruthy();
    expect(restored.styleNotices.map((n) => n.code)).toEqual(["STYLE_FIRST_DRAFT", "STYLE_INJECTION_DEGRADED", "STYLE_PLAGIARISM_HIT", "STYLE_SOMETHING_NEW"]);
    expect(restored.styleWindows.bookId).toBe("book-1");
    expect(restored.styleWindows.windows.length).toBe(2);
    mod.scnRunSave("ch01s1", restored);
    const reloaded = mod.scnRunLoad("ch01s1");
    expect(reloaded.styleNotices.map((n) => n.code)).toEqual(restored.styleNotices.map((n) => n.code));
    expect(reloaded.styleWindows).toEqual(restored.styleWindows);

    // 没有 notices / 窗口的运行：空列表与 null，不是 undefined（场景页据此清掉上一轮的提示）
    client.apiGet.mockImplementation((url, options) => {
      if (url === "/api/v1/scenes/s1/workbench") {
        return Promise.resolve({
          neutral_draft: { content: "潮水退去。" },
          scene_run_state: { scene_status: "neutral_running" },
          generation_summary: { draft_mode: "neutral_first", notices: [], style_windows: null },
        });
      }
      return baseGet(url, options);
    });
    const plain = await mod.scnHydrateFromBackend("ch01s1", {});
    expect(plain.styleNotices).toEqual([]);
    expect(plain.styleWindows).toBeNull();
  });

  it("scnRun：运行记录带 styleNotices / styleWindows，运行日志写一行提示摘要", async () => {
    const { mod, client } = await loadSceneRun();
    const baseGet = client.apiGet.getMockImplementation();
    client.apiPost.mockImplementation((url) => {
      if (/\/api\/v1\/scenes\/s1\/run\/jobs$/.test(url)) {
        return Promise.resolve({ job_id: "job-wp4", scene_id: "s1", status: "running" });
      }
      return Promise.resolve({});
    });
    client.apiGet.mockImplementation((url) => {
      if (url === "/api/v1/run-jobs/job-wp4") {
        return Promise.resolve({ job_id: "job-wp4", scene_id: "s1", status: "completed", current_step: "near_final" });
      }
      if (url === "/api/v1/scenes/s1/workbench") {
        return Promise.resolve({
          style_draft: { content: "潮水退去。\n她留下了证词。" },
          scene_run_state: { scene_status: "near_final" },
          generation_summary: { draft_mode: "style_first", notices: NOTICES.slice(0, 2), style_windows: STYLE_WINDOWS },
        });
      }
      return baseGet(url);
    });
    vi.useFakeTimers();
    let result;
    try {
      const runPromise = mod.scnRun({ sid: "ch01s1", kind: "主动场景" }, "", "", {});
      await vi.runAllTimersAsync();
      result = await runPromise;
    } finally {
      vi.useRealTimers();
    }
    expect(result.styleNotices.map((n) => n.code)).toEqual(["STYLE_FIRST_DRAFT", "STYLE_INJECTION_DEGRADED"]);
    expect(result.styleWindows.windows.length).toBe(2);
    const texts = result.log.map((l) => l.text);
    expect(texts.some((t) => t.includes("风格提示 2 条") && t.includes(mod.STYLE_NOTICE_LABELS.STYLE_INJECTION_DEGRADED))).toBe(true);
    expect(texts.some((t) => t.includes("参考书原文窗口 2 个") && t.includes("5310 字"))).toBe(true);
  });

  it("真实场景页：从 workbench 恢复的风格提示渲染成提示条，本场参考窗口进证据栏并可展开原文", async () => {
    const { mod, client } = await loadSceneRun({ projects: [NON_DEMO_PROJECT] });
    await queueSceneIntent({ sid: "ch01s1" });
    client.getLatestSceneRunJob.mockResolvedValue({
      job_id: "job-page-wp4",
      scene_id: "s1",
      status: "running",
      current_step: "style_running",
    });
    const baseGet = client.apiGet.getMockImplementation();
    client.apiGet.mockImplementation((url, options) => {
      if (url === "/api/v1/scenes/s1/workbench") {
        return Promise.resolve({
          neutral_draft: { content: "潮水退去。\n她留下了证词。" },
          scene_run_state: { scene_status: "style_running" },
          generation_summary: { draft_mode: "style_first", notices: NOTICES.slice(0, 3), style_windows: STYLE_WINDOWS },
        });
      }
      return baseGet(url, options);
    });
    const calls = routeParagraphs(client, ({ bookId, start, end }) => Promise.resolve({
      book_id: bookId, start, end, capped: false,
      paragraphs: [{ paragraph_index: start, paragraph_type: "narration", text: "页面里展开的参考原文。" }],
    }));
    const page = await import("./ws-scene.jsx");
    const view = await renderRunJobControl(page.WsScene, { go: vi.fn(), t: {} });
    await vi.waitFor(() => {
      expect(view.host.querySelector('[data-testid="scene-run-job-control"]')?.dataset.jobId).toBe("job-page-wp4");
      expect(view.host.querySelectorAll('[data-testid="scene-style-notices"] li').length).toBe(3);
      expect(view.host.querySelectorAll('[data-testid="scene-style-window-row"]').length).toBe(2);
    }, T);
    const strip = view.host.querySelector('[data-testid="scene-style-notices"]');
    expect(strip.textContent).toContain(mod.STYLE_NOTICE_LABELS.STYLE_PLAGIARISM_HIT);
    expect(strip.querySelector("li.sev-error")).not.toBeNull();
    await click(view.host.querySelector('[data-testid="scene-style-window-row"] button'));
    await vi.waitFor(() => {
      expect(view.host.querySelector('[data-testid="scene-style-window-text"]')?.textContent).toContain("页面里展开的参考原文。");
    }, T);
    expect(calls).toEqual(["/api/v2/style-reference/books/book-1/paragraphs?start=0&end=59"]);
  });
});

/* ---- 2026-09-23 风格参考 v3 · P6b：像不像 ----
   起草台证据栏的「像不像」：这次运行一步步量出来的位次与决定（首稿 → 风格步 → 修改稿 → 补丁 → 终稿）、评审最弱的几维、
   和作者不一样的地方、对照检查；风格步 / 补丁的新提示码按 reason 说白话；v3 的参考窗口带梗概与标签。合成数据。 */
describe("像不像（风格参考 v3）", () => {
  beforeEach(() => {
    vi.resetModules();
    window.localStorage.clear();
  });

  afterEach(async () => {
    while (mountedRoots.length) {
      const { root, host } = mountedRoots.pop();
      await act(async () => root.unmount());
      host.remove();
    }
    vi.restoreAllMocks();
  });

  const READ = (over = {}) => ({
    reading_id: "r-first", percentile: 91.2, within_range: false, reliable: true, max_percentile: 90, char_count: 1800,
    emphasized_dimensions: [], excluded_dimensions: [],
    out_of_band: [{ feature: "fw_modal_per_1k", dimension: "language.vocabulary", dimension_label: "词汇选择", direction: "low", phrase: "语气词比作者少", z: -2.4 }],
    dimension_scores: { "language.vocabulary": 5.9 },
    ...over,
  });
  const RUN_FIDELITY = {
    first_draft: READ(),
    style_step: {
      kind: "style_step", decision: "revision_kept", reason: "closer_to_author", dimensions: ["language.vocabulary"],
      dimension_labels: ["词汇选择"], first_percentile: 91.2, revision_percentile: 60.4, bundle_id: "b1",
    },
    revision: READ({ reading_id: "r-rev", percentile: 60.4, within_range: true, out_of_band: [] }),
    patch: { kind: "patch", decision: "reverted", reason: "judge_worse", bundle_id: "b1" },
    final: READ({ reading_id: "r-final", percentile: 58, within_range: true, out_of_band: [{ feature: "punct_dash_per_1k", dimension: "language.punctuation", dimension_label: "标点节奏", direction: "high", phrase: "破折号比作者多", z: 2.2 }] }),
    judge: { overall: 6.8, dimensions: { "scene.dialogue": { score: 5.5, note: "对白偏正式" }, "language.rhetoric": { score: 8, note: "" } } },
  };

  it("新的提示码：风格步 / 补丁的决定按 reason 说白话；参考书的状态四条都认", async () => {
    const { mod } = await loadSceneRun();
    const notices = mod.scnStyleNoticesFrom({
      generation_summary: {
        notices: [
          { code: "STYLE_FIRST_DRAFT_ACCEPTED", severity: "info", reason: "within_author_range", message: "首稿读数在参考作者的正常范围内，风格步没有再调模型" },
          { code: "STYLE_FIRST_DRAFT_ACCEPTED", severity: "info", reason: "reading_unreliable", message: "…" },
          { code: "STYLE_REVISION_REJECTED", severity: "info", reason: "not_closer", message: "定向修改没有让稿子更像参考作者（读数没有变近）" },
          { code: "STYLE_REVISION_REJECTED", severity: "warning", reason: "revision_template_missing", message: "…sync_prompt_templates…" },
          { code: "STYLE_PATCH_REVERTED", severity: "info", reason: "judge_worse", message: "软 QC 的补丁让稿子离参考作者更远" },
          { code: "STYLE_REFERENCE_SAMPLES_BLOCKED", severity: "warning", message: "云端策略不允许" },
          { code: "STYLE_REFERENCE_BOOK_CHANGED", severity: "warning", message: "…" },
          { code: "STYLE_REFERENCE_BOOK_MISSING", severity: "warning", message: "…" },
          { code: "STYLE_REFERENCE_NO_WINDOWS", severity: "warning", message: "…" },
          { code: "STYLE_DRAFT_FALLBACK_NEUTRAL", severity: "warning", draft_mode: "style_first", message: "…" },
        ],
      },
    });
    expect(notices[0].reason).toBe("within_author_range");
    expect(notices[9].draftMode).toBe("style_first");
    const view = await renderRunJobControl(mod.SceneStyleNoticeStrip, { notices });
    const rows = Array.from(view.host.querySelectorAll('[data-testid="scene-style-notices"] li'));
    const text = (i) => rows[i].textContent;
    expect(text(0)).toContain("首稿已在作者范围内，没有再改");
    expect(text(0)).toContain("量下来首稿已经像这位作者");
    expect(text(1)).toContain("首稿量不准，没有再改");
    expect(text(2)).toContain("改了一版没有更像，保留首稿");
    expect(text(3)).toContain("缺「定向修改」模板");
    expect(rows[3].className).toContain("sev-warning");
    expect(text(4)).toContain("补丁没有更像，已退回");
    expect(text(4)).toContain("参考评审分降了");
    expect(text(5)).toContain("这本书的原文不能发给当前模型");
    expect(text(6)).toContain("参考书学完后改过");
    expect(text(7)).toContain("参考书已不在书库里");
    expect(text(8)).toContain("参考书还没有样例片段");
    expect(text(9)).toContain("风格稿没过检查，用回了首稿");
    const all = view.host.textContent;
    expect(all).not.toMatch(/风格步|读数尺子|软 QC|sync_prompt_templates|STYLE_/);
  });

  it("本场参考窗口：v3 的窗带一句话梗概与标签（配额、场面 / 情绪、手法、以哪种段落为主）", async () => {
    const { mod } = await loadSceneRun();
    const styleWindows = mod.scnStyleWindowsFrom({
      generation_summary: {
        style_windows: {
          book_id: "book-1", profile_id: "profile-1", step: "neutral_draft",
          windows: [{
            start: 120, end: 179, chapter: 3, position: "opening", paragraph_type: "narration", paragraphs: 60, chars: 3820,
            window_no: 7, slot: "position", situations: ["开章引入"], moods: ["平静"], devices: ["留白"], gist: "某人在渡口等船",
          }],
        },
      },
    });
    expect(styleWindows.windows[0]).toMatchObject({ windowNo: 7, slot: "position", gist: "某人在渡口等船", situations: ["开章引入"], moods: ["平静"], devices: ["留白"] });
    const view = await renderRunJobControl(mod.SceneStyleWindowsPanel, { styleWindows });
    const row = view.host.querySelector('[data-testid="scene-style-window-row"]');
    expect(row.textContent).toContain("第 3 章 · 章首 · 第 121–180 段（3,820 字）");
    expect(row.querySelector('[data-testid="scene-style-window-gist"]').textContent).toBe("某人在渡口等船");
    for (const word of ["按章内位置挑", "开章引入", "平静", "留白", "叙述为主"]) expect(row.textContent).toContain(word);
    expect(view.host.textContent).toContain("（首稿）");
  });

  it("运行记录带这次运行的像不像，随 scnRunSave 持久化；运行日志写一行", async () => {
    const { mod, client } = await loadSceneRun();
    const baseGet = client.apiGet.getMockImplementation();
    client.apiPost.mockImplementation((url) => (/\/api\/v1\/scenes\/s1\/run\/jobs$/.test(url)
      ? Promise.resolve({ job_id: "job-fid", scene_id: "s1", status: "running" })
      : Promise.resolve({})));
    client.apiGet.mockImplementation((url) => {
      if (url === "/api/v1/run-jobs/job-fid") return Promise.resolve({ job_id: "job-fid", scene_id: "s1", status: "completed", current_step: "near_final" });
      if (url === "/api/v1/scenes/s1/workbench") {
        return Promise.resolve({
          style_draft: { content: "潮水退去。\n她留下了证词。" },
          scene_run_state: { scene_status: "near_final" },
          generation_summary: { draft_mode: "style_first", notices: [], style_windows: null, style_fidelity: RUN_FIDELITY },
        });
      }
      return baseGet(url);
    });
    vi.useFakeTimers();
    let result;
    try {
      const runPromise = mod.scnRun({ sid: "ch01s1", kind: "主动场景" }, "", "", {});
      await vi.runAllTimersAsync();
      result = await runPromise;
    } finally {
      vi.useRealTimers();
    }
    expect(result.styleFidelity.style_step.decision).toBe("revision_kept");
    const line = result.log.map((l) => l.text).find((t) => t.startsWith("像不像："));
    expect(line).toContain("首稿第 91 位（超出作者的正常范围）");
    expect(line).toContain("按 1 个维度（词汇选择）定向修改并采用：第 91 位 → 第 60 位");
    expect(line).toContain("补丁没有更像");
    expect(line).toContain("终稿第 58 位（在作者的正常范围内）");
    mod.scnRunSave("ch01s1", result);
    expect(mod.scnRunLoad("ch01s1").styleFidelity.final.reading_id).toBe("r-final");
  });

  it("证据栏「像不像」：这次运行的一串、评审最弱的几维、和作者不一样的地方；对照检查跑一次并显示结果", async () => {
    const { client } = await loadSceneRun();
    const scenePayload = { scene_id: "s1", bound: true, readings: { manual: null }, decisions: [], judge: null };
    const baseGet = client.apiGet.getMockImplementation();
    let checkStatus = "running";
    client.apiGet.mockImplementation((url) => {
      if (url === "/api/v1/scenes/s1/style-fidelity") return Promise.resolve(scenePayload);
      if (url === "/api/v2/style-reference/checks/job-chk") {
        return Promise.resolve(checkStatus === "running"
          ? { job: { job_id: "job-chk", kind: "check", status: "running", phase: "judge" }, reading: null }
          : { job: { job_id: "job-chk", kind: "check", status: "succeeded" }, reading: { ...READ({ reading_id: "r-man", percentile: 44, within_range: true, stage: "manual" }), judge: { overall: 7.4, summary: "对白再松一点就更像", dimensions: {} } } });
      }
      return baseGet(url);
    });
    const basePost = client.apiPost.getMockImplementation();
    const posted = [];
    client.apiPost.mockImplementation((url, body, opts) => {
      if (url === "/api/v2/style-reference/checks") {
        posted.push(body);
        return Promise.resolve({ job_id: "job-chk", state: "queued", job: { job_id: "job-chk", kind: "check", status: "queued" } });
      }
      return basePost(url, body, opts);
    });
    const { SceneFidelityPanel } = await import("./ws-scene-fidelity.jsx");
    const view = await renderRunJobControl(SceneFidelityPanel, { sceneId: "s1", runFidelity: RUN_FIDELITY, go: vi.fn() });
    await vi.waitFor(() => expect(view.host.querySelector('[data-testid="scene-fidelity"]')).not.toBeNull(), T);
    const panel = view.host.querySelector('[data-testid="scene-fidelity"]');
    expect(panel.querySelector('[data-testid="scene-fidelity-first"]').textContent).toContain("第 91 位");
    expect(panel.querySelector('[data-testid="scene-fidelity-first"]').textContent).toContain("超出作者的正常范围");
    expect(panel.querySelector('[data-testid="scene-fidelity-step"]').textContent).toContain("按 1 个维度（词汇选择）定向修改并采用：第 91 位 → 第 60 位");
    expect(panel.querySelector('[data-testid="scene-fidelity-revision"]').textContent).toContain("第 60 位");
    expect(panel.querySelector('[data-testid="scene-fidelity-patch"]').textContent).toContain("补丁没有更像（参考评审分降了），已退回补丁前的稿子");
    expect(panel.querySelector('[data-testid="scene-fidelity-final"]').textContent).toContain("在作者的正常范围内");
    const judge = panel.querySelector('[data-testid="scene-fidelity-judge"]');
    expect(judge.textContent).toContain("6.8");
    expect(judge.textContent).toContain("对话写法");
    expect(judge.textContent).toContain("对白偏正式");
    expect(panel.querySelector('[data-testid="scene-fidelity-gaps"]').textContent).toContain("和作者不一样的地方（终稿）");
    expect(panel.querySelector('[data-testid="scene-fidelity-gaps"]').textContent).toContain("破折号比作者多");
    expect(panel.textContent).not.toMatch(/2\.2|-2\.4/);
    expect(panel.querySelector('[data-testid="scene-fidelity-check"]').textContent).toContain("还没对这一场做过对照检查");

    await click(panel.querySelector('[data-testid="scene-fidelity-check-run"]'));
    expect(posted).toEqual([{ scene_id: "s1" }]);
    expect(panel.querySelector('[data-testid="scene-fidelity-check-run"]').disabled).toBe(true);
    checkStatus = "succeeded";
    scenePayload.readings = { manual: { ...READ({ reading_id: "r-man", percentile: 44, within_range: true, stage: "manual" }), judge: { overall: 7.4, summary: "对白再松一点就更像", dimensions: {} } } };
    await vi.waitFor(() => {
      const manual = view.host.querySelector('[data-testid="scene-fidelity-manual"]');
      expect(manual).not.toBeNull();
      expect(manual.textContent).toContain("第 44 位");
      expect(manual.textContent).toContain("7.4");
    }, { timeout: 6000, interval: 50 });
  });

  it("没绑定、也没有这次运行的读数：整块不出现", async () => {
    const { client } = await loadSceneRun();
    const baseGet = client.apiGet.getMockImplementation();
    client.apiGet.mockImplementation((url) => (url === "/api/v1/scenes/s1/style-fidelity"
      ? Promise.resolve({ scene_id: "s1", bound: false, readings: {}, decisions: [], judge: null })
      : baseGet(url)));
    const { SceneFidelityPanel } = await import("./ws-scene-fidelity.jsx");
    const view = await renderRunJobControl(SceneFidelityPanel, { sceneId: "s1", runFidelity: null });
    await vi.waitFor(() => expect(client.apiGet.mock.calls.some(([url]) => url === "/api/v1/scenes/s1/style-fidelity")).toBe(true), T);
    await act(async () => { await Promise.resolve(); });
    expect(view.host.querySelector('[data-testid="scene-fidelity"]')).toBeNull();
  });

  it("对照检查被拒（没有模型）：说成中文并给「去设置模型」", async () => {
    const { client } = await loadSceneRun();
    const baseGet = client.apiGet.getMockImplementation();
    client.apiGet.mockImplementation((url) => (url === "/api/v1/scenes/s1/style-fidelity"
      ? Promise.resolve({ scene_id: "s1", bound: true, readings: {}, decisions: [], judge: null })
      : baseGet(url)));
    const basePost = client.apiPost.getMockImplementation();
    client.apiPost.mockImplementation((url, body, opts) => (url === "/api/v2/style-reference/checks"
      ? Promise.reject(Object.assign(new Error("llm required"), { code: "STYLE_REFERENCE_LLM_REQUIRED", status: 409 }))
      : basePost(url, body, opts)));
    const go = vi.fn();
    const { SceneFidelityPanel } = await import("./ws-scene-fidelity.jsx");
    const view = await renderRunJobControl(SceneFidelityPanel, { sceneId: "s1", runFidelity: null, go });
    await vi.waitFor(() => expect(view.host.querySelector('[data-testid="scene-fidelity-check-run"]')).not.toBeNull(), T);
    expect(view.host.textContent).toContain("这一场还没有读数");
    await click(view.host.querySelector('[data-testid="scene-fidelity-check-run"]'));
    await vi.waitFor(() => expect(view.host.querySelector('[data-testid="scene-fidelity-check-error"]')).not.toBeNull(), T);
    expect(view.host.querySelector('[data-testid="scene-fidelity-check-error"]').textContent).toContain("对照检查要由模型对着原文样例评审");
    await click(view.host.querySelector('[data-testid="scene-fidelity-check-error-action"]'));
    expect(go).toHaveBeenCalledWith("settings", { type: "ws:settings-tab", detail: "ai" });
  });

  it("采用时被抄袭门拦下：说有几处与参考书原文相同、稿子已保留，不给英文原话", async () => {
    const { copyGateAdoptMessage, isCopyGateError } = await import("./ws-copy-gate.js");
    const error = Object.assign(new Error("reference copy gate blocked adoption — draft is kept and can be revised"), {
      code: "SOURCE_SAFETY_BLOCKED",
      details: { reference_copy: { blocked: true, hit_count: 2, hits: [{ start: 10, end: 24 }, { start: 90, end: 104 }], protected_hit_count: 1, protected_hits: [{ start: 3, end: 5 }] } },
    });
    expect(isCopyGateError(error)).toBe(true);
    const message = copyGateAdoptMessage(error);
    expect(message).toBe("这一稿里有 2 处与参考书原文连续相同、1 处用了参考书里的专名，不能采用：改写这些地方（或退回重写）之后再采用。稿子已保留，写作台的正文没有改动。");
    expect(isCopyGateError(Object.assign(new Error("x"), { code: "SOURCE_SAFETY_BLOCKED", details: {} }))).toBe(false);
  });
});

/* ---- 2026-09-21 起草台重排：裁决只认后端、进度只认后端、一个轮询者 ----
   过去台面上有一套前端自己的「质检」（短句率 55%、划红线、「通过 · 有风险」）和永远是 0/4 的
   「戏剧卡对齐」；进度条按 700ms 计时器走到 92%，scnRun 与任务控制条各自每 2 秒问一次同一个任务；
   候选终选暂停被画成红色的「硬问题」；「送写作台深改」把作者送到一张空白页。 */
describe("AI 起草台 · 只认后端（2026-09-21）", () => {
  beforeEach(() => {
    vi.resetModules();
    window.localStorage.clear();
  });

  afterEach(async () => {
    while (mountedRoots.length) {
      const { root, host } = mountedRoots.pop();
      await act(async () => root.unmount());
      host.remove();
    }
    const { clearViewIntents } = await import("./ws-view-intents.js");
    clearViewIntents("scene");
    vi.restoreAllMocks();
  });

  const NOT_FOUND = () => Object.assign(new Error("not found"), { status: 404, code: "RUN_JOB_NOT_FOUND" });
  const readyRecord = (mod, gate, text = "潮水退去，她留下了证词。") => ({
    ...mod.scnQC([{ id: "p1", text }]),
    state: "ready", attempt: 1, attempts: [], cost: [], log: [], gate,
  });

  it("scnQC 只切段、数字数：没有短句率、风险划线和戏剧卡对齐", async () => {
    const { mod } = await loadSceneRun();
    const qc = mod.scnQC([{ id: "p1", text: "她推门。" }, { id: "p2", text: "雨下了一整夜，街上没有一个人。" }]);
    expect(qc.words).toBe(19);
    expect(qc.draft.map(p => p.parts.map(x => x.text).join(""))).toEqual(["她推门。", "雨下了一整夜，街上没有一个人。"]);
    expect(qc.draft.every(p => p.parts.every(part => !part.risk))).toBe(true);
    expect(qc).not.toHaveProperty("metrics");
    expect(qc).not.toHaveProperty("alignment");
    expect(qc.verdict).toEqual({ words: 19 });
    expect(mod.scnSetQcThresholds).toBeUndefined();
    expect(mod.scnPickList).toBeUndefined();
  });

  it("scnRunRecordFromWorkbench：起草与恢复共用一份——终稿优先、段落与字数、预算断点一律不可归档", async () => {
    const { mod } = await loadSceneRun();
    const wb = {
      final_scene: { content: "终稿第一段。\n\n终稿第二段。" },
      style_draft: { content: "风格稿不该被读到。" },
      scene_run_state: { scene_status: "near_final", lifecycle_budget: { recommended_topup_tokens: 5000 } },
      author_state: { author_state: "draft_ready", can_archive: true },
      hard_qc_summary: { rewrite_brief: ["把结尾收得更短"] },
      generation_summary: { draft_mode: "style_first" },
    };
    const plain = mod.scnRunRecordFromWorkbench(wb, { authorNote: "保留对白" });
    expect(plain.draft.map(p => p.parts[0].text)).toEqual(["终稿第一段。", "终稿第二段。"]);
    expect(plain.words).toBe(12);
    expect(plain).toMatchObject({
      pipeState: "near_final", authorNote: "保留对白", rewriteBrief: "把结尾收得更短",
      draftMode: "style_first", budgetBlock: null, gate: { authorState: "draft_ready", canArchive: true },
    });
    // 调用方给的管线状态优先（scnRun 在 workbench 没有 scene_run_state 时退回任务状态）
    expect(mod.scnRunRecordFromWorkbench({}, { pipeState: "completed" }).pipeState).toBe("completed");
    const blocked = mod.scnRunRecordFromWorkbench(wb, { job: { error_code: "LLM_SCENE_TOKEN_BUDGET_EXHAUSTED", current_step: "style_running" } });
    expect(blocked.budgetBlock).toMatchObject({ topup: { extra_tokens: 5000 }, currentStep: "style_running" });
    expect(blocked.gate).toMatchObject({ authorState: "draft_ready", canArchive: false, blockReason: "lifecycle_budget" });
    // 没有投影也照样不可归档
    expect(mod.scnRunRecordFromWorkbench({}, { job: { error_code: "LLM_BUSINESS_ATTEMPT_BUDGET_EXHAUSTED" } }).gate)
      .toMatchObject({ authorState: null, canArchive: false, blockReason: "lifecycle_budget" });
  });

  it("scnRunStageIndex：后端 token 映射到七步，硬质检的 rewrite_required 不会被当成近终稿", async () => {
    const { mod } = await loadSceneRun();
    expect(mod.RUN_STAGES.map(s => s.name)).toEqual(["预检", "首稿", "硬质检", "风格稿", "软质检", "近终稿", "归档"]);
    expect(mod.scnRunStageIndex("bundle_built")).toBe(0);
    expect(mod.scnRunStageIndex("neutral_running")).toBe(1);
    expect(mod.scnRunStageIndex("hard_qc_partial_rewrite_required")).toBe(2);
    expect(mod.scnRunStageIndex("style_running")).toBe(3);
    expect(mod.scnRunStageIndex("awaiting_candidate_selection")).toBe(3);
    expect(mod.scnRunStageIndex("soft_qc_patch_required")).toBe(4);
    expect(mod.scnRunStageIndex("rewrite_running")).toBe(5);
    expect(mod.scnRunStageIndex("near_final")).toBe(5);
    expect(mod.scnRunStageIndex("archived")).toBe(6);
    expect(mod.scnRunStageIndex("human_review_required")).toBe(-1);
    expect(mod.scnRunStageIndex("")).toBe(-1);
  });

  it("待复核稿的裁决来自后端 author_state：质量建议显示条数与说明，不显示本地读数与英文 issue_key", async () => {
    const { mod, client } = await loadSceneRun({ projects: [NON_DEMO_PROJECT] });
    mod.scnRunSave("ch01s1", readyRecord(mod, mod.scnGateFrom({ author_state: {
      author_state: "quality_warning", can_archive: true,
      quality_warnings: [
        { issue_key: "dialogue_ratio_high", message: "对白比例偏高，可以删两句" },
        { issue_key: "soft_qc_payload_invalid", message: "soft QC payload validation failed: 5 validation errors for SoftQCOutput" },
      ],
    } })));
    await queueSceneIntent({ sid: "ch01s1" });
    client.getLatestSceneRunJob.mockRejectedValue(NOT_FOUND());
    const page = await import("./ws-scene.jsx");
    const view = await renderRunJobControl(page.WsScene, { go: vi.fn(), t: {} });

    await vi.waitFor(() => expect(view.host.querySelector('[data-testid="scene-archive"]')).toBeTruthy(), T);
    expect(view.host.querySelector(".scn2-verdict-badge")?.textContent).toBe("2 条建议");
    expect(view.host.querySelector('[data-testid="scene-archive"]').disabled).toBe(false);
    expect(view.host.querySelector(".scn2-gate-list")?.textContent).toBe("对白比例偏高，可以删两句");
    // 原样透出的英文异常不当作一句建议摆出来：折叠在「查看原文」里
    expect(view.host.querySelector(".scn2-gate-raw summary")?.textContent).toContain("1 条只有后端原始信息");
    expect(view.host.querySelector(".scn2-gate-raw pre")?.textContent).toContain("validation failed");
    // 退回重写的快捷短语只取中文改法
    await click([...view.host.querySelectorAll("button")].find(b => b.textContent.includes("退回重写")));
    expect([...view.host.querySelectorAll(".scn2-rework-chip")].map(c => c.textContent)).toEqual(["对白比例偏高，可以删两句"]);
    const text = view.host.textContent;
    expect(text).not.toContain("短句率");
    expect(text).not.toContain("戏剧卡");
    expect(text).not.toContain("dialogue_ratio_high");
    expect(text).not.toMatch(/Claude|Sonnet|Haiku/);
    expect(view.host.querySelector(".scn2-risk, .scn2-beat-tab, .scn2-meter")).toBeNull();
  });

  it("头部不再有一键「重跑」；退回重写不写指令时就是「直接重跑」，照常提交", async () => {
    const { mod, client } = await loadSceneRun({ projects: [NON_DEMO_PROJECT] });
    mod.scnRunSave("ch01s1", readyRecord(mod, null));
    await queueSceneIntent({ sid: "ch01s1" });
    client.getLatestSceneRunJob.mockRejectedValue(NOT_FOUND());
    const basePost = client.apiPost.getMockImplementation();
    client.apiPost.mockImplementation((url, body, options = {}) => {
      if (url === "/api/v1/scenes/s1/run/jobs") {
        return new Promise((resolve, reject) => {
          void resolve;
          options.signal?.addEventListener("abort", () => reject(new DOMException("aborted", "AbortError")), { once: true });
        });
      }
      return basePost(url, body, options);
    });
    const page = await import("./ws-scene.jsx");
    const view = await renderRunJobControl(page.WsScene, { go: vi.fn(), t: {} });
    await vi.waitFor(() => expect(view.host.querySelector('[data-testid="scene-archive"]')).toBeTruthy(), T);
    expect([...view.host.querySelectorAll("button")].some(b => b.textContent.trim() === "重跑")).toBe(false);

    await click([...view.host.querySelectorAll("button")].find(b => b.textContent.includes("退回重写")));
    const submit = [...view.host.querySelectorAll(".scn2-rework button")].find(b => b.textContent.includes("直接重跑"));
    expect(submit.disabled).toBe(false);
    await click(submit);
    await vi.waitFor(() => expect(client.apiPost).toHaveBeenCalledWith(
      "/api/v1/scenes/s1/run/jobs", { run_policy: "strict" }, expect.objectContaining({ signal: expect.anything() }),
    ), T);
  });

  it("已证实的硬问题：红色提示给出重写入口、归档禁用；没附说明的条目只报条数", async () => {
    const { mod, client } = await loadSceneRun({ projects: [NON_DEMO_PROJECT] });
    mod.scnRunSave("ch01s1", readyRecord(mod, mod.scnGateFrom({ author_state: {
      author_state: "hard_blocked", can_archive: false,
      blocking_findings: [{ issue_key: "missing_required_text", quality_level: "Q1" }],
    } })));
    await queueSceneIntent({ sid: "ch01s1" });
    client.getLatestSceneRunJob.mockRejectedValue(NOT_FOUND());
    const page = await import("./ws-scene.jsx");
    const view = await renderRunJobControl(page.WsScene, { go: vi.fn(), t: {} });

    await vi.waitFor(() => expect(view.host.querySelector('[data-testid="scene-hard-rewrite"]')).toBeTruthy(), T);
    expect(view.host.querySelector('[data-testid="scene-archive"]').disabled).toBe(true);
    expect(view.host.querySelector('[data-testid="scene-gate"]')?.textContent).toContain("另有 1 条没有附说明");
    expect(view.host.textContent).not.toContain("missing_required_text");
  });

  it("候选终选暂停不是硬问题：没有红色阻断、没有重写与归档按钮，台面是盲选页签", async () => {
    const { client } = await loadSceneRun({ projects: [NON_DEMO_PROJECT] });
    await queueSceneIntent({ sid: "ch01s1" });
    client.getLatestSceneRunJob.mockRejectedValue(NOT_FOUND());
    const baseGet = client.apiGet.getMockImplementation();
    client.apiGet.mockImplementation((url, options) => {
      if (url === "/api/v1/scenes/s1/workbench") {
        return Promise.resolve({
          style_draft: { content: "候选之一。" },
          scene_run_state: { scene_status: "awaiting_candidate_selection" },
          author_state: { author_state: "awaiting_author_choice", can_archive: false, blocking_findings: [], recommended_actions: ["select_candidate"] },
        });
      }
      if (url === "/api/v1/scenes/s1/style-candidates") {
        return Promise.resolve({ candidates: [
          { row_id: "cand-x", content: "第一份候选。\n它有两段。" },
          { row_id: "cand-y", content: "第二份候选。" },
          { row_id: "cand-z", content: "第三份。" },
        ] });
      }
      return baseGet(url, options);
    });
    const page = await import("./ws-scene.jsx");
    const view = await renderRunJobControl(page.WsScene, { go: vi.fn(), t: {} });

    await vi.waitFor(() => expect(view.host.querySelector('[data-testid="scene-candidate-select"]')).toBeTruthy(), T);
    expect(view.host.querySelector('[data-testid="scene-awaiting-choice"]')?.textContent).toContain("等你终选一稿");
    expect(view.host.querySelector('[data-testid="scene-hard-rewrite"]')).toBeNull();
    expect(view.host.querySelector('[data-testid="scene-archive"]')).toBeNull();
    expect(view.host.textContent).not.toContain("硬问题");
    const tabs = [...view.host.querySelectorAll('.scn2-pick-tabs [role="tab"]')];
    expect(tabs.map(tab => tab.textContent)).toEqual(["候选 A11 字", "候选 B6 字", "候选 C4 字"]);
    expect(view.host.querySelector('[data-testid="scene-candidate-select"]').dataset.candidateRowId).toBe("cand-x");
    await click(tabs[2]);
    expect(view.host.querySelector('[data-testid="scene-candidate-select"]').dataset.candidateRowId).toBe("cand-z");
    expect(view.host.querySelector('[data-testid="scene-candidate-tie"]')?.textContent).toContain("各稿无明显差异");
  });

  it("「存为候选并去写作台」：稿进「同步与恢复」、不归档不覆盖，然后打开写作台这一场和那份候选", async () => {
    const { mod, client } = await loadSceneRun({ projects: [NON_DEMO_PROJECT] });
    mod.scnRunSave("ch01s1", readyRecord(mod, mod.scnGateFrom({ author_state: { author_state: "draft_ready", can_archive: true } }), "交给写作台的那一稿。"));
    await queueSceneIntent({ sid: "ch01s1" });
    client.getLatestSceneRunJob.mockRejectedValue(NOT_FOUND());
    const opened = vi.fn();
    window.addEventListener("ws:recovery-open", opened);
    const go = vi.fn();
    const page = await import("./ws-scene.jsx");
    const view = await renderRunJobControl(page.WsScene, { go, t: {} });
    try {
      await vi.waitFor(() => expect(view.host.querySelector('[data-testid="scene-archive"]')).toBeTruthy(), T);
      expect(view.host.textContent).not.toContain("送写作台深改");
      const button = [...view.host.querySelectorAll("button")].find(b => b.textContent.includes("存为候选并去写作台"));
      await click(button);

      await vi.waitFor(() => expect(go).toHaveBeenCalledWith("writer", [{ type: "ws:writer-scene", detail: "ch01s1" }]), T);
      const candidates = window.WrRecovery.list().filter(item => item.type === "candidate");
      expect(candidates).toHaveLength(1);
      expect(candidates[0].html).toContain("交给写作台的那一稿。");
      expect(opened).toHaveBeenCalledTimes(1);
      expect(opened.mock.calls[0][0].detail).toEqual({ id: candidates[0].id });
      expect(client.apiPost.mock.calls.filter(([url]) => /adopt-current/.test(url))).toEqual([]);
    } finally {
      window.removeEventListener("ws:recovery-open", opened);
    }
  });

  it("归档后的深改走 go 的意图握手，不再 setTimeout 派发", async () => {
    const { mod, client } = await loadSceneRun({ projects: [NON_DEMO_PROJECT] });
    mod.scnRunSave("ch01s1", { ...readyRecord(mod, null), state: "archived" });
    await queueSceneIntent({ sid: "ch01s1" });
    client.getLatestSceneRunJob.mockRejectedValue(NOT_FOUND());
    const go = vi.fn();
    const page = await import("./ws-scene.jsx");
    const view = await renderRunJobControl(page.WsScene, { go, t: {} });
    await vi.waitFor(() => expect([...view.host.querySelectorAll("button")].some(b => b.textContent.includes("在写作台深改"))).toBe(true), T);
    await click([...view.host.querySelectorAll("button")].find(b => b.textContent.includes("在写作台深改")));
    expect(go).toHaveBeenCalledWith("writer", [
      { type: "ws:writer-scene", detail: "ch01s1" },
      { type: "ws:writer-posture", detail: "deep" },
    ]);
    // 从后端恢复的归档场没有归档时刻：副标题不再是一句悬空的「已写回 ·」
    expect(view.host.querySelector(".scn2-head-sub")?.textContent).toBe("已写入写作台正文");
  });

  it("进度条只看后端 current_step：运行在硬质检时，前两步打勾、硬质检是当前步", async () => {
    const { client } = await loadSceneRun({ projects: [NON_DEMO_PROJECT] });
    await queueSceneIntent({ sid: "ch01s1" });
    client.getLatestSceneRunJob.mockResolvedValue({ job_id: "job-hard", scene_id: "s1", status: "running", current_step: "hard_qc_running" });
    const page = await import("./ws-scene.jsx");
    const view = await renderRunJobControl(page.WsScene, { go: vi.fn(), t: {} });

    await vi.waitFor(() => expect(view.host.querySelector('.scn2-pstep[aria-current="step"]')?.textContent).toContain("硬质检"), T);
    const steps = [...view.host.querySelectorAll(".scn2-pstep")];
    expect(steps.map(step => step.className.match(/s-(\w+)/)[1])).toEqual(["done", "done", "active", "todo", "todo", "todo", "todo"]);
    expect(view.host.querySelector(".scn2-run")).toBeTruthy();
    expect(view.host.textContent).not.toMatch(/\d+%/);
  });

  it("一个轮询者：开始起草后 scnRun 不再自己问 run-jobs/{id}，由任务控制条的终态兑现", async () => {
    const { client } = await loadSceneRun({ projects: [NON_DEMO_PROJECT] });
    await queueSceneIntent({ sid: "ch01s1" });
    let created = false;
    let polls = 0;
    client.getLatestSceneRunJob.mockImplementation(() => {
      if (!created) return Promise.reject(NOT_FOUND());
      polls += 1;
      return Promise.resolve({ job_id: "job-one", scene_id: "s1", status: polls >= 1 ? "completed" : "running", current_step: "near_final" });
    });
    const basePost = client.apiPost.getMockImplementation();
    client.apiPost.mockImplementation((url, body, options) => {
      if (url === "/api/v1/scenes/s1/run/jobs") { created = true; return Promise.resolve({ job_id: "job-one", scene_id: "s1", status: "running", current_step: "queued" }); }
      return basePost(url, body, options);
    });
    const baseGet = client.apiGet.getMockImplementation();
    client.apiGet.mockImplementation((url, options) => {
      if (url === "/api/v1/scenes/s1/workbench") {
        return Promise.resolve(created
          ? { style_draft: { content: "一个轮询者写出的稿。" }, scene_run_state: { scene_status: "near_final" }, author_state: { author_state: "draft_ready", can_archive: true } }
          : { scene_run_state: { scene_status: "ready" } });
      }
      return baseGet(url, options);
    });
    const page = await import("./ws-scene.jsx");
    const view = await renderRunJobControl(page.WsScene, { go: vi.fn(), t: {} });
    await vi.waitFor(() => expect(view.host.querySelector('[data-testid="scene-start"]')).toBeTruthy(), T);

    await click(view.host.querySelector('[data-testid="scene-start"]'));

    await vi.waitFor(() => expect(view.host.querySelector('[data-testid="scene-archive"]')).toBeTruthy(), { timeout: 6000, interval: 50 });
    expect(view.host.querySelector(".scn2-draft")?.textContent).toContain("一个轮询者写出的稿。");
    expect(client.apiGet.mock.calls.filter(([url]) => /\/api\/v1\/run-jobs\//.test(url))).toEqual([]);
    // 管线停在近终稿：前六步打勾，归档还等作者
    const steps = [...view.host.querySelectorAll(".scn2-pstep")].map(step => step.className.match(/s-(\w+)/)[1]);
    expect(steps).toEqual(["done", "done", "done", "done", "done", "done", "todo"]);
  }, 10000);

  it("控制条报来的是同一场的另一个任务（另一个标签页又起了一次）：startRun 退回自己盯住自己的任务，不卡在运行中", async () => {
    const { client } = await loadSceneRun({ projects: [NON_DEMO_PROJECT] });
    await queueSceneIntent({ sid: "ch01s1" });
    let created = false;
    client.getLatestSceneRunJob.mockImplementation(() => (created
      ? Promise.resolve({ job_id: "job-other-tab", scene_id: "s1", status: "completed", current_step: "near_final" })
      : Promise.reject(NOT_FOUND())));
    const basePost = client.apiPost.getMockImplementation();
    client.apiPost.mockImplementation((url, body, options) => {
      if (url === "/api/v1/scenes/s1/run/jobs") { created = true; return Promise.resolve({ job_id: "job-mine", scene_id: "s1", status: "running" }); }
      return basePost(url, body, options);
    });
    const baseGet = client.apiGet.getMockImplementation();
    client.apiGet.mockImplementation((url, options) => {
      if (url === "/api/v1/run-jobs/job-mine") return Promise.resolve({ job_id: "job-mine", scene_id: "s1", status: "completed", current_step: "near_final" });
      if (url === "/api/v1/scenes/s1/workbench") {
        return Promise.resolve(created
          ? { style_draft: { content: "我自己的这一稿。" }, scene_run_state: { scene_status: "near_final" }, author_state: { author_state: "draft_ready", can_archive: true } }
          : { scene_run_state: { scene_status: "ready" } });
      }
      return baseGet(url, options);
    });
    const page = await import("./ws-scene.jsx");
    const view = await renderRunJobControl(page.WsScene, { go: vi.fn(), t: {} });
    await vi.waitFor(() => expect(view.host.querySelector('[data-testid="scene-start"]')).toBeTruthy(), T);

    await click(view.host.querySelector('[data-testid="scene-start"]'));

    await vi.waitFor(() => expect(view.host.querySelector('[data-testid="scene-archive"]')).toBeTruthy(), { timeout: 9000, interval: 50 });
    expect(client.apiGet.mock.calls.some(([url]) => url === "/api/v1/run-jobs/job-mine")).toBe(true);
    expect(view.host.querySelector(".scn2-draft")?.textContent).toContain("我自己的这一稿。");
  }, 14000);

  it("scnRun 没传 waitForTerminal 时自己轮询到终态，且没有 5 分钟客户端时限", async () => {
    const { mod, client } = await loadSceneRun();
    client.apiPost.mockImplementation((url) => (
      /\/run\/jobs$/.test(url) ? Promise.resolve({ job_id: "job-long", scene_id: "s1", status: "running" }) : Promise.resolve({})
    ));
    let reads = 0;
    const baseGet = client.apiGet.getMockImplementation();
    client.apiGet.mockImplementation((url) => {
      if (url === "/api/v1/run-jobs/job-long") {
        reads += 1;
        return Promise.resolve({ job_id: "job-long", scene_id: "s1", status: reads < 200 ? "running" : "completed" });
      }
      if (url === "/api/v1/scenes/s1/workbench") return Promise.resolve({ style_draft: { content: "慢慢写完。" }, scene_run_state: { scene_status: "near_final" } });
      return baseGet(url);
    });
    vi.useFakeTimers();
    try {
      const pending = mod.scnRun({ sid: "ch01s1", kind: "主动场景" }, "", "");
      await vi.advanceTimersByTimeAsync(200 * 2000 + 10);   // ≈6.7 分钟
      const result = await pending;
      expect(result.state).toBe("ready");
      expect(result.pipeState).toBe("near_final");
      expect(reads).toBe(200);
    } finally {
      vi.useRealTimers();
    }
  });
});

/* ---- 2026-09-21 对抗复查：几条没有出口的路 ----
   候选取不到时台面只剩一句话；控制条没挂上时 startRun 永远等；待复核时的失败不出声；
   在办场在后端恢复完成前一律写「待起草」。 */
describe("AI 起草台 · 出口与真话（对抗复查）", () => {
  beforeEach(() => {
    vi.resetModules();
    window.localStorage.clear();
  });

  afterEach(async () => {
    while (mountedRoots.length) {
      const { root, host } = mountedRoots.pop();
      await act(async () => root.unmount());
      host.remove();
    }
    const { clearViewIntents } = await import("./ws-view-intents.js");
    clearViewIntents("scene");
    vi.restoreAllMocks();
  });

  const NOT_FOUND = () => Object.assign(new Error("not found"), { status: 404, code: "RUN_JOB_NOT_FOUND" });
  const hangingRunJobs = (client) => {
    const basePost = client.apiPost.getMockImplementation();
    client.apiPost.mockImplementation((url, body, options = {}) => {
      if (url === "/api/v1/scenes/s1/run/jobs") {
        return new Promise((resolve, reject) => {
          void resolve;
          options.signal?.addEventListener("abort", () => reject(new DOMException("aborted", "AbortError")), { once: true });
        });
      }
      return basePost(url, body, options);
    });
  };

  it("候选终选取不到候选稿：台面给出「重新取候选」和「重新起草这一场」，不是一句话的死胡同", async () => {
    const { client } = await loadSceneRun({ projects: [NON_DEMO_PROJECT] });
    await queueSceneIntent({ sid: "ch01s1" });
    client.getLatestSceneRunJob.mockRejectedValue(NOT_FOUND());
    let candidateReads = 0;
    const baseGet = client.apiGet.getMockImplementation();
    client.apiGet.mockImplementation((url, options) => {
      if (url === "/api/v1/scenes/s1/workbench") {
        return Promise.resolve({
          style_draft: { content: "候选之一。" },
          scene_run_state: { scene_status: "awaiting_candidate_selection" },
          author_state: { author_state: "awaiting_author_choice", can_archive: false, blocking_findings: [] },
        });
      }
      if (url === "/api/v1/scenes/s1/style-candidates") {
        candidateReads += 1;
        return candidateReads === 1 ? Promise.reject(new Error("network down")) : Promise.resolve({ candidates: [] });
      }
      return baseGet(url, options);
    });
    hangingRunJobs(client);
    const page = await import("./ws-scene.jsx");
    const view = await renderRunJobControl(page.WsScene, { go: vi.fn(), t: {} });

    await vi.waitFor(() => expect(view.host.querySelector('[data-testid="scene-candidate-empty"]')).toBeTruthy(), T);
    const empty = () => view.host.querySelector('[data-testid="scene-candidate-empty"]');
    expect(empty().textContent).toContain("候选稿没取回来");
    expect(empty().textContent).not.toContain("network down");
    // 裁决条在等终选时仍然没有重写 / 归档按钮——出口在台面上
    expect(view.host.querySelector('[data-testid="scene-hard-rewrite"]')).toBeNull();
    expect(view.host.querySelector('[data-testid="scene-archive"]')).toBeNull();

    await click(view.host.querySelector('[data-testid="scene-candidate-reload"]'));
    await vi.waitFor(() => expect(candidateReads).toBe(2), T);
    await vi.waitFor(() => expect(empty()?.textContent).toContain("后端没有给出可选的候选稿"), T);

    await click(view.host.querySelector('[data-testid="scene-candidate-rework"]'));
    await vi.waitFor(() => expect(client.apiPost).toHaveBeenCalledWith(
      "/api/v1/scenes/s1/run/jobs", { run_policy: "strict" }, expect.objectContaining({ signal: expect.anything() }),
    ), T);
  });

  it("页面还没解析出后端 scene id 就开始起草：控制条没挂上，startRun 自己盯住任务，不卡在运行中", async () => {
    const { client } = await loadSceneRun({ projects: [NON_DEMO_PROJECT] });
    await queueSceneIntent({ sid: "ch01s1" });
    client.getLatestSceneRunJob.mockRejectedValue(NOT_FOUND());
    const { WsCatalog } = await import("./ws-catalog.jsx");
    const realSceneId = WsCatalog.__backendSceneId;
    let synced = false;
    vi.spyOn(WsCatalog, "__backendSceneId").mockImplementation((sid) => (
      synced ? realSceneId.call(WsCatalog, sid) : Promise.resolve(null)
    ));
    let created = false;
    const basePost = client.apiPost.getMockImplementation();
    client.apiPost.mockImplementation((url, body, options) => {
      if (url === "/api/v1/scenes/s1/run/jobs") { created = true; return Promise.resolve({ job_id: "job-late", scene_id: "s1", status: "running" }); }
      return basePost(url, body, options);
    });
    const baseGet = client.apiGet.getMockImplementation();
    client.apiGet.mockImplementation((url, options) => {
      if (url === "/api/v1/run-jobs/job-late") return Promise.resolve({ job_id: "job-late", scene_id: "s1", status: "completed", current_step: "near_final" });
      if (url === "/api/v1/scenes/s1/workbench") {
        return Promise.resolve(created
          ? { style_draft: { content: "晚到的 id 也写完了。" }, scene_run_state: { scene_status: "near_final" }, author_state: { author_state: "draft_ready", can_archive: true } }
          : {});
      }
      return baseGet(url, options);
    });
    const page = await import("./ws-scene.jsx");
    const view = await renderRunJobControl(page.WsScene, { go: vi.fn(), t: {} });
    await vi.waitFor(() => expect(view.host.querySelector('[data-testid="scene-start"]')).toBeTruthy(), T);
    expect(view.host.querySelector('[data-testid="scene-run-job-control"]')).toBeNull();

    synced = true;
    await click(view.host.querySelector('[data-testid="scene-start"]'));

    // 不等控制条的宽限：解析出的 id 对不上时立刻退回自己轮询（2 秒一问）
    await vi.waitFor(() => expect(view.host.querySelector('[data-testid="scene-archive"]')).toBeTruthy(), { timeout: 5000, interval: 50 });
    expect(client.apiGet.mock.calls.some(([url]) => url === "/api/v1/run-jobs/job-late")).toBe(true);
    expect(view.host.querySelector(".scn2-draft")?.textContent).toContain("晚到的 id 也写完了。");
  }, 10000);

  it("待复核时的失败会说出来：追加预算没有可执行的量，台面上有一条红色说明而不是按钮闪一下", async () => {
    const { mod, client } = await loadSceneRun({ projects: [NON_DEMO_PROJECT] });
    mod.scnRunSave("ch01s1", {
      ...mod.scnQC([{ id: "p1", text: "潮水退去，她留下了证词。" }]),
      state: "ready", attempt: 1, attempts: [], cost: [], log: [],
      gate: { authorState: "draft_ready", canArchive: false, blocking: [], warnings: [], blockReason: "lifecycle_budget" },
      budgetBlock: { code: "LLM_SCENE_TOKEN_BUDGET_EXHAUSTED", label: "这一场的预算用完了", topup: {} },
    });
    await queueSceneIntent({ sid: "ch01s1" });
    client.getLatestSceneRunJob.mockRejectedValue(NOT_FOUND());
    const page = await import("./ws-scene.jsx");
    const view = await renderRunJobControl(page.WsScene, { go: vi.fn(), t: {} });
    await vi.waitFor(() => expect(view.host.querySelector('[data-testid="scene-budget-topup"]')).toBeTruthy(), T);
    expect(view.host.querySelector('[data-testid="scene-ready-error"]')).toBeNull();

    await click(view.host.querySelector('[data-testid="scene-budget-topup"]'));

    await vi.waitFor(() => expect(view.host.querySelector('[data-testid="scene-ready-error"]')?.textContent).toContain("没有给出可以追加的预算量"), T);
    expect(view.host.querySelector('[data-testid="scene-archive"]')).toBeTruthy();
    expect(client.apiPost.mock.calls.filter(([url]) => /budget\/topup/.test(url))).toEqual([]);
  });

  it("参考旧尝试的复盘意见重写：意见很长时截短，整条指令不超过 2000 字，照常提交", async () => {
    const { mod, client } = await loadSceneRun({ projects: [NON_DEMO_PROJECT] });
    const longVerdict = "作者改写指令：" + "把这一段收紧，".repeat(300);
    mod.scnRunSave("ch01s1", {
      ...mod.scnQC([{ id: "p1", text: "潮水退去，她留下了证词。" }]),
      state: "ready", attempt: 2, cost: [], log: [],
      gate: { authorState: "draft_ready", canArchive: true, blocking: [], warnings: [] },
      attempts: [
        { n: 2, time: "本次 · 待裁决", result: "待裁决", tone: "gold", note: "按指令改写" },
        { n: 1, time: "9/21 10:00", result: "退回重写", tone: "slate", note: "初稿", cmp: { verdict: longVerdict } },
      ],
    });
    await queueSceneIntent({ sid: "ch01s1" });
    client.getLatestSceneRunJob.mockRejectedValue(NOT_FOUND());
    hangingRunJobs(client);
    const page = await import("./ws-scene.jsx");
    const view = await renderRunJobControl(page.WsScene, { go: vi.fn(), t: {} });
    await vi.waitFor(() => expect(view.host.querySelector(".scn2-try-view")).toBeTruthy(), T);

    await click(view.host.querySelector(".scn2-try-view"));
    await click(view.host.querySelector('[data-testid="scene-attempt-rewrite"]'));

    await vi.waitFor(() => expect(client.apiPost.mock.calls.some(([url]) => url === "/api/v1/scenes/s1/run/jobs")).toBe(true), T);
    const [, body] = client.apiPost.mock.calls.find(([url]) => url === "/api/v1/scenes/s1/run/jobs");
    expect(Array.from(body.author_note).length).toBeLessThanOrEqual(2000);
    expect(Array.from(body.author_note).length).toBeGreaterThan(1900);
    expect(body.author_note).toMatch(/^参考第 1 次尝试的复盘意见重写（作者改写指令：把这一段收紧/);
    expect(body.author_note).toContain("…）；不恢复该版正文");
    expect(view.host.querySelector('[data-testid="scene-ready-error"]')).toBeNull();
  });

  it("窄屏证据抽屉：拉开时焦点进抽屉，收起后回到「证据」按钮；文本框里的 Esc 不收抽屉", async () => {
    const { mod, client } = await loadSceneRun({ projects: [NON_DEMO_PROJECT] });
    mod.scnRunSave("ch01s1", {
      ...mod.scnQC([{ id: "p1", text: "潮水退去，她留下了证词。" }]),
      state: "ready", attempt: 1, attempts: [], cost: [], log: [],
      gate: { authorState: "draft_ready", canArchive: true, blocking: [], warnings: [] },
    });
    await queueSceneIntent({ sid: "ch01s1" });
    client.getLatestSceneRunJob.mockRejectedValue(NOT_FOUND());
    const page = await import("./ws-scene.jsx");
    const view = await renderRunJobControl(page.WsScene, { go: vi.fn(), t: {} });
    await vi.waitFor(() => expect(view.host.querySelector(".scn2-evi-toggle")).toBeTruthy(), T);
    const toggle = view.host.querySelector(".scn2-evi-toggle");
    const drawer = () => view.host.querySelector("#scn2-evi");
    const esc = async (target) => {
      await act(async () => { target.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true })); });
    };

    toggle.focus();
    await click(toggle);
    expect(drawer().classList.contains("is-open")).toBe(true);
    expect(drawer().contains(document.activeElement)).toBe(true);

    // 退回重写的指令框里按 Esc：抽屉不动
    await click([...view.host.querySelectorAll(".scn2-decide-acts button")].find(b => b.textContent.includes("退回重写")));
    const note = view.host.querySelector(".scn2-rework-input");
    note.focus();
    await esc(note);
    expect(drawer().classList.contains("is-open")).toBe(true);
    // 输入法组字时的 Esc 也不算
    await act(async () => { document.body.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true, isComposing: true })); });
    expect(drawer().classList.contains("is-open")).toBe(true);

    drawer().querySelector(".scn2-evi-drawerhead button").focus();
    await esc(document.activeElement);
    expect(drawer().classList.contains("is-open")).toBe(false);
    expect(document.activeElement).toBe(view.host.querySelector(".scn2-evi-toggle"));
  });

  it("在办场还没有运行记录时，书脊按目录状态标：已完成的场不写「待起草」", async () => {
    const doneChap = {
      ...TWO_SCENE_CHAP,
      scenes: [TWO_SCENE_CHAP.scenes[0], { ...TWO_SCENE_CHAP.scenes[1], state: "done" }],
    };
    const { client } = await loadSceneRun({ projects: [NON_DEMO_PROJECT], catalog: [doneChap] });
    await queueSceneIntent({ sids: ["ch01s1", "ch01s2"] });
    client.getLatestSceneRunJob.mockRejectedValue(NOT_FOUND());
    const page = await import("./ws-scene.jsx");
    const view = await renderRunJobControl(page.WsScene, { go: vi.fn(), t: {} });
    const row = (sid) => view.host.querySelector(`[data-testid="scene-queue-item"][data-scene-sid="${sid}"]`);
    await vi.waitFor(() => expect(row("ch01s2")).toBeTruthy(), T);
    expect(row("ch01s2").querySelector(".scn2-chip")?.textContent).toBe("已完成");
    expect(row("ch01s1").querySelector(".scn2-chip")?.textContent).toBe("待起草");
  });
});
