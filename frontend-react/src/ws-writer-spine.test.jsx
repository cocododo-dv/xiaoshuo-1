// 写作台与雪花整理出来的章（阶段 X「一条书脊」）。
// 真实故障：目录里躺着一章手建的空白占位「第 1 章 / 开场」（场标着「在写」），雪花的章排在它后面——
// 写作台的落点规则是「全书任何一场在写的场」，于是开在那张空白占位场上；雪花的章在大纲里折叠着，
// 徽标印着英文原词 planned，场景上下文里 POV / 时间 / 地点全是「—」（只读章，不读场）。
import React, { act } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { DEFAULT_CHAP, installApiRouter } from "./test-helpers.js";

vi.mock("./lib/client.js", () => ({
  apiGet: vi.fn(), apiPost: vi.fn(), apiPatch: vi.fn(), apiDelete: vi.fn(),
}));

globalThis.IS_REACT_ACT_ENVIRONMENT = true;
const T = { timeout: 5000, interval: 25 };
const mounted = [];
const innerTextDescriptor = Object.getOwnPropertyDescriptor(HTMLElement.prototype, "innerText");

const scene = (id, title, extra = {}) => ({
  slug: id, legacy_slug: "", scene_id: id, title, summary: `${title}——整句摘要`, kind: "proactive", state: "todo", words: 0,
  brief: { goal: "目标", conflict: "冲突", setback: "挫折" }, pov_character_name: "",
  ...extra,
});
/* 第一章：手建的、作者写过东西的章（所以没被移走），里面一场标着「在写」；当前章是雪花的第 2 章 */
const BOOK = [
  { ...DEFAULT_CHAP, slug: "ch01", chapter_id: "C_HAND", no: "01", title: "楔子", state: "writing", current: false, pov: "",
    scenes: [scene("SC_hand", "开场", { state: "writing" })] },
  { ...DEFAULT_CHAP, slug: "ch02", chapter_id: "P_CH01", no: "02", title: "雨夜来信", state: "planned", current: true, pov: "",
    origin: "snowflake", summary: "她被停职了。", spine: "灾一",
    scenes: [
      scene("SC_a", "旧信到了", { state: "done" }),
      scene("SC_b", "雨里站着", {
        kind: "reactive", brief: { reaction: "她在雨里站了很久", dilemma: "装作没看见，还是查到底", decision: "去档案馆调那份卷宗" },
        pov_character_name: "林昭", hook: "信封里还有第二张车票。",
        design: { origin: "snowflake", crucible: "停职通知明早生效", location: "雨城旧码头", story_time: "第二日 · 晨",
          cast: [{ character_id: "c2", name: "沈越" }], followup: {}, length_band: "1300-1600" },
      }),
      scene("SC_c", "档案馆夜访"),
    ] },
];

function matchMedia() {
  return { matches: false, media: "", addEventListener: vi.fn(), removeEventListener: vi.fn(), addListener: vi.fn(), removeListener: vi.fn() };
}

async function loadWriter() {
  const client = await import("./lib/client.js");
  installApiRouter(client, { catalog: BOOK });
  await import("./ws-catalog.jsx");
  await vi.waitFor(() => expect(window.WsWorks && window.WsWorks.activeId()).toBe("prj-main"), T);
  await vi.waitFor(() => expect(window.WsCatalog && window.WsCatalog.get().length).toBeGreaterThan(0), T);
  const writer = await import("./ws-writer.jsx");
  return { ...writer, client };
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

beforeEach(() => {
  vi.resetModules();
  window.localStorage.clear();
  window.location.hash = "";
  Object.defineProperty(window, "matchMedia", { configurable: true, value: matchMedia });
  if (!innerTextDescriptor) {
    Object.defineProperty(HTMLElement.prototype, "innerText", {
      configurable: true,
      get() { return this.textContent || ""; },
      set(value) { this.textContent = value; },
    });
  }
  vi.spyOn(window, "alert").mockImplementation(() => {});
});
afterEach(async () => {
  while (mounted.length) {
    const { root, host } = mounted.pop();
    await act(async () => root.unmount());
    host.remove();
  }
  if (!innerTextDescriptor) delete HTMLElement.prototype.innerText;
  const { clearViewIntents } = await import("./ws-view-intents.js");
  clearViewIntents("snowflake");
  vi.restoreAllMocks();
});

describe("写作台 · 雪花整理出来的章", () => {
  it("落点是当前章里现在该写的那一场，不是别的章里标着「在写」的那一场", async () => {
    const { WriterRoom } = await loadWriter();
    const host = await render(<WriterRoom t={{}} setTweak={() => {}} go={vi.fn()} />);
    expect(host.querySelector(".wr-scene-title").textContent).toBe("雨里站着");
    expect(host.querySelector(".wr-stamp").textContent).toContain("第 2 章 · 第 2 场");
  });

  it("正文上方随行的是与 AI 起草台同一张设计卡：反应场按 反应 / 两难 / 决定 命名，POV / 时间 / 地点读这一场", async () => {
    const go = vi.fn();
    const { WriterRoom } = await loadWriter();
    const host = await render(<WriterRoom t={{}} setTweak={() => {}} go={go} />);
    const card = host.querySelector('.wr-scene-head [data-testid="scene-design-card"]');
    expect(card.getAttribute("data-origin")).toBe("snowflake");
    expect([...card.querySelectorAll(".sdc-beat-k")].map(n => n.textContent)).toEqual(["反应", "两难", "决定"]);
    ["停职通知明早生效", "林昭", "第二日 · 晨", "雨城旧码头", "沈越", "1300–1600 字"].forEach((part) => expect(card.textContent).toContain(part));
    await click(card.querySelector('[data-testid="scene-design-edit-plan"]'));
    expect(go).toHaveBeenCalledWith("snowflake", [
      { type: "ws:snow-step", detail: "planning" }, { type: "ws:snow-scene", detail: "SC_b" },
    ]);
  });

  it("场景上下文抽屉里是整张设计卡（钩子、章摘要、脊柱），不再是套着 Goal / Conflict / Setback 标签的三行", async () => {
    const { WriterRoom } = await loadWriter();
    const host = await render(<WriterRoom t={{}} setTweak={() => {}} go={vi.fn()} />);
    const drawer = host.querySelector(".wr-drawer.right");
    const card = drawer.querySelector('[data-testid="scene-design-card"]');
    ["信封里还有第二张车票。", "她被停职了。", "灾一", "雨里站着——整句摘要"].forEach((part) => expect(card.textContent).toContain(part));
    expect(drawer.textContent).not.toContain("Goal · 目标");
    expect(drawer.textContent).toContain("交给 AI 起草整场");
  });

  it("正文上方的卡展开时，抽屉不再重复三拍与事实行；收起正文上方的卡，抽屉给回整张", async () => {
    const { WriterRoom } = await loadWriter();
    const host = await render(<WriterRoom t={{}} setTweak={() => {}} go={vi.fn()} />);
    const drawerCard = () => host.querySelector('.wr-drawer.right [data-testid="scene-design-card"]');
    expect(drawerCard().querySelectorAll(".sdc-beat").length).toBe(0);
    expect(drawerCard().textContent).not.toContain("她在雨里站了很久");
    expect(host.querySelector(".wr-scene-head").textContent).toContain("她在雨里站了很久");

    await click(host.querySelector('.wr-scene-head [data-testid="scene-design-toggle"]'));
    expect([...drawerCard().querySelectorAll(".sdc-beat-k")].map(n => n.textContent)).toEqual(["反应", "两难", "决定"]);
    expect(drawerCard().textContent).toContain("信封里还有第二张车票。");
  });

  it("页头只印章场编号（不再猜「反应场景」），字数对照这一场设计的篇幅", async () => {
    const { WriterRoom } = await loadWriter();
    const host = await render(<WriterRoom t={{}} setTweak={() => {}} go={vi.fn()} />);
    expect(host.querySelector(".wr-stamp").textContent).toBe("第 2 章 · 第 2 场");
    expect(host.querySelector(".wr-count").textContent.replace(/\s/g, "")).toBe("0/1300–1600字");
    expect(host.textContent).not.toContain("/ 1500");
  });

  it("大纲：章的状态是中文、和成稿中心 / 章节编排同一个词与同一条规则、脊柱标记与章摘要在章名的提示里、落点所在的章展开、场题名带整句摘要提示", async () => {
    const { WriterRoom } = await loadWriter();
    const host = await render(<WriterRoom t={{}} setTweak={() => {}} go={vi.fn()} />);
    const outline = host.querySelector(".wr-drawer.left");
    const chapters = [...outline.querySelectorAll(".wr-ch")];
    // 第 2 章目录上挂着 planned，但已经写完一场：和成稿中心、章节编排、主页一样读作「写作中」（以前大纲照抄目录说「规划」）
    expect(chapters.map(ch => ch.querySelector(".wr-ch-pill").textContent)).toEqual(["写作中", "写作中"]);
    expect(outline.textContent).not.toContain("planned");
    expect(chapters[1].querySelector(".wr-ch-title").getAttribute("title")).toBe("收在灾一 · 她被停职了。");
    const names = [...chapters[1].querySelectorAll(".wr-sc-name")];
    expect(names.map(n => n.textContent)).toEqual(["旧信到了", "雨里站着", "档案馆夜访"]);
    expect(names[1].getAttribute("title")).toBe("雨里站着——整句摘要");
    // 阶段 Y：雪花整理出来的场，先后 = 构思第 9 步的行序——大纲里不给拖；没有设计归属的场（手加的）照常能拖
    const rows = [...chapters[1].querySelectorAll(".wr-sc-list > li")].filter(li => li.querySelector(".wr-sc-name"));
    expect(rows.map(li => li.getAttribute("draggable"))).toEqual(["true", "false", "true"]);
    expect(rows[1].querySelector(".wr-sc-grip").getAttribute("title")).toContain("构思第 9 步");
  });

  it("「下一场」的名字读目录里真的下一场（这里曾写死着已退役演示作品的场名）", async () => {
    const { WriterRoom } = await loadWriter();
    const host = await render(<WriterRoom t={{}} setTweak={() => {}} go={vi.fn()} />);
    expect(host.querySelector(".wr-next-text").textContent).toContain("档案馆夜访");
    expect(host.textContent).not.toContain("馆长出现");
  });
});
