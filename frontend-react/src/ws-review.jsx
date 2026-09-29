import React from "react";
import ReactDOM from "react-dom";
import { I } from "./icons.jsx";
import { PageHeader, Segmented, Tag, EmptyState, Notice, Spinner } from "./ws-ui.jsx";
import { wsToast } from "./ws-notify.jsx";
import { UndoToast, useUndoToast } from "./ws-undo-toast.jsx";
import {
  RV_KINDS, rvOpenItems, rvSnoozedList, rvReady, rvLoadErrorOf, rvDoneToday, rvFetch, rvPush,
  rvMarkResolved, rvMarkSnoozed, rvUnsnooze, rvResolveAction, rvMigrateLegacy, rvSubscribe, useReviewOpenItems,
} from "./ws-review-store.js";
import { RV_TONE, RV_BAND, rvNeedsChoice, RvBand, RvItem, RvEmpty } from "./ws-review-parts.jsx";
import { RV_UNDO_MS, useRvKeyboard, useRvUndo } from "./ws-review-hooks.js";

/* ==========================================================
   WsReview — 待办收件箱
   把工作台各处需要作者拍板的事汇到一处——决策、风险、质检建议、构思缺口、
   批注——按紧急程度排好，每条带来源、位置和理由。处理完就回去写。
   store 在 ws-review-store.js，卡片与段头在 ws-review-parts.jsx，撤销与键盘在 ws-review-hooks.js；
   这里转出主页、成稿中心与测试一直从 ws-review.jsx 取的名字。
   ========================================================== */

function WsReview({ go }) {
  const [items, setItems] = React.useState(rvOpenItems);
  const [snoozed, setSnoozed] = React.useState(rvSnoozedList);
  const [ready, setReady] = React.useState(rvReady);
  const [loadError, setLoadError] = React.useState(rvLoadErrorOf);
  const [retrying, setRetrying] = React.useState(false);
  const [filter, setFilter] = React.useState("all");
  const [openId, setOpenId] = React.useState(() => { const l = rvOpenItems(); return l[0] ? l[0].id : null; });
  const [removing, setRemoving] = React.useState(null);
  const [doneToday, setDoneToday] = React.useState(rvDoneToday);
  const [showSnoozed, setShowSnoozed] = React.useState(false);
  const [selId, setSelId] = React.useState(null);
  const [kbd, setKbd] = React.useState(false); // 是否用过键盘（点亮快捷键提示）
  const rootRef = React.useRef(null);
  const { toast: localToast, show: showLocalToast, clear: clearLocalToast } = useUndoToast();

  /* store 异步装载：缓存更新（后端刷新 / 外部投递 / 换作品）同步进视图列表 */
  React.useEffect(() => rvSubscribe(() => {
    setItems(rvOpenItems()); setSnoozed(rvSnoozedList()); setReady(rvReady()); setLoadError(rvLoadErrorOf());
  }), []);

  const retryLoad = async () => {
    setRetrying(true);
    try { await rvFetch(); } finally { setRetrying(false); }
  };

  const counts = React.useMemo(() => {
    const c = { all: items.length };
    Object.keys(RV_KINDS).forEach(k => { c[k] = items.filter(i => i.kind === k).length; });
    return c;
  }, [items]);

  /* 回执：外壳的提示层（wsToast）；单独渲染本视图（没有提示层）时用本地的同款回执。 */
  const notify = (message, action) => {
    if (wsToast({ message, action, timeout: RV_UNDO_MS })) return;
    showLocalToast({ text: message, actionLabel: action && action.label, onAction: action && action.onClick, timeout: RV_UNDO_MS });
  };

  const { undoRef, undo, receipt } = useRvUndo({ setItems, setDoneToday, notify, clearLocalToast });

  const resolve = (id) => {
    setRemoving(id);
    setTimeout(() => {
      const index = items.findIndex(x => x.id === id);
      const item = items[index];
      setItems(prev => prev.filter(x => x.id !== id));
      rvMarkResolved([id]);
      setDoneToday(n => n + 1);
      setRemoving(null);
      if (item) receipt([{ item, index }]);
    }, 300);
  };

  // 一键收尾：把当前筛选下的待办全部标记完成（整批可撤销）
  const resolveAll = () => {
    const pool = (filter === "all" ? items : items.filter(i => i.kind === filter));
    const ids = pool.filter(i => !rvNeedsChoice(i)).map(x => x.id);
    const skipped = pool.length - ids.length;
    if (!ids.length) {
      if (skipped) notify(`${skipped} 条需要你拍板或去源头处理，不能批量划掉`);
      return;
    }
    setRemoving("__all__");
    setTimeout(() => {
      const entries = ids.map(id => ({ item: items.find(x => x.id === id), index: items.findIndex(x => x.id === id) }))
        .filter(e => e.item).sort((a, b) => a.index - b.index);
      setItems(prev => prev.filter(x => !ids.includes(x.id)));
      rvMarkResolved(ids);
      setDoneToday(n => n + ids.length);
      setRemoving(null);
      if (entries.length) receipt(entries);
    }, 300);
  };

  /* 两份列表都由 store 的广播同步（sync）；这里只留 300ms 的移出动画 */
  const snooze = (id) => {
    setRemoving(id);
    const item = items.find(x => x.id === id);
    setTimeout(() => {
      rvMarkSnoozed(id, item);
      setRemoving(null);
    }, 300);
  };

  const unsnooze = (id) => rvUnsnooze(id);

  const act = (item, a) => {
    if (a.op === "nav" && a.to) {
      /* 带上下文深链：雪花步骤 / 写作器场景 / 深改姿态 / AI 起草台入列（与命令面板同一套事件） */
      const intents = [];
      if (a.step) intents.push({ type: "ws:snow-step", detail: a.step });
      if (a.scene && a.to === "scene") intents.push({ type: "ws:scene-enqueue", detail: { sid: a.scene } });
      else if (a.scene) intents.push({ type: "ws:writer-scene", detail: a.scene });
      if (a.posture) intents.push({ type: "ws:writer-posture", detail: a.posture });
      go(a.to, intents);
    }
    else if (a.op === "snooze") snooze(item.id);
    else {
      // 补出来的「知道了」不是卡上的动作，不带 action_index
      if (!a.fallback) rvResolveAction(item, a);
      resolve(item.id);
    }
  };

  const visible = filter === "all" ? items : items.filter(i => i.kind === filter);
  const decisionsLeft = items.filter(i => i.priority === 1).length;
  const allClear = items.length === 0;

  // 筛选或列表变化后，选中项不在可见列表里就清掉
  React.useEffect(() => {
    if (selId && !visible.some(x => x.id === selId)) setSelId(null);
  }, [filter, items]); // eslint-disable-line

  useRvKeyboard({ rootRef, visible, selId, setSelId, setKbd, setOpenId, undoRef, undo, resolve, snooze });

  // 按优先级分段；只有一段时不画段头（一个孤零零的「其余待办」前面什么都没有）
  const groups = [1, 2]
    .map(band => ({ band, items: visible.filter(it => (it.priority === 1 ? 1 : 2) === band) }))
    .filter(g => g.items.length);
  const showBands = filter === "all" && groups.length > 1;

  const filterOptions = [
    { value: "all", label: "全部", count: counts.all },
    ...Object.entries(RV_KINDS)
      .filter(([k]) => counts[k] > 0)
      .map(([k, m]) => ({ value: k, label: m.label, count: counts[k], icon: <span className="rv-chip-dot" data-tone={RV_TONE[m.tone]} aria-hidden="true" /> })),
  ];

  /* 还没成功拉到过、上一次又失败了：说清楚，给重试（以前只在控制台里警告一句，页面永远「正在读取」） */
  const failed = allClear && !ready && !!loadError;
  const description = allClear
    ? (ready ? "都处理完了，回写作房间继续吧。" : failed ? null : "正在读取待办…")
    : <>现在 <b>{items.length}</b> 条待处理{decisionsLeft ? <>，其中 <b className="rv-em">{decisionsLeft}</b> 条要尽快处理</> : ""}。处理完就回去写。</>;

  return (
    <div className="ws-page ws-view rv" ref={rootRef}>
      <PageHeader className="rv-head" title="待办收件箱" description={description} />

      {!allClear && (
        <div className="rv-toolbar">
          <Segmented label="按类型筛选" value={filter} onChange={setFilter} options={filterOptions} className="rv-chips" />
          <div className="rv-toolbar-right">
            <div className="rv-progress" title="今天在收件箱里处理掉的条数">
              <I.CheckCircle size={14} /> 今日已处理 <b>{doneToday}</b>
            </div>
            {visible.length > 1 && (
              <button type="button" className="btn btn-ghost btn-sm rv-clear-all" onClick={resolveAll}
                title={filter === "all" ? "把能直接划掉的待办都标记完成（需要拍板的会留下）" : "把这一类里能直接划掉的待办标记完成"}>
                <I.Check size={14} /> 全部处理完
              </button>
            )}
          </div>
        </div>
      )}

      {!allClear && (
        <div className={`rv-kbd-hint ${kbd ? "is-lit" : ""}`} aria-hidden="true">
          <kbd>J</kbd><kbd>K</kbd><span>上下切换</span>
          <kbd>↵</kbd><span>展开</span>
          <kbd>E</kbd><span>处理</span>
          <kbd>S</kbd><span>稍后</span>
          <kbd>U</kbd><span>撤销</span>
        </div>
      )}

      {allClear ? (
        ready ? <RvEmpty hasSnoozed={snoozed.length > 0} go={go} />
          : failed ? (
            <Notice tone="danger" className="rv-load-error" testId="review-load-error" title="待办暂时读不出来"
              actions={(
                <button type="button" className="btn btn-ghost btn-sm" data-testid="review-load-retry" disabled={retrying} onClick={retryLoad}>
                  {retrying ? <Spinner size={13} /> : <I.Refresh size={13} />} {retrying ? "重新读取中…" : "重试"}
                </button>
              )}>
              <span title={loadError.message || undefined}>后端可能没有连上。待办存在后端，不会因此丢失；连上后重试即可。</span>
            </Notice>
          )
          : <div className="rv-loading" role="status">正在读取待办…</div>
      ) : (
        <div className="rv-list">
          {groups.map(g => (
            <section key={g.band} className="rv-group" aria-label={showBands ? RV_BAND[g.band].label : "待办"}>
              {showBands && <RvBand band={g.band} />}
              <div className="rv-group-items" role="list">
                {g.items.map(it => (
                  <RvItem key={it.id} item={it} selected={selId === it.id && kbd}
                    open={openId === it.id} removing={removing === it.id || removing === "__all__"}
                    onToggle={() => { setSelId(it.id); setOpenId(o => o === it.id ? null : it.id); }}
                    onAct={(a) => act(it, a)} />
                ))}
              </div>
            </section>
          ))}
          {visible.length === 0 && (
            <EmptyState compact title="这一类暂时没有待办"
              actions={<button type="button" className="btn btn-ghost btn-sm" onClick={() => setFilter("all")}>看全部</button>} />
          )}
        </div>
      )}

      {snoozed.length > 0 && (
        <div className="rv-snoozed">
          <button type="button" className="rv-snoozed-head" onClick={() => setShowSnoozed(s => !s)} aria-expanded={showSnoozed}>
            <I.Clock size={14} />
            <span>稍后处理</span>
            <span className="rv-snoozed-n">{snoozed.length}</span>
            <span className="rv-snoozed-chev" data-open={showSnoozed}><I.ChevronDown size={15} /></span>
          </button>
          {showSnoozed && (
            <div className="rv-snoozed-list">
              {snoozed.map(it => {
                const m = RV_KINDS[it.kind] || RV_KINDS.note;
                return (
                  <div key={it.id} className="rv-snoozed-item">
                    <Tag tone={RV_TONE[m.tone]} dot>{m.label}</Tag>
                    <span className="rv-snoozed-title" title={it.title}>{it.title}</span>
                    <button type="button" className="btn btn-quiet btn-sm" onClick={() => unsnooze(it.id)}><I.Refresh size={13} /> 恢复</button>
                  </div>
                );
              })}
            </div>
          )}
        </div>
      )}

      {localToast && typeof document !== "undefined" && ReactDOM.createPortal(
        <UndoToast toast={localToast} onClose={clearLocalToast} />,
        document.body,
      )}
    </div>
  );
}

Object.assign(window, { WsReview, RV_KINDS, rvOpenItems, rvMarkResolved, rvPush, rvResolveAction });

/* ESM 导出（window.* 赋值过渡期保留，冒烟脚本仍读 window.rvOpenItems） */
export {
  WsReview, RV_KINDS, rvOpenItems, rvMarkResolved, rvPush, rvMigrateLegacy,
  rvResolveAction, rvReady, useReviewOpenItems,
};
