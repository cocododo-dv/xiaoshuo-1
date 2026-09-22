import React from "react";
import { I } from "./icons.jsx";
import { CloseButton, Notice, Spinner, Tag } from "./ws-ui.jsx";
import { scnFindingIsPlainLanguage, scnFindingText } from "./ws-scene-derive.js";

const { useEffect, useState } = React;

/* ==========================================================
   AI 起草台 — 裁决条（台面底部）
   待起草：开始起草（或追加预算）· 运行中：等待 · 待复核：采纳并归档 / 退回重写 / 存为候选并去写作台
   · 等你终选：只有一句等待（终选在台面上做）· 已归档：去写作台或成稿中心。
   裁决只认后端 author_state：这里不再有前端自己的判词。
   ========================================================== */

/* 退回重写的快捷短语：从后端质检给的改法里取（rewrite_brief 与带说明的质量建议），不再是四句写死的原型文案。
   没有汉字的条目（后端透出的英文 issue_key、异常原文）不当作给作者的一句话摆出来。 */
function reworkChipsFor(scene) {
  const out = [];
  const add = (text) => {
    const value = String(text || "").trim();
    if (value && scnFindingIsPlainLanguage(value) && !out.includes(value)) out.push(value);
  };
  String(scene.rewriteBrief || "").split(/[；;\n]+/).forEach(add);
  const gate = scene.gate || {};
  [...(gate.blocking || []), ...(gate.warnings || [])].map(scnFindingText).forEach(add);
  return out.slice(0, 4);
}

function verdictOf(scene) {
  const gate = scene.gate || null;
  if (scene.budgetBlock) return { tone: "warn", label: "需要追加预算" };
  if (!gate) return { tone: "neutral", label: "待你裁决" };
  if (gate.authorState === "hard_blocked") return { tone: "danger", label: `${gate.blocking.length || 1} 条硬问题` };
  if (gate.authorState === "quality_warning") return { tone: "warn", label: `${gate.warnings.length || 1} 条建议` };
  if (gate.canArchive === false) return { tone: "neutral", label: "暂不能归档" };
  return { tone: "ok", label: "可以归档" };
}

function DecisionBar({ scene, state, runJobStatus, go, onEditPlan = null, onArchive, onRun, onBudgetTopup, onCandidateToWriter = null, archiveBusy = false }) {
  const [rework, setRework] = useState(false);
  const [note, setNote] = useState("");
  const normalizedNoteLength = Array.from(note.trim()).length;
  const noteTooLong = normalizedNoteLength > 2000;
  useEffect(() => { setRework(false); setNote(""); }, [scene.id]);

  const openWriter = (posture) => go && go("writer", [
    { type: "ws:writer-scene", detail: scene.sid },
    ...(posture ? [{ type: "ws:writer-posture", detail: posture }] : []),
  ]);

  if (state === "queued") {
    if (runJobStatus === "queued") {
      return (
        <div className="scn2-decide is-wait">
          <div className="scn2-decide-sum"><I.Clock size={14} /> 任务已排队，等后端开始起草</div>
          <div className="scn2-decide-acts">
            <button className="btn btn-ghost btn-sm" type="button" disabled>等待运行</button>
          </div>
        </div>
      );
    }
    return (
      <div className="scn2-decide">
        <div className={`scn2-decide-sum${scene.error ? " is-error" : ""}`}>
          {scene.error
            ? <><I.AlertTriangle size={14} /> <span>{scene.error}</span></>
            : <><I.Compass size={14} /> <span>AI 按上面这张设计卡和已确认的构思起草，写完交给你裁决</span></>}
        </div>
        <div className="scn2-decide-acts">
          {/* 阶段 Y：雪花整理出来的场，设计在构思第 10 步改；手加的场的卡在章节编排里改 */}
          {onEditPlan
            ? <button className="btn btn-quiet btn-sm" data-testid="scene-decide-edit-plan" onClick={onEditPlan} title="这一场是雪花整理出来的：设计在构思第 10 步改，确认后这张卡自动跟上">在构思里改</button>
            : <button className="btn btn-quiet btn-sm" onClick={() => go && go("author")} title="场景卡在章节编排里维护">编辑场景卡</button>}
          {scene.budgetBlock && onBudgetTopup
            ? <button className="btn btn-accent" data-testid="scene-budget-topup" onClick={() => onBudgetTopup()}><I.Plus size={13} /> 追加预算并继续</button>
            : <button className="btn btn-accent" data-testid="scene-start" onClick={() => onRun && onRun("")}><I.Play size={13} /> 开始起草</button>}
        </div>
      </div>
    );
  }

  if (state === "running") {
    return (
      <div className="scn2-decide is-wait">
        <div className="scn2-decide-sum"><Spinner size={13} /> AI 正在起草，写完后在这里裁决</div>
      </div>
    );
  }

  if (state === "archived") {
    return (
      <div className="scn2-decide is-done">
        <div className="scn2-decide-sum"><I.CheckCircle size={14} /> 已写入写作台正文，场景卡已置「完成」</div>
        <div className="scn2-decide-acts">
          <button className="btn btn-quiet btn-sm" onClick={() => go && go("manuscripts")}>在成稿中心查看</button>
          <button className="btn btn-ghost btn-sm" onClick={() => openWriter("deep")}><I.Microscope size={13} /> 在写作台深改</button>
          <button className="btn btn-accent btn-sm" onClick={() => openWriter()}><I.Pen size={13} /> 在写作台打开</button>
        </div>
      </div>
    );
  }

  // ready → 真正的裁决时刻
  const gate = scene.gate || null;
  /* 候选终选暂停（Best-of-N 关键场景）：后端答 can_archive=false、没有任何阻断条目——这不是硬问题。
     过去这里照样亮红色「无法继续：已证实的硬问题」，还给一个会丢掉终选、另起一轮的「按硬问题重写」。 */
  if (gate && gate.authorState === "awaiting_author_choice") {
    return (
      <div className="scn2-decide is-wait" data-testid="scene-awaiting-choice">
        <div className="scn2-decide-sum"><I.Users size={14} /> 等你终选一稿，选中后管线自动续跑</div>
      </div>
    );
  }
  const budgetBlocked = !!scene.budgetBlock;
  const hardBlocked = !!(gate && gate.authorState === "hard_blocked" && !budgetBlocked);
  const archiveBlocked = budgetBlocked || hardBlocked || !!(gate && gate.canArchive === false);
  const verdict = verdictOf(scene);
  const hardReasons = hardBlocked ? gate.blocking.map(scnFindingText).filter(scnFindingIsPlainLanguage) : [];
  const chips = rework ? reworkChipsFor(scene) : [];
  return (
    <div className="scn2-decide-wrap">
      {/* 待复核时的失败也要说出来：追加预算没成、退回重写的指令被拒收，过去只是按钮闪一下就没了下文 */}
      {scene.error && (
        <Notice tone="danger" className="scn2-decide-note" testId="scene-ready-error">{scene.error}</Notice>
      )}
      {budgetBlocked && (
        <Notice tone="warn" className="scn2-decide-note" title={scene.budgetBlock.label}
          actions={<button className="btn btn-accent btn-sm" data-testid="scene-budget-topup" onClick={() => onBudgetTopup && onBudgetTopup()}><I.Plus size={13} /> 追加预算并继续</button>}>
          已有正文和持久化的恢复点都保留着，追加后从断点继续。
        </Notice>
      )}
      {hardBlocked && (
        <Notice tone="danger" className="scn2-decide-note" title={`有 ${gate.blocking.length || 1} 条已证实的硬问题，这一稿暂不能归档`}
          actions={(
            <button
              className="btn btn-accent btn-sm"
              data-testid="scene-hard-rewrite"
              disabled={!onRun}
              onClick={() => onRun && onRun(
                scene.rewriteBrief
                || gate.blocking.map(f => scnFindingText(f) || f.issue_key || f.kind).filter(Boolean).join("；")
                || "按已证实的 Q0/Q1 硬问题修正正文，逐项满足场景卡约束后重新复检。"
              )}
            ><I.Refresh size={13} /> 按硬问题重写并复检</button>
          )}>
          {hardReasons.length ? hardReasons.slice(0, 2).join("；") : "正文已保留；按问题重写，或存为候选后自己在写作台接手。"}
        </Notice>
      )}
      {rework && (
        <div className="scn2-rework">
          <div className="scn2-rework-head">
            <I.Refresh size={13} /><span>退回重写</span>
            <CloseButton label="收起退回重写" size="xs" className="scn2-rework-x" onClick={() => setRework(false)} />
          </div>
          {chips.length > 0 && (
            <div className="scn2-rework-chips" aria-label="后端质检给的改法">
              {chips.map(c => (
                <button key={c} type="button" className="scn2-rework-chip" title={c} onClick={() => setNote(n => n ? n + "；" + c : c)}>{c}</button>
              ))}
            </div>
          )}
          <textarea
            className="scn2-rework-input" rows={2}
            placeholder="写给 AI 的改写指令，例如：保留第 5 段的节奏，把最后一句的比喻收得更克制。不写就按原设计卡重跑。"
            value={note} onChange={e => setNote(e.target.value)}
            aria-label="改写指令"
            aria-invalid={noteTooLong ? "true" : undefined}
            aria-describedby="scene-author-note-limit"
          />
          <div className="scn2-rework-foot">
            <span
              id="scene-author-note-limit"
              className={`scn2-rework-hint${noteTooLong ? " is-error" : ""}`}
              role={noteTooLong ? "alert" : undefined}
            >
              {noteTooLong
                ? `作者指令 ${normalizedNoteLength} / 2000 字；请精简，系统不会静默截断。`
                : `作者指令 ${normalizedNoteLength} / 2000 字 · 将保留为第 ${(scene.attempt || 1) + 1} 次尝试`}
            </span>
            <button className="btn btn-accent btn-sm" onClick={() => { if (onRun) { onRun(note); setRework(false); } }} disabled={noteTooLong || !onRun}>
              <I.Refresh size={13} /> {note.trim() ? "确认退回重写" : "直接重跑"}
            </button>
          </div>
        </div>
      )}
      <div className="scn2-decide is-ready">
        <div className="scn2-decide-verdict">
          <Tag tone={verdict.tone} className="scn2-verdict-badge">{verdict.label}</Tag>
          <span className="scn2-verdict-meta">采纳后写入写作台正文，字数回写目录</span>
        </div>
        <div className="scn2-decide-acts">
          <button className={`btn btn-quiet btn-sm ${rework ? "is-on" : ""}`} aria-expanded={rework} onClick={() => setRework(r => !r)}><I.Refresh size={13} /> 退回重写</button>
          {onCandidateToWriter && (
            <button className="btn btn-ghost btn-sm" onClick={onCandidateToWriter} disabled={archiveBusy}
              title="把这一稿存进「同步与恢复」（不改作者正文、不归档），然后到写作台这一场接着改">
              <I.Pen size={13} /> 存为候选并去写作台
            </button>
          )}
          <button
            className="btn btn-accent"
            data-testid="scene-archive"
            onClick={onArchive}
            disabled={archiveBlocked || archiveBusy}
            aria-busy={archiveBusy ? "true" : undefined}
            title={archiveBusy ? "正在核对作者正文与归档状态，请稍候" : (budgetBlocked ? "生命周期预算用完了，追加后续跑才能归档" : (hardBlocked ? "有已证实的硬问题，暂不能归档（正文已保留）" : (archiveBlocked ? "后端暂不允许归档这一稿" : undefined)))}
          ><I.Check size={14} /> {archiveBusy ? "正在核对作者稿…" : (budgetBlocked ? "需续跑完成" : (hardBlocked ? "需处理硬问题" : "采纳并归档"))}</button>
        </div>
      </div>
    </div>
  );
}

export { DecisionBar };
