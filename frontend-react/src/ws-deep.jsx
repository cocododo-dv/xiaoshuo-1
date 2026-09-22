import React from "react";
import { I } from "./icons.jsx";
import { wsKey } from "./ws-works.jsx";
import { apiGet, apiPatch } from "./lib/client.js";
import { CloseButton, IconButton } from "./ws-ui.jsx";
import { useWrInert } from "./ws-writer-hooks.js";

/* ==========================================================
   ws-deep — 深改引擎 + 写作台深改面板（原独立「深改台」并入写作台）
   ----------------------------------------------------------
   · wrDeepScan(el, sid)      诊断当前场正文 → issues
   · wrDeepMark / wrDeepUnmark  在编辑器里标注 / 清除风险高亮
   · WrDeepDrawer             写作台右栏的诊断面板（问题 + 决定日志）
   诊断是本机的启发式规则，只指出问题、不代笔：每一项都给「选中这一句去改写」，
   回到起草姿态、选中那一句，改写走选区工具条那条真实的改写接口。
   （过去这里有一条「采纳候选 · 写回正文」的通路，可诊断从不产出候选，那条路永远走不到。）
   姿态本身（进出、偏好同步、选中去改写）在 ws-writer-deep-posture.js。
   ========================================================== */

const dxKey = (base) => (wsKey ? wsKey(base) : base);

/* ---- 决定日志 / 忽略清单（按场景持久化）---- */
function wrDxLog(sid) {
  try { return JSON.parse(localStorage.getItem(dxKey("wr-deep-log:" + sid))) || []; } catch (e) { return []; }
}
function wrDxPushLog(sid, text) {
  const list = [{ at: Date.now(), text }, ...wrDxLog(sid)].slice(0, 30);
  try { localStorage.setItem(dxKey("wr-deep-log:" + sid), JSON.stringify(list)); } catch (e) {}
  return list;
}
function wrDxSkips(sid) {
  try { return new Set(JSON.parse(localStorage.getItem(dxKey("wr-deep-skip:" + sid))) || []); } catch (e) { return new Set(); }
}
function wrDxAddSkip(sid, key) {
  const s = wrDxSkips(sid); s.add(key);
  try { localStorage.setItem(dxKey("wr-deep-skip:" + sid), JSON.stringify([...s])); } catch (e) {}
}
function wrDxClearSkips(sid) {
  try { localStorage.removeItem(dxKey("wr-deep-skip:" + sid)); } catch (e) {}
}

function wrDxSnapshot(sid) {
  return { decision_log: wrDxLog(sid), ignored_issue_keys: [...wrDxSkips(sid)] };
}

function wrDxApplyPreferences(sid, preferences) {
  const decisionLog = Array.isArray(preferences?.decision_log) ? preferences.decision_log.slice(0, 30) : [];
  const ignoredKeys = Array.isArray(preferences?.ignored_issue_keys)
    ? [...new Set(preferences.ignored_issue_keys)].slice(0, 200)
    : [];
  try { localStorage.setItem(dxKey("wr-deep-log:" + sid), JSON.stringify(decisionLog)); } catch (e) {}
  try { localStorage.setItem(dxKey("wr-deep-skip:" + sid), JSON.stringify(ignoredKeys)); } catch (e) {}
  return { decision_log: decisionLog, ignored_issue_keys: ignoredKeys };
}

function wrDxMergePreferences(remote, local, { localIgnoredAuthoritative = false } = {}) {
  const seenLogs = new Set();
  const decisionLog = [...(local?.decision_log || []), ...(remote?.decision_log || [])]
    .filter((entry) => {
      const key = `${entry?.at ?? ""}:${entry?.text ?? ""}`;
      if (!entry?.text || seenLogs.has(key)) return false;
      seenLogs.add(key);
      return true;
    })
    .sort((a, b) => Number(b.at || 0) - Number(a.at || 0))
    .slice(0, 30);
  const ignoredSource = localIgnoredAuthoritative
    ? (local?.ignored_issue_keys || [])
    : [...(local?.ignored_issue_keys || []), ...(remote?.ignored_issue_keys || [])];
  return {
    decision_log: decisionLog,
    ignored_issue_keys: [...new Set(ignoredSource)].slice(0, 200),
  };
}

async function wrDxLoadPreferences(sid) {
  return apiGet(`/api/v1/scenes/${encodeURIComponent(sid)}/deep-review/preferences`);
}

async function wrDxSavePreferences(sid, snapshot, baseRevisionNo) {
  return apiPatch(`/api/v1/scenes/${encodeURIComponent(sid)}/deep-review/preferences`, {
    decision_log: (snapshot?.decision_log || []).slice(0, 30),
    ignored_issue_keys: [...new Set(snapshot?.ignored_issue_keys || [])].slice(0, 200),
    base_revision_no: baseRevisionNo,
  });
}


const WR_DX_KINDS = {
  echo: { label: "回响" },
  vague: { label: "抽象" },
  dump: { label: "堆叠" },
  rdn: { label: "重复" },
};
const WR_DX_SEV = { high: "重", mid: "中", low: "轻" };

/* ---- 诊断：启发式规则 ---- */
function wrDeepScan(el, sid) {
  if (!el) return [];
  const paras = Array.from(el.querySelectorAll("p, blockquote"));
  const texts = paras.map(p => (p.textContent || "").trim());
  const issues = [];
  const skips = wrDxSkips(sid);

  texts.forEach((t, pid) => {
    if (!t) return;
    /* 贴邻叠句：「安静，安静到」式回响 */
    const m = t.match(/([\u4e00-\u9fa5]{2,5})([，、；]?)\1/);
    if (m && !issues.some(it => it.pid === pid && it.find && it.find.includes(m[0]))) {
      issues.push({
        key: "echo:" + pid + ":" + m[1], kind: "echo", sev: "mid", pid, find: m[0],
        title: `「${m[1]}${m[2] || ""}${m[1]}」贴邻重复`,
        hint: "短语回响节奏偏刻意，考虑改换连接或删一处。",
      });
    }
    /* 段落偏长 */
    if (t.length > 170 && !issues.some(it => it.pid === pid && it.kind === "dump")) {
      issues.push({
        key: "dump:" + pid, kind: "dump", sev: "low", pid,
        title: `第 ${pid + 1} 段偏长（${t.length} 字）`,
        hint: "单段信息密度偏高，考虑拆段或删减一件物事。",
      });
    }
    /* 连续三句同字开头 */
    const sents = t.split(/(?<=[。！？!?])/).filter(s => s.trim());
    for (let i = 0; i + 2 < sents.length; i++) {
      const c = sents[i].trim()[0];
      if (c && sents[i + 1].trim()[0] === c && sents[i + 2].trim()[0] === c) {
        if (!issues.some(it => it.pid === pid && it.kind === "rdn")) issues.push({
          key: "rdn:" + pid + ":" + c, kind: "rdn", sev: "low", pid,
          title: `连续三句以「${c}」开头`,
          hint: "句首重复读起来平，考虑改写其中一句的主语或语序。",
        });
        break;
      }
    }
  });

  const sevRank = { high: 0, mid: 1, low: 2 };
  return issues.filter(it => !skips.has(it.key))
    .sort((a, b) => (sevRank[a.sev] ?? 2) - (sevRank[b.sev] ?? 2) || a.pid - b.pid);
}

/* ---- 编辑器内标注 ---- */
function wrDeepUnmark(el) {
  if (!el) return;
  el.querySelectorAll("mark.wr-dx").forEach(mk => {
    const parent = mk.parentNode;
    while (mk.firstChild) parent.insertBefore(mk.firstChild, mk);
    parent.removeChild(mk);
    parent.normalize();
  });
  el.querySelectorAll(".wr-dx-para").forEach(p => {
    p.classList.remove("wr-dx-para", "is-active");
    p.removeAttribute("data-dx");
  });
}
function wrDeepMark(el, issues, activeKey) {
  if (!el) return;
  wrDeepUnmark(el);
  const paras = Array.from(el.querySelectorAll("p, blockquote"));
  issues.forEach(it => {
    const p = paras[it.pid];
    if (!p) return;
    let wrapped = false;
    if (it.find) {
      const walker = document.createTreeWalker(p, NodeFilter.SHOW_TEXT);
      let node;
      while ((node = walker.nextNode())) {
        const i = node.nodeValue.indexOf(it.find);
        if (i >= 0) {
          try {
            const range = document.createRange();
            range.setStart(node, i); range.setEnd(node, i + it.find.length);
            const mk = document.createElement("mark");
            mk.className = `wr-dx k-${it.kind}${it.key === activeKey ? " is-active" : ""}`;
            mk.setAttribute("data-dx", it.key);
            range.surroundContents(mk);
            wrapped = true;
          } catch (e) {}
          break;
        }
      }
    }
    if (!wrapped) {
      p.classList.add("wr-dx-para");
      if (it.key === activeKey) p.classList.add("is-active");
      p.setAttribute("data-dx", it.key);
    }
  });
}

/* ==========================================================
   WrDeepDrawer — 写作台右栏 · 深改面板
   ========================================================== */
function WrDeepDrawer({ open, issues, activeKey, onPick, onIgnore, onRescan, onSelect, log, persistenceStatus = "idle", onClose }) {
  const active = issues.find(it => it.key === activeKey) || issues[0] || null;
  const asideRef = React.useRef(null);
  /* 收起时只是移出画面：标 inert，Tab 不会走进看不见的按钮 */
  useWrInert(asideRef, !open);
  const syncNote = persistenceStatus === "loading" || persistenceStatus === "saving" ? "决定正在同步…"
    : persistenceStatus === "synced" ? "决定已同步到服务器。"
    : persistenceStatus === "local" ? "服务器暂时连不上，决定先存在本机，下次操作时再同步。"
    : "";
  return (
    <aside ref={asideRef} className={`wr-drawer right wr-dxd ${open ? "show" : ""}`} aria-label="深改诊断">
      <header className="wr-drawer-head">
        <span className="wr-drawer-title"><I.Microscope size={15} /> 深改诊断 {issues.length} 项</span>
        <div className="wr-dxd-head-acts">
          <IconButton icon="Refresh" label="重新诊断本场（找回忽略过的项）" onClick={onRescan} />
          <CloseButton className="wr-drawer-x" label="收起深改诊断" onClick={onClose} />
        </div>
      </header>

      <div className="wr-dxd-scroll">
        {issues.length === 0 ? (
          <div className="wr-dxd-clear">
            <I.CheckCircle size={22} />
            <div className="wr-dxd-clear-t">本场没有发现待改的句段</div>
            <p>可以回到起草姿态继续写，或者换一场再诊断。</p>
          </div>
        ) : (
          <>
            <ul className="wr-dxd-list">
              {issues.map(it => {
                const k = WR_DX_KINDS[it.kind] || WR_DX_KINDS.rdn;
                const on = !!(active && active.key === it.key);
                return (
                  <li key={it.key}>
                    <button type="button" className={`wr-dxd-row ${on ? "is-active" : ""}`} aria-pressed={on} onClick={() => onPick(it.key)}>
                      <span className={`wr-dxd-mark sev-${it.sev}`} title={`严重程度：${WR_DX_SEV[it.sev] || "轻"}`}>{k.label}</span>
                      <span className="wr-dxd-body">
                        <span className="wr-dxd-t">{it.title}</span>
                        <span className="wr-dxd-h">{it.hint}</span>
                      </span>
                    </button>
                  </li>
                );
              })}
            </ul>

            {active && (
              <div className="wr-dxd-cands">
                <div className="wr-dxd-sub">这一项怎么处理</div>
                <div className="wr-dxd-row-acts">
                  {onSelect && (
                    <button type="button" className="btn btn-ghost btn-sm" onClick={() => onSelect({ pid: active.pid, find: active.find || "" })}>
                      <I.Pen size={13} /> {active.find ? "选中这一句去改写" : "选中这一段去改写"}
                    </button>
                  )}
                  <button type="button" className="btn btn-quiet btn-sm" onClick={() => onIgnore(active)}>忽略这一项</button>
                </div>
              </div>
            )}
          </>
        )}

        {log && log.length > 0 && (
          <div className="wr-dxd-log">
            <div className="wr-dxd-sub">最近的决定</div>
            <ul>
              {log.slice(0, 6).map((d, i) => (
                <li key={i} className="wr-dxd-dec">
                  <span className="wr-dxd-dec-t">{new Date(d.at).toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit" })}</span>
                  <span className="wr-dxd-dec-x">{d.text}</span>
                </li>
              ))}
            </ul>
          </div>
        )}

        <div className="wr-dxd-note">
          诊断是本机的启发式规则，只指出问题、不替你改字。忽略的条目不再提示，点右上角的重新诊断可以找回。
          {syncNote ? <> {syncNote}</> : null}
        </div>
      </div>
    </aside>
  );
}

export {
  wrDeepScan, wrDeepMark, wrDeepUnmark,
  wrDxLog, wrDxPushLog, wrDxAddSkip, wrDxClearSkips, wrDxSnapshot,
  wrDxApplyPreferences, wrDxMergePreferences, wrDxLoadPreferences, wrDxSavePreferences,
  WrDeepDrawer,
};
