// 阶段 U（2026-09-17）：教练 · 要点 · 方向 · 生成——「候选」页签并入教练之后的视图层契约。
// 1) 编辑页 AI 工具条：「AI 生成本步」直接走 generate（不带方向）；「先看 3 个方向」走 fe-candidates
//    （draft_override 同源、不再折叠 context 文本）并切到教练页，方向卡从回包的 assistant_history 渲染；
// 2) 方向卡「按此生成本步」→ generate 带 direction_text / direction_turn_id / direction_index；回包里带 adoption 的
//    回合打「已按此生成」；02 自由文本步的方向卡是「就用这一句」，直接写草稿、不调模型；
// 3) 教练页：输入框里的要求随「给 3 个方向」作为 ask 上行；教练回复「按此生成本步」带 direction_turn_id（coach_reply）；
//    每轮的要点差异（turn.brief_delta）在日志里那一轮下面显示；
// 4) 要点卡：撤下 / 加条 / 切范围 / 继承都走 SnowSync.saveDirectionBrief；没有「生成时带入」开关；要点改过而本稿
//    没跟上 → 编辑页工具条与要点卡都给「按最新要点重新生成」（source=fe_brief_regen）；右栏只读镜像。
import React, { act } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const catalog = vi.hoisted(() => ({ get: vi.fn(() => []) }));
vi.mock("./ws-catalog.jsx", () => ({ WsCatalog: catalog }));
vi.mock("./ws-works.jsx", () => ({
  wsKey: (base) => `${base}::coach-book`,
  WsWorks: { activeId: () => "coach-book", active: () => ({ id: "coach-book", title: "方向之书" }) },
}));
// 视图从 ws-snow-sync.jsx 直接 import SnowSync。每个用例把自己的假 SnowSync 挂在 window 上，
// 这里的模块 mock 转发过去（用例没给的方法读出来是 undefined）。
vi.mock("./ws-snow-sync.jsx", () => ({
  SnowSync: new Proxy({}, { get: (_target, name) => (window.SnowSync ? window.SnowSync[name] : undefined) }),
}));
vi.mock("./lib/client.js", () => ({
  apiGet: vi.fn(async () => ({})),
  apiPost: vi.fn(async () => ({})),
  apiPatch: vi.fn(async () => ({})),
  apiPut: vi.fn(async () => ({})),
  apiDelete: vi.fn(async () => ({})),
}));

import { WsSnowflake } from "./ws-snow.jsx";
import * as client from "./lib/client.js";


const T = { timeout: 5000, interval: 25 };
const mounted = [];

const BRIEF = {
  step_key: "one_paragraph_summary", revision: 2, inherit_upstream: true, active_count: 2,
  lines: [
    { line_id: "dl_1", kind: "decision", scope: "step", text: "主角是被动卷入", origin: "coach", status: "active" },
    { line_id: "dl_2", kind: "pending", scope: "step", text: "结局是否团圆", origin: "author", status: "active" },
    { line_id: "dl_3", kind: "rejection", scope: "step", text: "不要热血", origin: "coach", status: "dismissed", dismissed_by: "author" },
  ],
  inherited: [{ step_key: "book_brief", step_label: "读者定位", line_id: "dl_0", kind: "constraint", text: "基调冷" }],
};
const DIRECTIONS = [
  { label: "冷处理", tag: "情绪压强", text: "她被旧案拖回雨城，谁也不肯先开口。", notes: ["锁定代价"] },
  { label: "推进向", tag: "情节推进", text: "一封信逼她在三天内回乡。", notes: [] },
  { label: "对照向", tag: "道德对照", text: "她替恩师撒的谎，如今要她自己付账。", notes: [] },
];
const dirTurn = (stepKey, extra = {}) => ({
  turn_id: "turn-dir", step_key: stepKey, turn_kind: "candidates", message: "给我 3 个不同方向", reply: "", suggestions: [],
  candidate_patch: null, source: "llm", created_at: "2026-09-17T00:00:00Z", candidates: DIRECTIONS, adoption: null, brief_delta: null, ...extra,
});
const CHAT_TURN = {
  turn_id: "turn-chat", step_key: "one_paragraph_summary", turn_kind: "chat", message: "主角被动一点", reply: "先把主角的被动写实。",
  suggestions: ["第一灾之前他不出手"], candidate_patch: null, source: "llm", created_at: "2026-09-17T00:00:01Z", candidates: [],
  adoption: null, brief_delta: { added: ["dl_9"], updated: [], superseded: ["dl_2"], kept: 1 },
};
const STEP_KEY = { paragraph: "one_paragraph_summary", logline: "one_sentence_summary" };

async function renderSnow(step = "paragraph") {
  const host = document.createElement("div");
  document.body.appendChild(host);
  const root = createRoot(host);
  mounted.push({ root, host });
  await act(async () => root.render(<WsSnowflake initialStep={step} />));
  return host;
}
async function openCoach(host) {
  const tab = Array.from(host.querySelectorAll('[role="tab"]')).find(b => (b.textContent || "").includes("教练"));
  expect(tab).toBeTruthy();
  await act(async () => tab.click());
  await vi.waitFor(() => expect(host.querySelector('[data-testid="snow-brief-card"]')).toBeTruthy(), T);
  return host.querySelector('[data-testid="snow-brief-card"]');
}
const setValue = (el, text) => {
  const proto = el.tagName === "TEXTAREA" ? window.HTMLTextAreaElement.prototype : window.HTMLInputElement.prototype;
  Object.getOwnPropertyDescriptor(proto, "value").set.call(el, text);
  el.dispatchEvent(new Event("input", { bubbles: true }));
};
const generateCalls = () => client.apiPost.mock.calls.filter(c => String(c[0]).endsWith("/generate"));

function installApi({ history = [], health = {} } = {}) {
  client.apiGet.mockImplementation(async (url) => (String(url).includes("/snowflake-workspace") ? { assistant_history: history, steps: [], direction_briefs: {} } : {}));
  client.apiPost.mockImplementation(async (url, body) => {
    const u = String(url);
    if (u.endsWith("/fe-candidates")) {
      const stepKey = u.split("/steps/")[1].split("/")[0];
      const turn = dirTurn(stepKey, { message: (body && body.ask) || "给我 3 个不同方向" });
      return { source: "llm", llm_call_id: "call-1", candidates: DIRECTIONS, turn_id: turn.turn_id, turn, assistant_history: [...history, turn] };
    }
    if (u.endsWith("/assistant")) {
      return { reply: "记下了。", suggestions: [], source: "llm", turn_id: "turn-2", assistant_history: [...history, { ...CHAT_TURN, turn_id: "turn-2", message: body.message, reply: "记下了。" }],
        direction_brief: { ...BRIEF, revision: 3 }, brief_delta: { added: ["dl_9"], updated: [], superseded: [], kept: 2 } };
    }
    if (u.endsWith("/generate")) {
      const stepKey = u.split("/steps/")[1].split("/")[0];
      const adopted = (body && body.direction_turn_id)
        ? history.map(t => (t.turn_id === body.direction_turn_id ? { ...t, adoption: { step_run_id: "run-2", candidate_index: body.direction_index ?? null, adopted_at: "2026-09-17T00:00:02Z" } } : t))
        : history;
      return { step: { step_key: stepKey, draft: {}, health: {} }, workspace: { direction_briefs: {}, assistant_history: adopted } };
    }
    return {};
  });
  window.SnowSync = {
    directionBrief: vi.fn(() => BRIEF),
    briefUsage: vi.fn(() => ({ hasBrief: true, stale: true, disabled: false, usedRevision: 1, currentRevision: 2 })),
    saveDirectionBrief: vi.fn(async () => BRIEF),
    setDirectionBrief: vi.fn(),
    captureBriefs: vi.fn(),
    health: vi.fn(() => health),
    canonDraft: vi.fn(() => ({})),
    pushCanon: vi.fn(() => ({ sentences: ["她回到雨城。", "", "", "", ""], moral_premise: "" })),
    applyServerStep: vi.fn(() => null),
    applyCanonPatch: vi.fn(() => null),
    chapterPreview: vi.fn(async () => ({})),
    materialize: vi.fn(async () => ({})),
  };
}

describe("阶段 U · 教练 · 要点 · 方向 · 生成", () => {
  beforeEach(() => {
    vi.spyOn(window, "confirm").mockReturnValue(true);
    installApi();
  });

  afterEach(async () => {
    while (mounted.length) {
      const { root, host } = mounted.pop();
      await act(async () => root.unmount());
      host.remove();
    }
    vi.restoreAllMocks();
    try { delete window.SnowSync; } catch (e) {}
  });

  it("编辑页 AI 工具条：「AI 生成本步」直接生成（不带方向、不带 use_direction_brief）；提示本稿出处与要点落后", async () => {
    installApi({ health: { paragraph: { generationSource: "llm", direction: { kind: "candidate", label: "推进向", turn_id: "turn-dir", candidate_index: 1 }, directionBrief: { used: true, revision: 1 } } } });
    const host = await renderSnow("paragraph");
    const bar = host.querySelector('[data-testid="snow-aibar"]');
    expect(bar).toBeTruthy();
    expect(host.querySelector('[data-testid="snow-ai-brief"]').textContent).toContain("2");
    const prov = host.querySelector('[data-testid="snow-ai-provenance"]');
    expect(prov.textContent).toContain("按方向「推进向」生成");
    expect(prov.textContent).toContain("第 1 版要点");
    expect(prov.textContent).toContain("第 2 版");
    // 没有「候选」页签了
    expect(Array.from(host.querySelectorAll('[role="tab"]')).map(b => b.textContent.trim())).not.toContain("候选 0");
    expect(Array.from(host.querySelectorAll('[role="tab"]')).some(b => (b.textContent || "").includes("候选"))).toBe(false);

    await act(async () => host.querySelector('[data-testid="snow-ai-generate"]').click());
    await vi.waitFor(() => expect(generateCalls().length).toBe(1), T);
    const [url, body] = generateCalls()[0];
    expect(url).toContain("/steps/one_paragraph_summary/generate");
    expect(body).toMatchObject({ require_llm: true, source: "fe_scaffold_ai" });
    expect(body.direction_text).toBeUndefined();
    expect(body.direction_turn_id).toBeUndefined();
    expect("use_direction_brief" in body).toBe(false);
    expect(body.draft_override).toMatchObject({ sentences: ["她回到雨城。", "", "", "", ""] });

    // 要点落后 → 工具条上的「按最新要点重新生成」
    await act(async () => host.querySelector('[data-testid="snow-ai-regen-brief"]').click());
    await vi.waitFor(() => expect(generateCalls().length).toBe(2), T);
    expect(generateCalls()[1][1]).toMatchObject({ source: "fe_brief_regen" });
  });

  it("「先看 3 个方向」→ fe-candidates（draft_override，不折叠 context）→ 教练页方向卡 →「按此生成本步」带回合与编号，回包打「已按此生成」", async () => {
    const host = await renderSnow("paragraph");
    await act(async () => host.querySelector('[data-testid="snow-ai-directions"]').click());
    await vi.waitFor(() => expect(host.querySelectorAll('[data-testid="snow-direction-card"]').length).toBe(3), T);
    const dirCall = client.apiPost.mock.calls.find(c => String(c[0]).endsWith("/fe-candidates"));
    expect(dirCall[0]).toContain("/steps/one_paragraph_summary/fe-candidates");
    expect(dirCall[1]).toMatchObject({ target_chars: 300, draft_override: { sentences: ["她回到雨城。", "", "", "", ""] } });
    expect(dirCall[1].context).toBeUndefined();
    expect(dirCall[1].ask).toBeUndefined();
    expect(dirCall[1].use_direction_brief).toBeUndefined();
    // 教练页已打开，方向回合在日志里，「我」那一行是默认句
    expect(host.querySelector('[data-testid="snow-coach-turn-directions"]').textContent).toContain("给我 3 个不同方向");
    const cards = host.querySelectorAll('[data-testid="snow-direction-card"]');
    expect(cards[1].textContent).toContain("推进向");
    expect(cards[1].textContent).toContain("一封信逼她在三天内回乡。");
    expect(host.querySelector('[data-testid="snow-direction-use-text"]')).toBeNull(); // 脚手架步没有「就用这一句」

    // 让 generate 的回包把这一回合标成已采纳（history 里此时已有方向回合）
    const history = [dirTurn("one_paragraph_summary")];
    client.apiPost.mockImplementation(async (url, body) => {
      if (String(url).endsWith("/generate")) {
        return { step: { step_key: "one_paragraph_summary", draft: {}, health: {} }, workspace: { direction_briefs: {},
          assistant_history: history.map(t => ({ ...t, adoption: { step_run_id: "run-2", candidate_index: body.direction_index, adopted_at: "x" } })) } };
      }
      return {};
    });
    await act(async () => cards[1].querySelector('[data-testid="snow-direction-adopt"]').click());
    await vi.waitFor(() => expect(generateCalls().length).toBe(1), T);
    expect(generateCalls()[0][1]).toMatchObject({
      require_llm: true, source: "fe_candidate_adopt", direction_kind: "candidate",
      direction_text: "一封信逼她在三天内回乡。", direction_turn_id: "turn-dir", direction_index: 1,
    });
    // 生成后切回编辑页；再开教练页，那张卡打了「已按此生成」
    await vi.waitFor(() => expect(host.querySelector('[data-testid="snow-aibar"]')).toBeTruthy(), T);
    await openCoach(host);
    await vi.waitFor(() => expect(host.querySelectorAll('[data-testid="snow-direction-card"]')[1].textContent).toContain("已按此生成"), T);
    expect(host.querySelectorAll('[data-testid="snow-direction-card"]')[0].textContent).not.toContain("已按此生成");
  });

  it("LLM 未配置：「先看 3 个方向」的 409 在教练页留下可关闭的错误行，日志里不出现方向回合", async () => {
    const host = await renderSnow("paragraph");
    client.apiPost.mockImplementation(async (url) => {
      if (String(url).endsWith("/fe-candidates")) { const e = new Error("雪花工作台的 AI 生成需要先启用真实模型。"); e.status = 409; e.code = "SNOWFLAKE_LLM_NOT_CONFIGURED"; throw e; }
      return {};
    });
    await act(async () => host.querySelector('[data-testid="snow-ai-directions"]').click());
    await vi.waitFor(() => expect(host.querySelector('[data-testid="snow-coach-error"]')).toBeTruthy(), T);
    expect(host.querySelector('[data-testid="snow-coach-error"]').textContent).toContain("需要先启用真实模型");
    expect(host.querySelector('[data-testid="snow-coach-turn-directions"]')).toBeNull();
    await act(async () => host.querySelector('[data-testid="snow-coach-error"] button').click());
    expect(host.querySelector('[data-testid="snow-coach-error"]')).toBeNull();
  });

  it("生成期间只有被点的那张方向卡显示「生成中…」，其余卡与教练回复只是禁用、文案不变", async () => {
    installApi({ history: [CHAT_TURN, dirTurn("one_paragraph_summary")] });
    const host = await renderSnow("paragraph");
    await openCoach(host);
    await vi.waitFor(() => expect(host.querySelectorAll('[data-testid="snow-direction-adopt"]').length).toBe(3), T);
    let release = null;
    client.apiPost.mockImplementation((url) => (String(url).endsWith("/generate")
      ? new Promise(resolve => { release = () => resolve({ step: { step_key: "one_paragraph_summary", draft: {}, health: {} }, workspace: { direction_briefs: {}, assistant_history: [] } }); })
      : Promise.resolve({})));
    const buttons = () => Array.from(host.querySelectorAll('[data-testid="snow-direction-adopt"]'));
    await act(async () => buttons()[1].click());
    await vi.waitFor(() => expect(buttons()[1].textContent).toContain("生成中"), T);
    expect(buttons()[0].textContent).toContain("按此生成本步");
    expect(buttons()[2].textContent).toContain("按此生成本步");
    expect(buttons().map(b => b.disabled)).toEqual([true, true, true]);
    const cards = host.querySelectorAll('[data-testid="snow-direction-card"]');
    expect(cards[1].className).toContain("is-busy");
    expect(cards[0].className).not.toContain("is-busy");
    const coachAdopt = host.querySelector('[data-testid="snow-coach-adopt"]');
    expect(coachAdopt.disabled).toBe(true);
    expect(coachAdopt.textContent).toContain("按此生成本步");
    expect(coachAdopt.textContent).not.toContain("生成中");
    await act(async () => { release(); });
    await vi.waitFor(() => expect(host.querySelector('[data-testid="snow-aibar"]')).toBeTruthy(), T);
    // 生成结束回到编辑页：工具条按钮恢复可点、不再转圈
    const gen = host.querySelector('[data-testid="snow-ai-generate"]');
    expect(gen.disabled).toBe(false);
    expect(gen.textContent).toContain("AI 生成本步");
  });

  it("02 一句话概括（自由文本）：方向卡是「就用这一句」，直接写进草稿，不调用 generate", async () => {
    const host = await renderSnow("logline");
    await act(async () => host.querySelector('[data-testid="snow-ai-directions"]').click());
    await vi.waitFor(() => expect(host.querySelectorAll('[data-testid="snow-direction-use-text"]').length).toBe(3), T);
    expect(host.querySelector('[data-testid="snow-direction-adopt"]')).toBeNull();
    await act(async () => host.querySelectorAll('[data-testid="snow-direction-use-text"]')[0].click());
    await vi.waitFor(() => expect(host.querySelector("textarea.edit-text")).toBeTruthy(), T);
    expect(host.querySelector("textarea.edit-text").value).toBe("她被旧案拖回雨城，谁也不肯先开口。");
    expect(generateCalls().length).toBe(0);
  });

  it("教练页：输入框里的要求随「给 3 个方向」作为 ask 上行；教练回复「按此生成本步」带 coach_reply 的回合；要点差异显示在那一轮下面", async () => {
    installApi({ history: [CHAT_TURN] });
    const host = await renderSnow("paragraph");
    await openCoach(host);
    await vi.waitFor(() => expect(host.querySelector('[data-testid="snow-coach-turn"]')).toBeTruthy(), T);
    expect(host.querySelector('[data-testid="snow-coach-delta"]').textContent).toContain("+1");
    expect(host.querySelector('[data-testid="snow-coach-delta"]').textContent).toContain("撤 1");

    const textarea = host.querySelector(".sf-coach-input textarea");
    await act(async () => setValue(textarea, "都要更冷一点"));
    await act(async () => host.querySelector('[data-testid="snow-coach-directions"]').click());
    await vi.waitFor(() => expect(host.querySelectorAll('[data-testid="snow-direction-card"]').length).toBe(3), T);
    const dirCall = client.apiPost.mock.calls.find(c => String(c[0]).endsWith("/fe-candidates"));
    expect(dirCall[1].ask).toBe("都要更冷一点");
    expect(host.querySelector('[data-testid="snow-coach-turn-directions"]').textContent).toContain("都要更冷一点");
    expect(host.querySelector(".sf-coach-input textarea").value).toBe("");

    await act(async () => host.querySelector('[data-testid="snow-coach-adopt"]').click());
    await vi.waitFor(() => expect(generateCalls().length).toBe(1), T);
    expect(generateCalls()[0][1]).toMatchObject({ direction_text: "先把主角的被动写实。", direction_kind: "coach_reply", direction_turn_id: "turn-chat", source: "fe_coach_adopt" });
    expect(generateCalls()[0][1].direction_index).toBeUndefined();

    // 发消息：回包的 direction_brief 落镜像
    await openCoach(host);
    await act(async () => setValue(host.querySelector(".sf-coach-input textarea"), "结局苦乐参半"));
    const send = Array.from(host.querySelectorAll(".sf-coach-input button")).find(b => (b.textContent || "").includes("发送"));
    await act(async () => send.click());
    await vi.waitFor(() => expect(window.SnowSync.setDirectionBrief).toHaveBeenCalledTimes(1), T);
    expect(window.SnowSync.setDirectionBrief.mock.calls[0][1]).toBe("paragraph");
    expect(window.SnowSync.setDirectionBrief.mock.calls[0][2].revision).toBe(3);
    // F02-10：教练的底稿与生成 / 方向同一份（pushCanon：服务端镜像 ⊕ 本地脚手架），不再是不带服务端键的 canonDraft
    const coachCall = client.apiPost.mock.calls.find(c => String(c[0]).endsWith("/assistant"));
    expect(coachCall[1]).toMatchObject({ step_key: "one_paragraph_summary", message: "结局苦乐参半", draft_override: { sentences: ["她回到雨城。", "", "", "", ""] } });
    expect(window.SnowSync.canonDraft).not.toHaveBeenCalled();
  });

  it("教练回复里的 markdown 排成段落 / 粗体 / 斜体 / 列表，不再把星号印出来；「按此生成本步」仍把原文交给模型", async () => {
    const reply = "缺口有两处：\n\n1. **目标**：还不能拍。\n   - *做法*：给一个今晚的期限。\n2. **代价**：还没落地。\n\n<i>这句像标签</i>，照样是文字。";
    installApi({ history: [{ ...CHAT_TURN, reply, suggestions: ["先把**代价**写实"] }] });
    const host = await renderSnow("paragraph");
    await openCoach(host);
    await vi.waitFor(() => expect(host.querySelector('[data-testid="snow-coach-turn"]')).toBeTruthy(), T);
    const body = host.querySelector('[data-testid="snow-coach-turn"] .sf-coach-body');
    expect(Array.from(body.querySelectorAll("ol > li > strong")).map(n => n.textContent)).toEqual(["目标", "代价"]);
    expect(body.querySelector("ol > li > ul > li > em").textContent).toBe("做法");
    expect(body.querySelector(".sf-coach-sugs strong").textContent).toBe("代价");
    expect(body.querySelector("i")).toBeNull();
    expect(body.textContent).toContain("<i>这句像标签</i>，照样是文字。");
    expect(body.textContent).not.toContain("**");

    await act(async () => host.querySelector('[data-testid="snow-coach-adopt"]').click());
    await vi.waitFor(() => expect(generateCalls().length).toBe(1), T);
    expect(generateCalls()[0][1].direction_text).toBe(reply);
  });

  it("要点卡：撤下 = 提交缺了它的完整列表；加条 / 切范围 / 关继承都走 saveDirectionBrief；没有「生成时带入」；右栏有只读镜像", async () => {
    const host = await renderSnow("paragraph");
    const rail = host.querySelector('[data-testid="snow-brief-rail"]');
    expect(rail.textContent).toContain("主角是被动卷入");
    expect(rail.textContent).toContain("基调冷");
    const card = await openCoach(host);
    expect(card.querySelector('[data-testid="snow-brief-use"]')).toBeNull();
    const lines = card.querySelectorAll('[data-testid="snow-brief-line"]');
    expect(lines.length).toBe(2);
    expect(lines[1].textContent).toContain("你");
    expect(card.querySelector('[data-testid="snow-brief-inherited"]').textContent).toContain("读者定位");
    expect(card.querySelector('[data-testid="snow-brief-stale"]')).toBeTruthy();

    await act(async () => lines[0].querySelector('[data-testid="snow-brief-dismiss"]').click());
    const [workId, feKey, payload] = window.SnowSync.saveDirectionBrief.mock.calls[0];
    expect(workId).toBe("coach-book");
    expect(feKey).toBe("paragraph");
    expect(payload.lines.map(l => l.line_id)).toEqual(["dl_2"]);

    await act(async () => card.querySelector('[data-testid="snow-brief-dismissed-toggle"]').click());
    await act(async () => card.querySelector('[data-testid="snow-brief-restore"]').click());
    expect(window.SnowSync.saveDirectionBrief.mock.calls[1][2].lines.find(l => l.line_id === "dl_3")).toMatchObject({ status: "active", text: "不要热血" });

    await act(async () => setValue(card.querySelector('[data-testid="snow-brief-add-text"]'), "第一章不出现凶手"));
    await act(async () => card.querySelector('[data-testid="snow-brief-add"]').click());
    const added = window.SnowSync.saveDirectionBrief.mock.calls[2][2].lines;
    expect(added[added.length - 1]).toMatchObject({ kind: "decision", scope: "step", text: "第一章不出现凶手", status: "active" });

    await act(async () => card.querySelectorAll('[data-testid="snow-brief-scope"]')[0].click());
    expect(window.SnowSync.saveDirectionBrief.mock.calls[3][2].lines[0]).toMatchObject({ line_id: "dl_1", scope: "book" });
    await act(async () => card.querySelector('[data-testid="snow-brief-inherit"]').click());
    expect(window.SnowSync.saveDirectionBrief.mock.calls[4][2]).toMatchObject({ inherit_upstream: false });

    await act(async () => card.querySelector('[data-testid="snow-brief-regen"]').click());
    await vi.waitFor(() => expect(generateCalls().length).toBe(1), T);
    expect(generateCalls()[0][1]).toMatchObject({ source: "fe_brief_regen" });
  });

  /* F02-02：整步生成回来的脚手架由规范草稿反推，里面没有只活在前端的内容——以前整步替换把它们一并抹掉 */
  const CACHE = "ws_snow_state_v2::coach-book";
  const readCache = () => JSON.parse(window.localStorage.getItem(CACHE));

  it("F02-02：03「AI 生成本步」换掉五句骨架，作者写的错误信念留着", async () => {
    window.localStorage.setItem(CACHE, JSON.stringify({ scaffolds: {
      paragraph: { premiseF: "谎言能护住所爱的人", premiseT: "只有说出真相才护得住", setup: "旧", d1: "", d2: "", d3: "", resolution: "" },
    } }));
    window.SnowSync.applyServerStep = vi.fn(() => ({ scaffold: {
      premiseF: "", premiseT: "只有说出真相才护得住", setup: "她回到雨城。", d1: "旧信寄到。", d2: "", d3: "", resolution: "" } }));
    const host = await renderSnow("paragraph");
    await act(async () => host.querySelector('[data-testid="snow-ai-generate"]').click());
    await vi.waitFor(() => expect(generateCalls().length).toBe(1), T);
    await vi.waitFor(() => expect(readCache().scaffolds.paragraph.setup).toBe("她回到雨城。"), T);
    expect(readCache().scaffolds.paragraph.premiseF).toBe("谎言能护住所爱的人");
    expect(host.querySelector('input[aria-label="错误信念"]').value).toBe("谎言能护住所爱的人");
  });

  it("F02-02：09「AI 生成整表」换掉场景行，支线（含折射）与还在的场挂在哪条线上都留着", async () => {
    const lines = [
      { id: "main", name: "主线", kind: "main", tone: "crimson", refract: "" },
      { id: "L1", name: "旧案线", kind: "sub", tone: "slate", refract: "替恩师撒的谎" },
    ];
    window.localStorage.setItem(CACHE, JSON.stringify({ scaffolds: {
      scenes: { lines, list: [
        { id: "S01", type: "proactive", line: "L1", pov: "", place: "码头", event: "取信", crucible: "", fn: "", spine: "" },
        { id: "S02", type: "reactive", line: "main", pov: "", place: "旅馆", event: "消化", crucible: "", fn: "", spine: "" },
      ] },
    } }));
    window.SnowSync.applyServerStep = vi.fn(() => ({ scaffold: { lines: [], list: [
      { id: "S01", type: "proactive", line: "main", pov: "", place: "码头", event: "她在码头取到旧信", crucible: "退路被断", fn: "", spine: "" },
      { id: "S09", type: "proactive", line: "main", pov: "", place: "旧屋", event: "新的一场", crucible: "", fn: "", spine: "" },
    ] } }));
    const host = await renderSnow("scenes");
    await act(async () => host.querySelector('[data-testid="snow-ai-generate-table"]').click());
    await vi.waitFor(() => expect(generateCalls().length).toBe(1), T);
    await vi.waitFor(() => expect(readCache().scaffolds.scenes.list[0].event).toBe("她在码头取到旧信"), T);
    const scenes = readCache().scaffolds.scenes;
    expect(scenes.lines).toEqual(lines);
    expect(scenes.list.map(r => [r.id, r.line])).toEqual([["S01", "L1"], ["S09", "main"]]);
  });

  /* F02-19：分诊、修复补丁、单角色补全、填入教练改写都经工作台 API（useSnowWorkbenchApi）读写视图——以前三条通道
     共读一个每次渲染整份重写的 env ref。这几条入口以前没有直接的用例，改接口时靠它们守住：底稿与上行同源、
     动手前留底、只动焦点成员、回执与历史记在发起的那一步上。 */
  const castAndScenes = () => ({ scaffolds: {
    characters: { sel: "c2", chars: {
      c1: { name: "林昭", role: "主角", goal: "查清旧案", ambition: "", values: "", conflict: "", epiphany: "" },
      c2: { name: "许言", role: "配角", goal: "", ambition: "", values: "", conflict: "", epiphany: "" },
    } },
    scenes: { lines: [], list: [{ id: "S01", type: "proactive", line: "main", pov: "c1", place: "码头", event: "取信", crucible: "", fn: "", spine: "" }] },
    planning: { sel: "S01", plans: { S01: { goal: "拿到旧信" } } },
  } });
  const withRoutes = (routes) => {
    const base = client.apiPost.getMockImplementation();
    client.apiPost.mockImplementation(async (url, body) => {
      const hit = Object.keys(routes).find(k => String(url).endsWith(k));
      return hit ? routes[hit](body) : base(url, body);
    });
  };
  const hist = () => readCache().history || [];

  it("F02-19：AI 分诊（底稿同源、存档、历史）→ 应用修复补丁（三拍进 10、坩埚回写 09、应用前留底）", async () => {
    window.localStorage.setItem(CACHE, JSON.stringify(castAndScenes()));
    window.SnowSync.pushCanon = vi.fn((key) => ({ scenes: [{ row_uid: "S01", marker: `canon-${key}` }] }));
    window.SnowSync.refetch = vi.fn();
    withRoutes({
      "/scene-triage/suggest": () => ({ source: "llm", items: [{ row_uid: "S01", scene_id: "SC1", scene_plan_id: "sp1", status: "maybe", score: 60,
        notes: "坩埚空着", fix_steps: ["补上坩埚"], missing_fields: ["crucible"], repair_patch: { crucible: "退路被断", conflict: "三方堵截" } }] }),
      "/scene-triage": () => ({ items: [{ scene_plan_id: "sp1", triage_id: "t1" }] }),
    });
    const host = await renderSnow("planning");
    await act(async () => host.querySelector('[data-testid="snow-ai-triage"]').click());
    await vi.waitFor(() => expect(host.querySelector(".sf-triage-badge")).toBeTruthy(), T);
    const suggest = client.apiPost.mock.calls.find(c => String(c[0]).endsWith("/scene-triage/suggest"));
    expect(suggest[0]).toBe("/api/v2/projects/coach-book/snowflake-workspace/scene-triage/suggest");
    expect(suggest[1]).toEqual({ draft_override: { scenes: [{ row_uid: "S01", marker: "canon-planning" }] } });
    expect(window.SnowSync.pushCanon).toHaveBeenCalledWith("planning", expect.objectContaining({ scaffolds: expect.objectContaining({ planning: expect.any(Object) }) }), "coach-book");
    const saved = client.apiPost.mock.calls.find(c => String(c[0]).endsWith("/scene-triage"));
    expect(saved[1].items[0]).toMatchObject({ scene_plan_id: "sp1", recommended_status: "maybe" });
    expect(host.querySelector(".sf-triage-badge").textContent).toBe("需修补");
    await vi.waitFor(() => expect(hist()[0]).toMatchObject({ action: "场景分诊", who: "AI", key: "planning" }), T);

    const repair = [...host.querySelectorAll("button")].find(b => b.textContent.includes("应用修复补丁"));
    await act(async () => repair.click());
    await vi.waitFor(() => expect(readCache().scaffolds.planning.plans.S01.conflict).toBe("三方堵截"), T);
    const cache = readCache();
    expect(cache.scaffolds.planning.plans.S01).toEqual({ goal: "拿到旧信", conflict: "三方堵截" });
    expect(cache.scaffolds.scenes.list[0].crucible).toBe("退路被断");
    expect(cache.history[0]).toMatchObject({ action: "应用修复补丁", note: "10 场景规划 · S01 修复前留底", key: "planning" });
    expect(cache.history[0].snap.scaffold.plans.S01).toEqual({ goal: "拿到旧信" });
  });

  it("F02-19：04「AI 补全此角色」只并回焦点角色；教练回复「填入本步」经 applyCanonPatch 合并当前两块内容、填入前留底、回编辑页", async () => {
    window.localStorage.setItem(CACHE, JSON.stringify(castAndScenes()));
    const patchTurn = { ...CHAT_TURN, turn_id: "turn-p", step_key: "character_sheets", reply: "改好了。",
      candidate_patch: { characters: [{ character_id: "c2", goal: "替恩师顶罪" }] }, candidate_label: "许言的目标", brief_delta: null };
    installApi({ history: [patchTurn] });
    window.SnowSync.pushCanon = vi.fn((key) => ({ characters: [{ character_id: "c2", marker: `canon-${key}` }] }));
    window.SnowSync.applyServerStep = vi.fn(() => ({ scaffold: { sel: "c1", chars: {
      c1: { name: "林昭（模型改了）", role: "主角", goal: "别的", ambition: "", values: "", conflict: "", epiphany: "" },
      c2: { name: "许言", role: "配角", goal: "AI 写的目标", ambition: "", values: "", conflict: "", epiphany: "" },
    } } }));
    const host = await renderSnow("characters");
    const fill = [...host.querySelectorAll("button")].find(b => b.textContent.includes("AI 补全此角色"));
    await act(async () => fill.click());
    await vi.waitFor(() => expect(generateCalls().length).toBe(1), T);
    expect(generateCalls()[0][0]).toContain("/steps/character_sheets/generate");
    expect(generateCalls()[0][1]).toMatchObject({ source: "fe_char_focus_ai", focus_character_refs: ["c2"], draft_override: { characters: [{ character_id: "c2", marker: "canon-characters" }] } });
    await vi.waitFor(() => expect(readCache().scaffolds.characters.chars.c2.goal).toBe("AI 写的目标"), T);
    expect(readCache().scaffolds.characters.chars.c1.name).toBe("林昭");
    expect(hist().slice(0, 2).map(h => h.action)).toEqual(["AI 补全角色「许言」", "AI 补全角色「许言」"]);
    expect(hist()[1].snap.scaffold.chars.c2.goal).toBe("");

    window.SnowSync.applyCanonPatch = vi.fn((key, doc) => ({ scaffold: { ...doc.scaffolds.characters,
      chars: { ...doc.scaffolds.characters.chars, c2: { ...doc.scaffolds.characters.chars.c2, goal: "替恩师顶罪" } } } }));
    await openCoach(host);
    await vi.waitFor(() => expect(host.querySelector('[data-testid="snow-coach-patch"]')).toBeTruthy(), T);
    await act(async () => host.querySelector('[data-testid="snow-coach-patch"]').click());
    expect(window.SnowSync.applyCanonPatch).toHaveBeenCalledWith("characters",
      expect.objectContaining({ drafts: expect.any(Object), scaffolds: expect.objectContaining({ characters: expect.any(Object) }) }),
      patchTurn.candidate_patch, null);
    await vi.waitFor(() => expect(readCache().scaffolds.characters.chars.c2.goal).toBe("替恩师顶罪"), T);
    expect(hist()[0]).toMatchObject({ action: "填入教练改写", key: "characters" });
    expect(hist()[0].snap.scaffold.chars.c2.goal).toBe("AI 写的目标");
    expect(host.querySelector('[data-testid="snow-aibar"]')).toBeTruthy(); // 回到编辑页
    expect(host.querySelector('[data-testid="undo-toast"]').textContent).toContain("已填入「许言的目标」");
  });
});
