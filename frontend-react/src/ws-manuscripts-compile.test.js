// 成稿中心纯函数：权威稿判定、逐场正文、左栏分组与进度格、导出编译。
// 快照全部由测试直接给出——这些函数不读 store，所以不需要 mock 任何模块。
import { describe, expect, it } from "vitest";
import {
  manuBuildBody, manuCanonicalBlockReason, manuCanonicalComplete, manuChapterRows, manuCompile,
  manuDefaultPick, manuFirstMissingScene, manuListGroups, manuProgressCells, manuScenesArchived, manuScopeProblem,
} from "./ws-manuscripts-compile.js";

const CANON_DONE = { complete: true, missing_final_scene_ids: [], pending_scene_ids: [], pending_candidate_count: 0 };

function ready(body) {
  return { status: "ready", body, error: null };
}

const COMPLETE = ready({
  completion: "complete",
  missingSceneIds: [],
  canonContinuity: CANON_DONE,
  scenes: [
    { sceneId: "s1", live: true, paras: ["潮水退去。", "闸门上有字。"] },
    { sceneId: "s2", live: true, paras: ["灯还亮着。"] },
  ],
});

function chapter(extra = {}) {
  return {
    id: "ch01", backendId: "c1", n: "01", title: "盐场的早班", state: "review",
    words: { cur: 1200 },
    scenes: [
      { sid: "sid-1", backendId: "s1", title: "交班", state: "done" },
      { sid: "sid-2", backendId: "s2", title: "夜渡", state: "done" },
    ],
    drama: { promise: "闸门背后是谁", spine: "—", arc: "", aftertaste: "灯" },
    ...extra,
  };
}

describe("权威稿判定", () => {
  it("只有聚合完整、没有缺场、正史核验完成才算可流转", () => {
    expect(manuCanonicalComplete(COMPLETE)).toBe(true);
    expect(manuCanonicalComplete({ ...COMPLETE, status: "loading" })).toBe(false);
    expect(manuCanonicalComplete(ready({ ...COMPLETE.body, missingSceneIds: ["s2"] }))).toBe(false);
    expect(manuCanonicalComplete(ready({ ...COMPLETE.body, canonContinuity: { ...CANON_DONE, complete: false } }))).toBe(false);
  });

  it("卡住的原因按「加载 → 缺场 → 正史」的顺序说一句", () => {
    expect(manuCanonicalBlockReason(null)).toBe("正在等待服务端正文核验。");
    expect(manuCanonicalBlockReason({ status: "error", error: { message: "服务暂时不可用" } })).toBe("服务暂时不可用");
    expect(manuCanonicalBlockReason(ready({ ...COMPLETE.body, missingSceneIds: ["s2"] }))).toBe("服务端仍缺 1 场归档正文。");
    expect(manuCanonicalBlockReason(ready({ ...COMPLETE.body, canonContinuity: { ...CANON_DONE, complete: false, pending_candidate_count: 2 } })))
      .toBe("仍有 2 条事实候选待裁决。");
  });

  it("各场都已归档但正史没核完：scenesArchived 为真、complete 为假", () => {
    const snap = ready({ ...COMPLETE.body, canonContinuity: { ...CANON_DONE, complete: false, pending_scene_ids: ["s1"] } });
    expect(manuScenesArchived(snap)).toBe(true);
    expect(manuCanonicalComplete(snap)).toBe(false);
    expect(manuScenesArchived(ready({ ...COMPLETE.body, completion: "partial", missingSceneIds: ["s2"] }))).toBe(false);
  });
});

describe("manuBuildBody", () => {
  it("没有 ready 快照就没有正文（加载失败也不退回任何示例稿）", () => {
    expect(manuBuildBody(chapter(), null)).toBeNull();
    expect(manuBuildBody(chapter(), { status: "error", body: null, error: {} })).toBeNull();
    expect(manuBuildBody(chapter({ backendId: "" }), COMPLETE)).toBeNull();
  });

  it("按目录场景排，缺终稿的场留占位；戏剧卡丢掉「—」和空项", () => {
    const partial = ready({ ...COMPLETE.body, completion: "partial", missingSceneIds: ["s2"], scenes: [COMPLETE.body.scenes[0]] });
    const body = manuBuildBody(chapter(), partial);
    expect(body.scenes.map((s) => [s.idx, s.title, s.live, s.missing])).toEqual([["01", "交班", true, false], ["02", "夜渡", false, true]]);
    expect(body.complete).toBe(false);
    expect(body.drama).toEqual({ promise: "闸门背后是谁", thrust: "", turn: "", after: "灯" });
  });
});

describe("左栏与进度格", () => {
  const catalog = [
    chapter({ state: "approved" }),
    chapter({ id: "ch02", backendId: "c2", n: "02", title: "雾里的灯", state: "planned", words: { cur: 800 }, scenes: [] }),
    chapter({ id: "ch03", backendId: "c3", n: "03", title: "第 3 章", state: "planned", words: { cur: 0 }, scenes: [] }),
  ];

  it("章行：阶段来自 manuscriptStage（规划但有字 = 写作中），不再带恒定的版本号", () => {
    const rows = manuChapterRows(catalog, (c) => c.state !== "planned" || c.words.cur > 0);
    expect(rows.map((r) => [r.id, r.stage, r.words, r.sceneDone, r.scenes])).toEqual([["ch01", "approved", 1200, 2, 2], ["ch02", "writing", 800, 0, 0]]);
    expect(rows[0]).not.toHaveProperty("ver");
  });

  it("分组先放要拍板的、定稿最后，空组不出现；默认选中写作中的章", () => {
    const rows = manuChapterRows(catalog);
    const groups = manuListGroups(rows);
    expect(groups.map((g) => [g.label, g.items.length])).toEqual([["写作中", 1], ["还没动笔", 1], ["已定稿", 1]]);
    expect(manuDefaultPick(rows).id).toBe("ch02");
  });

  it("进度格按目录全序，计划章数多于目录时补占位格", () => {
    const cells = manuProgressCells(catalog, 5);
    expect(cells.map((c) => c.stage)).toEqual(["approved", "writing", "planned", "planned", "planned"]);
    expect(cells.slice(3).every((c) => c.plan)).toBe(true);
    expect(cells[4].n).toBe("5");
  });

  it("先去写哪一场：服务端说缺的那一场优先，其次目录里没写完的", () => {
    const ch = chapter({ scenes: [{ sid: "a", backendId: "s1", state: "todo" }, { sid: "b", backendId: "s2", state: "done" }] });
    expect(manuFirstMissingScene(ch, ready({ missingSceneIds: ["s2"] })).sid).toBe("b");
    expect(manuFirstMissingScene(ch, ready({ missingSceneIds: [] })).sid).toBe("a");
  });
});

describe("导出", () => {
  const snapshotOf = (c) => (c.backendId === "c1" ? COMPLETE : { status: "idle", body: null, error: null });

  it("范围问题：没章 / 没同步 / 还在核验 / 缺正文，都说清楚；全齐了返回空串", () => {
    const chapters = [chapter(), chapter({ id: "ch02", backendId: "" })];
    expect(manuScopeProblem(chapters, [], snapshotOf)).toBe("该范围内没有章节。");
    expect(manuScopeProblem(chapters, ["ch02"], snapshotOf)).toBe("有 1 章尚未同步到服务端。");
    expect(manuScopeProblem([chapter({ backendId: "c9" })], ["ch01"], snapshotOf)).toBe("正在核验 1 章服务端正文…");
    expect(manuScopeProblem(chapters, ["ch01"], snapshotOf)).toBe("");
  });

  it("Markdown：书名、目录、「第 N 章 · 章名」、逐场段落、戏剧卡附录", () => {
    const out = manuCompile({ title: "测试长篇", kind: "悬疑" }, [chapter()], ["ch01"], "md", { toc: true, appendix: true, snapshotOf });
    expect(out.name).toBe("测试长篇.md");
    expect(out.mime).toContain("text/markdown");
    expect(out.content).toContain("# 测试长篇");
    expect(out.content).toContain("- 第 1 章 · 盐场的早班");
    expect(out.content).toContain("## 第 1 章 · 盐场的早班");
    expect(out.content).toContain("### 01 · 交班");
    expect(out.content).toContain("闸门上有字。");
    expect(out.content).toContain("> 戏剧卡 — 承诺：闸门背后是谁；推进：—；转变：—；余味：灯");
  });

  it("纯文本：段首缩进四格；没有快照的章写「本章尚无正文」", () => {
    const out = manuCompile({ title: "测试长篇" }, [chapter(), chapter({ id: "ch02", backendId: "c2", n: "02", title: "雾里的灯" })], ["ch01", "ch02"], "txt", { snapshotOf });
    expect(out.content).toContain("    潮水退去。");
    expect(out.content).toContain("第 2 章 · 雾里的灯\n");
    expect(out.content).toContain("（本章尚无正文）");
  });

  it("Word：所有文字都转义（与写作台同一个 escapeManuscriptText），占位章名不重复章号", () => {
    const risky = chapter({ n: "01", title: "第 1 章", scenes: [{ backendId: "s1", title: "<b>交班</b>" }] });
    const snap = ready({ ...COMPLETE.body, scenes: [{ sceneId: "s1", live: true, paras: ['他说："<script>"'] }] });
    const out = manuCompile({ title: "A&B" }, [risky], ["ch01"], "doc", { snapshotOf: () => snap });
    expect(out.name).toBe("A&B.doc");
    expect(out.content).toContain("<title>A&amp;B</title>");
    expect(out.content).toContain("<h2>第 1 章</h2>");
    expect(out.content).toContain("&lt;b&gt;交班&lt;/b&gt;");
    expect(out.content).toContain("<p>他说：&quot;&lt;script&gt;&quot;</p>");
    expect(out.content).not.toContain("<script>");
  });
});
