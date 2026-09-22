import React from "react";
import { I } from "./icons.jsx";
import { S2AutoText, S2PovPick } from "./ws-snow-fields.jsx";
import { s2LoadUiPref, s2SaveUiPref, S2_PREF_KEYS } from "./ws-snow-hooks.js";
import {
  S2_TRIAGE_LABEL, S2_VERDICTS, s2BusyOn, s2InferSpine, s2PlanState, s2PovLabel, s2RosterList, s2SceneNo,
} from "./ws-snow-model.js";

/* ==========================================================
   10 场景规划（从 ws-snow-scenes.jsx 拆出，2026-09-22）
   ----------------------------------------------------------
   逐场画草图：主动 目标 / 冲突 / 挫败，反应 反应 / 两难 / 决定。形态跟随 09，这里只规划、裁定。
   ========================================================== */

const { useState: useSS } = React;

/* 第 10 步的版面围绕三拍：场景头（编号 · 题名 · 形态 · 视角 · 你的裁定）→ 主三拍 → 接着的次要三拍 →
   收起的「场景卡细节」→ 破例理由 → 分诊结果。以前三拍前面先铺了十二个零散输入框，三拍本身落在首屏之外、
   还是两行高的框，长一点的节拍就被截住。「场景卡细节」收起与否按本机记住。 */
export function S2ScenePlan({ scaffold, onScaffold, refs, go, ai }) {
  const list = ((refs && refs.scenes) || {}).list || [];
  const roster = s2RosterList(refs);
  const plans = scaffold.plans || {};
  const selId = list.some(s => s.id === scaffold.sel) ? scaffold.sel : (list[0] ? list[0].id : "");
  const scene = list.find(s => s.id === selId) || null;
  const selIdx = list.findIndex(s => s.id === selId);
  // 类型跟随 09 的真相：主动/反应在场景列表里定，这里不再各说各话
  const proactive = scene ? scene.type !== "reactive" : true;
  const plan = { mode: proactive ? "proactive" : "reactive", pov: (scene && scene.pov) || "", goal: "", conflict: "", setback: "", reaction: "", dilemma: "", decision: "", cost_requirement: "", rendering: "full", onstage: [], story_time: "", reader_emotion: "", hook: "", exit_change: "", title: "", length: "", must_include: "", exception: "", ...(plans[selId] || {}) };
  plan.mode = proactive ? "proactive" : "reactive";
  const setPlan = (f, v) => onScaffold(s => ({ ...s, sel: selId, plans: { ...(s.plans || {}), [selId]: { ...plan, [f]: v } } }));
  const selScene = (id) => onScaffold(s => ({ ...s, sel: id }));
  // hooks 一律在提前返回之前（09 在空与非空之间切换时——水合、清空——hook 数量不能变）
  const [secondaryOpen, setSecondaryOpen] = useSS(false);
  const [detailsOpen, setDetailsOpen] = useSS(() => !!s2LoadUiPref(S2_PREF_KEYS.planDetails).open);
  const toggleDetails = (open) => { setDetailsOpen(open); s2SaveUiPref(S2_PREF_KEYS.planDetails, { open }); };

  if (!list.length) {
    return (
      <div className="sf-scaffold sf-scene">
        <div className="sf-plan-empty">
          <I.List size={20} />
          <div>
            <div className="fw-600">还没有可规划的场景</div>
            <div className="text-muted text-sm">第 10 步逐场画草图——先去 09 把全书拆成一行一场。</div>
          </div>
          <button className="btn btn-primary btn-sm" onClick={() => go && go("scenes")}>去 09 场景列表</button>
        </div>
      </div>
    );
  }

  // 阶段 E：覆盖格按 09 的类型数槽（与编辑器同一真相），不再看存储的 plan.mode
  const typeOf = Object.fromEntries(list.map(s => [s.id, s.type]));
  const stateOf = (id) => s2PlanState(plans[id], typeOf[id]);
  const fully = list.filter(s => stateOf(s.id) === 2).length;
  const triItems = (ai && ai.triage && ai.triage.items) || null;
  const triOf = (id) => (triItems ? triItems[id] : null);
  const selTri = triOf(selId);
  const prev = selIdx > 0 ? list[selIdx - 1] : null;
  const prevPlan = prev ? plans[prev.id] : null;
  const prevSeam = prev ? ((prev.type === "reactive" ? (prevPlan || {}).decision : (prevPlan || {}).setback) || "").trim() : "";
  const nextUnplanned = () => { const t = list.find(s => stateOf(s.id) === 0 && s.id !== selId); if (t) selScene(t.id); };
  const no = s2SceneNo(scene.id, selIdx);
  const spineMark = scene.spine || s2InferSpine(scene.fn);

  const triples = proactive
    ? [
        { f: "goal",     label: "目标", desc: "视角人物进这一场时想要的、看得见能达成的东西" },
        { f: "conflict", label: "冲突", desc: "一连串挡在目标前的阻碍，逐级升级" },
        { f: "setback",  label: "挫败", desc: "结尾的一记打击，通常是「是的，但…」；非赢不可时写带代价的胜利，以主角衡量" },
        { f: "cost_requirement", label: "代价", desc: "角色为这个结果具体付出了什么——免费的选择是注水（建议，不是硬性要求）" },
      ]
    : [
        { f: "reaction", label: "反应", desc: "对上一场挫败的情绪反应（允许角色崩一下）" },
        { f: "dilemma",  label: "两难", desc: "没有好选项，只有两个都要付代价的坏选项" },
        { f: "decision", label: "决定", desc: "她选一个坏选项——它成为下一场的目标" },
        { f: "cost_requirement", label: "代价", desc: "角色为这个决定具体付出了什么——免费的选择是注水（建议，不是硬性要求）" },
      ];
  const secondaryTriples = proactive
    ? [
        { f: "reaction", label: "反应", desc: "挫败之后当场的情绪反应" },
        { f: "dilemma",  label: "两难", desc: "当场权衡的两个坏选项" },
        { f: "decision", label: "决定", desc: "当场选定的下一步——本场以它收尾" },
      ]
    : [
        { f: "goal",     label: "目标", desc: "决定之后立刻去做的事" },
        { f: "conflict", label: "冲突", desc: "当场遇到的阻碍" },
        { f: "setback",  label: "挫败", desc: "当场的打击——本场以它收尾" },
      ];
  const detailFields = ["story_time", "reader_emotion", "exit_change", "hook", "must_include"];
  const detailsFilled = detailFields.filter(f => String(plan[f] || "").trim()).length + ((plan.onstage || []).length ? 1 : 0);
  const renderMode = plan.rendering === "summary" || plan.rendering === "skip" ? plan.rendering : "full";
  const cellTitle = (s, i, st, tri) => `${s2SceneNo(s.id, i)} · ${s.type === "reactive" ? "反应" : "主动"}`
    + `${(plans[s.id] || {}).rendering === "summary" ? " · 概述" : s.type === "reactive" && (plans[s.id] || {}).rendering === "skip" ? " · 略过" : ""}`
    + `${(s.spine || s2InferSpine(s.fn)) ? " · " + (s.spine || s2InferSpine(s.fn)) : ""}`
    + ` · ${((plans[s.id] || {}).exception || "").trim() ? "破例" : st === 2 ? "三拍齐" : st === 1 ? "填了一半" : "未规划"}`
    + `${tri ? " · 分诊：" + (S2_TRIAGE_LABEL[tri.status] || tri.status) : ""}`;

  return (
    <div className="sf-scaffold sf-scene">
      {/* 覆盖率导航：一格一场，点击切换 */}
      <div className="sf-plan-nav">
        <span className={`sf-plan-cov ${fully === list.length ? "is-all" : ""}`}><I.CheckCircle size={12} /> {fully} / {list.length} 场已规划</span>
        <div className="sf-plan-cells" role="group" aria-label="逐场规划进度">
          {list.map((s, i) => {
            const st = stateOf(s.id);
            const tri = triOf(s.id);
            const label = cellTitle(s, i, st, tri);
            return (
              <button key={s.id}
                className={`sf-plan-cell st-${st} ${s.id === selId ? "is-sel" : ""} ${s.type === "reactive" ? "is-rea" : "is-pro"} ${(s.spine || s2InferSpine(s.fn)) ? "is-spine" : ""} ${tri ? "tri-" + tri.status : ""}`}
                onClick={() => selScene(s.id)} title={label} aria-label={label} aria-current={s.id === selId ? "true" : undefined}>
                {i + 1}
              </button>
            );
          })}
        </div>
        {fully < list.length && <button className="btn btn-quiet btn-sm" onClick={nextUnplanned}><I.ChevronRight size={13} /> 下一个未规划</button>}
        <div className="sf-plan-legend" aria-hidden="true">
          <span><i className="lg-pro" />主动</span>
          <span><i className="lg-rea" />反应</span>
          <span><i className="lg-full" />三拍齐</span>
          <span><i className="lg-half" />填了一半</span>
          <span><i className="lg-spine" />灾难场</span>
          <span><i className="lg-tri" />下划线 = 分诊结果</span>
        </div>
      </div>

      {/* 场景头：编号 · 题名 · 形态 · 视角 · AI 补全这一场；下面一行是你的裁定 */}
      <div className="sf-plan-head">
        <div className="sf-plan-head-top">
          <span className="sf-plan-cur-id">{no}</span>
          {/* 阶段 R：场景题名（原著第 9 步每场有标题；留空 = 跟随 09 的事件文本，模型给的短题名不再被覆盖） */}
          <input className="sf-plan-title" data-testid="snow-plan-title" value={plan.title || ""} onChange={(e) => setPlan("title", e.target.value)}
            placeholder={scene.event || "场景题名（留空就跟随 09）"} aria-label={`${no} 的题名`} title="这一场的短题名；留空时跟随 09 的事件文本" />
          <span className={`sf-plan-type ${proactive ? "is-pro" : "is-rea"}`} title="形态跟随 09 场景列表">
            {proactive ? "主动场" : "反应场"}
            <button type="button" className="sf-plan-type-go" onClick={() => go && go("scenes")} title="在 09 场景列表里改形态">在 09 改</button>
          </span>
          <label className="sf-plan-pov">
            <span className="sf-field-label">视角</span>
            <S2PovPick value={plan.pov} roster={roster} onChange={(v) => setPlan("pov", v)} className="sf-field-input" placeholder={s2PovLabel(scene.pov, roster) || "视角人物"} ariaLabel={`${no} 的视角人物`} />
          </label>
        </div>
        <div className="sf-plan-head-sub">
          {scene.place && <span><I.MapPin size={11} /> {scene.place}</span>}
          {spineMark && <span className="sf-plan-spine" title={scene.spine ? "09 里标的灾难" : "按「功能」一栏认出的灾难"}>{spineMark}{scene.spine ? "" : "（推断）"}</span>}
          {scene.fn && <span>{scene.fn}</span>}
          {ai && (
            <button className="btn btn-quiet btn-sm sf-plan-fill" disabled={ai.structBusy} onClick={() => ai.onFillScene(selId)}
              title="只补全这一场的三拍、坩埚与钩子，其余场景不动（生成前自动留底）">
              <I.Wand size={13} className={s2BusyOn(ai, "fill_scene", selId) ? "sf-spin" : ""} /> {s2BusyOn(ai, "fill_scene", selId) ? "生成中…" : "AI 补全这一场"}
            </button>
          )}
        </div>
        {/* 阶段 R：作者的裁定——原著的 Yes / No / Maybe 由作者拍板；该重写 / 待删的场不物化、不阻断全书 */}
        {ai && ai.onVerdict && (
          <div className="sf-plan-verdict" data-testid="snow-plan-verdict" role="group" aria-label="你对这一场的裁定"
            title="通过 = 这一场成立；需修补 = 能修；该重写 = 从设计重建，整理时先不建卡；待删 = 标记待删（不真删，三拍留在构思里，整理时不建卡）">
            <span className="sf-field-label">你的裁定</span>
            {S2_VERDICTS.map(v => (
              <button key={v} type="button" data-testid={`snow-verdict-${v}`} aria-pressed={!!(selTri && selTri.status === v && selTri.manual)}
                className={`sf-plan-render-opt tri-${v} ${selTri && selTri.status === v && selTri.manual ? "is-on" : ""}`} onClick={() => ai.onVerdict(selId, v)}>{S2_TRIAGE_LABEL[v]}</button>
            ))}
            {selTri && !selTri.manual && selTri.status && <span className="sf-plan-verdict-hint">系统建议：{S2_TRIAGE_LABEL[selTri.status] || selTri.status}</span>}
          </div>
        )}
      </div>

      {prev && (
        <div className={`sf-plan-seam ${prevSeam ? "" : "is-empty"}`}>
          <span className="sf-plan-seam-tag">接上一场 {s2SceneNo(prev.id, selIdx - 1)}</span>
          {prevSeam
            ? <span className="sf-plan-seam-text">{prev.type === "reactive" ? "决定" : "挫败"}：「{prevSeam}」——本场从这里接住。</span>
            : <span className="sf-plan-seam-text">上一场还没写{prev.type === "reactive" ? "决定" : "挫败"}，链条在这里是断的。<button className="sf-plan-seam-go" onClick={() => selScene(prev.id)}>去补 {s2SceneNo(prev.id, selIdx - 1)}</button></span>}
        </div>
      )}

      <div className="sf-gcs" key={selId + plan.mode} role="group" aria-label={proactive ? "主动场三拍：目标、冲突、挫败" : "反应场三拍：反应、两难、决定"}>
        {triples.map((t, i) => (
          <div key={t.f} className={`sf-beat ${proactive ? "tone-crimson" : "tone-slate"} ${t.f === "cost_requirement" ? "is-optional" : ""}`}>
            <div className="sf-beat-side"><span className="sf-beat-idx">{i + 1}</span></div>
            <label className="sf-beat-main">
              <span className="sf-beat-label">{t.label}<span className="sf-beat-desc">{t.desc}</span></span>
              <S2AutoText className="sf-beat-text" minRows={2} value={plan[t.f] || ""}
                onChange={(e) => setPlan(t.f, e.target.value)} placeholder={`写「${t.label}」…`} />
            </label>
          </div>
        ))}
      </div>

      {/* 阶段 I：一场可以接着另一组三拍（原著 Goldilocks 场景 1 / 8 / 13）——主三拍之后接着发生，本场以最后一拍收尾。可选。 */}
      <details className="sf-plan-secondary" data-testid="snow-plan-secondary" open={secondaryOpen || secondaryTriples.some(t => (plan[t.f] || "").trim())} onToggle={(e) => setSecondaryOpen(!!e.target.open)}>
        <summary>接着的次要三拍（可选）：{proactive ? "挫败之后在同一场里反应、两难、决定" : "决定之后在同一场里立刻行动、受阻、挫败"}</summary>
        <div className="sf-gcs">
          {secondaryTriples.map((t, i) => (
            <div key={t.f} className={`sf-beat ${proactive ? "tone-slate" : "tone-crimson"}`}>
              <div className="sf-beat-side"><span className="sf-beat-idx">{triples.length + i + 1}</span></div>
              <label className="sf-beat-main">
                <span className="sf-beat-label">{t.label}<span className="sf-beat-desc">{t.desc}</span></span>
                <S2AutoText className="sf-beat-text" minRows={2} value={plan[t.f] || ""}
                  onChange={(e) => setPlan(t.f, e.target.value)} placeholder={`写「${t.label}」…（留空 = 本场没有这一拍）`} />
              </label>
            </div>
          ))}
        </div>
      </details>

      {/* 场景卡细节：写手与连续性检查要用，但不是这一步的重心——收起时也在 DOM 里（单测、表单都在） */}
      <details className="sf-plan-details" data-testid="snow-plan-details" open={detailsOpen} onToggle={(e) => { if (!!e.target.open !== detailsOpen) toggleDetails(!!e.target.open); }}>
        <summary>场景卡细节<span className="sf-plan-details-count">已填 {detailsFilled} / {detailFields.length + 1}</span><span className="sf-plan-details-sum">在场人物 · 故事时间 · 读者情绪 · 离场变化 · 钩子 · 必须出现 · 呈现与篇幅</span></summary>
        <div className="sf-plan-details-grid">
          {/* 阶段 J：原著第 9 步「列出在场人物」——从 04 名册点选，视角人物之外的人 */}
          <div className="sf-field is-wide sf-plan-onstage" data-testid="snow-plan-onstage" title="这一场里还有谁在场（视角人物之外）——写手与连续性检查都要用">
            <span className="sf-field-label">在场人物</span>
            <span className="sf-plan-onstage-chips" role="group" aria-label="在场人物">
              {(roster || []).filter(r => r.id !== plan.pov).map(r => {
                const on = (plan.onstage || []).includes(r.id);
                return (
                  <button key={r.id} type="button" aria-pressed={on} className={`sf-plan-render-opt ${on ? "is-on" : ""}`}
                    onClick={() => setPlan("onstage", on ? (plan.onstage || []).filter(x => x !== r.id) : [...(plan.onstage || []), r.id])}>{r.name}</button>
                );
              })}
              {!(roster || []).length && <span className="sf-muted-note">04 名册还是空的</span>}
            </span>
          </div>
          <label className="sf-field"><span className="sf-field-label">故事时间</span>
            <input className="sf-field-input" data-testid="snow-plan-story-time" value={plan.story_time || ""} onChange={(e) => setPlan("story_time", e.target.value)} placeholder="如：第三天傍晚" title="原著场景表的时间戳——连续性的锚" /></label>
          <label className="sf-field"><span className="sf-field-label">读者应感到</span>
            <input className="sf-field-input" data-testid="snow-plan-reader-emotion" value={plan.reader_emotion || ""} onChange={(e) => setPlan("reader_emotion", e.target.value)} placeholder="这一场读完，读者被留在什么情绪里" title="近终稿评审据此判这一场落地没有" /></label>
          {/* 阶段 M：钩子与离场变化——一直是场景卡的列，此前前端没有输入框 */}
          <label className="sf-field is-wide"><span className="sf-field-label">离场变化</span>
            <S2AutoText className="sf-field-input" data-testid="snow-plan-exit-change" value={plan.exit_change || ""} onChange={(e) => setPlan("exit_change", e.target.value)} placeholder="这一场结束时什么不可逆地变了（留空 = 就是挫败 / 决定）" /></label>
          <label className="sf-field is-wide"><span className="sf-field-label">钩子</span>
            <S2AutoText className="sf-field-input" data-testid="snow-plan-hook" value={plan.hook || ""} onChange={(e) => setPlan("hook", e.target.value)} placeholder="逼读者翻页的未解之事（留空 = 挫败 / 决定本身就是牵引）" /></label>
          {/* 阶段 R：原著第 9 步「好的对话片段」——必须出现的对话 / 物件 / 一句话；一直是场景卡的列，此前只有模型能写 */}
          <label className="sf-field is-wide"><span className="sf-field-label">必须出现</span>
            <S2AutoText className="sf-field-input" data-testid="snow-plan-must-include" value={plan.must_include || ""} onChange={(e) => setPlan("must_include", e.target.value)} placeholder="这一场想到的好对话、必须出现的物件或一句话（原著第 9 步的「对话片段」）" /></label>
          {/* 阶段 C / N：呈现方式——概述对两种形态都合法（原著第 1 场就是主动场的叙述概述，收尾几场也是）；略过只给反应场 */}
          <div className="sf-field sf-plan-render" data-testid="snow-plan-render" role="group" aria-label="呈现方式"
            title="整场戏剧化，还是两三段叙述概述（约 200–500 字）？概述场整理后拿到 200-500 的篇幅带，起草按概述写">
            <span className="sf-field-label">呈现</span>
            <span className="sf-plan-opts">
              <button type="button" aria-pressed={renderMode === "full"} className={`sf-plan-render-opt ${renderMode === "full" ? "is-on" : ""}`} onClick={() => setPlan("rendering", "full")}>完整场</button>
              <button type="button" aria-pressed={renderMode === "summary"} className={`sf-plan-render-opt ${renderMode === "summary" ? "is-on" : ""}`} onClick={() => setPlan("rendering", "summary")}>概述两段</button>
              {!proactive && <button type="button" aria-pressed={renderMode === "skip"} className={`sf-plan-render-opt ${renderMode === "skip" ? "is-on" : ""}`} onClick={() => setPlan("rendering", "skip")} title="页面上略过这一场，直接进下一场主动场景——反应 / 两难 / 决定照样写，它们决定下一场的目标，也会带给下一场的写手">略过</button>}
            </span>
          </div>
          {/* 阶段 R：篇幅带——原著「场景长度没有标准，一百词到五千词都可以」；短 / 中 / 长或自定义字数区间（如 800-1200，数值带会被起草硬约束）；概述场固定 200–500 */}
          {renderMode === "full" && (
            <div className="sf-field sf-plan-render sf-plan-length" data-testid="snow-plan-length" role="group" aria-label="篇幅"
              title="短 / 中 / 长只是给写手的提示，自定义字数区间（如 800-1200）会被起草按数值硬约束；原著说场景长度没有标准，选适合这一场的">
              <span className="sf-field-label">篇幅</span>
              <span className="sf-plan-opts">
                {[["short", "短"], ["medium", "中"], ["long", "长"]].map(([v, l]) => (
                  <button key={v} type="button" aria-pressed={(plan.length || "medium") === v} className={`sf-plan-render-opt ${(plan.length || "medium") === v ? "is-on" : ""}`} onClick={() => setPlan("length", v)}>{l}</button>
                ))}
                <input className="sf-field-input sf-plan-length-custom" data-testid="snow-plan-length-custom" value={/^\d+\s*[-–—]\s*\d+$/.test(plan.length || "") ? plan.length : ""} onChange={(e) => setPlan("length", e.target.value.trim())} placeholder="或写字数，如 800-1200" aria-label="自定义字数区间" />
              </span>
            </div>
          )}
        </div>
      </details>

      {/* 阶段 R：破例理由——原著「不过关也可以放行，但我要知道理由」；写了理由，缺的三拍 / 坩埚不再算缺失 */}
      <label className="sf-field sf-plan-exception"><span className="sf-field-label">破例理由<span className="sf-field-hint">这一场故意不按三拍走？写下理由，缺的三拍就不算缺</span></span>
        <input className="sf-field-input" data-testid="snow-plan-exception" value={plan.exception || ""} onChange={(e) => setPlan("exception", e.target.value)} placeholder="如：全书收尾的叙述交代，没有新冲突" title="原著第 22 场「冲突：无」——破例要知道理由；写了理由，规则层不再把缺的三拍算缺失，起草与评审按理由判" /></label>

      {/* 本场分诊结果：状态 + 诊断 + 修复步骤 + 一键应用补丁 */}
      {selTri && (
        <div className={`sf-triage tri-${selTri.status}`}>
          <div className="sf-triage-head">
            <span className="sf-triage-badge">{S2_TRIAGE_LABEL[selTri.status] || selTri.status}</span>
            {typeof selTri.score === "number" && <span className="sf-triage-score">{selTri.score} 分</span>}
            <span className="sf-triage-notes">{selTri.notes || ""}</span>
            {Object.keys(selTri.repair_patch || {}).length > 0 && (
              <button className="btn btn-accent btn-sm" disabled={ai.structBusy} onClick={() => ai.onApplyRepair(selId, selTri)}
                title="把分诊给出的修复补丁写进本场三拍 / 坩埚（应用前自动留底）">
                <I.Check size={13} /> 应用修复补丁
              </button>
            )}
          </div>
          {(selTri.fix_steps || []).length > 0 && (
            <ul className="sf-triage-fixes">
              {(selTri.fix_steps || []).slice(0, 4).map((f, i) => <li key={i}>{f}</li>)}
            </ul>
          )}
        </div>
      )}

      <div className="sf-plan-foot">
        <button className="btn btn-ghost btn-sm" disabled={selIdx <= 0} onClick={() => selScene(list[selIdx - 1].id)}><I.ChevronLeft size={13} /> {selIdx > 0 ? s2SceneNo(list[selIdx - 1].id, selIdx - 1) : "上一场"}</button>
        <span className="sf-plan-foot-pos">{selIdx + 1} / {list.length}</span>
        <button className="btn btn-ghost btn-sm" disabled={selIdx >= list.length - 1} onClick={() => selScene(list[selIdx + 1].id)}>{selIdx < list.length - 1 ? s2SceneNo(list[selIdx + 1].id, selIdx + 1) : "下一场"} <I.ChevronRight size={13} /></button>
      </div>
    </div>
  );
}
