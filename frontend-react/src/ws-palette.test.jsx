// 命令面板（ws-palette.jsx）单测：
// 页面清单与侧栏同源（文学质量 / 成本看板找得到）、输入后在全书所有场景里找、对话框 + combobox / listbox 语义、
// 输入法组字中的回车不当命令、Esc 关闭。
import React, { act } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

globalThis.IS_REACT_ACT_ENVIRONMENT = true;

const fx = vi.hoisted(() => ({ chapters: [] }));

vi.mock("./ws-works.jsx", () => ({
  WsWorks: {
    list: () => [{ id: "w1", title: "北岸手记", genre: "悬疑" }, { id: "w2", title: "灯塔旁的信", genre: "短篇" }],
    activeId: () => "w1",
  },
}));
vi.mock("./ws-catalog.jsx", () => ({
  WsCatalog: { get: () => fx.chapters },
}));

let host;
let root;

function bigBook() {
  // 20 场，只有第 2 场在写：旧面板只列前 12 场，后面的场景怎么搜都搜不到
  return Array.from({ length: 5 }, (_, c) => ({
    n: String(c + 1).padStart(2, "0"),
    title: `第${c + 1}章`,
    scenes: Array.from({ length: 4 }, (_, s) => {
      const k = c * 4 + s + 1;
      return { sid: `s${k}`, title: k === 17 ? "潮水退去的清晨" : `场景${k}`, state: k === 2 ? "writing" : "todo" };
    }),
  }));
}

async function mountPalette(props = {}) {
  const { WsPalette } = await import("./ws-palette.jsx");
  const run = props.run || vi.fn();
  const onClose = props.onClose || vi.fn();
  host = document.createElement("div");
  document.body.appendChild(host);
  root = createRoot(host);
  await act(async () => root.render(<WsPalette open run={run} onClose={onClose} theme="day" />));
  return { run, onClose };
}

const input = () => document.querySelector(".pal-input");
const options = () => [...document.querySelectorAll('[role="option"]')];

async function type(text) {
  const setValue = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value").set;
  await act(async () => {
    setValue.call(input(), text);
    input().dispatchEvent(new Event("input", { bubbles: true }));
  });
}

async function key(k, extra = {}) {
  await act(async () => { input().dispatchEvent(new KeyboardEvent("keydown", { key: k, bubbles: true, ...extra })); });
}

beforeEach(() => {
  vi.resetModules();
  fx.chapters = bigBook();
});

afterEach(async () => {
  if (root) await act(async () => root.unmount());
  if (host) host.remove();
  root = null;
  host = null;
});

describe("命令面板", () => {
  it("是一个对话框：输入框是 combobox，结果是 listbox，当前项由 aria-activedescendant 指出", async () => {
    await mountPalette();
    const dialog = document.querySelector(".pal");
    expect(dialog.getAttribute("role")).toBe("dialog");
    expect(dialog.getAttribute("aria-modal")).toBe("true");
    expect(dialog.getAttribute("aria-label")).toBe("命令面板");
    expect(document.activeElement).toBe(input());
    expect(input().getAttribute("role")).toBe("combobox");
    const listbox = document.getElementById(input().getAttribute("aria-controls"));
    expect(listbox.getAttribute("role")).toBe("listbox");
    expect(input().getAttribute("aria-activedescendant")).toBe(options()[0].id);
    await key("ArrowDown");
    expect(input().getAttribute("aria-activedescendant")).toBe(options()[1].id);
    expect(options()[1].getAttribute("aria-selected")).toBe("true");
  });

  it("页面清单与侧栏同源：「成本」「质量」都找得到，回车跳过去", async () => {
    const { run, onClose } = await mountPalette();
    await type("成本");
    expect(options()[0].textContent).toContain("成本看板");
    await key("Enter");
    expect(run).toHaveBeenCalledWith({ type: "go", view: "cost" });
    expect(onClose).toHaveBeenCalled();

    await type("质量");
    expect(options().some(o => o.textContent.includes("文学质量"))).toBe(true);
  });

  it("没输入时场景只列前几条（在写的排前面）；输入后在全书所有场景里找", async () => {
    const { run } = await mountPalette();
    const sceneOpts = () => options().filter(o => /第 \d+ 章 · 第 \d+ 场/.test(o.textContent));
    expect(sceneOpts().length).toBeLessThanOrEqual(8);
    expect(sceneOpts()[0].textContent).toContain("场景2");
    expect(options().some(o => o.textContent.includes("潮水退去的清晨"))).toBe(false);

    await type("潮水");
    const hit = options().find(o => o.textContent.includes("潮水退去的清晨"));
    expect(hit).toBeTruthy();
    await act(async () => { hit.click(); });
    expect(run).toHaveBeenCalledWith({ type: "scene", sceneId: "s17" });
  });

  it("输入法组字中的回车不执行命令", async () => {
    const { run } = await mountPalette();
    await type("chengben");
    await key("Enter", { isComposing: true });
    await key("Enter", { keyCode: 229 });
    expect(run).not.toHaveBeenCalled();
    await key("Enter");
    expect(run).toHaveBeenCalledTimes(1);
  });

  it("Esc 关闭面板；没有结果时说清楚", async () => {
    const { onClose } = await mountPalette();
    await type("完全不存在的东西xyz");
    expect(options()).toHaveLength(0);
    expect(document.querySelector(".pal-empty").textContent).toContain("没有匹配");
    await act(async () => { input().dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true })); });
    expect(onClose).toHaveBeenCalled();
  });
});

describe("⌘K 只作用于最上层", () => {
  async function mountShell() {
    const { WsPalette, usePaletteShortcut } = await import("./ws-palette.jsx");
    const { WsDialog } = await import("./ws-dialog.jsx");
    const run = vi.fn();
    let openConfirm = null;
    // 和 App 一样挂 ⌘K；另有一个应用内确认框（WsDialog）可以叠在页面上
    function Shell() {
      const [palette, setPalette] = React.useState(false);
      const [confirm, setConfirm] = React.useState(false);
      openConfirm = setConfirm;
      usePaletteShortcut(setPalette);
      return (
        <>
          <button type="button" className="page-btn">页面上的按钮</button>
          <WsPalette open={palette} onClose={() => setPalette(false)} run={run} theme="day" />
          {confirm ? (
            <WsDialog label="删除这一章？" onClose={() => setConfirm(false)}>
              <button type="button" className="confirm-cancel">取消</button>
            </WsDialog>
          ) : null}
        </>
      );
    }
    host = document.createElement("div");
    document.body.appendChild(host);
    root = createRoot(host);
    await act(async () => root.render(<Shell />));
    return { run, setConfirm: (v) => act(async () => openConfirm(v)) };
  }
  const ctrlK = async (target = document.activeElement || document.body) => {
    const event = new KeyboardEvent("keydown", { key: "k", ctrlKey: true, bubbles: true, cancelable: true });
    await act(async () => { target.dispatchEvent(event); });
    return event;
  };

  it("没有别的模态层时 ⌘K 开关面板", async () => {
    await mountShell();
    host.querySelector(".page-btn").focus();
    await ctrlK();
    expect(document.querySelector(".pal")).not.toBeNull();
    expect(document.activeElement).toBe(input());
    await ctrlK();
    expect(document.querySelector(".pal")).toBeNull();
  });

  it("确认框开着时 ⌘K 不在它底下打开面板（焦点留在确认框里），也不让浏览器把焦点抢去地址栏", async () => {
    const shell = await mountShell();
    host.querySelector(".page-btn").focus();
    await shell.setConfirm(true);
    const cancel = document.querySelector(".confirm-cancel");
    expect(document.activeElement).toBe(cancel);

    const event = await ctrlK();
    expect(event.defaultPrevented).toBe(true);
    expect(document.querySelector(".pal")).toBeNull();
    expect(document.activeElement).toBe(cancel);

    // 确认框关掉以后 ⌘K 照常
    await shell.setConfirm(false);
    await ctrlK(document.body);
    expect(document.querySelector(".pal")).not.toBeNull();
    expect(shell.run).not.toHaveBeenCalled();
  });
});
