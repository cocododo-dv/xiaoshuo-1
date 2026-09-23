// 「像不像」的共用界面件（ws-fidelity-ui.jsx）：位次 + 判断 + 解释 + 量不准；越界短语（不给 z 分）；按维的表（测得只有
// 能统计的几维、评审带说明、重点 / 不学）；成稿中心角标；走势图（图例、范围带、方向键 / 悬停的提示、表格视图）；出错一句话 +
// 下一步。合成数据。
import React, { act } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, describe, expect, it, vi } from "vitest";
import {
  FidelityBadge, FidelityCopyLine, FidelityDimensionTable, FidelityErrorLine, FidelityGaps, FidelityHeadline, FidelityTrend,
} from "./ws-fidelity-ui.jsx";
import { fidErrorInfo } from "./ws-fidelity-model.js";

globalThis.IS_REACT_ACT_ENVIRONMENT = true;
const mounted = [];

async function render(node) {
  const host = document.createElement("div");
  document.body.appendChild(host);
  const root = createRoot(host);
  mounted.push({ root, host });
  await act(async () => root.render(node));
  return host;
}

afterEach(async () => {
  while (mounted.length) {
    const { root, host } = mounted.pop();
    await act(async () => root.unmount());
    host.remove();
  }
});

const READING = {
  percentile: 72.4, within_range: true, reliable: true, max_percentile: 90, char_count: 1800,
  emphasized_dimensions: ["scene.dialogue"], excluded_dimensions: ["theme.values"],
  out_of_band: [
    { feature: "a", dimension: "language.vocabulary", dimension_label: "词汇选择", direction: "low", phrase: "语气词比作者少", z: -2.6 },
    { feature: "b", dimension: "language.punctuation", dimension_label: "标点节奏", direction: "high", phrase: "破折号比作者多", z: 2.3 },
    { feature: "c", dimension: "narrative.pacing", dimension_label: "节奏控制", direction: "high", phrase: "一句一段比作者多", z: 2.1 },
  ],
  dimension_scores: { "language.vocabulary": 6.4, "scene.dialogue": 8.1 },
  copy_check: { blocked: true, hits: 1, protected_hits: 0 },
};
const JUDGE = { overall: 7.2, dimensions: { "scene.dialogue": { score: 4.5, note: "对白偏正式" }, "language.rhetoric": { score: 8.2, note: "" } } };

describe("位次、越界、照搬", () => {
  it("位次读得出（读屏也读得出），判断、解释、量不准的原因都在", async () => {
    const host = await render(<FidelityHeadline reading={{ ...READING, reliable: false, char_count: 300, unreliable_reason: "too_short", min_reliable_chars: 600 }} testId="h" />);
    expect(host.querySelector(".fid-rank").getAttribute("aria-label")).toBe("第 72 位，共 100 位");
    expect(host.querySelector('[data-testid="h-verdict"]').textContent).toBe("量不准");
    expect(host.querySelector('[data-testid="h-caveat"]').textContent).toContain("这段只有 300 字");
    expect(host.textContent).toContain("前 90 位都算作者的正常范围");
  });

  it("越界短语超过上限时收起、可展开；没有越界时说一句", async () => {
    const host = await render(<FidelityGaps reading={READING} limit={2} testId="g" />);
    expect(host.querySelectorAll(".fid-gap")).toHaveLength(2);
    const more = host.querySelector(".fid-more");
    expect(more.textContent).toBe("另有 1 条");
    await act(async () => more.click());
    expect(host.querySelectorAll(".fid-gap")).toHaveLength(3);
    expect(host.textContent).not.toMatch(/-2\.6|2\.3|2\.1/);
    const none = await render(<FidelityGaps reading={{ ...READING, out_of_band: [] }} />);
    expect(none.textContent).toContain("测得出的写作习惯都在作者的常态里");
  });

  it("照搬检查只说计数，带色调", async () => {
    const host = await render(<FidelityCopyLine reading={READING} testId="c" />);
    const line = host.querySelector('[data-testid="c"]');
    expect(line.dataset.tone).toBe("danger");
    expect(line.textContent).toContain("有 1 处与参考书原文连续相同");
  });
});

describe("按维的表", () => {
  it("16 维按层；测得只有能统计的几维，其余「—」；评审带说明；重点 / 不学标出来；分数条有读屏说法", async () => {
    const host = await render(<FidelityDimensionTable reading={READING} judge={JUDGE} states={{ "scene.dialogue": "emphasize", "theme.values": "exclude" }} testId="t" />);
    const rows = host.querySelectorAll("tbody tr[data-dimension]");
    expect(rows).toHaveLength(16);
    expect([...host.querySelectorAll(".fid-table-layer")].map((row) => row.textContent)).toEqual(["语言", "叙事", "场景", "主题"]);
    const dialogue = host.querySelector('tr[data-dimension="scene.dialogue"]');
    expect(dialogue.textContent).toContain("重点");
    expect(dialogue.textContent).toContain("8.1");
    expect(dialogue.textContent).toContain("4.5");
    expect(dialogue.textContent).toContain("对白偏正式");
    expect(dialogue.querySelectorAll(".fid-meter")[1].dataset.tone).toBe("danger");
    expect(dialogue.querySelectorAll(".fid-meter")[1].getAttribute("aria-label")).toBe("对话写法 · 评审：4.5 分（满分 10），差得远");
    const rhetoric = host.querySelector('tr[data-dimension="language.rhetoric"]');
    expect(rhetoric.querySelector(".fid-meter.is-empty").textContent).toBe("—");
    expect(host.querySelector('tr[data-dimension="theme.values"]').className).toContain("is-excluded");
  });

  it("没有任何分数就不画表", async () => {
    const host = await render(<FidelityDimensionTable reading={{ dimension_scores: {} }} judge={null} />);
    expect(host.querySelector("table")).toBeNull();
  });
});

describe("角标与出错", () => {
  it("角标：色调与说明", async () => {
    const host = await render(<FidelityBadge final={{ percentile: 96.4, within_range: false, reliable: true }} testId="b" />);
    const badge = host.querySelector('[data-testid="b"]');
    expect(badge.textContent).toBe("超出范围 · 第 96 位");
    expect(badge.dataset.tone).toBe("warn");
    expect(badge.getAttribute("title")).toContain("超出作者的正常范围");
  });

  it("出错一句话 + 下一步按钮", async () => {
    const onAction = vi.fn();
    const info = fidErrorInfo({ code: "STYLE_REFERENCE_CHECK_JUDGE_FAILED" });
    const host = await render(<FidelityErrorLine info={info} onAction={onAction} testId="e" />);
    await act(async () => host.querySelector('[data-testid="e-action"]').click());
    expect(onAction).toHaveBeenCalledWith({ type: "retry", label: "重新检查" });
  });
});

describe("走势图", () => {
  const TREND = [
    { reading_id: "t1", scene_id: "s1", stage: "first_draft", percentile: 93, within_range: false, reliable: true, max_percentile: 85, created_at: "2026-09-22T08:00:00" },
    { reading_id: "t2", scene_id: "s1", stage: "final", percentile: 55, within_range: true, reliable: true, max_percentile: 85, created_at: "2026-09-22T09:00:00" },
    { reading_id: "t3", scene_id: "s2", stage: "final", percentile: 40, within_range: true, reliable: false, max_percentile: 85, created_at: "2026-09-23T09:00:00" },
  ];
  const labelOf = (id) => ({ s1: "第 1 章 · 第 1 场", s2: "第 1 章 · 第 2 场" }[id] || "");

  it("图例、范围带（按读数入库时的上限）、终稿连线与首稿背景点、量不准画成空心、最近一次终稿直接标位次", async () => {
    const host = await render(<FidelityTrend trend={TREND} labelOf={labelOf} testId="tr" />);
    expect(host.querySelector(".fid-legend").textContent).toContain("作者的正常范围（前 85 位）");
    expect(host.querySelectorAll(".fid-trend-dot.is-final")).toHaveLength(2);
    expect(host.querySelectorAll(".fid-trend-dot.is-draft")).toHaveLength(1);
    expect(host.querySelectorAll(".fid-trend-dot.is-unreliable")).toHaveLength(1);
    expect(host.querySelector(".fid-trend-line")).not.toBeNull();
    expect(host.querySelector(".fid-trend-label").textContent).toBe("第 40 位");
  });

  it("方向键逐点看：提示里是位次在前，哪一场、什么时候在后；Home / End 跳到头尾；失焦收起", async () => {
    const host = await render(<FidelityTrend trend={TREND} labelOf={labelOf} testId="tr" />);
    const plot = host.querySelector(".fid-trend-plot");
    const key = (k) => act(async () => plot.dispatchEvent(new KeyboardEvent("keydown", { key: k, bubbles: true })));
    await key("ArrowRight");
    let tip = host.querySelector(".fid-tip");
    expect(tip.querySelector(".fid-tip-value").textContent).toBe("首稿 · 第 93 位 · 超出作者的正常范围");
    expect(tip.textContent).toContain("第 1 章 · 第 1 场");
    await key("End");
    tip = host.querySelector(".fid-tip");
    expect(tip.querySelector(".fid-tip-value").textContent).toBe("终稿 · 第 40 位 · 量不准");
    await key("Home");
    expect(host.querySelector(".fid-tip .fid-tip-value").textContent).toContain("第 93 位");
    await act(async () => plot.dispatchEvent(new FocusEvent("focusout", { bubbles: true })));
    expect(host.querySelector(".fid-tip")).toBeNull();
  });

  it("表格视图：新 → 旧，每一行说哪一场、哪一稿、第几位、在不在范围", async () => {
    const host = await render(<FidelityTrend trend={TREND} labelOf={labelOf} testId="tr" />);
    await act(async () => host.querySelector('[data-testid="tr-table-toggle"]').click());
    const rows = [...host.querySelectorAll('[data-testid="tr-table"] tbody tr')].map((row) => row.textContent);
    expect(rows).toHaveLength(3);
    expect(rows[0]).toContain("第 1 章 · 第 2 场");
    expect(rows[0]).toContain("量不准");
    expect(rows[2]).toContain("首稿");
    expect(rows[2]).toContain("超出范围");
  });

  it("没有读数：什么也不画", async () => {
    const host = await render(<FidelityTrend trend={[]} testId="tr" />);
    expect(host.innerHTML).toBe("");
  });
});
