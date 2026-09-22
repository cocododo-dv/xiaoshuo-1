import React, { act } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { installApiRouter } from "./test-helpers.js";

vi.mock("./lib/client.js", () => ({
  apiGet: vi.fn(), apiPost: vi.fn(), apiPatch: vi.fn(), apiDelete: vi.fn(),
}));

globalThis.IS_REACT_ACT_ENVIRONMENT = true;
const T = { timeout: 5000, interval: 25 };
const mounted = [];

async function loadRecovery() {
  const client = await import("./lib/client.js");
  installApiRouter(client);
  client.apiPost.mockImplementation((url) => {
    if (/\/author-drafts\/scene\/.+\/ensure$/.test(url)) {
      return Promise.resolve({ draft: { draft_id: "d1", revision_no: 1, content: "" } });
    }
    return Promise.resolve({});
  });
  client.apiPatch.mockImplementation((url, body) => Promise.resolve({
    draft: { draft_id: "d1", revision_no: 2, content: body.content },
  }));
  await import("./ws-catalog.jsx");
  await vi.waitFor(() => expect(window.WsWorks && window.WsWorks.activeId()).toBe("prj-main"), T);
  await vi.waitFor(() => expect(window.WsCatalog.get().length).toBeGreaterThan(0), T);
  const store = await import("./wr-doc-store.jsx");
  const ui = await import("./wr-recovery-center.jsx");
  const notify = await import("./ws-notify.jsx");
  return { ...store, ...ui, ...notify, client };
}

async function renderCenter(Component) {
  const host = document.createElement("div");
  document.body.appendChild(host);
  const root = createRoot(host);
  mounted.push({ root, host });
  await act(async () => root.render(<Component />));
  return host;
}

async function click(node) {
  await act(async () => node.dispatchEvent(new MouseEvent("click", { bubbles: true })));
}

beforeEach(() => {
  vi.resetModules();
  window.localStorage.clear();
  vi.spyOn(window, "confirm").mockReturnValue(true);
});

afterEach(async () => {
  while (mounted.length) {
    const { root, host } = mounted.pop();
    await act(async () => root.unmount());
    host.remove();
  }
  vi.restoreAllMocks();
});

describe("同步与恢复中心", () => {
  it("冲突/候选可发现、可看差异、可复制，Esc 关闭后焦点回到入口", async () => {
    const { WrRecovery, WrRecoveryCenter } = await loadRecovery();
    window.localStorage.setItem(window.wsKey("wr-doc:ch01s1"), "<p>作者当前正文。</p>");
    WrRecovery.createCandidate("ch01s1", "<p>AI 候选正文。</p>");
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(window.navigator, "clipboard", { configurable: true, value: { writeText } });

    const host = await renderCenter(WrRecoveryCenter);
    const trigger = host.querySelector(".wrr-trigger");
    expect(trigger.getAttribute("aria-label")).toContain("有 1 份恢复记录");
    expect(host.querySelector('[role="dialog"]')).toBeNull();
    await click(trigger);

    // 对话框 portal 到 <body>（入口在侧栏里，侧栏的 overflow / backdrop-filter 会裁掉它）
    const dialog = document.querySelector('[role="dialog"]');
    expect(dialog).toBeTruthy();
    expect(host.contains(dialog)).toBe(false);
    expect(dialog.textContent).toContain("AI 候选正文");
    expect(dialog.textContent).toContain("作者当前正文");
    await vi.waitFor(() => expect(document.activeElement?.getAttribute("aria-label")).toBe("关闭同步与恢复中心"), T);
    // 一组动作只有一个实心主按钮：候选稿的主动作是「恢复为当前草稿」
    const byLabel = (text) => [...dialog.querySelectorAll(".wrr-actions button")].find(button => button.textContent.includes(text));
    expect(byLabel("恢复为当前草稿").classList.contains("btn-accent")).toBe(true);
    expect(byLabel("重试同步").classList.contains("btn-accent")).toBe(false);
    expect(dialog.querySelectorAll(".wrr-actions .btn-accent, .wrr-actions .btn-primary")).toHaveLength(1);

    await click([...dialog.querySelectorAll("button")].find(button => button.textContent.includes("复制正文")));
    expect(writeText).toHaveBeenCalledWith("AI 候选正文。");

    await act(async () => document.activeElement.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true })));
    expect(document.querySelector('[role="dialog"]')).toBeNull();
    await vi.waitFor(() => expect(document.activeElement).toBe(trigger), T);
  });

  it("重试同步走 author-drafts 乐观并发保存，成功后从恢复列表移除", async () => {
    const { WrRecovery, WrRecoveryCenter, client } = await loadRecovery();
    WrRecovery.create({ sid: "ch01s1", html: "<p>断网留下的正文。</p>", type: "unsynced", reason: "网络中断" });
    const host = await renderCenter(WrRecoveryCenter);
    await click(host.querySelector(".wrr-trigger"));
    const retry = [...document.querySelectorAll('[role="dialog"] button')].find(button => button.textContent.includes("重试同步"));
    // 还没同步上去的稿件：主动作是「重试同步」
    expect(retry.classList.contains("btn-accent")).toBe(true);
    expect(document.querySelectorAll('[role="dialog"] .wrr-actions .btn-accent, [role="dialog"] .wrr-actions .btn-primary')).toHaveLength(1);
    await click(retry);

    await vi.waitFor(() => expect(client.apiPatch).toHaveBeenCalledWith(
      "/api/v1/author-drafts/d1",
      { content: "<p>断网留下的正文。</p>", base_revision_no: 1 },
    ), T);
    await vi.waitFor(() => expect(WrRecovery.list()).toEqual([]), T);
    expect(document.querySelector('[role="dialog"]').textContent).toContain("没有待恢复稿件");
    // 空了只说一次（详情区），列表栏不再重复一个空态
    expect(document.querySelector('[role="dialog"]').textContent.match(/没有待恢复稿件/g)).toHaveLength(1);
    expect(host.querySelector(".wrr-trigger").getAttribute("aria-label")).toBe("打开同步与恢复中心");
  });

  it("ws:recovery-open 从任何页面直接打开并选中那份记录", async () => {
    const { WrRecovery, WrRecoveryCenter } = await loadRecovery();
    WrRecovery.create({ sid: "ch01s1", html: "<p>较早的一份。</p>", type: "unsynced", reason: "网络中断", label: "较早记录" });
    const target = WrRecovery.create({ sid: "ch01s2", html: "<p>要看的这一份。</p>", type: "conflict", reason: "冲突", label: "目标记录" });
    await renderCenter(WrRecoveryCenter);

    await act(async () => window.dispatchEvent(new CustomEvent("ws:recovery-open", { detail: { id: target.id } })));
    const dialog = document.querySelector('[role="dialog"]');
    expect(dialog).toBeTruthy();
    expect(dialog.querySelector(".wrr-row.is-active").textContent).toContain("目标记录");
    expect(dialog.querySelector(".wrr-detail h3").textContent).toBe("目标记录");
  });

  it("新记录落进来时经外壳提示层给一条带「打开」的回执；中心开着时不打扰", async () => {
    const { WrRecovery, WrRecoveryCenter, WsToastHost } = await loadRecovery();
    // App 里提示层和侧栏入口各挂一次；这里一起渲染
    await renderCenter(() => <><WrRecoveryCenter /><WsToastHost /></>);
    expect(document.querySelector('[data-testid="undo-toast"]')).toBeNull();

    await act(async () => { WrRecovery.createCandidate("ch01s1", "<p>新到的候选。</p>"); });
    const toast = document.querySelector('[data-testid="undo-toast"]');
    expect(toast).toBeTruthy();
    expect(toast.textContent).toContain("已放入「同步与恢复」");
    expect(toast.textContent).not.toContain("ch01s1");

    await click(toast.querySelector('[data-testid="undo-toast-action"]'));
    expect(document.querySelector('[role="dialog"]')).toBeTruthy();
    expect(document.querySelector('[data-testid="undo-toast"]')).toBeNull();

    await act(async () => { WrRecovery.create({ sid: "ch01s1", html: "<p>开着时又来一份。</p>", type: "backup" }); });
    expect(document.querySelector('[data-testid="undo-toast"]')).toBeNull();
  });

  it("提示层挂着时，删除记录走应用内确认框（不弹浏览器 confirm）；取消就什么都不动", async () => {
    const { WrRecovery, WrRecoveryCenter, WsToastHost } = await loadRecovery();
    WrRecovery.create({ sid: "ch01s1", html: "<p>留着的一份。</p>", type: "unsynced", reason: "网络中断" });
    const host = await renderCenter(() => <><WrRecoveryCenter /><WsToastHost /></>);
    await click(host.querySelector(".wrr-trigger"));
    const remove = () => [...document.querySelectorAll(".wrr-panel button")].find(button => button.textContent.includes("删除"));

    await click(remove());
    const confirmBox = document.querySelector('[data-testid="ws-confirm"]');
    expect(confirmBox).toBeTruthy();
    expect(confirmBox.textContent).toContain("永久删除这份恢复记录？");
    expect(window.confirm).not.toHaveBeenCalled();
    await click(document.querySelector('[data-testid="ws-confirm-cancel"]'));
    expect(WrRecovery.list()).toHaveLength(1);
    // 确认框关掉后，恢复中心还开着
    expect(document.querySelector(".wrr-panel")).toBeTruthy();

    await click(remove());
    await click(document.querySelector('[data-testid="ws-confirm-ok"]'));
    await vi.waitFor(() => expect(WrRecovery.list()).toEqual([]), T);
  });
});
