// 写作台「章节大纲」抽屉的删除 / 批量删除契约。
// 目录写穿点是 WsCatalog（单一真相源）——这里断言抽屉里的删除动作最终变成
// 一次带完整 id 集合的软删调用，而不是只改本地渲染；被删章下的场不再单独进场景桶
// （后端会以「章下已有单独回收的场景」挡下整章删除，那正是「删了又冒出来」的来源）。
import React, { act } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { DEFAULT_CHAP, DEFAULT_PROJECT, installApiRouter } from "./test-helpers.js";

vi.mock("./lib/client.js", () => ({
  apiGet: vi.fn(), apiPost: vi.fn(), apiPatch: vi.fn(), apiDelete: vi.fn(),
}));

globalThis.IS_REACT_ACT_ENVIRONMENT = true;
const T = { timeout: 5000, interval: 25 };
const mounted = [];
const innerTextDescriptor = Object.getOwnPropertyDescriptor(HTMLElement.prototype, "innerText");
const scrollToDescriptor = Object.getOwnPropertyDescriptor(Element.prototype, "scrollTo");

const SECOND_CHAP = {
  ...DEFAULT_CHAP,
  slug: "ch02", chapter_id: "c2", no: "02", title: "第二章", current: false,
  scenes: [
    { ...DEFAULT_CHAP.scenes[0], slug: "ch02s1", scene_id: "s2", title: "夜航" },
    { ...DEFAULT_CHAP.scenes[0], slug: "ch02s2", scene_id: "s3", title: "回港" },
  ],
};

function matchMedia() {
  return { matches: false, media: "", addEventListener: vi.fn(), removeEventListener: vi.fn(), addListener: vi.fn(), removeListener: vi.fn() };
}

async function loadWriter(opts) {
  const client = await import("./lib/client.js");
  installApiRouter(client, opts);
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

/* 大纲抽屉常驻 DOM（靠 class 控制显隐），所以直接取节点即可，不必先开抽屉 */
const outline = (host) => host.querySelector(".wr-drawer.left");

beforeEach(() => {
  vi.resetModules();
  window.localStorage.clear();
  Object.defineProperty(window, "matchMedia", { configurable: true, value: matchMedia });
  if (!innerTextDescriptor) {
    Object.defineProperty(HTMLElement.prototype, "innerText", {
      configurable: true,
      get() { return this.textContent || ""; },
      set(value) { this.textContent = value; },
    });
  }
  // jsdom 没有滚动：采纳续写后要滚到正文末尾
  if (!scrollToDescriptor) Object.defineProperty(Element.prototype, "scrollTo", { configurable: true, value() {} });
  vi.spyOn(window, "confirm").mockReturnValue(true);
  vi.spyOn(window, "alert").mockImplementation(() => {});
});

afterEach(async () => {
  while (mounted.length) {
    const { root, host } = mounted.pop();
    await act(async () => root.unmount());
    host.remove();
  }
  if (!innerTextDescriptor) delete HTMLElement.prototype.innerText;
  if (!scrollToDescriptor) delete Element.prototype.scrollTo;
  vi.restoreAllMocks();
});

describe("写作台 · 章节大纲的删除与批量删除", () => {
  it("章行删除按钮把整章（含章下场景）软删，只发一次 chapters/trash", async () => {
    const { WriterRoom, client } = await loadWriter({ catalog: [DEFAULT_CHAP, SECOND_CHAP] });
    const host = await render(<WriterRoom t={{}} setTweak={() => {}} />);
    client.apiPost.mockClear();

    const delButtons = [...outline(host).querySelectorAll(".wr-ch-del")];
    expect(delButtons).toHaveLength(2);
    await click(delButtons[1]);

    expect(window.confirm).toHaveBeenCalled();
    await vi.waitFor(() => expect(client.apiPost).toHaveBeenCalledWith(
      "/api/v1/chapters/trash", { chapter_ids: ["c2"] },
    ), T);
    expect(client.apiPost.mock.calls.some(([url]) => url === "/api/v1/scenes/trash")).toBe(false);
    expect(client.apiPost.mock.calls.filter(([url]) => url === "/api/v1/chapters/trash")).toHaveLength(1);
  });

  it("多选批量删除：章 + 场一次提交，被删章下的场不再单独进场景桶", async () => {
    const { WriterRoom, client } = await loadWriter({ catalog: [DEFAULT_CHAP, SECOND_CHAP] });
    const host = await render(<WriterRoom t={{}} setTweak={() => {}} />);
    client.apiPost.mockClear();

    await click(outline(host).querySelector('[data-testid="writer-outline-select-mode"]'));
    // 勾第二章整章 + 第一章的那一场
    const chBoxes = [...outline(host).querySelectorAll(".wr-ch-check input")];
    expect(chBoxes).toHaveLength(2);
    await act(async () => { chBoxes[1].click(); });
    const scBoxes = [...outline(host).querySelectorAll(".wr-sc-check input")];
    await act(async () => { scBoxes[0].click(); });

    await click(outline(host).querySelector('[data-testid="writer-outline-batch-delete"]'));

    await vi.waitFor(() => expect(client.apiPost).toHaveBeenCalledWith(
      "/api/v1/chapters/trash", { chapter_ids: ["c2"] },
    ), T);
    // 只有第一章那一场单独进场景桶；第二章下的 s2/s3 随章一起走
    await vi.waitFor(() => expect(client.apiPost).toHaveBeenCalledWith(
      "/api/v1/scenes/trash", { scene_ids: ["s1"] },
    ), T);
  });

  it("勾选整章后章下场景显示为随章带走，不再当成一次独立选择计数", async () => {
    const { WriterRoom } = await loadWriter({ catalog: [DEFAULT_CHAP, SECOND_CHAP] });
    const host = await render(<WriterRoom t={{}} setTweak={() => {}} />);
    const panel = outline(host);

    await click(panel.querySelector('[data-testid="writer-outline-select-mode"]'));
    const chBoxes = [...panel.querySelectorAll(".wr-ch-check input")];
    await act(async () => { chBoxes[1].click(); });      // 勾第二章（章下两场）

    const scBoxes = [...panel.querySelectorAll(".wr-sc-check input")];
    const followers = scBoxes.filter((box) => box.disabled);
    expect(followers).toHaveLength(2);                    // 第二章的两场
    expect(followers.every((box) => box.checked)).toBe(true);
    // 计数只报作者真正做过的选择，随章带走的场单独说明
    expect(panel.querySelector(".wr-outline-batch-n").textContent).toContain("1");
    expect(panel.querySelector(".wr-outline-batch-n").textContent).toContain("随章带走 2 场");
  });

  it("删除后给出回执并指向回收站，不让作者对着消失的章发呆", async () => {
    const go = vi.fn();
    const { WriterRoom } = await loadWriter({ catalog: [DEFAULT_CHAP, SECOND_CHAP] });
    const host = await render(<WriterRoom t={{}} setTweak={() => {}} go={go} />);

    await click([...outline(host).querySelectorAll(".wr-ch-del")][1]);

    const toast = host.querySelector('[data-testid="undo-toast"]');
    expect(toast.textContent).toContain("移入回收站");
    await click(toast.querySelector('[data-testid="undo-toast-action"]'));
    expect(go).toHaveBeenCalledWith("trash");
  });

  it("已批准章节在大纲里既删不掉也勾不动（要先去成稿中心重新打开）", async () => {
    const approved = { ...DEFAULT_CHAP, state: "approved" };
    const { WriterRoom, client } = await loadWriter({ catalog: [approved] });
    const host = await render(<WriterRoom t={{}} setTweak={() => {}} />);
    client.apiPost.mockClear();

    const del = outline(host).querySelector(".wr-ch-del");
    expect(del).toHaveProperty("disabled", true);
    await click(del);
    expect(client.apiPost).not.toHaveBeenCalled();

    await click(outline(host).querySelector('[data-testid="writer-outline-select-mode"]'));
    expect(outline(host).querySelector(".wr-ch-check input")).toHaveProperty("disabled", true);
    expect(outline(host).querySelector(".wr-sc-check input")).toHaveProperty("disabled", true);
  });

  it("章徽标与场的叫法走 ws-labels：目录上挂着「规划」但已经写完一场的章读作写作中，定稿章读作已定稿", async () => {
    // 以前大纲照抄目录状态、自带一份词表：写了字的章挂着蓝色的「规划」，定稿章叫「已批准」，别处都不这么叫
    const started = { ...DEFAULT_CHAP, state: "planned", scenes: [{ ...DEFAULT_CHAP.scenes[0], state: "done" }] };
    const approved = { ...SECOND_CHAP, state: "approved" };
    const { WriterRoom } = await loadWriter({ catalog: [started, approved] });
    const host = await render(<WriterRoom t={{}} setTweak={() => {}} />);
    const pills = [...outline(host).querySelectorAll(".wr-ch-pill")];
    expect(pills.map((p) => p.textContent)).toEqual(["写作中", "已定稿"]);
    expect(pills.map((p) => p.getAttribute("data-tone"))).toEqual(["accent", "ok"]);
    // 场的状态词也是同一份：写完的场叫「已完成」
    expect(outline(host).querySelector(".wr-sc-open").getAttribute("aria-label")).toBe("交班（已完成）");
  });
});

describe("写作台 · 停靠的栏与续写托盘", () => {
  it("Esc 与「从别处跳来」都不会收起停靠的栏（只收叠放的抽屉）", async () => {
    const { WriterRoom } = await loadWriter({ catalog: [DEFAULT_CHAP, SECOND_CHAP] });
    const host = await render(<WriterRoom t={{}} setTweak={() => {}} />);
    const root = host.querySelector(".wr-root");
    expect(root.getAttribute("data-dock-left")).toBe("on");
    expect(root.getAttribute("data-left")).toBe("on");
    expect(root.getAttribute("data-right")).toBe("on");

    await act(async () => window.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true })));
    expect(root.getAttribute("data-left")).toBe("on");
    expect(root.getAttribute("data-right")).toBe("on");

    await act(async () => window.dispatchEvent(new CustomEvent("ws:writer-scene", { detail: "ch02s1" })));
    expect(host.querySelector(".wr-scene-title").textContent).toBe("夜航");
    expect(root.getAttribute("data-left")).toBe("on");
    expect(root.getAttribute("data-right")).toBe("on");
  });

  it("打开续写托盘不会自己发生成请求；按「生成三条」才发一次，没配模型时给出去系统设置的出口", async () => {
    const go = vi.fn();
    const { WriterRoom, client } = await loadWriter();
    vi.spyOn(window.WrDocs, "draftId").mockResolvedValue("draft-s1");
    const host = await render(<WriterRoom t={{}} setTweak={() => {}} go={go} />);
    client.apiPost.mockClear();
    client.apiPost.mockRejectedValue(Object.assign(new Error("author proposal generation requires a configured live LLM capability"), {
      code: "AUTHOR_PROPOSAL_LLM_NOT_CONFIGURED", status: 409, details: { next_action: "configure_author_proposal_route_and_retry" },
    }));

    const aiButton = [...host.querySelectorAll("button")].find((button) => button.textContent.includes("AI 续写"));
    await click(aiButton);
    const tray = host.querySelector(".wr-tray");
    expect(tray.classList.contains("show")).toBe(true);
    expect(client.apiPost.mock.calls.some(([url]) => String(url).includes("generate-set"))).toBe(false);

    const generate = [...tray.querySelectorAll("button")].find((button) => button.textContent.includes("生成三条"));
    await click(generate);
    await vi.waitFor(() => expect(tray.textContent).toContain("去系统设置"), T);
    expect(client.apiPost.mock.calls.filter(([url]) => String(url).includes("generate-set"))).toHaveLength(1);
    expect(tray.textContent).not.toContain("configured live LLM");
    await click([...tray.querySelectorAll("button")].find((button) => button.textContent === "去系统设置"));
    // 直接落到「设置 · AI 模型」页签，而不是设置的第一页
    expect(go).toHaveBeenCalledWith("settings", { type: "ws:settings-tab", detail: "ai" });
  });
});

describe("写作台 · 续写托盘关上后焦点回到正文", () => {
  it("⌘J 打开托盘、Esc 关上：焦点回到正文和原来的光标处；采纳整段后光标落在新段末尾", async () => {
    const { WriterRoom, client } = await loadWriter();
    vi.spyOn(window.WrDocs, "load").mockReturnValue("<p>她把灯关了。</p>");
    vi.spyOn(window.WrDocs, "draftId").mockResolvedValue("draft-s1");
    vi.spyOn(window.WrDocs, "save").mockResolvedValue({});
    const host = await render(<WriterRoom t={{}} setTweak={() => {}} />);
    await vi.waitFor(() => expect(host.textContent).toContain("她把灯关了"), T);
    const editor = host.querySelector(".wr-editor");
    const tray = host.querySelector(".wr-tray");
    const textNode = editor.querySelector("p").firstChild;
    await act(async () => {
      editor.focus();
      const range = document.createRange();
      range.setStart(textNode, 2);
      range.collapse(true);
      window.getSelection().removeAllRanges();
      window.getSelection().addRange(range);
    });

    await act(async () => window.dispatchEvent(new KeyboardEvent("keydown", { key: "j", ctrlKey: true, bubbles: true, cancelable: true })));
    expect(tray.classList.contains("show")).toBe(true);
    await vi.waitFor(() => expect(document.activeElement).toBe(tray.querySelector("textarea")), T);
    await act(async () => window.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true, cancelable: true })));
    expect(tray.classList.contains("show")).toBe(false);
    expect(document.activeElement).toBe(editor);
    expect(window.getSelection().anchorNode).toBe(textNode);
    expect(window.getSelection().anchorOffset).toBe(2);

    client.apiPost.mockResolvedValue({
      mode: "continuation_variants",
      proposals: [{ proposal_id: "p-action", proposal_type: "continuation", content: "屋里只剩炉火的声音。", rationale: "动作推进" }],
    });
    await act(async () => window.dispatchEvent(new KeyboardEvent("keydown", { key: "j", ctrlKey: true, bubbles: true, cancelable: true })));
    await click([...tray.querySelectorAll("button")].find((button) => button.textContent.includes("生成三条")));
    await vi.waitFor(() => expect(tray.textContent).toContain("屋里只剩炉火的声音"), T);
    await click([...tray.querySelectorAll("button")].find((button) => button.textContent === "采纳整段"));
    expect(tray.classList.contains("show")).toBe(false);
    const added = editor.querySelectorAll("p")[1];
    expect(added.textContent).toBe("屋里只剩炉火的声音。");
    expect(document.activeElement).toBe(editor);
    expect(added.contains(window.getSelection().anchorNode) || window.getSelection().anchorNode === added).toBe(true);
  });
});

describe("写作台 · 大纲里的改名与输入法", () => {
  it("输入法确认候选的那一下回车不算改完；真正的回车才提交", async () => {
    const { WriterRoom } = await loadWriter({ catalog: [DEFAULT_CHAP, SECOND_CHAP] });
    const rename = vi.spyOn(window.WsCatalog, "renameScene");
    const host = await render(<WriterRoom t={{}} setTweak={() => {}} />);
    await click(outline(host).querySelector('[aria-label="重命名 夜航"]'));
    const input = outline(host).querySelector(".wr-sc-edit");
    const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value").set;
    await act(async () => { setter.call(input, "夜航二"); input.dispatchEvent(new Event("input", { bubbles: true })); });

    await act(async () => input.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", isComposing: true, bubbles: true, cancelable: true })));
    expect(outline(host).querySelector(".wr-sc-edit")).not.toBeNull();
    expect(rename).not.toHaveBeenCalled();

    await act(async () => input.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", bubbles: true, cancelable: true })));
    expect(outline(host).querySelector(".wr-sc-edit")).toBeNull();
    expect(rename).toHaveBeenCalledWith(expect.anything(), expect.anything(), "夜航二");
  });

  it("行按钮带着「怎么调先后」的说明：雪花整理出来的场指向构思第 9 步，其余的给 Alt + ↑ / ↓", async () => {
    const { WriterRoom } = await loadWriter({ catalog: [DEFAULT_CHAP, SECOND_CHAP] });
    const host = await render(<WriterRoom t={{}} setTweak={() => {}} />);
    const row = [...outline(host).querySelectorAll(".wr-sc-open")].find((node) => node.textContent.includes("夜航"));
    const hint = document.getElementById(row.getAttribute("aria-describedby"));
    expect(hint.textContent).toBe("拖动或 Alt + ↑ / ↓ 调整先后");
    expect(row.getAttribute("aria-keyshortcuts")).toBe("Alt+ArrowUp Alt+ArrowDown");
  });
});

describe("写作台 · AI 续写三候选", () => {
  it("用一次 generate-set 请求取得三份独立续写，不再并发三个同签名 mutation", async () => {
    const { wrContinueMulti, client } = await loadWriter();
    vi.spyOn(window.WrDocs, "draftId").mockResolvedValue("draft-s1");
    client.apiPost.mockResolvedValue({
      mode: "continuation_variants",
      proposals: [
        { proposal_id: "p-action", proposal_type: "continuation", content: "她推门追了出去。", rationale: "动作推进" },
        { proposal_id: "p-relation", proposal_type: "continuation", content: "他没有回头，却放慢了脚步。", rationale: "关系压力" },
        { proposal_id: "p-suspense", proposal_type: "continuation", content: "门外只剩一枚还在发热的钥匙。", rationale: "悬念信息" },
      ],
    });
    client.apiPost.mockClear();

    const candidates = await wrContinueMulti("自然承接下一拍");

    expect(client.apiPost).toHaveBeenCalledTimes(1);
    expect(client.apiPost).toHaveBeenCalledWith(
      "/api/v1/author-drafts/draft-s1/proposals/generate-set",
      {
        mode: "continuation_variants",
        instruction: "自然承接下一拍",
      },
    );
    expect(candidates.map((item) => item.id)).toEqual(["p-action", "p-relation", "p-suspense"]);
    expect(candidates.map((item) => item.html)).toEqual([
      "她推门追了出去。",
      "他没有回头，却放慢了脚步。",
      "门外只剩一枚还在发热的钥匙。",
    ]);
  });
});

/* ---------- 键盘与读屏（2026-09-21 a11y 复审） ---------- */
const THREE = {
  mode: "continuation_variants",
  proposals: [
    { proposal_id: "p1", proposal_type: "continuation", content: "候选一的一句。", rationale: "动作推进" },
    { proposal_id: "p2", proposal_type: "continuation", content: "候选二的一句。", rationale: "关系压力" },
    { proposal_id: "p3", proposal_type: "continuation", content: "候选三的一句。", rationale: "悬念信息" },
  ],
};
const keyOn = async (target, init) => {
  const event = new KeyboardEvent("keydown", { bubbles: true, cancelable: true, ...init });
  await act(async () => { target.dispatchEvent(event); });
  return event;
};
const genCalls = (client) => client.apiPost.mock.calls.filter(([url]) => String(url).includes("generate-set")).length;

async function writerWithCandidates() {
  const { WriterRoom, client } = await loadWriter();
  vi.spyOn(window.WrDocs, "load").mockReturnValue("<p>她把灯关了。</p>");
  vi.spyOn(window.WrDocs, "draftId").mockResolvedValue("draft-s1");
  vi.spyOn(window.WrDocs, "save").mockResolvedValue({});
  const host = await render(<WriterRoom t={{}} setTweak={() => {}} />);
  await vi.waitFor(() => expect(host.textContent).toContain("她把灯关了"), T);
  const editor = host.querySelector(".wr-editor");
  const tray = host.querySelector(".wr-tray");
  client.apiPost.mockClear();
  client.apiPost.mockResolvedValue(THREE);
  await act(async () => { editor.focus(); });
  await keyOn(window, { key: "j", ctrlKey: true });
  // 托盘打开时在下一帧把焦点放进提示框：先等它落定，免得它晚到、抢走测试里放好的焦点
  await vi.waitFor(() => expect(document.activeElement).toBe(tray.querySelector("textarea")), T);
  await click([...tray.querySelectorAll("button")].find((b) => b.textContent.includes("生成三条")));
  await vi.waitFor(() => expect(tray.querySelectorAll(".wr-cand")).toHaveLength(3), T);
  return { host, client, editor, tray };
}

describe("写作台 · 续写托盘的快捷键只认托盘里的按键", () => {
  it("焦点在托盘按钮上时回车是按那个按钮，不是采纳第 1 条；焦点回到正文时 1 / 2 / R / 回车都是作者在打字", async () => {
    const { client, editor, tray } = await writerWithCandidates();
    expect(genCalls(client)).toBe(1);

    const regen = [...tray.querySelectorAll("button")].find((b) => b.textContent.includes("重新生成"));
    await act(async () => { regen.focus(); });
    const onButton = await keyOn(regen, { key: "Enter" });
    expect(onButton.defaultPrevented).toBe(false);          // 浏览器照常把回车交给按钮
    expect(tray.classList.contains("show")).toBe(true);
    expect(editor.textContent).not.toContain("候选一");

    await act(async () => { editor.focus(); });
    for (const key of ["1", "2", "r", "Enter"]) {
      const typed = await keyOn(editor, { key });
      expect(typed.defaultPrevented, key).toBe(false);
    }
    expect(genCalls(client)).toBe(1);
    expect(editor.textContent).not.toContain("候选");
    expect(tray.classList.contains("show")).toBe(true);
  });

  it("候选栏可以用 Tab 走到：在那里按 2 选第二条、输入法组字的回车不采纳、真正的回车采纳第二条", async () => {
    const { editor, tray } = await writerWithCandidates();
    const list = tray.querySelector(".wr-cands");
    expect(list.getAttribute("tabindex")).toBe("0");
    expect(list.getAttribute("aria-keyshortcuts")).toBe("1 2 3 Enter R");
    await act(async () => { list.focus(); });

    expect((await keyOn(list, { key: "2" })).defaultPrevented).toBe(true);
    expect(tray.querySelectorAll(".wr-cand")[1].classList.contains("is-sel")).toBe(true);
    await keyOn(list, { key: "Enter", isComposing: true });
    await keyOn(list, { key: "Enter", keyCode: 229 });
    expect(tray.classList.contains("show")).toBe(true);
    expect(editor.textContent).not.toContain("候选二");

    await keyOn(list, { key: "Enter" });
    expect(tray.classList.contains("show")).toBe(false);
    expect(editor.textContent).toContain("候选二的一句。");
    expect(editor.textContent).not.toContain("候选一");
  });

  it("托盘是模态的：开着时 aria-modal，Tab 在托盘里循环，不会走到被遮罩盖住的正文", async () => {
    const { tray } = await writerWithCandidates();
    expect(tray.getAttribute("aria-modal")).toBe("true");
    const { focusableIn } = await import("./ws-dialog.jsx");
    const nodes = focusableIn(tray);
    await act(async () => { nodes[nodes.length - 1].focus(); });
    await keyOn(document.activeElement, { key: "Tab" });
    expect(document.activeElement).toBe(nodes[0]);
    await keyOn(document.activeElement, { key: "Tab", shiftKey: true });
    expect(document.activeElement).toBe(nodes[nodes.length - 1]);
  });
});

describe("写作台 · 输入法组字中的 Esc 不是命令", () => {
  it("续写提示里组字时按 Esc 不关托盘；沉浸写作时在正文里组字按 Esc 不退出沉浸", async () => {
    const { WriterRoom } = await loadWriter();
    vi.spyOn(window.WrDocs, "load").mockReturnValue("<p>她把灯关了。</p>");
    const host = await render(<WriterRoom t={{}} setTweak={() => {}} />);
    await vi.waitFor(() => expect(host.textContent).toContain("她把灯关了"), T);
    const root = host.querySelector(".wr-root");
    const editor = host.querySelector(".wr-editor");
    const tray = host.querySelector(".wr-tray");

    await act(async () => { editor.focus(); });
    await keyOn(window, { key: "j", ctrlKey: true });
    const prompt = tray.querySelector("textarea");
    await vi.waitFor(() => expect(document.activeElement).toBe(prompt), T);
    await keyOn(prompt, { key: "Escape", isComposing: true });
    await keyOn(prompt, { key: "Escape", keyCode: 229 });
    expect(tray.classList.contains("show")).toBe(true);
    await keyOn(prompt, { key: "Escape" });
    expect(tray.classList.contains("show")).toBe(false);

    await keyOn(editor, { key: ".", ctrlKey: true });
    expect(root.getAttribute("data-immersion")).toBe("on");
    await keyOn(editor, { key: "Escape", isComposing: true });
    expect(root.getAttribute("data-immersion")).toBe("on");
    await keyOn(editor, { key: "Escape" });
    expect(root.getAttribute("data-immersion")).toBe("off");
  });
});

describe("写作台 · 模态层开着时写作台的快捷键不响应", () => {
  it("命令面板这样的对话框开着：⌘J 不在它底下开托盘、⌘1 不收大纲；对话框关了照常", async () => {
    const { WriterRoom } = await loadWriter();
    const { WsDialog } = await import("./ws-dialog.jsx");
    vi.spyOn(window.WrDocs, "load").mockReturnValue("<p>她把灯关了。</p>");
    const host = await render(<WriterRoom t={{}} setTweak={() => {}} />);
    await vi.waitFor(() => expect(host.textContent).toContain("她把灯关了"), T);
    const root = host.querySelector(".wr-root");
    const tray = host.querySelector(".wr-tray");
    expect(root.getAttribute("data-left")).toBe("on");

    const dialogHost = await render(<WsDialog label="命令面板" onClose={() => {}}><input className="probe-input" aria-label="搜索" /></WsDialog>);
    const inside = document.querySelector(".probe-input");
    expect(document.activeElement).toBe(inside);
    await keyOn(inside, { key: "j", ctrlKey: true });
    await keyOn(inside, { key: "1", ctrlKey: true });
    await keyOn(inside, { key: ".", ctrlKey: true });
    expect(tray.classList.contains("show")).toBe(false);
    expect(document.activeElement).toBe(inside);
    expect(root.getAttribute("data-left")).toBe("on");
    expect(root.getAttribute("data-immersion")).toBe("off");

    const [entry] = mounted.splice(mounted.findIndex((m) => m.host === dialogHost), 1);
    await act(async () => entry.root.unmount());
    entry.host.remove();
    await keyOn(host.querySelector(".wr-editor"), { key: "1", ctrlKey: true });
    expect(root.getAttribute("data-left")).toBe("off");
  });
});

describe("写作台 · 大纲改名结束后焦点回到那一场", () => {
  it("Esc 取消 / 回车提交：焦点落在这一场的行按钮上，而不是掉到 <body>", async () => {
    const { WriterRoom } = await loadWriter({ catalog: [DEFAULT_CHAP, SECOND_CHAP] });
    const host = await render(<WriterRoom t={{}} setTweak={() => {}} />);
    const row = () => outline(host).querySelector('[data-scene-open="ch02s1"]') || [...outline(host).querySelectorAll(".wr-sc-open")].find((n) => n.textContent.includes("夜航"));

    await click(outline(host).querySelector('[aria-label="重命名 夜航"]'));
    let input = outline(host).querySelector(".wr-sc-edit");
    await act(async () => { input.focus(); });
    await keyOn(input, { key: "Escape" });
    expect(outline(host).querySelector(".wr-sc-edit")).toBeNull();
    expect(document.activeElement).toBe(row());

    await click(outline(host).querySelector('[aria-label="重命名 夜航"]'));
    input = outline(host).querySelector(".wr-sc-edit");
    await act(async () => { input.focus(); });
    await keyOn(input, { key: "Enter" });
    expect(outline(host).querySelector(".wr-sc-edit")).toBeNull();
    expect(document.activeElement).toBe(row());
  });
});
