// 章的说明（2026-10-16 规划层收拢第 2、3 步）：分章面板的本地态 / 载荷带着三格（只给目录里已经有的章）、
// 第 10 步按 09 的「所在章」分组并认出收章的那一场、按章名在目录里取这一章的说明。
import { describe, expect, it } from "vitest";
import { buildChapterPlanPayload, shapeDraft, splitChapterAt } from "./ws-snow-chapters-model.js";
import { s2ChapterIntent, s2ChapterRuns } from "./ws-snow-derive.js";
import { DRAMA_FIELDS, DRAMA_KEYS } from "./labels/catalog.js";

const PREVIEW = {
  chapters: [
    {
      row_uid: "c1", title: "旧信", act: 1, spine: "", chapter_goal: "", summary: "",
      catalog_ready: true, intent: { promise: "林昭读完旧信", forbidden: "寄信人", notes: "放慢" },
      scenes: [{ scene_plan_id: "p1" }, { scene_plan_id: "p2" }],
    },
    {
      row_uid: "c2", title: "案卷", act: 1, spine: "", chapter_goal: "", summary: "",
      catalog_ready: false, intent: { promise: "", forbidden: "", notes: "" },
      scenes: [{ scene_plan_id: "p3" }],
    },
  ],
};

describe("章的说明 · 分章面板", () => {
  it("三格只有：给读者什么 / 先别揭 / 备注", () => {
    expect(DRAMA_KEYS).toEqual(["promise", "forbidden", "notes"]);
    expect(DRAMA_FIELDS.map((f) => f.label)).toEqual(["这一章给读者什么", "先别揭", "备注"]);
  });

  it("预览 → 本地态带着三格；载荷只给目录里已经有的章送章的说明", () => {
    const draft = shapeDraft(PREVIEW);
    expect(draft.chapters[0]).toMatchObject({ catalogReady: true, promise: "林昭读完旧信", forbidden: "寄信人", notes: "放慢" });
    draft.chapters[0].forbidden = "寄信人\n案卷编号";
    const payload = buildChapterPlanPayload(draft);
    expect(payload.chapters[0]).toMatchObject({ promise: "林昭读完旧信", forbidden_text: "寄信人\n案卷编号", notes: "放慢" });
    expect(payload.chapters[1]).not.toHaveProperty("promise");
    expect(payload.chapters[1]).not.toHaveProperty("forbidden_text");
  });

  it("拆出来的新章目录里还没有：不送章的说明", () => {
    const split = splitChapterAt(shapeDraft(PREVIEW), 0, 1);
    expect(split.chapters[1].catalogReady).toBe(false);
    expect(buildChapterPlanPayload(split).chapters[1]).not.toHaveProperty("promise");
  });
});

describe("第 10 步按章分组", () => {
  const list = [
    { id: "r1", chapter: "旧信" }, { id: "r2", chapter: "旧信" },
    { id: "r3", chapter: "案卷" },
    { id: "r4", chapter: "" }, { id: "r5", chapter: "" },
  ];

  it("相邻同章的场归一组，最后一个就是收章的那一场；没有章标签的场各自一组", () => {
    expect(s2ChapterRuns(list)).toEqual([
      { label: "旧信", ids: ["r1", "r2"], start: 0 },
      { label: "案卷", ids: ["r3"], start: 2 },
      { label: "", ids: ["r4"], start: 3 },
      { label: "", ids: ["r5"], start: 4 },
    ]);
  });

  it("按章名在目录里取这一章的说明；「先别揭」按行拆开、空行不算；找不到就是空", () => {
    const catalog = [{ title: "旧信", drama: { promise: " 林昭读完旧信 ", forbidden: "寄信人\n\n案卷编号\n", notes: "不显示" } }];
    expect(s2ChapterIntent(catalog, "旧信")).toEqual({ found: true, promise: "林昭读完旧信", notYet: ["寄信人", "案卷编号"] });
    expect(s2ChapterIntent(catalog, "案卷")).toEqual({ found: false, promise: "", notYet: [] });
    expect(s2ChapterIntent(null, "")).toEqual({ found: false, promise: "", notYet: [] });
  });
});

describe("第 10 步的界面：章头、收章、章的说明", () => {
  it("选中收章的那一场：场景头写着它在哪一章、标着「收章」，上面一行是这一章的说明和去分章面板的门", async () => {
    const React = (await import("react")).default;
    const { act } = await import("react");
    const { createRoot } = await import("react-dom/client");
    const { S2ScenePlan } = await import("./ws-snow-scene-plan.jsx");
    const list = [
      { id: "r1", type: "proactive", chapter: "旧信", event: "林昭拆信" },
      { id: "r2", type: "reactive", chapter: "旧信", event: "林昭发抖" },
      { id: "r3", type: "proactive", chapter: "案卷", event: "去档案馆" },
    ];
    const catalogChapters = [{ title: "旧信", drama: { promise: "林昭读完旧信", forbidden: "寄信人\n案卷编号" } }];
    let opened = 0;
    const host = document.createElement("div");
    document.body.appendChild(host);
    const root = createRoot(host);
    await act(async () => root.render(React.createElement(S2ScenePlan, {
      scaffold: { sel: "r2", plans: {} }, onScaffold: () => {}, refs: { scenes: { list }, characters: { chars: {} } },
      go: () => {}, ai: null, catalogChapters, onOpenChapterPlan: () => { opened += 1; },
    })));

    expect(host.querySelectorAll('[data-testid="snow-plan-chgroup"]')).toHaveLength(2);
    expect(host.querySelector('[data-testid="snow-plan-chapter"]').textContent).toBe("旧信");
    expect(host.querySelector('[data-testid="snow-plan-chapter-end"]')).not.toBeNull();
    const strip = host.querySelector('[data-testid="snow-plan-chapter-intent"]');
    expect(strip.textContent).toContain("给读者：林昭读完旧信");
    expect(strip.textContent).toContain("先别揭：寄信人、案卷编号");
    await act(async () => { strip.querySelector("button").click(); });
    expect(opened).toBe(1);

    // 不是收章的那一场没有「收章」
    await act(async () => root.render(React.createElement(S2ScenePlan, {
      scaffold: { sel: "r1", plans: {} }, onScaffold: () => {}, refs: { scenes: { list }, characters: { chars: {} } },
      go: () => {}, ai: null, catalogChapters, onOpenChapterPlan: () => {},
    })));
    expect(host.querySelector('[data-testid="snow-plan-chapter-end"]')).toBeNull();
    await act(async () => root.unmount());
    host.remove();
  });
});
