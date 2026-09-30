import React, { act } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { installApiRouter } from "./test-helpers.js";

vi.mock("./lib/client.js", () => ({
  apiGet: vi.fn(), apiPost: vi.fn(), apiPatch: vi.fn(), apiDelete: vi.fn(),
}));
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

  /* 复核 W1-A1：恢复稿交给 WrDocs 之后（编辑器与本机缓存已经换成它）PATCH 断网失败，过去这里说「操作未完成」，
     可那一稿已经在本机、下一次保存就会同步上去——说的和发生的不一样 */
  it("重试同步时断网：恢复稿已在编辑器和本机缓存里，中心照实说「还没同步」（不说「操作未完成」），记录留着", async () => {
    const { WrRecovery, WrRecoveryCenter, WrDocs, client } = await loadRecovery();
    const entry = WrRecovery.create({ sid: "ch01s1", html: "<p>断网留下的正文。</p>", type: "unsynced", reason: "网络中断" });
    client.apiPatch.mockImplementation(() => Promise.reject(Object.assign(new Error("offline"), { code: "NETWORK_ERROR", retryable: true })));
    vi.spyOn(console, "warn").mockImplementation(() => {});
    const host = await renderCenter(WrRecoveryCenter);
    await click(host.querySelector(".wrr-trigger"));
    await click([...document.querySelectorAll('[role="dialog"] button')].find(button => button.textContent.includes("重试同步")));

    const live = () => document.querySelector('[role="dialog"] .wrr-live').textContent;
    await vi.waitFor(() => expect(live()).toContain("还没同步到服务端"), T);
    expect(live()).toContain("已恢复到编辑器");
    expect(live()).not.toContain("操作未完成");
    expect(WrRecovery.list().some(item => item.id === entry.id)).toBe(true);
    expect(WrDocs.cachedHTML("ch01s1")).toBe("<p>断网留下的正文。</p>");
  });

  /* 复核 W1-R1B-2：恢复稿还没发出去就被随后的一稿取代（排队只留最新一稿），存上的不是它——过去照样说「已同步、移出列表」 */
  it("重试同步时恢复稿被随后改过的一稿取代：记录留着，中心不说「移出恢复列表」", async () => {
    const { WrRecovery, WrRecoveryCenter, WrDocs, client } = await loadRecovery();
    const entry = WrRecovery.create({ sid: "ch01s1", html: "<p>断网留下的正文。</p>", type: "unsynced", reason: "网络中断" });
    let release;
    const hung = new Promise((resolve) => { release = resolve; });
    client.apiPatch
      .mockImplementationOnce(() => hung.then(() => ({ draft: { draft_id: "d1", revision_no: 2, content: "<p>起点正文</p>" } })))
      .mockImplementation((url, body) => Promise.resolve({ draft: { draft_id: "d1", revision_no: 3, content: body.content } }));
    void WrDocs.save("ch01s1", "<p>起点正文</p>").catch(() => {});        // 路上还有一次保存
    await vi.waitFor(() => expect(client.apiPatch).toHaveBeenCalledTimes(1), T);
    const host = await renderCenter(WrRecoveryCenter);
    await click(host.querySelector(".wrr-trigger"));
    await click([...document.querySelectorAll('[role="dialog"] button')].find(button => button.textContent.includes("重试同步")));
    await vi.waitFor(() => expect(WrDocs.cachedHTML("ch01s1")).toBe("<p>断网留下的正文。</p>"), T);
    void WrDocs.save("ch01s1", "<p>断网留下的正文。又改了一句。</p>").catch(() => {});   // 恢复稿发出去之前就被取代
    await act(async () => { release(); });

    const live = () => document.querySelector('[role="dialog"] .wrr-live').textContent;
    await vi.waitFor(() => expect(live()).toContain("这份记录先留着"), T);
    expect(live()).not.toContain("移出恢复列表");
    expect(WrRecovery.list().some(item => item.id === entry.id)).toBe(true);
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

/* 审计 F03-23：恢复记录是作者的安全网——从不自动淘汰；一场多到软上限时只提示导出或清理。列出时不再每次把每一条重解析一遍。 */
describe("恢复记录的索引与软上限", () => {
  it("列出只解析新的或变了的记录；别处写进来的、删掉的照样认得", async () => {
    const { WrRecovery } = await loadRecovery();
    const ids = [];
    for (let i = 0; i < 5; i += 1) ids.push(WrRecovery.create({ sid: "ch01s1", html: `<p>第 ${i} 份。</p>`, type: "backup" }).id);
    expect(WrRecovery.list()).toHaveLength(5);
    const parse = vi.spyOn(JSON, "parse");
    expect(WrRecovery.list()).toHaveLength(5);
    expect(parse).not.toHaveBeenCalled();                     // 以前：每次列出都把五条全文重解析一遍
    parse.mockRestore();

    // 另一个标签页直接写进来的一份：照样列出来；删掉的一份：不再列出，也按 id 找不到
    const outside = { id: "outside-1", version: 1, workId: "prj-main", sid: "ch01s1", type: "conflict", reason: "", label: "场景 ch01s1",
      source: "writer", createdAt: Date.now() + 1000, html: "<p>别处的一份。</p>", durable: true };
    window.localStorage.setItem("wr-recovery:v1:outside-1", JSON.stringify(outside));
    expect(WrRecovery.list()[0]).toMatchObject({ id: "outside-1", html: "<p>别处的一份。</p>" });
    expect(WrRecovery.remove(ids[0])).toBe(true);
    expect(WrRecovery.list().map((e) => e.id)).not.toContain(ids[0]);
    expect(WrRecovery.diff(ids[0])).toBeNull();
    expect(WrRecovery.diff("outside-1")).toMatchObject({ candidate: "<p>别处的一份。</p>" });
    // 列表给出去的是副本：改它不会改到下一次列出的内容
    WrRecovery.list()[0].html = "被改掉";
    expect(WrRecovery.list()[0].html).toBe("<p>别处的一份。</p>");
  });

  it("一场的恢复记录超过软上限：中心提示导出或清理、新记录的回执顺带说一句；一份都不替作者删", async () => {
    const { WrRecovery, WrRecoveryCenter, WsToastHost } = await loadRecovery();
    for (let i = 0; i < 20; i += 1) WrRecovery.create({ sid: "ch01s1", html: `<p>第 ${i} 份。</p>`, type: "backup" });
    const host = await renderCenter(() => <><WrRecoveryCenter /><WsToastHost /></>);
    await click(host.querySelector(".wrr-trigger"));
    expect(document.querySelector('[data-testid="recovery-crowded"]')).toBeNull();    // 20 份：还在软上限之内
    await click(document.querySelector(".wrr-close"));

    await act(async () => { WrRecovery.create({ sid: "ch01s1", html: "<p>第 21 份。</p>", type: "conflict" }); });
    const toast = document.querySelector('[data-testid="undo-toast"]');
    expect(toast.textContent).toContain("这一场已有 21 份恢复记录，打开后可导出或清理");
    expect(WrRecovery.list()).toHaveLength(21);                                          // 没有自动淘汰

    await click(host.querySelector(".wrr-trigger"));
    const notice = document.querySelector('[data-testid="recovery-crowded"]');
    expect(notice.textContent).toContain("《交班》 21 份");
    expect(notice.textContent).toContain("先导出再删除");
  });
});

