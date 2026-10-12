import React from "react";
import { I } from "./icons.jsx";
import { apiGet, apiPost } from "./lib/client.js";
import { wsConfirm } from "./ws-notify.jsx";
import { Notice, Spinner, Tag } from "./ws-ui.jsx";

/* ==========================================================
   成稿中心 · 记忆页签（故事记忆，2026-10-12）
   ----------------------------------------------------------
   起草后面各章时，模型从远处看得到的东西都在后台自动维护（backend services/story_memory.py、story_editorial.py）：
   · 章摘要：一章写完（各场都有终稿，或你已经往后写了）之后 AI 写一份——发生了什么、章末停在哪、
     人物的持久变化，以及这一章埋下 / 推进 / 兑现了哪几条线。后面各章起草时它就是「前情」。
   · 线索台账（全书）：由各章摘要的线索更新叠出来；冷了、计划兑现的章已过会提醒。你可以把一条线「放下」
     （不再提醒、不进起草）或判它「已收」。
   · 编辑通读（全书）：每写完三章 AI 通读一遍，写问题清单与给后面几章的意见；你采纳的意见进那几章的起草。
   · 事实抽取：每场定稿之后自动抽进事件日志；没核对的也进起草，你在「正史」里驳回的不进。
   接口：
   · GET  /api/v1/projects/{pid}/story-memory —— 各章摘要与状态、台账、编辑通读，外加事实抽取还欠几场；
   · POST …/chapters/{cid}/digest {base_row_id, digest} —— 存你改过的文字（编辑期间后台换了一份 → 409）；
   · POST …/chapters/{cid}/digest/regenerate —— 让 AI 重写（没配模型 409、调用失败 502）；
   · POST …/threads/{thread_id}/mark {status: dropped | resolved | open} —— 标记一条线；
   · POST …/editorial-review —— 现在通读一次；
   · POST …/editorial-guidance/{gid}/decision {decision: accept | dismiss, text?} —— 采纳（可改字）/ 不采纳。
   ========================================================== */

const { useCallback, useEffect, useState } = React;

export const MEMORY_STATUS_META = {
  fresh: { label: "已是最新", tone: "ok" },
  stale: { label: "正文改过，待更新", tone: "warn" },
  missing: { label: "还没写", tone: "info" },
  not_ready: { label: "本章还没写完", tone: "neutral" },
};

export const THREAD_UPDATE_LABELS = { opened: "埋下", advanced: "推进", resolved: "兑现" };

export const FINDING_KIND_LABELS = {
  arc_stall: "人物弧停滞",
  promise_unpaid: "承诺没兑现",
  thread_cold: "线索冷了",
  pacing: "节奏",
  continuity: "前后矛盾",
  plan_drift: "正文偏离计划",
  repetition: "重复",
  other: "其他",
};

const SEVERITY_META = {
  high: { label: "严重", tone: "danger" },
  medium: { label: "中等", tone: "warn" },
  low: { label: "轻微", tone: "neutral" },
};

const GUIDANCE_STATUS_META = {
  proposed: { label: "待你决定", tone: "info" },
  accepted: { label: "已采纳", tone: "ok" },
  dismissed: { label: "没采纳", tone: "neutral" },
  superseded: { label: "已作废", tone: "neutral" },
};

const memoryPath = (projectId) => `/api/v1/projects/${encodeURIComponent(projectId)}/story-memory`;
const digestPath = (projectId, chapterId) => `${memoryPath(projectId)}/chapters/${encodeURIComponent(chapterId)}/digest`;
const threadMarkPath = (projectId, threadId) => `${memoryPath(projectId)}/threads/${encodeURIComponent(threadId)}/mark`;
const guidanceDecisionPath = (projectId, guidanceId) =>
  `${memoryPath(projectId)}/editorial-guidance/${encodeURIComponent(guidanceId)}/decision`;

const lines = (text) => String(text || "").split("\n").map((line) => line.trim()).filter(Boolean);
const splitPair = (line) => {
  const match = line.match(/^(.+?)[：:](.+)$/);
  return match ? [match[1].trim(), match[2].trim()] : [line, ""];
};

/* 摘要 → 编辑框里的文字（人物变化一行一条「人物：变化」）。线索不在这里改：用台账里的「放下 / 已收」。 */
export function digestToDraft(digest) {
  const d = digest || {};
  return {
    summary: d.summary || "",
    ending_state: d.ending_state || "",
    character_changes: (d.character_changes || []).map((item) => `${item.character}：${item.change}`).join("\n"),
  };
}

/* 编辑框 → 请求体里的摘要（这一章的线索更新由后端原样带过来）。 */
export function draftToDigest(draft) {
  return {
    summary: String(draft.summary || "").trim(),
    ending_state: String(draft.ending_state || "").trim(),
    character_changes: lines(draft.character_changes)
      .map(splitPair)
      .filter(([character, change]) => character && change)
      .map(([character, change]) => ({ character, change })),
  };
}

/* 一条线的位置：第几章埋下、最近推进、计划兑现（台账行） */
export function threadWhere(thread) {
  const parts = [`第 ${thread.opened_in} 章埋下`];
  if (thread.last_touched_in && thread.last_touched_in !== thread.opened_in) parts.push(`第 ${thread.last_touched_in} 章最近推进`);
  if (thread.resolved_in) parts.push(`第 ${thread.resolved_in} 章兑现`);
  else if (thread.planned_payoff_in) parts.push(`计划第 ${thread.planned_payoff_in} 章兑现`);
  return parts.join(" · ");
}

export function threadAlertText(thread) {
  if (thread.alert === "cold") return `已经 ${thread.dormant_chapters} 章没碰`;
  if (thread.alert === "payoff_missed") return `计划第 ${thread.planned_payoff_in} 章兑现，还没兑现`;
  return "";
}

const guidanceTarget = (item, review) => {
  if (item.chapter_number) return `第 ${item.chapter_number} 章`;
  const numbers = (item.window || (review && review.window ? review.window.map((w) => w.number) : [])).filter(Boolean);
  return numbers.length ? `第 ${numbers.join("、")} 章` : "后面几章";
};

function DigestView({ digest }) {
  return (
    <dl className="ms-memory-digest">
      <dt>发生了什么</dt>
      <dd>{digest.summary}</dd>
      {digest.ending_state && (<><dt>章末停在哪</dt><dd>{digest.ending_state}</dd></>)}
      {digest.character_changes.length > 0 && (
        <>
          <dt>人物的持久变化</dt>
          <dd><ul>{digest.character_changes.map((item, index) => <li key={index}><b>{item.character}</b>：{item.change}</li>)}</ul></dd>
        </>
      )}
      <dt>这一章的线索</dt>
      <dd>
        {(digest.threads || []).length ? (
          <ul>
            {digest.threads.map((item) => (
              <li key={item.id}>
                <span className="ms-memory-since">{THREAD_UPDATE_LABELS[item.status] || item.status}</span> {item.thread}
                {item.note ? `：${item.note}` : ""}
              </li>
            ))}
          </ul>
        ) : "（没有埋下或推进任何线）"}
      </dd>
    </dl>
  );
}

function DigestEditor({ draft, setDraft }) {
  const field = (key) => (event) => setDraft({ ...draft, [key]: event.target.value });
  return (
    <div className="ms-canon-manual-grid">
      <label className="is-wide"><span>发生了什么（必填）</span><textarea value={draft.summary} onChange={field("summary")} rows={4} /></label>
      <label className="is-wide"><span>章末停在哪</span><textarea value={draft.ending_state} onChange={field("ending_state")} rows={2} /></label>
      <label className="is-wide"><span>人物的持久变化（一行一条：人物：变化）</span><textarea value={draft.character_changes} onChange={field("character_changes")} rows={4} /></label>
      <p className="ms-memory-hint is-wide">线索不在这里改：在下面的「线索台账」里放下或判已收。</p>
    </div>
  );
}

function ThreadRow({ thread, busy, onMark }) {
  const alert = threadAlertText(thread);
  return (
    <li className="ms-memory-thread" data-testid="manuscript-memory-thread">
      <div className="ms-memory-thread-main">
        <span className="ms-memory-thread-label">{thread.thread}</span>
        {alert && <Tag tone="warn" dot>{alert}</Tag>}
        {thread.mark === "dropped" && <Tag tone="neutral" outline>你放下了</Tag>}
        {thread.mark === "resolved" && <Tag tone="ok" outline>你判为已收</Tag>}
      </div>
      <div className="ms-memory-thread-meta">{threadWhere(thread)}{thread.mark_note ? ` · ${thread.mark_note}` : ""}</div>
      <div className="ms-canon-actions">
        {thread.open ? (
          <>
            <button className="btn btn-ghost btn-sm" type="button" disabled={!!busy} onClick={() => onMark(thread, "dropped")}>放下</button>
            <button className="btn btn-ghost btn-sm" type="button" disabled={!!busy} onClick={() => onMark(thread, "resolved")}><I.Check size={13} /> 已收</button>
          </>
        ) : thread.mark ? (
          <button className="btn btn-ghost btn-sm" type="button" disabled={!!busy} onClick={() => onMark(thread, "open")}>取消标记</button>
        ) : null}
      </div>
    </li>
  );
}

function ThreadLedger({ ledger, busy, onMark }) {
  const open = ledger.open || [];
  const closed = ledger.closed || [];
  return (
    <section className="ms-canon-scene" data-testid="manuscript-memory-ledger">
      <header>
        <div>
          <span className="ms-canon-scene-no">全书</span>
          <strong>线索台账</strong>
          {ledger.alert_count > 0 && <Tag tone="warn" dot>{ledger.alert_count} 条要留意</Tag>}
        </div>
      </header>
      <div className="ms-memory-body">
        {open.length ? (
          <ul className="ms-memory-thread-list">{open.map((thread) => <ThreadRow key={thread.thread_id} thread={thread} busy={busy} onMark={onMark} />)}</ul>
        ) : (
          <p className="ms-memory-empty">现在没有还开着的线。章摘要会记下每章埋下、推进、兑现了哪几条。</p>
        )}
        {closed.length > 0 && (
          <details className="ms-memory-closed">
            <summary>已经收掉或放下的 {closed.length} 条</summary>
            <ul className="ms-memory-thread-list">{closed.map((thread) => <ThreadRow key={thread.thread_id} thread={thread} busy={busy} onMark={onMark} />)}</ul>
          </details>
        )}
      </div>
    </section>
  );
}

function GuidanceRow({ item, review, busy, editing, setEditing, onDecide }) {
  const meta = GUIDANCE_STATUS_META[item.status] || GUIDANCE_STATUS_META.proposed;
  const isEditing = editing && editing.id === item.guidance_id;
  const text = item.author_text || item.text;
  const decidable = item.status !== "superseded";
  return (
    <li className="ms-memory-guidance" data-testid="manuscript-memory-guidance">
      <div className="ms-memory-thread-main">
        <span className="ms-memory-since">{guidanceTarget(item, review)}</span>
        <Tag tone={meta.tone} dot>{meta.label}</Tag>
        {item.author_text && <Tag tone="accent" outline>你改过</Tag>}
      </div>
      {isEditing ? (
        <textarea className="ms-memory-guidance-edit" value={editing.text} rows={3} onChange={(event) => setEditing({ ...editing, text: event.target.value })} />
      ) : (
        <p className="ms-memory-guidance-text">{text}</p>
      )}
      {item.reason && !isEditing && <div className="ms-memory-thread-meta">为什么：{item.reason}</div>}
      {decidable && (
        <div className="ms-canon-actions">
          {isEditing ? (
            <>
              <button className="btn btn-ghost btn-sm" type="button" disabled={!!busy} onClick={() => setEditing(null)}><I.X size={13} /> 取消</button>
              <button className="btn btn-primary btn-sm" type="button" disabled={!!busy || !editing.text.trim()} onClick={() => onDecide(item, "accept", editing.text)}>
                <I.Check size={13} /> 采纳这一版
              </button>
            </>
          ) : (
            <>
              {item.status !== "accepted" && (
                <button className="btn btn-primary btn-sm" type="button" disabled={!!busy} onClick={() => onDecide(item, "accept")} data-testid="manuscript-memory-guidance-accept">
                  <I.Check size={13} /> 采纳
                </button>
              )}
              <button className="btn btn-ghost btn-sm" type="button" disabled={!!busy} onClick={() => setEditing({ id: item.guidance_id, text })}>
                <I.Edit size={13} /> {item.status === "accepted" ? "改字" : "改后采纳"}
              </button>
              {item.status !== "dismissed" && (
                <button className="btn btn-ghost btn-sm" type="button" disabled={!!busy} onClick={() => onDecide(item, "dismiss")} data-testid="manuscript-memory-guidance-dismiss">
                  {item.status === "accepted" ? "撤销采纳" : "不采纳"}
                </button>
              )}
            </>
          )}
        </div>
      )}
    </li>
  );
}

function EditorialPanel({ editorial, llmEnabled, busy, onRun, onDecide }) {
  const [editing, setEditing] = useState(null);
  const latest = editorial.latest;
  const latestIds = new Set(((latest && latest.guidance) || []).map((item) => item.guidance_id));
  const carried = (editorial.accepted || []).filter((item) => !latestIds.has(item.guidance_id));
  const decide = (item, decision, text) => { setEditing(null); onDecide(item, decision, text); };
  return (
    <section className="ms-canon-scene" data-testid="manuscript-memory-editorial">
      <header>
        <div>
          <span className="ms-canon-scene-no">全书</span>
          <strong>编辑通读</strong>
          {latest && <Tag tone="neutral" outline>读到第 {latest.through_chapter_number || "?"} 章 · {latest.origin === "auto" ? "自动" : "你让它读的"}</Tag>}
        </div>
        {editorial.can_run && llmEnabled && (
          <button className="btn btn-ghost btn-sm" type="button" disabled={!!busy} onClick={onRun} data-testid="manuscript-memory-review-run">
            {busy === "review" ? <Spinner size={13} /> : <I.BookOpen size={13} />} {busy === "review" ? "通读中，可能要一两分钟…" : "现在通读"}
          </button>
        )}
      </header>
      <div className="ms-memory-body">
        {latest ? (
          <>
            <p className="ms-memory-assessment">{latest.assessment}</p>
            {latest.findings.length > 0 && (
              <ul className="ms-memory-thread-list">
                {latest.findings.map((finding, index) => {
                  const severity = SEVERITY_META[finding.severity] || SEVERITY_META.medium;
                  return (
                    <li key={index} className="ms-memory-finding">
                      <div className="ms-memory-thread-main">
                        <Tag tone={severity.tone} dot>{severity.label}</Tag>
                        <strong>{FINDING_KIND_LABELS[finding.kind] || FINDING_KIND_LABELS.other}</strong>
                        {finding.chapters.length > 0 && <span className="ms-memory-since">第 {finding.chapters.join("、")} 章</span>}
                      </div>
                      <p className="ms-memory-guidance-text">{finding.finding}</p>
                      {finding.suggestion && <div className="ms-memory-thread-meta">建议：{finding.suggestion}</div>}
                    </li>
                  );
                })}
              </ul>
            )}
            {latest.guidance.length > 0 && (
              <>
                <h4 className="ms-memory-subhead">给后面几章的意见（采纳的会进那几章的起草）</h4>
                <ul className="ms-memory-thread-list">
                  {latest.guidance.map((item) => (
                    <GuidanceRow key={item.guidance_id} item={item} review={latest} busy={busy} editing={editing} setEditing={setEditing} onDecide={decide} />
                  ))}
                </ul>
              </>
            )}
          </>
        ) : (
          <p className="ms-memory-empty">
            还没有通读过。每写完 {editorial.interval_chapters} 章、章摘要都整理好之后，后台会自动通读一遍{editorial.can_run ? "；也可以现在就让它读。" : "。"}
          </p>
        )}
        {carried.length > 0 && (
          <>
            <h4 className="ms-memory-subhead">以前采纳、仍在生效的意见</h4>
            <ul className="ms-memory-thread-list">
              {carried.map((item) => (
                <GuidanceRow key={item.guidance_id} item={item} review={null} busy={busy} editing={editing} setEditing={setEditing} onDecide={decide} />
              ))}
            </ul>
          </>
        )}
        {latest && (
          <p className="ms-memory-hint">下一次自动通读：写到第 {editorial.next_auto_after_chapter} 章、摘要整理好之后。{editorial.history.length ? `更早还有 ${editorial.history.length} 份通读。` : ""}</p>
        )}
      </div>
    </section>
  );
}

export function ManuMemory({ projectId, chapterId }) {
  const [load, setLoad] = useState({ status: "idle", data: null, error: null });
  const [busy, setBusy] = useState("");
  const [feedback, setFeedback] = useState({ error: "", note: "" });
  const [draft, setDraft] = useState(null);

  const refresh = useCallback(async () => {
    if (!projectId) return;
    setLoad((prev) => ({ ...prev, status: prev.data ? "refreshing" : "loading", error: null }));
    try {
      const data = await apiGet(memoryPath(projectId));
      setLoad({ status: "ready", data, error: null });
    } catch (error) {
      setLoad((prev) => ({ ...prev, status: "error", error }));
    }
  }, [projectId]);

  useEffect(() => { void refresh(); }, [refresh]);
  useEffect(() => { setDraft(null); setFeedback({ error: "", note: "" }); }, [chapterId]);

  if (!projectId || !chapterId) {
    return <div className="ms-canon-empty"><I.Info size={16} /> 这一章还没同步到服务端，暂时没有故事记忆。</div>;
  }
  if (load.status === "idle" || load.status === "loading") {
    return <div className="ms-canon-empty" role="status"><Spinner size={15} /> 正在读取故事记忆…</div>;
  }
  if (load.status === "error" && !load.data) {
    return <div className="ms-canon-empty is-error"><I.AlertTriangle size={17} /> {(load.error && load.error.message) || "故事记忆加载失败。"}</div>;
  }

  const data = load.data || {};
  const entry = (data.chapters || []).find((item) => item.chapter_id === chapterId);
  if (!entry) {
    return <div className="ms-canon-empty"><I.Info size={16} /> 这一章不在作品的章序里（可能在回收站），没有故事记忆。</div>;
  }
  const meta = MEMORY_STATUS_META[entry.status] || MEMORY_STATUS_META.not_ready;
  const extraction = data.extraction || {};
  const backlog = (extraction.pending_scene_count || 0) + (extraction.degraded_scene_count || 0);

  // keepDraft：台账 / 通读上的操作不碰正在改的章摘要
  const run = async (key, action, note, { keepDraft = false } = {}) => {
    if (busy) return;
    setBusy(key);
    setFeedback({ error: "", note: "" });
    try {
      await action();
      setFeedback({ error: "", note });
      if (!keepDraft) setDraft(null);
      await refresh();
    } catch (error) {
      setFeedback({ error: (error && error.message) || "故事记忆操作失败。", note: "" });
    } finally {
      setBusy("");
    }
  };

  const regenerate = async () => {
    if (entry.origin === "author") {
      const ok = await wsConfirm({
        title: "让 AI 重写这一章的摘要？",
        body: "这一份是你改过的；AI 重写会换成新的一份（你改的那份留在历史里，不再用于起草）。",
        confirmLabel: "让 AI 重写",
      });
      if (!ok) return;
    }
    void run("regenerate", () => apiPost(`${digestPath(projectId, chapterId)}/regenerate`, {}), "AI 已重写这一章的摘要。");
  };
  const markThread = (thread, status) => {
    const note = { dropped: "已放下这条线：不再提醒，也不进起草。", resolved: "已把这条线判为收了。", open: "已取消标记。" }[status];
    void run(`thread:${thread.thread_id}`, () => apiPost(threadMarkPath(projectId, thread.thread_id), { status }), note, { keepDraft: true });
  };
  const runReview = () => {
    void run("review", () => apiPost(`${memoryPath(projectId)}/editorial-review`, {}), "编辑通读写好了，看下面的问题与意见。", { keepDraft: true });
  };
  const decideGuidance = (item, decision, text) => {
    const body = decision === "accept" && text !== undefined ? { decision, text } : { decision };
    const note = decision === "accept" ? "已采纳：这条意见会进那几章的起草（还没写的场会按它重新规划）。" : "这条意见不再进起草。";
    void run(`guidance:${item.guidance_id}`, () => apiPost(guidanceDecisionPath(projectId, item.guidance_id), body), note, { keepDraft: true });
  };
  const save = () => {
    const digest = draftToDigest(draft);
    if (!digest.summary) {
      setFeedback({ error: "「发生了什么」不能为空。", note: "" });
      return;
    }
    void run("save", () => apiPost(digestPath(projectId, chapterId), { base_row_id: entry.digest_row_id, digest }), "已保存你改过的摘要；之后正文再变，它只会标「待更新」，不会被自动重写。");
  };

  return (
    <div className="ms-canon" data-testid="manuscript-memory">
      <div className="ms-canon-feedback">
        <Notice tone="info">
          后面各章起草时，模型从这里读「前情」：每章写完后 AI 自动整理一份摘要，并把埋下、推进、兑现的线记进全书的线索台账；每写完 {(data.editorial && data.editorial.interval_chapters) || 3} 章还会通读一遍、给后面几章提意见。每场定稿后的事实也会自动抽进事件日志（在「正史」里驳回的不用）。
          {backlog > 0 && ` 还有 ${backlog} 场的事实没抽完${extraction.degraded_scene_count ? `（${extraction.degraded_scene_count} 场上次失败）` : ""}，后台会接着做。`}
          {!data.llm_enabled && " 现在没有接入可用的模型，后台暂停。"}
        </Notice>
      </div>
      {(feedback.error || feedback.note) && (
        <div className="ms-canon-feedback"><Notice tone={feedback.error ? "danger" : "ok"}>{feedback.error || feedback.note}</Notice></div>
      )}
      <div className="ms-canon-scenes">
        <section className="ms-canon-scene">
          <header>
            <div>
              <span className="ms-canon-scene-no">第 {entry.number} 章</span>
              <strong>章摘要</strong>
              <Tag tone={meta.tone} dot testId="manuscript-memory-status">{meta.label}</Tag>
              {entry.origin === "author" && <Tag tone="accent" outline>你改过</Tag>}
            </div>
            {load.status === "refreshing" && <Spinner size={13} label="正在刷新" />}
          </header>
          <div className="ms-memory-body">
            {draft ? (
              <DigestEditor draft={draft} setDraft={setDraft} />
            ) : entry.digest ? (
              <DigestView digest={entry.digest} />
            ) : (
              <p className="ms-memory-empty">
                {entry.status === "not_ready"
                  ? `这一章还没写完（${entry.written_scene_count}/${entry.scene_count} 场有终稿）。写完、或你开始写后面的章之后，后台会自动整理。`
                  : "这一章写完了，后台会在一两分钟内整理出摘要；也可以现在就让 AI 写。"}
              </p>
            )}
            <div className="ms-canon-actions">
              {draft ? (
                <>
                  <button className="btn btn-ghost btn-sm" type="button" disabled={!!busy} onClick={() => setDraft(null)}><I.X size={13} /> 取消</button>
                  <button className="btn btn-primary btn-sm" type="button" disabled={!!busy} onClick={save} data-testid="manuscript-memory-save">
                    {busy === "save" ? <Spinner size={13} /> : <I.Save size={13} />} 保存
                  </button>
                </>
              ) : (
                <>
                  {entry.digest && (
                    <button className="btn btn-ghost btn-sm" type="button" disabled={!!busy} onClick={() => setDraft(digestToDraft(entry.digest))} data-testid="manuscript-memory-edit">
                      <I.Edit size={13} /> 修改
                    </button>
                  )}
                  {entry.eligible && (
                    <button className="btn btn-ghost btn-sm" type="button" disabled={!!busy} onClick={regenerate} data-testid="manuscript-memory-regenerate">
                      {busy === "regenerate" ? <Spinner size={13} /> : <I.Sparkles size={13} />} {entry.digest ? "让 AI 重写" : "让 AI 现在写"}
                    </button>
                  )}
                </>
              )}
            </div>
          </div>
        </section>
        {data.threads && <ThreadLedger ledger={data.threads} busy={busy} onMark={markThread} />}
        {data.editorial && (
          <EditorialPanel editorial={data.editorial} llmEnabled={!!data.llm_enabled} busy={busy} onRun={runReview} onDecide={decideGuidance} />
        )}
      </div>
    </div>
  );
}
