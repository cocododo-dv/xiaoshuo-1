// SnowSync（雪花构思 ↔ snowflake-workspace v2）store 层单测：
// AI 融合 F1 的两条核心契约——
// 1) 规范字段保真合并：上行 PATCH 不再把「后端 generate 产出、脚手架表达不了的富字段」剪掉
//    （对象缺席键幸存 / 数组按 id 对位继承 / FE 出现的标量作者说了算）；
// 2) applyServerStep（采纳并结构化的接缝）：generate 回包 → canon 镜像 + 权威健康 + 原型形状反推。
// 另测 feFromCanon 的 backstory 前缀行拆解与 audience 期待读者情绪的往返。
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { installApiRouter } from "./test-helpers.js";

vi.mock("./lib/client.js", () => ({
  apiGet: vi.fn(),
  apiPost: vi.fn(),
  apiPatch: vi.fn(),
  apiPut: vi.fn(),
  apiDelete: vi.fn(),
}));

// SnowSync 只从 ws-snow.jsx 取 S2_BE_STEPS 这份纯映射；mock 掉避免拉入整张雪花视图模块。
vi.mock("./ws-snow.jsx", () => ({
  S2_BE_STEPS: [
    ["audience", "book_brief"], ["logline", "one_sentence_summary"], ["paragraph", "one_paragraph_summary"],
    ["characters", "character_sheets"], ["synopsis", "short_synopsis"], ["backstory", "character_synopses"],
    ["outline", "long_synopsis"], ["profile", "character_bibles"], ["scenes", "scene_list"], ["planning", "scene_details"],
  ],
  s2NormalizeState: (saved) => {
    const feKeys = ["audience", "logline", "paragraph", "characters", "synopsis", "backstory", "outline", "profile", "scenes", "planning"];
    return {
      ...saved,
      drafts: { ...Object.fromEntries(feKeys.map(k => [k, ""])), ...(saved.drafts || {}) },
      scaffolds: {
        audience: { genre: "", reader: "", pleasure: "", source: "", exclude: "", emotion: "" },
        paragraph: { premiseF: "", premiseT: "", setup: "", d1: "", d2: "", d3: "", resolution: "" },
        characters: { sel: "c1", chars: { c1: { name: "", role: "主角", goal: "", ambition: "", values: "", conflict: "", epiphany: "" } } },
        synopsis: { paras: { setup: "", d1: "", d2: "", d3: "", resolution: "" } },
        backstory: { sel: "c1", chars: { c1: { name: "", role: "主角", belief: "", wound: "", desire: "", fear: "", relation: "" } } },
        outline: { chapters: [] },
        profile: { sel: "c1", chars: { c1: { name: "", role: "主角", physical: "", psych: "", environment: "", personality: "", contradiction: "", views: "" } } },
        scenes: { lines: [], list: [] }, planning: { sel: "", plans: {} },
        ...(saved.scaffolds || {}),
      },
      checks: { ...Object.fromEntries(feKeys.map(k => [k, []])), ...(saved.checks || {}) },
      states: { ...Object.fromEntries(feKeys.map(k => [k, "todo"])), ...(saved.states || {}) },
      history: Array.isArray(saved.history) ? saved.history : [],
    };
  },
}));

const T = { timeout: 5000, interval: 25 };
const CACHE_KEY = "ws_snow_state_v2::prj-main";

const BOOK_BRIEF_DRAFT = {
  category: "文学悬疑",
  target_reader: "想看旧案与家庭代价的读者",
  story_kind: "家庭真相悬疑",
  delight_reason: "线索逼近真相的同时抬高代价",
  genre_promise: "真相越清晰失去越多",
  expected_reader_emotion: "压迫与向前的拉力",
  // 脚手架没有输入框的富字段——保真合并要让它在上行时活下来
  safety_rules: ["只借鉴抽象手法", "不复制人物设定"],
};

const WS_WITH_BOOK_BRIEF = {
  ready_to_materialize: false,
  current_step_key: "book_brief",
  steps: [{
    step_key: "book_brief",
    status: "approved",
    gate_satisfied: true,
    draft: { ...BOOK_BRIEF_DRAFT },
    health: { score: 82, status: "pass", gaps: [], next_actions: [] },
    completeness: { filled_count: 6, total_count: 6, missing_fields: [] },
  }],
};

async function loadSync(opts) {
  const client = await import("./lib/client.js");
  installApiRouter(client, opts);
  const mod = await import("./ws-snow-sync.jsx");
  await vi.waitFor(() => expect(window.WsWorks && window.WsWorks.activeId()).toBe("prj-main"), T);
  return { mod, client };
}

function saveCache(cache) {
  window.localStorage.setItem(CACHE_KEY, JSON.stringify({ _t: Date.now(), ...cache }));
  window.dispatchEvent(new CustomEvent("ws:snow-saved", { detail: CACHE_KEY }));
}

const patchCallFor = (client, beKey) =>
  client.apiPatch.mock.calls.find(c => String(c[0]).includes(`/steps/${beKey}`));

// 窗口级污染免疫：resetModules 后旧模块实例的 ws:snow-saved 监听仍活着（jsdom window
// 跨用例共享），而 vi.mock 的 client 是同一批 fn——旧实例（没有本用例的 canon 镜像）也会
// 推一次同步骤 PATCH。按「携带服务端富字段」的内容特征定位当前实例的调用：若保真合并
// 真的坏了，任何调用都不会带富字段，find 落空照样转红，可证伪性不受影响。
const patchCallWith = (client, beKey, probe) =>
  client.apiPatch.mock.calls.find(c => String(c[0]).includes(`/steps/${beKey}`) && probe(c[1].draft));

describe("SnowSync（规范字段保真合并 + 结构化采纳接缝）", () => {
  beforeEach(() => {
    vi.resetModules();
    window.localStorage.clear();
  });
  afterEach(() => vi.restoreAllMocks());

  it("importCanonicalPlan：由浏览器按十步依赖顺序保存并批准，最终返回物化就绪工作台", async () => {
    const { mod, client } = await loadSync({ snowflakeWorkspace: { ready_to_materialize: false, steps: [] } });
    const order = [
      "book_brief", "one_sentence_summary", "one_paragraph_summary", "character_sheets", "short_synopsis",
      "character_synopses", "long_synopsis", "character_bibles", "scene_list", "scene_details",
    ];
    const stepDrafts = Object.fromEntries(order.map((key) => [key, { marker: key }]));
    const calls = [];
    client.apiPatch.mockImplementation(async (url, body) => {
      const key = String(url).split("/steps/")[1];
      calls.push(`patch:${key}`);
      return { step: { step_key: key, status: "pending_review", draft: body.draft, health: {}, completeness: {} } };
    });
    client.apiPost.mockImplementation(async (url) => {
      const key = String(url).split("/steps/")[1].split("/approve")[0];
      calls.push(`approve:${key}`);
      return { step: { step_key: key, status: "approved", draft: stepDrafts[key], health: {}, completeness: {} } };
    });
    client.apiGet.mockResolvedValue({ ready_to_materialize: true, steps: [] });

    const result = await mod.SnowSync.importCanonicalPlan("prj-main", { steps: stepDrafts });

    expect(calls).toEqual(order.flatMap((key) => [`patch:${key}`, `approve:${key}`]));
    expect(client.apiPatch).toHaveBeenCalledTimes(10);
    expect(client.apiPost).toHaveBeenCalledTimes(10);
    expect(result.readyToMaterialize).toBe(true);
    expect(result.approvedStepKeys).toEqual(order);

    const importedCache = JSON.parse(window.localStorage.getItem(CACHE_KEY));
    expect(importedCache.scaffolds.audience).toEqual(expect.objectContaining({ genre: "", reader: "" }));
    expect(importedCache.history[0]).toEqual(expect.objectContaining({ action: "导入结构化计划", key: "planning" }));

    client.apiPatch.mockClear();
    client.apiPost.mockClear();
    window.dispatchEvent(new CustomEvent("ws:snow-saved"));
    await new Promise((resolve) => setTimeout(resolve, 850));
    expect(client.apiPatch).not.toHaveBeenCalled();
    expect(client.apiPost).not.toHaveBeenCalled();
  });

  it("hydrate 后上行：脚手架字段作者说了算，脚手架表达不了的富字段（safety_rules）不被剪掉", async () => {
    const { mod, client } = await loadSync({ snowflakeWorkspace: WS_WITH_BOOK_BRIEF });
    window.dispatchEvent(new CustomEvent("ws:work-changed", { detail: "prj-main" }));
    await vi.waitFor(() => expect((mod.SnowSync.health("prj-main").audience || {}).score).toBe(82), T);

    client.apiPatch.mockClear();
    saveCache({
      drafts: {},
      scaffolds: { audience: { genre: "都市怪谈", reader: "改后的读者画像", pleasure: "颤栗", source: "叙述", exclude: "不写猎奇", emotion: "压迫与向前的拉力" } },
      checks: {}, states: { audience: "active" },
    });

    await vi.waitFor(() => expect(patchCallFor(client, "book_brief")).toBeTruthy(), T);
    const body = patchCallFor(client, "book_brief")[1].draft;
    expect(body.category).toBe("都市怪谈");                       // FE 脚手架赢
    expect(body.target_reader).toBe("改后的读者画像");
    expect(body.expected_reader_emotion).toBe("压迫与向前的拉力"); // 新增表单字段往返
    expect(body.safety_rules).toEqual(BOOK_BRIEF_DRAFT.safety_rules); // 富字段幸存（无保真层时为 undefined）
    expect(body.fe_scaffold.genre).toBe("都市怪谈");               // 写穿缓存契约不变
  });

  it("applyServerStep：generate 回包 → 原型形状反推 + 权威健康；后续上行按 id 继承角色富字段", async () => {
    const { mod, client } = await loadSync({});
    const beStep = {
      step_key: "character_bibles",
      status: "pending_review",
      gate_satisfied: false,
      draft: {
        characters: [{
          character_id: "c1", display_name: "林岑", role: "主角",
          physical_profile: { appearance: "手指总带薄茧", posture: "背脊笔直" },
          personality_profile: { strongest_trait: "沉静固执" },
          environment_profile: { home: "修复室阁楼" },
          psychological_profile: { philosophy: "记录即救赎", self_image: "旁观者", deepest_fear: "成为共谋" },
        }],
      },
      health: { score: 74, status: "maybe", gaps: ["c1_pressure_too_soft"], next_actions: ["再压实变化"] },
      completeness: { filled_count: 1, total_count: 1, missing_fields: [] },
    };

    const fe = mod.SnowSync.applyServerStep("prj-main", "profile", beStep);
    expect(fe.scaffold.chars.c1.physical).toBe("手指总带薄茧");
    expect(fe.scaffold.chars.c1.views).toBe("记录即救赎");
    const h = mod.SnowSync.health("prj-main").profile;
    expect(h.score).toBe(74);
    expect(h.beStatus).toBe("pending_review");

    // 作者只微调外貌一格后保存：posture（脚手架无此输入框）必须按 character_id 对位继承
    client.apiPatch.mockClear();
    saveCache({
      drafts: {},
      scaffolds: { profile: { sel: "c1", chars: { c1: {
        name: "林岑", role: "主角", physical: "手指总带薄茧（左手更重）", psych: "成为共谋",
        environment: "修复室阁楼", personality: "沉静固执", contradiction: "旁观者", views: "记录即救赎",
      } } } },
      checks: {}, states: { profile: "active" },
    });

    const hasPosture = (draft) => !!(((draft.characters || [])[0] || {}).physical_profile || {}).posture;
    await vi.waitFor(() => expect(patchCallWith(client, "character_bibles", hasPosture)).toBeTruthy(), T);
    const body = patchCallWith(client, "character_bibles", hasPosture)[1].draft;
    expect(body.characters).toHaveLength(1);
    expect(body.characters[0].physical_profile.appearance).toBe("手指总带薄茧（左手更重）"); // FE 赢
    expect(body.characters[0].physical_profile.posture).toBe("背脊笔直");                   // 富字段幸存
    expect(body.characters[0].psychological_profile.deepest_fear).toBe("成为共谋");
  });

  it("mergeCanon：数组成员以 FE 为准（删除即删除），未匹配 id 的服务端成员不复活", async () => {
    const { mod } = await loadSync({});
    const server = { characters: [
      { character_id: "c1", display_name: "林岑", bio: "富字段" },
      { character_id: "c2", display_name: "周岚", bio: "将被删" },
    ] };
    const fe = { characters: [{ character_id: "c1", display_name: "林岑（改）" }] };
    const merged = mod.mergeCanon(server, fe);
    expect(merged.characters).toHaveLength(1);
    expect(merged.characters[0].display_name).toBe("林岑（改）");
    expect(merged.characters[0].bio).toBe("富字段");
  });

  it("applyCanonPatch：咨询式补丁——空值不清空、按 id 对位合并、不删未提到的成员", async () => {
    const { mod } = await loadSync({});
    const base = {
      summary: "既有一句话",
      characters: [
        { character_id: "c1", display_name: "林岑", goal: "旧目标", conflict: "旧冲突" },
        { character_id: "c2", display_name: "周岚", goal: "对手目标" },
      ],
    };
    const patch = {
      summary: "",                                   // 空值：不清空既有内容
      characters: [
        { character_id: "c1", display_name: "", goal: "新目标" },   // 对位改一格，空名不清名
        { character_id: "c3", display_name: "陈默", goal: "新盟友目标" }, // 新成员追加
      ],
    };
    const out = mod.applyCanonPatch(base, patch);
    expect(out.summary).toBe("既有一句话");
    expect(out.characters).toHaveLength(3);
    const c1 = out.characters.find(c => c.character_id === "c1");
    expect(c1.goal).toBe("新目标");
    expect(c1.display_name).toBe("林岑");
    expect(c1.conflict).toBe("旧冲突");
    expect(out.characters.find(c => c.character_id === "c2").goal).toBe("对手目标"); // 未提到的成员不删
    expect(out.characters.find(c => c.character_id === "c3").goal).toBe("新盟友目标");
  });

  it("pushCanon（draft_override 载荷）：与上行 PATCH 同源——竞态窗口内的新成员进载荷、按 id 对位、富字段幸存", async () => {
    const { mod } = await loadSync({});
    // 服务端 canon 镜像：c1 带脚手架没有输入框的富字段（one_paragraph_summary）
    mod.SnowSync.applyServerStep("prj-main", "characters", {
      step_key: "character_sheets", status: "pending_review", gate_satisfied: false,
      draft: { characters: [{
        character_id: "c1", display_name: "林岑", role: "主角", goal: "拿到母本",
        ambition: "被看见", values: ["真相"], conflict: "恩师挡路", epiphany: "给活人",
        one_paragraph_summary: "服务端独有的富字段",
      }] },
      health: {}, completeness: {},
    });
    // 本地脚手架：c1 被作者改了 goal；c9 是刚加、还没自动保存上行的新角色
    const cache = { drafts: {}, scaffolds: { characters: { sel: "c9", chars: {
      c1: { name: "林岑", role: "主角", goal: "改后的目标", ambition: "被看见", values: "真相", conflict: "恩师挡路", epiphany: "给活人" },
      c9: { name: "王五", role: "帮手", goal: "递出钥匙", ambition: "", values: "", conflict: "", epiphany: "" },
    } } } };
    const canon = mod.SnowSync.pushCanon("characters", cache, "prj-main");
    const byId = Object.fromEntries(canon.characters.map((c) => [c.character_id, c]));
    expect(Object.keys(byId).sort()).toEqual(["c1", "c9"]);
    expect(byId.c1.goal).toBe("改后的目标");                          // 作者最新编辑赢
    expect(byId.c1.one_paragraph_summary).toBe("服务端独有的富字段"); // 富字段不被剪掉
    expect(byId.c9.display_name).toBe("王五");                        // 新成员（竞态窗口）进载荷
    expect(byId.c9.goal).toBe("递出钥匙");
  });

  it("planning 往返：反应场的呈现方式 full / summary 上行并水合，主动场不上行该键", async () => {
    const { mod } = await loadSync({});
    const saved = {
      scaffolds: {
        scenes: { lines: [], list: [
          { id: "S01", type: "proactive", pov: "c1", place: "码头", event: "取账本", crucible: "退不出", fn: "起疑", spine: "" },
          { id: "S02", type: "reactive", pov: "c1", place: "旅馆", event: "消化挫败", crucible: "无人可信", fn: "转向", spine: "" },
        ] },
        planning: { sel: "S02", plans: {
          S01: { mode: "proactive", goal: "拿到账本", conflict: "三轮受阻", setback: "账本被烧", cost_requirement: "失去遗物", rendering: "summary" },
          S02: { mode: "reactive", reaction: "手抖", dilemma: "报警或沉默", decision: "去找证人", cost_requirement: "弟弟不再信她", rendering: "summary" },
        } },
      },
    };
    const canon = mod.canonFromFE("planning", saved);
    // 阶段 N：概述对两种形态都合法（原著第 1 场是主动场的叙述概述）——主动场的 summary 照样上行
    expect(canon.scenes[0].rendering_mode).toBe("summary");
    expect(canon.scenes[1].rendering_mode).toBe("summary");

    const hydrated = mod.feFromCanon("planning", { scenes: [
      { row_uid: "S01", primary_form: "proactive", goal: "拿到账本" },
      { row_uid: "S02", primary_form: "reactive", reaction: "手抖", rendering_mode: "summary" },
      { row_uid: "S03", primary_form: "reactive", reaction: "沉默" },
    ] });
    expect(hydrated.scaffold.plans.S01.rendering).toBe("full");
    expect(hydrated.scaffold.plans.S02.rendering).toBe("summary");
    expect(hydrated.scaffold.plans.S03.rendering).toBe("full");
  });

  it("阶段 I：反应场的第三档 skip 与次要三拍往返保真", async () => {
    const { mod } = await loadSync({ snowflakeWorkspace: { ready_to_materialize: false, steps: [] } });
    const saved = {
      drafts: {}, checks: {}, states: {},
      scaffolds: {
        scenes: { lines: [], list: [
          { id: "S01", type: "proactive", pov: "c1", place: "码头", event: "取账本", crucible: "退不出的困局", fn: "起疑", spine: "" },
          { id: "S02", type: "reactive", pov: "c1", place: "旅馆", event: "消化挫败", crucible: "无人可信", fn: "转向", spine: "" },
        ] },
        planning: { sel: "S01", plans: {
          // 主动场接着次要三拍（原著场景 1）：反应 / 两难 / 决定一起上行
          S01: { mode: "proactive", goal: "拿到账本", conflict: "三轮受阻", setback: "账本被烧", reaction: "她蹲在码头发抖", dilemma: "报警或沉默", decision: "去找证人", rendering: "skip" },
          S02: { mode: "reactive", reaction: "手抖", dilemma: "报警或沉默", decision: "去找证人", rendering: "skip" },
        } },
      },
    };
    const canon = mod.canonFromFE("planning", saved);
    expect(canon.scenes[0].rendering_mode).toBe("full"); // 略过只给反应场：主动场残留的 skip 收口成 full
    expect(canon.scenes[0]).toEqual(expect.objectContaining({ reaction: "她蹲在码头发抖", dilemma: "报警或沉默", decision: "去找证人" }));
    expect(canon.scenes[1].rendering_mode).toBe("skip");

    const hydrated = mod.feFromCanon("planning", { scenes: [
      { row_uid: "S01", primary_form: "proactive", goal: "拿到账本", reaction: "她蹲在码头发抖", decision: "去找证人" },
      { row_uid: "S02", primary_form: "reactive", reaction: "手抖", rendering_mode: "skip" },
      { row_uid: "S03", primary_form: "reactive", reaction: "沉默", rendering_mode: "bogus" },
    ] });
    expect(hydrated.scaffold.plans.S01.reaction).toBe("她蹲在码头发抖");
    expect(hydrated.scaffold.plans.S01.decision).toBe("去找证人");
    expect(hydrated.scaffold.plans.S02.rendering).toBe("skip");
    expect(hydrated.scaffold.plans.S03.rendering).toBe("full");
  });

  it("阶段 J：在场人物 / 故事时间 / 读者应感到 与 01 的叙述人称往返保真", async () => {
    const { mod } = await loadSync({ snowflakeWorkspace: { ready_to_materialize: false, steps: [] } });
    const saved = {
      drafts: {}, checks: {}, states: {},
      scaffolds: {
        audience: { genre: "悬疑", reader: "成年读者", pleasure: "追索", source: "旧案", exclude: "不猎奇", emotion: "压迫", stance: "第三人称限知，过去时" },
        scenes: { lines: [], list: [
          { id: "S01", type: "proactive", pov: "c1", place: "码头", event: "取账本", crucible: "退不出的困局", fn: "起疑", spine: "" },
        ] },
        planning: { sel: "S01", plans: {
          S01: { mode: "proactive", goal: "拿到账本", conflict: "三轮受阻", setback: "账本被烧", onstage: ["c2", "", "c3"], story_time: "第三天傍晚", reader_emotion: "替她捏一把汗" },
        } },
      },
    };
    expect(mod.canonFromFE("audience", saved).narrative_stance).toBe("第三人称限知，过去时");
    expect(mod.feFromCanon("audience", { narrative_stance: "第一人称，现在时" }).scaffold.stance).toBe("第一人称，现在时");

    const canon = mod.canonFromFE("planning", saved);
    expect(canon.scenes[0].onstage_chars_json).toEqual(["c2", "c3"]);
    expect(canon.scenes[0].story_time).toBe("第三天傍晚");
    expect(canon.scenes[0].expected_reader_emotion).toBe("替她捏一把汗");

    const hydrated = mod.feFromCanon("planning", { scenes: [
      { row_uid: "S01", primary_form: "proactive", onstage_chars_json: ["c2"], story_time: "第四天清晨", expected_reader_emotion: "松一口气又不安" },
      { row_uid: "S02", primary_form: "reactive" },
    ] });
    expect(hydrated.scaffold.plans.S01).toEqual(expect.objectContaining({ onstage: ["c2"], story_time: "第四天清晨", reader_emotion: "松一口气又不安" }));
    expect(hydrated.scaffold.plans.S02).toEqual(expect.objectContaining({ onstage: [], story_time: "", reader_emotion: "" }));
  });

  it("阶段 L：04 的全书主角往返保真；旧缓存没有这个键时不上行", async () => {
    const { mod } = await loadSync({ snowflakeWorkspace: { ready_to_materialize: false, steps: [] } });
    const withPick = { drafts: {}, checks: {}, states: {}, scaffolds: { characters: { sel: "c1", protagonist: "c2", chars: {
      c1: { name: "甲", role: "主角", goal: "x", values: "" }, c2: { name: "乙", role: "主角", goal: "y", values: "" },
    } } } };
    expect(mod.canonFromFE("characters", withPick).protagonist_character_id).toBe("c2");
    const legacy = { drafts: {}, checks: {}, states: {}, scaffolds: { characters: { sel: "c1", chars: { c1: { name: "甲", role: "主角", goal: "x", values: "" } } } } };
    expect(mod.canonFromFE("characters", legacy)).not.toHaveProperty("protagonist_character_id");
    const hydrated = mod.feFromCanon("characters", { protagonist_character_id: "c2", characters: [
      { character_id: "c1", display_name: "甲", role: "主角" }, { character_id: "c2", display_name: "乙", role: "主角" },
    ] });
    expect(hydrated.scaffold.protagonist).toBe("c2");
    expect(mod.feFromCanon("profile", { characters: [{ character_id: "c1", display_name: "甲" }] }).scaffold).not.toHaveProperty("protagonist");
  });

  it("feFromCanon backstory：前缀行拆回六字段，无前缀散文整段进「视角故事」", async () => {
    const { mod } = await loadSync({});
    const withPrefix = mod.feFromCanon("backstory", { characters: [{
      character_id: "c1", display_name: "林岑", role: "主角",
      synopsis: "信念：记录即救赎\n旧伤：父亲失踪于那年潮汐\n关系：周岚的养女",
    }] });
    const c1 = withPrefix.scaffold.chars.c1;
    expect(c1.belief).toBe("记录即救赎");
    expect(c1.wound).toBe("父亲失踪于那年潮汐");
    expect(c1.relation).toBe("周岚的养女");
    expect(c1.desire).toBe("");

    const prose = mod.feFromCanon("backstory", { characters: [{
      character_id: "c2", display_name: "周岚", role: "对手", synopsis: "她在暴雨夜做了那个决定。\n此后每一年都在偿还。",
    }] });
    // 阶段 D：没有前缀的整段角色梗概就是书里第 5 步的视角故事，不再塞进「信念」
    expect(prose.scaffold.chars.c2.povstory).toBe("她在暴雨夜做了那个决定。\n此后每一年都在偿还。");
    expect(prose.scaffold.chars.c2.belief).toBe("");
  });

  /* —— 阶段 D（方法保真）：04 故事线两栏 + 多条价值观、06 视角故事第六行、07 五段展开 —— */
  it("characters 往返：价值观一行一条按「没有什么比___更重要」上行，故事线两栏往返，旧缓存缺键不上行", async () => {
    const { mod } = await loadSync({});
    const saved = { scaffolds: { characters: { sel: "c1", chars: {
      c1: { name: "林岑", role: "主角", goal: "查清谁改了档案", ambition: "被看见", values: "真相\n弟弟活着", conflict: "恩师挡路", epiphany: "给活人",
            storyline: "林岑必须交出母本，但交出去弟弟就没了退路。", storyline_para: "她从档案室的一页缺口进入故事……" },
      // 旧缓存：hydrate 于阶段 D 之前，没有 storyline 键 —— 不能把服务端 AI 生成过的故事线清空
      c2: { name: "周岚", role: "对立面", goal: "封存档案", ambition: "", values: "没有什么比体面更重要。", conflict: "", epiphany: "" },
    } } } };
    const canon = mod.canonFromFE("characters", saved);
    const byId = Object.fromEntries(canon.characters.map(c => [c.character_id, c]));
    expect(byId.c1.values).toEqual(["没有什么比真相更重要", "没有什么比弟弟活着更重要"]);
    expect(byId.c1.one_sentence_summary).toBe("林岑必须交出母本，但交出去弟弟就没了退路。");
    expect(byId.c1.one_paragraph_summary).toBe("她从档案室的一页缺口进入故事……");
    expect(byId.c2.values).toEqual(["没有什么比体面更重要。"]);            // 已是整句：原样上行
    expect(byId.c2).not.toHaveProperty("one_sentence_summary");
    expect(byId.c2).not.toHaveProperty("one_paragraph_summary");

    const hydrated = mod.feFromCanon("characters", { characters: [{
      character_id: "c1", display_name: "林岑", role: "主角",
      values: ["没有什么比真相更重要", "没有什么比弟弟活着更重要。", "相信记录即救赎"],
      one_sentence_summary: "一句话线", one_paragraph_summary: "一段话线",
    }] });
    const c1 = hydrated.scaffold.chars.c1;
    expect(c1.values).toBe("真相\n弟弟活着\n相信记录即救赎");   // 句式剥掉给输入框，旧式陈述原样保留
    expect(c1.storyline).toBe("一句话线");
    expect(c1.storyline_para).toBe("一段话线");
  });

  it("backstory 往返：视角故事作为第六个前缀行打包并拆回", async () => {
    const { mod } = await loadSync({});
    const saved = { scaffolds: { backstory: { sel: "c1", chars: {
      c1: { name: "林岑", role: "主角", belief: "记录即救赎", wound: "", desire: "", fear: "", relation: "周岚的养女", povstory: "在她眼里这是一场归还。\n她以为周岚只是怕丑闻。" },
    } } } };
    const canon = mod.canonFromFE("backstory", saved);
    expect(canon.characters[0].synopsis).toBe("信念：记录即救赎\n关系：周岚的养女\n视角故事：在她眼里这是一场归还。\n她以为周岚只是怕丑闻。");
    const back = mod.feFromCanon("backstory", { characters: [{ character_id: "c1", display_name: "林岑", role: "主角", synopsis: canon.characters[0].synopsis }] });
    expect(back.scaffold.chars.c1.povstory).toBe("在她眼里这是一场归还。\n她以为周岚只是怕丑闻。");
    expect(back.scaffold.chars.c1.relation).toBe("周岚的养女");
    expect(back.scaffold.chars.c1.belief).toBe("记录即救赎");
  });

  /* —— 阶段 E：失效的单一真相在后端——health 暴露 staleReason / inputRefs / stepRunId，
     accept-stale 留痕并刷新健康，upstreamChanges 用 input_refs 对照上游历史拼出消费版本 vs 当前版本 —— */
  it("阶段 E：后端 stale 步的健康带原因与消费版本；stale 水合成「已确认」而非「需补」", async () => {
    const ws = {
      ready_to_materialize: false, current_step_key: "one_paragraph_summary",
      steps: [
        { step_key: "one_sentence_summary", status: "approved", gate_satisfied: true, version: 2,
          draft: { summary: "林岑必须烧掉母本，但烧掉它养母就永远逍遥。" }, health: {}, completeness: {},
          artifact: { step_run_id: "run_logline_v2", input_refs: { book_brief: "run_brief_v1" } } },
        { step_key: "one_paragraph_summary", status: "stale", gate_satisfied: false, version: 1,
          stale_reason: "one_sentence_summary 改了被消费字段 ['summary']", stale_accepted_at: null,
          draft: { sentences: ["一", "二", "三", "四", "五"], moral_premise: "逃避代价只会放大伤害。" }, health: {}, completeness: {},
          artifact: { step_run_id: "run_para_v1", input_refs: { book_brief: "run_brief_v1", one_sentence_summary: "run_logline_v1" } } },
      ],
    };
    const { mod } = await loadSync({ snowflakeWorkspace: ws });
    window.dispatchEvent(new CustomEvent("ws:work-changed", { detail: "prj-main" }));
    await vi.waitFor(() => expect((mod.SnowSync.health("prj-main").paragraph || {}).beStatus).toBe("stale"), T);
    const h = mod.SnowSync.health("prj-main");
    expect(h.paragraph.staleReason).toContain("one_sentence_summary");
    expect(h.paragraph.staleAcceptedAt).toBeNull();
    expect(h.paragraph.stepRunId).toBe("run_para_v1");
    expect(h.paragraph.inputRefs).toEqual({ book_brief: "run_brief_v1", one_sentence_summary: "run_logline_v1" });
    expect(h.logline.stepRunId).toBe("run_logline_v2");
    expect(h.logline.version).toBe(2);
    // 规范字段水合：后端 stale 的步骤在前端仍是「已确认」，需复核由健康驱动，不再被打成「需补」
    const cache = JSON.parse(window.localStorage.getItem(CACHE_KEY));
    expect(cache.states.paragraph).toBe("done");
    expect(cache.states.logline).toBe("done");
  });

  it("E3 第二步：approve 回包自带 workspace → 下游 stale 立即进入权威健康，不必等下一次全量水合", async () => {
    const scaffold = { genre: "悬疑", reader: "成年读者", pleasure: "追索", source: "旧案", exclude: "不猎奇", emotion: "压迫" };
    const cache = { drafts: {}, scaffolds: { audience: scaffold }, checks: {}, states: { audience: "done" }, history: [] };
    window.localStorage.setItem(CACHE_KEY, JSON.stringify({ _t: Date.now() + 10_000, ...cache }));
    const { mod, client } = await loadSync({
      snowflakeWorkspace: {
        ready_to_materialize: false, current_step_key: "book_brief",
        steps: [
          { step_key: "book_brief", status: "pending_review", gate_satisfied: false, health: {}, completeness: {},
            draft: { category: scaffold.genre, target_reader: scaffold.reader, delight_reason: scaffold.pleasure, story_kind: scaffold.source,
              genre_promise: scaffold.exclude, expected_reader_emotion: scaffold.emotion, fe_text: "", fe_scaffold: scaffold, fe_checks: [], fe_state: "done", fe_t: 1 } },
          { step_key: "one_sentence_summary", status: "approved", gate_satisfied: true, version: 1, health: {}, completeness: {},
            draft: { summary: "林岑必须交出母本，但交出去弟弟就没了退路。" }, artifact: { step_run_id: "run_logline_v1", input_refs: { book_brief: "run_brief_v1" } } },
        ],
      },
    });
    client.apiPost.mockResolvedValue({
      step: { step_key: "book_brief", status: "approved", draft: {}, health: {}, completeness: {}, artifact: { step_run_id: "run_brief_v2", input_refs: {} } },
      workspace: { steps: [
        { step_key: "book_brief", status: "approved", gate_satisfied: true, draft: {}, health: {}, completeness: {}, artifact: { step_run_id: "run_brief_v2", input_refs: {} } },
        { step_key: "one_sentence_summary", status: "stale", gate_satisfied: false, version: 1, stale_reason: "book_brief 改了被消费字段 ['target_reader']", stale_accepted_at: null,
          draft: { summary: "林岑必须交出母本，但交出去弟弟就没了退路。" }, health: {}, completeness: {}, artifact: { step_run_id: "run_logline_v1", input_refs: { book_brief: "run_brief_v1" } } },
      ] },
    });
    await vi.waitFor(() => expect((mod.SnowSync.health("prj-main").audience || {}).beStatus).toBe("pending_review"), T);
    expect(mod.SnowSync.health("prj-main").logline.beStatus).toBe("approved");
    saveCache(cache);
    await vi.waitFor(() => expect(client.apiPost.mock.calls.some(([url]) => String(url).endsWith("/steps/book_brief/approve"))).toBe(true), T);
    await vi.waitFor(() => expect((mod.SnowSync.health("prj-main").logline || {}).beStatus).toBe("stale"), T);
    const logline = mod.SnowSync.health("prj-main").logline;
    expect(logline.staleReason).toContain("book_brief");
    expect(logline.inputRefs).toEqual({ book_brief: "run_brief_v1" });
    expect(mod.SnowSync.health("prj-main").audience.stepRunId).toBe("run_brief_v2");
  });

  it("阶段 E：acceptStale 走 accept-stale 端点并刷新权威健康；失败时不动健康", async () => {
    const { mod, client } = await loadSync({});
    mod.SnowSync.applyServerStep("prj-main", "paragraph", {
      step_key: "one_paragraph_summary", status: "stale", gate_satisfied: false,
      stale_reason: "one_sentence_summary 改了被消费字段 ['summary']", stale_accepted_at: null,
      draft: { sentences: ["一", "二", "三", "四", "五"] }, health: {}, completeness: {},
      artifact: { step_run_id: "run_para_v1", input_refs: { one_sentence_summary: "run_logline_v1" } },
    });
    client.apiPost.mockClear();
    client.apiPost.mockResolvedValueOnce({ step: {
      step_key: "one_paragraph_summary", status: "stale", gate_satisfied: true,
      stale_reason: "one_sentence_summary 改了被消费字段 ['summary']", stale_accepted_at: "2026-09-13T10:00:00Z",
      draft: { sentences: ["一", "二", "三", "四", "五"] }, health: {}, completeness: {},
      // E3 第二步：后端把消费的上游版本刷新到当前
      artifact: { step_run_id: "run_para_v1", input_refs: { one_sentence_summary: "run_logline_v2" } },
    }, workspace: { steps: [
      { step_key: "character_sheets", status: "approved", gate_satisfied: true, draft: { characters: [] }, health: {}, completeness: {}, artifact: { step_run_id: "run_chars_v1", input_refs: {} } },
    ] } });
    const events = [];
    const onHealth = () => events.push("health");
    window.addEventListener("ws:snow-health", onHealth);
    const health = await mod.SnowSync.acceptStale("prj-main", "paragraph", "措辞改动，五句不受影响");
    window.removeEventListener("ws:snow-health", onHealth);
    const call = client.apiPost.mock.calls.find(c => String(c[0]).includes("/steps/one_paragraph_summary/accept-stale"));
    expect(call).toBeTruthy();
    expect(call[1]).toEqual({ note: "措辞改动，五句不受影响" });
    expect(health.staleAcceptedAt).toBe("2026-09-13T10:00:00Z");
    expect(health.gateSatisfied).toBe(true);
    expect(health.inputRefs).toEqual({ one_sentence_summary: "run_logline_v2" });
    expect(mod.SnowSync.health("prj-main").characters.gateSatisfied).toBe(true); // 回包里的 workspace 也收进来
    expect(mod.SnowSync.health("prj-main").paragraph.staleAcceptedAt).toBe("2026-09-13T10:00:00Z");
    expect(events).toContain("health");

    // 失败：抛错给视图（视图据此不动本地图），健康保持原样
    client.apiPost.mockRejectedValueOnce(new Error("network down"));
    await expect(mod.SnowSync.acceptStale("prj-main", "paragraph")).rejects.toThrow("network down");
    expect(mod.SnowSync.health("prj-main").paragraph.staleAcceptedAt).toBe("2026-09-13T10:00:00Z");
    await expect(mod.SnowSync.acceptStale("prj-main", "not-a-step")).rejects.toThrow("步骤未知");
  });

  it("阶段 E：upstreamChanges 只对变了的上游拉历史，按 step_run_id 配对消费版本与当前版本并折成文本", async () => {
    const { mod, client } = await loadSync({});
    mod.SnowSync.applyServerStep("prj-main", "audience", {
      step_key: "book_brief", status: "approved", gate_satisfied: true, version: 1, draft: { category: "文学悬疑" }, health: {}, completeness: {},
      artifact: { step_run_id: "run_brief_v1", input_refs: {} },
    });
    mod.SnowSync.applyServerStep("prj-main", "logline", {
      step_key: "one_sentence_summary", status: "approved", gate_satisfied: true, version: 2,
      draft: { summary: "林岑必须烧掉母本，但烧掉它养母就永远逍遥。" }, health: {}, completeness: {},
      artifact: { step_run_id: "run_logline_v2", input_refs: { book_brief: "run_brief_v1" } },
    });
    mod.SnowSync.applyServerStep("prj-main", "paragraph", {
      step_key: "one_paragraph_summary", status: "stale", gate_satisfied: false, version: 1,
      stale_reason: "one_sentence_summary 改了被消费字段 ['summary']",
      draft: { sentences: ["一", "二", "三", "四", "五"] }, health: {}, completeness: {},
      artifact: { step_run_id: "run_para_v1", input_refs: { book_brief: "run_brief_v1", one_sentence_summary: "run_logline_v1" } },
    });
    client.apiGet.mockClear();
    client.apiGet.mockImplementation(async (url) => {
      if (String(url).includes("/steps/one_sentence_summary/history")) {
        expect(String(url)).toContain("include_draft=true");
        return { items: [
          { step_run_id: "run_logline_v2", version: 2, status: "approved", draft: { summary: "林岑必须烧掉母本，但烧掉它养母就永远逍遥。" } },
          { step_run_id: "run_logline_v1", version: 1, status: "superseded", draft: { summary: "林岑必须交出母本，但交出去弟弟就没了退路。" } },
        ] };
      }
      throw new Error("unexpected GET " + url);
    });
    const items = await mod.SnowSync.upstreamChanges("prj-main", "paragraph");
    expect(items).toHaveLength(1);                                  // book_brief 没变，不拉它的历史
    expect(items[0]).toMatchObject({ feKey: "logline", beKey: "one_sentence_summary", oldRunId: "run_logline_v1", newRunId: "run_logline_v2", oldFound: true, oldVersion: 1, newVersion: 2 });
    expect(items[0].oldText).toBe("林岑必须交出母本，但交出去弟弟就没了退路。");
    expect(items[0].newText).toBe("林岑必须烧掉母本，但烧掉它养母就永远逍遥。");
    expect(client.apiGet).toHaveBeenCalledTimes(1);

    // 旧版本已不在历史里：仍返回当前版本文本并标 oldFound=false；脚手架型步骤折成多行文本
    mod.SnowSync.applyServerStep("prj-main", "characters", {
      step_key: "character_sheets", status: "approved", gate_satisfied: true, version: 3,
      draft: { characters: [{ character_id: "c1", display_name: "林岑", role: "主角", goal: "查清谁改了档案", values: ["没有什么比真相更重要"] }] },
      health: {}, completeness: {}, artifact: { step_run_id: "run_chars_v3", input_refs: {} },
    });
    mod.SnowSync.applyServerStep("prj-main", "synopsis", {
      step_key: "short_synopsis", status: "stale", gate_satisfied: false, version: 1,
      draft: { paragraphs: ["", "", "", "", ""] }, health: {}, completeness: {},
      artifact: { step_run_id: "run_syn_v1", input_refs: { character_sheets: "run_chars_v1" } },
    });
    client.apiGet.mockImplementation(async (url) => {
      if (String(url).includes("/steps/character_sheets/history")) {
        return { items: [{ step_run_id: "run_chars_v3", version: 3, status: "approved", draft: { characters: [{ character_id: "c1", display_name: "林岑", role: "主角", goal: "查清谁改了档案", values: ["没有什么比真相更重要"] }] } }] };
      }
      throw new Error("unexpected GET " + url);
    });
    const [chars] = await mod.SnowSync.upstreamChanges("prj-main", "synopsis");
    expect(chars.oldFound).toBe(false);
    expect(chars.oldText).toBe("");
    expect(chars.newText).toContain("林岑");
    expect(chars.newText).toContain("查清谁改了档案");
    expect(chars.newText).toContain("真相");
    // 没有 input_refs 记录（旧数据）→ 空数组，不发任何请求
    client.apiGet.mockClear();
    mod.SnowSync.applyServerStep("prj-main", "backstory", {
      step_key: "character_synopses", status: "stale", gate_satisfied: false, version: 1, draft: { characters: [] }, health: {}, completeness: {},
      artifact: { step_run_id: "run_bk_v1" },
    });
    expect(await mod.SnowSync.upstreamChanges("prj-main", "backstory")).toEqual([]);
    expect(client.apiGet).not.toHaveBeenCalled();
  });

  it("outline 往返：paragraphs 是五段展开而非章行镜像；历史章行镜像水合成空槽；散文不造假章", async () => {
    const { mod } = await loadSync({});
    const saved = { scaffolds: { outline: {
      expansions: { setup: "雨城的第一天，她在档案室数缺页……", d1: "", d2: "中点：她发现改档案的是养母……", d3: "", resolution: "" },
      chapters: [
        { row_uid: "ch_a", id: "01", act: 1, title: "雨夜来信", summary: "信件迫使主角回乡", spine: "灾一", goal: "" },
        { row_uid: "ch_b", id: "02", act: 2, title: "旧屋回声", summary: "旧证词出现裂缝", spine: "", goal: "" },
      ],
    } } };
    const canon = mod.canonFromFE("outline", saved);
    expect(canon.paragraphs).toEqual(["雨城的第一天，她在档案室数缺页……", "", "中点：她发现改档案的是养母……", "", ""]);
    expect(canon.paragraphs.some(p => /章名|雨夜来信/.test(p))).toBe(false);   // 不再把章行写进 paragraphs
    expect(canon.chapters.map(c => c.title)).toEqual(["雨夜来信", "旧屋回声"]);

    // 阶段 D 之前的草稿：paragraphs 是按幕的章行镜像 —— 不是展开文，水合成空槽；章表照旧
    const legacy = mod.feFromCanon("outline", {
      paragraphs: ["01 雨夜来信：信件迫使主角回乡（灾一）\n02 旧屋回声：旧证词出现裂缝", "03 中点：养母", "", ""],
      chapters: [{ row_uid: "ch_a", act: 1, title: "雨夜来信", summary: "信件迫使主角回乡", spine: "灾一", chapter_goal: "" }],
    });
    expect(legacy.scaffold.expansions).toEqual({ setup: "", d1: "", d2: "", d3: "", resolution: "" });
    expect(legacy.scaffold.chapters.map(c => c.title)).toEqual(["雨夜来信"]);

    // 新草稿：五段散文进五槽；没有章表时散文绝不被解析成假章（与后端 parse_outline_chapters 同一纪律）
    const prose = mod.feFromCanon("outline", { paragraphs: ["第一段展开。", "第二段展开。", "第三段展开。", "第四段展开。", "第五段展开。"] });
    expect(prose.scaffold.expansions).toEqual({ setup: "第一段展开。", d1: "第二段展开。", d2: "第三段展开。", d3: "第四段展开。", resolution: "第五段展开。" });
    expect(prose.scaffold.chapters).toEqual([]);
    // 历史纯文本草稿（无 chapters）：只有真正的章行才成章
    const legacyText = mod.feFromCanon("outline", { paragraphs: ["01 雨夜来信：信件迫使主角回乡（灾一）\n这一行不是章", "", "", ""] });
    expect(legacyText.scaffold.chapters).toEqual([{ id: "01", act: 1, title: "雨夜来信", summary: "信件迫使主角回乡", spine: "灾一" }]);
  });

  /* —— 物化后回流（resync 补接）：pending 状态只读后端真相；resync() 同步后
     状态随回包刷新、目录重拉——写作台/AI 起草台才能拿到最新场景卡 —— */
  it("hydrate 捕获 resync_status；resync() 全量同步 → 状态清零 + 目录重拉", async () => {
    const wsPending = {
      ready_to_materialize: false,
      current_step_key: "book_brief",
      steps: [],
      resync_status: {
        pending_count: 2,
        pending_scene_plan_ids: ["sp1", "sp2"],
        pending_scenes: [
          { scene_plan_id: "sp1", scene_id: "s1", title: "改过的场", changed_fields: ["goal"] },
          { scene_plan_id: "sp2", scene_id: "s2", title: "另一场", changed_fields: ["title", "conflict"] },
        ],
      },
    };
    const { mod, client } = await loadSync({ snowflakeWorkspace: wsPending });
    window.dispatchEvent(new CustomEvent("ws:work-changed", { detail: "prj-main" }));
    await vi.waitFor(() => expect(mod.SnowSync.resyncStatus("prj-main").pendingCount).toBe(2), T);
    expect(mod.SnowSync.resyncStatus("prj-main").pendingScenes[0]).toEqual(
      { scenePlanId: "sp1", sceneId: "s1", title: "改过的场", changedFields: ["goal"] });

    // resync 回包自带清零后的 workspace → 状态就地刷新（无需再拉全量）
    client.apiPost.mockImplementation((url) => {
      if (url.endsWith("/snowflake-workspace/resync")) return Promise.resolve({
        dry_run: false,
        results: [
          { scene_plan_id: "sp1", scene_id: "s1", synced: true, reason: "changed" },
          { scene_plan_id: "sp2", scene_id: "s2", synced: false, reason: "already_current" },
        ],
        workspace: { ...wsPending, resync_status: { pending_count: 0, pending_scene_plan_ids: [], pending_scenes: [] } },
      });
      return Promise.resolve({});
    });
    client.apiGet.mockClear();
    const r = await mod.SnowSync.resync("prj-main");

    expect(client.apiPost).toHaveBeenCalledWith("/api/v2/projects/prj-main/snowflake-workspace/resync", {});
    expect(r.synced).toBe(1);
    expect(r.skipped).toBe(1);
    expect(mod.SnowSync.resyncStatus("prj-main").pendingCount).toBe(0);
    // 同步成功后目录重拉（写作台/起草台的场景卡三拍才会换新）
    await vi.waitFor(() =>
      expect(client.apiGet.mock.calls.some(c => /\/projects\/prj-main\/catalog/.test(String(c[0])))).toBe(true), T);
  });

  it("9/10 步保存上行后强制重拉工作台：resync 待同步数跟上（目录已有章时）", async () => {
    const { mod, client } = await loadSync({ snowflakeWorkspace: { ready_to_materialize: false, current_step_key: "scene_list", steps: [] } });
    window.dispatchEvent(new CustomEvent("ws:work-changed", { detail: "prj-main" }));
    // 目录装载（installApiRouter 默认一章一场）——强制重拉的前置条件
    await vi.waitFor(() => expect(window.WsCatalog && window.WsCatalog.get().length).toBeGreaterThan(0), T);
    expect(mod.SnowSync.resyncStatus("prj-main").pendingCount).toBe(0);

    // 此后的 workspace GET 返回「1 场待同步」——模拟 9 步改动已在服务端形成 diff
    const routed = client.apiGet.getMockImplementation();
    client.apiGet.mockImplementation((url) => {
      if (/\/snowflake-workspace(\?|$)/.test(String(url))) return Promise.resolve({
        ready_to_materialize: false, current_step_key: "scene_list", steps: [],
        resync_status: { pending_count: 1, pending_scene_plan_ids: ["sp1"], pending_scenes: [
          { scene_plan_id: "sp1", scene_id: "s1", title: "夜巡", changed_fields: ["goal"] },
        ] },
      });
      return routed(url);
    });

    saveCache({
      drafts: {},
      scaffolds: { scenes: { lines: [], list: [{ id: "S01", type: "proactive", line: "main", pov: "c1", place: "堤上", event: "夜巡", crucible: "潮水上涨", fn: "", spine: "" }] } },
      checks: {}, states: { scenes: "active" },
    });

    // 9 步 PATCH 后触发强制 hydrate → 捕获到最新 resync_status（若不强拉则永远是 0，可证伪）
    await vi.waitFor(() => expect(mod.SnowSync.resyncStatus("prj-main").pendingCount).toBe(1), T);
  });

  it("本机首次出现时已经是 done：分章预览先完成 PATCH + approve，再读取预览与物化闸门", async () => {
    const gate = {
      status: "ready", blockers: [], warnings: [], items: [],
    };
    const { mod, client } = await loadSync({
      snowflakeWorkspace: {
        ready_to_materialize: true,
        current_step_key: "book_brief",
        steps: [],
        materialization_gate: gate,
      },
    });
    const order = [];
    client.apiPatch.mockImplementation(async (url, body) => {
      order.push("patch");
      return { step: { status: "pending_review", draft: body.draft, health: {}, completeness: {} } };
    });
    client.apiPost.mockImplementation(async (url) => {
      if (String(url).endsWith("/chapter-plan/preview")) {
        order.push("preview");
        return { strategy: "spine_anchor", chapters: [], unassigned: [], warnings: [] };
      }
      if (String(url).endsWith("/steps/book_brief/approve")) {
        order.push("approve");
        return { step: { status: "approved", draft: {}, health: {}, completeness: {} } };
      }
      return {};
    });

    const cache = {
      drafts: {},
      scaffolds: { audience: { genre: "悬疑", reader: "成年读者", pleasure: "追索", source: "旧案", exclude: "不猎奇", emotion: "压迫" } },
      checks: {}, states: { audience: "done" }, revs: {}, confirmRevs: {}, history: [],
    };
    const flushLocal = () => saveCache(cache);
    window.addEventListener("ws:snow-flush-local", flushLocal);

    let preview;
    try {
      preview = await mod.SnowSync.chapterPreview("spine_anchor", "prj-main");
    } finally {
      window.removeEventListener("ws:snow-flush-local", flushLocal);
    }
    const firstPatch = order.indexOf("patch");
    const approve = order.indexOf("approve");
    const previewCall = order.indexOf("preview");
    expect(firstPatch).toBeGreaterThanOrEqual(0);
    expect(approve).toBeGreaterThan(firstPatch);
    expect(previewCall).toBeGreaterThan(approve);
    expect(preview.materialization_gate).toEqual(gate);
  });

  it("阶段 G：确认过的步骤被改动（revised_after_approval）不再自动补批准，等作者显式 approveStep", async () => {
    // 路由器每次都返回同一个工作台对象：改动之后把它改成「待审 + 确认后又改过」，模拟服务端的真实回包
    const ws = JSON.parse(JSON.stringify(WS_WITH_BOOK_BRIEF));
    const { mod, client } = await loadSync({ snowflakeWorkspace: ws });
    await vi.waitFor(() => expect((mod.SnowSync.health("prj-main").audience || {}).beStatus).toBe("approved"), T);
    client.apiPatch.mockImplementation(async (url, body) => ({
      step: { step_key: "book_brief", status: "pending_review", revised_after_approval: true, draft: body.draft, health: {}, completeness: {} },
    }));
    client.apiPost.mockImplementation(async (url) => {
      if (String(url).endsWith("/steps/book_brief/approve")) {
        return {
          step: { step_key: "book_brief", status: "approved", revised_after_approval: false, draft: {}, health: {}, completeness: {} },
          workspace: { steps: [] },
        };
      }
      return {};
    });

    // 本机 done 的步改了内容 → PATCH 出去，但**不**自动 approve。用 retry() 显式驱动本实例的一次上行，
    // 不依赖 700ms 的 autosave 定时器（旧模块实例的监听也会响应 ws:snow-saved，不能拿 mock 调用记录当本实例的证据）。
    window.localStorage.setItem(CACHE_KEY, JSON.stringify({
      _t: Date.now() + 10_000,
      drafts: {},
      scaffolds: { audience: { genre: "悬疑", reader: "改动后的读者画像", pleasure: "追索", source: "旧案", exclude: "不猎奇", emotion: "压迫" } },
      checks: {}, states: { audience: "done" }, history: [],
    }));
    ws.steps[0].status = "pending_review";
    ws.steps[0].revised_after_approval = true;
    ws.steps[0].gate_satisfied = false;
    const state = await mod.SnowSync.retry("prj-main");
    expect(state.phase).toBe("synced");
    expect((mod.SnowSync.health("prj-main").audience || {}).beStatus).toBe("pending_review");
    expect(client.apiPost.mock.calls.some(([url]) => String(url).endsWith("/steps/book_brief/approve"))).toBe(false);
    expect((mod.SnowSync.health("prj-main").audience || {}).revisedAfterApproval).toBe(true);
    expect(mod.SnowSync.needsReconfirm("prj-main", "audience")).toBe(true);

    // 作者显式重新确认 → POST approve，健康刷新
    const health = await mod.SnowSync.approveStep("prj-main", "audience");
    expect(client.apiPost.mock.calls.some(([url]) => String(url).endsWith("/steps/book_brief/approve"))).toBe(true);
    expect(health.beStatus).toBe("approved");
    expect(mod.SnowSync.needsReconfirm("prj-main", "audience")).toBe(false);
  });

  it("阶段 G：水合发现 pending_review 但 revised_after_approval：不补 approve（等作者重新确认）", async () => {
    const scaffold = {
      genre: "悬疑", reader: "成年读者", pleasure: "追索", source: "旧案", exclude: "不猎奇", emotion: "压迫",
    };
    const cache = {
      drafts: {}, scaffolds: { audience: scaffold }, checks: {}, states: { audience: "done" }, history: [],
    };
    window.localStorage.setItem(CACHE_KEY, JSON.stringify({ _t: Date.now() + 10_000, ...cache }));
    const { mod, client } = await loadSync({
      snowflakeWorkspace: {
        ready_to_materialize: false,
        current_step_key: "book_brief",
        steps: [{
          step_key: "book_brief",
          status: "pending_review",
          revised_after_approval: true,
          gate_satisfied: false,
          draft: {
            category: scaffold.genre,
            target_reader: scaffold.reader,
            delight_reason: scaffold.pleasure,
            story_kind: scaffold.source,
            genre_promise: scaffold.exclude,
            expected_reader_emotion: scaffold.emotion,
            fe_text: "",
            fe_scaffold: scaffold,
            fe_checks: [],
            fe_state: "done",
            fe_t: 1,
            fe_meta: { history: [] },
          },
          health: {}, completeness: {},
        }],
      },
    });
    client.apiPost.mockResolvedValue({ step: { status: "approved", draft: {}, health: {}, completeness: {} } });
    await vi.waitFor(() => expect((mod.SnowSync.health("prj-main").audience || {}).revisedAfterApproval).toBe(true), T);
    saveCache(cache);
    await vi.waitFor(() => expect(mod.SnowSync.syncState("prj-main").phase).toBe("synced"), T);
    expect(client.apiPost.mock.calls.some(([url]) => String(url).endsWith("/steps/book_brief/approve"))).toBe(false);
    expect(mod.SnowSync.needsReconfirm("prj-main", "audience")).toBe(true);
  });

  it("水合发现服务端仍是 pending_review：即使本机签名没有变化也会补 approve", async () => {
    const scaffold = {
      genre: "悬疑", reader: "成年读者", pleasure: "追索", source: "旧案", exclude: "不猎奇", emotion: "压迫",
    };
    const cache = {
      drafts: {}, scaffolds: { audience: scaffold }, checks: {}, states: { audience: "done" },
      revs: {}, confirmRevs: {}, history: [],
    };
    window.localStorage.setItem(CACHE_KEY, JSON.stringify({ _t: Date.now() + 10_000, ...cache }));
    const { mod, client } = await loadSync({
      snowflakeWorkspace: {
        ready_to_materialize: false,
        current_step_key: "book_brief",
        steps: [{
          step_key: "book_brief",
          status: "pending_review",
          gate_satisfied: false,
          draft: {
            category: scaffold.genre,
            target_reader: scaffold.reader,
            delight_reason: scaffold.pleasure,
            story_kind: scaffold.source,
            genre_promise: scaffold.exclude,
            expected_reader_emotion: scaffold.emotion,
            fe_text: "",
            fe_scaffold: scaffold,
            fe_checks: [],
            fe_state: "done",
            fe_t: 1,
            fe_meta: { revs: {}, confirmRevs: {}, history: [] },
          },
          health: {}, completeness: {},
        }],
      },
    });
    client.apiPost.mockResolvedValue({ step: { status: "approved", draft: {}, health: {}, completeness: {} } });
    await vi.waitFor(() => expect((mod.SnowSync.health("prj-main").audience || {}).beStatus).toBe("pending_review"), T);

    saveCache(cache);

    await vi.waitFor(
      () => expect(client.apiPost.mock.calls.some(([url]) => String(url).endsWith("/steps/book_brief/approve"))).toBe(true),
      T,
    );
  });

  it("十步待审稿都已在本机确认：水合后自动按依赖顺序补齐全部 approve", async () => {
    const pairs = [
      ["audience", "book_brief"], ["logline", "one_sentence_summary"], ["paragraph", "one_paragraph_summary"],
      ["characters", "character_sheets"], ["synopsis", "short_synopsis"], ["backstory", "character_synopses"],
      ["outline", "long_synopsis"], ["profile", "character_bibles"], ["scenes", "scene_list"], ["planning", "scene_details"],
    ];
    const remoteSteps = pairs.map(([, beKey]) => ({
      step_key: beKey,
      status: "pending_review",
      gate_satisfied: false,
      draft: { fe_state: "done", fe_t: 9_999_999_999_999 },
      health: {}, completeness: {},
    }));
    const { mod, client } = await loadSync({
      snowflakeWorkspace: {
        ready_to_materialize: false,
        current_step_key: "book_brief",
        steps: remoteSteps,
      },
    });
    client.apiPost.mockImplementation(async (url) => {
      const beKey = String(url).split("/steps/")[1]?.split("/approve")[0];
      return beKey ? { step: { step_key: beKey, status: "approved", draft: {}, health: {}, completeness: {} } } : {};
    });
    await vi.waitFor(() => expect((mod.SnowSync.health("prj-main").planning || {}).beStatus).toBe("pending_review"), T);

    await vi.waitFor(() => {
      const count = client.apiPost.mock.calls.filter(([url]) => String(url).endsWith("/approve")).length;
      expect(count).toBe(10);
    }, T);
    const approvals = client.apiPost.mock.calls.map(([url]) => String(url))
      .filter((url) => url.endsWith("/approve"))
      .map((url) => url.split("/steps/")[1].split("/approve")[0]);
    expect(approvals).toEqual(pairs.map(([, beKey]) => beKey));
    await vi.waitFor(
      () => expect(mod.SnowSync.syncState("prj-main")).toMatchObject({ phase: "synced", error: null }),
      T,
    );
  });

  it("断网导致 PATCH 失败：状态明确停在“仅本机”，重试成功后才标服务器已同步", async () => {
    const { mod, client } = await loadSync({ snowflakeWorkspace: { ready_to_materialize: false, current_step_key: "book_brief", steps: [] } });
    Object.defineProperty(window.navigator, "onLine", { configurable: true, value: false });
    const offline = Object.assign(new Error("网络不可达"), { code: "NETWORK_ERROR" });
    client.apiPatch.mockRejectedValue(offline);

    saveCache({
      drafts: {},
      scaffolds: { audience: { genre: "悬疑", reader: "成年读者", pleasure: "追索", source: "旧案", exclude: "不猎奇", emotion: "压迫" } },
      checks: {}, states: { audience: "active" },
    });

    await vi.waitFor(() => expect(mod.SnowSync.syncState("prj-main").phase).toBe("error"), T);
    expect(mod.SnowSync.syncState("prj-main")).toMatchObject({
      pendingSteps: expect.arrayContaining(["audience"]),
      error: expect.objectContaining({ message: "网络不可达", offline: true, scope: "remote" }),
    });

    Object.defineProperty(window.navigator, "onLine", { configurable: true, value: true });
    client.apiPatch.mockImplementation(async (url, body) => ({ step: { status: "pending_review", draft: body.draft, health: {}, completeness: {} } }));
    await mod.SnowSync.retry("prj-main");
    expect(mod.SnowSync.syncState("prj-main")).toMatchObject({ phase: "synced", pendingSteps: [], error: null });
  });

  it("approve 409 不再吞掉：记录待批准步骤，重试只要批准成功即可收敛", async () => {
    const { mod, client } = await loadSync({ snowflakeWorkspace: { ready_to_materialize: false, current_step_key: "book_brief", steps: [] } });
    client.apiPatch.mockImplementation(async (url, body) => ({ step: { status: "pending_review", draft: body.draft, health: {}, completeness: {} } }));
    const cache = {
      drafts: {},
      scaffolds: { audience: { genre: "悬疑", reader: "成年读者", pleasure: "追索", source: "旧案", exclude: "不猎奇", emotion: "压迫" } },
      checks: {}, states: { audience: "active" },
    };
    saveCache(cache);
    await vi.waitFor(() => expect(mod.SnowSync.syncState("prj-main").phase).toBe("synced"), T);

    const gate = Object.assign(new Error("前序闸门未满足"), { status: 409, code: "SNOWFLAKE_GATE_BLOCKED" });
    client.apiPost.mockRejectedValue(gate);
    saveCache({ ...cache, states: { audience: "done" } });
    await vi.waitFor(() => expect(mod.SnowSync.syncState("prj-main").phase).toBe("error"), T);
    expect(mod.SnowSync.syncState("prj-main")).toMatchObject({
      pendingSteps: expect.arrayContaining(["audience"]),
      failures: expect.arrayContaining([expect.objectContaining({ feKey: "audience", stage: "approve", code: "SNOWFLAKE_GATE_BLOCKED" })]),
    });

    client.apiPost.mockResolvedValue({ step: { status: "approved", draft: {}, health: {}, completeness: {} } });
    await mod.SnowSync.retry("prj-main");
    expect(mod.SnowSync.syncState("prj-main").phase).toBe("synced");
    expect(client.apiPost.mock.calls.some(([url]) => String(url).endsWith("/steps/book_brief/approve"))).toBe(true);
  });

  it("前序步骤未确认（SNOWFLAKE_PREVIOUS_STEP_REQUIRED）不算同步故障：draft 已上行则保持已同步、不弹红错", async () => {
    const { mod, client } = await loadSync({ snowflakeWorkspace: { ready_to_materialize: false, current_step_key: "book_brief", steps: [] } });
    client.apiPatch.mockImplementation(async (url, body) => ({ step: { status: "pending_review", draft: body.draft, health: {}, completeness: {} } }));
    const cache = {
      drafts: {},
      scaffolds: { audience: { genre: "悬疑", reader: "成年读者", pleasure: "追索", source: "旧案", exclude: "不猎奇", emotion: "压迫" } },
      checks: {}, states: { audience: "active" },
    };
    saveCache(cache);
    await vi.waitFor(() => expect(mod.SnowSync.syncState("prj-main").phase).toBe("synced"), T);

    // approve 被后端以「需要先确认前面的雪花步骤」挡下（重试无用，作者得先去确认上游步骤）。
    const prevGate = Object.assign(new Error("需要先确认前面的雪花步骤。"), { status: 409, code: "SNOWFLAKE_PREVIOUS_STEP_REQUIRED" });
    client.apiPost.mockRejectedValue(prevGate);
    saveCache({ ...cache, states: { audience: "done" } });

    // 先等 approve 真的被尝试过，再断言状态——否则可能断在「保存前的已同步」上，形成伪绿。
    await vi.waitFor(() => expect(client.apiPost.mock.calls.some(([url]) => String(url).endsWith("/steps/book_brief/approve"))).toBe(true), T);
    const state = mod.SnowSync.syncState("prj-main");
    expect(state.phase).toBe("synced"); // draft 已上行；确认待上游，不是「服务器同步失败」
    expect(state.error).toBeNull();
    expect(state.failures || []).toHaveLength(0);
  });
  it("阶段 M：钩子 / 离场变化往返；09 行只读章归属水合（章号退化值不显示，且从不上行）", async () => {
    const { mod } = await loadSync({ snowflakeWorkspace: { ready_to_materialize: false, steps: [] } });
    const saved = {
      drafts: {}, checks: {}, states: {},
      scaffolds: {
        scenes: { lines: [], list: [
          { id: "S01", type: "proactive", pov: "c1", place: "码头", event: "取账本", crucible: "退不出的困局", fn: "起疑", spine: "", chapter: "第一章 雨夜来信" },
        ] },
        planning: { sel: "S01", plans: {
          S01: { mode: "proactive", goal: "拿到账本", conflict: "三轮受阻", setback: "账本被烧", hook: "烧账本的人留下了她的名字", exit_change: "证据没了，嫌疑落到她头上" },
        } },
      },
    };
    const canon = mod.canonFromFE("planning", saved);
    expect(canon.scenes[0].hook).toBe("烧账本的人留下了她的名字");
    expect(canon.scenes[0].exit_change).toBe("证据没了，嫌疑落到她头上");
    // 章归属是分章面板的事：09 上行行里没有它
    expect(mod.canonFromFE("scenes", saved).scenes[0]).not.toHaveProperty("chapter");

    const hydrated = mod.feFromCanon("planning", { scenes: [
      { row_uid: "S01", primary_form: "proactive", hook: "她听见楼上有脚步", exit_change: "旅馆不再安全" },
      { row_uid: "S02", primary_form: "reactive" },
    ] });
    expect(hydrated.scaffold.plans.S01).toEqual(expect.objectContaining({ hook: "她听见楼上有脚步", exit_change: "旅馆不再安全" }));
    expect(hydrated.scaffold.plans.S02).toEqual(expect.objectContaining({ hook: "", exit_change: "" }));

    const scenes = mod.feFromCanon("scenes", { scenes: [
      { row_uid: "S01", scene_id: "prj-main_SC01", chapter_id: "prj-main_CH01", chapter_title: "第一章 雨夜来信", summary: "取账本" },
      // 服务端没有章题时会用 chapter_id 顶替——那不是章题，不展示
      { row_uid: "S02", scene_id: "prj-main_SC02", chapter_id: "prj-main_CH01", chapter_title: "prj-main_CH01", summary: "消化挫败" },
      { row_uid: "S03", scene_id: "prj-main_SC03", summary: "找证人" },
    ] }).scaffold.list;
    expect(scenes.map(s => s.chapter)).toEqual(["第一章 雨夜来信", "", ""]);
  });

  it("阶段 M：分诊随工作台水合——按 scene_id 对到 09 的 row_uid，作者裁定优先于系统建议", async () => {
    const ws = {
      ready_to_materialize: false, current_step_key: "scene_details",
      steps: [
        { step_key: "scene_list", status: "approved", gate_satisfied: true, health: {}, completeness: {},
          draft: { scenes: [
            { row_uid: "S01", scene_id: "prj-main_SC01", summary: "取账本", primary_form: "proactive" },
            { row_uid: "S02", scene_id: "prj-main_SC02", summary: "消化挫败", primary_form: "reactive" },
          ] } },
      ],
      triage_items: [
        // 只有系统建议（未裁定）：状态取建议
        { triage_id: "", scene_plan_id: "sp1", scene_id: "prj-main_SC01", status: "", recommended_status: "maybe", effective_status: "unreviewed",
          triage_source: "auto_diagnosis", score: 55, notes: "", missing_fields: ["crucible"], fix_steps: ["补坩埚"], repair_patch: {} },
        // 作者已裁定通过（覆盖了系统的重写建议）：状态取裁定
        { triage_id: "t2", scene_plan_id: "sp2", scene_id: "prj-main_SC02", status: "pass", recommended_status: "rewrite", effective_status: "pass",
          triage_source: "author_saved", score: 30, notes: "反应场就该短", missing_fields: [], fix_steps: [], repair_patch: { reaction: "手抖" }, manual_override: true },
        // 对不上任何 09 行的条目：按 scene_id 兜底键
        { triage_id: "", scene_plan_id: "sp9", scene_id: "prj-main_SC09", recommended_status: "pass", effective_status: "unreviewed", triage_source: "auto_diagnosis", score: 90 },
      ],
    };
    const { mod } = await loadSync({ snowflakeWorkspace: ws });
    window.dispatchEvent(new CustomEvent("ws:work-changed", { detail: "prj-main" }));
    await vi.waitFor(() => expect(mod.SnowSync.triageItems("prj-main")).toBeTruthy(), T);
    const saved = mod.SnowSync.triageItems("prj-main");
    expect(saved.source).toBe("workspace");
    expect(Object.keys(saved.items).sort()).toEqual(["S01", "S02", "prj-main_SC09"]);
    expect(saved.items.S01).toEqual(expect.objectContaining({ status: "maybe", score: 55, fix_steps: ["补坩埚"], missing_fields: ["crucible"], scene_plan_id: "sp1" }));
    expect(saved.items.S02).toEqual(expect.objectContaining({ status: "pass", recommended_status: "rewrite", notes: "反应场就该短", repair_patch: { reaction: "手抖" }, triage_id: "t2" }));
    expect(mod.SnowSync.triageItems("someone-else")).toBeNull();
  });

  it("阶段 M：skipStep 走 generate skip=true 并带理由，回包刷新本步与整个工作台的健康；未知步在同步层就拒绝", async () => {
    const { mod, client } = await loadSync({});
    client.apiPost.mockClear();
    client.apiPost.mockResolvedValueOnce({ step: {
      step_key: "character_sheets", status: "skipped", gate_satisfied: true, skip_reason: "先按梗概走，人物表等第二稿",
      draft: { characters: [] }, health: {}, completeness: {}, artifact: { step_run_id: "run_chars_skip", input_refs: {} },
    }, workspace: { steps: [
      { step_key: "short_synopsis", status: "approved", gate_satisfied: true, draft: { paragraphs: [] }, health: {}, completeness: {}, artifact: { step_run_id: "run_syn_v1", input_refs: {} } },
    ] } });
    const events = [];
    const onHealth = () => events.push("health");
    window.addEventListener("ws:snow-health", onHealth);
    const health = await mod.SnowSync.skipStep("prj-main", "characters", "先按梗概走，人物表等第二稿");
    window.removeEventListener("ws:snow-health", onHealth);
    const call = client.apiPost.mock.calls.find(c => String(c[0]).includes("/steps/character_sheets/generate"));
    expect(call).toBeTruthy();
    expect(call[1]).toEqual({ skip: true, skip_reason: "先按梗概走，人物表等第二稿" });
    expect(health.beStatus).toBe("skipped");
    expect(mod.SnowSync.health("prj-main").characters.beStatus).toBe("skipped");
    expect(mod.SnowSync.health("prj-main").synopsis.gateSatisfied).toBe(true); // 回包里的 workspace 也收进来
    expect(events).toContain("health");

    // 服务端拒绝（必填步 / 缺理由）：错误上抛给视图，本地健康不动
    client.apiPost.mockRejectedValueOnce(new Error("SNOWFLAKE_STEP_NOT_SKIPPABLE"));
    await expect(mod.SnowSync.skipStep("prj-main", "logline", "想跳过")).rejects.toThrow("SNOWFLAKE_STEP_NOT_SKIPPABLE");
    expect((mod.SnowSync.health("prj-main").logline || {}).beStatus || null).not.toBe("skipped");
    await expect(mod.SnowSync.skipStep("prj-main", "not-a-step", "x")).rejects.toThrow("步骤未知");
  });

  it("阶段 R：题名 / 篇幅带 / 必须出现 / 破例理由往返；题名留空不上行；scene_id ↔ row_uid 对照；裁定走 scene-triage 端点", async () => {
    const ws = {
      ready_to_materialize: false, current_step_key: "scene_details",
      steps: [
        { step_key: "scene_list", status: "approved", gate_satisfied: true, health: {}, completeness: {},
          draft: { scenes: [
            { row_uid: "S01", scene_id: "prj-main_SC01", summary: "取账本", primary_form: "proactive" },
            { row_uid: "S02", scene_id: "prj-main_SC02", summary: "消化挫败", primary_form: "reactive" },
          ] } },
      ],
      triage_items: [
        { triage_id: "t2", scene_plan_id: "sp2", scene_id: "prj-main_SC02", status: "cut", manual_status: "cut", recommended_status: "maybe", effective_status: "cut",
          triage_source: "author_saved", score: 30, notes: "", missing_fields: [], fix_steps: [], repair_patch: {}, manual_override: true },
      ],
    };
    const { mod, client } = await loadSync({ snowflakeWorkspace: ws });
    window.dispatchEvent(new CustomEvent("ws:work-changed", { detail: "prj-main" }));
    await vi.waitFor(() => expect(mod.SnowSync.triageItems("prj-main")).toBeTruthy(), T);
    expect(mod.SnowSync.rowUidForSceneId("prj-main", "prj-main_SC02")).toBe("S02");
    expect(mod.SnowSync.sceneIdForRow("prj-main", "S01")).toBe("prj-main_SC01");
    expect(mod.SnowSync.triageItems("prj-main").items.S02).toEqual(expect.objectContaining({ status: "cut", manual: true }));

    const saved = { drafts: {}, checks: {}, states: {}, scaffolds: {
      scenes: { lines: [], list: [
        { id: "S01", type: "proactive", pov: "c1", place: "码头", event: "取账本", crucible: "退不出的困局", fn: "起疑", spine: "" },
        { id: "S02", type: "reactive", pov: "c1", place: "旅馆", event: "消化挫败", crucible: "无人可信", fn: "转向", spine: "" },
      ] },
      planning: { sel: "S01", plans: {
        S01: { mode: "proactive", goal: "拿到账本", conflict: "三轮受阻", setback: "账本被烧", title: "", length: "800-1200", must_include: "「你以为我不知道？」", exception: "" },
        S02: { mode: "reactive", reaction: "手抖", dilemma: "报警或沉默", decision: "去找证人", title: "雨夜旅馆", exception: "过场：决定在上一场已经做了" },
      } } } };
    const canon = mod.canonFromFE("planning", saved);
    expect(canon.scenes[0]).not.toHaveProperty("title"); // 题名留空 = 跟随 09，不再覆盖服务端（模型）的短题名
    expect(canon.scenes[0]).toEqual(expect.objectContaining({ target_length_band: "800-1200", must_include_text: "「你以为我不知道？」", exception_reason: "" }));
    expect(canon.scenes[1]).toEqual(expect.objectContaining({ title: "雨夜旅馆", exception_reason: "过场：决定在上一场已经做了" }));
    // 旧缓存没有这些键：不上行，服务端（模型）给的值不被抹掉
    const legacy = mod.canonFromFE("planning", { scaffolds: { scenes: saved.scaffolds.scenes, planning: { sel: "S01", plans: { S01: { mode: "proactive", goal: "g" } } } } });
    expect(legacy.scenes[0]).not.toHaveProperty("target_length_band");
    expect(legacy.scenes[0]).not.toHaveProperty("must_include_text");
    expect(legacy.scenes[0]).not.toHaveProperty("exception_reason");

    const hydrated = mod.feFromCanon("planning", { scenes: [
      { row_uid: "S01", primary_form: "proactive", summary: "取账本", title: "取账本", target_length_band: "long", must_include_text: "账本", exception_reason: "" },
      { row_uid: "S02", primary_form: "reactive", summary: "消化挫败", title: "雨夜旅馆", exception_reason: "过场" },
    ] });
    expect(hydrated.scaffold.plans.S01).toEqual(expect.objectContaining({ title: "", length: "long", must_include: "账本", exception: "" }));
    expect(hydrated.scaffold.plans.S02).toEqual(expect.objectContaining({ title: "雨夜旅馆", exception: "过场" }));

    client.apiPost.mockClear();
    client.apiPost.mockResolvedValueOnce({
      items: [{ triage_id: "t1", scene_plan_id: "sp1", scene_id: "prj-main_SC01", status: "cut", effective_status: "cut", recommended_status: "pass" }],
      workspace: { ...ws, ready_to_materialize: true, triage_items: [] },
    });
    const result = await mod.SnowSync.saveTriageVerdict("prj-main", { row_uid: "S01", status: "cut" });
    const call = client.apiPost.mock.calls.find(c => String(c[0]).includes("/snowflake-workspace/scene-triage"));
    expect(call).toBeTruthy();
    expect(call[1].items[0]).toEqual(expect.objectContaining({ scene_id: "prj-main_SC01", status: "cut" }));
    expect(result).toEqual(expect.objectContaining({ triage_id: "t1" }));
    await expect(mod.SnowSync.saveTriageVerdict("prj-main", { row_uid: "S01", status: "bogus" })).rejects.toThrow("非法的裁定");
  });
});


/* —— 阶段 T（2026-09-16）：作者意图要点镜像（direction_briefs）与作者编辑的乐观写入 / 回滚 —— */
describe("阶段 T · 作者意图要点镜像", () => {
  beforeEach(() => {
    vi.resetModules();
    window.localStorage.clear();
  });

  const BRIEF = {
    step_key: "one_sentence_summary", revision: 2, inherit_upstream: true, active_count: 1,
    lines: [{ line_id: "dl_1", kind: "decision", scope: "step", text: "主角是被动卷入", origin: "coach", status: "active" }],
    inherited: [{ step_key: "book_brief", step_label: "读者定位", line_id: "dl_0", kind: "constraint", text: "基调冷" }],
  };
  const WS = {
    ready_to_materialize: false,
    current_step_key: "one_sentence_summary",
    direction_briefs: { one_sentence_summary: BRIEF },
    steps: [{
      step_key: "one_sentence_summary", status: "pending_review", gate_satisfied: false, draft: { summary: "她回到雨城。" },
      health: { score: 60, status: "maybe", gaps: [], next_actions: [],
        direction_brief: { used: true, revision: 1, sha: "abc", line_ids: ["dl_1"], inherited_line_ids: [], inherit_upstream: true } },
      completeness: { filled_count: 1, total_count: 1, missing_fields: [] },
    }],
  };

  it("水合把每步要点落镜像；health.direction_brief 的版本落后于当前 revision 即 stale", async () => {
    const { mod } = await loadSync({ snowflakeWorkspace: WS });
    await vi.waitFor(() => expect(mod.SnowSync.directionBrief("prj-main", "logline")).toBeTruthy(), T);
    expect(mod.SnowSync.directionBrief("prj-main", "logline").revision).toBe(2);
    expect(mod.SnowSync.directionBrief("prj-main", "audience")).toBeNull();
    expect(mod.SnowSync.briefUsage("prj-main", "logline")).toMatchObject({ hasBrief: true, stale: true, usedRevision: 1, currentRevision: 2 });
    expect(mod.SnowSync.briefUsage("prj-main", "audience")).toMatchObject({ hasBrief: false, stale: false });
    // 教练回包直接落镜像
    mod.SnowSync.setDirectionBrief("prj-main", "logline", { ...BRIEF, revision: 3, active_count: 2 });
    expect(mod.SnowSync.briefUsage("prj-main", "logline").currentRevision).toBe(3);
  });

  it("saveDirectionBrief：乐观写入（缺席 = 撤下、改过归作者），服务端结果覆盖镜像；失败回滚并上抛", async () => {
    const { mod, client } = await loadSync({ snowflakeWorkspace: WS });
    await vi.waitFor(() => expect(mod.SnowSync.directionBrief("prj-main", "logline")).toBeTruthy(), T);
    const served = {
      ...BRIEF, revision: 3, active_count: 2,
      lines: [
        { ...BRIEF.lines[0], scope: "book", origin: "author" },
        { line_id: "dl_2", kind: "constraint", scope: "step", text: "不出现凶手", origin: "author", status: "active" },
      ],
    };
    let resolveServer = null;
    client.apiPut.mockImplementationOnce(() => new Promise(resolve => { resolveServer = () => resolve({ direction_brief: served }); }));
    const pending = mod.SnowSync.saveDirectionBrief("prj-main", "logline", { lines: [
      { line_id: "dl_1", kind: "decision", scope: "book", text: "主角是被动卷入" },
      { kind: "constraint", scope: "step", text: "不出现凶手" },
    ] });
    // 乐观：本地立刻反映——改了范围的条目归作者，新条目带临时 id，请求体不带临时 id
    const optimistic = mod.SnowSync.directionBrief("prj-main", "logline");
    expect(optimistic.lines.find(l => l.line_id === "dl_1")).toMatchObject({ scope: "book", origin: "author" });
    expect(optimistic.lines.find(l => l.text === "不出现凶手").line_id).toMatch(/^local_/);
    expect(optimistic.active_count).toBe(2);
    const [url, body] = client.apiPut.mock.calls[0];
    expect(url).toBe("/api/v2/projects/prj-main/snowflake-workspace/steps/one_sentence_summary/direction-brief");
    expect(body.lines[0]).toMatchObject({ line_id: "dl_1", scope: "book", status: "active" });
    expect(body.lines[1].line_id).toBeUndefined();
    expect(body.inherit_upstream).toBeUndefined();
    resolveServer();
    await pending;
    expect(mod.SnowSync.directionBrief("prj-main", "logline").revision).toBe(3);
    expect(mod.SnowSync.directionBrief("prj-main", "logline").lines[1].line_id).toBe("dl_2");

    // 失败：回滚到失败前的镜像，错误上抛给视图诚实提示
    client.apiPut.mockRejectedValueOnce(new Error("network down"));
    await expect(mod.SnowSync.saveDirectionBrief("prj-main", "logline", { inherit_upstream: false })).rejects.toThrow("network down");
    expect(mod.SnowSync.directionBrief("prj-main", "logline").inherit_upstream).toBe(true);
    expect(mod.SnowSync.directionBrief("prj-main", "logline").revision).toBe(3);
  });
});
