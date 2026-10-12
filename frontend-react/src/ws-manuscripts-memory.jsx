import React from "react";
import { I } from "./icons.jsx";
import { apiGet, apiPost } from "./lib/client.js";
import { wsConfirm } from "./ws-notify.jsx";
import { Notice, Spinner, Tag } from "./ws-ui.jsx";

/* ==========================================================
   成稿中心 · 记忆页签（故事记忆，2026-10-12）
   ----------------------------------------------------------
   起草后面各章时，模型从远处只看得到两样东西，都在后台自动维护（backend services/story_memory.py）：
   · 章摘要：一章写完（各场都有终稿，或你已经往后写了）之后 AI 写一份——发生了什么、章末停在哪、
     人物的持久变化、还没收的线（一章章往下带）。后面各章起草时它就是「前情」。
   · 事实抽取：每场定稿之后自动抽进事件日志；没核对的也进起草，你在「正史」里驳回的不进。
   这里看这一章的那一份、改它（改过的不再被自动重写，正文再变只标「待更新」）、或让 AI 重写。
   · GET  /api/v1/projects/{pid}/story-memory —— 各章的摘要与状态，外加事实抽取还欠几场；
   · POST …/chapters/{cid}/digest {base_row_id, digest} —— 存你改过的（编辑期间后台换了一份 → 409，刷新再改）；
   · POST …/chapters/{cid}/digest/regenerate —— 让 AI 重写（没配模型 409、调用失败 502）。
   ========================================================== */

const { useCallback, useEffect, useState } = React;

export const MEMORY_STATUS_META = {
  fresh: { label: "已是最新", tone: "ok" },
  stale: { label: "正文改过，待更新", tone: "warn" },
  missing: { label: "还没写", tone: "info" },
  not_ready: { label: "本章还没写完", tone: "neutral" },
};

const memoryPath = (projectId) => `/api/v1/projects/${encodeURIComponent(projectId)}/story-memory`;
const digestPath = (projectId, chapterId) => `${memoryPath(projectId)}/chapters/${encodeURIComponent(chapterId)}/digest`;

const lines = (text) => String(text || "").split("\n").map((line) => line.trim()).filter(Boolean);
const splitPair = (line) => {
  const match = line.match(/^(.+?)[：:](.+)$/);
  return match ? [match[1].trim(), match[2].trim()] : [line, ""];
};

/* 摘要 → 编辑框里的文字（列表一行一条，「人物：变化」「线：怎么收的」） */
export function digestToDraft(digest) {
  const d = digest || {};
  return {
    summary: d.summary || "",
    ending_state: d.ending_state || "",
    character_changes: (d.character_changes || []).map((item) => `${item.character}：${item.change}`).join("\n"),
    open_threads: (d.open_threads || []).map((item) => item.thread).join("\n"),
    resolved_threads: (d.resolved_threads || []).map((item) => `${item.thread}：${item.resolution}`).join("\n"),
  };
}

/* 编辑框 → 请求体里的摘要。线索保留原来的「第几章起」，新加的记在这一章。 */
export function draftToDigest(draft, previous, chapterNumber) {
  const openedIn = new Map(((previous && previous.open_threads) || []).map((item) => [item.thread, item.opened_in]));
  return {
    summary: String(draft.summary || "").trim(),
    ending_state: String(draft.ending_state || "").trim(),
    character_changes: lines(draft.character_changes)
      .map(splitPair)
      .filter(([character, change]) => character && change)
      .map(([character, change]) => ({ character, change })),
    open_threads: lines(draft.open_threads).map((thread) => ({
      thread,
      opened_in: openedIn.get(thread) || chapterNumber || null,
    })),
    resolved_threads: lines(draft.resolved_threads)
      .map(splitPair)
      .filter(([thread, resolution]) => thread && resolution)
      .map(([thread, resolution]) => ({ thread, resolution })),
  };
}

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
      <dt>至今未收的线</dt>
      <dd>
        {digest.open_threads.length ? (
          <ul>{digest.open_threads.map((item, index) => <li key={index}>{item.thread}{item.opened_in ? <span className="ms-memory-since">第 {item.opened_in} 章起</span> : null}</li>)}</ul>
        ) : "（没有）"}
      </dd>
      {digest.resolved_threads.length > 0 && (
        <>
          <dt>这一章收掉的线</dt>
          <dd><ul>{digest.resolved_threads.map((item, index) => <li key={index}><b>{item.thread}</b>：{item.resolution}</li>)}</ul></dd>
        </>
      )}
    </dl>
  );
}

function DigestEditor({ draft, setDraft }) {
  const field = (key) => (event) => setDraft({ ...draft, [key]: event.target.value });
  return (
    <div className="ms-canon-manual-grid">
      <label className="is-wide"><span>发生了什么（必填）</span><textarea value={draft.summary} onChange={field("summary")} rows={4} /></label>
      <label className="is-wide"><span>章末停在哪</span><textarea value={draft.ending_state} onChange={field("ending_state")} rows={2} /></label>
      <label><span>人物的持久变化（一行一条：人物：变化）</span><textarea value={draft.character_changes} onChange={field("character_changes")} rows={4} /></label>
      <label><span>至今未收的线（一行一条）</span><textarea value={draft.open_threads} onChange={field("open_threads")} rows={4} /></label>
      <label className="is-wide"><span>这一章收掉的线（一行一条：线：怎么收的）</span><textarea value={draft.resolved_threads} onChange={field("resolved_threads")} rows={2} /></label>
    </div>
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

  const run = async (key, action, note) => {
    if (busy) return;
    setBusy(key);
    setFeedback({ error: "", note: "" });
    try {
      await action();
      setFeedback({ error: "", note });
      setDraft(null);
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
  const save = () => {
    const digest = draftToDigest(draft, entry.digest, entry.number);
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
          后面各章起草时，模型从这里读「前情」：每章写完后 AI 自动整理一份摘要，未收的线一章章往下带；每场定稿后的事实也会自动抽进事件日志（在「正史」里驳回的不用）。
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
      </div>
    </div>
  );
}
