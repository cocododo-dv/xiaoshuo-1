import React from "react";
import { SnowSync } from "./ws-snow-sync.jsx";
import { apiPost } from "./lib/client.js";
import { S2_BE_KEY, s2AdoptServerScaffold, s2SettlePlanning } from "./ws-snow-model.js";
import { activeWorkId } from "./ws-snow-hooks.js";
import { navigateWithViewIntent } from "./ws-view-intents.js";

/* ==========================================================
   雪花工作台 · AI 生成（从 ws-snow-hooks.js 拆出，2026-09-29）
   ----------------------------------------------------------
   整步生成 / 按方向生成 / 定向补全的通道（useSnowGeneration）、「先看 3 个方向」，以及四个 AI 请求
   （generate、fe-candidates、教练 assistant、场景分诊 suggest）共用的一份底稿 snowDraftOverride。
   ========================================================== */

const { useState } = React;

/* 四个 AI 请求的 draft_override 同一份：与上行 PATCH 同源的规范草稿（服务端镜像 ⊕ 本地脚手架，成员 id 齐全，
   服务端能按 id 对位合并）。以前教练与分诊用的是不带服务端镜像的 canonDraft，拿不到场景 id / 章归属 /
   canonFromFE 不产生的角色字段。doc = { drafts, scaffolds }；空草稿给 null。 */
export function snowDraftOverride(key, doc, workId) {
  try {
    const d = SnowSync.pushCanon(key, { drafts: (doc && doc.drafts) || {}, scaffolds: (doc && doc.scaffolds) || {} }, workId);
    return d && Object.keys(d).length ? d : null;
  } catch (e) { return null; }
}

/* AI 请求被服务端以「还没接好模型」拒绝时的回执（雪花的 AI 节点都 fail-closed：没有可用的模型就 409
   SNOWFLAKE_LLM_NOT_CONFIGURED，节点路由 / 提示词没配好是 SNOWFLAKE_LLM_ROUTE_OR_PROMPT_MISSING，前者带 details.author_action）：
   写全服务端的原话（以前截在 40 个字，正好把「去哪儿配」截掉），带一扇去「设置 → AI 模型」的门，按钮字取 author_action。
   其它失败只给原话。返回 UndoToast 的参数（text / tone / timeout / actionLabel / onAction）。 */
const LLM_SETUP_CODES = ["SNOWFLAKE_LLM_NOT_CONFIGURED", "SNOWFLAKE_LLM_ROUTE_OR_PROMPT_MISSING"];
export function snowAiFailureToast(lead, err) {
  const message = String((err && err.message) || "稍后重试");
  const action = (err && err.details && err.details.author_action) || null;
  const setup = LLM_SETUP_CODES.includes(err && err.code) || !!(action && action.target_view === "config");
  if (!setup) return { text: `${lead}：${message.slice(0, 60)}`, tone: "crimson", timeout: 6000 };
  return {
    text: `${lead}：${message}`, tone: "crimson", timeout: 12000,
    actionLabel: String((action && action.primary_button_label) || "去系统配置"),
    onAction: () => navigateWithViewIntent("settings", "ws:settings-tab", "ai"),
  };
}

/* ---- AI 生成：忙态 / 入口 / 错误按步骤隔离 ----
   全局布尔会让「生成中…」在所有步骤的按钮上亮起，并挡住其它步骤发起自己的生成；
   「生成中…」也只该亮在被点的那个入口上（{kind, turnId, index, focused, id}），其余按钮只禁用、不改文案。
   api 是工作台 API（ws-snow-workbench.jsx 的 useSnowWorkbenchApi：调用时读视图的最新值），
   setCoachHist 是教练的历史 setter（生成 / 方向回包带着整条教练历史）；异步回来后的写回一律用调用那一刻的步骤键。 */
export function useSnowGeneration(api, setCoachHist) {
  const [structBusyMap, setStructBusyMap] = useState({});
  const [genTargetMap, setGenTargetMap] = useState({});
  const [genErrMap, setGenErrMap] = useState({});   // 本步最近一次 AI 动作的错误（编辑页 AI 工具条显示）
  const [dirBusyMap, setDirBusyMap] = useState({});  // 「先看 3 个方向」进行中

  /* 结构化生成通用通道：按方向生成 / AI 生成本步 / 整表生成 / 全部补全 / 单场补全共用——
     后端 generate（每步专用模板 + 权威上游材料 + 压力诊断 + 本步要点 + 空字段定向重试；require_llm 保证
     LLM 不可用时诚实报错，绝不落一版启发式草稿冒充）→ 整步规范草稿经 applyServerStep 反推回脚手架。
     focusRow 时只回写焦点场的规划（其余场保留本地态，防止未上行编辑被服务端旧值盖掉）。
     本步要点默认带入（服务端 use_direction_brief 缺省 true）：不想让某条要点约束生成，撤下那条即可。 */
  const structuredGenerate = async ({ direction = null, directionKind = null, directionTurnId = null, directionIndex = null, focus = null, focusRow = null, focusChars = null, focusChar = null, source = null, target = null, histAction, histNote, doneAction, doneNote, toastOk, toastFail, switchTab = false }) => {
    const { key, step } = api.current();
    if (structBusyMap[key]) return false;
    setGenErrMap(prev => ({ ...prev, [key]: null }));
    setStructBusyMap(prev => ({ ...prev, [key]: true }));
    setGenTargetMap(prev => ({ ...prev, [key]: target || { kind: source || "generate" } }));
    api.journal(histAction, `${step.num} ${step.name}${histNote ? " · " + histNote : ""} · 生成前留底`, "我", api.snapshot(key), key);
    try {
      const workId = activeWorkId();
      const beKey = S2_BE_KEY[key];
      if (!workId || !beKey) throw new Error("作品尚未就绪，稍后重试");
      const body = { require_llm: true, source: source || (focus ? "fe_scene_focus_ai" : focusChars ? "fe_char_focus_ai" : (direction ? "fe_candidate_adopt" : "fe_scaffold_ai")) };
      if (direction) body.direction_text = direction;
      if (direction && directionKind) body.direction_kind = directionKind; // 教练回复 vs 方向正文：用法说明不同
      // 阶段 U：方向来自教练日志里的哪一回合——服务端在回合上记「已按此生成」、在 health.direction 记出处
      if (direction && directionTurnId) {
        body.direction_turn_id = directionTurnId;
        if (directionIndex != null) body.direction_index = directionIndex;
      }
      if (focus) body.focus_scene_refs = focus;
      if (focusChars) body.focus_character_refs = focusChars;
      /* 本地最新规范草稿随请求带入（与上行 PATCH 同源）：消除「刚加的角色/场
         还没自动保存上行，模型看不到、合并后被丢掉」的竞态 */
      const dOv = snowDraftOverride(key, api.doc(), workId);
      if (dOv) body.draft_override = dOv;
      const res = await apiPost(`/api/v2/projects/${workId}/snowflake-workspace/steps/${beKey}/generate`, body);
      if (!res || !res.step) throw new Error("生成回包缺少 step");
      try { if (res.workspace) SnowSync.captureBriefs(workId, res.workspace); } catch (ignored) {}
      // 回包的教练历史带「已按此生成」标记（adoption）——方向卡 / 回复上的徽章据此更新
      if (res.workspace && Array.isArray(res.workspace.assistant_history)) setCoachHist(res.workspace.assistant_history);
      let fe = null;
      try { fe = SnowSync.applyServerStep(workId, key, res.step); } catch (ignored) {}
      const { setScaffolds, setDrafts } = api;
      if (fe && fe.scaffold) {
        if (focusRow && key === "planning") {
          const fePlans = (fe.scaffold || {}).plans || {};
          setScaffolds(prev => {
            const cur = prev[key] || {};
            return s2SettlePlanning({ ...prev, [key]: { ...cur, sel: focusRow, plans: { ...(cur.plans || {}), [focusRow]: fePlans[focusRow] || (cur.plans || {})[focusRow] || {} } } });
          });
        } else if (focusChar && (key === "characters" || key === "backstory" || key === "profile")) {
          /* 单角色定向：只把焦点角色的生成结果并回本地，其余角色保持本地态
             （与 planning 的 focusRow 同一防线：未上行编辑不被服务端旧值盖掉） */
          const feChars = (fe.scaffold || {}).chars || {};
          setScaffolds(prev => {
            const cur = prev[key] || {};
            return { ...prev, [key]: { ...cur, sel: focusChar, chars: { ...(cur.chars || {}), [focusChar]: feChars[focusChar] || (cur.chars || {})[focusChar] || {} } } };
          });
        } else {
          // 整步替换：只活在前端的线索 / 错误信念接到新稿上，plan 的形态 / 视角摘掉（F02-01 / 02）
          setScaffolds(prev => s2AdoptServerScaffold(prev, key, fe.scaffold));
        }
        setDrafts(prev => ({ ...prev, [key]: "" })); // 脚手架即唯一内容源，避免旧自由草稿盖住它
      } else if (fe && fe.text != null) {
        setDrafts(prev => ({ ...prev, [key]: fe.text }));
      }
      if (switchTab) api.showTab(key, "edit");
      /* 分批深化中途失败等半成品：后端把事实放在 health.generation_notice，
         这里必须把绿色的「已生成」降级成警告——否则作者以为整表都做完了 */
      const notice = ((res.step || {}).health || {}).generation_notice;
      const noticeMsg = notice && String(notice.message || "").trim();
      api.journal(doneAction || histAction,
        `${step.num} ${step.name}${doneNote ? " · " + doneNote : ""}${noticeMsg ? " · " + noticeMsg : ""}`, "AI", null, key);
      if (noticeMsg) {
        setGenErrMap(prev => ({ ...prev, [key]: noticeMsg }));
        api.toast(noticeMsg.slice(0, 60), "crimson");
      } else {
        api.toast(toastOk || "已生成 · 可回滚", "gold");
      }
      return true;
    } catch (err) {
      setGenErrMap(prev => ({ ...prev, [key]: (err && err.message) || "生成失败，请稍后重试" }));
      api.toast(toastFail || ("生成失败：" + ((err && err.message) || "稍后重试").slice(0, 40)), "crimson");
      return false;
    } finally {
      setStructBusyMap(prev => ({ ...prev, [key]: false }));
      setGenTargetMap(prev => ({ ...prev, [key]: null }));
    }
  };

  /* 「先看 3 个方向」（阶段 U）：走后端节点 snowflake_step_candidates，结果是教练日志里的一种回合
     （turn_kind=candidates），回包带整条教练历史；底稿与整步生成同源（draft_override），作者在输入框里
     写的要求作为 ask 一并带上；第 10 步只针对选中的那一场。进行中就切到教练页（方向卡出现在那里）；
     fail-closed：LLM 不可用 → 后端 409，按步记错误并提示。 */
  const requestDirections = async (ask = "") => {
    const { key, step, data } = api.current();
    if (dirBusyMap[key]) return false;
    setGenErrMap(prev => ({ ...prev, [key]: null }));
    setDirBusyMap(prev => ({ ...prev, [key]: true }));
    api.showTab(key, "coach");
    try {
      const doc = api.doc();
      const focusRow = key === "planning" ? ((doc.scaffolds.planning || {}).sel || "") : "";
      const workId = activeWorkId();
      const beKey = S2_BE_KEY[key];
      if (!workId || !beKey) throw new Error("作品尚未就绪，稍后重试");
      const body = { target_chars: data.target || 120 };
      const askText = String(ask || "").trim();
      if (askText) body.ask = askText;
      if (key === "planning" && focusRow) body.focus_scene_id = focusRow;
      const dOv = snowDraftOverride(key, doc, workId);
      if (dOv) body.draft_override = dOv;
      const res = await apiPost(`/api/v2/projects/${workId}/snowflake-workspace/steps/${beKey}/fe-candidates`, body);
      if (!res || !res.turn_id) throw new Error("方向回包缺少回合");
      if (Array.isArray(res.assistant_history)) setCoachHist(res.assistant_history);
      const n = (res.candidates || []).length;
      api.journal(`教练给了 ${n} 个方向`, `${step.num} ${step.name}${focusRow ? " · 聚焦 " + api.sceneLabel(focusRow) : ""}`, "AI", null, key);
      api.toast(`教练给了 ${n} 个方向 · 选一个「按此生成本步」`, "gold");
      return true;
    } catch (err) {
      const msg = (err && err.message) || "方向生成失败，请稍后重试";
      setGenErrMap(prev => ({ ...prev, [key]: msg }));
      api.toast(msg.slice(0, 60), "crimson");
      return false;
    } finally {
      setDirBusyMap(prev => ({ ...prev, [key]: false }));
    }
  };

  const clearGenErr = (key) => setGenErrMap(prev => ({ ...prev, [key]: null }));
  return { structBusyMap, genTargetMap, genErrMap, dirBusyMap, clearGenErr, structuredGenerate, requestDirections };
}
