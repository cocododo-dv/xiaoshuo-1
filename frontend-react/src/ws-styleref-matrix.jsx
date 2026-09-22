import React from "react";
import { I } from "./icons.jsx";
import { EmptyState, Notice, Segmented, Spinner, Tag } from "./ws-ui.jsx";
import {
  SR_ACTIVITY_WHERE, SR_CONF_LABEL, SR_CONF_TONE, SR_LAYERS, SR_RESYNTH_REASON_LABEL, computeResynthState, srParagraphLabel, srSynthErrorMessage,
} from "./ws-styleref-model.js";
import { srActivityFor, srBookAction, srFindingFeedback, srReviewFinding, srSynthesize } from "./ws-styleref-store.js";
import { SR_ACTIVITY_KIND_LABEL, srActivityView } from "./ws-styleref-activity.jsx";
import { srNotify, useSrDeep, useSrStore } from "./ws-styleref-ui.jsx";

/* ==========================================================
   风格参考 · 维度矩阵：4 层 × 4 子维的格子（按真实观察着色）+ 右侧证据抽屉
   格子是 role=grid，方向键只在网格里移动；提示是纯 CSS（悬停 / 键盘聚焦）。
   ========================================================== */

/* 后端 finding → FindingCard 用的形状 */
function srAdaptFinding(f) {
  return {
    id: f.finding_id,
    conf: f.confidence,
    statement: f.statement,
    review: f.status || "pending",
    vote: f.user_vote || null,   // 回显当前用户已投的票（跨刷新持久）
    evidence: (f.evidence || []).map((e) => ({
      p: e.paragraph_id || null,
      quote: e.quote_text || "",
      kind: e.anchor_kind,
      synthetic: !!e.is_synthetic,
    })),
  };
}

/* 矩阵空态的引导：先看这本书正在跑什么（抽取 / 分类），再看书的状态，再看上一次抽取的结局，
   最后才是「开始抽取」。deep 可省略（只按书判断）。 */
export function srMatrixEmptyHint(book, deep = null) {
  const running = book ? srActivityFor(book.id) : null;
  if (running) {
    const v = srActivityView(running);
    return `正在${SR_ACTIVITY_KIND_LABEL[running.kind] || running.kind}：${v.detail}。完成后矩阵按真实观察点亮，${SR_ACTIVITY_WHERE}里可看进度。`;
  }
  const raw = book && book.rawStatus;
  if (raw === "failed") return "上次段落分类没有完成（失败或已取消）：在概览里「继续分类」，或删除后重新导入；分类完成前不能抽取。";
  if (raw === "ingesting" || raw === "cancelling") return `这本书的段落分类还在进行：等它完成后再抽取（${SR_ACTIVITY_WHERE}里可看进度）。`;
  const runStatus = deep && deep.run ? deep.run.status : null;
  if (runStatus === "done") return "上一次抽取完成了，但没有抽出任何观察（语料太少，或模型没给出够格的证据）。可以重跑抽取。";
  if (runStatus === "failed" || runStatus === "cancelled") return "上一次抽取没有完成。点「重跑抽取」再来一次（需已接入模型）。";
  return "这本书还没有抽取结果。点「开始抽取」启动后台抽取（需已接入模型），完成后矩阵按真实观察点亮。";
}

const SR_GRID_KEYS = { ArrowRight: [0, 1], ArrowLeft: [0, -1], ArrowDown: [1, 0], ArrowUp: [-1, 0] };

/* 一格的数据：input_assessment=skip 的层不可选；未抽取 / 该维度没有观察 → none */
function srCellData(layerId, sub, dimCounts, inputAssessment) {
  const path = `${layerId}.${sub.id}`;
  if (inputAssessment && inputAssessment[layerId] === "skip") return { path, name: sub.name, conf: "skip", obs: 0, fp: 0, q: 0, skip: true };
  const dc = dimCounts ? dimCounts[path] : null;
  if (!dc) return { path, name: sub.name, conf: "none", obs: 0, fp: 0, q: 0, skip: false };
  return { path, name: sub.name, conf: dc.conf, obs: dc.obs, fp: dc.fp, q: dc.q, mix: dc.mix || null, skip: false };
}

export function SrMatrix({ go, book }) {
  const deep = useSrDeep(book);
  useSrStore("activity");
  const inputAssessment = (deep && deep.book && deep.book.stats_json && deep.book.stats_json.input_assessment) || null;
  const hasFindings = !!(deep && deep.runId && deep.dimCounts && Object.keys(deep.dimCounts).length > 0);
  const [cell, setCell] = React.useState("language.sentence_structure");
  const [kindFilter, setKindFilter] = React.useState("all");
  const [synthBusy, setSynthBusy] = React.useState(false);
  const [startBusy, setStartBusy] = React.useState(false);
  const cellRefs = React.useRef({});

  const rows = SR_LAYERS.map((l) => ({ layer: l, cells: l.subs.map((s) => srCellData(l.id, s, hasFindings ? deep.dimCounts : null, inputAssessment)) }));
  const flatCells = rows.flatMap((r) => r.cells);
  /* 选中格若被跳过（该层语料不足），落到第一个可选格——否则网格里没有可 Tab 进入的格子 */
  const activePath = (flatCells.find((c) => c.path === cell && !c.skip) || flatCells.find((c) => !c.skip) || {}).path || null;
  const activeRow = rows.find((r) => r.cells.some((c) => c.path === activePath)) || null;
  const activeCell = activeRow ? activeRow.cells.find((c) => c.path === activePath) : null;

  // 抽屉：有观察时取 deep.findingsByDim[格子]；还没有抽取产物 → null（空态）
  const group = hasFindings && activePath ? deep.findingsByDim[activePath] : null;
  const findings = group
    ? { observations: group.observations.map(srAdaptFinding), forbidden_patterns: group.forbidden_patterns.map(srAdaptFinding) }
    : null;
  /* 审核 / 投票返回 Promise：失败时由 FindingCard 回滚并提示 */
  const onReview = hasFindings ? (findingId, decision) => srReviewFinding(findingId, decision, book.id) : null;
  const onVote = hasFindings ? (findingId, vote) => srFindingFeedback(findingId, vote, book.id) : null;

  /* 方向键只在网格里生效（挂在 window 上会抢走整页的滚动和单选组的方向键） */
  const onGridKey = (e) => {
    const dir = SR_GRID_KEYS[e.key];
    if (!dir || e.metaKey || e.ctrlKey || e.altKey) return;
    e.preventDefault();
    const grid = rows.map((row) => row.cells);
    let r = 0; let c = 0;
    grid.forEach((row, ri) => row.forEach((x, ci) => { if (x.path === activePath) { r = ri; c = ci; } }));
    for (let i = 0; i < 6; i++) {
      r += dir[0]; c += dir[1];
      if (r < 0 || r >= grid.length || c < 0 || c >= grid[r].length) return;
      const target = grid[r][c];
      if (!target.skip) {
        setCell(target.path);
        const node = cellRefs.current[target.path];
        if (node) node.focus();
        return;
      }
    }
  };

  const totals = hasFindings
    ? Object.values(deep.dimCounts).reduce((a, d) => ({ obs: a.obs + d.obs, fp: a.fp + d.fp, q: a.q + d.q }), { obs: 0, fp: 0, q: 0 })
    : { obs: 0, fp: 0, q: 0 };
  const hasProfile = !!(deep && deep.profileId);
  /* 再合成：有新 run / 画像 stale / 画像非 active 时允许再次调用 synthesize，
     否则（画像 active 且对应最新 run）按钮只导航到画像页 */
  const resynth = computeResynthState(deep);
  const synthAllowed = hasFindings && !!deep.runId && (!hasProfile || resynth.canResynth);
  const running = srActivityFor(book.id);
  const notReady = !!(book.rawStatus && book.rawStatus !== "ready");
  /* 抽取跑过（done）但一条观察都没有：格子写「暂无观察」，不写「未抽取」 */
  const ranEmpty = !hasFindings && !!(deep && deep.run && deep.run.status === "done");
  const noneLabel = hasFindings || ranEmpty ? SR_CONF_LABEL.none : "未抽取";

  const onSynth = async () => {
    if (!synthAllowed) { if (hasProfile && go) go("profile"); return; }
    if (synthBusy) return;
    setSynthBusy(true);
    try {
      await srSynthesize(deep.runId, book.id);
      if (go) go("profile");
    } catch (e) {
      srNotify(srSynthErrorMessage(e));
    } finally { setSynthBusy(false); }
  };
  const onStart = async () => {
    if (startBusy) return;
    setStartBusy(true);
    try { await srBookAction("rerun", book.id); } finally { setStartBusy(false); }
  };
  const synthLabel = !hasProfile ? "合成风格画像" : synthAllowed ? "重新合成画像" : "查看风格画像";
  const obsCount = findings ? findings.observations.length : 0;
  const fpCount = findings ? findings.forbidden_patterns.length : 0;

  return (
    <div className="sr-matrix-wrap">
      <div className="sr-matrix-side">
        {!hasFindings && (
          <Notice
            tone={!running && book.rawStatus === "failed" ? "warn" : "info"}
            className="sr-matrix-empty"
            testId="sr-matrix-empty"
            actions={!running && !notReady ? (
              <button type="button" className="btn btn-accent btn-sm" disabled={startBusy} onClick={onStart}>
                {startBusy ? <><Spinner size={12} /> 启动中…</> : deep && deep.run ? "重跑抽取" : "开始抽取"}
              </button>
            ) : null}
          >
            {srMatrixEmptyHint(book, deep)}
          </Notice>
        )}
        <div className="sr-matrix-legend">
          <span className="sr-matrix-legend-label">格子颜色</span>
          <span className="sr-lg sr-lg-high">高置信</span>
          <span className="sr-lg sr-lg-medium">中置信</span>
          <span className="sr-lg sr-lg-low">低置信</span>
          <span className="sr-lg sr-lg-none">{noneLabel}</span>
          <span className="sr-lg sr-lg-skip">语料不足</span>
          <span className="sr-matrix-kbd" aria-hidden="true"><kbd>←</kbd><kbd>↑</kbd><kbd>↓</kbd><kbd>→</kbd> 在格子间移动</span>
        </div>

        <div className="sr-matrix" role="grid" aria-label="16 个风格维度" onKeyDown={onGridKey}>
          {rows.map(({ layer: l, cells }) => (
            <div key={l.id} className="sr-matrix-row" role="row">
              <div className="sr-matrix-rowhead" role="rowheader">
                <span className="sr-matrix-abbr" aria-hidden="true">{l.abbr}</span>
                <span className="sr-matrix-layer">{l.name}</span>
              </div>
              {cells.map((c) => (
                <SrCell
                  key={c.path}
                  layerName={l.name}
                  cell={c}
                  selected={activePath === c.path}
                  noneLabel={noneLabel}
                  cellRef={(node) => { cellRefs.current[c.path] = node; }}
                  onSelect={() => { if (!c.skip) setCell(c.path); }}
                />
              ))}
            </div>
          ))}
        </div>

        <div className="sr-matrix-foot">
          <div className="sr-matrix-foot-stat"><b className="tab-num">{totals.obs}</b> 观察</div>
          <div className="sr-matrix-foot-stat"><b className="tab-num">{totals.fp}</b> 禁忌</div>
          <div className="sr-matrix-foot-stat"><b className="tab-num">{totals.q}</b> 引文</div>
          <div className="flex-1" />
          {hasProfile && synthAllowed && resynth.reason && (
            <Tag tone="warn" dot testId="sr-matrix-resynth-reason">{SR_RESYNTH_REASON_LABEL[resynth.reason] || resynth.reason}</Tag>
          )}
          {hasProfile && synthAllowed && (
            <button type="button" className="btn btn-quiet btn-sm" disabled={synthBusy} onClick={() => go && go("profile")}>查看画像</button>
          )}
          <button type="button" className="btn btn-accent btn-sm" data-testid="sr-matrix-synth" disabled={synthBusy || (!hasFindings && !hasProfile)} title={!hasFindings && !hasProfile ? "先完成抽取再合成画像" : undefined} onClick={onSynth}>
            {synthBusy ? <><Spinner size={12} /> 合成中…</>
              : <><I.Sparkles size={13} /> {synthLabel}</>}
          </button>
        </div>
        {!hasFindings && !hasProfile && <p className="sr-matrix-foot-hint">抽出观察后才能合成画像。</p>}
      </div>

      <aside className="sr-findings" aria-label="这个维度的观察与证据">
        {activeCell && (
          <header className="sr-findings-head">
            <div>
              <div className="sr-findings-crumb">{activeRow.layer.name}</div>
              <h3 className="sr-findings-title text-serif">{activeCell.name}</h3>
            </div>
            {SR_CONF_TONE[activeCell.conf] && <Tag tone={SR_CONF_TONE[activeCell.conf]} dot>{SR_CONF_LABEL[activeCell.conf]}</Tag>}
          </header>
        )}

        <div className="sr-findings-filter">
          <Segmented
            label="按类型筛选"
            value={kindFilter}
            onChange={setKindFilter}
            options={[
              { value: "all", label: "全部" },
              { value: "obs", label: "观察", count: obsCount },
              { value: "fp", label: "禁忌", count: fpCount },
            ]}
          />
        </div>

        <div className="sr-findings-scroll" key={activePath || "none"}>
          {!findings && (
            <EmptyState icon="Quote" compact>
              {!hasFindings && !ranEmpty ? "还没有抽取。抽取完成后，这里显示这个维度的观察与原文证据。"
                : "这个维度没有抽出观察（语料不足或模型没给出够格的证据）。"}
            </EmptyState>
          )}
          {findings && (kindFilter === "all" || kindFilter === "obs") && findings.observations.map((o) => (
            <FindingCard key={o.id} kind="obs" finding={o} onReview={onReview ? (d) => onReview(o.id, d) : null} onVote={onVote ? (v) => onVote(o.id, v) : null} />
          ))}
          {findings && (kindFilter === "all" || kindFilter === "fp") && findings.forbidden_patterns.map((f) => (
            <FindingCard key={f.id} kind="fp" finding={f} onReview={onReview ? (d) => onReview(f.id, d) : null} onVote={onVote ? (v) => onVote(f.id, v) : null} />
          ))}
        </div>
      </aside>
    </div>
  );
}

/* 一个格子：名字 + 计数（或「语料不足」「未抽取」），悬停 / 聚焦时出提示。 */
function SrCell({ layerName, cell: c, selected, noneLabel, cellRef, onSelect }) {
  const confLabel = c.skip ? SR_CONF_LABEL.skip : c.conf === "none" ? noneLabel : SR_CONF_LABEL[c.conf] || c.conf;
  const mix = c.mix ? ["high", "medium", "low"].filter((k) => c.mix[k]).map((k) => `${SR_CONF_LABEL[k].slice(0, 1)} ${c.mix[k]}`).join(" · ") : "";
  return (
    <button
      type="button"
      role="gridcell"
      ref={cellRef}
      className={`sr-cell conf-${c.conf} ${selected ? "is-active" : ""}`}
      aria-selected={selected}
      aria-disabled={c.skip || undefined}
      aria-label={`${layerName}·${c.name}：${confLabel}${c.skip ? "" : `，${c.obs} 条观察、${c.q} 条引文、${c.fp} 条禁忌`}`}
      tabIndex={selected ? 0 : -1}
      onClick={onSelect}
    >
      <span className="sr-cell-name">{c.name}</span>
      {c.skip || c.conf === "none" ? (
        <span className="sr-cell-skip">{confLabel}</span>
      ) : (
        <span className="sr-cell-stats">
          <span className="sr-cell-stat"><b>{c.obs}</b> 观察</span>
          <span className="sr-cell-stat"><b>{c.q}</b> 引文</span>
          {c.fp > 0 && <span className="sr-cell-stat fp"><b>{c.fp}</b> 禁忌</span>}
        </span>
      )}
      {!c.skip && c.conf !== "none" && (
        <span className="sr-cell-tip" aria-hidden="true">
          <span className="sr-cell-tip-conf"><span className={`conf-dot conf-${c.conf}`} />{confLabel}{mix ? `（${mix}）` : ""}</span>
          <span className="sr-cell-tip-hint">点击或按回车查看证据</span>
        </span>
      )}
    </button>
  );
}

/* 一条观察 / 禁忌：审核与投票先改界面，再等服务端；失败就回到原来的状态并提示。请求在路上时按钮禁用。 */
function FindingCard({ kind, finding, onReview, onVote }) {
  const isFp = kind === "fp";
  const [review, setReview] = React.useState(finding.review || "pending");
  const [vote, setVote] = React.useState(finding.vote || null);
  const [busy, setBusy] = React.useState(null);
  // 深层数据重载后 finding.review / vote 变化 → 同步（同 key 实例不会重跑 initializer）
  React.useEffect(() => { setReview(finding.review || "pending"); }, [finding.review]);
  React.useEffect(() => { setVote(finding.vote || null); }, [finding.vote]);

  const commit = async (field, next) => {
    if (busy) return;
    const send = field === "review" ? onReview : onVote;
    const prev = field === "review" ? review : vote;
    const set = field === "review" ? setReview : setVote;
    set(next);
    if (!send) return;
    setBusy(field);
    try {
      await send(next);
    } catch (e) {
      set(prev);
      srNotify(`${field === "review" ? "审核" : "反馈"}没有保存：${(e && e.message) || e}`);
    } finally {
      setBusy(null);
    }
  };
  // 反馈没有「撤票」语义（后端幂等，可改向）：点已投的那一票不再发请求
  const castVote = (v) => { if (vote !== v) commit("vote", v); };
  const reviewLabel = review === "approved" ? "已通过" : review === "rejected" ? "已驳回" : "待审";
  return (
    <article className={`sr-finding ${isFp ? "is-fp" : ""} rev-${review}`} aria-busy={busy ? "true" : undefined}>
      <header className="sr-finding-head">
        <Tag tone={isFp ? "danger" : "ok"} className="sr-finding-tag">
          {isFp ? <I.Ban size={11} /> : <I.Check size={11} />}
          {isFp ? "禁忌" : "观察"}
        </Tag>
        {!isFp && finding.conf && SR_CONF_TONE[finding.conf] && (
          <Tag tone={SR_CONF_TONE[finding.conf]} outline>{SR_CONF_LABEL[finding.conf]}</Tag>
        )}
        <span className={`sr-rev-state st-${review}`}>{reviewLabel}</span>
        <div className="sr-finding-actions">
          <button type="button" className={`sr-rev-btn ${review === "approved" ? "on-ok" : ""}`} title="通过" aria-label="通过这条" aria-pressed={review === "approved"} disabled={!!busy} onClick={() => commit("review", review === "approved" ? "pending" : "approved")}><I.Check size={13} /></button>
          <button type="button" className={`sr-rev-btn ${review === "rejected" ? "on-no" : ""}`} title="驳回" aria-label="驳回这条" aria-pressed={review === "rejected"} disabled={!!busy} onClick={() => commit("review", review === "rejected" ? "pending" : "rejected")}><I.X size={13} /></button>
        </div>
      </header>
      <p className="sr-finding-statement text-serif">{finding.statement}</p>
      <div className="sr-finding-evidence">
        <div className="sr-finding-evidence-label">
          证据 {finding.evidence.length} 条{finding.evidence.length < 2 ? "（不足两条）" : ""}
        </div>
        {finding.evidence.map((e, i) => {
          const para = srParagraphLabel(e.p);
          return (
            <div key={i} className="sr-ev">
              <div className="sr-ev-mark">
                {e.kind === "counter_example" || e.synthetic ? <span className="sr-ev-badge syn">合成反例</span>
                  : e.kind === "author_avoidance" ? <span className="sr-ev-badge avoid">作者刻意回避</span>
                  : <span className="sr-ev-badge quote" title={e.p || undefined}>{para || "引文"}</span>}
              </div>
              {e.quote && <p className="sr-ev-quote text-serif">{e.quote}</p>}
            </div>
          );
        })}
      </div>
      <footer className="sr-finding-foot">
        <span className="sr-finding-ask">这条准吗？</span>
        <div className="sr-vote">
          <button type="button" className={`sr-vote-btn ${vote === "up" ? "on" : ""}`} aria-label="准" title="准" aria-pressed={vote === "up"} disabled={!!busy} onClick={() => castVote("up")}><I.ThumbsUp size={13} /></button>
          <button type="button" className={`sr-vote-btn ${vote === "down" ? "on" : ""}`} aria-label="不准" title="不准" aria-pressed={vote === "down"} disabled={!!busy} onClick={() => castVote("down")}><I.ThumbsDown size={13} /></button>
        </div>
        <span className="sr-finding-note">反馈会调整这条观察的置信度</span>
      </footer>
    </article>
  );
}
