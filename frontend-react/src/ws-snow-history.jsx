import React from "react";
import { I } from "./icons.jsx";
import { WsDialog } from "./ws-dialog.jsx";
import { dayTimeLabel } from "./lib/ago.js";
import { S2_STEPS, s2Ancestors, s2Content } from "./ws-snow-model.js";

/* ==========================================================
   版本与来路：「历史」页签（操作时间线，带快照的节点可回滚）、「引用上下文」页签（本步展开自哪几层上游），
   以及两个对照对话框——回滚预览（快照 vs 当前）与「上游改了什么」（本步确认时消费的上游版本 vs 现在）。
   对话框统一走 WsDialog（焦点移入、Tab 困在框内、关闭后焦点回到打开它的按钮、Esc / 遮罩走同一条关闭路径）。
   ========================================================== */

/* 一小时内走相对文案，更早走 lib/ago.js 的天级绝对文案（与成稿中心版本标签同款） */
function s2HistTime(t) {
  const diff = Date.now() - t;
  if (diff < 60000) return "刚刚";
  if (diff < 3600000) return Math.floor(diff / 60000) + " 分钟前";
  return dayTimeLabel(t);
}

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
            <span className="hist-time">{s2HistTime(h.t)}</span>
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
        const text = clip(s2Content(drafts[k], scaffolds[k]), 160);
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

/* ---- 回滚预览：快照 vs 当前，看清再恢复 ---- */
export function S2SnapDiff({ h, current, onApply, onClose }) {
  const st = S2_STEPS.find(s => s.key === h.key) || {};
  const oldText = s2Content(h.snap.draft, h.snap.scaffold).trim();
  const curText = s2Content(current.draft, current.scaffold).trim();
  const same = oldText === curText;
  const cnt = (t) => t.replace(/\s+/g, "").length;
  return (
    <WsDialog onClose={onClose} labelledBy="sf-snap-title" describedBy="sf-snap-desc" size="lg" className="sf-diff-dialog">
      <header className="ws-dialog-head">
        <div>
          <h2 className="ws-dialog-title" id="sf-snap-title">回滚预览：{st.num} {st.name}</h2>
          <p className="ws-dialog-desc" id="sf-snap-desc">快照留于 {new Date(h.t).toLocaleString("zh-CN")}（{h.action}）</p>
        </div>
        <button className="ws-dialog-x" onClick={onClose} aria-label="关闭" title="关闭（Esc）"><I.X size={16} /></button>
      </header>
      <div className="ws-dialog-body">
        {same ? (
          <div className="sf-sd-same"><I.Check size={14} /> 快照与当前内容完全一致，不需要回滚。</div>
        ) : (
          <div className="sf-sd-cols">
            <div className="sf-sd-col is-old">
              <div className="sf-sd-coltag"><I.Clock size={11} /> 快照（会恢复成这一版） · {cnt(oldText)} 字</div>
              <pre className="sf-sd-text text-serif">{oldText || "（空）"}</pre>
            </div>
            <div className="sf-sd-col is-cur">
              <div className="sf-sd-coltag"><I.Pen size={11} /> 当前（会被替换，替换前另留一份） · {cnt(curText)} 字</div>
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
  const cnt = (t) => String(t || "").replace(/\s+/g, "").length;
  const items = diff.items || [];
  return (
    <WsDialog onClose={onClose} labelledBy="sf-updiff-title" describedBy="sf-updiff-desc" size="lg" className="sf-diff-dialog" testId="snow-upstream-diff">
      <header className="ws-dialog-head">
        <div>
          <h2 className="ws-dialog-title" id="sf-updiff-title">上游改了什么：{st.num} {st.name}</h2>
          <p className="ws-dialog-desc" id="sf-updiff-desc">{diff.reason ? `后端判定失效的原因：${diff.reason}` : "左边是本步确认时用的上游版本，右边是现在的版本"}</p>
        </div>
        <button className="ws-dialog-x" onClick={onClose} aria-label="关闭" title="关闭（Esc）"><I.X size={16} /></button>
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
                      <div className="sf-sd-coltag"><I.Clock size={11} /> 本步确认时用的版本 · {cnt(item.oldText)} 字</div>
                      <pre className="sf-sd-text text-serif">{item.oldText || "（空）"}</pre>
                    </div>
                    <div className="sf-sd-col is-cur">
                      <div className="sf-sd-coltag"><I.Pen size={11} /> 现在的版本 · {cnt(item.newText)} 字</div>
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
