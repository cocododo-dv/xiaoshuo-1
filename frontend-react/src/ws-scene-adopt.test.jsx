// AI 起草台 · 采纳并归档：作者稿修订与归档原子提交、作者状态门、预检先等服务器上的作者稿。
// （前两块 2026-09-29 从 ws-scene-run.test.jsx 原样拆出。）
import React, { act } from "react";
import { createRoot } from "react-dom/client";
import { readFileSync } from "node:fs";
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { installApiRouter, DEFAULT_CHAP, DEFAULT_PROJECT } from "./test-helpers.js";
import {
  T, RUN_STATES_URL, NON_DEMO_PROJECT, TWO_SCENE_CHAP, settleActive, routeRunStates, loadSceneRun,
  mountedRoots, renderRunJobControl, click, deferred, queueSceneIntent,
} from "./ws-scene-run.test-harness.jsx";

vi.mock("./lib/client.js", () => ({
  apiGet: vi.fn(),
  apiPost: vi.fn(),
  apiPatch: vi.fn(),
  apiDelete: vi.fn(),
}));
// 任务控制条的两个请求（ws-scene-job-api.js）
vi.mock("./ws-scene-job-api.js", () => ({ cancelRunJob: vi.fn(), getLatestSceneRunJob: vi.fn() }));

/* ==========================================================
   采纳归档必须把浏览器当前正文、作者稿修订和 FinalScene 绑定为一次事务。
   · 成功路径 = POST exact_author_draft 保存+归档成功 → 吸收服务端修订并置 done
   · 后端拒绝（冲突/来源安全）→ 不置 done、不把待采用稿写成当前正文
   ========================================================== */
describe("scnAdoptToDoc（精确作者稿修订的原子归档）", () => {
  beforeEach(() => {
    vi.resetModules();
    window.localStorage.clear();
    vi.spyOn(window, "confirm").mockReturnValue(true);
  });
  afterEach(() => vi.restoreAllMocks());

  const DRAFT = [{ id: "p1", parts: [{ text: "潮水退去，她看清了闸门上的名字。" }] }];

  async function loadWithCatalog(opts) {
    const { mod, client } = await loadSceneRun(opts);
    const cat = await import("./ws-catalog.jsx");
    await vi.waitFor(() => expect(cat.WsCatalog.get().length).toBeGreaterThan(0), T);
    let revision = 1;
    let content = "";
    const basePost = client.apiPost.getMockImplementation();
    const basePatch = client.apiPatch.getMockImplementation();
    client.apiPost.mockImplementation((url, body, options) => {
      if (/\/api\/v1\/author-drafts\/scene\/s1\/ensure$/.test(url)) {
        return Promise.resolve({
          draft: {
            draft_id: "author_draft_scene_s1",
            revision_no: revision,
            content,
            last_promoted_revision_no: null,
            last_promoted_final_scene_row_id: null,
            canonical_dirty: true,
          },
          runtime_final_ref: null,
        });
      }
      return basePost(url, body, options);
    });
    client.apiPatch.mockImplementation((url, body, options) => {
      if (/\/api\/v1\/author-drafts\/author_draft_scene_s1$/.test(url)) {
        revision += 1;
        content = body.content;
        return Promise.resolve({ draft: { draft_id: "author_draft_scene_s1", revision_no: revision, content } });
      }
      return basePatch(url, body, options);
    });
    return { mod, client, cat };
  }

  it("成功：把确切正文和 base revision 原子提交，回包后才置 done + 同步缓存", async () => {
    const { mod, client, cat } = await loadWithCatalog();
    const basePost = client.apiPost.getMockImplementation();
    client.apiPost.mockImplementation((url, body, options) => {
      if (/\/api\/v1\/scenes\/s1\/adopt-current$/.test(url)) {
        return Promise.resolve({
          scene_id: "s1",
          scene_status: "archived",
          final_scene_row_id: "final_s1_v1",
          draft_id: body.exact_author_draft.draft_id,
          draft_revision_no: 2,
          content_hash: "hash-exact",
          author_draft: {
            draft_id: body.exact_author_draft.draft_id,
            revision_no: 2,
            content: body.exact_author_draft.content,
            last_promoted_revision_no: 2,
            last_promoted_final_scene_row_id: "final_s1_v1",
            canonical_dirty: false,
          },
        });
      }
      return basePost(url, body, options);
    });

    const result = await mod.scnAdoptToDoc("ch01s1", DRAFT);

    expect(result.ok).toBe(true);
    expect(client.apiPost).toHaveBeenCalledWith("/api/v1/scenes/s1/adopt-current", {
      accepted_warning_codes: [],
      exact_author_draft: {
        draft_id: "author_draft_scene_s1",
        base_revision_no: 1,
        expected_current_final_scene_row_id: null,
        content: "<p>潮水退去，她看清了闸门上的名字。</p>",
      },
    });
    expect(client.apiPatch.mock.calls.filter(([url]) => /\/author-drafts\//.test(url))).toEqual([]);
    // done 只由服务端 archived 响应映射，且写穿到目录 PATCH（mock 后端重拉
    // 会把乐观缓存收敛回 mock 值，故断言写穿动作而非最终缓存态）
    await vi.waitFor(() => expect(client.apiPatch).toHaveBeenCalledWith(
      expect.stringMatching(/\/scenes\/s1$/),
      expect.objectContaining({ state: "done" })
    ), T);
    void cat;
    // 正文写作器缓存同步（写穿主路径或缓存）
    const wrKeys = Object.keys(window.localStorage).filter(k => k.includes("wr-doc:ch01s1"));
    expect(wrKeys.length).toBeGreaterThan(0);
  });

  it("归档成功但成稿门报了专名（不拦）：结果带上中文的一句，归档照常", async () => {
    const { mod, client } = await loadWithCatalog();
    const basePost = client.apiPost.getMockImplementation();
    client.apiPost.mockImplementation((url, body, options) => {
      if (/\/api\/v1\/scenes\/s1\/adopt-current$/.test(url)) {
        return Promise.resolve({
          scene_id: "s1",
          scene_status: "archived",
          final_scene_row_id: "final_s1_v1",
          draft_id: body.exact_author_draft.draft_id,
          draft_revision_no: 2,
          author_draft: {
            draft_id: body.exact_author_draft.draft_id,
            revision_no: 2,
            content: body.exact_author_draft.content,
            last_promoted_revision_no: 2,
            last_promoted_final_scene_row_id: "final_s1_v1",
            canonical_dirty: false,
          },
          validation: {
            final_text_gate: {
              archive_blockers: [],
              warnings: [{ issue_key: "source_safety:protected_term", blocking: false, terms: ["灰港学会"], hit_count: 1 }],
            },
          },
        });
      }
      return basePost(url, body, options);
    });

    const result = await mod.scnAdoptToDoc("ch01s1", DRAFT);

    expect(result.ok).toBe(true);
    expect(result.archived).toBe(true);
    expect(result.gateNotes).toEqual([
      "正文用了参考书里的专名「灰港学会」（共 1 处）。这一项不拦归档；是参考书里的人名、地名或设定名的话，建议换成你自己的——只是日常用词被误收进专名表的，可以到文风画像的禁用词里删掉它。",
    ]);
  });

  it("后端拒绝（409 无稿/来源安全）：不置 done、不写缓存、faithful 返回失败", async () => {
    const { mod, client, cat } = await loadWithCatalog();
    const blocked = Object.assign(new Error("blocked"), { code: "SOURCE_SAFETY_BLOCKED" });
    const basePost = client.apiPost.getMockImplementation();
    client.apiPost.mockImplementation((url, body, options) => {
      if (/\/adopt-current$/.test(url)) return Promise.reject(blocked);
      return basePost(url, body, options);
    });

    const result = await mod.scnAdoptToDoc("ch01s1", DRAFT);

    expect(result.ok).toBe(false);
    expect(result.reason).toContain("SOURCE_SAFETY_BLOCKED");
    // 可证伪：先本地置 done 的旧实现会发出 state:"done" 的目录 PATCH，此断言转红
    const donePatches = client.apiPatch.mock.calls.filter(c => c[1] && c[1].state === "done");
    expect(donePatches).toEqual([]);
    const scene = cat.WsCatalog.get()[0].scenes.find(s => s.sid === "ch01s1");
    expect(scene.state).not.toBe("done");
    expect(Object.keys(window.localStorage).filter(k => k.includes("wr-doc:ch01s1"))).toEqual([]);
  });

  it("内容安全 409 保留结构化错误，并仅把当前 exact finding codes 传回服务端", async () => {
    const { mod, client } = await loadWithCatalog();
    const reviewError = Object.assign(new Error("review required"), {
      code: "CONTENT_SAFETY_REVIEW_REQUIRED",
      status: 409,
      details: {
        final_text_gate: {
          content_safety: {
            findings: [{
              code: "sexual_content_with_minor_indicators",
              review_required: true,
              acknowledged: false,
            }],
          },
        },
      },
    });
    const basePost = client.apiPost.getMockImplementation();
    client.apiPost.mockImplementation((url, body, options) => {
      if (!/\/adopt-current$/.test(url)) return basePost(url, body, options);
      if (!body.accepted_warning_codes.length) return Promise.reject(reviewError);
      return Promise.resolve({
        scene_status: "archived",
        final_scene_row_id: "final_s1_v1",
        draft_id: body.exact_author_draft.draft_id,
        draft_revision_no: 2,
        author_draft: { ...body.exact_author_draft, revision_no: 2, canonical_dirty: false },
      });
    });

    const blocked = await mod.scnAdoptToDoc("ch01s1", DRAFT);
    expect(blocked).toMatchObject({ ok: false, error: reviewError });
    expect(client.apiPost).toHaveBeenNthCalledWith(
      client.apiPost.mock.calls.findIndex(([url]) => /\/adopt-current$/.test(url)) + 1,
      "/api/v1/scenes/s1/adopt-current",
      expect.objectContaining({
        accepted_warning_codes: [],
        exact_author_draft: expect.objectContaining({
          draft_id: "author_draft_scene_s1",
          base_revision_no: 1,
        }),
      }),
    );

    const accepted = await mod.scnAdoptToDoc("ch01s1", DRAFT, null, {
      acceptedWarningCodes: ["sexual_content_with_minor_indicators"],
    });
    expect(accepted.ok).toBe(true);
    expect(client.apiPost).toHaveBeenCalledWith(
      "/api/v1/scenes/s1/adopt-current",
      expect.objectContaining({
        accepted_warning_codes: ["sexual_content_with_minor_indicators"],
        exact_author_draft: expect.objectContaining({ draft_id: "author_draft_scene_s1" }),
      }),
    );
  });

  it("内容安全复核重试复用已验证的作者稿备份，不制造重复副本", async () => {
    const { mod, client } = await loadWithCatalog();
    const key = window.wsKey("wr-doc:ch01s1");
    window.localStorage.setItem(key, "<p>作者亲写的正文。</p>");
    const reviewError = Object.assign(new Error("review required"), {
      code: "CONTENT_SAFETY_REVIEW_REQUIRED",
      status: 409,
      details: { final_text_gate: { content_safety: { findings: [] } } },
    });
    const basePost = client.apiPost.getMockImplementation();
    client.apiPost.mockImplementation((url, body, options) => {
      if (!/\/adopt-current$/.test(url)) return basePost(url, body, options);
      return body.accepted_warning_codes.length
        ? Promise.resolve({
            scene_status: "archived",
            final_scene_row_id: "final_s1_v1",
            draft_id: body.exact_author_draft.draft_id,
            draft_revision_no: 2,
            author_draft: { ...body.exact_author_draft, revision_no: 2, canonical_dirty: false },
          })
        : Promise.reject(reviewError);
    });

    const blocked = await mod.scnAdoptToDoc("ch01s1", DRAFT, null, {
      mode: "overwrite",
      confirmed: true,
    });
    expect(blocked).toMatchObject({
      ok: false,
      authorBackup: expect.objectContaining({ type: "backup", durable: true }),
    });
    expect(window.WrRecovery.list()).toHaveLength(1);

    const accepted = await mod.scnAdoptToDoc("ch01s1", DRAFT, null, {
      mode: "overwrite",
      confirmed: true,
      authorBackupId: blocked.authorBackup.id,
      acceptedWarningCodes: ["sexual_content_with_minor_indicators"],
    });
    expect(accepted.ok).toBe(true);
    expect(window.WrRecovery.list()).toHaveLength(1);
  });

  it("目录未同步到后端（无 backendId）：不静默装成功", async () => {
    const { mod } = await loadSceneRun({ catalog: [] });
    const cat = await import("./ws-catalog.jsx");
    await vi.waitFor(() => expect(cat.WsCatalog.ready()).toBe(true), T);
    const result = await mod.scnAdoptToDoc("ch99s9", DRAFT);
    expect(result.ok).toBe(false);
  });

  it("已有作者稿时可默认保存为候选：不调用归档、不覆盖正文", async () => {
    const { mod, client } = await loadWithCatalog();
    const key = window.wsKey("wr-doc:ch01s1");
    window.localStorage.setItem(key, "<p>作者亲写的正文。</p>");

    const result = await mod.scnAdoptToDoc("ch01s1", DRAFT);

    expect(result).toMatchObject({ ok: true, archived: false, mode: "candidate" });
    expect(window.localStorage.getItem(key)).toBe("<p>作者亲写的正文。</p>");
    expect(client.apiPost.mock.calls.filter(([url]) => /adopt-current/.test(url))).toEqual([]);
    expect(window.WrRecovery.list()).toEqual([
      expect.objectContaining({ sid: "ch01s1", type: "candidate", source: "ai" }),
    ]);
  });

  it("调用层只声明 overwrite 但没有显式确认时也 fail closed", async () => {
    const { mod, client } = await loadWithCatalog();
    const key = window.wsKey("wr-doc:ch01s1");
    window.localStorage.setItem(key, "<p>作者亲写的正文。</p>");

    const result = await mod.scnAdoptToDoc("ch01s1", DRAFT, null, { mode: "overwrite" });

    expect(result).toMatchObject({ ok: false, confirmationRequired: true });
    expect(window.localStorage.getItem(key)).toBe("<p>作者亲写的正文。</p>");
    expect(window.WrRecovery.list()).toEqual([]);
    expect(client.apiPost.mock.calls.filter(([url]) => /adopt-current/.test(url))).toEqual([]);
  });

  it("作者正文后文恰好写到旧占位那句话：仍是作者稿，覆盖必须先确认", async () => {
    // 旧判定：整份草稿里出现过「在这里开始写这一场」就当空稿——这里会不经确认直接覆盖并发 adopt-current
    const { mod, client } = await loadWithCatalog();
    const key = window.wsKey("wr-doc:ch01s1");
    const authored = "<p>她把纸条翻过来。</p><p>背面只有一行字：在这里开始写这一场……</p>";
    window.localStorage.setItem(key, authored);

    const preview = await mod.scnPrepareAdoption("ch01s1", DRAFT);
    expect(preview.hasReal).toBe(true);

    const result = await mod.scnAdoptToDoc("ch01s1", DRAFT, null, { mode: "overwrite" });
    expect(result).toMatchObject({ ok: false, confirmationRequired: true });
    expect(window.localStorage.getItem(key)).toBe(authored);
    expect(client.apiPost.mock.calls.filter(([url]) => /adopt-current/.test(url))).toEqual([]);
  });

  /* 两次各是新打开的一页：本机缓存里各是一份旧草稿（W1 复核二起，一页打开过的场读的是这一页自己的那一份，
     另一页 / 别处后来直接写进本机存储的字不算这一页的正文，所以第二份草稿要在新的一页里打开） */
  it("旧草稿只剩开头那句占位：算空稿，不要求确认；占位后面接着写了字就算作者稿", async () => {
    window.localStorage.setItem("wr-doc:ch01s1::prj-main", "<p><br></p><p>在这里开始写这一场……</p>");
    const first = await loadWithCatalog();
    const empty = await first.mod.scnPrepareAdoption("ch01s1", DRAFT);
    expect(empty.hasReal).toBe(false);
    // 差异里也不把占位当成要删掉的一段
    expect(empty.diff.dels).toBe(0);

    vi.resetModules();
    window.localStorage.clear();
    window.localStorage.setItem("wr-doc:ch01s1::prj-main", "<p>在这里开始写这一场……潮水涨上来了。</p>");
    const second = await loadWithCatalog();
    const started = await second.mod.scnPrepareAdoption("ch01s1", DRAFT);
    expect(started.hasReal).toBe(true);
  });

  it("采用预检会先水合服务器作者稿：即使本机无缓存也不得直接覆盖", async () => {
    const { mod, client } = await loadWithCatalog();
    client.apiPost.mockImplementation((url) => {
      if (/\/author-drafts\/scene\/s1\/ensure$/.test(url)) {
        return Promise.resolve({ draft: { draft_id: "d-server", revision_no: 7, content: "<p>另一台设备写下的作者稿。</p>" } });
      }
      return Promise.resolve({});
    });

    const preview = await mod.scnPrepareAdoption("ch01s1", DRAFT);

    expect(preview.hasReal).toBe(true);
    expect(preview.existing).toBe("<p>另一台设备写下的作者稿。</p>");
    expect(preview.diff.dels).toBeGreaterThan(0);
  });

  it("明确覆盖时先持久备份作者稿，再归档并写入 AI 稿", async () => {
    const { mod, client } = await loadWithCatalog();
    const key = window.wsKey("wr-doc:ch01s1");
    window.localStorage.setItem(key, "<p>作者亲写的正文。</p>");
    const basePost = client.apiPost.getMockImplementation();
    client.apiPost.mockImplementation((url, body, options) => {
      if (/\/adopt-current$/.test(url)) return Promise.resolve({
        scene_status: "archived",
        final_scene_row_id: "final_s1_v1",
        draft_id: body.exact_author_draft.draft_id,
        draft_revision_no: 2,
        author_draft: { ...body.exact_author_draft, revision_no: 2, canonical_dirty: false },
      });
      return basePost(url, body, options);
    });

    const result = await mod.scnAdoptToDoc("ch01s1", DRAFT, null, { mode: "overwrite", confirmed: true });

    expect(result).toMatchObject({ ok: true, archived: true, authorBackup: expect.objectContaining({ durable: true }) });
    expect(window.WrRecovery.list()).toEqual([
      expect.objectContaining({ type: "backup", html: "<p>作者亲写的正文。</p>" }),
    ]);
    expect(window.localStorage.getItem(key)).toContain("潮水退去，她看清了闸门上的名字");
    expect(client.apiPost.mock.calls.some(([url]) => /adopt-current$/.test(url))).toBe(true);
  });

  it("覆盖前备份触发 quota 时 fail-safe：阻止归档，作者稿保持不变", async () => {
    const { mod, client } = await loadWithCatalog();
    const key = window.wsKey("wr-doc:ch01s1");
    window.localStorage.setItem(key, "<p>作者亲写的正文。</p>");
    const originalSetItem = Storage.prototype.setItem;
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(function quotaForBackup(storageKey, value) {
      if (String(storageKey).startsWith("wr-recovery:v1:")) throw new DOMException("full", "QuotaExceededError");
      return originalSetItem.call(this, storageKey, value);
    });

    const result = await mod.scnAdoptToDoc("ch01s1", DRAFT, null, { mode: "overwrite", confirmed: true });

    expect(result).toMatchObject({ ok: false, backupFailed: true });
    expect(window.localStorage.getItem(key)).toBe("<p>作者亲写的正文。</p>");
    expect(client.apiPost.mock.calls.filter(([url]) => /adopt-current/.test(url))).toEqual([]);
  });
});

/* ==========================================================
   Wave 2（结果闭环治理 §5.3/§5.4）：作者可见状态门。
   「无法继续」（hard_blocked = verified Q0/Q1，不可归档）与
   「已有稿但建议修改」（quality_warning = Q2/Q3，可归档）必须分开：
   · scnGateFrom 从 workbench/status 的 author_state 投影提取 gate
   · scnAdoptToDoc 对 canArchive=false 前置拦截（不发 adopt POST）
   · quality_warning 不拦归档
   ========================================================== */
describe("作者状态门（Wave 2：无法继续 vs 有稿建议修改）", () => {
  beforeEach(() => {
    vi.resetModules();
    window.localStorage.clear();
    vi.spyOn(window, "confirm").mockReturnValue(true);
  });
  afterEach(() => vi.restoreAllMocks());

  const DRAFT = [{ id: "p1", parts: [{ text: "潮水退去，她看清了闸门上的名字。" }] }];

  const HARD_BLOCKED_PROJECTION = {
    author_state: "hard_blocked",
    blocking_findings: [{ issue_key: "missing_required_text", quality_level: "Q1", verified_by: "scene_card_required_text" }],
    quality_warnings: [],
    recommended_actions: ["review_pipeline_gate"],
    can_archive: false,
  };
  const QUALITY_WARNING_PROJECTION = {
    author_state: "quality_warning",
    blocking_findings: [],
    quality_warnings: [{ issue_key: "pacing_flat", quality_level: "Q2" }],
    recommended_actions: ["adopt_or_patch"],
    can_archive: true,
  };

  it("scnGateFrom：hard_blocked 投影 → canArchive=false + 阻断条目", async () => {
    const { mod } = await loadSceneRun();
    const gate = mod.scnGateFrom({ author_state: HARD_BLOCKED_PROJECTION });
    expect(gate.authorState).toBe("hard_blocked");
    expect(gate.canArchive).toBe(false);
    expect(gate.blocking[0].issue_key).toBe("missing_required_text");
  });

  it("hard QC rewrite_brief becomes an actionable author rewrite instruction", async () => {
    const { mod } = await loadSceneRun();
    expect(mod.scnRewriteBriefFrom({
      hard_qc: { rewrite_brief: ["补齐推门动作", "明确主动销毁通行证"] },
      author_state: HARD_BLOCKED_PROJECTION,
    })).toBe("补齐推门动作；明确主动销毁通行证");
  });

  it("scnRewriteBriefFrom：读后端真实键名 hard_qc_summary / soft_qc_summary，硬优先于软", async () => {
    const { mod } = await loadSceneRun();
    // 服务端形状（services/scene_workbench.py `serialize_qc_summary`）
    expect(mod.scnRewriteBriefFrom({
      hard_qc_summary: { qc_type: "hard_qc", pass_flag: false, rewrite_brief: ["补齐推门动作", "明确主动销毁通行证"] },
      soft_qc_summary: { qc_type: "soft_qc", pass_flag: false, rewrite_brief: ["收紧结尾三句"] },
      author_state: HARD_BLOCKED_PROJECTION,
    })).toBe("补齐推门动作；明确主动销毁通行证");
    // 硬质检已过（rewrite_brief 为空列表）→ 退到软质检的修补建议，而不是 issue_key 拼接
    expect(mod.scnRewriteBriefFrom({
      hard_qc_summary: { qc_type: "hard_qc", pass_flag: true, rewrite_brief: [] },
      soft_qc_summary: { qc_type: "soft_qc", pass_flag: false, rewrite_brief: ["收紧结尾三句"] },
      author_state: HARD_BLOCKED_PROJECTION,
    })).toBe("收紧结尾三句");
    // 两份摘要都为 null（尚未跑质检）→ 退到 author_state 阻断项的可读原因
    expect(mod.scnRewriteBriefFrom({
      hard_qc_summary: null,
      soft_qc_summary: null,
      author_state: HARD_BLOCKED_PROJECTION,
    })).toBe("missing_required_text");
  });

  it("scnRewriteBriefFrom：旧键名 hard_qc / soft_qc / latest_qc 仍可兜底，且服务端键名优先", async () => {
    const { mod } = await loadSceneRun();
    expect(mod.scnRewriteBriefFrom({
      hard_qc_summary: { rewrite_brief: ["服务端硬指令"] },
      hard_qc: { rewrite_brief: ["旧键硬指令"] },
    })).toBe("服务端硬指令");
    expect(mod.scnRewriteBriefFrom({ soft_qc: { rewrite_brief: ["旧键软指令"] } })).toBe("旧键软指令");
    expect(mod.scnRewriteBriefFrom({ latest_qc: { rewrite_brief: ["旧键最近指令"] } })).toBe("旧键最近指令");
    // qc-reports 明细路径的原始 rewrite_brief_json 条目（instruction / carry_note_text）也不能变成 [object Object]
    expect(mod.scnRewriteBriefFrom({
      hard_qc_summary: { rewrite_brief: [{ instruction: "补齐推门动作" }, { carry_note_text: "通行证已销毁" }, "", null] },
    })).toBe("补齐推门动作；通行证已销毁");
    expect(mod.scnRewriteBriefFrom(null)).toBe("");
  });

  it("scnGateFrom：quality_warning 投影 → 可归档 + 警告随行；无投影 → null", async () => {
    const { mod } = await loadSceneRun();
    const gate = mod.scnGateFrom({ author_state: QUALITY_WARNING_PROJECTION });
    expect(gate.authorState).toBe("quality_warning");
    expect(gate.canArchive).toBe(true);
    expect(gate.warnings.length).toBe(1);
    expect(mod.scnGateFrom({})).toBeNull();
  });

  it("scnAdoptToDoc：gate 不可归档 → 前置拦截，不发 adopt-current POST", async () => {
    const { mod, client } = await loadSceneRun();
    const cat = await import("./ws-catalog.jsx");
    await vi.waitFor(() => expect(cat.WsCatalog.get().length).toBeGreaterThan(0), T);
    const gate = mod.scnGateFrom({ author_state: HARD_BLOCKED_PROJECTION });

    const result = await mod.scnAdoptToDoc("ch01s1", DRAFT, gate);

    expect(result.ok).toBe(false);
    expect(result.reason).toContain("已证实的硬问题");
    // 拦下的原因是说给作者的：不念英文 issue_key
    expect(result.reason).not.toMatch(/[a-z]+_[a-z_]+/);
    const adoptCalls = client.apiPost.mock.calls.filter(c => /adopt-current/.test(c[0]));
    expect(adoptCalls).toEqual([]);
    // 正文保留、不置 done、不写缓存
    expect(Object.keys(window.localStorage).filter(k => k.includes("wr-doc:ch01s1"))).toEqual([]);
  });

  it("Wave 3 终选三函数：盲化取数 / 选择提交 / 续跑（sid→后端 id 对位）", async () => {
    const { mod, client } = await loadSceneRun();
    const cat = await import("./ws-catalog.jsx");
    await vi.waitFor(() => expect(cat.WsCatalog.get().length).toBeGreaterThan(0), T);
    const base = client.apiGet.getMockImplementation();
    client.apiGet.mockImplementation((url) => {
      if (/\/api\/v1\/scenes\/s1\/style-candidates$/.test(url)) {
        return Promise.resolve({
          blinded: true,
          candidates: [
            { row_id: "cand_b", content: "候选乙全文" },
            { row_id: "cand_a", content: "候选甲全文" },
          ],
          selection: { decision_status: "awaiting", selected_row_id: null },
        });
      }
      return base(url);
    });
    client.apiPost.mockImplementation((url) => Promise.resolve({ ok: true, url }));

    const list = await mod.scnCandidates("ch01s1");
    // 盲化契约：按后端 blinded_order 原样呈现，不重排、无分数字段
    expect(list.blinded).toBe(true);
    expect(list.candidates.map(c => c.row_id)).toEqual(["cand_b", "cand_a"]);
    expect(list.candidates.every(c => !("adversarial_score" in c))).toBe(true);

    await mod.scnSelectCandidate("ch01s1", "cand_b", {
      no_clear_difference: true,
      preference_tags: ["style_match"],
    });
    expect(client.apiPost).toHaveBeenCalledWith(
      "/api/v1/scenes/s1/style-candidates/cand_b/select",
      expect.objectContaining({ no_clear_difference: true, preference_tags: ["style_match"] })
    );

    await mod.scnResumeAfterSelection("ch01s1");
    expect(client.apiPost).toHaveBeenCalledWith("/api/v1/scenes/s1/resume-after-selection", expect.anything());
  });

  it("Wave 3 终选锁定：SELECTION_LOCKED 拒绝原样上抛（不静默吞掉）", async () => {
    const { mod, client } = await loadSceneRun();
    const cat = await import("./ws-catalog.jsx");
    await vi.waitFor(() => expect(cat.WsCatalog.get().length).toBeGreaterThan(0), T);
    const locked = Object.assign(new Error("selection locked"), { code: "SELECTION_LOCKED" });
    client.apiPost.mockImplementation((url) => {
      if (/\/select$/.test(url)) return Promise.reject(locked);
      return Promise.resolve({});
    });

    await expect(mod.scnSelectCandidate("ch01s1", "cand_x", {})).rejects.toMatchObject({ code: "SELECTION_LOCKED" });
  });

  it("scnAdoptToDoc：quality_warning 的 gate 不拦归档（Q2/Q3 照常交付）", async () => {
    const { mod, client } = await loadSceneRun();
    const cat = await import("./ws-catalog.jsx");
    await vi.waitFor(() => expect(cat.WsCatalog.get().length).toBeGreaterThan(0), T);
    const basePost = client.apiPost.getMockImplementation();
    client.apiPost.mockImplementation((url, body, options) => {
      if (/adopt-current$/.test(url)) {
        return Promise.resolve({
          scene_id: "s1",
          scene_status: "archived",
          final_scene_row_id: "final_s1_v1",
          draft_id: body.exact_author_draft.draft_id,
          draft_revision_no: 2,
          author_draft: {
            ...body.exact_author_draft,
            revision_no: 2,
            last_promoted_revision_no: 2,
            last_promoted_final_scene_row_id: "final_s1_v1",
            canonical_dirty: false,
          },
        });
      }
      return basePost(url, body, options);
    });
    const gate = mod.scnGateFrom({ author_state: QUALITY_WARNING_PROJECTION });

    const result = await mod.scnAdoptToDoc("ch01s1", DRAFT, gate);

    expect(result.ok).toBe(true);
    expect(client.apiPost).toHaveBeenCalledWith("/api/v1/scenes/s1/adopt-current", expect.anything());
  });
});

/* ---- 预检先等服务器上的作者稿（F03-05）：WrDocs.hydrate 遇到别处正在进行的水合会立刻返回、出错也不抛 ---- */
async function loadAdoption() {
  const client = await import("./lib/client.js");
  installApiRouter(client);
  await import("./ws-catalog.jsx");
  await vi.waitFor(() => expect(window.WsWorks && window.WsWorks.activeId()).toBe("prj-main"), T);
  await vi.waitFor(() => expect(window.WsCatalog && window.WsCatalog.get().length).toBeGreaterThan(0), T);
  const api = await import("./ws-scene-api.js");
  const store = await import("./wr-doc-store.jsx");
  return { client, api, store };
}

const DRAFT = [{ id: "p1", text: "AI 起草的一段。" }];


describe("scnPrepareAdoption · 预检先等服务器上的作者稿", () => {
  beforeEach(() => {
    vi.resetModules();
    window.localStorage.clear();
  });
  afterEach(() => vi.restoreAllMocks());

  it("写作台的水合还在路上：等它落地，看得见作者正文（不当成空稿直接覆盖）", async () => {
    const { client, api, store } = await loadAdoption();
    const ensure = deferred();
    client.apiPost.mockImplementation((url) => (
      /\/author-drafts\/scene\/s1\/ensure$/.test(url) ? ensure.promise : Promise.resolve({})
    ));
    store.WrDocs.load("ch01s1"); // 写作台的预热水合先发出去
    const preview = api.scnPrepareAdoption("ch01s1", DRAFT);
    ensure.resolve({ draft: { draft_id: "d1", revision_no: 2, content: "<p>作者自己写的正文。</p>" } });
    const result = await preview;
    expect(result.hasReal).toBe(true);
    expect(result.existing).toContain("作者自己写的正文");
  });

  it("服务器读不到：停下来说「无法核对服务器上的作者稿」", async () => {
    const { client, api } = await loadAdoption();
    const down = Object.assign(new Error("offline"), { code: "NETWORK_ERROR" });
    client.apiPost.mockImplementation((url) => (
      /\/author-drafts\/scene\/s1\/ensure$/.test(url) ? Promise.reject(down) : Promise.resolve({})
    ));
    await expect(api.scnPrepareAdoption("ch01s1", DRAFT)).rejects.toMatchObject({ code: "AUTHOR_DRAFT_PREFLIGHT_FAILED" });
  });

  it("显式「存为候选」不碰作者稿，也就不必核对：服务器读不到时照样存进同步与恢复", async () => {
    const { client, api, store } = await loadAdoption();
    const down = Object.assign(new Error("offline"), { code: "NETWORK_ERROR" });
    client.apiPost.mockImplementation((url) => (
      /\/author-drafts\/scene\/s1\/ensure$/.test(url) ? Promise.reject(down) : Promise.resolve({})
    ));
    client.apiPost.mockClear();

    const draft = [{ id: "p1", parts: [{ text: "AI 起草的一段。" }] }];
    const result = await api.scnAdoptToDoc("ch01s1", draft, null, { mode: "candidate" });

    expect(result).toMatchObject({ ok: true, archived: false, mode: "candidate" });
    expect(store.WrRecovery.list()).toEqual([
      expect.objectContaining({ sid: "ch01s1", type: "candidate", source: "ai", html: "<p>AI 起草的一段。</p>" }),
    ]);
    expect(client.apiPost.mock.calls.filter(([url]) => /\/ensure$/.test(url))).toEqual([]);
  });
});

/* ---- 复核五 · 保护对话框开着时，服务端的作者稿在别处又存了一版（W1-R5A-1） ---- */
describe("起草台「采纳并归档」：确认覆盖的是对话框里看过的那一稿（复核五 W1-R5A-1）", () => {
  const casConflict = (current) => Object.assign(new Error("author draft has changed; refresh before saving"), {
    code: "AUTHOR_DRAFT_CONFLICT", status: 409, details: { current_revision_no: current },
  });

  beforeEach(() => {
    vi.resetModules();
    window.localStorage.clear();
    vi.spyOn(window, "alert").mockImplementation(() => {});
    vi.spyOn(console, "warn").mockImplementation(() => {});
  });
  afterEach(async () => {
    while (mountedRoots.length) {
      const { root, host } = mountedRoots.pop();
      await act(async () => root.unmount());
      host.remove();
    }
    vi.restoreAllMocks();
  });

  it("R5A-1 对话框给作者看的是 X 的差异，勾选确认、点「确认覆盖并归档」之前另一台设备存下了 Y：不备份、不发采纳请求，Y 不被换掉，对话框换成 Y 的差异、确认框复位", async () => {
    const X = "<p>作者亲写的开场 X：码头的灯还亮着。</p>";
    const Y = "<p>作者亲写的开场 X：码头的灯还亮着。另一台设备补上的一句 Y：她把船票撕了。</p>";
    const { mod, client } = await loadSceneRun({ projects: [NON_DEMO_PROJECT] });
    const server = { revision: 2, content: X, adopted: [] };
    const basePost = client.apiPost.getMockImplementation();
    client.apiPost.mockImplementation((url, body, options) => {
      if (/\/api\/v1\/author-drafts\/scene\/s1\/ensure$/.test(url)) {
        return Promise.resolve({ draft: { draft_id: "author_draft_scene_s1", revision_no: server.revision, content: server.content, last_promoted_revision_no: null, last_promoted_final_scene_row_id: null, canonical_dirty: true }, runtime_final_ref: null });
      }
      if (/\/api\/v1\/scenes\/s1\/adopt-current$/.test(url)) {
        const exact = body.exact_author_draft;
        if (Number(exact.base_revision_no) !== server.revision) return Promise.reject(casConflict(server.revision));
        server.adopted.push({ base: exact.base_revision_no, overwritten: server.content });
        server.revision += 1;
        server.content = exact.content;
        return Promise.resolve({
          scene_id: "s1", scene_status: "archived", final_scene_row_id: `f-${server.revision}`, content_hash: "h",
          author_draft: { draft_id: "author_draft_scene_s1", revision_no: server.revision, content: server.content, last_promoted_revision_no: server.revision, canonical_dirty: false },
        });
      }
      return basePost(url, body, options);
    });
    const cached = {
      ...mod.scnQC([{ id: "p1", text: "AI 写下了另一种开场。" }]),
      state: "ready", progress: 1, attempt: 1, attempts: [], cost: [], log: [],
    };
    mod.scnRunSave("ch01s1", cached);
    await queueSceneIntent({ sid: "ch01s1" });
    client.getLatestSceneRunJob.mockRejectedValue(Object.assign(new Error("not found"), { status: 404, code: "RUN_JOB_NOT_FOUND" }));
    const page = await import("./ws-scene.jsx");
    const view = await renderRunJobControl(page.WsScene, { go: vi.fn(), t: {} });

    await vi.waitFor(() => expect(view.host.querySelector('[data-testid="scene-archive"]')).toBeTruthy(), T);
    await click(view.host.querySelector('[data-testid="scene-archive"]'));
    await vi.waitFor(() => expect(document.body.querySelector(".scn2-adopt")).toBeTruthy(), T);
    const dialog = document.body.querySelector(".scn2-adopt");
    expect(dialog.querySelector(".scn2-adopt-diff").textContent).not.toContain("她把船票撕了");   // 对话框里是 X 的差异

    server.revision = 3;                                                     // 对话框开着：另一台设备存下了 Y
    server.content = Y;

    await act(async () => { dialog.querySelector(".scn2-adopt-confirm input").click(); });
    const overwrite = dialog.querySelector('[data-testid="scene-confirm-overwrite"]');
    await vi.waitFor(() => expect(overwrite.disabled).toBe(false), T);
    await click(overwrite);
    await vi.waitFor(() => expect(document.body.querySelector(".scn2-adopt-live").textContent).toContain("差异已按最新的一版重算"), T);
    await act(async () => { await new Promise((resolve) => setTimeout(resolve, 200)); });

    expect(client.apiPost.mock.calls.filter(([url]) => /adopt-current$/.test(url))).toEqual([]);
    expect(server).toMatchObject({ revision: 3, content: Y, adopted: [] });
    expect(window.WrRecovery.list().filter((entry) => entry.type === "backup")).toEqual([]);
    expect(document.body.querySelector(".scn2-adopt")).toBeTruthy();         // 对话框还开着，作者没被告知「已归档」
    // 对话框换成了 Y 的差异，确认框复位：作者得对着 Y 重新确认
    expect(document.body.querySelector(".scn2-adopt-diff").textContent).toContain("她把船票撕了");
    expect(document.body.querySelector(".scn2-adopt-confirm input").checked).toBe(false);
    expect(document.body.querySelector('[data-testid="scene-confirm-overwrite"]').disabled).toBe(true);
    expect(document.body.textContent).not.toContain("采用未完成");
  }, 40000);

  it("对话框开着时写作台这边读到了 Y：差异当场换成 Y 的、确认框复位；作者对着 Y 确认，覆盖的就是 Y（备份的也是 Y）", async () => {
    const X = "<p>作者亲写的开场 X：码头的灯还亮着。</p>";
    const Y = "<p>作者亲写的开场 X：码头的灯还亮着。另一台设备补上的一句 Y：她把船票撕了。</p>";
    const { mod, client } = await loadSceneRun({ projects: [NON_DEMO_PROJECT] });
    const server = { revision: 2, content: X, adopted: [] };
    const basePost = client.apiPost.getMockImplementation();
    client.apiPost.mockImplementation((url, body, options) => {
      if (/\/api\/v1\/author-drafts\/scene\/s1\/ensure$/.test(url)) {
        return Promise.resolve({ draft: { draft_id: "author_draft_scene_s1", revision_no: server.revision, content: server.content, last_promoted_revision_no: null, last_promoted_final_scene_row_id: null, canonical_dirty: true }, runtime_final_ref: null });
      }
      if (/\/api\/v1\/scenes\/s1\/adopt-current$/.test(url)) {
        const exact = body.exact_author_draft;
        if (Number(exact.base_revision_no) !== server.revision) return Promise.reject(casConflict(server.revision));
        server.adopted.push({ base: exact.base_revision_no, overwritten: server.content });
        server.revision += 1;
        server.content = exact.content;
        return Promise.resolve({
          scene_id: "s1", scene_status: "archived", final_scene_row_id: `f-${server.revision}`, content_hash: "h",
          author_draft: { draft_id: "author_draft_scene_s1", revision_no: server.revision, content: server.content, last_promoted_revision_no: server.revision, canonical_dirty: false },
        });
      }
      return basePost(url, body, options);
    });
    const cached = {
      ...mod.scnQC([{ id: "p1", text: "AI 写下了另一种开场。" }]),
      state: "ready", progress: 1, attempt: 1, attempts: [], cost: [], log: [],
    };
    mod.scnRunSave("ch01s1", cached);
    await queueSceneIntent({ sid: "ch01s1" });
    client.getLatestSceneRunJob.mockRejectedValue(Object.assign(new Error("not found"), { status: 404, code: "RUN_JOB_NOT_FOUND" }));
    const page = await import("./ws-scene.jsx");
    const view = await renderRunJobControl(page.WsScene, { go: vi.fn(), t: {} });

    await vi.waitFor(() => expect(view.host.querySelector('[data-testid="scene-archive"]')).toBeTruthy(), T);
    await click(view.host.querySelector('[data-testid="scene-archive"]'));
    await vi.waitFor(() => expect(document.body.querySelector(".scn2-adopt")).toBeTruthy(), T);
    const dialog = document.body.querySelector(".scn2-adopt");
    await act(async () => { dialog.querySelector(".scn2-adopt-confirm input").click(); });
    expect(dialog.querySelector(".scn2-adopt-confirm input").checked).toBe(true);

    server.revision = 3;                                                     // 另一台设备存下了 Y，写作台这边后台读到了它
    server.content = Y;
    await act(async () => { window.WrDocs.load("ch01s1"); });
    await vi.waitFor(() => expect(dialog.querySelector(".scn2-adopt-diff").textContent).toContain("她把船票撕了"), T);
    expect(dialog.querySelector(".scn2-adopt-confirm input").checked).toBe(false);
    expect(dialog.querySelector(".scn2-adopt-live").textContent).toContain("差异已按最新的一版重算");

    await act(async () => { dialog.querySelector(".scn2-adopt-confirm input").click(); });
    await click(dialog.querySelector('[data-testid="scene-confirm-overwrite"]'));
    await vi.waitFor(() => expect(server.adopted).toEqual([{ base: 3, overwritten: Y }]), T);
    const backups = window.WrRecovery.list().filter((entry) => entry.type === "backup");
    expect(backups.map((entry) => entry.html)).toEqual([Y]);
  }, 40000);
});
