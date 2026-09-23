import React from "react";
import { I } from "./icons.jsx";
import { Notice, Tag } from "./ws-ui.jsx";
import {
  FID_STAGE_LABELS, fidBadgeView, fidDimensionGroups, fidJudgeView, fidReadingView, fidScoreText, fidScoreTone,
  fidScoreWord, fidTrendPoints,
} from "./ws-fidelity-model.js";

/* ==========================================================
   「像不像」的共用界面件（2026-09-23 风格参考 v3 · P6b）
   风格参考的「对照检查」、起草台证据栏的「像不像」、成稿中心的角标、文风画像的作品卡都用这几件：
   · FidelityHeadline —— 位次（第 72 位 / 100）+ 在不在作者的正常范围 + 一句解释 + 量不准的原因
   · FidelityGaps —— 和作者不一样的地方（后端给的白话短语，按维度名标注；从不给 z 分）
   · FidelityMeter / FidelityDimensionTable —— 0–10 的分数条；16 维按层：测得（按字数统计的几维）/ 评审（模型打分）
   · FidelityCopyLine —— 照搬检查一句话（只有计数）
   · FidelityBadge —— 成稿中心按场的小角标
   · FidelityTrend —— 作品最近读数的走势（终稿一条线、首稿作背景点；悬停 / 方向键看每一点，附表格）
   说法都在 ws-fidelity-model.js。不写 window。
   ========================================================== */

const { useEffect, useLayoutEffect, useRef, useState } = React;

/* ---------- 位次 + 判断 + 解释 ---------- */
export function FidelityHeadline({ reading, size = "lg", testId }) {
  const view = fidReadingView(reading);
  if (!view || view.rank == null) return null;
  return (
    <div className="fid-head" data-size={size} data-testid={testId}>
      <div className="fid-head-line">
        <span className="fid-rank" aria-label={`第 ${view.rank} 位，共 100 位`}>
          <span className="fid-rank-word" aria-hidden="true">第</span>
          <span className="fid-rank-num" aria-hidden="true">{view.rank}</span>
          <span className="fid-rank-word" aria-hidden="true">位</span>
          <span className="fid-rank-of" aria-hidden="true">/ 100</span>
        </span>
        {view.verdict && <Tag tone={view.verdict.tone} dot testId={testId ? `${testId}-verdict` : undefined}>{view.verdict.label}</Tag>}
      </div>
      {view.explain && <p className="fid-head-explain">{view.explain}</p>}
      {view.unreliable && <p className="fid-head-caveat" data-testid={testId ? `${testId}-caveat` : undefined}><I.Info size={13} aria-hidden="true" /> {view.unreliable}</p>}
    </div>
  );
}

/* ---------- 和作者不一样的地方 ---------- */
export function FidelityGaps({ reading, limit = 6, title = "和作者不一样的地方", emptyText = "测得出的写作习惯都在作者的常态里。", testId }) {
  const [all, setAll] = useState(false);
  const view = fidReadingView(reading);
  if (!view) return null;
  const gaps = view.gaps;
  const shown = all ? gaps : gaps.slice(0, limit);
  return (
    <div className="fid-gaps" data-testid={testId}>
      <div className="fid-sub">{title}</div>
      {gaps.length === 0 ? (
        <p className="fid-muted">{emptyText}</p>
      ) : (
        <ul className="fid-gap-list">
          {shown.map((gap) => (
            <li key={gap.key} className="fid-gap">
              {gap.label && <span className="fid-gap-dim">{gap.label}</span>}
              <span className="fid-gap-phrase">{gap.phrase}</span>
            </li>
          ))}
        </ul>
      )}
      {gaps.length > limit && (
        <button type="button" className="btn btn-quiet btn-xs fid-more" aria-expanded={all} onClick={() => setAll((v) => !v)}>
          {all ? "收起" : `另有 ${gaps.length - limit} 条`}
        </button>
      )}
    </div>
  );
}

/* ---------- 0–10 分数条 ---------- */
export function FidelityMeter({ score, label = "" }) {
  const text = fidScoreText(score);
  if (text === "—") return <span className="fid-meter is-empty">—</span>;
  const width = Math.max(0, Math.min(100, Number(score) * 10));
  return (
    <span className="fid-meter" data-tone={fidScoreTone(score)} role="img" aria-label={`${label ? `${label}：` : ""}${text} 分（满分 10），${fidScoreWord(score)}`}>
      <span className="fid-meter-track" aria-hidden="true"><span className="fid-meter-fill" style={{ width: `${width}%` }} /></span>
      <span className="fid-meter-num tab-num" aria-hidden="true">{text}</span>
    </span>
  );
}

const STATE_TAG = { emphasize: { tone: "accent", label: "重点" }, exclude: { tone: "neutral", label: "不学" } };

/* 16 维按层的表：测得 / 评审 / 评审说明。states：给这部作品设的维度状态（可无） */
export function FidelityDimensionTable({ reading, judge, states = null, testId }) {
  const groups = fidDimensionGroups(reading, judge, { states });
  const judged = fidJudgeView(judge);
  const anyMeasured = groups.some((g) => g.rows.some((r) => r.measured != null));
  if (!anyMeasured && !judged) return null;
  return (
    <div className="fid-dims" data-testid={testId}>
      <div className="fid-table-scroll" role="region" aria-label="按维度的分数" tabIndex={0}>
        <table className="fid-table">
          <thead>
            <tr>
              <th scope="col">维度</th>
              <th scope="col" title="按字数统计出来的写作习惯（句长、标点、对白……），只有几维能这样测">测得</th>
              <th scope="col" title="模型对着参考书的原文样例和文风卡，按这一维打的分">评审</th>
              <th scope="col" className="fid-table-note">评审说明</th>
            </tr>
          </thead>
          {groups.map((group) => (
            <tbody key={group.layer}>
              <tr className="fid-table-layer"><th scope="rowgroup" colSpan={4}>{group.label}</th></tr>
              {group.rows.map((row) => {
                const state = STATE_TAG[row.state];
                return (
                  <tr key={row.dimension} data-dimension={row.dimension} className={row.state === "exclude" ? "is-excluded" : undefined}>
                    <th scope="row" className="fid-table-dim">
                      {row.label}
                      {state && <Tag tone={state.tone} outline>{state.label}</Tag>}
                    </th>
                    <td><FidelityMeter score={row.measured} label={`${row.label} · 测得`} /></td>
                    <td><FidelityMeter score={row.judged} label={`${row.label} · 评审`} /></td>
                    <td className="fid-table-note">{row.note || ""}</td>
                  </tr>
                );
              })}
            </tbody>
          ))}
        </table>
      </div>
      <p className="fid-foot">测得：只有句子、词汇、标点、视角、节奏、信息密度、对白这几维能按字数统计，其余维「—」；评审：模型对着原文样例打的分。分数 7 以上算像，5 以下差得远。</p>
    </div>
  );
}

/* 评审总分一行 */
export function FidelityJudgeLine({ judge, testId }) {
  const view = fidJudgeView(judge);
  if (!view || view.overall == null) return null;
  return (
    <p className="fid-judge" data-testid={testId}>
      <span className="fid-judge-label">参考评审</span>
      <FidelityMeter score={view.overall} label="参考评审总分" />
      {view.summary && <span className="fid-judge-summary">{view.summary}</span>}
    </p>
  );
}

/* ---------- 照搬检查 ---------- */
export function FidelityCopyLine({ reading, testId }) {
  const view = fidReadingView(reading);
  if (!view || !view.copy) return null;
  const Icon = view.copy.tone === "ok" ? I.ShieldCheck : I.AlertTriangle;
  return (
    <p className="fid-copy" data-tone={view.copy.tone} data-testid={testId}>
      <Icon size={13} aria-hidden="true" /> <span className="fid-copy-label">照搬检查</span> {view.copy.text}
    </p>
  );
}

/* ---------- 成稿中心的角标 ---------- */
export function FidelityBadge({ final, testId }) {
  const view = fidBadgeView(final);
  if (!view) return null;
  return <Tag tone={view.tone} title={view.title} testId={testId} className="fid-badge">{view.text}</Tag>;
}

/* ==========================================================
   走势：作品最近的读数（时间顺序）。终稿是要看的那条（强调色、连线）；首稿是背景（石板色点）。
   纵轴是位次：第 1 位在上（越靠上越像），灰带是作者的正常范围。悬停 / 方向键看每一点；附一张表格。
   ========================================================== */

const TREND_H = 150;
const PAD = { top: 12, right: 20, bottom: 12, left: 44 };

function useWidth(ref, fallback) {
  const [width, setWidth] = useState(fallback);
  useLayoutEffect(() => {
    const el = ref.current;
    if (!el) return undefined;
    const measure = () => { const w = el.clientWidth; if (w > 0) setWidth(w); };
    measure();
    if (typeof ResizeObserver === "undefined") return undefined;
    const observer = new ResizeObserver(measure);
    observer.observe(el);
    return () => observer.disconnect();
  }, [ref]);
  return width;
}

function fmtWhen(iso) {
  const d = iso ? new Date(iso) : null;
  if (!d || Number.isNaN(d.getTime())) return "";
  return `${d.getMonth() + 1} 月 ${d.getDate()} 日 ${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`;
}

function pointText(point, labelOf) {
  const where = (labelOf && point.sceneId ? labelOf(point.sceneId) : "") || "不在场景上的文字";
  const range = point.reliable ? (point.within ? "在作者的正常范围内" : "超出作者的正常范围") : "量不准";
  return { where, what: `${FID_STAGE_LABELS[point.stage] || ""} · 第 ${point.rank} 位 · ${range}`, when: fmtWhen(point.at) };
}

export function FidelityTrend({ trend, labelOf = null, maxPercentile = 90, testId }) {
  const points = fidTrendPoints(trend);
  const boxRef = useRef(null);
  const width = useWidth(boxRef, 640);
  const [active, setActive] = useState(null);
  const [table, setTable] = useState(false);
  useEffect(() => { setActive(null); }, [points.length]);
  if (!points.length) return null;

  const plotW = Math.max(40, width - PAD.left - PAD.right);
  const step = points.length > 1 ? plotW / (points.length - 1) : 0;
  const xOf = (i) => PAD.left + (points.length > 1 ? i * step : plotW / 2);
  const yOf = (rank) => PAD.top + ((rank - 1) / 99) * (TREND_H - PAD.top - PAD.bottom);
  const finals = points.filter((p) => p.stage === "final");
  const drafts = points.filter((p) => p.stage !== "final");
  const lastFinal = finals.length ? finals[finals.length - 1] : null;
  // 正常范围的上限：最近一条读数入库时记下的（阈值改过的话以最近的为准），没有就用调用方给的
  const recorded = [...points].reverse().find((p) => p.maxPercentile != null);
  const threshold = Math.max(1, Math.min(100, Math.round(Number(recorded ? recorded.maxPercentile : maxPercentile) || 90)));
  const finalsWithin = finals.filter((p) => p.reliable && p.within).length;
  const summary = `最近 ${points.length} 次读数：终稿 ${finals.length} 次，其中 ${finalsWithin} 次在作者的正常范围内`
    + (lastFinal ? `；最近一次终稿第 ${lastFinal.rank} 位` : "");
  const current = active != null ? points[active] : null;
  const tip = current ? pointText(current, labelOf) : null;

  const pickNearest = (clientX) => {
    const rect = boxRef.current ? boxRef.current.getBoundingClientRect() : null;
    if (!rect) return;
    const x = clientX - rect.left;
    let best = 0;
    points.forEach((p, i) => { if (Math.abs(xOf(i) - x) < Math.abs(xOf(best) - x)) best = i; });
    setActive(best);
  };
  const onKeyDown = (e) => {
    if (e.key !== "ArrowLeft" && e.key !== "ArrowRight" && e.key !== "Home" && e.key !== "End") return;
    e.preventDefault();
    setActive((i) => {
      if (e.key === "Home") return 0;
      if (e.key === "End") return points.length - 1;
      if (i == null) return e.key === "ArrowLeft" ? points.length - 1 : 0;
      return Math.max(0, Math.min(points.length - 1, i + (e.key === "ArrowRight" ? 1 : -1)));
    });
  };
  const line = finals.map((p) => `${xOf(p.index)},${yOf(p.rank)}`).join(" ");
  const bandBottom = yOf(threshold);

  return (
    <div className="fid-trend" data-testid={testId}>
      <div className="fid-trend-top">
        <ul className="fid-legend" aria-label="图例">
          <li><span className="fid-key is-final" aria-hidden="true" />终稿</li>
          <li><span className="fid-key is-draft" aria-hidden="true" />首稿</li>
          <li><span className="fid-key is-band" aria-hidden="true" />作者的正常范围（前 {threshold} 位）</li>
        </ul>
        <button type="button" className="btn btn-quiet btn-xs" aria-pressed={table} data-testid={testId ? `${testId}-table-toggle` : undefined} onClick={() => setTable((v) => !v)}>
          <I.List size={12} /> {table ? "看图" : "看表格"}
        </button>
      </div>
      {!table ? (
        <div
          className="fid-trend-plot"
          ref={boxRef}
          tabIndex={0}
          role="group"
          aria-label={`${summary}。用左右方向键逐点查看。`}
          onKeyDown={onKeyDown}
          onMouseMove={(e) => pickNearest(e.clientX)}
          onMouseLeave={() => setActive(null)}
          onBlur={() => setActive(null)}
        >
          <svg width={width} height={TREND_H} role="img" aria-label={summary}>
            <rect className="fid-trend-band" x={PAD.left} y={PAD.top} width={plotW} height={Math.max(0, bandBottom - PAD.top)} />
            {[1, 50, 100].map((rank) => (
              <g key={rank}>
                <line className="fid-trend-grid" x1={PAD.left} x2={PAD.left + plotW} y1={yOf(rank)} y2={yOf(rank)} />
                <text className="fid-trend-axis" x={PAD.left - 8} y={yOf(rank)} dy="0.32em" textAnchor="end">{`第${rank}位`}</text>
              </g>
            ))}
            {current && <line className="fid-trend-cross" x1={xOf(current.index)} x2={xOf(current.index)} y1={PAD.top} y2={TREND_H - PAD.bottom} />}
            {drafts.map((p) => (
              <circle key={p.readingId} className={`fid-trend-dot is-draft${p.reliable ? "" : " is-unreliable"}`} cx={xOf(p.index)} cy={yOf(p.rank)} r={4} />
            ))}
            {finals.length > 1 && <polyline className="fid-trend-line" points={line} />}
            {finals.map((p) => (
              <circle key={p.readingId} className={`fid-trend-dot is-final${p.reliable ? "" : " is-unreliable"}${current && current.index === p.index ? " is-active" : ""}`} cx={xOf(p.index)} cy={yOf(p.rank)} r={4.5} />
            ))}
            {lastFinal && (
              <text className="fid-trend-label" x={Math.min(xOf(lastFinal.index) + 8, PAD.left + plotW - 2)} y={yOf(lastFinal.rank) - 8} textAnchor={xOf(lastFinal.index) + 60 > PAD.left + plotW ? "end" : "start"}>
                {`第 ${lastFinal.rank} 位`}
              </text>
            )}
          </svg>
          {tip && (
            <div
              className="fid-tip"
              role="status"
              style={{ left: `${Math.min(Math.max(xOf(current.index), 90), width - 90)}px`, top: `${Math.max(0, yOf(current.rank) - 12)}px` }}
            >
              <b className="fid-tip-value">{tip.what}</b>
              <span className="fid-tip-where">{tip.where}</span>
              {tip.when && <span className="fid-tip-when">{tip.when}</span>}
            </div>
          )}
          <div className="fid-trend-axis-row" aria-hidden="true">
            <span>{fmtWhen(points[0].at)}</span>
            <span>{points.length > 1 ? fmtWhen(points[points.length - 1].at) : ""}</span>
          </div>
        </div>
      ) : (
        <div className="fid-table-scroll" role="region" aria-label="读数走势（表格）" tabIndex={0}>
          <table className="fid-table fid-trend-table" data-testid={testId ? `${testId}-table` : undefined}>
            <thead><tr><th scope="col">时间</th><th scope="col">哪一场</th><th scope="col">稿</th><th scope="col">位次</th><th scope="col">范围</th></tr></thead>
            <tbody>
              {points.slice().reverse().map((p) => {
                const text = pointText(p, labelOf);
                return (
                  <tr key={p.readingId}>
                    <td className="tab-num">{text.when || "—"}</td>
                    <td>{text.where}</td>
                    <td>{FID_STAGE_LABELS[p.stage]}</td>
                    <td className="tab-num">第 {p.rank} 位</td>
                    <td>{p.reliable ? (p.within ? "在范围内" : "超出范围") : "量不准"}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

/* 出错一句话 + 下一步（fidErrorInfo 的结果；onAction(action) 由页面决定怎么走） */
export function FidelityErrorLine({ info, onAction, testId }) {
  if (!info) return null;
  return (
    <Notice
      tone="danger"
      testId={testId}
      actions={info.action && onAction ? (
        <button type="button" className="btn btn-ghost btn-sm" data-testid={testId ? `${testId}-action` : undefined} onClick={() => onAction(info.action)}>{info.action.label}</button>
      ) : null}
    >
      <span title={info.code ? `错误代码：${info.code}` : undefined}>{info.message}</span>
    </Notice>
  );
}
