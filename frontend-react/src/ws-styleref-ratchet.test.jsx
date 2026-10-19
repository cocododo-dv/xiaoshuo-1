// 风格参考 ·「配置对比」：两边一样不让比、改一项后预估与开跑、在跑的进度与取消（页内 / 参考书活动）、
// 一次比较先盲读（不给答案和数字）、逐段选最像 / 最不像、揭晓后才有结论、来源与逐段数字，揭晓后选择锁定。
import React, { act } from "react";
import { describe, expect, it, vi } from "vitest";

vi.mock("./lib/client.js", () => ({
  apiGet: vi.fn(() => Promise.resolve({})),
  apiPost: vi.fn(() => Promise.resolve({})),
  apiPatch: vi.fn(() => Promise.resolve({})),
  apiPut: vi.fn(() => Promise.resolve({})),
  apiDelete: vi.fn(() => Promise.resolve({})),
  getOperatorRef: vi.fn(() => "operator"),
}));
const workHolder = vi.hoisted(() => ({ current: { id: "w1", title: "北岸手记" } }));
vi.mock("./ws-works.jsx", () => ({
  WsWorks: {
    active: () => workHolder.current,
    activeId: () => (workHolder.current ? workHolder.current.id : null),
  },
}));

import {
  $, $$, API, bookRow, byTestId, click, client, mountView, openStage, PROFILE_SUMMARY, settle, setupStyleRefSuite, state,
} from "./ws-styleref.test-helpers.jsx";

setupStyleRefSuite(workHolder);

const ARMS = {
  A: { label: "A", describe: "模型 flash-x · 样例 6 窗 · 样例在 user 末尾", model: "flash-x", windows: 6, placement: "user_tail", rep: 1 },
  B: { label: "B", describe: "模型 flash-x · 样例 10 窗 · 样例在 user 末尾", model: "flash-x", windows: 10, placement: "user_tail", rep: 1 },
};

function overview(over = {}) {
  return {
    profile_id: "pf-a",
    available: true,
    reason: null,
    current: { label: "A", describe: ARMS.A.describe, model: "flash-x", windows: 6, placement: "user_tail", rep: 1, from_binding: true },
    models: ["flash-x", "opus-y"],
    active: null,
    runs: [],
    noise: [],
    passage_choices: [3, 4, 5, 6],
    passages_default: 4,
    windows_max: 16,
    default_epsilon: 0.2,
    ...over,
  };
}

const READING = [
  { index: 1, window_no: 12, chapter_no: 3, chars: 1200, beats: ["甲在桥边等人", "乙撑船过来"], texts: [
    { label: "甲", text: "雨城的桥上没有灯。\n他把旧信揣进怀里。" },
    { label: "乙", text: "“你来了？”他问。" },
    { label: "丙", text: "桥下的水一夜没停。" },
  ] },
  { index: 2, window_no: 30, chapter_no: 7, chars: 1500, beats: ["案卷被翻开"], texts: [
    { label: "甲", text: "案卷压在最底下。" },
    { label: "乙", text: "林昭把案卷推了回去。" },
    { label: "丙", text: "“这卷不能看。”" },
  ] },
];
const KEY = { 1: { 甲: "original", 乙: "A", 丙: "B" }, 2: { 甲: "B", 乙: "original", 丙: "A" } };

function runDetail({ revealed = false, votes = {} } = {}) {
  const reading = READING.map((item) => (revealed ? { ...item, key: KEY[item.index] } : item));
  const summary = revealed
    ? {
      arms: ARMS, same_config: false, epsilon_source: "缺省值", noise: null,
      verdict: { winner: "B", wins_a: 0, wins_b: 2, mean_gain: 0.31, epsilon: 0.2, passages: 2 },
      rows: [
        { window_no: 12, chapter_no: 3, chars: 1200, original_percentile: 50, a: { to_original: 1.2, chars: 1100 }, b: { to_original: 0.7, chars: 1180 } },
        { window_no: 30, chapter_no: 7, chars: 1500, original_percentile: 50, a: { to_original: 1.4, chars: 1300 }, b: { to_original: 0.9, chars: 1460 } },
      ],
      usage: { calls: 6, prompt_tokens: 52000, completion_tokens: 4000 },
    }
    : { arms: ARMS, same_config: false, usage: { calls: 6, prompt_tokens: 52000, completion_tokens: 4000 } };
  return {
    job: { job_id: "job-r", kind: "ratchet", status: "succeeded", finished_at: "2026-10-18T10:00:00Z" },
    entry: {
      job_id: "job-r", status: "succeeded", revealed, arms: ARMS, winner: revealed ? "B" : null,
      reading: revealed ? { done: 2, total: 2, prefer: { A: 0, B: 2, tie: 0 }, original_best: 2 } : { done: 0, total: 2 },
    },
    revealed,
    summary,
    reading,
    votes,
  };
}

function route({ ov = overview(), preview = null, detail = () => runDetail() } = {}) {
  state.books = [bookRow({ profile: PROFILE_SUMMARY })];
  const baseGet = client.apiGet.getMockImplementation();
  client.apiGet.mockImplementation((url) => {
    if (url === `${API}/profiles/pf-a/ratchet`) return Promise.resolve(typeof ov === "function" ? ov() : ov);
    if (url === `${API}/ratchet-runs/job-r`) return Promise.resolve(detail());
    return baseGet(url);
  });
  const basePost = client.apiPost.getMockImplementation();
  client.apiPost.mockImplementation((url, body, opts) => {
    if (url === `${API}/profiles/pf-a/ratchet/preview`) return Promise.resolve(preview || { calls: 9, plot_calls: 3, draft_calls: { A: 3, B: 3 }, passages: [{}, {}, {}], input_chars_estimate: 61000 });
    if (url === `${API}/profiles/pf-a/ratchet`) return Promise.resolve({ profile_id: "pf-a", job_id: "job-new", state: "queued", job: { book_id: "bk-a" } });
    if (url === `${API}/profiles/pf-a/ratchet/cancel`) return Promise.resolve({ profile_id: "pf-a", job_id: "job-new", state: "cancelled", finished: true });
    return basePost(url, body, opts);
  });
  client.apiPut.mockReset();
  client.apiPut.mockImplementation((url, body) => Promise.resolve({ job_id: "job-r", votes: body.votes, revealed: !!body.reveal }));
}

async function setValue(el, value) {
  const proto = el.tagName === "SELECT" ? window.HTMLSelectElement.prototype : window.HTMLInputElement.prototype;
  const setter = Object.getOwnPropertyDescriptor(proto, "value").set;
  await act(async () => {
    setter.call(el, String(value));
    el.dispatchEvent(new Event(el.tagName === "SELECT" ? "change" : "input", { bubbles: true }));
  });
}

async function openRatchet() {
  await mountView();
  await openStage("ratchet");
  await settle();
}

describe("配置对比", () => {
  it("没学过：先去学习文风", async () => {
    await mountView();
    await openStage("ratchet");
    expect(byTestId("sr-ratchet-empty").textContent).toContain("学完文风之后才能比");
  });

  it("两边一样不让比；改了 B 的窗数：B 栏标「改」、预估三块数字、没量过噪声时 A/A 标「先做这个」，开跑送出两套配置", async () => {
    route();
    await openRatchet();
    expect(byTestId("sr-ratchet-same")).toBeTruthy();
    expect(byTestId("sr-ratchet-start").disabled).toBe(true);
    expect(byTestId("sr-ratchet-a-model").value).toBe("flash-x");

    await setValue(byTestId("sr-ratchet-b-windows"), 10);
    await settle(450);
    expect(byTestId("sr-ratchet-same")).toBeNull();
    expect(byTestId("sr-ratchet-b-arm").textContent).toContain("样例窗数 改");
    expect(byTestId("sr-ratchet-b-arm").textContent).toContain("10 窗");
    expect(byTestId("sr-ratchet-a-arm").textContent).not.toContain("改");
    expect(byTestId("sr-ratchet-preview-calls").textContent).toContain("9次");
    expect(byTestId("sr-ratchet-preview-input").textContent).toContain("6.1万字");
    expect(byTestId("sr-ratchet-mode-aa").textContent).toContain("先做这个");
    const body = {
      a: { model: "flash-x", windows: 6, placement: "user_tail" },
      b: { model: "flash-x", windows: 10, placement: "user_tail" },
      passages: 4,
    };
    expect(client.apiPost).toHaveBeenCalledWith(`${API}/profiles/pf-a/ratchet/preview`, body);

    await click(byTestId("sr-ratchet-start"));
    expect(client.apiPost).toHaveBeenCalledWith(`${API}/profiles/pf-a/ratchet`, body);
  });

  it("量噪声：B 就是 A 再写一份；没接模型时锁住", async () => {
    route();
    state.runtime = { ...state.runtime, llm_enabled: false };
    await openRatchet();
    await click(byTestId("sr-ratchet-mode-aa"));
    await settle(450);
    expect(byTestId("sr-ratchet-b-arm")).toBeNull();
    expect(client.apiPost).toHaveBeenCalledWith(`${API}/profiles/pf-a/ratchet/preview`, expect.objectContaining({
      b: { model: "flash-x", windows: 6, placement: "user_tail", rep: 2 },
    }));
    expect(byTestId("sr-ratchet-no-llm").textContent).toContain("还没有接入模型");
    expect(byTestId("sr-ratchet-start").disabled).toBe(true);
    expect(byTestId("sr-ratchet-start").textContent).toContain("开始量噪声");
  });

  it("在跑：页内看进度、可以取消；「参考书活动」里也能取消", async () => {
    const active = { key: "job:job-new", job_id: "job-new", kind: "ratchet", kind_label: "配置对比", status: "running", book_id: "bk-a", profile_id: "pf-a", phase_label: "配置 B 起草：第 12 窗", steps: { done: 4, total: 9 }, percent: 44.4, cancellable: true };
    route({ ov: overview({ active }) });
    state.activity = [active];
    await openRatchet();
    expect(byTestId("sr-ratchet-form")).toBeNull();
    expect(byTestId("sr-ratchet-active").textContent).toContain("配置 B 起草：第 12 窗 4/9");
    await click(byTestId("sr-ratchet-cancel"));
    expect(client.apiPost).toHaveBeenCalledWith(`${API}/profiles/pf-a/ratchet/cancel`, {});

    client.apiPost.mockClear();
    const item = $('[data-activity-kind="ratchet"]');
    expect(item.textContent).toContain("配置对比");
    await click(item.querySelector('[data-testid="sr-activity-cancel"]'));
    expect(client.apiPost).toHaveBeenCalledWith(`${API}/profiles/pf-a/ratchet/cancel`, {});
  });

  it("盲读：揭晓前没有答案和数字；逐段选，读完揭晓，出结论、来源、逐段数字，之后选择锁定", async () => {
    let revealed = false;
    let votes = {};
    route({
      ov: () => overview({ runs: [{ job_id: "job-r", status: "succeeded", created_at: "2026-10-18T09:00:00Z", finished_at: "2026-10-18T10:00:00Z", arms: ARMS, revealed, reading: { done: 0, total: 2 } }] }),
      detail: () => runDetail({ revealed, votes }),
    });
    client.apiPut.mockImplementation((url, body) => {
      votes = body.votes;
      if (body.reveal) revealed = true;
      return Promise.resolve({ job_id: "job-r", votes: body.votes, revealed });
    });
    await openRatchet();
    expect(byTestId("sr-ratchet-history").textContent).toContain("待盲读 0/2");
    await click(byTestId("sr-ratchet-run"));
    await settle();

    expect(byTestId("sr-ratchet-blind-hint")).toBeTruthy();
    expect(byTestId("sr-ratchet-verdict")).toBeNull();
    expect(byTestId("sr-ratchet-rows")).toBeNull();
    expect($$('[data-testid="sr-ratchet-source"]')).toHaveLength(0);
    expect(byTestId("sr-ratchet-passage").textContent).toContain("雨城的桥上没有灯。");

    const pick = async (kind, label) => {
      const card = $(`.sr-ratchet-text[data-label="${label}"]`);
      await click(card.querySelector(`[data-testid="sr-ratchet-${kind}"]`));
      await settle();
    };
    await pick("best", "甲");
    await pick("worst", "乙");
    expect(client.apiPut).toHaveBeenLastCalledWith(`${API}/ratchet-runs/job-r/votes`, { votes: { 1: { best: "甲", worst: "乙" } }, reveal: false });
    expect(byTestId("sr-ratchet-passage").textContent).toContain("林昭把案卷推了回去。");
    await pick("best", "乙");
    await pick("worst", "丙");
    expect(byTestId("sr-ratchet-progress").textContent).toBe("已读 2/2");

    await click(byTestId("sr-ratchet-reveal"));
    await settle();
    expect(client.apiPut).toHaveBeenLastCalledWith(`${API}/ratchet-runs/job-r/votes`, {
      votes: { 1: { best: "甲", worst: "乙" }, 2: { best: "乙", worst: "丙" } }, reveal: true,
    });
    expect(byTestId("sr-ratchet-verdict").textContent).toContain("B 更像");
    expect(byTestId("sr-ratchet-readout").textContent).toContain("原文被你认作最像 2/2 段");
    expect(byTestId("sr-ratchet-agreement").textContent).toContain("一致");
    expect(byTestId("sr-ratchet-rows").textContent).toContain("0.700");
    // 停在第 2 段（读完自动翻过去的）：甲 = B、乙 = 原文、丙 = A
    expect($$('[data-testid="sr-ratchet-source"]').map((t) => t.textContent)).toEqual(["配置 B", "原文", "配置 A"]);
    expect($$('[data-testid="sr-ratchet-best"]').every((b) => b.disabled)).toBe(true);
  });

  it("没读完就揭晓要先确认；不确认什么都不发", async () => {
    route({ ov: overview({ runs: [{ job_id: "job-r", status: "succeeded", arms: ARMS, revealed: false, reading: { done: 0, total: 2 } }] }) });
    const confirm = vi.spyOn(window, "confirm").mockReturnValue(false);
    await openRatchet();
    await click(byTestId("sr-ratchet-run"));
    await settle();
    expect(byTestId("sr-ratchet-reveal").textContent).toContain("不读了，直接揭晓");
    await click(byTestId("sr-ratchet-reveal"));
    await settle();
    expect(confirm).toHaveBeenCalled();
    expect(client.apiPut).not.toHaveBeenCalled();
  });

  it("这份画像比不了：说原因，不给表单", async () => {
    route({ ov: overview({ available: false, reason: "这本书还没分类完。", current: null, models: [] }) });
    await openRatchet();
    expect(byTestId("sr-ratchet-unavailable").textContent).toBe("这本书还没分类完。");
    expect(byTestId("sr-ratchet-form")).toBeNull();
  });
});
