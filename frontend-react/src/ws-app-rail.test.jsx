// 侧栏（Rail，ws-rail.jsx）渲染测（2026-09-21 外壳重构）：
// 断「可观测结果」——设置 / 回收站常驻底部、当前页 aria-current、同步与恢复入口在侧栏底部、
// 作家 / 高级是单选组而不是又一个导航项、悬停意图（不是一掠过就展开）、只有用键盘把焦点挪进来才展开
// （浮层关掉把焦点送回入口不算，哪怕在浮层里按过 Tab / 方向键）、
// 紧急待办的徽标、删除作品走应用内确认框（ws-notify.jsx）。
import React, { act } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { installApiRouter } from "./test-helpers.js";

vi.mock("./lib/client.js", () => ({
  apiGet: vi.fn(), apiPost: vi.fn(), apiPatch: vi.fn(), apiDelete: vi.fn(),
}));

globalThis.IS_REACT_ACT_ENVIRONMENT = true;
const T = { timeout: 5000, interval: 25 };

let host;
let root;

async function mountRail(props = {}, apiOpts = {}, { withNotify = false, withPalette = false } = {}) {
  const client = await import("./lib/client.js");
  installApiRouter(client, apiOpts);
  const { Rail } = await import("./ws-rail.jsx");
  const { WsToastHost } = await import("./ws-notify.jsx");
  const { WsPalette } = withPalette ? await import("./ws-palette.jsx") : { WsPalette: null };
  // 侧栏底部的同步与恢复是懒加载的；先把模块载好，挂载后在 act 里等它落定，免得 act 警告刷屏
  await import("./wr-recovery-center.jsx");
  const setTweak = props.setTweak || vi.fn();
  const go = props.go || vi.fn();
  const run = vi.fn();
  // 和 App 一样：侧栏的「快速跳转」打开命令面板，面板关掉时 WsDialog 把焦点还给它
  function Shell(next) {
    const [palette, setPalette] = React.useState(false);
    return (
      <>
        <Rail view="home" mode="writer" t={{ theme: "day" }} go={go} setTweak={setTweak}
          onPalette={withPalette ? () => setPalette(true) : vi.fn()} {...props} {...next} />
        {WsPalette ? <WsPalette open={palette} onClose={() => setPalette(false)} run={run} theme="day" /> : null}
        {withNotify ? <WsToastHost /> : null}
      </>
    );
  }
  host = document.createElement("div");
  document.body.appendChild(host);
  root = createRoot(host);
  const render = (next = {}) => root.render(<Shell {...next} />);
  await act(async () => render());
  await act(() => new Promise((resolve) => setTimeout(resolve, 30)));
  return { host, go, run, setTweak, rerender: (next) => act(async () => render(next)) };
}

/* 模拟一次鼠标点击：指针按下（侧栏据此认定不是键盘）→ 聚焦 → click */
async function pointerClick(el) {
  await act(async () => {
    el.dispatchEvent(new MouseEvent("pointerdown", { bubbles: true }));
    el.dispatchEvent(new MouseEvent("mousedown", { bubbles: true }));
    el.focus();
    el.click();
  });
}

const press = (el, key) => act(async () => { el.dispatchEvent(new KeyboardEvent("keydown", { key, bubbles: true })); });

const itemLabels = (scope) => [...scope.querySelectorAll(".ws-item .ws-item-label")].map(n => n.textContent);

beforeEach(() => {
  vi.resetModules();
  window.localStorage.clear();
});

afterEach(async () => {
  vi.useRealTimers();
  if (root) await act(async () => root.unmount());
  if (host) host.remove();
  root = null;
  host = null;
});

describe("侧栏 · 结构", () => {
  it("作家模式：日常写作在导航区，设置 / 回收站常驻底部，生产组不出现", async () => {
    const { host } = await mountRail();
    const nav = host.querySelector(".ws-nav-scroll");
    const foot = host.querySelector(".ws-rail-foot");
    expect(itemLabels(nav)).toEqual(["主页", "构思", "写作", "风格", "待办", "资料"]);
    expect(itemLabels(foot)).toEqual(["设置", "回收站"]);
    expect(host.textContent).not.toContain("AI 起草台");
  });

  it("高级模式：生产与运维组进入导航区，分组标题留在 DOM 里（折叠时只是一条线）", async () => {
    const { host } = await mountRail({ mode: "advanced", view: "scene" });
    const nav = host.querySelector(".ws-nav-scroll");
    expect(itemLabels(nav)).toEqual(["主页", "构思", "写作", "风格", "待办", "资料", "章节编排", "AI 起草台", "成稿中心", "文学质量", "成本看板"]);
    expect([...nav.querySelectorAll(".ws-nav-label")].map(n => n.textContent)).toEqual(["生产与质控", "运维工具"]);
    expect(itemLabels(host.querySelector(".ws-rail-foot"))).toEqual(["设置", "回收站"]);
  });

  it("当前页面只有一项带 aria-current=page", async () => {
    const { host, rerender } = await mountRail({ view: "library" });
    const current = host.querySelectorAll('[aria-current="page"]');
    expect(current.length).toBe(1);
    expect(current[0].textContent).toContain("资料");
    await rerender({ view: "trash" });
    expect([...host.querySelectorAll('[aria-current="page"]')].map(n => n.textContent)).toEqual(["回收站"]);
  });

  it("同步与恢复入口在侧栏底部（不再悬浮在右下角）", async () => {
    const { host } = await mountRail();
    await vi.waitFor(() => expect(host.querySelector(".ws-rail-foot button.wrr-trigger")).not.toBeNull(), T);
    const trigger = host.querySelector(".ws-rail-foot button.wrr-trigger");
    expect(trigger.getAttribute("aria-label")).toBe("打开同步与恢复中心");
    expect(trigger.classList.contains("ws-foot-btn")).toBe(true);
  });

  it("作家 / 高级是单选组，方向键切换模式", async () => {
    const { host, setTweak } = await mountRail();
    const group = host.querySelector('[role="radiogroup"][aria-label="界面模式"]');
    const radios = [...group.querySelectorAll('[role="radio"]')];
    expect(radios.map(r => [r.textContent, r.getAttribute("aria-checked")])).toEqual([["作家", "true"], ["高级", "false"]]);
    expect(group.closest(".ws-nav-scroll")).toBeNull();
    await act(async () => {
      radios[0].dispatchEvent(new KeyboardEvent("keydown", { key: "ArrowRight", bubbles: true }));
    });
    expect(setTweak).toHaveBeenCalledWith("mode", "advanced");
  });
});

describe("侧栏 · 展开与徽标", () => {
  it("悬停意图：指针掠过不立即展开，停留片刻才展开，离开即收起", async () => {
    const { host } = await mountRail();
    vi.useFakeTimers();
    const rail = host.querySelector(".ws-rail");
    await act(async () => { rail.dispatchEvent(new MouseEvent("pointerenter")); });
    expect(rail.classList.contains("is-expanded")).toBe(false);
    await act(async () => { vi.advanceTimersByTime(200); });
    expect(rail.classList.contains("is-expanded")).toBe(true);
    await act(async () => { rail.dispatchEvent(new MouseEvent("pointerleave")); });
    expect(rail.classList.contains("is-expanded")).toBe(false);

    // 一掠而过（不到悬停意图的时长就离开）不展开
    await act(async () => { rail.dispatchEvent(new MouseEvent("pointerenter")); });
    await act(async () => { vi.advanceTimersByTime(60); });
    await act(async () => { rail.dispatchEvent(new MouseEvent("pointerleave")); });
    await act(async () => { vi.advanceTimersByTime(300); });
    expect(rail.classList.contains("is-expanded")).toBe(false);
  });

  it("紧急待办：待办项带徽标，读屏名字里说清几条", async () => {
    const { host } = await mountRail({}, {
      reviewOpen: [{ id: "rv1", kind: "decision", title: "需要拍板", priority: 1 }, { id: "rv2", kind: "note", title: "普通", priority: 2 }],
    });
    await vi.waitFor(() => expect(host.querySelector(".ws-item-badge")).not.toBeNull(), T);
    const item = host.querySelector(".ws-item-badge").closest(".ws-item");
    expect(item.textContent).toContain("待办");
    expect(host.querySelector(".ws-item-badge").textContent).toBe("1");
    expect(item.getAttribute("aria-label")).toBe("待办，1 条紧急待办");
  });

  it("目录 / 雪花保存这类高频信号按 20 秒节流合并成一次补拉；作者动作（待办变化）照常即时刷新", async () => {
    await mountRail();
    const client = await import("./lib/client.js");
    const badgeFetches = () => client.apiGet.mock.calls.filter(([url]) => String(url).startsWith("/api/v1/review-items?state=open")).length;
    await vi.waitFor(() => expect(badgeFetches()).toBeGreaterThan(0), T);
    vi.useFakeTimers();
    const before = badgeFetches();
    await act(async () => {
      for (let i = 0; i < 5; i += 1) window.dispatchEvent(new CustomEvent("ws:catalog-changed"));
      window.dispatchEvent(new CustomEvent("ws:snow-saved"));
      vi.advanceTimersByTime(1000);
    });
    expect(badgeFetches()).toBe(before);
    await act(async () => { vi.advanceTimersByTime(20_000); });
    expect(badgeFetches()).toBe(before + 1);

    await act(async () => {
      window.dispatchEvent(new CustomEvent("ws:review-changed"));
      vi.advanceTimersByTime(200);
    });
    expect(badgeFetches()).toBe(before + 2);
  });
});

describe("侧栏 · 键盘焦点才展开", () => {
  it("用 Tab 把焦点移进侧栏时展开；鼠标点过留下的焦点不展开；焦点离开侧栏即收起", async () => {
    const { host } = await mountRail();
    const rail = host.querySelector(".ws-rail");
    const settings = [...host.querySelectorAll(".ws-item")].find(b => b.textContent.includes("设置"));

    await act(async () => {
      document.dispatchEvent(new MouseEvent("pointerdown", { bubbles: true }));
      settings.focus();
    });
    expect(document.activeElement).toBe(settings);
    expect(rail.classList.contains("is-expanded")).toBe(false);

    await act(async () => { settings.blur(); });
    await act(async () => {
      document.dispatchEvent(new KeyboardEvent("keydown", { key: "Tab", bubbles: true }));
      settings.focus();
    });
    expect(rail.classList.contains("is-expanded")).toBe(true);

    const outside = document.createElement("button");
    document.body.appendChild(outside);
    await act(async () => { outside.focus(); });
    expect(rail.classList.contains("is-expanded")).toBe(false);
    outside.remove();
  });
});

describe("侧栏 · 浮层把焦点还给入口时不展开（在浮层里按过键也一样）", () => {
  it("同步与恢复：鼠标打开，在对话框里按两下 Tab 再 Esc——焦点回到入口，侧栏不展开", async () => {
    const { host } = await mountRail();
    await vi.waitFor(() => expect(host.querySelector(".ws-rail-foot button.wrr-trigger")).not.toBeNull(), T);
    const rail = host.querySelector(".ws-rail");
    const trigger = host.querySelector(".ws-rail-foot button.wrr-trigger");

    await pointerClick(trigger);
    const dialog = document.querySelector(".wrr-panel");
    expect(dialog).not.toBeNull();
    expect(dialog.contains(document.activeElement)).toBe(true);
    await press(document.activeElement, "Tab");
    await press(document.activeElement, "Tab");
    await press(document.activeElement, "Escape");

    expect(document.querySelector(".wrr-panel")).toBeNull();
    expect(document.activeElement).toBe(trigger);
    expect(rail.classList.contains("is-expanded")).toBe(false);
  });

  it("命令面板：鼠标打开，↓ 选一项、回车跳过去——焦点回到「快速跳转」，侧栏不展开", async () => {
    const { host, run } = await mountRail({}, {}, { withPalette: true });
    const rail = host.querySelector(".ws-rail");
    const cmdk = host.querySelector(".ws-cmdk");

    await pointerClick(cmdk);
    const input = document.querySelector(".pal-input");
    expect(document.activeElement).toBe(input);
    await press(input, "ArrowDown");
    await press(input, "Enter");

    expect(run).toHaveBeenCalledTimes(1);
    expect(document.querySelector(".pal")).toBeNull();
    expect(document.activeElement).toBe(cmdk);
    expect(rail.classList.contains("is-expanded")).toBe(false);
  });

  it("键盘用户：Tab 进侧栏展开，回车用掉一个入口后收回，再按 Tab 又展开", async () => {
    const { host, go } = await mountRail();
    const rail = host.querySelector(".ws-rail");
    const [first, second] = [...host.querySelectorAll(".ws-nav-scroll .ws-item")];

    await act(async () => {
      document.dispatchEvent(new KeyboardEvent("keydown", { key: "Tab", bubbles: true }));
      first.focus();
    });
    expect(rail.classList.contains("is-expanded")).toBe(true);

    await press(first, "Enter");
    await act(async () => { first.click(); });
    expect(go).toHaveBeenCalledWith("home");
    expect(rail.classList.contains("is-expanded")).toBe(false);

    await act(async () => {
      first.dispatchEvent(new KeyboardEvent("keydown", { key: "Tab", bubbles: true }));
      second.focus();
    });
    expect(rail.classList.contains("is-expanded")).toBe(true);
  });
});

describe("作品切换器", () => {
  const TWO_WORKS = [
    { project_id: "prj-main", title: "北岸手记", genre: "悬疑", target_word_count: 100000, stats: {} },
    { project_id: "prj-side", title: "灯塔旁的信", genre: "短篇", target_word_count: 50000, stats: {} },
  ];

  it("删除一部作品先经应用内确认框：取消什么都不发，确认才 DELETE，且不弹浏览器 confirm", async () => {
    const confirmSpy = vi.spyOn(window, "confirm").mockReturnValue(true);
    const { host } = await mountRail({}, { projects: TWO_WORKS }, { withNotify: true });
    const client = await import("./lib/client.js");
    await vi.waitFor(() => expect(host.querySelector(".ws-brand-title").textContent).toBe("北岸手记"), T);

    await act(async () => { host.querySelector('[data-testid="work-switcher"]').click(); });
    const del = document.querySelector('.ws-wsw-del[aria-label="删除《灯塔旁的信》"]');
    expect(del).not.toBeNull();

    await act(async () => { del.click(); });
    const dialog = document.querySelector('[data-testid="ws-confirm"]');
    expect(dialog).not.toBeNull();
    expect(dialog.getAttribute("role")).toBe("dialog");
    expect(dialog.textContent).toContain("删除《灯塔旁的信》？");
    // 危险动作：焦点先落在「取消」上
    expect(document.activeElement.getAttribute("data-testid")).toBe("ws-confirm-cancel");
    await act(async () => { document.querySelector('[data-testid="ws-confirm-cancel"]').click(); });
    expect(document.querySelector('[data-testid="ws-confirm"]')).toBeNull();
    expect(client.apiDelete).not.toHaveBeenCalled();

    await act(async () => { document.querySelector('.ws-wsw-del[aria-label="删除《灯塔旁的信》"]').click(); });
    await act(async () => { document.querySelector('[data-testid="ws-confirm-ok"]').click(); });
    await vi.waitFor(() => expect(client.apiDelete).toHaveBeenCalledWith("/api/v2/projects/prj-side"), T);
    expect(confirmSpy).not.toHaveBeenCalled();
  });

  it("新建作品：输入法组字中的回车不提交，组字结束后的回车才创建", async () => {
    const { host } = await mountRail({}, { projects: TWO_WORKS });
    await vi.waitFor(() => expect(host.querySelector(".ws-brand-title").textContent).toBe("北岸手记"), T);
    await act(async () => { window.dispatchEvent(new CustomEvent("ws:new-work")); });
    const input = document.querySelector('[data-testid="work-new-title"]');
    const setValue = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value").set;
    await act(async () => {
      setValue.call(input, "新书");
      input.dispatchEvent(new Event("input", { bubbles: true }));
    });
    const client = await import("./lib/client.js");
    const creates = () => client.apiPost.mock.calls.filter(([url]) => url === "/api/v2/projects").length;
    await act(async () => { input.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", keyCode: 229, bubbles: true })); });
    expect(creates()).toBe(0);
    expect(document.querySelector(".ws-nw")).not.toBeNull();
    await act(async () => { input.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", bubbles: true })); });
    await vi.waitFor(() => expect(creates()).toBe(1), T);
  });
  it("浮层是模态的：Tab 不出浮层；Esc 只关浮层、焦点回书名，不传给 window（写作台的托盘 / 沉浸不跟着关）；组字中的 Esc 不关", async () => {
    const { host } = await mountRail({}, { projects: TWO_WORKS }, { withNotify: true });
    await vi.waitFor(() => expect(host.querySelector(".ws-brand-title").textContent).toBe("北岸手记"), T);
    const brand = host.querySelector('[data-testid="work-switcher"]');
    const seen = [];
    const behind = (e) => seen.push(e.key);   // 写作台的快捷键挂在 window 上（冒泡阶段）
    window.addEventListener("keydown", behind);
    try {
      await act(async () => { brand.click(); });
      const pop = document.querySelector(".ws-wsw");
      expect(pop.getAttribute("aria-modal")).toBe("true");
      expect(document.activeElement.classList.contains("is-active")).toBe(true);   // 当前作品那一行

      const newBtn = pop.querySelector(".ws-wsw-new");
      await act(async () => { newBtn.focus(); });
      await press(newBtn, "Tab");
      expect(pop.contains(document.activeElement)).toBe(true);
      expect(document.activeElement).not.toBe(newBtn);

      const row = document.activeElement;
      await act(async () => { row.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", isComposing: true, bubbles: true })); });
      expect(document.querySelector(".ws-wsw")).not.toBeNull();

      // 删除确认框叠在浮层上：Esc 只关确认框
      await act(async () => { pop.querySelector('.ws-wsw-del[aria-label="删除《灯塔旁的信》"]').click(); });
      expect(document.querySelector('[data-testid="ws-confirm"]')).not.toBeNull();
      await press(document.activeElement, "Escape");
      expect(document.querySelector('[data-testid="ws-confirm"]')).toBeNull();
      expect(document.querySelector(".ws-wsw")).not.toBeNull();

      seen.length = 0;
      await press(pop.querySelector(".ws-wsw-row"), "Escape");
      expect(document.querySelector(".ws-wsw")).toBeNull();
      expect(document.activeElement).toBe(brand);
      expect(seen).not.toContain("Escape");
    } finally {
      window.removeEventListener("keydown", behind);
    }
  });
});
