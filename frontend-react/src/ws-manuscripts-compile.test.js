// 成稿中心纯函数：权威稿判定、逐场正文、左栏分组与进度格、导出编译。
// 快照全部由测试直接给出——这些函数不读 store，所以不需要 mock 任何模块。
import { describe, expect, it } from "vitest";
import {
  manuApprovalTurnReason, manuBuildBody, manuCanonPendingCount, manuCanonicalBlockReason, manuCanonicalComplete, manuChapterRows, manuCompile,
  manuDefaultPick, manuFirstMissingScene, manuListGroups, manuProgressCells, manuScopeNote, manuScopeProblem,
} from "./ws-manuscripts-compile.js";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import {
  CANON_EVENT_LABELS, CANON_EXTRACTION_OUTCOMES, CANON_EXTRACTION_REASONS, CANON_FACT_KEY_SUGGESTIONS, CANON_FACT_KIND_LABELS,
  canonFactKeyLabel, canonFactValueLabel,
} from "./labels/canon.js";

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
  it("正文齐了（聚合完整、没有缺场）就可以批准；正史核没核完不算在里面", () => {
    expect(manuCanonicalComplete(COMPLETE)).toBe(true);
    expect(manuCanonicalComplete({ ...COMPLETE, status: "loading" })).toBe(false);
    expect(manuCanonicalComplete(ready({ ...COMPLETE.body, missingSceneIds: ["s2"] }))).toBe(false);
    expect(manuCanonicalComplete(ready({ ...COMPLETE.body, completion: "partial" }))).toBe(false);
    // 正史没核对、还有候选待裁决，甚至服务端没给正史状态：照样可以批准（2026-10 起正史不拦定稿与导出）
    const unverified = { ...CANON_DONE, complete: false, pending_scene_ids: ["s1"], pending_candidate_count: 2 };
    expect(manuCanonicalComplete(ready({ ...COMPLETE.body, canonContinuity: unverified }))).toBe(true);
    expect(manuCanonicalComplete(ready({ ...COMPLETE.body, canonContinuity: null }))).toBe(true);
  });

  it("卡住的原因按「加载 → 缺场」说一句，不再提正史", () => {
    expect(manuCanonicalBlockReason(null)).toBe("正在等待服务端正文核验。");
    expect(manuCanonicalBlockReason({ status: "error", error: { message: "服务暂时不可用" } })).toBe("服务暂时不可用");
    expect(manuCanonicalBlockReason(ready({ ...COMPLETE.body, missingSceneIds: ["s2"] }))).toBe("服务端仍缺 1 场归档正文。");
    expect(manuCanonicalBlockReason(ready({ ...COMPLETE.body, completion: "empty", canonContinuity: { ...CANON_DONE, complete: false, pending_candidate_count: 2 } })))
      .toBe("服务端尚未将本章标记为可流转稿。");
  });

  it("正史未核对的场数 = 有终稿、还没核对的场（服务端的 pending_scene_ids）；没给正史状态、还没读到都是 0", () => {
    expect(manuCanonPendingCount(ready({ ...COMPLETE.body, canonContinuity: { ...CANON_DONE, complete: false, pending_scene_ids: ["s1", "s2"] } }))).toBe(2);
    expect(manuCanonPendingCount(COMPLETE)).toBe(0);
    expect(manuCanonPendingCount(ready({ ...COMPLETE.body, canonContinuity: null }))).toBe(0);
    expect(manuCanonPendingCount({ status: "loading", body: null, error: null })).toBe(0);
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
    expect(body.drama).toEqual({ promise: "闸门背后是谁", spine: "", arc: "", aftertaste: "灯" });
  });
});

describe("左栏与进度格", () => {
  const catalog = [
    chapter({ state: "approved" }),
    chapter({ id: "ch02", backendId: "c2", n: "02", title: "雾里的灯", state: "planned", words: { cur: 800 }, scenes: [] }),
    chapter({ id: "ch03", backendId: "c3", n: "03", title: "第 3 章", state: "planned", words: { cur: 0 }, scenes: [] }),
  ];

  it("章行：阶段来自 chapterStage（规划但有字 = 写作中），不再带恒定的版本号", () => {
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

  it("按章依次定稿：默认选中项目的当前章（目录的 current），哪怕别的章写齐了在等你批准；章行带着 current", () => {
    const book = [
      chapter({ state: "planned", current: false, scenes: [{ sid: "a", backendId: "s1", state: "done", work: { hasFinal: true } }] }),
      chapter({ id: "ch02", backendId: "c2", n: "02", title: "雾里的灯", state: "planned", current: true, words: { cur: 300 }, scenes: [] }),
    ];
    const rows = manuChapterRows(book);
    expect(rows.map((r) => [r.id, r.stage, r.current])).toEqual([["ch01", "review", false], ["ch02", "writing", true]]);
    expect(manuDefaultPick(rows).id).toBe("ch02");
    // 当前章不在左栏（还没动笔）：照旧「等你批准 → 写作中 → 第一章」
    expect(manuDefaultPick(rows.filter((r) => r.id !== "ch02")).id).toBe("ch01");
  });

  it("按章依次定稿的原因：轮到这一章是空串；没轮到就说先批哪一章；目录里没有当前章也照实说没轮到", () => {
    const first = chapter({ current: true });
    const second = chapter({ id: "ch02", backendId: "c2", n: "02", title: "雾里的灯", current: false });
    expect(manuApprovalTurnReason(first, [first, second])).toBe("");
    expect(manuApprovalTurnReason(second, [first, second])).toBe("按章依次定稿：先批准第 1 章。");
    expect(manuApprovalTurnReason(second, [{ ...first, current: false }, second])).toBe("按章依次定稿：这一章还没轮到。");
    expect(manuApprovalTurnReason(null, [first])).toBe("");
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

  const PARTIAL = ready({ ...COMPLETE.body, completion: "partial", missingSceneIds: ["s2"], scenes: [COMPLETE.body.scenes[0]] });
  const EMPTY = ready({ completion: "empty", missingSceneIds: ["s1", "s2"], canonContinuity: null, scenes: [] });

  it("范围问题：没章 / 没同步 / 加载失败 / 还在核验都说清楚；全齐了返回空串", () => {
    const chapters = [chapter(), chapter({ id: "ch02", backendId: "" })];
    expect(manuScopeProblem(chapters, [], snapshotOf)).toBe("该范围内没有章节。");
    expect(manuScopeProblem(chapters, ["ch02"], snapshotOf)).toBe("有 1 章尚未同步到服务端。");
    expect(manuScopeProblem([chapter({ backendId: "c9" })], ["ch01"], snapshotOf)).toBe("正在核验 1 章服务端正文…");
    expect(manuScopeProblem([chapter()], ["ch01"], () => ({ status: "error", body: null, error: { message: "服务暂时不可用" } }))).toBe("服务暂时不可用");
    expect(manuScopeProblem(chapters, ["ch01"], snapshotOf)).toBe("");
  });

  it("没写齐的章照样能导出（正史也不看）；整个范围一个字的终稿都没有才拒绝", () => {
    const two = [chapter(), chapter({ id: "ch02", backendId: "c2", n: "02", title: "雾里的灯" })];
    const partialOf = (c) => (c.backendId === "c1" ? PARTIAL : EMPTY);
    expect(manuScopeProblem(two, ["ch01"], partialOf)).toBe("");
    expect(manuScopeProblem(two, ["ch01", "ch02"], partialOf)).toBe("");
    const unverified = ready({ ...COMPLETE.body, canonContinuity: { ...CANON_DONE, complete: false, pending_scene_ids: ["s1"] } });
    expect(manuScopeProblem(two, ["ch01"], () => unverified)).toBe("");
    expect(manuScopeProblem(two, ["ch02"], partialOf)).toBe("该范围内还没有终稿正文。");
  });

  it("范围说明：有没写齐的章时说一句几章缺几场（不是错误）；全齐了、还没核验完都是空串", () => {
    const two = [chapter(), chapter({ id: "ch02", backendId: "c2", n: "02", title: "雾里的灯" })];
    const partialOf = (c) => (c.backendId === "c1" ? PARTIAL : EMPTY);
    expect(manuScopeNote(two, ["ch01"], partialOf)).toBe("其中 1 章还不完整（缺 1 场），文件里每个缺的场留一行「（本场还没有终稿）」。");
    expect(manuScopeNote(two, ["ch01", "ch02"], partialOf)).toBe("其中 2 章还不完整（缺 3 场），文件里每个缺的场留一行「（本场还没有终稿）」。");
    expect(manuScopeNote(two, ["ch01"], snapshotOf)).toBe("");
    expect(manuScopeNote(two, ["ch02"], snapshotOf)).toBe("");
  });

  it("缺终稿的场在三种格式里都留一行「（本场还没有终稿）」，不会被当成写齐的一章", () => {
    const partialOf = () => PARTIAL;
    const md = manuCompile({ title: "测试长篇" }, [chapter()], ["ch01"], "md", { snapshotOf: partialOf }).content;
    expect(md).toContain("### 01 · 交班\n\n潮水退去。");
    expect(md).toContain("### 02 · 夜渡\n\n（本场还没有终稿）");
    const txt = manuCompile({ title: "测试长篇" }, [chapter()], ["ch01"], "txt", { snapshotOf: partialOf }).content;
    expect(txt).toContain("　　闸门上有字。\n\n（本场还没有终稿）");
    const doc = manuCompile({ title: "测试长篇" }, [chapter()], ["ch01"], "doc", { snapshotOf: partialOf }).content;
    expect(doc).toContain("<h3>02 · 夜渡</h3><p>（本场还没有终稿）</p>");
    // 写齐的章里没有占位
    expect(manuCompile({ title: "测试长篇" }, [chapter()], ["ch01"], "md", { snapshotOf }).content).not.toContain("本场还没有终稿");
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
    // 附录的叫法与章节编排的编辑器同一套（批准 #19：以前写「承诺 / 推进 / 转变 / 余味」）
    expect(out.content).toContain("> 戏剧卡 — 核心承诺：闸门背后是谁；主线推进：—；人物变化：—；结尾余味：灯");
  });

  it("纯文本：段首缩进两个全角空格（中文排版的习惯）；没有快照的章写「本章尚无正文」", () => {
    const out = manuCompile({ title: "测试长篇" }, [chapter(), chapter({ id: "ch02", backendId: "c2", n: "02", title: "雾里的灯" })], ["ch01", "ch02"], "txt", { snapshotOf });
    expect(out.content).toContain("\n　　潮水退去。\n　　闸门上有字。");
    expect(out.content).not.toContain("    潮水退去。");
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

describe("正史审核台的词表", () => {
  it("候选事实的类型与后端 narrative/taxonomy.py 的 EVENT_TYPES 逐项相同，每一类都有中文名", () => {
    const source = fs.readFileSync(path.resolve(path.dirname(fileURLToPath(import.meta.url)),
      "../../backend/src/novel_system/services/narrative/taxonomy.py"), "utf8");
    const start = source.indexOf("EVENT_TYPES = (");
    expect(start).toBeGreaterThanOrEqual(0);
    const backend = [...source.slice(start, source.indexOf(")", start)).matchAll(/"([a-z_]+)"/g)].map((m) => m[1]);
    expect(Object.keys(CANON_EVENT_LABELS)).toEqual(backend);
    expect(Object.values(CANON_EVENT_LABELS).every((label) => /[\u4e00-\u9fff]/.test(label))).toBe(true);
  });

  it("硬质检认得的事实种类与后端 narrative/fact_vocabulary.py 的 FACT_KINDS 逐项相同；带槽的键与固定取值翻成中文", () => {
    const source = fs.readFileSync(path.resolve(path.dirname(fileURLToPath(import.meta.url)),
      "../../backend/src/novel_system/services/narrative/fact_vocabulary.py"), "utf8");
    const start = source.indexOf("FACT_KINDS = (");
    expect(start).toBeGreaterThanOrEqual(0);
    const backend = [...source.slice(start, source.indexOf(")", start)).matchAll(/"([a-z_]+)"/g)].map((m) => m[1]);
    expect(Object.keys(CANON_FACT_KIND_LABELS)).toEqual(backend);
    expect(CANON_FACT_KEY_SUGGESTIONS.map((item) => item.value.split(":")[0])).toEqual(backend);

    expect(canonFactKeyLabel("has_item:旧信")).toBe("持有物品 · 旧信");
    expect(canonFactKeyLabel("appearance:hair")).toBe("外貌 · 发色");
    expect(canonFactKeyLabel("location")).toBe("位置");
    expect(canonFactKeyLabel("oath")).toBe("oath");
    expect(canonFactValueLabel("has_item:旧信", "lost")).toBe("已失去");
    expect(canonFactValueLabel("alive", "dead")).toBe("已死");
    expect(canonFactValueLabel("location", "钟楼")).toBe("钟楼");
    // 不是硬质检种类的键，值里碰巧是 lost 也原样
    expect(canonFactValueLabel("mood", "lost")).toBe("lost");
  });

  it("提取结果 / 原因的叫法只收后端还会写的值（后端早不再写的键是死叫法，比如离线模式退役后的 offline_unsupported）", () => {
    const servicesDir = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../../backend/src/novel_system/services");
    const sources = [];
    const walk = (dir) => {
      for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
        const abs = path.join(dir, entry.name);
        if (entry.isDirectory()) walk(abs);
        else if (entry.name.endsWith(".py")) sources.push(fs.readFileSync(abs, "utf8"));
      }
    };
    walk(servicesDir);
    const backend = sources.join("\n");
    const keys = [...Object.keys(CANON_EXTRACTION_OUTCOMES), ...Object.keys(CANON_EXTRACTION_REASONS)];
    expect(keys.length).toBeGreaterThan(0);
    expect(keys.filter((key) => !backend.includes(`"${key}"`))).toEqual([]);
  });
});
