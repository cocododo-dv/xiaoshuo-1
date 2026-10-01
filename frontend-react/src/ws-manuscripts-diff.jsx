import React from "react";
import { I } from "./icons.jsx";
import { dayTimeLabel } from "./lib/format.js";
import { Notice } from "./ws-ui.jsx";
import { WrDocVersions } from "./wr-doc-versions.js";

/* ==========================================================
   对比：一场正文的两个历史版本逐句比对。
   数据源是写作台的版本 store（wr-doc-versions.js 的 WrDocVersions：list / paras / diff，ES 导入）——
   成稿中心只读它，不另存版本。以前读 window.WrDocVersions：它要等门面 wr-doc-store.jsx 被写作台、AI 起草台
   或侧栏底部懒加载的同步与恢复载进来才有，还没载进来时「对比」就说这一场还没有历史版本。
   直接 import 版本模块、不经门面：门面还登记目录装载后的跟随与预热（会替在写那一场发作者稿 ensure）、写 window，
   那是写作台 store 的事，成稿中心不带进来。
   ========================================================== */

const { useEffect, useRef, useState } = React;

/* 同一场的版本列表（第一页）同一时刻只拉一次：开发模式下 React 会把挂载 effect 连跑两遍。
   列表只读——没有作者稿就是没有版本，不会替这一场建一份（重评 R15a）；版本多时分页，「更早的版本」接着往下取（批准 #8）。 */
const listInflight = new Map();
function listVersions(versions, sid) {
  if (!listInflight.has(sid)) {
    const pending = Promise.resolve()
      .then(() => versions.list(sid))
      .finally(() => { if (listInflight.get(sid) === pending) listInflight.delete(sid); });
    listInflight.set(sid, pending);
  }
  return listInflight.get(sid);
}

/* 读版本失败时给作者的话：认得的错误码说人话，其余用服务端给的说明 */
function versionErrorText(error, fallback) {
  if (error && error.code === "IDEMPOTENCY_REQUEST_IN_PROGRESS") return "上一次读取还没结束，稍等一下再点「重试」。";
  return (error && error.message) || fallback;
}

/* 版本下拉里的一项：「v3 · 9/21 14:05 · 1,200 字」（时间文案契约在 lib/format.js：非法入参给空串） */
function versionLabel(v) {
  return `v${v.revisionNo} · ${dayTimeLabel(v.at) || "—"}${v.words ? ` · ${v.words} 字` : ""}`;
}

function ManuDiff({ picked, chapter }) {
  const scenes = (chapter && chapter.scenes) || [];
  const [sid, setSid] = useState(() => (scenes[0] ? scenes[0].sid : null));
  const [vers, setVers] = useState(null);   // null = 列表加载中
  const [nextCursor, setNextCursor] = useState(null);   // 还有更早的版本时是下一页的游标
  const [moreBusy, setMoreBusy] = useState(false);
  const [selNew, setSelNew] = useState(null);
  const [selOld, setSelOld] = useState(null);
  const [diff, setDiff] = useState(null);
  const [historyError, setHistoryError] = useState("");
  const [diffError, setDiffError] = useState("");
  const [historyRetry, setHistoryRetry] = useState(0);
  const [diffRetry, setDiffRetry] = useState(0);
  /* 版本列表的「代」：换一场 / 重试就是新的一代。「更早的版本」读回来时代已经换了，这一页、它的失败和它的「读取中…」
     都不属于眼下这一份列表——过去换场时只丢了这一页，忙碌状态却留着，按钮在每一场都卡在「读取中…」（复核 Q1c-R2） */
  const listGen = useRef(0);

  useEffect(() => {
    if (scenes.length && !scenes.some((s) => s.sid === sid)) setSid(scenes[0].sid);
  }, [picked && picked.id, scenes.length]); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    let on = true;
    listGen.current += 1;
    setVers(null); setNextCursor(null); setMoreBusy(false); setDiff(null); setSelNew(null); setSelOld(null); setHistoryError(""); setDiffError("");
    if (!sid) { setVers([]); return undefined; }
    listVersions(WrDocVersions, sid).then((page) => {
      if (!on) return;
      const items = (page && page.items) || [];
      setVers(items);
      setNextCursor((page && page.nextCursor) || null);
      if (items.length >= 2) { setSelNew(items[0].revisionNo); setSelOld(items[1].revisionNo); }
    }).catch((error) => {
      if (!on) return;
      setVers([]);
      setHistoryError(versionErrorText(error, "版本历史加载失败。"));
    });
    return () => { on = false; };
  }, [sid, historyRetry]);

  useEffect(() => {
    let on = true;
    setDiff(null); setDiffError("");
    if (!sid || selNew == null || selOld == null) return undefined;
    Promise.all([WrDocVersions.paras(sid, selOld), WrDocVersions.paras(sid, selNew)])
      .then(([a, b]) => { if (on) setDiff(WrDocVersions.diff(a, b)); })
      .catch((error) => {
        if (!on) return;
        setDiff(null);
        setDiffError(versionErrorText(error, "两个版本比对失败。"));
      });
    return () => { on = false; };
  }, [sid, selNew, selOld, diffRetry]);

  /* 接着取更早的一页，接在列表后面（已选的两版不动） */
  const loadMore = () => {
    if (!sid || !nextCursor || moreBusy) return;
    const gen = listGen.current;
    const current = () => gen === listGen.current;
    setMoreBusy(true);
    WrDocVersions.list(sid, { cursor: nextCursor }).then((page) => {
      if (!current()) return;
      const older = (page && page.items) || [];
      setVers((prev) => {
        const seen = new Set((prev || []).map((v) => v.revisionNo));
        return (prev || []).concat(older.filter((v) => !seen.has(v.revisionNo)));
      });
      setNextCursor((page && page.nextCursor) || null);
    }).catch((error) => {
      if (current()) setHistoryError(versionErrorText(error, "更早的版本加载失败。"));
    }).finally(() => { if (current()) setMoreBusy(false); });
  };

  const ready = vers && vers.length >= 2;
  const pickNew = (n) => {
    setSelNew(n);
    if (selOld != null && selOld >= n) {
      const older = vers.find((v) => v.revisionNo < n);
      setSelOld(older ? older.revisionNo : null);
    }
  };
  return (
    <div className="ms-diff">
      <div className="ms-diff-head">
        <span className="ms-diff-lab">对比</span>
        {scenes.length > 1 && (
          <select className="select ms-diff-select" aria-label="选择场景" value={sid || ""} onChange={(e) => setSid(e.target.value)}>
            {scenes.map((s, i) => <option key={s.sid} value={s.sid}>第 {i + 1} 场 · {s.title}</option>)}
          </select>
        )}
        {ready && (
          <>
            <select className="select ms-diff-select" aria-label="新版本" value={selNew ?? ""} onChange={(e) => pickNew(Number(e.target.value))}>
              {vers.map((v) => (
                <option key={v.revisionNo} value={v.revisionNo}>
                  {v.revisionNo === vers[0].revisionNo ? `${versionLabel(v)} · 当前` : versionLabel(v)}
                </option>
              ))}
            </select>
            <span className="ms-diff-vs">对照</span>
            <select className="select ms-diff-select" aria-label="旧版本" value={selOld ?? ""} onChange={(e) => setSelOld(Number(e.target.value))}>
              {vers.filter((v) => selNew == null || v.revisionNo < selNew).map((v) => (
                <option key={v.revisionNo} value={v.revisionNo}>{versionLabel(v)}</option>
              ))}
            </select>
            {nextCursor && (
              <button type="button" className="btn btn-ghost btn-sm" data-testid="manuscript-diff-more" disabled={moreBusy} onClick={loadMore}>
                {moreBusy ? "读取中…" : "更早的版本"}
              </button>
            )}
            {diff && <span className="ms-diff-stat"><span className="d-add-dot" />+{diff.adds} 句</span>}
            {diff && <span className="ms-diff-stat"><span className="d-del-dot" />−{diff.dels} 句</span>}
          </>
        )}
      </div>
      <div className="ms-diff-body">
        {vers === null && <p className="ms-diff-wait">正在加载版本历史…</p>}
        {historyError && (
          <Notice tone="danger" className="ms-diff-notice"
            actions={<button className="btn btn-ghost btn-sm" type="button" data-testid="manuscript-diff-history-retry" onClick={() => setHistoryRetry((n) => n + 1)}><I.Refresh size={13} /> 重试</button>}>
            {historyError}
          </Notice>
        )}
        {!historyError && vers && vers.length < 2 && (
          <p className="ms-diff-wait">这一场还没有可对比的历史版本——在写作台再保存一次正文，这里就会出现两个版本。</p>
        )}
        {ready && !diff && !diffError && <p className="ms-diff-wait">正在比对两个版本…</p>}
        {diffError && (
          <Notice tone="danger" className="ms-diff-notice"
            actions={<button className="btn btn-ghost btn-sm" type="button" data-testid="manuscript-diff-retry" onClick={() => setDiffRetry((n) => n + 1)}><I.Refresh size={13} /> 重试</button>}>
            {diffError}
          </Notice>
        )}
        {diff && diff.paras.map((pg, k) => (
          <p key={k} className="ms-diff-p">
            {pg.segs.map((sg, x) => (
              <span key={x} className={sg.t === "same" ? "d-same" : sg.t === "del" ? "d-del" : "d-add"}>{sg.text}</span>
            ))}
          </p>
        ))}
      </div>
    </div>
  );
}

export { ManuDiff };
