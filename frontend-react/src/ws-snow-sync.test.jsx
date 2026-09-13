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
      revs: { ...Object.fromEntries(feKeys.map(k => [k, 0])), ...(saved.revs || {}) },
      confirmRevs: { ...(saved.confirmRevs || {}) },
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
    // 主动场没有「概述两段」这一说：即便本机脚手架残留了 rendering，也不上行
    expect(canon.scenes[0]).not.toHaveProperty("rendering_mode");
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
});
