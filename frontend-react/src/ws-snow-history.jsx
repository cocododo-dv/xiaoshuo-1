import React from "react";
import { I } from "./icons.jsx";
import { WsDialog } from "./ws-dialog.jsx";
import { CloseButton, Notice, Tag } from "./ws-ui.jsx";
import { apiGet } from "./lib/client.js";
import { recentOrDayTimeLabel } from "./lib/format.js";
import { S2_BE_KEY, S2_STEPS, s2Ancestors, s2StepText } from "./ws-snow-model.js";
import { feFromCanon, stripFe } from "./ws-snow-canon.js";
import { countChars } from "./lib/text.js";

/* ==========================================================
   版本与来路：「历史」页签——上面是「服务器上保存的版本」（这一步在服务器上的每一版，可以预览、恢复），下面是本机的
   操作时间线（带快照的节点可回滚）；「引用上下文」页签（本步展开自哪几层上游）；以及三个对照对话框——回滚预览
   （快照 vs 当前）、服务器版本预览（那一版 vs 现在）与「上游改了什么」（本步确认时消费的上游版本 vs 现在）。
   对话框统一走 WsDialog（焦点移入、Tab 困在框内、关闭后焦点回到打开它的按钮、Esc / 遮罩走同一条关闭路径）。
   ========================================================== */

export function S2History({ history, go, onRestore }) {
  const list = history || [];
  if (!list.length) return (
    <div className="hist-empty">
      <I.Clock size={18} />
      <div>
        <div className="fw-600">还没有操作记录</div>
        <div className="text-muted text-sm">确认步骤、采纳候选、让 AI 生成或复核后，这里会留下时间线；带快照的节点可一键回滚。</div>
      </div>
    </div>
  );
  return (
    <ul className="hist">
      {list.map((h, i) => {
        const st = S2_STEPS.find(s => s.key === h.key);
        return (
          <li key={i} className="hist-row">
            <span className="hist-time">{recentOrDayTimeLabel(h.t)}</span>
            {/* 生成走的是作者在系统配置里接的任意一家模型，不是某个具体产品：旧记录里的「Claude」也显示为「AI」 */}
            <span className={`hist-who ${h.who === "Claude" || h.who === "AI" ? "is-ai" : ""}`}>{h.who === "Claude" ? "AI" : h.who}</span>
            <span className="hist-action">{h.action}</span>
            {/* 旧记录里可能留着 09 行的内部 id（row_<uuid>）——显示时换成「一场」，不把内部 id 摆给作者 */}
            <span className="hist-note">{String(h.note || "").replace(/row_[0-9a-f]{12,}/gi, "一场")}</span>
            {h.snap && onRestore && <button className="btn btn-quiet btn-sm hist-restore" onClick={() => onRestore(h)} title="把这一步回滚到此刻的内容快照"><I.Refresh size={12} /> 回滚</button>}
            {st && go && <button className="btn btn-quiet btn-sm" onClick={() => go(h.key)}>前往</button>}
          </li>
        );
      })}
    </ul>
  );
}

export function S2Ref({ active, drafts, scaffolds }) {
  const ancs = s2Ancestors(active.key).slice().reverse(); // root → nearest
  const para = (scaffolds && scaffolds.paragraph) || {};
  const pf = (para.premiseF || "").trim(), pt = (para.premiseT || "").trim();
  const clip = (s, n) => { s = (s || "").replace(/\s+/g, " ").trim(); return s.length > n ? s.slice(0, n) + "…" : s; };
  return (
    <div className="refpane">
      <div className="ref-lead text-muted text-sm">
        {ancs.length ? `本步「${active.name}」展开自下面 ${ancs.length} 层上游——保持一致。` : "这是雪花的原点，没有上游引用。"}
      </div>
      {ancs.map(k => {
        const s = S2_STEPS.find(x => x.key === k);
        // 带栏名的分步文本（批准 #18b）：不再把 09 的行 id、角色键、proactive 这些内部值印进引用卡片
        const text = clip(s2StepText(k, drafts[k], scaffolds[k], scaffolds), 160);
        return (
          <div key={k} className="card-flat ref-card">
            <div className="ref-card-h"><span className={`sf-trk-tag trk-${s.track}`}>{s.num}</span><span className="fw-600">{s.name}</span></div>
            <p className="text-serif ref-card-body">{text || <em className="text-muted">（该步尚未填写）</em>}</p>
          </div>
        );
      })}
      {(pf || pt) && (
        <div className="card-flat ref-card ref-spine">
          <div className="ref-card-h"><span className="sf-trk-tag trk-plot">脊柱</span><span className="fw-600">道德前提·中点翻转</span></div>
          <p className="ref-premise"><span className="ref-premise-f">{pf || "—"}</span><I.ArrowRight size={12} /><span className="ref-premise-t">{pt || "—"}</span></p>
        </div>
      )}
    </div>
  );
}

/* ---- 回滚预览：快照 vs 当前，看清再恢复 ----
   两边都是带栏名的分步文本（s2StepText）；视角名、04 名册、第 10 步的场序取 refs（现在的整份脚手架）。 */
export function S2SnapDiff({ h, current, refs, onApply, onClose }) {
  const st = S2_STEPS.find(s => s.key === h.key) || {};
  const oldText = s2StepText(h.key, h.snap.draft, h.snap.scaffold, refs).trim();
  const curText = s2StepText(h.key, current.draft, current.scaffold, refs).trim();
  const same = oldText === curText;
  return (
    <WsDialog onClose={onClose} labelledBy="sf-snap-title" describedBy="sf-snap-desc" size="lg" className="sf-diff-dialog">
      <header className="ws-dialog-head">
        <div>
          <h2 className="ws-dialog-title" id="sf-snap-title">回滚预览：{st.num} {st.name}</h2>
          <p className="ws-dialog-desc" id="sf-snap-desc">快照留于 {new Date(h.t).toLocaleString("zh-CN")}（{h.action}）</p>
        </div>
        <CloseButton className="ws-dialog-x" title="关闭（Esc）" onClick={onClose} />
      </header>
      <div className="ws-dialog-body">
        {same ? (
          <div className="sf-sd-same"><I.Check size={14} /> 快照与当前内容完全一致，不需要回滚。</div>
        ) : (
          <div className="sf-sd-cols">
            <div className="sf-sd-col is-old">
              <div className="sf-sd-coltag"><I.Clock size={11} /> 快照（会恢复成这一版） · {countChars(oldText)} 字</div>
              <pre className="sf-sd-text text-serif">{oldText || "（空）"}</pre>
            </div>
            <div className="sf-sd-col is-cur">
              <div className="sf-sd-coltag"><I.Pen size={11} /> 当前（会被替换，替换前另留一份） · {countChars(curText)} 字</div>
              <pre className="sf-sd-text text-serif">{curText || "（空）"}</pre>
            </div>
          </div>
        )}
      </div>
      <footer className="ws-dialog-foot">
        <span className="sf-dialog-hint">回滚前会给当前内容再留一份快照，回滚本身也能撤回。</span>
        <button className="btn btn-quiet btn-sm" onClick={onClose}>取消</button>
        <button className="btn btn-accent btn-sm" onClick={onApply} disabled={same}><I.Refresh size={13} /> 确认回滚</button>
      </footer>
    </WsDialog>
  );
}

/* ---- 阶段 E：上游改了什么——本步确认时消费的上游版本 vs 现在的版本（后端 input_refs + history） ---- */
export function S2UpstreamDiff({ diff, onClose }) {
  const st = S2_STEPS.find(s => s.key === diff.key) || {};
  const items = diff.items || [];
  return (
    <WsDialog onClose={onClose} labelledBy="sf-updiff-title" describedBy="sf-updiff-desc" size="lg" className="sf-diff-dialog" testId="snow-upstream-diff">
      <header className="ws-dialog-head">
        <div>
          <h2 className="ws-dialog-title" id="sf-updiff-title">上游改了什么：{st.num} {st.name}</h2>
          <p className="ws-dialog-desc" id="sf-updiff-desc">{diff.reason ? `后端判定失效的原因：${diff.reason}` : "左边是本步确认时用的上游版本，右边是现在的版本"}</p>
        </div>
        <CloseButton className="ws-dialog-x" title="关闭（Esc）" onClick={onClose} />
      </header>
      <div className="ws-dialog-body">
        {diff.loading ? (
          <div className="sf-sd-same is-muted" role="status"><I.Refresh size={14} className="sf-spin" /> 正在拉取上游历史…</div>
        ) : diff.error ? (
          <div className="sf-sd-same is-warn" role="alert"><I.AlertTriangle size={14} /> {diff.error}</div>
        ) : !items.length ? (
          <div className="sf-sd-same is-warn"><I.Info size={14} /> 服务端没有记下本步确认时用的上游版本（旧数据），或者上游版本没有变化——请直接回上游核对。</div>
        ) : (
          <div className="sf-updiff-list">
            {items.map(item => {
              const up = S2_STEPS.find(s => s.key === item.feKey) || {};
              return (
                <div key={item.feKey} className="sf-updiff-item" data-testid="snow-upstream-diff-item">
                  <div className="sf-updiff-head"><b>{up.num} {up.name}</b>：第 {item.oldVersion == null ? "?" : item.oldVersion} 版 → 第 {item.newVersion == null ? "?" : item.newVersion} 版{item.oldFound ? "" : "（旧版本已不在历史里）"}</div>
                  <div className="sf-sd-cols">
                    <div className="sf-sd-col is-old">
                      <div className="sf-sd-coltag"><I.Clock size={11} /> 本步确认时用的版本 · {countChars(item.oldText || "")} 字</div>
                      <pre className="sf-sd-text text-serif">{item.oldText || "（空）"}</pre>
                    </div>
                    <div className="sf-sd-col is-cur">
                      <div className="sf-sd-coltag"><I.Pen size={11} /> 现在的版本 · {countChars(item.newText || "")} 字</div>
                      <pre className="sf-sd-text text-serif">{item.newText || "（空）"}</pre>
                    </div>
                  </div>
                </div>
              );
            })}
          </div>
        )}
      </div>
      <footer className="ws-dialog-foot">
        <span className="sf-dialog-hint">看清差异后，回本步「按新上游重新展开」，或改完点「已复核」。</span>
        <button className="btn btn-quiet btn-sm" onClick={onClose}>关闭</button>
      </footer>
    </WsDialog>
  );
}

/* ==========================================================
   服务器上保存的版本（R15a）
   ----------------------------------------------------------
   每次确认、AI 生成、整步清空（抹空保护另起一版）、从历史恢复，服务器都为这一步留一版；以前只能请开发者调
   POST …/restore 取回。这里按版本列出（GET …/history 不带草稿），「预览」再按版本取草稿（step_run_id +
   include_draft），对话框里看清那一版与现在的差别再恢复。恢复本身在工作台（ws-snow-workbench.jsx 的 useSnowStepFlow）。
   ========================================================== */
const VERSION_STATUS = {
  pending_review: { label: "待确认", tone: "warn" },
  approved: { label: "已确认", tone: "ok" },
  stale: { label: "需复核", tone: "warn" },
  skipped: { label: "已略过", tone: "neutral" },
  superseded: { label: "已被新版取代", tone: "neutral" },
};
const VERSION_SOURCE = { llm: "AI 生成", author: "你写的", history_restore: "从历史恢复", skip: "略过", fallback: "旧版规则稿" };

/* 一版的状态与来历（列表行与预览对话框共用） */
export function s2VersionStatus(item) {
  const it = item || {};
  if (it.status === "stale" && it.stale_accepted_at) return { label: "已复核 · 仍有效", tone: "ok" };
  return VERSION_STATUS[it.status] || { label: "旧版本", tone: "neutral" };
}
export function s2VersionSource(item) {
  const it = item || {};
  if (it.wipe_guard_preserved_step_run_id) return "整步清空时另起的一版";
  return VERSION_SOURCE[it.generation_source] || "";
}
function versionTime(item) {
  const t = Date.parse((item && (item.updated_at || item.created_at)) || "");
  return Number.isFinite(t) ? recentOrDayTimeLabel(t) : "";
}

/* 一版服务端草稿 → 带栏名的分步文本（与回滚预览同一份写法）。第 10 步的场序与题名取那一版自己的场景行；
   07 的章表换成现在这一张——恢复 07 只恢复五段展开的文字，章表留着现在的分章（服务端 keep_live_chapter_table），
   预览里两边就是同一张章表，差别只在文字上。 */
export function s2VersionText(key, draft, refs) {
  const canon = stripFe(draft || {});
  const fe = feFromCanon(key, canon);
  const ownRefs = key === "planning" ? { ...(refs || {}), scenes: (feFromCanon("scenes", canon).scaffold || {}) } : refs;
  const scaffold = key === "outline" && fe.scaffold
    ? { ...fe.scaffold, chapters: (((refs || {}).outline || {}).chapters) || [] }
    : fe.scaffold;
  return s2StepText(key, fe.text != null ? fe.text : "", scaffold, ownRefs).trim();
}

/* 恢复前要说清的后果：07 只恢复文字（章表是分章结果，不跟着回去）；09 / 10 会动场景与整理之后的场景卡 */
const RESTORE_WARNING = {
  outline: "只恢复五段展开的文字。章节表（章名、章界、每章有哪几场）保持现在的分章——章表只在分章面板里改。",
  scenes: "场景列表会回到这一版：这一版里没有的场会从场景列表删去（已经建了场景卡的场留在目录里，等你在分章面板里决定保留还是删除），之后加的场、改过的形态与视角也一并回到这一版的样子。",
  planning: "每一场的三拍、坩埚、钩子等规划会回到这一版，之后改过的会被替换（形态与视角仍以 09 为准）。场景卡等你重新确认第 10 步之后才跟着更新。",
};

export function S2ServerVersions({ workId, step, refreshKey, onPreview }) {
  const beKey = step ? S2_BE_KEY[step.key] : "";
  const [state, setState] = React.useState({ loading: true, error: "", items: [] });
  const [tick, setTick] = React.useState(0);
  React.useEffect(() => {
    if (!workId || !beKey) { setState({ loading: false, error: "", items: [] }); return undefined; }
    let alive = true;
    setState(prev => ({ ...prev, loading: true, error: "" }));
    (async () => {
      try {
        const res = await apiGet(`/api/v2/projects/${workId}/snowflake-workspace/steps/${beKey}/history`);
        if (alive) setState({ loading: false, error: "", items: Array.isArray(res && res.items) ? res.items : [] });
      } catch (err) {
        if (alive) setState({ loading: false, error: (err && err.message) || "稍后重试", items: [] });
      }
    })();
    return () => { alive = false; };
  }, [workId, beKey, refreshKey, tick]);
  const items = state.items;
  const latest = items[0] || null;
  const preserved = latest && latest.wipe_guard_preserved_step_run_id
    ? items.find(it => it.step_run_id === latest.wipe_guard_preserved_step_run_id) || null
    : null;
  return (
    <section className="sf-versions" data-testid="snow-server-versions" aria-labelledby="sf-versions-title">
      <div className="sf-versions-head">
        <h3 className="sf-history-title" id="sf-versions-title">服务器上保存的版本</h3>
        <span className="sf-versions-step">{step.num} {step.name}</span>
      </div>
      <p className="sf-versions-lead">确认、AI 生成、整步清空、从历史恢复，服务器都会给这一步另存一版——换了浏览器也能从这里找回。</p>
      {preserved && (
        <Notice tone="warn" testId="snow-version-wipe"
          actions={<button type="button" className="btn btn-quiet btn-sm" onClick={() => onPreview(preserved)} data-testid="snow-version-wipe-open">看清空前的第 {preserved.version} 版</button>}>
          这一步最近一次保存把内容整个清空了，服务器另起了现在这一版；清空前的第 {preserved.version} 版还在，可以预览后恢复。
        </Notice>
      )}
      {state.loading && !items.length ? (
        <div className="sf-versions-empty" role="status">正在读取服务器上的版本…</div>
      ) : state.error ? (
        <Notice tone="danger" testId="snow-versions-error"
          actions={<button type="button" className="btn btn-quiet btn-sm" onClick={() => setTick(t => t + 1)}>重试</button>}>
          读不到服务器上的版本：{state.error}
        </Notice>
      ) : !items.length ? (
        <div className="sf-versions-empty">服务器上还没有这一步的版本——写下内容、自动保存之后就有了。</div>
      ) : (
        <ul className="sf-versions-list">
          {items.map((it, i) => {
            const status = s2VersionStatus(it);
            const source = s2VersionSource(it);
            return (
              <li key={it.step_run_id} className="sf-version-row" data-testid="snow-version-row">
                <span className="sf-version-no">第 {it.version} 版</span>
                <Tag tone={status.tone}>{status.label}</Tag>
                {source && <span className="sf-version-src">{source}</span>}
                <span className="sf-version-time">{versionTime(it)}</span>
                {i === 0
                  ? <span className="sf-version-current">现在的版本</span>
                  : <button type="button" className="btn btn-quiet btn-sm" onClick={() => onPreview(it)} data-testid="snow-version-preview"
                      aria-label={`预览第 ${it.version} 版`} title="看清这一版与现在的差别，再决定要不要恢复">预览</button>}
              </li>
            );
          })}
        </ul>
      )}
    </section>
  );
}

/* 服务器版本预览：那一版（会恢复成这样）vs 现在（本机此刻的内容）。restore 由工作台做；09 / 10 / 07 先把后果说清楚。 */
export function S2VersionDiff({ diff, current, refs, onRestore, onClose }) {
  const st = S2_STEPS.find(s => s.key === diff.key) || {};
  const item = diff.item || {};
  const status = s2VersionStatus(item);
  const source = s2VersionSource(item);
  const oldText = diff.draft ? s2VersionText(diff.key, diff.draft, refs) : "";
  const curText = s2StepText(diff.key, current.draft, current.scaffold, refs).trim();
  const same = !!diff.draft && oldText === curText;
  const empty = !!diff.draft && !oldText;
  const skipped = item.status === "skipped";
  const warning = RESTORE_WARNING[diff.key] || "";
  const canRestore = !!diff.draft && !diff.loading && !diff.restoring && !same && !empty && !skipped;
  return (
    <WsDialog onClose={onClose} labelledBy="sf-version-title" describedBy="sf-version-desc" size="lg" className="sf-diff-dialog" testId="snow-version-dialog">
      <header className="ws-dialog-head">
        <div>
          <h2 className="ws-dialog-title" id="sf-version-title">服务器上的第 {item.version} 版：{st.num} {st.name}</h2>
          <p className="ws-dialog-desc" id="sf-version-desc">{[status.label, source, versionTime(item)].filter(Boolean).join(" · ")}</p>
        </div>
        <CloseButton className="ws-dialog-x" title="关闭（Esc）" onClick={onClose} disabled={!!diff.restoring} />
      </header>
      <div className="ws-dialog-body">
        {warning && <Notice tone="warn" testId="snow-version-warning">{warning}</Notice>}
        {diff.loading ? (
          <div className="sf-sd-same is-muted" role="status"><I.Refresh size={14} className="sf-spin" /> 正在读取这一版…</div>
        ) : diff.error ? (
          <div className="sf-sd-same is-warn" role="alert"><I.AlertTriangle size={14} /> 读不到这一版：{diff.error}</div>
        ) : skipped ? (
          <div className="sf-sd-same is-muted"><I.Info size={14} /> 这一版是「略过此步」时记下的，没有内容可恢复。</div>
        ) : empty ? (
          <div className="sf-sd-same is-warn"><I.Info size={14} /> 这一版是空的——恢复它等于清空这一步，所以不提供恢复。</div>
        ) : same ? (
          <div className="sf-sd-same"><I.Check size={14} /> 这一版与现在的内容一样，不需要恢复。</div>
        ) : (
          <div className="sf-sd-cols">
            <div className="sf-sd-col is-old">
              <div className="sf-sd-coltag"><I.Clock size={11} /> 第 {item.version} 版（会恢复成这样） · {countChars(oldText)} 字</div>
              <pre className="sf-sd-text text-serif" data-testid="snow-version-old">{oldText}</pre>
            </div>
            <div className="sf-sd-col is-cur">
              <div className="sf-sd-coltag"><I.Pen size={11} /> 现在（恢复前另留一份快照） · {countChars(curText)} 字</div>
              <pre className="sf-sd-text text-serif" data-testid="snow-version-cur">{curText || "（空）"}</pre>
            </div>
          </div>
        )}
      </div>
      <footer className="ws-dialog-foot">
        <span className="sf-dialog-hint">恢复会在服务器上另存一版（现在的内容仍是上一版），恢复后这一步要重新确认；本机的「操作记录」里也留一份快照，可以回滚。</span>
        <button className="btn btn-quiet btn-sm" onClick={onClose} disabled={!!diff.restoring}>取消</button>
        <button className="btn btn-accent btn-sm" onClick={onRestore} disabled={!canRestore} data-testid="snow-version-restore">
          {diff.restoring ? "恢复中…" : <><I.Refresh size={13} /> 恢复第 {item.version} 版</>}
        </button>
      </footer>
    </WsDialog>
  );
}
