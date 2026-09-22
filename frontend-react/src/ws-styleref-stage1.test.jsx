// 风格参考 2026-09-21 重构（第一阶段）的行为契约：
//   (a) 书库：左栏说真话——用于当前作品的书置顶并标「当前作品在用」，进页面落在这本书的「注入应用」；
//       作者打开别的书后，下次进来回到那本（ws_sr_ui_v1）；读书库失败给「重试」而不是空书库；
//   (b) 破坏性动作先确认：重新分类（「更多」菜单）、解除应用，取消就不发请求；
//   (c) 观察的审核失败要回滚并提示（以前失败被吞掉，卡片一直显示「已通过」）；
//   (d) 矩阵：没抽取的格子是中性的「未抽取」，不是「低置信」；方向键只在网格里生效；
//   (e) 注入应用：表单跟着所选作用域的现有绑定走，改了表单「已进入审核」就复位；示例预览要点了才生成。
import React, { act } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("./lib/client.js", () => ({
  apiGet: vi.fn(), apiPost: vi.fn(), apiPatch: vi.fn(), apiDelete: vi.fn(),
  buildUrl: (path) => path,
  getOperatorRef: () => "operator",
  getRemoteAccessToken: () => null,
}));
vi.mock("./ws-works.jsx", () => ({
  WsWorks: { active: () => ({ id: "prj-main", title: "北岸手记" }), activeId: () => "prj-main" },
}));
vi.mock("./ws-review.jsx", () => ({ rvPush: vi.fn() }));

globalThis.IS_REACT_ACT_ENVIRONMENT = true;
const T = { timeout: 5000, interval: 25 };
const mounted = [];

const SUB_DIMS = { "language.sentence_structure": { confidence: "high", observation_count: 2, forbidden_pattern_count: 0, quote_count: 2 } };

/* 两本书：bkA 没抽取过；bkB 有画像、正用于当前作品（项目级绑定） */
function library({ bindingsB = null, findingsB = null } = {}) {
  const books = [
    { book_id: "bkA", title: "甲书", author_label: null, total_chars: 20000, status: "ready" },
    { book_id: "bkB", title: "乙书", author_label: "某作者", total_chars: 90000, status: "ready" },
  ];
  const profileB = {
    profile_id: "pB", book_id: "bkB", run_id: "runB", title: "乙书风格画像", status: "active",
    profile_json: { sub_dimensions: SUB_DIMS, qualitative_summary: "短句为骨。" }, coverage_json: { findings_count: 2, sub_dim_count: 1 },
  };
  return {
    books,
    profiles: [profileB],
    deep: {
      bkA: { book: { book_id: "bkA", title: "甲书", status: "ready", stats_json: {} }, runs: [], findings: [], profiles: [], bindings: [] },
      bkB: {
        book: { book_id: "bkB", title: "乙书", status: "ready", stats_json: { input_assessment: { language: "high" } } },
        runs: [{ run_id: "runB", status: "done", finished_at: "2026-09-01T00:00:00Z" }],
        findings: findingsB || [
          { finding_id: "f1", sub_dimension: "language.sentence_structure", finding_kind: "observation", confidence: "high", statement: "短句为骨", status: "pending", evidence: [{ quote_text: "一句", paragraph_id: "sr_para_x_41" }, { quote_text: "两句" }] },
        ],
        profiles: [profileB],
        bindings: bindingsB || [{ binding_id: "bindB", profile_id: "pB", scope: "project", scope_ref_id: "prj-main", strategy: "B", status: "active", config_json: { intensity: 40, draft_mode: "neutral_first", sub_dimensions: ["language.sentence_structure"] } }],
      },
    },
  };
}

function install(client, lib, { booksFail = false } = {}) {
  client.apiGet.mockImplementation((url) => {
    if (url === "/api/v2/style-reference/activity") return Promise.resolve({ items: [] });
    if (url === "/api/v2/style-reference/books") return booksFail ? Promise.reject(Object.assign(new Error("连接被拒绝"), { code: "NETWORK" })) : Promise.resolve({ books: lib.books });
    if (url === "/api/v2/style-reference/profiles") return Promise.resolve({ profiles: lib.profiles });
    if (url.startsWith("/api/v2/style-reference/injection/layers?")) {
      const layers = lib.layers !== undefined ? lib.layers
        : [{ scope: "project", scope_ref_id: "prj-main", binding_id: "bindB", profile_id: "pB", strategy: "B", weight: 1, budget_chars: 900, fragment_count: 3 }];
      return Promise.resolve({ layers, merged: { layer_count: layers.length, prefix_chars: 900 }, budget_total: 900 });
    }
    const m = /^\/api\/v2\/style-reference\/books\/([^/?]+)(\/runs)?$/.exec(url);
    if (m) {
      const d = lib.deep[m[1]];
      return Promise.resolve(m[2] ? { runs: d.runs } : { book: d.book });
    }
    if (/^\/api\/v2\/style-reference\/runs\/runB\/findings/.test(url)) return Promise.resolve({ findings: lib.deep.bkB.findings });
    if (url.startsWith("/api/v2/style-reference/profiles?book_id=")) {
      const id = decodeURIComponent(url.split("=")[1]);
      return Promise.resolve({ profiles: lib.deep[id] ? lib.deep[id].profiles : [] });
    }
    if (/\/profiles\/pB\/bindings$/.test(url)) return Promise.resolve({ bindings: lib.deep.bkB.bindings });
    if (/\/profiles\/pB\/reports$/.test(url)) return Promise.resolve({ reports: lib.deep.bkB.reports || [] });
    if (/\/profiles\/pB\/banned-terms$/.test(url)) return Promise.resolve({ terms: [] });
    if (url === "/api/v2/style-reference/injection/task-defaults") return Promise.resolve({ tasks: [] });
    if (/\/catalog(\?|$)/.test(url)) return Promise.resolve({ chapters: [] });
    if (/\/library(\?|$)/.test(url)) return Promise.resolve({ characters: [] });
    return Promise.resolve({});
  });
  client.apiPost.mockImplementation((url) => {
    if (/\/injection-preview$/.test(url)) return Promise.resolve({ fragments: { positive_block: "[正向]\n- 短句" }, prefix: "x", stats: { positive_lines: 1, total_prefix_chars: 1 } });
    return Promise.resolve({});
  });
  client.apiDelete.mockResolvedValue({});
}

async function load(lib = library(), opts) {
  const client = await import("./lib/client.js");
  install(client, lib, opts);
  const mod = await import("./ws-styleref.jsx");
  const review = await import("./ws-review.jsx");
  return { mod, client, rvPush: review.rvPush };
}

async function render(node) {
  const host = document.createElement("div");
  document.body.appendChild(host);
  const root = createRoot(host);
  mounted.push({ root, host });
  await act(async () => root.render(node));
  return { host, root };
}

const click = (node) => act(async () => node.dispatchEvent(new MouseEvent("click", { bubbles: true })));
const postUrls = (client) => client.apiPost.mock.calls.map((c) => c[0]);
const railTitles = (host) => [...host.querySelectorAll(".sr-books .sr-book .sr-book-title")].map((n) => n.textContent);
const currentStep = (host) => {
  const node = host.querySelector('.sr-step[aria-current="step"]');
  return node ? node.getAttribute("aria-label") : null;
};

beforeEach(() => {
  vi.resetModules();
  window.localStorage.clear();
  vi.spyOn(window, "alert").mockImplementation(() => {});
});

afterEach(async () => {
  while (mounted.length) {
    const { root, host } = mounted.pop();
    await act(async () => root.unmount());
    host.remove();
  }
  document.body.innerHTML = "";
  vi.useRealTimers();
  vi.restoreAllMocks();
});

describe("书库说真话、落点对", () => {
  it("用于当前作品的书置顶并标「当前作品在用」，进页面就落在它的「注入应用」", async () => {
    const { mod } = await load();
    const { host } = await render(<mod.WsStyleRef go={vi.fn()} />);
    await vi.waitFor(() => expect(railTitles(host)).toEqual(["乙书", "甲书"]), T);
    const rows = [...host.querySelectorAll(".sr-books .sr-book")];
    await vi.waitFor(() => expect(rows[0].textContent).toContain("当前作品在用"), T);
    expect(rows[1].textContent).not.toContain("当前作品在用");
    expect(rows[1].textContent).not.toContain("待抽取 · ");
    await vi.waitFor(() => expect(host.querySelector('.sr-books .sr-book[aria-current="true"] .sr-book-title').textContent).toBe("乙书"), T);
    await vi.waitFor(() => expect(currentStep(host)).toMatch(/^注入应用/), T);
    // 页头：作者 + 字数，标记取书名首字；步骤条来自真实数据（有 active 绑定 → 注入应用已完成）
    expect(host.querySelector(".sr-stage-mark").textContent).toBe("乙");
    expect(host.querySelector(".sr-stage-meta").textContent).toContain("某作者");
    expect(host.querySelector('.sr-step[aria-label^="注入应用"]').className).toContain("is-done");
    expect(host.querySelector('.sr-step[aria-label^="回测校验"]').className).toContain("is-todo");
    mod.srActivityStop();
  });

  it("作者换到别的书后，离开页面再回来接着看那本；刷新后回到当前作品在用的书（本机记录不压过它）", async () => {
    const { mod } = await load();
    const first = await render(<mod.WsStyleRef go={vi.fn()} />);
    await vi.waitFor(() => expect(railTitles(first.host)).toEqual(["乙书", "甲书"]), T);
    await click([...first.host.querySelectorAll(".sr-books .sr-book")].find((b) => b.textContent.includes("甲书")));
    await vi.waitFor(() => expect(currentStep(first.host)).toMatch(/^维度矩阵/), T);
    expect(JSON.parse(window.localStorage.getItem("ws_sr_ui_v1")).works["prj-main"].bookId).toBe("bkA");
    await act(async () => first.root.unmount());
    mounted.splice(mounted.findIndex((m) => m.root === first.root), 1);

    const again = await render(<mod.WsStyleRef go={vi.fn()} />);
    await vi.waitFor(() => expect(again.host.querySelector(".sr-stage-title")?.textContent).toBe("甲书"), T);
    // 甲书没有抽取产物：格子是中性的「未抽取」，不是「低置信」
    await vi.waitFor(() => expect(again.host.querySelectorAll(".sr-cell.conf-none").length).toBe(16), T);
    expect(again.host.querySelector(".sr-cell.conf-low")).toBeNull();
    expect(again.host.querySelector(".sr-findings").textContent).not.toContain("低置信");
    mod.srActivityStop();
    await act(async () => again.root.unmount());
    mounted.splice(mounted.findIndex((m) => m.root === again.root), 1);

    // 刷新（模块重新加载，本次会话的记录没了；本机记录还指着甲书）：落在当前作品在用的乙书
    vi.resetModules();
    const reloaded = await load();
    const fresh = await render(<reloaded.mod.WsStyleRef go={vi.fn()} />);
    await vi.waitFor(() => expect(fresh.host.querySelector(".sr-stage-title")?.textContent).toBe("乙书"), T);
    await vi.waitFor(() => expect(currentStep(fresh.host)).toMatch(/^注入应用/), T);
    expect(JSON.parse(window.localStorage.getItem("ws_sr_ui_v1")).works["prj-main"].bookId).toBe("bkA");
    reloaded.mod.srActivityStop();
  });

  it("书库读失败：给出原因和「重试」，不说书库是空的", async () => {
    const { mod, client } = await load(library(), { booksFail: true });
    const { host } = await render(<mod.WsStyleRef go={vi.fn()} />);
    await vi.waitFor(() => expect(host.textContent).toContain("读不到参考书库"), T);
    expect(host.textContent).not.toContain("参考书库还是空的");
    // 只有一处报错、一个「重试」（左栏只标一行）；错误代码不进正文，只在悬停提示里
    expect([...host.querySelectorAll("button")].filter((b) => b.textContent.includes("重试"))).toHaveLength(1);
    expect(host.querySelector(".sr-lib-error").textContent).toContain("读不到书库");
    expect(host.textContent).toContain("连接被拒绝");
    expect(host.textContent).not.toContain("NETWORK");
    expect(host.querySelector('[title="错误代码：NETWORK"]')).not.toBeNull();
    install(client, library());
    const retry = [...host.querySelectorAll("button")].find((b) => b.textContent.includes("重试"));
    await click(retry);
    await vi.waitFor(() => expect(railTitles(host)).toEqual(["乙书", "甲书"]), T);
    mod.srActivityStop();
  });
});

describe("破坏性动作先确认", () => {
  it("重新分类在「更多」里，确认框说清要删什么；取消不发请求，确认才发", async () => {
    const confirm = vi.spyOn(window, "confirm").mockReturnValue(false);
    const { mod, client } = await load();
    const { host } = await render(<mod.WsStyleRef go={vi.fn()} />);
    await vi.waitFor(() => expect(currentStep(host)).toMatch(/^注入应用/), T);
    // 页头不再直接摆着「重新分类」
    expect(host.querySelector('[data-testid="sr-header-reclassify"]')).toBeNull();
    await click(host.querySelector('button[aria-label="这本书的更多操作"]'));
    const item = host.querySelector('[data-testid="sr-header-reclassify"]');
    expect(item.getAttribute("role")).toBe("menuitem");
    await click(item);
    await vi.waitFor(() => expect(confirm).toHaveBeenCalledTimes(1), T);
    // 菜单项已卸载：焦点回到「更多」按钮（确认框关掉后焦点才有地方回），不是掉到 body
    expect(document.activeElement).toBe(host.querySelector('button[aria-label="这本书的更多操作"]'));
    const asked = confirm.mock.calls[0][0];
    expect(asked).toContain("重新分类《乙书》");
    expect(asked).toContain("风格画像 1 份");
    expect(asked).toContain("《北岸手记》正在用这本书的风格");
    expect(postUrls(client).some((u) => u.endsWith("/reclassify"))).toBe(false);

    confirm.mockReturnValue(true);
    await click(host.querySelector('button[aria-label="这本书的更多操作"]'));
    await click(host.querySelector('[data-testid="sr-header-reclassify"]'));
    await vi.waitFor(() => expect(postUrls(client)).toContain("/api/v2/style-reference/books/bkB/reclassify"), T);
    mod.srActivityStop();
  });

  it("解除应用要确认；取消不删，确认才 DELETE", async () => {
    const confirm = vi.spyOn(window, "confirm").mockReturnValue(false);
    const { mod, client } = await load();
    const book = { id: "bkB", real: true, title: "乙书", author: "某作者", chars: 90000, color: "crimson", rawStatus: "ready" };
    const { host } = await render(<mod.SrApply book={book} go={vi.fn()} />);
    const unbindBtn = () => [...host.querySelectorAll(".sr-binding button")].find((b) => b.textContent.includes("解除应用"));
    await vi.waitFor(() => expect(unbindBtn()).toBeTruthy(), T);
    // 绑定行显示作品名，不印 project id
    expect(host.querySelector(".sr-binding-target").textContent).toContain("《北岸手记》");
    expect(host.querySelector(".sr-binding-target").textContent).not.toContain("prj-main");
    await click(unbindBtn());
    await vi.waitFor(() => expect(confirm).toHaveBeenCalledTimes(1), T);
    expect(confirm.mock.calls[0][0]).toContain("起草时不再带这本书的风格");
    expect(client.apiDelete).not.toHaveBeenCalled();
    confirm.mockReturnValue(true);
    await click(unbindBtn());
    await vi.waitFor(() => expect(client.apiDelete).toHaveBeenCalledWith("/api/v2/style-reference/bindings/bindB"), T);
    mod.srActivityStop();
  });
});

describe("观察的审核：失败回滚", () => {
  it("审核请求失败 → 卡片回到「待审」并提示；成功才保持「已通过」", async () => {
    const { mod, client } = await load();
    const book = { id: "bkB", real: true, title: "乙书", author: "某作者", chars: 90000, color: "crimson", rawStatus: "ready" };
    const { host } = await render(<mod.SrMatrix book={book} go={vi.fn()} />);
    await vi.waitFor(() => expect(host.querySelector(".sr-finding")).toBeTruthy(), T);
    // 证据徽标写「第 N 段」，不印段落 id
    expect(host.querySelector(".sr-ev-badge.quote").textContent).toBe("第 42 段");
    const state = () => host.querySelector(".sr-rev-state").textContent;
    expect(state()).toBe("待审");

    client.apiPost.mockImplementation((url) => (url.endsWith("/review") ? Promise.reject(new Error("服务端拒绝")) : Promise.resolve({})));
    await click(host.querySelector('button[aria-label="通过这条"]'));
    await vi.waitFor(() => expect(window.alert).toHaveBeenCalledTimes(1), T);
    expect(window.alert.mock.calls[0][0]).toContain("审核没有保存");
    await vi.waitFor(() => expect(state()).toBe("待审"), T);
    expect(host.querySelector('button[aria-label="通过这条"]').getAttribute("aria-pressed")).toBe("false");

    client.apiPost.mockImplementation(() => Promise.resolve({}));
    await click(host.querySelector('button[aria-label="通过这条"]'));
    await vi.waitFor(() => expect(postUrls(client).filter((u) => u.endsWith("/findings/f1/review"))).toHaveLength(2), T);
    expect(state()).toBe("已通过");
    expect(window.alert).toHaveBeenCalledTimes(1);
    mod.srActivityStop();
  });
});

describe("矩阵键盘", () => {
  it("方向键只在网格里移动选中格；网格外的方向键不被拦截（以前挂在 window 上抢走整页滚动）", async () => {
    const { mod } = await load();
    const book = { id: "bkB", real: true, title: "乙书", chars: 90000, color: "crimson", rawStatus: "ready" };
    const { host } = await render(<mod.SrMatrix book={book} go={vi.fn()} />);
    await vi.waitFor(() => expect(host.querySelector('[role="grid"]')).toBeTruthy(), T);
    const outside = new KeyboardEvent("keydown", { key: "ArrowDown", bubbles: true, cancelable: true });
    document.body.dispatchEvent(outside);
    expect(outside.defaultPrevented).toBe(false);

    const selected = () => host.querySelector('[role="gridcell"][aria-selected="true"]');
    expect(selected().getAttribute("aria-label")).toContain("句式结构");
    expect(selected().tabIndex).toBe(0);
    const inside = new KeyboardEvent("keydown", { key: "ArrowRight", bubbles: true, cancelable: true });
    await act(async () => { selected().dispatchEvent(inside); });
    expect(inside.defaultPrevented).toBe(true);
    expect(selected().getAttribute("aria-label")).toContain("词汇选择");
    expect(document.activeElement).toBe(selected());
    mod.srActivityStop();
  });
});

describe("注入应用：表单跟着现有绑定走", () => {
  it("项目已有绑定：表单按它的策略 / 强度 / 起草方式 / 维度填好；改了任何一项，「已进入审核」复位", async () => {
    const { mod, rvPush } = await load();
    const book = { id: "bkB", real: true, title: "乙书", author: "某作者", chars: 90000, color: "crimson", rawStatus: "ready" };
    const { host } = await render(<mod.SrApply book={book} go={vi.fn()} />);
    await vi.waitFor(() => expect(host.querySelector(".sr-intensity-val")?.textContent).toBe("40%"), T);
    expect(host.querySelector('input[name="sr-draft-mode"][value="neutral_first"]').checked).toBe(true);
    expect(host.querySelector('.sr-strat[aria-checked="true"] .sr-strat-badge').textContent).toBe("B");
    expect(host.querySelector('[data-testid="sr-apply-shadow"]').textContent).toContain("将替换当前绑定");

    const applyBtn = () => host.querySelector(".sr-apply-btn");
    await click(applyBtn());
    expect(rvPush).toHaveBeenCalledTimes(1);
    const effect = rvPush.mock.calls[0][0].actions.find((a) => a.effect).effect;
    expect(effect).toMatchObject({ strategy: "B", intensity: 40, draft_mode: "neutral_first", scope: "project", scope_ref_id: "prj-main" });
    expect(applyBtn().textContent).toContain("已进入审核");
    expect(applyBtn().disabled).toBe(true);

    await click(host.querySelector('input[name="sr-draft-mode"][value="style_first"]'));
    await vi.waitFor(() => expect(applyBtn().textContent).toContain("应用到项目 · 进审核"), T);
    expect(applyBtn().disabled).toBe(false);
    mod.srActivityStop();
  });

  it("注入策略是一个单选组：整组只有选中的那张卡能 Tab 到，←→ / Home / End 换策略并把焦点带过去", async () => {
    const { mod } = await load();
    const book = { id: "bkB", real: true, title: "乙书", author: "某作者", chars: 90000, color: "crimson", rawStatus: "ready" };
    const { host } = await render(<mod.SrApply book={book} go={vi.fn()} />);
    await vi.waitFor(() => expect(host.querySelector('.sr-strat[aria-checked="true"] .sr-strat-badge')?.textContent).toBe("B"), T);
    const cards = () => [...host.querySelectorAll('.sr-strat-row [role="radio"]')];
    const checked = () => host.querySelector('.sr-strat[aria-checked="true"] .sr-strat-badge').textContent;
    expect(cards().map((c) => c.tabIndex)).toEqual([-1, -1, 0, -1]);   // A+B / A / B / C，选中的是 B

    const key = async (k) => {
      const event = new KeyboardEvent("keydown", { key: k, bubbles: true, cancelable: true });
      await act(async () => { document.activeElement.dispatchEvent(event); });
      return event;
    };
    await act(async () => { cards()[2].focus(); });
    expect((await key("ArrowRight")).defaultPrevented).toBe(true);
    expect(checked()).toBe("C");
    expect(document.activeElement).toBe(cards()[3]);
    expect(cards().map((c) => c.tabIndex)).toEqual([-1, -1, -1, 0]);
    await key("ArrowRight");                                            // 末尾绕回开头
    expect(checked()).toBe("A+B");
    expect(document.activeElement).toBe(cards()[0]);
    await key("End");
    expect(checked()).toBe("C");
    await key("Home");
    expect(checked()).toBe("A+B");
    await key("ArrowLeft");
    expect(checked()).toBe("C");
    mod.srActivityStop();
  });

  it("示例预览要点了才生成（以前一打开页签就连发三次模型调用）", async () => {
    const { mod, client } = await load();
    const book = { id: "bkB", real: true, title: "乙书", chars: 90000, color: "crimson", rawStatus: "ready" };
    const { host } = await render(<mod.SrProfile book={book} go={vi.fn()} />);
    const previewTab = () => [...host.querySelectorAll('[role="tab"]')].find((b) => b.textContent.includes("示例预览"));
    await vi.waitFor(() => expect(previewTab()).toBeTruthy(), T);
    await click(previewTab());
    await new Promise((r) => setTimeout(r, 50));
    expect(postUrls(client).some((u) => u.endsWith("/preview"))).toBe(false);
    client.apiPost.mockImplementation((url, body) => Promise.resolve({ samples: [{ paragraph_type: body.paragraph_types[0], sample_text: "示例段落", verdict: "pass" }] }));
    await click([...host.querySelectorAll("button")].find((b) => b.textContent.includes("生成 3 段示例")));
    await vi.waitFor(() => expect(postUrls(client).filter((u) => u.endsWith("/profiles/pB/preview"))).toHaveLength(3), T);
    await vi.waitFor(() => expect(host.querySelectorAll(".sr-pv-card").length).toBe(3), T);
    expect(host.textContent).toContain("回测通过");
    expect(host.textContent).not.toContain("PASS");
    mod.srActivityStop();
  });
});

/* ---- 复审修补（2026-09-21）：同一屏上各处说同一件事 ---- */
describe("离开再回来：深层数据重读，步骤条与页头一致", () => {
  it("离开期间跑完了回测、在待办里批准了绑定：回来后步骤条、「当前应用」与页头都跟上", async () => {
    const lib = library({ bindingsB: [] });
    lib.layers = [];
    lib.deep.bkB.reports = [];
    const { mod } = await load(lib);
    const step = (host, name) => host.querySelector(`.sr-step[aria-label^="${name}"]`);

    const first = await render(<mod.WsStyleRef go={vi.fn()} />);
    await vi.waitFor(() => expect(railTitles(first.host)).toEqual(["甲书", "乙书"]), T);
    await click([...first.host.querySelectorAll(".sr-books .sr-book")].find((b) => b.textContent.includes("乙书")));
    await vi.waitFor(() => expect(currentStep(first.host)).toMatch(/^注入应用/), T);
    expect(step(first.host, "注入应用").className).toContain("is-todo");
    expect(step(first.host, "回测校验").className).toContain("is-todo");
    await vi.waitFor(() => expect(first.host.querySelector(".sr-bindings-empty")).toBeTruthy(), T);
    await act(async () => first.root.unmount());
    mounted.splice(mounted.findIndex((m) => m.root === first.root), 1);

    // 离开期间：别处跑完一次回测（绑定没变，「当前作品在用」也没翻转——只能靠重新挂载时重读）
    lib.deep.bkB.reports = [{ report_id: "rep1", verdict: "partial", status: "done", finished_at: "2026-09-20T14:05:00" }];
    const second = await render(<mod.WsStyleRef go={vi.fn()} />);
    await vi.waitFor(() => expect(second.host.querySelector(".sr-stage-title")?.textContent).toBe("乙书"), T);
    await vi.waitFor(() => expect(step(second.host, "回测校验").className).toContain("is-done"), T);
    await act(async () => second.root.unmount());
    mounted.splice(mounted.findIndex((m) => m.root === second.root), 1);

    // 离开期间：在待办里批准了项目级绑定
    lib.deep.bkB.bindings = [{ binding_id: "bindB", profile_id: "pB", scope: "project", scope_ref_id: "prj-main", strategy: "B", status: "active", config_json: { intensity: 40 } }];
    lib.layers = [{ scope: "project", scope_ref_id: "prj-main", binding_id: "bindB", profile_id: "pB", strategy: "B" }];
    const third = await render(<mod.WsStyleRef go={vi.fn()} />);
    await vi.waitFor(() => expect(third.host.querySelector(".sr-stage-meta").textContent).toContain("当前作品在用"), T);
    await vi.waitFor(() => expect(step(third.host, "注入应用").className).toContain("is-done"), T);
    await vi.waitFor(() => expect(third.host.querySelectorAll(".sr-bindings .sr-binding").length).toBe(1), T);
    expect(third.host.querySelector(".sr-bindings-empty")).toBeNull();
    // 表单按新绑定填好
    await vi.waitFor(() => expect(third.host.querySelector(".sr-intensity-val")?.textContent).toBe("40%"), T);
    mod.srActivityStop();
  });

  it("深层数据没到之前，步骤条后四步不写「未开始 / 等前一步」", async () => {
    const { mod, client } = await load();
    const hold = new Promise(() => {}); // 书详情一直不回来：深层数据停在读取中
    const base = client.apiGet.getMockImplementation();
    client.apiGet.mockImplementation((url) => (/^\/api\/v2\/style-reference\/books\/bkB$/.test(url) ? hold : base(url)));
    const { host } = await render(<mod.WsStyleRef go={vi.fn()} />);
    await vi.waitFor(() => expect(host.querySelector(".sr-stage-title")?.textContent).toBe("乙书"), T);
    await vi.waitFor(() => expect(host.querySelector(".sr-stage-meta").textContent).toContain("当前作品在用"), T);
    const states = [...host.querySelectorAll(".sr-step")].map((n) => n.getAttribute("aria-label"));
    expect(states[0]).toBe("概览（已完成）");
    expect(states.slice(1)).toEqual(["维度矩阵", "风格画像", "回测校验", "注入应用"]);
    expect(host.querySelector(".sr-step.is-todo, .sr-step.is-blocked")).toBeNull();
    mod.srActivityStop();
  });
});

describe("回测校验页与步骤条说同一件事", () => {
  it("这份画像已有一份部分通过的回测：页面显示「上次回测：部分通过」与那份报告，不说「还没回测」", async () => {
    const lib = library();
    lib.deep.bkB.reports = [
      { report_id: "rep0", verdict: "pass", status: "done", finished_at: "2026-09-18T09:00:00", quantitative_json: [], semantic_json: [], plagiarism_json: { passed: true, hits: [] }, forbidden_hits_json: [], mode_executed: "sync_only" },
      {
        report_id: "rep1", verdict: "partial", status: "done", finished_at: "2026-09-20T14:05:00", mode_executed: "sync_only",
        quantitative_json: [{ metric: "avg_sentence_length", target_mean: 20, target_std: 4, actual: 31, tolerance: 5, passed: false, deviation_ratio: 2.2 }],
        semantic_json: [], plagiarism_json: { passed: true, hits: [] }, forbidden_hits_json: [],
      },
    ];
    const { mod } = await load(lib);
    const book = { id: "bkB", title: "乙书", author: "某作者", chars: 90000, color: "crimson", rawStatus: "ready" };
    const { host } = await render(<mod.SrValidation book={book} go={vi.fn()} />);
    const verdict = () => host.querySelector('[data-testid="srv-verdict"]');
    await vi.waitFor(() => expect(verdict().textContent).toContain("上次回测：部分通过"), T);
    expect(verdict().textContent).toContain("9 月 20 日 14:05");
    expect(verdict().textContent).toContain("本次还没回测");
    expect(host.textContent).not.toContain("还没回测粘贴");
    expect(host.textContent).toContain("上次回测的报告");
    // 报告本身：量化一项没过（平均句长），不是后台补算的转圈
    expect(host.querySelector(".vrc")).toBeTruthy();
    expect(host.querySelector(".qbar-name").textContent).toBe("平均句长");
    expect(host.textContent).not.toContain("后台补算");
    mod.srActivityStop();
  });

  it("没有任何回测报告：才说「还没回测」", async () => {
    const { mod } = await load();
    const book = { id: "bkB", title: "乙书", chars: 90000, color: "crimson", rawStatus: "ready" };
    const { host } = await render(<mod.SrValidation book={book} go={vi.fn()} />);
    await vi.waitFor(() => expect(host.querySelector('[data-testid="srv-verdict"]')?.textContent).toContain("还没回测"), T);
    expect(host.querySelector(".vrc")).toBeNull();
    mod.srActivityStop();
  });
});

describe("窄屏的书库按钮与概览的重新分类", () => {
  it("「参考书库」按钮上直接写着进行中与失败的条数（≤1280 时活动面板收在抽屉里）", async () => {
    const { mod } = await load();
    const { host } = await render(<mod.WsStyleRef go={vi.fn()} />);
    await vi.waitFor(() => expect(host.querySelector(".sr-stage-title")?.textContent).toBe("乙书"), T);
    const sw = () => host.querySelector('[data-testid="sr-books-switch"]');
    expect(sw().textContent).not.toContain("失败");
    await act(async () => {
      mod.srActivitySet("sr-import-x", { kind: "import", title: "丙书", status: "failed", error: "需要先接入模型", startedAt: Date.now() });
      mod.srActivitySet("run:r9", { kind: "extract", title: "乙书", bookId: "bkB", targetId: "r9", status: "running", startedAt: Date.now() });
    });
    expect(host.querySelector('[data-testid="sr-books-switch-failed"]').textContent).toContain("1 项失败");
    expect(sw().textContent).toContain("1 项进行中");
    mod.srActivityDismiss("sr-import-x");
    mod.srActivityDismiss("run:r9");
    mod.srActivityStop();
  });

  it("正在抽取时概览的「重新分类…」置灰并写明原因（以前点了没反应）", async () => {
    const { mod } = await load();
    const { host } = await render(<mod.WsStyleRef go={vi.fn()} />);
    await vi.waitFor(() => expect(currentStep(host)).toMatch(/^注入应用/), T);
    await click(host.querySelector('.sr-step[aria-label^="概览"]'));
    const btn = () => host.querySelector('[data-testid="sr-overview-reclassify"]');
    await vi.waitFor(() => expect(btn()).toBeTruthy(), T);
    expect(btn().disabled).toBe(false);
    await act(async () => {
      mod.srActivitySet("run:r7", { kind: "extract", title: "乙书", bookId: "bkB", targetId: "r7", status: "running", startedAt: Date.now() });
    });
    expect(btn().disabled).toBe(true);
    expect(host.querySelector('[data-testid="sr-overview-reclassify-hint"]').textContent).toContain("正在后台抽取");
    mod.srActivityDismiss("run:r7");
    mod.srActivityStop();
  });
});

describe("注入预览：旧请求晚到不覆盖新设置的读数", () => {
  it("强度从 100 拖到 10：100 的预览比 10 的晚回来，读数仍是 10 的", async () => {
    const { mod, client } = await load(library({ bindingsB: [] }));
    const pending = [];
    client.apiPost.mockImplementation((url, body) => {
      if (!/\/injection-preview$/.test(url)) return Promise.resolve({});
      return new Promise((resolve) => { pending.push({ body, resolve }); });
    });
    const book = { id: "bkB", title: "乙书", chars: 90000, color: "crimson", rawStatus: "ready" };
    const { host } = await render(<mod.SrApply book={book} go={vi.fn()} />);
    await vi.waitFor(() => expect(pending.some((p) => p.body.intensity === 100)).toBe(true), T);
    const range = host.querySelector(".sr-range");
    await act(async () => {
      Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value").set.call(range, "10");
      range.dispatchEvent(new Event("input", { bubbles: true }));
    });
    await vi.waitFor(() => expect(pending.some((p) => p.body.intensity === 10)).toBe(true), T);
    const readout = () => host.querySelector('[data-testid="sr-intensity-readout"]').textContent;
    await act(async () => { pending.find((p) => p.body.intensity === 10).resolve({ stats: { positive_lines: 2, total_prefix_chars: 900 } }); });
    await vi.waitFor(() => expect(readout()).toContain("共 900 字"), T);
    await act(async () => { pending.filter((p) => p.body.intensity === 100).forEach((p) => p.resolve({ stats: { positive_lines: 9, total_prefix_chars: 2400 } })); });
    await new Promise((r) => setTimeout(r, 20));
    expect(readout()).toContain("共 900 字");
    expect(readout()).not.toContain("2400");
    mod.srActivityStop();
  });
});
