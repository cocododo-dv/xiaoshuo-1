// 雪花十步的纯推导与缓存归一（ws-snow-model.js 门面后面的三个叶子模块）——不渲染视图，直接测函数。
// 以前这些只经整张视图的渲染间接测到；第 10 步回写 09 形态、整步生成抹掉线索这两处丢数据都没有守卫。
import fs from "node:fs";
import path from "node:path";
import { describe, expect, it } from "vitest";
import { PLACEHOLDER_CHAPTER_TITLE_MARKERS, chapterNoInTitle, isAutoChapterTitle, isPlaceholderChapterRow } from "./labels/catalog.js";
import { isAutoChapterTitle as panelIsAutoChapterTitle } from "./ws-snow-chapters-model.js";
import { SNOW_STEPS, snowStepByBackendKey } from "./snow-steps.js";
import { WS_SNOW_STEPS } from "./ws-nav.js";
import {
  S2_BE_KEY, S2_BE_STEPS, S2_STEPS, S2_STEP_DATA,
  s2AdoptServerScaffold, s2Ancestors, s2BlankScaffolds, s2FindStepKey, s2InferSpine, s2LandingStep, s2LineStats,
  s2MergeScaffolds, s2NormalizeState, s2PacingRuns, s2PlanAuto, s2PlanSlots, s2PlanState, s2PreserveFeOnly,
  s2ReorderScenes, s2SceneAuto, s2SettlePlanning, s2StaleMap, s2UpstreamDrift,
} from "./ws-snow-model.js";

describe("步骤目录只有一份", () => {
  it("外壳导航与构思视图都取 snow-steps.js 的那一份；构思视图只加文案，展开来源的名字由目录推出", () => {
    expect(WS_SNOW_STEPS).toBe(SNOW_STEPS);
    expect(S2_STEPS.map(s => [s.key, s.be, s.num, s.name, s.track, s.essential])).toEqual(SNOW_STEPS.map(s => [s.key, s.be, s.num, s.name, s.track, s.essential]));
    expect(S2_BE_STEPS).toEqual(SNOW_STEPS.map(s => [s.key, s.be]));
    expect(S2_BE_KEY.planning).toBe("scene_details");
    const paragraph = S2_STEPS.find(s => s.key === "paragraph");
    expect(paragraph).toMatchObject({ from: "一句话概括", fromKey: "logline", book: "原著第 2 步" });
    expect(S2_STEPS.find(s => s.key === "audience").from).toBeUndefined();
    S2_STEPS.forEach(s => {
      expect(s.blurb, s.key).toBeTruthy();
      expect(S2_STEP_DATA[s.key], s.key).toBeTruthy();
    });
    expect(snowStepByBackendKey("scene_list").key).toBe("scenes");
    expect(snowStepByBackendKey("nope")).toBeNull();
    expect(s2FindStepKey("long_synopsis")).toBe("outline");
    expect(s2FindStepKey("outline")).toBe("outline");
    expect(s2FindStepKey("nope")).toBe("");
    // 依赖 DAG：09 既从 07 展开，也依赖 04 的角色表
    expect(s2Ancestors("scenes")).toEqual(expect.arrayContaining(["outline", "characters", "synopsis", "paragraph", "logline", "audience"]));
  });
});

describe("09 场景列表的推导", () => {
  it("s2PacingRuns：连续主动 ≥5 才提醒，连续反应 ≥3 算松散", () => {
    const row = (t) => ({ type: t });
    const list = [..."ppppp".split("").map(() => row("proactive")), row("reactive"), row("reactive"), row("reactive"), row("proactive")];
    const { runs, tight, slack } = s2PacingRuns(list);
    expect(runs.map(r => [r.t, r.len])).toEqual([["pro", 5], ["rea", 3], ["pro", 1]]);
    expect(tight).toEqual([{ t: "pro", len: 5, start: 0, end: 4 }]);
    expect(slack).toEqual([{ t: "rea", len: 3, start: 5, end: 7 }]);
    expect(s2PacingRuns(undefined)).toEqual({ runs: [], tight: [], slack: [] });
  });

  it("s2LineStats：每条线的位置 / 跨度；挤在一小段里算扎堆；支线没写折射要提醒", () => {
    const lines = [{ id: "main", kind: "main", refract: "" }, { id: "L1", kind: "sub", refract: "" }];
    const list = Array.from({ length: 9 }, (_, i) => ({ id: `S0${i + 1}`, line: i === 1 || i === 2 ? "L1" : undefined }));
    const [main, sub] = s2LineStats(list, lines);
    expect(main).toMatchObject({ count: 7, span: 9, clustered: false, noRefract: false });
    expect(sub).toMatchObject({ pos: [1, 2], count: 2, span: 2, clustered: true, noRefract: true });
  });

  it("s2SceneAuto：场场有坩埚、节奏、支线分布三项核对", () => {
    expect(s2SceneAuto({ list: [], lines: [] })[0]).toMatchObject({ pass: false, val: "还没有场景" });
    const auto = s2SceneAuto({ list: [{ id: "S01", type: "proactive", crucible: "退路被断" }, { id: "S02", type: "reactive", crucible: "" }], lines: [] });
    expect(auto.map(a => [a.t, a.pass])).toEqual([["场场有冲突", false], ["节奏（建议）", true], ["支线不扎堆", true]]);
    expect(auto[0].val).toBe("1 场还没写坩埚");
  });

  // 与后端 test_snowflake_chaptering_story_order.py 的 spine_from_role 用同一张表
  it.each([
    ["灾难一·一幕高潮", "灾一"],
    ["灾难二·二幕高潮", "灾二"],
    ["灾难三", "灾三"],
    ["灾一", "灾一"],
    ["第二个灾难", "灾二"],
    ["灾难 3 / 高潮", "灾三"],
    ["Disaster 1", "灾一"],
    ["一幕高潮", ""],
    ["中点逆转/道德抉择", ""],
    ["", ""],
  ])("s2InferSpine(%j) → %j（与后端 spine_from_role 同一族写法）", (role, expected) => {
    expect(s2InferSpine(role)).toBe(expected);
  });

  it("s2ReorderScenes：把一场挪到另一位置，其余顺序不变；越界或原地是无操作且不改原数组", () => {
    const list = [{ id: "S01" }, { id: "S02" }, { id: "S03" }, { id: "S04" }];
    expect(s2ReorderScenes(list, 0, 2).map(s => s.id)).toEqual(["S02", "S03", "S01", "S04"]);
    expect(s2ReorderScenes(list, 3, 0).map(s => s.id)).toEqual(["S04", "S01", "S02", "S03"]);
    expect(s2ReorderScenes(list, 1, 1).map(s => s.id)).toEqual(["S01", "S02", "S03", "S04"]);
    expect(s2ReorderScenes(list, 1, 9).map(s => s.id)).toEqual(["S01", "S02", "S03", "S04"]);
    expect(s2ReorderScenes(list, -1, 0).map(s => s.id)).toEqual(["S01", "S02", "S03", "S04"]);
    expect(list.map(s => s.id)).toEqual(["S01", "S02", "S03", "S04"]);
    expect(s2ReorderScenes(undefined, 0, 1)).toEqual([]);
  });
});

describe("打开构思时落在哪一步", () => {
  it("需复核的第一步 → 还没确认也没略过的第一步 → 上次看的那一步 → 最后一步", () => {
    const all = Object.fromEntries(S2_STEPS.map(s => [s.key, "done"]));
    expect(s2LandingStep({ states: { ...all, synopsis: "active" }, health: { outline: { beStatus: "stale" } } })).toBe("outline");
    expect(s2LandingStep({ states: { ...all, synopsis: "active" }, health: {} })).toBe("synopsis");
    expect(s2LandingStep({ states: { ...all, synopsis: "skip" }, health: {}, lastVisited: "profile" })).toBe("profile");
    expect(s2LandingStep({ states: all, health: {}, lastVisited: "long_synopsis" })).toBe("outline");
    expect(s2LandingStep({ states: all, health: {} })).toBe("planning");
  });
});

describe("阶段 E · 场景规划覆盖格与失效图", () => {
  it("s2PlanSlots / s2PlanState：传入 09 的类型时按它取三槽，存储的 plan.mode 只是兜底", () => {
    const legacyReactivePlan = { mode: "reactive", reaction: "手抖", dilemma: "报警或沉默", decision: "去找证人", goal: "", conflict: "", setback: "" };
    expect(s2PlanSlots(legacyReactivePlan)).toEqual(["reaction", "dilemma", "decision"]);
    expect(s2PlanState(legacyReactivePlan)).toBe(2);
    // 09 把这一场切回主动：格子必须按 GCS 数槽——三个 RDD 槽再满也算「未规划」
    expect(s2PlanSlots(legacyReactivePlan, "proactive")).toEqual(["goal", "conflict", "setback"]);
    expect(s2PlanState(legacyReactivePlan, "proactive")).toBe(0);
    expect(s2PlanState({ mode: "proactive", goal: "拿到账本", conflict: "", setback: "" }, "reactive")).toBe(0);
    expect(s2PlanState({ mode: "proactive", goal: "拿到账本", conflict: "三轮受阻", setback: "" }, "proactive")).toBe(1);
    expect(s2PlanState(null, "proactive")).toBe(0);
  });

  it("s2PlanAuto：逐场覆盖与三槽填满都按 09 的类型判", () => {
    const scenes = { list: [
      { id: "S01", type: "proactive" },
      { id: "S02", type: "proactive" },   // 09 已切回主动，存储的 plan 还是 RDD
    ] };
    const planning = { plans: {
      S01: { mode: "proactive", goal: "拿到账本", conflict: "三轮受阻", setback: "账本被烧" },
      S02: { mode: "reactive", reaction: "手抖", dilemma: "报警或沉默", decision: "去找证人" },
    } };
    const auto = s2PlanAuto(planning, scenes);
    const byTitle = Object.fromEntries(auto.map(a => [a.t, a]));
    expect(byTitle["逐场覆盖"].val).toBe("1/2 场已规划");
    expect(byTitle["三槽填满"].val).toBe("1/2 场三槽齐");
    expect(byTitle["链条衔接"].pass).toBe(true);
  });

  it("E3 第二步：需复核只来自后端 status=stale（未确认仍有效）；漂移上游按 input_refs 对照当前 step_run_id", () => {
    const health = {
      audience: { beStatus: "approved", stepRunId: "run_brief_v1", inputRefs: {} },
      logline: { beStatus: "approved", stepRunId: "run_logline_v2", inputRefs: { book_brief: "run_brief_v1" } },
      paragraph: { beStatus: "stale", staleAcceptedAt: null, staleReason: "one_sentence_summary 改了被消费字段 ['summary']",
        stepRunId: "run_para_v1", inputRefs: { book_brief: "run_brief_v1", one_sentence_summary: "run_logline_v1" } },
      // 作者已「确认仍有效」：后端刷新了它消费的版本，不再进需复核图
      characters: { beStatus: "stale", staleAcceptedAt: "2026-09-13T10:00:00Z", stepRunId: "run_chars_v1", inputRefs: { one_sentence_summary: "run_logline_v2" } },
      // 上游有了新版本但后端没判定失效（消费的字段没变）：只是漂移提示，不算需复核
      synopsis: { beStatus: "approved", stepRunId: "run_syn_v1", inputRefs: { one_sentence_summary: "run_logline_v1" } },
      // 后端 stale 但没有 input_refs 记录（旧数据）：仍需复核，漂移列表为空
      outline: { beStatus: "stale", staleAcceptedAt: null, stepRunId: "run_out_v1", inputRefs: {} },
    };
    expect(s2UpstreamDrift(health, "paragraph")).toEqual(["logline"]);   // book_brief 没变，不在列表里
    expect(s2UpstreamDrift(health, "synopsis")).toEqual(["logline"]);
    expect(s2UpstreamDrift(health, "characters")).toEqual([]);
    expect(s2StaleMap(health)).toEqual({ paragraph: ["logline"], outline: [] });
    expect(s2StaleMap({})).toEqual({});
    expect(s2StaleMap(undefined)).toEqual({});
  });
});


/* —— 阶段 M：09 是一张能随手挪的表；10 有钩子 / 离场变化；分诊随水合回来；略过写回服务端 —— */

describe("缓存归一与服务端脚手架的落地（F02-01 / F02-02）", () => {
  const scenes = (rows) => ({ lines: [], list: rows });
  it("s2SettlePlanning：plan 里的形态 / 视角摘掉；09 那一行没有视角时视角挪进去；没有可摘的键时原样返回", () => {
    const sc = {
      scenes: scenes([{ id: "S01", type: "reactive", pov: "c2" }, { id: "S02", type: "proactive", pov: "" }]),
      planning: { sel: "S01", plans: { S01: { mode: "proactive", pov: "c1", goal: "g" }, S02: { mode: "reactive", pov: "c3" } } },
    };
    const out = s2SettlePlanning(sc);
    expect(out.planning.plans).toEqual({ S01: { goal: "g" }, S02: {} });
    expect(out.scenes.list.map(r => r.pov)).toEqual(["c2", "c3"]);
    expect(out.scenes.list[0]).toBe(sc.scenes.list[0]);
    const clean = { scenes: sc.scenes, planning: { sel: "S01", plans: { S01: { goal: "g" } } } };
    expect(s2SettlePlanning(clean)).toBe(clean);
    expect(s2SettlePlanning(null)).toBeNull();
  });

  it("s2MergeScaffolds：缺的步骤补空白；第 10 步没选中时落在第一份规划上，并经 s2SettlePlanning 归一", () => {
    const merged = s2MergeScaffolds({ planning: { plans: { S02: { mode: "reactive", decision: "d" } } } });
    expect(merged.planning).toEqual({ sel: "S02", plans: { S02: { decision: "d" } } });
    expect(merged.audience).toEqual(s2BlankScaffolds().audience);
    expect(s2MergeScaffolds(null)).toEqual(s2BlankScaffolds());
    // 存过的角色表整张用存的（sel 等其余键补空白）
    expect(s2MergeScaffolds({ characters: { chars: { c2: { name: "沈砚" } } } }).characters).toEqual({ sel: "c1", chars: { c2: { name: "沈砚" } } });
  });

  it("s2NormalizeState：补齐五块并保留时间戳", () => {
    const n = s2NormalizeState({ states: { logline: "done" }, _t: 5 });
    expect(n.states.logline).toBe("done");
    expect(n.states.audience).toBe("todo");
    expect(n.history).toEqual([]);
    expect(n._t).toBe(5);
    expect(Object.keys(n.drafts)).toHaveLength(10);
  });

  it("s2PreserveFeOnly：03 的错误信念留下（只写了它时，回来的真信念是它的回声，保持空）", () => {
    const next = { premiseF: "", premiseT: "真信念", setup: "新" };
    expect(s2PreserveFeOnly("paragraph", { premiseF: "谎言", premiseT: "真信念" }, next)).toEqual({ premiseF: "谎言", premiseT: "真信念", setup: "新" });
    expect(s2PreserveFeOnly("paragraph", { premiseF: "谎言", premiseT: "" }, { premiseF: "", premiseT: "谎言", setup: "新" }))
      .toEqual({ premiseF: "谎言", premiseT: "", setup: "新" });
    expect(s2PreserveFeOnly("paragraph", { premiseF: "" }, next)).toBe(next);
  });

  it("s2PreserveFeOnly：09 的线索表与还在的场挂的线留下；新场、线已不在的场用新稿的值", () => {
    const lines = [{ id: "main", kind: "main" }, { id: "L1", kind: "sub", refract: "折射" }];
    const prev = { lines, list: [{ id: "S01", line: "L1" }, { id: "S02", line: "L9" }, { id: "S03", line: "L1" }] };
    const next = { lines: [], list: [{ id: "S01", line: "main", event: "新" }, { id: "S02", line: "main" }, { id: "S04", line: "main" }] };
    const out = s2PreserveFeOnly("scenes", prev, next);
    expect(out.lines).toBe(lines);
    expect(out.list.map(r => [r.id, r.line])).toEqual([["S01", "L1"], ["S02", "main"], ["S04", "main"]]);
    expect(out.list[0].event).toBe("新");
    // 新稿自己带了线索表：用新稿的
    const own = [{ id: "main", kind: "main" }];
    expect(s2PreserveFeOnly("scenes", prev, { lines: own, list: [] }).lines).toBe(own);
    expect(s2PreserveFeOnly("synopsis", prev, next)).toBe(next);
  });

  it("s2AdoptServerScaffold：整步替换本步，同时守住只活在前端的内容与第 10 步的规矩", () => {
    const prev = {
      scenes: { lines: [{ id: "L1", kind: "sub" }], list: [{ id: "S01", type: "reactive", pov: "c2", line: "L1" }] },
      planning: { sel: "S01", plans: { S01: { goal: "旧" } } },
    };
    const planned = s2AdoptServerScaffold(prev, "planning", { sel: "S01", plans: { S01: { mode: "proactive", pov: "c1", goal: "新" } } });
    expect(planned.planning.plans.S01).toEqual({ goal: "新" });
    expect(planned.scenes).toBe(prev.scenes);
    const listed = s2AdoptServerScaffold(prev, "scenes", { lines: [], list: [{ id: "S01", type: "reactive", pov: "c2", line: "main" }] });
    expect(listed.scenes.list[0].line).toBe("L1");
    expect(listed.scenes.lines).toEqual([{ id: "L1", kind: "sub" }]);
  });
});

describe("章名规则只有一份，与后端同一张标记表（F02-06）", () => {
  it("PLACEHOLDER_CHAPTER_TITLE_MARKERS 与后端 chapter_title_sync.PLACEHOLDER_TITLE_MARKERS 逐项相同", () => {
    const src = fs.readFileSync(path.resolve(__dirname, "../../backend/src/novel_system/services/chapter_title_sync.py"), "utf8");
    const tuple = /PLACEHOLDER_TITLE_MARKERS\s*=\s*\(([^)]*)\)/.exec(src);
    expect(tuple).toBeTruthy();
    const backend = [...tuple[1].matchAll(/"([^"]*)"/g)].map(m => m[1]);
    expect(PLACEHOLDER_CHAPTER_TITLE_MARKERS).toEqual(backend);
  });

  it("isAutoChapterTitle：空 / 「第 N 章」/ 带占位标记（含「未命名章节」）是系统章名；作者起的名字不是", () => {
    ["", "  ", "第 3 章", "第12章", "（待补）", "未命名章节", "未命名", "TODO 开场"].forEach(t => expect(isAutoChapterTitle(t), t).toBe(true));
    ["雨夜来信", "第三章的灯", "第 3 章 · 雨夜"].forEach(t => expect(isAutoChapterTitle(t), t).toBe(false));
    // 面板从分章模型取的是同一个函数
    expect(panelIsAutoChapterTitle).toBe(isAutoChapterTitle);
  });

  it("isPlaceholderChapterRow：章名空或带占位标记、且摘要 / 章目标 / 脊柱全空", () => {
    expect(isPlaceholderChapterRow({ title: "（待补）" })).toBe(true);
    expect(isPlaceholderChapterRow({ title: "未命名章节", summary: "" })).toBe(true);
    expect(isPlaceholderChapterRow({ title: "", goal: "信件迫使主角回乡" })).toBe(false);
    expect(isPlaceholderChapterRow({ title: "（待补）", spine: "灾一" })).toBe(false);
    expect(isPlaceholderChapterRow({ title: "雨夜来信" })).toBe(false);
  });

  it("chapterNoInTitle：章名空着或就是对得上的「第 N 章」时，章号已经在框里", () => {
    expect(chapterNoInTitle("", 0)).toBe(true);
    expect(chapterNoInTitle("第 2 章", 1)).toBe(true);
    expect(chapterNoInTitle("第 2 章", 2)).toBe(false);
    expect(chapterNoInTitle("雨夜来信", 0)).toBe(false);
  });
});
