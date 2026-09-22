import React from "react";
import { I } from "./icons.jsx";
import { dayTimeLabel } from "./lib/ago.js";
import { Notice } from "./ws-ui.jsx";

/* ==========================================================
   对比：一场正文的两个历史版本逐句比对。
   数据源是写作台的版本 store（window.WrDocVersions：list / paras / diff）——
   成稿中心只读它，不另存版本。
   ========================================================== */

const { useEffect, useState } = React;

/* 同一场的版本列表同一时刻只拉一次。list 第一次会先 POST ensure（没有作者稿就建一份空稿），
   开发模式下 React 会把挂载 effect 连跑两遍：两个 ensure 带着同一个幂等键撞在一起，后一个拿到
   409 IDEMPOTENCY_REQUEST_IN_PROGRESS，作者第一次点开「对比」就看到一句英文报错。 */
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

/* 版本下拉里的一项：「v3 · 9/21 14:05 · 1,200 字」（时间文案契约在 lib/ago.js：非法入参给空串） */
function versionLabel(v) {
  return `v${v.revisionNo} · ${dayTimeLabel(v.at) || "—"}${v.words ? ` · ${v.words} 字` : ""}`;
}

function ManuDiff({ picked, chapter }) {
  const scenes = (chapter && chapter.scenes) || [];
  const [sid, setSid] = useState(() => (scenes[0] ? scenes[0].sid : null));
  const [vers, setVers] = useState(null);   // null = 列表加载中
  const [selNew, setSelNew] = useState(null);
  const [selOld, setSelOld] = useState(null);
  const [diff, setDiff] = useState(null);
  const [historyError, setHistoryError] = useState("");
  const [diffError, setDiffError] = useState("");
  const [historyRetry, setHistoryRetry] = useState(0);
  const [diffRetry, setDiffRetry] = useState(0);

  useEffect(() => {
    if (scenes.length && !scenes.some((s) => s.sid === sid)) setSid(scenes[0].sid);
  }, [picked && picked.id, scenes.length]); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    let on = true;
    setVers(null); setDiff(null); setSelNew(null); setSelOld(null); setHistoryError(""); setDiffError("");
    const versions = window.WrDocVersions;
    if (!sid || !versions) { setVers([]); return undefined; }
    listVersions(versions, sid).then((items) => {
      if (!on) return;
      setVers(items);
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
    const versions = window.WrDocVersions;
    if (!sid || selNew == null || selOld == null || !versions) return undefined;
    Promise.all([versions.paras(sid, selOld), versions.paras(sid, selNew)])
      .then(([a, b]) => { if (on) setDiff(versions.diff(a, b)); })
      .catch((error) => {
        if (!on) return;
        setDiff(null);
        setDiffError(versionErrorText(error, "两个版本比对失败。"));
      });
    return () => { on = false; };
  }, [sid, selNew, selOld, diffRetry]);

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
