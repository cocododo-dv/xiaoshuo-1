import React from "react";
import { I } from "./icons.jsx";
import { apiGet, apiPost } from "./lib/client.js";
import { modEnterShortcut } from "./lib/platform.js";
import { isImeComposing } from "./ws-dialog.jsx";
import { SnowSync } from "./ws-snow-sync.jsx";
import { activeWorkId } from "./ws-snow-hooks.js";
import { CoachInline, CoachReply } from "./ws-snow-reply.jsx";
import { BRIEF_KIND_LABEL, BRIEF_KIND_ORDER, S2_BE_KEY, s2BriefDeltaParts, s2Provenance } from "./ws-snow-model.js";

/* ==========================================================
   教练 · 要点 · 方向 · 生成（阶段 T / U）
   ----------------------------------------------------------
   作者面对的是四个名词一个动词：要点（持久的意图）、方向（一次生成的蓝本：「先看 3 个方向」的一张卡
   或一段教练回复）、改写（教练的 candidate_patch，「填入本步」）、生成。
   这里有编辑页顶部的 AI 工具条、教练页（要点卡 + 教练日志 + 方向卡 + 输入框），以及教练的状态钩子。
   右栏的要点只读镜像在 ws-snow-rail.jsx。
   ========================================================== */

const { useState: useSS, useEffect: useSE, useRef: useSR } = React;

/* 驻场教练（snowflake_workspace_assistant）：逐步对话辅导，回合服务端持久化。
   第 10 步自动聚焦当前选中场（row_uid，后端已兼容）；带 draft_override 免竞态。
   candidate_patch 是教练的「改写」：应用时空值不清空、按 id 对位、不删成员。
   env 同 useSnowGeneration（调用时读最新值）；tab 是当前步骤的页签。 */
export function useSnowCoach(env, tab) {
  /* 驻场教练日志（后端 assistant_history，全步骤，服务端持久化；阶段 U 起方向回合也在里面） */
  const [coachHist, setCoachHist] = useSS([]);
  const [coachBusy, setCoachBusy] = useSS(false);
  const [briefBusy, setBriefBusy] = useSS(false);

  /* 进教练页且本地还没有历史 → 从 workspace 懒加载（跨会话回合可见） */
  useSE(() => {
    if (tab !== "coach" || coachHist.length) return;
    (async () => {
      try {
        const workId = activeWorkId();
        if (!workId) return;
        const ws = await apiGet(`/api/v2/projects/${workId}/snowflake-workspace`);
        // 只在本地仍为空时采用：「先看 3 个方向」会先切到教练页再收到更新的历史，懒加载的旧回包不能把它盖掉
        if (ws && Array.isArray(ws.assistant_history) && ws.assistant_history.length) setCoachHist(prev => (prev.length ? prev : ws.assistant_history));
      } catch (e) {}
    })();
  }, [tab]);

  const sendCoach = async (message) => {
    const msg = String(message || "").trim();
    if (coachBusy || !msg) return;
    setCoachBusy(true);
    const { activeKey: key, active: step, drafts, scaffolds } = env.current;
    try {
      const workId = activeWorkId();
      const beKey = S2_BE_KEY[key];
      if (!workId || !beKey) throw new Error("作品尚未就绪，稍后重试");
      const body = { step_key: beKey, message: msg };
      let dOv = null;
      try { dOv = SnowSync.canonDraft(key, { drafts, scaffolds }); } catch (e) {}
      if (dOv && Object.keys(dOv).length) body.draft_override = dOv;
      const focusRow = key === "planning" ? ((scaffolds.planning || {}).sel || "") : "";
      if (focusRow) body.focus_scene_id = focusRow;
      const res = await apiPost(`/api/v2/projects/${workId}/snowflake-workspace/assistant`, body);
      setCoachHist((res && res.assistant_history) || []);
      // 阶段 T：教练每轮重述作者意图要点——回包带本步最新要点，落镜像；差异随回合落表，日志里那一轮自己会说
      if (res && res.direction_brief) { try { SnowSync.setDirectionBrief(workId, key, res.direction_brief); } catch (e) {} }
      const parts = s2BriefDeltaParts(res && res.brief_delta);
      env.current.pushHist("教练问答", `${step.num} ${step.name}${body.focus_scene_id ? " · 聚焦 " + env.current.sceneLabel(body.focus_scene_id) : ""}${parts.length ? " · 要点 " + parts.join(" / ") : ""}`, "AI", null, key);
    } catch (err) {
      env.current.showToast("教练回复失败：" + ((err && err.message) || "稍后重试").slice(0, 40), "crimson");
    } finally {
      setCoachBusy(false);
    }
  };

  /* 填入教练的改写（当前步任意带改写的回合）：咨询式合并——空值不清空、按 id 对位、不删成员 */
  const applyCoachPatch = (turn) => {
    const patch = turn && turn.candidate_patch;
    if (!patch || !Object.keys(patch).length) return;
    const e = env.current;
    const key = e.activeKey;
    e.pushHist("填入教练改写", `${e.active.num} ${e.active.name} · 填入前留底`, "我", e.snapNow(key), key);
    let fe = null;
    try { fe = SnowSync.applyCanonPatch(key, { drafts: e.drafts, scaffolds: e.scaffolds }, patch, null); } catch (err) {}
    if (fe && fe.scaffold) e.setScaffolds(prev => ({ ...prev, [key]: fe.scaffold }));
    else if (fe && fe.text != null) e.setDrafts(prev => ({ ...prev, [key]: fe.text }));
    e.setTabFor(key, "edit");
    e.showToast(`已填入「${(turn && turn.candidate_label) || "教练改写"}」· 可回滚`, "gold");
  };

  /* 阶段 T：作者编辑本步要点（撤下 / 改写 / 加条 / 范围 / 恢复 / 继承）——乐观写入，失败由 store 回滚并上抛 */
  const saveBrief = async (lines, inherit) => {
    const workId = activeWorkId();
    if (!workId) return;
    const key = env.current.activeKey;
    setBriefBusy(true);
    try {
      await SnowSync.saveDirectionBrief(workId, key, { lines, inherit_upstream: inherit });
    } catch (err) {
      env.current.showToast("要点未保存：" + ((err && err.message) || "稍后重试").slice(0, 40), "crimson");
    } finally {
      setBriefBusy(false);
    }
  };

  return { coachHist, setCoachHist, coachBusy, sendCoach, applyCoachPatch, briefBusy, saveBrief };
}

/* 编辑页顶部的 AI 工具条（阶段 U）：每一步都有同一组入口，排在同一个位置——
   整步动作（01–08「AI 生成本步」；09「AI 生成整表」；10「AI 补全所有场景」+「AI 分诊」，由 primary 传入）、
   「先看 3 个方向」（去教练页看方向卡）、要点条数（点去教练页编辑）；下面一行说本步当前版本是怎么来的
   （按方向「X」/ 教练回复 / AI 生成 · 带第 N 版要点），要点改过而本稿没跟上时给「按最新要点重新生成」。 */
export function S2AiBar({ stepName, canGenerate, emphasize = false, primary, structBusy, busyTarget, dirBusy, onGenerate, onDirections, brief, usage, health, onOpenCoach, onRegenWithBrief, err, onClearErr }) {
  const active = ((brief && brief.lines) || []).filter(l => l && l.status === "active").length;
  const genBusy = !!(structBusy && busyTarget && busyTarget.kind === "bar");
  const inherited = (brief && brief.inherit_upstream !== false && Array.isArray(brief.inherited)) ? brief.inherited.length : 0;
  const provenance = s2Provenance(health);
  return (
    <div className="sf-aibar" data-testid="snow-aibar" role="group" aria-label="AI 工具">
      <div className="sf-aibar-row">
        {canGenerate && (
          <button className={`btn btn-sm ${emphasize ? "btn-accent" : "btn-ghost"}`} disabled={structBusy} onClick={onGenerate} data-testid="snow-ai-generate"
            title={`让 AI 按上游材料和本步要点整步写好「${stepName}」（生成前留底，可回滚）`}>
            {genBusy ? <I.Refresh size={13} className="sf-spin" /> : <I.Wand size={13} />} {genBusy ? "生成中…" : "AI 生成本步"}
          </button>
        )}
        {primary}
        <button className="btn btn-quiet btn-sm" disabled={dirBusy} onClick={onDirections} data-testid="snow-ai-directions"
          title="让教练先给三个不同方向（出现在教练页），挑一个再生成">
          {dirBusy ? <I.Refresh size={13} className="sf-spin" /> : <I.Compass size={13} />} {dirBusy ? "想方向中…" : "先看 3 个方向"}
        </button>
        <button className="sf-aibar-brief" onClick={onOpenCoach} data-testid="snow-ai-brief" title="本步要点：你定下的、否决的、约束的——AI 生成、方向、分诊都照它写；点这里去教练页编辑">
          <I.Sparkles size={12} /> 要点 <b>{active}</b> 条{inherited ? <span className="sf-aibar-inh">，继承 {inherited}</span> : null}
        </button>
      </div>
      {(provenance || (usage && usage.stale)) && (
        <div className={`sf-aibar-prov ${usage && usage.stale ? "is-stale" : ""}`} data-testid="snow-ai-provenance">
          {provenance && <span>本稿：{provenance}</span>}
          {usage && usage.stale && (
            <React.Fragment>
              <span>要点已改到第 {usage.currentRevision} 版，本稿还是按第 {usage.usedRevision} 版写的。</span>
              <button className="btn btn-quiet btn-sm" disabled={structBusy} onClick={onRegenWithBrief} data-testid="snow-ai-regen-brief"><I.Wand size={12} /> 按最新要点重新生成</button>
            </React.Fragment>
          )}
        </div>
      )}
      {err && (
        <div className="sf-cand-err" role="alert" data-testid="snow-ai-error">
          <I.AlertTriangle size={13} /><span>{err}</span>
          <button className="btn btn-quiet btn-sm" onClick={onClearErr}>知道了</button>
        </div>
      )}
    </div>
  );
}

/* ====== 阶段 T / U：本步要点（作者意图要点）卡 ======
   教练每轮蒸馏、作者定夺：撤下 / 改写 / 切换类型与范围 / 加条 / 恢复；「继承上游」决定上游全书级要点是否带入本步；
   要点改过而本稿没跟上时给「按最新要点重新生成」。要点永远带入生成（阶段 U 去掉了「生成时带入」开关：
   不想让某条约束生成，撤下那条即可）。 */
function S2BriefCard({ brief, busy, onSave, usage, onRegen, structBusy }) {
  const [editing, setEditing] = useSS(null);
  const [adding, setAdding] = useSS({ kind: "decision", scope: "step", text: "" });
  const [showDismissed, setShowDismissed] = useSS(false);
  const lines = (brief && Array.isArray(brief.lines)) ? brief.lines : [];
  const active = lines.filter(l => l && l.status === "active");
  const dismissed = lines.filter(l => l && l.status === "dismissed");
  const inherited = (brief && Array.isArray(brief.inherited)) ? brief.inherited : [];
  const inherit = !brief || brief.inherit_upstream !== false;
  const visible = () => active.map(l => ({ line_id: l.line_id, kind: l.kind, scope: l.scope, text: l.text, status: "active" }));
  const save = (nextLines, nextInherit) => onSave(nextLines, nextInherit);
  const dismiss = (id) => save(visible().filter(l => l.line_id !== id));
  const restore = (line) => save([...visible(), { line_id: line.line_id, kind: line.kind, scope: line.scope, text: line.text, status: "active" }]);
  const toggleScope = (line) => save(visible().map(l => l.line_id === line.line_id ? { ...l, scope: l.scope === "book" ? "step" : "book" } : l));
  const cycleKind = (line) => {
    const kind = BRIEF_KIND_ORDER[(BRIEF_KIND_ORDER.indexOf(line.kind) + 1) % BRIEF_KIND_ORDER.length];
    save(visible().map(l => l.line_id === line.line_id ? { ...l, kind } : l));
  };
  const commitEdit = () => {
    if (!editing) return;
    const text = editing.text.trim();
    const next = text ? visible().map(l => l.line_id === editing.line_id ? { ...l, text } : l) : visible().filter(l => l.line_id !== editing.line_id);
    setEditing(null);
    save(next);
  };
  const add = () => {
    const text = adding.text.trim();
    if (!text) return;
    save([...visible(), { kind: adding.kind, scope: adding.scope, text, status: "active" }]);
    setAdding({ ...adding, text: "" });
  };
  const clearAll = () => {
    if (!active.length) return;
    if (!window.confirm("撤下本步全部要点？（可在「已撤」里恢复）")) return;
    save([]);
  };
  const hasAnything = active.length || dismissed.length || inherited.length;
  return (
    <div className={`sf-brief ${busy ? "is-busy" : ""}`} data-testid="snow-brief-card">
      <div className="sf-brief-head">
        <div className="sf-brief-title">
          <I.Sparkles size={14} /> 本步要点 <span className="sf-brief-count">{active.length}</span>
          <span className="sf-brief-hint">你定下的、否决的、还在犹豫的——教练记，你改；AI 生成、方向、分诊都照它写</span>
        </div>
        <div className="sf-brief-tools">
          <label className="sf-brief-toggle" title="把上游各步标为「全书」的要点一并带入本步">
            <input type="checkbox" checked={inherit} disabled={busy} onChange={(e) => save(undefined, e.target.checked)} data-testid="snow-brief-inherit" /> 继承上游
          </label>
          {active.length > 0 && <button className="btn btn-quiet btn-sm" disabled={busy} onClick={clearAll} data-testid="snow-brief-clear">清空</button>}
        </div>
      </div>
      {usage && usage.stale && (
        <div className="sf-brief-stale" data-testid="snow-brief-stale">
          <span>要点改过了（第 {usage.currentRevision} 版），本步草稿还是按第 {usage.usedRevision} 版写的。</span>
          <button className="btn btn-accent btn-sm" disabled={structBusy} onClick={onRegen} data-testid="snow-brief-regen"><I.Wand size={12} /> 按最新要点重新生成</button>
        </div>
      )}
      {!hasAnything && <div className="sf-brief-empty">还没有要点。和教练聊几句，它会把你定下的、否决的、还在犹豫的记在这里；也可以直接在下面加一条。</div>}
      {active.length > 0 && (
        <ul className="sf-brief-list" data-testid="snow-brief-lines">
          {active.map(l => (
            <li key={l.line_id} className={`sf-brief-line is-${l.kind}`} data-testid="snow-brief-line">
              <button className="sf-brief-kind" title="点击切换类型（决定 → 约束 → 否决 → 待定）" disabled={busy} onClick={() => cycleKind(l)}>{BRIEF_KIND_LABEL[l.kind] || l.kind}</button>
              {editing && editing.line_id === l.line_id
                ? <input className="sf-brief-edit" autoFocus value={editing.text} data-testid="snow-brief-edit" aria-label="改写这一条要点"
                    onChange={(e) => setEditing({ ...editing, text: e.target.value })} onBlur={commitEdit}
                    onKeyDown={(e) => {
                      if (isImeComposing(e)) return;
                      if (e.key === "Enter") { e.preventDefault(); commitEdit(); }
                      if (e.key === "Escape") { e.preventDefault(); e.stopPropagation(); setEditing(null); }
                    }} />
                : <button type="button" className="sf-brief-text" title="点一下改写这一条" disabled={busy} onClick={() => setEditing({ line_id: l.line_id, text: l.text })}>{l.text}</button>}
              <button className={`sf-brief-scope ${l.scope === "book" ? "is-book" : ""}`} title="本步 / 全书：全书级要点会带入后面每一步" disabled={busy} onClick={() => toggleScope(l)} data-testid="snow-brief-scope">{l.scope === "book" ? "全书" : "本步"}</button>
              {l.origin === "author" && <span className="sf-brief-origin" title="你写或改过的条目，教练不能再改写或撤下">你</span>}
              <button className="sf-brief-x" title="撤下（可恢复）" aria-label={`撤下「${l.text}」`} disabled={busy} onClick={() => dismiss(l.line_id)} data-testid="snow-brief-dismiss">×</button>
            </li>
          ))}
        </ul>
      )}
      <div className="sf-brief-add">
        <select value={adding.kind} disabled={busy} onChange={(e) => setAdding({ ...adding, kind: e.target.value })} aria-label="要点类型">
          {BRIEF_KIND_ORDER.map(k => <option key={k} value={k}>{BRIEF_KIND_LABEL[k]}</option>)}
        </select>
        <select value={adding.scope} disabled={busy} onChange={(e) => setAdding({ ...adding, scope: e.target.value })} aria-label="要点范围">
          <option value="step">本步</option>
          <option value="book">全书</option>
        </select>
        <input value={adding.text} disabled={busy} placeholder="加一条你自己的要点…" data-testid="snow-brief-add-text" aria-label="新要点"
          onChange={(e) => setAdding({ ...adding, text: e.target.value })} onKeyDown={(e) => { if (e.key === "Enter" && !isImeComposing(e)) { e.preventDefault(); add(); } }} />
        <button className="btn btn-quiet btn-sm" disabled={busy || !adding.text.trim()} onClick={add} data-testid="snow-brief-add">加入</button>
      </div>
      {inherited.length > 0 && (
        <div className={`sf-brief-inherited ${inherit ? "" : "is-off"}`} data-testid="snow-brief-inherited">
          <div className="sf-brief-sub">继承自上游（全书级）{inherit ? "" : " · 已关闭，不带入本步"}</div>
          <ul>{inherited.map(i => (
            <li key={i.line_id}><span className="sf-brief-kind is-static">{BRIEF_KIND_LABEL[i.kind] || i.kind}</span><span>{i.text}</span><span className="sf-brief-from">{i.step_label}</span></li>
          ))}</ul>
        </div>
      )}
      {dismissed.length > 0 && (
        <div className="sf-brief-dismissed">
          <button className="btn btn-quiet btn-sm" onClick={() => setShowDismissed(v => !v)} data-testid="snow-brief-dismissed-toggle">已撤 {dismissed.length} 条{showDismissed ? " · 收起" : " · 展开"}</button>
          {showDismissed && <ul>{dismissed.map(l => (
            <li key={l.line_id}><span className="sf-brief-kind is-static">{BRIEF_KIND_LABEL[l.kind] || l.kind}</span><span className="sf-brief-text is-dismissed">{l.text}</span><span className="sf-brief-from">{l.dismissed_by === "author" ? "你撤下" : "教练撤下"}</span><button className="btn btn-quiet btn-sm" disabled={busy} onClick={() => restore(l)} data-testid="snow-brief-restore">恢复</button></li>
          ))}</ul>}
        </div>
      )}
    </div>
  );
}

const S2_COACH_QUICKS = [
  "这一步还缺什么？先告诉我最要命的一个缺口。",
  "帮我把这一步的压力再抬高一档——具体到代价。",
  "请直接给我一版可用的改写。",
];

/* 方向卡的字母编号（A/B/C…） */
const S2_ID_LETTERS = ["A", "B", "C", "D"];

/* 方向回合：三张方向卡，每张可「按此生成本步」（02 自由文本步：「就用这一句」）；
   多成员步骤另给「只更新「X」」；被采纳过的卡打「已按此生成」徽章（回合的 adoption）。 */
function S2DirectionCards({ turn, freeText, focusLabel, structBusy, busyTarget, onAdopt, onAdoptText }) {
  const items = (turn && turn.candidates) || [];
  const chosen = turn && turn.adoption && typeof turn.adoption.candidate_index === "number" ? turn.adoption.candidate_index : null;
  // 只有被点的那张卡转圈；其余卡在生成期间只是禁用，文案不变
  const busyOn = (i, focused) => !!(structBusy && busyTarget && busyTarget.kind === "direction" && busyTarget.turnId === turn.turn_id && busyTarget.index === i && !!busyTarget.focused === focused);
  return (
    <div className="sf-dir-cards" data-testid="snow-direction-cards">
      {items.map((c, i) => (
        <article key={i} className={`sf-dir-card ${chosen === i ? "is-chosen" : ""} ${busyOn(i, false) || busyOn(i, true) ? "is-busy" : ""}`} data-testid="snow-direction-card">
          <header className="sf-dir-head">
            <span className="sf-dir-id">{S2_ID_LETTERS[i] || i + 1}</span>
            <span className="sf-dir-label">{c.label}</span>
            {c.tag && <span className="pill text-xs"><span className="pill-dot" />{c.tag}</span>}
            {chosen === i && <span className="pill pill-sage text-xs sf-dir-chosen" title="本步有一版就是按这个方向生成的"><span className="pill-dot" />已按此生成</span>}
          </header>
          <p className="sf-dir-text">{c.text}</p>
          {(c.notes || []).length > 0 && <div className="sf-dir-notes">{c.notes.map((n, j) => <span key={j} className="pill text-xs">{n}</span>)}</div>}
          <div className="sf-dir-actions">
            {freeText ? (
              <button className="btn btn-primary btn-sm" onClick={() => onAdoptText(turn, i)} data-testid="snow-direction-use-text" title="这一句就是本步的内容，直接采用（不再调用模型）">
                <I.Check size={13} /> 就用这一句
              </button>
            ) : (
              <button className="btn btn-primary btn-sm" disabled={structBusy} onClick={() => onAdopt(turn, i)} data-testid="snow-direction-adopt"
                title="以这个方向为蓝本，让 AI 把本步全部字段整套写好（生成前留底，可回滚）">
                {busyOn(i, false) ? <I.Refresh size={13} className="sf-spin" /> : <I.Wand size={13} />} {busyOn(i, false) ? "生成中…" : "按此生成本步"}
              </button>
            )}
            {!freeText && focusLabel && (
              <button className="btn btn-quiet btn-sm" disabled={structBusy} onClick={() => onAdopt(turn, i, { focused: true })} data-testid="snow-direction-adopt-focused"
                title={`只按这个方向更新当前选中的「${focusLabel}」，其余成员保持不动（可回滚）`}>
                {busyOn(i, true) ? <I.Refresh size={13} className="sf-spin" /> : null} {busyOn(i, true) ? "定向中…" : `只更新「${focusLabel}」`}
              </button>
            )}
          </div>
        </article>
      ))}
    </div>
  );
}

/* ====== 驻场教练：逐步对话辅导（回合服务端持久化；第 10 步自动聚焦选中场） ====== */
export function S2Coach({ active, beKey, history, busy, dirBusy, focusRow, focusLabel, sceneLabel, freeText, onSend, onDirections, onApplyPatch, onAdoptDirection, onAdoptDirectionAsText, brief, briefBusy, onSaveBrief, briefUsage, onRegenWithBrief, structBusy, busyTarget, err, onClearErr }) {
  const [input, setInput] = useSS("");
  const turns = (history || []).filter(t => t.step_key === beKey);
  const endRef = useSR(null);
  /* 日志跟着画布一起滚（不再是一层 420px 的内嵌滚动框）。只在对话真的往下长的时候把输入框带进视野：
     刚打开教练页、历史懒加载回来时不跳——页首的要点卡要先看得见。 */
  const lastSeenRef = useSR(null);
  useSE(() => {
    const prev = lastSeenRef.current;
    lastSeenRef.current = { n: turns.length, busy, dirBusy };
    if (!prev) return;
    const grew = prev.n > 0 && turns.length > prev.n;
    const started = (busy && !prev.busy) || (dirBusy && !prev.dirBusy);
    if (!grew && !started) return;
    try { if (endRef.current && endRef.current.scrollIntoView) endRef.current.scrollIntoView({ block: "end" }); } catch (e) {}
  }, [turns.length, busy, dirBusy]);
  const send = (text) => { const t = (text != null ? text : input).trim(); if (!t || busy) return; onSend(t); setInput(""); };
  const directions = () => { if (busy || dirBusy) return; const ask = input.trim(); setInput(""); onDirections(ask); };
  return (
    <div className="sf-coach">
      <div className="sf-coach-note">
        <I.Sparkles size={14} />
        <span>教练记得本步的对话，读得到你<b>本步草稿</b>与上游材料{focusRow ? <>，当前聚焦 <b>{focusLabel || "选中的那一场"}</b>（跟随第 10 步选中的场）</> : null}。
          三件事：<b>聊</b>——问缺口、抬压力；<b>记</b>——它把你定下的、否决的、还在犹豫的记进下面的「本步要点」，生成都照要点写；
          <b>写</b>——「先看 3 个方向」挑一个「按此生成本步」，或让它「直接改写」再「填入本步」。</span>
      </div>
      <S2BriefCard brief={brief} busy={briefBusy} onSave={onSaveBrief} usage={briefUsage} onRegen={onRegenWithBrief} structBusy={structBusy} />
      <div className="sf-coach-log">
        {!turns.length && !busy && !dirBusy && (
          <div className="sf-coach-empty">还没有对话。从下面的快捷提问开始，问「{active.name}」这一步的任何问题，或直接「先看 3 个方向」。</div>
        )}
        {turns.map(t => {
          const isCards = t.turn_kind === "candidates";
          const deltaParts = s2BriefDeltaParts(t.brief_delta);
          const hasPatch = !!(t.candidate_patch && Object.keys(t.candidate_patch).length > 0);
          const adopted = !!t.adoption;
          const replyBusy = !!(structBusy && busyTarget && busyTarget.kind === "direction" && busyTarget.turnId === t.turn_id);
          return (
            <div key={t.turn_id} className={`sf-coach-turn ${isCards ? "is-directions" : ""}`} data-testid={isCards ? "snow-coach-turn-directions" : "snow-coach-turn"}>
              <div className="sf-coach-q"><span className="sf-coach-who">我</span><span>{t.message || "（生成建议）"}</span></div>
              <div className="sf-coach-a">
                <span className={`sf-coach-who ${t.source === "llm" ? "is-ai" : ""}`}>{t.source === "llm" ? "教练" : "规则"}</span>
                <div className="sf-coach-body">
                  {isCards ? (
                    <S2DirectionCards turn={t} freeText={freeText} focusLabel={focusLabel} structBusy={structBusy} busyTarget={busyTarget}
                      onAdopt={onAdoptDirection} onAdoptText={onAdoptDirectionAsText} />
                  ) : (
                    <React.Fragment>
                      <CoachReply text={t.reply} />
                      {(t.suggestions || []).length > 0 && (
                        <ul className="sf-coach-sugs">{(t.suggestions || []).slice(0, 4).map((s, i) => <li key={i}><CoachInline text={s} /></li>)}</ul>
                      )}
                      {(hasPatch || (t.source === "llm" && t.reply)) && (
                        <div className="sf-coach-actions">
                          {hasPatch && (
                            <button className="btn btn-accent btn-sm" onClick={() => onApplyPatch(t)} data-testid="snow-coach-patch"
                              title="把教练给出的改写合并进本步（空字段不清空、按角色/场景对位；填入前自动留底）">
                              <I.Check size={13} /> 填入本步{t.candidate_label ? `「${t.candidate_label}」` : ""}
                            </button>
                          )}
                          {t.source === "llm" && t.reply && (
                            <button className="btn btn-quiet btn-sm" disabled={structBusy} onClick={() => onAdoptDirection(t)} data-testid="snow-coach-adopt"
                              title="把这段回复作为本步的方向重新展开整步（按它点名的缺口 / 走向 / 禁忌；生成前留底，可回滚）">
                              {replyBusy ? <I.Refresh size={13} className="sf-spin" /> : <I.Wand size={13} />} {replyBusy ? "生成中…" : "按此生成本步"}
                            </button>
                          )}
                          {adopted && <span className="pill pill-sage text-xs sf-dir-chosen" title="本步有一版就是按这段回复生成的"><span className="pill-dot" />已按此生成</span>}
                        </div>
                      )}
                    </React.Fragment>
                  )}
                  {(deltaParts.length > 0 || t.focus_scene_id) && (
                    <div className="sf-coach-meta">
                      {deltaParts.length > 0 && <span className="sf-coach-delta" data-testid="snow-coach-delta" title="这一轮教练对本步要点做的改动（上面的要点卡里可核对）">要点 {deltaParts.join(" · ")}</span>}
                      {t.focus_scene_id && <span className="sf-coach-focus">聚焦 {sceneLabel ? sceneLabel(t.focus_scene_id) : "一场"}</span>}
                    </div>
                  )}
                </div>
              </div>
            </div>
          );
        })}
        {busy && <div className="sf-coach-busy"><I.Refresh size={13} className="sf-spin" /> 教练正在读你的草稿…</div>}
        {dirBusy && <div className="sf-coach-busy" data-testid="snow-directions-busy"><I.Refresh size={13} className="sf-spin" /> 教练正在依上游材料与本步要点想三个方向…</div>}
      </div>
      {err && (
        <div className="sf-cand-err" role="alert" data-testid="snow-coach-error">
          <I.AlertTriangle size={13} /><span>{err}</span>
          <button className="btn btn-quiet btn-sm" onClick={onClearErr}>知道了</button>
        </div>
      )}
      <div className="sf-coach-quicks" role="group" aria-label="快捷提问">
        {S2_COACH_QUICKS.map((q, i) => (
          <button key={i} type="button" className="sf-coach-quick" disabled={busy} onClick={() => send(q)}>{q}</button>
        ))}
      </div>
      <div className="sf-coach-input">
        <textarea rows={2} value={input} disabled={busy || dirBusy} aria-label={`问教练「${active.name}」这一步`} placeholder={`问「${active.name}」这一步的任何问题；写下要求再点「给 3 个方向」，教练按要求给方向…`}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={(e) => { if ((e.metaKey || e.ctrlKey) && e.key === "Enter" && !isImeComposing(e)) { e.preventDefault(); e.stopPropagation(); send(); } }} />
        <div className="sf-coach-send">
          <button className="btn btn-primary" disabled={busy || dirBusy || !input.trim()} onClick={() => send()} title={`发送（${modEnterShortcut()}）`}>
            {busy ? <I.Refresh size={14} className="sf-spin" /> : <I.ArrowRight size={14} />} 发送
          </button>
          <button className="btn btn-quiet" disabled={busy || dirBusy} onClick={directions} data-testid="snow-coach-directions"
            title="让教练给本步三个不同方向（输入框里写了要求就按要求给）">
            {dirBusy ? <I.Refresh size={14} className="sf-spin" /> : <I.Compass size={14} />} 给 3 个方向
          </button>
        </div>
      </div>
      <div ref={endRef} />
    </div>
  );
}
