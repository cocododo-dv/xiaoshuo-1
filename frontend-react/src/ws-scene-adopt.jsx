import React from "react";
import { I } from "./icons.jsx";
import { WsDialog } from "./ws-dialog.jsx";
import { wsToast } from "./ws-notify.jsx";
import { CloseButton, IconButton, Notice, Tag } from "./ws-ui.jsx";
import { ContentSafetyReviewDialog, contentSafetyReviewFromError } from "./wr-content-safety-review.jsx";
import { scnAdoptToDoc, scnPrepareAdoption } from "./ws-scene-api.js";
import { scnRunSave } from "./ws-scene-store.js";

const { useEffect, useRef, useState } = React;

/* ==========================================================
   AI 起草台 — 采用（归档或存为候选）
   · useSceneAdoption：「采纳并归档」先核对服务器上的作者稿：写作器里已有作者正文就先看差异（默认存为候选），
     没有就直接归档；内容安全复核、防双击锁、切场后不弹旧场的决策都在这里
   · AdoptionProtectDialog：作者稿保护对话框（差异 + 存为候选 / 替换并归档）
   · SceneAdoptionNote：采用结果的一句话（可打开「同步与恢复」）
   ========================================================== */

/* 「同步与恢复」在侧栏底部；外壳听这个事件打开它（detail {id} 定位到一份候选，{sid} 定位到这一场）。 */
function openRecoveryCenter(detail) {
  window.dispatchEvent(new CustomEvent("ws:recovery-open", { detail: detail || null }));
}

function useSceneAdoption({ items, runs, setRuns, pickedId, pickedIdRef, mountedRef, onArchived, go }) {
  const [decision, setDecision] = useState(null);           // 作者稿存在时的安全采用决策 { sc, r, preview }
  const [busy, setBusy] = useState("");                     // "" | "candidate" | "overwrite" | "archive"
  const [previewBusy, setPreviewBusy] = useState(false);
  /* 采用结果的一句话：{ text, tone, recovery }——recovery=true 时给「打开同步与恢复」 */
  const [note, setNote] = useState(null);
  const [safetyReview, setSafetyReview] = useState(null);
  const [safetyError, setSafetyError] = useState("");
  const epochRef = useRef(0);
  const previewLocks = useRef(new Set());
  const commitLocks = useRef(new Set());
  const sceneOf = (id) => items.find(x => x.id === id) || null;

  useEffect(() => () => { epochRef.current += 1; }, []);
  /* 换场：旧场的对话框、提示和忙碌态一概不带过来；还在进行的采用由锁挡住重复点击 */
  useEffect(() => {
    epochRef.current += 1;
    setPreviewBusy(previewLocks.current.has(pickedId));
    setBusy(commitLocks.current.has(pickedId) ? "archive" : "");
    setDecision(null);
    setNote(null);
    setSafetyReview(null);
    setSafetyError("");
  }, [pickedId]);

  /* 采用（归档或存为候选）。返回 scnAdoptToDoc 的结果（被拦下 / 出错时返回 null）。 */
  const commit = async (sc, r, mode = "overwrite", options = {}) => {
    if (!sc || commitLocks.current.has(sc.id)) return null;
    commitLocks.current.add(sc.id);
    const epoch = epochRef.current;
    const isCurrentTarget = () => mountedRef.current && epochRef.current === epoch && pickedIdRef.current === sc.id;
    const askSafetyReview = (review, reviewOptions) => {
      if (!isCurrentTarget()) return;
      setDecision(null);
      setNote(null);
      setSafetyError("");
      setSafetyReview({ review, sc, r, mode, options: reviewOptions });
    };
    if (isCurrentTarget()) {
      setBusy(mode);
      setNote(null);
    }
    try {
      // 归档单入口仍由后端裁决；若作者稿存在，overwrite 路径会先落一份持久本地备份。
      const res = await scnAdoptToDoc(sc.sid, r.draft, r.gate, { mode, ...options });
      if (!res.ok) {
        const review = contentSafetyReviewFromError(res.error);
        if (review) {
          askSafetyReview(review, {
            ...options,
            ...(res.authorBackup && res.authorBackup.id ? { authorBackupId: res.authorBackup.id } : {}),
          });
          return null;
        }
        if (isCurrentTarget()) setNote({ tone: "danger", text: `采用未完成：${res.reason || "请稍后重试"}` });
        return null;
      }
      if (res.archived === false) {
        if (isCurrentTarget()) {
          setDecision(null);
          setNote(res.warning
            ? { tone: "warn", recovery: true, text: `AI 稿没有覆盖作者正文；${res.warning}。` }
            : { tone: "ok", recovery: true, text: "AI 稿已存为候选，作者正文没有被改动。可以在侧栏底部的「同步与恢复」里比较、复制或恢复。" });
        }
        return res;
      }
      const nr = { ...r, state: "archived", justArchived: true, archivedAt: new Date().toLocaleString("zh-CN") };
      if (mountedRef.current) setRuns(m => ({ ...m, [sc.id]: nr }));
      scnRunSave(sc.sid, nr);
      if (isCurrentTarget()) {
        if (onArchived) onArchived();
        setDecision(null);
        setSafetyReview(null);
        setSafetyError("");
        // 成稿门的不拦警告（用了参考书的专名等）随归档结果说一句：归档照常，语气降一档
        const gateNotes = Array.isArray(res.gateNotes) ? res.gateNotes.filter(Boolean) : [];
        const tail = gateNotes.length ? ` ${gateNotes.join(" ")}` : "";
        const tone = gateNotes.length ? "warn" : "ok";
        setNote(res.authorBackup
          ? { tone, recovery: true, text: `已归档；覆盖前的作者稿已自动备份到「同步与恢复」。${tail}` }
          : { tone, text: `已归档并写入正文文档。${tail}` });
      }
      return res;
    } catch (error) {
      const review = contentSafetyReviewFromError(error);
      if (review) askSafetyReview(review, options);
      else if (isCurrentTarget()) setNote({ tone: "danger", text: `采用未完成：${(error && error.message) || "请稍后重试"}` });
      return null;
    } finally {
      commitLocks.current.delete(sc.id);
      if (mountedRef.current && pickedIdRef.current === sc.id) setBusy("");
    }
  };

  /* 「采纳并归档」：先核对服务器上的作者稿，有作者正文就交给保护对话框，没有就直接归档 */
  const onArchive = async () => {
    const sc = sceneOf(pickedId);
    if (!sc) return;
    const r = runs[sc.id];
    if (!r || !r.draft || r.state !== "ready") return;
    if (previewLocks.current.has(sc.id) || commitLocks.current.has(sc.id)) return;
    const epoch = epochRef.current;
    const isCurrentTarget = () => mountedRef.current && epochRef.current === epoch && pickedIdRef.current === sc.id;
    previewLocks.current.add(sc.id);
    if (isCurrentTarget()) setPreviewBusy(true);
    try {
      const preview = await scnPrepareAdoption(sc.sid, r.draft);
      if (!isCurrentTarget()) return;
      if (preview.hasReal) {
        setDecision({ sc, r, preview });
        return;
      }
      await commit(sc, r, "overwrite");
    } catch (error) {
      if (isCurrentTarget()) setNote({ tone: "danger", text: (error && error.message) || "无法核对作者稿，已停止采用" });
    } finally {
      previewLocks.current.delete(sc.id);
      if (mountedRef.current && pickedIdRef.current === sc.id) setPreviewBusy(false);
    }
  };

  /* 待复核稿「存为候选并去写作台」：过去的「送写作台深改」只是跳到写作台，AI 稿还留在起草台的缓存里，
     作者在写作台看到的是自己的空白页。现在先把稿存进「同步与恢复」（不碰作者正文、不归档），
     再打开写作台这一场，并直接打开那份候选——比较、复制或恢复都在那里。 */
  const candidateToWriter = async () => {
    const sc = sceneOf(pickedId);
    const r = sc ? runs[sc.id] : null;
    if (!sc || !r || !Array.isArray(r.draft) || !r.draft.length || !go) return;
    const res = await commit(sc, r, "candidate");
    if (!res || !res.ok || res.archived !== false) return;
    const target = res.candidate && res.candidate.id ? { id: res.candidate.id } : { sid: sc.sid };
    /* 核对作者稿可能要一会儿：这期间作者点去了别的场、或离开了起草台，就不再把人拽进写作台——
       候选照样存下（在「同步与恢复」里），只给一条带「打开同步与恢复」的提示。 */
    if (!mountedRef.current || pickedIdRef.current !== sc.id) {
      wsToast({
        tone: "ok",
        message: `「${sc.title || sc.n}」的 AI 稿已存为候选，作者正文没有改动`,
        action: { label: "打开同步与恢复", onClick: () => openRecoveryCenter(target) },
      });
      return;
    }
    go("writer", [{ type: "ws:writer-scene", detail: sc.sid }]);
    openRecoveryCenter(target);
  };

  return {
    busy,
    archiveBusy: previewBusy || Boolean(busy),
    note,
    clearNote: () => setNote(null),
    onArchive,
    candidateToWriter: go ? candidateToWriter : null,
    decision,
    closeDecision: () => { if (!busy) setDecision(null); },
    commit,
    safetyReview,
    safetyError,
    setSafetyError,
    closeSafetyReview: () => {
      if (!busy) {
        setSafetyReview(null);
        setSafetyError("");
      }
    },
  };
}

/* 采用结果的一句话；存成候选或备份过作者稿时给「打开同步与恢复」 */
function SceneAdoptionNote({ note, sid, onClose }) {
  if (!note) return null;
  return (
    <Notice
      tone={note.tone || "ok"}
      className="scn2-adopt-note"
      icon={I.ShieldCheck}
      actions={(
        <>
          {note.recovery && (
            <button type="button" className="btn btn-ghost btn-sm" onClick={() => openRecoveryCenter({ sid })}>打开同步与恢复</button>
          )}
          <CloseButton label="关闭采用提示" onClick={onClose} />
        </>
      )}
    >
      {note.text}
    </Notice>
  );
}

/* 作者稿保护：写作器里已有作者正文时，AI 稿不直接覆盖——先看差异，默认存为候选。 */
function AdoptionProtectDialog({ decision, busy, message, onClose, onCandidate, onOverwrite }) {
  const [confirmed, setConfirmed] = useState(false);
  const safeRef = useRef(null);
  const titleId = React.useId();
  const descId = React.useId();
  const preview = decision.preview || {};
  const diff = preview.diff || { paras: [], adds: 0, dels: 0 };
  const sc = decision.sc || {};

  return (
    <WsDialog
      onClose={onClose}
      onBeforeClose={() => !busy}
      labelledBy={titleId}
      describedBy={descId}
      size="lg"
      className="scn2-adopt"
      initialFocus={safeRef}
      testId="scene-adopt-dialog"
    >
      <div className="ws-dialog-head">
        <div className="scn2-adopt-headmain">
          <h2 className="ws-dialog-title" id={titleId}>写作器里已有作者正文</h2>
          <p className="ws-dialog-desc" id={descId}>AI 稿不会直接覆盖。先看差异，再决定把它存为候选，还是明确替换并归档。</p>
        </div>
        <IconButton icon="X" label="关闭" onClick={onClose} disabled={!!busy} className="ws-dialog-x" />
      </div>

      <div className="ws-dialog-body scn2-adopt-body">
        <div className="scn2-adopt-summary">
          <span className="scn2-adopt-where">{sc.n}{sc.title ? `「${sc.title}」` : ""}</span>
          <span className="is-del">作者稿将替换 {diff.dels} 句</span>
          <span className="is-add">AI 稿新增 {diff.adds} 句</span>
        </div>
        <div className="scn2-adopt-diff" aria-label="作者正文与 AI 稿差异" tabIndex={0}>
          <div className="scn2-adopt-legend"><span className="is-del">作者当前稿</span><span className="is-add">AI 候选稿</span></div>
          {diff.paras.length ? diff.paras.map((para, index) => (
            <p key={`${para.p}-${index}`}>
              {para.segs.map((seg, segIndex) => <span key={`${seg.t}-${segIndex}`} className={`is-${seg.t}`}>{seg.text}</span>)}
            </p>
          )) : <div className="scn2-adopt-no-diff">两份正文内容一致；仍需由你决定是否归档。</div>}
        </div>

        <div className="scn2-adopt-choices">
          <section className="scn2-adopt-choice is-safe">
            <div className="scn2-adopt-choice-head"><I.FileText size={15} /><strong>存为候选</strong><Tag tone="ok">推荐</Tag></div>
            <p>AI 稿存进侧栏底部的「同步与恢复」，之后可以比较、复制或恢复；作者正文和归档状态都不变。</p>
            <button ref={safeRef} type="button" className="btn btn-accent" onClick={onCandidate} disabled={!!busy} data-testid="scene-save-candidate">
              <I.Save size={14} /> {busy === "candidate" ? "正在保存…" : "存为候选"}
            </button>
          </section>
          <section className="scn2-adopt-choice is-overwrite">
            <div className="scn2-adopt-choice-head"><I.AlertTriangle size={15} /><strong>替换并归档</strong><Tag tone="danger">会改写作者稿</Tag></div>
            <p>先自动备份当前作者稿，再由后端归档、写入 AI 稿。备份失败时不会覆盖。</p>
            <label className="scn2-adopt-confirm"><input type="checkbox" checked={confirmed} onChange={(event) => setConfirmed(event.target.checked)} /> <span>我已看过差异，确认用 AI 稿替换当前正文</span></label>
            <button type="button" className="btn btn-danger" onClick={onOverwrite} disabled={!confirmed || !!busy} data-testid="scene-confirm-overwrite">
              <I.Check size={14} /> {busy === "overwrite" ? "正在备份并归档…" : "确认覆盖并归档"}
            </button>
          </section>
        </div>
        <div className="scn2-adopt-live" role="status" aria-live="polite">{message || "默认的安全选项是存为候选。"}</div>
      </div>
    </WsDialog>
  );
}

/* 采用流程里的两个对话框：作者稿保护，以及后端要求逐项确认的内容安全复核 */
function SceneAdoptionDialogs({ adoption }) {
  const { decision, safetyReview, busy } = adoption;
  return (
    <>
      {decision && (
        <AdoptionProtectDialog
          decision={decision}
          busy={busy}
          message={adoption.note && adoption.note.text}
          onClose={adoption.closeDecision}
          onCandidate={() => adoption.commit(decision.sc, decision.r, "candidate")}
          onOverwrite={() => adoption.commit(decision.sc, decision.r, "overwrite", { confirmed: true })}
        />
      )}
      {safetyReview && (
        <ContentSafetyReviewDialog
          review={safetyReview.review}
          busy={Boolean(busy)}
          error={adoption.safetyError}
          onCancel={adoption.closeSafetyReview}
          onConfirm={async (acceptedWarningCodes) => {
            const expected = safetyReview.review.findings.map(item => item.code);
            if (acceptedWarningCodes.length !== expected.length || expected.some(code => !acceptedWarningCodes.includes(code))) {
              adoption.setSafetyError("请逐项核对当前服务端返回的全部风险提示后再继续。");
              return;
            }
            adoption.setSafetyError("");
            await adoption.commit(safetyReview.sc, safetyReview.r, safetyReview.mode, {
              ...safetyReview.options,
              confirmed: true,
              acceptedWarningCodes,
            });
          }}
        />
      )}
    </>
  );
}

export { useSceneAdoption, SceneAdoptionNote, SceneAdoptionDialogs };
