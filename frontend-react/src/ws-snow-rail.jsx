import React from "react";
import { I } from "./icons.jsx";
import {
  BRIEF_KIND_LABEL, S2_DISASTERS, S2_RUBRIC, s2PlanAuto, s2Pipeline, s2SceneAuto,
} from "./ws-snow-model.js";

/* ==========================================================
   右栏「本步上下文」：本步任务 → 本步要点（只读）→ 本步检查 → 写作指引 → 故事脊柱
   ----------------------------------------------------------
   以前右栏有三套互相打架的「评分」：按关键词计数的「实时自评 0–100」、「机器核验」（一句合格的
   一句话概括也会 0/3）、页脚「自检 0/3」——它们和后端的「已批准」同时出现、互相矛盾。现在本步唯一的
   评分是后端完备性闸门的评定；09 / 10 另有几条确定性的结构核对（只说事实，不打分）；人工清单只是
   写完对一遍的阅读辅助，不计分、不挡确认。
   S2Rail 是 memo 的：只拿它真正读的那几块状态（09 / 10 才拿本步脚手架，10 另拿 09，
   情节步才拿 03 的脊柱），在别的步骤里打字不会让右栏跟着重算。
   ========================================================== */

const { useState: useSS } = React;

export const S2Rail = React.memo(function S2Rail({ step, guide, health, stepScaffold, scenesScaffold, para, checks, onToggle, brief, onOpenBrief, go, guideDefaultOpen }) {
  const checklist = (guide && guide.checklist) || [];
  return (
    <React.Fragment>
      <S2Task guide={guide} />
      <S2BriefRail brief={brief} onOpen={onOpenBrief} />
      <S2Checks stepKey={step.key} health={health} scaffold={stepScaffold} scenes={scenesScaffold}
        checklist={checklist} checks={checks || []} onToggle={onToggle} />
      <S2GuideSec key={`guide-${step.key}`} guide={guide} stepKey={step.key} go={go} defaultOpen={guideDefaultOpen} />
      {step.track === "plot" && step.key !== "paragraph" && <S2Spine step={step} go={go} para={para} />}
    </React.Fragment>
  );
});

/* ====== Collapsible flat section (shared rail primitive) ======
   可折叠时表头是一个真正的按钮（aria-expanded），键盘能开合；不可折叠时是普通标题。 */
function S2Sec({ label, meta, children, collapsible, defaultOpen = true, testId }) {
  const [open, setOpen] = useSS(defaultOpen);
  const isOpen = !collapsible || open;
  return (
    <section className={`sfx-sec ${collapsible ? "is-clp" : ""} ${isOpen ? "is-open" : "is-closed"}`} data-testid={testId}>
      {collapsible ? (
        <button type="button" className="sfx-h" aria-expanded={open} onClick={() => setOpen(o => !o)}>
          <span className="sfx-h-label">{label}</span>
          {meta != null && <span className="sfx-h-meta">{meta}</span>}
          <I.ChevronRight size={13} className="sfx-h-chev" />
        </button>
      ) : (
        <h3 className="sfx-h">
          <span className="sfx-h-label">{label}</span>
          {meta != null && <span className="sfx-h-meta">{meta}</span>}
        </h3>
      )}
      {isOpen && <div className="sfx-sec-body">{children}</div>}
    </section>
  );
}

function S2Task({ guide }) {
  if (!guide) return null;
  return (
    <div className="sfx-task">
      <h3 className="sfx-task-h">本步任务</h3>
      <p className="sfx-task-text">{guide.task}</p>
    </div>
  );
}

/* 右栏的要点摘要（只读）：编辑时随时看得到本步意图，最多三条；完整的卡与编辑在教练页。 */
function S2BriefRail({ brief, onOpen }) {
  const lines = ((brief && brief.lines) || []).filter(l => l && l.status === "active");
  const inherit = !brief || brief.inherit_upstream !== false;
  const inherited = (brief && inherit && Array.isArray(brief.inherited)) ? brief.inherited : [];
  const items = [...lines.map(l => ({ ...l, from: "" })), ...inherited.map(i => ({ ...i, from: i.step_label || "" }))];
  const shown = items.slice(0, 3);
  return (
    <section className="sfx-brief" data-testid="snow-brief-rail">
      <div className="sfx-brief-h">
        <h3 className="sfx-h-label">本步要点</h3>
        <button type="button" className="sfx-link" onClick={onOpen} data-testid="snow-brief-rail-open">{items.length ? "去教练页编辑" : "去教练页加一条"}</button>
      </div>
      {!items.length && <p className="sfx-muted">还没有要点。和教练聊几句，或在教练页直接加一条——AI 生成、方向、分诊都照要点写。</p>}
      {shown.length > 0 && (
        <ul className="sfx-brief-list">
          {shown.map(l => (
            <li key={l.line_id} className={`is-${l.kind} ${l.from ? "is-inherited" : ""}`}>
              <span className="sfx-brief-kind">{BRIEF_KIND_LABEL[l.kind] || l.kind}</span>
              <span className="sfx-brief-text">{l.text}</span>
              {l.from && <span className="sfx-brief-from">{l.from}</span>}
            </li>
          ))}
        </ul>
      )}
      {items.length > shown.length && <p className="sfx-muted">还有 {items.length - shown.length} 条，在教练页看全部。</p>}
    </section>
  );
}

const S2_BE_VERDICT = {
  pass: { label: "结构达标", tone: "sage" },
  maybe: { label: "可以更好", tone: "gold" },
  rewrite: { label: "建议重写", tone: "rose" },
};
function S2Checks({ stepKey, health, scaffold, scenes, checklist, checks, onToggle }) {
  const be = health || null;
  const hasBe = !!(be && (typeof be.score === "number" || (be.missingFields && be.missingFields.length) || be.beStatus));
  const verdict = be && S2_BE_VERDICT[be.status];
  const structural = stepKey === "scenes" ? s2SceneAuto(scaffold)
    : stepKey === "planning" ? s2PlanAuto(scaffold, scenes)
    : [];
  const missing = (be && be.missingFields) || [];
  const actions = (be && be.nextActions) || [];
  return (
    <S2Sec label="本步检查" testId="snow-step-checks"
      meta={hasBe && typeof be.score === "number" ? <span className="sfx-score">后端 {be.score} 分</span> : <span className="sfx-score is-muted">未同步</span>}>
      {!hasBe ? (
        <p className="sfx-muted"><I.Cpu size={11} /> 保存本步后，这里显示服务端的评定：完备度、缺什么、前序步骤是否已确认。</p>
      ) : (
        <div className="sfx-verdict">
          {verdict && <span className={`pill pill-${verdict.tone} text-xs`}><span className="pill-dot" />{verdict.label}</span>}
          {typeof be.filled === "number" && typeof be.total === "number" && <span className="sfx-verdict-fact">已填 {be.filled}/{be.total} 项</span>}
          <span className={`sfx-verdict-fact ${be.gateSatisfied ? "is-ok" : "is-warn"}`}>
            {be.gateSatisfied ? <><I.Unlock size={11} /> 前序步骤已确认</> : <><I.Lock size={11} /> 前序步骤还没确认</>}
          </span>
        </div>
      )}
      {hasBe && missing.length > 0 && (
        <p className="sfx-muted is-warn"><I.AlertTriangle size={11} /> 还有 {missing.length} 处没填（空着的输入框就是）。</p>
      )}
      {hasBe && actions.length > 0 && (
        <ul className="sfx-actions">
          {actions.slice(0, 4).map((a, i) => <li key={i}>{String(a).replace(/^建议[：:]\s*/, "")}</li>)}
        </ul>
      )}
      {structural.length > 0 && (
        <React.Fragment>
          <h4 className="sfx-sub">结构核对</h4>
          <ul className="sfx-autos">
            {structural.map((a, i) => (
              <li key={i} className={a.pass ? "is-pass" : "is-fail"}>
                <span className="sfx-auto-ic" aria-hidden="true">{a.pass ? <I.Check size={11} /> : <I.AlertTriangle size={10} />}</span>
                <span className="sfx-auto-t">{a.t}</span>
                <span className="sfx-auto-val">{a.val}</span>
              </li>
            ))}
          </ul>
        </React.Fragment>
      )}
      {checklist.length > 0 && (
        <React.Fragment>
          <h4 className="sfx-sub">写完自己对一遍<span className="sfx-sub-note">不计分，不挡确认</span></h4>
          <ul className="sfx-checks">
            {checklist.map((c, i) => (
              <li key={i}>
                <button type="button" role="checkbox" aria-checked={!!checks[i]} className={`sfx-check ${checks[i] ? "is-done" : ""}`} onClick={() => onToggle(i)}>
                  <span className="sfx-cbox" aria-hidden="true">{checks[i] && <I.Check size={11} />}</span>
                  <span className="sfx-ctext">{c}</span>
                </button>
              </li>
            ))}
          </ul>
        </React.Fragment>
      )}
    </S2Sec>
  );
}

/* 写作指引：分形管线（上游 → 本步 → 下游，取代了原来的「关联与影响」）+ 本步的写法 + 五个回头自问。
   每一步第一次打开时展开，之后默认收起。 */
function S2GuideSec({ guide, stepKey, go, defaultOpen }) {
  if (!guide) return null;
  const pipe = s2Pipeline(stepKey);
  return (
    <S2Sec label="写作指引" collapsible defaultOpen={defaultOpen} testId="snow-guide">
      {pipe && (
        <div className="sfx-pipe" aria-label="本步在展开链上的位置">
          <button className="sfx-pipe-node" disabled={!pipe.inKey} onClick={() => pipe.inKey && go(pipe.inKey)} title={pipe.inKey ? "回到上游那一步" : "雪花的原点"}>{pipe.inName}</button>
          <span className="sfx-pipe-arr" aria-hidden="true"><I.ChevronRight size={11} /></span>
          <span className="sfx-pipe-cur">本步 <b>{pipe.ratio}</b></span>
          <span className="sfx-pipe-arr" aria-hidden="true"><I.ChevronRight size={11} /></span>
          <button className="sfx-pipe-node" disabled={!pipe.outKey} onClick={() => pipe.outKey && go(pipe.outKey)} title={pipe.outKey ? "去下游那一步" : "下游就是正文"}>{pipe.outName}</button>
        </div>
      )}
      <ol className="sfx-ops">
        {guide.writing.map((w, i) => (
          <li key={i}>
            <span className="sfx-op-idx">{String(i + 1).padStart(2, "0")}</span>
            <div className="sfx-op-body"><span className="sfx-op-k">{w.k}</span><span className="sfx-op-v">{w.v}</span></div>
          </li>
        ))}
      </ol>
      {guide.note && <p className="sfx-note">{guide.note}</p>}
      <h4 className="sfx-sub">写完回头问自己</h4>
      <ul className="sfx-asks">
        {S2_RUBRIC.map(r => <li key={r.k}><b>{r.k}</b>{r.q}</li>)}
      </ul>
    </S2Sec>
  );
}

/* ====== Story spine (derived live from step 03 — single source of truth) ====== */
function S2Spine({ step, go, para }) {
  const p = para || {};
  const clip = (s, n) => { s = (s || "").trim(); return s.length > n ? s.slice(0, n) + "…" : s; };
  const rows = [
    { meta: S2_DISASTERS[0], text: p.d1 },
    { meta: S2_DISASTERS[1], text: p.d2 },
    { meta: S2_DISASTERS[2], text: p.d3 },
  ];
  return (
    <div className={`ctx-block sf-spine-block ${step && step.track === "plot" ? "is-hot" : ""}`}>
      <h3 className="sf-spine-head"><I.Activity size={13} /><span>故事脊柱：三幕三灾难</span></h3>
      <div className="sf-premise-mini" title="道德前提：在第二个灾难处，错误信念翻转为正确信念">
        <span className="sf-pm-false">{p.premiseF || "错误信念"}</span>
        <I.ArrowRight size={12} />
        <span className="sf-pm-true">{p.premiseT || "正确信念"}</span>
      </div>
      <div className="sf-spine">
        {rows.map((d, i) => (
          <div key={i} className={`sf-spine-row tone-${d.meta.tone}`}>
            <span className="sf-spine-id">{d.meta.id}</span>
            <div className="sf-spine-body">
              <span className="sf-spine-title">{clip(d.text, 18) || "（待填）"}</span>
              <span className="sf-spine-act">{d.meta.act}</span>
            </div>
          </div>
        ))}
      </div>
      <button className="sf-spine-link" onClick={() => go("paragraph")}>
        <I.Edit size={11} /> 在 03 一段话里编辑脊柱
      </button>
    </div>
  );
}
