import React from "react";
import { act } from "react";
import ReactDOMClient from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { PageHeader, Segmented, Tabs, Tag, Notice, EmptyState, StatTile, SectionLabel, Spinner, IconButton, CloseButton } from "./ws-ui.jsx";

globalThis.IS_REACT_ACT_ENVIRONMENT = true;

let host;
let root;

beforeEach(() => {
  host = document.createElement("div");
  document.body.appendChild(host);
  root = ReactDOMClient.createRoot(host);
});

afterEach(async () => {
  await act(async () => root.unmount());
  host.remove();
});

const render = (node) => act(async () => root.render(node));

function key(target, k) {
  const event = new KeyboardEvent("keydown", { key: k, bubbles: true, cancelable: true });
  target.dispatchEvent(event);
  return event;
}

describe("PageHeader", () => {
  it("renders the title as a heading with optional crumb, description and actions", async () => {
    await render(<PageHeader title="成稿中心" crumb="第 3 章" description="通读后批准终稿。" actions={<button type="button">导出</button>} testId="ph" />);
    const head = host.querySelector("[data-testid=ph]");
    expect(head.querySelector("h1.ws-page-title").textContent).toBe("成稿中心");
    expect(head.querySelector(".ws-page-crumb").textContent).toBe("第 3 章");
    expect(head.querySelector(".ws-page-desc").textContent).toBe("通读后批准终稿。");
    expect(head.querySelector(".ws-page-actions button").textContent).toBe("导出");
  });

  it("omits empty slots instead of rendering blank elements", async () => {
    await render(<PageHeader title="设置" />);
    expect(host.querySelector(".ws-page-crumb")).toBeNull();
    expect(host.querySelector(".ws-page-desc")).toBeNull();
    expect(host.querySelector(".ws-page-actions")).toBeNull();
  });
});

describe("Segmented", () => {
  const options = [
    { value: "all", label: "全部", count: 4 },
    { value: "open", label: "待办", count: 1 },
    { value: "done", label: "已完成", disabled: true },
    { value: "later", label: "稍后", className: "my-later" },
  ];

  it("is a radiogroup whose checked option is the only tab stop", async () => {
    await render(<Segmented label="筛选" value="open" options={options} onChange={() => {}} />);
    const group = host.querySelector("[role=radiogroup]");
    expect(group.getAttribute("aria-label")).toBe("筛选");
    const radios = [...host.querySelectorAll("[role=radio]")];
    expect(radios.map((r) => r.getAttribute("aria-checked"))).toEqual(["false", "true", "false", "false"]);
    expect(radios.map((r) => r.tabIndex)).toEqual([-1, 0, -1, -1]);
    expect(radios[1].classList.contains("is-active")).toBe(true);
    expect(radios.map((r) => r.dataset.value)).toEqual(["all", "open", "done", "later"]);
    expect(radios[0].querySelector(".seg-count").textContent).toBe("4");
    expect(radios[3].classList.contains("my-later")).toBe(true);
  });

  it("moves the choice with arrow keys, skipping disabled options and wrapping", async () => {
    const onChange = vi.fn();
    await render(<Segmented label="筛选" value="open" options={options} onChange={onChange} />);
    const group = host.querySelector("[role=radiogroup]");
    const right = key(group, "ArrowRight");
    expect(right.defaultPrevented).toBe(true);
    expect(onChange).toHaveBeenLastCalledWith("later");
    await render(<Segmented label="筛选" value="later" options={options} onChange={onChange} />);
    key(host.querySelector("[role=radiogroup]"), "ArrowRight");
    expect(onChange).toHaveBeenLastCalledWith("all");
    key(host.querySelector("[role=radiogroup]"), "Home");
    expect(onChange).toHaveBeenLastCalledWith("all");
  });

  it("keeps one Tab stop when nothing (or a disabled option) is selected", async () => {
    await render(<Segmented label="筛选" value="nope" options={options} onChange={vi.fn()} />);
    expect([...host.querySelectorAll("[role=radio]")].map((r) => r.tabIndex)).toEqual([0, -1, -1, -1]);
    const withDisabledPick = options.map((o) => (o.value === "all" ? { ...o, disabled: true } : o));
    await render(<Segmented label="筛选" value="all" options={withDisabledPick} onChange={vi.fn()} />);
    const radios = [...host.querySelectorAll("[role=radio]")];
    expect(radios.filter((r) => r.tabIndex === 0 && !r.disabled)).toHaveLength(1);
  });

  it("busy marks the group aria-busy without disabling the options (focus must not fall to body)", async () => {
    await render(<Segmented label="筛选" value="open" options={options} onChange={vi.fn()} busy />);
    const group = host.querySelector("[role=radiogroup]");
    expect(group.getAttribute("aria-busy")).toBe("true");
    expect([...host.querySelectorAll("[role=radio]")].map((r) => r.disabled)).toEqual([false, false, true, false]);
    await render(<Segmented label="筛选" value="open" options={options} onChange={vi.fn()} />);
    expect(host.querySelector("[role=radiogroup]").hasAttribute("aria-busy")).toBe(false);
  });

  it("does not fire onChange when the current option is clicked again", async () => {
    const onChange = vi.fn();
    await render(<Segmented label="筛选" value="open" options={options} onChange={onChange} />);
    await act(async () => host.querySelectorAll("[role=radio]")[1].click());
    expect(onChange).not.toHaveBeenCalled();
    await act(async () => host.querySelectorAll("[role=radio]")[0].click());
    expect(onChange).toHaveBeenCalledWith("all");
  });
});

describe("Tabs", () => {
  it("renders a tablist with aria-selected and aria-controls, and roves with arrow keys", async () => {
    const onChange = vi.fn();
    await render(<Tabs label="视图" idPrefix="mc" value="read" onChange={onChange}
      tabs={[{ id: "read", label: "正文" }, { id: "structure", label: "结构", count: 2 }, { id: "canon", label: "正史" }]} />);
    const tabs = [...host.querySelectorAll("[role=tab]")];
    expect(tabs[0].getAttribute("aria-selected")).toBe("true");
    expect(tabs[0].getAttribute("aria-controls")).toBe("mc-panel-read");
    // 只有选中的页签指向面板：调用方只渲染选中的面板，给没渲染的面板写 aria-controls 是悬空引用
    expect(tabs[1].hasAttribute("aria-controls")).toBe(false);
    expect(tabs[2].hasAttribute("aria-controls")).toBe(false);
    expect(tabs.map((t) => t.id)).toEqual(["mc-tab-read", "mc-tab-structure", "mc-tab-canon"]);
    expect(tabs[1].querySelector(".ws-tab-count").textContent).toBe("2");
    tabs[0].focus();
    await act(async () => { key(tabs[0], "ArrowRight"); });
    expect(onChange).toHaveBeenCalledWith("structure");
  });
});

describe("Tag / Notice / EmptyState / StatTile / SectionLabel / Spinner", () => {
  it("carries tone as data-tone so the tone resolver can colour it", async () => {
    await render(<div>
      <Tag tone="ok" dot testId="tag">已批准</Tag>
      <Notice tone="danger" title="没保存" testId="notice">网络断了，稿子留在本机。</Notice>
      <Notice tone="info" testId="info">提示</Notice>
      <StatTile label="已定稿" value="12,400" unit="字" tone="ok" testId="stat" />
    </div>);
    expect(host.querySelector("[data-testid=tag]").dataset.tone).toBe("ok");
    expect(host.querySelector("[data-testid=tag] .ws-tag-dot")).not.toBeNull();
    const notice = host.querySelector("[data-testid=notice]");
    expect(notice.dataset.tone).toBe("danger");
    expect(notice.getAttribute("role")).toBe("alert");
    expect(host.querySelector("[data-testid=info]").getAttribute("role")).toBe("status");
    expect(host.querySelector("[data-testid=stat] .ws-stat-value").textContent).toBe("12,400字");
  });

  it("renders an empty state with a title, body and action", async () => {
    await render(<EmptyState icon="Inbox" title="没有待办" actions={<button type="button">回去写</button>} testId="empty">处理完了。</EmptyState>);
    const empty = host.querySelector("[data-testid=empty]");
    expect(empty.querySelector(".ws-empty-title").textContent).toBe("没有待办");
    expect(empty.querySelector(".ws-empty-text").textContent).toBe("处理完了。");
    expect(empty.querySelector("svg")).not.toBeNull();
  });

  it("section label and spinner", async () => {
    await render(<div><SectionLabel icon="Inbox" aside="3">待处理</SectionLabel><Spinner label="加载中" /></div>);
    expect(host.querySelector(".ws-section-label").textContent).toBe("待处理3");
    expect(host.querySelector(".ws-spinner").getAttribute("aria-label")).toBe("加载中");
  });
});

describe("IconButton / CloseButton", () => {
  it("always has an accessible name", async () => {
    const onClick = vi.fn();
    await render(<div><IconButton icon="Refresh" label="刷新" onClick={onClick} /><CloseButton onClick={onClick} /></div>);
    const [refresh, close] = host.querySelectorAll("button");
    expect(refresh.getAttribute("aria-label")).toBe("刷新");
    expect(refresh.classList.contains("btn-icon")).toBe(true);
    expect(close.getAttribute("aria-label")).toBe("关闭");
    await act(async () => close.click());
    expect(onClick).toHaveBeenCalledTimes(1);
  });

  it("CloseButton passes disabled and an explicit tooltip through, and keeps the shared quiet icon-button classes", async () => {
    const onClick = vi.fn();
    await render(<CloseButton label="关闭本步上下文" title="关闭（Esc）" className="wr-drawer-x" disabled onClick={onClick} />);
    const close = host.querySelector("button");
    expect(close.getAttribute("aria-label")).toBe("关闭本步上下文");
    expect(close.title).toBe("关闭（Esc）");
    expect(close.disabled).toBe(true);
    for (const c of ["btn", "btn-quiet", "btn-icon", "wr-drawer-x"]) expect(close.classList.contains(c)).toBe(true);
    await act(async () => close.click());
    expect(onClick).not.toHaveBeenCalled();
  });
});
