import React from "react";
import { act } from "react";
import ReactDOMClient from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  PageHeader, Segmented, Tabs, Tag, Notice, EmptyState, StatTile, SectionLabel, Spinner, IconButton, CloseButton,
  ProgressBar, RadioCards, Popover, MenuButton,
} from "./ws-ui.jsx";

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

describe("ProgressBar", () => {
  it("is a progressbar with clamped aria values and a proportional fill; tone colours the fill", async () => {
    await render(<div>
      <ProgressBar label="分类进度" value={30} testId="p1" />
      <ProgressBar label="本月用量" value={1500} max={1000} tone="danger" valueText="已超出" testId="p2" />
      <ProgressBar label="占比" value={-3} max={0} testId="p3" />
    </div>);
    const p1 = host.querySelector("[data-testid=p1]");
    expect(p1.getAttribute("role")).toBe("progressbar");
    expect(p1.getAttribute("aria-label")).toBe("分类进度");
    expect([p1.getAttribute("aria-valuemin"), p1.getAttribute("aria-valuemax"), p1.getAttribute("aria-valuenow")]).toEqual(["0", "100", "30"]);
    expect(p1.querySelector(".ws-progress-fill").style.width).toBe("30%");
    expect(p1.hasAttribute("data-tone")).toBe(false);
    const p2 = host.querySelector("[data-testid=p2]");
    expect(p2.getAttribute("aria-valuenow")).toBe("1000");
    expect(p2.getAttribute("aria-valuetext")).toBe("已超出");
    expect(p2.dataset.tone).toBe("danger");
    expect(p2.querySelector(".ws-progress-fill").style.width).toBe("100%");
    const p3 = host.querySelector("[data-testid=p3]");
    expect([p3.getAttribute("aria-valuemax"), p3.getAttribute("aria-valuenow")]).toEqual(["100", "0"]);
    expect(p3.querySelector(".ws-progress-fill").style.width).toBe("0%");
  });
});

describe("RadioCards", () => {
  const items = [
    { value: "full", label: "全面模仿", badge: "推荐", detail: "带原文样例与文风卡。" },
    { value: "samples_only", label: "只用原文样例", detail: "对照用。", disabled: true },
    { value: "card_only", label: "只用文风卡", testId: "card-only" },
  ];

  it("renders a labelled radiogroup of cards with one Tab stop, badge and detail", async () => {
    await render(<RadioCards label="参考方式" items={items} value="card_only" onChange={vi.fn()} testId="rc" />);
    const set = host.querySelector("[data-testid=rc]");
    expect(set.tagName).toBe("FIELDSET");
    expect(set.querySelector("legend").textContent).toBe("参考方式");
    expect(set.querySelector("[role=radiogroup]").getAttribute("aria-label")).toBe("参考方式");
    const cards = [...set.querySelectorAll("[role=radio]")];
    expect(cards.map((c) => c.dataset.value)).toEqual(["full", "samples_only", "card_only"]);
    expect(cards.map((c) => c.getAttribute("aria-checked"))).toEqual(["false", "false", "true"]);
    expect(cards.map((c) => c.tabIndex)).toEqual([-1, -1, 0]);
    expect(cards[2].classList.contains("is-active")).toBe(true);
    expect(cards[2].dataset.testid).toBe("card-only");
    expect(cards[0].querySelector(".ws-radio-card-title em").textContent).toBe("推荐");
    expect(cards[0].querySelector(".ws-radio-card-detail").textContent).toBe("带原文样例与文风卡。");
    expect(cards[2].querySelector(".ws-radio-card-detail")).toBeNull();
    expect(cards[1].disabled).toBe(true);
  });

  it("arrow keys move the choice past disabled cards; clicking the current card does nothing", async () => {
    const onChange = vi.fn();
    await render(<RadioCards label="参考方式" items={items} value="full" onChange={onChange} />);
    const group = host.querySelector("[role=radiogroup]");
    const right = key(group, "ArrowRight");
    expect(right.defaultPrevented).toBe(true);
    expect(onChange).toHaveBeenLastCalledWith("card_only");
    await act(async () => host.querySelectorAll("[role=radio]")[0].click());
    expect(onChange).toHaveBeenCalledTimes(1);
    await act(async () => host.querySelectorAll("[role=radio]")[2].click());
    expect(onChange).toHaveBeenLastCalledWith("card_only");
  });
});

describe("Popover", () => {
  function Harness({ align, onClose }) {
    const [open, setOpen] = React.useState(false);
    const anchorRef = React.useRef(null);
    return (
      <div className="ws-popover-wrap">
        <button type="button" ref={anchorRef} data-testid="anchor" onClick={() => setOpen((o) => !o)}>筛选</button>
        <Popover open={open} anchorRef={anchorRef} label="筛选图谱" align={align} testId="pop"
          onClose={(reason) => { if (onClose) onClose(reason); setOpen(false); }}>
          <label><input type="checkbox" data-testid="opt" /> 人物</label>
        </Popover>
      </div>
    );
  }

  it("renders nothing while closed; open it is a labelled dialog that takes focus and closes on Escape", async () => {
    const onClose = vi.fn();
    await render(<Harness onClose={onClose} align="start" />);
    expect(host.querySelector("[data-testid=pop]")).toBeNull();
    const anchor = host.querySelector("[data-testid=anchor]");
    anchor.focus();
    await act(async () => anchor.click());
    const pop = host.querySelector("[data-testid=pop]");
    expect(pop.getAttribute("role")).toBe("dialog");
    expect(pop.getAttribute("aria-label")).toBe("筛选图谱");
    expect(pop.classList.contains("ws-popover")).toBe(true);
    expect(pop.classList.contains("is-start")).toBe(true);
    expect(document.activeElement).toBe(host.querySelector("[data-testid=opt]"));
    await act(async () => { key(document.activeElement, "Escape"); });
    expect(onClose).toHaveBeenCalledWith("escape");
    expect(host.querySelector("[data-testid=pop]")).toBeNull();
    expect(document.activeElement).toBe(anchor);
  });
});

describe("MenuButton", () => {
  function menu(onSelect = {}) {
    return [
      { id: "import", label: "导入", icon: "Download", hint: "粘贴十步 JSON", testId: "m-import", onSelect: onSelect.import },
      { id: "export", label: "导出", icon: <svg data-testid="own-icon" />, disabled: true, testId: "m-export", onSelect: onSelect.export },
      { separator: true },
      { id: "clear", label: "清空", danger: true, testId: "m-clear", onSelect: onSelect.clear },
    ];
  }
  const items = () => [...host.querySelectorAll("[role=menuitem]")];

  it("an icon-only button opens a labelled menu and focuses the first usable item", async () => {
    await render(<MenuButton label="更多操作" items={menu()} testId="more" menuTestId="more-menu" />);
    const btn = host.querySelector("[data-testid=more]");
    expect(btn.getAttribute("aria-label")).toBe("更多操作");
    expect(btn.getAttribute("aria-haspopup")).toBe("menu");
    expect(btn.getAttribute("aria-expanded")).toBe("false");
    expect(btn.classList.contains("btn-icon")).toBe(true);
    expect(host.querySelector("[role=menu]")).toBeNull();
    btn.focus();
    await act(async () => btn.click());
    const list = host.querySelector("[data-testid=more-menu]");
    expect(list.getAttribute("role")).toBe("menu");
    expect(list.getAttribute("aria-label")).toBe("更多操作");
    expect(btn.getAttribute("aria-expanded")).toBe("true");
    expect(btn.getAttribute("aria-controls")).toBe(list.id);
    expect(document.activeElement).toBe(host.querySelector("[data-testid=m-import]"));
    expect(host.querySelector("[data-testid=m-import] .ws-menu-text small").textContent).toBe("粘贴十步 JSON");
    expect(host.querySelector("[data-testid=m-import] .ws-menu-ic svg")).not.toBeNull();
    expect(host.querySelector("[data-testid=m-export] [data-testid=own-icon]")).not.toBeNull();
    expect(host.querySelector("[data-testid=m-clear]").classList.contains("is-danger")).toBe(true);
    expect(host.querySelectorAll("[role=separator].ws-menu-sep")).toHaveLength(1);
  });

  it("↑↓ Home End rove over enabled items only and wrap", async () => {
    await render(<MenuButton label="更多操作" items={menu()} testId="more" />);
    await act(async () => host.querySelector("[data-testid=more]").click());
    const list = host.querySelector("[role=menu]");
    const at = () => document.activeElement.dataset.testid;
    key(document.activeElement, "ArrowDown");
    expect(at()).toBe("m-clear"); // 跳过禁用的「导出」
    key(document.activeElement, "ArrowDown");
    expect(at()).toBe("m-import");
    key(document.activeElement, "ArrowUp");
    expect(at()).toBe("m-clear");
    key(document.activeElement, "Home");
    expect(at()).toBe("m-import");
    const end = key(document.activeElement, "End");
    expect(end.defaultPrevented).toBe(true);
    expect(at()).toBe("m-clear");
    expect(list.contains(document.activeElement)).toBe(true);
    expect(items()).toHaveLength(3);
  });

  it("choosing an item closes the menu and hands focus back to the button before onSelect runs", async () => {
    let focusedAtSelect = null;
    const onClear = vi.fn(() => { focusedAtSelect = document.activeElement; });
    await render(<MenuButton label="更多操作" items={menu({ clear: onClear })} testId="more" />);
    const btn = host.querySelector("[data-testid=more]");
    await act(async () => btn.click());
    await act(async () => host.querySelector("[data-testid=m-clear]").click());
    expect(onClear).toHaveBeenCalledTimes(1);
    expect(focusedAtSelect).toBe(btn);
    expect(host.querySelector("[role=menu]")).toBeNull();
    expect(btn.getAttribute("aria-expanded")).toBe("false");
  });

  it("Escape closes and refocuses the button; ArrowDown on the button opens it; a text button keeps its visible label", async () => {
    await render(<MenuButton label="导出选项" text="导出" icon="Download" items={menu()} testId="more" />);
    const btn = host.querySelector("[data-testid=more]");
    expect(btn.classList.contains("btn-icon")).toBe(false);
    expect(btn.hasAttribute("aria-label")).toBe(false);
    expect(btn.textContent).toBe("导出");
    btn.focus();
    await act(async () => { key(btn, "ArrowDown"); });
    expect(host.querySelector("[role=menu]")).not.toBeNull();
    await act(async () => { key(document.activeElement, "Escape"); });
    expect(host.querySelector("[role=menu]")).toBeNull();
    expect(document.activeElement).toBe(btn);
  });

  it("pressing outside or tabbing away closes the menu", async () => {
    await render(<div><MenuButton label="更多操作" items={menu()} testId="more" /><button type="button" data-testid="elsewhere">别处</button></div>);
    const btn = host.querySelector("[data-testid=more]");
    await act(async () => btn.click());
    await act(async () => { host.querySelector("[data-testid=elsewhere]").dispatchEvent(new Event("pointerdown", { bubbles: true })); });
    expect(host.querySelector("[role=menu]")).toBeNull();
    await act(async () => btn.click());
    expect(host.querySelector("[role=menu]")).not.toBeNull();
    await act(async () => host.querySelector("[data-testid=elsewhere]").focus());
    expect(host.querySelector("[role=menu]")).toBeNull();
  });
});
