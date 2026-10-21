// 「等你处理」（ws-attention.js）单测：两类提醒都从目录现算——被管线停下、还没写完的场；写齐了、等你批准的章。
// 主页卡与侧栏徽标的渲染在 ws-home.test.jsx / ws-app-rail.test.jsx；这里钉规则本身。
import { describe, expect, it, vi } from "vitest";
import { ATTN_SCENE_STOPS, attentionBadges, attentionChapters, attentionOf, attentionScenes } from "./ws-attention.js";

// 只测纯规则：目录 store（启动就去拉目录）换成空的
vi.mock("./ws-catalog.jsx", () => ({ useCatalogChapters: () => [] }));

const SC = (sid, state, runStatus, extra = {}) => ({
  sid, title: sid, kind: "主动", state, words: 0, work: { runStatus, hasFinal: runStatus === "archived" }, ...extra,
});
const CH = (n, state, scenes = [], extra = {}) => ({
  id: `ch${n}`, n: String(n).padStart(2, "0"), title: `雨城${n}`, state, scenes, words: { cur: 0, target: 3000 }, ...extra,
});

describe("等你处理 · 被管线停下的场", () => {
  it("每个停点都有作者读得懂的标签；进行中的步位、归档、没跑过的场都不算", () => {
    const stops = Object.keys(ATTN_SCENE_STOPS);
    expect(stops).toEqual([
      "awaiting_candidate_selection", "trial_draft_ready", "quality_warning_pending_acceptance", "human_review_required",
      "hard_qc_partial_rewrite_required", "hard_qc_full_rewrite_required", "soft_qc_patch_required", "needs_replan",
    ]);
    stops.forEach((status) => expect(ATTN_SCENE_STOPS[status].label).toMatch(/[一-龥]/));
    const book = [CH(1, "writing", [
      SC("a", "todo", "bundle_built"), SC("b", "writing", "near_final"), SC("c", "todo", ""),
      SC("d", "todo", "ready"), SC("e", "done", "archived"), SC("f", "todo", "needs_replan"),
    ])];
    expect(attentionScenes(book).map((s) => [s.sid, s.label, s.tone])).toEqual([["f", "要重新规划", "warn"]]);
  });

  it("作者在写作台采纳了（目录标了完成）就不再提醒，哪怕管线状态还停在那里", () => {
    const book = [CH(1, "writing", [SC("a", "done", "human_review_required"), SC("b", "writing", "awaiting_candidate_selection")])];
    expect(attentionScenes(book).map((s) => s.sid)).toEqual(["b"]);
    expect(attentionScenes(book)[0].text).toBe("第 1 章 · 第 2 场「b」");
  });
});

describe("等你处理 · 等你批准的章", () => {
  it("与成稿中心同一条规则：各场都有终稿、还没批准；批准过的、没写齐的不算", () => {
    const book = [
      CH(1, "approved", [SC("a", "done", "archived")]),
      CH(2, "draft", [SC("b", "done", "archived"), SC("c", "done", "archived")], { current: true }),
      CH(3, "writing", [SC("d", "done", "archived"), SC("e", "writing", "")]),
    ];
    expect(attentionChapters(book).map((c) => [c.id, c.current, c.text])).toEqual([["ch2", true, "第 2 章 · 雨城2"]]);
  });

  it("成稿中心左栏不收的章（「规划中」又没动笔）不报——那里点进去找不到它", () => {
    const book = [CH(1, "planned", [SC("a", "todo", "archived", { work: { runStatus: "archived", hasFinal: true } })])];
    expect(attentionChapters(book)).toEqual([]);
    const started = [CH(1, "planned", [SC("a", "done", "archived")])];
    expect(attentionChapters(started).map((c) => c.id)).toEqual(["ch1"]);
  });
});

describe("等你处理 · 侧栏徽标", () => {
  const book = [
    CH(1, "draft", [SC("a", "done", "archived")], { current: true }),
    CH(2, "writing", [SC("b", "todo", "human_review_required"), SC("c", "todo", "trial_draft_ready")]),
  ];

  it("高级模式：场数挂在 AI 起草台，章数挂在成稿中心；主页不挂", () => {
    const att = attentionOf(book);
    expect(att.total).toBe(3);
    expect(attentionBadges(att, "advanced")).toEqual({ scene: "2", manuscripts: "1" });
  });

  it("作家模式：侧栏里没有那两页，合计挂在主页；什么都没有就一个都不挂", () => {
    expect(attentionBadges(attentionOf(book), "writer")).toEqual({ scene: "2", manuscripts: "1", home: "3" });
    expect(attentionBadges(attentionOf([CH(1, "writing", [SC("a", "todo", "")])]), "writer")).toEqual({});
  });
});
