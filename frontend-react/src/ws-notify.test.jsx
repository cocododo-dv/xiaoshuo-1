// 外壳提示 / 确认层（ws-notify.jsx）单测：
// 提示层挂着时 storeAlert 变成应用内提示条、wsConfirm 是应用内确认框（Esc = 取消、排队一次一个）；
// 没挂时退回浏览器的 alert / confirm——约二十个 store 单测靠监视 window.alert 断言失败路径。
import React, { act } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

globalThis.IS_REACT_ACT_ENVIRONMENT = true;

let host;
let root;

async function load() {
  const notify = await import("./ws-notify.jsx");
  const utils = await import("./lib/store-utils.js");
  return { ...notify, ...utils };
}

async function mountHost(WsToastHost) {
  host = document.createElement("div");
  document.body.appendChild(host);
  root = createRoot(host);
  await act(async () => root.render(<WsToastHost />));
}

beforeEach(() => {
  vi.resetModules();
});

afterEach(async () => {
  if (root) await act(async () => root.unmount());
  if (host) host.remove();
  root = null;
  host = null;
  vi.useRealTimers();
  vi.restoreAllMocks();
});

describe("没有挂提示层时", () => {
  it("storeAlert 仍走 window.alert，wsConfirm 退回 window.confirm，wsToast 返回 false", async () => {
    const { storeAlert, wsConfirm, wsToast, wsNotifyReady } = await load();
    const alertSpy = vi.spyOn(window, "alert").mockImplementation(() => {});
    const confirmSpy = vi.spyOn(window, "confirm").mockReturnValue(false);
    expect(wsNotifyReady()).toBe(false);
    storeAlert(new Error("保存失败"), "兜底");
    expect(alertSpy).toHaveBeenCalledWith("保存失败");
    await expect(wsConfirm({ title: "删除？", body: "会进回收站" })).resolves.toBe(false);
    expect(confirmSpy).toHaveBeenCalledWith("删除？\n会进回收站");
    expect(wsToast({ message: "你好" })).toBe(false);
  });
});

describe("挂了提示层之后", () => {
  it("storeAlert 显示成应用内的提示条（role=alert），不弹浏览器 alert；卸载后恢复原样", async () => {
    const { storeAlert, WsToastHost } = await load();
    const alertSpy = vi.spyOn(window, "alert").mockImplementation(() => {});
    await mountHost(WsToastHost);
    await act(async () => { storeAlert(null, "章节目录尚未加载完成。"); });
    const toast = document.querySelector('[data-testid="undo-toast"]');
    expect(toast.textContent).toContain("章节目录尚未加载完成。");
    expect(toast.getAttribute("role")).toBe("alert");
    expect(alertSpy).not.toHaveBeenCalled();

    await act(async () => root.unmount());
    root = null;
    storeAlert(null, "卸载之后");
    expect(alertSpy).toHaveBeenCalledWith("卸载之后");
  });

  it("wsToast 带动作按钮，点了执行并收起；过时自动消失", async () => {
    const { wsToast, WsToastHost } = await load();
    await mountHost(WsToastHost);
    vi.useFakeTimers();
    const onClick = vi.fn();
    await act(async () => { expect(wsToast({ message: "已放入回收站", action: { label: "打开回收站", onClick } })).toBe(true); });
    await act(async () => { document.querySelector('[data-testid="undo-toast-action"]').click(); });
    expect(onClick).toHaveBeenCalledTimes(1);
    expect(document.querySelector('[data-testid="undo-toast"]')).toBeNull();

    await act(async () => { wsToast("第二条"); });
    expect(document.querySelector('[data-testid="undo-toast"]').getAttribute("role")).toBe("status");
    await act(async () => { vi.advanceTimersByTime(7000); });
    expect(document.querySelector('[data-testid="undo-toast"]')).toBeNull();
  });

  it("提示摞成一列：后来的提示不顶掉还没到时的失败提示（store 的失败提示过去是阻塞的 alert）", async () => {
    const { storeAlert, wsToast, WsToastHost } = await load();
    await mountHost(WsToastHost);
    vi.useFakeTimers();
    await act(async () => { storeAlert(null, "本机存储已满，这一版没有存下来。"); });
    await act(async () => { wsToast({ message: "已下载", tone: "ok" }); });
    const texts = () => [...document.querySelectorAll('[data-testid="undo-toast"]')].map((el) => el.querySelector(".ws-toast-text").textContent);
    expect(texts()).toEqual(["本机存储已满，这一版没有存下来。", "已下载"]);
    const stack = document.querySelector('[data-testid="ws-toast-stack"]');
    expect(stack.parentElement).toBe(document.body);
    expect(stack.querySelectorAll('[data-testid="undo-toast"]')).toHaveLength(2);

    // 普通提示 6 秒到时；失败提示照样留满自己的 9 秒
    await act(async () => { vi.advanceTimersByTime(7000); });
    expect(texts()).toEqual(["本机存储已满，这一版没有存下来。"]);
    await act(async () => { vi.advanceTimersByTime(2500); });
    expect(texts()).toEqual([]);
  });

  it("同一句话重复来只留一条；超过上限先让普通提示退场，失败提示留着", async () => {
    const { storeAlert, wsToast, WsToastHost } = await load();
    await mountHost(WsToastHost);
    const texts = () => [...document.querySelectorAll('[data-testid="undo-toast"]')].map((el) => el.querySelector(".ws-toast-text").textContent);
    await act(async () => { storeAlert(null, "保存失败"); storeAlert(null, "保存失败"); });
    expect(texts()).toEqual(["保存失败"]);
    await act(async () => { ["一", "二", "三", "四"].forEach((n) => wsToast(`回执${n}`)); });
    expect(texts()).toEqual(["保存失败", "回执二", "回执三", "回执四"]);
    // 关掉一条只关那一条
    await act(async () => { document.querySelectorAll(".ws-toast-x")[1].click(); });
    expect(texts()).toEqual(["保存失败", "回执三", "回执四"]);
  });

  it("视图自己的回执（UndoToast）在提示层挂着时进同一列，不和外壳提示叠在同一块像素上；没挂时就地渲染", async () => {
    const { storeAlert, WsToastHost } = await load();
    const { UndoToast } = await import("./ws-undo-toast.jsx");
    const viewHost = document.createElement("div");
    document.body.appendChild(viewHost);
    const viewRoot = createRoot(viewHost);
    const renderView = (text) => act(async () => viewRoot.render(
      <div className="view-under-test"><UndoToast toast={{ text, actionLabel: "撤销", onAction: () => {} }} onClose={() => {}} /></div>,
    ));
    try {
      await renderView("已移入回收站");
      // 提示层没挂：就地渲染在视图里
      expect(viewHost.querySelector('.view-under-test [data-testid="undo-toast"]')).toBeTruthy();

      await mountHost(WsToastHost);
      await act(async () => { storeAlert(null, "删除没有成功"); });
      const stack = document.querySelector('[data-testid="ws-toast-stack"]');
      const inStack = [...stack.querySelectorAll('[data-testid="undo-toast"]')].map((el) => el.querySelector(".ws-toast-text").textContent);
      expect(inStack).toEqual(expect.arrayContaining(["已移入回收站", "删除没有成功"]));
      expect(inStack).toHaveLength(2);
      expect(viewHost.querySelector('[data-testid="undo-toast"]')).toBeNull();
      // 视图回执上的「撤销」还在、点得到
      expect(stack.querySelector('[data-testid="undo-toast-action"]').textContent).toBe("撤销");

      // 提示层卸载：视图回执回到视图里，栈容器也从 <body> 摘掉
      await act(async () => root.unmount());
      root = null;
      expect(document.querySelector('[data-testid="ws-toast-stack"]')).toBeNull();
      expect(viewHost.querySelector('.view-under-test [data-testid="undo-toast"]')).toBeTruthy();
    } finally {
      await act(async () => viewRoot.unmount());
      viewHost.remove();
    }
  });

  it("wsConfirm 是应用内确认框：确认 → true，Esc → false，同时来两个就排队一次一个", async () => {
    const { wsConfirm, WsToastHost } = await load();
    const confirmSpy = vi.spyOn(window, "confirm").mockReturnValue(true);
    await mountHost(WsToastHost);

    let first;
    let second;
    await act(async () => {
      first = wsConfirm({ title: "第一问", body: "说明", confirmLabel: "好的" });
      second = wsConfirm({ title: "第二问", tone: "danger" });
    });
    const dialogs = () => [...document.querySelectorAll('[data-testid="ws-confirm"]')];
    expect(dialogs()).toHaveLength(1);
    expect(dialogs()[0].getAttribute("role")).toBe("dialog");
    expect(dialogs()[0].getAttribute("aria-modal")).toBe("true");
    expect(dialogs()[0].textContent).toContain("第一问");
    expect(document.querySelector('[data-testid="ws-confirm-ok"]').textContent).toBe("好的");
    await act(async () => { document.querySelector('[data-testid="ws-confirm-ok"]').click(); });
    await expect(first).resolves.toBe(true);

    expect(dialogs()).toHaveLength(1);
    expect(dialogs()[0].textContent).toContain("第二问");
    // 危险动作：初始焦点在「取消」
    expect(document.activeElement.getAttribute("data-testid")).toBe("ws-confirm-cancel");
    await act(async () => { document.activeElement.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true })); });
    await expect(second).resolves.toBe(false);
    expect(dialogs()).toHaveLength(0);
    expect(confirmSpy).not.toHaveBeenCalled();
  });

  it("提示层卸载时还没回答的确认按「取消」结算，调用方不会一直等", async () => {
    const { wsConfirm, WsToastHost } = await load();
    await mountHost(WsToastHost);
    let pending;
    await act(async () => { pending = wsConfirm("还在问"); });
    await act(async () => root.unmount());
    root = null;
    await expect(pending).resolves.toBe(false);
  });
});
