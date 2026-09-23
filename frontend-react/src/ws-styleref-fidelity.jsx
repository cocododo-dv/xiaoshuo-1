import React from "react";
import { StatTile, Tag } from "./ws-ui.jsx";
import { fidFinalsSummary, fidGapsByDimension, fidScoreText } from "./ws-fidelity-model.js";
import { fidLoadProject, fidProject, useFidelityStore } from "./ws-fidelity-store.js";
import { FidelityMeter, FidelityTrend } from "./ws-fidelity-ui.jsx";
import { srSceneLabel } from "./ws-styleref-model.js";
import { srLoadWorkScenes } from "./ws-styleref-store.js";

/* ==========================================================
   风格参考 · 文风画像里「当前作品像不像」（2026-09-23 风格参考 v3 · P6b）
   读 GET /api/v1/projects/{id}/style-fidelity（作品当前绑定的那份画像的读数）：
   · 画像页顶上一张卡：终稿有几场在作者范围内、近期常见偏差几条（下一场首稿会被特别提醒）、最近首稿与终稿的走势；
   · 每一维右边：这部作品各场在这一维上的平均分（测得 = 按字数统计；评审 = 模型对着样例打的分，0–10），
     是近期常见偏差的维标出来；展开那一维看是哪几句偏差、最近几次首稿里出现了几次。
   只在这本书正用于当前作品时出现（读数是对着作品现在用的这份画像量的）。不写 window。
   ========================================================== */

/* 当前作品对着这份画像的读数：{ data, averages, gapsByDim }；这本书没用于当前作品 / 还没读到时 data 为 null */
export function useSrWorkFidelity(workId, profileId) {
  useFidelityStore();
  const enabled = !!(workId && profileId);
  React.useEffect(() => { if (enabled) fidLoadProject(workId, { force: true }); }, [enabled, workId, profileId]);
  const entry = enabled ? fidProject(workId) : null;
  const data = entry && entry.data;
  if (!data || !data.bound || data.profile_id !== profileId) return { data: null, averages: {}, gapsByDim: {} };
  return {
    data,
    averages: data.dimension_averages && typeof data.dimension_averages === "object" ? data.dimension_averages : {},
    gapsByDim: fidGapsByDimension(data.recent_gap_details),
  };
}

export function SrWorkFidelityCard({ data, workId, workTitle, go }) {
  const [chapters, setChapters] = React.useState(null);
  React.useEffect(() => {
    let alive = true;
    if (!workId) return undefined;
    srLoadWorkScenes(workId).then((list) => { if (alive) setChapters(list); }).catch(() => { if (alive) setChapters([]); });
    return () => { alive = false; };
  }, [workId]);
  if (!data) return null;
  const finals = fidFinalsSummary(data.scene_finals);
  const gaps = Array.isArray(data.recent_gap_details) ? data.recent_gap_details : [];
  const trend = Array.isArray(data.trend) ? data.trend : [];
  const labelOf = (sceneId) => srSceneLabel(chapters, sceneId);
  return (
    <div className="card sr-work-fid" data-testid="sr-work-fidelity">
      <div className="card-head">
        <div>
          <div className="card-title">《{workTitle}》像不像</div>
          <div className="card-sub">把这位作者自己书里的段落按像不像作者本人排成 100 位（第 1 位最像）：每一场的首稿和终稿排在哪。</div>
        </div>
        {go && <button type="button" className="btn btn-ghost btn-sm" onClick={() => go("check")}>对照检查一段</button>}
      </div>
      {!trend.length && !finals ? (
        <p className="sr-ov-text sr-ov-muted" data-testid="sr-work-fidelity-empty">还没有读数：起草几场，或在「对照检查」里查一场之后，这里会显示走势。</p>
      ) : (
        <>
          <div className="sr-work-fid-stats">
            <StatTile
              label="终稿在作者范围内"
              value={finals ? `${finals.within} / ${finals.total}` : "—"}
              unit="场"
              tone={finals && finals.total && finals.within === finals.total ? "ok" : undefined}
              testId="sr-work-fid-finals"
            />
            <StatTile label="近期常见偏差" value={gaps.length} unit="条" hint="下一场首稿会被特别提醒" testId="sr-work-fid-gaps" />
          </div>
          <FidelityTrend trend={trend} labelOf={labelOf} testId="sr-work-trend" />
          {gaps.length > 0 && (
            <div className="sr-work-gaps">
              <div className="fid-sub">近期常见偏差（最近 {gaps[0].window || 5} 次首稿里反复出现的）</div>
              <ul className="fid-gap-list">
                {gaps.map((gap) => (
                  <li key={`${gap.feature}:${gap.direction}`} className="fid-gap">
                    {gap.dimension_label && <span className="fid-gap-dim">{gap.dimension_label}</span>}
                    <span className="fid-gap-phrase">{gap.phrase}（{gap.hits} 次）</span>
                  </li>
                ))}
              </ul>
            </div>
          )}
          <p className="sr-ov-hint">下面每一维右边是《{workTitle}》各场在这一维上的平均分：测得 = 按字数统计的写作习惯，评审 = 模型对着原文样例打的分（0–10，7 以上算像）。</p>
        </>
      )}
    </div>
  );
}

/* 一维的标题行上：作品在这一维的平均分（测得 / 评审）与「近期常见偏差」 */
export function SrDimensionFidelityChip({ fidelity, gaps }) {
  const measured = fidelity && fidelity.deterministic != null ? fidelity.deterministic : null;
  const judged = fidelity && fidelity.judge != null ? fidelity.judge : null;
  const hasGaps = Array.isArray(gaps) && gaps.length > 0;
  if (measured == null && judged == null && !hasGaps) return null;
  return (
    <span className="sr-dim-fid" data-testid="sr-dim-fid">
      {measured != null && (
        <span className="sr-dim-fid-item" title={`这一维测得的平均（${fidelity.scenes || 0} 场终稿）`}>
          <span className="sr-dim-fid-label">测得</span><FidelityMeter score={measured} label="作品在这一维测得的平均" />
        </span>
      )}
      {judged != null && (
        <span className="sr-dim-fid-item" title={`这一维评审的平均（${fidelity.judged || 0} 次评审）`}>
          <span className="sr-dim-fid-label">评审</span><FidelityMeter score={judged} label="作品在这一维评审的平均" />
        </span>
      )}
      {hasGaps && <Tag tone="warn" title={gaps.map((g) => g.phrase).join("；")}>近期常见偏差</Tag>}
    </span>
  );
}

/* 一维展开后：作品在这一维的平均与偏差的原话 */
export function SrDimensionFidelityBody({ fidelity, gaps, workTitle }) {
  const measured = fidelity && fidelity.deterministic != null ? fidelity.deterministic : null;
  const judged = fidelity && fidelity.judge != null ? fidelity.judge : null;
  const list = Array.isArray(gaps) ? gaps : [];
  if (measured == null && judged == null && !list.length) return null;
  const parts = [
    measured != null ? `测得平均 ${fidScoreText(measured)} / 10（${fidelity.scenes || 0} 场终稿）` : null,
    judged != null ? `评审平均 ${fidScoreText(judged)} / 10（${fidelity.judged || 0} 次评审）` : null,
  ].filter(Boolean);
  return (
    <div className="sr-dim-section sr-dim-fid-body" data-testid="sr-dim-fid-body">
      <div className="sr-dim-section-label">《{workTitle}》在这一维</div>
      {parts.length > 0 && <p className="sr-ov-text">{parts.join("；")}</p>}
      {list.map((gap) => (
        <p key={gap.phrase} className="sr-ov-text sr-dim-fid-gap">近期常见偏差：{gap.phrase}（最近 {gap.window || 5} 次首稿里 {gap.hits} 次）</p>
      ))}
    </div>
  );
}
