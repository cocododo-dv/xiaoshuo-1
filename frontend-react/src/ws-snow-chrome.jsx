import React from "react";
import { I } from "./icons.jsx";
import { onRovingTabKeyDown } from "./a11y-tabs.js";
import { modEnterShortcut } from "./lib/platform.js";
import { WsDialog, isImeComposing } from "./ws-dialog.jsx";
import { Notice } from "./ws-ui.jsx";
import { S2_STATE_LABEL, S2_STEPS } from "./ws-snow-model.js";

/* ==========================================================
   雪花工作台的外框：页头工具栏、左侧十步列表、三条横幅（交付 / 待同步 / 需复核）、页脚、
   「更多」菜单、「略过此步」浮层，以及从「更多」打开的两个对话框（导入结构 / 清空十步构思）。
   页头与步骤列表是 memo 的：在编辑器里打字不会让它们跟着重渲染。
   ========================================================== */

const { useState: useSS, useEffect: useSE, useRef: useSR } = React;

const tickLabel = (s, states, staleMap) => `${s.num} ${s.name}：${staleMap[s.key] ? "需复核" : (S2_STATE_LABEL[states[s.key]] || "待写")}`;

/* 页头是一条工具栏：标题 · 进度 · 一个主操作 + 「更多」。以前三行标题 + 原则句 + 四个按钮，
   1100 宽时占掉 760 高里的 245px，「10 / 10 已确认」还会一个字一行地折下来。 */
export const S2Strip = React.memo(function S2Strip({ states, staleMap, activeKey, activeSettled = false, onSelect, onOpenChapterPlan, moreItems, serverUnread = false }) {
  const doneCount = S2_STEPS.filter(s => states[s.key] === "done").length;
  const staleKeys = Object.keys(staleMap);
  /* 一页只有一个实心主按钮：第 10 步确认过、而且眼前这一步也已经落定时，「整理章节结构」才是这一页该按的；
     眼前这一步还没确认（或刚改过）时，主按钮是页脚的「确认本步」，这里退成次要 */
  const planReady = states.planning === "done" && activeSettled;
  return (
    <header className="snow-strip">
      <div className="sf-strip-left">
        <S2Fractal progress={doneCount / S2_STEPS.length} />
        {/* 页头只放标题和进度；口号（「越早回头修订越省力」）已删——每一步右栏的任务说明讲得更具体 */}
        <div className="sf-strip-titles">
          <h1 className="snow-strip-title">雪花十步</h1>
        </div>
      </div>
      <div className="snow-strip-progress">
        {/* 读不到服务器时本机的确认数说明不了什么（新浏览器里就是 0/10，像是构思没了）：先写「—」 */}
        <span className="sf-progress" data-testid="snow-progress"
          title={serverUnread ? "还没读到服务器上的构思，确认了几步暂时不知道" : undefined}>
          <b key={serverUnread ? "unread" : doneCount} className="sf-count">{serverUnread ? "—" : doneCount}</b>/10 已确认
        </span>
        {staleKeys.length > 0 && (
          <button className="sf-stale-count" onClick={() => onSelect(staleKeys[0])} title="跳到第一个需复核的步骤">
            <I.AlertTriangle size={12} /> {staleKeys.length} 需复核
          </button>
        )}
        <div className="snow-strip-bar" role="group" aria-label="十步进度">
          {S2_STEPS.map((s) => (
            <button key={s.key} className={`snow-strip-tick s-${states[s.key]} ${staleMap[s.key] ? "is-stale" : ""} ${activeKey === s.key ? "is-current" : ""}`}
              aria-label={tickLabel(s, states, staleMap)} title={tickLabel(s, states, staleMap)} aria-current={activeKey === s.key ? "step" : undefined} onClick={() => onSelect(s.key)} />
          ))}
        </div>
      </div>
      <div className="snow-strip-actions">
        {/* 第 10 步确认之前、或眼前这一步还没落定时，它不是这一页的主动作：次要按钮 */}
        <button className={`btn btn-sm ${planReady ? "btn-accent" : "btn-ghost"}`} data-testid="snow-materialize-top"
          onClick={onOpenChapterPlan} title="先预览分章（07 章表 + 09 场景 + 10 规划），确认后才写入章节目录">
          <I.Layout size={13} /> 整理章节结构
        </button>
        <S2MoreMenu label="更多操作" items={moreItems} />
      </div>
    </header>
  );
});

/* ====== Fractal progress mark (the namesake snowflake) ====== */
function S2Fractal({ progress }) {
  const arms = 6;
  const lit = Math.round((progress || 0) * arms);
  const pts = [];
  for (let i = 0; i < arms; i++) {
    const a = (i * 60) * Math.PI / 180;
    const ex = Math.cos(a) * 40, ey = Math.sin(a) * 40;
    const bx = Math.cos(a) * 23, by = Math.sin(a) * 23;
    const off = 13;
    pts.push({ i, ex, ey, bx, by,
      l1x: bx + Math.cos(a + 0.55) * off, l1y: by + Math.sin(a + 0.55) * off,
      l2x: bx + Math.cos(a - 0.55) * off, l2y: by + Math.sin(a - 0.55) * off,
      on: i < lit });
  }
  return (
    <svg className="sf-fractal" viewBox="-50 -50 100 100" width="32" height="32" aria-hidden="true">
      {pts.map(p => (
        <g key={p.i} className={`sf-fractal-arm ${p.on ? "is-on" : ""}`} strokeWidth="2.4" strokeLinecap="round">
          <line x1="0" y1="0" x2={p.ex} y2={p.ey} />
          <line x1={p.bx} y1={p.by} x2={p.l1x} y2={p.l1y} />
          <line x1={p.bx} y1={p.by} x2={p.l2x} y2={p.l2y} />
        </g>
      ))}
      <circle className="sf-fractal-core" cx="0" cy="0" r="4" />
    </svg>
  );
}

/* 左侧十步列表：情节 / 角色 / 定位三条轨交替展开；每一步的状态点、需复核、确认后又改过。 */
export const S2StepList = React.memo(function S2StepList({ states, staleMap, health, activeKey, onSelect }) {
  const beKnownUnapproved = (k) => { const b = health[k]; return !!(b && b.beStatus && b.beStatus !== "approved" && b.beStatus !== "skipped"); };
  return (
    <nav className="snow-steps" aria-label="雪花十步">
      <div className="sf-track-legend">
        <span className="sf-trk-chip plot"><span className="sf-trk-dot" />情节</span>
        <span className="sf-trk-chip character"><span className="sf-trk-dot" />角色</span>
        <span className="sf-trk-chip orient"><span className="sf-trk-dot" />定位</span>
        <span className="sf-trk-note">两条线交替展开</span>
      </div>
      {S2_STEPS.map((s) => {
        const st = states[s.key];
        const stale = !!staleMap[s.key];
        const revised = st === "done" && !!(health[s.key] && health[s.key].revisedAfterApproval);
        return (
          <button key={s.key} data-testid={`snow-step-${s.key}`} aria-current={activeKey === s.key ? "step" : undefined}
            className={`snow-step ${activeKey === s.key ? "is-active" : ""} s-${st} ${stale ? "is-stale" : ""} ${revised ? "is-revised" : ""}`}
            onClick={() => onSelect(s.key)} title={revised ? "确认之后又改过 · 待重新确认" : (st === "done" && beKnownUnapproved(s.key) ? "本地已确认 · 后端未批准（前序闸门未满足）" : undefined)}>
            <span className={`sf-track-bar trk-${s.track}`} />
            <span className="snow-step-num">{s.num}</span>
            <span className="snow-step-body">
              <span className="snow-step-name">{s.name}</span>
              <span className="snow-step-blurb">{stale ? `上游已改 · 需复核` : revised ? `已改动 · 待重新确认` : s.blurb}</span>
            </span>
            <span className="snow-step-mark" aria-label={stale ? "需复核" : (S2_STATE_LABEL[st] || undefined)}>
              {stale ? <I.AlertTriangle size={13} className="sf-stale-ic" />
                : st === "done" ? <I.Check size={13} className={beKnownUnapproved(s.key) ? "sf-mark-local" : undefined} />
                : st === "warn" ? <I.AlertTriangle size={13} />
                : st === "skip" ? <span className="sf-skip-mark">–</span>
                : (st === "active" && activeKey !== s.key) ? <span className="snow-step-dot" /> : null}
            </span>
          </button>
        );
      })}
    </nav>
  );
});

export function S2Tab({ id, cur, on, children }) {
  return <button role="tab" aria-selected={cur === id} tabIndex={cur === id ? 0 : -1} onKeyDown={onRovingTabKeyDown}
    className={`snow-tab ${cur === id ? "is-active" : ""}`} onClick={() => on(id)}>{children}</button>;
}

/* 阶段 X：「确认写入」之后不再只丢一句回执就把作者晾在构思里——给一条常驻的交付条：
   目录现在是什么样（几章几场）、顺手做了什么（空白占位章 / 变空的旧章进了回收站），
   以及下一步去哪：写作台从「现在该写的那一场」开始写，或去 AI 起草台（书脊上已经是这一版的章与场）。 */
export function S2DeliveredBanner({ totals, notes, onWrite, onClose }) {
  return (
    <div className="sf-stale-banner sf-resync-banner is-ok" data-testid="snow-delivered">
      <span className="sf-stale-banner-ic"><I.Check size={15} /></span>
      <div className="sf-stale-body">
        <div className="sf-stale-title">章节结构已写入目录{totals.chapters ? `：${totals.chapters} 章 ${totals.scenes} 场` : "，正在读取目录…"}</div>
        <div className="sf-stale-sub">
          写作台的大纲、AI 起草台的书脊、章节编排读的都是这一份，每一场都带着它的设计卡。
          {notes.length ? ` ${notes.join("；")}。` : ""}
          之后再改 09 / 10，点「确认本步」场景卡就会跟上。
        </div>
      </div>
      <div className="sf-stale-actions-inline">
        <button className="btn btn-accent btn-sm sf-stale-ok" data-testid="snow-delivered-write" onClick={onWrite} title="进写作台，落在现在该写的那一场上"><I.Pen size={13} /> 去写作台</button>
        <button className="btn btn-ghost btn-sm" data-testid="snow-delivered-draft" onClick={() => { location.hash = "#scene"; }} title="AI 起草台的书脊上已经是这一版的章与场"><I.Play size={13} /> 去 AI 起草台</button>
        <button className="btn btn-quiet btn-sm" onClick={() => { location.hash = "#author"; }}>章节编排</button>
        <button className="btn btn-quiet btn-sm" aria-label="收起这条提示" title="收起" onClick={onClose}><I.X size={13} /></button>
      </div>
    </div>
  );
}

/* 物化后回流：构思 9/10 步领先于目录场景卡的场（SnowSync.resyncStatus，后端真相）。
   不同步，写作台 / AI 起草台拿到的是旧三拍。 */
export function S2ResyncBanner({ info, busy, onResync }) {
  const titles = info.pendingScenes.slice(0, 3).map(s => s.title).filter(Boolean);
  return (
    <div className="sf-stale-banner sf-resync-banner">
      <span className="sf-stale-banner-ic"><I.Refresh size={15} /></span>
      <div className="sf-stale-body">
        <div className="sf-stale-title">构思已更新：{info.pendingCount} 场的改动还没同步到章节目录</div>
        <div className="sf-stale-sub">
          整理成章之后你又改了这些场的规划
          {titles.length ? <>（{titles.join("、")}{info.pendingCount > 3 ? " 等" : ""}）</> : null}
          ——不同步的话，写作台和 AI 起草台拿到的还是旧场景卡。
        </div>
      </div>
      <button className="btn btn-accent btn-sm sf-stale-ok" disabled={busy} onClick={onResync} title="把构思里这些场的最新三拍、视角、题名写回目录场景卡">
        <I.Refresh size={13} className={busy ? "sf-spin" : ""} /> {busy ? "同步中…" : "同步到目录"}
      </button>
    </div>
  );
}

/* 本步需要复核：后端判定失效（原因）、哪些上游有了新版本、看 diff / 按新上游重新展开 / 已复核。 */
export function S2StaleBanner({ reason, drift, busy, onGoStep, onShowDiff, onRegen, onReviewed }) {
  return (
    <div className="sf-stale-banner" data-testid="snow-stale-banner">
      <span className="sf-stale-banner-ic"><I.AlertTriangle size={15} /></span>
      <div className="sf-stale-body">
        <div className="sf-stale-title">上游改过了，本步需要复核</div>
        <div className="sf-stale-sub">
          {reason && <span className="sf-stale-reason" title="后端失效分析给出的原因">{reason}</span>}
          {drift.length > 0 && (<>
            本步确认后，
            {drift.map((a) => { const u = S2_STEPS.find(x => x.key === a); return (
              <button key={a} className="sf-stale-up" onClick={() => onGoStep(a)}>{u.num} {u.name}</button>
            ); })}
            有了新版本。
          </>)}
          先看看上游改了什么；可以按新上游重新展开本步，或核对无误后点「已复核」（会在服务端留痕）。
        </div>
        <div className="sf-stale-actions">
          <button className="btn btn-quiet btn-sm" onClick={onShowDiff} data-testid="snow-stale-diff" title="对照本步确认时消费的上游版本与现在的版本"><I.GitBranch size={12} /> 查看上游改了什么</button>
          <button className="btn btn-quiet btn-sm" disabled={busy} onClick={onRegen} data-testid="snow-stale-regen" title="用现在的上游材料重新生成本步（生成前留底，可回滚），生成后需要你再确认"><I.Wand size={12} className={busy ? "sf-spin" : ""} /> 按新上游重新展开</button>
        </div>
      </div>
      <button className="btn btn-accent btn-sm sf-stale-ok" onClick={onReviewed} title="核对无误：在服务端记下「仍然有效」（消费的上游版本刷新到当前）"><I.Check size={13} /> 已复核</button>
    </div>
  );
}

/* 同步出了什么问题、作者该做什么：画布上方的提示条用它（页脚只放一句短状态，放不下原因）。
   没出错返回 null。scope：hydrate = 本会话还没读到服务器（新浏览器里页面此时是空白默认稿，
   最容易让作者以为构思没了）、local = 本机保存失败、其余 = 改动没能上传。 */
export function s2SyncProblem(syncState) {
  const error = syncState && syncState.phase === "error" ? syncState.error : null;
  if (!error) return null;
  const offline = error.offline ? "当前离线。" : "";
  const message = String(error.message || "同步失败").trim().replace(/[。.]+$/, "");
  if (error.scope === "local") {
    return { scope: "local", title: "本机保存失败", text: `${offline}${message}。先导出一份构思，免得这次的改动丢失。` };
  }
  if (error.scope === "hydrate") {
    return { scope: "hydrate", title: "暂时读不到服务器",
      text: `${offline}${message}。页面上显示的是本机缓存，不代表服务器上的构思没了；连上以后点「重试」，会先读回服务器的版本。` };
  }
  return { scope: "remote", title: "改动还没同步到服务器", text: `${offline}${message}。本机已经保存，点「重试」再同步一次。` };
}

export function S2SyncNotice({ syncState, retryBusy, onRetry, onExport }) {
  const problem = s2SyncProblem(syncState);
  if (!problem) return null;
  return (
    <Notice tone="danger" className="sf-sync-notice" testId="snow-sync-notice" title={problem.title}
      actions={problem.scope === "local" ? (
        <button type="button" className="btn btn-ghost btn-sm" onClick={onExport}>立即导出</button>
      ) : (
        <button type="button" className="btn btn-ghost btn-sm" data-testid="snow-sync-notice-retry" disabled={retryBusy} onClick={onRetry}>
          {retryBusy ? "重试中…" : "重试"}
        </button>
      )}>
      {problem.text}
    </Notice>
  );
}

/* 页脚：上一步 · 保存 / 同步状态（一句短话；出错时原因在画布上方的提示条里，这里只留「重试 / 立即导出」）·
   略过此步 · 确认本步 · 下一步 */
const hhmm = (t) => new Date(t).toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit" });
export function S2Footer({ step, idx, settled, blank = false, syncState, savedAt, retryBusy, onRetry, onExport, onSkip, onConfirm, onPrev, onNext }) {
  const phase = (syncState && syncState.phase) || "idle";
  const error = syncState && syncState.error;
  const savedLabel = phase === "synced"
    ? `服务器已同步${syncState.lastSyncedAt ? ` · ${hhmm(syncState.lastSyncedAt)}` : ""}`
    : phase === "syncing"
      ? "本机已保存 · 正在同步服务器…"
      : phase === "error"
        ? (error && error.scope === "local" ? "本机保存失败" : "同步失败")
        : savedAt
          ? `仅本机已保存 · ${hhmm(savedAt)}`
          : "本机自动保存已开启 · 尚未同步服务器";
  return (
    <footer className="snow-canvas-foot">
      <button className="btn btn-ghost" disabled={idx === 0} onClick={onPrev}><I.ChevronLeft size={14} /> 上一步</button>
      <div className={`sf-sync-state is-${phase}`} data-testid="snow-sync-status" role="status" aria-live="polite" title={(error && error.message) || savedLabel}>
        {phase === "error" ? <I.AlertTriangle size={12} /> : phase === "syncing" ? <I.Refresh size={12} className="sf-spin" /> : <I.Check size={12} />}
        <span>{savedLabel}</span>
        {phase === "error" && error && error.scope !== "local" && (
          <button type="button" data-testid="snow-sync-retry" disabled={retryBusy} onClick={onRetry}>
            {retryBusy ? "重试中…" : "重试"}
          </button>
        )}
        {phase === "error" && error && error.scope === "local" && (
          <button type="button" onClick={onExport}>立即导出</button>
        )}
      </div>
      <div className="sf-foot-actions">
        <S2SkipControl key={step.key} step={step} onSkip={onSkip} />
        {/* 写了内容、还没确认（或确认后又改过）的一步，「确认本步」才是这一页的实心主按钮；落定的步骤、
            还空着的步骤（那时主按钮是 AI 工具条上的生成）都退成次要，一页只有一种实心红 */}
        <button className={`btn ${settled || blank ? "btn-ghost" : "btn-accent"}`} data-testid="snow-confirm-step" onClick={onConfirm}
          title={settled ? `这一步已经确认；再按一次会按现在的内容重新确认（${modEnterShortcut()}）` : `确认本步（${modEnterShortcut()}）`}><I.Check size={14} /> 确认本步</button>
        <button className="btn btn-ghost" disabled={idx === S2_STEPS.length - 1} onClick={onNext}>下一步 <I.ChevronRight size={14} /></button>
      </div>
    </footer>
  );
}

/* ---- 页头「更多」菜单：次要动作收进这里（导入 / 导出 / 危险区的清空），主操作只留一个 ---- */
function S2MoreMenu({ label, items }) {
  const [open, setOpen] = useSS(false);
  const btnRef = useSR(null);
  const menuRef = useSR(null);
  useSE(() => {
    if (!open) return undefined;
    const first = menuRef.current && menuRef.current.querySelector('[role="menuitem"]');
    if (first) first.focus();
    const onDown = (e) => {
      if (menuRef.current && menuRef.current.contains(e.target)) return;
      if (btnRef.current && btnRef.current.contains(e.target)) return;
      setOpen(false);
    };
    document.addEventListener("mousedown", onDown);
    return () => document.removeEventListener("mousedown", onDown);
  }, [open]);
  const close = (refocus) => { setOpen(false); if (refocus && btnRef.current) btnRef.current.focus(); };
  const onMenuKey = (e) => {
    const nodes = menuRef.current ? [...menuRef.current.querySelectorAll('[role="menuitem"]')] : [];
    if (!nodes.length) return;
    const at = nodes.indexOf(document.activeElement);
    if (e.key === "Escape") { e.preventDefault(); e.stopPropagation(); close(true); }
    else if (e.key === "ArrowDown") { e.preventDefault(); nodes[(at + 1) % nodes.length].focus(); }
    else if (e.key === "ArrowUp") { e.preventDefault(); nodes[(at - 1 + nodes.length) % nodes.length].focus(); }
    else if (e.key === "Home") { e.preventDefault(); nodes[0].focus(); }
    else if (e.key === "End") { e.preventDefault(); nodes[nodes.length - 1].focus(); }
    else if (e.key === "Tab") close(false);
  };
  return (
    <div className="sf-more">
      <button ref={btnRef} type="button" className="btn btn-ghost btn-sm sf-more-btn" aria-haspopup="menu" aria-expanded={open}
        aria-label={label} title={label} onClick={() => setOpen(o => !o)}>
        <I.More size={15} />
      </button>
      {open && (
        <div ref={menuRef} className="sf-more-menu" role="menu" aria-label={label} onKeyDown={onMenuKey}>
          {items.map((it, i) => it.sep ? <div key={i} role="separator" className="sf-more-sep" /> : (
            <button key={i} type="button" role="menuitem" className={`sf-more-item ${it.danger ? "is-danger" : ""}`} data-testid={it.testId}
              onClick={() => { close(true); it.onSelect(); }}>
              <span className="sf-more-ic" aria-hidden="true">{it.icon}</span>
              <span className="sf-more-text"><span>{it.label}</span>{it.hint && <small>{it.hint}</small>}</span>
            </button>
          ))}
        </div>
      )}
    </div>
  );
}

/* ---- 页脚「略过此步」：必填步直接禁用（说明为什么）；可略过的步在一个小浮层里写一句理由再略过。
   以前用浏览器原生 prompt 收理由，必填步还能点、点了只弹一句「不能略过」。 ---- */
function S2SkipControl({ step, onSkip }) {
  const [open, setOpen] = useSS(false);
  const [reason, setReason] = useSS("");
  const [busy, setBusy] = useSS(false);
  const wrapRef = useSR(null);
  const btnRef = useSR(null);
  useSE(() => {
    if (!open) return undefined;
    const onDown = (e) => { if (wrapRef.current && !wrapRef.current.contains(e.target)) setOpen(false); };
    document.addEventListener("mousedown", onDown);
    return () => document.removeEventListener("mousedown", onDown);
  }, [open]);
  if (step.essential) {
    return (
      <button type="button" className="btn btn-ghost" disabled
        title={`${step.name}是整理章节结构之前必须确认的一步，不能略过——先写完再确认`}>略过此步</button>
    );
  }
  const close = () => { setOpen(false); setReason(""); if (btnRef.current) btnRef.current.focus(); };
  const submit = async () => {
    const text = reason.trim();
    if (!text || busy) return;
    setBusy(true);
    const ok = await onSkip(text);
    setBusy(false);
    if (ok) { setOpen(false); setReason(""); }
  };
  return (
    <div className="sf-skip" ref={wrapRef}>
      <button ref={btnRef} type="button" className="btn btn-ghost" data-testid="snow-skip-open" aria-expanded={open} aria-controls="sf-skip-pop"
        onClick={() => setOpen(o => !o)}>略过此步</button>
      {open && (
        <div className="sf-skip-pop" id="sf-skip-pop" role="dialog" aria-label={`略过「${step.name}」`}
          onKeyDown={(e) => { if (e.key === "Escape" && !isImeComposing(e)) { e.preventDefault(); e.stopPropagation(); close(); } }}>
          <label className="sf-skip-label" htmlFor="sf-skip-reason">略过「{step.name}」的理由<small>会记在服务端，下游步骤照常继续</small></label>
          <input id="sf-skip-reason" className="input" autoFocus data-testid="snow-skip-reason" value={reason} disabled={busy}
            placeholder="比如：先按梗概走，人物表等第二稿" onChange={(e) => setReason(e.target.value)}
            onKeyDown={(e) => { if (e.key === "Enter" && !isImeComposing(e)) { e.preventDefault(); submit(); } }} />
          <div className="sf-skip-actions">
            <button type="button" className="btn btn-quiet btn-sm" data-testid="snow-skip-cancel" onClick={close} disabled={busy}>取消</button>
            <button type="button" className="btn btn-accent btn-sm" data-testid="snow-skip-confirm" disabled={!reason.trim() || busy} onClick={submit}>{busy ? "记录中…" : "确认略过"}</button>
          </div>
        </div>
      )}
    </div>
  );
}

/* ---- 导入结构化雪花计划（「更多」菜单）：导入进行中不能关；粘了内容之后点遮罩不关 ---- */
export function S2ImportPlanDialog({ value, busy, error, onChange, onImport, onClose }) {
  const textRef = useSR(null);
  return (
    <WsDialog onClose={onClose} onBeforeClose={() => !busy} dismissOnBackdrop={!value.trim()} labelledBy="sf-import-title" describedBy="sf-import-desc"
      size="lg" className="sf-import-dialog" testId="snow-import-dialog" initialFocus={textRef}>
      <header className="ws-dialog-head">
        <div>
          <h2 className="ws-dialog-title" id="sf-import-title">导入结构化雪花计划</h2>
          <p className="ws-dialog-desc" id="sf-import-desc">粘贴包含 <code>steps</code> 的十步规范 JSON，系统按依赖顺序逐步保存、批准并保留版本历史。</p>
        </div>
        <button className="ws-dialog-x" onClick={onClose} disabled={busy} aria-label="关闭" title="关闭（Esc）"><I.X size={16} /></button>
      </header>
      <div className="ws-dialog-body sf-import-body">
        <div className="sf-import-warning"><I.AlertTriangle size={14} /> 导入会为当前作品的十步各建一个新版本；任何一步失败就立即停下，不会装作已经完成。</div>
        <textarea ref={textRef} data-testid="snow-import-json" value={value} disabled={busy} onChange={(e) => onChange(e.target.value)} aria-label="十步规范 JSON"
          spellCheck="false" placeholder={'{\n  "steps": {\n    "book_brief": { ... },\n    "one_sentence_summary": { ... },\n    ...\n  }\n}'} />
        {error && <div className="sf-cand-err" role="alert"><I.AlertTriangle size={13} /><span>{error}</span></div>}
      </div>
      <footer className="ws-dialog-foot">
        <span className="sf-dialog-hint">十步都要有：从读者定位到场景规划。</span>
        <button className="btn btn-quiet btn-sm" onClick={onClose} disabled={busy}>取消</button>
        <button className="btn btn-accent btn-sm" data-testid="snow-import-submit" onClick={onImport} disabled={busy || !value.trim()}>
          {busy ? <I.Refresh size={13} className="sf-spin" /> : <I.Download size={13} />} {busy ? "逐步导入中…" : "导入并逐步批准"}
        </button>
      </footer>
    </WsDialog>
  );
}

/* ---- 清空十步构思：说清后果的二次确认（从「更多」菜单的危险区打开） ---- */
export function S2ResetDialog({ workTitle, onExport, onConfirm, onClose }) {
  const cancelRef = useSR(null);
  return (
    <WsDialog onClose={onClose} labelledBy="sf-reset-title" describedBy="sf-reset-desc" size="sm" testId="snow-reset-dialog" initialFocus={cancelRef}>
      <header className="ws-dialog-head">
        <div>
          <h2 className="ws-dialog-title" id="sf-reset-title">清空十步构思？</h2>
          <p className="ws-dialog-desc" id="sf-reset-desc">{workTitle ? `《${workTitle}》` : "这部作品"}十步的草稿和确认状态会全部清空，空稿随后同步到服务器。</p>
        </div>
        <button className="ws-dialog-x" onClick={onClose} aria-label="关闭" title="关闭（Esc）"><I.X size={16} /></button>
      </header>
      <div className="ws-dialog-body">
        <ul className="sf-reset-facts">
          <li>服务器会为每一步另存一版空稿，原来的版本仍留在服务器上，不会被覆盖。</li>
          <li>清空前每一步都会在「历史」里留一份快照，可以逐步回滚。</li>
          <li>已经写进章节目录的章与场不受影响。</li>
        </ul>
      </div>
      <footer className="ws-dialog-foot">
        <button className="btn btn-quiet btn-sm sf-dialog-lead" onClick={onExport}><I.UploadCloud size={13} /> 先导出大纲</button>
        <button ref={cancelRef} className="btn btn-quiet btn-sm" onClick={onClose}>取消</button>
        <button className="btn btn-danger btn-sm" data-testid="snow-reset-confirm" onClick={onConfirm}><I.Trash size={13} /> 清空十步构思</button>
      </footer>
    </WsDialog>
  );
}
