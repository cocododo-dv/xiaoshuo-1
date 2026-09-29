// AI 起草台运行队列的「移出 / 批量移出」契约。
// 队列是本地在办清单，但成员同时会从后端 scene-run-states 恢复（换浏览器、后台跑完的场
// 不该消失）。所以移出必须留下持久化的移出记号，否则下次进页面又被恢复回来——删除就成了假动作。
// 移出本身不碰场景卡与已生成的 AI 稿：这里也断言它从不调用任何软删端点。
import React, { act } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { DEFAULT_CHAP, DEFAULT_PROJECT, installApiRouter } from "./test-helpers.js";

vi.mock("./lib/client.js", () => ({
  apiGet: vi.fn(), apiPost: vi.fn(), apiPatch: vi.fn(), apiDelete: vi.fn(),
}));
// 任务控制条的两个请求（ws-scene-job-api.js）
vi.mock("./ws-scene-job-api.js", () => ({ cancelRunJob: vi.fn(), getLatestSceneRunJob: vi.fn() }));

const T = { timeout: 5000, interval: 25 };
const mounted = [];

const TWO_SCENE_CHAP = {
  ...DEFAULT_CHAP,
  scenes: [
    { ...DEFAULT_CHAP.scenes[0], slug: "ch01s1", scene_id: "s1", title: "交班" },
    { ...DEFAULT_CHAP.scenes[0], slug: "ch01s2", scene_id: "s2", title: "回潮" },
  ],
};
const RUN_STATES_URL = /^\/api\/v1\/scene-run-states\?/;

async function loadScene(opts = {}) {
  const clientModule = await import("./lib/client.js");
  installApiRouter(clientModule, { catalog: [TWO_SCENE_CHAP], ...opts });
  if (opts.runStateSceneIds) {
    const base = clientModule.apiGet.getMockImplementation();
    clientModule.apiGet.mockImplementation((url) => (
      RUN_STATES_URL.test(url)
        ? Promise.resolve({ items: opts.runStateSceneIds.map((id) => ({ scene_id: id })) })
        : base(url)
    ));
  }
  // 任务控制条的两个请求（mock）并进 client，测试照旧写 client.getLatestSceneRunJob
  const { cancelRunJob, getLatestSceneRunJob } = await import("./ws-scene-job-api.js");
  const client = { ...clientModule, cancelRunJob, getLatestSceneRunJob };
  client.getLatestSceneRunJob.mockRejectedValue(Object.assign(new Error("no job"), { status: 404 }));
  await import("./ws-catalog.jsx");
  await vi.waitFor(() => expect(window.WsWorks && window.WsWorks.activeId()).toBe("prj-main"), T);
  await vi.waitFor(() => expect(window.WsCatalog && window.WsCatalog.get().length).toBeGreaterThan(0), T);
  const mod = await import("./ws-scene.jsx");
  return { ...mod, client };
}

async function render(node) {
  const host = document.createElement("div");
  document.body.appendChild(host);
  const root = createRoot(host);
  mounted.push({ root, host });
  await act(async () => root.render(node));
  return host;
}

const click = (node) => act(async () => node.dispatchEvent(new MouseEvent("click", { bubbles: true })));
const rows = (host) => [...host.querySelectorAll('[data-testid="scene-queue-item"]')];
const dismissed = () => JSON.parse(window.localStorage.getItem("scn-queue-dismissed:v1::prj-main") || "[]");
async function queueSceneIntent(detail) {
  const { queueViewIntent } = await import("./ws-view-intents.js");
  queueViewIntent("scene", "ws:scene-enqueue", detail);
}

beforeEach(() => {
  vi.resetModules();
  window.localStorage.clear();
  vi.spyOn(window, "confirm").mockReturnValue(true);
  vi.spyOn(window, "alert").mockImplementation(() => {});
});

afterEach(async () => {
  while (mounted.length) {
    const { root, host } = mounted.pop();
    await act(async () => root.unmount());
    host.remove();
  }
  const { clearViewIntents } = await import("./ws-view-intents.js");
  clearViewIntents("scene");
  vi.restoreAllMocks();
});

describe("AI 起草台 · 进页面时取回在办场的运行记录（F03-11）", () => {
  it("一场的 workbench 慢：另一场照样去取，不排在它后面干等", async () => {
    window.localStorage.setItem("scn-queue:v1::prj-main", JSON.stringify(["ch01s1", "ch01s2"]));
    const { WsScene, client } = await loadScene();
    const base = client.apiGet.getMockImplementation();
    client.apiGet.mockImplementation((url, options) => (
      url === "/api/v1/scenes/s1/workbench" ? new Promise(() => {}) : base(url, options)
    ));
    await render(<WsScene t={{}} />);
    await vi.waitFor(() => {
      const urls = client.apiGet.mock.calls.map(([url]) => url);
      expect(urls).toContain("/api/v1/scenes/s1/workbench");
      expect(urls).toContain("/api/v1/scenes/s2/workbench");
    }, T);
  });
});

describe("AI 起草台 · 运行队列移出", () => {
  it("单条移出：不拦确认弹窗，直接移出并给回执，且从不调用场景软删端点", async () => {
    await queueSceneIntent({ sids: ["ch01s1", "ch01s2"] });
    const { WsScene, client } = await loadScene();
    const host = await render(<WsScene t={{}} />);
    await vi.waitFor(() => expect(rows(host)).toHaveLength(2), T);
    client.apiPost.mockClear();

    await click(host.querySelectorAll('[data-testid="scene-queue-remove"]')[0]);

    // 纯本地、可撤销的动作不该再拿 confirm 打断作者
    expect(window.confirm).not.toHaveBeenCalled();
    await vi.waitFor(() => expect(rows(host)).toHaveLength(1), T);
    expect(rows(host)[0].dataset.sceneSid).toBe("ch01s2");
    expect(dismissed()).toEqual(["ch01s1"]);
    // 回执说清了「稿还在」，并给出撤销
    const toast = host.querySelector('[data-testid="undo-toast"]');
    expect(toast.textContent).toContain("移出队列");
    expect(toast.querySelector('[data-testid="undo-toast-action"]').textContent).toContain("撤销");
    // 移出 ≠ 删除场景卡：任何 trash 端点都不该被碰
    expect(client.apiPost.mock.calls.some(([url]) => String(url).includes("/trash"))).toBe(false);
  });

  it("撤销把场原样放回队列并销掉移出记号", async () => {
    await queueSceneIntent({ sids: ["ch01s1", "ch01s2"] });
    const { WsScene } = await loadScene();
    const host = await render(<WsScene t={{}} />);
    await vi.waitFor(() => expect(rows(host)).toHaveLength(2), T);

    await click(host.querySelectorAll('[data-testid="scene-queue-remove"]')[0]);
    await vi.waitFor(() => expect(rows(host)).toHaveLength(1), T);
    expect(dismissed()).toEqual(["ch01s1"]);

    await click(host.querySelector('[data-testid="undo-toast-action"]'));

    await vi.waitFor(() => expect(rows(host)).toHaveLength(2), T);
    expect(rows(host).map((node) => node.dataset.sceneSid)).toEqual(["ch01s1", "ch01s2"]);
    expect(dismissed()).toEqual([]);          // 记号销掉，下次进页面不会再被挡
    expect(host.querySelector('[data-testid="undo-toast"]')).toBeNull();
  });

  it("多选批量移出：勾选的场一次清空队列，全部记名", async () => {
    await queueSceneIntent({ sids: ["ch01s1", "ch01s2"] });
    const { WsScene } = await loadScene();
    const host = await render(<WsScene t={{}} />);
    await vi.waitFor(() => expect(rows(host)).toHaveLength(2), T);

    await click(host.querySelector('[data-testid="scene-queue-select-mode"]'));
    const boxes = [...host.querySelectorAll(".scn2-qrow-check input")];
    expect(boxes).toHaveLength(2);
    await act(async () => { boxes[0].click(); });
    await act(async () => { boxes[1].click(); });
    await click(host.querySelector('[data-testid="scene-queue-batch-remove"]'));

    await vi.waitFor(() => expect(rows(host)).toHaveLength(0), T);
    expect(dismissed().sort()).toEqual(["ch01s1", "ch01s2"]);
  });

  it("已移出的场不会被后端 run-states 恢复回队列；重新入列即销名", async () => {
    window.localStorage.setItem("scn-queue-dismissed:v1::prj-main", JSON.stringify(["ch01s1"]));
    await queueSceneIntent({ sids: ["ch01s2"] });
    const { WsScene } = await loadScene({ runStateSceneIds: ["s1", "s2"] });
    const host = await render(<WsScene t={{}} />);

    await vi.waitFor(() => expect(rows(host)).toHaveLength(1), T);
    expect(rows(host)[0].dataset.sceneSid).toBe("ch01s2");

    // 重新入列（章节编排的「交给 AI」走同一事件）→ 销名并回到队列
    await act(async () => { window.dispatchEvent(new CustomEvent("ws:scene-enqueue", { detail: { sid: "ch01s1" } })); });
    await vi.waitFor(() => expect(rows(host)).toHaveLength(2), T);
    expect(dismissed()).toEqual([]);
  });

  it("运行中的场不许移出：拦下并说明先中止", async () => {
    await queueSceneIntent({ sids: ["ch01s1"] });
    window.localStorage.setItem("scn-run:ch01s1::prj-main", JSON.stringify({ state: "running", draft: [] }));
    const { WsScene } = await loadScene();
    const host = await render(<WsScene t={{}} />);
    await vi.waitFor(() => expect(rows(host)).toHaveLength(1), T);

    await click(host.querySelector('[data-testid="scene-queue-remove"]'));

    const toast = host.querySelector('[data-testid="undo-toast"]');
    expect(toast.textContent).toContain("正在运行");
    expect(toast.querySelector('[data-testid="undo-toast-action"]')).toBeNull();   // 没得撤销：本来就没动
    expect(rows(host)).toHaveLength(1);
    expect(dismissed()).toEqual([]);
  });
});
