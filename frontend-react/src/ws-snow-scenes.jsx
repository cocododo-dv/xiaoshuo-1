import React from "react";
import { I } from "./icons.jsx";
import { apiPost } from "./lib/client.js";
import { SnowSync } from "./ws-snow-sync.jsx";
import { activeWorkId, useSnowEvents } from "./ws-snow-hooks.js";
import {
  S2_PLAN_FIELDS, S2_TRIAGE_LABEL, s2BusyOn,
} from "./ws-snow-model.js";
export { S2SceneList } from "./ws-snow-scene-list.jsx";
export { S2ScenePlan } from "./ws-snow-scene-plan.jsx";

/* ==========================================================
   09 场景列表 · 10 场景规划
   ----------------------------------------------------------
   把雪花的分形原则落到底层：主线与支线在 09 编织；反应场是少数，只在下一目标不明显时才写
   （阶段 B：不再把机械交替当节奏标准）。10 逐场画草图：主动 目标 / 冲突 / 挫败，反应 反应 / 两难 / 决定。
   两张表各有自己的文件（ws-snow-scene-list.jsx / ws-snow-scene-plan.jsx）；这里留下只属于这两步的
   另外两样东西：AI 工具条上的整表动作（09 整表生成、10 全部补全 + 分诊），以及分诊结果与作者裁定的
   状态（useSnowTriage）。
   ========================================================== */

const { useState: useSS, useEffect: useSE, useRef: useSR } = React;

/* 09 / 10 的整表动作住在编辑页 AI 工具条的主位上（阶段 U 说好的「每一步统一的 AI 工具条」）：
   09「AI 生成整表」；10「AI 补全所有场景」+「AI 分诊」+ 分诊计数。逐成员的「这一场」仍在成员旁边。
   会整体替换已有内容的动作先问一句（生成前留底，可在「历史」里回滚）。
   emphasize：这一步还空着时整表动作才是这一页的实心主按钮；写了内容之后主按钮是页脚的「确认本步」。 */
export function S2SceneAiActions({ step, ai, sceneRows, plans, emphasize = false }) {
  const tone = emphasize ? "btn-accent" : "btn-ghost";
  if (step === "scenes") {
    const generateTable = () => {
      if (sceneRows.some(s => (s.event || s.crucible || "").trim())
        && !window.confirm(`AI 会依上游材料重新生成整份场景表，现有 ${sceneRows.length} 场将被整体替换（生成前留底，可在「历史」里回滚）。继续？`)) return;
      ai.onGenerateAll();
    };
    return (
      <button className={`btn btn-sm ${tone}`} data-testid="snow-ai-generate-table" disabled={ai.structBusy} onClick={generateTable}
        title="让 AI 依已确认的大纲、角色与道德前提生成整份场景表（生成前留底，可回滚）">
        {s2BusyOn(ai, "scenes_all") ? <I.Refresh size={13} className="sf-spin" /> : <I.Wand size={13} />} {s2BusyOn(ai, "scenes_all") ? "生成中…" : "AI 生成整表"}
      </button>
    );
  }
  if (step !== "planning") return null;
  const fillAllScenes = () => {
    const hasPlans = Object.values(plans || {}).some(p => p && S2_PLAN_FIELDS.some(f => (p[f] || "").trim()));
    if (hasPlans && !window.confirm("AI 会逐场补齐三拍（主动：目标 / 冲突 / 挫败；反应：反应 / 两难 / 决定）、坩埚与钩子，已填的内容会被深化改写（生成前留底，可在「历史」里回滚）。继续？")) return;
    ai.onFillAll();
  };
  const triage = ai.triage;
  const triItems = (triage && triage.items) || null;
  const triCount = (st) => (triItems ? sceneRows.filter(s => ((triItems[s.id] || {}).status) === st).length : 0);
  return (
    <React.Fragment>
      <button className={`btn btn-sm ${tone}`} data-testid="snow-ai-fill-all" disabled={ai.structBusy || !sceneRows.length} onClick={fillAllScenes}
        title="让 AI 依上游材料逐场补齐目标 / 冲突 / 挫败（或反应 / 两难 / 决定）">
        {s2BusyOn(ai, "fill_all") ? <I.Refresh size={13} className="sf-spin" /> : <I.Wand size={13} />} {s2BusyOn(ai, "fill_all") ? "生成中…" : "AI 补全所有场景"}
      </button>
      <button className="btn btn-quiet btn-sm" data-testid="snow-ai-triage" disabled={ai.triageBusy || !sceneRows.length} onClick={ai.onTriage}
        title="逐场评估压力结构：可通过 / 需修补 / 该重写，并给出修复步骤与补丁">
        <I.Activity size={13} className={ai.triageBusy ? "sf-spin" : ""} /> {ai.triageBusy ? "分诊中…" : "AI 分诊"}
      </button>
      {triItems && (
        <span className="sf-triage-sum" title={`分诊于 ${new Date(triage.at).toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit" })}${triage.source === "llm" ? " · AI 评估" : ""}`}>
          <span className="tri-pass">{triCount("pass")} 过</span>
          <span className="tri-maybe">{triCount("maybe")} 修</span>
          <span className="tri-rewrite">{triCount("rewrite")} 重写</span>
          {triCount("cut") > 0 && <span className="tri-cut">{triCount("cut")} 待删</span>}
        </span>
      )}
    </React.Fragment>
  );
}

/* 场景分诊（第 10 步）：后端逐场评估 pass/maybe/rewrite + 修复建议/补丁。
   draft_override 带本地最新折叠草稿，免受自动保存节流竞态影响。分诊结果随手存档（save_scene_triage）；
   会话内记住 triage_id，复诊时原行更新而不是堆新行。作者的裁定（pass / maybe / rewrite / cut）本地即时更新、
   服务端存档，失败回滚并提示。env 同 useSnowGeneration（调用时读最新值）。 */
export function useSnowTriage(env) {
  const [triage, setTriage] = useSS(null);   // { items: rowUid -> item, at, source }
  const [triageBusy, setTriageBusy] = useSS(false);
  const triageIdsRef = useSR({});            // scene_plan_id -> triage_id（会话内复用）

  // 阶段 M：分诊结果随工作台水合——刷新后第 10 步仍能看到上次存档的分诊；本会话新跑的分诊优先
  const restore = () => {
    if (triage) return;
    try {
      const saved = SnowSync.triageItems();
      if (saved && saved.items && Object.keys(saved.items).length) setTriage(saved);
    } catch (e) {}
  };
  useSE(() => { restore(); }, [triage]);
  useSnowEvents({ "ws:snow-hydrated": restore });

  const runTriage = async () => {
    if (triageBusy) return;
    const { activeKey: key, drafts, scaffolds } = env.current;
    setTriageBusy(true);
    try {
      const workId = activeWorkId();
      if (!workId) throw new Error("作品尚未就绪");
      let draftOverride = null;
      try { draftOverride = SnowSync.canonDraft("planning", { drafts, scaffolds }); } catch (e) {}
      const res = await apiPost(`/api/v2/projects/${workId}/snowflake-workspace/scene-triage/suggest`,
        draftOverride && (draftOverride.scenes || []).length ? { draft_override: draftOverride } : {});
      const byRow = {};
      (res && res.items || []).forEach(it => { const k = it.row_uid || it.scene_id; if (k) byRow[k] = it; });
      setTriage({ items: byRow, at: Date.now(), source: (res && res.source) || "fallback" });
      env.current.pushHist("场景分诊", `10 场景规划 · ${Object.keys(byRow).length} 场`, res && res.source === "llm" ? "AI" : "规则", null, key);
      // 存档为推荐态（不写人工裁定），让「重写场挡物化」的闸门真实生效
      try {
        const saved = await apiPost(`/api/v2/projects/${workId}/snowflake-workspace/scene-triage`, {
          items: (res && res.items || []).map(it => ({
            triage_id: triageIdsRef.current[it.scene_plan_id] || "",
            scene_plan_id: it.scene_plan_id, scene_id: it.scene_id,
            recommended_status: it.status, score: it.score,
            missing_fields: it.missing_fields, fix_steps: it.fix_steps,
            repair_patch: it.repair_patch, notes: it.notes,
          })),
        });
        (saved && saved.items || []).forEach(it => { if (it.scene_plan_id && it.triage_id) triageIdsRef.current[it.scene_plan_id] = it.triage_id; });
        try { SnowSync.refetch(workId); } catch (e2) {}
      } catch (e2) { /* 存档失败不打断分诊展示；下次分诊重试 */ }
      env.current.showToast(res && res.source === "llm" ? "分诊完成 · AI 评估每场压力 · 已存档" : "分诊完成 · 规则诊断（启用 LLM 可得更深评估）· 已存档", "gold");
    } catch (err) {
      env.current.showToast("分诊失败：" + ((err && err.message) || "稍后重试").slice(0, 40), "crimson");
    } finally {
      setTriageBusy(false);
    }
  };

  /* 一键应用分诊修复补丁：GCS/RDD 字段进 10 的 plans，坩埚/摘要/地点回写 09 的场景行 */
  const applyTriageRepair = (rowUid, item) => {
    const patch = (item && item.repair_patch) || {};
    if (!Object.keys(patch).length) return;
    const e = env.current;
    e.pushHist("应用修复补丁", `10 场景规划 · ${e.sceneLabel(rowUid)} 修复前留底`, "我", e.snapNow("planning"), e.activeKey);
    const planKeys = ["goal", "conflict", "setback", "reaction", "dilemma", "decision", "cost_requirement"];
    e.setScaffolds(prev => {
      const cur = prev.planning || {};
      const plan = { ...((cur.plans || {})[rowUid] || {}) };
      planKeys.forEach(k => { if (patch[k]) plan[k] = patch[k]; });
      const next = { ...prev, planning: { ...cur, sel: rowUid, plans: { ...(cur.plans || {}), [rowUid]: plan } } };
      const cru = patch.scene_crucible || patch.crucible;
      if (cru || patch.summary || patch.location) {
        const sc = prev.scenes || {};
        next.scenes = { ...sc, list: (sc.list || []).map(s => s.id !== rowUid ? s : {
          ...s, crucible: cru || s.crucible, event: patch.summary || s.event, place: patch.location || s.place,
        }) };
      }
      return next;
    });
    e.showToast(`已应用修复补丁 · ${e.sceneLabel(rowUid)} · 可回滚`, "gold");
  };

  /* 阶段 R：作者对某一场的裁定（pass / maybe / rewrite / cut）——本地即时更新，服务端存档（失败回滚并提示） */
  const setTriageVerdict = async (rowUid, status) => {
    const key = env.current.activeKey;
    const prevTriage = triage;
    const cur = (triage && triage.items && triage.items[rowUid]) || {};
    const next = { ...(triage || { at: Date.now(), source: "author" }), items: { ...((triage && triage.items) || {}), [rowUid]: { ...cur, status, manual: true } } };
    setTriage(next);
    try {
      const workId = activeWorkId();
      if (!workId) throw new Error("作品尚未就绪");
      const saved = await SnowSync.saveTriageVerdict(workId, { ...cur, row_uid: rowUid, status });
      if (saved && saved.triage_id) {
        if (saved.scene_plan_id) triageIdsRef.current[saved.scene_plan_id] = saved.triage_id;
        setTriage(t => t ? { ...t, items: { ...t.items, [rowUid]: { ...(t.items[rowUid] || {}), triage_id: saved.triage_id, scene_plan_id: saved.scene_plan_id || (t.items[rowUid] || {}).scene_plan_id, recommended_status: saved.recommended_status || (t.items[rowUid] || {}).recommended_status } } } : t);
      }
      const no = env.current.sceneLabel(rowUid);
      env.current.pushHist("分诊裁定", `10 场景规划 · ${no}：${S2_TRIAGE_LABEL[status] || status}`, "我", null, key);
      env.current.showToast(status === "cut" ? `${no} 已标待删：整理时不建卡，三拍留在构思里` : status === "rewrite" ? `${no} 标为该重写：整理时先不建卡` : `${no} 裁定为${S2_TRIAGE_LABEL[status] || status}`, "gold");
    } catch (err) {
      setTriage(prevTriage);
      env.current.showToast("裁定未保存：" + ((err && err.message) || "稍后重试").slice(0, 40), "crimson");
    }
  };

  return { triage, triageBusy, runTriage, applyTriageRepair, setTriageVerdict };
}
